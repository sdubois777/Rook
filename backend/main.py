import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response,
)
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

    # Weekly full sweep — keeps season-long metrics current. Tuesday ~09:00 UTC
    # (≈4-5am ET): post-MNF, post-Tuesday injury reports, BEFORE Tue/Wed waiver
    # processing. Draft-window-gated + run OFF the event loop (subprocess), per the
    # load-test lesson (heavy work must never compete with the serving event loop).
    _scheduler.add_job(
        _weekly_full_sweep,
        "cron",
        day_of_week=settings.weekly_sweep_day_of_week,
        hour=settings.weekly_sweep_hour_utc,
        id="weekly_full_sweep",
        replace_existing=True,
    )

    _scheduler.start()
    logger.info("Beat Reporter scheduler started (daily at 7am)")
    logger.info("Stale draft-session reaper registered (every 30 min)")
    logger.info(
        "Weekly full sweep registered (%s %02d:00 UTC, draft-window-gated)",
        settings.weekly_sweep_day_of_week, settings.weekly_sweep_hour_utc,
    )

    # Cross-process WebSocket bus (Postgres LISTEN/NOTIFY). One dedicated LISTEN
    # connection per process; the loop reconnects if Railway drops it. Harmless in
    # the single-process deploy today (a process ignores its own publications) and
    # already correct the moment a second process is added.
    from backend.websocket.manager import pubsub
    await pubsub.start()
    logger.info("WebSocket pub/sub bus started (Postgres LISTEN/NOTIFY)")


@app.on_event("shutdown")
async def shutdown_checks():
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Beat Reporter scheduler stopped")

    from backend.websocket.manager import pubsub
    await pubsub.stop()
    logger.info("WebSocket pub/sub bus stopped")


# Abandoned-draft safety TTL. Set to comfortably EXCEED a real draft's wall-clock
# (a 12-team/15-round snake at a 90s clock is ~4.5h, auctions longer, plus pauses),
# so the reaper never cold-starts a live-but-paused draft mid-draft. The load-test
# follow-up showed the resident-engine pile-up is mostly FINISHED drafts — those are
# now evicted immediately (see evict_finished_and_stale), so a LONGER abandon TTL
# costs little memory while removing all risk of evicting a live draft.
_DRAFT_SESSION_TTL_SECONDS = int(
    os.environ.get("DRAFT_SESSION_SAFETY_TTL_SECONDS", str(8 * 60 * 60))
)


async def _evict_stale_draft_sessions():
    """Reap draft sessions: evict FINISHED (board-full) warm engines immediately
    plus ABANDONED (idle beyond the safety TTL) ones, and durably deactivate the DB
    rows (incl. cold rows never held warm here). Distinguishing finished from
    merely-idle-live is the fix for the warm-engine pile-up the load test found;
    live/paused drafts stay warm until the long safety TTL."""
    from backend.routers.draft import session_manager

    reaped = await session_manager.evict_finished_and_stale(_DRAFT_SESSION_TTL_SECONDS)
    deactivated = await session_manager.deactivate_stale_rows(_DRAFT_SESSION_TTL_SECONDS)
    if reaped["finished"] or reaped["abandoned"] or deactivated:
        logger.info(
            "Reaper: evicted %d finished + %d abandoned warm session(s), "
            "deactivated %d idle DB row(s)",
            reaped["finished"], reaped["abandoned"], deactivated,
        )


async def _weekly_full_sweep():
    """Scheduled full-board DIRTY refresh (keeps availability/schedule/valuation and
    materially-changed profiles current). DRAFT-WINDOW-GATED: if a draft is live or
    imminent it defers and retries in an hour rather than compete for the pool/CPU
    (the load test's single biggest self-inflicted risk). Runs as a SUBPROCESS so
    the heavy pass never blocks the serving event loop."""
    import asyncio
    import sys
    from datetime import datetime, timedelta, timezone

    from backend.database import AsyncSessionLocal
    from backend.services.pipeline_triggers import is_draft_window_active

    async with AsyncSessionLocal() as db:
        active, reason = await is_draft_window_active(db)
    if active:
        retry_at = datetime.now(timezone.utc) + timedelta(hours=1)
        logger.warning(
            "Weekly full sweep DEFERRED — draft window active: %s. Retrying at %s.",
            reason, retry_at.isoformat(),
        )
        if _scheduler is not None:
            _scheduler.add_job(
                _weekly_full_sweep, "date", run_date=retry_at,
                id="weekly_sweep_retry", replace_existing=True,
            )
        return

    logger.info("Weekly full sweep starting (dirty-only, off-process subprocess)")
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "scripts/run_predraft_pipeline.py", "--skip-seed",
        )
        await proc.wait()
        logger.info("Weekly full sweep finished (exit %s)", proc.returncode)
    except Exception:
        logger.exception("Weekly full sweep failed to run")


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
# SEO / AEO — robots.txt, sitemap.xml, llms.txt.
# ---------------------------------------------------------------------------
# Server-rendered like /privacy and registered BEFORE the SPA catch-all so they
# return real files, not the SPA shell. We WANT AI crawlers (the product is
# behind auth; only these public surfaces are citable). Prices in llms.txt derive
# from user.py — never a second hardcoded copy. Content-only; no auth.
_SITE_ORIGIN = "https://rookff.com"
_SEO_CACHE = {"Cache-Control": "public, max-age=86400"}

# Public, crawlable URLs. Tier 2 appends /learn/* here — nothing else changes.
_SITEMAP_URLS: list[tuple[str, str, str]] = [
    ("/", "weekly", "1.0"),
    ("/pricing", "monthly", "0.7"),
    ("/privacy", "yearly", "0.3"),
]

# AI crawlers explicitly named (welcomed); auth-gated app routes disallowed for
# everyone (a login wall isn't worth crawling). Stacked UA lines share the rules.
_ROBOTS_TXT = """# rookff.com — AI crawlers welcome; we want to be cited.
User-agent: GPTBot
User-agent: OAI-SearchBot
User-agent: ChatGPT-User
User-agent: ClaudeBot
User-agent: Claude-Web
User-agent: PerplexityBot
User-agent: Google-Extended
User-agent: Googlebot
User-agent: Bingbot
User-agent: *
Allow: /
Disallow: /dashboard
Disallow: /teams
Disallow: /news
Disallow: /draftboard
Disallow: /draft-room
Disallow: /account
Disallow: /trade
Disallow: /waiver
Disallow: /matchup
Disallow: /league-setup
Disallow: /sign-in
Disallow: /sign-up
Disallow: /api/

Sitemap: https://rookff.com/sitemap.xml
"""


def _render_sitemap() -> str:
    urls = "".join(
        f"<url><loc>{_SITE_ORIGIN}{path}</loc>"
        f"<changefreq>{cf}</changefreq><priority>{pri}</priority></url>"
        for path, cf, pri in _SITEMAP_URLS
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{urls}</urlset>"
    )


def _render_llms_txt() -> str:
    """Concise, answer-first site summary for AI crawlers. Prices derive from
    user.py (single source). The valuation numbers are real engine records
    (docs/kdef_streaming_baseline_report.md) and the landing-page backtest set."""
    from backend.models.user import TIER_LIMITS as T
    std, pro = T["standard"], T["pro"]
    return f"""# Rook — AI Fantasy Football Manager

> Rook (https://rookff.com) is an AI-powered fantasy football platform that builds
> every player's value from in-season usage — and the causes behind it — then finds
> the trades, waivers, and draft picks your league is mispricing. It works with
> Yahoo, ESPN, and Sleeper, for both auction and snake drafts.

## What Rook does
- Values players from in-season usage trajectory (snap share, target share, opportunity),
  not preseason consensus rankings.
- Grounds every call in YOUR league: its history, scoring settings, and your opponents' tendencies.
- Trade analyzer, trade finder, waiver-wire recommendations, lineup start/sit, injury monitoring,
  and a live draft assistant that reads the draft room in real time.

## Key differentiator — replacement-level (VOR) valuation
Most trade calculators value a good defense like a mid-tier skill player because they use
ABSOLUTE projected points with no replacement adjustment. Rook's own engine had this bug: the
top defense — the Minnesota Vikings (13.32 fantasy points/game) — was valued 31.5 on Rook's
0–100 trade scale, IDENTICAL to Justin Jefferson at 31.5. A streamable defense priced like an
elite WR. The fix: a league-local, waiver-aware Value Over Replacement using a streaming
baseline (the mean of the top-3 waiver defenses, ~11.1 pts/game — what you can actually stream
this week). After the fix, that same Vikings defense values 2.4 — correctly near-zero, because
you can replace it off waivers for nearly the same points. This is why a streamable defense is
not worth an elite tight end.

## 2025 backtest (a backtest — not live results)
Trained on pre-2025 data, projected 2025, then scored against actual 2025 results:
74.1% signal accuracy, 93% buy-signal accuracy, 87% of top opportunities identified (13 of 15),
and 0.88 correlation between projected and actual PPR points.

## Platforms and formats
Yahoo Fantasy, ESPN, and Sleeper. Auction and snake drafts. One-click league import.

## Pricing
- Free: $0/month — metered free tier; 30 credits at signup; player values, draft board, waiver
  browse, start/sit, and injury monitoring are always free.
- Standard: ${std['price_monthly_usd']}/month or ${std['price_season_usd']}/season — unlimited,
  no credits; includes the live draft assistant.
- Pro: ${pro['price_monthly_usd']}/month or ${pro['price_season_usd']}/season — everything in
  Standard plus unlimited leagues and a cross-league view.

## Links
- Home: https://rookff.com/
- Pricing: https://rookff.com/pricing
- Privacy: https://rookff.com/privacy

## Contact
Rook Fantasy Football LLC — rookadmin@rookff.com
"""


# Content engine (/learn) — docs/content/*.md → server-rendered crawlable pages,
# built once at startup. Loaded BEFORE the sitemap so its URLs are included (the
# Tier-1 sitemap was built list-based for exactly this — we extend, not rewrite).
from backend.content import load_content

_LEARN_PAGES, _LEARN_INDEX_HTML, _LEARN_ARTICLES = load_content()
_SITEMAP_URLS.append(("/learn", "weekly", "0.6"))
for _article in _LEARN_ARTICLES:
    _SITEMAP_URLS.append((f"/learn/{_article.slug}", "monthly", "0.8"))

_SITEMAP_XML = _render_sitemap()
_LLMS_TXT = _render_llms_txt()


@app.get("/robots.txt", include_in_schema=False)
async def robots_txt():
    return PlainTextResponse(_ROBOTS_TXT, headers=_SEO_CACHE)


@app.get("/sitemap.xml", include_in_schema=False)
async def sitemap_xml():
    return Response(_SITEMAP_XML, media_type="application/xml", headers=_SEO_CACHE)


@app.get("/llms.txt", include_in_schema=False)
async def llms_txt():
    return PlainTextResponse(_LLMS_TXT, headers=_SEO_CACHE)


# Content engine routes — registered BEFORE the SPA catch-all so /learn* returns
# real server-rendered HTML, not the SPA shell.
@app.get("/learn", include_in_schema=False)
async def learn_index():
    return HTMLResponse(_LEARN_INDEX_HTML, headers=_SEO_CACHE)


@app.get("/learn/{slug}", include_in_schema=False)
async def learn_article(slug: str):
    page = _LEARN_PAGES.get(slug)
    if page is None:
        raise HTTPException(status_code=404)
    return HTMLResponse(page, headers=_SEO_CACHE)


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
