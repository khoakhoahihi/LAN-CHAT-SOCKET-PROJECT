from dataclasses import dataclass
import threading


@dataclass(frozen=True)
class Message:
    sender: str
    text: str


class ChatState:
    def __init__(self) -> None:
        self.current: str | None = None
        self._lock = threading.Lock()
        self._history: dict[str, list[Message]] = {}
        self._unread: dict[str, int] = {}

    def add_message(self, peer_id: str, sender: str, text: str) -> bool:
        with self._lock:
            self._history.setdefault(peer_id, []).append(Message(sender, text))
            if peer_id == self.current:
                return True
            self._unread[peer_id] = self._unread.get(peer_id, 0) + 1
            return False

    def switch_to(self, peer_id: str) -> list[Message]:
        with self._lock:
            self.current = peer_id
            count = self._unread.get(peer_id, 0)
            self._unread[peer_id] = 0
            if count == 0:
                return []
            return list(self._history.get(peer_id, [])[-count:])

    def unread(self, peer_id: str) -> int:
        with self._lock:
            return self._unread.get(peer_id, 0)

    def leave(self) -> None:
        with self._lock:
            self.current = None

    def history(self, peer_id: str) -> list[Message]:
        with self._lock:
            return list(self._history.get(peer_id, []))
