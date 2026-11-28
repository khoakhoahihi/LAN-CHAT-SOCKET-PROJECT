from __future__ import annotations

import math
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass

from gui_festive import TILE, draw_wallpaper
from gui_shapes import draw_rounded
from gui_style import LIGHT, SP_LG, SP_MD, SP_SM, SP_XL, SP_XS, Fonts
from gui_widgets import SlimScrollbar

GROUP_WINDOW_SEC = 300
WRAP_SHARE = 0.65
MIN_WRAP_WIDTH = 120
MIN_LOG_WIDTH = 300
R_BUBBLE = 18
R_TOUCH = 6
R_TAIL = 4
SIDE_MARGIN = SP_LG + SP_XS
FIRST_GAP = SP_LG
NEXT_GAP = 2
NOTICE_GAP = SP_SM
PLAIN_GAP = 2
CHIP_GAP = SP_MD
CHIP_PAD_X = SP_MD
CHIP_PAD_Y = SP_XS
BOTTOM_PAD = SP_MD
SCROLL_STEP = 20
RESIZE_DELAY_MS = 40
MESSAGE_KINDS = ("mine", "other")
NOTICE_KINDS = ("info", "error", "plain")


@dataclass(frozen=True)
class LogLine:
    kind: str
    who: str
    text: str
    stamp: str
    timestamp: float
    date: str


def bubble_radii(mine: bool, is_first: bool, is_last: bool) -> tuple[int, int, int, int]:
    outer_top = R_BUBBLE if is_first else R_TOUCH
    tail = R_TAIL if is_last else R_TOUCH
    if mine:
        return R_BUBBLE, outer_top, tail, R_BUBBLE
    return outer_top, R_BUBBLE, R_BUBBLE, tail


class ConversationView(tk.Frame):
    def __init__(self, parent: tk.Misc, fonts: Fonts, on_resize: Callable[[], None]) -> None:
        super().__init__(parent)
        self.fonts = fonts
        self.on_resize = on_resize
        self.palette = LIGHT
        self.lines: list[LogLine] = []
        self.width = 620
        self._tops: list[int] = []
        self._end_y = 0
        self._offset = 0
        self._wallpaper_columns = 0
        self._wallpaper_rows = 0
        self._scroll_job: str | None = None
        self._resize_job: str | None = None
        self._view_end = 1.0
        self.canvas = tk.Canvas(self, highlightthickness=0, bd=0, yscrollincrement=SCROLL_STEP)
        self.scrollbar = SlimScrollbar(self, self.canvas, "bg")
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.configure(yscrollcommand=self._on_scrolled)
        self.canvas.bind("<Configure>", self._on_resize)
        self.bind("<Destroy>", lambda _event: self.cancel_pending())

    def apply_palette(self, palette: dict[str, str]) -> None:
        self.palette = palette
        self.configure(bg=palette["bg"])
        self.canvas.configure(bg=palette["bg"])
        self.scrollbar.apply_palette(palette)
        self.render_all()

    def append(self, line: LogLine) -> None:
        index = len(self.lines)
        self.lines.append(line)
        start = index - 1 if self._same_group(index - 1, index) else index
        self._layout_from(start)
        self.scroll_to_end()

    def render_all(self) -> None:
        self.canvas.delete("all")
        self._tops.clear()
        self._end_y = 0
        self._offset = 0
        self._wallpaper_columns = 0
        self._wallpaper_rows = 0
        self._layout_from(0)
        self.scroll_to_end()

    def message_texts(self) -> list[str]:
        return [line.text for line in self.lines if line.kind in MESSAGE_KINDS]

    def message_items(self, index: int) -> list[str]:
        items = self.canvas.find_withtag(f"block{index}")
        return [self.canvas.itemcget(item, "text") for item in items if self.canvas.type(item) == "text"]

    def date_chip_texts(self) -> list[str]:
        return ["Today" for index in range(len(self.lines)) if self._starts_date(index)]

    def visible_text(self) -> str:
        notices = [line.text for line in self.lines if line.kind in NOTICE_KINDS]
        return "\n".join([*notices, *self.date_chip_texts()])

    def has_notices_since(self, index: int) -> bool:
        return any(line.kind in NOTICE_KINDS for line in self.lines[index:])

    def anchor_offset(self) -> int:
        return self._offset

    def scroll_to_end(self) -> None:
        self.cancel_scroll()
        self.canvas.update_idletasks()
        self.canvas.yview_moveto(1.0)
        self._scroll_job = self.after_idle(self._finish_scroll)

    def cancel_scroll(self) -> None:
        if self._scroll_job is None:
            return
        try:
            self.after_cancel(self._scroll_job)
        except tk.TclError:
            pass
        self._scroll_job = None

    def cancel_pending(self) -> None:
        self.cancel_scroll()
        if self._resize_job is not None:
            try:
                self.after_cancel(self._resize_job)
            except tk.TclError:
                pass
            self._resize_job = None

    def _finish_scroll(self) -> None:
        self._scroll_job = None
        if self.canvas.winfo_exists():
            self.canvas.yview_moveto(1.0)

    def _on_scrolled(self, first: str, last: str) -> None:
        self._view_end = float(last)
        self.scrollbar.set(first, last)

    def _on_resize(self, event: tk.Event) -> None:
        stuck_to_bottom = self._view_end >= 0.995
        width_changed = event.width > 40 and abs(event.width - self.width) > 2
        if width_changed:
            self.width = event.width
            self.on_resize()
            if self._resize_job is not None:
                self.after_cancel(self._resize_job)
            self._resize_job = self.after(RESIZE_DELAY_MS, self._relayout)
        self._apply_geometry()
        if stuck_to_bottom:
            self.scroll_to_end()

    def _relayout(self) -> None:
        self._resize_job = None
        self._layout_from(0)
        self.scroll_to_end()

    def _same_group(self, first: int, second: int) -> bool:
        if first < 0 or second >= len(self.lines):
            return False
        a, b = self.lines[first], self.lines[second]
        return (
            a.kind in MESSAGE_KINDS and a.kind == b.kind and a.who == b.who
            and a.date == b.date and b.timestamp - a.timestamp < GROUP_WINDOW_SEC
        )

    def _starts_date(self, index: int) -> bool:
        line = self.lines[index]
        if line.kind not in MESSAGE_KINDS:
            return False
        for previous in range(index - 1, -1, -1):
            if self.lines[previous].kind in MESSAGE_KINDS:
                return self.lines[previous].date != line.date
        return True

    def _layout_from(self, start: int) -> None:
        for index in range(start, len(self._tops)):
            self.canvas.delete(f"block{index}")
        y = self._tops[start] if start < len(self._tops) else self._end_y
        del self._tops[start:]
        for index in range(start, len(self.lines)):
            self._tops.append(y)
            y += self._draw_block(index, y + self._offset)
        self._end_y = y
        self._apply_geometry()

    def _apply_geometry(self) -> None:
        view_height = max(self.canvas.winfo_height(), 1)
        content_height = self._end_y + BOTTOM_PAD
        offset = max(0, view_height - content_height)
        if offset != self._offset:
            self.canvas.move("content", 0, offset - self._offset)
            self._offset = offset
        region_height = max(content_height, view_height)
        self.canvas.configure(scrollregion=(0, 0, self.width, region_height))
        self._fit_wallpaper(region_height)

    def _fit_wallpaper(self, height: int) -> None:
        color = self.palette.get("flake")
        if not color:
            self.canvas.delete("wallpaper")
            self._wallpaper_columns = self._wallpaper_rows = 0
            return
        columns = math.ceil(self.width / TILE)
        rows = math.ceil(height / TILE)
        if columns > self._wallpaper_columns:
            self.canvas.delete("wallpaper")
            self._wallpaper_rows = 0
        if columns > self._wallpaper_columns or rows > self._wallpaper_rows:
            draw_wallpaper(self.canvas, color, self._wallpaper_rows, rows, columns)
            self._wallpaper_columns = columns
            self._wallpaper_rows = rows

    def _draw_block(self, index: int, y: int) -> int:
        line = self.lines[index]
        tag = f"block{index}"
        if line.kind in MESSAGE_KINDS:
            return self._draw_message(index, y, tag)
        return self._draw_notice(line, y, tag)

    def _draw_notice(self, line: LogLine, y: int, tag: str) -> int:
        fonts, palette = self.fonts, self.palette
        if line.kind == "plain":
            options = {"font": fonts.mono, "fill": palette["text"], "anchor": "nw", "justify": "left"}
            x, gap = SP_LG, PLAIN_GAP
        else:
            color = palette["error"] if line.kind == "error" else palette["subtle"]
            options = {"font": fonts.sub, "fill": color, "anchor": "n", "justify": "center"}
            x, gap = self.width // 2, NOTICE_GAP
        item = self.canvas.create_text(
            x, y + gap, text=line.text, width=self.width - 2 * SP_XL, tags=(tag, "content"), **options,
        )
        box = self.canvas.bbox(item) or (0, 0, 0, 0)
        if palette["flake"] and line.kind != "plain" and box[2] > box[0]:
            self._draw_notice_backing(box, item, tag)
        return gap + (box[3] - box[1]) + SP_XS

    def _draw_notice_backing(self, box: tuple[int, int, int, int], item: int, tag: str) -> None:
        shape = f"{tag}-backing"
        left, top, right, bottom = box[0] - SP_SM, box[1] - 2, box[2] + SP_SM, box[3] + 2
        radius = (bottom - top) // 2
        draw_rounded(self.canvas, left, top, right, bottom, radius, self.palette["bg"], tags=(tag, "content", shape))
        self.canvas.tag_lower(shape, item)

    def _draw_message(self, index: int, y: int, tag: str) -> int:
        height = 0
        if self._starts_date(index):
            height += self._draw_chip(y, tag, "Today")
        gap = NEXT_GAP if self._same_group(index - 1, index) else FIRST_GAP
        return height + gap + self._draw_bubble(index, y + height + gap, tag)

    def _draw_chip(self, y: int, tag: str, label: str) -> int:
        font, palette = self.fonts.small, self.palette
        width = font.measure(label) + 2 * CHIP_PAD_X
        height = font.metrics("linespace") + 2 * CHIP_PAD_Y
        left, top = (self.width - width) // 2, y + CHIP_GAP
        box = (left, top, left + width, top + height)
        draw_rounded(self.canvas, *box, height // 2, palette["chip"], tags=(tag, "content"))
        self.canvas.create_text(
            self.width // 2, top + height // 2, text=label, fill=palette["chip_text"], font=font, tags=(tag, "content"),
        )
        return CHIP_GAP + height

    def _draw_bubble(self, index: int, top: int, tag: str) -> int:
        line, palette, fonts, canvas = self.lines[index], self.palette, self.fonts, self.canvas
        mine = line.kind == "mine"
        is_first = not self._same_group(index - 1, index)
        is_last = not self._same_group(index, index + 1)
        max_width = max(int(max(self.width - 2 * SP_LG, MIN_LOG_WIDTH) * WRAP_SHARE) - 2 * SP_MD, MIN_WRAP_WIDTH)
        tags = (tag, "content")

        text_id = canvas.create_text(
            0, 0, text=line.text, font=fonts.body, width=max_width, anchor="nw",
            fill=palette["mine_text" if mine else "other_text"], tags=tags,
        )
        box = canvas.bbox(text_id) or (0, 0, 0, fonts.body.metrics("linespace"))
        text_w, text_h = box[2] - box[0], box[3] - box[1]
        show_name = not mine and is_first
        name_w = fonts.btn_bold.measure(line.who) if show_name else 0
        name_h = fonts.btn_bold.metrics("linespace") + 2 if show_name else 0
        time_w = fonts.small.measure(line.stamp) if is_last else 0
        time_h = fonts.small.metrics("linespace") if is_last else 0
        single_line = "\n" not in line.text and text_h <= fonts.body.metrics("linespace") * 1.4
        if is_last and single_line and text_w + SP_SM + time_w <= max_width:
            body_w, body_h = text_w + SP_SM + time_w, max(text_h, time_h)
        else:
            body_w, body_h = max(text_w, time_w), text_h + (time_h + 2 if is_last else 0)
        width = max(name_w, body_w) + 2 * SP_MD
        height = name_h + body_h + 2 * SP_SM
        left = self.width - SIDE_MARGIN - width if mine else SIDE_MARGIN

        shape = f"shape{index}"
        radii = bubble_radii(mine, is_first, is_last)
        fill = palette["mine" if mine else "other"]
        draw_rounded(canvas, left, top, left + width, top + height, radii, fill, tags=(*tags, shape))
        canvas.tag_lower(shape, text_id)
        canvas.coords(text_id, left + SP_MD, top + SP_SM + name_h)
        if show_name:
            canvas.create_text(
                left + SP_MD, top + SP_SM, text=line.who, anchor="nw",
                fill=palette["accent"], font=fonts.btn_bold, tags=tags,
            )
        if is_last:
            canvas.create_text(
                left + width - SP_MD, top + height - SP_SM, text=line.stamp, anchor="se",
                fill=palette["mine_time" if mine else "other_time"], font=fonts.small, tags=tags,
            )
        return height
