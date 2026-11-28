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

    def bubble_widgets(self, window) -> list[tk.Canvas]:
        return [
            window.log.nametowidget(item[1])
            for item in window.log.dump("1.0", "end", window=True)
            if item[0] == "window"
        ]

    def bubble_texts(self, canvas: tk.Canvas) -> list[str]:
        return [canvas.itemcget(i, "text") for i in canvas.find_all() if canvas.type(i) == "text"]

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

    def test_inspection_hooks_report_what_is_on_screen(self) -> None:
        window = self.open([ALICE, BOB])
        window.state.switch_to(BOB.peer_id)
        for line in ("[Alice] hello", "[Me] hi", "[*] Sending a.bin (1.0 MB) ...", "[!] boom"):
            window.say(line)
        window._drain()
        window._refresh()
        self.assertEqual(window.message_texts(), ["hello", "hi"])
        self.assertIn("Alice", window.sidebar_texts())
        self.assertEqual(window.partner_title(), "Bob")
        self.assertFalse(window.transfer_active())
        self.assertIn("boom", window.log_text())

    def test_set_theme_switches_to_the_named_theme(self) -> None:
        window = self.open()
        window.set_theme("light")
        light = window.palette["bg"]
        window.set_theme("dark")
        self.assertNotEqual(window.palette["bg"], light)
        window.set_theme("light")
        self.assertEqual(window.palette["bg"], light)

    def test_closing_the_window_sets_stop(self) -> None:
        from gui import GuiTerminal

        try:
            window = GuiTerminal("Me", StubStore([]), ChatState(), threading.Event())
        except tk.TclError:
            self.skipTest("no display available")
        window._close()
        self.assertTrue(window.stop.is_set())

    def test_consecutive_messages_group_within_five_minutes_and_split_after(self) -> None:
        window = self.open([ALICE])
        with patch("gui.time.time", side_effect=[1000.0, 1060.0, 1400.0]):
            window.say("[Alice] one")
            window.say("[Alice] two")
            window.say("[Alice] three")
            window._drain()
        bubbles = self.bubble_widgets(window)
        self.assertEqual(len(bubbles), 3)
        self.assertEqual(self.bubble_texts(bubbles[0]), ["one", "Alice"])
        self.assertEqual(self.bubble_texts(bubbles[1])[0], "two")
        self.assertNotIn("Alice", self.bubble_texts(bubbles[1]))
        self.assertIn("Alice", self.bubble_texts(bubbles[2]))

    def test_date_label_today_appears_once_before_first_message(self) -> None:
        window = self.open([ALICE])
        window.state.switch_to(ALICE.peer_id)
        window._refresh()
        window.say("[*] Connected to Alice")
        window.say("[Alice] first")
        window.say("[Me] second")
        window._drain()
        raw = window.log.get("1.0", "end")
        self.assertEqual(raw.count("Today"), 1)
        self.assertTrue(window.log.tag_ranges("date"))
        self.assertIn("Today", window.log_text())

    def test_empty_state_shows_when_unselected_and_hides_when_selected(self) -> None:
        window = self.open([ALICE])
        window._refresh()
        self.assertTrue(window._empty_visible)
        self.assertEqual(window.empty_state.winfo_manager(), "place")
        self.assertIn("Select", window.log_text())
        window.state.switch_to(ALICE.peer_id)
        window._refresh()
        self.assertFalse(window._empty_visible)
        self.assertEqual(window.empty_state.winfo_manager(), "")
        window.state.leave()
        window._refresh()
        self.assertTrue(window._empty_visible)
        self.assertEqual(window.empty_state.winfo_manager(), "place")

    def test_notices_remain_visible_in_empty_state_and_log_text(self) -> None:
        window = self.open([ALICE])
        window._refresh()
        window.say("[*] Quan is online")
        window.say("[*] New message from Quan (1 unread)")
        window.say("[!] Minh is offline")
        window._drain()
        self.assertEqual(window.empty_state.winfo_manager(), "")
        self.assertEqual(window.empty_hint.winfo_manager(), "pack")
        text = window.log_text()
        self.assertIn("Quan is online", text)
        self.assertIn("New message from Quan (1 unread)", text)
        self.assertIn("Minh is offline", text)

    def test_sidebar_shrinks_on_narrow_window(self) -> None:
        nhung = Peer("33333333", "NguyenThiHongNhung", "10.0.0.4", 5003, "online", 0.0)
        window = self.open([ALICE, nhung])
        window.root.geometry("720x520")
        window.root.update()
        self.assertEqual(window._side_width, 220)
        self.assertGreaterEqual(window.main.winfo_width(), 480)
        self.assertTrue(any(text.endswith("...") for text in window.sidebar_texts()))
        window.root.geometry("960x640")
        window.root.update()
        self.assertEqual(window._side_width, 300)

    def test_long_peer_name_is_truncated_with_ellipsis(self) -> None:
        long_name = "NguyenThiHongNhungWithAVeryLongDisplayName"
        long_peer = Peer("33333333", long_name, "10.0.0.4", 5003, "online", 0.0)
        window = self.open([long_peer])
        window._refresh()
        shown = window.sidebar_texts()
        self.assertNotIn(long_name, shown)
        self.assertTrue(any(text.endswith("...") for text in shown))

    def test_bubbles_and_controls_have_no_black_outline(self) -> None:
        window = self.open([ALICE])
        window.say("[Alice] hello")
        window._drain()
        bubble = self.bubble_widgets(window)[0]
        polys = [i for i in bubble.find_all() if bubble.type(i) == "polygon"]
        self.assertEqual(bubble.itemcget(polys[0], "outline"), "")

    def test_send_button_is_dimmed_when_empty_and_active_when_filled(self) -> None:
        window = self.open([ALICE])
        self.assertFalse(window._send_active)
        self.assertEqual(window.send_button.itemcget(window.send_button.find_all()[0], "fill"), window.palette["dim"])
        window.entry.insert(0, "   ")
        self.assertFalse(window._send_active)
        window.entry.delete(0, "end")
        window.entry.insert(0, "hello")
        self.assertTrue(window._send_active)
        self.assertEqual(window.send_button.itemcget(window.send_button.find_all()[0], "fill"), window.palette["accent"])
        window._submit()
        self.assertFalse(window._send_active)
        self.assertEqual(window.send_button.itemcget(window.send_button.find_all()[0], "fill"), window.palette["dim"])

    def test_input_stays_mapped_during_vietnamese_edits_and_redraws(self) -> None:
        window = self.open([ALICE])
        window.state.switch_to(ALICE.peer_id)
        window._refresh()
        window.root.update()
        unmapped = []
        window.entry.bind("<Unmap>", lambda _e: unmapped.append(True), add="+")
        for text in ("t", "ti", "tie", "tiế", "tiếng", "tiếng Việt", "tiếng Việt "):
            window.entry.delete(0, "end")
            window.entry.insert(0, text)
            window.root.update()
        window.say("[Alice] hello")
        window._drain()
        window._toggle_theme()
        window.root.geometry("720x520")
        window.root.update()
        self.assertEqual(unmapped, [])
        self.assertEqual(window.entry.get(), "tiếng Việt ")
        window._submit()
        self.assertEqual(next(window.lines()), "tiếng Việt")
        self.assertEqual(window.entry.get(), "")

    def test_search_filters_peers_and_open_chat_uses_visible_list(self) -> None:
        window = self.open([ALICE, BOB])
        window._refresh()
        window.search_entry.insert(0, "bob")
        shown = window.sidebar_texts()
        self.assertIn("Bob", shown)
        self.assertNotIn("Alice", shown)
        window._open_chat(0)
        self.assertEqual(next(window.lines()), "/chat Bob")


if __name__ == "__main__":
    unittest.main()
