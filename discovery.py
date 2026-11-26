"""UDP discovery theo docs/protocol.md và API trong CONTRIBUTING.md.

main.py tạo PeerStore, queue và my_id dùng chung với Transport. Tất cả thao
tác gửi UDP thuộc broadcaster để không có HELLO mới sau GOODBYE.
"""

from __future__ import annotations

import logging
import queue
import re
import socket
import threading
import time
from typing import Any

from events import Event
from peer_store import Peer, PeerStore
from protocol import (
    GOODBYE_GAP,
    GOODBYE_REPEAT,
    HELLO_INTERVAL,
    MAX_DATAGRAM,
    PEER_TIMEOUT,
    VALID_STATUSES,
    is_valid_name,
    is_valid_peer_id,
)


def parse_datagram(data: bytes) -> dict[str, Any] | None:
    """Giải mã một HELLO/GOODBYE hợp lệ, sai thì trả None."""
    # Độ dài datagram tính bằng byte; gói sai bị bỏ qua im lặng.
    if not isinstance(data, bytes) or len(data) > MAX_DATAGRAM:
        return None
    try:
        # Không strip(): dấu cách hoặc xuống dòng thừa phải bị kiểm tra.
        fields = data.decode("utf-8").split("|")
    except UnicodeDecodeError:
        return None
    if fields[0] == "HELLO" and len(fields) == 5:
        _, peer_id, name, port_text, status = fields
        # Dùng chung quy tắc với transport và CLI qua protocol.py.
        if not is_valid_peer_id(peer_id) or not is_valid_name(name):
            return None
        # Port là số thập phân ASCII, không dấu và không có số 0 đầu.
        if re.fullmatch(r"[1-9][0-9]{0,4}", port_text) is None:
            return None
        tcp_port = int(port_text)
        if tcp_port > 65535 or status not in VALID_STATUSES:
            return None
        return {
            "type": "HELLO", "peer_id": peer_id, "name": name,
            "tcp_port": tcp_port, "status": status,
        }
    if fields[0] == "GOODBYE" and len(fields) == 2:
        if is_valid_peer_id(fields[1]):
            return {"type": "GOODBYE", "peer_id": fields[1]}
    return None


def build_hello(peer_id: str, name: str, tcp_port: int, status: str) -> bytes:
    """Tạo HELLO UTF-8; cấu hình không hợp lệ gây ValueError."""
    # Dữ liệu do chương trình mình cung cấp sai thì báo lỗi để nơi gọi sửa.
    if not is_valid_peer_id(peer_id):
        raise ValueError("peer_id không hợp lệ")
    if not is_valid_name(name):
        raise ValueError("Tên không hợp lệ")
    if type(tcp_port) is not int or not 1 <= tcp_port <= 65535:
        raise ValueError("TCP port không hợp lệ")
    if not isinstance(status, str) or status not in VALID_STATUSES:
        raise ValueError("Trạng thái phải là online hoặc away")
    try:
        # Socket gửi bytes; không thêm ký tự xuống dòng ở cuối thông điệp.
        data = f"HELLO|{peer_id}|{name}|{tcp_port}|{status}".encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("Tên không mã hóa được bằng UTF-8") from error
    if len(data) > MAX_DATAGRAM:
        raise ValueError("Datagram vượt giới hạn giao thức")
    return data


def build_goodbye(peer_id: str) -> bytes:
    """Tạo GOODBYE cho một ID hợp lệ."""
    # GOODBYE chỉ cần ID, không cần tên, trạng thái hay cổng TCP.
    if not is_valid_peer_id(peer_id):
        raise ValueError("peer_id không hợp lệ")
    return f"GOODBYE|{peer_id}".encode("utf-8")


class Discovery:
    """Phát hiện peer bằng UDP, dùng kho và queue do main.py cấp."""

    def __init__(
        self, my_id: str, name: str, tcp_port: int,
        store: PeerStore, events: queue.Queue[Event],
        udp_port: int, broadcast_addr: str,
    ) -> None:
        """Kiểm tra cấu hình và chuẩn bị trạng thái, chưa mở socket."""
        # my_id phải giống ID mà main.py truyền cho Transport.
        # Gọi builder để kiểm tra ID, tên và TCP port ngay khi khởi tạo.
        build_hello(my_id, name, tcp_port, "online")
        if type(udp_port) is not int or not 1 <= udp_port <= 65535:
            raise ValueError("UDP port không hợp lệ")
        if not isinstance(broadcast_addr, str) or not broadcast_addr.strip():
            raise ValueError("Địa chỉ broadcast không hợp lệ")
        self.my_id = my_id
        self.name = name
        self.tcp_port = tcp_port
        # Dùng kho và queue chung; không tạo bản riêng bên trong discovery.
        self.store = store
        self.events = events
        self.udp_port = udp_port
        self.broadcast_addr = broadcast_addr
        self._status = "online"
        # Khóa trạng thái chỉ bảo vệ các thao tác đọc/ghi trong bộ nhớ.
        self._state_lock = threading.Lock()
        # Giữ thứ tự cập nhật kho và phát sự kiện giữa listener và cleaner.
        self._peer_event_lock = threading.Lock()
        # Ba worker cùng kiểm tra Event này để biết khi nào phải dừng.
        self._stop_event = threading.Event()
        # HELLO đổi trạng thái vào queue này; None là tín hiệu đánh thức dừng.
        self._outbox: queue.Queue[bytes | None] = queue.Queue()
        self._socket: socket.socket | None = None
        self._threads: list[threading.Thread] = []
        self._sender: threading.Thread | None = None
        # _started ngăn start lại; _closing ngăn nhận lệnh đổi trạng thái.
        self._started = False
        self._closing = False

    def _create_socket(self) -> socket.socket:
        """Tạo UDP socket hỗ trợ broadcast và nhận trên các giao diện IPv4."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # Đặt tùy chọn chia sẻ cổng trước bind để thử nhiều instance.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError as error:
                    logging.debug("Không bật được SO_REUSEPORT: %s", error)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            # Chuỗi rỗng là mọi giao diện IPv4, không chỉ localhost.
            sock.bind(("", self.udp_port))
            # Listener định kỳ thoát chờ để kiểm tra tín hiệu dừng.
            sock.settimeout(0.5)
            return sock
        except OSError:
            # Không để rò socket nếu cấu hình hoặc bind thất bại.
            sock.close()
            raise

    def _report_error(self, message: str) -> None:
        """Đưa lỗi hoạt động UDP vào queue để CLI hiển thị."""
        # Không print ở luồng mạng; chỉ CLI đọc queue sự kiện chung.
        self.events.put(Event("ERROR", {"source": "udp", "message": message}))

    def _send_datagram(self, data: bytes) -> None:
        """Gửi một datagram; lỗi socket được chuyển thành sự kiện ERROR."""
        sock = self._socket
        if sock is None:
            return
        try:
            sock.sendto(data, (self.broadcast_addr, self.udp_port))
        except OSError as error:
            self._report_error(f"Không gửi được UDP: {error}")

    def _broadcast_loop(self) -> None:
        """Gửi HELLO định kỳ, HELLO đổi trạng thái và GOODBYE khi dừng."""
        # Chỉ worker này ghi socket: không cần giữ khóa trong lúc sendto().
        # Mốc bằng thời điểm hiện tại khiến HELLO đầu tiên được gửi ngay.
        next_hello = time.monotonic()
        while not self._stop_event.is_set():
            try:
                # Có lệnh đổi trạng thái thì nhận ngay; nếu không, chờ đến
                # hạn phát định kỳ. max() tránh truyền timeout âm cho queue.
                data = self._outbox.get(
                    timeout=max(0.0, next_hello - time.monotonic())
                )
            except queue.Empty:
                # Đến hạn định kỳ: lấy trạng thái mới nhất rồi nhả khóa
                # trước khi thực hiện gửi mạng ở bên dưới.
                with self._state_lock:
                    status = self._status
                data = build_hello(
                    self.my_id, self.name, self.tcp_port, status
                )
                next_hello = time.monotonic() + HELLO_INTERVAL
            # Khi thoát, bỏ các HELLO còn chờ và chuyển sang gửi GOODBYE.
            if data is None or self._stop_event.is_set():
                break
            self._send_datagram(data)
        # Cùng một worker gửi mọi gói nên không có HELLO mới sau GOODBYE.
        data = build_goodbye(self.my_id)
        for index in range(GOODBYE_REPEAT):
            self._send_datagram(data)
            if index + 1 < GOODBYE_REPEAT:
                time.sleep(GOODBYE_GAP)

    def _upsert_peer(self, message: dict[str, Any], ip: str) -> None:
        """Thêm/cập nhật peer từ HELLO và phát sự kiện nếu cần."""
        # IP do recvfrom() cung cấp; last_seen do máy nhận tự ghi lại.
        peer = Peer(
            peer_id=message["peer_id"], name=message["name"], ip=ip,
            tcp_port=message["tcp_port"], status=message["status"],
            last_seen=time.monotonic(),
        )
        # Giữ thứ tự thay đổi kho và sự kiện; không làm I/O trong khóa.
        with self._peer_event_lock:
            old, current = self.store.upsert(peer)
            if old is None:
                self.events.put(Event("PEER_ONLINE", {"peer": current}))
            # Không so last_seen: HELLO lặp chỉ làm mới thời gian, không
            # phải một thay đổi cần thông báo cho giao diện.
            elif (old.name, old.ip, old.tcp_port, old.status) != (
                current.name, current.ip, current.tcp_port, current.status
            ):
                self.events.put(Event(
                    "PEER_UPDATED", {"peer": current, "old": old}
                ))

    def _remove_peer(self, peer_id: str) -> None:
        """Xóa peer nhận GOODBYE và thông báo offline một lần."""
        with self._peer_event_lock:
            peer = self.store.remove(peer_id)
            # GOODBYE lặp hoặc ID không tồn tại không tạo thêm sự kiện.
            if peer is not None:
                self.events.put(Event(
                    "PEER_OFFLINE", {"peer": peer, "reason": "goodbye"}
                ))

    def _handle_datagram(self, data: bytes, ip: str) -> None:
        """Kiểm tra một datagram rồi chuyển cho hàm xử lý tương ứng."""
        message = parse_datagram(data)
        # So theo ID, không theo IP: nhiều instance có thể dùng chung máy.
        if message is None or message["peer_id"] == self.my_id:
            return
        if message["type"] == "HELLO":
            self._upsert_peer(message, ip)
        else:
            self._remove_peer(message["peer_id"])

    def _listen_loop(self) -> None:
        """Chờ UDP đến và xử lý cho đến khi có tín hiệu dừng."""
        sock = self._socket
        if sock is None:
            return
        while not self._stop_event.is_set():
            try:
                # Nhận đủ datagram rồi parser mới áp giới hạn 512 byte,
                # tránh nhận một tiền tố hợp lệ của gói dài bị cắt.
                data, address = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError as error:
                if not self._stop_event.is_set():
                    self._report_error(f"Không nhận được UDP: {error}")
                self._stop_event.set()
                # Đánh thức broadcaster nếu nó đang chờ trong Queue.get().
                self._outbox.put(None)
                return
            if not self._stop_event.is_set():
                self._handle_datagram(data, address[0])

    def _expire_peers(self, now: float | None = None) -> None:
        """Xóa peer quá hạn và phát offline với lý do timeout."""
        # now có thể truyền mốc giả khi test; lúc chạy thật dùng monotonic.
        with self._peer_event_lock:
            for peer in self.store.remove_expired(PEER_TIMEOUT, now=now):
                self.events.put(Event(
                    "PEER_OFFLINE", {"peer": peer, "reason": "timeout"}
                ))

    def _cleanup_loop(self) -> None:
        """Quét peer hết hạn mỗi giây, kể cả khi không nhận thêm UDP."""
        # Mục 1.4 quy định quét mỗi giây; protocol chưa có hằng nhịp quét.
        # Event.wait() giúp kết thúc chờ sớm khi stop() bật Event.
        while not self._stop_event.wait(1.0):
            self._expire_peers()

    def set_status(self, status: str) -> None:
        """Đổi trạng thái và chuyển HELLO ngay cho broadcaster."""
        if not isinstance(status, str) or status not in VALID_STATUSES:
            raise ValueError("Trạng thái phải là online hoặc away")
        with self._state_lock:
            if not self._started or self._closing or self._stop_event.is_set():
                raise RuntimeError("Discovery không hoạt động")
            changed = status != self._status
            self._status = status
            if changed:
                # Chỉ xếp bytes vào queue trong khóa, không gửi mạng ở đây.
                # Queue giữ thứ tự khi người dùng đổi trạng thái liên tiếp.
                self._outbox.put(build_hello(
                    self.my_id, self.name, self.tcp_port, status
                ))

    def start(self) -> None:
        """Mở socket và bật worker; lỗi bind được giữ nguyên cho main.py."""
        if self._started:
            raise RuntimeError("Mỗi đối tượng chỉ được start một lần")
        self._socket = self._create_socket()
        self._started = True
        try:
            for target in (
                self._listen_loop, self._cleanup_loop, self._broadcast_loop
            ):
                worker = threading.Thread(
                    target=target, name=target.__name__, daemon=True,
                )
                if target == self._broadcast_loop:
                    # Ghi riêng worker gửi để stop() chờ GOODBYE trước.
                    self._sender = worker
                worker.start()
                self._threads.append(worker)
        except Exception:
            # Nếu chỉ bật được một phần worker thì dọn tài nguyên đã mở,
            # rồi ném lại lỗi cho main.py; không che giấu lỗi khởi động.
            self._closing = True
            self._stop_event.set()
            self._outbox.put(None)
            self._finish_shutdown()
            raise

    def _finish_shutdown(self) -> None:
        """Chờ gửi xong GOODBYE, đóng socket và thu hồi các worker."""
        # Cho broadcaster gửi GOODBYE trước khi đóng socket để ngắt recvfrom.
        sender = self._sender
        if sender is not None and sender.ident is not None:
            sender.join()
        sock = self._socket
        self._socket = None
        if sock is not None:
            # Đóng socket giúp listener đang recvfrom() thoát thao tác chờ.
            sock.close()
        for worker in self._threads:
            if worker is not sender:
                worker.join()

    def stop(self) -> None:
        """Gửi GOODBYE và dừng module; gọi nhiều lần từ main an toàn."""
        # API vòng đời được gọi từ main, không từ worker để tránh tự join.
        if self._socket is None:
            return
        with self._state_lock:
            self._closing = True
        self._stop_event.set()
        # None đánh thức broadcaster ngay, không phải đợi đủ chu kỳ 3 giây.
        self._outbox.put(None)
        self._finish_shutdown()
