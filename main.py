import argparse
import logging
import os
import queue
import sys
import threading

from chat_state import ChatState
from cli import CLI
from discovery import Discovery
from file_transfer import receive_file
from peer_store import PeerStore
from protocol import DEFAULT_TCP_PORT, UDP_PORT, is_valid_name, new_id
from transport import Transport


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    parser.add_argument("--port", type=int, default=DEFAULT_TCP_PORT)
    parser.add_argument("--udp-port", type=int, default=UDP_PORT)
    parser.add_argument("--broadcast", default="255.255.255.255")
    parser.add_argument("--downloads", default="downloads")
    parser.add_argument("--gui", action="store_true", help="open a window instead of the console UI")
    args = parser.parse_args()
    if not is_valid_name(args.name):
        parser.error("invalid name")
    if not (0 <= args.port <= 65535 and 0 <= args.udp_port <= 65535):
        parser.error("ports must be in 0-65535")
    if args.udp_port == 0:
        parser.error("UDP port must be in 1-65535")

    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")
    os.makedirs("logs", exist_ok=True)
    logging.basicConfig(
        filename=f"logs/lanchat-{args.name}.log",
        encoding="utf-8",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    my_id, events, stop = new_id(), queue.Queue(), threading.Event()
    store, state = PeerStore(), ChatState()
    window = None
    if args.gui:
        from gui import GuiTerminal

        window = GuiTerminal(args.name, store, state, stop)

    def on_file(sock, peer_id, peer_name):
        receive_file(sock, peer_id, peer_name, events, args.downloads)

    transport = Transport(my_id, args.name, args.port, events, on_file)
    try:
        transport.start()
    except OSError as e:
        sys.exit(f"[!] cannot listen on TCP port {args.port}: {e}")

    discovery = Discovery(
        my_id, args.name, transport.port, store, events, args.udp_port, args.broadcast
    )
    try:
        discovery.start()
    except OSError as e:
        transport.stop()
        sys.exit(f"[!] cannot listen on UDP port {args.udp_port}: {e}")

    cli = CLI(
        args.name, store, transport, discovery, state, events, stop, transport.port, args.udp_port,
        window,
    )

    def run_ui() -> None:
        try:
            cli.run()
        except Exception:
            logging.exception("Unexpected exception")
            raise

    try:
        window.run(run_ui) if window else run_ui()
    finally:
        discovery.stop()
        transport.stop()
        stop.set()


if __name__ == "__main__":
    main()
