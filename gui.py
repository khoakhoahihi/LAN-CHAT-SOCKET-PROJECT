from __future__ import annotations

import math, queue, re, sys, threading, time, tkinter as tk
from collections.abc import Callable, Iterator
from tkinter import filedialog, font as tkfont

POLL_MS, CLOSE_DELAY_MS, GROUP_WINDOW_SEC, WIN_W, WIN_H, WIN_MIN_W, WIN_MIN_H = 100, 400, 300, 960, 640, 640, 440
SIDE_WIDE, SIDE_MID, SIDE_NARROW, HEADER_H, ROW_HEIGHT, AVATAR_SIZE, HEADER_AVATAR, DOT_SIZE = 300, 250, 220, 64, 60, 44, 36, 12
INPUT_H, SEARCH_H, BTN_SIZE, SCROLL_W, MIN_THUMB, WRAP_SHARE = 38, 32, 36, 8, 24, 0.65
SP_XS, SP_SM, SP_MD, SP_LG, SP_XL = 4, 8, 12, 16, 24
R_TAIL, R_TOUCH, R_BTN, R_CARD, R_BUBBLE, R_PILL = 4, 6, 8, 12, 14, 19
F_TIME, F_SUB, F_BTN, F_BODY, F_TITLE, F_EMPTY = 7, 8, 9, 10, 12, 13
EMPTY_TITLE, EMPTY_PROMPT, EMPTY_INPUT = "No chat selected", "Select someone on the left to start chatting", "Select a chat to message..."
PEER_COLORS = ("#E05A47", "#E8833A", "#C98A1C", "#2F9E44", "#0CA678", "#4263EB", "#7048E8", "#D6336C")
STATUS_COLORS = {"online": "#22C55E", "away": "#F59E0B"}
LIGHT = {
    "bg": "#E7ECF1", "panel": "#FFFFFF", "text": "#17212B", "muted": "#707F8C", "border": "#E6EAEF", "mine": "#E1F6CC", "mine_text": "#17212B", "mine_time": "#5FA146",
    "other": "#FFFFFF", "other_text": "#17212B", "other_time": "#8A9BA8", "accent": "#3390EC", "row": "#3390EC", "row_text": "#FFFFFF", "row_sub": "#DCEEFF",
    "hover": "#F1F4F7", "error": "#D14B4B", "input_bg": "#F4F6F8", "input_focus": "#FFFFFF", "dim": "#C5CFD8", "pill_bg": "#D8E0E8", "scroll": "#BCC7D4", "scroll_hover": "#92A2B3",
}
DARK = {
    "bg": "#0E1621", "panel": "#17212B", "text": "#E4ECF2", "muted": "#7D8E9E", "border": "#22303F", "mine": "#2B5278", "mine_text": "#E4ECF2", "mine_time": "#8BB0D3",
    "other": "#182533", "other_text": "#E4ECF2", "other_time": "#6C7F91", "accent": "#5288C1", "row": "#2B5278", "row_text": "#E4ECF2", "row_sub": "#A8C2DC",
    "hover": "#202B36", "error": "#E06C75", "input_bg": "#242F3D", "input_focus": "#242F3D", "dim": "#384656", "pill_bg": "#182533", "scroll": "#394B5E", "scroll_hover": "#546D87",
}
MESSAGE, TRANSFER_START, TRANSFER_STEP = re.compile(r"\[([^\]]+)\] (.*)", re.S), re.compile(r"(?:Sending|Receiving) (.+) \(.+\) \.\.\."), re.compile(r"(.+)  (\d+)%")


def classify(line: str, me: str) -> tuple[str, str, str]:
    if line[:3] in ("[*]", "[!]"):
        return ("error" if line[1] == "!" else "info"), "", line[4:]
    m = MESSAGE.fullmatch(line)
    return (("mine" if m[1] == me else "other"), m[1], m[2]) if m else ("plain", "", line)


def rounded_rect(canvas: tk.Canvas, x1: float, y1: float, x2: float, y2: float, radii: int | tuple[int, int, int, int], **kw: object) -> int:
    tl, tr, br, bl = (radii,) * 4 if isinstance(radii, int) else radii
    cap = max(0.0, min((x2 - x1) / 2, (y2 - y1) / 2))
    kw.setdefault("outline", "")
    if not kw["outline"]:
        kw["width"] = 0
    pts: list[float] = []
    for cx, cy, r, a0 in ((x2 - min(tr, cap), y1 + min(tr, cap), min(tr, cap), -math.pi / 2), (x2 - min(br, cap), y2 - min(br, cap), min(br, cap), 0.0),
                          (x1 + min(bl, cap), y2 - min(bl, cap), min(bl, cap), math.pi / 2), (x1 + min(tl, cap), y1 + min(tl, cap), min(tl, cap), math.pi)):
        for s in range((6 if r > 0 else 0) + 1):
            a = a0 + (math.pi / 2) * (s / 6 if r > 0 else 0)
            pts.extend((cx + r * math.cos(a), cy + r * math.sin(a)))
    return canvas.create_polygon(pts, **kw)


def truncate_text(text: str, font: tkfont.Font, max_w: int) -> str:
    if max_w <= 0 or font.measure(text) <= max_w:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        lo, hi = (mid, hi) if font.measure(text[:mid] + "...") <= max_w else (lo, mid - 1)
    return text[:lo] + "..." if lo > 0 else "..."


class GuiTerminal:
    def __init__(self, name: str, store: object, state: object, stop: threading.Event) -> None:
        self.name, self.store, self.state, self.stop = name, store, state, stop
        self._out, self._in, self._history, self._bubble_widgets = queue.Queue(), queue.Queue(), [], {}
        self._peer_names, self._rows, self._paints, self._pills, self._scrolls, self._signature = [], [], [], [], [], ()
        self._hover_idx, self._status, self._transferring, self._empty_visible, self._send_active, self._dark = None, "online", False, True, False, False
        self._transfer_label, self._log_width, self._side_width, self._empty_since, self._last_msg_date, self._partner, self._scroll_job, self.palette = "", 620, SIDE_WIDE, 0, None, None, None, LIGHT
        if sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except (AttributeError, OSError):
                pass
        self.root = tk.Tk()
        self.root.title(f"LAN Chat - {name}")
        self.root.geometry(f"{WIN_W}x{WIN_H}"), self.root.minsize(WIN_MIN_W, WIN_MIN_H)
        fam = "Segoe UI" if sys.platform == "win32" else tkfont.nametofont("TkDefaultFont")["family"]
        mono = "Consolas" if sys.platform == "win32" else tkfont.nametofont("TkFixedFont")["family"]
        for attr, f, sz, wt in (("small", fam, F_TIME, "normal"), ("sub", fam, F_SUB, "normal"), ("btn", fam, F_BTN, "normal"), ("btn_bold", fam, F_BTN, "bold"),
                                ("mono", mono, F_BTN, "normal"), ("body", fam, F_BODY, "normal"), ("bold", fam, F_BODY, "bold"), ("title", fam, F_TITLE, "bold"), ("empty_title", fam, F_EMPTY, "bold")):
            setattr(self, attr, tkfont.Font(family=f, size=sz, weight=wt))
        self._build_side(), self._build_chat()
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.bind("<Configure>", self._on_root_resize)
        [self.root.bind_all(ev, self._wheel) for ev in ("<MouseWheel>", "<Button-4>", "<Button-5>")]
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
        self.entry.focus_set(), self.root.mainloop(), self.stop.set(), worker.join()

    def _mk(self, cls: type, parent: tk.Misc, pack: dict | None = None, paint: dict | None = None, **kw: object) -> object:
        w = cls(parent, **kw)
        if pack is not None:
            w.pack(**pack)
        if paint:
            self._paints.append((w, paint))
        return w

    def _make_scroll(self, parent: tk.Misc, target: tk.Widget, bg_key: str) -> tk.Canvas:
        sc = self._mk(tk.Canvas, parent, {"side": "right", "fill": "y"}, {"bg": bg_key}, width=SCROLL_W, highlightthickness=0, bd=0)
        st = {"r": (0.0, 1.0), "h": False, "p": False}
        def draw() -> None:
            sc.delete("thumb")
            lo, hi = st["r"]
            if st["p"] and (lo > 0.0 or hi < 1.0):
                ht = max(sc.winfo_height(), 1)
                y1, y2 = max(0.0, min(lo * ht, ht - MIN_THUMB)), min(ht, max(hi * ht, lo * ht + MIN_THUMB))
                rounded_rect(sc, 2, y1 + 2, SCROLL_W - 1, y2 - 2, 2, fill=self.palette["scroll_hover" if st["h"] else "scroll"])
        st["draw"] = draw
        target.configure(yscrollcommand=lambda lo, hi: (st.__setitem__("r", (float(lo), float(hi))), draw()))
        mv = lambda e: target.yview_moveto(max(0.0, min(1.0 - (st["r"][1] - st["r"][0]), e.y / max(sc.winfo_height(), 1) - (st["r"][1] - st["r"][0]) / 2)))
        def on_leave(e: tk.Event) -> None:
            w = parent.winfo_containing(e.x_root, e.y_root)
            st["p"] = w is not None and (w is parent or str(w).startswith(f"{parent}."))
            draw()
        [sc.bind(ev, fn) for ev, fn in (("<Configure>", lambda _e: draw()), ("<Enter>", lambda _e: (st.update(p=True, h=True), draw())), ("<Leave>", lambda _e: (st.__setitem__("h", False), draw())), ("<Button-1>", mv), ("<B1-Motion>", mv))]
        parent.bind("<Enter>", lambda _e: (st.__setitem__("p", True), draw()), add="+"), parent.bind("<Leave>", on_leave, add="+")
        self._scrolls.append(st), target.pack(side="left", fill="both", expand=True)
        return sc

    def _build_side(self) -> None:
        self.side = side = self._mk(tk.Frame, self.root, {"side": "left", "fill": "y"}, {"bg": "panel"}, width=SIDE_WIDE)
        side.pack_propagate(False)
        head = self._mk(tk.Frame, side, {"fill": "x"}, {"bg": "panel"}, height=HEADER_H, padx=SP_MD, pady=SP_LG)
        head.pack_propagate(False)
        self.search_entry, self.search_placeholder = self._make_pill(head, SEARCH_H, "Search", self.btn, 32, True, self._refresh, {"fill": "both", "expand": True})
        self._mk(tk.Frame, side, {"fill": "x"}, {"bg": "border"}, height=1)
        body = self._mk(tk.Frame, side, {"fill": "both", "expand": True}, {"bg": "panel"})
        self.peers = self._mk(tk.Canvas, body, None, {"bg": "panel"}, width=SIDE_WIDE - SCROLL_W, highlightthickness=0, bd=0)
        self.peers_scroll = self._make_scroll(body, self.peers, "panel")
        [self.peers.bind(ev, fn) for ev, fn in (("<Button-1>", lambda e: self._open_chat(int(self.peers.canvasy(e.y) // ROW_HEIGHT))), ("<Motion>", lambda e: self._hover_peer(int(self.peers.canvasy(e.y) // ROW_HEIGHT))), ("<Leave>", lambda _e: self._hover_peer(None)))]
        self._mk(tk.Frame, side, {"fill": "x"}, {"bg": "border"}, height=1)
        foot = self._mk(tk.Frame, side, {"fill": "x"}, {"bg": "panel"}, padx=SP_XS, pady=SP_SM)
        bp = {"bg": "input_bg", "fg": "text", "activebackground": "hover", "activeforeground": "text"}
        self.status_button, self.theme_button = [self._mk(tk.Button, foot, {"side": "left", "fill": "x", "expand": True, "padx": px}, bp, text="", command=cmd, font=self.btn, relief="flat", cursor="hand2", padx=SP_XS, pady=5, bd=0) for cmd, px in ((self._change_status, (0, SP_XS)), (self._toggle_theme, (SP_XS, 0)))]
        self._mk(tk.Frame, self.root, {"side": "left", "fill": "y"}, {"bg": "border"}, width=1)

    def _build_chat(self) -> None:
        self.main = self._mk(tk.Frame, self.root, {"side": "left", "fill": "both", "expand": True}, {"bg": "bg"})
        header = self._mk(tk.Frame, self.main, {"fill": "x"}, {"bg": "panel"}, height=HEADER_H, padx=SP_LG)
        header.pack_propagate(False)
        self.header_avatar = self._mk(tk.Canvas, header, None, {"bg": "panel"}, width=HEADER_AVATAR, height=HEADER_AVATAR, highlightthickness=0, bd=0)
        self.header_info = self._mk(tk.Frame, header, {"side": "left", "fill": "x", "expand": True, "pady": (0, 2)}, {"bg": "panel"})
        self.partner = self._mk(tk.Label, self.header_info, {"fill": "x"}, {"bg": "panel", "fg": "text"}, text=EMPTY_TITLE, font=self.title, anchor="w", pady=0, bd=0)
        self.partner_sub = self._mk(tk.Label, self.header_info, {"fill": "x"}, {"bg": "panel", "fg": "muted"}, text=EMPTY_PROMPT, font=self.sub, anchor="w", pady=0, bd=0)
        self.header_badge = self._mk(tk.Canvas, header, None, {"bg": "panel"}, width=72, height=22, highlightthickness=0, bd=0)
        self._mk(tk.Frame, self.main, {"fill": "x"}, {"bg": "border"}, height=1)
        self._build_input()
        self.chat_body = self._mk(tk.Frame, self.main, {"side": "top", "fill": "both", "expand": True}, {"bg": "bg"})
        self.empty_hint = self._mk(tk.Label, self.chat_body, None, {"bg": "bg", "fg": "muted"}, text=EMPTY_PROMPT, font=self.btn, anchor="center")
        self.log = self._mk(tk.Text, self.chat_body, None, {"bg": "bg", "fg": "text"}, state="disabled", wrap="word", width=64, height=22, relief="flat", highlightthickness=0, padx=SP_LG, pady=SP_MD, cursor="arrow")
        self.log_scroll = self._make_scroll(self.chat_body, self.log, "bg")
        self.log.bind("<Configure>", self._on_log_resize), self.log.bind("<Destroy>", lambda _e: self._scroll_end(True))
        [self.log.tag_configure(tag, **opts) for tag, opts in (("mine", {"justify": "right", "rmargin": SP_XS}), ("other", {"justify": "left", "lmargin1": SP_XS}), ("grp_first", {"spacing1": SP_MD}), ("grp_next", {"spacing1": SP_XS}),
         ("plain", {"font": self.mono, "spacing1": 2}), ("date", {"justify": "center", "spacing1": SP_MD, "spacing3": SP_SM}), ("info", {"justify": "center", "spacing1": SP_SM, "spacing3": SP_XS}), ("error", {"justify": "center", "spacing1": SP_SM, "spacing3": SP_XS}))]
        self.empty_state = self._mk(tk.Frame, self.chat_body, None, {"bg": "bg"})
        self.empty_pill = self._mk(tk.Canvas, self.empty_state, None, {"bg": "bg"}, width=320, height=64, highlightthickness=0, bd=0)
        self.empty_pill.place(relx=0.5, rely=0.5, anchor="center")

    def _build_input(self) -> None:
        self.transfer = self._mk(tk.Frame, self.main, None, {"bg": "bg"}, padx=SP_LG, pady=SP_SM)
        self.transfer_card = self._mk(tk.Canvas, self.transfer, {"fill": "x"}, {"bg": "bg"}, height=50, highlightthickness=0, bd=0)
        self.transfer_card.bind("<Configure>", lambda _e: self._draw_transfer_card())
        self.bar, self.input_bar = {"value": 0}, self._mk(tk.Frame, self.main, {"side": "bottom", "fill": "x"}, {"bg": "panel"}, padx=SP_LG, pady=SP_SM)
        self._input_div = self._mk(tk.Frame, self.main, {"side": "bottom", "fill": "x"}, {"bg": "border"}, height=1)
        self.file_button = self._mk(tk.Canvas, self.input_bar, {"side": "left", "padx": (0, SP_SM)}, {"bg": "panel"}, width=BTN_SIZE, height=BTN_SIZE, highlightthickness=0, bd=0, cursor="hand2")
        [self.file_button.bind(ev, fn) for ev, fn in (("<Button-1>", lambda _e: self._send_file()), ("<Enter>", lambda _e: self._draw_attach_btn(True)), ("<Leave>", lambda _e: self._draw_attach_btn(False)))]
        self.entry, self.placeholder = self._make_pill(self.input_bar, INPUT_H, EMPTY_INPUT, self.body, SP_LG, False, lambda: (setattr(self, "_send_active", bool(self.entry.get().strip())), self._draw_send_btn()), {"side": "left", "fill": "x", "expand": True})
        self.entry.bind("<Return>", self._submit), self.entry.bind("<KeyPress>", lambda _e: "break" if self._empty_visible else None)
        self.send_button = self._mk(tk.Canvas, self.input_bar, {"side": "left", "padx": (SP_SM, 0)}, {"bg": "panel"}, width=INPUT_H, height=INPUT_H, highlightthickness=0, bd=0)
        self.send_button.bind("<Button-1>", lambda _e: self._submit())

    def _make_pill(self, parent: tk.Misc, h: int, hint: str, font: tkfont.Font, x_pad: int, icon: bool, on_change: Callable[[], None], pack: dict) -> tuple[tk.Entry, tk.Label]:
        c, var = self._mk(tk.Canvas, parent, pack, {"bg": "panel"}, height=h, highlightthickness=0, bd=0), tk.StringVar()
        entry = self._mk(tk.Entry, c, None, {"bg": "input_bg", "fg": "text", "insertbackground": "text"}, textvariable=var, font=font, relief="flat", bd=0, highlightthickness=0)
        ph = self._mk(tk.Label, c, None, {"bg": "input_bg", "fg": "muted"}, text=hint, font=font, anchor="w", cursor="xterm")
        pill = {"canvas": c, "entry": entry, "var": var, "ph": ph, "h": h, "x": x_pad, "icon": icon, "focused": False, "font": font, "hint": hint}
        pill["window"] = c.create_window(x_pad, h // 2, window=entry, anchor="w", width=40)
        self._pills.append(pill)
        var.trace_add("write", lambda *_a: (on_change(), self._draw_pill(pill))), ph.bind("<Button-1>", lambda _e: entry.focus_set()), c.bind("<Configure>", lambda _e: self._draw_pill(pill))
        [entry.bind(ev, lambda _e, v=val: (pill.__setitem__("focused", v), self._draw_pill(pill))) for ev, val in (("<FocusIn>", True), ("<FocusOut>", False))]
        return entry, ph

    def _draw_pill(self, pill: dict[str, object]) -> None:
        c, p, h, x = pill["canvas"], self.palette, pill["h"], pill["x"]
        w, dimmed = max(c.winfo_width(), 120), (not pill["icon"]) and self._empty_visible
        for item in c.find_all():
            if item != pill["window"]: c.delete(item)
        fill = p["bg"] if dimmed else p["input_focus" if pill["focused"] else "input_bg"]
        rounded_rect(c, 1, 1, w - 2, h - 2, (h - 2) // 2, fill=fill, outline=p["accent" if pill["focused"] and not dimmed else "border"], width=1)
        if pill["icon"]:
            c.create_oval(12, h // 2 - 5, 20, h // 2 + 3, outline=p["muted"], width=1.6), c.create_line(19, h // 2 + 2, 23, h // 2 + 6, fill=p["muted"], width=1.8, capstyle="round")
        inner_w = max(w - x - SP_LG, 40)
        pill["entry"].configure(bg=fill, cursor="arrow" if dimmed else "xterm", takefocus=0 if dimmed else 1)
        pill["ph"].configure(bg=fill, text=truncate_text(str(pill["hint"]), pill["font"], inner_w), cursor="arrow" if dimmed else "xterm")
        c.coords(pill["window"], x, h // 2), c.itemconfigure(pill["window"], width=inner_w), c.tag_raise(pill["window"])
        pill["ph"].place(x=x, y=h // 2, anchor="w") if not pill["var"].get() else pill["ph"].place_forget()

    def _draw_send_btn(self) -> None:
        c, p, m = self.send_button, self.palette, INPUT_H // 2
        c.delete("all"), rounded_rect(c, 1, 1, INPUT_H - 2, INPUT_H - 2, R_PILL - 1, fill=p["accent" if self._send_active else "dim"])
        c.create_polygon((m - 5, m - 5, m + 6, m, m - 5, m + 5, m - 3, m), fill="#FFFFFF", outline=""), c.configure(cursor="hand2" if self._send_active else "")

    def _draw_attach_btn(self, hovered: bool = False) -> None:
        c, p, m = self.file_button, self.palette, BTN_SIZE // 2
        c.delete("all"), rounded_rect(c, 1, 1, BTN_SIZE - 2, BTN_SIZE - 2, BTN_SIZE // 2, fill=p["hover" if hovered else "input_bg"])
        c.create_line(m - 5, m, m + 5, m, fill=p["muted"], width=2, capstyle="round"), c.create_line(m, m - 5, m, m + 5, fill=p["muted"], width=2, capstyle="round")

    def _draw_transfer_card(self) -> None:
        c, p, pct, w = self.transfer_card, self.palette, self.bar["value"], max(self.transfer_card.winfo_width(), 240)
        c.delete("all"), rounded_rect(c, 1, 1, w - 2, 48, R_CARD, fill=p["panel"], outline=p["border"], width=1)
        lbl = truncate_text(self._transfer_label, self.btn_bold, max(w - 2 * SP_LG - self.btn_bold.measure(f"{pct}%") - SP_SM, 60))
        c.create_text(SP_LG, 17, text=lbl, anchor="w", fill=p["text"], font=self.btn_bold), c.create_text(w - SP_LG, 17, text=f"{pct}%", anchor="e", fill=p["accent"], font=self.btn_bold)
        rounded_rect(c, SP_LG, 33, w - SP_LG, 37, 2, fill=p["border"])
        if (fw := int((w - 2 * SP_LG) * max(0, min(100, pct)) / 100)) > 0: rounded_rect(c, SP_LG, 33, SP_LG + max(fw, 4), 37, 2, fill=p["accent"])

    def _apply_theme(self) -> None:
        self.palette = DARK if self._dark else LIGHT
        [w.configure(**{k: self.palette[v] for k, v in opts.items()}) for w, opts in self._paints]
        self.root.configure(bg=self.palette["bg"])
        [self.log.tag_configure(tag, foreground=self.palette[fk], font=fn) for tag, fk, fn in (("date", "muted", self.btn_bold), ("info", "muted", self.sub), ("error", "error", self.sub), ("plain", "text", self.mono))]
        self.theme_button.configure(text="Light mode" if self._dark else "Dark mode"), self.status_button.configure(text=f"Status: {self._status}")
        [sc["draw"]() for sc in self._scrolls], self._draw_send_btn(), self._draw_attach_btn(), [self._draw_pill(pill) for pill in self._pills]
        if self._transferring: self._draw_transfer_card()
        self._signature = ()
        self._refresh()
        self.log.configure(state="normal"), self.log.delete("1.0", "end"), self.log.configure(state="disabled"), self._bubble_widgets.clear()
        self._last_msg_date = None
        [self._render(idx) for idx in range(len(self._history))]

    def set_theme(self, name: str) -> None:
        self._dark = name == "dark"
        self._apply_theme()

    def _toggle_theme(self) -> None:
        self.set_theme("light" if self._dark else "dark")

    def message_texts(self) -> list[str]:
        return [e[2] for e in self._history if e[0] in ("mine", "other")]

    def sidebar_texts(self) -> list[str]:
        return [self.peers.itemcget(i, "text") for i in self.peers.find_all() if self.peers.type(i) == "text"]

    def partner_title(self) -> str:
        return self.partner.cget("text")

    def transfer_active(self) -> bool:
        return self._transferring

    def log_text(self) -> str:
        parts = [self.log.get("1.0", "end").strip(), *self.message_texts(), EMPTY_PROMPT if self._empty_visible else ""]
        return "\n".join(p for p in parts if p)

    def _on_root_resize(self, event: tk.Event) -> None:
        if event.widget is self.root and event.width >= 100:
            sw = SIDE_NARROW if event.width < 800 else (SIDE_MID if event.width < 900 else SIDE_WIDE)
            if sw != self._side_width:
                self._side_width = sw
                self.side.configure(width=sw), self.peers.configure(width=sw - SCROLL_W), self._draw_peers(self._rows)

    def _scroll_end(self, cancel_only: bool = False) -> None:
        if self._scroll_job:
            try:
                self.root.after_cancel(self._scroll_job)
            except tk.TclError:
                pass
            self._scroll_job = None
        if not cancel_only:
            self.log.update_idletasks(), self.log.yview_moveto(1.0)
            self._scroll_job = self.root.after_idle(lambda: (setattr(self, "_scroll_job", None), self.log.yview_moveto(1.0) if self.log.winfo_exists() else None))

    def _on_log_resize(self, event: tk.Event) -> None:
        if event.width > 40 and abs(event.width - self._log_width) > 2:
            self._log_width = event.width
            [self._layout_bubble(c, i) for i, c in self._bubble_widgets.items()]
            self._update_header(self._partner), self._scroll_end()

    def _wheel(self, event: tk.Event) -> None:
        step = -1 if event.num == 4 or getattr(event, "delta", 0) > 0 else 1
        target = self.peers if event.widget in (self.peers, self.peers_scroll) else self.log
        if not ((target is self.peers and self.peers.yview() == (0.0, 1.0)) or (target is self.log and self.empty_state.winfo_manager() == "place")):
            target.yview_scroll(step * 2, "units")

    def _hover_peer(self, idx: int | None) -> None:
        hovered = idx if idx is not None and 0 <= idx < len(self._rows) else None
        if hovered != self._hover_idx:
            self._hover_idx = hovered
            self.peers.configure(cursor="hand2" if hovered is not None else ""), self._draw_peers(self._rows)

    def _submit(self, _event: object = None) -> None:
        if text := self.entry.get().strip():
            self._in.put(text), self.entry.delete(0, "end")

    def _send_file(self) -> None:
        if path := filedialog.askopenfilename():
            self._in.put(f"/send {path}")

    def _open_chat(self, index: int) -> None:
        if 0 <= index < len(self._peer_names):
            self._in.put(f"/chat {self._peer_names[index]}")

    def _change_status(self) -> None:
        self._status = "away" if self._status == "online" else "online"
        self.status_button.configure(text=f"Status: {self._status}"), self._in.put(f"/status {self._status}")

    def _handle_transfer(self, kind: str, text: str) -> bool:
        if kind == "info" and (step := TRANSFER_STEP.fullmatch(text)) and self._transferring:
            self.bar["value"] = int(step[2])
            self._draw_transfer_card()
            return True
        if kind == "info" and (start := TRANSFER_START.fullmatch(text)):
            self._transfer_label, self.bar["value"], self._transferring = f"Transferring {start[1]}", 0, True
            self.transfer.pack(side="bottom", fill="x", before=self.chat_body), self._draw_transfer_card()
        elif (kind == "info" and text.startswith(("Sent ", "Received "))) or kind == "error":
            self.transfer.pack_forget(), setattr(self, "_transferring", False)
        return False

    def _same_group(self, i: int, j: int) -> bool:
        if i < 0 or j >= len(self._history):
            return False
        a, b = self._history[i], self._history[j]
        return a[0] in ("mine", "other") and a[0] == b[0] and a[1] == b[1] and a[5] == b[5] and b[4] - a[4] < GROUP_WINDOW_SEC

    def _render(self, idx: int) -> None:
        kind, _who, text, _stamp, _ts, date_str = self._history[idx]
        self.log.configure(state="normal")
        if kind in ("mine", "other"):
            if date_str != self._last_msg_date:
                self._last_msg_date = date_str
                self.log.insert("end", "Today\n", "date")
            grp = "grp_next" if self._same_group(idx - 1, idx) else "grp_first"
            canvas = self._bubble_widgets[idx] = tk.Canvas(self.log, highlightthickness=0, bd=0)
            self._layout_bubble(canvas, idx)
            start = self.log.index("end-1c")
            self.log.window_create(start, window=canvas), self.log.tag_add(kind, start), self.log.tag_add(grp, start), self.log.insert("end", "\n", (kind, grp))
        else:
            self.log.insert("end", text + "\n", kind)
        self.log.configure(state="disabled"), self._scroll_end()

    def _layout_bubble(self, canvas: tk.Canvas, idx: int) -> None:
        kind, who, text, stamp, _ts, _date = self._history[idx]
        mine, p, is_first, is_last = kind == "mine", self.palette, not self._same_group(idx - 1, idx), not self._same_group(idx, idx + 1)
        bg, fg, t_fg = p["mine" if mine else "other"], p["mine_text" if mine else "other_text"], p["mine_time" if mine else "other_time"]
        max_w = max(int(max(self._log_width - 2 * SP_LG, 300) * WRAP_SHARE) - 2 * SP_MD, 120)
        canvas.delete("all")
        tid = canvas.create_text(0, 0, text=text, font=self.body, width=max_w, anchor="nw", fill=fg, justify="left")
        box = canvas.bbox(tid)
        tw, th = (box[2] - box[0], box[3] - box[1]) if box else (0, self.body.metrics("linespace"))
        sw, sh = (self.btn_bold.measure(who), self.btn_bold.metrics("linespace") + 2) if (not mine) and is_first else (0, 0)
        time_w, time_h = (self.small.measure(stamp), self.small.metrics("linespace")) if is_last else (0, 0)
        inline = is_last and "\n" not in text and th <= self.body.metrics("linespace") * 1.4 and tw + SP_SM + time_w <= max_w
        bw, bh = max(sw, tw + SP_SM + time_w if inline else max(tw, time_w)) + 2 * SP_MD, sh + (max(th, time_h) if inline else th + (time_h + 2 if is_last else 0)) + 2 * SP_SM
        radii = (R_BUBBLE if mine or is_first else R_TOUCH, R_BUBBLE if not mine or is_first else R_TOUCH, (R_TAIL if is_last else R_TOUCH) if mine else R_BUBBLE, (R_TAIL if is_last else R_TOUCH) if not mine else R_BUBBLE)
        canvas.configure(width=bw, height=bh, bg=p["bg"]), canvas.tag_lower(rounded_rect(canvas, 0, 0, bw - 1, bh - 1, radii, fill=bg)), canvas.coords(tid, SP_MD, SP_SM + sh)
        if (not mine) and is_first: canvas.create_text(SP_MD, SP_SM, text=who, anchor="nw", fill=p["accent"], font=self.btn_bold)
        if is_last: canvas.create_text(bw - SP_MD, bh - SP_SM, text=stamp, anchor="se", fill=t_fg, font=self.small)

    def _drain(self) -> None:
        while not self._out.empty():
            for line in self._out.get_nowait().split("\n"):
                kind, who, text = classify(line, self.name)
                if not self._handle_transfer(kind, text):
                    idx = len(self._history)
                    self._history.append((kind, who, text, time.strftime("%H:%M"), time.time(), time.strftime("%Y-%m-%d")))
                    if idx > 0 and self._same_group(idx - 1, idx) and idx - 1 in self._bubble_widgets: self._layout_bubble(self._bubble_widgets[idx - 1], idx - 1)
                    self._render(idx)
        self._sync_empty_view()

    def _refresh(self) -> None:
        query = self.search_entry.get().strip().lower()
        peers = [p for p in self.store.get_peers() if not query or query in p.name.lower()]
        current = self.state.current
        rows = [(p.name, p.status, self.state.unread(p.peer_id), p.peer_id == current) for p in peers]
        partner = self.store.get(current) if current else None
        sig = (tuple(rows), (partner.name, partner.status, partner.ip, partner.tcp_port) if partner else None, self._dark, query)
        if sig != self._signature:
            was_empty = self._empty_visible
            self._signature, self._rows, self._peer_names, self._partner = sig, rows, [r[0] for r in rows], partner
            if not was_empty and not partner: self._empty_since = len(self._history)
            self._draw_peers(rows), self._update_header(partner), self._draw_pill(self._pills[0])

    def _draw_avatar(self, c: tk.Canvas, x: int, y: int, sz: int, name: str, font: tkfont.Font) -> None:
        c.create_oval(x, y, x + sz, y + sz, fill=PEER_COLORS[sum(map(ord, name)) % len(PEER_COLORS)], width=0)
        c.create_text(x + sz // 2, y + sz // 2, text=name[0].upper(), fill="#FFFFFF", font=font)

    def _sync_empty_view(self) -> None:
        if not self._empty_visible:
            self.empty_state.place_forget(), self.empty_hint.pack_forget()
            return
        if any(e[0] in ("info", "error", "plain") for e in self._history[self._empty_since:]):
            self.empty_state.place_forget(), self.empty_hint.pack(side="bottom", fill="x", before=self.log, pady=(0, SP_SM))
            return
        self.empty_hint.pack_forget(), self.empty_state.place(relx=0, rely=0, relwidth=1, relheight=1)
        c, p, max_w = self.empty_pill, self.palette, max(self._log_width - 2 * SP_XL, 200)
        sub = truncate_text(EMPTY_PROMPT, self.btn, max_w - 2 * SP_LG)
        w = min(max_w, max(self.empty_title.measure(EMPTY_TITLE), self.btn.measure(sub)) + 2 * SP_XL)
        c.delete("all"), c.configure(width=w, height=64), rounded_rect(c, 0, 0, w - 1, 63, R_BUBBLE, fill=p["pill_bg"])
        c.create_text(w // 2, 22, text=EMPTY_TITLE, fill=p["text"], font=self.empty_title), c.create_text(w // 2, 44, text=sub, fill=p["muted"], font=self.btn)

    def _update_header(self, partner: object) -> None:
        p, self._empty_visible = self.palette, not bool(partner)
        avail = max(self._log_width - 2 * SP_LG - (HEADER_AVATAR + 96 if partner else 0), 120)
        self.partner.configure(text=truncate_text(partner.name, self.title, avail) if partner else EMPTY_TITLE), self.partner_sub.configure(text=truncate_text(f"{partner.status}  -  {partner.ip}:{partner.tcp_port}" if partner else EMPTY_PROMPT, self.sub, avail))
        self._pills[1]["hint"] = f"Message {partner.name}" if partner else EMPTY_INPUT
        if partner:
            self.header_avatar.pack(side="left", before=self.header_info, padx=(0, SP_MD)), self.header_badge.pack(side="right")
            self.header_avatar.delete("all"), self.header_badge.delete("all"), self._draw_avatar(self.header_avatar, 0, 0, HEADER_AVATAR, partner.name, self.bold)
            rounded_rect(self.header_badge, 1, 1, 70, 20, 10, fill=p["input_bg"], outline=p["border"], width=1)
            self.header_badge.create_oval(9, 7, 17, 15, fill=STATUS_COLORS[partner.status], width=0), self.header_badge.create_text(23, 11, text=partner.status, anchor="w", fill=p["muted"], font=self.small)
        else:
            self.header_avatar.pack_forget(), self.header_badge.pack_forget()
        self._sync_empty_view(), self._draw_pill(self._pills[1])

    def _draw_peers(self, rows: list[tuple[str, str, int, bool]]) -> None:
        self.peers.delete("all")
        p, canvas, w = self.palette, self.peers, self._side_width - SCROLL_W
        for i, (name, status, unread, active) in enumerate(rows):
            top, row_bg = i * ROW_HEIGHT, p["row"] if active else (p["hover"] if i == self._hover_idx else p["panel"])
            canvas.create_rectangle(0, top, w, top + ROW_HEIGHT, fill=row_bg, width=0)
            x, y = SP_MD, top + (ROW_HEIGHT - AVATAR_SIZE) // 2
            self._draw_avatar(canvas, x, y, AVATAR_SIZE, name, self.bold)
            canvas.create_oval(x + AVATAR_SIZE - DOT_SIZE, y + AVATAR_SIZE - DOT_SIZE, x + AVATAR_SIZE, y + AVATAR_SIZE, fill=STATUS_COLORS[status], outline=row_bg, width=2)
            right = w - SP_MD
            if unread:
                label = "99+" if unread > 99 else str(unread)
                bx1 = right - max(20, self.btn_bold.measure(label) + SP_MD)
                rounded_rect(canvas, bx1, top + 20, right, top + 40, 10, fill="#FFFFFF" if active else p["accent"])
                canvas.create_text((bx1 + right) / 2, top + 30, text=label, fill=p["accent"] if active else "#FFFFFF", font=self.btn_bold)
                right = bx1 - SP_SM
            tx = x + AVATAR_SIZE + SP_MD
            canvas.create_text(tx, top + 20, text=truncate_text(name, self.bold, right - tx), anchor="w", fill=p["row_text" if active else "text"], font=self.bold), canvas.create_text(tx, top + 40, text=status, anchor="w", fill=p["row_sub" if active else "muted"], font=self.sub)
        canvas.configure(scrollregion=(0, 0, w, max(len(rows) * ROW_HEIGHT, 1)))

    def _poll(self) -> None:
        self._drain(), self._refresh()
        self.root.after(CLOSE_DELAY_MS if self.stop.is_set() else POLL_MS, self._close if self.stop.is_set() else self._poll)

    def _close(self) -> None:
        self.stop.set(), self._drain(), self._scroll_end(True), self.root.destroy()
