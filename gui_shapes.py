from __future__ import annotations

import base64
import struct
import tkinter as tk
import zlib
from collections.abc import Callable
from functools import lru_cache

from gui_style import AVATAR_COLORS

SAMPLES = 4
CORNERS = ("tl", "tr", "br", "bl")

def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def _png(width: int, height: int, rgba: bytes) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    stride = width * 4
    rows = b"".join(b"\x00" + rgba[y * stride:(y + 1) * stride] for y in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


@lru_cache(maxsize=None)
def _disk_coverage(width: int, height: int, radius_x: float, radius_y: float) -> tuple[tuple[int, ...], ...]:
    rows = []
    for y in range(height):
        row = []
        for x in range(width):
            inside = 0
            for sy in range(SAMPLES):
                for sx in range(SAMPLES):
                    dx = (x + (sx + 0.5) / SAMPLES - radius_x) / radius_x
                    dy = (y + (sy + 0.5) / SAMPLES - radius_y) / radius_y
                    inside += dx * dx + dy * dy <= 1.0
            row.append(round(255 * inside / (SAMPLES * SAMPLES)))
        rows.append(tuple(row))
    return tuple(rows)


def _image_cache(widget: tk.Misc) -> dict[tuple, tk.PhotoImage]:
    root = widget.nametowidget(".")
    if not hasattr(root, "shape_images"):
        root.shape_images = {}
    return root.shape_images


def _to_image(widget: tk.Misc, coverage: tuple[tuple[int, ...], ...], color: str) -> tk.PhotoImage:
    red, green, blue = _hex_to_rgb(color)
    height, width = len(coverage), len(coverage[0])
    pixels = bytearray()
    for row in coverage:
        for alpha in row:
            pixels += bytes((red, green, blue, alpha))
    return tk.PhotoImage(master=widget, data=base64.b64encode(_png(width, height, bytes(pixels))))


def coverage_image(widget: tk.Misc, coverage: tuple[tuple[int, ...], ...], color: str) -> tk.PhotoImage:
    return _to_image(widget, coverage, color)


def cached_image(widget: tk.Misc, key: tuple, build: Callable[[], tk.PhotoImage]) -> tk.PhotoImage:
    cache = _image_cache(widget)
    if key not in cache:
        cache[key] = build()
    return cache[key]


def circle_image(widget: tk.Misc, diameter: int, color: str) -> tk.PhotoImage:
    cache = _image_cache(widget)
    key = ("circle", diameter, color)
    if key not in cache:
        radius = diameter / 2
        cache[key] = _to_image(widget, _disk_coverage(diameter, diameter, radius, radius), color)
    return cache[key]


def corner_image(widget: tk.Misc, radius: int, corner: str, color: str) -> tk.PhotoImage:
    cache = _image_cache(widget)
    key = ("corner", radius, corner, color)
    if key not in cache:
        rows = _disk_coverage(radius, radius, radius, radius)
        if corner in ("bl", "br"):
            rows = rows[::-1]
        if corner in ("tr", "br"):
            rows = tuple(row[::-1] for row in rows)
        cache[key] = _to_image(widget, rows, color)
    return cache[key]


def draw_circle(canvas: tk.Canvas, x: int, y: int, diameter: int, color: str, tags: tuple = ()) -> None:
    canvas.create_image(x, y, image=circle_image(canvas, diameter, color), anchor="nw", tags=tags)


def _rect(canvas: tk.Canvas, x1: int, y1: int, x2: int, y2: int, color: str, tags: tuple) -> None:
    if x2 > x1 and y2 > y1:
        canvas.create_rectangle(x1, y1, x2, y2, fill=color, outline="", width=0, tags=tags)


def draw_rounded(
    canvas: tk.Canvas, x1: int, y1: int, x2: int, y2: int,
    radii: int | tuple[int, int, int, int], color: str, tags: tuple = (),
) -> None:
    if isinstance(radii, int):
        radii = (radii, radii, radii, radii)
    limit = max(0, min(x2 - x1, y2 - y1) // 2)
    top_left, top_right, bottom_right, bottom_left = (min(r, limit) for r in radii)
    top = max(top_left, top_right)
    bottom = max(bottom_left, bottom_right)
    _rect(canvas, x1, y1 + top, x2, y2 - bottom, color, tags)
    _rect(canvas, x1 + top_left, y1, x2 - top_right, y1 + top, color, tags)
    _rect(canvas, x1 + bottom_left, y2 - bottom, x2 - bottom_right, y2, color, tags)
    _rect(canvas, x1, y1 + top_left, x1 + top_left, y1 + top, color, tags)
    _rect(canvas, x2 - top_right, y1 + top_right, x2, y1 + top, color, tags)
    _rect(canvas, x1, y2 - bottom, x1 + bottom_left, y2 - bottom_left, color, tags)
    _rect(canvas, x2 - bottom_right, y2 - bottom, x2, y2 - bottom_right, color, tags)
    placements = (
        ("tl", top_left, x1, y1),
        ("tr", top_right, x2 - top_right, y1),
        ("br", bottom_right, x2 - bottom_right, y2 - bottom_right),
        ("bl", bottom_left, x1, y2 - bottom_left),
    )
    for corner, radius, x, y in placements:
        if radius > 0:
            canvas.create_image(x, y, image=corner_image(canvas, radius, corner, color), anchor="nw", tags=tags)


def draw_rounded_outlined(
    canvas: tk.Canvas, x1: int, y1: int, x2: int, y2: int,
    radii: tuple[int, int, int, int], fill: str, border: str, tags: tuple = (),
) -> None:
    draw_rounded(canvas, x1, y1, x2, y2, radii, border, tags)
    inner = tuple(max(radius - 1, 0) for radius in radii)
    draw_rounded(canvas, x1 + 1, y1 + 1, x2 - 1, y2 - 1, inner, fill, tags)


def draw_rounded_border(
    canvas: tk.Canvas, x1: int, y1: int, x2: int, y2: int,
    radius: int, fill: str, border: str, tags: tuple = (),
) -> None:
    draw_rounded(canvas, x1, y1, x2, y2, radius, border, tags)
    draw_rounded(canvas, x1 + 1, y1 + 1, x2 - 1, y2 - 1, max(radius - 1, 0), fill, tags)


def draw_avatar(canvas: tk.Canvas, x: int, y: int, size: int, name: str, font: object) -> None:
    color = AVATAR_COLORS[sum(map(ord, name)) % len(AVATAR_COLORS)]
    draw_circle(canvas, x, y, size, color)
    canvas.create_text(x + size // 2, y + size // 2, text=name[0].upper(), fill="#FFFFFF", font=font)


def draw_presence_dot(canvas: tk.Canvas, x: int, y: int, size: int, color: str, ring: str) -> None:
    draw_circle(canvas, x - 2, y - 2, size + 4, ring)
    draw_circle(canvas, x, y, size, color)
