from __future__ import annotations

import queue
import re
import sys
import threading
import time
import tkinter as tk
from collections.abc import Callable, Iterator
from tkinter import filedialog, font as tkfont, ttk

from chat_state import ChatState
from peer_store import PeerStore

POLL_MS = 100
CLOSE_DELAY_MS = 400
SIDE_WIDTH = 250
ROW_HEIGHT = 56
AVATAR_SIZE = 40
WRAP_SHARE = 0.62
PEER_COLORS = ("#ef4444", "#f97316", "#d97706", "#16a34a", "#0d9488", "#2563eb", "#7c3aed", "#db2777")
STATUS_COLORS = {"online": "#22c55e", "away": "#f59e0b"}
LIGHT = {
    "bg": "#f3f5f9", "panel": "#ffffff", "text": "#1f2937", "muted": "#6b7280", "border": "#d9dee8",
    "mine": "#2563eb", "mine_text": "#ffffff", "mine_time": "#bfdbfe", "other": "#e8ecf3",
    "other_text": "#1f2937", "accent": "#2563eb", "row": "#e8f0fe", "error": "#b91c1c",
}
DARK = {
    "bg": "#0f172a", "panel": "#1e293b", "text": "#e5e7eb", "muted": "#94a3b8", "border": "#334155",
    "mine": "#2563eb", "mine_text": "#ffffff", "mine_time": "#bfdbfe", "other": "#334155",
    "other_text": "#f1f5f9", "accent": "#3b82f6", "row": "#1e3a5f", "error": "#fca5a5",
}
MESSAGE = re.compile(r"\[([^\]]+)\] (.*)", re.S)
TRANSFER_START = re.compile(r"(?:Sending|Receiving) (.+) \(.+\) \.\.\.")
TRANSFER_STEP = re.compile(r"(.+)  (\d+)%")


def classify(line: str, me: str) -> tuple[str, str, str]:
    if line[:3] in ("[*]", "[!]"):
        return ("error" if line[1] == "!" else "info"), "", line[4:]
    match = MESSAGE.fullmatch(line)
    if match:
        return ("mine" if match[1] == me else "other"), match[1], match[2]
    return "plain", "", line


class GuiTerminal:
    def __init__(self, name: str, store: PeerStore, state: ChatState, stop: threading.Event) -> None:
        self.name, self.store, self.state, self.stop = name, store, state, stop
        self._out: queue.Queue[str] = queue.Queue()
        self._in: queue.Queue[str] = queue.Queue()
        self._history: list[tuple[str, str, str, str]] = []
        self._peer_names: list[str] = []
        self._signature: tuple = ()
        self._bubbles: list[tk.Label] = []
        self._paints: list[tuple[tk.Misc, dict[str, str]]] = []
        self._status = "online"
        self._transferring = False
        self._dark = False
        self.palette = LIGHT
        if sys.platform == "win32":
            self._sharpen_text()
        self.root = tk.Tk()
        self.root.title(f"LAN Chat - {name}")
        self._make_fonts()
        self._build_side()
        self._build_chat()
        self._build_input()
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.bind_all("<MouseWheel>", self._wheel)
        self.root.bind_all("<Button-4>", self._wheel)
        self.root.bind_all("<Button-5>", self._wheel)
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

    @staticmethod
    def _sharpen_text() -> None:
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass

    def _make_fonts(self) -> None:
        family = "Segoe UI" if sys.platform == "win32" else tkfont.nametofont("TkDefaultFont")["family"]
        mono = "Consolas" if sys.platform == "win32" else tkfont.nametofont("TkFixedFont")["family"]
        self.body = tkfont.Font(family=family, size=11)
        self.small = tkfont.Font(family=family, size=8)
        self.bold = tkfont.Font(family=family, size=11, weight="bold")
        self.title = tkfont.Font(family=family, size=14, weight="bold")
        self.mono = tkfont.Font(family=mono, size=10)

    def _paint(self, widget: tk.Misc, **options: str) -> None:
        self._paints.append((widget, options))

    def _build_side(self) -> None:
        side = tk.Frame(self.root, width=SIDE_WIDTH)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        head = tk.Label(side, text="LAN Chat", font=self.title, anchor="w", padx=16, pady=14)
        head.pack(fill="x")
        self.peers = tk.Canvas(side, width=SIDE_WIDTH, highlightthickness=0, cursor="hand2")
        self.peers.pack(fill="both", expand=True)
        self.peers.bind("<Button-1>", lambda e: self._open_chat(int(self.peers.canvasy(e.y) // ROW_HEIGHT)))
        foot = tk.Frame(side)
        foot.pack(fill="x", padx=12, pady=10)
        self.status_button = self._flat_button(foot, "", self._change_status)
        self.status_button.pack(fill="x", pady=(0, 4))
        self.theme_button = self._flat_button(foot, "", self._toggle_theme)
        self.theme_button.pack(fill="x")
        for widget in (side, head, self.peers, foot):
            self._paint(widget, bg="panel")
        self._paint(head, fg="text")

    def _build_chat(self) -> None:
        self.main = main = tk.Frame(self.root)
        main.pack(side="left", fill="both", expand=True)
        header = tk.Frame(main, padx=18, pady=10)
        header.pack(fill="x")
        self.partner = tk.Label(header, text="No chat selected", font=self.title, anchor="w")
        self.partner.pack(fill="x")
        self.partner_sub = tk.Label(header, text="Pick someone on the left to start", font=self.small, anchor="w")
        self.partner_sub.pack(fill="x")
        body = tk.Frame(main)
        body.pack(fill="both", expand=True)
        self.log = tk.Text(body, state="disabled", wrap="word", width=64, height=22, relief="flat",
                           highlightthickness=0, padx=14, pady=8, cursor="arrow")
        scroll = ttk.Scrollbar(body, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        self.log.bind("<Configure>", self._rewrap)
        for tag, options in {
            "mine": {"justify": "right", "spacing1": 5, "rmargin": 6},
            "other": {"justify": "left", "spacing1": 5, "lmargin1": 6},
            "info": {"justify": "center", "spacing1": 6, "font": self.small},
            "error": {"justify": "center", "spacing1": 6, "font": self.small},
            "plain": {"font": self.mono},
        }.items():
            self.log.tag_configure(tag, **options)
        self._paint(main, bg="bg")
        self._paint(body, bg="bg")
        self._paint(header, bg="panel")
        self._paint(self.partner, bg="panel", fg="text")
        self._paint(self.partner_sub, bg="panel", fg="muted")
        self._paint(self.log, bg="bg", fg="text")

    def _build_input(self) -> None:
        self.transfer = tk.Frame(self.main, padx=16, pady=6)
        self.transfer_name = tk.Label(self.transfer, font=self.small, anchor="w")
        self.transfer_name.pack(fill="x")
        self.bar = ttk.Progressbar(self.transfer, maximum=100)
        self.bar.pack(fill="x")
        self.input_bar = bar = tk.Frame(self.main, padx=14, pady=12)
        bar.pack(side="bottom", fill="x")
        self.file_button = self._flat_button(bar, "+ File", self._send_file)
        self.file_button.pack(side="left", padx=(0, 8))
        self.entry = tk.Entry(bar, font=self.body, relief="flat", highlightthickness=1)
        self.entry.pack(side="left", fill="x", expand=True, ipady=7)
        self.entry.bind("<Return>", self._submit)
        self.send_button = tk.Button(bar, text="Send", font=self.bold, relief="flat", cursor="hand2",
                                     padx=18, pady=5, command=self._submit, fg="#ffffff")
        self.send_button.pack(side="left", padx=(8, 0))
        for widget in (self.transfer, self.transfer_name, bar):
            self._paint(widget, bg="panel")
        self._paint(self.transfer_name, fg="muted")
        self._paint(self.entry, bg="bg", fg="text", insertbackground="text", highlightbackground="border",
                    highlightcolor="accent")
        self._paint(self.send_button, bg="accent", activebackground="accent")

    def _flat_button(self, parent: tk.Misc, text: str, command: Callable[[], None]) -> tk.Button:
        button = tk.Button(parent, text=text, command=command, font=self.small, relief="flat", cursor="hand2",
                           padx=8, pady=4, bd=0)
        self._paint(button, bg="bg", fg="text", activebackground="border", activeforeground="text")
        return button

    def _apply_theme(self) -> None:
        self.palette = DARK if self._dark else LIGHT
        for widget, options in self._paints:
            widget.configure(**{opt: self.palette[key] for opt, key in options.items()})
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("Horizontal.TProgressbar", troughcolor=self.palette["border"],
                        background=self.palette["accent"], bordercolor=self.palette["panel"], thickness=6)
        self.root.configure(bg=self.palette["bg"])
        self.log.tag_configure("info", foreground=self.palette["muted"])
        self.log.tag_configure("error", foreground=self.palette["error"])
        self.log.tag_configure("plain", foreground=self.palette["text"])
        self.theme_button.configure(text="Light mode" if self._dark else "Dark mode")
        self.status_button.configure(text=f"Status: {self._status}")
        self._signature = ()
        self._refresh()
        self._render_all()

    def _toggle_theme(self) -> None:
        self._dark = not self._dark
        self._apply_theme()

    def _wheel(self, event: tk.Event) -> None:
        step = -1 if event.num == 4 or getattr(event, "delta", 0) > 0 else 1
        self.log.yview_scroll(step * 2, "units")

    def _rewrap(self, event: tk.Event) -> None:
        for label in self._bubbles:
            label.configure(wraplength=int(event.width * WRAP_SHARE))

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
        self.status_button.configure(text=f"Status: {self._status}")
        self._in.put(f"/status {self._status}")

    def _add(self, line: str) -> None:
        kind, who, text = classify(line, self.name)
        if kind == "info":
            step = TRANSFER_STEP.fullmatch(text)
            if step and self._transferring:
                self.bar["value"] = int(step[2])
                return
            start = TRANSFER_START.fullmatch(text)
            if start:
                self.transfer_name.configure(text=f"Transferring {start[1]}")
                self.bar["value"] = 0
                self.transfer.pack(side="bottom", fill="x", after=self.input_bar)
                self._transferring = True
            elif text.startswith(("Sent ", "Received ")):
                self._end_transfer()
        elif kind == "error":
            self._end_transfer()
        entry = (kind, who, text, time.strftime("%H:%M"))
        self._history.append(entry)
        self._render(*entry)

    def _end_transfer(self) -> None:
        self.transfer.pack_forget()
        self._transferring = False

    def _render_all(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self._bubbles.clear()
        for entry in self._history:
            self._render(*entry)

    def _render(self, kind: str, who: str, text: str, stamp: str) -> None:
        self.log.configure(state="normal")
        if kind in ("mine", "other"):
            self._bubble(kind, who, text, stamp)
        else:
            self.log.insert("end", text + "\n", kind)
        self.log.configure(state="disabled")
        self.log.see("end")

    def _bubble(self, kind: str, who: str, text: str, stamp: str) -> None:
        mine, p = kind == "mine", self.palette
        bg, fg = (p["mine"], p["mine_text"]) if mine else (p["other"], p["other_text"])
        box = tk.Frame(self.log, bg=bg, padx=12, pady=7)
        if not mine:
            tk.Label(box, text=who, bg=bg, fg=p["accent"], font=self.small).pack(anchor="w")
        message = tk.Label(box, text=text, bg=bg, fg=fg, font=self.body, justify="left",
                           wraplength=int(max(self.log.winfo_width(), 400) * WRAP_SHARE))
        message.pack(anchor="w")
        tk.Label(box, text=stamp, bg=bg, fg=p["mine_time"] if mine else p["muted"], font=self.small).pack(anchor="e")
        self._bubbles.append(message)
        start = self.log.index("end-1c")
        self.log.window_create(start, window=box)
        self.log.tag_add(kind, start)
        self.log.insert("end", "\n", kind)

    def _drain(self) -> None:
        while True:
            try:
                text = self._out.get_nowait()
            except queue.Empty:
                return
            for line in text.split("\n"):
                self._add(line)

    def _refresh(self) -> None:
        peers = self.store.get_peers()
        current = self.state.current
        rows = [(p.name, p.status, self.state.unread(p.peer_id), p.peer_id == current) for p in peers]
        partner = self.store.get(current) if current else None
        about = (partner.name, partner.status, partner.ip, partner.tcp_port) if partner else None
        signature = (tuple(rows), about, self._dark)
        if signature == self._signature:
            return
        self._signature = signature
        self._peer_names = [r[0] for r in rows]
        self._draw_peers(rows)
        if partner:
            self.partner.configure(text=partner.name)
            self.partner_sub.configure(text=f"{partner.status}  -  {partner.ip}:{partner.tcp_port}")
        else:
            self.partner.configure(text="No chat selected")
            self.partner_sub.configure(text="Pick someone on the left to start")

    def _draw_peers(self, rows: list[tuple[str, str, int, bool]]) -> None:
        p, canvas = self.palette, self.peers
        canvas.delete("all")
        for i, (name, status, unread, active) in enumerate(rows):
            top = i * ROW_HEIGHT
            canvas.create_rectangle(0, top, SIDE_WIDTH, top + ROW_HEIGHT, fill=p["row"] if active else p["panel"], width=0)
            x, y = 14, top + (ROW_HEIGHT - AVATAR_SIZE) // 2
            color = PEER_COLORS[sum(map(ord, name)) % len(PEER_COLORS)]
            canvas.create_oval(x, y, x + AVATAR_SIZE, y + AVATAR_SIZE, fill=color, width=0)
            canvas.create_text(x + AVATAR_SIZE // 2, y + AVATAR_SIZE // 2, text=name[0].upper(), fill="#ffffff", font=self.bold)
            canvas.create_oval(x + 28, y + 28, x + 40, y + 40, fill=STATUS_COLORS[status], outline=p["panel"], width=2)
            canvas.create_text(x + 56, top + 20, text=name, anchor="w", fill=p["text"], font=self.bold)
            canvas.create_text(x + 56, top + 38, text=status, anchor="w", fill=p["muted"], font=self.small)
            if unread:
                canvas.create_oval(SIDE_WIDTH - 40, top + 17, SIDE_WIDTH - 16, top + 39, fill="#ef4444", width=0)
                canvas.create_text(SIDE_WIDTH - 28, top + 28, text=str(unread), fill="#ffffff", font=self.small)
        canvas.configure(scrollregion=(0, 0, SIDE_WIDTH, max(len(rows) * ROW_HEIGHT, 1)))

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
        self.root.destroy()
