"""Test cho transport.py (PR C1–C3). Chạy: python -m unittest tests.test_transport -v"""

from __future__ import annotations

import hashlib
import queue
import socket
import struct
import threading
import time
import unittest
from dataclasses import dataclass
from unittest import mock

import events as ev
import protocol as p
import transport
from transport import ConnectError, NotConnected, Transport

WAIT = 3.0   # thời gian chờ tối đa cho một sự kiện


@dataclass(frozen=True)
class FakePeer:
    """Thay cho peer_store.Peer trong lúc PR #2 chưa merge."""
    peer_id: str
    name: str
    ip: str
    tcp_port: int
    status: str = "online"
    last_seen: float = 0.0


def wait_event(q: queue.Queue, etype: str, timeout: float = WAIT) -> ev.Event:
    """Lấy sự kiện đầu tiên đúng loại (bỏ qua loại khác); quá giờ thì AssertionError."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f"không nhận được {etype} sau {timeout}s")
        try:
            e = q.get(timeout=remaining)
        except queue.Empty:
            continue
        if e.type == etype:
            return e


def drain(q: queue.Queue, within: float = 0.3) -> list[ev.Event]:
    """Lấy mọi sự kiện tới trong `within` giây."""
    out = []
    deadline = time.monotonic() + within
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return out
        try:
            out.append(q.get(timeout=remaining))
        except queue.Empty:
            return out


def types(evts) -> list[str]:
    return [e.type for e in evts]


def wait_until(pred, timeout: float = WAIT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def closed_port() -> int:
    """Một cổng (gần như chắc chắn) không có ai lắng nghe."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class RawClient:
    """Socket thường giả làm một peer, gửi frame tùy ý tới Transport."""

    def __init__(self, port: int):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=WAIT)

    def send(self, ftype: str, payload: bytes = b"") -> None:
        p.send_frame(self.sock, ftype, payload)

    def send_raw(self, data: bytes) -> None:
        self.sock.sendall(data)

    def recv(self):
        return p.recv_frame(self.sock)

    def handshake(self, peer_id: str = "11111111", name: str = "Raw", kind: str = "CHAT"):
        self.send("INTRO", p.make_intro(kind, peer_id, name))
        return self.recv()

    def is_closed_by_peer(self) -> bool:
        """True nếu bên kia đã đóng (recv trả b"")."""
        try:
            return self.sock.recv(1) == b""
        except ConnectionError:
            return True

    def close(self) -> None:
        self.sock.close()


class RawServer:
    """Listener thường; `behavior(sock)` chạy cho mỗi kết nối đến."""

    def __init__(self, behavior):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.port = self.listener.getsockname()[1]
        self.behavior = behavior
        self.conns = []
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            sock, _ = self.listener.accept()
        except OSError:
            return
        self.conns.append(sock)
        try:
            self.behavior(sock)
        except OSError:
            pass

    def close(self):
        self.listener.close()
        for c in self.conns:
            c.close()


class TransportTest(unittest.TestCase):
    """Tạo các Transport trên 127.0.0.1 với cổng do hệ điều hành chọn."""

    def make(self, peer_id: str, name: str, start: bool = True, on_file=None):
        q = queue.Queue()
        t = Transport(peer_id, name, 0, q, on_file_connection=on_file or (lambda *a: None))
        if start:
            t.start()
        self.addCleanup(t.stop)
        return t, q

    @staticmethod
    def peer(t: Transport, peer_id: str, name: str) -> FakePeer:
        return FakePeer(peer_id, name, "127.0.0.1", t.port)

    def pair(self):
        """A (aaaaaaaa) đã kết nối chat tới B (bbbbbbbb)."""
        a, qa = self.make("aaaaaaaa", "An")
        b, qb = self.make("bbbbbbbb", "Bình")
        a.connect_chat(self.peer(b, "bbbbbbbb", "Bình"))
        wait_event(qa, ev.CHAT_CONNECTED)
        wait_event(qb, ev.CHAT_CONNECTED)
        return a, qa, b, qb


# --- #1–2: vòng đời ----------------------------------------------------------
class TestLifecycle(TransportTest):
    def test_start_stop_idempotent_and_fast(self):  # 1
        t, _ = self.make("aaaaaaaa", "An")
        port = t.port
        self.assertIsInstance(port, int)
        start = time.monotonic()
        t.stop()
        t.stop()
        self.assertLess(time.monotonic() - start, 1.0)
        # Cổng được trả lại: một Transport mới bind lại được ngay.
        t2 = Transport("cccccccc", "Chi", port, queue.Queue(), lambda *a: None)
        t2.start()
        self.addCleanup(t2.stop)
        self.assertEqual(t2.port, port)

    def test_stop_before_start(self):  # 1
        t, _ = self.make("aaaaaaaa", "An", start=False)
        t.stop()

    def test_port_in_use(self):  # 2
        t, _ = self.make("aaaaaaaa", "An")
        t2 = Transport("bbbbbbbb", "Bình", t.port, queue.Queue(), lambda *a: None)
        with self.assertRaises(OSError):
            t2.start()

    def test_bad_constructor_args(self):
        with self.assertRaises(ValueError):
            Transport("XYZ", "An", 0, queue.Queue(), lambda *a: None)
        with self.assertRaises(ValueError):
            Transport("aaaaaaaa", "a b", 0, queue.Queue(), lambda *a: None)


# --- #3–7: chat giữa hai Transport ------------------------------------------
class TestChat(TransportTest):
    def test_connect_events(self):  # 3
        a, qa = self.make("aaaaaaaa", "An")
        b, qb = self.make("bbbbbbbb", "Bình")
        a.connect_chat(self.peer(b, "bbbbbbbb", "Bình"))
        ea = wait_event(qa, ev.CHAT_CONNECTED)
        eb = wait_event(qb, ev.CHAT_CONNECTED)
        self.assertEqual(ea.data, {"peer_id": "bbbbbbbb", "name": "Bình", "incoming": False})
        self.assertEqual(eb.data, {"peer_id": "aaaaaaaa", "name": "An", "incoming": True})
        self.assertTrue(a.is_connected("bbbbbbbb"))
        self.assertTrue(wait_until(lambda: b.is_connected("aaaaaaaa")))

    def test_name_comes_from_intro_ok(self):
        a, qa = self.make("aaaaaaaa", "An")
        b, _ = self.make("bbbbbbbb", "Bình")
        a.connect_chat(self.peer(b, "bbbbbbbb", "TênCũ"))
        self.assertEqual(wait_event(qa, ev.CHAT_CONNECTED).data["name"], "Bình")

    def test_reuse_connection(self):  # 4
        a, qa, b, qb = self.pair()
        a.connect_chat(self.peer(b, "bbbbbbbb", "Bình"))
        self.assertNotIn(ev.CHAT_CONNECTED, types(drain(qa) + drain(qb)))

    def test_messages_both_directions(self):  # 5
        a, qa, b, qb = self.pair()
        texts = [f"Tin số {i}: Xin chào, đã nhận file chưa? ăâđêôơư" for i in range(200)]
        for t in texts:
            a.send_chat("bbbbbbbb", t)
            b.send_chat("aaaaaaaa", t.upper())
        got_b = [wait_event(qb, ev.CHAT_RECEIVED) for _ in texts]
        got_a = [wait_event(qa, ev.CHAT_RECEIVED) for _ in texts]
        self.assertEqual([e.data["text"] for e in got_b], texts)
        self.assertEqual([e.data["text"] for e in got_a], [t.upper() for t in texts])
        self.assertEqual(got_b[0].data["peer_id"], "aaaaaaaa")
        self.assertEqual(got_b[0].data["name"], "An")

    def test_concurrent_senders(self):  # 6
        a, qa, b, qb = self.pair()
        n = 500
        big = "x" * 30_000

        def sender(tag):
            for i in range(n):
                a.send_chat("bbbbbbbb", f"{tag}{i}|{big if i % 50 == 0 else ''}")

        threads = [threading.Thread(target=sender, args=(tag,)) for tag in "XY"]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        got = [wait_event(qb, ev.CHAT_RECEIVED).data["text"] for _ in range(2 * n)]
        for tag in "XY":
            mine = [s.split("|", 1)[0] for s in got if s.startswith(tag)]
            self.assertEqual(mine, [f"{tag}{i}" for i in range(n)])
        self.assertNotIn(ev.CONNECTION_CLOSED, types(drain(qb)))

    def test_disconnect(self):  # 7
        a, qa, b, qb = self.pair()
        a.disconnect_chat("bbbbbbbb")
        self.assertEqual(wait_event(qa, ev.CONNECTION_CLOSED).data["reason"], ev.CLOSE_LOCAL_LEAVE)
        self.assertEqual(wait_event(qb, ev.CONNECTION_CLOSED).data["reason"], ev.CLOSE_BYE)
        self.assertFalse(a.is_connected("bbbbbbbb"))
        self.assertTrue(wait_until(lambda: not b.is_connected("aaaaaaaa")))
        a.disconnect_chat("bbbbbbbb")   # không còn kết nối: bỏ qua
        self.assertEqual(types(drain(qa) + drain(qb)), [])
        # Kết nối lại được sau khi /leave
        a.connect_chat(self.peer(b, "bbbbbbbb", "Bình"))
        wait_event(qb, ev.CHAT_CONNECTED)

    def test_connect_to_self(self):
        a, _ = self.make("aaaaaaaa", "An")
        with self.assertRaises(ConnectError):
            a.connect_chat(self.peer(a, "aaaaaaaa", "An"))


# --- #8–11: phía nhận gặp peer xấu ------------------------------------------
class TestIncoming(TransportTest):
    def setUp(self):
        self.t, self.q = self.make("bbbbbbbb", "Bình")

    def raw(self) -> RawClient:
        c = RawClient(self.t.port)
        self.addCleanup(c.close)
        return c

    def test_handshake_reply(self):
        c = self.raw()
        self.assertEqual(c.handshake("11111111", "Raw"), ("INTRO_OK", b"bbbbbbbb|B\xc3\xacnh"))
        e = wait_event(self.q, ev.CHAT_CONNECTED)
        self.assertEqual(e.data, {"peer_id": "11111111", "name": "Raw", "incoming": True})

    def test_abrupt_close_is_lost(self):  # 8
        c = self.raw()
        c.handshake()
        wait_event(self.q, ev.CHAT_CONNECTED)
        c.close()
        e = wait_event(self.q, ev.CONNECTION_CLOSED)
        self.assertEqual(e.data["reason"], ev.CLOSE_LOST)
        self.assertFalse(self.t.is_connected("11111111"))

    def test_protocol_errors_after_handshake(self):  # 9
        cases = [
            lambda c: c.send_raw(b"GARBAGE_GARBAGE_GARBAGE_GARBAGE_GARBAGE"),
            lambda c: c.send("MSG", b"a\nb"),
            lambda c: c.send("MSG", b"\xff\xfe"),
            lambda c: c.send("INTRO", p.make_intro("CHAT", "11111111", "Raw")),
            lambda c: c.send("FILE_META", b"3fa9c2d1|1|a"),
            lambda c: c.send("BYE", b"x"),
        ]
        for i, bad in enumerate(cases):
            with self.subTest(case=i):
                c = self.raw()
                c.handshake()
                wait_event(self.q, ev.CHAT_CONNECTED)
                bad(c)
                e = wait_event(self.q, ev.CONNECTION_CLOSED)
                self.assertEqual(e.data["reason"], ev.CLOSE_PROTOCOL_ERROR)
                self.assertTrue(c.is_closed_by_peer())

    def test_msg_from_raw_client(self):
        c = self.raw()
        c.handshake()
        c.send("MSG", "Xin chào".encode())
        self.assertEqual(wait_event(self.q, ev.CHAT_RECEIVED).data["text"], "Xin chào")
        c.send("BYE")
        self.assertEqual(wait_event(self.q, ev.CONNECTION_CLOSED).data["reason"], ev.CLOSE_BYE)

    def test_bad_intro_rejected(self):  # 10
        cases = [
            lambda c: c.send("MSG", b"hi"),
            lambda c: c.send("INTRO", b"CHAT|11111111|a b"),
            lambda c: c.send("INTRO", b"CHAT|11111111"),
            lambda c: c.send("INTRO", b"VOICE|11111111|Raw"),
            lambda c: c.send("INTRO", p.make_intro("CHAT", "bbbbbbbb", "Bình")),  # id của chính nó
            lambda c: c.send_raw(b"HACK|0|"),
        ]
        for i, bad in enumerate(cases):
            with self.subTest(case=i):
                c = self.raw()
                bad(c)
                ftype, payload = c.recv()
                self.assertEqual(ftype, "INTRO_ERR")
                self.assertEqual(p.parse_intro_err(payload)[0], "BAD_INTRO")
                self.assertTrue(c.is_closed_by_peer())
        self.assertNotIn(ev.CHAT_CONNECTED, types(drain(self.q)))

    def test_intro_timeout_does_not_block_accept(self):  # 11
        with mock.patch.object(transport, "INTRO_TIMEOUT", 0.3):
            silent = self.raw()
            start = time.monotonic()
            # Trong lúc `silent` còn treo, một peer khác vẫn bắt tay được.
            other = self.raw()
            self.assertEqual(other.handshake()[0], "INTRO_OK")
            self.assertTrue(silent.is_closed_by_peer())
            self.assertLess(time.monotonic() - start, 2.0)


# --- #12: phía kết nối gặp lỗi ----------------------------------------------
class TestConnectErrors(TransportTest):
    def setUp(self):
        self.a, self.qa = self.make("aaaaaaaa", "An")

    def server(self, behavior) -> FakePeer:
        s = RawServer(behavior)
        self.addCleanup(s.close)
        return FakePeer("bbbbbbbb", "Bình", "127.0.0.1", s.port)

    def assert_clean(self):
        self.assertFalse(self.a.is_connected("bbbbbbbb"))
        self.assertNotIn(ev.CHAT_CONNECTED, types(drain(self.qa, 0.1)))

    def test_port_closed(self):
        with self.assertRaises(ConnectError):
            self.a.connect_chat(FakePeer("bbbbbbbb", "Bình", "127.0.0.1", closed_port()))
        self.assert_clean()

    def test_silent_server(self):
        stop = threading.Event()
        peer = self.server(lambda s: stop.wait(5))
        self.addCleanup(stop.set)
        with mock.patch.object(transport, "INTRO_TIMEOUT", 0.3):
            start = time.monotonic()
            with self.assertRaises(ConnectError) as cm:
                self.a.connect_chat(peer)
            self.assertLess(time.monotonic() - start, 2.0)
        self.assertIn("không trả lời", str(cm.exception))
        self.assert_clean()

    def test_wrong_peer_id(self):
        def behavior(s):
            p.recv_frame(s)
            p.send_frame(s, "INTRO_OK", p.make_intro_ok("cccccccc", "Chi"))
            time.sleep(0.5)
        with self.assertRaises(ConnectError) as cm:
            self.a.connect_chat(self.server(behavior))
        self.assertIn("không còn là Bình", str(cm.exception))
        self.assert_clean()

    def test_intro_err(self):
        def behavior(s):
            p.recv_frame(s)
            p.send_frame(s, "INTRO_ERR", p.make_intro_err("SHUTTING_DOWN", "đang thoát"))
        with self.assertRaises(ConnectError) as cm:
            self.a.connect_chat(self.server(behavior))
        self.assertIn("đang thoát", str(cm.exception))
        self.assert_clean()

    def test_garbage_reply(self):
        def behavior(s):
            p.recv_frame(s)
            s.sendall(b"NOT A FRAME AT ALL, JUST GARBAGE!!!!")
        with self.assertRaises(ConnectError):
            self.a.connect_chat(self.server(behavior))
        self.assert_clean()

    def test_closed_during_handshake(self):
        def behavior(s):
            p.recv_frame(s)
            s.close()
        with self.assertRaises(ConnectError):
            self.a.connect_chat(self.server(behavior))
        self.assert_clean()

    def test_can_retry_after_failure(self):
        with self.assertRaises(ConnectError):
            self.a.connect_chat(FakePeer("bbbbbbbb", "Bình", "127.0.0.1", closed_port()))
        b, _ = self.make("bbbbbbbb", "Bình")
        self.a.connect_chat(self.peer(b, "bbbbbbbb", "Bình"))
        self.assertTrue(self.a.is_connected("bbbbbbbb"))


# --- #13–14: send_chat -------------------------------------------------------
class TestSendChat(TransportTest):
    def test_errors(self):  # 13
        a, qa, b, qb = self.pair()
        with self.assertRaises(NotConnected):
            a.send_chat("cccccccc", "hi")
        with self.assertRaises(ValueError):
            a.send_chat("bbbbbbbb", "a\nb")
        with self.assertRaises(ValueError):
            a.send_chat("bbbbbbbb", "a" * (p.MAX_FRAME_PAYLOAD + 1))
        a.send_chat("bbbbbbbb", "vẫn chat được")
        self.assertEqual(wait_event(qb, ev.CHAT_RECEIVED).data["text"], "vẫn chat được")

    def test_after_peer_lost(self):  # 14
        a, qa, b, qb = self.pair()
        b.stop()
        e = wait_event(qa, ev.CONNECTION_CLOSED)
        self.assertEqual(e.data["reason"], ev.CLOSE_BYE)   # stop() gửi BYE trước khi đóng
        with self.assertRaises(NotConnected):
            a.send_chat("bbbbbbbb", "còn đó không?")
        self.assertNotIn(ev.CONNECTION_CLOSED, types(drain(qa)))

    def test_lost_exactly_once(self):  # 14
        a, qa = self.make("aaaaaaaa", "An")
        c = RawClient(a.port)
        c.handshake("11111111", "Raw")
        wait_event(qa, ev.CHAT_CONNECTED)
        c.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        c.close()   # SO_LINGER 0: gửi RST thay vì FIN
        self.assertEqual(wait_event(qa, ev.CONNECTION_CLOSED).data["reason"], ev.CLOSE_LOST)
        for _ in range(3):
            with self.assertRaises(NotConnected):
                a.send_chat("11111111", "hi")
        self.assertNotIn(ev.CONNECTION_CLOSED, types(drain(qa)))


# --- #15: stop khi đang có kết nối ------------------------------------------
class TestStop(TransportTest):
    def test_stop_with_open_chats(self):  # 15
        hub, qh = self.make("aaaaaaaa", "Hub")
        others = []
        for i, pid in enumerate(["bbbbbbbb", "cccccccc", "dddddddd"]):
            t, q = self.make(pid, f"Peer{i}")
            t.connect_chat(self.peer(hub, "aaaaaaaa", "Hub"))
            wait_event(q, ev.CHAT_CONNECTED)
            others.append((t, q))
        self.assertTrue(wait_until(lambda: all(hub.is_connected(t._my_id) for t, _ in others)))
        drain(qh)
        hub.stop()
        for t, q in others:
            self.assertEqual(wait_event(q, ev.CONNECTION_CLOSED).data["reason"], ev.CLOSE_BYE)
        self.assertNotIn(ev.CONNECTION_CLOSED, types(drain(qh)))
        self.assertFalse(hub.is_connected("bbbbbbbb"))
        hub_readers = {f"tcp-chat-{pid}" for pid in ("bbbbbbbb", "cccccccc", "dddddddd")}
        self.assertTrue(wait_until(
            lambda: not any(th.name in hub_readers for th in threading.enumerate())))

    def test_connect_after_stop(self):
        a, _ = self.make("aaaaaaaa", "An")
        b, _ = self.make("bbbbbbbb", "Bình")
        peer = self.peer(b, "bbbbbbbb", "Bình")
        a.stop()
        with self.assertRaises(ConnectError):
            a.connect_chat(peer)

    def test_incoming_after_stop_is_refused(self):
        a, _ = self.make("aaaaaaaa", "An")
        port = a.port
        a.stop()
        with self.assertRaises(OSError):
            c = RawClient(port)
            c.handshake()
            c.close()


# --- #16–19: kết nối file (PR C2) -------------------------------------------
class FileReceiver:
    """on_file_connection giả: ghi lại lời gọi, nhận `size` byte, trả FILE_DONE kèm SHA-256."""

    def __init__(self, size: int, gate: threading.Event | None = None):
        self.size = size
        self.gate = gate             # nếu có: chờ gate rồi mới đọc (giả lập ghi đĩa chậm)
        self.calls = []              # (peer_id, name, tên luồng, timeout của socket)
        self.done = threading.Event()
        self.error = None

    def __call__(self, sock, peer_id, name):
        self.calls.append((peer_id, name, threading.current_thread().name, sock.gettimeout()))
        try:
            if self.gate is not None:
                self.gate.wait(WAIT)
            data = p.recv_exact(sock, self.size)
            p.send_frame(sock, "FILE_DONE",
                         f"3fa9c2d1|{hashlib.sha256(data).hexdigest()}".encode())
        except Exception as exc:     # báo lỗi về luồng test
            self.error = exc
        finally:
            sock.close()
            self.done.set()


class TestFile(TransportTest):
    def send_blob(self, sock, wire: bytes, data: bytes | None = None):
        """Gửi `wire`, chờ FILE_DONE và so SHA-256 với `data` (mặc định = wire)."""
        sock.sendall(wire)
        ftype, payload = p.recv_frame(sock)
        self.assertEqual(ftype, "FILE_DONE")
        expected = wire if data is None else data
        self.assertEqual(payload.decode().split("|")[1], hashlib.sha256(expected).hexdigest())

    def test_handoff(self):  # 16
        data = bytes(range(256)) * 4096      # 1 MiB
        recv = FileReceiver(len(data))
        a, qa = self.make("aaaaaaaa", "An")
        b, qb = self.make("bbbbbbbb", "Bình", on_file=recv)
        sock = a.open_file_connection(self.peer(b, "bbbbbbbb", "Bình"))
        self.addCleanup(sock.close)
        self.assertIsNone(sock.gettimeout())
        self.send_blob(sock, data)
        self.assertTrue(recv.done.wait(WAIT))
        self.assertIsNone(recv.error)
        self.assertEqual(len(recv.calls), 1)
        peer_id, name, thread_name, timeout = recv.calls[0]
        self.assertEqual((peer_id, name), ("aaaaaaaa", "An"))
        self.assertFalse(thread_name.startswith("tcp-accept"))
        self.assertIsNone(timeout)
        # Kết nối file không phải kết nối chat
        self.assertFalse(a.is_connected("bbbbbbbb"))
        self.assertFalse(b.is_connected("aaaaaaaa"))
        self.assertEqual(types(drain(qa) + drain(qb)), [])

    def test_empty_file_and_parallel(self):  # 16
        """3 kết nối file song song (0 B, 10 B, 100 KB); mỗi luồng gửi 8 byte độ dài trước."""
        results = []
        lock = threading.Lock()

        def sized_receiver(sock, pid, name):
            try:
                size = int.from_bytes(p.recv_exact(sock, 8), "big")
                data = p.recv_exact(sock, size)
                p.send_frame(sock, "FILE_DONE",
                             f"3fa9c2d1|{hashlib.sha256(data).hexdigest()}".encode())
                with lock:
                    results.append(size)
            finally:
                sock.close()

        a, _ = self.make("aaaaaaaa", "An")
        b, _ = self.make("bbbbbbbb", "Bình", on_file=sized_receiver)
        peer = self.peer(b, "bbbbbbbb", "Bình")
        blobs = [b"", b"0123456789", bytes(range(256)) * 390]
        socks = [a.open_file_connection(peer) for _ in blobs]
        for s in socks:
            self.addCleanup(s.close)
        threads = [threading.Thread(target=self.send_blob,
                                    args=(s, len(blob).to_bytes(8, "big") + blob, blob))
                   for s, blob in zip(socks, blobs)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(WAIT)
        self.assertEqual(sorted(results), sorted(len(b) for b in blobs))

    def test_chat_not_blocked_by_file(self):  # 17
        gate = threading.Event()
        data = b"x" * 3_000_000
        recv = FileReceiver(len(data), gate=gate)
        a, qa = self.make("aaaaaaaa", "An")
        b, qb = self.make("bbbbbbbb", "Bình", on_file=recv)
        peer_b = self.peer(b, "bbbbbbbb", "Bình")
        fsock = a.open_file_connection(peer_b)
        self.addCleanup(fsock.close)
        sender = threading.Thread(target=self.send_blob, args=(fsock, data), daemon=True)
        sender.start()                       # bị kẹt vì bên nhận chưa đọc (gate đóng)
        a.connect_chat(peer_b)
        wait_event(qb, ev.CHAT_CONNECTED)
        for i in range(20):
            a.send_chat("bbbbbbbb", f"tin {i} trong lúc gửi file")
            b.send_chat("aaaaaaaa", f"trả lời {i}")
        self.assertEqual([wait_event(qb, ev.CHAT_RECEIVED).data["text"] for _ in range(20)],
                         [f"tin {i} trong lúc gửi file" for i in range(20)])
        self.assertEqual(len([wait_event(qa, ev.CHAT_RECEIVED) for _ in range(20)]), 20)
        self.assertFalse(recv.done.is_set())   # file vẫn đang dở
        gate.set()
        sender.join(WAIT * 2)
        self.assertTrue(recv.done.wait(WAIT))
        self.assertIsNone(recv.error)
        self.assertTrue(a.is_connected("bbbbbbbb"))

    def test_open_errors(self):  # 18
        a, _ = self.make("aaaaaaaa", "An")

        def server(behavior):
            srv = RawServer(behavior)
            self.addCleanup(srv.close)
            return FakePeer("bbbbbbbb", "Bình", "127.0.0.1", srv.port), srv

        def reply(ftype, payload):
            def behavior(s):
                kind = p.parse_intro(p.recv_frame(s)[1])[0]
                self.assertEqual(kind, "FILE")
                p.send_frame(s, ftype, payload)
                time.sleep(0.5)
            return behavior

        cases = [
            reply("INTRO_ERR", p.make_intro_err("SHUTTING_DOWN", "đang thoát")),
            reply("INTRO_OK", p.make_intro_ok("cccccccc", "Chi")),
            reply("INTRO_ERR", p.make_intro_err("DUPLICATE", "x")),
            reply("MSG", b"hi"),
        ]
        for i, behavior in enumerate(cases):
            with self.subTest(case=i):
                peer, srv = server(behavior)
                with self.assertRaises(ConnectError):
                    a.open_file_connection(peer)
                # Transport đã đóng socket của nó: phía server đọc thấy EOF.
                self.assertTrue(wait_until(lambda: srv.conns))
                srv.conns[0].settimeout(WAIT)
                self.assertEqual(srv.conns[0].recv(1), b"")
        with self.assertRaises(ConnectError):
            a.open_file_connection(FakePeer("bbbbbbbb", "Bình", "127.0.0.1", closed_port()))
        with self.assertRaises(ConnectError):
            a.open_file_connection(self.peer(a, "aaaaaaaa", "An"))
        with mock.patch.object(transport, "INTRO_TIMEOUT", 0.3):
            stop = threading.Event()
            self.addCleanup(stop.set)
            peer, _ = server(lambda s: stop.wait(5))
            with self.assertRaises(ConnectError):
                a.open_file_connection(peer)

    def test_stop_does_not_touch_handed_over_sockets(self):  # 19
        gate = threading.Event()
        data = b"y" * 200_000
        recv = FileReceiver(len(data), gate=gate)
        a, _ = self.make("aaaaaaaa", "An")
        b, _ = self.make("bbbbbbbb", "Bình", on_file=recv)
        fsock = a.open_file_connection(self.peer(b, "bbbbbbbb", "Bình"))
        self.addCleanup(fsock.close)
        self.assertTrue(wait_until(lambda: recv.calls))
        a.stop()
        b.stop()
        gate.set()
        self.send_blob(fsock, data)          # vẫn gửi/nhận được sau khi cả hai stop
        self.assertTrue(recv.done.wait(WAIT))
        self.assertIsNone(recv.error)

    def test_open_after_stop(self):
        a, _ = self.make("aaaaaaaa", "An")
        b, _ = self.make("bbbbbbbb", "Bình")
        peer = self.peer(b, "bbbbbbbb", "Bình")
        a.stop()
        with self.assertRaises(ConnectError):
            a.open_file_connection(peer)

    def test_callback_exception_reported(self):
        def boom(sock, pid, name):
            sock.close()
            raise RuntimeError("lỗi giả")

        a, _ = self.make("aaaaaaaa", "An")
        b, qb = self.make("bbbbbbbb", "Bình", on_file=boom)
        with self.assertLogs("transport", level="ERROR"):
            sock = a.open_file_connection(self.peer(b, "bbbbbbbb", "Bình"))
            self.addCleanup(sock.close)
            e = wait_event(qb, ev.ERROR)
        self.assertEqual(e.data["source"], ev.SOURCE_TCP)
        self.assertIn("lỗi giả", e.data["message"])
        # Transport vẫn chạy bình thường sau đó
        a.connect_chat(self.peer(b, "bbbbbbbb", "Bình"))
        wait_event(qb, ev.CHAT_CONNECTED)

    def test_raw_file_intro(self):
        recv = FileReceiver(5)
        b, _ = self.make("bbbbbbbb", "Bình", on_file=recv)
        c = RawClient(b.port)
        self.addCleanup(c.close)
        self.assertEqual(c.handshake("11111111", "Raw", kind="FILE"), ("INTRO_OK", "bbbbbbbb|Bình".encode()))
        c.send_raw(b"hello")
        self.assertEqual(c.recv()[0], "FILE_DONE")
        self.assertEqual(recv.calls[0][:2], ("11111111", "Raw"))


# --- #20–23: trùng kết nối chat (PR C3, protocol.md 3.2) ---------------------
class TestDuplicate(TransportTest):
    """Mình đang `connecting` tới một RawServer (giữ INTRO, chưa trả lời) thì một
    INTRO CHAT khác của cùng peer tới qua RawClient."""

    def held_server(self, peer_id, name):
        """RawServer nhận INTRO rồi chờ test quyết định trả lời gì."""
        state = {"intro": threading.Event(), "reply": None, "go": threading.Event(),
                 "done": threading.Event()}

        def behavior(sock):
            p.recv_frame(sock)
            state["intro"].set()
            state["go"].wait(WAIT)
            if state["reply"] is not None:
                p.send_frame(sock, *state["reply"])
            state["done"].wait(WAIT)      # giữ kết nối mở tới khi test xong

        srv = RawServer(behavior)
        self.addCleanup(srv.close)
        self.addCleanup(state["done"].set)
        return FakePeer(peer_id, name, "127.0.0.1", srv.port), srv, state

    def start_connect(self, t, peer):
        box = {}

        def run():
            try:
                t.connect_chat(peer)
                box["ok"] = True
            except Exception as exc:
                box["error"] = exc

        th = threading.Thread(target=run, daemon=True)
        th.start()
        return th, box

    def raw(self, t) -> RawClient:
        c = RawClient(t.port)
        self.addCleanup(c.close)
        return c

    def test_smaller_id_keeps_own(self):  # 20
        a, qa = self.make("aaaaaaaa", "An")             # mình nhỏ hơn
        peer, srv, st = self.held_server("bbbbbbbb", "Bình")
        th, box = self.start_connect(a, peer)
        self.assertTrue(st["intro"].wait(WAIT))
        c = self.raw(a)
        ftype, payload = c.handshake("bbbbbbbb", "Bình")
        self.assertEqual((ftype, p.parse_intro_err(payload)[0]), ("INTRO_ERR", "DUPLICATE"))
        self.assertTrue(c.is_closed_by_peer())
        st["reply"] = ("INTRO_OK", p.make_intro_ok("bbbbbbbb", "Bình"))
        st["go"].set()
        th.join(WAIT)
        self.assertEqual(box, {"ok": True})
        evts = drain(qa)
        self.assertEqual(types(evts), [ev.CHAT_CONNECTED])
        self.assertFalse(evts[0].data["incoming"])
        self.assertTrue(a.is_connected("bbbbbbbb"))

    def test_larger_id_yields(self):  # 20
        b, qb = self.make("bbbbbbbb", "Bình")           # mình lớn hơn
        peer, srv, st = self.held_server("aaaaaaaa", "An")
        th, box = self.start_connect(b, peer)
        self.assertTrue(st["intro"].wait(WAIT))
        c = self.raw(b)
        self.assertEqual(c.handshake("aaaaaaaa", "An")[0], "INTRO_OK")
        e = wait_event(qb, ev.CHAT_CONNECTED)
        self.assertTrue(e.data["incoming"])
        st["reply"] = ("INTRO_ERR", p.make_intro_err("DUPLICATE", "trùng"))
        st["go"].set()
        th.join(WAIT)
        self.assertEqual(box, {"ok": True})            # connect_chat dùng kết nối từ bên kia
        self.assertEqual(types(drain(qb)), [])         # không CHAT_CONNECTED thứ hai, không CLOSED
        c.send("MSG", "qua kết nối của An".encode())
        self.assertEqual(wait_event(qb, ev.CHAT_RECEIVED).data["text"], "qua kết nối của An")
        b.send_chat("aaaaaaaa", "trả lời")
        self.assertEqual(p.decode_msg(c.recv()[1]), "trả lời")

    def test_larger_id_yields_even_if_peer_says_ok(self):  # 20
        b, qb = self.make("bbbbbbbb", "Bình")
        peer, srv, st = self.held_server("aaaaaaaa", "An")
        th, box = self.start_connect(b, peer)
        self.assertTrue(st["intro"].wait(WAIT))
        c = self.raw(b)
        self.assertEqual(c.handshake("aaaaaaaa", "An")[0], "INTRO_OK")
        wait_event(qb, ev.CHAT_CONNECTED)
        st["reply"] = ("INTRO_OK", p.make_intro_ok("aaaaaaaa", "An"))   # bên kia làm sai quy tắc
        st["go"].set()
        th.join(WAIT)
        self.assertEqual(box, {"ok": True})
        self.assertEqual(types(drain(qb)), [])
        # Kết nối đi ra của mình đã bị bỏ: server đọc thấy EOF
        srv.conns[0].settimeout(WAIT)
        self.assertEqual(srv.conns[0].recv(1), b"")
        c.send("MSG", b"ok")
        self.assertEqual(wait_event(qb, ev.CHAT_RECEIVED).data["text"], "ok")

    def test_simultaneous_connect(self):  # 21
        a, qa = self.make("aaaaaaaa", "An")
        b, qb = self.make("bbbbbbbb", "Bình")
        peer_a, peer_b = self.peer(a, "aaaaaaaa", "An"), self.peer(b, "bbbbbbbb", "Bình")
        rounds = 50
        for r in range(rounds):
            with self.subTest(round=r):
                barrier = threading.Barrier(2)
                errors = []

                def go(t, peer):
                    barrier.wait()
                    try:
                        t.connect_chat(peer)
                    except Exception as exc:
                        errors.append(exc)

                ths = [threading.Thread(target=go, args=(a, peer_b)),
                       threading.Thread(target=go, args=(b, peer_a))]
                for th in ths:
                    th.start()
                for th in ths:
                    th.join(WAIT * 3)
                self.assertEqual(errors, [])
                self.assertTrue(wait_until(
                    lambda: a.is_connected("bbbbbbbb") and b.is_connected("aaaaaaaa")))
                ea, eb = drain(qa, 0.05), drain(qb, 0.05)
                self.assertEqual(types(ea), [ev.CHAT_CONNECTED])
                self.assertEqual(types(eb), [ev.CHAT_CONNECTED])
                # Một bên thấy kết nối đến, bên kia thấy kết nối đi: cùng một kết nối.
                self.assertNotEqual(ea[0].data["incoming"], eb[0].data["incoming"])
                a.send_chat("bbbbbbbb", f"vòng {r}")
                self.assertEqual(wait_event(qb, ev.CHAT_RECEIVED).data["text"], f"vòng {r}")
                b.send_chat("aaaaaaaa", f"đáp {r}")
                self.assertEqual(wait_event(qa, ev.CHAT_RECEIVED).data["text"], f"đáp {r}")
                a.disconnect_chat("bbbbbbbb")
                wait_event(qa, ev.CONNECTION_CLOSED)
                wait_event(qb, ev.CONNECTION_CLOSED)
                self.assertTrue(wait_until(lambda: not b.is_connected("aaaaaaaa")))

    def test_duplicate_without_other_connection(self):  # 22
        a, qa = self.make("aaaaaaaa", "An")
        peer, srv, st = self.held_server("bbbbbbbb", "Bình")
        st["reply"] = ("INTRO_ERR", p.make_intro_err("DUPLICATE", "trùng"))
        st["go"].set()
        with mock.patch.object(transport, "DUPLICATE_WAIT", 0.3):
            start = time.monotonic()
            with self.assertRaises(ConnectError) as cm:
                a.connect_chat(peer)
            elapsed = time.monotonic() - start
        self.assertGreaterEqual(elapsed, 0.25)
        self.assertLess(elapsed, 2.0)
        self.assertIn("trùng", str(cm.exception))
        self.assertFalse(a.is_connected("bbbbbbbb"))
        self.assertEqual(types(drain(qa)), [])

    def test_stop_while_waiting_for_duplicate(self):  # 22
        a, _ = self.make("aaaaaaaa", "An")
        peer, srv, st = self.held_server("bbbbbbbb", "Bình")
        st["reply"] = ("INTRO_ERR", p.make_intro_err("DUPLICATE", "trùng"))
        th, box = self.start_connect(a, peer)
        self.assertTrue(st["intro"].wait(WAIT))
        st["go"].set()
        time.sleep(0.1)                 # để connect_chat vào bước chờ DUPLICATE_WAIT
        start = time.monotonic()
        a.stop()
        th.join(WAIT)
        self.assertLess(time.monotonic() - start, 1.0)
        self.assertIsInstance(box.get("error"), ConnectError)

    def test_same_peer_reconnects(self):  # 23 (Q1)
        b, qb = self.make("bbbbbbbb", "Bình")
        old = self.raw(b)
        old.handshake("11111111", "Raw")
        wait_event(qb, ev.CHAT_CONNECTED)
        new = self.raw(b)                # kết nối cũ đã chết ở phía Raw, Raw kết nối lại
        self.assertEqual(new.handshake("11111111", "Raw")[0], "INTRO_OK")
        self.assertTrue(old.is_closed_by_peer())
        self.assertEqual(types(drain(qb)), [])      # thay êm: không CLOSED, không CONNECTED mới
        self.assertTrue(b.is_connected("11111111"))
        new.send("MSG", "vẫn chat được".encode())
        self.assertEqual(wait_event(qb, ev.CHAT_RECEIVED).data["text"], "vẫn chat được")
        b.send_chat("11111111", "nhận rồi")
        self.assertEqual(p.decode_msg(new.recv()[1]), "nhận rồi")
        new.close()
        self.assertEqual(wait_event(qb, ev.CONNECTION_CLOSED).data["reason"], ev.CLOSE_LOST)
        self.assertEqual(types(drain(qb)), [])

    def test_half_open_mine_replaced_by_smaller(self):  # 23
        b, qb = self.make("bbbbbbbb", "Bình")
        peer, srv, st = self.held_server("aaaaaaaa", "An")
        st["reply"] = ("INTRO_OK", p.make_intro_ok("aaaaaaaa", "An"))
        st["go"].set()
        b.connect_chat(peer)
        wait_event(qb, ev.CHAT_CONNECTED)
        # Kết nối do mình mở đã chết ở phía An; An (id nhỏ hơn) kết nối lại.
        c = self.raw(b)
        self.assertEqual(c.handshake("aaaaaaaa", "An")[0], "INTRO_OK")
        srv.conns[0].settimeout(WAIT)
        self.assertEqual(srv.conns[0].recv(1), b"")
        self.assertEqual(types(drain(qb)), [])
        b.send_chat("aaaaaaaa", "qua kết nối mới")
        self.assertEqual(p.decode_msg(c.recv()[1]), "qua kết nối mới")

    def test_half_open_mine_kept_if_smaller(self):  # 23 — giới hạn đã biết
        """Mình id nhỏ hơn và đang giữ kết nối do mình mở: kết nối mới của peer bị DUPLICATE.

        Đúng theo protocol.md 3.2, nhưng nếu kết nối của mình thực ra đã chết một chiều
        thì peer chỉ kết nối lại được sau khi mình phát hiện mất kết nối (khi gửi tin).
        """
        a, qa = self.make("aaaaaaaa", "An")
        peer, srv, st = self.held_server("bbbbbbbb", "Bình")
        st["reply"] = ("INTRO_OK", p.make_intro_ok("bbbbbbbb", "Bình"))
        st["go"].set()
        a.connect_chat(peer)
        wait_event(qa, ev.CHAT_CONNECTED)
        c = self.raw(a)
        ftype, payload = c.handshake("bbbbbbbb", "Bình")
        self.assertEqual((ftype, p.parse_intro_err(payload)[0]), ("INTRO_ERR", "DUPLICATE"))
        self.assertTrue(a.is_connected("bbbbbbbb"))
        self.assertEqual(types(drain(qa)), [])

    def test_replacing_open_then_failing_reports_lost(self):  # 23
        b, qb = self.make("bbbbbbbb", "Bình")
        old = self.raw(b)
        old.handshake("11111111", "Raw")
        wait_event(qb, ev.CHAT_CONNECTED)
        new = self.raw(b)
        new.handshake("11111111", "Raw")
        new.close()                      # kết nối thay thế cũng mất
        self.assertEqual(wait_event(qb, ev.CONNECTION_CLOSED).data["reason"], ev.CLOSE_LOST)
        self.assertFalse(b.is_connected("11111111"))


if __name__ == "__main__":
    unittest.main()
