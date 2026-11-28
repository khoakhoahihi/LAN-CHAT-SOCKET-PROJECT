from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass

from gui_shapes import draw_rounded
from gui_style import LIGHT, SP_LG, SP_MD, SP_SM, SP_XS, Fonts
from gui_widgets import SlimScrollbar

GROUP_WINDOW_SEC = 300
WRAP_SHARE = 0.65
MIN_WRAP_WIDTH = 120
MIN_LOG_WIDTH = 300
R_BUBBLE = 18
R_TOUCH = 6
R_TAIL = 4
CHIP_PAD_X = SP_MD
CHIP_PAD_Y = SP_XS
MESSAGE_KINDS = ("mine", "other")


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
        self._canvases: dict[int, tk.Canvas] = {}
        self._chips: list[tk.Canvas] = []
        self._last_date: str | None = None
        self._scroll_job: str | None = None
        self.text = tk.Text(
            self, state="disabled", wrap="word", width=64, height=22, relief="flat",
            highlightthickness=0, padx=SP_LG, pady=SP_MD, cursor="arrow",
        )
        self.scrollbar = SlimScrollbar(self, self.text, "bg")
        self.scrollbar.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        self.text.bind("<Configure>", self._on_text_resize)
        self.text.bind("<Destroy>", lambda _event: self.cancel_scroll())
        self._configure_tags()

    def apply_palette(self, palette: dict[str, str]) -> None:
        self.palette = palette
        self.configure(bg=palette["bg"])
        self.text.configure(bg=palette["bg"], fg=palette["text"])
        self.text.tag_configure("info", foreground=palette["subtle"], font=self.fonts.sub)
        self.text.tag_configure("error", foreground=palette["error"], font=self.fonts.sub)
        self.text.tag_configure("plain", foreground=palette["text"], font=self.fonts.mono)
        self.scrollbar.apply_palette(palette)
        self.render_all()

    def append(self, line: LogLine) -> None:
        index = len(self.lines)
        self.lines.append(line)
        previous = index - 1
        if previous in self._canvases and self._same_group(previous, index):
            self._layout_bubble(self._canvases[previous], previous)
        self._render(index)

    def render_all(self) -> None:
        for canvas in [*self._canvases.values(), *self._chips]:
            canvas.destroy()
        self._canvases.clear()
        self._chips.clear()
        self._last_date = None
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")
        for index in range(len(self.lines)):
            self._render(index)

    def message_texts(self) -> list[str]:
        return [line.text for line in self.lines if line.kind in MESSAGE_KINDS]

    def bubbles(self) -> list[tk.Canvas]:
        return [self._canvases[index] for index in sorted(self._canvases)]

    def date_chip_texts(self) -> list[str]:
        return [
            chip.itemcget(item, "text")
            for chip in self._chips
            for item in chip.find_all()
            if chip.type(item) == "text"
        ]

    def visible_text(self) -> str:
        parts = [self.text.get("1.0", "end").strip(), *self.date_chip_texts()]
        return "\n".join(part for part in parts if part)

    def has_notices_since(self, index: int) -> bool:
        return any(line.kind in ("info", "error", "plain") for line in self.lines[index:])

    def scroll_to_end(self) -> None:
        self.cancel_scroll()
        self.text.update_idletasks()
        self.text.yview_moveto(1.0)
        self._scroll_job = self.after_idle(self._finish_scroll)

    def cancel_scroll(self) -> None:
        if self._scroll_job is None:
            return
        try:
            self.after_cancel(self._scroll_job)
        except tk.TclError:
            pass
        self._scroll_job = None

    def _finish_scroll(self) -> None:
        self._scroll_job = None
        if self.text.winfo_exists():
            self.text.yview_moveto(1.0)

    def _configure_tags(self) -> None:
        self.text.tag_configure("mine", justify="right", rmargin=SP_XS)
        self.text.tag_configure("other", justify="left", lmargin1=SP_XS)
        self.text.tag_configure("group_first", spacing1=SP_LG)
        self.text.tag_configure("group_next", spacing1=2)
        self.text.tag_configure("plain", spacing1=2)
        self.text.tag_configure("date", justify="center", spacing1=SP_MD, spacing3=SP_XS)
        self.text.tag_configure("info", justify="center", spacing1=SP_SM, spacing3=SP_XS)
        self.text.tag_configure("error", justify="center", spacing1=SP_SM, spacing3=SP_XS)

    def _on_text_resize(self, event: tk.Event) -> None:
        if event.width <= 40 or abs(event.width - self.width) <= 2:
            return
        self.width = event.width
        for index, canvas in self._canvases.items():
            self._layout_bubble(canvas, index)
        self.on_resize()
        self.scroll_to_end()

    def _same_group(self, first: int, second: int) -> bool:
        if first < 0 or second >= len(self.lines):
            return False
        a, b = self.lines[first], self.lines[second]
        return (
            a.kind in MESSAGE_KINDS and a.kind == b.kind and a.who == b.who
            and a.date == b.date and b.timestamp - a.timestamp < GROUP_WINDOW_SEC
        )

    def _render(self, index: int) -> None:
        line = self.lines[index]
        self.text.configure(state="normal")
        if line.kind in MESSAGE_KINDS:
            self._render_message(index)
        else:
            self.text.insert("end", line.text + "\n", line.kind)
        self.text.configure(state="disabled")
        self.scroll_to_end()

    def _render_message(self, index: int) -> None:
        line = self.lines[index]
        if line.date != self._last_date:
            self._last_date = line.date
            self._insert_date_chip("Today")
        group = "group_next" if self._same_group(index - 1, index) else "group_first"
        canvas = tk.Canvas(self.text, highlightthickness=0, bd=0)
        self._canvases[index] = canvas
        self._layout_bubble(canvas, index)
        start = self.text.index("end-1c")
        self.text.window_create(start, window=canvas)
        self.text.tag_add(line.kind, start)
        self.text.tag_add(group, start)
        self.text.insert("end", "\n", (line.kind, group))

    def _insert_date_chip(self, label: str) -> None:
        font, palette = self.fonts.small, self.palette
        width = font.measure(label) + 2 * CHIP_PAD_X
        height = font.metrics("linespace") + 2 * CHIP_PAD_Y
        chip = tk.Canvas(self.text, width=width, height=height, highlightthickness=0, bd=0, bg=palette["bg"])
        draw_rounded(chip, 0, 0, width, height, height // 2, palette["chip"])
        chip.create_text(width // 2, height // 2, text=label, fill=palette["chip_text"], font=font)
        self._chips.append(chip)
        start = self.text.index("end-1c")
        self.text.window_create(start, window=chip)
        self.text.tag_add("date", start)
        self.text.insert("end", "\n", "date")

    def _layout_bubble(self, canvas: tk.Canvas, index: int) -> None:
        line, palette, fonts = self.lines[index], self.palette, self.fonts
        mine = line.kind == "mine"
        is_first = not self._same_group(index - 1, index)
        is_last = not self._same_group(index, index + 1)
        fill = palette["mine" if mine else "other"]
        text_color = palette["mine_text" if mine else "other_text"]
        time_color = palette["mine_time" if mine else "other_time"]
        max_width = max(int(max(self.width - 2 * SP_LG, MIN_LOG_WIDTH) * WRAP_SHARE) - 2 * SP_MD, MIN_WRAP_WIDTH)

        canvas.delete("all")
        text_id = canvas.create_text(
            0, 0, text=line.text, font=fonts.body, width=max_width, anchor="nw", fill=text_color,
        )
        box = canvas.bbox(text_id)
        text_w = box[2] - box[0] if box else 0
        text_h = box[3] - box[1] if box else fonts.body.metrics("linespace")
        show_name = not mine and is_first
        name_w = fonts.btn_bold.measure(line.who) if show_name else 0
        name_h = fonts.btn_bold.metrics("linespace") + 2 if show_name else 0
        time_w = fonts.small.measure(line.stamp) if is_last else 0
        time_h = fonts.small.metrics("linespace") if is_last else 0
        single_line = "\n" not in line.text and text_h <= fonts.body.metrics("linespace") * 1.4
        inline_time = is_last and single_line and text_w + SP_SM + time_w <= max_width
        if inline_time:
            body_w, body_h = text_w + SP_SM + time_w, max(text_h, time_h)
        else:
            body_w, body_h = max(text_w, time_w), text_h + (time_h + 2 if is_last else 0)
        width = max(name_w, body_w) + 2 * SP_MD
        height = name_h + body_h + 2 * SP_SM

        canvas.configure(width=width, height=height, bg=palette["bg"])
        draw_rounded(canvas, 0, 0, width, height, bubble_radii(mine, is_first, is_last), fill, tags=("shape",))
        canvas.tag_lower("shape")
        canvas.coords(text_id, SP_MD, SP_SM + name_h)
        if show_name:
            canvas.create_text(SP_MD, SP_SM, text=line.who, anchor="nw", fill=palette["accent"], font=fonts.btn_bold)
        if is_last:
            canvas.create_text(
                width - SP_MD, height - SP_SM, text=line.stamp, anchor="se", fill=time_color, font=fonts.small,
            )
