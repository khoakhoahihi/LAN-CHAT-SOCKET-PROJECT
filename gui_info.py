from __future__ import annotations

import os
import subprocess
import sys
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass

from gui_shapes import draw_avatar, draw_presence_dot, draw_rounded
from gui_style import LIGHT, SP_LG, SP_MD, SP_SM, SP_XL, SP_XS, STATUS_COLORS, Fonts, truncate_text

INFO_WIDTH = 260
AVATAR_SIZE = 72
DOT_SIZE = 14
FILE_ROW_HEIGHT = 52
FILE_ICON_SIZE = 32
MAX_FILES = 12
BUTTON_HEIGHT = 36

Region = tuple[int, int, Callable[[], None]]


@dataclass(frozen=True)
class SharedFile:
    name: str
    direction: str
    size: str
    stamp: str
    path: str


def open_folder(path: str) -> None:
    target = path if os.path.isdir(path) else os.path.dirname(os.path.abspath(path))
    try:
        if sys.platform == "win32":
            os.startfile(target)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", target])
        else:
            subprocess.Popen(["xdg-open", target])
    except OSError:
        pass


def paint_transfer_arrow(canvas: tk.Canvas, cx: int, cy: int, color: str, up: bool) -> None:
    tip, tail = (cy - 6, cy + 6) if up else (cy + 6, cy - 6)
    wing = 4 if up else -4
    canvas.create_line(cx, tail, cx, tip, fill=color, width=2, capstyle="round")
    head = (cx - 4, tip + wing, cx, tip, cx + 4, tip + wing)
    canvas.create_line(*head, fill=color, width=2, capstyle="round", joinstyle="round")


class InfoPanel(tk.Canvas):
    def __init__(self, parent: tk.Misc, fonts: Fonts, downloads: str) -> None:
        super().__init__(parent, highlightthickness=0, bd=0)
        self.fonts = fonts
        self.downloads = downloads
        self.palette = LIGHT
        self.partner: object | None = None
        self.files: list[SharedFile] = []
        self._regions: list[Region] = []
        self.bind("<Configure>", lambda _event: self.draw())
        self.bind("<Button-1>", self._on_click)
        self.bind("<Motion>", self._on_motion)

    def apply_palette(self, palette: dict[str, str]) -> None:
        self.palette = palette
        self.configure(bg=palette["panel"])
        self.draw()

    def set_partner(self, partner: object | None) -> None:
        self.partner = partner
        self.draw()

    def add_file(self, shared: SharedFile) -> None:
        self.files.append(shared)
        self.draw()

    def draw(self) -> None:
        self.delete("all")
        self._regions.clear()
        width, height = max(self.winfo_width(), INFO_WIDTH), max(self.winfo_height(), 1)
        y = self._draw_partner(width)
        y = self._draw_files(width, y + SP_LG)
        self._draw_open_button(width, max(y + SP_LG, height - BUTTON_HEIGHT - SP_LG))

    def _draw_partner(self, width: int) -> int:
        palette, fonts = self.palette, self.fonts
        centre = width // 2
        if self.partner is None:
            self.create_text(centre, SP_XL + 30, text="No chat selected", fill=palette["muted"], font=fonts.bold)
            return SP_XL + 50
        partner = self.partner
        top = SP_XL
        draw_avatar(self, centre - AVATAR_SIZE // 2, top, AVATAR_SIZE, partner.name, fonts.title)
        edge = centre + AVATAR_SIZE // 2 - DOT_SIZE - 2
        dot_top = top + AVATAR_SIZE - DOT_SIZE - 2
        draw_presence_dot(self, edge, dot_top, DOT_SIZE, STATUS_COLORS[partner.status], palette["panel"])
        name = truncate_text(partner.name, fonts.title, width - 2 * SP_LG)
        self.create_text(centre, top + AVATAR_SIZE + SP_LG, text=name, fill=palette["text"], font=fonts.title)
        about = f"{partner.status}  ·  {partner.ip}:{partner.tcp_port}"
        about = truncate_text(about, fonts.sub, width - 2 * SP_LG)
        self.create_text(centre, top + AVATAR_SIZE + SP_LG + 22, text=about, fill=palette["muted"], font=fonts.sub)
        return top + AVATAR_SIZE + SP_LG + 34

    def _draw_files(self, width: int, top: int) -> int:
        palette, fonts = self.palette, self.fonts
        self.create_line(SP_LG, top, width - SP_LG, top, fill=palette["line"])
        title_y = top + SP_LG + 2
        self.create_text(SP_LG, title_y, text="Shared files", anchor="w", fill=palette["text"], font=fonts.btn_bold)
        count = str(len(self.files))
        self.create_text(width - SP_LG, title_y, text=count, anchor="e", fill=palette["subtle"], font=fonts.sub)
        y = title_y + SP_MD
        if not self.files:
            empty = "No files shared yet"
            self.create_text(SP_LG, y + SP_LG, text=empty, anchor="w", fill=palette["subtle"], font=fonts.sub)
            return y + 2 * SP_LG
        for shared in reversed(self.files[-MAX_FILES:]):
            self._draw_file_row(shared, width, y)
            y += FILE_ROW_HEIGHT
        return y

    def _draw_file_row(self, shared: SharedFile, width: int, top: int) -> None:
        palette, fonts = self.palette, self.fonts
        left, middle = SP_LG, top + FILE_ROW_HEIGHT // 2
        half = FILE_ICON_SIZE // 2
        draw_rounded(self, left, middle - half, left + FILE_ICON_SIZE, middle + half, 9, palette["selected"])
        paint_transfer_arrow(self, left + FILE_ICON_SIZE // 2, middle, palette["accent"], shared.direction == "sent")
        text_x = left + FILE_ICON_SIZE + SP_MD
        room = width - text_x - SP_LG
        name = truncate_text(shared.name, fonts.bold, room)
        self.create_text(text_x, middle - 8, text=name, anchor="w", fill=palette["text"], font=fonts.bold)
        parts = [shared.direction.capitalize(), shared.size, shared.stamp]
        detail = "  ·  ".join(part for part in parts if part)
        detail = truncate_text(detail, fonts.sub, room)
        self.create_text(text_x, middle + 10, text=detail, anchor="w", fill=palette["muted"], font=fonts.sub)
        if shared.path:
            self._regions.append((top, top + FILE_ROW_HEIGHT, lambda path=shared.path: open_folder(path)))

    def _draw_open_button(self, width: int, top: int) -> None:
        palette = self.palette
        draw_rounded(self, SP_LG, top, width - SP_LG, top + BUTTON_HEIGHT, BUTTON_HEIGHT // 2, palette["field"])
        label = "Open downloads folder"
        middle = top + BUTTON_HEIGHT // 2
        self.create_text(width // 2, middle, text=label, fill=palette["accent"], font=self.fonts.btn_bold)
        self._regions.append((top, top + BUTTON_HEIGHT, lambda: open_folder(self.downloads)))

    def _region_at(self, y: float) -> Region | None:
        return next((region for region in self._regions if region[0] <= y < region[1]), None)

    def _on_click(self, event: tk.Event) -> None:
        region = self._region_at(event.y)
        if region is not None:
            region[2]()

    def _on_motion(self, event: tk.Event) -> None:
        self.configure(cursor="hand2" if self._region_at(event.y) else "")
