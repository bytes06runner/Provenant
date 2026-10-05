"""Synthetic evidence for demos and planted scenarios (there is no camera in this pipeline).

Images are plain renders, clearly not real photos; they exercise the vision check honestly
(color is visible) without pretending to be a customer's picture.
"""

from __future__ import annotations

import io

COLORS: dict[str, tuple[int, int, int]] = {
    "black": (18, 18, 18),
    "navy": (20, 33, 74),
    "grey": (128, 128, 128),
    "red": (170, 30, 30),
}


def sneaker_photo(color: str) -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (512, 384), (238, 238, 232))
    d = ImageDraw.Draw(img)
    d.polygon(
        [
            (70, 250),
            (120, 170),
            (230, 150),
            (300, 110),
            (360, 120),
            (420, 200),
            (450, 250),
            (450, 280),
            (70, 280),
        ],
        fill=COLORS[color],
    )
    d.rectangle([(70, 280), (450, 305)], fill=(245, 245, 245), outline=(200, 200, 200))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
