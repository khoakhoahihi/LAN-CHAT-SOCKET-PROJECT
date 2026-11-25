"""Test cho protocol.py. Chạy: python -m unittest tests.test_protocol -v"""

import random
import socket
import threading
import unittest

import protocol as p


class FakeSocket:
    """Socket giả: recv trả dữ liệu theo từng mảnh định sẵn, không bao giờ quá bufsize.

    Hết mảnh thì recv trả b"" (giống bên kia đóng kết nối).
    """

    def __init__(self, chunks=()):
        self._chunks = [bytes(c) for c in chunks if c]
        self.sent = bytearray()
        self.sendall_calls = 0
        self.recv_calls = 0

    def recv(self, bufsize):
        self.recv_calls += 1
        if not self._chunks:
            return b""
        head = self._chunks[0]
        out, rest = head[:bufsize], head[bufsize:]
        if rest:
            self._chunks[0] = rest
        else:
            self._chunks.pop(0)
        return out

    def sendall(self, data):
        self.sendall_calls += 1
        self.sent += data

    def remaining(self):
        return b"".join(self._chunks)


def one_byte_chunks(data):
    return [data[i:i + 1] for i in range(len(data))]


class SocketPairTest(unittest.TestCase):
    def setUp(self):
        self.a, self.b = socket.socketpair()
        self.a.settimeout(10)
        self.b.settimeout(10)

    def tearDown(self):
        self.a.close()
        self.b.close()


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


# --- Test #1–2: recv_exact ---------------------------------------------------
class TestRecvExact(unittest.TestCase):
    def test_byte_by_byte(self):  # 1
        data = "Xin chào".encode()
        sock = FakeSocket(one_byte_chunks(data))
        self.assertEqual(p.recv_exact(sock, len(data)), data)

    def test_does_not_read_past_n(self):
        sock = FakeSocket([b"abcdefgh"])
        self.assertEqual(p.recv_exact(sock, 3), b"abc")
        self.assertEqual(sock.remaining(), b"defgh")

    def test_eof_midway(self):  # 2
        sock = FakeSocket([b"abc"])
        with self.assertRaises(p.ConnectionClosed) as cm:
            p.recv_exact(sock, 5)
        self.assertIn("3/5", str(cm.exception))

    def test_zero(self):  # 2
        sock = FakeSocket([b"abc"])
        self.assertEqual(p.recv_exact(sock, 0), b"")
        self.assertEqual(sock.recv_calls, 0)

    def test_bad_n(self):
        with self.assertRaises(ValueError):
            p.recv_exact(FakeSocket(), -1)
        with self.assertRaises(TypeError):
            p.recv_exact(FakeSocket(), 1.5)


# --- Test #3–4: send_frame ---------------------------------------------------
class TestSendFrame(unittest.TestCase):
    def test_vietnamese_msg(self):  # 3
        sock = FakeSocket()
        p.send_frame(sock, "MSG", "Xin chào".encode())
        self.assertEqual(bytes(sock.sent), "MSG|9|Xin chào".encode())
        self.assertEqual(sock.sendall_calls, 1)

    def test_empty_payload(self):  # 4
        sock = FakeSocket()
        p.send_frame(sock, "BYE", b"")
        self.assertEqual(bytes(sock.sent), b"BYE|0|")

    def test_max_payload_ok(self):  # 4
        sock = FakeSocket()
        p.send_frame(sock, "MSG", b"x" * p.MAX_FRAME_PAYLOAD)
        self.assertTrue(sock.sent.startswith(b"MSG|65536|"))
        self.assertEqual(len(sock.sent), len(b"MSG|65536|") + 65_536)
        self.assertEqual(sock.sendall_calls, 1)

    def test_rejects(self):  # 4
        sock = FakeSocket()
        with self.assertRaises(ValueError):
            p.send_frame(sock, "MSG", b"x" * (p.MAX_FRAME_PAYLOAD + 1))
        with self.assertRaises(ValueError):
            p.send_frame(sock, "HACK", b"")
        with self.assertRaises(ValueError):
            p.send_frame(sock, "msg", b"")
        with self.assertRaises(TypeError):
            p.send_frame(sock, "MSG", "chuỗi chứ không phải bytes")
        self.assertEqual(sock.sendall_calls, 0)


# --- Test #5–9: recv_frame ---------------------------------------------------
class TestRecvFrameSocketPair(SocketPairTest):
    def test_roundtrip(self):  # 5
        cases = [("MSG", "Xin chào".encode()), ("BYE", b""),
                 ("INTRO", b"CHAT|a1b2c3d4|Quan"), ("FILE_META", b"x" * 1000)]
        for ftype, payload in cases:
            p.send_frame(self.a, ftype, payload)
        for ftype, payload in cases:
            self.assertEqual(p.recv_frame(self.b), (ftype, payload))

    def test_max_payload_roundtrip(self):
        payload = bytes(random.Random(1).randrange(256) for _ in range(p.MAX_FRAME_PAYLOAD))
        t = threading.Thread(target=p.send_frame, args=(self.a, "MSG", payload))
        t.start()
        self.assertEqual(p.recv_frame(self.b), ("MSG", payload))
        t.join()

    def test_eof_before_first_byte(self):  # 9
        self.a.close()
        with self.assertRaises(p.ConnectionClosed):
            p.recv_frame(self.b)


class TestRecvFrameFake(unittest.TestCase):
    def test_three_frames_one_chunk(self):  # 6
        data = b"MSG|2|hiBYE|0|" + "MSG|9|Xin chào".encode()
        sock = FakeSocket([data])
        self.assertEqual(p.recv_frame(sock), ("MSG", b"hi"))
        self.assertEqual(p.recv_frame(sock), ("BYE", b""))
        self.assertEqual(p.recv_frame(sock), ("MSG", "Xin chào".encode()))
        with self.assertRaises(p.ConnectionClosed):
            p.recv_frame(sock)

    def test_header_split_byte_by_byte(self):  # 6
        data = b"MSG|2|hiBYE|0|" + "MSG|9|Xin chào".encode()
        sock = FakeSocket(one_byte_chunks(data))
        self.assertEqual(p.recv_frame(sock), ("MSG", b"hi"))
        self.assertEqual(p.recv_frame(sock), ("BYE", b""))
        self.assertEqual(p.recv_frame(sock), ("MSG", "Xin chào".encode()))

    def test_no_over_read_after_file_meta(self):  # 7
        meta = b"3fa9c2d1|7|a.txt"
        data = b"FILE_META|%d|" % len(meta) + meta + b"RAWDATA"
        sock = FakeSocket([data])
        self.assertEqual(p.recv_frame(sock), ("FILE_META", meta))
        self.assertEqual(sock.remaining(), b"RAWDATA")
        self.assertEqual(p.recv_exact(sock, 7), b"RAWDATA")

    def test_protocol_errors(self):  # 8
        bad = [
            b"MSG|09|aaaaaaaaa",
            b"MSG|abc|",
            b"MSG|-1|",
            b"MSG||",
            b"MSG|65537|",
            b"MSG|1 |a",
            b"msg|0|",
            b"HACK|0|",
            b"|0|",
            b"ABCDEFGHIJKLMNOPQ|0|",          # TYPE 17 ký tự
            b"A" * 33,                       # 33 byte không có '|'
            b"MSG|" + b"1" * 40 + b"|",      # header vượt 32 byte
            "MSG|1|".encode("ascii").replace(b"M", b"\xc3"),  # byte không phải ASCII
            "MSĞ|0|".encode("utf-8"),
        ]
        for data in bad:
            with self.subTest(data=data):
                with self.assertRaises(p.ProtocolError):
                    p.recv_frame(FakeSocket([data]))

    def test_header_limit_boundary(self):
        # Header đúng 32 byte qua được bước đọc header, bị loại ở bước kiểm tra TYPE.
        with self.assertRaises(p.ProtocolError) as cm:
            p.recv_frame(FakeSocket([b"A" * 30 + b"||"]))
        self.assertIn("loại frame", str(cm.exception))
        # 33 byte thì bị loại ngay vì header quá dài.
        with self.assertRaises(p.ProtocolError) as cm:
            p.recv_frame(FakeSocket([b"A" * 31 + b"||"]))
        self.assertIn("header", str(cm.exception))

    def test_eof_cases(self):  # 9
        for data in [b"", b"MS", b"MSG|", b"MSG|5", b"MSG|5|ab"]:
            with self.subTest(data=data):
                with self.assertRaises(p.ConnectionClosed):
                    p.recv_frame(FakeSocket([data]))


# --- Test #11: hàm payload ---------------------------------------------------
class TestPayload(unittest.TestCase):
    def test_intro_roundtrip(self):
        for kind in ("CHAT", "FILE"):
            payload = p.make_intro(kind, "a1b2c3d4", "Đăng")
            self.assertEqual(p.parse_intro(payload), (kind, "a1b2c3d4", "Đăng"))

    def test_intro_make_rejects(self):
        for args in [("VOICE", "a1b2c3d4", "Quan"), ("CHAT", "A1B2C3D4", "Quan"),
                     ("CHAT", "a1b2c3d4", "a b"), ("CHAT", "a1b2c3d4", "12"),
                     ("CHAT", "a1b2c3d4", "")]:
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    p.make_intro(*args)

    def test_intro_parse_rejects(self):
        for payload in [b"CHAT|a1b2c3d4", b"CHAT|a1b2c3d4|Quan|x", b"",
                        b"VOICE|a1b2c3d4|Quan", b"chat|a1b2c3d4|Quan",
                        b"CHAT|A1B2C3D4|Quan", b"CHAT|a1b2c3d4|", b"CHAT|a1b2c3d4|123",
                        b"CHAT|a1b2c3d4|Qu\xffan", b"CHAT|a1b2c3d4|" + b"x" * 21]:
            with self.subTest(payload=payload):
                with self.assertRaises(p.ProtocolError):
                    p.parse_intro(payload)

    def test_intro_ok(self):
        payload = p.make_intro_ok("9f8e7d6c", "Minh")
        self.assertEqual(payload, b"9f8e7d6c|Minh")
        self.assertEqual(p.parse_intro_ok(payload), ("9f8e7d6c", "Minh"))
        for bad in [b"9f8e7d6c", b"9f8e7d6c|Minh|x", b"9f8e7d6c|M inh", b"\xff|Minh"]:
            with self.subTest(payload=bad):
                with self.assertRaises(p.ProtocolError):
                    p.parse_intro_ok(bad)
        with self.assertRaises(ValueError):
            p.make_intro_ok("xyz", "Minh")

    def test_intro_err_message_with_pipe(self):
        payload = p.make_intro_err("DUPLICATE", "đã có kết nối | dùng kết nối cũ")
        self.assertEqual(p.parse_intro_err(payload),
                         ("DUPLICATE", "đã có kết nối | dùng kết nối cũ"))
        self.assertEqual(p.parse_intro_err(b"SHUTTING_DOWN|"), ("SHUTTING_DOWN", ""))

    def test_intro_err_rejects(self):
        with self.assertRaises(ValueError):
            p.make_intro_err("OOPS", "x")
        for bad in [b"DUPLICATE", b"OOPS|x", b"", b"BAD_INTRO|\xff"]:
            with self.subTest(payload=bad):
                with self.assertRaises(p.ProtocolError):
                    p.parse_intro_err(bad)

    def test_msg(self):
        self.assertEqual(p.encode_msg("Xin chào"), "Xin chào".encode())
        self.assertEqual(p.decode_msg("Xin chào | ok".encode()), "Xin chào | ok")
        self.assertEqual(p.encode_msg(""), b"")
        # Giới hạn tính bằng byte: 'à' là 2 byte UTF-8
        self.assertEqual(len(p.encode_msg("à" * (p.MAX_FRAME_PAYLOAD // 2))), p.MAX_FRAME_PAYLOAD)

    def test_encode_msg_rejects(self):
        for bad in ["a\nb", "a\rb", "à" * (p.MAX_FRAME_PAYLOAD // 2 + 1), "\ud800"]:
            with self.subTest(text=bad[:10]):
                with self.assertRaises(ValueError):
                    p.encode_msg(bad)
        with self.assertRaises(TypeError):
            p.encode_msg(b"bytes")

    def test_decode_msg_rejects(self):
        for bad in [b"\xff\xfe", b"a\nb", b"a\rb", "chà".encode()[:-1]]:
            with self.subTest(payload=bad):
                with self.assertRaises(p.ProtocolError):
                    p.decode_msg(bad)


# --- Test #12: ví dụ trong protocol.md mục 6 ---------------------------------
class TestSpecExamples(unittest.TestCase):
    def frame(self, ftype, payload):
        sock = FakeSocket()
        p.send_frame(sock, ftype, payload)
        return bytes(sock.sent)

    def test_session_bytes(self):
        self.assertEqual(self.frame("INTRO", p.make_intro("CHAT", "a1b2c3d4", "Quan")),
                         b"INTRO|18|CHAT|a1b2c3d4|Quan")
        self.assertEqual(self.frame("INTRO", p.make_intro("FILE", "a1b2c3d4", "Quan")),
                         b"INTRO|18|FILE|a1b2c3d4|Quan")
        self.assertEqual(self.frame("INTRO_OK", p.make_intro_ok("9f8e7d6c", "Minh")),
                         b"INTRO_OK|13|9f8e7d6c|Minh")
        self.assertEqual(self.frame("MSG", p.encode_msg("Hi Minh")), b"MSG|7|Hi Minh")
        self.assertEqual(self.frame("MSG", p.encode_msg("Xin chào")),
                         "MSG|9|Xin chào".encode())
        self.assertEqual(self.frame("BYE", b""), b"BYE|0|")
        # Ví dụ ở mục 3.1: tin 28 byte
        self.assertEqual(self.frame("MSG", p.encode_msg("Chào Minh, file xong chưa?")),
                         "MSG|28|Chào Minh, file xong chưa?".encode())


# --- Test #13: stress qua socketpair ----------------------------------------
class TestStress(SocketPairTest):
    N_FRAMES = 1000

    def test_many_random_frames(self):
        rng = random.Random(2026)
        alphabet = "abcxyz ABC 0123|!?ăâđêôơưáàảãạấầẩẫậếềểễệốồổỗộớờởỡợứừửữựíìỉĩịýỳỷỹỵ"
        types = sorted(p.FRAME_TYPES)
        frames = []
        for i in range(self.N_FRAMES):
            size = rng.choice([0, 1, p.MAX_FRAME_PAYLOAD, rng.randint(0, p.MAX_FRAME_PAYLOAD)])
            if i % 2:
                payload = "".join(rng.choices(alphabet, k=size)).encode()[:size]
            else:
                payload = rng.randbytes(size)
            frames.append((rng.choice(types), payload))

        errors = []

        def sender():
            try:
                for ftype, payload in frames:
                    p.send_frame(self.a, ftype, payload)
            except Exception as exc:  # báo lỗi về luồng chính
                errors.append(exc)

        t = threading.Thread(target=sender, daemon=True)
        t.start()
        for idx, expected in enumerate(frames):
            got = p.recv_frame(self.b)
            if got != expected:
                self.fail(f"frame #{idx} sai: {got[0]} {len(got[1])}B")
        t.join(10)
        self.assertEqual(errors, [])
        self.assertFalse(t.is_alive())


if __name__ == "__main__":
    unittest.main()
