# -*- coding: utf-8 -*-
"""PhotoTrans 网盘冒烟测试：双配对码权限分级 + 核心文件操作。

覆盖：
1. 普通配对码 → 只读权限（能浏览/下载，不能上传/建目录/删除）
2. 管理员配对码 → 完整权限（所有操作）
3. 含空格文件名上传/下载/删除
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HOST = "127.0.0.1"
PORT = 47999
PAIR_CODE = "123456"      # 普通配对码（只读）
ADMIN_CODE = "888888"     # 管理员配对码（完整）


def recv_line(sock):
    data = b""
    while not data.endswith(b"\n"):
        chunk = sock.recv(1)
        if not chunk:
            break
        data += chunk
    return data.decode("utf-8", errors="replace").strip()


def recv_exact(sock, n):
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            break
        data += chunk
    return data


def send(sock, line):
    sock.sendall(line.encode("utf-8") + b"\n")


def pair_and_auth(sock, code, name):
    """配对 + 认证，返回 (成功, 详情)。"""
    send(sock, f"PT-PAIR smoke-{name} {code} SmokeTest-{name}")
    resp = recv_line(sock)
    if not resp.startswith("PT-PAIR-OK"):
        return False, f"配对失败: {resp}"
    token = resp.split()[-1]
    send(sock, f"PT-AUTH smoke-{name} {token}")
    resp = recv_line(sock)
    return resp.startswith("PT-AUTH-OK"), resp


def main():
    root = Path(tempfile.mkdtemp(prefix="pt_smoke_"))
    here = Path(__file__).parent

    exe = here / "dist" / "PhotoTransDrive.exe"
    if exe.exists():
        server_cmd = [str(exe), "--root", str(root),
                      "--pair-code", PAIR_CODE, "--admin-code", ADMIN_CODE,
                      "--port", str(PORT)]
        print(f"启动目标: {exe.name}")
    else:
        server_cmd = [sys.executable, "server.py", "--root", str(root),
                      "--pair-code", PAIR_CODE, "--admin-code", ADMIN_CODE,
                      "--port", str(PORT)]
        print("启动目标: server.py（未找到 EXE）")

    proc = subprocess.Popen(server_cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, cwd=str(here))

    results = []

    def check(name, ok, detail=""):
        results.append(ok)
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  <- {detail}" if detail else ""))

    try:
        sock = None
        for _ in range(50):
            try:
                sock = socket.create_connection((HOST, PORT), timeout=1)
                break
            except OSError:
                time.sleep(0.2)
        if sock is None:
            print("FAIL: 服务端未能启动")
            return 1

        # ── 1. 普通配对码 → 只读设备 ──
        ok, resp = pair_and_auth(sock, PAIR_CODE, "readonly")
        check("普通配对码 配对+认证", ok, resp)

        # 只读设备：LIST 允许
        send(sock, "LIST /")
        resp = recv_line(sock)
        ok = resp.startswith("PT-JSON")
        if ok:
            length = int(resp.split()[1])
            recv_exact(sock, length)  # 读掉 JSON 体，避免残留
        check("只读设备 LIST 允许", ok, resp)

        # 只读设备：MKDIR 拒绝
        send(sock, "MKDIR /readonly_test")
        resp = recv_line(sock)
        check("只读设备 MKDIR 拒绝", "只读权限" in resp, resp)

        # 只读设备：UPLOAD 拒绝
        content = b"test".ljust(16, b"x")
        send(sock, f"UPLOAD /ro.txt Content-Length {len(content)}")
        resp = recv_line(sock)
        check("只读设备 UPLOAD 拒绝", "只读权限" in resp, resp)

        # 只读设备：DELETE 拒绝
        send(sock, "DELETE /ro.txt")
        resp = recv_line(sock)
        check("只读设备 DELETE 拒绝", "只读权限" in resp, resp)

        sock.close()

        # ── 2. 管理员配对码 → 完整设备 ──
        sock = socket.create_connection((HOST, PORT), timeout=3)
        ok, resp = pair_and_auth(sock, ADMIN_CODE, "admin")
        check("管理员配对码 配对+认证", ok, resp)

        # MKDIR 允许
        send(sock, "MKDIR /testdir")
        resp = recv_line(sock)
        check("管理员 MKDIR 允许", resp == "PT-OK", resp)

        # UPLOAD 允许（含空格文件名）
        content = "hello phototrans admin full access".encode()
        fname = "/testdir/hello world.txt"
        send(sock, f"UPLOAD {fname} Content-Length {len(content)}")
        resp = recv_line(sock)
        ready = (resp == "PT-OK")
        if ready:
            sock.sendall(content)
            resp = recv_line(sock)
        check("管理员 UPLOAD 允许(含空格)", ready and resp == "PT-OK", resp)

        # LIST 允许
        send(sock, "LIST /testdir")
        resp = recv_line(sock)
        if resp.startswith("PT-JSON"):
            length = int(resp.split()[1])
            items = json.loads(recv_exact(sock, length).decode("utf-8"))
            names = [it["name"] for it in items]
            check("管理员 LIST 允许", "hello world.txt" in names, str(names))
        else:
            check("管理员 LIST 允许", False, resp)

        # DOWNLOAD 允许
        send(sock, "DOWNLOAD /testdir/hello world.txt")
        resp = recv_line(sock)
        if resp.startswith("PT-OK Content-Length"):
            length = int(resp.split()[-1])
            body = recv_exact(sock, length)
            check("管理员 DOWNLOAD 允许", body == content, f"{len(body)} bytes")
        else:
            check("管理员 DOWNLOAD 允许", False, resp)

        # DELETE 允许
        send(sock, "DELETE /testdir/hello world.txt")
        resp = recv_line(sock)
        check("管理员 DELETE 允许", resp == "PT-OK", resp)

        # ── 3. 含空格目录 + 特殊文件名（回归验证）──
        send(sock, "MKDIR /my folder")
        resp = recv_line(sock)
        check("MKDIR 含空格目录", resp == "PT-OK", resp)

        send(sock, "LIST /my folder")
        resp = recv_line(sock)
        if resp.startswith("PT-JSON"):
            length = int(resp.split()[1])
            items = json.loads(recv_exact(sock, length).decode("utf-8"))
            check("LIST 含空格目录", isinstance(items, list), str(items))
        else:
            check("LIST 含空格目录", False, resp)

        # 上传文件名含 "Content-Length"（rfind 修复验证）
        content2 = b"special name test"
        fname2 = "/my folder/Content-Length.txt"
        send(sock, f"UPLOAD {fname2} Content-Length {len(content2)}")
        resp = recv_line(sock)
        ready2 = (resp == "PT-OK")
        if ready2:
            sock.sendall(content2)
            resp = recv_line(sock)
        check("UPLOAD 文件名含 Content-Length", ready2 and resp == "PT-OK", resp)

        sock.close()

        # ── 4. 设备名含空格（PT-PAIR 用 raw_line 解析，需新连接）──
        sock2 = socket.create_connection((HOST, PORT), timeout=3)
        send(sock2, "PT-PAIR smoke-space 123456 My Phone 2")
        resp = recv_line(sock2)
        ok_space = resp.startswith("PT-PAIR-OK")
        if ok_space:
            token = resp.split()[-1]
            send(sock2, f"PT-AUTH smoke-space {token}")
            resp = recv_line(sock2)
            ok_space = resp.startswith("PT-AUTH-OK") and "My Phone 2" in resp
        check("设备名含空格完整保存", ok_space, resp)
        sock2.close()
    finally:
        # 终止服务进程；PyInstaller onefile 在 Windows 会 fork 子进程，terminate 可能不够，
        # 最后用 taskkill 兜底清理，避免残留进程占用端口/文件。
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        if os.name == "nt":
            try:
                subprocess.run(["taskkill", "/F", "/IM", "PhotoTransDrive.exe"],
                               capture_output=True, timeout=10)
            except Exception:
                pass

    total = len(results)
    passed = sum(results)
    print(f"\n结果: {passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
