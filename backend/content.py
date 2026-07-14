"""Public server-rendered content engine — powers /learn and /learn/{slug}.

Generalizes the /privacy render (backend/main.py): every ``docs/content/*.md``
file becomes a fully server-rendered, crawlable HTML page — present in the raw
HTML with NO JavaScript, which is the whole point (AI crawlers can't see the
auth-gated product or client-rendered SPA content). Built ONCE at startup.

Per-file front-matter (``---`` delimited, simple ``key: value`` lines):
    title, description, date (YYYY-MM-DD), slug (defaults to the filename).

Each page carries its OWN <title>, meta description, canonical (/learn/{slug} —
never the homepage's), and Article JSON-LD. The routes + sitemap wiring live in
main.py, registered before the SPA catch-all.
"""
from __future__ import annotations

import html
import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

SITE_ORIGIN = "https://rookff.com"
AUTHOR = "Rook Fantasy Football LLC"
LOGO = f"{SITE_ORIGIN}/rook-lockup.png"
CONTENT_DIR = Path(__file__).resolve().parent.parent / "docs" / "content"

# Dark theme matching the app (surface-0 bg, slate text, brand-accent links) so a
# human who lands on a crawlable page doesn't see a raw markdown dump.
_CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { max-width: 44rem; margin: 0 auto; padding: 3rem 1.25rem 5rem;
  font: 17px/1.7 system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  color: #cbd5e1; background: #0f1117; -webkit-font-smoothing: antialiased; }
a { color: #8190e6; text-decoration: none; }
a:hover { text-decoration: underline; }
h1 { color: #f1f5f9; font-size: 2rem; line-height: 1.2; letter-spacing: -.01em; margin: 0 0 1rem; }
h2 { color: #f1f5f9; font-size: 1.4rem; margin: 2.25rem 0 .6rem; }
h3 { color: #e2e8f0; font-size: 1.15rem; margin: 1.5rem 0 .4rem; }
strong { color: #f1f5f9; }
code { background: #1c1f2e; color: #e2e8f0; padding: .12em .4em; border-radius: 4px; font-size: .9em; }
pre { background: #161822; border: 1px solid #2d3148; border-radius: 8px; padding: 1rem; overflow: auto; }
pre code { background: none; padding: 0; }
blockquote { border-left: 3px solid #2a3d8f; margin: 1.25rem 0; padding: .25rem 0 .25rem 1.1rem; color: #94a3b8; }
hr { border: none; border-top: 1px solid #2d3148; margin: 2.5rem 0; }
table { border-collapse: collapse; width: 100%; margin: 1.25rem 0; font-size: .95rem; }
th, td { border: 1px solid #2d3148; padding: .5rem .7rem; text-align: left; vertical-align: top; }
th { background: #161822; color: #f1f5f9; }
.nav { margin-bottom: 2rem; font-size: .9rem; color: #64748b; }
.meta { color: #64748b; font-size: .9rem; margin: -.4rem 0 2rem; }
.card { display: block; border: 1px solid #2d3148; background: #161822; border-radius: 10px;
  padding: 1.1rem 1.25rem; margin: .85rem 0; }
.card:hover { border-color: #3a3f5c; text-decoration: none; }
.card h2 { margin: .1rem 0 .35rem; font-size: 1.15rem; }
.card p { margin: 0; color: #94a3b8; font-size: .95rem; }
footer { margin-top: 3.5rem; padding-top: 1.5rem; border-top: 1px solid #2d3148; color: #64748b; font-size: .85rem; }
""".strip()


@dataclass(frozen=True)
class Article:
    slug: str
    title: str
    description: str
    date: str      # ISO YYYY-MM-DD (from front-matter)
    html: str      # full rendered page


def _parse_front_matter(raw: str) -> tuple[dict, str]:
    """Split leading ``---`` front-matter (simple key: value) from the body.
    Returns (meta, body_markdown). No YAML dependency — one level, string values."""
    meta: dict[str, str] = {}
    text = raw.lstrip()
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            block = text[3:end]
            body = text[end + 4:].lstrip("\n")
            for line in block.splitlines():
                line = line.strip()
                if not line or ":" not in line:
                    continue
                key, _, val = line.partition(":")
                meta[key.strip().lower()] = val.strip().strip('"').strip("'")
            return meta, body
    return meta, raw


def _shell(*, title: str, description: str, canonical: str, json_ld: dict,
           nav_html: str, body_html: str) -> str:
    """Wrap rendered body in a full, self-contained crawlable HTML page."""
    t = html.escape(title)
    d = html.escape(description)
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{t} — Rook</title>"
        f'<meta name="description" content="{d}">'
        f'<link rel="canonical" href="{canonical}">'
        '<meta property="og:type" content="article">'
        f'<meta property="og:title" content="{t}">'
        f'<meta property="og:description" content="{d}">'
        f'<meta property="og:url" content="{canonical}">'
        f"<style>{_CSS}</style>"
        f'<script type="application/ld+json">{json.dumps(json_ld)}</script>'
        "</head><body>"
        f'<nav class="nav">{nav_html}</nav>'
        f"<main>{body_html}</main>"
        '<footer>© Rook Fantasy Football LLC · '
        '<a href="/">rookff.com</a> · <a href="/privacy">Privacy</a></footer>'
        "</body></html>"
    )


def _render_article(meta: dict, body_md: str, filename: str) -> Article:
    import markdown

    slug = meta.get("slug") or filename
    title = meta.get("title") or slug
    description = meta.get("description", "")
    date = meta.get("date", "")
    canonical = f"{SITE_ORIGIN}/learn/{slug}"
    body_html = markdown.markdown(
        body_md, extensions=["tables", "fenced_code", "sane_lists"]
    )
    json_ld = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": title,
        "description": description,
        "datePublished": date,
        "author": {"@type": "Organization", "name": AUTHOR},
        "publisher": {
            "@type": "Organization",
            "name": AUTHOR,
            "logo": {"@type": "ImageObject", "url": LOGO},
        },
        "mainEntityOfPage": {"@type": "WebPage", "@id": canonical},
    }
    nav = '<a href="/learn">← Rook Learn</a> · <a href="https://rookff.com/">rookff.com</a>'
    page = _shell(
        title=title, description=description, canonical=canonical, json_ld=json_ld,
        nav_html=nav,
        body_html=(
            f"<h1>{html.escape(title)}</h1>"
            f'<div class="meta">Published {html.escape(date)} · {AUTHOR}</div>'
            f"{body_html}"
        ),
    )
    return Article(slug=slug, title=title, description=description, date=date, html=page)


def _render_index(articles: list[Article]) -> str:
    canonical = f"{SITE_ORIGIN}/learn"
    cards = "".join(
        f'<a class="card" href="/learn/{html.escape(a.slug)}">'
        f"<h2>{html.escape(a.title)}</h2><p>{html.escape(a.description)}</p></a>"
        for a in articles
    )
    if not cards:
        cards = '<p class="meta">No articles published yet.</p>'
    json_ld = {
        "@context": "https://schema.org",
        "@type": "CollectionPage",
        "name": "Rook Learn",
        "description": "Explainers on how Rook values fantasy football players and finds mispriced trades, waivers, and draft picks.",
        "url": canonical,
    }
    return _shell(
        title="Rook Learn",
        description="Explainers on how Rook values fantasy football players and finds mispriced trades, waivers, and draft picks.",
        canonical=canonical, json_ld=json_ld,
        nav_html='<a href="https://rookff.com/">← rookff.com</a>',
        body_html=(
            "<h1>Rook Learn</h1>"
            '<p class="meta">How Rook reasons about fantasy football value — '
            "server-rendered so it can be read without an account.</p>"
            f"{cards}"
        ),
    )


def load_content() -> tuple[dict[str, str], str, list[Article]]:
    """Load docs/content/*.md → ({slug: page_html}, index_html, [Article,...]).
    Defensive: an unreadable file is skipped (logged), never crashes boot."""
    articles: list[Article] = []
    if CONTENT_DIR.is_dir():
        for md in sorted(CONTENT_DIR.glob("*.md")):
            try:
                meta, body = _parse_front_matter(md.read_text(encoding="utf-8"))
                articles.append(_render_article(meta, body, md.stem))
            except Exception as exc:  # one bad file must not take down the site
                logger.error("Content page %s failed to render: %s", md.name, exc)
    # Newest first when dates are present.
    articles.sort(key=lambda a: a.date, reverse=True)
    pages = {a.slug: a.html for a in articles}
    return pages, _render_index(articles), articles
