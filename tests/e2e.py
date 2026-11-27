import hashlib
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time


def get_free_ports(count: int) -> list[int]:
    sockets = [socket.socket(socket.AF_INET, socket.SOCK_STREAM) for _ in range(count)]
    for s in sockets:
        s.bind(("", 0))
    ports = [s.getsockname()[1] for s in sockets]
    for s in sockets:
        s.close()
    return ports


def main() -> None:
    repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    main_py = os.path.join(repo_dir, "main.py")
    tmp_dir = tempfile.mkdtemp()
    names = ["Quan", "Minh", "Huy"]
    ports = get_free_ports(3)
    procs: dict[str, subprocess.Popen] = {}
    outputs: dict[subprocess.Popen, list[str]] = {}
    consumed: dict[subprocess.Popen, set[int]] = {}

    def send(proc: subprocess.Popen, line: str) -> None:
        if proc.stdin is not None:
            proc.stdin.write(line + "\n")
            proc.stdin.flush()

    def expect(proc: subprocess.Popen, text: str, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            lines = outputs.get(proc, [])
            for i, line in enumerate(lines):
                if i not in consumed[proc] and text in line:
                    consumed[proc].add(i)
                    return line
            time.sleep(0.05)
        return ""

    failed = False

    def check(name: str, cond: bool) -> None:
        nonlocal failed
        if cond:
            print(f"PASS {name}")
        else:
            print(f"FAIL {name}")
            failed = True

    try:
        for name, port in zip(names, ports):
            cwd = os.path.join(tmp_dir, name)
            os.makedirs(cwd, exist_ok=True)
            cmd = [
                sys.executable,
                "-X",
                "utf8",
                main_py,
                "--name",
                name,
                "--port",
                str(port),
                "--broadcast",
                "127.255.255.255",
            ]
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
            procs[name] = proc
            outputs[proc] = []
            consumed[proc] = set()

            def make_reader(p: subprocess.Popen) -> None:
                def run() -> None:
                    try:
                        if p.stdout is not None:
                            for line in iter(p.stdout.readline, ""):
                                outputs[p].append(line.rstrip("\r\n"))
                    except (OSError, UnicodeDecodeError):
                        pass

                threading.Thread(target=run, daemon=True).start()

            make_reader(proc)

        p_quan, p_minh, p_huy = procs["Quan"], procs["Minh"], procs["Huy"]

        c1 = bool(
            expect(p_quan, "[*] Minh is online", 8.0)
            and expect(p_quan, "[*] Huy is online", 8.0)
        )
        check("online", c1)

        send(p_quan, "/list")
        l_minh = expect(p_quan, "Minh", 4.0)
        l_huy = expect(p_quan, "Huy", 4.0)
        c2 = bool(l_minh and "127.0.0.1" in l_minh and l_huy and "127.0.0.1" in l_huy)
        check("list", c2)

        send(p_quan, "/chat Minh")
        c3 = bool(
            expect(p_quan, "[*] Connected to Minh", 5.0)
            and expect(p_minh, "[*] Quan opened a chat with you", 5.0)
        )
        check("chat_minh", c3)

        send(p_quan, "Xin chào Minh")
        c4 = bool(expect(p_minh, "New message from Quan (1 unread)", 5.0))
        check("unread_message", c4)

        expect(p_minh, "[*] Quan is online", 5.0)
        send(p_minh, "/chat Quan")
        c5 = bool(
            expect(p_minh, "Switched to Quan", 5.0)
            and expect(p_minh, "[Quan] Xin chào Minh", 5.0)
        )
        check("switch_chat", c5)

        send(p_minh, "Chào Quan")
        c6 = bool(expect(p_quan, "[Minh] Chào Quan", 5.0))
        check("reply_chat", c6)

        expect(p_huy, "[*] Quan is online", 5.0)
        send(p_huy, "/chat Quan")
        expect(p_huy, "[*] Connected to Quan", 5.0)
        send(p_huy, "hello from Huy")
        c7 = bool(expect(p_quan, "New message from Huy (1 unread)", 5.0))
        check("third_party_unread", c7)

        send(p_minh, "/status away")
        c8 = bool(expect(p_quan, "Minh is now away", 4.0))
        check("status_away", c8)

        file_path = os.path.join(tmp_dir, "Quan", "sample.bin")
        file_data = os.urandom(3 * 1024 * 1024)
        with open(file_path, "wb") as f:
            f.write(file_data)
        expected_hash = hashlib.sha256(file_data).hexdigest()

        send(p_quan, f'/send "{file_path}"')
        send(p_quan, "chat during file")
        chat_during = bool(expect(p_minh, "chat during file", 15.0))
        done_printed = bool(expect(p_quan, "Done.", 15.0))
        recv_path = os.path.join(tmp_dir, "Minh", "downloads", "sample.bin")
        file_ok = False
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if os.path.exists(recv_path) and os.path.getsize(recv_path) == len(file_data):
                try:
                    with open(recv_path, "rb") as f:
                        file_ok = hashlib.sha256(f.read()).hexdigest() == expected_hash
                    if file_ok:
                        break
                except OSError:
                    pass
            time.sleep(0.1)
        check("file_transfer", chat_during and done_printed and file_ok)

        p_huy.kill()
        c10 = bool(expect(p_quan, "Huy is offline", 15.0))
        check("kill_huy", c10)

        send(p_minh, "/quit")
        c11 = bool(expect(p_quan, "Minh is offline", 6.0))
        check("quit_minh", c11)

    finally:
        for proc in procs.values():
            try:
                proc.kill()
                proc.wait(timeout=2.0)
            except OSError:
                pass
        time.sleep(0.2)
        shutil.rmtree(tmp_dir, ignore_errors=True)

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
