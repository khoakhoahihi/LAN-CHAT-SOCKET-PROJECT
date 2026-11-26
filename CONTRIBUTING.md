# Quy tắc làm việc nhóm

Tài liệu này gồm: ai giữ phần nào, hợp đồng giữa các module, quy tắc viết code, quy trình Git và tiêu chí hoàn thành. Định dạng thông điệp mạng nằm riêng trong [`docs/protocol.md`](docs/protocol.md).

Thứ tự ưu tiên khi các tài liệu mâu thuẫn: **`docs/protocol.md` > file này > code**. Thấy mâu thuẫn thì sửa tài liệu trước, rồi mới sửa code.

---

## 1. Phân công và quyền sở hữu file

| File | Chủ sở hữu | Người review chính |
|---|---|---|
| `discovery.py`, `peer_store.py` | Người 1 | Người 2 |
| `protocol.py`, `transport.py` | Người 2 | Người 3 |
| `cli.py`, `chat_state.py`, `file_transfer.py`, `main.py` | Người 3 | Người 1 |
| `events.py`, `docs/protocol.md`, file này | Cả nhóm | Cả ba cùng duyệt |

- Chủ sở hữu quyết định cách cài đặt bên trong file của mình.
- Muốn sửa file của người khác: mở PR và nhờ chủ sở hữu review. Không sửa thẳng rồi merge.
- Người review chính phải đọc hiểu được file đó. Khi demo, giảng viên có thể hỏi bất kỳ ai về bất kỳ phần nào.

## 2. Hợp đồng giữa các module

Đây là phần **phải chốt trước khi code riêng**. Đổi tên hàm, tham số, giá trị trả về hay cách báo lỗi đều phải qua PR được cả ba người duyệt.

### 2.1 Dữ liệu dùng chung

```python
# peer_store.py
@dataclass(frozen=True)
class Peer:
    peer_id: str      # 8 ký tự hex, định danh thật
    name: str
    ip: str           # lấy từ recvfrom()
    tcp_port: int
    status: str       # "online" | "away"
    last_seen: float  # time.monotonic()
```

`Peer` là bất biến (`frozen=True`). Mọi hàm trả về `Peer` hoặc danh sách `Peer` đều trả **bản sao**, nên bên dùng không thể làm hỏng dữ liệu bên trong.

### 2.2 Người 1 — `discovery.py`, `peer_store.py`

```python
class PeerStore:
    def get_peers(self) -> list[Peer]          # sắp theo (name, peer_id); /list đánh số theo thứ tự này
    def get(self, peer_id: str) -> Peer | None
    def find_by_name(self, name: str) -> list[Peer]   # có thể trả nhiều peer nếu trùng tên

class Discovery:
    def __init__(self, my_id: str, name: str, tcp_port: int,
                 store: PeerStore, events: queue.Queue,
                 udp_port: int, broadcast_addr: str): ...
    def start(self) -> None     # lỗi bind cổng UDP: raise OSError, main.py báo lỗi rồi thoát
    def set_status(self, status: str) -> None   # status sai: raise ValueError; đúng: gửi HELLO ngay
    def stop(self) -> None      # gửi GOODBYE, dừng các luồng, đóng socket; gọi nhiều lần không lỗi
```

### 2.3 Người 2 — `protocol.py`, `transport.py`

```python
# protocol.py
class ProtocolError(Exception): ...
class ConnectionClosed(Exception): ...
def recv_exact(sock, n: int) -> bytes
def send_frame(sock, ftype: str, payload: bytes) -> None   # không tự khóa; transport khóa thay
def recv_frame(sock) -> tuple[str, bytes]                   # đọc header từng byte, không đọc dư

# Kiểm tra trường và tạo/đọc payload (protocol.md mục 0 và 2.4); parse_* sai cấu trúc: raise ProtocolError
def is_valid_name(name: str) -> bool        # main.py dùng để kiểm tra --name
def is_valid_peer_id(peer_id: str) -> bool  # Người 1 dùng khi đọc HELLO
def make_intro(kind: str, peer_id: str, name: str) -> bytes
def parse_intro(payload: bytes) -> tuple[str, str, str]     # (kind, peer_id, name)
def parse_intro_ok(payload: bytes) -> tuple[str, str]       # (peer_id, name)
def parse_intro_err(payload: bytes) -> tuple[str, str]      # (code, message)

# transport.py
class ConnectError(Exception): ...    # message đọc được cho người dùng
class NotConnected(Exception): ...

FileHandler = Callable[[socket.socket, str, str], None]   # (sock, peer_id, name)

class Transport:
    def __init__(self, my_id: str, name: str, tcp_port: int,
                 events: queue.Queue, on_file_connection: FileHandler): ...
    def start(self) -> None
    def connect_chat(self, peer: Peer) -> None        # đã có kết nối thì dùng lại; lỗi: raise ConnectError
    def send_chat(self, peer_id: str, text: str) -> None
        # chưa có kết nối: raise NotConnected
        # text chứa \n hoặc \r, hoặc dài hơn MAX_FRAME_PAYLOAD byte UTF-8: raise ValueError
        # lỗi socket khi gửi: đóng kết nối (CONNECTION_CLOSED, reason "lost") rồi raise NotConnected
    def disconnect_chat(self, peer_id: str) -> None   # gửi BYE và đóng; không có kết nối thì bỏ qua
    def is_connected(self, peer_id: str) -> bool
    def open_file_connection(self, peer: Peer) -> socket.socket
        # đã xong INTRO FILE / INTRO_OK; từ đây socket thuộc về người gọi
        # lỗi connect, timeout, INTRO_ERR hoặc peer_id trong INTRO_OK không khớp: raise ConnectError
    def stop(self) -> None
```

- `on_file_connection` được gọi **trên một luồng mới** do `Transport` tạo, sau khi đã gửi `INTRO_OK`. Không gọi trên luồng acceptor.
- Từ lúc gọi `on_file_connection` hoặc trả về từ `open_file_connection`, `Transport` không đọc, không ghi, không đóng socket đó nữa.
- Socket file được bàn giao ở chế độ blocking (`settimeout(None)`). Người 3 tự đặt timeout cho phần truyền file (protocol.md mục 4.1).
- `connect_chat` có thể chặn tới khoảng **10 giây**: `CONNECT_TIMEOUT` 3 s + `INTRO_TIMEOUT` 5 s + `DUPLICATE_WAIT` 2 s. Nên gọi nó trên một luồng phụ để dòng nhập không bị đứng. Không gọi khi đang giữ khóa màn hình.
- `CHAT_CONNECTED` chỉ phát một lần cho mỗi kết nối được đăng ký. Kết nối bị loại vì trùng (protocol.md mục 3.2) không phát sự kiện nào.

### 2.4 Người 3 — `file_transfer.py`, `chat_state.py`, `cli.py`, `main.py`

```python
# file_transfer.py
def send_file(sock, path: str, peer_id: str, events: queue.Queue) -> None
def receive_file(sock, peer_id: str, name: str, events: queue.Queue, downloads_dir: str) -> None
# Cả hai đều chạy trên luồng riêng, tự đóng socket khi xong, không raise ra ngoài.
# Mọi kết quả được báo bằng sự kiện: FILE_COMPLETED, FILE_UNCONFIRMED (chỉ bên gửi) hoặc ERROR.

# chat_state.py
class ChatState:
    current: str | None                              # peer_id đang chat
    def add_message(self, peer_id: str, sender: str, text: str) -> bool  # True nếu là người đang chat
    def switch_to(self, peer_id: str) -> list[Message]   # trả tin chưa đọc rồi đặt bộ đếm về 0
    def unread(self, peer_id: str) -> int
```

`main.py` tạo `events = queue.Queue()`, khởi động theo thứ tự `PeerStore → Transport → Discovery → CLI`, và dừng theo thứ tự ngược lại.

### 2.5 Sự kiện (`events.py`)

```python
@dataclass(frozen=True)
class Event:
    type: str
    data: dict
```

| `type` | Ai phát | `data` |
|---|---|---|
| `PEER_ONLINE` | Người 1 | `peer: Peer` |
| `PEER_UPDATED` | Người 1 | `peer: Peer`, `old: Peer` |
| `PEER_OFFLINE` | Người 1 | `peer: Peer`, `reason: "timeout" \| "goodbye"` |
| `CHAT_CONNECTED` | Người 2 | `peer_id`, `name`, `incoming: bool` |
| `CHAT_RECEIVED` | Người 2 | `peer_id`, `name`, `text` |
| `CONNECTION_CLOSED` | Người 2 | `peer_id`, `name`, `reason: "local_leave" \| "bye" \| "lost" \| "protocol_error"` |
| `FILE_PROGRESS` | Người 3 | `file_id`, `peer_id`, `filename`, `direction: "send" \| "recv"`, `done: int`, `total: int` |
| `FILE_COMPLETED` | Người 3 | `file_id`, `peer_id`, `filename`, `direction`, `path` (chỉ khi nhận) |
| `FILE_UNCONFIRMED` | Người 3 | `file_id`, `peer_id`, `filename` (chỉ bên gửi) |
| `ERROR` | Tất cả | `source: "udp" \| "tcp" \| "file" \| "cli"`, `message: str`, `file_id` (nếu liên quan) |

- Chỉ `cli.py` đọc hàng đợi `events`. Các module khác chỉ `put()`.
- `FILE_COMPLETED` chỉ phát khi chắc chắn thành công: bên gửi nhận `FILE_DONE` có hash khớp, bên nhận đã lưu file và gửi `FILE_DONE`. Ba kết quả của bên gửi được liệt kê ở protocol.md mục 4.2.
- `CHAT_CONNECTED` và `FILE_UNCONFIRMED` được thêm so với danh sách trong tài liệu phân công. `CHAT_CONNECTED` dùng để thông báo khi có người khác mở chat tới mình.
- Muốn thêm loại sự kiện: thêm vào bảng này trong cùng PR.

## 3. Quy tắc lập trình

### 3.1 Luồng, khóa và socket

1. **Không `print` ngoài `cli.py`.** Module khác cần báo gì thì phát sự kiện.
2. **Mỗi socket chỉ có một nơi đọc.** Khi người 2 bàn giao socket file cho người 3, luồng của người 2 dừng hẳn việc đọc socket đó.
3. **Bàn giao cả thông tin đã đọc.** Frame `INTRO` của kết nối file đã bị người 2 đọc, nên người 2 truyền `peer_id` và `name` kèm socket. Không đọc lại header đã tiêu thụ.
4. **Mọi lần ghi lên cùng một socket chat đi qua cùng một khóa gửi** do `Transport` giữ.
5. **Không giữ khóa trong lúc chờ mạng hoặc đọc ghi file.** Trong khóa chỉ được đọc hoặc sửa dữ liệu trong bộ nhớ. Cần dữ liệu thì copy ra, nhả khóa, rồi mới làm tiếp.
6. **Ba trạng thái tách riêng:** peer đang online (Người 1), kết nối TCP đang mở (Người 2), người đang được chọn chat (Người 3). Không module nào suy ra trạng thái của module khác.
7. **Peer offline theo UDP không đóng kết nối TCP.** Giao diện có thể ghi chú "(offline)", còn kết nối chỉ đóng theo các trường hợp ở `protocol.md` mục 3.3.
8. **`/leave`** đóng kết nối chat với người đang chọn, giữ nguyên lịch sử và tin chưa đọc, không đụng các file đang truyền.
9. **Mọi luồng là `daemon=True`** và dừng theo một `threading.Event` chung. `stop()` của mỗi module đóng socket để các lệnh `recv()`/`accept()` đang chờ thoát ra.
10. **Đo thời gian bằng `time.monotonic()`**, không dùng `time.time()`.

### 3.2 Phong cách code

- Python 3.10+, theo PEP 8. Có type hint cho hàm public.
- Mỗi hàm public có docstring một dòng.
- Hằng số chỉ khai báo trong `protocol.py` (danh sách ở `protocol.md` mục 5).
- Không bắt lỗi rỗng kiểu `except: pass`. Bắt lỗi cụ thể, rồi phát `ERROR` hoặc ghi log.
- Log gỡ lỗi dùng `logging`, ghi ra file `logs/lanchat-<name>.log`, không in ra màn hình. Thư mục `logs/` đã nằm trong `.gitignore`.
- Không commit file nhận được, file `.pcap` hay file test lớn.

## 4. Git

### 4.1 Nhánh

| Nhánh | Dùng cho |
|---|---|
| `main` | Luôn chạy được. Chỉ nhận merge qua PR |
| `feat/discovery` | Người 1 |
| `feat/transport` | Người 2 |
| `feat/cli-file` | Người 3 |
| `fix/<mô-tả-ngắn>` | Sửa lỗi nhỏ |
| `docs/<mô-tả-ngắn>` | Sửa tài liệu |

Kéo `main` về nhánh của mình ít nhất mỗi ngày làm việc (`git pull --rebase origin main`), để conflict được phát hiện khi còn nhỏ.

### 4.2 Commit

Viết theo dạng `<loại>(<phạm vi>): <mô tả ngắn>`. Mô tả bằng tiếng Việt hoặc tiếng Anh đều được, nhưng cả nhóm dùng một thứ tiếng.

```text
feat(discovery): gửi GOODBYE khi thoát
fix(transport): recv_frame đọc dư byte khi header bị cắt
docs(protocol): thêm mã lỗi TOO_LARGE
test(file): kiểm tra xoá file .part khi mất kết nối
```

Các loại dùng được: `feat`, `fix`, `docs`, `test`, `refactor`, `chore`.

### 4.3 Pull Request

- Mỗi PR làm một việc. Nên dưới khoảng 400 dòng thay đổi.
- Mô tả PR gồm: làm gì, test bằng cách nào, có đổi hợp đồng (mục 2) hay không.
- Cần **ít nhất 1 người khác approve**. Riêng PR sửa `protocol.md`, `events.py` hoặc mục 2 của file này cần **cả 3 người** approve.
- Người tạo PR tự merge sau khi được duyệt. Dùng **Squash and merge**.
- Không force-push lên `main`.

## 5. Kiểm thử

| Mức | Ai làm | Cách làm |
|---|---|---|
| Module | Chủ sở hữu | `tests/test_<module>.py` chạy bằng `python -m unittest` |
| Thử bằng tay | Chủ sở hữu | Dùng `nc` (UDP, TCP) như trong README. Người 3 dùng `fake_net.py` để đẩy sự kiện giả |
| Tích hợp | Cả nhóm | Chạy danh sách kiểm thử tích hợp mỗi khi merge một mốc |
| Test chéo | Mỗi người test module của người khác | Ở mốc 5, tập trung vào các kịch bản lỗi |

Test tối thiểu cho từng module:
- **`protocol.py`:** frame rỗng; frame 64 KiB; tin tiếng Việt; nhiều frame liền nhau trong một lần `recv`; header bị cắt thành nhiều lần `recv`; `LEN` không phải số; `LEN` vượt giới hạn.
- **`peer_store.py`:** thêm, cập nhật và hết hạn peer; bỏ qua gói của chính mình; 5 luồng ghi cùng lúc không lỗi.
- **`file_transfer.py`:** file 0 byte; file nhiều MB so khớp SHA-256; tên file chứa `../`; mất kết nối giữa chừng thì xoá `.part`; trùng tên thì thêm hậu tố.

## 6. Tiêu chí hoàn thành một task

Một task chỉ được đánh dấu **Xong** khi:

- [ ] Code đã merge vào `main` qua PR có người review.
- [ ] Có test tự động hoặc kịch bản test bằng tay ghi trong mô tả PR.
- [ ] Chạy được cùng các module khác trên `main`, không chỉ chạy riêng trên nhánh.
- [ ] Không có traceback khi gặp các lỗi liên quan (mục 5.3 của đề).
- [ ] Tài liệu liên quan (`protocol.md`, README, file này) đã cập nhật nếu có thay đổi.

## 7. Giao tiếp

- Bị chặn bởi phần của người khác quá một ngày: báo ngay trong nhóm chat, đừng tự viết lại phần của họ.
- Trước mỗi mốc, họp ngắn khoảng 15 phút: mỗi người nói đã xong gì, đang vướng gì, cần gì từ người khác.
- Quyết định thiết kế chốt trong buổi họp phải được ghi lại vào tài liệu (`protocol.md` hoặc file này) trong vòng một ngày.
