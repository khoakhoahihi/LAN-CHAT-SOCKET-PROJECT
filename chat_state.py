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
        self._unread: dict[str, list[Message]] = {}

    def add_message(self, peer_id: str, sender: str, text: str) -> bool:
        with self._lock:
            if peer_id == self.current:
                return True
            self._unread.setdefault(peer_id, []).append(Message(sender, text))
            return False

    def switch_to(self, peer_id: str) -> list[Message]:
        with self._lock:
            self.current = peer_id
            return self._unread.pop(peer_id, [])

    def unread(self, peer_id: str) -> int:
        with self._lock:
            return len(self._unread.get(peer_id, []))

    def leave(self) -> None:
        with self._lock:
            self.current = None
