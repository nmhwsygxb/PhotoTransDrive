# -*- coding: utf-8 -*-
"""模拟 Android 网盘客户端 DriveClient.kt 的完整协议操作，端到端验证 server.py。

覆盖 DriveActivity/DriveClient 会用到的每条协议路径：
配对(PT-PAIR/含空格设备名) → 认证(PT-AUTH) → LIST → MKDIR → UPLOAD → DOWNLOAD(DELETE) →
含空格路径 → 子目录 joinPath 行为 → UDP 自动发现(PT-DISCOVER)。

与 DriveClient.kt 的命令序列逐字节一致（\n 结尾、UTF-8、PT-JSON len 精确读、Content-Length 解析）。
"""
import json
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HOST = "127.0.0.1"
PORT = 47997
UDP_PORT = 47811

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  <- {detail}" if detail else ""))


# ── 与 DriveClient.kt 一致的底层 IO ──
def connect():
    s = socket.create_connection((HOST, PORT), timeout=15)
    s.settimeout(60)
    return s


def read_line(sock):
    data = bytearray()
    while True:
        b = sock.recv(1)
        if not b:
            return None
        if b == b"\n":
            return data.decode("utf-8", errors="replace").strip("\r")
        data += b


def write_line(sock, line):
    sock.sendall((line + "\n").encode("utf-8"))


def pair(sock, device_id, code, name):
    write_line(sock, f"PT-PAIR {device_id} {code} {name}")
    resp = read_line(sock)
    if resp and resp.startswith("PT-PAIR-OK "):
        return resp.removeprefix("PT-PAIR-OK ").strip()
    return resp


def auth_cmd(sock, device_id, token, cmd_line):
    """认证后下一条指令（与 DriveClient.command() 一致），返回响应行。"""
    write_line(sock, f"PT-AUTH {device_id} {token}")
    auth = read_line(sock)
    if not auth or not auth.startswith("PT-AUTH-OK"):
        return auth
    write_line(sock, cmd_line)
    return read_line(sock)


def list_dir(sock, device_id, token, path):
    """与 DriveClient.list() 一致（每个 LIST 独立连接）。"""
    s = connect()
    try:
        write_line(s, f"PT-AUTH {device_id} {token}")
        if not (read_line(s) or "").startswith("PT-AUTH-OK"):
            return None
        write_line(s, f"LIST {path}")
        hdr = read_line(s)
        if not hdr or not hdr.startswith("PT-JSON"):
            return hdr
        length = int(hdr.split(" ", 1)[1])
        buf = bytearray()
        while len(buf) < length:
            chunk = s.recv(length - len(buf))
            if not chunk:
                break
            buf += chunk
        return json.loads(bytes(buf).decode("utf-8"))
    finally:
        s.close()


def upload(sock, device_id, token, remote_path, content: bytes):
    """与 DriveClient.upload() 一致。"""
    s = connect()
    try:
        write_line(s, f"PT-AUTH {device_id} {token}")
        if not (read_line(s) or "").startswith("PT-AUTH-OK"):
            return "认证失败"
        write_line(s, f"UPLOAD {remote_path} Content-Length {len(content)}")
        ready = read_line(s)
        if ready != "PT-OK":
            return ready
        s.sendall(content)
        resp = read_line(s)
        return None if resp == "PT-OK" else resp
    finally:
        s.close()


def download(sock, device_id, token, remote_path):
    """与 DriveClient.download() 一致，返回 (成功, 内容)。"""
    s = connect()
    try:
        write_line(s, f"PT-AUTH {device_id} {token}")
        if not (read_line(s) or "").startswith("PT-AUTH-OK"):
            return False, None
        write_line(s, f"DOWNLOAD {remote_path}")
        resp = read_line(s)
        if not resp or not resp.startswith("PT-OK Content-Length"):
            return False, resp
        size = int(resp.split("Content-Length ")[1])
        buf = bytearray()
        while len(buf) < size:
            chunk = s.recv(size - len(buf))
            if not chunk:
                break
            buf += chunk
        return len(buf) == size, bytes(buf)
    finally:
        s.close()


def delete(sock, device_id, token, path):
    resp = auth_cmd(sock, device_id, token, f"DELETE {path}")
    return resp == "PT-OK"


def join_path(parent, name):
    """与 DriveActivity.joinPath() 一致。"""
    return f"{parent}{name}" if parent.endswith("/") else f"{parent}/{name}"


def discover():
    """与 DriveClient.discover() 一致：广播到 127.0.0.1 收 PT-DRIVE。"""
    found = []
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.settimeout(2)
    try:
        udp.sendto(b"PT-DISCOVER\n", (HOST, UDP_PORT))
        while True:
            try:
                data, addr = udp.recvfrom(512)
                msg = data.decode("utf-8").strip()
                if msg.startswith("PT-DRIVE|"):
                    port = int(msg.removeprefix("PT-DRIVE|").strip() or PORT)
                    found.append((addr[0], port))
                    break
            except socket.timeout:
                break
    finally:
        udp.close()
    return found


def main():
    root = Path(tempfile.mkdtemp(prefix="pt_sim_"))
    env = dict(sys._getframe().f_globals)
    proc = subprocess.Popen(
        [sys.executable, "server.py", "--root", str(root),
         "--pair-code", "123456", "--admin-code", "888888",
         "--port", str(PORT)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        cwd=str(Path(__file__).parent),
    )
    time.sleep(2.5)
    try:
        did = "android-sim000001"
        token = None

        # 1. 配对（设备名含空格 + 管理员码 → 完整权限，验证 BUG-226/DriveClient.pair）
        s0 = connect()
        t0 = pair(s0, did, "888888", "OPPO Find X6")
        s0.close()
        check("配对(设备名含空格)获 token", bool(t0) and not t0.startswith("PT-"), str(t0)[:24] if t0 else t0)
        if t0 and not t0.startswith("PT-"):
            token = t0
        else:
            print("  无法继续：配对失败"); return 1

        # 2. LIST /（首屏）
        items = list_dir(None, did, token, "/")
        check("LIST / 空目录", items is not None and items == [], repr(items))

        # 3. MKDIR /sub（父为 /，joinPath 后 "//" 不应出现 → "/sub"）
        s = connect()
        r = auth_cmd(s, did, token, "MKDIR /sub")
        s.close()
        check("MKDIR /sub", r == "PT-OK", str(r))

        # 4. 子目录 joinPath：currentPath="/sub" 后 MKDIR → "/sub/内 层"（含空格，验证 BUG-237 修复路径）
        s = connect()
        r = auth_cmd(s, did, token, f"MKDIR {join_path('/sub', '内 层')}")
        s.close()
        check("子目录 MKDIR(joinPath+空格)", r == "PT-OK", f"path={join_path('/sub', '内 层')} r={r}")

        # 5. 上传中文/空格文件到子目录（joinPath 路径）
        content = ("你好 PhotoTrans 网盘 测试内容 123").encode("utf-8")
        remote = join_path("/sub", "测试 文件.txt")
        err = upload(None, did, token, remote, content)
        check(f"子目录 UPLOAD {remote}", err is None, str(err))

        # 6. LIST /sub 应看到 1 个目录 + 1 个文件
        items = list_dir(None, did, token, "/sub")
        names = sorted(i["name"] for i in items) if isinstance(items, list) else []
        check("LIST /sub 含 内层+测试文件", names == ["内 层", "测试 文件.txt"], str(names))

        # 7. DOWNLOAD 回读校验内容一致
        ok, got = download(None, did, token, remote)
        check("DOWNLOAD 回读内容一致", ok and got == content, str(len(got or b"")) + " bytes")

        # 8. 删除内层目录（空）→ 删测试文件 → 删 /sub
        s = connect()
        r = auth_cmd(s, did, token, f"DELETE {join_path('/sub', '内 层')}")
        s.close()
        check("DELETE 空子目录", r == "PT-OK", str(r))
        s = connect()
        r = auth_cmd(s, did, token, f"DELETE {remote}")
        s.close()
        check("DELETE 测试文件", r == "PT-OK", str(r))
        s = connect()
        r = auth_cmd(s, did, token, "DELETE /sub")
        s.close()
        check("DELETE /sub(已空)", r == "PT-OK", str(r))

        # 9. UDP 自动发现（DriveClient.discover 同路径）
        found = discover()
        check("UDP 发现返回 PT-DRIVE", len(found) == 1 and found[0][1] == PORT, str(found))

        # 10. 只读配对码验证权限（DriveActivity 用普通码配对 → 只读，UPLOAD 应被拒）
        s = connect()
        rt = pair(s, "android-sim-ro", "123456", "ReadOnly")
        s.close()
        err = upload(None, "android-sim-ro", rt, "/ro.txt", b"x")
        check("只读设备 UPLOAD 被拒", err is not None and "只读" in str(err), str(err))
    finally:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    passed = sum(results)
    total = len(results)
    print(f"\n结果: {passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())