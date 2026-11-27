import hashlib
import os
import queue
import socket
import threading

from events import ERROR, FILE_COMPLETED, FILE_PROGRESS, FILE_UNCONFIRMED, Event
from peer_store import Peer
from protocol import (
    ConnectionClosed,
    FILE_CHUNK,
    FILE_DONE_TIMEOUT,
    FILE_IDLE_TIMEOUT,
    MAX_FILE_SIZE,
    ProtocolError,
    is_valid_peer_id,
    new_id,
    recv_frame,
    send_frame,
)
from transport import ConnectError, Transport

NO_FILE_ID = "00000000"
BAD_CHARS = '<>:"|?*'
rename_lock = threading.Lock()


class MetaError(Exception):
    def __init__(self, code: str, msg: str, file_id: str = NO_FILE_ID) -> None:
        super().__init__(msg)
        self.code, self.file_id = code, file_id


def _error(events: queue.Queue, msg: str, file_id: str | None = None) -> None:
    data = {"source": "file", "message": msg}
    if file_id and file_id != NO_FILE_ID:
        data["file_id"] = file_id
    events.put(Event(ERROR, data))


def _progress(events: queue.Queue, info: dict, done: int, total: int, last: int) -> int:
    pct = done * 100 // total if total else 100
    if pct != last:
        events.put(Event(FILE_PROGRESS, {**info, "done": done, "total": total}))
    return pct


def _unique_path(folder: str, name: str) -> tuple[str, str]:
    base, ext = os.path.splitext(name)
    final, n = name, 0
    while os.path.exists(os.path.join(folder, final)):
        n += 1
        final = f"{base} ({n}){ext}"
    return final, os.path.join(folder, final)


def _parse_meta(payload: bytes) -> tuple[str, int, str]:
    try:
        file_id, size, name = payload.decode("utf-8").split("|", 2)
    except ValueError:
        raise MetaError("BAD_META", "malformed FILE_META")
    if not is_valid_peer_id(file_id):
        raise MetaError("BAD_META", "invalid file_id")
    if not (size.isascii() and size.isdigit()):
        raise MetaError("BAD_META", "invalid size in FILE_META", file_id)
    if len(size) > len(str(MAX_FILE_SIZE)) or int(size) > MAX_FILE_SIZE:
        raise MetaError("TOO_LARGE", "file too large", file_id)
    clean = name.replace("\\", "/").split("/")[-1]
    clean = "".join("_" if c in BAD_CHARS or c < " " else c for c in clean)
    if not clean.strip(" ."):
        raise MetaError("BAD_META", f"invalid filename: {name}", file_id)
    return file_id, int(size), clean


def _push(sock: socket.socket, path: str, info: dict, events: queue.Queue) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        size = os.path.getsize(path)
        sock.settimeout(FILE_IDLE_TIMEOUT)
        meta = f"{info['file_id']}|{size}|{info['filename']}"
        send_frame(sock, "FILE_META", meta.encode("utf-8"))
        done, last = 0, _progress(events, info, 0, size, -1)
        while done < size:
            chunk = f.read(min(FILE_CHUNK, size - done))
            if not chunk:
                raise OSError("file changed while sending")
            hasher.update(chunk)
            sock.sendall(chunk)
            done += len(chunk)
            last = _progress(events, info, done, size, last)
    return hasher.hexdigest()


def send_file(sock: socket.socket, path: str, peer_id: str, events: queue.Queue) -> None:
    name, file_id = os.path.basename(path), new_id()
    info = {"file_id": file_id, "peer_id": peer_id, "filename": name, "direction": "send"}
    try:
        digest = _push(sock, path, info, events)
    except (OSError, ProtocolError) as e:
        sock.close()
        _error(events, f"transfer interrupted: {name}: {e}", file_id)
        return
    try:
        sock.settimeout(FILE_DONE_TIMEOUT)
        ftype, resp = recv_frame(sock)
    except (OSError, ProtocolError):
        ftype, resp = "", b""
    sock.close()
    parts = resp.decode("utf-8", errors="replace").lower().split("|", 2)
    if ftype == "FILE_DONE" and parts[:2] == [file_id, digest]:
        events.put(Event(FILE_COMPLETED, info))
    elif ftype == "FILE_DONE":
        _error(events, f"file arrived corrupted: {name}", file_id)
    elif ftype == "FILE_ERR":
        _error(events, parts[-1], file_id)
    else:
        events.put(Event(FILE_UNCONFIRMED, info))


def _fail(sock: socket.socket, events: queue.Queue, file_id: str, code: str, msg: str) -> None:
    try:
        send_frame(sock, "FILE_ERR", f"{file_id}|{code}|{msg}".encode("utf-8"))
    except (OSError, ProtocolError):
        pass
    _error(events, msg, file_id)


def receive_file(
    sock: socket.socket, peer_id: str, name: str, events: queue.Queue, downloads_dir: str
) -> None:
    file_id, part, filename = NO_FILE_ID, None, ""
    try:
        sock.settimeout(FILE_IDLE_TIMEOUT)
        ftype, payload = recv_frame(sock)
        if ftype != "FILE_META":
            raise MetaError("BAD_META", f"expected FILE_META, got {ftype}")
        file_id, size, filename = _parse_meta(payload)
        info = {"file_id": file_id, "peer_id": peer_id, "filename": filename, "direction": "recv"}
        os.makedirs(downloads_dir, exist_ok=True)
        part = os.path.join(downloads_dir, f"{file_id}.part")
        hasher = hashlib.sha256()
        done, last = 0, _progress(events, info, 0, size, -1)
        with open(part, "wb") as out:
            while done < size:
                chunk = sock.recv(min(FILE_CHUNK, size - done))
                if not chunk:
                    raise ConnectionClosed()
                out.write(chunk)
                hasher.update(chunk)
                done += len(chunk)
                last = _progress(events, info, done, size, last)
        with rename_lock:
            filename, final = _unique_path(downloads_dir, filename)
            os.replace(part, final)
        part = None
        sock.settimeout(FILE_DONE_TIMEOUT)
        send_frame(sock, "FILE_DONE", f"{file_id}|{hasher.hexdigest()}".encode("ascii"))
        events.put(Event(FILE_COMPLETED, {**info, "filename": filename, "path": final}))
    except MetaError as e:
        _fail(sock, events, e.file_id, e.code, str(e))
    except (OSError, ProtocolError) as e:
        if part and os.path.exists(part):
            os.remove(part)
        _fail(sock, events, file_id, "IO_ERROR", f"receiving {filename} failed: {e}")
    finally:
        sock.close()


def start_send(transport: Transport, peer: Peer, path: str, events: queue.Queue) -> None:
    def run() -> None:
        try:
            sock = transport.open_file_connection(peer)
        except ConnectError as e:
            _error(events, str(e))
            return
        send_file(sock, path, peer.peer_id, events)

    threading.Thread(target=run, daemon=True).start()
