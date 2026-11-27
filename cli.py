from __future__ import annotations

import os
import queue
import threading

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
from peer_store import Peer, PeerStore
from protocol import is_valid_status
from terminal import Terminal
from transport import ConnectError, NotConnected, Transport

QUEUE_POLL = 0.2
PUMP_JOIN_TIMEOUT = 0.5
NO_CHAT = "[*] No active chat. Use /chat <name|number>"
CLOSE_REASONS = {
    "bye": "{} closed the chat",
    "lost": "Connection to {} lost",
    "protocol_error": "Connection to {} closed: protocol error",
}
HELP = """\
/list                  show online users (number, name, IP, status)
/chat <name|number>    chat with a user (number from the last /list)
<text>                 send a message to the current chat
/send <file>           send a file to the current chat
/leave                 close the current chat (unread messages are kept)
/status <online|away>  change your status
/help                  show this list
/quit                  exit"""


def _format_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    return f"{n / 1024:.1f} KB" if n < 1024 * 1024 else f"{n / 1024 / 1024:.1f} MB"


class CLI:
    def __init__(
        self, name: str, store: PeerStore, transport: Transport, discovery: Discovery,
        state: ChatState, events: queue.Queue, stop: threading.Event, tcp_port: int, udp_port: int,
        term: Terminal | None = None,
    ) -> None:
        self.name, self.store, self.transport = name, store, transport
        self.discovery, self.state, self.events = discovery, state, events
        self.stop, self.tcp_port, self.udp_port = stop, tcp_port, udp_port
        self.term = term or Terminal(stop)
        self.say = self.term.say
        self._listing: list[str] = []
        self._progress: dict[str, int] = {}
        self._commands = {
            "/list": self._cmd_list, "/chat": self._cmd_chat, "/send": self._cmd_send,
            "/leave": self._cmd_leave, "/status": self._cmd_status,
            "/help": lambda _: self.say(HELP), "/quit": lambda _: self.stop.set(),
        }

    def _peer_name(self, pid: str) -> str:
        peer = self.store.get(pid)
        return peer.name if peer else pid

    def _show(self, event: Event) -> None:
        t, d = event.type, event.data
        if t == PEER_ONLINE:
            self.say(f"[*] {d['peer'].name} is online")
        elif t == PEER_OFFLINE:
            self.say(f"[*] {d['peer'].name} is offline")
        elif t == PEER_UPDATED and d["old"].status != d["peer"].status:
            self.say(f"[*] {d['peer'].name} is now {d['peer'].status}")
        elif t == CHAT_CONNECTED and d.get("incoming"):
            self.say(f"[*] {d['name']} opened a chat with you")
        elif t == CHAT_RECEIVED:
            pid, name, text = d["peer_id"], d["name"], d["text"]
            if self.state.add_message(pid, name, text):
                self.say(f"[{name}] {text}")
            else:
                self.say(f"[*] New message from {name} ({self.state.unread(pid)} unread)")
        elif t == CONNECTION_CLOSED and d.get("reason") in CLOSE_REASONS:
            name = d.get("name") or self._peer_name(d.get("peer_id", ""))
            self.say("[*] " + CLOSE_REASONS[d["reason"]].format(name))
        elif t == FILE_PROGRESS:
            self._show_progress(d)
        elif t in (FILE_COMPLETED, FILE_UNCONFIRMED):
            self._progress.pop(d["file_id"], None)
            if t == FILE_UNCONFIRMED:
                self.say(f"[!] Sent 100% of {d['filename']} but the receiver did not confirm it")
            elif d["direction"] == "send":
                self.say(f"[*] Sent {d['filename']} ... 100%  Done.")
            else:
                self.say(f"[*] Received {d['filename']} -> {d.get('path', '')}")
        elif t == ERROR:
            self.say(f"[!] {d.get('message', '')}")

    def _show_progress(self, d: dict) -> None:
        fid, name = d["file_id"], d["filename"]
        if fid not in self._progress:
            self._progress[fid] = 0
            verb = "Sending" if d["direction"] == "send" else "Receiving"
            self.say(f"[*] {verb} {name} ({_format_size(d['total'])}) ...")
            return
        tens = min(d["done"] * 10 // d["total"], 9)
        if tens > self._progress[fid]:
            self._progress[fid] = tens
            self.say(f"[*] {name}  {tens * 10}%")

    def _event_pump(self) -> None:
        while not self.stop.is_set():
            try:
                self._show(self.events.get(timeout=QUEUE_POLL))
            except queue.Empty:
                pass

    def _cmd_list(self, _arg: str = "") -> None:
        peers = self.store.get_peers()
        self._listing = [p.peer_id for p in peers]
        if not peers:
            self.say("[*] No one else is online")
            return
        w = max(len(p.name) for p in peers)
        rows = [f"{i:>2}. {p.name:<{w}}  ({p.ip})  {p.status}" for i, p in enumerate(peers, 1)]
        self.say("\n".join(rows))

    def _pick(self, arg: str) -> Peer | None:
        if arg.isdecimal():
            ids = self._listing or [p.peer_id for p in self.store.get_peers()]
            n = int(arg)
            peer = self.store.get(ids[n - 1]) if 1 <= n <= len(ids) else None
            if peer is None:
                self.say(f"[!] No user number {n}. Use /list")
            return peer
        matches = self.store.find_by_name(arg)
        if not matches:
            self.say(f"[!] No such user: {arg}")
        elif len(matches) > 1:
            self._cmd_list()
            self.say(f"[!] {len(matches)} users named {arg}, use /chat <number>")
        return matches[0] if len(matches) == 1 else None

    def _cmd_chat(self, arg: str) -> None:
        if not arg:
            self.say("[!] Usage: /chat <name|number>")
            return
        peer = self._pick(arg)
        if peer is None:
            return
        if self.transport.is_connected(peer.peer_id):
            self.say(f"[*] Switched to {peer.name}")
        else:
            try:
                self.transport.connect_chat(peer)
            except ConnectError as e:
                self.say(f"[!] {e}")
                return
            self.say(f"[*] Connected to {peer.name} ({peer.ip}:{peer.tcp_port})")
        unread = self.state.switch_to(peer.peer_id)
        if unread:
            rows = [f"[*] {len(unread)} unread:"] + [f"[{m.sender}] {m.text}" for m in unread]
            self.say("\n".join(rows))

    def _cmd_send(self, arg: str) -> None:
        path, cur = arg.strip("\"'"), self.state.current
        peer = self.store.get(cur) if cur else None
        if not path:
            self.say("[!] Usage: /send <file>")
        elif cur is None:
            self.say(NO_CHAT)
        elif peer is None:
            self.say(f"[!] {cur} is offline, cannot send a file")
        elif not (os.path.isfile(path) and os.access(path, os.R_OK)):
            self.say(f"[!] File not found: {path}")
        else:
            file_transfer.start_send(self.transport, peer, path, self.events)

    def _cmd_leave(self, _arg: str) -> None:
        cur = self.state.current
        if cur is None:
            self.say("[*] No active chat")
            return
        name = self._peer_name(cur)
        self.transport.disconnect_chat(cur)
        self.state.leave()
        self.say(f"[*] Chat with {name} closed")

    def _cmd_status(self, arg: str) -> None:
        status = arg.lower()
        if not is_valid_status(status):
            self.say("[!] Usage: /status <online|away>")
            return
        self.discovery.set_status(status)
        self.say(f"[*] Status set to {status}")

    def _send_text(self, text: str) -> None:
        cur = self.state.current
        if cur is None:
            self.say(NO_CHAT)
            return
        try:
            self.transport.send_chat(cur, text)
        except NotConnected:
            name = self._peer_name(cur)
            self.say(f"[!] Not connected to {name}. Use /chat {name}")
            return
        except ValueError as e:
            self.say(f"[!] {e}")
            return
        self.state.add_message(cur, self.name, text)
        self.say(f"[{self.name}] {text}")

    def _handle_line(self, line: str) -> None:
        line = line.strip()
        if not line.startswith("/"):
            if line:
                self._send_text(line)
            return
        cmd, _, arg = line.partition(" ")
        handler = self._commands.get(cmd.lower())
        if handler:
            handler(arg.strip())
        else:
            self.say(f"[*] Unknown command: {cmd.lower()}. Type /help")

    def run(self) -> None:
        self.say(f"[*] Listening on TCP port {self.tcp_port}")
        self.say(f"[*] Broadcasting presence on UDP port {self.udp_port}")
        pump = threading.Thread(target=self._event_pump, daemon=True)
        pump.start()
        try:
            for line in self.term.lines():
                self._handle_line(line)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop.set()
            self.say("[*] Bye")
            pump.join(timeout=PUMP_JOIN_TIMEOUT)
