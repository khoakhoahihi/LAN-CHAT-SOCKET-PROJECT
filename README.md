# LAN Chat & File Sharing

Ứng dụng chat ngang hàng (peer-to-peer) chạy trên console, dùng trong mạng LAN. Mọi người chạy cùng một chương trình. Chương trình tự tìm những người đang online trong mạng, cho phép chọn người để chat và gửi file trong khi vẫn đang chat.

Đồ án môn Socket Programming. Chỉ dùng socket API gốc của Python (`socket.socket` với `SOCK_STREAM` và `SOCK_DGRAM`), không dùng WebSocket, HTTP hay thư viện mạng bậc cao.

| Việc | Giao thức | Lý do |
|---|---|---|
| Tìm peer, cập nhật trạng thái | UDP broadcast, cổng 9999 | Nhanh, không cần kết nối, mất một gói cũng không sao |
| Chat | TCP, mỗi cặp peer một kết nối | Tin cần đến đủ và đúng thứ tự |
| Gửi file | TCP, mỗi file một kết nối riêng | Gửi file không chặn chat |

Chi tiết định dạng thông điệp nằm trong [`docs/protocol.md`](docs/protocol.md).

## Yêu cầu

- Python 3.10 trở lên.
- Các máy chạy chung một mạng LAN hoặc Wi-Fi. Cũng có thể chạy nhiều instance trên một máy.
- Thư viện: chỉ dùng thư viện chuẩn.
  > Nếu nhóm chọn `prompt_toolkit` cho giao diện (phương án a ở mục 4.3 của đề), cài thêm bằng `pip install -r requirements.txt` và cập nhật dòng này.

## Cài đặt

```bash
git clone <url-repo>
cd lanchat
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate
pip install -r requirements.txt   # chỉ cần khi có requirements.txt
```

**Windows:** lần đầu chạy, khi Windows Firewall hỏi, chọn **Allow** cho Python ở cả mạng Private lẫn Public. Nếu không, chương trình sẽ không nhận được gói broadcast.

## Chạy

```bash
python main.py --name Quan
```

| Tham số | Mặc định | Ý nghĩa |
|---|---|---|
| `--name` | *(bắt buộc)* | Tên hiển thị: 1–20 ký tự, không có dấu cách hay `\|`, không chỉ gồm toàn chữ số |
| `--port` | `5000` | Cổng TCP để nhận chat và file |
| `--udp-port` | `9999` | Cổng UDP cho discovery. Mọi peer phải dùng chung cổng này |
| `--broadcast` | `255.255.255.255` | Địa chỉ broadcast. Dùng `127.255.255.255` khi chạy trên một máy không có mạng |
| `--downloads` | `downloads` | Thư mục lưu file nhận được |

### Trên nhiều máy

Mỗi máy chạy một lệnh, cổng TCP có thể trùng nhau vì khác máy:

```bash
python main.py --name Quan
python main.py --name Minh
```

### Nhiều instance trên một máy

Mỗi instance phải dùng một cổng TCP khác nhau. Mở mỗi lệnh trong một cửa sổ terminal riêng:

```bash
python main.py --name Quan --port 5001 --broadcast 127.255.255.255
python main.py --name Minh --port 5002 --broadcast 127.255.255.255
python main.py --name Huy  --port 5003 --broadcast 127.255.255.255
```

## Lệnh

Gõ chữ bình thường để gửi tin cho người đang chat. Dòng bắt đầu bằng `/` là lệnh.

| Lệnh | Tác dụng |
|---|---|
| `/list` | Hiện người đang online: số thứ tự, tên, IP, trạng thái |
| `/chat <tên\|số>` | Chọn người để chat. Số là số thứ tự trong lần `/list` gần nhất |
| `<văn bản>` | Gửi tin cho người đang chat |
| `/send <file>` | Gửi file cho người đang chat. Đường dẫn có dấu cách vẫn dùng được |
| `/leave` | Đóng cuộc chat hiện tại. Tin chưa đọc vẫn được giữ |
| `/status <online\|away>` | Đổi trạng thái, người khác thấy trong khoảng 1 giây |
| `/help` | Hiện danh sách lệnh |
| `/quit` | Thoát chương trình |

### Phiên mẫu

```text
$ python main.py --name Quan
[*] Listening on TCP port 5000
[*] Broadcasting presence on UDP port 9999
[*] Minh is online
> /list
 1. Minh   (192.168.1.12)  online
 2. Huy    (192.168.1.20)  away
> /chat Minh
[*] Connected to Minh (192.168.1.12:6000)
> Hi Minh
[Quan] Hi Minh
[Minh] Xin chào
[*] New message from Huy (1 unread)
> /send slides.pdf
[*] Sending slides.pdf (2.4 MB) ... 100%  Done.
> /quit
```

File nhận được nằm trong `downloads/`. Nếu trùng tên, file mới được đổi thành `slides (1).pdf`, `slides (2).pdf`, …

## Cấu trúc mã nguồn

```text
lanchat/
├── main.py            # điểm khởi động: đọc tham số, bật và tắt các module   (Người 3)
├── cli.py             # dòng nhập, phân tích lệnh, hiển thị                (Người 3)
├── chat_state.py      # người đang chat, lịch sử, số tin chưa đọc            (Người 3)
├── file_transfer.py   # gửi và nhận file, tiến độ, downloads/              (Người 3)
├── discovery.py       # UDP broadcaster, listener, kiểm tra timeout          (Người 1)
├── peer_store.py      # danh sách peer, dataclass Peer                       (Người 1)
├── protocol.py        # hằng số, send_frame, recv_frame, recv_exact          (Người 2)
├── transport.py       # TCP listener, kết nối chat, phân loại kết nối file   (Người 2)
├── events.py          # định nghĩa sự kiện dùng chung                         (cả nhóm)
├── tests/             # test cho từng module
├── docs/
│   ├── protocol.md    # đặc tả giao thức
│   └── report/        # báo cáo, ảnh Wireshark
├── CONTRIBUTING.md    # quy tắc làm việc nhóm, hợp đồng giữa các module
└── README.md
```

Kiến trúc tóm tắt:
- Các luồng mạng không bao giờ tự in ra màn hình. Chúng chỉ đẩy sự kiện vào một `queue.Queue` chung.
- Giao diện là nơi duy nhất đọc hàng đợi này và in ra màn hình, có khóa bảo vệ.
- Danh sách luồng chạy và hợp đồng giữa các module nằm trong [`CONTRIBUTING.md`](CONTRIBUTING.md).

## Kiểm thử

### Test tự động

```bash
python -m unittest discover tests
```

### Kiểm tra nhanh bằng netcat

```bash
# Xem gói HELLO đang được broadcast
nc -ul 9999

# Giả làm một peer: gửi INTRO tới chương trình đang chạy ở port 5001
printf 'INTRO|18|CHAT|a1b2c3d4|Quan' | nc 127.0.0.1 5001
```

### Kiểm thử tích hợp

Chạy hết danh sách kiểm thử tích hợp trong tài liệu phân công trước mỗi lần merge mốc và trước buổi demo. Tối thiểu phải kiểm tra:

- [ ] Hai máy tự thấy nhau; tắt một máy thì máy kia báo offline sau khoảng 10 giây.
- [ ] Chat hai chiều, tin tiếng Việt và nhiều tin liên tiếp hiển thị đúng.
- [ ] Đang gõ dở thì có tin đến, phần đang gõ không bị mất.
- [ ] Gửi file lớn trong khi vẫn chat được; file nhận có SHA-256 giống file gốc.
- [ ] Tắt đột ngột bên kia giữa lúc gửi file: không crash, file `.part` bị xoá.

## Xử lý sự cố

| Hiện tượng | Nguyên nhân thường gặp | Cách xử lý |
|---|---|---|
| Không thấy ai trong `/list` | Firewall chặn Python | Cho phép Python qua firewall |
| Cùng Wi-Fi mà vẫn không thấy nhau | Mạng trường hoặc quán cà phê bật *client isolation* | Dùng hotspot điện thoại hoặc router riêng |
| `Address already in use` | Cổng TCP đang bị instance khác dùng | Đổi `--port` |
| Chạy một máy không thấy nhau | Broadcast không đi qua loopback | Thêm `--broadcast 127.255.255.255` |
| Ký tự tiếng Việt bị lỗi trên Windows | Terminal không dùng UTF-8 | Dùng Windows Terminal, hoặc chạy `chcp 65001` |

## Nhóm

| Thành viên | Phụ trách |
|---|---|
| Người 1 — *Phạm Gia Huy* | UDP discovery, danh sách peer, Wireshark UDP |
| Người 2 — *Mai Nguyễn Đăng Khoa* | Giao thức TCP, framing, quản lý kết nối, Wireshark TCP |
| Người 3 — *Lê Nam Khánh* | Giao diện, hội thoại, truyền file, ghép chương trình |
