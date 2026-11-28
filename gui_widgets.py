from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from tkinter import font as tkfont

from gui_shapes import draw_avatar, draw_circle, draw_presence_dot, draw_rounded, draw_rounded_border
from gui_style import LIGHT, SP_LG, SP_MD, SP_SM, STATUS_COLORS, Fonts, truncate_text

SCROLL_W = 8
MIN_THUMB = 24
THUMB_INSET = 2
FIELD_INSET = 2
MIN_ENTRY_WIDTH = 40
MIN_BUTTON_WIDTH = 60

IconPainter = Callable[[tk.Canvas, int, int, str, str], None]


def paint_plus(canvas: tk.Canvas, cx: int, cy: int, color: str, fill: str) -> None:
    canvas.create_line(cx - 6, cy, cx + 6, cy, fill=color, width=2, capstyle="round")
    canvas.create_line(cx, cy - 6, cx, cy + 6, fill=color, width=2, capstyle="round")


def paint_send(canvas: tk.Canvas, cx: int, cy: int, color: str, fill: str) -> None:
    points = (cx - 6, cy - 6, cx + 7, cy, cx - 6, cy + 6, cx - 3, cy)
    canvas.create_polygon(points, fill=color, outline=color, width=1, joinstyle="round")


def paint_search(canvas: tk.Canvas, cx: int, cy: int, color: str, fill: str) -> None:
    canvas.create_oval(cx - 5, cy - 5, cx + 3, cy + 3, outline=color, width=1.6)
    canvas.create_line(cx + 2, cy + 2, cx + 6, cy + 6, fill=color, width=1.8, capstyle="round")


def paint_moon(canvas: tk.Canvas, cx: int, cy: int, color: str, fill: str) -> None:
    draw_circle(canvas, cx - 6, cy - 6, 12, color)
    draw_circle(canvas, cx - 2, cy - 8, 11, fill)


def paint_file(canvas: tk.Canvas, cx: int, cy: int, color: str, fill: str) -> None:
    outline = (cx - 5, cy - 7, cx + 2, cy - 7, cx + 6, cy - 3, cx + 6, cy + 7, cx - 5, cy + 7)
    canvas.create_polygon(outline, fill="", outline=color, width=1.6, joinstyle="round")
    canvas.create_line(cx + 2, cy - 7, cx + 2, cy - 3, cx + 6, cy - 3, fill=color, width=1.6, joinstyle="round")


def paint_close(canvas: tk.Canvas, cx: int, cy: int, color: str, fill: str) -> None:
    canvas.create_line(cx - 5, cy - 5, cx + 5, cy + 5, fill=color, width=2, capstyle="round")
    canvas.create_line(cx - 5, cy + 5, cx + 5, cy - 5, fill=color, width=2, capstyle="round")


RAY_DIRECTIONS = ((0, -1), (0, 1), (-1, 0), (1, 0), (-0.7, -0.7), (0.7, -0.7), (-0.7, 0.7), (0.7, 0.7))


def paint_sun(canvas: tk.Canvas, cx: int, cy: int, color: str, fill: str) -> None:
    draw_circle(canvas, cx - 4, cy - 4, 8, color)
    for dx, dy in RAY_DIRECTIONS:
        canvas.create_line(
            cx + dx * 6, cy + dy * 6, cx + dx * 8.5, cy + dy * 8.5, fill=color, width=1.6, capstyle="round",
        )


class SlimScrollbar(tk.Canvas):
    def __init__(self, parent: tk.Misc, target: tk.Widget, surface: str) -> None:
        super().__init__(parent, width=SCROLL_W, highlightthickness=0, bd=0)
        self.parent = parent
        self.target = target
        self.surface = surface
        self.palette = LIGHT
        self.first = 0.0
        self.last = 1.0
        self.thumb_hovered = False
        self.pointer_inside = False
        target.configure(yscrollcommand=self.set)
        self.bind("<Configure>", lambda _event: self.draw())
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._drag)
        self.bind("<B1-Motion>", self._drag)
        parent.bind("<Enter>", self._on_parent_enter, add="+")
        parent.bind("<Leave>", self._on_parent_leave, add="+")

    def apply_palette(self, palette: dict[str, str]) -> None:
        self.palette = palette
        self.configure(bg=palette[self.surface])
        self.draw()

    def set(self, first: str, last: str) -> None:
        self.first = float(first)
        self.last = float(last)
        self.draw()

    def draw(self) -> None:
        self.delete("thumb")
        scrollable = self.first > 0.0 or self.last < 1.0
        if not (self.pointer_inside and scrollable):
            return
        height = max(self.winfo_height(), 1)
        top = max(0.0, min(self.first * height, height - MIN_THUMB))
        bottom = min(height, max(self.last * height, self.first * height + MIN_THUMB))
        color = self.palette["scroll_hover" if self.thumb_hovered else "scroll"]
        draw_rounded(
            self, THUMB_INSET, int(top) + THUMB_INSET, SCROLL_W - THUMB_INSET, int(bottom) - THUMB_INSET,
            (SCROLL_W - 2 * THUMB_INSET) // 2, color, tags=("thumb",),
        )

    def _drag(self, event: tk.Event) -> None:
        span = self.last - self.first
        fraction = event.y / max(self.winfo_height(), 1) - span / 2
        self.target.yview_moveto(max(0.0, min(1.0 - span, fraction)))

    def _on_enter(self, _event: tk.Event) -> None:
        self.pointer_inside = True
        self.thumb_hovered = True
        self.draw()

    def _on_leave(self, _event: tk.Event) -> None:
        self.thumb_hovered = False
        self.draw()

    def _on_parent_enter(self, _event: tk.Event) -> None:
        self.pointer_inside = True
        self.draw()

    def _on_parent_leave(self, event: tk.Event) -> None:
        under = self.parent.winfo_containing(event.x_root, event.y_root)
        self.pointer_inside = under is not None and (under is self.parent or str(under).startswith(f"{self.parent}."))
        self.draw()


class PillInput(tk.Canvas):
    def __init__(
        self, parent: tk.Misc, height: int, hint: str, font: tkfont.Font, padding: int,
        with_search_icon: bool, on_change: Callable[[], None], surface: str,
    ) -> None:
        super().__init__(parent, height=height, highlightthickness=0, bd=0)
        self.height = height
        self.hint = hint
        self.font = font
        self.padding = padding
        self.with_search_icon = with_search_icon
        self.on_change = on_change
        self.surface = surface
        self.palette = LIGHT
        self.focused = False
        self.dimmed = False
        self.variable = tk.StringVar()
        self.entry = tk.Entry(self, textvariable=self.variable, font=font, relief="flat", bd=0, highlightthickness=0)
        self.placeholder = tk.Label(self, text=hint, font=font, anchor="w", cursor="xterm")
        self.entry_window = self.create_window(
            padding, height // 2, window=self.entry, anchor="w", width=MIN_ENTRY_WIDTH,
        )
        self.variable.trace_add("write", self._on_write)
        self.placeholder.bind("<Button-1>", lambda _event: self.entry.focus_set())
        self.entry.bind("<FocusIn>", lambda _event: self._set_focus(True))
        self.entry.bind("<FocusOut>", lambda _event: self._set_focus(False))
        self.bind("<Configure>", lambda _event: self.draw())

    def apply_palette(self, palette: dict[str, str]) -> None:
        self.palette = palette
        self.configure(bg=palette[self.surface])
        self.entry.configure(fg=palette["text"], insertbackground=palette["text"])
        self.placeholder.configure(fg=palette["subtle"])
        self.draw()

    def draw(self) -> None:
        palette, height = self.palette, self.height
        width = max(self.winfo_width(), 120)
        for item in self.find_all():
            if item != self.entry_window:
                self.delete(item)
        radius = (height - 2 * FIELD_INSET) // 2
        if self.focused and not self.dimmed:
            draw_rounded(self, 0, 0, width, height, height // 2, palette["ring"])
            border, fill = palette["accent"], palette["field_focus"]
        else:
            border = palette["line"]
            fill = palette["panel"] if self.dimmed else palette["field"]
        box = (FIELD_INSET, FIELD_INSET, width - FIELD_INSET, height - FIELD_INSET)
        draw_rounded_border(self, *box, radius, fill, border)
        if self.with_search_icon:
            paint_search(self, 20, height // 2, palette["subtle"], fill)
        inner_width = max(width - self.padding - SP_LG, MIN_ENTRY_WIDTH)
        cursor = "arrow" if self.dimmed else "xterm"
        self.entry.configure(bg=fill, cursor=cursor, takefocus=0 if self.dimmed else 1)
        self.placeholder.configure(bg=fill, text=truncate_text(self.hint, self.font, inner_width), cursor=cursor)
        self.coords(self.entry_window, self.padding, height // 2)
        self.itemconfigure(self.entry_window, width=inner_width)
        self.tag_raise(self.entry_window)
        if self.variable.get():
            self.placeholder.place_forget()
        else:
            self.placeholder.place(x=self.padding, y=height // 2, anchor="w")

    def _set_focus(self, focused: bool) -> None:
        self.focused = focused
        self.draw()

    def _on_write(self, *_args: object) -> None:
        self.on_change()
        self.draw()


class _PressableCanvas(tk.Canvas):
    def __init__(self, parent: tk.Misc, command: Callable[[], None], surface: str, **options: object) -> None:
        super().__init__(parent, highlightthickness=0, bd=0, cursor="hand2", **options)
        self.command = command
        self.surface = surface
        self.palette = LIGHT
        self.hovered = False
        self.pressed = False
        self.enabled = True
        self.bind("<Enter>", lambda _event: self._set_hover(True))
        self.bind("<Leave>", lambda _event: self._set_hover(False))
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Configure>", lambda _event: self.draw())

    def apply_palette(self, palette: dict[str, str]) -> None:
        self.palette = palette
        self.configure(bg=palette[self.surface])
        self.draw()

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        self.configure(cursor="hand2" if enabled else "")
        self.draw()

    def draw(self) -> None:
        raise NotImplementedError

    def _set_hover(self, hovered: bool) -> None:
        self.hovered = hovered
        self.pressed = self.pressed and hovered
        self.draw()

    def _on_press(self, _event: tk.Event) -> None:
        self.pressed = self.enabled
        self.draw()

    def _on_release(self, _event: tk.Event) -> None:
        was_pressed = self.pressed
        self.pressed = False
        self.draw()
        if was_pressed and self.hovered:
            self.command()


class IconButton(_PressableCanvas):
    def __init__(
        self, parent: tk.Misc, size: int, icon: IconPainter, command: Callable[[], None],
        surface: str, accent: bool,
    ) -> None:
        super().__init__(parent, command, surface, width=size, height=size)
        self.size = size
        self.icon = icon
        self.accent = accent

    def draw(self) -> None:
        palette = self.palette
        self.delete("all")
        if self.accent:
            state = "accent" if self.enabled else "disabled"
            if self.enabled and self.pressed:
                state = "accent_press"
            elif self.enabled and self.hovered:
                state = "accent_hover"
            fill = palette[state]
            icon_color = palette["on_accent"] if self.enabled else palette["subtle"]
        else:
            fill = palette[self.surface]
            if self.pressed:
                fill = palette["line"]
            elif self.hovered:
                fill = palette["hover"]
            icon_color = palette["text"] if self.hovered else palette["muted"]
        draw_circle(self, 0, 0, self.size, fill)
        self.icon(self, self.size // 2, self.size // 2, icon_color, fill)


class PillButton(_PressableCanvas):
    def __init__(
        self, parent: tk.Misc, height: int, font: tkfont.Font, command: Callable[[], None], surface: str,
    ) -> None:
        super().__init__(parent, command, surface, width=MIN_BUTTON_WIDTH, height=height)
        self.height = height
        self.font = font
        self.text = ""
        self.dot_color: str | None = None
        self.icon: IconPainter | None = None

    def configure_content(self, text: str, dot_color: str | None = None, icon: IconPainter | None = None) -> None:
        self.text = text
        self.dot_color = dot_color
        self.icon = icon
        self.draw()

    def draw(self) -> None:
        palette = self.palette
        self.delete("all")
        width = max(self.winfo_width(), 60)
        fill = palette["line"] if self.pressed else (palette["hover"] if self.hovered else palette["field"])
        draw_rounded(self, 0, 0, width, self.height, self.height // 2, fill)
        left = SP_MD + 2
        middle = self.height // 2
        if self.dot_color:
            draw_circle(self, left, middle - 4, 8, self.dot_color)
            left += 8 + SP_SM
        elif self.icon:
            self.icon(self, left + 6, middle, palette["muted"], fill)
            left += 12 + SP_SM
        label = truncate_text(self.text, self.font, width - left - SP_MD)
        self.create_text(left, middle, text=label, anchor="w", fill=palette["text"], font=self.font)


class ProfileCard(_PressableCanvas):
    def __init__(
        self, parent: tk.Misc, name: str, height: int, fonts: Fonts, command: Callable[[], None], surface: str,
    ) -> None:
        super().__init__(parent, command, surface, width=MIN_BUTTON_WIDTH, height=height)
        self.name = name
        self.height = height
        self.fonts = fonts
        self.status = "online"

    def configure_content(self, status: str) -> None:
        self.status = status
        self.draw()

    def draw(self) -> None:
        palette, fonts = self.palette, self.fonts
        self.delete("all")
        width = max(self.winfo_width(), MIN_BUTTON_WIDTH)
        if self.pressed:
            draw_rounded(self, 0, 0, width, self.height, 12, palette["line"])
        elif self.hovered:
            draw_rounded(self, 0, 0, width, self.height, 12, palette["hover"])
        size = 34
        x, y = SP_SM, (self.height - size) // 2
        draw_avatar(self, x, y, size, self.name, fonts.bold)
        draw_presence_dot(self, x + size - 9, y + size - 9, 9, STATUS_COLORS[self.status], palette[self.surface])
        text_x = x + size + SP_SM
        room = width - text_x - SP_SM
        self.create_text(
            text_x, self.height // 2 - 8, text=truncate_text(self.name, fonts.bold, room),
            anchor="w", fill=palette["text"], font=fonts.bold,
        )
        self.create_text(
            text_x, self.height // 2 + 9, text=self.status.capitalize(), anchor="w", fill=palette["muted"], font=fonts.sub,
        )
