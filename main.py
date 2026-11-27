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
from protocol import DEFAULT_BROADCAST, DEFAULT_TCP_PORT, MAX_PORT, UDP_PORT, is_valid_name, new_id
from transport import Transport


def _port(text: str) -> int:
    try:
        port = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid port: {text}")
    if not 0 <= port <= MAX_PORT:
        raise argparse.ArgumentTypeError(f"port must be 0-{MAX_PORT}: {text}")
    return port


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    parser.add_argument("--port", type=_port, default=DEFAULT_TCP_PORT)
    parser.add_argument("--udp-port", type=_port, default=UDP_PORT)
    parser.add_argument("--broadcast", default=DEFAULT_BROADCAST)
    parser.add_argument("--downloads", default="downloads")
    args = parser.parse_args()

    if not is_valid_name(args.name):
        parser.error("invalid name")

    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")

    os.makedirs("logs", exist_ok=True)
    logging.basicConfig(
        filename=f"logs/lanchat-{args.name}.log",
        encoding="utf-8",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logging.info("Starting LAN Chat")

    my_id = new_id()
    events = queue.Queue()
    stop = threading.Event()
    store = PeerStore()
    state = ChatState()

    transport = Transport(
        my_id,
        args.name,
        args.port,
        events,
        lambda sock, peer_id, peer_name: receive_file(
            sock, peer_id, peer_name, events, args.downloads
        ),
    )
    try:
        transport.start()
    except OSError as e:
        sys.exit(f"[!] cannot listen on TCP port {args.port}: {e}")

    discovery = Discovery(
        my_id,
        args.name,
        transport.port,
        store,
        events,
        args.udp_port,
        args.broadcast,
    )
    try:
        discovery.start()
    except OSError as e:
        transport.stop()
        sys.exit(f"[!] cannot listen on UDP port {args.udp_port}: {e}")

    cli = CLI(
        my_id,
        args.name,
        store,
        transport,
        discovery,
        state,
        events,
        stop,
        transport.port,
        args.udp_port,
        args.downloads,
    )
    try:
        cli.run()
    except Exception:
        logging.exception("Unexpected exception")
        raise
    finally:
        discovery.stop()
        transport.stop()
        stop.set()
        logging.info("Stopping LAN Chat")


if __name__ == "__main__":
    main()
