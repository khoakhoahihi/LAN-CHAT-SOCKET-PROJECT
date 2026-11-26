"""Sự kiện các module gửi cho cli.py qua hàng đợi chung (CONTRIBUTING.md mục 2.5).

Chỉ cli.py đọc hàng đợi `events`; các module khác chỉ `put(Event(...))`.
Event tự kiểm tra loại sự kiện, các khóa bắt buộc trong `data` và các trường
có tập giá trị cố định, để lỗi gõ nhầm lộ ra ngay ở nơi phát thay vì ở cli.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ============================================================================
# Loại sự kiện
# ============================================================================

PEER_ONLINE = "PEER_ONLINE"              # Người 1
PEER_UPDATED = "PEER_UPDATED"            # Người 1
PEER_OFFLINE = "PEER_OFFLINE"            # Người 1
CHAT_CONNECTED = "CHAT_CONNECTED"        # Người 2
CHAT_RECEIVED = "CHAT_RECEIVED"          # Người 2
CONNECTION_CLOSED = "CONNECTION_CLOSED"  # Người 2
FILE_PROGRESS = "FILE_PROGRESS"          # Người 3
FILE_COMPLETED = "FILE_COMPLETED"        # Người 3
FILE_UNCONFIRMED = "FILE_UNCONFIRMED"    # Người 3
ERROR = "ERROR"                          # Tất cả

# Khóa bắt buộc trong `data` của từng loại. Khóa tùy chọn (`path` của
# FILE_COMPLETED, `file_id` của ERROR) được phép có thêm.
REQUIRED_KEYS: dict[str, frozenset[str]] = {
    PEER_ONLINE: frozenset({"peer"}),
    PEER_UPDATED: frozenset({"peer", "old"}),
    PEER_OFFLINE: frozenset({"peer", "reason"}),
    CHAT_CONNECTED: frozenset({"peer_id", "name", "incoming"}),
    CHAT_RECEIVED: frozenset({"peer_id", "name", "text"}),
    CONNECTION_CLOSED: frozenset({"peer_id", "name", "reason"}),
    FILE_PROGRESS: frozenset({"file_id", "peer_id", "filename", "direction", "done", "total"}),
    FILE_COMPLETED: frozenset({"file_id", "peer_id", "filename", "direction"}),
    FILE_UNCONFIRMED: frozenset({"file_id", "peer_id", "filename"}),
    ERROR: frozenset({"source", "message"}),
}

EVENT_TYPES = frozenset(REQUIRED_KEYS)

# ============================================================================
# Giá trị hợp lệ của các trường liệt kê
# ============================================================================

OFFLINE_TIMEOUT = "timeout"
OFFLINE_GOODBYE = "goodbye"
OFFLINE_REASONS = frozenset({OFFLINE_TIMEOUT, OFFLINE_GOODBYE})

CLOSE_LOCAL_LEAVE = "local_leave"
CLOSE_BYE = "bye"
CLOSE_LOST = "lost"
CLOSE_PROTOCOL_ERROR = "protocol_error"
CLOSE_REASONS = frozenset({CLOSE_LOCAL_LEAVE, CLOSE_BYE, CLOSE_LOST, CLOSE_PROTOCOL_ERROR})

DIR_SEND = "send"
DIR_RECV = "recv"
DIRECTIONS = frozenset({DIR_SEND, DIR_RECV})

SOURCE_UDP = "udp"
SOURCE_TCP = "tcp"
SOURCE_FILE = "file"
SOURCE_CLI = "cli"
ERROR_SOURCES = frozenset({SOURCE_UDP, SOURCE_TCP, SOURCE_FILE, SOURCE_CLI})

# (loại sự kiện, khóa) -> tập giá trị hợp lệ
_ENUM_FIELDS: dict[tuple[str, str], frozenset[str]] = {
    (PEER_OFFLINE, "reason"): OFFLINE_REASONS,
    (CONNECTION_CLOSED, "reason"): CLOSE_REASONS,
    (FILE_PROGRESS, "direction"): DIRECTIONS,
    (FILE_COMPLETED, "direction"): DIRECTIONS,
    (ERROR, "source"): ERROR_SOURCES,
}


@dataclass(frozen=True)
class Event:
    """Một sự kiện gửi cho cli.py; sai loại, thiếu khóa hoặc sai giá trị thì ValueError."""

    type: str
    data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.type not in REQUIRED_KEYS:
            raise ValueError(f"loại sự kiện không hợp lệ: {self.type!r}")
        if not isinstance(self.data, dict):
            raise TypeError("data phải là dict")
        missing = REQUIRED_KEYS[self.type] - self.data.keys()
        if missing:
            raise ValueError(f"{self.type} thiếu khóa: {', '.join(sorted(missing))}")
        for (etype, key), allowed in _ENUM_FIELDS.items():
            if etype == self.type and self.data[key] not in allowed:
                raise ValueError(f"{self.type}.{key} không hợp lệ: {self.data[key]!r}")
        # Sao chép để nơi phát sửa dict của mình sau khi put() cũng không ảnh hưởng.
        object.__setattr__(self, "data", dict(self.data))
