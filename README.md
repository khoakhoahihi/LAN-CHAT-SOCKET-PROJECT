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
- Thư viện: chỉ dùng thư viện chuẩn của Python, không cần `pip install` gì thêm.

## Cài đặt

```bash
git clone https://github.com/khoakhoahihi/LAN-CHAT-SOCKET-PROJECT.git
cd LAN-CHAT-SOCKET-PROJECT
```

**Windows:** nên chạy trong Windows Terminal hoặc PowerShell (hỗ trợ ANSI). Lần đầu chạy, khi Windows Firewall hỏi, chọn **Allow** cho Python ở cả mạng Private lẫn Public. Nếu không, chương trình sẽ không nhận được gói broadcast.

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
| `--gui` | tắt | Mở cửa sổ đồ hoạ (tkinter) thay cho giao diện console |

### Giao diện cửa sổ

Thêm `--gui` để dùng cửa sổ thay cho console. Lõi chương trình giữ nguyên, các lệnh `/list`, `/chat`, `/send`... vẫn gõ được trong ô nhập.

```bash
python main.py --name Quan --gui
```

- Cột trái: ô tìm kiếm lọc theo tên, rồi danh sách người đang online với avatar màu, chấm trạng thái (xanh là online, cam là away) và huy hiệu số tin chưa đọc. Bấm vào một người để mở chat; người đang chat được tô xanh đặc và hiện tên ở thanh tiêu đề. Cột trái tự thu hẹp khi cửa sổ hẹp.
- Khung chat: tin của mình là bong bóng xanh lá nhạt bên phải, tin của bạn màu trắng bên trái, có giờ gửi; tin liên tiếp của cùng một người trong 5 phút được gộp nhóm. Thông báo hệ thống hiện ở giữa, lỗi màu đỏ, và vẫn hiện khi chưa chọn ai.
- Gửi file có thanh tiến độ phía trên ô nhập. Nút `+` mở hộp chọn file, nút *Status* đổi `online`/`away`, nút *Dark mode* đổi giao diện sáng hoặc tối.
- Hướng dẫn chi tiết từng thao tác, kèm ảnh: [`docs/huong-dan-gui.md`](docs/huong-dan-gui.md).
- Cần `tkinter`, đã có sẵn trong Python trên Windows và macOS. Trên Ubuntu cài thêm: `sudo apt install python3-tk`.

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
├── terminal.py        # đọc phím, vẽ lại dòng nhập khi có tin đến          (Người 3)
├── gui.py             # cửa sổ tkinter, dùng chung lõi lệnh với cli.py     (Người 3)
├── chat_state.py      # người đang chat, lịch sử, số tin chưa đọc            (Người 3)
├── file_transfer.py   # gửi và nhận file, tiến độ, downloads/              (Người 3)
├── discovery.py       # UDP broadcaster, listener, kiểm tra timeout          (Người 1)
├── peer_store.py      # danh sách peer, dataclass Peer                       (Người 1)
├── protocol.py        # hằng số, send_frame, recv_frame, recv_exact          (Người 2)
├── transport.py       # TCP listener, kết nối chat, phân loại kết nối file   (Người 2)
├── events.py          # định nghĩa sự kiện dùng chung                         (cả nhóm)
├── tests/             # test cho từng module; e2e.py chạy 3 instance thật
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

### Test end-to-end

Tự mở 3 instance (Quan, Minh, Huy) trên một máy rồi chạy kịch bản: thấy nhau, chat, tin chưa đọc, đổi người chat, đổi trạng thái, gửi file kiểm SHA-256, tắt đột ngột một peer, thoát. In `PASS` hoặc `FAIL` cho từng bước.

```bash
python tests/e2e.py
```

### Kiểm tra nhanh bằng netcat

```bash
# Xem gói HELLO đang được broadcast
nc -ul 9999

# Giả làm một peer: gửi INTRO tới chương trình đang chạy ở port 5001
printf 'INTRO|18|CHAT|a1b2c3d4|Quan' | nc 127.0.0.1 5001
```

### Kiểm thử tích hợp

`tests/e2e.py` đã phủ phần lớn danh sách dưới đây trên một máy. Các mục cần hai máy thật thì kiểm tay trước buổi demo:

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
