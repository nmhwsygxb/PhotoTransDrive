#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PhotoTransDrive · bore.pub 公网转发一键启动
===========================================
原理: 家里电脑 bore 客户端 → bore.pub 公网端口 → 本地 server.py:47810
手机 App 用【直连模式】填: 地址 bore.pub  端口 <脚本打印的端口>  配对码 <--pair-code>

免注册、开箱即用: 首次运行自动下载 bore 客户端 (GitHub release)。

用法:
    python bore-up.py                     # 默认配对码 131420
    python bore-up.py --pair-code 123456
    python bore-up.py --local-port 47810

特性:
    - 自动启动 server.py (若未运行)
    - 自动下载 bore (若缺失; 也可用 --bore-command 指定系统 bore)
    - 解析 bore.pub 分配的随机公网端口并打印
    - 断线自动重连 (重新分配端口, 再次打印)
    - 日志: ~/.phototransdrive/bore-up.log
"""
import argparse
import os
import re
import subprocess
import sys
import time
import urllib.request

BORE_RELEASES = {
    "v0.5.1": {
        "windows": "https://github.com/ekzhang/bore/releases/download/v0.5.1/bore-v0.5.1-x86_64-pc-windows-msvc.zip",
        "linux": "https://github.com/ekzhang/bore/releases/download/v0.5.1/bore-v0.5.1-x86_64-unknown-linux-musl.tar.gz",
        "macos": "https://github.com/ekzhang/bore/releases/download/v0.5.1/bore-v0.5.1-x86_64-apple-darwin.tar.gz",
    }
}
LOG_PATH = os.path.expanduser("~/.phototransdrive/bore-up.log")


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def is_port_listening(port: int) -> bool:
    import socket
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", port))
        s.close()
        return True
    except Exception:
        return False


def ensure_server(port: int, pair_code: str, python: str, script_dir: str):
    if is_port_listening(port):
        log(f"server.py 已在运行 (端口 {port}), 复用")
        return None
    server_py = os.path.join(script_dir, "server.py")
    log(f"启动 server.py --port {port} --pair-code {pair_code}")
    proc = subprocess.Popen(
        [python, server_py, "--port", str(port), "--pair-code", pair_code],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(20):
        if is_port_listening(port):
            return proc
        time.sleep(0.5)
    log("警告: server.py 未在 10s 内监听")
    return proc


def ensure_bore(script_dir: str) -> str:
    """确保 bore 可执行文件, 返回命令路径。"""
    # 1) 系统 bore
    import shutil
    sys_bore = shutil.which("bore")
    if sys_bore:
        return sys_bore
    # 2) 本地缓存
    cache = os.path.join(script_dir, "bin", "bore.exe" if os.name == "nt" else "bore")
    if os.path.isfile(cache):
        return cache
    # 3) 下载 (带超时和重试)
    log("未找到 bore, 自动下载 bore 客户端...")
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    url = None
    if os.name == "nt":
        url = BORE_RELEASES["v0.5.1"]["windows"]
        ext = ".zip"
    elif sys.platform == "darwin":
        url = BORE_RELEASES["v0.5.1"]["macos"]
        ext = ".tar.gz"
    else:
        url = BORE_RELEASES["v0.5.1"]["linux"]
        ext = ".tar.gz"
    tmp = cache + ".dl" + ext
    downloaded = False
    for attempt in range(3):
        log(f"下载 {url} (第 {attempt + 1}/3 次)")
        try:
            urllib.request.urlretrieve(url, tmp, timeout=60)
            downloaded = True
            break
        except Exception as e:
            log(f"下载失败: {e}")
            time.sleep(2)
    if not downloaded:
        log("× 自动下载失败, 请手动下载 bore 放到 bin/bore:")
        log(f"  {url}")
        log("  (GitHub: ekzhang/bore releases)")
        sys.exit(1)
    if ext == ".zip":
        import zipfile
        with zipfile.ZipFile(tmp) as z:
            member = next(n for n in z.namelist() if n.endswith("bore.exe"))
            z.extract(member, os.path.dirname(cache))
            os.replace(os.path.join(os.path.dirname(cache), member), cache)
    else:
        import tarfile
        with tarfile.open(tmp, "r:gz") as t:
            member = next(n for n in t.getnames() if n.endswith("/bore"))
            f = t.extractfile(member)
            with open(cache, "wb") as out:
                out.write(f.read())
        if os.name != "nt":
            os.chmod(cache, 0o755)
    os.remove(tmp)
    log(f"bore 就绪: {cache}")
    return cache


def run_tunnel(bore_cmd: str, local_port: int, pair_code: str):
    while True:
        log("连接 bore.pub 隧道...")
        p = subprocess.Popen(
            [bore_cmd, "local", str(local_port), "--to", "bore.pub"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1)
        allocated = None
        for line in p.stdout:
            line = line.strip()
            if not line:
                continue
            m = re.search(r"listening at ([\w.-]+):(\d+)", line)
            if m:
                host, port = m.group(1), m.group(2)
                allocated = port
                log("=" * 55)
                log(f"✅ 公网地址就绪: {host}:{port}")
                log(f"   手机 App 直连模式: 地址 {host}  端口 {port}  配对码 {pair_code}")
                log("=" * 55)
            elif "keepalive" in line.lower() or "error" in line.lower():
                log(f"[bore] {line}")
        code = p.wait()
        log(f"隧道退出 (exit {code}), 3 秒后自动重连...")
        time.sleep(3)


def main():
    ap = argparse.ArgumentParser(description="bore.pub 公网转发一键启动")
    ap.add_argument("--pair-code", default="131420", help="配对码 (默认 131420)")
    ap.add_argument("--local-port", type=int, default=47810, help="本地 server.py 端口 (默认 47810)")
    ap.add_argument("--bore-command", default=None, help="指定 bore 可执行文件 (默认自动查找/下载)")
    args = ap.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    ensure_server(args.local_port, args.pair_code, sys.executable, script_dir)

    if args.bore_command:
        bore_cmd = args.bore_command
    else:
        bore_cmd = ensure_bore(script_dir)
    return run_tunnel(bore_cmd, args.local_port, args.pair_code)


if __name__ == "__main__":
    sys.exit(main())