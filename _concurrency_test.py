# -*- coding: utf-8 -*-
"""并发限制验证：MAX_PER_IP=4 时第 5 个连接应被拒绝（防单点占满）。"""
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HOST = "127.0.0.1"
PORT = 47998
PAIR_CODE = "123456"


def recv_line(sock):
    data = b""
    while not data.endswith(b"\n"):
        chunk = sock.recv(1)
        if not chunk:
            break
        data += chunk
    return data.decode("utf-8", errors="replace").strip()


def send(sock, line):
    sock.sendall(line.encode("utf-8") + b"\n")


def pair_and_auth(sock, name):
    """配对 + 认证，认证成功后连接保持在 _command_loop（保持打开）。"""
    send(sock, f"PT-PAIR smoke-{name} {PAIR_CODE} Concurrency-{name}")
    resp = recv_line(sock)
    if not resp.startswith("PT-PAIR-OK"):
        return False
    token = resp.split()[-1]
    send(sock, f"PT-AUTH smoke-{name} {token}")
    resp = recv_line(sock)
    return resp.startswith("PT-AUTH-OK")


def main():
    root = Path(tempfile.mkdtemp(prefix="pt_conc_"))
    here = Path(__file__).parent
    exe = here / "dist" / "PhotoTransDrive.exe"
    if exe.exists():
        cmd = [str(exe), "--root", str(root), "--pair-code", PAIR_CODE,
               "--admin-code", "888888", "--port", str(PORT)]
        print(f"启动目标: {exe.name}")
    else:
        cmd = [sys.executable, "server.py", "--root", str(root),
               "--pair-code", PAIR_CODE, "--admin-code", "888888", "--port", str(PORT)]
        print("启动目标: server.py（未找到 EXE）")

    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2.0)

    results = []

    def check(name, ok, detail=""):
        results.append(ok)
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  <- {detail}" if detail else ""))

    try:
        # 开 4 个连接（达到 MAX_PER_IP），认证后保持打开
        conns = []
        for i in range(4):
            sock = socket.create_connection((HOST, PORT), timeout=5)
            ok = pair_and_auth(sock, f"c{i}")
            check(f"第 {i+1} 个连接认证成功", ok)
            conns.append(sock)

        # 第 5 个连接应被拒绝：服务端 accept 后立即 close，客户端 recv 返回空
        rejected = False
        try:
            sock5 = socket.create_connection((HOST, PORT), timeout=5)
            sock5.settimeout(5)
            data = sock5.recv(1024)
            rejected = (data == b"")
            sock5.close()
        except (ConnectionResetError, ConnectionAbortedError, OSError):
            rejected = True
        check("第 5 个连接被拒绝（MAX_PER_IP=4）", rejected)

        # 关闭前 4 个连接，释放计数
        for s in conns:
            s.close()

        # 释放后新连接应能成功
        time.sleep(0.3)
        sock6 = socket.create_connection((HOST, PORT), timeout=5)
        ok6 = pair_and_auth(sock6, "c5")
        check("释放后新连接可再次认证", ok6)
        sock6.close()

    finally:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    total = len(results)
    passed = sum(results)
    print(f"\n结果: {passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
