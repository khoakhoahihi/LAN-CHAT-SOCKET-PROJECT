from __future__ import annotations

import math
import os
import queue
import shutil
import sys
import threading
import unicodedata

if sys.platform == "win32":
    import msvcrt
else:
    import select
    import termios
    import tty

from chat_state import ChatState
from discovery import Discovery
from events import (
    CHAT_CONNECTED,
    CHAT_RECEIVED,
    CONNECTION_CLOSED,
    ERROR,
    Event,
    FILE_COMPLETED,
    FILE_PROGRESS,
    FILE_UNCONFIRMED,
    PEER_OFFLINE,
    PEER_ONLINE,
    PEER_UPDATED,
)
import file_transfer
from peer_store import PeerStore
from protocol import KEY_POLL, PUMP_JOIN_TIMEOUT, QUEUE_POLL, is_valid_status
from transport import ConnectError, NotConnected, Transport

_EXTENDED_KEYS = "HPKMGOIQRS"

HELP_LINES = (
    "/list                  show online users (number, name, IP, status)",
    "/chat <name|number>    chat with a user (number from the last /list)",
    "<text>                 send a message to the current chat",
    "/send <file>           send a file to the current chat",
    "/leave                 close the current chat (unread messages are kept)",
    "/status <online|away>  change your status",
    "/help                  show this list",
    "/quit                  exit",
)


def _format_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    return f"{n / 1024:.1f} KB" if n < 1048576 else f"{n / 1048576:.1f} MB"


class CLI:
    def __init__(
        self, my_id: str, name: str, store: PeerStore, transport: Transport, discovery: Discovery,
        state: ChatState, events: queue.Queue, stop: threading.Event, tcp_port: int, udp_port: int,
        downloads: str,
    ) -> None:
        self.name, self.store, self.transport = name, store, transport
        self.discovery, self.state, self.events = discovery, state, events
        self.stop, self.tcp_port, self.udp_port = stop, tcp_port, udp_port
        self.downloads = downloads
        self._print_lock = threading.Lock()
        self._input_buf: list[str] = []
        self._pending: list[str] = []
        self._listing: list[str] | None = None
        self._names: dict[str, str] = {}
        self._file_transfers: dict[str, int] = {}
        self._commands = {
            "/list": self._cmd_list, "/chat": self._cmd_chat, "/send": self._cmd_send,
            "/leave": self._cmd_leave, "/status": self._cmd_status, "/help": self._cmd_help,
            "/quit": self._cmd_quit,
        }
        if self._is_terminal() and sys.platform == "win32":
            os.system("")

    def _is_terminal(self) -> bool:
        return bool(
            getattr(sys.stdin, "isatty", lambda: False)()
            and getattr(sys.stdout, "isatty", lambda: False)()
        )

    def _clear_prompt(self) -> str:
        cells = sum(
            2 if unicodedata.east_asian_width(c) in "WF" else 1
            for c in "> " + "".join(self._input_buf)
        )
        up = max(math.ceil(cells / shutil.get_terminal_size().columns) - 1, 0)
        return "\r" + (f"\033[{up}A" if up else "") + "\033[J"

    def say(self, text: str) -> None:
        with self._print_lock:
            if not self._is_terminal():
                buf = f"{text}\n"
            elif self.stop.is_set():
                buf = f"{self._clear_prompt()}{text}\n"
            else:
                buf = f"{self._clear_prompt()}{text}\n> {''.join(self._input_buf)}"
            sys.stdout.write(buf)
            sys.stdout.flush()

    def _peer_name(self, pid: str) -> str:
        p = self.store.get(pid)
        return self._names.get(pid) or (p.name if p else pid)

    def _on_peer(self, t: str, d: dict) -> None:
        p = d["peer"]
        self._names[p.peer_id] = p.name
        if t == PEER_ONLINE:
            self.say(f"[*] {p.name} is online")
        elif t == PEER_UPDATED and d["old"].status != p.status:
            self.say(f"[*] {p.name} is now {p.status}")
        elif t == PEER_OFFLINE:
            self.say(f"[*] {p.name} is offline")

    def _on_chat(self, t: str, d: dict) -> None:
        if t == CHAT_CONNECTED:
            self._names[d["peer_id"]] = d["name"]
            if d.get("incoming"):
                self.say(f"[*] {d['name']} opened a chat with you")
        elif t == CHAT_RECEIVED:
            pid, name, text = d["peer_id"], d["name"], d["text"]
            self._names[pid] = name
            if self.state.add_message(pid, name, text):
                self.say(f"[{name}] {text}")
            else:
                self.say(f"[*] New message from {name} ({self.state.unread(pid)} unread)")
        elif t == CONNECTION_CLOSED:
            name = d.get("name") or self._peer_name(d.get("peer_id", ""))
            r = d.get("reason")
            if r == "bye":
                self.say(f"[*] {name} closed the chat")
            elif r == "lost":
                self.say(f"[*] Connection to {name} lost")
            elif r == "protocol_error":
                self.say(f"[*] Connection to {name} closed: protocol error")

    def _on_file(self, t: str, d: dict) -> None:
        if t == FILE_PROGRESS:
            fid, fn, dr = d["file_id"], d["filename"], d["direction"]
            done, tot = d["done"], d["total"]
            if fid not in self._file_transfers:
                self._file_transfers[fid] = 0
                verb = "Sending" if dr == "send" else "Receiving"
                self.say(f"[*] {verb} {fn} ({_format_size(tot)}) ...")
                return
            pct = (done * 100) // tot if tot > 0 else 0
            last, curr = self._file_transfers[fid], (pct // 10) * 10
            while last + 10 <= min(curr, 90):
                last += 10
                self.say(f"[*] {fn}  {last}%")
            self._file_transfers[fid] = last
        elif t == FILE_COMPLETED:
            if d.get("direction") == "send":
                self.say(f"[*] Sent {d['filename']} ... 100%  Done.")
            else:
                self.say(f"[*] Received {d['filename']} -> {d.get('path', '')}")
            self._file_transfers.pop(d["file_id"], None)
        elif t == FILE_UNCONFIRMED:
            self.say(f"[!] Sent 100% of {d['filename']} but the receiver did not confirm it")
            self._file_transfers.pop(d["file_id"], None)

    def _show(self, event: Event) -> None:
        t, d = event.type, event.data
        if t in (PEER_ONLINE, PEER_UPDATED, PEER_OFFLINE):
            self._on_peer(t, d)
        elif t in (CHAT_CONNECTED, CHAT_RECEIVED, CONNECTION_CLOSED):
            self._on_chat(t, d)
        elif t in (FILE_PROGRESS, FILE_COMPLETED, FILE_UNCONFIRMED):
            self._on_file(t, d)
        elif t == ERROR:
            self.say(f"[!] {d.get('message', '')}")

    def _event_pump(self) -> None:
        while not self.stop.is_set():
            try:
                event = self.events.get(timeout=QUEUE_POLL)
            except queue.Empty:
                continue
            self._show(event)

    def _cmd_list(self, _arg: str = "") -> None:
        peers = self.store.get_peers()
        self._listing = [p.peer_id for p in peers]
        if not peers:
            self.say("[*] No one else is online")
            return
        w = max(len(p.name) for p in peers)
        lines = [f"{i:>2}. {p.name:<{w}}  ({p.ip})  {p.status}" for i, p in enumerate(peers, 1)]
        self.say("\n".join(lines))

    def _cmd_chat(self, arg: str) -> None:
        if not arg:
            self.say("[!] Usage: /chat <name|number>")
            return
        if arg.isdigit():
            n = int(arg)
            listing = self._listing or [p.peer_id for p in self.store.get_peers()]
            if n < 1 or n > len(listing):
                self.say(f"[!] No user number {n}. Use /list")
                return
            pid = listing[n - 1]
            peer = self.store.get(pid)
            if peer is None:
                self.say("[!] That user is offline")
                return
        else:
            matches = self.store.find_by_name(arg)
            if not matches:
                self.say(f"[!] No such user: {arg}")
                return
            if len(matches) > 1:
                self._cmd_list()
                self.say(f"[!] {len(matches)} users named {arg}, use /chat <number>")
                return
            peer, pid = matches[0], matches[0].peer_id
        if self.transport.is_connected(pid):
            self.say(f"[*] Switched to {peer.name}")
        else:
            try:
                self.transport.connect_chat(peer)
            except ConnectError as e:
                self.say(f"[!] {e}")
                return
            self.say(f"[*] Connected to {peer.name} ({peer.ip}:{peer.tcp_port})")
        msgs = self.state.switch_to(pid)
        if msgs:
            lines = [f"[*] {len(msgs)} unread:"] + [f"[{m.sender}] {m.text}" for m in msgs]
            self.say("\n".join(lines))
        self._names[pid] = peer.name

    def _cmd_send(self, arg: str) -> None:
        is_quoted = arg.startswith(('"', "'")) and arg.endswith(('"', "'"))
        path = arg[1:-1].strip() if is_quoted else arg
        if not path:
            self.say("[!] Usage: /send <file>")
            return
        if self.state.current is None:
            self.say("[*] No active chat. Use /chat <name|number>")
            return
        peer = self.store.get(self.state.current)
        if peer is None:
            self.say(f"[!] {self._peer_name(self.state.current)} is offline, cannot send a file")
            return
        if not (os.path.isfile(path) and os.access(path, os.R_OK)):
            self.say(f"[!] File not found: {path}")
            return
        file_transfer.start_send(self.transport, peer, path, self.events)

    def _cmd_leave(self, _arg: str) -> None:
        if self.state.current is None:
            self.say("[*] No active chat")
            return
        name = self._peer_name(self.state.current)
        self.transport.disconnect_chat(self.state.current)
        self.state.leave()
        self.say(f"[*] Chat with {name} closed")

    def _cmd_status(self, arg: str) -> None:
        st = arg.lower()
        if not is_valid_status(st):
            self.say("[!] Usage: /status <online|away>")
            return
        try:
            self.discovery.set_status(st)
        except ValueError:
            self.say("[!] Usage: /status <online|away>")
            return
        self.say(f"[*] Status set to {st}")

    def _cmd_help(self, _arg: str) -> None:
        self.say("\n".join(HELP_LINES))

    def _cmd_quit(self, _arg: str) -> None:
        self.stop.set()

    def _send_text(self, text: str) -> None:
        if self.state.current is None:
            self.say("[*] No active chat. Use /chat <name|number>")
            return
        cur, name = self.state.current, self._peer_name(self.state.current)
        try:
            self.transport.send_chat(cur, text)
        except NotConnected:
            self.say(f"[!] Not connected to {name}. Use /chat {name}")
            return
        except ValueError as e:
            self.say(f"[!] {e}")
            return
        self.state.add_message(cur, self.name, text)
        self.say(f"[{self.name}] {text}")

    def _handle_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        if not line.startswith("/"):
            self._send_text(line)
            return
        parts = line.split(maxsplit=1)
        cmd, arg = parts[0].lower(), parts[1].strip() if len(parts) > 1 else ""
        handler = self._commands.get(cmd)
        if handler:
            handler(arg)
        else:
            self.say(f"[*] Unknown command: {cmd}. Type /help")

    def _read_key(self) -> str | None:
        if sys.platform == "win32":
            if self._pending:
                return self._pending.pop(0)
            if not msvcrt.kbhit():
                self.stop.wait(KEY_POLL)
                return None
            ch = msvcrt.getwch()
            if ch == "\x00":
                msvcrt.getwch()
                return None
            if ch == "\xe0" and msvcrt.kbhit():
                follow = msvcrt.getwch()
                if follow in _EXTENDED_KEYS:
                    return None
                self._pending.append(follow)
            return ch
        r, _, _ = select.select([sys.stdin], [], [], KEY_POLL)
        return sys.stdin.read(1) if r else None

    def _on_key(self, ch: str) -> bool:
        if ch == "\x03":
            return False
        if ch in ("\x04", "\x1a"):
            return bool(self._input_buf)
        if ch in ("\r", "\n"):
            with self._print_lock:
                clear = self._clear_prompt()
                line = "".join(self._input_buf)
                self._input_buf.clear()
                sys.stdout.write(clear)
                sys.stdout.flush()
            self._handle_line(line)
            if self.stop.is_set():
                return False
            with self._print_lock:
                sys.stdout.write(f"\r\033[K> {''.join(self._input_buf)}")
                sys.stdout.flush()
        elif ch in ("\x08", "\x7f"):
            with self._print_lock:
                if self._input_buf:
                    clear = self._clear_prompt()
                    self._input_buf.pop()
                    sys.stdout.write(f"{clear}> {''.join(self._input_buf)}")
                    sys.stdout.flush()
        elif ch.isprintable():
            with self._print_lock:
                self._input_buf.append(ch)
                sys.stdout.write(ch)
                sys.stdout.flush()
        return True

    def _run_terminal(self) -> None:
        fd = sys.stdin.fileno() if sys.platform != "win32" else 0
        old = termios.tcgetattr(fd) if sys.platform != "win32" else None
        try:
            if sys.platform != "win32":
                tty.setcbreak(fd)
            while not self.stop.is_set():
                ch = self._read_key()
                if ch is not None and not self._on_key(ch):
                    break
        finally:
            if old is not None:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)

    def _run_plain(self) -> None:
        for raw in sys.stdin:
            if self.stop.is_set():
                break
            self._handle_line(raw.rstrip("\r\n"))
            if self.stop.is_set():
                break

    def run(self) -> None:
        self.say(f"[*] Listening on TCP port {self.tcp_port}")
        self.say(f"[*] Broadcasting presence on UDP port {self.udp_port}")
        pump = threading.Thread(target=self._event_pump, daemon=True, name="lanchat-cli-pump")
        pump.start()
        try:
            self._run_terminal() if self._is_terminal() else self._run_plain()
        except KeyboardInterrupt:
            self.stop.set()
        finally:
            self.stop.set()
            self.say("[*] Bye")
            pump.join(timeout=PUMP_JOIN_TIMEOUT)
