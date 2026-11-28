from __future__ import annotations

import queue
import re
import sys
import threading
import time
import tkinter as tk
from collections.abc import Callable, Iterator
from tkinter import filedialog
from typing import NamedTuple

from gui_chat import ConversationView, LogLine
from gui_shapes import draw_avatar, draw_circle, draw_presence_dot, draw_rounded, draw_rounded_border
from gui_style import (
    DARK, LIGHT, SP_LG, SP_MD, SP_SM, SP_XL, SP_XS, STATUS_COLORS, Fonts, classify, truncate_text,
)
from gui_widgets import (
    SCROLL_W, IconButton, PillButton, PillInput, SlimScrollbar,
    paint_moon, paint_plus, paint_send, paint_sun,
)

POLL_MS = 100
CLOSE_DELAY_MS = 400
WINDOW_WIDTH = 960
WINDOW_HEIGHT = 640
WINDOW_MIN_WIDTH = 640
WINDOW_MIN_HEIGHT = 440
SIDE_WIDE = 300
SIDE_MID = 250
SIDE_NARROW = 220
MID_BELOW = 900
NARROW_BELOW = 800
HEADER_H = 64
ROW_HEIGHT = 60
ROW_INSET = 8
ROW_RADIUS = 10
AVATAR_SIZE = 40
DOT_SIZE = 10
SEARCH_H = 36
FOOTER_BUTTON_H = 32
INPUT_H = 40
ATTACH_SIZE = 36
CARD_H = 54
CARD_RADIUS = 12
BADGE_H = 20
EMPTY_ART_H = 150

EMPTY_TITLE = "No chat selected"
EMPTY_PROMPT = "Select someone on the left to start chatting"
EMPTY_INPUT = "Select a chat to message..."

TRANSFER_START = re.compile(r"(?:Sending|Receiving) (.+) \(.+\) \.\.\.")
TRANSFER_STEP = re.compile(r"(.+)  (\d+)%")
TRANSFER_DONE_PREFIXES = ("Sent ", "Received ")


class PeerRow(NamedTuple):
    name: str
    status: str
    unread: int
    active: bool


class GuiTerminal:
    def __init__(self, name: str, store: object, state: object, stop: threading.Event) -> None:
        self.name = name
        self.store = store
        self.state = state
        self.stop = stop
        self._out: queue.Queue[str] = queue.Queue()
        self._in: queue.Queue[str] = queue.Queue()
        self._paints: list[tuple[tk.Widget, dict[str, str]]] = []
        self._themed: list[object] = []
        self._rows: list[PeerRow] = []
        self._peer_names: list[str] = []
        self._signature: tuple = ()
        self._partner: object | None = None
        self._hover_index: int | None = None
        self._status = "online"
        self._dark = False
        self._transferring = False
        self._transfer_label = ""
        self.transfer_percent = 0
        self._empty_visible = True
        self._empty_since = 0
        self._send_active = False
        self._side_width = SIDE_WIDE
        self.palette = LIGHT
        self._enable_sharp_text()
        self.root = tk.Tk()
        self.root.title(f"LAN Chat - {name}")
        self.root.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}")
        self.root.minsize(WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT)
        self.fonts = Fonts()
        self._build_side()
        self._build_chat()
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.bind("<Configure>", self._on_root_resize)
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.root.bind_all(sequence, self._wheel)
        self._apply_theme()

    def say(self, text: str) -> None:
        self._out.put(text)

    def lines(self) -> Iterator[str]:
        while not self.stop.is_set():
            try:
                yield self._in.get(timeout=POLL_MS / 1000)
            except queue.Empty:
                pass

    def run(self, work: Callable[[], None]) -> None:
        worker = threading.Thread(target=work)
        worker.start()
        self._poll()
        self.entry.focus_set()
        self.root.mainloop()
        self.stop.set()
        worker.join()

    def set_theme(self, name: str) -> None:
        self._dark = name == "dark"
        self._apply_theme()

    def message_texts(self) -> list[str]:
        return self.view.message_texts()

    def sidebar_texts(self) -> list[str]:
        canvas = self.peers
        return [canvas.itemcget(item, "text") for item in canvas.find_all() if canvas.type(item) == "text"]

    def partner_title(self) -> str:
        return self.partner.cget("text")

    def transfer_active(self) -> bool:
        return self._transferring

    def log_text(self) -> str:
        parts = [self.view.visible_text(), *self.message_texts()]
        if self._empty_visible:
            parts.append(EMPTY_PROMPT)
        return "\n".join(part for part in parts if part)

    @staticmethod
    def _enable_sharp_text() -> None:
        if sys.platform != "win32":
            return
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass

    def _paint(self, widget: tk.Widget, **mapping: str) -> tk.Widget:
        self._paints.append((widget, mapping))
        return widget

    def _hairline(self, parent: tk.Misc, vertical: bool = False, **pack: object) -> None:
        line = self._paint(tk.Frame(parent), bg="line")
        if vertical:
            line.configure(width=1)
        else:
            line.configure(height=1)
        line.pack(**pack)

    def _build_side(self) -> None:
        self.side = self._paint(tk.Frame(self.root, width=SIDE_WIDE), bg="panel")
        self.side.pack(side="left", fill="y")
        self.side.pack_propagate(False)

        top = self._paint(tk.Frame(self.side, height=HEADER_H, padx=SP_MD, pady=SP_MD), bg="panel")
        top.pack(fill="x")
        top.pack_propagate(False)
        self.search = PillInput(top, SEARCH_H, "Search", self.fonts.btn, 36, True, self._refresh, "panel")
        self.search.pack(fill="both", expand=True)
        self.search_entry = self.search.entry
        self._themed.append(self.search)

        body = self._paint(tk.Frame(self.side), bg="panel")
        body.pack(fill="both", expand=True)
        self.peers = self._paint(
            tk.Canvas(body, width=SIDE_WIDE - SCROLL_W, highlightthickness=0, bd=0), bg="panel",
        )
        self.peers_scroll = SlimScrollbar(body, self.peers, "panel")
        self.peers_scroll.pack(side="right", fill="y")
        self.peers.pack(side="left", fill="both", expand=True)
        self.peers.bind("<Button-1>", self._on_peer_click)
        self.peers.bind("<Motion>", self._on_peer_motion)
        self.peers.bind("<Leave>", self._on_peer_leave)
        self._themed.append(self.peers_scroll)

        footer = self._paint(tk.Frame(self.side, padx=SP_MD, pady=SP_MD), bg="panel")
        footer.pack(fill="x")
        self.status_button = PillButton(footer, FOOTER_BUTTON_H, self.fonts.btn, self._change_status, "panel")
        self.status_button.pack(side="left", fill="x", expand=True, padx=(0, SP_XS))
        self.theme_button = PillButton(footer, FOOTER_BUTTON_H, self.fonts.btn, self._toggle_theme, "panel")
        self.theme_button.pack(side="left", fill="x", expand=True, padx=(SP_XS, 0))
        self._themed.extend((self.status_button, self.theme_button))
        self._hairline(self.root, vertical=True, side="left", fill="y")

    def _build_chat(self) -> None:
        self.main = self._paint(tk.Frame(self.root), bg="bg")
        self.main.pack(side="left", fill="both", expand=True)
        self._build_header()
        self._hairline(self.main, fill="x")
        self._build_input()
        self.chat_body = self._paint(tk.Frame(self.main), bg="bg")
        self.chat_body.pack(side="top", fill="both", expand=True)
        self.view = ConversationView(self.chat_body, self.fonts, self._on_view_resize)
        self.log = self.view.text
        self.view.pack(fill="both", expand=True)
        self._themed.append(self.view)
        self.empty_hint = self._paint(
            tk.Label(self.chat_body, text=EMPTY_PROMPT, font=self.fonts.btn, anchor="center"), bg="bg", fg="subtle",
        )
        self.empty_state = self._paint(tk.Frame(self.chat_body), bg="bg")
        self.empty_art = self._paint(tk.Canvas(self.empty_state, highlightthickness=0, bd=0), bg="bg")
        self.empty_art.place(relx=0.5, rely=0.5, anchor="center")

    def _build_header(self) -> None:
        header = self._paint(tk.Frame(self.main, height=HEADER_H, padx=SP_LG), bg="bg")
        header.pack(fill="x")
        header.pack_propagate(False)
        self.header_avatar = self._paint(
            tk.Canvas(header, width=AVATAR_SIZE + 2, height=AVATAR_SIZE + 2, highlightthickness=0, bd=0), bg="bg",
        )
        self.header_info = self._paint(tk.Frame(header), bg="bg")
        self.header_info.pack(side="left", fill="x", expand=True, pady=(0, 2))
        self.partner = self._paint(
            tk.Label(self.header_info, text=EMPTY_TITLE, font=self.fonts.title, anchor="w", pady=0, bd=0),
            bg="bg", fg="text",
        )
        self.partner.pack(fill="x")
        self.partner_sub = self._paint(
            tk.Label(self.header_info, text=EMPTY_PROMPT, font=self.fonts.sub, anchor="w", pady=0, bd=0),
            bg="bg", fg="muted",
        )
        self.partner_sub.pack(fill="x")

    def _build_input(self) -> None:
        self.transfer = self._paint(tk.Frame(self.main, padx=SP_LG, pady=SP_SM), bg="bg")
        self.transfer_card = self._paint(
            tk.Canvas(self.transfer, height=CARD_H, highlightthickness=0, bd=0), bg="bg",
        )
        self.transfer_card.pack(fill="x")
        self.transfer_card.bind("<Configure>", lambda _event: self._draw_transfer_card())

        self.input_bar = self._paint(tk.Frame(self.main, padx=SP_LG, pady=SP_MD), bg="bg")
        self.input_bar.pack(side="bottom", fill="x")
        self._hairline(self.main, side="bottom", fill="x")
        self.file_button = IconButton(self.input_bar, ATTACH_SIZE, paint_plus, self._send_file, "bg", accent=False)
        self.file_button.pack(side="left", padx=(0, SP_SM))
        self.input = PillInput(
            self.input_bar, INPUT_H, EMPTY_INPUT, self.fonts.body, SP_LG, False, self._on_input_change, "bg",
        )
        self.input.pack(side="left", fill="x", expand=True)
        self.entry = self.input.entry
        self.entry.bind("<Return>", self._submit)
        self.entry.bind("<KeyPress>", self._block_typing)
        self.send_button = IconButton(self.input_bar, INPUT_H, paint_send, self._submit, "bg", accent=True)
        self.send_button.pack(side="left", padx=(SP_SM, 0))
        self.send_button.set_enabled(False)
        self._themed.extend((self.file_button, self.input, self.send_button))

    def _apply_theme(self) -> None:
        self.palette = palette = DARK if self._dark else LIGHT
        for widget, mapping in self._paints:
            widget.configure(**{option: palette[key] for option, key in mapping.items()})
        self.root.configure(bg=palette["bg"])
        for themed in self._themed:
            themed.apply_palette(palette)
        self._update_footer_buttons()
        if self._transferring:
            self._draw_transfer_card()
        self._signature = ()
        self._refresh()

    def _update_footer_buttons(self) -> None:
        self.status_button.configure_content(self._status.capitalize(), dot_color=STATUS_COLORS[self._status])
        label, icon = ("Light", paint_sun) if self._dark else ("Dark", paint_moon)
        self.theme_button.configure_content(label, icon=icon)

    def _toggle_theme(self) -> None:
        self.set_theme("light" if self._dark else "dark")

    def _on_root_resize(self, event: tk.Event) -> None:
        if event.widget is not self.root or event.width < 100:
            return
        if event.width < NARROW_BELOW:
            side_width = SIDE_NARROW
        elif event.width < MID_BELOW:
            side_width = SIDE_MID
        else:
            side_width = SIDE_WIDE
        if side_width == self._side_width:
            return
        self._side_width = side_width
        self.side.configure(width=side_width)
        self.peers.configure(width=side_width - SCROLL_W)
        self._draw_peers()

    def _on_view_resize(self) -> None:
        self._update_header(self._partner)

    def _wheel(self, event: tk.Event) -> None:
        step = -1 if event.num == 4 or getattr(event, "delta", 0) > 0 else 1
        if event.widget in (self.peers, self.peers_scroll):
            target, scrollable = self.peers, self.peers.yview() != (0.0, 1.0)
        else:
            target, scrollable = self.log, self.empty_state.winfo_manager() != "place"
        if scrollable:
            target.yview_scroll(step * 2, "units")

    def _on_peer_click(self, event: tk.Event) -> None:
        self._open_chat(int(self.peers.canvasy(event.y) // ROW_HEIGHT))

    def _on_peer_motion(self, event: tk.Event) -> None:
        self._hover_peer(int(self.peers.canvasy(event.y) // ROW_HEIGHT))

    def _on_peer_leave(self, _event: tk.Event) -> None:
        self._hover_peer(None)

    def _hover_peer(self, index: int | None) -> None:
        if index is not None and not 0 <= index < len(self._rows):
            index = None
        if index == self._hover_index:
            return
        self._hover_index = index
        self.peers.configure(cursor="hand2" if index is not None else "")
        self._draw_peers()

    def _block_typing(self, _event: tk.Event) -> str | None:
        return "break" if self._empty_visible else None

    def _on_input_change(self) -> None:
        self._send_active = bool(self.entry.get().strip())
        self.send_button.set_enabled(self._send_active)

    def _submit(self, _event: object = None) -> None:
        text = self.entry.get().strip()
        if text:
            self._in.put(text)
            self.entry.delete(0, "end")

    def _send_file(self) -> None:
        path = filedialog.askopenfilename()
        if path:
            self._in.put(f"/send {path}")

    def _open_chat(self, index: int) -> None:
        if 0 <= index < len(self._peer_names):
            self._in.put(f"/chat {self._peer_names[index]}")

    def _change_status(self) -> None:
        self._status = "away" if self._status == "online" else "online"
        self._update_footer_buttons()
        self._in.put(f"/status {self._status}")

    def _consume_transfer_notice(self, kind: str, text: str) -> bool:
        if kind == "info":
            step = TRANSFER_STEP.fullmatch(text)
            if step and self._transferring:
                self.transfer_percent = int(step[2])
                self._draw_transfer_card()
                return True
            start = TRANSFER_START.fullmatch(text)
            if start:
                self._begin_transfer(start[1])
                return False
            if text.startswith(TRANSFER_DONE_PREFIXES):
                self._end_transfer()
        elif kind == "error":
            self._end_transfer()
        return False

    def _begin_transfer(self, filename: str) -> None:
        self._transfer_label = f"Transferring {filename}"
        self.transfer_percent = 0
        self._transferring = True
        self.transfer.pack(side="bottom", fill="x", before=self.chat_body)
        self._draw_transfer_card()

    def _end_transfer(self) -> None:
        self.transfer.pack_forget()
        self._transferring = False

    def _drain(self) -> None:
        while not self._out.empty():
            for raw in self._out.get_nowait().split("\n"):
                kind, who, text = classify(raw, self.name)
                if self._consume_transfer_notice(kind, text):
                    continue
                line = LogLine(kind, who, text, time.strftime("%H:%M"), time.time(), time.strftime("%Y-%m-%d"))
                self.view.append(line)
        self._sync_empty_view()

    def _refresh(self) -> None:
        query = self.search_entry.get().strip().lower()
        peers = [peer for peer in self.store.get_peers() if not query or query in peer.name.lower()]
        current = self.state.current
        rows = [PeerRow(p.name, p.status, self.state.unread(p.peer_id), p.peer_id == current) for p in peers]
        partner = self.store.get(current) if current else None
        about = (partner.name, partner.status, partner.ip, partner.tcp_port) if partner else None
        signature = (tuple(rows), about, self._dark, query)
        if signature == self._signature:
            return
        was_empty = self._empty_visible
        self._signature = signature
        self._rows = rows
        self._peer_names = [row.name for row in rows]
        self._partner = partner
        if not was_empty and not partner:
            self._empty_since = len(self.view.lines)
        self._draw_peers()
        self._update_header(partner)
        self.search.draw()

    def _draw_peers(self) -> None:
        canvas, palette = self.peers, self.palette
        canvas.delete("all")
        width = self._side_width - SCROLL_W
        for index, row in enumerate(self._rows):
            top = index * ROW_HEIGHT
            if row.active:
                background = palette["selected"]
            elif index == self._hover_index:
                background = palette["hover"]
            else:
                background = palette["panel"]
            if background != palette["panel"]:
                row_box = (ROW_INSET, top + 2, width - ROW_INSET, top + ROW_HEIGHT - 2)
                draw_rounded(canvas, *row_box, ROW_RADIUS, background)
            self._draw_peer_row(canvas, row, top, width, background)
        canvas.configure(scrollregion=(0, 0, width, max(len(self._rows) * ROW_HEIGHT, 1)))

    def _draw_peer_row(self, canvas: tk.Canvas, row: PeerRow, top: int, width: int, background: str) -> None:
        palette, fonts = self.palette, self.fonts
        x = ROW_INSET + SP_SM
        y = top + (ROW_HEIGHT - AVATAR_SIZE) // 2
        draw_avatar(canvas, x, y, AVATAR_SIZE, row.name, fonts.bold)
        dot = (x + AVATAR_SIZE - DOT_SIZE, y + AVATAR_SIZE - DOT_SIZE)
        draw_presence_dot(canvas, dot[0], dot[1], DOT_SIZE, STATUS_COLORS[row.status], background)
        right = width - ROW_INSET - SP_SM
        if row.unread:
            right = self._draw_unread_badge(canvas, row.unread, right, top) - SP_SM
        text_x = x + AVATAR_SIZE + SP_MD
        name = truncate_text(row.name, fonts.bold, right - text_x)
        canvas.create_text(text_x, top + 22, text=name, anchor="w", fill=palette["text"], font=fonts.bold)
        canvas.create_text(text_x, top + 40, text=row.status, anchor="w", fill=palette["muted"], font=fonts.sub)

    def _draw_unread_badge(self, canvas: tk.Canvas, unread: int, right: int, top: int) -> int:
        label = "99+" if unread > 99 else str(unread)
        left = right - max(BADGE_H, self.fonts.btn_bold.measure(label) + SP_MD)
        badge_top = top + (ROW_HEIGHT - BADGE_H) // 2
        draw_rounded(canvas, left, badge_top, right, badge_top + BADGE_H, BADGE_H // 2, self.palette["accent"])
        centre = ((left + right) // 2, badge_top + BADGE_H // 2)
        canvas.create_text(*centre, text=label, fill=self.palette["on_accent"], font=self.fonts.btn_bold)
        return left

    def _update_header(self, partner: object | None) -> None:
        self._empty_visible = partner is None
        available = max(self.view.width - 2 * SP_LG - (AVATAR_SIZE + SP_MD if partner else 0), 120)
        if partner:
            title = truncate_text(partner.name, self.fonts.title, available)
            subtitle = f"{partner.status}  ·  {partner.ip}:{partner.tcp_port}"
            self.input.hint = f"Message {partner.name}"
            self.header_avatar.pack(side="left", before=self.header_info, padx=(0, SP_MD))
            self._draw_header_avatar(partner)
        else:
            title, subtitle = EMPTY_TITLE, EMPTY_PROMPT
            self.input.hint = EMPTY_INPUT
            self.header_avatar.pack_forget()
        self.partner.configure(text=title)
        self.partner_sub.configure(text=truncate_text(subtitle, self.fonts.sub, available))
        self.input.dimmed = self._empty_visible
        self._sync_empty_view()
        self.input.draw()

    def _draw_header_avatar(self, partner: object) -> None:
        canvas = self.header_avatar
        canvas.delete("all")
        draw_avatar(canvas, 0, 0, AVATAR_SIZE, partner.name, self.fonts.bold)
        corner = AVATAR_SIZE - DOT_SIZE
        color = STATUS_COLORS[partner.status]
        draw_presence_dot(canvas, corner, corner, DOT_SIZE, color, self.palette["bg"])

    def _sync_empty_view(self) -> None:
        if not self._empty_visible:
            self.empty_state.place_forget()
            self.empty_hint.pack_forget()
        elif self.view.has_notices_since(self._empty_since):
            self.empty_state.place_forget()
            self.empty_hint.pack(side="bottom", fill="x", before=self.view, pady=(0, SP_SM))
        else:
            self.empty_hint.pack_forget()
            self.empty_state.place(relx=0, rely=0, relwidth=1, relheight=1)
            self._draw_empty_art()

    def _draw_empty_art(self) -> None:
        canvas, palette, fonts = self.empty_art, self.palette, self.fonts
        prompt = truncate_text(EMPTY_PROMPT, fonts.btn, max(self.view.width - 2 * SP_XL, 200))
        width = max(fonts.empty_title.measure(EMPTY_TITLE), fonts.btn.measure(prompt)) + 2 * SP_XL
        centre = width // 2
        canvas.delete("all")
        canvas.configure(width=width, height=EMPTY_ART_H)
        draw_rounded(canvas, centre - 40, 8, centre + 24, 56, (16, 16, 16, 4), palette["selected"])
        draw_rounded(canvas, centre - 4, 40, centre + 40, 72, (12, 12, 4, 12), palette["chip"])
        for offset in (-24, -12, 0):
            draw_circle(canvas, centre + offset - 3, 29, 6, palette["accent"])
        canvas.create_text(centre, 100, text=EMPTY_TITLE, fill=palette["text"], font=fonts.empty_title)
        canvas.create_text(centre, 124, text=prompt, fill=palette["muted"], font=fonts.btn)

    def _draw_transfer_card(self) -> None:
        canvas, palette, fonts = self.transfer_card, self.palette, self.fonts
        width = max(canvas.winfo_width(), 240)
        percent = max(0, min(100, self.transfer_percent))
        canvas.delete("all")
        draw_rounded_border(canvas, 0, 0, width, CARD_H, CARD_RADIUS, palette["bg"], palette["line"])
        percent_label = f"{percent}%"
        room = width - 2 * SP_LG - fonts.btn_bold.measure(percent_label) - SP_SM
        label = truncate_text(self._transfer_label, fonts.btn_bold, max(room, 60))
        canvas.create_text(SP_LG, 19, text=label, anchor="w", fill=palette["text"], font=fonts.btn_bold)
        canvas.create_text(
            width - SP_LG, 19, text=percent_label, anchor="e", fill=palette["accent"], font=fonts.btn_bold,
        )
        track_left, track_right = SP_LG, width - SP_LG
        draw_rounded(canvas, track_left, 34, track_right, 38, 2, palette["line"])
        filled = int((track_right - track_left) * percent / 100)
        if filled > 0:
            draw_rounded(canvas, track_left, 34, track_left + max(filled, 4), 38, 2, palette["accent"])

    def _poll(self) -> None:
        self._drain()
        self._refresh()
        if self.stop.is_set():
            self.root.after(CLOSE_DELAY_MS, self._close)
        else:
            self.root.after(POLL_MS, self._poll)

    def _close(self) -> None:
        self.stop.set()
        self._drain()
        self.view.cancel_scroll()
        self.root.destroy()
