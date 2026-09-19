#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Generate assets/app.ico for Book Reader — Pillow only, no other dependencies.

Design notes
------------
* Solid rounded-square badge. A transparent-background glyph would vanish on a
  light taskbar (white glyph) or a dark one (dark glyph); a filled badge carries
  its own contrast and reads identically on both.
* Flat two-tone pages (pure white + a dimmed white) separated by a badge-coloured
  spine gap. The tone split is what makes it read as an *open book* at 16px,
  where a hairline spine alone would blur away.
* No gradients. Every size is rendered natively (geometry retuned per tier) and
  supersampled, instead of downscaling one 256px master — downscaling a detailed
  master is exactly what produces 16px mush.

Usage
-----
    python tools/make_icon.py                   # writes assets/app.ico
    python tools/make_icon.py --preview OUTDIR  # also dumps per-size PNGs + sheet
"""

from __future__ import annotations

import argparse
import os
import sys

from PIL import Image, ImageDraw

# --- palette -----------------------------------------------------------------
BADGE = (29, 78, 216, 255)      # #1D4ED8  deep blue, high contrast on light+dark
PAGE_A = (255, 255, 255, 255)   # right page  (pure white)
PAGE_B = (198, 216, 255, 255)   # left page   (dimmed white -> reads as open book)
RULE = BADGE                    # knocked-out text lines share the badge colour

ICO_SIZES = (16, 32, 48, 64, 128, 256)


def _tier(size: int) -> dict:
    """Geometry retuned per size tier so small icons stay legible."""
    # `edges` = (top_out, top_in, bot_out, bot_in) as fractions of the canvas.
    # Small sizes give the book more vertical room (and drop the stack band,
    # which would otherwise smear into the pages).
    if size <= 24:
        return dict(pad=0.035, radius=0.225, margin=0.105, rules=0, stack=False,
                    edges=(0.230, 0.370, 0.720, 0.795))
    if size <= 48:
        return dict(pad=0.045, radius=0.225, margin=0.120, rules=0, stack=True,
                    edges=(0.255, 0.385, 0.690, 0.750))
    if size <= 64:
        return dict(pad=0.050, radius=0.228, margin=0.135, rules=2, stack=True,
                    edges=(0.262, 0.388, 0.690, 0.748))
    return dict(pad=0.055, radius=0.230, margin=0.150, rules=3, stack=True,
                edges=(0.272, 0.392, 0.688, 0.742))


def _supersample(size: int) -> int:
    if size <= 16:
        return 16
    if size <= 64:
        return 8
    return 4


def _rounded(draw: ImageDraw.ImageDraw, box, radius: float, fill) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def render(size: int) -> Image.Image:
    """Render one icon size natively."""
    t = _tier(size)
    s = _supersample(size)
    n = size * s
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # --- badge ---------------------------------------------------------------
    pad = t["pad"] * n
    _rounded(d, (pad, pad, n - 1 - pad, n - 1 - pad), t["radius"] * n, BADGE)

    # --- open book -----------------------------------------------------------
    m = t["margin"] * n
    cx = n / 2.0
    # Spine gap: force it to be at least ~1 device pixel wide at the final size,
    # otherwise the two pages fuse into one blob when downsampled.
    gap_half = max(0.026 * n, 0.55 * s)

    # An open book dips at the spine and rises at the outer edges: the top
    # silhouette is a shallow valley, the bottom a shallower one.
    e_top_out, e_top_in, e_bot_out, e_bot_in = t["edges"]
    top_out, top_in = e_top_out * n, e_top_in * n
    bot_out, bot_in = e_bot_out * n, e_bot_in * n

    left = [(m, top_out), (cx - gap_half, top_in), (cx - gap_half, bot_in), (m, bot_out)]
    right = [(n - m, top_out), (cx + gap_half, top_in), (cx + gap_half, bot_in), (n - m, bot_out)]
    d.polygon(left, fill=PAGE_B)
    d.polygon(right, fill=PAGE_A)

    # --- stacked-pages edge --------------------------------------------------
    # A thin band under each page's bottom edge, in the dim tone, so the book
    # reads as having thickness. Skipped at 16px where it would just smear.
    if t["stack"]:
        th = max(0.030 * n, 1.2 * s)
        off = th * 1.55
        d.polygon(
            [(m, bot_out + off), (cx - gap_half, bot_in + off),
             (cx - gap_half, bot_in + off + th), (m, bot_out + off + th)],
            fill=PAGE_B,
        )
        d.polygon(
            [(n - m, bot_out + off), (cx + gap_half, bot_in + off),
             (cx + gap_half, bot_in + off + th), (n - m, bot_out + off + th)],
            fill=PAGE_B,
        )

    # --- ruled text lines ----------------------------------------------------
    if t["rules"]:
        k = t["rules"]
        lw = max(0.022 * n, 1.0 * s)
        first, step = (0.36, 0.230) if k == 2 else (0.30, 0.185)
        for i in range(k):
            f = first + i * step             # vertical position inside the page
            for sign in (-1, 1):
                x_out = m + 0.075 * n if sign < 0 else n - m - 0.075 * n
                x_in = cx - gap_half - 0.055 * n if sign < 0 else cx + gap_half + 0.055 * n
                y_out = top_out + f * (bot_out - top_out)
                y_in = top_in + f * (bot_in - top_in)
                d.line([(x_out, y_out), (x_in, y_in)], fill=RULE, width=int(round(lw)))

    return img.resize((size, size), Image.Resampling.LANCZOS)


def main(argv=None) -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser(description="Generate assets/app.ico")
    ap.add_argument("--out", default=os.path.join(root, "assets", "app.ico"))
    ap.add_argument("--preview", default=None, help="directory for per-size PNG previews")
    args = ap.parse_args(argv)

    frames = [render(s) for s in ICO_SIZES]
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    base = frames[-1]
    base.save(
        args.out,
        format="ICO",
        sizes=[(s, s) for s in ICO_SIZES],
        append_images=frames[:-1],
    )
    print("wrote %s (%d bytes, sizes=%s)" % (args.out, os.path.getsize(args.out), list(ICO_SIZES)))

    if args.preview:
        os.makedirs(args.preview, exist_ok=True)
        for s, f in zip(ICO_SIZES, frames):
            f.save(os.path.join(args.preview, "app_%d.png" % s))
            # 8x nearest-neighbour blow-up: shows the icon exactly as pixels.
            f.resize((s * 8, s * 8), Image.Resampling.NEAREST).save(
                os.path.join(args.preview, "app_%d_x8.png" % s)
            )
        # Contact sheet on light and on dark, at true size and 8x.
        for name, bg in (("light", (243, 244, 246, 255)), ("dark", (24, 24, 27, 255))):
            w = sum(s * 8 + 16 for s in ICO_SIZES) + 16
            sheet = Image.new("RGBA", (w, 256 * 8 + 32), bg)
            x = 16
            for s, f in zip(ICO_SIZES, frames):
                big = f.resize((s * 8, s * 8), Image.Resampling.NEAREST)
                sheet.alpha_composite(big, (x, 16))
                x += s * 8 + 16
            sheet.save(os.path.join(args.preview, "sheet_%s.png" % name))
        print("previews in %s" % args.preview)
    return 0


if __name__ == "__main__":
    sys.exit(main())
