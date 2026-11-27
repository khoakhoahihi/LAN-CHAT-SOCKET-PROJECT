# Tích hợp discovery và peer store — dành cho người 3

Tài liệu mô tả API `Discovery` hiện tại. Nguồn chuẩn vẫn là
[docs/protocol.md](protocol.md) và [CONTRIBUTING.md](../CONTRIBUTING.md).

## 1. Khởi tạo trong main.py

`main.py` tạo một ID và một queue chung cho các module. Khởi động TCP
listener thành công trước khi bật discovery để không quảng bá cổng chưa sẵn sàng.

```python
import queue

from discovery import Discovery
from events import Event
from peer_store import PeerStore
from protocol import UDP_PORT, new_id

my_id = new_id()
events: queue.Queue[Event] = queue.Queue()
store = PeerStore()

# Truyền cùng my_id, tên, TCP port và events cho Transport; bật Transport trước.
discovery = Discovery(
    my_id=my_id,
    name="Quan",
    tcp_port=5001,
    store=store,
    events=events,
    udp_port=UDP_PORT,
    broadcast_addr="255.255.255.255",
)
```

Gọi `discovery.start()` khi các module đã sẵn sàng. Dùng `try/finally` trong
vòng đời chương trình để gọi `discovery.stop()` khi thoát. Lỗi bind socket
được giữ nguyên dưới dạng `OSError` để `main.py` xử lý và thu hồi tài nguyên.

Thử nhiều instance offline trên một máy: dùng `127.255.255.255`, cùng cổng
UDP nhưng TCP port khác nhau. Cấu hình này cần kiểm tra trên hệ điều hành thực tế.

## 2. API public cần dùng

| API | Kết quả và trách nhiệm |
|---|---|
| `discovery.start()` | Bật listener, cleaner và broadcaster; mỗi đối tượng chỉ start một lần |
| `discovery.set_status(status)` | Chỉ nhận `online`/`away`, sai thì `ValueError`; đổi khi đang chạy gửi HELLO ngay |
| `discovery.stop()` | Gửi hai GOODBYE, đóng socket, chờ worker; gọi lại an toàn |
| `store.get_peers()` | `list[Peer]` bản sao, đã sắp `(name, peer_id)` |
| `store.get(peer_id)` | Bản sao `Peer`, hoặc `None` |
| `store.find_by_name(name)` | Danh sách bản sao theo tên chính xác; có thể nhiều kết quả |

Nếu `set_status()` được gọi trước start hoặc khi đang/đã dừng, trạng thái hợp
lệ vẫn được lưu nhưng không gửi UDP. Đặt lại cùng trạng thái không gửi thêm.
`start()` lặp trên cùng đối tượng gây `RuntimeError`; muốn chạy lại, tạo đối
tượng mới. Gọi API vòng đời từ luồng chính, không gọi stop từ worker.

`upsert()`, `remove()`, `remove_expired()` là phần ghi do discovery sử dụng.
CLI không sửa kho hoặc truy cập `_peers`, `_status`, socket và các khóa nội bộ.

## 3. Dữ liệu Peer

`Peer` là `@dataclass(frozen=True)` với các trường:

```text
peer_id: str       định danh thật, không dùng tên thay ID
name: str          tên hiển thị; có thể trùng
ip: str            lấy từ recvfrom(), dùng để kết nối TCP
tcp_port: int      cổng listener TCP, không phải cổng UDP
status: str        online hoặc away
last_seen: float   time.monotonic(), không phải giờ lịch
```

Đọc bằng `peer.name`, `peer.ip`, không dùng cú pháp dictionary
`peer["name"]`. Các hàm đọc trả bản sao; snapshot không tự cập nhật khi có HELLO mới.

## 4. /list và /chat

- `/list`: lưu `last_list_snapshot = store.get_peers()` rồi hiển thị số từ 1.
- `/chat <số>`: lấy ID theo snapshot của lần `/list` gần nhất. Tra lại bằng
  `store.get(peer_id)` để lấy IP/cổng mới nhất; nếu không còn, báo offline.
  Không dùng số cũ trên một danh sách mới vì có thể chọn nhầm người.
- `/chat <tên>`: dùng `store.find_by_name(name)`. Không có thì báo không tìm
  thấy; có một thì chọn; có nhiều thì yêu cầu `/list` và chọn theo số.
- Truyền `Peer` đã chọn cho `Transport.connect_chat()`; xử lý lỗi kết nối
  vì địa chỉ có thể không còn khả dụng ngay cả khi vừa nhận HELLO.

## 5. Sự kiện trên queue chung

Sự kiện là đối tượng `Event`, đọc `event.type` và `event.data`:

| Loại | data |
|---|---|
| `PEER_ONLINE` | `peer: Peer` |
| `PEER_UPDATED` | `peer: Peer`, `old: Peer` |
| `PEER_OFFLINE` | `peer: Peer`, `reason: timeout` hoặc `goodbye` |
| `ERROR` | `source: udp`, `message: str` |

Tên loại và giá trị reason/source lấy từ hằng số trong `events.py`.
Chỉ CLI đọc queue; các module khác chỉ put. Dùng queue không giới hạn như
ví dụ khởi tạo, và xử lý sự kiện thường xuyên để không giữ luồng mạng chờ UI.

HELLO lặp chỉ thay đổi last_seen, không tạo PEER_UPDATED. GOODBYE lặp chỉ tạo
một lần offline. `away` vẫn là đang hiện diện. Offline theo UDP không đóng
TCP, không xóa lịch sử/số chưa đọc và không hủy file đang truyền.

## 6. Hành vi khi có lỗi mạng

- Reset UDP tạm thời (ví dụ lỗi Windows 10054 sau ICMP) không dừng discovery.
- Lỗi nhận tạm thời khác được thử lại có khoảng chờ; lỗi lặp chỉ báo một lần
  cho đến khi nhận lại được datagram. Gói sai giao thức vẫn bị loại im lặng.
- Socket không hợp lệ hoặc bị shutdown khiến module báo ERROR rồi dừng
  worker. main.py vẫn gọi stop để đóng tài nguyên; không tự khởi động lại đối tượng.
- Lỗi gửi liên tục chỉ báo một ERROR cho đến lần gửi thành công tiếp theo.
  sendto thành công không chứng minh peer đã nhận gói.
- Logging dùng logger `discovery`; main.py cấu hình handler ghi file theo quy tắc nhóm.

## 7. Kiểm thử và review

Tại thư mục gốc repository:

```powershell
python -m unittest tests.test_peer_store tests.test_discovery -v
python -m unittest discover -s tests -v
```

Test gồm parser/builder, kho bất biến và bản sao, tên trùng, timeout, 5 luồng,
sự kiện đúng schema, phục hồi sau reset, hạn chế lỗi lặp, vòng đời và hai
instance broadcast loopback trên một cổng UDP thử riêng.

Test loopback không thay thế kiểm tra hai máy trong LAN hoặc minh chứng Wireshark.
Không commit pcap/pcapng. Giữ trạng thái PR và mô tả kiểm thử đúng với những gì
thực sự đã chạy; các hằng số mới trong protocol và đặc tả cần nhóm review trước merge.
