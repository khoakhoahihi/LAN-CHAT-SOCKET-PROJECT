"""Bản ghi peer bất biến và kho dữ liệu dùng chung giữa các luồng."""

from dataclasses import dataclass, replace
import threading
import time


@dataclass(frozen=True)
class Peer:
    """Thông tin một phiên người dùng được discovery xác nhận."""

    peer_id: str
    name: str
    ip: str
    tcp_port: int
    status: str
    last_seen: float


class PeerStore:
    """Lưu peer theo ID và trả các bản sao độc lập cho bên gọi."""

    def __init__(self) -> None:
        """Khởi tạo kho rỗng và khóa bảo vệ dữ liệu."""
        self._peers: dict[str, Peer] = {}
        self._lock = threading.Lock()

    def upsert(self, peer: Peer) -> tuple[Peer | None, Peer]:
        """Thêm hoặc cập nhật peer, trả bản sao trước và sau."""
        if not isinstance(peer, Peer):
            raise TypeError("peer phải là đối tượng Peer")
        with self._lock:
            old_peer = self._peers.get(peer.peer_id)
            stored_peer = replace(peer)
            self._peers[peer.peer_id] = stored_peer
            old_copy = replace(old_peer) if old_peer is not None else None
            return old_copy, replace(stored_peer)

    def get(self, peer_id: str) -> Peer | None:
        """Trả bản sao peer theo ID, hoặc None nếu không có."""
        with self._lock:
            peer = self._peers.get(peer_id)
            return replace(peer) if peer is not None else None

    def get_peers(self) -> list[Peer]:
        """Trả bản sao danh sách sắp theo (name, peer_id)."""
        with self._lock:
            peers = [replace(peer) for peer in self._peers.values()]
        return sorted(peers, key=lambda peer: (peer.name, peer.peer_id))

    def find_by_name(self, name: str) -> list[Peer]:
        """Trả mọi peer trùng tên chính xác, sắp theo peer_id."""
        with self._lock:
            peers = [
                replace(peer)
                for peer in self._peers.values()
                if peer.name == name
            ]
        return sorted(peers, key=lambda peer: peer.peer_id)

    def remove(self, peer_id: str) -> Peer | None:
        """Xóa và trả bản sao peer, hoặc None nếu đã vắng mặt."""
        with self._lock:
            peer = self._peers.pop(peer_id, None)
            return replace(peer) if peer is not None else None

    def remove_expired(
        self, timeout: float, *, now: float | None = None
    ) -> list[Peer]:
        """Xóa và trả bản sao các peer đã quá timeout."""
        if timeout < 0:
            raise ValueError("timeout không được âm")
        if now is None:
            now = time.monotonic()
        with self._lock:
            expired_ids = [
                peer_id
                for peer_id, peer in self._peers.items()
                if now - peer.last_seen > timeout
            ]
            expired = [
                replace(self._peers.pop(peer_id))
                for peer_id in expired_ids
            ]
        return sorted(expired, key=lambda peer: (peer.name, peer.peer_id))
