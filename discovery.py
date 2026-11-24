import socket 
import logging 
import queue
import re
import threading
import time
import secrets
import unicodedata

UDP_PORT = 9999
HELLO_INTERVAL = 3.0
PEER_TIMEOUT = 10.0
CLEANUP_INTERVAL = 1.0
MAX_DATAGRAM_SIZE = 512
VALID_STATUSES = {"online", "away"}

""" Hàm kiểm tra peer_id gồm 8 ký tự hex thường """
def valid_peer_id(value):
    return(
        isinstance(value,str)
        and re.fullmatch(r"[0-9a-f]{8}",value) is not None
    )

""" Kiểm tra tên """
def valid_name(value):
    if not isinstance(value,str):
        return False
    if not 1<=len(value)<=20:
        return False
    if value.isdigit():
        return False
    for character in value:
        if character == "|":
            return False
        if character.isspace():
            return False
        if unicodedata.category(character) == "Cc":
            return False
    return True

""" Phân tích thông điệp nhận được """
def parse_datagram(data):
    if not isinstance(data,bytes):
        return None
    if len(data)>MAX_DATAGRAM_SIZE:
        return None
    try:
        text=data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    fields=text.split("|")
    if fields[0] == "HELLO":
        if len(fields) != 5:
            return None
        _,peer_id,name,port_text,status=fields
        if not valid_peer_id(peer_id):
            return None
        if not valid_name(name):
            return None
        if re.fullmatch(r"[1-9][0-9]{0,4}",port_text) is None:
            return None
        tcp_port=int(port_text)
        if not 1<= tcp_port <= 65535:
            return None
        if status not in VALID_STATUSES:
            return None
        return {
            "type": "HELLO",
            "peer_id": peer_id,
            "name": name,
            "tcp_port": tcp_port,
            "status": status,
        }
    if fields[0] == "GOODBYE":
        if len(fields) != 2:
            return None
        _,peer_id=fields
        if not valid_peer_id(peer_id):
            return None
        return {
            "type": "GOODBYE",
            "peer_id": peer_id,
        }
    return None

""" Tạo dữ liệu gửi """
def build_hello(peer_id,name,tcp_port,status):
    if not valid_peer_id(peer_id):
        raise ValueError("peer_id không hợp lệ")
    if not valid_name(name):
        raise ValueError("Tên không hợp lệ")
    if type(tcp_port) is not int or not 1<=tcp_port<=65535:
        raise ValueError("TCP_PORT không hợp lệ")
    if not isinstance(status,str) or status not in VALID_STATUSES:
        raise ValueError("Trạng thái không hợp lệ")
    text=f"HELLO|{peer_id}|{name}|{tcp_port}|{status}"
    data=text.encode("utf-8")
    if len(data) > MAX_DATAGRAM_SIZE:
        raise ValueError("Datagram vượt quá 512 byte")
    return data

def build_goodbye(peer_id):
    if not valid_peer_id(peer_id):
        raise ValueError("peer_id không hợp lệ")
    return f"GOODBYE|{peer_id}".encode("utf-8")

class DiscoveryService:
    def __init__(
            self,
            name,
            tcp_port,
            broadcast_ip="255.255.255.255",
            peer_id=None,
    ):
        self.peer_id=(
            secrets.token_hex(4)
            if peer_id is None
            else peer_id
        )
        self.name=name
        self.tcp_port=tcp_port
        self.broadcast_ip=broadcast_ip
        self._status="online"
        self._peers={}
        self._state_lock=threading.Lock()
        self._tx_lock=threading.Lock()
        self._stop_event=threading.Event()
        self.events=queue.Queue()
        self._socket=None
        self._threads=[]
        self._started=False
        self._closing=False
        build_hello(
            self.peer_id,
            self.name,
            self.tcp_port,
            self._status,
        )

    """ Tạo socket UDP """
    def _create_socket(self):
        sock=socket.socket(
            socket.AF_INET,
            socket.SOCK_DGRAM,
        )
        try:
            sock.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_REUSEADDR,
                1,
            )
            if hasattr(socket,"SO_REUSEPORT"):
                try:
                    sock.setsockopt(
                        socket.SOL_SOCKET,
                        socket.SO_REUSEPORT,
                        1,
                    )
                except OSError:
                    pass
            sock.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_BROADCAST,
                1,
            )
            sock.bind(("",UDP_PORT))
            sock.settimeout(0.5)
            return sock
        except OSError:
            sock.close()
            raise
    """ Thêm hoặc cập nhật peer """
    def _upsert_peer(self,message,ip):
        peer_id=message["peer_id"]
        peer={
            "peer_id":peer_id,
            "name": message["name"],
            "ip": ip,
            "tcp_port": message["tcp_port"],
            "status": message["status"],
            "last_seen": time.monotonic(),
        }
        with self._state_lock:
            old_peer=self._peers.get(peer_id)
            self._peers[peer_id]=peer
            if old_peer is None:
                event_type="PEER_ONLINE"
            else :
                fields=(
                    "name",
                    "ip",
                    "tcp_port",
                    "status",
                )
                changed=any(
                    old_peer[field] != peer[field] for field in fields
                )
                event_type=("PEER_UPDATED" if changed else None)
            if event_type is not None:
                self.events.put({
                    "type": event_type,
                    "peer": peer.copy(),
                })
    """ Xóa peer khi có GOODBYE """
    def _remove_peer(self,peer_id):
        with self._state_lock:
            peer = self._peers.pop(peer_id,None)
            if peer is None :
                return
            self.events.put({
                "type": "PEER_OFFLINE",
                "peer": peer.copy(),
            })
    """ Cung cấp danh sách an toàn """
    def get_peers(self):
        with self._state_lock:
            return [ peer.copy() for peer in self._peers.values() ]
    """ Gửi hello 1 lần """
    def _send_hello(self):
        with self._tx_lock:
            if self._closing or self._stop_event.is_set():
                return
            with self._state_lock:
                status=self._status
            data = build_hello(
                self.peer_id,
                self.name,
                self.tcp_port,
                status,
            )
            try :
                self._socket.sendto(
                    data,
                    (self.broadcast_ip,UDP_PORT),
                )
            except OSError as error:
                logging.warning(
                    "Không gửi được HELLO: %s",
                    error,
                )
    """ Phát định kỳ """
    def _broadcast_loop(self):
        while not self._stop_event.is_set():
            self._send_hello()
            if self._stop_event.wait(HELLO_INTERVAL):
                break
    """ Đổi trạng thái và phát ngay """
    def set_status(self,status):
        if not isinstance(status,str) or status not in VALID_STATUSES:
            raise ValueError("Trạng thái phải là online hoặc away")
        if (not self._started or self._closing or self._stop_event.is_set()):
            raise RuntimeError("Discovery không hoạt động")
        with self._state_lock:
            changed = status != self._status
            self._status=status
        if changed:
            self._send_hello()
    """ Nhận và điều phối thông điệp """
    def _listen_loop(self):
        while not self._stop_event.is_set():
            try:
                data,address=self._socket.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError as error:
                if not self._stop_event.is_set():
                    logging.error(
                        "Lỗi nhận UDP: %s",
                        error,
                    )
                self._stop_event.set()
                return
            message =parse_datagram(data)
            if message is None:
                continue
            if message["peer_id"]==self.peer_id:
                continue
            if message["type"]=="HELLO":
                self._upsert_peer(message,ip=address[0],)
            if message["type"]=="GOODBYE":
                self._remove_peer(message["peer_id"],)
    """ Loại peer hết hạn """
    def _expire_peers(self,now=None):
        if now is None:
            now=time.monotonic()
        with self._state_lock:
            expired_ids = [
                peer_id
                for peer_id, peer in self._peers.items()
                if now - peer["last_seen"] > PEER_TIMEOUT
            ]
            for peer_id in expired_ids:
                peer=self._peers.pop(peer_id)
                self.events.put({
                    "type": "PEER_OFFLINE",
                    "peer": peer.copy(),
                })
    def _cleanup_loop(self):
        while not self._stop_event.wait(CLEANUP_INTERVAL):
            self._expire_peers()
    """ Khởi động socket và các worker """
    def start(self):
        if self._started:
            raise RuntimeError("Mỗi đối tượng chỉ được start 1 lần")
        self._socket=self._create_socket()
        self._started=True
        try:
            targets=(
                self._listen_loop,
                self._cleanup_loop,
                self._broadcast_loop,
            )
            for target in targets:
                thread=threading.Thread(
                    target=target,
                    name=target.__name__,
                )
                thread.start()
                self._threads.append(thread)
        except Exception:
            self._closing=True
            self._stop_event.set()
            for thread in self._threads:
                thread.join()
            self._socket.close()
            self._socket = None
            raise
    """ Gửi GOODBYE và giải phóng tài nguyên """
    def stop(self):
        if self._socket is None:
            return
        with self._tx_lock:
            self._closing=True
            self._stop_event.set()
            data=build_goodbye(self.peer_id)
            for index in range(2):
                try:
                    self._socket.sendto(
                        data,
                        (self.broadcast_ip,UDP_PORT),
                    )
                except OSError as error:
                    logging.warning(
                        "Không được gửi GOODBYE: %s",
                        error,
                    )
                if index==0:
                    time.sleep(0.1)
        for thread in self._threads:
            thread.join()
        self._socket.close()
        self._socket=None