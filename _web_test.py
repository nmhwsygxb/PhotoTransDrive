# -*- coding: utf-8 -*-
"""Web UI 权限回归测试：只读设备不能上传/删除，管理员可以。

用 http.client 模拟浏览器（登录 → 拿 cookie → 上传/删除）。
"""
import http.client
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

HOST = "127.0.0.1"
PORT = 18080
PAIR_CODE = "123456"      # 普通（只读）
ADMIN_CODE = "888888"     # 管理员（完整）


class Client:
    def __init__(self):
        self.cookie = None

    def req(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection(HOST, PORT, timeout=5)
        h = dict(headers or {})
        if body is not None and "Content-Length" not in h:
            h["Content-Length"] = str(len(body))
        if self.cookie:
            h["Cookie"] = self.cookie
        conn.request(method, path, body=body, headers=h)
        resp = conn.getresponse()
        data = resp.read()
        sc = resp.getheader("Set-Cookie")
        if sc:
            self.cookie = sc.split(";")[0]
        conn.close()
        return resp.status, data

    def login(self, code, name):
        body = json.dumps({"pair_code": code, "device_name": name}).encode()
        st, data = self.req("POST", "/api/login", body=body,
                            headers={"Content-Type": "application/json"})
        return st, data


def main():
    root = Path(tempfile.mkdtemp(prefix="pt_web_"))
    here = Path(__file__).parent

    proc = subprocess.Popen(
        [sys.executable, "web_ui.py", "--root", str(root),
         "--pair-code", PAIR_CODE, "--admin-code", ADMIN_CODE,
         "--port", str(PORT)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(here))

    results = []

    def check(name, ok, detail=""):
        results.append(ok)
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  <- {detail}" if detail else ""))

    try:
        # 等待服务就绪
        for _ in range(50):
            try:
                c = http.client.HTTPConnection(HOST, PORT, timeout=1)
                c.request("GET", "/api/status")
                c.getresponse().read()
                c.close()
                break
            except OSError:
                time.sleep(0.2)

        # ── 只读设备 ──
        ro = Client()
        st, _ = ro.login(PAIR_CODE, "RO")
        check("只读设备登录", st == 200, f"status={st}")

        st, data = ro.req("POST", "/api/upload?path=/", body=b"hello",
                          headers={"X-Filename": "ro.txt"})
        check("只读设备上传被拒(403)", st == 403, f"status={st} {data[:60]!r}")

        st, data = ro.req("POST", "/api/delete?path=/ro.txt")
        check("只读设备删除被拒(403)", st == 403, f"status={st} {data[:60]!r}")

        # ── 管理员设备 ──
        ad = Client()
        st, _ = ad.login(ADMIN_CODE, "ADMIN")
        check("管理员登录", st == 200, f"status={st}")

        st, data = ad.req("POST", "/api/upload?path=/", body=b"hello admin",
                          headers={"X-Filename": "admin file.txt"})
        ok_up = (st == 200 and b'"success": true' in data)
        check("管理员上传成功", ok_up, f"status={st} {data[:80]!r}")

        st, data = ad.req("GET", "/api/list?path=/")
        ok_list = (st == 200 and b"admin file.txt" in data)
        check("管理员列表可见上传文件", ok_list, f"status={st}")

        st, data = ad.req("POST", f"/api/delete?path={quote('/admin file.txt')}")
        ok_del = (st == 200 and b'"success": true' in data)
        check("管理员删除成功", ok_del, f"status={st} {data[:80]!r}")

        # ── 暴力破解锁定（连续失败 5 次后第 6 次应 429）──
        bf = Client()
        for i in range(5):
            st, _ = bf.login("000000", f"BF{i}")  # 错误配对码
        st6, data6 = bf.login("000000", "BF6")
        check("登录失败 5 次后第 6 次被锁(429)", st6 == 429, f"status={st6} {data6[:60]!r}")

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
