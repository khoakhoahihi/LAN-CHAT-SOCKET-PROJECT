from dataclasses import dataclass,replace
import threading
import time

@dataclass(frozen=True)
class Peer:
    peer_id: str
    name: str
    ip: str
    tcp_port: int
    status: str
    last_seen: float

class PeerStore:
    def __init__(self)->None:
        """ Khở tạo peer rỗng và khóa bảo  vệ. """
        self._peers: dict[str,Peer]={}
        self._lock=threading.Lock()

    """ Thêm mới hoặc cập nhật peer, trả về trước và sau """
    def upsert(self,peer: Peer) ->tuple[Peer | None,Peer]:
        with self._lock:
            old_peer=self._peers.get(peer.peer_id)
            stored_peer=replace(peer)
            self._peers[peer.peer_id]=stored_peer
            old_copy=(
                replace(old_peer)
                if old_peer is not None
                else None
            )
            return old_copy,replace(stored_peer)

    """ Tìm 1 peer theo ID """
    def get(self,peer_id:str)->Peer| None:
        with self._lock:
            peer=self._peers.get(peer_id)

            return (
                replace(peer)
                if peer is not None
                else None
            )

    """ Lấy danh sách đã sắp xếp """
    def get_peers(self)->list[Peer]:
        with self._lock:
            peers=[replace(peer) for peer in self._peers.values()]
        return sorted(peers, key=lambda peer : (peer.name,peer.peer_id))

    """ Tìm theo tên và xử lý trùng tên """
    def find_by_name(self,name:str)->list[Peer]:
        with self._lock:
            peers=[
                replace(peer)
                for peer in self._peers.values()
                if peer.name==name
            ]
        return sorted(peers,key=lambda peer: peer.peer_id)

    """ Xóa peer theo ID """
    def remove(self,peer_id:str)->Peer | None:
        with self._lock:
            peer=self._peers.pop(peer_id,None)
            return (
                replace(peer)
                if peer is not None
                else None
            )

    """ Xóa peer quá hạn """
    def remove_expired(self,timeout: float,*,now: float |None=None)->list[Peer]:
        if (timeout<0):
            raise ValueError("timeout không được âm")
        if now is None:
            now=time.monotonic()
        with self._lock:
            expired_ids=[
                peer_id
                for peer_id,peer in self._peers.items()
                if now-peer.last_seen > timeout
            ]
            expired = [
                replace(self._peers.pop(peer_id))
                for peer_id in expired_ids
            ]
        return sorted(expired,key=lambda peer:(peer.name,peer.peer_id))
    