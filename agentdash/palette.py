"""Colour palette and allocation.

Base palette: https://coolors.co/palette/664d00-6e2a0c-691312-5d0933-291938-042d3a-12403c-475200

Eight dark hues. When they run out we generate further *shades* of the same
hues by walking lightness up and down, so window N+1 is always visibly distinct
from every window currently on screen while still belonging to the palette.
"""
import colorsys
from typing import Iterable, List, Optional

BASE_PALETTE: List[str] = [
    "#664D00",  # olive gold
    "#6E2A0C",  # burnt sienna
    "#691312",  # oxblood
    "#5D0933",  # plum
    "#291938",  # aubergine
    "#042D3A",  # deep teal-navy
    "#12403C",  # pine
    "#475200",  # moss
]

# Lightness multipliers applied in rings. Ring 0 is the palette as published.
# Later rings stay inside a range that keeps light text readable on top.
_LIGHTNESS_RINGS = (1.00, 1.48, 0.62, 1.86, 1.22, 0.42, 1.66, 0.80)
# A small hue nudge per ring keeps shades of the same base from converging.
_HUE_NUDGES = (0.0, 0.012, -0.012, 0.022, -0.022, 0.032, -0.032, 0.042)

_MIN_L = 0.045
_MAX_L = 0.33


def hex_to_rgb(value: str) -> tuple:
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def rgb_to_hex(rgb: Iterable[int]) -> str:
    return "#" + "".join("%02X" % max(0, min(255, int(round(c)))) for c in rgb)


def _shade(base_hex: str, ring: int) -> str:
    r, g, b = (c / 255.0 for c in hex_to_rgb(base_hex))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    l = max(_MIN_L, min(_MAX_L, l * _LIGHTNESS_RINGS[ring % len(_LIGHTNESS_RINGS)]))
    h = (h + _HUE_NUDGES[ring % len(_HUE_NUDGES)]) % 1.0
    nr, ng, nb = colorsys.hls_to_rgb(h, l, s)
    return rgb_to_hex((nr * 255, ng * 255, nb * 255))


def palette_sequence(count: int) -> List[str]:
    """Ordered list of `count` distinct palette colours.

    Ring 0 (the eight published colours) is exhausted before any shade is used,
    and within a ring we walk the hues in order so adjacent windows differ in
    hue rather than in brightness alone.
    """
    out: List[str] = []
    seen = set()
    ring = 0
    while len(out) < count:
        for base in BASE_PALETTE:
            colour = _shade(base, ring)
            if colour not in seen:
                seen.add(colour)
                out.append(colour)
                if len(out) == count:
                    break
        ring += 1
        if ring > 64:  # pathological guard; 8 hues x 64 rings is 512 colours
            break
    return out


def allocate(in_use: Iterable[str], preferred: Optional[str] = None) -> str:
    """Return a colour not present in `in_use`.

    `preferred` (e.g. the colour a window had before a daemon restart) wins if
    it is still free, so window colours survive a reconnect.
    """
    used = {c.upper() for c in in_use if c}
    if preferred and preferred.upper() not in used:
        return preferred.upper()
    for colour in palette_sequence(len(used) + len(BASE_PALETTE) + 8):
        if colour.upper() not in used:
            return colour.upper()
    return BASE_PALETTE[0]


def readable_fg(bg_hex: str) -> str:
    """Pick black or white text for a given background by relative luminance."""
    r, g, b = (c / 255.0 for c in hex_to_rgb(bg_hex))
    def lin(c):
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    lum = 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)
    return "#000000" if lum > 0.34 else "#FFFFFF"


def lighten(hex_value: str, factor: float) -> str:
    r, g, b = (c / 255.0 for c in hex_to_rgb(hex_value))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    l = max(0.0, min(1.0, l * factor))
    nr, ng, nb = colorsys.hls_to_rgb(h, l, s)
    return rgb_to_hex((nr * 255, ng * 255, nb * 255))
