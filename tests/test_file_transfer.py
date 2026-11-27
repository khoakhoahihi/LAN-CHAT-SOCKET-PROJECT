import contextlib
import hashlib
import os
import queue
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

from events import ERROR, FILE_COMPLETED, FILE_PROGRESS, FILE_UNCONFIRMED, Event
from file_transfer import ConnectError, receive_file, send_file, start_send
from peer_store import Peer
from protocol import MAX_FILE_SIZE, recv_frame, send_frame


def make_pair() -> tuple[socket.socket, socket.socket]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.connect(("127.0.0.1", port))
    server, _ = listener.accept()
    listener.close()
    return client, server


def drain_events(q: queue.Queue) -> list[Event]:
    events: list[Event] = []
    while not q.empty():
        try:
            events.append(q.get_nowait())
        except queue.Empty:
            break
    return events


def file_sha256(path: str) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


class TestFileTransfer(unittest.TestCase):
    def test_round_trip_small_file(self) -> None:
        with tempfile.TemporaryDirectory() as src_dir, tempfile.TemporaryDirectory() as dst_dir:
            file_path = os.path.join(src_dir, "small.txt")
            data = b"Hello, LAN Chat file transfer!" * 30
            with open(file_path, "wb") as f:
                f.write(data)
            s_client, s_server = make_pair()
            send_q: queue.Queue = queue.Queue()
            recv_q: queue.Queue = queue.Queue()
            recv_thread = threading.Thread(
                target=receive_file,
                args=(s_server, "peer1", "Alice", recv_q, dst_dir),
                daemon=True,
            )
            recv_thread.start()
            send_file(s_client, file_path, "peer2", send_q)
            recv_thread.join(timeout=5)
            self.assertFalse(recv_thread.is_alive())
            out_file = os.path.join(dst_dir, "small.txt")
            self.assertTrue(os.path.exists(out_file))
            self.assertEqual(file_sha256(file_path), file_sha256(out_file))
            s_events = drain_events(send_q)
            r_events = drain_events(recv_q)
            s_completed = [e for e in s_events if e.type == FILE_COMPLETED]
            r_completed = [e for e in r_events if e.type == FILE_COMPLETED]
            self.assertEqual(len(s_completed), 1)
            self.assertEqual(len(r_completed), 1)
            self.assertEqual(s_completed[0].data["direction"], "send")
            self.assertEqual(r_completed[0].data["direction"], "recv")
            self.assertEqual(r_completed[0].data["path"], out_file)
            s_prog = [e for e in s_events if e.type == FILE_PROGRESS]
            r_prog = [e for e in r_events if e.type == FILE_PROGRESS]
            self.assertGreater(len(s_prog), 0)
            self.assertGreater(len(r_prog), 0)

    def test_round_trip_3mb_file(self) -> None:
        with tempfile.TemporaryDirectory() as src_dir, tempfile.TemporaryDirectory() as dst_dir:
            file_path = os.path.join(src_dir, "random_3mb.bin")
            data = os.urandom(3 * 1024 * 1024)
            with open(file_path, "wb") as f:
                f.write(data)
            s_client, s_server = make_pair()
            send_q: queue.Queue = queue.Queue()
            recv_q: queue.Queue = queue.Queue()
            recv_thread = threading.Thread(
                target=receive_file,
                args=(s_server, "peer1", "Alice", recv_q, dst_dir),
                daemon=True,
            )
            recv_thread.start()
            send_file(s_client, file_path, "peer2", send_q)
            recv_thread.join(timeout=10)
            self.assertFalse(recv_thread.is_alive())
            out_file = os.path.join(dst_dir, "random_3mb.bin")
            self.assertTrue(os.path.exists(out_file))
            self.assertEqual(file_sha256(file_path), file_sha256(out_file))
            s_events = drain_events(send_q)
            r_events = drain_events(recv_q)
            s_completed = [e for e in s_events if e.type == FILE_COMPLETED]
            r_completed = [e for e in r_events if e.type == FILE_COMPLETED]
            self.assertEqual(len(s_completed), 1)
            self.assertEqual(len(r_completed), 1)

    def test_zero_byte_file(self) -> None:
        with tempfile.TemporaryDirectory() as src_dir, tempfile.TemporaryDirectory() as dst_dir:
            file_path = os.path.join(src_dir, "empty.txt")
            open(file_path, "wb").close()
            s_client, s_server = make_pair()
            send_q: queue.Queue = queue.Queue()
            recv_q: queue.Queue = queue.Queue()
            recv_thread = threading.Thread(
                target=receive_file,
                args=(s_server, "peer1", "Alice", recv_q, dst_dir),
                daemon=True,
            )
            recv_thread.start()
            send_file(s_client, file_path, "peer2", send_q)
            recv_thread.join(timeout=5)
            self.assertFalse(recv_thread.is_alive())
            out_file = os.path.join(dst_dir, "empty.txt")
            self.assertTrue(os.path.exists(out_file))
            self.assertEqual(os.path.getsize(out_file), 0)
            s_completed = [e for e in drain_events(send_q) if e.type == FILE_COMPLETED]
            r_completed = [e for e in drain_events(recv_q) if e.type == FILE_COMPLETED]
            self.assertEqual(len(s_completed), 1)
            self.assertEqual(len(r_completed), 1)

    def test_name_sanitization_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as dst_dir:
            s_client, s_server = make_pair()
            try:
                recv_q: queue.Queue = queue.Queue()
                recv_thread = threading.Thread(
                    target=receive_file,
                    args=(s_server, "peer1", "Alice", recv_q, dst_dir),
                    daemon=True,
                )
                recv_thread.start()
                send_frame(s_client, "FILE_META", b"a1b2c3d4|5|../../x.txt")
                s_client.sendall(b"hello")
                recv_thread.join(timeout=5)
                self.assertFalse(recv_thread.is_alive())
                out_file = os.path.join(dst_dir, "x.txt")
                self.assertTrue(os.path.exists(out_file))
                with open(out_file, "rb") as f:
                    self.assertEqual(f.read(), b"hello")
                self.assertFalse(os.path.exists(os.path.join(dst_dir, "..", "x.txt")))
            finally:
                with contextlib.suppress(OSError):
                    s_client.close()

    def test_name_sanitization_backslashes(self) -> None:
        with tempfile.TemporaryDirectory() as dst_dir:
            s_client, s_server = make_pair()
            try:
                recv_q: queue.Queue = queue.Queue()
                recv_thread = threading.Thread(
                    target=receive_file,
                    args=(s_server, "peer1", "Alice", recv_q, dst_dir),
                    daemon=True,
                )
                recv_thread.start()
                send_frame(s_client, "FILE_META", b"a1b2c3d4|5|a\\b\\c.txt")
                s_client.sendall(b"world")
                recv_thread.join(timeout=5)
                self.assertFalse(recv_thread.is_alive())
                out_file = os.path.join(dst_dir, "c.txt")
                self.assertTrue(os.path.exists(out_file))
                with open(out_file, "rb") as f:
                    self.assertEqual(f.read(), b"world")
            finally:
                with contextlib.suppress(OSError):
                    s_client.close()

    def test_duplicate_filename_renaming(self) -> None:
        with tempfile.TemporaryDirectory() as dst_dir:
            for expected_name, content in [
                ("doc.txt", b"v1"),
                ("doc (1).txt", b"v2"),
                ("doc (2).txt", b"v3"),
            ]:
                s_client, s_server = make_pair()
                try:
                    recv_q: queue.Queue = queue.Queue()
                    recv_thread = threading.Thread(
                        target=receive_file,
                        args=(s_server, "peer1", "Alice", recv_q, dst_dir),
                        daemon=True,
                    )
                    recv_thread.start()
                    send_frame(
                        s_client,
                        "FILE_META",
                        f"a1b2c3d4|{len(content)}|doc.txt".encode("utf-8"),
                    )
                    s_client.sendall(content)
                    recv_thread.join(timeout=5)
                    self.assertFalse(recv_thread.is_alive())
                    out_path = os.path.join(dst_dir, expected_name)
                    self.assertTrue(os.path.exists(out_path))
                    with open(out_path, "rb") as f:
                        self.assertEqual(f.read(), content)
                finally:
                    with contextlib.suppress(OSError):
                        s_client.close()

    def test_sender_closes_halfway(self) -> None:
        with tempfile.TemporaryDirectory() as dst_dir:
            s_client, s_server = make_pair()
            recv_q: queue.Queue = queue.Queue()
            recv_thread = threading.Thread(
                target=receive_file,
                args=(s_server, "peer1", "Alice", recv_q, dst_dir),
                daemon=True,
            )
            recv_thread.start()
            send_frame(s_client, "FILE_META", b"a1b2c3d4|1000|interrupted.bin")
            s_client.sendall(b"X" * 100)
            s_client.close()
            recv_thread.join(timeout=5)
            self.assertFalse(recv_thread.is_alive())
            part_file = os.path.join(dst_dir, "interrupted.bin.part")
            final_file = os.path.join(dst_dir, "interrupted.bin")
            self.assertFalse(os.path.exists(part_file))
            self.assertFalse(os.path.exists(final_file))
            r_events = drain_events(recv_q)
            err_events = [e for e in r_events if e.type == ERROR]
            self.assertEqual(len(err_events), 1)
            self.assertEqual(err_events[0].data["source"], "file")

    def test_size_above_max_file_size(self) -> None:
        with tempfile.TemporaryDirectory() as dst_dir:
            s_client, s_server = make_pair()
            try:
                recv_q: queue.Queue = queue.Queue()
                recv_thread = threading.Thread(
                    target=receive_file,
                    args=(s_server, "peer1", "Alice", recv_q, dst_dir),
                    daemon=True,
                )
                recv_thread.start()
                bad_size = MAX_FILE_SIZE + 1
                send_frame(
                    s_client,
                    "FILE_META",
                    f"a1b2c3d4|{bad_size}|big.bin".encode("utf-8"),
                )
                ftype, payload = recv_frame(s_client)
                recv_thread.join(timeout=5)
                self.assertFalse(recv_thread.is_alive())
                self.assertEqual(ftype, "FILE_ERR")
                parts = payload.decode("utf-8").split("|", 2)
                self.assertEqual(parts[1], "TOO_LARGE")
                self.assertEqual(len(os.listdir(dst_dir)), 0)
                r_events = drain_events(recv_q)
                err_events = [e for e in r_events if e.type == ERROR]
                self.assertEqual(len(err_events), 1)
            finally:
                with contextlib.suppress(OSError):
                    s_client.close()

    def test_garbage_first_frame(self) -> None:
        with tempfile.TemporaryDirectory() as dst_dir:
            s_client, s_server = make_pair()
            try:
                recv_q: queue.Queue = queue.Queue()
                recv_thread = threading.Thread(
                    target=receive_file,
                    args=(s_server, "peer1", "Alice", recv_q, dst_dir),
                    daemon=True,
                )
                recv_thread.start()
                send_frame(s_client, "MSG", b"not file meta")
                ftype, payload = recv_frame(s_client)
                recv_thread.join(timeout=5)
                self.assertFalse(recv_thread.is_alive())
                self.assertEqual(ftype, "FILE_ERR")
                parts = payload.decode("utf-8").split("|", 2)
                self.assertEqual(parts[1], "BAD_META")
                r_events = drain_events(recv_q)
                err_events = [e for e in r_events if e.type == ERROR]
                self.assertEqual(len(err_events), 1)
            finally:
                with contextlib.suppress(OSError):
                    s_client.close()

    def test_fake_receiver_wrong_hash(self) -> None:
        with tempfile.TemporaryDirectory() as src_dir:
            file_path = os.path.join(src_dir, "test.txt")
            with open(file_path, "wb") as f:
                f.write(b"content")
            s_client, s_server = make_pair()
            send_q: queue.Queue = queue.Queue()

            def fake_receiver() -> None:
                _, payload = recv_frame(s_server)
                file_id = payload.decode("utf-8").split("|")[0]
                s_server.recv(7)
                send_frame(
                    s_server,
                    "FILE_DONE",
                    f"{file_id}|{'0' * 64}".encode("ascii"),
                )
                s_server.close()

            t = threading.Thread(target=fake_receiver, daemon=True)
            t.start()
            send_file(s_client, file_path, "peer1", send_q)
            t.join(timeout=5)
            events = drain_events(send_q)
            errs = [e for e in events if e.type == ERROR]
            self.assertEqual(len(errs), 1)
            self.assertIn("file arrived corrupted", errs[0].data["message"])

    @mock.patch("file_transfer.FILE_DONE_TIMEOUT", 0.1)
    def test_fake_receiver_no_answer(self) -> None:
        with tempfile.TemporaryDirectory() as src_dir:
            file_path = os.path.join(src_dir, "test.txt")
            with open(file_path, "wb") as f:
                f.write(b"content")
            s_client, s_server = make_pair()
            send_q: queue.Queue = queue.Queue()

            def fake_receiver() -> None:
                recv_frame(s_server)
                s_server.recv(7)
                time.sleep(0.3)
                s_server.close()

            t = threading.Thread(target=fake_receiver, daemon=True)
            t.start()
            send_file(s_client, file_path, "peer1", send_q)
            t.join(timeout=5)
            events = drain_events(send_q)
            self.assertEqual([e for e in events if e.type == ERROR], [])
            unconfirmed = [e for e in events if e.type == FILE_UNCONFIRMED]
            self.assertEqual(len(unconfirmed), 1)
            self.assertEqual(unconfirmed[0].data["filename"], "test.txt")
            self.assertEqual(len([e for e in events if e.type == FILE_COMPLETED]), 0)

    def test_send_missing_path(self) -> None:
        s_client, s_server = make_pair()
        send_q: queue.Queue = queue.Queue()
        try:
            send_file(s_client, "nonexistent_file_path.xyz", "peer1", send_q)
            events = drain_events(send_q)
            errs = [e for e in events if e.type == ERROR]
            self.assertEqual(len(errs), 1)
            self.assertEqual(errs[0].data["source"], "file")
            self.assertIn("cannot read", errs[0].data["message"])
            s_server.settimeout(0.1)
            self.assertEqual(s_server.recv(1), b"")
        finally:
            with contextlib.suppress(OSError):
                s_server.close()

    def test_start_send_connect_error(self) -> None:
        class DummyTransport:
            def open_file_connection(self, peer: Peer) -> socket.socket:
                raise ConnectError("connection refused")

        events: queue.Queue = queue.Queue()
        peer = Peer("a1b2c3d4", "Alice", "127.0.0.1", 5000, "online", 0.0)
        with tempfile.TemporaryDirectory() as src_dir:
            path = os.path.join(src_dir, "any_path.txt")
            with open(path, "wb") as f:
                f.write(b"x")
            start_send(DummyTransport(), peer, path, events)
            time.sleep(0.1)
        evs = drain_events(events)
        errs = [e for e in evs if e.type == ERROR]
        self.assertEqual(len(errs), 1)
        self.assertEqual(errs[0].data["source"], "file")
        self.assertEqual(errs[0].data["message"], "connection refused")

    def test_start_send_success(self) -> None:
        s_client, s_server = make_pair()

        class DummyTransport:
            def open_file_connection(self, peer: Peer) -> socket.socket:
                return s_client

        with tempfile.TemporaryDirectory() as src_dir, tempfile.TemporaryDirectory() as dst_dir:
            file_path = os.path.join(src_dir, "test.txt")
            with open(file_path, "wb") as f:
                f.write(b"data")
            send_q: queue.Queue = queue.Queue()
            recv_q: queue.Queue = queue.Queue()
            peer = Peer("a1b2c3d4", "Alice", "127.0.0.1", 5000, "online", 0.0)
            recv_thread = threading.Thread(
                target=receive_file,
                args=(s_server, "peer1", "Alice", recv_q, dst_dir),
                daemon=True,
            )
            recv_thread.start()
            start_send(DummyTransport(), peer, file_path, send_q)
            recv_thread.join(timeout=5)
            self.assertFalse(recv_thread.is_alive())
            out_file = os.path.join(dst_dir, "test.txt")
            self.assertTrue(os.path.exists(out_file))

    def test_extra_data_after_file(self) -> None:
        with tempfile.TemporaryDirectory() as dst_dir:
            s_client, s_server = make_pair()
            try:
                recv_q: queue.Queue = queue.Queue()
                recv_thread = threading.Thread(
                    target=receive_file,
                    args=(s_server, "peer1", "Alice", recv_q, dst_dir),
                    daemon=True,
                )
                recv_thread.start()
                send_frame(s_client, "FILE_META", b"a1b2c3d4|5|extra.txt")
                s_client.sendall(b"12345" + b"X")
                ftype, payload = recv_frame(s_client)
                recv_thread.join(timeout=5)
                self.assertFalse(recv_thread.is_alive())
                self.assertEqual(ftype, "FILE_ERR")
                parts = payload.decode("utf-8").split("|", 2)
                self.assertEqual(parts[1], "IO_ERROR")
                self.assertFalse(os.path.exists(os.path.join(dst_dir, "extra.txt.part")))
                self.assertFalse(os.path.exists(os.path.join(dst_dir, "extra.txt")))
                r_events = drain_events(recv_q)
                errs = [e for e in r_events if e.type == ERROR]
                self.assertEqual(len(errs), 1)
            finally:
                with contextlib.suppress(OSError):
                    s_client.close()


def meta_exchange(meta: bytes, dst_dir: str, body: bytes = b"") -> tuple[str, bytes, list[Event]]:
    s_client, s_server = make_pair()
    recv_q: queue.Queue = queue.Queue()
    t = threading.Thread(
        target=receive_file, args=(s_server, "peer1", "Alice", recv_q, dst_dir), daemon=True
    )
    t.start()
    try:
        send_frame(s_client, "FILE_META", meta)
        if body:
            s_client.sendall(body)
        s_client.settimeout(5)
        ftype, payload = recv_frame(s_client)
    finally:
        s_client.close()
    t.join(timeout=5)
    return ftype, payload, drain_events(recv_q)


class TestFileTransferReview(unittest.TestCase):
    def test_start_send_missing_file_opens_no_connection(self) -> None:
        opened = []

        class DummyTransport:
            def open_file_connection(self, peer: Peer) -> socket.socket:
                opened.append(peer)
                raise ConnectError("must not be called")

        events: queue.Queue = queue.Queue()
        peer = Peer("a1b2c3d4", "Alice", "127.0.0.1", 5000, "online", 0.0)
        start_send(DummyTransport(), peer, "no_such_file_here.bin", events)
        time.sleep(0.2)
        errs = [e for e in drain_events(events) if e.type == ERROR]
        self.assertEqual(opened, [])
        self.assertEqual(len(errs), 1)
        self.assertIn("cannot read", errs[0].data["message"])

    def test_drive_prefix_stays_inside_downloads(self) -> None:
        with tempfile.TemporaryDirectory() as dst_dir:
            ftype, _, _ = meta_exchange(b"a1b2c3d4|3|C:pwned.txt", dst_dir, b"abc")
            self.assertEqual(ftype, "FILE_DONE")
            self.assertEqual(os.listdir(dst_dir), ["C_pwned.txt"])

    def test_pipe_in_filename(self) -> None:
        with tempfile.TemporaryDirectory() as dst_dir:
            ftype, _, _ = meta_exchange(b"a1b2c3d4|3|foo|bar.txt", dst_dir, b"abc")
            self.assertEqual(ftype, "FILE_DONE")
            self.assertEqual(os.listdir(dst_dir), ["foo_bar.txt"])

    def test_255_byte_filename_accepted_256_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as dst_dir:
            ok = "a" * 251 + ".txt"
            ftype, _, _ = meta_exchange(f"a1b2c3d4|1|{ok}".encode(), dst_dir, b"x")
            self.assertEqual(ftype, "FILE_DONE")
            bad = "a" * 252 + ".txt"
            ftype, payload, _ = meta_exchange(f"a1b2c3d4|1|{bad}".encode(), dst_dir)
            self.assertEqual(ftype, "FILE_ERR")
            self.assertIn(b"BAD_META", payload)

    def test_blank_filenames_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as dst_dir:
            for name in ("   ", " . ", "...", "dir/", "dir\\"):
                ftype, payload, _ = meta_exchange(f"a1b2c3d4|1|{name}".encode(), dst_dir)
                self.assertEqual(ftype, "FILE_ERR", name)
                self.assertIn(b"BAD_META", payload)
            self.assertEqual(os.listdir(dst_dir), [])

    def test_size_grammar(self) -> None:
        cases = (("١٠", b"BAD_META"), ("9" * 5000, b"TOO_LARGE"), ("01", b"BAD_META"))
        with tempfile.TemporaryDirectory() as dst_dir:
            for size, code in cases:
                ftype, payload, events = meta_exchange(f"a1b2c3d4|{size}|f.txt".encode(), dst_dir)
                self.assertEqual(ftype, "FILE_ERR")
                self.assertIn(code, payload)
                self.assertEqual(len([e for e in events if e.type == ERROR]), 1)

    def test_receive_has_no_fixed_delay(self) -> None:
        with tempfile.TemporaryDirectory() as dst_dir:
            start = time.monotonic()
            ftype, _, _ = meta_exchange(b"a1b2c3d4|1|f.txt", dst_dir, b"x")
            self.assertEqual(ftype, "FILE_DONE")
            self.assertLess(time.monotonic() - start, 0.15)

    def test_concurrent_same_name_keeps_both(self) -> None:
        with tempfile.TemporaryDirectory() as src_dir, tempfile.TemporaryDirectory() as dst_dir:
            payloads = [os.urandom(3_000_000), os.urandom(3_000_000)]
            threads = []
            for i, data in enumerate(payloads):
                path = os.path.join(src_dir, f"s{i}", "same.bin")
                os.makedirs(os.path.dirname(path))
                with open(path, "wb") as f:
                    f.write(data)
                s_client, s_server = make_pair()
                threads.append(
                    threading.Thread(
                        target=receive_file,
                        args=(s_server, f"p{i}", "Alice", queue.Queue(), dst_dir),
                        daemon=True,
                    )
                )
                threads.append(
                    threading.Thread(
                        target=send_file,
                        args=(s_client, path, "peer2", queue.Queue()),
                        daemon=True,
                    )
                )
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=20)
            names = sorted(os.listdir(dst_dir))
            self.assertEqual(names, ["same (1).bin", "same.bin"])
            got = sorted(file_sha256(os.path.join(dst_dir, n)) for n in names)
            want = sorted(hashlib.sha256(d).hexdigest() for d in payloads)
            self.assertEqual(got, want)

    def test_shrinking_file_reports_changed(self) -> None:
        s_client, s_server = make_pair()
        with tempfile.TemporaryDirectory() as src_dir:
            path = os.path.join(src_dir, "shrink.bin")
            with open(path, "wb") as f:
                f.write(b"x" * 1000)
            send_q: queue.Queue = queue.Queue()
            with mock.patch("file_transfer.os.path.getsize", lambda p: 100000):
                send_file(s_client, path, "peer1", send_q)
        errs = [e for e in drain_events(send_q) if e.type == ERROR]
        self.assertEqual(len(errs), 1)
        self.assertIn("changed while sending", errs[0].data["message"])
        s_server.close()

    def test_growing_file_sends_only_declared_size(self) -> None:
        s_client, s_server = make_pair()
        with tempfile.TemporaryDirectory() as src_dir:
            path = os.path.join(src_dir, "grow.bin")
            with open(path, "wb") as f:
                f.write(b"x" * 1000)
            with mock.patch("file_transfer.os.path.getsize", lambda p: 100):
                t = threading.Thread(
                    target=send_file, args=(s_client, path, "peer1", queue.Queue()), daemon=True
                )
                t.start()
                s_server.settimeout(5)
                ftype, _ = recv_frame(s_server)
                self.assertEqual(ftype, "FILE_META")
                got = b""
                while len(got) < 100:
                    got += s_server.recv(100 - len(got))
                s_server.settimeout(0.3)
                with self.assertRaises(TimeoutError):
                    s_server.recv(1)
                s_server.close()
                t.join(timeout=15)
        self.assertEqual(len(got), 100)


if __name__ == "__main__":
    unittest.main()
