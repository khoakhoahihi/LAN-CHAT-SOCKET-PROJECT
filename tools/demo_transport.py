"""Chạy 2 peer (Quan và Minh) thật trong cùng một máy để chụp Wireshark, chưa cần giao diện.

Dùng đúng Discovery + Transport của đồ án, nên các gói bắt được là gói thật:
  UDP  HELLO|<peer_id>|<tên>|<cổng TCP>|<trạng thái>  tới 127.255.255.255:9999
  TCP  INTRO / INTRO_OK, MSG, BYE trên kết nối chat
  TCP  INTRO / INTRO_OK, FILE_META, byte file, FILE_DONE trên kết nối file riêng
  UDP  GOODBYE khi thoát

Cách dùng (chạy ở thư mục gốc của repo):
  1. Mở Wireshark, bấm đúp "Adapter for loopback traffic capture" để bắt đầu bắt gói.
  2. python tools/demo_transport.py
  3. Khi script hỏi, nhấn Enter. Xem hướng dẫn lọc gói in ra ở cuối.

Tham số: --no-wait (không chờ Enter), --udp-port, --broadcast, --tcp-a, --tcp-b.
"""

from __future__ import annotations

import argparse
import hashlib
import queue
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import protocol as p  # noqa: E402
from discovery import Discovery  # noqa: E402
from peer_store import PeerStore  # noqa: E402
from transport import Transport  # noqa: E402

PRINT_LOCK = threading.Lock()


def say(text: str) -> None:
    with PRINT_LOCK:
        print(text, flush=True)


class Node:
    """Một peer: PeerStore + Transport + Discovery, giống cách main.py sẽ dựng."""

    def __init__(self, name: str, tcp_port: int, udp_port: int, broadcast: str) -> None:
        self.name = name
        self.id = p.new_id()
        self.events: queue.Queue = queue.Queue()
        self.store = PeerStore()
        self.transport = Transport(self.id, name, tcp_port, self.events, self._on_file)
        self.transport.start()
        self.discovery = Discovery(self.id, name, self.transport.port, self.store,
                                   self.events, udp_port, broadcast)
        self.discovery.start()
        say(f"[{name}] peer_id={self.id}  TCP :{self.transport.port}  UDP :{udp_port}")
        threading.Thread(target=self._print_events, daemon=True).start()

    def _print_events(self) -> None:
        """Đóng vai CLI: chỉ luồng này đọc hàng đợi sự kiện và in ra."""
        while True:
            e = self.events.get()
            d = e.data
            if e.type in ("PEER_ONLINE", "PEER_UPDATED", "PEER_OFFLINE"):
                say(f"   [{self.name}] {e.type}: {d['peer'].name} ({d['peer'].ip}:{d['peer'].tcp_port}, {d['peer'].status})")
            elif e.type == "CHAT_RECEIVED":
                say(f"   [{self.name}] nhận từ {d['name']}: {d['text']}")
            else:
                extra = {k: v for k, v in d.items() if k not in ("peer_id",)}
                say(f"   [{self.name}] {e.type}: {extra}")

    def _on_file(self, sock, peer_id: str, peer_name: str) -> None:
        """Bên nhận file: đọc FILE_META + dữ liệu rồi trả FILE_DONE kèm SHA-256 (protocol.md mục 4)."""
        _ftype, payload = p.recv_frame(sock)
        file_id, size, filename = payload.decode().split("|", 2)
        data = p.recv_exact(sock, int(size))
        digest = hashlib.sha256(data).hexdigest()
        p.send_frame(sock, "FILE_DONE", f"{file_id}|{digest}".encode())
        say(f"   [{self.name}] nhận file '{filename}' ({len(data)} byte) từ {peer_name}, sha256={digest[:12]}…")
        sock.close()

    def stop(self) -> None:
        self.discovery.stop()
        self.transport.stop()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--udp-port", type=int, default=p.UDP_PORT)
    ap.add_argument("--broadcast", default="127.255.255.255")
    ap.add_argument("--tcp-a", type=int, default=5001, help="cổng TCP của Quan")
    ap.add_argument("--tcp-b", type=int, default=5002, help="cổng TCP của Minh")
    ap.add_argument("--no-wait", action="store_true")
    args = ap.parse_args()

    if not args.no_wait:
        input("Wireshark đã bắt gói chưa (Adapter for loopback traffic capture)? Nhấn Enter để bắt đầu... ")

    say("\n1) Khởi động 2 peer, UDP HELLO broadcast mỗi 3 giây")
    a = Node("Quan", args.tcp_a, args.udp_port, args.broadcast)
    b = Node("Minh", args.tcp_b, args.udp_port, args.broadcast)

    # HELLO đầu của peer khởi động trước có thể mất vì peer kia chưa mở socket; đổi trạng thái để gửi lại ngay.
    deadline = time.monotonic() + 6
    flip = 0
    while not (a.store.get(b.id) and b.store.get(a.id)):
        if time.monotonic() > deadline:
            say("Không thấy nhau qua UDP. Kiểm tra firewall hoặc thử --broadcast 255.255.255.255")
            a.stop(); b.stop()
            return 1
        flip += 1
        a.discovery.set_status("away" if flip % 2 else "online")
        b.discovery.set_status("away" if flip % 2 else "online")
        time.sleep(0.3)
    a.discovery.set_status("online")
    b.discovery.set_status("online")
    time.sleep(1.5)

    say("\n2) Quan /chat Minh: TCP three-way handshake rồi INTRO / INTRO_OK")
    a.transport.connect_chat(a.store.get(b.id))
    time.sleep(1)

    say("\n3) Nhắn tin qua lại (MSG)")
    a.transport.send_chat(b.id, "Hi Minh")
    time.sleep(0.5)
    b.transport.send_chat(a.id, "Xin chào Quan")
    time.sleep(0.5)
    a.transport.send_chat(b.id, "Báo cáo xong chưa?")
    time.sleep(1)

    say("\n4) Quan /send: kết nối TCP riêng cho file (INTRO FILE, FILE_META, byte file, FILE_DONE)")
    content = ("LAN Chat demo - đồ án Socket Programming\n" * 5000).encode()   # ~210 KB, vài khối 64 KiB
    file_id = p.new_id()
    sock = a.transport.open_file_connection(a.store.get(b.id))
    p.send_frame(sock, "FILE_META", f"{file_id}|{len(content)}|baocao.txt".encode())
    for i in range(0, len(content), p.FILE_CHUNK):
        sock.sendall(content[i:i + p.FILE_CHUNK])
    ftype, reply = p.recv_frame(sock)
    ok = reply.decode().split("|")[1] == hashlib.sha256(content).hexdigest()
    say(f"   [Quan] nhận {ftype}, hash {'khớp' if ok else 'KHÔNG khớp'}")
    sock.close()
    time.sleep(1)

    say("\n5) Quan /leave: BYE rồi đóng kết nối chat")
    a.transport.disconnect_chat(b.id)
    time.sleep(1)

    say("\n6) Quan /quit: GOODBYE qua UDP")
    a.stop()
    time.sleep(1)
    b.stop()

    say(f"""
Xong. Bấm ô vuông đỏ trong Wireshark để dừng bắt gói, rồi gõ các filter sau vào ô display filter:

  udp.port == {args.udp_port}
      HELLO định kỳ (Len=31 khi online, Len=29 khi away) và 2 gói GOODBYE của mỗi peer khi thoát.

  tcp.flags.syn == 1 && (tcp.port == {args.tcp_a} || tcp.port == {args.tcp_b})
      Các gói SYN và SYN-ACK. Có 2 kết nối TCP: chat và file, nên có 2 lần handshake.
      Muốn thấy cả ACK thứ ba thì bỏ phần tcp.flags.syn == 1 và nhìn 3 gói đầu mỗi kết nối.

  tcp.port == {args.tcp_a} || tcp.port == {args.tcp_b}
      Chuột phải một gói -> Follow -> TCP Stream. Stream chat hiện INTRO, MSG, BYE; stream file hiện
      INTRO, FILE_META, nội dung file và FILE_DONE.
""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
