"""Discovery tests: deterministic socket errors and real loopback broadcast."""

from dataclasses import replace
import errno
import queue
import socket
import threading
import time
import unittest
from unittest.mock import patch

import discovery
from discovery import Discovery, build_goodbye, build_hello, parse_datagram
from events import Event
from peer_store import Peer, PeerStore
import protocol


class FakeSocket:
    def __init__(self):
        self.sent = queue.Queue()
        self.incoming = queue.Queue()
        self.closed = False
        self.fail_send = False

    def sendto(self, data, address):
        if self.fail_send or self.closed:
            raise OSError("simulated send failure")
        self.sent.put((data, address, time.monotonic(), threading.get_ident()))
        return len(data)

    def recvfrom(self, size):
        try:
            item = self.incoming.get(timeout=0.05)
        except queue.Empty:
            raise socket.timeout() from None
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True
        self.incoming.put(OSError("closed"))


class DatagramTests(unittest.TestCase):
    def test_shared_validators(self):
        self.assertIs(discovery.is_valid_name, protocol.is_valid_name)
        self.assertIs(discovery.is_valid_peer_id, protocol.is_valid_peer_id)

    def test_hello_and_goodbye(self):
        data = build_hello("a1b2c3d4", "Quan", 5000, "online")
        self.assertEqual(len(data), 31)
        self.assertEqual(parse_datagram(data)["tcp_port"], 5000)
        self.assertEqual(
            parse_datagram(build_goodbye("a1b2c3d4")),
            {"type": "GOODBYE", "peer_id": "a1b2c3d4"},
        )

    def test_utf8(self):
        data = build_hello("a1b2c3d4", "Tuấn", 5000, "away")
        self.assertEqual(parse_datagram(data)["name"], "Tuấn")

    def test_invalid_packets_are_silent(self):
        bad_packets = [
            b"", b"\xff", b"x" * 513, b"HELLO|a1b2c3d4|Quan|5000|online\n",
            b"HELLO|A1B2C3D4|Quan|5000|online",
            b"HELLO|a1b2c3d4|123|5000|online",
            b"HELLO|a1b2c3d4|An Binh|5000|online",
            b"HELLO|a1b2c3d4|An\x00|5000|online",
            b"HELLO|a1b2c3d4|Quan|05000|online",
            b"HELLO|a1b2c3d4|Quan|0|online",
            b"HELLO|a1b2c3d4|Quan|65536|online",
            b"HELLO|a1b2c3d4|Quan|+5000|online",
            b"HELLO|a1b2c3d4|Quan|5000|busy",
            b"GOODBYE|a1b2c3d4|extra", b"OTHER|a1b2c3d4",
        ]
        for packet in bad_packets:
            with self.subTest(packet=packet):
                self.assertIsNone(parse_datagram(packet))

    def test_builders_reject_bad_configuration(self):
        for port in [0, 65536, True, "5000"]:
            with self.subTest(port=port), self.assertRaises(ValueError):
                build_hello("a1b2c3d4", "Quan", port, "online")
        with self.assertRaises(ValueError):
            build_goodbye("bad")
        with self.assertRaises(ValueError):
            build_hello("a1b2c3d4", "Quan", 5000, "busy")


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.store = PeerStore()
        self.events = queue.Queue()
        self.service = Discovery(
            "11223344", "Minh", 5001, self.store, self.events,
            udp_port=19999, broadcast_addr="127.0.0.1",
        )

    def start_fake(self):
        fake = FakeSocket()
        self.service._create_socket = lambda: fake
        self.service.start()
        self.addCleanup(self.service.stop)
        return fake

    def hello(self, **changes):
        fields = dict(
            peer_id="a1b2c3d4", name="Quan", tcp_port=5000, status="online"
        )
        fields.update(changes)
        return build_hello(**fields)

    def test_uses_supplied_store_queue_and_configuration(self):
        self.assertIs(self.service.store, self.store)
        self.assertIs(self.service.events, self.events)
        self.assertEqual(self.service.udp_port, 19999)
        self.assertEqual(self.service.my_id, "11223344")

    def test_new_update_and_refresh_events(self):
        self.service._handle_datagram(self.hello(), "192.168.1.10")
        event = self.events.get_nowait()
        self.assertIsInstance(event, Event)
        self.assertEqual(event.type, "PEER_ONLINE")
        self.assertIsInstance(event.data["peer"], Peer)
        self.service._handle_datagram(self.hello(), "192.168.1.10")
        self.assertTrue(self.events.empty())
        self.service._handle_datagram(
            self.hello(status="away"), "192.168.1.10"
        )
        event = self.events.get_nowait()
        self.assertEqual(event.type, "PEER_UPDATED")
        self.assertEqual(event.data["old"].status, "online")
        self.assertEqual(event.data["peer"].status, "away")

    def test_ip_name_and_port_changes(self):
        self.service._handle_datagram(self.hello(), "192.168.1.10")
        self.events.get_nowait()
        self.service._handle_datagram(
            self.hello(name="Quan2", tcp_port=5002), "192.168.1.11"
        )
        event = self.events.get_nowait()
        self.assertEqual(event.type, "PEER_UPDATED")
        self.assertEqual(event.data["peer"].ip, "192.168.1.11")
        self.assertEqual(event.data["peer"].tcp_port, 5002)

    def test_self_and_malformed_are_ignored(self):
        for data in [
            self.hello(peer_id="11223344"),
            build_goodbye("11223344"), b"bad",
        ]:
            self.service._handle_datagram(data, "127.0.0.1")
        self.assertEqual(self.store.get_peers(), [])
        self.assertTrue(self.events.empty())

    def test_goodbye_and_timeout_reasons(self):
        self.service._handle_datagram(self.hello(), "127.0.0.1")
        self.events.get_nowait()
        self.service._handle_datagram(build_goodbye("a1b2c3d4"), "127.0.0.1")
        event = self.events.get_nowait()
        self.assertEqual(
            (event.type, event.data["reason"]), ("PEER_OFFLINE", "goodbye")
        )
        self.service._handle_datagram(build_goodbye("a1b2c3d4"), "127.0.0.1")
        self.assertTrue(self.events.empty())
        self.service._handle_datagram(self.hello(), "127.0.0.1")
        self.events.get_nowait()
        seen = self.store.get("a1b2c3d4").last_seen
        self.service._expire_peers(now=seen + protocol.PEER_TIMEOUT)
        self.assertTrue(self.events.empty())
        self.service._expire_peers(now=seen + protocol.PEER_TIMEOUT + 1)
        event = self.events.get_nowait()
        self.assertEqual(event.data["reason"], "timeout")

    def test_start_sends_immediately_and_workers_are_daemon(self):
        fake = self.start_fake()
        data, address, _, _ = fake.sent.get(timeout=1)
        self.assertEqual(parse_datagram(data)["peer_id"], "11223344")
        self.assertEqual(address, ("127.0.0.1", 19999))
        self.assertTrue(all(worker.daemon for worker in self.service._threads))

    def test_periodic_hello(self):
        with patch.object(discovery, "HELLO_INTERVAL", 0.05):
            fake = self.start_fake()
            first = fake.sent.get(timeout=1)
            second = fake.sent.get(timeout=1)
            self.assertGreaterEqual(second[2] - first[2], 0.035)
            self.service.stop()

    def test_status_is_immediate_and_rapid_changes_keep_order(self):
        fake = self.start_fake()
        fake.sent.get(timeout=1)
        self.service.set_status("away")
        self.service.set_status("online")
        first = fake.sent.get(timeout=0.7)
        second = fake.sent.get(timeout=0.7)
        self.assertEqual(parse_datagram(first[0])["status"], "away")
        self.assertEqual(parse_datagram(second[0])["status"], "online")
        self.assertEqual(first[3], second[3])
        self.assertNotEqual(first[3], threading.get_ident())

    def test_status_same_value_and_invalid_status(self):
        fake = self.start_fake()
        fake.sent.get(timeout=1)
        self.service.set_status("online")
        self.assertTrue(fake.sent.empty())
        with self.assertRaises(ValueError):
            self.service.set_status("busy")

    def test_stop_order_goodbye_gap_and_idempotence(self):
        fake = self.start_fake()
        fake.sent.get(timeout=1)
        self.service.stop()
        outgoing = []
        while not fake.sent.empty():
            outgoing.append(fake.sent.get_nowait())
        self.assertEqual(
            [packet[0] for packet in outgoing],
            [build_goodbye("11223344")] * protocol.GOODBYE_REPEAT,
        )
        self.assertGreaterEqual(
            outgoing[1][2] - outgoing[0][2], protocol.GOODBYE_GAP - 0.01
        )
        self.assertEqual(outgoing[0][3], outgoing[1][3])
        self.assertTrue(fake.closed)
        self.assertTrue(all(
            not worker.is_alive() for worker in self.service._threads
        ))
        self.service.stop()
        self.assertTrue(fake.sent.empty())
        self.assertTrue(self.events.empty())

    def test_invalid_lifecycle(self):
        self.service.stop()
        # Trước start: lưu trạng thái; HELLO đầu tiên dùng trạng thái đó.
        self.service.set_status("away")
        fake = self.start_fake()
        self.assertEqual(
            parse_datagram(fake.sent.get(timeout=1)[0])["status"], "away"
        )
        with self.assertRaises(RuntimeError):
            self.service.start()
        self.service.stop()
        sent_count = fake.sent.qsize()
        self.service.set_status("online")
        self.assertEqual(self.service._status, "online")
        self.assertEqual(fake.sent.qsize(), sent_count)

    def test_status_during_shutdown_does_not_send(self):
        fake = self.start_fake()
        fake.sent.get(timeout=1)
        with self.service._state_lock:
            self.service._closing = True
        self.service.set_status("away")
        self.assertEqual(self.service._status, "away")
        self.assertTrue(fake.sent.empty())

    def test_bind_error_propagates(self):
        with patch.object(
            self.service, "_create_socket", side_effect=OSError("bind failed")
        ):
            with self.assertRaises(OSError):
                self.service.start()
        self.assertFalse(self.service._started)
        self.assertIsNone(self.service._socket)

    def test_partial_start_cleans_up(self):
        fake = FakeSocket()
        self.service._create_socket = lambda: fake
        original_start = threading.Thread.start
        starts = 0

        def start_or_fail(worker):
            nonlocal starts
            starts += 1
            if starts == 2:
                raise RuntimeError("simulated thread failure")
            return original_start(worker)

        with patch.object(threading.Thread, "start", start_or_fail):
            with self.assertRaises(RuntimeError):
                self.service.start()
        self.assertTrue(fake.closed)
        self.assertTrue(all(
            not worker.is_alive() for worker in self.service._threads
        ))

    def test_udp_errors_become_error_events(self):
        fake = FakeSocket()
        fake.fail_send = True
        self.service._create_socket = lambda: fake
        self.service.start()
        try:
            event = self.events.get(timeout=1)
            self.assertEqual(
                (event.type, event.data["source"]), ("ERROR", "udp")
            )
        finally:
            self.service.stop()

    def test_fatal_receive_failure_stops_workers(self):
        fake = FakeSocket()
        fake.incoming.put(OSError(errno.ENOTSOCK, "invalid socket"))
        self.service._create_socket = lambda: fake
        self.service.start()
        try:
            event = self.events.get(timeout=1)
            self.assertEqual(event.type, "ERROR")
            self.assertEqual(event.data["source"], "udp")
            self.assertTrue(self.service._stop_event.wait(timeout=1))
        finally:
            self.service.stop()
        self.assertTrue(all(
            not worker.is_alive() for worker in self.service._threads
        ))

    def test_reset_does_not_stop_listener(self):
        fake = self.start_fake()
        fake.sent.get(timeout=1)
        fake.incoming.put(ConnectionResetError(errno.ECONNRESET, "ICMP"))
        fake.incoming.put((self.hello(), ("127.0.0.1", 9999)))
        event = self.events.get(timeout=2)
        self.assertEqual(event.type, "PEER_ONLINE")
        self.assertFalse(self.service._stop_event.is_set())
        self.assertIsNotNone(self.store.get("a1b2c3d4"))

    def test_receive_outage_reports_once_then_recovers(self):
        fake = self.start_fake()
        fake.sent.get(timeout=1)
        for _ in range(2):
            fake.incoming.put(OSError(errno.ENETDOWN, "network down"))
        fake.incoming.put((self.hello(), ("127.0.0.1", 9999)))
        self.assertEqual(self.events.get(timeout=2).type, "ERROR")
        self.assertEqual(self.events.get(timeout=2).type, "PEER_ONLINE")
        self.assertTrue(self.events.empty())
        self.assertFalse(self.service._stop_event.is_set())
        fake.incoming.put(OSError(errno.ENETDOWN, "network down again"))
        fake.incoming.put((self.hello(status="away"), ("127.0.0.1", 9999)))
        self.assertEqual(self.events.get(timeout=2).type, "ERROR")
        self.assertEqual(self.events.get(timeout=2).type, "PEER_UPDATED")

    def test_receive_retry_waits_instead_of_spinning(self):
        fake = FakeSocket()
        for _ in range(3):
            fake.incoming.put(OSError(errno.ENETDOWN, "network down"))
        self.service._socket = fake
        waits = []

        def retry_wait(seconds):
            waits.append(seconds)
            if len(waits) == 3:
                self.service._stop_event.set()
                return True
            return False

        with patch.object(self.service._stop_event, "wait", retry_wait):
            self.service._listen_loop()
        self.assertEqual(waits, [protocol.UDP_RETRY_INTERVAL] * 3)
        self.assertEqual(self.events.qsize(), 1)
        fake.close()

    def test_send_errors_report_once_until_success(self):
        fake = FakeSocket()
        self.service._socket = fake
        fake.fail_send = True
        for _ in range(3):
            self.service._send_datagram(b"probe")
        self.assertEqual(self.events.qsize(), 1)
        self.assertEqual(self.events.get_nowait().type, "ERROR")
        fake.fail_send = False
        self.service._send_datagram(b"probe")
        self.assertTrue(self.events.empty())
        fake.fail_send = True
        self.service._send_datagram(b"probe")
        self.assertEqual(self.events.get_nowait().type, "ERROR")
        self.assertTrue(self.events.empty())
        fake.close()

    def test_retry_is_interruptible_during_stop(self):
        fake = self.start_fake()
        fake.sent.get(timeout=1)
        fake.incoming.put(OSError(errno.ENETDOWN, "network down"))
        self.assertEqual(self.events.get(timeout=1).type, "ERROR")
        started = time.monotonic()
        self.service.stop()
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertTrue(all(
            not worker.is_alive() for worker in self.service._threads
        ))

    def test_real_udp_localhost(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sock.settimeout(0.1)
        self.service.udp_port = sock.getsockname()[1]
        self.service._create_socket = lambda: sock
        self.service.start()
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                address = ("127.0.0.1", self.service.udp_port)
                sender.sendto(b"bad", address)
                sender.sendto(b"x" * (protocol.MAX_DATAGRAM + 100), address)
                sender.sendto(self.hello(), address)
                event = self.events.get(timeout=2)
                self.assertEqual(event.type, "PEER_ONLINE")
                self.assertEqual(event.data["peer"].ip, "127.0.0.1")
                sender.sendto(self.hello(status="away"), address)
                self.assertEqual(
                    self.events.get(timeout=2).type, "PEER_UPDATED"
                )
                sender.sendto(build_goodbye("a1b2c3d4"), address)
                event = self.events.get(timeout=2)
                self.assertEqual(event.data["reason"], "goodbye")
                self.assertEqual(self.store.get_peers(), [])
        finally:
            self.service.stop()

    def test_two_instances_broadcast_loopback(self):
        # Cùng cổng UDP thử nghiệm, tránh đụng cổng 9999 đang demo.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        other_store = PeerStore()
        other_events = queue.Queue()
        other = Discovery(
            "a1b2c3d4", "Quan", 5002, other_store, other_events,
            port, "127.255.255.255",
        )
        self.service.udp_port = port
        self.service.broadcast_addr = "127.255.255.255"
        with patch.object(discovery, "HELLO_INTERVAL", 0.1):
            try:
                self.service.start()
                other.start()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    if (
                        self.store.get(other.my_id) is not None
                        and other_store.get(self.service.my_id) is not None
                    ):
                        break
                    time.sleep(0.01)
                self.assertEqual(
                    [p.peer_id for p in self.store.get_peers()], [other.my_id]
                )
                self.assertEqual(
                    [p.peer_id for p in other_store.get_peers()],
                    [self.service.my_id],
                )
                self.assertEqual(
                    self.events.get(timeout=1).type, "PEER_ONLINE"
                )
                self.assertEqual(
                    other_events.get(timeout=1).type, "PEER_ONLINE"
                )
                other.set_status("away")
                event = self.events.get(timeout=1)
                self.assertEqual(event.type, "PEER_UPDATED")
                self.assertEqual(event.data["old"].status, "online")
                self.assertEqual(event.data["peer"].status, "away")
                other.stop()
                event = self.events.get(timeout=1)
                self.assertEqual(event.type, "PEER_OFFLINE")
                self.assertEqual(event.data["reason"], "goodbye")
                self.assertIsNone(self.store.get(other.my_id))
            finally:
                other.stop()
                self.service.stop()

if __name__ == "__main__":
    unittest.main()
