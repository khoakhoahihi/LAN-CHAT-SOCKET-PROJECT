from __future__ import annotations

import io
import os
import queue
import sys
import threading
import types
import unittest
from unittest.mock import patch

from chat_state import ChatState
from cli import CLI
import events
from peer_store import Peer
from transport import ConnectError, NotConnected


class StubStore:
    def __init__(self, peers: list[Peer] | None = None) -> None:
        self.peers = {p.peer_id: p for p in (peers or [])}

    def get_peers(self) -> list[Peer]:
        return sorted(self.peers.values(), key=lambda p: (p.name, p.peer_id))

    def get(self, peer_id: str) -> Peer | None:
        return self.peers.get(peer_id)

    def find_by_name(self, name: str) -> list[Peer]:
        return sorted(
            [p for p in self.peers.values() if p.name == name],
            key=lambda p: (p.name, p.peer_id),
        )


class StubTransport:
    def __init__(self) -> None:
        self.connected: set[str] = set()
        self.sent_messages: list[tuple[str, str]] = []
        self.disconnected: list[str] = []
        self.fail_connect = False

    def is_connected(self, peer_id: str) -> bool:
        return peer_id in self.connected

    def connect_chat(self, peer: Peer) -> None:
        if self.fail_connect:
            raise ConnectError("connection failed")
        self.connected.add(peer.peer_id)

    def send_chat(self, peer_id: str, text: str) -> None:
        if peer_id not in self.connected:
            raise NotConnected("not connected")
        self.sent_messages.append((peer_id, text))

    def disconnect_chat(self, peer_id: str) -> None:
        self.disconnected.append(peer_id)
        self.connected.discard(peer_id)


class StubDiscovery:
    def __init__(self) -> None:
        self.status = "online"

    def set_status(self, status: str) -> None:
        if status not in ("online", "away"):
            raise ValueError("invalid status")
        self.status = status


def make_cli(
    peers: list[Peer] | None = None,
) -> tuple[
    CLI,
    StubStore,
    StubTransport,
    StubDiscovery,
    ChatState,
    queue.Queue,
    threading.Event,
]:
    store = StubStore(peers)
    transport = StubTransport()
    discovery = StubDiscovery()
    state = ChatState()
    ev_queue: queue.Queue = queue.Queue()
    stop = threading.Event()
    cli = CLI(
        my_id="00000001",
        name="Tester",
        store=store,
        transport=transport,
        discovery=discovery,
        state=state,
        events=ev_queue,
        stop=stop,
        tcp_port=5000,
        udp_port=9999,
        downloads="downloads",
    )
    return cli, store, transport, discovery, state, ev_queue, stop


class FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


class TestCLI(unittest.TestCase):
    def test_unknown_command(self) -> None:
        cli, _, _, _, _, _, _ = make_cli()
        out = io.StringIO()
        with patch("sys.stdout", out):
            cli._handle_line("/foo")
        self.assertIn("[*] Unknown command: /foo. Type /help\n", out.getvalue())

    def test_chat_ambiguous_name(self) -> None:
        p1 = Peer("11111111", "Alice", "192.168.1.10", 5001, "online", 0.0)
        p2 = Peer("22222222", "Alice", "192.168.1.11", 5002, "away", 0.0)
        cli, _, _, _, _, _, _ = make_cli([p1, p2])
        out = io.StringIO()
        with patch("sys.stdout", out):
            cli._handle_line("/chat Alice")
        val = out.getvalue()
        self.assertIn(" 1. Alice  (192.168.1.10)  online", val)
        self.assertIn(" 2. Alice  (192.168.1.11)  away", val)
        self.assertIn("[!] 2 users named Alice, use /chat <number>", val)

    def test_chat_number_out_of_range(self) -> None:
        p1 = Peer("11111111", "Alice", "192.168.1.10", 5001, "online", 0.0)
        cli, _, _, _, _, _, _ = make_cli([p1])
        out = io.StringIO()
        with patch("sys.stdout", out):
            cli._handle_line("/chat 9")
        self.assertIn("[!] No user number 9. Use /list", out.getvalue())

    def test_plain_text_no_active_chat(self) -> None:
        cli, _, _, _, _, _, _ = make_cli()
        out = io.StringIO()
        with patch("sys.stdout", out):
            cli._handle_line("hello there")
        self.assertIn("[*] No active chat. Use /chat <name|number>", out.getvalue())

    def test_send_no_active_chat_and_missing_file(self) -> None:
        p1 = Peer("11111111", "Alice", "192.168.1.10", 5001, "online", 0.0)
        cli, _, _, _, state, _, _ = make_cli([p1])
        out = io.StringIO()
        with patch("sys.stdout", out):
            cli._handle_line("/send file.txt")
        self.assertIn("[*] No active chat. Use /chat <name|number>", out.getvalue())

        state.current = "11111111"
        out2 = io.StringIO()
        with patch("sys.stdout", out2):
            cli._handle_line("/send /nonexistent/path/file.txt")
        self.assertIn("[!] File not found: /nonexistent/path/file.txt", out2.getvalue())

    def test_chat_received_current_peer(self) -> None:
        cli, _, _, _, state, _, _ = make_cli()
        state.switch_to("11111111")
        out = io.StringIO()
        with patch("sys.stdout", out):
            cli._show(
                events.Event(
                    events.CHAT_RECEIVED,
                    {"peer_id": "11111111", "name": "Alice", "text": "hello"},
                )
            )
        self.assertEqual(out.getvalue(), "[Alice] hello\n")

    def test_chat_received_other_peer_unread(self) -> None:
        cli, _, _, _, state, _, _ = make_cli()
        state.switch_to("11111111")
        out = io.StringIO()
        with patch("sys.stdout", out):
            cli._show(
                events.Event(
                    events.CHAT_RECEIVED,
                    {"peer_id": "22222222", "name": "Bob", "text": "hey 1"},
                )
            )
            cli._show(
                events.Event(
                    events.CHAT_RECEIVED,
                    {"peer_id": "22222222", "name": "Bob", "text": "hey 2"},
                )
            )
        val = out.getvalue()
        self.assertIn("[*] New message from Bob (1 unread)", val)
        self.assertIn("[*] New message from Bob (2 unread)", val)

    def test_peer_updated_ip_only_prints_nothing(self) -> None:
        cli, _, _, _, _, _, _ = make_cli()
        old = Peer("11111111", "Alice", "192.168.1.10", 5001, "online", 0.0)
        new = Peer("11111111", "Alice", "192.168.1.20", 5001, "online", 0.0)
        out = io.StringIO()
        with patch("sys.stdout", out):
            cli._show(events.Event(events.PEER_UPDATED, {"peer": new, "old": old}))
        self.assertEqual(out.getvalue(), "")

    def test_list_formatting(self) -> None:
        p1 = Peer("22222222", "Minh", "192.168.1.12", 6000, "online", 0.0)
        p2 = Peer("33333333", "Huy", "192.168.1.20", 7000, "away", 0.0)
        cli, _, _, _, _, _, _ = make_cli([p1, p2])
        out = io.StringIO()
        with patch("sys.stdout", out):
            cli._handle_line("/list")
        val = out.getvalue()
        expected = " 1. Huy   (192.168.1.20)  away\n 2. Minh  (192.168.1.12)  online"
        self.assertIn(expected, val)

        cli_empty, _, _, _, _, _, _ = make_cli()
        out_empty = io.StringIO()
        with patch("sys.stdout", out_empty):
            cli_empty._handle_line("/list")
        self.assertIn("[*] No one else is online", out_empty.getvalue())

    def test_run_plain_and_quit(self) -> None:
        cli, _, _, _, _, _, stop = make_cli()
        in_buf = io.StringIO("/quit\n")
        out = io.StringIO()
        with patch("sys.stdin", in_buf), patch("sys.stdout", out):
            cli.run()
        val = out.getvalue()
        self.assertIn("[*] Listening on TCP port 5000", val)
        self.assertIn("[*] Broadcasting presence on UDP port 9999", val)
        self.assertIn("[*] Bye", val)
        self.assertTrue(stop.is_set())

    def test_chat_connect_and_switch(self) -> None:
        p1 = Peer("11111111", "Alice", "192.168.1.10", 5001, "online", 0.0)
        cli, _, transport, _, state, _, _ = make_cli([p1])
        state.add_message("11111111", "Alice", "hi tester")
        out = io.StringIO()
        with patch("sys.stdout", out):
            cli._handle_line("/chat Alice")
        val = out.getvalue()
        self.assertIn("[*] Connected to Alice (192.168.1.10:5001)", val)
        self.assertIn("[*] 1 unread:\n[Alice] hi tester", val)
        self.assertEqual(state.current, "11111111")
        self.assertTrue(transport.is_connected("11111111"))

        out2 = io.StringIO()
        with patch("sys.stdout", out2):
            cli._handle_line("/chat 1")
        self.assertIn("[*] Switched to Alice", out2.getvalue())

    def test_send_plain_message(self) -> None:
        p1 = Peer("11111111", "Alice", "192.168.1.10", 5001, "online", 0.0)
        cli, _, transport, _, state, _, _ = make_cli([p1])
        state.current = "11111111"
        transport.connected.add("11111111")
        out = io.StringIO()
        with patch("sys.stdout", out):
            cli._handle_line("Hello Alice")
        self.assertEqual(out.getvalue(), "[Tester] Hello Alice\n")
        self.assertEqual(transport.sent_messages, [("11111111", "Hello Alice")])
        self.assertEqual(len(state.history("11111111")), 1)

    def test_leave_and_status(self) -> None:
        p1 = Peer("11111111", "Alice", "192.168.1.10", 5001, "online", 0.0)
        cli, _, transport, discovery, state, _, _ = make_cli([p1])
        out_no_chat = io.StringIO()
        with patch("sys.stdout", out_no_chat):
            cli._handle_line("/leave")
        self.assertIn("[*] No active chat", out_no_chat.getvalue())

        state.current = "11111111"
        transport.connected.add("11111111")
        out_leave = io.StringIO()
        with patch("sys.stdout", out_leave):
            cli._handle_line("/leave")
        self.assertIn("[*] Chat with Alice closed", out_leave.getvalue())
        self.assertIsNone(state.current)

        out_status = io.StringIO()
        with patch("sys.stdout", out_status):
            cli._handle_line("/status away")
        self.assertIn("[*] Status set to away", out_status.getvalue())
        self.assertEqual(discovery.status, "away")

        out_bad_status = io.StringIO()
        with patch("sys.stdout", out_bad_status):
            cli._handle_line("/status invalid")
        self.assertIn("[!] Usage: /status <online|away>", out_bad_status.getvalue())

    def test_file_unconfirmed_is_reported_not_done(self) -> None:
        cli, _, _, _, _, _, _ = make_cli()
        out = io.StringIO()
        with patch("sys.stdout", out):
            cli._show(
                events.Event(
                    events.FILE_UNCONFIRMED,
                    {"file_id": "f1f1f1f1", "peer_id": "00000002", "filename": "doc.pdf"},
                )
            )
        val = out.getvalue()
        self.assertIn("[!] Sent 100% of doc.pdf but the receiver did not confirm it", val)
        self.assertNotIn("Done", val)

    def test_file_transfer_progress_and_completion(self) -> None:
        cli, _, _, _, _, _, _ = make_cli()
        out = io.StringIO()
        with patch("sys.stdout", out):
            cli._show(
                events.Event(
                    events.FILE_PROGRESS,
                    {
                        "file_id": "f1f1f1f1",
                        "peer_id": "00000002",
                        "filename": "doc.pdf",
                        "direction": "send",
                        "done": 0,
                        "total": 2516582,
                    },
                )
            )
            cli._show(
                events.Event(
                    events.FILE_PROGRESS,
                    {
                        "file_id": "f1f1f1f1",
                        "peer_id": "00000002",
                        "filename": "doc.pdf",
                        "direction": "send",
                        "done": 300000,
                        "total": 2516582,
                    },
                )
            )
            cli._show(
                events.Event(
                    events.FILE_COMPLETED,
                    {"file_id": "f1f1f1f1", "peer_id": "00000002", "filename": "doc.pdf", "direction": "send"},
                )
            )
        val = out.getvalue()
        self.assertIn("[*] Sending doc.pdf (2.4 MB) ...", val)
        self.assertIn("[*] doc.pdf  10%", val)
        self.assertIn("[*] Sent doc.pdf ... 100%  Done.", val)

    def test_connection_closed_and_error(self) -> None:
        cli, _, _, _, _, _, _ = make_cli()
        out = io.StringIO()
        with patch("sys.stdout", out):
            cli._show(
                events.Event(
                    events.CONNECTION_CLOSED,
                    {"peer_id": "11111111", "name": "Alice", "reason": "bye"},
                )
            )
            cli._show(
                events.Event(
                    events.CONNECTION_CLOSED,
                    {"peer_id": "11111111", "name": "Alice", "reason": "lost"},
                )
            )
            cli._show(
                events.Event(
                    events.CONNECTION_CLOSED,
                    {"peer_id": "11111111", "name": "Alice", "reason": "protocol_error"},
                )
            )
            cli._show(
                events.Event(
                    events.CONNECTION_CLOSED,
                    {"peer_id": "11111111", "name": "Alice", "reason": "local_leave"},
                )
            )
            cli._show(events.Event(events.ERROR, {"source": "tcp", "message": "something crashed"}))
        val = out.getvalue()
        self.assertIn("[*] Alice closed the chat", val)
        self.assertIn("[*] Connection to Alice lost", val)
        self.assertIn("[*] Connection to Alice closed: protocol error", val)
        self.assertIn("[!] something crashed", val)

    def test_help_command(self) -> None:
        cli, _, _, _, _, _, _ = make_cli()
        out = io.StringIO()
        with patch("sys.stdout", out):
            cli._handle_line("/help")
        val = out.getvalue()
        self.assertIn("/list", val)
        self.assertIn("/chat <name|number>", val)
        self.assertIn("<text>", val)
        self.assertIn("/send <file>", val)
        self.assertIn("/leave", val)
        self.assertIn("/status <online|away>", val)
        self.assertIn("/help", val)
        self.assertIn("/quit", val)

    def test_run_plain_stops_immediately_on_quit(self) -> None:
        cli, _, _, _, _, _, stop = make_cli()
        in_buf = io.StringIO("/quit\nhello\n")
        out = io.StringIO()
        with patch("sys.stdin", in_buf), patch("sys.stdout", out):
            cli.run()
        val = out.getvalue()
        self.assertTrue(stop.is_set())
        self.assertNotIn("No active chat", val)
        self.assertNotIn("hello", val)

    def test_say_terminal_mode_renders_prompt_with_input_buffer(self) -> None:
        cli, _, _, _, _, _, _ = make_cli()
        in_fake = FakeTTY()
        out_fake = FakeTTY()
        with patch("sys.stdin", in_fake), patch("sys.stdout", out_fake):
            cli._input_buf = list("abc")
            cli.say("x")
        self.assertEqual(out_fake.getvalue(), "\r\033[Jx\n> abc")

    def test_say_wrapped_prompt_moves_up_before_clearing(self) -> None:
        cli, _, _, _, _, _, _ = make_cli()
        out_fake = FakeTTY()
        with patch("sys.stdin", FakeTTY()), patch("sys.stdout", out_fake):
            with patch("cli.shutil.get_terminal_size", return_value=os.terminal_size((10, 24))):
                cli._input_buf = list("a" * 15)
                cli.say("x")
        self.assertEqual(out_fake.getvalue(), "\r\033[1A\033[Jx\n> " + "a" * 15)

    def test_say_after_stop_leaves_no_prompt(self) -> None:
        cli, _, _, _, _, _, stop = make_cli()
        out_fake = FakeTTY()
        stop.set()
        with patch("sys.stdin", FakeTTY()), patch("sys.stdout", out_fake):
            cli.say("[*] Bye")
        self.assertEqual(out_fake.getvalue(), "\r\033[J[*] Bye\n")

    @unittest.skipUnless(sys.platform == "win32", "msvcrt only on Windows")
    def test_read_key_keeps_accented_a_and_next_char(self) -> None:
        cli, _, _, _, _, _, _ = make_cli()
        keys = ["\xe0", "o"]

        class FakeMsvcrt:
            @staticmethod
            def kbhit() -> bool:
                return bool(keys)

            @staticmethod
            def getwch() -> str:
                return keys.pop(0)

        with patch("cli.msvcrt", FakeMsvcrt):
            self.assertEqual(cli._read_key(), "\xe0")
            self.assertEqual(cli._read_key(), "o")

    @unittest.skipUnless(sys.platform == "win32", "msvcrt only on Windows")
    def test_read_key_swallows_arrow_keys(self) -> None:
        cli, _, _, _, _, _, _ = make_cli()
        keys = ["\xe0", "H", "\x00", "P"]

        class FakeMsvcrt:
            @staticmethod
            def kbhit() -> bool:
                return bool(keys)

            @staticmethod
            def getwch() -> str:
                return keys.pop(0)

        with patch("cli.msvcrt", FakeMsvcrt):
            self.assertIsNone(cli._read_key())
            self.assertIsNone(cli._read_key())
        self.assertEqual(keys, [])


if __name__ == "__main__":
    unittest.main()
