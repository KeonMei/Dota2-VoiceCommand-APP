"""Pillow-rendered pieces of the control window. Tk's canvas can't anti-alias,
blur or blend, so everything with glow or soft edges is drawn here at 2x and
scaled down; plain text stays on the canvas."""
from __future__ import annotations

import math

from PIL import Image, ImageDraw, ImageFilter

from .config import PROJECT_ROOT

ASSETS = PROJECT_ROOT / "assets" / "ui"
S = 2  # supersampling factor
MIC_ICON_SIZE = 64

BG = (14, 16, 20)
PANEL = (22, 25, 31, 235)
PANEL_BORDER = (43, 47, 56, 255)
SEPARATOR = (34, 37, 44, 255)
RED = (229, 64, 47)
GREEN = (61, 220, 132)
GREY = (74, 79, 88)
CREAM = (245, 236, 222)
MIC_GREY = (150, 155, 163)


def _hex(rgb: tuple[int, ...]) -> str:
    return "#" + "".join(f"{c:02x}" for c in rgb[:3])


def _down(img: Image.Image) -> Image.Image:
    return img.resize((img.width // S, img.height // S), Image.LANCZOS)


def asset(name: str) -> Image.Image:
    return Image.open(ASSETS / name).convert("RGBA")


def tinted(img: Image.Image, rgb: tuple[int, int, int]) -> Image.Image:
    """Recolors a single-color icon, keeping its shape (alpha)."""
    out = Image.new("RGBA", img.size, rgb + (0,))
    out.putalpha(img.getchannel("A"))
    return out


def icon(name: str, size: int, rgb: tuple[int, int, int] | None = None) -> Image.Image:
    img = asset(name).resize((size, size), Image.LANCZOS)
    return tinted(img, rgb) if rgb else img


def render_background(size: tuple[int, int], panels: list[tuple[int, int, int, int, int]], separator_y: int) -> Image.Image:
    """The backdrop art with rounded panels and the footer separator on top.
    `panels` are (x0, y0, x1, y1, radius) in window pixels."""
    w, h = size[0] * S, size[1] * S
    img = asset("background.webp").resize((w, h), Image.LANCZOS)
    d = ImageDraw.Draw(img)
    for x0, y0, x1, y1, r in panels:
        d.rounded_rectangle((x0 * S, y0 * S, x1 * S, y1 * S), radius=r * S, fill=PANEL, outline=PANEL_BORDER, width=S)
    d.line((0, separator_y * S, w, separator_y * S), fill=SEPARATOR, width=S)
    return _down(img)


class MicButtonArt:
    """The round listening toggle: glow, ring, disc and microphone glyph as
    separate layers, composed per frame over the matching background crop."""

    def __init__(self, background_crop: Image.Image, ring_radius: int):
        self.background = background_crop.convert("RGBA")
        self.size = self.background.size
        self.r = ring_radius
        self._layers = {enabled: self._body(enabled) for enabled in (True, False)}
        self._hover_layers = {enabled: self._body(enabled, hover=True) for enabled in (True, False)}
        self._glow = self._make_glow()
        self._cache: dict[tuple, Image.Image] = {}

    def _center(self) -> tuple[float, float]:
        return self.size[0] * S / 2, self.size[1] * S / 2

    def _make_glow(self) -> Image.Image:
        w, h = self.size[0] * S, self.size[1] * S
        cx, cy = self._center()
        r = self.r * S
        mask = Image.new("L", (w, h), 0)
        md = ImageDraw.Draw(mask)
        md.ellipse((cx - r - 4 * S, cy - r - 4 * S, cx + r + 4 * S, cy + r + 4 * S), outline=255, width=10 * S)
        mask = mask.filter(ImageFilter.GaussianBlur(11 * S))
        glow = Image.new("RGBA", (w, h), GREEN + (0,))
        glow.putalpha(mask)
        return _down(glow)

    def _body(self, enabled: bool, hover: bool = False) -> Image.Image:
        w, h = self.size[0] * S, self.size[1] * S
        cx, cy = self._center()
        r = self.r * S
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)

        if enabled:
            ring, inner, outer = GREEN, (37, 96, 66, 255), (33, 58, 46, 255)
            disc_edge, disc_mid = ((17, 24, 27), (22, 32, 34)) if not hover else ((20, 30, 32), (27, 42, 42))
            mic = CREAM
        else:
            ring = (110, 116, 125) if hover else GREY
            inner, outer = (44, 48, 55, 255), (28, 31, 37, 255)
            disc_edge, disc_mid = ((20, 22, 27), (26, 29, 34)) if not hover else ((26, 29, 34), (33, 37, 43))
            mic = MIC_GREY

        d.ellipse((cx - r - 22 * S, cy - r - 22 * S, cx + r + 22 * S, cy + r + 22 * S), outline=outer, width=S)
        # Disc with a subtle radial gradient.
        steps = 24
        for i in range(steps):
            t = i / (steps - 1)
            rr = r - 2 * S - t * (r - 2 * S) * 0.85
            col = tuple(round(a + (b - a) * t) for a, b in zip(disc_edge, disc_mid))
            d.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), fill=col + (255,))
        d.ellipse((cx - r + 9 * S, cy - r + 9 * S, cx + r - 9 * S, cy + r - 9 * S), outline=inner, width=S)
        d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=ring + (255,), width=5 * S)
        body = _down(img)
        glyph = icon("mic.png", MIC_ICON_SIZE, mic)
        body.alpha_composite(glyph, ((body.width - glyph.width) // 2, (body.height - glyph.height) // 2))
        return body

    def frame(self, enabled: bool, hover: bool, pulse: float) -> Image.Image:
        """pulse: 0..1, only used while enabled."""
        level = round(pulse * 20) if enabled else 0
        key = (enabled, hover, level)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        out = self.background.copy()
        if enabled:
            strength = 0.45 + 0.4 * level / 20 + (0.15 if hover else 0.0)
            glow = self._glow.copy()
            glow.putalpha(glow.getchannel("A").point(lambda a: min(255, int(a * strength))))
            out = Image.alpha_composite(out, glow)
        out = Image.alpha_composite(out, (self._hover_layers if hover else self._layers)[enabled])
        self._cache[key] = out
        return out


def dot(color: tuple[int, int, int], diameter: int, glow: bool = False) -> Image.Image:
    pad = 5 if glow else 1
    size = (diameter + pad * 2) * S
    img = Image.new("RGBA", (size, size), color + (0,))
    if glow:
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).ellipse((pad * S, pad * S, size - pad * S, size - pad * S), fill=110)
        img.putalpha(mask.filter(ImageFilter.GaussianBlur(3 * S)))
    ImageDraw.Draw(img).ellipse((pad * S, pad * S, size - pad * S - 1, size - pad * S - 1), fill=color + (255,))
    return _down(img)


def rounded_square(color: tuple[int, int, int], side: int, radius: int) -> Image.Image:
    img = Image.new("RGBA", ((side + 2) * S, (side + 2) * S), (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle((S, S, (side + 1) * S, (side + 1) * S), radius=radius * S, fill=color + (255,))
    return _down(img)


def status_icon(kind: str, diameter: int = 22) -> Image.Image:
    """Round badge for the last command's outcome: done / failed / stopped."""
    size = diameter * S
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    fill = {"done": (38, 176, 104), "failed": (214, 64, 50), "stopped": (88, 94, 104)}[kind]
    d.ellipse((0, 0, size - 1, size - 1), fill=fill + (255,))
    white = (255, 255, 255, 255)
    c, k = size / 2, size / 22
    if kind == "done":
        d.line([(c - 5.5 * k, c + 0.3 * k), (c - 1.5 * k, c + 4.2 * k), (c + 5.8 * k, c - 4 * k)], fill=white, width=round(2.6 * k), joint="curve")
    elif kind == "failed":
        for sx in (-1, 1):
            d.line([(c - 4.5 * k * sx, c - 4.5 * k), (c + 4.5 * k * sx, c + 4.5 * k)], fill=white, width=round(2.6 * k))
    else:
        d.rounded_rectangle((c - 4 * k, c - 4 * k, c + 4 * k, c + 4 * k), radius=k, fill=white)
    return _down(img)


def spinner_frames(diameter: int = 22, count: int = 12) -> list[Image.Image]:
    frames = []
    size = diameter * S
    width = round(2.6 * size / 22)
    box = (width, width, size - width - 1, size - width - 1)
    for i in range(count):
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse(box, outline=(50, 56, 64, 255), width=width)
        start = i * 360 / count
        d.arc(box, start=start, end=start + 100, fill=(240, 180, 60, 255), width=width)
        frames.append(_down(img))
    return frames


def color(rgb: tuple[int, ...]) -> str:
    return _hex(rgb)


def wave_profile(index: int, count: int) -> float:
    """Bar height envelope: tallest in the middle, tapering to the ends."""
    x = (index - (count - 1) / 2) / ((count - 1) / 2)
    return math.exp(-(x / 0.55) ** 2)
