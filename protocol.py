"""Hằng số, hàm kiểm tra và hàm đọc/ghi frame của giao thức LAN Chat v1.

Đặc tả: docs/protocol.md. File này chỉ gồm hằng số, exception và hàm thuần:
không tạo luồng, không giữ trạng thái, không print, không khóa
(khóa gửi do transport.py giữ).
"""

from __future__ import annotations

import re
import secrets
import unicodedata

# ============================================================================
# A. Hằng số (protocol.md mục 5)
# ============================================================================

UDP_PORT = 9999
DEFAULT_TCP_PORT = 5000
HELLO_INTERVAL = 3.0          # giây
PEER_TIMEOUT = 10.0           # giây
MAX_DATAGRAM = 512            # byte
MAX_HEADER = 32               # byte
MAX_FRAME_PAYLOAD = 65_536    # byte
FILE_CHUNK = 65_536           # byte
MAX_FILE_SIZE = 2_147_483_648  # byte (2 GiB)
CONNECT_TIMEOUT = 3.0         # giây
INTRO_TIMEOUT = 5.0           # giây
FILE_IDLE_TIMEOUT = 15.0      # giây
FILE_DONE_TIMEOUT = 10.0      # giây
DUPLICATE_WAIT = 2.0          # giây
GOODBYE_REPEAT = 2            # lần
GOODBYE_GAP = 0.1             # giây
PROGRESS_STEP = 1             # phần trăm
MAX_NAME_LEN = 20             # ký tự

# Tập giá trị hợp lệ (protocol.md mục 1.1 và 2.4)
VALID_STATUSES = frozenset({"online", "away"})
CONN_KINDS = frozenset({"CHAT", "FILE"})
FRAME_TYPES = frozenset({
    "INTRO", "INTRO_OK", "INTRO_ERR", "MSG", "BYE",
    "FILE_META", "FILE_DONE", "FILE_ERR",
})
INTRO_ERR_CODES = frozenset({"BAD_INTRO", "DUPLICATE", "SHUTTING_DOWN"})
FILE_ERR_CODES = frozenset({"BAD_META", "TOO_LARGE", "IO_ERROR", "CANCELLED"})

FIELD_SEP = "|"

_HEX8_RE = re.compile(r"[0-9a-f]{8}")

# ============================================================================
# B. Exception
# ============================================================================
#
# Quy ước lỗi cả nhóm dùng chung:
#   ConnectionClosed     bên kia đóng kết nối (recv trả b"")   -> transport: lost / bye
#   ProtocolError        dữ liệu NHẬN TỪ MẠNG sai định dạng     -> transport: protocol_error
#   ValueError/TypeError code CỦA MÌNH gọi hàm sai              -> nơi gọi hàm xử lý
#   TimeoutError/OSError lỗi socket                             -> không bắt ở đây, để lan lên


class ProtocolError(Exception):
    """Dữ liệu nhận từ mạng không đúng định dạng giao thức."""


class ConnectionClosed(ConnectionError):
    """Bên kia đã đóng kết nối (recv trả về b"") trước khi nhận đủ dữ liệu."""


# ============================================================================
# C. Hàm kiểm tra và sinh id (protocol.md mục 0)
# ============================================================================


def is_valid_name(name: str) -> bool:
    """Tên 1–20 ký tự, không toàn chữ số, không chứa '|', khoảng trắng hay ký tự điều khiển."""
    if not isinstance(name, str):
        return False
    if not 1 <= len(name) <= MAX_NAME_LEN:
        return False
    if name.isdigit():
        return False
    for ch in name:
        if ch == FIELD_SEP or ch.isspace() or unicodedata.category(ch) == "Cc":
            return False
    return True


def is_valid_peer_id(peer_id: str) -> bool:
    """peer_id (và file_id) là đúng 8 ký tự hex thường."""
    return isinstance(peer_id, str) and _HEX8_RE.fullmatch(peer_id) is not None


def is_valid_status(status: str) -> bool:
    """Trạng thái là 'online' hoặc 'away'."""
    return isinstance(status, str) and status in VALID_STATUSES


def new_id() -> str:
    """Sinh id ngẫu nhiên 8 ký tự hex thường, dùng cho peer_id và file_id."""
    return secrets.token_hex(4)
