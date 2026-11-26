"""Test cho events.py. Chạy: python -m unittest tests.test_events -v"""

import queue
import unittest

import events as ev
from events import Event


class TestEvent(unittest.TestCase):
    def test_valid_events(self):
        cases = [
            Event(ev.PEER_ONLINE, {"peer": object()}),
            Event(ev.PEER_UPDATED, {"peer": object(), "old": object()}),
            Event(ev.PEER_OFFLINE, {"peer": object(), "reason": ev.OFFLINE_TIMEOUT}),
            Event(ev.CHAT_CONNECTED, {"peer_id": "a1b2c3d4", "name": "Quan", "incoming": True}),
            Event(ev.CHAT_RECEIVED, {"peer_id": "a1b2c3d4", "name": "Quan", "text": "Xin chào"}),
            Event(ev.CONNECTION_CLOSED, {"peer_id": "a1b2c3d4", "name": "Quan",
                                         "reason": ev.CLOSE_PROTOCOL_ERROR}),
            Event(ev.FILE_PROGRESS, {"file_id": "3fa9c2d1", "peer_id": "a1b2c3d4",
                                     "filename": "a.pdf", "direction": "send",
                                     "done": 10, "total": 100}),
            Event(ev.FILE_COMPLETED, {"file_id": "3fa9c2d1", "peer_id": "a1b2c3d4",
                                      "filename": "a.pdf", "direction": "recv",
                                      "path": "downloads/a.pdf"}),
            Event(ev.FILE_UNCONFIRMED, {"file_id": "3fa9c2d1", "peer_id": "a1b2c3d4",
                                        "filename": "a.pdf"}),
            Event(ev.ERROR, {"source": "tcp", "message": "lỗi"}),
            Event(ev.ERROR, {"source": "file", "message": "lỗi", "file_id": "3fa9c2d1"}),
        ]
        self.assertEqual({e.type for e in cases}, ev.EVENT_TYPES)

    def test_unknown_type(self):
        with self.assertRaises(ValueError):
            Event("CHAT_RECIEVED", {"peer_id": "a", "name": "b", "text": "c"})

    def test_missing_key(self):
        with self.assertRaises(ValueError) as cm:
            Event(ev.CHAT_RECEIVED, {"peer_id": "a1b2c3d4", "name": "Quan"})
        self.assertIn("text", str(cm.exception))

    def test_bad_enum_values(self):
        bad = [
            (ev.CONNECTION_CLOSED, {"peer_id": "x", "name": "y", "reason": "protocol-error"}),
            (ev.PEER_OFFLINE, {"peer": None, "reason": "bye"}),
            (ev.FILE_PROGRESS, {"file_id": "x", "peer_id": "y", "filename": "z",
                                "direction": "upload", "done": 0, "total": 0}),
            (ev.ERROR, {"source": "network", "message": "m"}),
        ]
        for etype, data in bad:
            with self.subTest(type=etype):
                with self.assertRaises(ValueError):
                    Event(etype, data)

    def test_data_must_be_dict(self):
        with self.assertRaises(TypeError):
            Event(ev.ERROR, [("source", "tcp"), ("message", "m")])

    def test_frozen_and_copied(self):
        data = {"source": "cli", "message": "m"}
        e = Event(ev.ERROR, data)
        data["message"] = "đổi sau khi tạo"
        self.assertEqual(e.data["message"], "m")
        with self.assertRaises(AttributeError):
            e.type = ev.CHAT_RECEIVED

    def test_through_queue(self):
        q = queue.Queue()
        q.put(Event(ev.CHAT_RECEIVED, {"peer_id": "a1b2c3d4", "name": "Quan", "text": "hi"}))
        got = q.get_nowait()
        self.assertEqual(got.type, ev.CHAT_RECEIVED)
        self.assertEqual(got.data["text"], "hi")


if __name__ == "__main__":
    unittest.main()
