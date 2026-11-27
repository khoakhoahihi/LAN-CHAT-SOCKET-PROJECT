"""Smoke test tích hợp: Discovery (UDP) + Transport (TCP) chạy cùng nhau, chưa có giao diện.

Mỗi test dựng 2 "node" như main.py sẽ dựng (PeerStore + Transport + Discovery) trên
loopback: UDP broadcast tới 127.255.255.255, TCP cổng tự chọn. Kiểm tra cả chuỗi
thấy nhau -> /chat -> nhắn tin -> /leave, cộng các tình huống lỗi trong đề (mục 5.3).

Chạy: python -m unittest tests.test_integration -v
Nếu máy không cho broadcast tới 127.255.255.255 (một số macOS, CI), các test được bỏ qua.
"""

from __future__ import annotations

import hashlib
import queue
import socket
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

import events as ev
import protocol as p
from discovery import Discovery
from peer_store import Peer, PeerStore
from transport import NotConnected, Transport

ROOT = Path(__file__).resolve().parent.parent
BROADCAST = "127.255.255.255"
WAIT = 5.0   # thời gian chờ tối đa cho một sự kiện


# --------------------------------------------------------------------------- tiện ích

def free_udp_port() -> int:
    """Một cổng UDP đang rảnh, để mỗi test dùng cổng riêng và không nghe nhầm gói của test khác."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def loopback_broadcast_works() -> bool:
    """Thử gửi một datagram tới 127.255.255.255 và xem socket khác trên máy có nhận được không."""
    port = free_udp_port()
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        rx.bind(("", port))
        rx.settimeout(1.0)
        tx.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        tx.sendto(b"ping", (BROADCAST, port))
        return rx.recv(16) == b"ping"
    except OSError:
        return False
    finally:
        rx.close()
        tx.close()


def wait_event(q: queue.Queue, etype: str, pred=None, timeout: float = WAIT) -> ev.Event:
    """Lấy sự kiện đầu tiên đúng loại (và đúng `pred` nếu có), bỏ qua các sự kiện khác."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f"không nhận được {etype} sau {timeout}s")
        try:
            e = q.get(timeout=remaining)
        except queue.Empty:
            continue
        if e.type == etype and (pred is None or pred(e)):
            return e


def drain(q: queue.Queue, within: float = 0.5) -> list[ev.Event]:
    """Lấy mọi sự kiện tới trong `within` giây."""
    out: list[ev.Event] = []
    deadline = time.monotonic() + within
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return out
        try:
            out.append(q.get(timeout=remaining))
        except queue.Empty:
            return out


def wait_until(pred, timeout: float = WAIT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


class Node:
    """Một peer đầy đủ như main.py sẽ dựng: PeerStore + Transport + (tuỳ chọn) Discovery."""

    def __init__(self, name: str, udp_port: int | None = None) -> None:
        self.name = name
        self.id = p.new_id()
        self.events: queue.Queue = queue.Queue()
        self.store = PeerStore()
        self.files: queue.Queue = queue.Queue()   # kết nối file được bàn giao: (sock, peer_id, name)
        self.transport = Transport(self.id, name, 0, self.events,
                                   lambda sock, pid, pname: self.files.put((sock, pid, pname)))
        self.transport.start()
        self.discovery: Discovery | None = None
        if udp_port is not None:
            self.discovery = Discovery(self.id, name, self.transport.port, self.store,
                                       self.events, udp_port, BROADCAST)
            self.discovery.start()

    def stop(self) -> None:
        if self.discovery is not None:
            self.discovery.stop()
        self.transport.stop()


def meet(a: Node, b: Node, timeout: float = WAIT) -> None:
    """Chờ hai node thấy nhau qua UDP rồi xoá hàng đợi sự kiện.

    HELLO đầu tiên của node khởi động trước có thể mất vì node kia chưa mở socket,
    nên cứ 0.3 s đổi trạng thái một lần: mỗi lần đổi, Discovery gửi HELLO ngay thay vì
    đợi chu kỳ 3 s. Kết thúc ở "online".
    """
    deadline = time.monotonic() + timeout
    flip = 0
    while time.monotonic() < deadline:
        if a.store.get(b.id) and b.store.get(a.id):
            break
        flip += 1
        status = "away" if flip % 2 else "online"
        a.discovery.set_status(status)
        b.discovery.set_status(status)
        time.sleep(0.3)
    else:
        raise AssertionError("hai node không thấy nhau qua UDP")
    a.discovery.set_status("online")
    b.discovery.set_status("online")
    assert wait_until(lambda: a.store.get(b.id).status == "online"
                      and b.store.get(a.id).status == "online"), "trạng thái không về online"
    drain(a.events, 0.2)
    drain(b.events, 0.2)


class IntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not loopback_broadcast_works():
            raise unittest.SkipTest("máy này không cho broadcast tới 127.255.255.255")

    def setUp(self) -> None:
        self.udp_port = free_udp_port()
        self.nodes: list[Node] = []

    def tearDown(self) -> None:
        for node in reversed(self.nodes):
            node.stop()

    def node(self, name: str, udp: bool = True) -> Node:
        n = Node(name, self.udp_port if udp else None)
        self.nodes.append(n)
        return n

    def connected_pair(self) -> tuple[Node, Node]:
        """Quan và Minh đã thấy nhau và đang có một kết nối chat (Quan mở)."""
        a, b = self.node("Quan"), self.node("Minh")
        meet(a, b)
        a.transport.connect_chat(a.store.get(b.id))
        wait_event(a.events, ev.CHAT_CONNECTED)
        wait_event(b.events, ev.CHAT_CONNECTED)
        return a, b


# --------------------------------------------------------------------------- các test

class TestDiscoveryThenChat(IntegrationTest):
    def test_discover_connect_chat_and_leave(self):
        a, b = self.node("Quan"), self.node("Minh")
        meet(a, b)

        peer_b = a.store.get(b.id)
        self.assertEqual(peer_b.name, "Minh")
        self.assertEqual(peer_b.ip, "127.0.0.1")           # IP lấy từ recvfrom()
        self.assertEqual(peer_b.tcp_port, b.transport.port)  # cổng TCP lấy từ HELLO

        a.transport.connect_chat(peer_b)
        ca = wait_event(a.events, ev.CHAT_CONNECTED)
        cb = wait_event(b.events, ev.CHAT_CONNECTED)
        self.assertFalse(ca.data["incoming"])
        self.assertTrue(cb.data["incoming"])
        self.assertEqual(cb.data["name"], "Quan")
        self.assertEqual(cb.data["peer_id"], a.id)

        a.transport.send_chat(b.id, "Xin chào Minh")
        got = wait_event(b.events, ev.CHAT_RECEIVED)
        self.assertEqual((got.data["name"], got.data["text"]), ("Quan", "Xin chào Minh"))

        b.transport.send_chat(a.id, "Chào Quan, file xong chưa?")
        got = wait_event(a.events, ev.CHAT_RECEIVED)
        self.assertEqual((got.data["name"], got.data["text"]), ("Minh", "Chào Quan, file xong chưa?"))

        a.transport.disconnect_chat(b.id)   # /leave
        self.assertEqual(wait_event(a.events, ev.CONNECTION_CLOSED).data["reason"], "local_leave")
        self.assertEqual(wait_event(b.events, ev.CONNECTION_CLOSED).data["reason"], "bye")
        self.assertFalse(a.transport.is_connected(b.id))
        self.assertFalse(b.transport.is_connected(a.id))

    def test_status_change_reaches_other_peer(self):
        a, b = self.node("Quan"), self.node("Minh")
        meet(a, b)
        a.discovery.set_status("away")   # /status away
        e = wait_event(b.events, ev.PEER_UPDATED,
                       lambda e: e.data["peer"].peer_id == a.id and e.data["peer"].status == "away")
        self.assertEqual(e.data["old"].status, "online")
        self.assertEqual(b.store.get(a.id).status, "away")


class TestUdpAndTcpAreIndependent(IntegrationTest):
    def test_peer_offline_over_udp_keeps_tcp_chat_open(self):
        """Quy tắc 7 (CONTRIBUTING 3.1): peer hết hạn UDP không tự đóng kết nối TCP."""
        a, b = self.connected_pair()
        a.discovery.stop()   # gửi GOODBYE, ngừng HELLO; transport của Quan vẫn chạy
        off = wait_event(b.events, ev.PEER_OFFLINE)
        self.assertEqual((off.data["peer"].peer_id, off.data["reason"]), (a.id, "goodbye"))
        self.assertIsNone(b.store.get(a.id))

        self.assertTrue(b.transport.is_connected(a.id))
        b.transport.send_chat(a.id, "vẫn nhận được chứ?")
        self.assertEqual(wait_event(a.events, ev.CHAT_RECEIVED).data["text"], "vẫn nhận được chứ?")


class TestKillPeer(IntegrationTest):
    CHILD = (
        "import sys, queue, time\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from transport import Transport\n"
        "t = Transport(sys.argv[1], 'Huy', 0, queue.Queue(), lambda *a: None)\n"
        "t.start()\n"
        "print(t.port, flush=True)\n"
        "time.sleep(60)\n"
    )

    def test_kill_remote_process_mid_chat(self):
        """Peer là một process thật bị kill cứng giữa cuộc chat: không BYE, không crash."""
        me = self.node("Quan", udp=False)
        child_id = p.new_id()
        proc = subprocess.Popen([sys.executable, "-c", self.CHILD, child_id],
                                stdout=subprocess.PIPE, cwd=str(ROOT), text=True)
        try:
            port = int(proc.stdout.readline())
            peer = Peer(child_id, "Huy", "127.0.0.1", port, "online", time.monotonic())
            me.transport.connect_chat(peer)
            wait_event(me.events, ev.CHAT_CONNECTED)
            me.transport.send_chat(child_id, "đang chat thì bạn bị kill")

            proc.kill()
            proc.wait(timeout=5)

            closed = wait_event(me.events, ev.CONNECTION_CLOSED)
            self.assertEqual((closed.data["peer_id"], closed.data["reason"]), (child_id, "lost"))
            with self.assertRaises(NotConnected):
                me.transport.send_chat(child_id, "còn ai không?")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            proc.stdout.close()


class TestFileAlongsideChat(IntegrationTest):
    def test_chat_keeps_flowing_while_file_transfers_on_separate_connection(self):
        a, b = self.connected_pair()

        data = bytes(range(256)) * 512 * 32   # 4 MiB
        digest = hashlib.sha256(data).hexdigest()
        meta = f"3fa9c2d1|{len(data)}|bao cao.pdf".encode()

        sock_a = a.transport.open_file_connection(a.store.get(b.id))
        sock_b, from_id, from_name = b.files.get(timeout=WAIT)
        self.assertEqual((from_id, from_name), (a.id, "Quan"))

        result: dict[str, object] = {}

        def receiver() -> None:
            ftype, payload = p.recv_frame(sock_b)
            _fid, size, _name = payload.decode().split("|", 2)
            result["type"] = ftype
            result["data"] = p.recv_exact(sock_b, int(size))
            result["done_at"] = time.monotonic()
            p.send_frame(sock_b, "FILE_DONE",
                         f"3fa9c2d1|{hashlib.sha256(result['data']).hexdigest()}".encode())

        def sender() -> None:
            p.send_frame(sock_a, "FILE_META", meta)
            for i in range(0, len(data), p.FILE_CHUNK):
                sock_a.sendall(data[i:i + p.FILE_CHUNK])
                time.sleep(0.01)   # kéo dài ~0,6 s để chat chắc chắn chạy xen vào
            ftype, payload = p.recv_frame(sock_a)
            result["reply"] = (ftype, payload.decode())

        threads = [threading.Thread(target=receiver), threading.Thread(target=sender)]
        for t in threads:
            t.start()

        # Trong lúc file đang chạy, hai bên nhắn qua lại 20 tin trên kết nối chat.
        fifth_received_at = 0.0
        for i in range(20):
            sender_node, receiver_node = (a, b) if i % 2 == 0 else (b, a)
            sender_node.transport.send_chat(receiver_node.id, f"tin {i}")
            got = wait_event(receiver_node.events, ev.CHAT_RECEIVED)
            self.assertEqual(got.data["text"], f"tin {i}")
            if i == 4:
                fifth_received_at = time.monotonic()
            time.sleep(0.01)

        for t in threads:
            t.join(timeout=15)
            self.assertFalse(t.is_alive(), "luồng truyền file bị treo")

        self.assertEqual(result["type"], "FILE_META")
        self.assertEqual(result["data"], data)
        self.assertEqual(result["reply"], ("FILE_DONE", f"3fa9c2d1|{digest}"))
        self.assertLess(fifth_received_at, result["done_at"],
                        "tin chat thứ 5 phải tới trước khi file truyền xong")
        sock_a.close()
        sock_b.close()


class TestSimultaneousConnect(IntegrationTest):
    def test_both_sides_connect_at_once_leaves_one_working_chat(self):
        a, b = self.node("Quan"), self.node("Minh")
        meet(a, b)

        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def dial(src: Node, dst: Node) -> None:
            try:
                barrier.wait(timeout=2)
                src.transport.connect_chat(src.store.get(dst.id))
            except BaseException as exc:   # noqa: BLE001 - báo lại cho luồng chính
                errors.append(exc)

        threads = [threading.Thread(target=dial, args=(a, b)),
                   threading.Thread(target=dial, args=(b, a))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        self.assertEqual(errors, [])

        self.assertTrue(wait_until(lambda: a.transport.is_connected(b.id)
                                   and b.transport.is_connected(a.id)))
        # Mỗi bên chỉ thấy một CHAT_CONNECTED và không có CONNECTION_CLOSED giả.
        for node in (a, b):
            evts = drain(node.events, 0.6)
            self.assertEqual([e.type for e in evts if e.type == ev.CHAT_CONNECTED],
                             [ev.CHAT_CONNECTED], f"{node.name}: {[e.type for e in evts]}")
            self.assertNotIn(ev.CONNECTION_CLOSED, [e.type for e in evts])

        a.transport.send_chat(b.id, "từ Quan")
        b.transport.send_chat(a.id, "từ Minh")
        self.assertEqual(wait_event(b.events, ev.CHAT_RECEIVED).data["text"], "từ Quan")
        self.assertEqual(wait_event(a.events, ev.CHAT_RECEIVED).data["text"], "từ Minh")


if __name__ == "__main__":
    unittest.main()
