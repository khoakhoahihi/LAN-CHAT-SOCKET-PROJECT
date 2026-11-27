from __future__ import annotations

import contextlib
import io
import os
import sys
import threading
import unittest
from unittest.mock import patch

from terminal import Terminal


class FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


def run_lines(keys: list[str | None], typed: str = "") -> tuple[list[str], str]:
    stop = threading.Event()
    term = Terminal(stop)
    out = FakeTTY()
    keys = list(keys)

    def next_key() -> str | None:
        if term._pending:
            return term._pending.pop(0)
        return keys.pop(0) if keys else "\x03"

    with patch("sys.stdin", FakeTTY(typed)), patch("sys.stdout", out):
        with patch.object(term, "_read_key", next_key), patch.object(
            term, "_raw_mode", contextlib.nullcontext
        ):
            return list(term.lines()), out.getvalue()


class TestSay(unittest.TestCase):
    def test_plain_output_has_no_prompt(self) -> None:
        out = io.StringIO()
        with patch("sys.stdin", io.StringIO()), patch("sys.stdout", out):
            Terminal(threading.Event()).say("hello")
        self.assertEqual(out.getvalue(), "hello\n")

    def test_terminal_mode_redraws_prompt_with_typed_text(self) -> None:
        term = Terminal(threading.Event())
        term._buf = list("abc")
        out = FakeTTY()
        with patch("sys.stdin", FakeTTY()), patch("sys.stdout", out):
            term.say("x")
        self.assertEqual(out.getvalue(), "\r\033[Jx\n> abc")

    def test_wrapped_prompt_moves_up_before_clearing(self) -> None:
        term = Terminal(threading.Event())
        term._buf = list("a" * 15)
        out = FakeTTY()
        with patch("sys.stdin", FakeTTY()), patch("sys.stdout", out):
            with patch("terminal.shutil.get_terminal_size", return_value=os.terminal_size((10, 24))):
                term.say("x")
        self.assertEqual(out.getvalue(), "\r\033[1A\033[Jx\n> " + "a" * 15)

    def test_after_stop_leaves_no_prompt(self) -> None:
        stop = threading.Event()
        stop.set()
        out = FakeTTY()
        with patch("sys.stdin", FakeTTY()), patch("sys.stdout", out):
            Terminal(stop).say("[*] Bye")
        self.assertEqual(out.getvalue(), "\r\033[J[*] Bye\n")


class TestLines(unittest.TestCase):
    def test_plain_mode_yields_each_line(self) -> None:
        term = Terminal(threading.Event())
        with patch("sys.stdin", io.StringIO("a\nb\n")):
            self.assertEqual(list(term.lines()), ["a\n", "b\n"])

    def test_plain_mode_stops_once_stop_is_set(self) -> None:
        stop = threading.Event()
        term = Terminal(stop)
        got = []
        with patch("sys.stdin", io.StringIO("a\nb\n")):
            for line in term.lines():
                got.append(line)
                stop.set()
        self.assertEqual(got, ["a\n"])

    def test_typing_enter_yields_the_line(self) -> None:
        lines, out = run_lines(list("hi") + ["\r"])
        self.assertEqual(lines, ["hi"])
        self.assertIn("hi", out)

    def test_backspace_removes_last_character(self) -> None:
        lines, _ = run_lines(list("abc") + ["\x7f", "\r"])
        self.assertEqual(lines, ["ab"])

    def test_none_keys_are_skipped(self) -> None:
        lines, _ = run_lines([None, "a", None, "\r"])
        self.assertEqual(lines, ["a"])

    def test_ctrl_d_quits_only_on_empty_line(self) -> None:
        lines, _ = run_lines(["a", "\x04", "\r"])
        self.assertEqual(lines, ["a"])
        lines, _ = run_lines(["\x04", "x", "\r"])
        self.assertEqual(lines, [])

    def test_arrow_key_escape_sequences_are_dropped(self) -> None:
        keys = ["a", "\x1b", "[", "A", "b", "\x1b", "O", "B", "\x1b", "[", "1", ";", "5", "C", "c", "\r"]
        lines, _ = run_lines(keys)
        self.assertEqual(lines, ["abc"])

    def test_unprintable_keys_are_ignored(self) -> None:
        lines, _ = run_lines(["a", "\x1b", "b", "\r"])
        self.assertEqual(lines, ["ab"])


@unittest.skipIf(sys.platform == "win32", "os.read on a pipe fd is the Unix path")
class TestUnixKeys(unittest.TestCase):
    def setUp(self) -> None:
        self.r, self.w = os.pipe()
        self.term = Terminal(threading.Event())
        self.term._fd = self.r

    def tearDown(self) -> None:
        os.close(self.r)
        os.close(self.w)

    def read_all(self, count: int) -> list[str | None]:
        return [self.term._read_key() for _ in range(count)]

    def test_all_pasted_characters_are_available_at_once(self) -> None:
        os.write(self.w, b"abc")
        self.assertEqual(self.read_all(3), ["a", "b", "c"])

    def test_multibyte_character_split_across_reads(self) -> None:
        os.write(self.w, "à".encode("utf-8")[:1])
        self.assertIsNone(self.term._read_key())
        os.write(self.w, "à".encode("utf-8")[1:])
        self.assertEqual(self.term._read_key(), "à")

    def test_nothing_typed_returns_none(self) -> None:
        self.assertIsNone(self.term._read_key())

    def test_closed_terminal_reads_as_ctrl_c(self) -> None:
        os.close(self.w)
        self.assertEqual(self.term._read_key(), "\x03")
        self.w = os.open(os.devnull, os.O_WRONLY)


@unittest.skipUnless(sys.platform == "win32", "msvcrt only on Windows")
class TestWindowsKeys(unittest.TestCase):
    def read_all(self, keys: list[str], count: int) -> list[str | None]:
        class FakeMsvcrt:
            @staticmethod
            def kbhit() -> bool:
                return bool(keys)

            @staticmethod
            def getwch() -> str:
                return keys.pop(0)

        term = Terminal(threading.Event())
        with patch("terminal.msvcrt", FakeMsvcrt):
            return [term._read_key() for _ in range(count)]

    def test_accented_a_keeps_the_next_character(self) -> None:
        self.assertEqual(self.read_all(["\xe0", "o"], 2), ["\xe0", "o"])

    def test_arrow_keys_are_swallowed(self) -> None:
        keys = ["\xe0", "H", "\x00", "P"]
        self.assertEqual(self.read_all(keys, 2), [None, None])
        self.assertEqual(keys, [])


if __name__ == "__main__":
    unittest.main()
