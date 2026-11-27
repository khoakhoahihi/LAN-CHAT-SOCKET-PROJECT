from __future__ import annotations

import codecs
import contextlib
import os
import shutil
import sys
import threading
from collections.abc import Iterator

if sys.platform == "win32":
    import msvcrt
else:
    import select
    import termios
    import tty

PROMPT = "> "
KEY_POLL = 0.05
ARROW_KEYS = "HPKMGOIQRS"
CTRL_C, CTRL_D, CTRL_Z, ESC = "\x03", "\x04", "\x1a", "\x1b"
BACKSPACE = ("\x08", "\x7f")


class Terminal:
    def __init__(self, stop: threading.Event) -> None:
        self.stop = stop
        self._lock = threading.Lock()
        self._buf: list[str] = []
        self._pending: list[str] = []
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._fd = -1
        if self.interactive() and sys.platform == "win32":
            os.system("")

    def interactive(self) -> bool:
        return sys.stdin.isatty() and sys.stdout.isatty()

    def say(self, text: str) -> None:
        with self._lock:
            if not self.interactive():
                self._write(f"{text}\n")
            elif self.stop.is_set():
                self._write(f"{self._erase_prompt()}{text}\n")
            else:
                self._write(f"{self._erase_prompt()}{text}\n{self._prompt()}")

    def lines(self) -> Iterator[str]:
        if not self.interactive():
            for raw in sys.stdin:
                yield raw
                if self.stop.is_set():
                    return
            return
        with self._raw_mode():
            while not self.stop.is_set():
                ch = self._read_key()
                if ch is None:
                    continue
                if ch == ESC:
                    self._skip_escape_sequence()
                elif ch == CTRL_C or (ch in (CTRL_D, CTRL_Z) and not self._buf):
                    return
                elif ch in ("\r", "\n"):
                    yield self._submit()
                    if self.stop.is_set():
                        return
                    with self._lock:
                        self._write(f"\r\033[K{self._prompt()}")
                elif ch in BACKSPACE:
                    self._backspace()
                elif ch.isprintable():
                    with self._lock:
                        self._buf.append(ch)
                        self._write(ch)

    def _write(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    def _prompt(self) -> str:
        return PROMPT + "".join(self._buf)

    def _erase_prompt(self) -> str:
        rows_up = (len(self._prompt()) - 1) // shutil.get_terminal_size().columns
        return "\r" + (f"\033[{rows_up}A" if rows_up else "") + "\033[J"

    def _submit(self) -> str:
        with self._lock:
            self._write(self._erase_prompt())
            line = "".join(self._buf)
            self._buf.clear()
        return line

    def _backspace(self) -> None:
        with self._lock:
            if self._buf:
                erase = self._erase_prompt()
                self._buf.pop()
                self._write(erase + self._prompt())

    @contextlib.contextmanager
    def _raw_mode(self) -> Iterator[None]:
        if sys.platform == "win32":
            yield
            return
        self._fd = sys.stdin.fileno()
        saved = termios.tcgetattr(self._fd)
        tty.setcbreak(self._fd)
        try:
            yield
        finally:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, saved)

    def _skip_escape_sequence(self) -> None:
        ch = self._read_key()
        if ch not in ("[", "O"):
            if ch is not None:
                self._pending.insert(0, ch)
            return
        while True:
            ch = self._read_key()
            if ch is None or "@" <= ch <= "~":
                return

    def _read_key(self) -> str | None:
        if self._pending:
            return self._pending.pop(0)
        if sys.platform == "win32":
            return self._read_key_windows()
        ready, _, _ = select.select([self._fd], [], [], KEY_POLL)
        if not ready:
            return None
        data = os.read(self._fd, 4096)
        if not data:
            return CTRL_C
        self._pending.extend(self._decoder.decode(data))
        return self._pending.pop(0) if self._pending else None

    def _read_key_windows(self) -> str | None:
        if not msvcrt.kbhit():
            self.stop.wait(KEY_POLL)
            return None
        ch = msvcrt.getwch()
        if ch == "\x00":
            msvcrt.getwch()
            return None
        if ch == "\xe0" and msvcrt.kbhit():
            follow = msvcrt.getwch()
            if follow in ARROW_KEYS:
                return None
            self._pending.append(follow)
        return ch
