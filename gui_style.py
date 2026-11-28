from __future__ import annotations

import re
import sys
from tkinter import font as tkfont

SP_XS = 4
SP_SM = 8
SP_MD = 12
SP_LG = 16
SP_XL = 24

F_TIME = 7
F_SUB = 8
F_BTN = 9
F_BODY = 10
F_TITLE = 12
F_EMPTY = 13

ELLIPSIS = "..."

AVATAR_COLORS = ("#E5484D", "#E26A1B", "#B68A00", "#2F9E5B", "#0E9C8F", "#3E63DD", "#8145B5", "#D6409F")
STATUS_COLORS = {"online": "#30A46C", "away": "#F5A524"}

LIGHT = {
    "bg": "#FFFFFF", "panel": "#F8F9FA", "text": "#1C2024", "muted": "#60646C", "subtle": "#8B8D98",
    "surface": "#FFFFFF", "flake": "",
    "line": "#E6E8EB", "hover": "#EEF0F3", "selected": "#E6ECFE",
    "accent": "#3E63DD", "accent_hover": "#3558C9", "accent_press": "#2F4EB5", "on_accent": "#FFFFFF",
    "field": "#F1F3F5", "field_focus": "#FFFFFF", "ring": "#C7D4FB",
    "mine": "#3E63DD", "mine_text": "#FFFFFF", "mine_time": "#C9D6FF",
    "other": "#F1F3F5", "other_text": "#1C2024", "other_time": "#8B8D98",
    "chip": "#EEF0F3", "chip_text": "#60646C", "error": "#CD2B31", "disabled": "#E1E4E8",
    "scroll": "#D0D5DB", "scroll_hover": "#A9B0BA",
}
DARK = {
    "bg": "#111214", "panel": "#17181B", "text": "#ECEEF1", "muted": "#A1A6AE", "subtle": "#767B84",
    "surface": "#111214", "flake": "",
    "line": "#26282D", "hover": "#202226", "selected": "#1E2A55",
    "accent": "#4C6BEB", "accent_hover": "#6382F0", "accent_press": "#3E5CD6", "on_accent": "#FFFFFF",
    "field": "#1F2125", "field_focus": "#17181B", "ring": "#2B3A78",
    "mine": "#4C6BEB", "mine_text": "#FFFFFF", "mine_time": "#CFD9FF",
    "other": "#212328", "other_text": "#ECEEF1", "other_time": "#767B84",
    "chip": "#212328", "chip_text": "#A1A6AE", "error": "#FF8589", "disabled": "#2A2D33",
    "scroll": "#3A3E46", "scroll_hover": "#565B66",
}

CHRISTMAS = {
    "bg": "#D6EBFA", "panel": "#E9F4FC", "text": "#16304D", "muted": "#4F6E8E", "subtle": "#7F9BB8",
    "surface": "#F2F9FE", "flake": "#FFFFFF",
    "line": "#BDD9EE", "hover": "#D3E8F8", "selected": "#FFDAD6",
    "accent": "#E53935", "accent_hover": "#D32F2F", "accent_press": "#B71C1C", "on_accent": "#FFFFFF",
    "field": "#FFFFFF", "field_focus": "#FFFFFF", "ring": "#FFC4C0",
    "mine": "#E53935", "mine_text": "#FFFFFF", "mine_time": "#FFD6D3",
    "other": "#FFFFFF", "other_text": "#16304D", "other_time": "#7F9BB8",
    "chip": "#FFE082", "chip_text": "#6B4F00", "error": "#C62828", "disabled": "#C5DBEC",
    "scroll": "#A9C8E2", "scroll_hover": "#7FA8CC",
}
THEMES = {"light": LIGHT, "dark": DARK, "christmas": CHRISTMAS}
THEME_ORDER = ("light", "dark", "christmas")
GARLAND_COLORS = ("#E53935", "#43A047", "#FFC107", "#1E88E5")

MESSAGE_LINE = re.compile(r"\[([^\]]+)\] (.*)", re.S)


def classify(line: str, me: str) -> tuple[str, str, str]:
    if line[:3] in ("[*]", "[!]"):
        kind = "error" if line[1] == "!" else "info"
        return kind, "", line[4:]
    match = MESSAGE_LINE.fullmatch(line)
    if match is None:
        return "plain", "", line
    kind = "mine" if match[1] == me else "other"
    return kind, match[1], match[2]


class Fonts:
    def __init__(self) -> None:
        if sys.platform == "win32":
            family, mono = "Segoe UI", "Consolas"
        else:
            family = tkfont.nametofont("TkDefaultFont")["family"]
            mono = tkfont.nametofont("TkFixedFont")["family"]
        self.small = tkfont.Font(family=family, size=F_TIME)
        self.sub = tkfont.Font(family=family, size=F_SUB)
        self.btn = tkfont.Font(family=family, size=F_BTN)
        self.btn_bold = tkfont.Font(family=family, size=F_BTN, weight="bold")
        self.mono = tkfont.Font(family=mono, size=F_BTN)
        self.body = tkfont.Font(family=family, size=F_BODY)
        self.bold = tkfont.Font(family=family, size=F_BODY, weight="bold")
        self.title = tkfont.Font(family=family, size=F_TITLE, weight="bold")
        self.empty_title = tkfont.Font(family=family, size=F_EMPTY, weight="bold")


def truncate_text(text: str, font: tkfont.Font, max_width: int) -> str:
    if max_width <= 0 or font.measure(text) <= max_width:
        return text
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if font.measure(text[:middle] + ELLIPSIS) <= max_width:
            low = middle
        else:
            high = middle - 1
    return text[:low] + ELLIPSIS if low > 0 else ELLIPSIS
