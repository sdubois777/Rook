import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.config import settings
from backend.core.exceptions import AppError
from backend.middleware.security_headers import SecurityHeadersMiddleware
from backend.middleware.request_logging import RequestLoggingMiddleware
from backend.routers import admin, auth, draft, draftboard, league, league_connect, news, pipeline, players, preferences, teams
from backend.routers import account, billing, matchup, trade, waiver, webhooks
from backend.websocket.manager import news_ws_manager

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Rook",
    version="0.1.0",
    description="Rook — AI-powered fantasy football management system",
)

# Middleware — order matters (outermost first, innermost last)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestLoggingMiddleware)

# Browser-extension origins (moz-extension://<id>, chrome-extension://<id>)
# carry per-install random IDs, so they cannot be listed explicitly —
# a regex is the only way to allow them alongside the web origins.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=(
        r"moz-extension://.*"
        r"|chrome-extension://.*"
        r"|http://localhost(:\d+)?"
        r"|https://fantasymanager-production"
        r"\.up\.railway\.app"
        r"|https://(www\.)?rookff\.com"
        r"|https://football\.fantasysports\.yahoo\.com"
        r"|https://fantasysports\.yahoo\.com"
        r"|https://.*\.yahoo\.com"
        r"|https://fantasy\.espn\.com"
        r"|https://sleeper\.com"
        r"|https://sleeper\.app"
    ),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"],
    allow_headers=["X-Draft-Token", "Authorization", "Content-Type"],
)


# ── Exception handlers ─────────────────────────────────────

@app.exception_handler(AppError)
async def app_error_handler(request, exc: AppError):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": exc.error_code,
            "message": exc.message,
            **exc.detail,
        },
    )


# ── Routers ─────────────────────────────────────────────────

# All API routers are served under /api so the root path namespace is free for
# the SPA (React Router paths like /draftboard, /account no longer collide with
# same-named API routers). The browser-extension and frontend clients both call
# /api/* now. EXCEPTION: webhooks stays at /webhooks — it's Clerk's configured
# endpoint (external consumer, no frontend collision); moving it would require a
# coordinated Clerk dashboard change.
for _router in (
    admin.router,
    auth.router,
    draft.router,
    draftboard.router,
    league.router,
    news.router,
    pipeline.router,
    players.router,
    preferences.router,
    teams.router,
    account.router,
    billing.router,
    billing.public_router,   # unauthenticated GET /billing/pricing
    league_connect.router,
    trade.router,
    waiver.router,
    matchup.router,
):
    app.include_router(_router, prefix="/api")

app.include_router(webhooks.router)  # /webhooks/{clerk,stripe} — external-configured, stay at root

_scheduler = None


@app.on_event("startup")
async def startup_checks():
    global _scheduler

    # Fail loudly at boot: production must never run without Clerk auth.
    # Without this, the first authenticated request would 401 hours later.
    if settings.environment == "production" and not settings.clerk_enabled:
        raise RuntimeError(
            "CLERK_SECRET_KEY not configured — refusing to start in production"
        )

    missing = []
    if not settings.yahoo_client_id:
        missing.append("YAHOO_CLIENT_ID")
    if not settings.yahoo_client_secret:
        missing.append("YAHOO_CLIENT_SECRET")
    if not settings.yahoo_league_id:
        missing.append("YAHOO_LEAGUE_ID")
    if not settings.yahoo_refresh_token:
        missing.append("YAHOO_REFRESH_TOKEN")
    if not settings.rapidapi_key:
        missing.append("RAPIDAPI_KEY")
    if missing:
        logger.warning("Optional settings not configured: %s", missing)
    logger.info(
        "App started — environment=%s, %d optional setting(s) not configured",
        settings.environment,
        len(missing),
    )

    # Start Beat Reporter daily scheduler (7am cron)
    from backend.agents.beat_reporter import setup_scheduler
    _scheduler = setup_scheduler()

    # (Monthly credit grants DELETED with the tier/credit spec: paid tiers are
    # unlimited — credits exist only as the free tier's meter + top-up packs.)

    # Evict idle warm draft sessions (memory reaper). The durable DB snapshot is
    # left intact, so an evicted-then-resumed draft still rehydrates.
    _scheduler.add_job(
        _evict_stale_draft_sessions,
        "interval",
        minutes=30,
        id="evict_stale_draft_sessions",
        replace_existing=True,
    )

    _scheduler.start()
    logger.info("Beat Reporter scheduler started (daily at 7am)")
    logger.info("Stale draft-session reaper registered (every 30 min)")


@app.on_event("shutdown")
async def shutdown_checks():
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Beat Reporter scheduler stopped")


# Warm draft sessions idle longer than this are evicted from memory (the DB
# snapshot survives, so they rehydrate on the next event). 6h comfortably covers
# a long auction plus breaks.
_DRAFT_SESSION_TTL_SECONDS = 6 * 60 * 60


async def _evict_stale_draft_sessions():
    """Reap idle draft sessions: evict warm memory AND durably deactivate the DB
    rows (incl. cold rows never held warm here), so an abandoned draft can't stay
    is_active=True forever. The recency read-gate already shows the board for
    stale drafts; this is the durable cleanup backstop."""
    from backend.routers.draft import session_manager

    evicted = session_manager.evict_stale(_DRAFT_SESSION_TTL_SECONDS)
    deactivated = await session_manager.deactivate_stale_rows(_DRAFT_SESSION_TTL_SECONDS)
    if evicted or deactivated:
        logger.info(
            "Reaper: evicted %d warm session(s), deactivated %d DB row(s)",
            evicted, deactivated,
        )


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "environment": settings.environment,
        "version": os.environ.get("RAILWAY_GIT_COMMIT_SHA", "local")[:8],
    }


@app.websocket("/ws/news")
async def news_websocket(websocket: WebSocket):
    """Live push of new beat reporter signals."""
    await news_ws_manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        news_ws_manager.disconnect(websocket)


# ---------------------------------------------------------------------------
# Public privacy policy (/privacy) — STATIC, server-rendered, crawler-safe.
# ---------------------------------------------------------------------------
# The Chrome Web Store review bot CRAWLS this URL; it must return 200 with the
# full content for a logged-out visitor with NO JS. So it is rendered SERVER-SIDE
# here (the content is in the raw HTML response), NOT a client-only SPA route —
# and registered BEFORE the SPA catch-all below so /privacy never falls through to
# the /dashboard redirect. Source of truth is the version-controlled markdown at
# docs/business/rook-privacy-policy.md (rendered once at startup; tables/headings
# preserved). NO auth dependency — fully public.
_PRIVACY_MD = (
    Path(__file__).resolve().parent.parent / "docs" / "business" / "rook-privacy-policy.md"
)

_PRIVACY_CSS = """
:root { color-scheme: light dark; }
body { max-width: 46rem; margin: 0 auto; padding: 2.5rem 1.25rem 4rem;
  font: 16px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  color: #1a1d29; background: #fff; }
h1 { font-size: 1.9rem; margin: 0 0 .25rem; }
h2 { font-size: 1.35rem; margin: 2rem 0 .5rem; border-top: 1px solid #e5e7eb; padding-top: 1.5rem; }
h3 { font-size: 1.1rem; margin: 1.25rem 0 .4rem; }
a { color: #2563eb; }
code { background: #f1f3f5; padding: .1em .35em; border-radius: 4px; font-size: .9em; }
hr { border: none; border-top: 1px solid #e5e7eb; margin: 2rem 0; }
table { border-collapse: collapse; width: 100%; margin: 1rem 0; font-size: .95rem; }
th, td { border: 1px solid #d1d5db; padding: .5rem .65rem; text-align: left; vertical-align: top; }
th { background: #f8fafc; }
@media (prefers-color-scheme: dark) {
  body { color: #e6e8ef; background: #0f1117; }
  h2, h3, hr, th, td { border-color: #2d3148; }
  th { background: #161822; } code { background: #1c1f2e; } a { color: #7aa2ff; }
}
"""


def _render_privacy_html() -> str:
    """Render the policy markdown to a self-contained HTML page. Defensive: a
    missing/unreadable file yields a minimal valid page rather than crashing boot."""
    try:
        import markdown
        body = markdown.markdown(
            _PRIVACY_MD.read_text(encoding="utf-8"),
            extensions=["tables", "fenced_code", "sane_lists"],
        )
    except Exception as exc:  # never let the policy page take down startup
        logger.error("Privacy policy render failed: %s", exc)
        body = "<h1>Rook Privacy Policy</h1><p>Contact rookadmin@rookff.com.</p>"
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>Rook Privacy Policy</title>"
        f"<style>{_PRIVACY_CSS}</style></head><body><main>{body}</main></body></html>"
    )


_PRIVACY_HTML = _render_privacy_html()


@app.get("/privacy", include_in_schema=False)
@app.get("/privacy.html", include_in_schema=False)
async def privacy_policy():
    """Public privacy policy — no auth, full content server-rendered."""
    return HTMLResponse(
        _PRIVACY_HTML,
        headers={"Cache-Control": "public, max-age=3600"},
    )


# ---------------------------------------------------------------------------
# Frontend static file serving (production — single Railway service)
# ---------------------------------------------------------------------------

FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"

# index.html must NEVER be cached, or a browser (Firefox especially) keeps
# serving a stale shell that references an old, deleted bundle hash after a
# deploy. The hashed assets it points to CAN be cached forever (immutable),
# since their filename changes on every build.
_NO_CACHE_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}


class _ImmutableStaticFiles(StaticFiles):
    """StaticFiles that marks content-hashed assets immutable for a year."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


if FRONTEND_DIST.exists():
    # Serve /assets (content-hashed JS/CSS bundles) — safe to cache forever.
    app.mount(
        "/assets",
        _ImmutableStaticFiles(directory=FRONTEND_DIST / "assets"),
        name="assets",
    )

    # Catch-all: serve real root static files when they exist, else the SPA
    # shell (React Router handles client-side routing). With every router under
    # /api, the only non-SPA paths are /api/*, the app-level /ws + /health, the
    # docs, the static mounts, and the root-mounted /webhooks.
    _API_PREFIXES = (
        "api/", "ws/", "webhooks", "health",
        "docs", "redoc", "openapi.json", "assets/",
    )
    _DIST_ROOT = FRONTEND_DIST.resolve()

    @app.get("/{full_path:path}")
    async def serve_frontend(full_path: str):
        if full_path and any(full_path.startswith(p) for p in _API_PREFIXES):
            raise HTTPException(status_code=404)
        # Vite copies everything in frontend/public/ to the dist ROOT (favicons,
        # site.webmanifest, og-image.png, the mascot, robots.txt, …) — NOT under
        # /assets. Serve those real files with the correct content-type. Without
        # this they fall through to the SPA shell below and get index.html, which
        # is why /site.webmanifest failed to parse ("Line 1, column 1") and the
        # hero /rook-mascot.png <img> rendered broken.
        if full_path:
            candidate = (FRONTEND_DIST / full_path).resolve()
            if (
                candidate.is_relative_to(_DIST_ROOT)   # block path traversal
                and candidate.is_file()
                and candidate.name != "index.html"     # shell stays no-cache below
            ):
                # mimetypes doesn't know .webmanifest; set it so the browser
                # parses the manifest instead of rejecting/sniffing it.
                media_type = (
                    "application/manifest+json"
                    if candidate.suffix == ".webmanifest"
                    else None
                )
                return FileResponse(candidate, media_type=media_type)
        # Never cache the SPA shell — always hand the browser the latest bundle
        # reference (the assets it points to are immutable + content-hashed).
        return FileResponse(FRONTEND_DIST / "index.html", headers=_NO_CACHE_HEADERS)

else:
    @app.get("/")
    async def root():
        return {"status": "ok", "message": "API running — no frontend build found"}
