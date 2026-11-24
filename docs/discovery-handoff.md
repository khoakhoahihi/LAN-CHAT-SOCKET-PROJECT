# Bàn giao discovery cho người 3

> **Trạng thái: bản bàn giao tạm thời để review.** Tài liệu mô tả code `discovery.py` đang có trên nhánh `feat/discovery`, đối chiếu tại commit `9bd6106`, ngày 24/11/2026. API hiện tại chưa hoàn toàn khớp hợp đồng tích hợp trong `CONTRIBUTING.md`.
>
> Thứ tự ưu tiên vẫn là [giao thức](protocol.md) → [hợp đồng nhóm](../CONTRIBUTING.md) → code. Tài liệu này không thay đổi hợp đồng và không có nghĩa module đã sẵn sàng merge.

## 1. Người 3 cần dùng discovery để làm gì?

| Phần việc của người 3 | Dữ liệu hoặc API cần dùng |
|---|---|
| Khởi động trong `main.py` | Tạo `DiscoveryService`, truyền đúng định danh và cổng TCP |
| `/list` | Gọi `get_peers()`, sắp xếp và lưu snapshot để đánh số |
| `/chat <tên hoặc số>` | Chọn đúng `peer_id`, lấy IP và TCP port; việc kết nối thuộc Transport |
| `/status online` hoặc `/status away` | Gọi `set_status(status)` |
| Thông báo peer vào, đổi trạng thái, rời mạng | CLI lấy sự kiện từ `discovery.events` |
| `/quit` | Gọi `stop()` để gửi GOODBYE và dừng worker |

Discovery không gửi tin chat, không truyền file, không quản lý người đang được chọn và không giữ lịch sử hoặc số tin chưa đọc.

## 2. Khởi tạo: API đang có

```python
from discovery import DiscoveryService

discovery = DiscoveryService(
    name="Quan",
    tcp_port=5000,
    broadcast_ip="255.255.255.255",
    peer_id=my_id,
)
```

`my_id` trong ví dụ phải được `main.py` tạo trước, một lần cho mỗi phiên chạy:

```python
import secrets

my_id = secrets.token_hex(4)
```

| Tham số | Kiểu | Ý nghĩa và quy tắc |
|---|---|---|
| `name` | `str` | Tên hiển thị 1–20 ký tự; không chứa `|`, khoảng trắng, ký tự điều khiển; không chỉ gồm chữ số |
| `tcp_port` | `int` | Cổng TCP listener thật, từ 1 đến 65535; không phải cổng UDP |
| `broadcast_ip` | `str` | Mặc định `255.255.255.255`; thử offline nhiều instance theo cấu hình `127.255.255.255` |
| `peer_id` | `str` hoặc `None` | 8 ký tự hex thường; nếu bỏ qua, discovery tự sinh |

**Khi tích hợp, luôn truyền `peer_id=my_id` và dùng cùng `my_id` cho Transport.** Không để hai module tự sinh hai định danh khác nhau.

Ánh xạ tham số CLI hiện tại: `--name` → `name`, `--port` → `tcp_port`, `--broadcast` → `broadcast_ip`.

Lưu ý: constructor hiện chưa nhận `udp_port`, `store` hoặc `events`. Cổng UDP đang cố định là 9999. Chưa được coi `--udp-port` là có tác dụng với module này.

## 3. Các phương thức public

| Phương thức | Trả về | Cách sử dụng và lỗi cần xử lý |
|---|---|---|
| `start()` | `None` | Tạo socket và ba worker; lỗi tạo/bind socket có thể phát sinh `OSError` |
| `get_peers()` | `list[dict]` | Lấy bản sao danh sách; có thể rỗng; chưa tự sắp xếp |
| `set_status(status)` | `None` | Chỉ nhận `online`/`away`; sai giá trị: `ValueError`; chưa chạy/đã dừng: `RuntimeError` |
| `stop()` | `None` | Gửi hai GOODBYE cách nhau khoảng 100 ms, dừng worker và đóng socket; gọi lại sau khi dừng không lỗi |

- Mỗi đối tượng chỉ `start()` một lần. Muốn chạy lại sau `stop()`, tạo đối tượng mới.
- Gọi `start()`, `set_status()` và `stop()` theo vòng đời do luồng chính điều khiển. Không gọi `stop()` từ worker của discovery vì hàm có `join()` các worker.
- `set_status()` cập nhật và gửi HELLO ngay khi trạng thái thực sự thay đổi. Đặt lại cùng trạng thái không phát thêm gói.
- `stop()` có thể chờ khoảng 100 ms giữa hai GOODBYE và chờ các worker kết thúc. Tránh giữ khóa giao diện khi gọi.
- Tên đúng là **`get_peers()`**, không phải `get_peer()`.

Các hàm bắt đầu bằng `_`, `_peers`, `_status`, `_socket` và các khóa là nội bộ. Người 3 không sửa trực tiếp các thuộc tính này.

## 4. Cấu trúc peer hiện tại

Một phần tử từ `get_peers()`:

```python
{
    "peer_id": "a1b2c3d4",
    "name": "Quan",
    "ip": "192.168.1.10",
    "tcp_port": 5000,
    "status": "online",
    "last_seen": 12345.67,
}
```

| Trường | Người 3 dùng để làm gì? |
|---|---|
| `peer_id` | Khóa thật cho chọn người chat, lịch sử, số chưa đọc; không dùng tên làm khóa |
| `name` | Hiển thị và tìm theo tên; có thể trùng giữa các peer |
| `ip` | Địa chỉ TCP đích; discovery lấy từ `recvfrom()` |
| `tcp_port` | Cổng TCP đích; lấy từ HELLO |
| `status` | Hiển thị `online` hoặc `away`; `away` vẫn là peer đang hiện diện |
| `last_seen` | Mốc `time.monotonic()` nội bộ; không phải giờ lịch để hiển thị |

Đây là dictionary: truy cập bằng `peer["name"]`, không phải `peer.name`.

Mỗi lần gọi trả danh sách và bản sao từng dictionary. Sửa bản sao không cập nhật discovery. Snapshot không tự đổi khi có HELLO mới.

## 5. Xử lý `/list` và `/chat`

### `/list`

Hiện code chưa tự sắp xếp. Trong bản giao diện thử với API hiện tại:

```python
last_list_snapshot = sorted(
    discovery.get_peers(),
    key=lambda peer: (peer["name"], peer["peer_id"]),
)
```

- CLI hiển thị số thứ tự từ 1 trên snapshot này.
- Giữ snapshot để xử lý `/chat <số>` theo đúng lần `/list` gần nhất.
- Không lấy danh sách mới rồi dùng lại số cũ: peer vào/rời mạng có thể làm số đó chỉ sang người khác.
- Danh sách rỗng: hiển thị thông báo chưa có peer.

### `/chat <số>`

1. Kiểm tra đã có snapshot và số nằm trong khoảng hợp lệ.
2. Lấy `peer_id` từ snapshot tại vị trí được chọn.
3. Tra lại theo ID trong danh sách mới để lấy IP/cổng hiện tại. Nếu không còn, thông báo peer không còn trong danh sách; không tự chuyển sang người có cùng số thứ tự.
4. Yêu cầu Transport mở hoặc dùng lại kết nối. Kết nối vẫn có thể thất bại dù peer vừa còn trong danh sách, nên phải xử lý lỗi Transport.

### `/chat <tên>`

- Không có tên phù hợp: báo không tìm thấy.
- Đúng một peer: chọn peer đó.
- Nhiều peer trùng tên: báo trùng tên, yêu cầu `/list` rồi chọn theo số.

**Chưa truyền thẳng dictionary vào `Transport.connect_chat()` theo hợp đồng nhóm:** Transport được đặc tả nhận `Peer`. Người 1 cần hoàn thiện `PeerStore`/`Peer` hoặc nhóm phải chốt adapter được review trước khi ghép thật.

## 6. Sự kiện hiện đang phát

Queue hiện tại là `discovery.events`, được discovery tự tạo. Sự kiện là dictionary, chưa phải đối tượng `Event` trong hợp đồng nhóm.

### `PEER_ONLINE`

```python
{
    "type": "PEER_ONLINE",
    "peer": {
        "peer_id": "a1b2c3d4",
        "name": "Quan",
        "ip": "192.168.1.10",
        "tcp_port": 5000,
        "status": "online",
        "last_seen": 12345.67,
    },
}
```

CLI thông báo người dùng xuất hiện và cập nhật phần hiển thị liên quan.

### `PEER_UPDATED`

```python
{"type": "PEER_UPDATED", "peer": updated_peer}
```

`updated_peer` là dictionary cùng cấu trúc mục 4. Sự kiện phát khi tên, IP, TCP port hoặc trạng thái thay đổi. HELLO lặp không đổi thông tin chỉ làm mới `last_seen`, không tạo sự kiện.

**Bản hiện tại chưa có trường `old`.** CLI không được truy cập `event["old"]` khi dùng code này.

### `PEER_OFFLINE`

```python
{"type": "PEER_OFFLINE", "peer": removed_peer}
```

`removed_peer` là bản ghi vừa bị xóa do GOODBYE hoặc timeout. **Bản hiện tại chưa có `reason`**, nên CLI chỉ thông báo offline, không tự đoán nguyên nhân.

Hai GOODBYE lặp chỉ tạo một lần offline. Peer ID của chính mình và các datagram sai bị bỏ qua.

### Cách đọc queue trong CLI thử nghiệm

```python
import queue


def drain_discovery_events(discovery, handle_event):
    """Gọi từ vòng xử lý giao diện, không tạo thêm consumer."""
    while True:
        try:
            event = discovery.events.get_nowait()
        except queue.Empty:
            break

        handle_event(event)
```

`handle_event` là hàm do CLI cung cấp. Ví dụ trong bản hiện tại, đọc `event["type"]`, `event["peer"]` và các trường của peer.

- Chỉ CLI đọc queue; không để cả `main.py` và CLI cùng lấy sự kiện vì mỗi sự kiện chỉ được lấy bởi một consumer.
- `get()` không có timeout có thể chặn; `get_nowait()` cần bắt `queue.Empty` như ví dụ.
- CLI phải thường xuyên xử lý queue hoặc tích hợp consumer với cơ chế nhập không chặn. Một vòng `input()` đơn thuần không tự hiển thị sự kiện ngay trong khi người dùng chưa nhấn Enter.
- Đoạn này chỉ minh họa queue riêng hiện tại. Hợp đồng tích hợp vẫn yêu cầu một queue chung được truyền từ `main.py`.

## 7. Vòng đời và trách nhiệm trong `main.py`

Thứ tự đích theo hợp đồng nhóm:

```text
Tạo my_id và queue chung
    → PeerStore
    → Transport.start() thành công
    → Discovery.start()
    → CLI
```

Khi thoát: dừng theo thứ tự ngược lại, bảo đảm discovery có cơ hội gửi GOODBYE trước khi tiến trình kết thúc.

Với API hiện tại, người 3 cần bảo đảm:

1. Discovery và Transport dùng cùng ID, tên và cổng TCP.
2. Không quảng bá khi TCP listener chưa sẵn sàng.
3. Lỗi cấu hình (`ValueError`) và bind socket (`OSError`) được báo qua giao diện, tài nguyên đã bật được thu hồi.
4. Dùng `try/finally` cho quá trình chạy để gọi `discovery.stop()` khi thoát bình thường hoặc có lỗi.
5. `/status` gọi API, không gán `_status` trực tiếp.
6. Peer offline theo UDP không tự đóng TCP, không xóa lịch sử và số tin chưa đọc, không hủy file đang truyền.
7. `/leave` thuộc phần chat; không gọi `discovery.stop()` cho lệnh này.

Hiện lỗi gửi/nhận UDP dùng `logging`, chưa phát `ERROR` vào queue. Nếu cần thông báo vận hành qua UI, phải hoàn thiện phần sự kiện; không chờ một `ERROR` mà module chưa phát. Logging cần được cấu hình ra file theo hợp đồng nhóm.

## 8. Những khác biệt phải giải quyết trước khi merge

| Nội dung | Code hiện tại | Hợp đồng trong `CONTRIBUTING.md` |
|---|---|---|
| Lớp discovery | `DiscoveryService` | `Discovery` |
| Định danh constructor | `peer_id` tùy chọn | `my_id` được truyền vào |
| Kho peer | Dictionary nội bộ | `PeerStore` riêng, dữ liệu `Peer` |
| Lấy danh sách | `discovery.get_peers()` trả `list[dict]`, chưa sắp xếp | `store.get_peers()` trả `list[Peer]`, sắp `(name, peer_id)` |
| Tra ID/tên | Chưa có API riêng | `store.get()` và `store.find_by_name()` |
| Queue | Tự tạo `discovery.events` | Queue chung từ `main.py` truyền vào |
| Dạng sự kiện | Dictionary có `type`, `peer` | `Event(type, data)` |
| Cập nhật peer | Chưa có `old` | `data` có `peer` và `old` |
| Offline | Chưa có `reason` | `reason` là `timeout` hoặc `goodbye` |
| Lỗi UDP | Ghi log | Chuẩn `ERROR` với `source`, `message` khi cần báo lỗi |
| Cổng UDP/địa chỉ đích | Cổng cố định, tham số `broadcast_ip` | Constructor nhận `udp_port`, `broadcast_addr` |
| Hằng số | Đặt trong `discovery.py` | Dùng chung từ `protocol.py` |
| Worker | Không đặt `daemon=True`, Event riêng | Quy tắc nhóm yêu cầu daemon và cơ chế dừng chung |

Ưu tiên người 1 hoàn thiện theo hợp đồng đã có; người 3 không tự đổi cấu trúc chung để khớp bản tạm. Nếu nhóm quyết định đổi hợp đồng, thực hiện qua PR và review theo `CONTRIBUTING.md`.

## 9. Checklist phối hợp

### Người 1 cần hoàn thiện

- [ ] Chốt API với người 2 và người 3; hoàn thiện `PeerStore`/`Peer` theo hợp đồng.
- [ ] Nhận queue chung và phát đúng kiểu `Event`, đủ `old`/`reason`.
- [ ] Đồng bộ tham số, hằng số và quy tắc worker với repository.
- [ ] Bổ sung/duy trì kiểm thử tự động trong repository và kiểm thử broadcast hai máy.

### Người 3 cần làm

- [ ] Dùng cùng `my_id`, tên và TCP port khi bật các module.
- [ ] Xử lý `/list`, snapshot số thứ tự, tên trùng và peer rời mạng.
- [ ] Chỉ CLI đọc sự kiện, in thông báo và bảo toàn dòng nhập.
- [ ] Xử lý lỗi API; gọi `stop()` khi thoát, không khi `/leave`.
- [ ] Giữ lịch sử, số chưa đọc và trạng thái TCP tách biệt với danh sách UDP.

### Cả nhóm xác nhận trước khi merge

- [ ] Khởi động được bằng `main.py` với đúng API, không chỉ chạy discovery độc lập.
- [ ] Chọn theo số không nhầm người khi danh sách thay đổi.
- [ ] Đổi trạng thái hiển thị được, nhận GOODBYE và timeout cập nhật đúng UI.
- [ ] Chat/file tiếp tục theo trạng thái TCP khi peer hết hạn UDP.
- [ ] Thoát không còn worker giữ tiến trình hoặc xuất hiện traceback.

## 10. Tài liệu tham chiếu

- [Code discovery hiện tại](../discovery.py).
- [Giao thức UDP/TCP](protocol.md).
- [Hợp đồng module, sự kiện và quy trình review](../CONTRIBUTING.md).

Sau mỗi lần đổi API hoặc schema sự kiện, cập nhật tài liệu này cùng code và ghi rõ phiên bản mô tả để người 3 không sử dụng nhầm bản cũ.
