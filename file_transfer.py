import contextlib
import hashlib
import os
import queue
import re
import socket
import threading
import unicodedata

from events import ERROR, FILE_COMPLETED, FILE_PROGRESS, FILE_UNCONFIRMED, Event
from peer_store import Peer
from protocol import (
    ConnectionClosed,
    FILE_CHUNK,
    FILE_DONE_TIMEOUT,
    FILE_IDLE_TIMEOUT,
    MAX_FILENAME_BYTES,
    MAX_FILE_SIZE,
    NO_FILE_ID,
    ProtocolError,
    is_valid_peer_id,
    new_id,
    recv_frame,
    send_frame,
)
from transport import ConnectError, Transport


_RESERVED_CHARS = re.compile(r'[<>:"|?*]')
_rename_lock = threading.Lock()


class _MetaError(Exception):
    def __init__(self, code: str, msg: str, file_id: str = NO_FILE_ID) -> None:
        super().__init__(msg)
        self.code, self.file_id = code, file_id


def _error(events: queue.Queue, msg: str, fid: str | None = None) -> None:
    data = {"source": "file", "message": msg}
    if fid is not None:
        data["file_id"] = fid
    events.put(Event(ERROR, data))


def _progress(
    events: queue.Queue, fid: str, pid: str, fn: str, d: str, done: int, tot: int
) -> None:
    events.put(
        Event(
            FILE_PROGRESS,
            {
                "file_id": fid,
                "peer_id": pid,
                "filename": fn,
                "direction": d,
                "done": done,
                "total": tot,
            },
        )
    )


def _completed(
    events: queue.Queue, fid: str, pid: str, fn: str, d: str, path: str | None = None
) -> None:
    data = {"file_id": fid, "peer_id": pid, "filename": fn, "direction": d}
    if path is not None:
        data["path"] = path
    events.put(Event(FILE_COMPLETED, data))


def _unconfirmed(events: queue.Queue, fid: str, pid: str, fn: str) -> None:
    events.put(Event(FILE_UNCONFIRMED, {"file_id": fid, "peer_id": pid, "filename": fn}))


class _Progress:
    def __init__(
        self, events: queue.Queue, fid: str, pid: str, fn: str, d: str, tot: int
    ) -> None:
        self.events, self.fid, self.pid, self.fn, self.d, self.tot = events, fid, pid, fn, d, tot
        self.last_pct = 0
        _progress(events, fid, pid, fn, d, 0, tot)

    def advance(self, done: int) -> None:
        if self.tot > 0:
            pct = (done * 100) // self.tot
            if pct > self.last_pct:
                _progress(self.events, self.fid, self.pid, self.fn, self.d, done, self.tot)
                self.last_pct = pct


def _unique_path(downloads_dir: str, filename: str) -> tuple[str, str]:
    target = os.path.join(downloads_dir, filename)
    if not os.path.exists(target):
        return filename, target
    base, ext = os.path.splitext(filename)
    counter = 1
    while True:
        candidate = f"{base} ({counter}){ext}"
        path = os.path.join(downloads_dir, candidate)
        if not os.path.exists(path):
            return candidate, path
        counter += 1


def _parse_meta(payload: bytes) -> tuple[str, int, str]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise _MetaError("BAD_META", "invalid utf-8 in FILE_META")
    parts = text.split("|", 2)
    if len(parts) != 3:
        raise _MetaError("BAD_META", "malformed FILE_META payload")
    raw_id, raw_size, raw_name = parts
    if not is_valid_peer_id(raw_id):
        raise _MetaError("BAD_META", "invalid file_id")
    if not (
        raw_size.isascii()
        and raw_size.isdigit()
        and (raw_size == "0" or not raw_size.startswith("0"))
    ):
        raise _MetaError("BAD_META", "invalid size in FILE_META", raw_id)
    if len(raw_size) > len(str(MAX_FILE_SIZE)):
        raise _MetaError("TOO_LARGE", "file too large", raw_id)
    clean = _RESERVED_CHARS.sub("_", raw_name.replace("\\", "/").split("/")[-1])
    if (
        not clean.strip(" .")
        or len(clean.encode("utf-8")) > MAX_FILENAME_BYTES
        or any(
            ord(c) < 32 or ord(c) == 127 or unicodedata.category(c).startswith("C")
            for c in clean
        )
    ):
        raise _MetaError("BAD_META", f"invalid filename: {raw_name}", raw_id)
    size = int(raw_size)
    if size > MAX_FILE_SIZE:
        raise _MetaError("TOO_LARGE", f"file too large: {size} bytes", raw_id)
    return raw_id, size, clean


def send_file(
    sock: socket.socket, path: str, peer_id: str, events: queue.Queue
) -> None:
    file_id: str | None = None
    filename = os.path.basename(path)
    try:
        try:
            f = open(path, "rb")
            size = os.path.getsize(path)
        except OSError as e:
            _error(events, f"cannot read {filename}: {e}")
            return
        with f:
            file_id = new_id()
            sock.settimeout(FILE_IDLE_TIMEOUT)
            try:
                send_frame(sock, "FILE_META", f"{file_id}|{size}|{filename}".encode("utf-8"))
            except (OSError, ProtocolError):
                _error(events, f"transfer interrupted: {filename}", file_id)
                return
            hasher = hashlib.sha256()
            prog = _Progress(events, file_id, peer_id, filename, "send", size)
            done = 0
            while done < size:
                try:
                    chunk = f.read(min(FILE_CHUNK, size - done))
                    if not chunk:
                        _error(events, f"file changed while sending: {filename}", file_id)
                        return
                    hasher.update(chunk)
                    sock.sendall(chunk)
                except OSError:
                    _error(events, f"transfer interrupted: {filename}", file_id)
                    return
                done += len(chunk)
                prog.advance(done)
            try:
                sock.settimeout(FILE_DONE_TIMEOUT)
                ftype, resp = recv_frame(sock)
            except (TimeoutError, ConnectionClosed, OSError, ProtocolError):
                _unconfirmed(events, file_id, peer_id, filename)
                return
            if ftype == "FILE_DONE":
                parts = resp.decode("utf-8", errors="replace").split("|", 1)
                if (
                    len(parts) == 2
                    and parts[0] == file_id
                    and parts[1].lower() == hasher.hexdigest().lower()
                ):
                    _completed(events, file_id, peer_id, filename, "send")
                else:
                    _error(events, f"file arrived corrupted: {filename}", file_id)
            elif ftype == "FILE_ERR":
                p = resp.decode("utf-8", errors="replace").split("|", 2)
                msg = p[2] if len(p) == 3 else resp.decode("utf-8", errors="replace")
                _error(events, msg, file_id)
            else:
                _unconfirmed(events, file_id, peer_id, filename)
    finally:
        with contextlib.suppress(OSError):
            sock.close()


def receive_file(
    sock: socket.socket,
    peer_id: str,
    name: str,
    events: queue.Queue,
    downloads_dir: str,
) -> None:
    file_id = NO_FILE_ID
    part_path: str | None = None
    clean_name = ""
    try:
        sock.settimeout(FILE_IDLE_TIMEOUT)
        ftype, payload = recv_frame(sock)
        if ftype != "FILE_META":
            raise _MetaError("BAD_META", f"expected FILE_META, got {ftype}")
        file_id, size, clean_name = _parse_meta(payload)
    except (ProtocolError, _MetaError) as e:
        code = e.code if isinstance(e, _MetaError) else "BAD_META"
        fid = e.file_id if isinstance(e, _MetaError) else NO_FILE_ID
        with contextlib.suppress(OSError, ProtocolError):
            send_frame(sock, "FILE_ERR", f"{fid}|{code}|{e}".encode("utf-8"))
        _error(events, str(e), fid if fid != NO_FILE_ID else None)
        with contextlib.suppress(OSError):
            sock.close()
        return
    except (TimeoutError, ConnectionClosed, OSError) as e:
        _error(events, f"handshake failed: {e}")
        with contextlib.suppress(OSError):
            sock.close()
        return

    try:
        os.makedirs(downloads_dir, exist_ok=True)
        part_path = os.path.join(downloads_dir, f"{file_id}.part")
        hasher = hashlib.sha256()
        prog = _Progress(events, file_id, peer_id, clean_name, "recv", size)
        done = 0
        with open(part_path, "wb") as out_f:
            remaining = size
            while remaining > 0:
                chunk = sock.recv(min(FILE_CHUNK, remaining))
                if not chunk:
                    raise ConnectionClosed()
                out_f.write(chunk)
                hasher.update(chunk)
                done += len(chunk)
                remaining -= len(chunk)
                prog.advance(done)

        sock.setblocking(False)
        try:
            extra = sock.recv(1, socket.MSG_PEEK)
        except BlockingIOError:
            extra = b""
        if extra:
            with contextlib.suppress(OSError):
                sock.recv(FILE_CHUNK)
            raise ProtocolError("unexpected data after file")

        with _rename_lock:
            final_name, final_path = _unique_path(downloads_dir, clean_name)
            os.replace(part_path, final_path)
        part_path = None
        sock.settimeout(FILE_DONE_TIMEOUT)
        send_frame(sock, "FILE_DONE", f"{file_id}|{hasher.hexdigest()}".encode("ascii"))
        _completed(events, file_id, peer_id, final_name, "recv", final_path)
    except (OSError, ConnectionClosed, ProtocolError, TimeoutError) as e:
        if part_path is not None:
            with contextlib.suppress(OSError):
                if os.path.exists(part_path):
                    os.remove(part_path)
        with contextlib.suppress(OSError, ProtocolError):
            send_frame(
                sock,
                "FILE_ERR",
                f"{file_id}|IO_ERROR|receiving {clean_name} failed: {e}".encode("utf-8"),
            )
        _error(events, f"receiving {clean_name} failed: {e}", file_id)
    finally:
        with contextlib.suppress(OSError):
            sock.close()


def start_send(
    transport: Transport, peer: Peer, path: str, events: queue.Queue
) -> None:
    def runner() -> None:
        try:
            open(path, "rb").close()
        except OSError as e:
            _error(events, f"cannot read {os.path.basename(path)}: {e}")
            return
        try:
            sock = transport.open_file_connection(peer)
        except ConnectError as e:
            _error(events, str(e))
            return
        send_file(sock, path, peer.peer_id, events)

    threading.Thread(target=runner, daemon=True).start()
