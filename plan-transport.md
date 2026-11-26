# Plan `transport.py` — Người 2

> Bám theo `docs/protocol.md` (mục 2–4) và `CONTRIBUTING.md` mục 2.3, 2.5, 3.1.
> Dùng `protocol.py` (đã merge qua #4, #5) và `events.py` (nhánh `feat/events`, cần cả 3 người duyệt).

## 1. Phạm vi

`transport.py` lo **mọi kết nối TCP**: lắng nghe, bắt tay `INTRO`, giữ kết nối chat, đọc/ghi tin, xử lý trùng kết nối, và bàn giao socket file cho Người 3.

- **Không** phân tích frame bằng tay: mọi đọc/ghi đi qua `recv_frame`, `send_frame`, `make_*`, `parse_*` của `protocol.py`.
- **Không** `print`. Báo cho giao diện bằng `events.put(Event(...))`, ghi log gỡ lỗi bằng `logging.getLogger("transport")`.
- **Không** hỏi `PeerStore` xem peer có online không (CONTRIBUTING 3.1 quy tắc 6). Ai gửi `INTRO` hợp lệ cũng được nhận.
- **Không** đọc, ghi hay đóng socket file sau khi đã bàn giao.

## 2. Chia làm 3 PR

| PR | Nội dung | Vì sao tách |
|---|---|---|
| **C1** `feat/transport-chat` | Khung `Transport`, `start`/`stop`, luồng accept, bắt tay `CHAT`, luồng đọc, `connect_chat` (chưa xử lý trùng), `send_chat`, `disconnect_chat`, `is_connected` | Phần lõi. Sau PR này hai máy đã chat được với nhau |
| **C2** `feat/transport-file` | Bắt tay `FILE`, gọi `on_file_connection`, `open_file_connection` | Nhỏ. Làm sớm để Nam Khánh có thể ghép `file_transfer.py` |
| **C3** `feat/transport-duplicate` | Hai bên cùng `/chat` một lúc (protocol.md 3.2), `DUPLICATE_WAIT` | Khó nhất, cần test đua luồng kỹ |

Bảng kết nối (mục 3.2 dưới đây) phải được thiết kế cho C3 ngay từ C1, để khỏi phải viết lại.

## 3. Thiết kế

### 3.1 Các luồng

| Luồng | Số lượng | Việc |
|---|---|---|
| `acceptor` | 1 | `accept()`, mỗi kết nối mới tạo một luồng `handshake` rồi quay lại `accept()` ngay |
| `handshake` | 1 cho mỗi kết nối đến | Chờ `INTRO` (timeout `INTRO_TIMEOUT`), trả `INTRO_OK`/`INTRO_ERR`. Kết nối file: gọi `on_file_connection` **trên chính luồng này** (đây là luồng mới, không phải acceptor, đúng hợp đồng) |
| `reader` | 1 cho mỗi kết nối chat đang mở | `recv_frame` lặp, phát `CHAT_RECEIVED`, phát hiện `BYE` / mất kết nối / lỗi giao thức |
| Luồng của người gọi | — | `connect_chat`, `open_file_connection` chạy trên luồng gọi (CLI tự đưa ra luồng phụ, CONTRIBUTING 2.3) |

Mọi luồng `daemon=True`. Dừng bằng `self._stopping = threading.Event()`.

### 3.2 Trạng thái và khóa

```python
class _ChatConn:            # một kết nối chat
    sock: socket.socket
    peer_id: str
    name: str               # tên lấy từ INTRO / INTRO_OK
    incoming: bool
    initiator: str          # peer_id của bên đã connect()
    send_lock: threading.Lock
    state: str              # "connecting" | "accepting" | "open"
    closed: bool
```

- `self._cond = threading.Condition()` bảo vệ `self._chats: dict[peer_id, _ChatConn]`. Mỗi `peer_id` có **tối đa một** ô trong bảng, kể cả khi đang bắt tay. Đây chính là điều kiện để phát hiện trùng ở C3.
- **Khóa bảng** (`_cond`): chỉ giữ khi đọc/sửa `_chats`. **Không bao giờ** giữ khi `connect`, `send`, `recv` (CONTRIBUTING 3.1 quy tắc 5).
- **Khóa gửi** (`send_lock`): mỗi kết nối chat một cái. Mọi `send_frame` trên socket chat (`INTRO_OK`, `MSG`, `BYE`) đều đi qua nó (quy tắc 4). Đây là khóa duy nhất được giữ trong lúc gửi mạng, và chỉ bao quanh đúng một lần `sendall`.
- Thứ tự lấy khóa nếu cần cả hai: `send_lock` **trước**, `_cond` sau. Tốt nhất là không bao giờ cần cả hai cùng lúc.

### 3.3 Hàm đóng dùng chung

Mọi đường đóng kết nối chat đều đi qua **một** hàm, để mỗi kết nối chỉ phát sự kiện đúng một lần:

```python
def _close(self, conn, reason: str | None) -> None:
    with self._cond:
        if conn.closed:
            return                       # đã có nơi khác đóng rồi
        conn.closed = True
        if self._chats.get(conn.peer_id) is conn:   # so `is`: ô có thể đã bị kết nối khác thay
            del self._chats[conn.peer_id]
        self._cond.notify_all()
    _shutdown_close(conn.sock)           # shutdown(SHUT_RDWR) rồi close(), nuốt OSError
    if reason is not None and conn.state == "open":
        self._emit(CONNECTION_CLOSED, peer_id=..., name=..., reason=reason)
```

`reason=None` dùng cho kết nối bị loại vì trùng: không phát sự kiện (protocol.md 3.2).

## 4. Luồng xử lý từng hàm

### 4.1 `start()`

1. Tạo socket TCP, bind `("", tcp_port)`, `listen()`.
   - POSIX: bật `SO_REUSEADDR` để chạy lại ngay sau khi thoát (tránh `TIME_WAIT`).
   - **Windows: không bật `SO_REUSEADDR`**, vì trên Windows nó cho hai process bind cùng một port. Nếu cần thì dùng `SO_EXCLUSIVEADDRUSE`.
   - Port đang bận: để `OSError` lan ra, `main.py` báo lỗi rồi thoát (giống `Discovery.start`).
2. Khởi động luồng `acceptor`.

### 4.2 Luồng `acceptor`

```text
while not stopping:
    sock, addr = listener.accept()        # OSError khi stop() đóng listener -> thoát vòng lặp
    if stopping: đóng sock; break
    Thread(target=_handle_incoming, args=(sock, addr), daemon=True).start()
```

`OSError` khi **không** đang dừng: log, phát `ERROR(source="tcp")`, rồi tiếp tục.

### 4.3 `_handle_incoming` (luồng `handshake`)

1. `sock.settimeout(INTRO_TIMEOUT)`, rồi `recv_frame`.
2. Frame đầu không phải `INTRO`, hoặc `parse_intro` lỗi, hoặc `peer_id == my_id`: gửi `INTRO_ERR BAD_INTRO` (bỏ qua lỗi gửi) rồi đóng. Chỉ ghi log, **không** phát `ERROR`, để một máy lạ gửi rác không làm phiền giao diện.
3. Timeout, `ConnectionClosed`, `OSError`: đóng, ghi log.
4. Đang dừng: gửi `INTRO_ERR SHUTTING_DOWN` rồi đóng.
5. `kind == "FILE"`: gửi `INTRO_OK`, rồi `settimeout(None)`, rồi gọi `on_file_connection(sock, peer_id, name)`. Callback ném exception thì ghi log và phát `ERROR(source="tcp")`. Sau đó **không đụng** socket nữa.
6. `kind == "CHAT"`:
   - Dưới `_cond`: quyết định theo bảng ở mục 4.8 (C1: nếu đã có ô thì tạm trả `DUPLICATE`). Nếu nhận thì giữ chỗ: `_chats[pid] = conn` với `state="accepting"`.
   - Ngoài khóa: `with conn.send_lock: send_frame(INTRO_OK)`. Lỗi thì gỡ ô ra (`_close(conn, None)`), không phát gì.
   - `settimeout(None)`. Dưới `_cond`: `state="open"`, `notify_all()`.
   - Phát `CHAT_CONNECTED(incoming=True)` **trước**, rồi mới khởi động `reader`. Như vậy giao diện luôn thấy "đã kết nối" trước tin đầu tiên.

### 4.4 `connect_chat(peer)`

```text
với _cond:
    nếu đang dừng                       -> ConnectError("đang thoát")
    ô của peer đang "open"              -> return (dùng lại)
    ô của peer đang "connecting"/"accepting" (luồng khác đang lo)
                                        -> wait_for(ô open hoặc biến mất, INTRO_TIMEOUT + CONNECT_TIMEOUT)
                                           open -> return; biến mất -> tự kết nối tiếp
    tạo ô "connecting" (initiator = my_id)
ngoài khóa:
    create_connection((peer.ip, peer.tcp_port), timeout=CONNECT_TIMEOUT)
    settimeout(INTRO_TIMEOUT); send_frame(INTRO CHAT); recv_frame()
```

| Nhận được | Xử lý |
|---|---|
| `INTRO_OK`, `peer_id` khớp | Dưới `_cond`: nếu ô vẫn là của mình thì `state="open"`. `settimeout(None)`, phát `CHAT_CONNECTED(incoming=False)`, khởi động `reader` |
| `INTRO_OK`, `peer_id` **khác** | IP hoặc port đã đổi chủ (protocol.md 3.1): đóng, `ConnectError` |
| `INTRO_ERR DUPLICATE` | (C3) đóng kết nối của mình, `wait_for` ô do bên kia mở, tối đa `DUPLICATE_WAIT`. Có thì `return`, hết giờ thì `ConnectError` |
| `INTRO_ERR` khác | `ConnectError` kèm `message` của bên kia |
| Frame khác, `ProtocolError`, `ConnectionClosed`, `TimeoutError`, `OSError` | `ConnectError` với câu dễ hiểu ("Không kết nối được tới Minh (192.168.1.5:6000): hết thời gian chờ") |

Mọi đường lỗi đều gỡ ô "connecting" (`_close(conn, None)`) và `notify_all()`, để luồng khác đang chờ không bị treo.

### 4.5 Luồng `reader`

| Nhận được | Xử lý |
|---|---|
| `MSG` | `decode_msg` → phát `CHAT_RECEIVED` |
| `BYE` | `_close(conn, "bye")` |
| Frame khác (`INTRO`, `FILE_*`, …) | Sai trạng thái: `_close(conn, "protocol_error")` |
| `ProtocolError` (kể cả từ `decode_msg`) | `_close(conn, "protocol_error")` |
| `ConnectionClosed`, `OSError` | `_close(conn, "lost")`. Nếu kết nối đã bị đóng ở phía mình (`/leave`, `stop`), `_close` thấy `closed=True` nên không phát gì |

### 4.6 `send_chat(peer_id, text)` và `disconnect_chat(peer_id)`

- **`send_chat`:**
  1. `payload = encode_msg(text)`. Lỗi thì `ValueError` lan thẳng ra, vì đây là lỗi nhập của người dùng.
  2. Lấy ô `open` dưới `_cond`. Không có thì `NotConnected`.
  3. `with conn.send_lock: send_frame(sock, "MSG", payload)`.
  4. `OSError` thì `_close(conn, "lost")` rồi `raise NotConnected`.
- **`disconnect_chat`:**
  1. Lấy ô `open`. Không có thì `return`.
  2. `with send_lock`: gửi `BYE`, bỏ qua `OSError`.
  3. `_close(conn, "local_leave")`.
- **`is_connected`:** ô tồn tại và `state == "open"`.

### 4.7 `open_file_connection(peer)` và `stop()`

- **`open_file_connection`:** giống `connect_chat` nhưng gửi `INTRO FILE` và **không** đưa vào bảng chat. Sau `INTRO_OK` có `peer_id` khớp thì `settimeout(None)` rồi trả socket. Mọi lỗi đều đóng socket và báo `ConnectError`.
- **`stop()`** (gọi nhiều lần không lỗi):
  1. `_stopping.set()`.
  2. Đánh thức `accept()`. Trên Linux, chỉ `close()` thì không làm `accept()` đang chờ thoát ra. Vì vậy: tự `create_connection(("127.0.0.1", port))` để chọc luồng accept, rồi `close()` listener.
  3. Lấy danh sách ô dưới `_cond`. Với mỗi kết nối `open`: gửi `BYE` (bỏ qua lỗi) rồi đóng **không phát sự kiện**, vì CLI đang thoát (cần nhóm chốt: mục 6, câu Q2).
  4. Socket file đã bàn giao: không đụng (thuộc Người 3).

### 4.8 Bảng quyết định khi trùng (C3, protocol.md 3.2)

Mình là `M`. Một `INTRO CHAT` mới vừa đến từ `P`, trong khi đã có ô cho `P`:

| Ô hiện có do ai khởi tạo | So sánh | Quyết định |
|---|---|---|
| `M` (mình đang `connecting`, hoặc đã `open`) | `M < P` | Giữ ô của mình. Gửi `INTRO_ERR DUPLICATE` cho kết nối mới rồi đóng |
| `M` | `P < M` | Nhận kết nối mới. Ô cũ bị thay (`_close(old, None)`). Luồng `connect_chat` của mình sẽ nhận `DUPLICATE`, rồi chờ thấy ô `open` và trả về thành công |
| `P` (cùng một bên khởi tạo hai lần, ví dụ kết nối cũ đã chết mà mình chưa biết) | — | **Đề xuất:** kết nối mới thay kết nối cũ, không phát sự kiện (cần chốt: Q1) |

Hai bên áp dụng cùng một bảng nên luôn giữ cùng một kết nối. `CHAT_CONNECTED` chỉ phát khi một ô chuyển sang `open` lần đầu.

## 5. Sự kiện `transport.py` phát

| Sự kiện | Khi nào |
|---|---|
| `CHAT_CONNECTED` | Ô chat chuyển sang `open`. `incoming=True` nếu bên kia kết nối tới |
| `CHAT_RECEIVED` | Nhận `MSG` hợp lệ. `name` lấy từ `INTRO` / `INTRO_OK` của kết nối đó |
| `CONNECTION_CLOSED` | `local_leave` / `bye` / `lost` / `protocol_error`. Mỗi kết nối tối đa một lần. Không phát cho kết nối bị loại vì trùng, và không phát khi `stop()` |
| `ERROR(source="tcp")` | `accept()` lỗi bất thường; callback `on_file_connection` ném exception |

`ConnectError` / `NotConnected` là exception trả cho người gọi, không phải sự kiện.

## 6. Câu hỏi cần cả nhóm chốt trước C1

| # | Câu hỏi | Đề xuất | Phải sửa |
|---|---|---|---|
| Q1 | Cùng một peer khởi tạo hai kết nối chat (kết nối cũ chết một chiều, bên kia kết nối lại) | Kết nối mới thay kết nối cũ, không phát sự kiện | `protocol.md` 3.2 |
| Q2 | `stop()` có gửi `BYE` và có phát `CONNECTION_CLOSED` không? | Gửi `BYE` để bên kia thấy `bye` chứ không phải `lost`; không phát sự kiện ở phía mình | `CONTRIBUTING.md` 2.3 |
| Q3 | Lỗi bắt tay từ máy lạ có phát `ERROR` không? | Chỉ ghi log | `CONTRIBUTING.md` 2.5 |
| Q4 | `CHAT_RECEIVED.name` lấy từ `INTRO`, có thể cũ sau khi bên kia đổi tên | CLI ưu tiên tên trong `PeerStore`, dùng `name` của sự kiện khi peer đã offline | Ghi chú cho Người 3 |
| Q5 | `send_chat` có thể chặn nếu bên kia ngừng đọc (bộ đệm gửi đầy) | Chấp nhận, vì tin chat nhỏ. Ghi vào báo cáo như một giới hạn | — |

**Phụ thuộc vào Người 1:** PR #2 hiện chưa có `Peer` / `PeerStore`, đẩy `dict` vào queue thay vì `Event`, và tự tạo queue riêng (`self.events = queue.Queue()`), lệch với CONTRIBUTING 2.1, 2.2, 2.5. `transport.py` chỉ cần `peer.peer_id`, `peer.name`, `peer.ip`, `peer.tcp_port`, nên test dùng tạm `FakePeer` và không bị chặn. Nhưng cần báo Huy sớm.

## 7. Kế hoạch test — `tests/test_transport.py`

Dụng cụ:

- Hai `Transport` thật trên `127.0.0.1`. Port lấy bằng `free_port()` (bind port 0, đọc port, đóng).
- `FakePeer` (dataclass có `peer_id`, `name`, `ip`, `tcp_port`, `status`, `last_seen`).
- `RawClient`: socket thường, dùng `protocol.py` để giả làm một peer gửi frame tùy ý, kể cả frame sai.
- `wait_event(q, type, timeout=2)`: lấy sự kiện đúng loại, quá giờ thì test fail. `assert_no_event(q, type, within=0.3)`.
- Rút timeout khi test: `mock.patch("transport.INTRO_TIMEOUT", 0.3)`. Muốn vậy, `transport.py` phải `from protocol import INTRO_TIMEOUT, …` và đọc hằng số lúc gọi hàm, không chép vào biến instance trong `__init__`.

| # | PR | Trường hợp | Kết quả mong đợi |
|---|---|---|---|
| 1 | C1 | `start` rồi `stop`; `stop` 2 lần; start lại cùng port ngay sau khi dừng | Không lỗi; `stop` xong dưới 1 s; bind lại được |
| 2 | C1 | Hai `Transport` cùng port | Cái thứ hai `start()` báo `OSError` (chạy cả trên Windows) |
| 3 | C1 | A `connect_chat(B)` | A: `CHAT_CONNECTED(incoming=False)`; B: `CHAT_CONNECTED(incoming=True)`; `is_connected` đúng ở cả hai |
| 4 | C1 | Gọi `connect_chat` lần hai | Dùng lại kết nối, không phát thêm `CHAT_CONNECTED` |
| 5 | C1 | Chat hai chiều, tiếng Việt, 200 tin | Nhận đủ, đúng thứ tự |
| 6 | C1 | 2 luồng cùng `send_chat` 500 tin trên một kết nối | Bên nhận được 1 000 tin hợp lệ, không có frame bị ghi xen (kiểm tra khóa gửi) |
| 7 | C1 | A `disconnect_chat` | A: `local_leave`; B: `bye`; cả hai `is_connected = False`; gọi lại không lỗi |
| 8 | C1 | `RawClient` bắt tay xong rồi đóng đột ngột | `CONNECTION_CLOSED(lost)` |
| 9 | C1 | `RawClient` gửi rác / `MSG` có `\n` / `INTRO` lần hai sau khi đã bắt tay | `CONNECTION_CLOSED(protocol_error)` |
| 10 | C1 | Frame đầu là `MSG`; `INTRO` có tên sai; `peer_id` bằng của mình | Nhận `INTRO_ERR BAD_INTRO`, bị đóng, không có `CHAT_CONNECTED` |
| 11 | C1 | Kết nối nhưng không gửi gì (`INTRO_TIMEOUT` patch còn 0,3 s) | Bị đóng sau khoảng 0,3 s; acceptor vẫn nhận kết nối khác bình thường trong lúc đó |
| 12 | C1 | `connect_chat` tới port đóng; tới `RawServer` im lặng; `INTRO_OK` có `peer_id` khác | `ConnectError` (lần lượt: ngay lập tức; sau timeout; ngay lập tức) |
| 13 | C1 | `send_chat` khi chưa kết nối; text có `\n`; text quá 64 KiB | `NotConnected`; `ValueError`; `ValueError` |
| 14 | C1 | `send_chat` sau khi bên kia mất kết nối | `NotConnected`; `CONNECTION_CLOSED(lost)` phát đúng một lần |
| 15 | C1 | `stop()` khi đang có 3 kết nối chat | Bên kia nhận `bye`; phía mình không phát sự kiện; các luồng `reader` thoát |
| 16 | C2 | A `open_file_connection(B)` | B: callback được gọi đúng một lần, với `(sock, A.id, A.name)`, **không** trên luồng acceptor, `sock.gettimeout() is None`; gửi 1 MB byte thô qua lại đúng |
| 17 | C2 | Đang truyền file thì chat song song | Tin chat tới bình thường, không bị chặn |
| 18 | C2 | `open_file_connection` tới peer trả `INTRO_ERR` hoặc sai `peer_id` | `ConnectError`, socket đã đóng |
| 19 | C2 | `stop()` khi socket file đã bàn giao | Socket file vẫn dùng được (transport không đóng nó) |
| 20 | C3 | `RawClient` giả lập: mình đang `connecting`, `INTRO` đến từ id lớn hơn / nhỏ hơn | Lớn hơn: `DUPLICATE`. Nhỏ hơn: nhận, `connect_chat` của mình trả về thành công |
| 21 | C3 | A và B cùng `connect_chat` nhau (dùng `threading.Barrier`), lặp 50 lần | Mỗi lần: cả hai lời gọi đều thành công; mỗi bên đúng **một** `CHAT_CONNECTED`; không có `CONNECTION_CLOSED`; kết nối được giữ do bên id nhỏ khởi tạo; chat hai chiều được |
| 22 | C3 | Bên kia nhận `DUPLICATE` nhưng không bao giờ mở kết nối của nó | `ConnectError` sau `DUPLICATE_WAIT` |
| 23 | C3 | Kết nối cũ chết một chiều, cùng peer kết nối lại (Q1) | Kết nối mới dùng được; không phát sự kiện cho kết nối cũ |

```bash
python -m unittest tests.test_transport -v
```

Test nào dính tới thời gian thì dùng `wait_event` có hạn chờ, **không** `time.sleep` cố định, để không chập chờn trên máy chậm.

## 8. Thứ tự làm

| Bước | Việc | Ước tính | Trạng thái |
|---|---|---|---|
| 0 | Chốt Q1–Q3 với nhóm; merge `events.py`; báo Huy về lệch hợp đồng ở PR #2 | 30 phút họp | [ ] |
| 1 | Khung class, `start`/`stop`, acceptor, helper test (`free_port`, `FakePeer`, `RawClient`, `wait_event`), test #1–2 | 1 giờ | [ ] |
| 2 | Bắt tay đến (`CHAT`), `connect_chat` không trùng, test #3–4, #10–12 | 1,5 giờ | [ ] |
| 3 | Luồng `reader`, `_close`, `send_chat`, `disconnect_chat`, test #5–9, #13–15 → **mở PR C1** | 2 giờ | [ ] |
| 4 | Bắt tay `FILE`, `open_file_connection`, test #16–19 → **mở PR C2**, báo Nam Khánh | 1 giờ | [ ] |
| 5 | Bảng quyết định trùng, `DUPLICATE_WAIT`, test #20–23 → **mở PR C3** | 2–3 giờ | [ ] |
| 6 | Thử bằng tay với `nc` (protocol.md mục 6), chạy test trên Windows | 30 phút | [ ] |

Tổng khoảng 9–10 giờ, chia làm 3 buổi.

## 9. Tiêu chí hoàn thành

- [ ] `python -m unittest tests.test_transport` qua hết. Chạy test #21 ít nhất 5 lần liên tiếp không lỗi, trên cả Windows và Linux/macOS.
- [ ] Không có số cứng: timeout lấy từ `protocol.py`.
- [ ] Không giữ `_cond` trong lúc gọi mạng (soát lại từng chỗ `with self._cond:` trước khi mở PR).
- [ ] Mỗi kết nối phát `CONNECTION_CLOSED` tối đa một lần; kết nối bị loại vì trùng không phát gì.
- [ ] Chữ ký khớp `CONTRIBUTING.md` 2.3. Mọi hàm public có type hint và docstring một dòng.
- [ ] Mỗi PR có Nam Khánh approve (người review chính phần Người 2).
- [ ] Thử bằng tay: `printf 'INTRO|18|CHAT|a1b2c3d4|Quan' | nc 127.0.0.1 6000` nhận về `INTRO_OK|…`.

## 10. Kiến thức cần nắm trước khi code

- `threading.Condition`: `wait_for(predicate, timeout)`, `notify_all()`; vì sao phải kiểm tra lại điều kiện sau khi thức dậy.
- `socket.create_connection(timeout=…)` và `settimeout`: timeout áp cho **cả** `send` lẫn `recv` trên socket đó.
- Khác nhau giữa `shutdown(SHUT_RDWR)` và `close()`: `shutdown` đánh thức `recv()` đang chờ ở luồng khác, còn `close()` một mình thì không chắc.
- `SO_REUSEADDR` trên Windows khác hẳn trên Linux/macOS.
- Half-open connection: một bên chết mà không gửi FIN, bên kia chỉ biết khi gửi dữ liệu.
- `unittest.mock.patch` để rút ngắn timeout; `threading.Barrier` để hai luồng chạy đúng cùng lúc.
