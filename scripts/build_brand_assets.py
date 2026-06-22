"""Generate brand previews (header lockup + OG social image) for review.

Sources, all already in the repo / brand system:
  * the "R" glyph — same navy rounded tile + white monogram as favicon.svg
    (rasterized here with Pillow disc-stamped strokes, no SVG/cairo dep).
  * rook.png (repo root, gitignored) — the mascot, for the OG image.
  * brand navy #2a3d8f.

Committed outputs (frontend/public/):
    og-image.png      1200x630 social-share card (mascot + wordmark + tagline)
    rook-mascot.png   the mascot, white card keyed to transparent (hero use)
    rook-lockup.png   horizontal lockup, white wordmark on transparent (Clerk)

Run:
    uv run --with pillow python scripts/build_brand_assets.py

The wordmark is set in Segoe UI Bold — on Windows that IS the `system-ui`
font the in-app text wordmark renders in, so the baked assets match the live
<Logo> component's text wordmark.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
ROOK = ROOT / "rook.png"
OUT = ROOT / "frontend" / "public"

NAVY = (42, 61, 143, 255)          # #2a3d8f  brand core
NAVY_DEEP = (31, 45, 107, 255)     # #1f2d6b  gradient bottom / og
NAVY_LIGHT = (52, 73, 159, 255)    # #34499f  mascot backing
WHITE = (255, 255, 255, 255)

# Segoe UI Bold = Windows system-ui bold (matches the in-app text wordmark).
FONT_CANDIDATES = [
    r"C:\Windows\Fonts\segoeuib.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
]


def font(size: int) -> ImageFont.FreeTypeFont:
    for p in FONT_CANDIDATES:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


# --- R glyph (navy rounded tile + white monogram) -------------------------- #
def _quad(p0, p1, p2, n=48):
    pts = []
    for i in range(n + 1):
        t = i / n
        u = 1 - t
        pts.append((u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0],
                    u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1]))
    return pts


def _densify(pts, step=0.4):
    out = []
    for a, b in zip(pts, pts[1:]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        dist = (dx * dx + dy * dy) ** 0.5
        n = max(1, int(dist / step))
        for i in range(n):
            out.append((a[0] + dx * i / n, a[1] + dy * i / n))
    out.append(pts[-1])
    return out


def glyph(px: int) -> Image.Image:
    s = px / 100.0
    img = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, px - 1, px - 1], radius=23 * s, fill=NAVY)
    stem = [(37, 26), (37, 74)]
    bowl = ([(37, 26), (64, 26)] + _quad((64, 26), (79, 26), (79, 40))
            + _quad((79, 40), (79, 54), (64, 54)) + [(37, 54)])
    leg = [(59, 54), (80, 74)]
    r = (11 * s) / 2.0
    for sub in (stem, bowl, leg):
        for x, y in _densify(sub):
            d.ellipse([x * s - r, y * s - r, x * s + r, y * s + r], fill=WHITE)
    return img


# --- Horizontal lockup: [glyph] Rook --------------------------------------- #
def lockup(h: int, wordmark_rgb) -> Image.Image:
    g = glyph(h)
    gap = round(h * 0.30)
    fnt = font(round(h * 0.82))
    text = "Rook"
    tmp = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    l, t, r, b = tmp.textbbox((0, 0), text, font=fnt)
    tw, th = r - l, b - t
    W = h + gap + tw
    img = Image.new("RGBA", (W, h), (0, 0, 0, 0))
    img.paste(g, (0, 0), g)
    d = ImageDraw.Draw(img)
    # vertically center the cap-height of the wordmark against the tile
    ty = (h - th) // 2 - t
    d.text((h + gap, ty), text, font=fnt, fill=wordmark_rgb)
    return img


def on_swatch(lk: Image.Image, bg, pad_x=40, pad_y=28) -> Image.Image:
    c = Image.new("RGBA", (lk.width + pad_x * 2, lk.height + pad_y * 2), bg)
    c.alpha_composite(lk, (pad_x, pad_y))
    return c


def mascot_cutout() -> Image.Image:
    """rook.png is the mascot on an opaque WHITE card. Flood-fill the EXTERIOR
    white to transparent from the borders — the cartoon's solid black outlines
    stop the fill, so interior whites (helmet stripe, eyes) are preserved."""
    im = Image.open(ROOK).convert("RGBA")
    w, h = im.size
    seeds = [(1, 1), (w - 2, 1), (1, h - 2), (w - 2, h - 2),
             (w // 2, 1), (w // 2, h - 2), (1, h // 2), (w - 2, h // 2)]
    for xy in seeds:
        ImageDraw.floodfill(im, xy, (0, 0, 0, 0), thresh=60)
    bb = im.getbbox()
    return im.crop(bb) if bb else im


# --- OG social image 1200x630 ---------------------------------------------- #
def _fit_font(draw, text, max_w, start, lo=22):
    for size in range(start, lo - 1, -2):
        f = font(size)
        if draw.textlength(text, font=f) <= max_w:
            return f
    return font(lo)


def og_image() -> Image.Image:
    W, H = 1200, 630
    img = Image.new("RGBA", (W, H), NAVY)
    # vertical navy gradient for depth
    top, bot = NAVY, NAVY_DEEP
    for y in range(H):
        f = y / (H - 1)
        img.paste(tuple(round(top[i] + (bot[i] - top[i]) * f) for i in range(3)) + (255,),
                  (0, y, W, y + 1))
    d = ImageDraw.Draw(img)

    # mascot (white card keyed out) on a soft lighter-navy disc for separation
    mascot = mascot_cutout()
    target_h = 430
    scale = target_h / mascot.height
    mascot = mascot.resize((round(mascot.width * scale), target_h), Image.LANCZOS)
    mx, my = 90, (H - mascot.height) // 2
    cx, cy = mx + mascot.width // 2, my + mascot.height // 2
    rad = int(target_h * 0.62)
    disc = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(disc).ellipse([cx - rad, cy - rad, cx + rad, cy + rad], fill=NAVY_LIGHT)
    img.alpha_composite(disc)
    img.alpha_composite(mascot, (mx, my))

    # right column: wordmark + tagline (tagline auto-fit so it never clips)
    tx = 610
    right_margin = 56
    max_w = W - tx - right_margin
    d.text((tx, 212), "Rook", font=font(150), fill=WHITE)
    tag = _fit_font(d, "Win your fantasy league with AI.", max_w, start=40)
    d.text((tx + 4, 392), "Win your fantasy league with AI.", font=tag,
           fill=(214, 221, 245, 255))
    return img


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    og_image().save(OUT / "og-image.png")
    mascot_cutout().save(OUT / "rook-mascot.png")          # hero (transparent)
    lockup(120, WHITE).save(OUT / "rook-lockup.png")       # Clerk auth logo
    print("wrote committed brand assets to", OUT)
    for f in ("og-image.png", "rook-mascot.png", "rook-lockup.png"):
        print("  ", f)


if __name__ == "__main__":
    main()
