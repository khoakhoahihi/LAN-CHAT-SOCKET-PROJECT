"""Tests for PeerStore; run python -m unittest tests.test_peer_store -v."""

from dataclasses import FrozenInstanceError, replace
import queue
import threading
import unittest
from unittest.mock import patch

from peer_store import Peer, PeerStore


class PeerStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = PeerStore()
        self.peer = Peer(
            "a1b2c3d4", "Quan", "192.168.1.10", 5000, "online", 100.0
        )

    def test_empty(self):
        self.assertEqual(self.store.get_peers(), [])
        self.assertIsNone(self.store.get("ffffffff"))
        self.assertEqual(self.store.find_by_name("Quan"), [])

    def test_frozen(self):
        with self.assertRaises(FrozenInstanceError):
            self.peer.status = "away"

    def test_upsert_copies_and_old_value(self):
        old, first = self.store.upsert(self.peer)
        self.assertIsNone(old)
        self.assertIsNot(first, self.peer)
        old, current = self.store.upsert(
            replace(self.peer, status="away", last_seen=103.0)
        )
        self.assertEqual(old, self.peer)
        self.assertEqual(current.status, "away")
        self.assertEqual(len(self.store.get_peers()), 1)

    def test_invalid_record_type(self):
        with self.assertRaises(TypeError):
            self.store.upsert({"peer_id": "a1b2c3d4"})

    def test_sorted_duplicate_names(self):
        for peer in [
            self.peer,
            replace(self.peer, peer_id="55667788"),
            replace(self.peer, peer_id="11223344", name="An"),
        ]:
            self.store.upsert(peer)
        self.assertEqual(
            [(p.name, p.peer_id) for p in self.store.get_peers()],
            [("An", "11223344"), ("Quan", "55667788"), ("Quan", "a1b2c3d4")],
        )
        self.assertEqual(
            [p.peer_id for p in self.store.find_by_name("Quan")],
            ["55667788", "a1b2c3d4"],
        )
        self.assertEqual(self.store.find_by_name("quan"), [])

    def test_all_readers_return_copies(self):
        self.store.upsert(self.peer)
        a = self.store.get(self.peer.peer_id)
        b = self.store.get(self.peer.peer_id)
        self.assertEqual(a, b)
        self.assertIsNot(a, b)
        self.assertIsNot(self.store.get_peers()[0], self.store.get_peers()[0])
        self.assertIsNot(
            self.store.find_by_name("Quan")[0],
            self.store.find_by_name("Quan")[0],
        )
        snapshot = self.store.get_peers()
        snapshot.clear()
        self.assertEqual(len(self.store.get_peers()), 1)

    def test_remove_idempotent(self):
        self.store.upsert(self.peer)
        removed = self.store.remove(self.peer.peer_id)
        self.assertEqual(removed, self.peer)
        self.assertIsNot(removed, self.peer)
        self.assertIsNone(self.store.remove(self.peer.peer_id))

    def test_timeout_boundary_and_fresh_peer(self):
        self.store.upsert(self.peer)
        fresh = replace(self.peer, peer_id="11223344", last_seen=108.0)
        self.store.upsert(fresh)
        self.assertEqual(self.store.remove_expired(10, now=110), [])
        self.assertEqual(self.store.remove_expired(10, now=111), [self.peer])
        self.assertEqual(self.store.get_peers(), [fresh])

    def test_real_clock_and_negative_timeout(self):
        self.store.upsert(self.peer)
        with self.assertRaises(ValueError):
            self.store.remove_expired(-1)
        self.assertEqual(len(self.store.get_peers()), 1)
        with patch("peer_store.time.monotonic", return_value=111.0):
            self.assertEqual(self.store.remove_expired(10), [self.peer])

    def test_five_writers_and_readers(self):
        barrier = threading.Barrier(5)
        errors = queue.Queue()

        def worker(index):
            try:
                barrier.wait(timeout=3)
                for number in range(100):
                    peer = replace(
                        self.peer,
                        peer_id=f"{index * 100 + number:08x}",
                        name=f"Peer{index}",
                    )
                    self.store.upsert(peer)
                    self.store.get(peer.peer_id)
                    if number % 10 == 0:
                        self.store.get_peers()
                        self.store.find_by_name(peer.name)
            except Exception as error:
                errors.put(error)

        threads = [
            threading.Thread(target=worker, args=(i,), daemon=True)
            for i in range(5)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertTrue(errors.empty(), list(errors.queue))
        self.assertEqual(len(self.store.get_peers()), 500)

if __name__ == "__main__":
    unittest.main()
