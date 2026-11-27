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
CLEANUP_INTERVAL = 1.0        # giây, quét peer hết hạn
UDP_RECV_TIMEOUT = 0.5        # giây, kiểm tra tín hiệu dừng khi chờ UDP
UDP_RETRY_INTERVAL = 0.1      # giây, tránh lặp nhanh khi nhận UDP bị lỗi
UDP_RECV_BUFSIZE = 65_535     # byte, kiểm tra MAX_DATAGRAM sau khi nhận
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
MAX_FILENAME_BYTES = 255      # byte UTF-8, tên file nhận (Người 3)
NO_FILE_ID = "00000000"       # file_id giả khi lỗi xảy ra trước khi đọc FILE_META (Người 3)
QUEUE_POLL = 0.2              # giây, cli chờ sự kiện (Người 3)
KEY_POLL = 0.05               # giây, cli chờ phím (Người 3)
PUMP_JOIN_TIMEOUT = 0.5       # giây, cli chờ luồng sự kiện khi thoát (Người 3)
DEFAULT_BROADCAST = "255.255.255.255"  # địa chỉ broadcast mặc định (Người 3)
MAX_PORT = 65535              # cổng lớn nhất (Người 3)

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


# ============================================================================
# D. Đọc/ghi socket (protocol.md mục 2.1–2.2)
# ============================================================================

_SEP_BYTE = FIELD_SEP.encode("ascii")
_TYPE_RE = re.compile(r"[A-Z_]{1,16}")
_LEN_RE = re.compile(r"0|[1-9][0-9]*")


def recv_exact(sock, n: int) -> bytes:
    """Đọc đúng n byte từ sock; bên kia đóng giữa chừng thì raise ConnectionClosed."""
    if not isinstance(n, int) or isinstance(n, bool):
        raise TypeError("n phải là int")
    if n < 0:
        raise ValueError("n không được âm")
    if n == 0:
        return b""
    buf = bytearray()
    while len(buf) < n:
        # Giới hạn mỗi lần recv để không cấp phát bộ đệm khổng lồ khi n lớn.
        chunk = sock.recv(min(n - len(buf), FILE_CHUNK))
        if not chunk:
            raise ConnectionClosed(f"kết nối bị đóng sau {len(buf)}/{n} byte")
        buf += chunk
    return bytes(buf)


def send_frame(sock, ftype: str, payload: bytes) -> None:
    """Gửi một frame TYPE|LEN|PAYLOAD bằng một lần sendall; không tự khóa."""
    if not isinstance(ftype, str):
        raise TypeError("ftype phải là str")
    if ftype not in FRAME_TYPES:
        raise ValueError(f"loại frame không hợp lệ: {ftype!r}")
    if not isinstance(payload, (bytes, bytearray)):
        raise TypeError("payload phải là bytes")
    if len(payload) > MAX_FRAME_PAYLOAD:
        raise ValueError(f"payload {len(payload)} byte vượt quá {MAX_FRAME_PAYLOAD}")
    header = f"{ftype}{FIELD_SEP}{len(payload)}{FIELD_SEP}".encode("ascii")
    sock.sendall(header + payload)


def recv_frame(sock) -> tuple[str, bytes]:
    """Đọc một frame, trả (type, payload); đọc header từng byte nên không bao giờ đọc dư."""
    header = bytearray()
    while header.count(_SEP_BYTE) < 2:
        b = sock.recv(1)
        if not b:
            if not header:
                raise ConnectionClosed("kết nối bị đóng")
            raise ConnectionClosed(f"kết nối bị đóng giữa header ({len(header)} byte)")
        header += b
        if len(header) > MAX_HEADER:
            raise ProtocolError(f"header dài quá {MAX_HEADER} byte")

    try:
        text = header.decode("ascii")
    except UnicodeDecodeError:
        raise ProtocolError("header chứa byte không phải ASCII") from None
    ftype, length, _ = text.split(FIELD_SEP, 2)

    if _TYPE_RE.fullmatch(ftype) is None or ftype not in FRAME_TYPES:
        raise ProtocolError(f"loại frame không hợp lệ: {ftype!r}")
    if _LEN_RE.fullmatch(length) is None:
        raise ProtocolError(f"LEN không hợp lệ: {length!r}")
    size = int(length)
    if size > MAX_FRAME_PAYLOAD:
        raise ProtocolError(f"LEN {size} vượt quá {MAX_FRAME_PAYLOAD}")

    return ftype, recv_exact(sock, size)


# ============================================================================
# E. Tạo/đọc payload (protocol.md mục 2.4)
# ============================================================================
#
# make_* / encode_*: đầu vào do code của mình đưa vào -> ValueError / TypeError.
# parse_* / decode_*: dữ liệu nhận từ mạng          -> ProtocolError.


def _join(*fields: str) -> bytes:
    """Ghép các trường bằng '|' và mã hóa UTF-8; quá MAX_FRAME_PAYLOAD thì ValueError."""
    data = FIELD_SEP.join(fields).encode("utf-8")
    if len(data) > MAX_FRAME_PAYLOAD:
        raise ValueError(f"payload {len(data)} byte vượt quá {MAX_FRAME_PAYLOAD}")
    return data


def _decode(payload: bytes) -> str:
    """Giải mã payload nhận từ mạng bằng UTF-8 strict; sai thì ProtocolError."""
    if not isinstance(payload, (bytes, bytearray)):
        raise TypeError("payload phải là bytes")
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        raise ProtocolError("payload không phải UTF-8 hợp lệ") from None


def _check_peer(peer_id: str, name: str) -> None:
    """Kiểm tra peer_id và name do code của mình đưa vào; sai thì ValueError."""
    if not is_valid_peer_id(peer_id):
        raise ValueError(f"peer_id không hợp lệ: {peer_id!r}")
    if not is_valid_name(name):
        raise ValueError(f"tên không hợp lệ: {name!r}")


def _parse_peer(peer_id: str, name: str) -> tuple[str, str]:
    """Kiểm tra peer_id và name nhận từ mạng; sai thì ProtocolError."""
    if not is_valid_peer_id(peer_id):
        raise ProtocolError(f"peer_id không hợp lệ: {peer_id!r}")
    if not is_valid_name(name):
        raise ProtocolError(f"tên không hợp lệ: {name!r}")
    return peer_id, name


def make_intro(kind: str, peer_id: str, name: str) -> bytes:
    """Tạo payload INTRO: <kind>|<peer_id>|<name>."""
    if kind not in CONN_KINDS:
        raise ValueError(f"kind không hợp lệ: {kind!r}")
    _check_peer(peer_id, name)
    return _join(kind, peer_id, name)


def parse_intro(payload: bytes) -> tuple[str, str, str]:
    """Đọc payload INTRO, trả (kind, peer_id, name)."""
    fields = _decode(payload).split(FIELD_SEP)
    if len(fields) != 3:
        raise ProtocolError(f"INTRO cần 3 trường, nhận {len(fields)}")
    kind, peer_id, name = fields
    if kind not in CONN_KINDS:
        raise ProtocolError(f"kind không hợp lệ: {kind!r}")
    return (kind, *_parse_peer(peer_id, name))


def make_intro_ok(peer_id: str, name: str) -> bytes:
    """Tạo payload INTRO_OK: <peer_id>|<name>."""
    _check_peer(peer_id, name)
    return _join(peer_id, name)


def parse_intro_ok(payload: bytes) -> tuple[str, str]:
    """Đọc payload INTRO_OK, trả (peer_id, name)."""
    fields = _decode(payload).split(FIELD_SEP)
    if len(fields) != 2:
        raise ProtocolError(f"INTRO_OK cần 2 trường, nhận {len(fields)}")
    return _parse_peer(*fields)


def make_intro_err(code: str, message: str) -> bytes:
    """Tạo payload INTRO_ERR: <code>|<message>; message được phép chứa '|'."""
    if code not in INTRO_ERR_CODES:
        raise ValueError(f"mã lỗi INTRO_ERR không hợp lệ: {code!r}")
    if not isinstance(message, str):
        raise TypeError("message phải là str")
    return _join(code, message)


def parse_intro_err(payload: bytes) -> tuple[str, str]:
    """Đọc payload INTRO_ERR, trả (code, message)."""
    fields = _decode(payload).split(FIELD_SEP, 1)
    if len(fields) != 2:
        raise ProtocolError("INTRO_ERR cần 2 trường")
    code, message = fields
    if code not in INTRO_ERR_CODES:
        raise ProtocolError(f"mã lỗi INTRO_ERR không hợp lệ: {code!r}")
    return code, message


def encode_msg(text: str) -> bytes:
    """Tạo payload MSG từ một dòng văn bản; có \\n, \\r hoặc quá dài thì ValueError."""
    if not isinstance(text, str):
        raise TypeError("text phải là str")
    if "\n" in text or "\r" in text:
        raise ValueError("tin nhắn không được chứa \\n hoặc \\r")
    try:
        return _join(text)
    except UnicodeEncodeError:
        raise ValueError("tin nhắn chứa ký tự không mã hóa được UTF-8") from None


def decode_msg(payload: bytes) -> str:
    """Đọc payload MSG; không phải UTF-8 hoặc có \\n, \\r thì ProtocolError."""
    text = _decode(payload)
    if "\n" in text or "\r" in text:
        raise ProtocolError("MSG chứa \\n hoặc \\r")
    return text
