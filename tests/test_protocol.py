"""Test cho protocol.py. Chạy: python -m unittest tests.test_protocol -v"""

import unittest

import protocol as p


class TestConstants(unittest.TestCase):
    def test_values_match_spec(self):
        self.assertEqual(p.UDP_PORT, 9999)
        self.assertEqual(p.MAX_HEADER, 32)
        self.assertEqual(p.MAX_FRAME_PAYLOAD, 65_536)
        self.assertEqual(p.MAX_FILE_SIZE, 2 * 1024 ** 3)
        self.assertEqual(p.MAX_NAME_LEN, 20)

    def test_connection_closed_is_oserror(self):
        # transport chỉ cần `except OSError` là bắt được cả mất kết nối lẫn lỗi socket
        self.assertTrue(issubclass(p.ConnectionClosed, OSError))
        self.assertFalse(issubclass(p.ProtocolError, OSError))


# --- Test #10: hàm kiểm tra -------------------------------------------------
class TestValidators(unittest.TestCase):
    def test_valid_names(self):
        for name in ["Quan", "Đăng", "Quan2", "a", "x" * 20, "Nguyễn_Văn-A"]:
            with self.subTest(name=name):
                self.assertTrue(p.is_valid_name(name))

    def test_invalid_names(self):
        for name in ["", "x" * 21, "12", "0", "a b", "a|b", "\x07a",
                     "a\tb", "a\nb", "a　b", None, 123]:
            with self.subTest(name=name):
                self.assertFalse(p.is_valid_name(name))

    def test_peer_id(self):
        self.assertTrue(p.is_valid_peer_id("a1b2c3d4"))
        self.assertTrue(p.is_valid_peer_id("00000000"))
        for bad in ["A1B2C3D4", "a1b2c3d", "a1b2c3d45", "g1b2c3d4",
                    "a1b2c3d4\n", "", None, 12345678]:
            with self.subTest(peer_id=bad):
                self.assertFalse(p.is_valid_peer_id(bad))

    def test_status(self):
        self.assertTrue(p.is_valid_status("online"))
        self.assertTrue(p.is_valid_status("away"))
        for bad in ["Online", "busy", "", None]:
            with self.subTest(status=bad):
                self.assertFalse(p.is_valid_status(bad))

    def test_new_id(self):
        ids = {p.new_id() for _ in range(200)}
        self.assertTrue(all(p.is_valid_peer_id(i) for i in ids))
        self.assertGreater(len(ids), 190)  # gần như không trùng


if __name__ == "__main__":
    unittest.main()
