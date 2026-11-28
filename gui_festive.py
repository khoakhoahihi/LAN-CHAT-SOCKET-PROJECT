from __future__ import annotations

import math
import random
import tkinter as tk
from functools import lru_cache

from gui_shapes import cached_image, coverage_image, draw_circle, draw_rounded
from gui_style import mix

TILE = 240
SAMPLES = 3
WALLPAPER_ELEMENTS = (
    ("flake", 34, 14, 10, 0.85),
    ("dot", 10, 96, 18, 0.55),
    ("flake", 22, 150, 24, 0.60),
    ("dot", 7, 208, 14, 0.45),
    ("flake", 16, 84, 70, 0.50),
    ("dot", 12, 28, 78, 0.40),
    ("flake", 28, 190, 96, 0.75),
    ("dot", 8, 140, 112, 0.50),
    ("flake", 12, 44, 134, 0.45),
    ("flake", 20, 106, 150, 0.60),
    ("dot", 9, 222, 148, 0.40),
    ("flake", 14, 206, 176, 0.50),
    ("flake", 30, 18, 182, 0.70),
    ("dot", 11, 150, 196, 0.45),
    ("flake", 16, 74, 214, 0.55),
    ("dot", 6, 120, 98, 0.35),
)

GARLAND_HEIGHT = 40
GARLAND_SWAG = 120
GARLAND_SAG = 20
GARLAND_SEED = 7
PINES = ("#1B5E43", "#2A7550", "#3D8A62")
BERRY = "#C62F45"
GOLD = "#E3B04B"
BULB = "#FFD98A"
TRUNK = "#7A4B2A"
STAR_POINTS = 5
TREE_LAYERS = ((10, 30, 34, 2), (32, 44, 38, 1), (56, 58, 40, 0))
TREE_BAUBLES = ((-12, 46, 7, BERRY), (13, 66, 7, GOLD), (-24, 88, 7, GOLD), (10, 94, 7, BERRY), (-2, 30, 6, GOLD))
TREE_LIGHTS = ((8, 40), (-18, 64), (22, 86), (-8, 80), (0, 100))
LEAF_LENGTH = 22
LEAF_BULGE = 5.5


def _distance_to_segment(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    dx, dy = bx - ax, by - ay
    length_squared = dx * dx + dy * dy
    along = 0.0 if length_squared == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_squared))
    return math.hypot(px - (ax + along * dx), py - (ay + along * dy))


def _flake_segments(radius: float) -> list[tuple[float, float, float, float]]:
    segments = []
    for arm in range(6):
        angle = arm * math.pi / 3
        direction = (math.cos(angle), math.sin(angle))
        segments.append((0.0, 0.0, radius * direction[0], radius * direction[1]))
        for position, reach in ((0.38, 0.22), (0.64, 0.30)):
            fork = (position * radius * direction[0], position * radius * direction[1])
            for side in (-1, 1):
                branch = angle + side * math.pi / 3
                end = (fork[0] + reach * radius * math.cos(branch), fork[1] + reach * radius * math.sin(branch))
                segments.append((fork[0], fork[1], end[0], end[1]))
    return segments


@lru_cache(maxsize=None)
def _flake_coverage(size: int, half_width: float) -> tuple[tuple[int, ...], ...]:
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
                    if any(_distance_to_segment(px, py, *segment) <= half_width for segment in segments):
                        hits += 1
            row.append(round(255 * hits / (SAMPLES * SAMPLES)))
        rows.append(tuple(row))
    return tuple(rows)


@lru_cache(maxsize=None)
def _dot_coverage(size: int) -> tuple[tuple[int, ...], ...]:
    radius = size / 2
    rows = []
    for y in range(size):
        row = []
        for x in range(size):
            distance = math.hypot(x + 0.5 - radius, y + 0.5 - radius) / radius
            row.append(round(255 * max(0.0, 1.0 - distance * distance)))
        rows.append(tuple(row))
    return tuple(rows)


@lru_cache(maxsize=None)
def _tile_coverage() -> tuple[tuple[int, ...], ...]:
    tile = [[0] * TILE for _ in range(TILE)]
    for kind, size, left, top, opacity in WALLPAPER_ELEMENTS:
        coverage = _flake_coverage(size, 0.7 if size < 20 else 0.85) if kind == "flake" else _dot_coverage(size)
        for y, row in enumerate(coverage):
            cells = tile[(top + y) % TILE]
            for x, alpha in enumerate(row):
                column = (left + x) % TILE
                cells[column] = max(cells[column], round(alpha * opacity))
    return tuple(tuple(row) for row in tile)


def _inside(px: float, py: float, points: tuple[tuple[float, float], ...]) -> bool:
    inside = False
    previous = points[-1]
    for current in points:
        if (current[1] > py) != (previous[1] > py):
            crossing = (previous[0] - current[0]) * (py - current[1]) / (previous[1] - current[1]) + current[0]
            if px < crossing:
                inside = not inside
        previous = current
    return inside


def polygon_image(widget: tk.Misc, key: str, points: tuple[tuple[float, float], ...], color: str) -> tk.PhotoImage:
    def build() -> tk.PhotoImage:
        width = math.ceil(max(x for x, _ in points)) + 1
        height = math.ceil(max(y for _, y in points)) + 1
        rows = []
        for y in range(height):
            row = []
            for x in range(width):
                hits = sum(
                    _inside(x + (sx + 0.5) / SAMPLES, y + (sy + 0.5) / SAMPLES, points)
                    for sy in range(SAMPLES) for sx in range(SAMPLES)
                )
                row.append(round(255 * hits / (SAMPLES * SAMPLES)))
            rows.append(tuple(row))
        return coverage_image(widget, tuple(rows), color)

    return cached_image(widget, ("polygon", key, color), build)


def draw_polygon(
    canvas: tk.Canvas, x: float, y: float, key: str, points: tuple[tuple[float, float], ...], color: str,
) -> None:
    canvas.create_image(round(x), round(y), image=polygon_image(canvas, key, points, color), anchor="nw")


def tile_image(widget: tk.Misc, color: str) -> tk.PhotoImage:
    return cached_image(widget, ("snow-tile", color), lambda: coverage_image(widget, _tile_coverage(), color))


def draw_wallpaper(canvas: tk.Canvas, color: str, first_row: int, last_row: int, columns: int) -> None:
    image = tile_image(canvas, color)
    for row in range(first_row, last_row):
        for column in range(columns):
            canvas.create_image(column * TILE, row * TILE, image=image, anchor="nw", tags=("wallpaper",))
    canvas.tag_lower("wallpaper")


def _swag_point(start: int, t: float) -> tuple[float, float]:
    return start + t * GARLAND_SWAG, 6 + GARLAND_SAG * 4 * t * (1 - t)


def draw_garland(canvas: tk.Canvas, width: int, surface: str) -> None:
    canvas.delete("all")
    rng = random.Random(GARLAND_SEED)
    swags = range(-30, width, GARLAND_SWAG)
    for start in swags:
        for step in range(0, GARLAND_SWAG + 1, 5):
            x, y = _swag_point(start, step / GARLAND_SWAG)
            for _ in range(2):
                size = rng.randrange(8, 13)
                left = int(x + rng.uniform(-3, 3)) - size // 2
                top = int(y + rng.uniform(-4, 5)) - size // 2
                draw_circle(canvas, left, top, size, rng.choice(PINES))
    halo = mix(BULB, surface, 0.4)
    for start in swags:
        for t in (0.22, 0.5, 0.78):
            x, y = _swag_point(start, t)
            for dx, dy in ((-4, 2), (3, 4), (0, -2)):
                draw_circle(canvas, int(x) + dx - 2, int(y) + dy - 2, 5, BERRY)
        x, y = _swag_point(start, 0.5)
        canvas.create_line(x, y + 5, x, y + 12, fill=GOLD, width=1)
        draw_circle(canvas, int(x) - 5, int(y) + 11, 10, GOLD)
        draw_circle(canvas, int(x) - 2, int(y) + 13, 3, mix(GOLD, "#FFFFFF", 0.6))
        for t in (0.12, 0.36, 0.64, 0.88):
            x, y = _swag_point(start, t)
            draw_circle(canvas, int(x) - 6, int(y) - 1, 13, halo)
            draw_circle(canvas, int(x) - 3, int(y) + 2, 6, BULB)


def _star_points() -> tuple[tuple[float, float], ...]:
    points = []
    for index in range(STAR_POINTS * 2):
        radius = 9 if index % 2 == 0 else 4
        angle = -math.pi / 2 + index * math.pi / STAR_POINTS
        points.append((10 + radius * math.cos(angle), 10 + radius * math.sin(angle)))
    return tuple(points)


def draw_tree(canvas: tk.Canvas, centre: int, top: int) -> None:
    for offset, half, height, shade in reversed(TREE_LAYERS):
        points = ((half, 0.0), (2.0 * half, float(height)), (0.0, float(height)))
        draw_polygon(canvas, centre - half, top + offset, f"tree-{half}-{height}", points, PINES[shade])
    draw_rounded(canvas, centre - 6, top + 94, centre + 6, top + 106, 2, TRUNK)
    for dx, dy, size, color in TREE_BAUBLES:
        draw_circle(canvas, centre + dx - size // 2, top + dy - size // 2, size, color)
    for dx, dy in TREE_LIGHTS:
        draw_circle(canvas, centre + dx - 2, top + dy - 2, 4, BULB)
    draw_polygon(canvas, centre - 10, top - 8, "star", _star_points(), GOLD)


def _leaf_points(angle: float) -> tuple[tuple[tuple[float, float], ...], float, float]:
    outline = []
    for step in range(13):
        t = step / 12
        outline.append((LEAF_LENGTH * t, LEAF_BULGE * math.sin(math.pi * t)))
    for step in range(12, -1, -1):
        t = step / 12
        outline.append((LEAF_LENGTH * t, -LEAF_BULGE * math.sin(math.pi * t)))
    cosine, sine = math.cos(angle), math.sin(angle)
    rotated = [(x * cosine - y * sine, x * sine + y * cosine) for x, y in outline]
    left, top = min(x for x, _ in rotated), min(y for _, y in rotated)
    return tuple((x - left, y - top) for x, y in rotated), left, top


def draw_holly(canvas: tk.Canvas, x: int, y: int) -> None:
    canvas.delete("all")
    for index, angle in enumerate((-0.55, 0.45)):
        points, left, top = _leaf_points(angle)
        draw_polygon(canvas, x + left, y + top, f"leaf-{index}", points, PINES[index])
    for dx, dy in ((-3, -3), (2, -1), (-1, 3)):
        draw_circle(canvas, x + dx - 3, y + dy - 3, 6, BERRY)
