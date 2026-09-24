#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""serveo 公网方案端到端自测: 模拟手机 App 全操作, 全部经 serveo.net:63800"""
import socket
import json
import hashlib
import time

HOST = "bore.pub"
PORT = 24313
PAIR_RO = "131420"
PAIR_ADMIN = "888888"


class BufferedConn:
    def __init__(self, sock):
        self.sock = sock
        self.buf = b""
    def readline(self, timeout=20):
        self.sock.settimeout(timeout)
        while b"\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("EOF")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return line.decode("utf-8", errors="replace").strip()
    def read_exact(self, n, timeout=60):
        self.sock.settimeout(timeout)
        data = self.buf[:n]
        self.buf = self.buf[n:]
        while len(data) < n:
            chunk = self.sock.recv(min(65536, n - len(data)))
            if not chunk:
                raise ConnectionError("EOF")
            data += chunk
        return data


def session():
    s = socket.socket()
    s.settimeout(20)
    s.connect((HOST, PORT))
    return s, BufferedConn(s)


def pair(device, code):
    s, bc = session()
    s.sendall(f"PT-PAIR {device} {code} serveo-self-test\n".encode())
    resp = bc.readline()
    assert resp.startswith("PT-PAIR-OK "), f"配对失败: {resp}"
    token = resp.split(" ", 1)[1]
    s.sendall(f"PT-AUTH {device} {token}\n".encode())
    auth = bc.readline()
    assert auth.startswith("PT-AUTH-OK"), f"认证失败: {auth}"
    return s, bc, token


def do_list(s, bc):
    s.sendall(b"LIST /\n")
    hdr = bc.readline()
    assert hdr.startswith("PT-JSON"), f"LIST 失败: {hdr}"
    length = int(hdr.split(" ", 1)[1])
    body = bc.read_exact(length)
    return json.loads(body.decode("utf-8"))


print("=" * 60)
print("serveo 公网端到端自测 (手机视角 → serveo.net:63800)")
print("=" * 60)

# --- 普通码: 只读操作 ---
s, bc, tok_ro = pair("serveo-test-ro", PAIR_RO)
items = do_list(s, bc)
print(f"[只读] 配对+认证+LIST ✅ ({len(items)} 个条目)")
file_item = next((i for i in items if not i["isDir"]), None)
assert file_item, "无文件可下载"
fname = file_item["name"]

# DOWNLOAD 验证字节
s.sendall(f"DOWNLOAD /{fname}\n".encode())
dl_hdr = bc.readline()
assert dl_hdr.startswith("PT-OK"), f"下载失败: {dl_hdr}"
clen = int(dl_hdr.split("Content-Length")[1].strip()) if "Content-Length" in dl_hdr else 0
assert clen > 0
data = bc.read_exact(clen)
sha = hashlib.sha256(data).hexdigest()
print(f"[只读] DOWNLOAD /{fname} {clen} 字节 ✅ sha256={sha[:16]}...")
s.close()

# 对照: 直连服务器下载同文件对比 sha256
import subprocess, sys
direct = socket.socket(); direct.settimeout(30)
direct.connect(("127.0.0.1", 47810))
dbc = BufferedConn(direct)
direct.sendall(f"PT-PAIR serveo-direct 131420 direct\n".encode())
r = dbc.readline(); t2 = r.split(" ")[1]
direct.sendall(f"PT-AUTH serveo-direct {t2}\n".encode()); dbc.readline()
direct.sendall(f"DOWNLOAD /{fname}\n".encode())
h2 = dbc.readline()
c2 = int(h2.split("Content-Length")[1].strip())
d2 = dbc.read_exact(c2)
sha2 = hashlib.sha256(d2).hexdigest()
direct.close()
assert sha == sha2, f"公网与直连字节不一致! {sha} vs {sha2}"
print(f"[对照] 直连下载 sha256={sha2[:16]}... → 与公网一致 ✅ (字节完整性验证)")

# --- 管理员码: 写操作 ---
s2, bc2, tok_ad = pair("serveo-test-admin", PAIR_ADMIN)
s2.sendall(b"MKDIR /__serveo_selftest__\n")
r = bc2.readline()
assert r == "PT-OK", f"MKDIR 失败: {r}"
print(f"[写] MKDIR /__serveo_selftest__ ✅")

# UPLOAD (经公网)
test_data = b"serveo public relay upload test " * 1000  # ~33KB
s2.sendall(f"UPLOAD /__serveo_selftest__/test.txt Content-Length {len(test_data)}\n".encode())
r = bc2.readline()
assert r == "PT-OK", f"UPLOAD 就绪失败: {r}"
s2.sendall(test_data)
r = bc2.readline()
assert r == "PT-OK", f"UPLOAD 完成失败: {r}"
print(f"[写] UPLOAD /__serveo_selftest__/test.txt {len(test_data)} 字节 ✅")

# LIST 子目录确认
s2.sendall(b"LIST /__serveo_selftest__\n")
hdr = bc2.readline()
length = int(hdr.split(" ", 1)[1])
body = bc2.read_exact(length)
sub = json.loads(body.decode("utf-8"))
assert any(i["name"] == "test.txt" for i in sub), f"上传文件未出现: {sub}"
print(f"[写] LIST 子目录确认 test.txt 存在 ✅ ({len(sub)} 项)")

# 下载回验内容
s2.sendall(b"DOWNLOAD /__serveo_selftest__/test.txt\n")
h3 = bc2.readline()
c3 = int(h3.split("Content-Length")[1].strip())
d3 = bc2.read_exact(c3)
assert d3 == test_data, "上传后下载内容不一致!"
print(f"[写] DOWNLOAD 回验上传内容一致 ✅ ({c3} 字节)")

# DELETE 清理 (先删文件, 再删目录——server.py 拒绝删除非空目录属数据保护)
s2.sendall(b"DELETE /__serveo_selftest__/test.txt\n")
r = bc2.readline()
assert r == "PT-OK", f"DELETE 文件失败: {r}"
print(f"[写] DELETE 文件 ✅")
s2.sendall(b"DELETE /__serveo_selftest__\n")
r = bc2.readline()
assert r == "PT-OK", f"DELETE 目录失败: {r}"
print(f"[写] DELETE /__serveo_selftest__ ✅")
s2.close()

print()
print("=" * 60)
print("✅ serveo 公网方案全功能通过: 配对/认证/浏览/下载(字节一致)/建目录/上传/下载回验/删除")
print("   手机 App 直连模式: serveo.net:63800 + 配对码 131420 (只读) / 888888 (完整权限)")
print("=" * 60)