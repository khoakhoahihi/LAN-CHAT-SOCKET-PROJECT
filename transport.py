"""Kết nối TCP của LAN Chat: lắng nghe, bắt tay INTRO, kết nối chat (protocol.md mục 2–3).

Hợp đồng: CONTRIBUTING.md mục 2.3. Thiết kế: plan-transport.md.

Khóa:
  - `self._cond` bảo vệ bảng `self._chats`. Chỉ giữ khi đọc/sửa bảng hoặc
    put() sự kiện, không bao giờ giữ trong lúc connect/send/recv.
  - Mỗi kết nối chat có một `send_lock`; mọi lần ghi lên socket chat đi qua nó.
"""

from __future__ import annotations

import logging
import queue
import socket
import sys
import threading
import time
from typing import Callable

from events import (
    CHAT_CONNECTED, CHAT_RECEIVED, CLOSE_BYE, CLOSE_LOCAL_LEAVE, CLOSE_LOST,
    CLOSE_PROTOCOL_ERROR, CONNECTION_CLOSED, ERROR, SOURCE_TCP, Event,
)
from protocol import (
    CONNECT_TIMEOUT, DUPLICATE_WAIT, INTRO_TIMEOUT, ProtocolError,
    decode_msg, encode_msg, is_valid_name, is_valid_peer_id, make_intro,
    make_intro_err, make_intro_ok, parse_intro, parse_intro_err,
    parse_intro_ok, recv_frame, send_frame,
)

log = logging.getLogger("transport")

FileHandler = Callable[[socket.socket, str, str], None]   # (sock, peer_id, name)


class ConnectError(Exception):
    """Không mở được kết nối tới peer; message đọc được cho người dùng."""


class NotConnected(Exception):
    """Chưa có (hoặc vừa mất) kết nối chat với peer."""


# Trạng thái một ô trong bảng kết nối chat
_CONNECTING = "connecting"   # mình đang connect() tới peer
_ACCEPTING = "accepting"     # peer kết nối tới mình, đang gửi INTRO_OK
_OPEN = "open"               # đã bắt tay xong, dùng được


class _ChatConn:
    """Một kết nối chat (hoặc chỗ giữ cho kết nối đang bắt tay)."""

    __slots__ = ("sock", "peer_id", "name", "incoming", "initiator",
                 "state", "closed", "send_lock")

    def __init__(self, sock: socket.socket | None, peer_id: str, name: str,
                 incoming: bool, initiator: str, state: str) -> None:
        self.sock = sock
        self.peer_id = peer_id
        self.name = name
        self.incoming = incoming
        self.initiator = initiator   # peer_id của bên đã connect(); C3 dùng để xử lý trùng
        self.state = state
        self.closed = False
        self.send_lock = threading.Lock()


def _shutdown_close(sock: socket.socket | None) -> None:
    """shutdown() để đánh thức recv() đang chờ ở luồng khác, rồi close(); bỏ qua OSError."""
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def _describe(peer) -> str:
    """Mô tả peer cho thông báo lỗi: 'Minh (192.168.1.5:6000)'."""
    return f"{peer.name} ({peer.ip}:{peer.tcp_port})"


class Transport:
    """Quản lý listener TCP và các kết nối chat; bàn giao kết nối file cho Người 3."""

    def __init__(self, my_id: str, name: str, tcp_port: int,
                 events: queue.Queue, on_file_connection: FileHandler) -> None:
        if not is_valid_peer_id(my_id):
            raise ValueError(f"peer_id không hợp lệ: {my_id!r}")
        if not is_valid_name(name):
            raise ValueError(f"tên không hợp lệ: {name!r}")
        self._my_id = my_id
        self._name = name
        self._tcp_port = tcp_port
        self._events = events
        self._on_file_connection = on_file_connection

        self._listener: socket.socket | None = None
        self._port: int | None = None
        self._acceptor: threading.Thread | None = None
        self._stopping = threading.Event()
        self._cond = threading.Condition()
        self._chats: dict[str, _ChatConn] = {}

    # ------------------------------------------------------------------ vòng đời

    @property
    def port(self) -> int | None:
        """Cổng TCP đang lắng nghe (hữu ích khi tcp_port=0); None nếu chưa start."""
        return self._port

    def start(self) -> None:
        """Bind cổng TCP và chạy luồng accept; cổng bận thì raise OSError."""
        if self._listener is not None:
            raise RuntimeError("Transport đã được start")
        if self._stopping.is_set():
            raise RuntimeError("Transport đã stop, hãy tạo đối tượng mới")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if sys.platform == "win32":
                # Trên Windows, SO_REUSEADDR cho hai process bind cùng một cổng.
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                # Cho chạy lại ngay sau khi thoát, không phải chờ TIME_WAIT.
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("", self._tcp_port))
            sock.listen()
        except OSError:
            sock.close()
            raise
        self._listener = sock
        self._port = sock.getsockname()[1]
        self._acceptor = threading.Thread(target=self._accept_loop, daemon=True,
                                          name=f"tcp-accept-{self._port}")
        self._acceptor.start()
        log.info("lắng nghe TCP ở cổng %d", self._port)

    def stop(self) -> None:
        """Gửi BYE cho các kết nối chat, đóng tất cả, dừng luồng accept; gọi nhiều lần không lỗi."""
        with self._cond:
            if self._stopping.is_set():
                return
            self._stopping.set()
            conns = list(self._chats.values())
            self._cond.notify_all()   # đánh thức connect_chat đang chờ

        listener = self._listener
        if listener is not None:
            # Trên Linux, close() không làm accept() đang chờ ở luồng khác thoát ra,
            # nên tự kết nối vào để đánh thức nó trước khi đóng.
            try:
                with socket.create_connection(("127.0.0.1", self._port),
                                              timeout=CONNECT_TIMEOUT):
                    pass
            except OSError:
                pass
            _shutdown_close(listener)

        for conn in conns:
            if conn.state == _OPEN and conn.send_lock.acquire(blocking=False):
                # Không chờ khóa: nếu một luồng đang kẹt trong sendall thì bỏ qua BYE.
                try:
                    send_frame(conn.sock, "BYE", b"")
                except OSError:
                    pass
                finally:
                    conn.send_lock.release()
            self._close(conn, None)   # đang thoát: không phát sự kiện

        if self._acceptor is not None and self._acceptor is not threading.current_thread():
            self._acceptor.join(CONNECT_TIMEOUT)
        log.info("đã dừng transport")

    # ------------------------------------------------------------------ API chat

    def connect_chat(self, peer) -> None:
        """Mở kết nối chat tới peer (đã có thì dùng lại); lỗi thì raise ConnectError."""
        pid = peer.peer_id
        if pid == self._my_id:
            raise ConnectError("Không thể chat với chính mình")
        deadline = time.monotonic() + CONNECT_TIMEOUT + INTRO_TIMEOUT + DUPLICATE_WAIT

        with self._cond:
            while True:
                if self._stopping.is_set():
                    raise ConnectError("Chương trình đang thoát")
                existing = self._chats.get(pid)
                if existing is None:
                    break
                if existing.state == _OPEN:
                    return
                # Một luồng khác đang bắt tay với peer này: chờ nó xong rồi xét lại.
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ConnectError(f"Hết thời gian chờ kết nối tới {_describe(peer)}")
                self._cond.wait(remaining)
            conn = _ChatConn(None, pid, peer.name, incoming=False,
                             initiator=self._my_id, state=_CONNECTING)
            self._chats[pid] = conn

        try:
            sock = socket.create_connection((peer.ip, peer.tcp_port), timeout=CONNECT_TIMEOUT)
        except OSError as exc:
            self._close(conn, None)
            raise ConnectError(f"Không kết nối được tới {_describe(peer)}: {exc}") from None

        with self._cond:
            if conn.closed:          # stop() chạy trong lúc đang connect()
                _shutdown_close(sock)
                raise ConnectError("Chương trình đang thoát")
            conn.sock = sock

        duplicate = False
        try:
            sock.settimeout(INTRO_TIMEOUT)
            with conn.send_lock:
                send_frame(sock, "INTRO", make_intro("CHAT", self._my_id, self._name))
            ftype, payload = recv_frame(sock)
            if ftype == "INTRO_OK":
                rid, rname = parse_intro_ok(payload)
                if rid != pid:
                    raise ConnectError(
                        f"{peer.ip}:{peer.tcp_port} không còn là {peer.name} "
                        f"(đang là {rname}, id {rid})")
                conn.name = rname
                sock.settimeout(None)
            elif ftype == "INTRO_ERR":
                code, message = parse_intro_err(payload)
                if code != "DUPLICATE":
                    raise ConnectError(f"{_describe(peer)} từ chối kết nối: {message or code}")
                duplicate = True
            else:
                raise ProtocolError(f"nhận {ftype} thay vì INTRO_OK")
        except ConnectError:
            self._close(conn, None)
            raise
        except ProtocolError as exc:
            self._close(conn, None)
            raise ConnectError(f"{_describe(peer)} trả lời sai giao thức: {exc}") from None
        except TimeoutError:
            self._close(conn, None)
            raise ConnectError(f"{_describe(peer)} không trả lời (hết thời gian chờ)") from None
        except OSError as exc:
            self._close(conn, None)
            raise ConnectError(f"Mất kết nối với {_describe(peer)} khi bắt tay: {exc}") from None

        if duplicate:
            # Bên kia đang giữ một kết nối chat khác với mình: dùng kết nối đó (protocol.md 3.2).
            self._close(conn, None)
            with self._cond:
                ok = self._cond.wait_for(lambda: self._is_open_locked(pid), DUPLICATE_WAIT)
            if not ok:
                raise ConnectError(f"{_describe(peer)} báo trùng kết nối nhưng không có kết nối nào")
            return

        if not self._open(conn):
            raise ConnectError("Chương trình đang thoát")
        self._start_reader(conn)

    def send_chat(self, peer_id: str, text: str) -> None:
        """Gửi một tin MSG; chưa kết nối thì NotConnected, text sai thì ValueError."""
        payload = encode_msg(text)
        conn = self._get_open(peer_id)
        if conn is None:
            raise NotConnected(f"Chưa có kết nối chat với {peer_id}")
        try:
            with conn.send_lock:
                send_frame(conn.sock, "MSG", payload)
        except OSError:
            self._close(conn, CLOSE_LOST)
            raise NotConnected(f"Mất kết nối với {conn.name}") from None

    def disconnect_chat(self, peer_id: str) -> None:
        """Gửi BYE rồi đóng kết nối chat; không có kết nối thì bỏ qua."""
        conn = self._get_open(peer_id)
        if conn is None:
            return
        with conn.send_lock:
            try:
                send_frame(conn.sock, "BYE", b"")
            except OSError:
                pass
        self._close(conn, CLOSE_LOCAL_LEAVE)

    def is_connected(self, peer_id: str) -> bool:
        """Có kết nối chat đang mở với peer_id hay không."""
        with self._cond:
            return self._is_open_locked(peer_id)

    def open_file_connection(self, peer) -> socket.socket:
        """Mở kết nối FILE đã bắt tay xong và trả socket cho người gọi (PR C2)."""
        raise NotImplementedError("open_file_connection sẽ có ở PR C2")

    # ------------------------------------------------------------------ luồng accept

    def _accept_loop(self) -> None:
        listener = self._listener
        while not self._stopping.is_set():
            try:
                sock, addr = listener.accept()
            except OSError as exc:
                if self._stopping.is_set():
                    break
                log.warning("accept() lỗi: %s", exc)
                self._emit(ERROR, source=SOURCE_TCP, message=f"Lỗi nhận kết nối TCP: {exc}")
                continue
            if self._stopping.is_set():
                _shutdown_close(sock)
                break
            threading.Thread(target=self._handle_incoming, args=(sock, addr), daemon=True,
                             name=f"tcp-handshake-{addr[0]}:{addr[1]}").start()

    def _handle_incoming(self, sock: socket.socket, addr) -> None:
        """Bắt tay một kết nối đến: chờ INTRO, trả INTRO_OK hoặc INTRO_ERR."""
        try:
            sock.settimeout(INTRO_TIMEOUT)
            ftype, payload = recv_frame(sock)
            if ftype != "INTRO":
                raise ProtocolError(f"frame đầu tiên là {ftype}, không phải INTRO")
            kind, pid, pname = parse_intro(payload)
            if pid == self._my_id:
                raise ProtocolError("peer_id trùng với chính mình")
        except ProtocolError as exc:
            log.info("từ chối %s: %s", addr, exc)
            self._reject(sock, "BAD_INTRO", str(exc))
            return
        except OSError as exc:   # gồm cả ConnectionClosed và TimeoutError
            log.info("bắt tay với %s thất bại: %s", addr, exc)
            _shutdown_close(sock)
            return

        if self._stopping.is_set():
            self._reject(sock, "SHUTTING_DOWN", "bên nhận đang thoát")
            return
        if kind == "FILE":
            self._reject(sock, "BAD_INTRO", "chưa hỗ trợ kết nối file")   # PR C2
            return
        self._accept_chat(sock, pid, pname)

    def _accept_chat(self, sock: socket.socket, pid: str, pname: str) -> None:
        conn = _ChatConn(sock, pid, pname, incoming=True, initiator=pid, state=_ACCEPTING)
        with self._cond:
            if self._stopping.is_set():
                code = "SHUTTING_DOWN"
            elif pid in self._chats:
                code = "DUPLICATE"   # PR C3 thay bằng bảng quyết định của protocol.md 3.2
            else:
                self._chats[pid] = conn
                code = None
        if code is not None:
            self._reject(sock, code, "đã có kết nối chat" if code == "DUPLICATE" else "bên nhận đang thoát")
            return

        try:
            with conn.send_lock:
                send_frame(sock, "INTRO_OK", make_intro_ok(self._my_id, self._name))
            sock.settimeout(None)
        except OSError as exc:
            log.info("gửi INTRO_OK cho %s thất bại: %s", pid, exc)
            self._close(conn, None)
            return
        if self._open(conn):
            self._start_reader(conn)

    def _reject(self, sock: socket.socket, code: str, message: str) -> None:
        """Gửi INTRO_ERR (bỏ qua lỗi gửi) rồi đóng."""
        try:
            send_frame(sock, "INTRO_ERR", make_intro_err(code, message))
        except OSError:
            pass
        _shutdown_close(sock)

    # ------------------------------------------------------------------ luồng đọc

    def _start_reader(self, conn: _ChatConn) -> None:
        threading.Thread(target=self._read_loop, args=(conn,), daemon=True,
                         name=f"tcp-chat-{conn.peer_id}").start()

    def _read_loop(self, conn: _ChatConn) -> None:
        """Đọc frame trên kết nối chat cho tới khi BYE, mất kết nối hoặc lỗi giao thức."""
        try:
            while True:
                ftype, payload = recv_frame(conn.sock)
                if ftype == "MSG":
                    text = decode_msg(payload)
                    self._emit(CHAT_RECEIVED, peer_id=conn.peer_id, name=conn.name, text=text)
                elif ftype == "BYE":
                    if payload:
                        raise ProtocolError("BYE phải có payload rỗng")
                    reason = CLOSE_BYE
                    break
                else:
                    raise ProtocolError(f"frame {ftype} không hợp lệ trên kết nối chat")
        except ProtocolError as exc:
            log.info("lỗi giao thức với %s: %s", conn.peer_id, exc)
            reason = CLOSE_PROTOCOL_ERROR
        except OSError as exc:   # gồm ConnectionClosed; nếu do mình đóng thì _close bỏ qua
            log.debug("kết nối với %s kết thúc: %s", conn.peer_id, exc)
            reason = CLOSE_LOST
        self._close(conn, reason)

    # ------------------------------------------------------------------ bảng kết nối

    def _is_open_locked(self, peer_id: str) -> bool:
        conn = self._chats.get(peer_id)
        return conn is not None and conn.state == _OPEN

    def _get_open(self, peer_id: str) -> _ChatConn | None:
        with self._cond:
            conn = self._chats.get(peer_id)
            return conn if conn is not None and conn.state == _OPEN else None

    def _open(self, conn: _ChatConn) -> bool:
        """Chuyển ô sang 'open' và phát CHAT_CONNECTED; False nếu ô đã bị đóng/thay."""
        with self._cond:
            if conn.closed or self._chats.get(conn.peer_id) is not conn:
                stale = True
            else:
                stale = False
                conn.state = _OPEN
                # put() trong khóa để CHAT_CONNECTED luôn đứng trước CONNECTION_CLOSED.
                self._emit(CHAT_CONNECTED, peer_id=conn.peer_id, name=conn.name,
                           incoming=conn.incoming)
                self._cond.notify_all()
        if stale:
            _shutdown_close(conn.sock)
            return False
        return True

    def _close(self, conn: _ChatConn, reason: str | None) -> bool:
        """Đóng kết nối đúng một lần; phát CONNECTION_CLOSED nếu reason khác None và ô đã open."""
        with self._cond:
            if conn.closed:
                return False
            conn.closed = True
            if self._chats.get(conn.peer_id) is conn:
                del self._chats[conn.peer_id]
            if reason is not None and conn.state == _OPEN:
                self._emit(CONNECTION_CLOSED, peer_id=conn.peer_id, name=conn.name,
                           reason=reason)
            self._cond.notify_all()
        _shutdown_close(conn.sock)
        return True

    def _emit(self, etype: str, **data) -> None:
        self._events.put(Event(etype, data))
