from __future__ import annotations

import math
import tkinter as tk
from functools import lru_cache

from gui_shapes import cached_image, coverage_image, draw_circle, draw_rounded

TILE = 96
SAMPLES = 3
ARM_HALF_WIDTH = 0.8
FLAKE_OPACITY = 0.75
FLAKES = ((28, 6, 4), (16, 56, 36), (12, 22, 62), (20, 64, 2))

GARLAND_HEIGHT = 22
GARLAND_SEGMENT = 70
GARLAND_SPACING = 35
GARLAND_SAG = 48
SNOWMAN_BODY = "#FFFFFF"
SNOWMAN_NOSE = "#FB8C00"
SNOWMAN_EYES = "#16304D"


def _distance_to_segment(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    dx, dy = bx - ax, by - ay
    length_squared = dx * dx + dy * dy
    along = 0.0 if length_squared == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_squared))
    return math.hypot(px - (ax + along * dx), py - (ay + along * dy))


def _flake_segments(radius: float) -> list[tuple[float, float, float, float]]:
    segments = []
    for arm in range(6):
        angle = arm * math.pi / 3
        segments.append((0.0, 0.0, radius * math.cos(angle), radius * math.sin(angle)))
        fork_x, fork_y = 0.55 * radius * math.cos(angle), 0.55 * radius * math.sin(angle)
        for side in (-1, 1):
            branch = angle + side * math.pi / 4
            reach = 0.32 * radius
            segments.append((fork_x, fork_y, fork_x + reach * math.cos(branch), fork_y + reach * math.sin(branch)))
    return segments


@lru_cache(maxsize=None)
def _flake_coverage(size: int) -> tuple[tuple[int, ...], ...]:
    segments = _flake_segments(size / 2 - 1)
    rows = []
    for y in range(size):
        row = []
        for x in range(size):
            hits = 0
            for sy in range(SAMPLES):
                for sx in range(SAMPLES):
                    px = x + (sx + 0.5) / SAMPLES - size / 2
                    py = y + (sy + 0.5) / SAMPLES - size / 2
                    if any(_distance_to_segment(px, py, *segment) <= ARM_HALF_WIDTH for segment in segments):
                        hits += 1
            row.append(round(255 * FLAKE_OPACITY * hits / (SAMPLES * SAMPLES)))
        rows.append(tuple(row))
    return tuple(rows)


@lru_cache(maxsize=None)
def _tile_coverage() -> tuple[tuple[int, ...], ...]:
    tile = [[0] * TILE for _ in range(TILE)]
    for size, left, top in FLAKES:
        for y, row in enumerate(_flake_coverage(size)):
            for x, alpha in enumerate(row):
                cell = tile[(top + y) % TILE]
                cell[(left + x) % TILE] = max(cell[(left + x) % TILE], alpha)
    return tuple(tuple(row) for row in tile)


def tile_image(widget: tk.Misc, color: str) -> tk.PhotoImage:
    return cached_image(widget, ("snow-tile", color), lambda: coverage_image(widget, _tile_coverage(), color))


def draw_wallpaper(canvas: tk.Canvas, color: str, first_row: int, last_row: int, columns: int) -> None:
    image = tile_image(canvas, color)
    for row in range(first_row, last_row):
        for column in range(columns):
            canvas.create_image(column * TILE, row * TILE, image=image, anchor="nw", tags=("wallpaper",))
    canvas.tag_lower("wallpaper")


def draw_garland(canvas: tk.Canvas, width: int, colors: tuple[str, ...], wire: str) -> None:
    canvas.delete("all")
    for start in range(-10, width, GARLAND_SEGMENT):
        points = []
        for step in range(0, GARLAND_SEGMENT + 1, 6):
            t = step / GARLAND_SEGMENT
            points.extend((start + step, 2 + GARLAND_SAG * t * (1 - t)))
        canvas.create_line(*points, fill=wire, width=2, smooth=True)
    for index, x in enumerate(range(0, width, GARLAND_SPACING)):
        t = ((x + 10) % GARLAND_SEGMENT) / GARLAND_SEGMENT
        y = 2 + GARLAND_SAG * t * (1 - t)
        draw_circle(canvas, x - 4, int(y) + 1, 9, colors[index % len(colors)])


def draw_snowman(canvas: tk.Canvas, centre: int, top: int, hat: str) -> None:
    draw_circle(canvas, centre - 24, top + 46, 48, SNOWMAN_BODY)
    draw_circle(canvas, centre - 17, top + 16, 34, SNOWMAN_BODY)
    draw_rounded(canvas, centre - 15, top + 12, centre + 15, top + 19, 3, hat)
    draw_rounded(canvas, centre - 9, top, centre + 9, top + 14, 3, hat)
    draw_circle(canvas, centre - 9, top + 26, 4, SNOWMAN_EYES)
    draw_circle(canvas, centre + 5, top + 26, 4, SNOWMAN_EYES)
    canvas.create_polygon(centre, top + 33, centre + 14, top + 36, centre, top + 39, fill=SNOWMAN_NOSE, outline="")
    for offset in (-2, 12, 26):
        draw_circle(canvas, centre - 3, top + 52 + offset, 6, SNOWMAN_EYES)
