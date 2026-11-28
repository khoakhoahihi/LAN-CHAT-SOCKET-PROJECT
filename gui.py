from __future__ import annotations

import math
import os
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
from gui_info import INFO_WIDTH, InfoPanel, SharedFile
from gui_festive import GARLAND_HEIGHT, TILE, draw_garland, draw_holly, draw_tree, draw_wallpaper
from gui_shapes import draw_avatar, draw_circle, draw_presence_dot, draw_rounded, draw_rounded_border
from gui_style import (
    LIGHT, SP_LG, SP_MD, SP_SM, SP_XL, SP_XS, STATUS_COLORS, THEME_ORDER, THEMES,
    Fonts, classify, truncate_text,
)
from gui_widgets import (
    SCROLL_W, IconButton, PillButton, PillInput, ProfileCard, SlimScrollbar, Toast,
    paint_close, paint_file, paint_info, paint_moon, paint_plus, paint_send, paint_snowflake, paint_sun,
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
PROFILE_H = 52
THEME_BUTTON = 36
ACTION_SIZE = 34
CHIP_H = 32
CHIP_W = 124
EMPTY_BLOCK_H = 250
HOLLY_WIDTH = 46
HOLLY_HEIGHT = 30
INPUT_H = 40
ATTACH_SIZE = 36
CARD_H = 54
CARD_RADIUS = 12
BADGE_H = 20
EMPTY_ART_H = 150

EMPTY_TITLE = "No chat selected"
EMPTY_PROMPT = "Select someone on the left to start chatting"
EMPTY_INPUT = "Select a chat to message..."

THEME_ICONS = {"light": paint_moon, "dark": paint_snowflake, "christmas": paint_sun}

TRANSFER_START = re.compile(r"(?:Sending|Receiving) (.+) \((.+)\) \.\.\.")
SENT_DONE = re.compile(r"Sent (.+) \.\.\. 100%  Done\.")
RECEIVED_DONE = re.compile(r"Received (.+) -> (.+)")
NEW_MESSAGE = re.compile(r"New message from (.+) \((\d+) unread\)")
TRANSFER_STEP = re.compile(r"(.+)  (\d+)%")
TRANSFER_DONE_PREFIXES = ("Sent ", "Received ")


class PeerRow(NamedTuple):
    name: str
    status: str
    unread: int
    active: bool
    preview: tuple[str, str] | None


class GuiTerminal:
    def __init__(
        self, name: str, store: object, state: object, stop: threading.Event, downloads: str = "downloads",
    ) -> None:
        self.name = name
        self.downloads = downloads
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
        self._previews: dict[str, tuple[str, str]] = {}
        self._pending_files: dict[str, str] = {}
        self._outgoing_paths: dict[str, str] = {}
        self._info_open = False
        self._hover_index: int | None = None
        self._status = "online"
        self._theme = "light"
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
        self._build_info()
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.bind("<Configure>", self._on_root_resize)
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.root.bind_all(sequence, self._wheel)
        self.root.bind_all("<Escape>", self._on_escape)
        self.root.bind_all("<Control-k>", self._on_search_shortcut)
        self.root.bind_all("<Control-i>", self._on_info_shortcut)
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
        self._theme = name if name in THEMES else "light"
        self._apply_theme()

    def message_texts(self) -> list[str]:
        return self.view.message_texts()

    def sidebar_texts(self) -> list[str]:
        canvas = self.peers
        return [canvas.itemcget(item, "text") for item in canvas.find_all() if canvas.type(item) == "text"]

    def peer_statuses(self) -> dict[str, str]:
        return {row.name: row.status for row in self._rows}

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

        brand = self._paint(tk.Frame(self.side, padx=SP_LG), bg="panel")
        brand.pack(fill="x", pady=(SP_MD, 0))
        self.brand_title = self._paint(
            tk.Label(brand, text="Messages", font=self.fonts.title, anchor="w"), bg="panel", fg="text",
        )
        self.brand_title.pack(side="left")
        self.brand_holly = self._paint(
            tk.Canvas(brand, width=HOLLY_WIDTH, height=HOLLY_HEIGHT, highlightthickness=0, bd=0), bg="panel",
        )
        self.online_count = self._paint(
            tk.Label(brand, text="0 online", font=self.fonts.sub, anchor="e"), bg="panel", fg="muted",
        )
        self.online_count.pack(side="right")
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
        self.peers.bind("<Configure>", lambda _event: self._draw_peers())
        self.peers.bind("<Button-1>", self._on_peer_click)
        self.peers.bind("<Motion>", self._on_peer_motion)
        self.peers.bind("<Leave>", self._on_peer_leave)
        self._themed.append(self.peers_scroll)

        footer = self._paint(tk.Frame(self.side, padx=SP_SM, pady=SP_SM), bg="panel")
        footer.pack(fill="x")
        self.theme_button = IconButton(footer, THEME_BUTTON, paint_moon, self._toggle_theme, "panel", accent=False)
        self.theme_button.pack(side="right", padx=(SP_XS, 0))
        self.profile = ProfileCard(footer, self.name, PROFILE_H, self.fonts, self._change_status, "panel")
        self.profile.pack(side="left", fill="x", expand=True)
        self.status_button = self.profile
        self._themed.extend((self.profile, self.theme_button))
        self._hairline(self.root, vertical=True, side="left", fill="y")

    def _build_chat(self) -> None:
        self.main = self._paint(tk.Frame(self.root), bg="surface")
        self.main.pack(side="left", fill="both", expand=True)
        garland = tk.Canvas(self.main, height=GARLAND_HEIGHT, highlightthickness=0, bd=0)
        self.garland = self._paint(garland, bg="surface")
        self.garland.bind("<Configure>", lambda _event: self._draw_garland())
        self._build_header()
        self._hairline(self.main, fill="x")
        self._build_input()
        self.chat_body = self._paint(tk.Frame(self.main), bg="bg")
        self.chat_body.pack(side="top", fill="both", expand=True)
        self.view = ConversationView(self.chat_body, self.fonts, self._on_view_resize)
        self.view.pack(fill="both", expand=True)
        self._themed.append(self.view)
        self.toast = Toast(self.chat_body, self.fonts, self._open_chat_named)
        self._themed.append(self.toast)
        self.empty_hint = self._paint(
            tk.Label(self.chat_body, text=EMPTY_PROMPT, font=self.fonts.btn, anchor="center"), bg="bg", fg="subtle",
        )
        self.empty_state = self._paint(tk.Canvas(self.chat_body, highlightthickness=0, bd=0), bg="bg")
        self.empty_state.bind("<Configure>", lambda _event: self._draw_empty_art())
        self.empty_actions = self._paint(tk.Frame(self.empty_state), bg="bg")
        suggestions = (
            ("Find someone", self._focus_search),
            ("Change status", self._change_status),
            ("Switch theme", self._toggle_theme),
        )
        for label, command in suggestions:
            chip = PillButton(self.empty_actions, CHIP_H, self.fonts.btn, command, "bg")
            chip.configure(width=CHIP_W)
            chip.configure_content(label)
            chip.pack(side="left", padx=SP_XS)
            self._themed.append(chip)

    def _build_header(self) -> None:
        header = self._paint(tk.Frame(self.main, height=HEADER_H, padx=SP_LG), bg="surface")
        self.header = header
        header.pack(fill="x")
        header.pack_propagate(False)
        self.header_avatar = self._paint(
            tk.Canvas(header, width=AVATAR_SIZE + 2, height=AVATAR_SIZE + 2, highlightthickness=0, bd=0), bg="surface",
        )
        self.header_info = self._paint(tk.Frame(header), bg="surface")
        self.header_info.pack(side="left", fill="x", expand=True, pady=(0, 2))
        self.partner = self._paint(
            tk.Label(self.header_info, text=EMPTY_TITLE, font=self.fonts.title, anchor="w", pady=0, bd=0),
            bg="surface", fg="text",
        )
        self.partner.pack(fill="x")
        self.partner_sub = self._paint(
            tk.Label(self.header_info, text=EMPTY_PROMPT, font=self.fonts.sub, anchor="w", pady=0, bd=0),
            bg="surface", fg="muted",
        )
        self.partner_sub.pack(fill="x")
        self.header_actions = self._paint(tk.Frame(header), bg="surface")
        actions = self.header_actions
        self.close_button = IconButton(actions, ACTION_SIZE, paint_close, self._leave_chat, "surface", accent=False)
        self.file_action = IconButton(actions, ACTION_SIZE, paint_file, self._send_file, "surface", accent=False)
        self.info_button = IconButton(actions, ACTION_SIZE, paint_info, self._toggle_info, "surface", accent=False)
        self.close_button.pack(side="right")
        self.file_action.pack(side="right", padx=(0, SP_XS))
        self.info_button.pack(side="right", padx=(0, SP_XS))
        self._themed.extend((self.info_button, self.file_action, self.close_button))

    def _build_input(self) -> None:
        self.transfer = self._paint(tk.Frame(self.main, padx=SP_LG, pady=SP_SM), bg="surface")
        self.transfer_card = self._paint(
            tk.Canvas(self.transfer, height=CARD_H, highlightthickness=0, bd=0), bg="surface",
        )
        self.transfer_card.pack(fill="x")
        self.transfer_card.bind("<Configure>", lambda _event: self._draw_transfer_card())

        self.input_bar = self._paint(tk.Frame(self.main, padx=SP_LG, pady=SP_MD), bg="surface")
        self.input_bar.pack(side="bottom", fill="x")
        self._hairline(self.main, side="bottom", fill="x")
        self.file_button = IconButton(self.input_bar, ATTACH_SIZE, paint_plus, self._send_file, "surface", accent=False)
        self.file_button.pack(side="left", padx=(0, SP_SM))
        self.input = PillInput(
            self.input_bar, INPUT_H, EMPTY_INPUT, self.fonts.body, SP_LG, False, self._on_input_change, "surface",
        )
        self.input.pack(side="left", fill="x", expand=True)
        self.entry = self.input.entry
        self.entry.bind("<Return>", self._submit)
        self.entry.bind("<KeyPress>", self._block_typing)
        self.send_button = IconButton(self.input_bar, INPUT_H, paint_send, self._submit, "surface", accent=True)
        self.send_button.pack(side="left", padx=(SP_SM, 0))
        self.send_button.set_enabled(False)
        self._themed.extend((self.file_button, self.input, self.send_button))

    def _build_info(self) -> None:
        self.info_frame = self._paint(tk.Frame(self.root, width=INFO_WIDTH + 1), bg="panel")
        self.info_frame.pack_propagate(False)
        self._hairline(self.info_frame, vertical=True, side="left", fill="y")
        self.info = InfoPanel(self.info_frame, self.fonts, self.downloads)
        self.info.pack(side="left", fill="both", expand=True)
        self._themed.append(self.info)

    def _toggle_info(self) -> None:
        width, height = self.root.winfo_width(), self.root.winfo_height()
        extra = INFO_WIDTH + 1
        if self._info_open:
            self.info_frame.pack_forget()
            width -= extra
        else:
            self.info_frame.pack(side="right", fill="y", before=self.main)
            width += extra
        self._info_open = not self._info_open
        self.root.geometry(f"{max(width, WINDOW_MIN_WIDTH)}x{height}")

    def _on_escape(self, _event: object = None) -> None:
        if self._partner is not None:
            self._leave_chat()
        elif self.search_entry.get():
            self.search_entry.delete(0, "end")

    def _on_search_shortcut(self, _event: object = None) -> str:
        self._focus_search()
        return "break"

    def _on_info_shortcut(self, _event: object = None) -> str:
        self._toggle_info()
        return "break"

    def _apply_theme(self) -> None:
        self.palette = palette = THEMES[self._theme]
        for widget, mapping in self._paints:
            widget.configure(**{option: palette[key] for option, key in mapping.items()})
        self.root.configure(bg=palette["surface"])
        for themed in self._themed:
            themed.apply_palette(palette)
        self._update_footer_buttons()
        self._update_garland()
        if self._transferring:
            self._draw_transfer_card()
        self._signature = ()
        self._refresh()

    def _update_footer_buttons(self) -> None:
        self.profile.configure_content(self._status)
        self.theme_button.icon = THEME_ICONS[self._theme]
        self.theme_button.draw()

    def _update_garland(self) -> None:
        if self.palette["flake"]:
            self.garland.pack(side="top", fill="x", before=self.header)
            self.brand_holly.pack(side="left", padx=(SP_XS, 0), after=self.brand_title)
            self._draw_garland()
            draw_holly(self.brand_holly, 8, 16)
        else:
            self.garland.pack_forget()
            self.brand_holly.pack_forget()

    def _draw_garland(self) -> None:
        if self.palette["flake"]:
            draw_garland(self.garland, max(self.garland.winfo_width(), 400), self.palette["surface"])

    def _toggle_theme(self) -> None:
        following = (THEME_ORDER.index(self._theme) + 1) % len(THEME_ORDER)
        self.set_theme(THEME_ORDER[following])

    def _on_root_resize(self, event: tk.Event) -> None:
        if event.widget is not self.root or event.width < 100:
            return
        usable = event.width - (INFO_WIDTH + 1 if self._info_open else 0)
        if usable < NARROW_BELOW:
            side_width = SIDE_NARROW
        elif usable < MID_BELOW:
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
            target, scrollable = self.view.canvas, self.empty_state.winfo_manager() != "place"
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
            if text.startswith("/send "):
                self._remember_outgoing(text[6:].strip().strip("\"'"))

    def _send_file(self) -> None:
        path = filedialog.askopenfilename()
        if path:
            self._in.put(f"/send {path}")
            self._remember_outgoing(path)

    def _remember_outgoing(self, path: str) -> None:
        self._outgoing_paths[os.path.basename(path)] = path

    def _open_chat_named(self, name: str) -> None:
        self._in.put(f"/chat {name}")

    def _open_chat(self, index: int) -> None:
        if 0 <= index < len(self._peer_names):
            self._in.put(f"/chat {self._peer_names[index]}")

    def _change_status(self) -> None:
        self._status = "away" if self._status == "online" else "online"
        self._update_footer_buttons()
        self._in.put(f"/status {self._status}")

    def _leave_chat(self) -> None:
        self._in.put("/leave")

    def _focus_search(self) -> None:
        self.search_entry.focus_set()

    def _consume_transfer_notice(self, kind: str, text: str) -> bool:
        if kind == "error":
            self._end_transfer()
            return False
        if kind != "info":
            return False
        step = TRANSFER_STEP.fullmatch(text)
        if step and self._transferring:
            self.transfer_percent = int(step[2])
            self._draw_transfer_card()
            return True
        start = TRANSFER_START.fullmatch(text)
        if start:
            self._pending_files[start[1]] = start[2]
            self._begin_transfer(start[1])
            return False
        self._record_finished_file(text)
        if text.startswith(TRANSFER_DONE_PREFIXES):
            self._end_transfer()
        return False

    def _record_finished_file(self, text: str) -> None:
        stamp = time.strftime("%H:%M")
        sent, received = SENT_DONE.fullmatch(text), RECEIVED_DONE.fullmatch(text)
        if sent:
            name = sent[1]
            path = self._outgoing_paths.get(name, "")
            self.info.add_file(SharedFile(name, "sent", self._pending_files.pop(name, ""), stamp, path))
        elif received:
            name = received[1]
            self.info.add_file(SharedFile(name, "received", self._pending_files.pop(name, ""), stamp, received[2]))

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
                self._remember_preview(line)
                self._maybe_toast(line)
        self._sync_empty_view()

    def _remember_preview(self, line: LogLine) -> None:
        first_line = line.text.split("\n")[0]
        if line.kind == "other":
            self._previews[line.who] = (first_line, line.stamp)
        elif line.kind == "mine" and self._partner is not None:
            self._previews[self._partner.name] = ("You: " + first_line, line.stamp)

    def _maybe_toast(self, line: LogLine) -> None:
        match = NEW_MESSAGE.fullmatch(line.text) if line.kind == "info" else None
        if match is None:
            return
        peer = next((p for p in self.store.get_peers() if p.name == match[1]), None)
        latest = self.state.latest_unread(peer.peer_id) if peer else None
        self.toast.show(match[1], latest.text.split("\n")[0] if latest else "New message")

    def _refresh(self) -> None:
        query = self.search_entry.get().strip().lower()
        peers = [peer for peer in self.store.get_peers() if not query or query in peer.name.lower()]
        current = self.state.current
        rows = [self._peer_row(peer, current) for peer in peers]
        partner = self.store.get(current) if current else None
        about = (partner.name, partner.status, partner.ip, partner.tcp_port) if partner else None
        signature = (tuple(rows), about, self._theme, query)
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
        self.online_count.configure(text=f"{len(self.store.get_peers())} online")

    def _peer_row(self, peer: object, current: str | None) -> PeerRow:
        latest = self.state.latest_unread(peer.peer_id)
        if latest is not None:
            preview = (latest.text.split("\n")[0], "")
        else:
            preview = self._previews.get(peer.name)
        unread = self.state.unread(peer.peer_id)
        return PeerRow(peer.name, peer.status, unread, peer.peer_id == current, preview)

    def _draw_peers(self) -> None:
        canvas, palette = self.peers, self.palette
        canvas.delete("all")
        width = self._side_width - SCROLL_W
        flake = palette["flake"]
        if flake:
            height = max(canvas.winfo_height(), len(self._rows) * ROW_HEIGHT)
            draw_wallpaper(canvas, flake, 0, math.ceil(height / TILE), math.ceil(width / TILE))
        for index, row in enumerate(self._rows):
            top = index * ROW_HEIGHT
            if row.active:
                background = palette["selected"]
            elif index == self._hover_index:
                background = palette["hover"]
            else:
                background = palette["panel"]
            if background != palette["panel"] or flake:
                row_box = (ROW_INSET, top + 2, width - ROW_INSET, top + ROW_HEIGHT - 2)
                draw_rounded(canvas, *row_box, ROW_RADIUS, background)
            self._draw_peer_row(canvas, row, top, width, background)
        canvas.configure(scrollregion=(0, 0, width, max(len(self._rows) * ROW_HEIGHT, 1)))

    def _draw_peer_row(self, canvas: tk.Canvas, row: PeerRow, top: int, width: int, background: str) -> None:
        palette, fonts = self.palette, self.fonts
        x = ROW_INSET + SP_SM
        y = top + (ROW_HEIGHT - AVATAR_SIZE) // 2
        draw_avatar(canvas, x, y, AVATAR_SIZE, row.name, fonts.bold)
        dot_x, dot_y = x + AVATAR_SIZE - DOT_SIZE, y + AVATAR_SIZE - DOT_SIZE
        draw_presence_dot(canvas, dot_x, dot_y, DOT_SIZE, STATUS_COLORS[row.status], background)
        right = width - ROW_INSET - SP_SM
        text_x = x + AVATAR_SIZE + SP_MD
        preview = row.preview
        subtitle = preview[0] if preview else row.status
        name_right = right
        if preview and preview[1]:
            canvas.create_text(right, top + 22, text=preview[1], anchor="e", fill=palette["subtle"], font=fonts.small)
            name_right = right - fonts.small.measure(preview[1]) - SP_SM
        name = truncate_text(row.name, fonts.bold, name_right - text_x)
        canvas.create_text(text_x, top + 22, text=name, anchor="w", fill=palette["text"], font=fonts.bold)
        subtitle_right = right
        if row.unread:
            subtitle_right = self._draw_unread_badge(canvas, row.unread, right, top + 42) - SP_SM
        shown = truncate_text(subtitle, fonts.sub, subtitle_right - text_x)
        canvas.create_text(text_x, top + 42, text=shown, anchor="w", fill=palette["muted"], font=fonts.sub)

    def _draw_unread_badge(self, canvas: tk.Canvas, unread: int, right: int, centre_y: int) -> int:
        label = "99+" if unread > 99 else str(unread)
        left = right - max(BADGE_H, self.fonts.btn_bold.measure(label) + SP_MD)
        badge_top = centre_y - BADGE_H // 2
        draw_rounded(canvas, left, badge_top, right, badge_top + BADGE_H, BADGE_H // 2, self.palette["badge"])
        canvas.create_text(
            (left + right) // 2, centre_y, text=label, fill=self.palette["on_badge"], font=self.fonts.btn_bold,
        )
        return left

    def _update_header(self, partner: object | None) -> None:
        self._empty_visible = partner is None
        available = max(self.view.width - 2 * SP_LG - (AVATAR_SIZE + SP_MD if partner else 0), 120)
        if partner:
            title = truncate_text(partner.name, self.fonts.title, available)
            subtitle = f"{partner.status}  ·  {partner.ip}:{partner.tcp_port}"
            self.input.hint = f"Message {partner.name}"
            self.header_avatar.pack(side="left", before=self.header_info, padx=(0, SP_MD))
            self.header_actions.pack(side="right")
            self._draw_header_avatar(partner)
        else:
            title, subtitle = EMPTY_TITLE, EMPTY_PROMPT
            self.input.hint = EMPTY_INPUT
            self.header_avatar.pack_forget()
            self.header_actions.pack_forget()
        self.partner.configure(text=title)
        self.info.set_partner(partner)
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
        draw_presence_dot(canvas, corner, corner, DOT_SIZE, color, self.palette["surface"])

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
        canvas, palette, fonts = self.empty_state, self.palette, self.fonts
        width, height = max(canvas.winfo_width(), 1), max(canvas.winfo_height(), 1)
        canvas.delete("all")
        if palette["flake"]:
            draw_wallpaper(canvas, palette["flake"], 0, math.ceil(height / TILE), math.ceil(width / TILE))
        centre = width // 2
        top = max(height // 2 - EMPTY_BLOCK_H // 2, SP_LG)
        self._draw_illustration(canvas, centre, top)
        prompt = truncate_text(EMPTY_PROMPT, fonts.btn, max(width - 2 * SP_XL, 200))
        canvas.create_text(centre, top + 134, text=EMPTY_TITLE, fill=palette["text"], font=fonts.empty_title)
        canvas.create_text(centre, top + 158, text=prompt, fill=palette["muted"], font=fonts.btn)
        canvas.create_window(centre, top + 182, window=self.empty_actions, anchor="n")

    def _draw_illustration(self, canvas: tk.Canvas, centre: int, top: int) -> None:
        palette = self.palette
        if palette["flake"]:
            draw_tree(canvas, centre, top + 10)
            return
        draw_rounded(canvas, centre - 40, top + 8, centre + 24, top + 56, (16, 16, 16, 4), palette["selected"])
        draw_rounded(canvas, centre - 4, top + 40, centre + 40, top + 72, (12, 12, 4, 12), palette["chip"])
        for offset in (-24, -12, 0):
            draw_circle(canvas, centre + offset - 3, top + 26, 6, palette["accent"])

    def _draw_transfer_card(self) -> None:
        canvas, palette, fonts = self.transfer_card, self.palette, self.fonts
        width = max(canvas.winfo_width(), 240)
        percent = max(0, min(100, self.transfer_percent))
        canvas.delete("all")
        draw_rounded_border(canvas, 0, 0, width, CARD_H, CARD_RADIUS, palette["surface"], palette["line"])
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
