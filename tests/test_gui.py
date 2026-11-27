from __future__ import annotations

import threading
import tkinter as tk
import unittest
from unittest.mock import patch

from chat_state import ChatState
from peer_store import Peer

ALICE = Peer("11111111", "Alice", "10.0.0.2", 5001, "online", 0.0)
BOB = Peer("22222222", "Bob", "10.0.0.3", 5002, "away", 0.0)


class StubStore:
    def __init__(self, peers: list[Peer]) -> None:
        self.peers = peers

    def get_peers(self) -> list[Peer]:
        return self.peers

    def get(self, peer_id: str) -> Peer | None:
        return next((p for p in self.peers if p.peer_id == peer_id), None)


class TestClassify(unittest.TestCase):
    def test_line_kinds(self) -> None:
        from gui import classify

        self.assertEqual(classify("[*] Bob is online", "Me"), ("info", "", "Bob is online"))
        self.assertEqual(classify("[!] boom", "Me"), ("error", "", "boom"))
        self.assertEqual(classify("[Me] hi", "Me"), ("mine", "Me", "hi"))
        self.assertEqual(classify("[Bob] [x] hi", "Me"), ("other", "Bob", "[x] hi"))
        self.assertEqual(classify("/list   show users", "Me"), ("plain", "", "/list   show users"))


class TestGuiTerminal(unittest.TestCase):
    def open(self, peers: list[Peer] | None = None):
        from gui import GuiTerminal

        try:
            window = GuiTerminal("Me", StubStore(peers or []), ChatState(), threading.Event())
        except tk.TclError:
            self.skipTest("no display available")
        self.addCleanup(window.root.destroy)
        return window

    def windows(self, window) -> int:
        return sum(1 for item in window.log.dump("1.0", "end", window=True) if item[0] == "window")

    def texts(self, window) -> list[str]:
        canvas = window.peers
        return [canvas.itemcget(i, "text") for i in canvas.find_all() if canvas.type(i) == "text"]

    def test_messages_become_bubbles_and_notices_stay_text(self) -> None:
        window = self.open()
        for line in ("[Alice] hello", "[Me] hi there", "[*] Alice is online", "[!] boom"):
            window.say(line)
        window._drain()
        self.assertEqual(self.windows(window), 2)
        self.assertIn("Alice is online", window.log.get("1.0", "end"))
        self.assertTrue(window.log.tag_ranges("mine"))
        self.assertTrue(window.log.tag_ranges("other"))
        self.assertTrue(window.log.tag_ranges("info"))
        self.assertTrue(window.log.tag_ranges("error"))

    def test_multiline_say_is_split_into_separate_entries(self) -> None:
        window = self.open()
        window.say("[*] 2 unread:\n[Alice] one\n[Alice] two")
        window._drain()
        self.assertEqual(self.windows(window), 2)

    def test_transfer_progress_bar_follows_the_notices(self) -> None:
        window = self.open()
        window.say("[*] Sending a.bin (1.0 MB) ...")
        window._drain()
        self.assertTrue(window._transferring)
        window.say("[*] a.bin  40%")
        window._drain()
        self.assertEqual(window.bar["value"], 40)
        self.assertNotIn("40%", window.log.get("1.0", "end"))
        window.say("[*] Sent a.bin ... 100%  Done.")
        window._drain()
        self.assertFalse(window._transferring)
        self.assertIn("Done.", window.log.get("1.0", "end"))

    def test_error_ends_a_transfer(self) -> None:
        window = self.open()
        window.say("[*] Receiving a.bin (1.0 MB) ...")
        window.say("[!] receiving a.bin failed")
        window._drain()
        self.assertFalse(window._transferring)

    def test_enter_submits_the_typed_line_and_clears_the_box(self) -> None:
        window = self.open()
        window.entry.insert(0, "  hello there  ")
        window._submit()
        self.assertEqual(next(window.lines()), "hello there")
        self.assertEqual(window.entry.get(), "")

    def test_empty_input_is_ignored(self) -> None:
        window = self.open()
        window.entry.insert(0, "   ")
        window._submit()
        self.assertTrue(window._in.empty())

    def test_lines_ends_when_stop_is_set(self) -> None:
        window = self.open()
        window.stop.set()
        self.assertEqual(list(window.lines()), [])

    def test_file_button_issues_send_command(self) -> None:
        window = self.open()
        with patch("gui.filedialog.askopenfilename", return_value="C:/my docs/a b.pdf"):
            window._send_file()
        self.assertEqual(next(window.lines()), "/send C:/my docs/a b.pdf")

    def test_cancelled_file_dialog_sends_nothing(self) -> None:
        window = self.open()
        with patch("gui.filedialog.askopenfilename", return_value=""):
            window._send_file()
        self.assertTrue(window._in.empty())

    def test_status_button_toggles_between_online_and_away(self) -> None:
        window = self.open()
        window._change_status()
        window._change_status()
        self.assertEqual([next(window.lines()), next(window.lines())], ["/status away", "/status online"])

    def test_sidebar_shows_names_status_unread_and_marks_current(self) -> None:
        window = self.open([ALICE, BOB])
        window.state.add_message(BOB.peer_id, "Bob", "hi")
        window.state.switch_to(ALICE.peer_id)
        window._refresh()
        shown = self.texts(window)
        for expected in ("Alice", "Bob", "online", "away", "1", "A", "B"):
            self.assertIn(expected, shown)
        fills = [window.peers.itemcget(i, "fill") for i in window.peers.find_all()
                 if window.peers.type(i) == "rectangle"]
        self.assertEqual(fills, [window.palette["row"], window.palette["panel"]])

    def test_header_names_the_current_partner(self) -> None:
        window = self.open([ALICE, BOB])
        self.assertEqual(window.partner.cget("text"), "No chat selected")
        window.state.switch_to(BOB.peer_id)
        window._refresh()
        self.assertEqual(window.partner.cget("text"), "Bob")
        self.assertIn("away", window.partner_sub.cget("text"))

    def test_clicking_a_row_opens_that_chat(self) -> None:
        window = self.open([ALICE, BOB])
        window._refresh()
        window._open_chat(1)
        window._open_chat(5)
        window._open_chat(-1)
        self.assertEqual(next(window.lines()), "/chat Bob")
        self.assertTrue(window._in.empty())

    def test_theme_toggle_recolours_and_keeps_the_history(self) -> None:
        window = self.open([ALICE])
        window.say("[Alice] hello")
        window.say("[Me] hi")
        window._drain()
        light = window.palette["bg"]
        window._toggle_theme()
        self.assertNotEqual(window.palette["bg"], light)
        self.assertEqual(window.root.cget("bg"), window.palette["bg"])
        self.assertEqual(self.windows(window), 2)
        self.assertEqual(window.theme_button.cget("text"), "Light mode")

    def test_closing_the_window_sets_stop(self) -> None:
        from gui import GuiTerminal

        try:
            window = GuiTerminal("Me", StubStore([]), ChatState(), threading.Event())
        except tk.TclError:
            self.skipTest("no display available")
        window._close()
        self.assertTrue(window.stop.is_set())


if __name__ == "__main__":
    unittest.main()
