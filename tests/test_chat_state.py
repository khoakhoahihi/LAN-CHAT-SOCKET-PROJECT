import threading
import unittest

from chat_state import ChatState, Message


class TestChatState(unittest.TestCase):
    def test_message_to_current_peer_not_unread(self) -> None:
        state = ChatState()
        state.switch_to("peer1")
        result = state.add_message("peer1", "alice", "hello")
        self.assertTrue(result)
        self.assertEqual(state.unread("peer1"), 0)

    def test_message_to_other_peer_counts(self) -> None:
        state = ChatState()
        state.switch_to("peer1")
        result = state.add_message("peer2", "bob", "hi")
        self.assertFalse(result)
        self.assertEqual(state.unread("peer2"), 1)
        result2 = state.add_message("peer2", "bob", "how are you")
        self.assertFalse(result2)
        self.assertEqual(state.unread("peer2"), 2)

    def test_switch_to_returns_unread_in_order_and_clears_counter(self) -> None:
        state = ChatState()
        state.add_message("peer1", "alice", "msg 1")
        state.add_message("peer1", "alice", "msg 2")
        unread = state.switch_to("peer1")
        self.assertEqual(
            unread,
            [Message("alice", "msg 1"), Message("alice", "msg 2")],
        )
        self.assertEqual(state.unread("peer1"), 0)
        self.assertEqual(state.current, "peer1")
        self.assertEqual(state.switch_to("peer1"), [])
        state.switch_to("peer2")
        state.add_message("peer1", "alice", "msg 3")
        self.assertEqual(state.switch_to("peer1"), [Message("alice", "msg 3")])
        self.assertEqual(state.unread("peer1"), 0)

    def test_unread_survives_leave(self) -> None:
        state = ChatState()
        state.add_message("peer1", "alice", "msg")
        state.switch_to("peer2")
        state.leave()
        self.assertIsNone(state.current)
        self.assertEqual(state.unread("peer1"), 1)
        self.assertFalse(state.add_message("peer2", "bob", "hi"))
        self.assertEqual(state.unread("peer2"), 1)

    def test_history_keeps_both_directions(self) -> None:
        state = ChatState()
        state.add_message("peer1", "alice", "inbound")
        state.add_message("peer1", "me", "outbound")
        expected = [Message("alice", "inbound"), Message("me", "outbound")]
        self.assertEqual(state.history("peer1"), expected)
        copied = state.history("peer1")
        copied.append(Message("other", "extra"))
        self.assertEqual(state.history("peer1"), expected)

    def test_concurrent_add_message(self) -> None:
        state = ChatState()

        def worker() -> None:
            for i in range(500):
                state.add_message("peer1", "sender", f"msg{i}")

        t1 = threading.Thread(target=worker, daemon=True)
        t2 = threading.Thread(target=worker, daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        self.assertEqual(state.unread("peer1"), 1000)
        self.assertEqual(len(state.history("peer1")), 1000)


if __name__ == "__main__":
    unittest.main()
