"""Build the app's favicons / touch icons from clean standalone sources.

Two independent sources (NOT the old contact sheet, which produced clipped big
icons and blurry small ones):

  * rook.png  (repo root, NOT committed) — clean full-res mascot on a
    transparent background. Source for the BIG icons (512 / 192 / 180). We trim
    to the alpha bbox and re-pad to a centered square with an even gutter so the
    mascot is never off-center or clipped.

  * the "R" glyph — a tiny rounded-square monogram. Source for the SMALL
    favicons (16 / 32 / .ico). A detailed mascot is unreadable at 16px; a bold
    monogram stays crisp. We also (over)write favicon.svg with the exact glyph so
    the vector icon and the rasters match. The glyph is rasterized with Pillow
    directly (disk-stamped strokes) — no native SVG/cairo dependency, which keeps
    this runnable on Windows via `uv run --with pillow`.

Run (Pillow is not a project dep — inject it ephemerally):
    uv run --with pillow python scripts/build_favicons.py

Writes frontend/public/_debug_crops/hero_squared.png for visual review; delete
that folder once verified (it is not committed).
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ROOK = ROOT / "rook.png"
OUT = ROOT / "frontend" / "public"
DEBUG = OUT / "_debug_crops"

# Even transparent gutter around the mascot's alpha bbox, per side, so the big
# icons read as centered with a small consistent margin (not edge-to-edge).
MARGIN = 0.07

# Brand navy of the monogram tile (matches favicon.svg / the manifest theme).
NAVY = (42, 61, 143, 255)  # #2a3d8f

# Exact favicon.svg the small rasters are derived from (kept in sync on disk).
FAVICON_SVG = """\
<svg width="100" height="100" viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">
  <rect width="100" height="100" rx="23" fill="#2a3d8f"/>
  <path d="M37 26 L37 74 M37 26 L64 26 Q79 26 79 40 Q79 54 64 54 L37 54 M59 54 L80 74"
        fill="none" stroke="#ffffff" stroke-width="11" stroke-linecap="round" stroke-linejoin="round"/>
</svg>
"""


# --------------------------------------------------------------------------- #
# Big icons — from rook.png
# --------------------------------------------------------------------------- #
def square_with_margin(im: Image.Image, margin: float) -> Image.Image:
    """Trim to the alpha bbox, then center on a transparent square whose side
    leaves `margin` of even gutter on the mascot's dominant dimension."""
    im = im.convert("RGBA")
    bbox = im.getbbox()  # bounds of all non-zero (here: non-transparent) pixels
    if bbox:
        im = im.crop(bbox)
    w, h = im.size
    side = round(max(w, h) / (1 - 2 * margin))
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(im, ((side - w) // 2, (side - h) // 2), im)
    return canvas


# --------------------------------------------------------------------------- #
# Small favicons — rasterize the "R" glyph with Pillow (no cairo dependency)
# --------------------------------------------------------------------------- #
def _quad(p0, p1, p2, n=48):
    """Sample a quadratic bezier (matches the SVG 'Q' commands)."""
    pts = []
    for i in range(n + 1):
        t = i / n
        u = 1 - t
        x = u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0]
        y = u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1]
        pts.append((x, y))
    return pts


def _densify(pts, step=0.4):
    """Insert points along straight segments so a disk-stamped stroke is solid."""
    out = []
    for a, b in zip(pts, pts[1:]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        dist = (dx * dx + dy * dy) ** 0.5
        n = max(1, int(dist / step))
        for i in range(n):
            out.append((a[0] + dx * i / n, a[1] + dy * i / n))
    out.append(pts[-1])
    return out


def render_glyph(px: int) -> Image.Image:
    """Render the R monogram at `px` square. Strokes are drawn by stamping discs
    of diameter = stroke-width along densely-sampled subpaths, which yields exact
    round caps + round joins (SVG stroke-linecap/linejoin='round')."""
    s = px / 100.0  # SVG user units are a 100x100 viewBox
    img = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Rounded-square background: rect width=100 rx=23.
    d.rounded_rectangle([0, 0, px - 1, px - 1], radius=23 * s, fill=NAVY)

    # The three subpaths of the "R" (stem, bowl, leg), in SVG user units.
    stem = [(37, 26), (37, 74)]
    bowl = (
        [(37, 26), (64, 26)]
        + _quad((64, 26), (79, 26), (79, 40))
        + _quad((79, 40), (79, 54), (64, 54))
        + [(37, 54)]
    )
    leg = [(59, 54), (80, 74)]

    r = (11 * s) / 2.0  # stroke half-width
    white = (255, 255, 255, 255)
    for sub in (stem, bowl, leg):
        for x, y in _densify(sub):
            cx, cy = x * s, y * s
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=white)
    return img


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)

    # --- Big icons from rook.png ------------------------------------------- #
    rook = Image.open(ROOK).convert("RGBA")
    print(f"source (big): {ROOK.name}  {rook.size}")
    hero = square_with_margin(rook, MARGIN)
    print(f"  squared master: {hero.size}  (margin {MARGIN:.0%}/side)")
    hero.save(DEBUG / "hero_squared.png")

    for size, fname in (
        (512, "android-chrome-512x512.png"),
        (192, "android-chrome-192x192.png"),
        (180, "apple-touch-icon.png"),
    ):
        hero.resize((size, size), Image.LANCZOS).save(OUT / fname)

    # --- favicon.svg (exact glyph) ----------------------------------------- #
    (OUT / "favicon.svg").write_text(FAVICON_SVG, encoding="utf-8")
    print("  wrote favicon.svg (R monogram)")

    # --- Small favicons rasterized from the glyph -------------------------- #
    master = render_glyph(256)
    master.resize((32, 32), Image.LANCZOS).save(OUT / "favicon-32x32.png")
    master.resize((16, 16), Image.LANCZOS).save(OUT / "favicon-16x16.png")
    master.resize((48, 48), Image.LANCZOS).save(
        OUT / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)]
    )

    print("\nwrote:")
    for f in (
        "android-chrome-512x512.png", "android-chrome-192x192.png",
        "apple-touch-icon.png", "favicon.svg", "favicon-32x32.png",
        "favicon-16x16.png", "favicon.ico",
    ):
        print(f"  frontend/public/{f}")
    print(f"\nReview {DEBUG / 'hero_squared.png'} then delete _debug_crops/ (not committed).")


if __name__ == "__main__":
    main()
