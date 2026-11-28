from __future__ import annotations

import threading
import tkinter as tk
import unittest
from unittest.mock import patch

from chat_state import ChatState
from gui_widgets import paint_snowflake
from peer_store import Peer

class QuietTk(tk.Tk):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.attributes("-alpha", 0.0)


def quiet_windows():
    return patch("tkinter.Tk", QuietTk)


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
            with quiet_windows():
                window = GuiTerminal("Me", StubStore(peers or []), ChatState(), threading.Event())
        except tk.TclError:
            self.skipTest("no display available")
        self.addCleanup(window.root.destroy)
        return window

    def texts(self, window) -> list[str]:
        canvas = window.peers
        return [canvas.itemcget(i, "text") for i in canvas.find_all() if canvas.type(i) == "text"]

    def test_messages_become_bubbles_and_notices_stay_text(self) -> None:
        window = self.open()
        for line in ("[Alice] hello", "[Me] hi there", "[*] Alice is online", "[!] boom"):
            window.say(line)
        window._drain()
        self.assertEqual(window.message_texts(), ["hello", "hi there"])
        shown = window.log_text()
        self.assertIn("Alice is online", shown)
        self.assertIn("boom", shown)
        canvas = window.view.canvas
        colors = {canvas.itemcget(i, "fill") for i in canvas.find_all() if canvas.type(i) == "text"}
        self.assertIn(window.palette["error"], colors)
        self.assertIn(window.palette["subtle"], colors)

    def test_multiline_say_is_split_into_separate_entries(self) -> None:
        window = self.open()
        window.say("[*] 2 unread:\n[Alice] one\n[Alice] two")
        window._drain()
        self.assertEqual(window.message_texts(), ["one", "two"])
        self.assertIn("2 unread:", window.log_text())

    def test_transfer_progress_bar_follows_the_notices(self) -> None:
        window = self.open()
        window.say("[*] Sending a.bin (1.0 MB) ...")
        window._drain()
        self.assertTrue(window._transferring)
        window.say("[*] a.bin  40%")
        window._drain()
        self.assertEqual(window.transfer_percent, 40)
        self.assertNotIn("40%", window.log_text())
        window.say("[*] Sent a.bin ... 100%  Done.")
        window._drain()
        self.assertFalse(window._transferring)
        self.assertIn("Done.", window.log_text())

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
        for expected in ("Alice", "Bob", "online", "hi", "1", "A", "B"):
            self.assertIn(expected, shown)
        fills = [window.peers.itemcget(i, "fill") for i in window.peers.find_all()
                 if window.peers.type(i) == "rectangle"]
        self.assertIn(window.palette["selected"], fills)
        self.assertNotIn(window.palette["hover"], fills)

    def test_unread_peer_shows_its_latest_message_as_preview(self) -> None:
        window = self.open([ALICE, BOB])
        window.state.add_message(BOB.peer_id, "Bob", "first")
        window.state.add_message(BOB.peer_id, "Bob", "second line")
        window._refresh()
        shown = self.texts(window)
        self.assertIn("second line", shown)
        self.assertNotIn("first", shown)
        self.assertNotIn("away", shown)

    def test_chat_partner_preview_shows_last_message_with_time(self) -> None:
        window = self.open([ALICE])
        window.state.switch_to(ALICE.peer_id)
        window._refresh()
        window.say("[Alice] hello there\nsecond line")
        window.say("[Me] on my way")
        with patch("gui.time.strftime", return_value="09:41"):
            window._drain()
        window._refresh()
        shown = self.texts(window)
        self.assertIn("You: on my way", shown)
        self.assertIn("09:41", shown)

    def test_profile_card_shows_own_name_and_toggles_status(self) -> None:
        window = self.open()
        self.assertEqual(window.profile.name, "Me")
        self.assertEqual(window.profile.status, "online")
        window.profile.command()
        self.assertEqual(window.profile.status, "away")
        self.assertEqual(next(window.lines()), "/status away")

    def test_header_actions_follow_the_selected_partner(self) -> None:
        window = self.open([ALICE])
        window._refresh()
        self.assertEqual(window.header_actions.winfo_manager(), "")
        window.state.switch_to(ALICE.peer_id)
        window._refresh()
        self.assertEqual(window.header_actions.winfo_manager(), "pack")

    def test_header_buttons_send_file_and_leave_chat(self) -> None:
        window = self.open([ALICE])
        window.state.switch_to(ALICE.peer_id)
        window._refresh()
        window.close_button.command()
        self.assertEqual(next(window.lines()), "/leave")
        with patch("gui.filedialog.askopenfilename", return_value="C:/x/y.txt"):
            window.file_action.command()
        self.assertEqual(next(window.lines()), "/send C:/x/y.txt")

    def test_empty_state_offers_three_suggestions(self) -> None:
        window = self.open([ALICE])
        window._refresh()
        chips = window.empty_actions.winfo_children()
        self.assertEqual([chip.text for chip in chips], ["Find someone", "Change status", "Switch theme"])
        chips[1].command()
        self.assertEqual(next(window.lines()), "/status away")

    def test_few_messages_sit_at_the_bottom_of_the_conversation(self) -> None:
        window = self.open([ALICE])
        window.state.switch_to(ALICE.peer_id)
        window._refresh()
        window.say("[Alice] hello")
        window._drain()
        window.root.update()
        window.root.update()
        canvas = window.view.canvas
        self.assertGreater(window.view.anchor_offset(), 100)
        bottom = canvas.bbox("content")[3]
        self.assertLessEqual(abs(canvas.winfo_height() - bottom), 24)

    def test_peer_statuses_report_what_the_sidebar_shows(self) -> None:
        window = self.open([ALICE, BOB])
        window._refresh()
        self.assertEqual(window.peer_statuses(), {"Alice": "online", "Bob": "away"})

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
        self.assertEqual(window.root.cget("bg"), window.palette["surface"])
        self.assertEqual(window.message_texts(), ["hello", "hi"])
        self.assertIs(window.theme_button.icon, paint_snowflake)

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

    def test_theme_button_cycles_light_dark_christmas(self) -> None:
        window = self.open()
        seen = []
        for _ in range(3):
            window._toggle_theme()
            seen.append(window._theme)
        self.assertEqual(seen, ["dark", "christmas", "light"])

    def test_christmas_theme_adds_wallpaper_garland_and_holly(self) -> None:
        window = self.open([ALICE])
        window.say("[Alice] hello")
        window._drain()
        self.assertEqual(window.view.canvas.find_withtag("wallpaper"), ())
        self.assertEqual(window.garland.winfo_manager(), "")
        window.set_theme("christmas")
        window.root.update()
        self.assertTrue(window.view.canvas.find_withtag("wallpaper"))
        self.assertTrue(window.peers.find_withtag("wallpaper"))
        self.assertEqual(window.garland.winfo_manager(), "pack")
        self.assertEqual(window.message_texts(), ["hello"])
        window.set_theme("light")
        self.assertEqual(window.view.canvas.find_withtag("wallpaper"), ())
        self.assertEqual(window.garland.winfo_manager(), "")

    def test_sidebar_header_counts_online_people(self) -> None:
        window = self.open([ALICE, BOB])
        window._refresh()
        self.assertEqual(window.online_count.cget("text"), "2 online")

    def test_escape_leaves_the_chat_then_clears_the_search(self) -> None:
        window = self.open([ALICE])
        window.state.switch_to(ALICE.peer_id)
        window._refresh()
        window._on_escape()
        self.assertEqual(next(window.lines()), "/leave")
        window.state.leave()
        window._refresh()
        window.search_entry.insert(0, "zz")
        window._on_escape()
        self.assertEqual(window.search_entry.get(), "")

    def test_ctrl_k_shortcut_is_handled(self) -> None:
        window = self.open()
        self.assertEqual(window._on_search_shortcut(), "break")
        self.assertEqual(window._on_info_shortcut(), "break")

    def test_copy_line_puts_the_message_on_the_clipboard(self) -> None:
        window = self.open([ALICE])
        window.say("[Alice] copy me")
        window._drain()
        window.view.copy_line(0)
        self.assertEqual(window.root.clipboard_get(), "copy me")

    def test_line_at_finds_the_message_under_the_pointer(self) -> None:
        window = self.open([ALICE])
        window.say("[Alice] hello")
        window.say("[Me] there")
        window._drain()
        window.root.update()
        canvas = window.view.canvas
        for index, expected in ((0, "hello"), (1, "there")):
            item = next(
                i for i in canvas.find_withtag(f"block{index}")
                if canvas.type(i) == "text" and canvas.itemcget(i, "text") == expected
            )
            left, top, right, bottom = canvas.bbox(item)
            x = (left + right) / 2 - canvas.canvasx(0)
            y = (top + bottom) / 2 - canvas.canvasy(0)
            self.assertEqual(window.view.line_at(x, y), index)

    def test_new_message_notice_raises_a_clickable_toast(self) -> None:
        window = self.open([ALICE])
        window.state.add_message(ALICE.peer_id, "Alice", "hi toast")
        window.say("[*] New message from Alice (1 unread)")
        window._drain()
        self.assertEqual((window.toast.name, window.toast.text), ("Alice", "hi toast"))
        self.assertEqual(window.toast.winfo_manager(), "place")
        window.toast._clicked(None)
        self.assertEqual(next(window.lines()), "/chat Alice")
        self.assertEqual(window.toast.winfo_manager(), "")

    def test_info_panel_lists_sent_and_received_files(self) -> None:
        window = self.open()
        for line in (
            "[*] Sending a.bin (1.0 MB) ...", "[*] a.bin  50%", "[*] Sent a.bin ... 100%  Done.",
            "[*] Receiving b.txt (2.0 KB) ...", "[*] Received b.txt -> C:/dl/b.txt",
        ):
            window.say(line)
        window._drain()
        shown = [(f.name, f.direction, f.size, f.path) for f in window.info.files]
        self.assertEqual(shown, [("a.bin", "sent", "1.0 MB", ""), ("b.txt", "received", "2.0 KB", "C:/dl/b.txt")])

    def test_sent_file_remembers_its_local_path(self) -> None:
        window = self.open()
        window.entry.insert(0, "/send C:/x/y.txt")
        window._submit()
        window.say("[*] Sending y.txt (3 B) ...")
        window.say("[*] Sent y.txt ... 100%  Done.")
        window._drain()
        self.assertEqual(window.info.files[0].path, "C:/x/y.txt")

    def test_info_button_toggles_the_panel_and_widens_the_window(self) -> None:
        window = self.open([ALICE])
        window.root.update()
        before = window.root.winfo_width()
        window._toggle_info()
        window.root.update()
        self.assertEqual(window.info_frame.winfo_manager(), "pack")
        self.assertGreaterEqual(window.root.winfo_width(), before + 250)
        window._toggle_info()
        window.root.update()
        self.assertEqual(window.info_frame.winfo_manager(), "")
        self.assertLessEqual(abs(window.root.winfo_width() - before), 4)

    def test_header_info_button_opens_and_closes_the_panel(self) -> None:
        window = self.open([ALICE])
        window.state.switch_to(ALICE.peer_id)
        window._refresh()
        window.info_button.command()
        self.assertEqual(window.info_frame.winfo_manager(), "pack")
        window.info_button.command()
        self.assertEqual(window.info_frame.winfo_manager(), "")

    def test_info_panel_shows_the_partner_and_opens_folders(self) -> None:
        window = self.open([ALICE])
        window.state.switch_to(ALICE.peer_id)
        window._refresh()
        window.say("[*] Received b.txt -> C:/dl/b.txt")
        window._drain()
        self.assertEqual(window.info.partner.name, "Alice")
        opened = []
        with patch("gui_info.open_folder", side_effect=opened.append):
            for _top, _bottom, action in window.info._regions:
                action()
        self.assertEqual(opened, ["C:/dl/b.txt", "downloads"])

    def test_closing_the_window_sets_stop(self) -> None:
        from gui import GuiTerminal

        try:
            with quiet_windows():
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
        shown = [[t for t in window.view.message_items(i) if t != "Today"] for i in range(3)]
        self.assertEqual(shown[0], ["one", "Alice"])
        self.assertEqual(shown[1][0], "two")
        self.assertNotIn("Alice", shown[1])
        self.assertIn("Alice", shown[2])

    def test_date_label_today_appears_once_before_first_message(self) -> None:
        window = self.open([ALICE])
        window.state.switch_to(ALICE.peer_id)
        window._refresh()
        window.say("[*] Connected to Alice")
        window.say("[Alice] first")
        window.say("[Me] second")
        window._drain()
        self.assertEqual(window.view.date_chip_texts(), ["Today"])
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
        canvas = window.view.canvas
        items = canvas.find_withtag("block0")
        self.assertTrue(items)
        self.assertNotIn("polygon", {canvas.type(i) for i in items})
        for item in items:
            if canvas.type(item) == "rectangle":
                self.assertEqual(canvas.itemcget(item, "outline"), "")

    def test_send_button_is_dimmed_when_empty_and_active_when_filled(self) -> None:
        window = self.open([ALICE])
        self.assertFalse(window._send_active)
        self.assertFalse(window.send_button.enabled)
        window.entry.insert(0, "   ")
        self.assertFalse(window._send_active)
        window.entry.delete(0, "end")
        window.entry.insert(0, "hello")
        self.assertTrue(window._send_active)
        self.assertTrue(window.send_button.enabled)
        window._submit()
        self.assertFalse(window._send_active)
        self.assertFalse(window.send_button.enabled)

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
