#!/usr/bin/env python3
"""Generate the PhotoBooth app icon: one geometry source -> SVG + PNG + ICO.

    python3 tools/make_icon.py

The mark is a camera aperture (six-blade iris) in mint on the booth's teal -- the
accent the kiosk already uses (--accent #5eead4) and the admin theme colour
(#0f766e).

The shape is defined once, as numbers, and emitted two ways: as an SVG (shipped
to browsers) and as PNGs rasterised here. Rasterising is done with signed
distance fields and one sample per pixel, which gives clean antialiasing without
Pillow/cairosvg/ImageMagick -- the standard library is the only dependency, so
this runs anywhere, not just on the Mac it was written on.

(macOS `qlmanage` was the obvious rasteriser and is deliberately not used: it
renders an SVG as a *text document* thumbnail at some sizes, so the output
silently varies with the requested size.)

Writes:
    frontend/assets/icon.svg              master, rounded, for the web pages
    frontend/assets/icon-192.png          PWA / android
    frontend/assets/icon-512.png          PWA / android
    frontend/assets/apple-touch-icon.png  iOS home screen (180px)
    frontend/assets/favicon.ico           32 + 48 px, PNG-in-ICO
    ios/PhotoBooth/Assets.xcassets/...    1024px app icon, opaque (no alpha)
"""
from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "frontend" / "assets"
APPICON = ROOT / "ios" / "PhotoBooth" / "Assets.xcassets" / "AppIcon.appiconset"

# --- geometry, in master-canvas units --------------------------------------
S = 1024.0               # master canvas
C = S / 2                # centre
R = 306.0                # iris outer radius
R_OPEN = 112.0           # circumradius of the hexagonal opening
GAP = 30.0               # width of the cut between two blades
CORNER = 0.22 * S        # rounded-corner radius (Apple-ish squircle proportion)

GLINT = (C - 42, C - 46, 34.0, 11.0, -38.0)   # cx, cy, rx, ry, rotation

# --- palette ---------------------------------------------------------------
BG_STOPS = ((0.00, (0x11, 0x5E, 0x59)),
            (0.55, (0x0A, 0x3F, 0x3B)),
            (1.00, (0x04, 0x2F, 0x2E)))
BLADE_STOPS = ((0.00, (0xF0, 0xFD, 0xFA)),
               (0.45, (0x7C, 0xEA, 0xDA)),
               (1.00, (0x22, 0xC3, 0xAE)))
GLOW = (0x2D, 0xD4, 0xBF)
GLOW_ALPHA = 0.30
OPENING = (0x02, 0x20, 0x1F)
OPENING_ALPHA = 0.62
GLINT_ALPHA = 0.20


def _pt(radius: float, deg: float) -> tuple[float, float]:
    """Polar -> canvas space (y grows downwards, so the sine is negated)."""
    a = math.radians(deg)
    return C + radius * math.cos(a), C - radius * math.sin(a)


def hexagon() -> list[tuple[float, float]]:
    return [_pt(R_OPEN, 30 + 60 * k) for k in range(6)]


def blade_cuts() -> list[tuple[float, float, float, float]]:
    """The six straight edges separating the blades, as (x1, y1, x2, y2).

    Each leaves a vertex of the opening along the continuation of the hexagon
    side that ends there -- which is what gives a real iris its twist -- and runs
    out past the rim, where the disk clips it.
    """
    hexa, cuts = hexagon(), []
    for k in range(6):
        vx, vy = hexa[(k + 1) % 6]
        px, py = hexa[k]
        dx, dy = vx - px, vy - py
        n = math.hypot(dx, dy)
        dx, dy = dx / n, dy / n
        ox, oy = vx - C, vy - C                  # solve |V + t*d| = R, positive root
        b = ox * dx + oy * dy
        t = -b + math.sqrt(b * b + R * R - (ox * ox + oy * oy)) + 40
        cuts.append((vx, vy, vx + dx * t, vy + dy * t))
    return cuts


# --- svg -------------------------------------------------------------------
def build_svg(rounded: bool) -> str:
    """Full-bleed square for iOS (the OS applies its own mask); rounded for the web."""
    rx = f' rx="{CORNER:.0f}"' if rounded else ""
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in hexagon())
    cuts = "\n".join(
        f'      <line x1="{a:.1f}" y1="{b:.1f}" x2="{c:.1f}" y2="{d:.1f}"/>'
        for a, b, c, d in blade_cuts())
    gx, gy, grx, gry, ga = GLINT
    hexf = "#%02x%02x%02x" % OPENING
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{S:.0f}" height="{S:.0f}"
     viewBox="0 0 {S:.0f} {S:.0f}" role="img" aria-label="PhotoBooth">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#115e59"/>
      <stop offset="0.55" stop-color="#0a3f3b"/>
      <stop offset="1" stop-color="#042f2e"/>
    </linearGradient>
    <radialGradient id="glow" cx="0.5" cy="0.44" r="0.55">
      <stop offset="0" stop-color="#2dd4bf" stop-opacity="{GLOW_ALPHA}"/>
      <stop offset="1" stop-color="#2dd4bf" stop-opacity="0"/>
    </radialGradient>
    <linearGradient id="blade" x1="0.15" y1="0" x2="0.75" y2="1">
      <stop offset="0" stop-color="#f0fdfa"/>
      <stop offset="0.45" stop-color="#7ceada"/>
      <stop offset="1" stop-color="#22c3ae"/>
    </linearGradient>
    <!-- white keeps the blade, black cuts through to the background -->
    <mask id="iris">
      <circle cx="{C:.0f}" cy="{C:.0f}" r="{R:.0f}" fill="#fff"/>
      <polygon points="{poly}" fill="#000"/>
      <g stroke="#000" stroke-width="{GAP:.0f}" stroke-linecap="butt">
{cuts}
      </g>
    </mask>
  </defs>

  <rect width="{S:.0f}" height="{S:.0f}"{rx} fill="url(#bg)"/>
  <rect width="{S:.0f}" height="{S:.0f}"{rx} fill="url(#glow)"/>

  <circle cx="{C:.0f}" cy="{C:.0f}" r="{R:.0f}" fill="url(#blade)" mask="url(#iris)"/>

  <!-- the opening: darkened, with a thin specular streak so it reads as glass.
       Deliberately low-contrast -- at favicon size it should fade out rather
       than turn into a smudge. -->
  <polygon points="{poly}" fill="{hexf}" fill-opacity="{OPENING_ALPHA}"/>
  <ellipse cx="{gx:.0f}" cy="{gy:.0f}" rx="{grx:.0f}" ry="{gry:.0f}" fill="#ffffff"
           fill-opacity="{GLINT_ALPHA}" transform="rotate({ga:.0f} {gx:.0f} {gy:.0f})"/>
</svg>
"""


# --- raster ----------------------------------------------------------------
def _grad(stops, t: float) -> tuple[float, float, float]:
    t = 0.0 if t < 0 else (1.0 if t > 1 else t)
    for i in range(len(stops) - 1):
        o0, c0 = stops[i]
        o1, c1 = stops[i + 1]
        if t <= o1:
            f = 0.0 if o1 == o0 else (t - o0) / (o1 - o0)
            return (c0[0] + (c1[0] - c0[0]) * f,
                    c0[1] + (c1[1] - c0[1]) * f,
                    c0[2] + (c1[2] - c0[2]) * f)
    return stops[-1][1]


def _cov(sd: float) -> float:
    """Signed distance -> coverage, antialiased across one pixel."""
    a = 0.5 - sd
    return 0.0 if a < 0 else (1.0 if a > 1 else a)


def _sd_convex(px: float, py: float, poly) -> float:
    """Signed distance to a convex polygon: the largest half-plane distance.

    Negative inside. Exact outside the vertex regions and slightly conservative
    at the corners, which is invisible at a one-pixel antialiasing band.
    """
    # The cross product below is positive *inside* for this winding, so the
    # signed distance is its negation -- and the outermost edge is therefore the
    # smallest cross product, not the largest.
    inner = 1e9
    n = len(poly)
    for i in range(n):
        ax, ay = poly[i]
        bx, by = poly[(i + 1) % n]
        ex, ey = bx - ax, by - ay
        d = ((px - ax) * ey - (py - ay) * ex) / math.hypot(ex, ey)
        if d < inner:
            inner = d
    return -inner


def _sd_segment(px: float, py: float, seg) -> float:
    ax, ay, bx, by = seg
    ex, ey = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    t = (wx * ex + wy * ey) / (ex * ex + ey * ey)
    t = 0.0 if t < 0 else (1.0 if t > 1 else t)
    return math.hypot(wx - ex * t, wy - ey * t)


def _blend(dst, src, a: float):
    return (dst[0] + (src[0] - dst[0]) * a,
            dst[1] + (src[1] - dst[1]) * a,
            dst[2] + (src[2] - dst[2]) * a)


def render(size: int, rounded: bool, opaque: bool) -> bytes:
    """Rasterise at `size` px square and return PNG bytes."""
    k = S / size                                  # canvas units per output pixel
    cuts = blade_cuts()
    poly = hexagon()
    half = S / 2 - CORNER
    gx, gy, grx, gry, ga = GLINT
    ca, sa = math.cos(math.radians(ga)), math.sin(math.radians(ga))
    glow_r = 0.55 * S
    glow_cx, glow_cy = 0.5 * S, 0.44 * S
    hexr = OPENING
    band = k / 2                                  # half a source-pixel, for AA scaling

    rows = bytearray()
    for py_i in range(size):
        y = (py_i + 0.5) * k
        rows.append(0)                            # PNG filter: none
        row = bytearray()
        for px_i in range(size):
            x = (px_i + 0.5) * k

            col = _grad(BG_STOPS, (x + y) / (2 * S))
            d = math.hypot(x - glow_cx, y - glow_cy) / glow_r
            if d < 1.0:
                col = _blend(col, GLOW, GLOW_ALPHA * (1.0 - d))

            dc = math.hypot(x - C, y - C)
            if dc < R + k:
                a = _cov((dc - R) / k)
                if a > 0:
                    a *= 1.0 - _cov(_sd_convex(x, y, poly) / k)
                    for seg in cuts:
                        if a <= 0:
                            break
                        a *= 1.0 - _cov((_sd_segment(x, y, seg) - GAP / 2) / k)
                    if a > 0:
                        col = _blend(col, _grad(
                            BLADE_STOPS,
                            (0.6 * (x - 0.15 * S) + y) / (1.36 * S)), a)

            if dc < R_OPEN + k:                   # the darkened opening
                a = _cov(_sd_convex(x, y, poly) / k)
                if a > 0:
                    col = _blend(col, hexr, OPENING_ALPHA * a)

            ex, ey = x - gx, y - gy               # the specular streak
            lx, ly = ex * ca + ey * sa, -ex * sa + ey * ca
            e = math.hypot(lx / grx, ly / gry)
            if e < 1.0 + k / grx:
                a = _cov((e - 1.0) * min(grx, gry) / k)
                if a > 0:
                    col = _blend(col, (255, 255, 255), GLINT_ALPHA * a)

            alpha = 255
            if rounded:
                qx = abs(x - C) - half
                qy = abs(y - C) - half
                qx = qx if qx > 0 else 0.0
                qy = qy if qy > 0 else 0.0
                alpha = int(round(255 * _cov(
                    (math.hypot(qx, qy) - CORNER) / k)))

            r = int(col[0] + 0.5); g = int(col[1] + 0.5); b = int(col[2] + 0.5)
            r = 0 if r < 0 else (255 if r > 255 else r)
            g = 0 if g < 0 else (255 if g > 255 else g)
            b = 0 if b < 0 else (255 if b > 255 else b)
            if opaque:
                row += bytes((r, g, b))
            else:
                row += bytes((r, g, b, alpha))
        rows += row

    return _png(size, rows, opaque)


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def _png(size: int, raw: bytes, opaque: bool) -> bytes:
    # colour type 2 = RGB, 6 = RGBA. App icons must be opaque: actool warns on an
    # alpha channel and App Store validation rejects it.
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2 if opaque else 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + _chunk(b"IEND", b""))


def build_ico(pngs: list[bytes]) -> bytes:
    """PNG-in-ICO -- understood by every browser we care about, and far smaller
    than the equivalent uncompressed BMP payloads."""
    offset = 6 + 16 * len(pngs)
    out = struct.pack("<HHH", 0, 1, len(pngs))
    for blob in pngs:
        w, h = struct.unpack(">II", blob[16:24])
        out += struct.pack("<BBBBHHII", w % 256, h % 256, 0, 0, 1, 32,
                           len(blob), offset)
        offset += len(blob)
    return out + b"".join(pngs)


# --- asset catalog ---------------------------------------------------------
APPICON_CONTENTS = """{
  "images" : [
    {
      "filename" : "icon-1024.png",
      "idiom" : "universal",
      "platform" : "ios",
      "size" : "1024x1024"
    }
  ],
  "info" : {
    "author" : "xcode",
    "version" : 1
  }
}
"""

XCASSETS_CONTENTS = """{
  "info" : {
    "author" : "xcode",
    "version" : 1
  }
}
"""


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data) if isinstance(data, bytes) else path.write_text(data)
    print(f"  {path.relative_to(ROOT)}")


def main() -> int:
    _write(ASSETS / "icon.svg", build_svg(rounded=True))
    for size, name in ((180, "apple-touch-icon.png"), (192, "icon-192.png"),
                       (512, "icon-512.png")):
        _write(ASSETS / name, render(size, rounded=True, opaque=False))

    _write(ASSETS / "favicon.ico",
           build_ico([render(s, rounded=True, opaque=False) for s in (32, 48)]))

    _write(APPICON / "icon-1024.png", render(1024, rounded=False, opaque=True))
    _write(APPICON / "Contents.json", APPICON_CONTENTS)
    _write(APPICON.parent / "Contents.json", XCASSETS_CONTENTS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
