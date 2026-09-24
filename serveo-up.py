#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PhotoTransDrive · serveo 公网转发一键启动
=========================================
原理: 家里电脑 ssh 反向隧道 → serveo.net 随机公网端口 → 本地 server.py:47810
手机 App 用【直连模式】填: 地址 serveo.net  端口 <脚本打印的端口>  配对码 <--pair-code>

用法:
    python serveo-up.py                     # 默认配对码 131420
    python serveo-up.py --pair-code 123456  # 指定配对码
    python serveo-up.py --local-port 47810  # 指定本地 server.py 端口

特性:
    - 自动启动 server.py (若未运行)
    - 解析 serveo 分配的随机公网端口并打印
    - ssh 断线自动重连 (重新分配端口, 再次打印)
    - 日志: ~/.phototransdrive/serveo-up.log
"""
import argparse
import os
import re
import subprocess
import sys
import time

LOG_PATH = os.path.expanduser("~/.phototransdrive/serveo-up.log")


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
    """若 47810 未监听则启动 server.py (子进程, 随本脚本退出)。"""
    if is_port_listening(port):
        log(f"server.py 已在运行 (端口 {port}), 复用")
        return None
    server_py = os.path.join(script_dir, "server.py")
    log(f"启动 server.py --port {port} --pair-code {pair_code}")
    proc = subprocess.Popen(
        [python, server_py, "--port", str(port), "--pair-code", pair_code],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # 等待监听
    for _ in range(20):
        if is_port_listening(port):
            return proc
        time.sleep(0.5)
    log("警告: server.py 未在 10s 内监听, 继续尝试隧道")
    return proc


def run_tunnel(ssh_cmd: str, local_port: int, pair_code: str):
    """启动 ssh 反向隧道, 解析端口, 断线重连循环。"""
    while True:
        log("连接 serveo.net 隧道...")
        try:
            p = subprocess.Popen(
                ssh_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                bufsize=1)
        except FileNotFoundError:
            log("× 找不到 ssh (Windows 请安装 OpenSSH 客户端 或 Git)")
            return 1
        allocated = None
        for line in p.stdout:
            line = line.strip()
            if not line:
                continue
            m = re.search(r"Allocated port (\d+)", line)
            if m:
                allocated = m.group(1)
                log("=" * 55)
                log(f"✅ 公网地址就绪: serveo.net:{allocated}")
                log(f"   手机 App 直连模式: 地址 serveo.net  端口 {allocated}  配对码 {pair_code}")
                log("=" * 55)
            elif "too many connections" in line or "limit" in line.lower():
                log(f"注意(免费额度提示): {line}")
            else:
                log(f"[serveo] {line}")
        # ssh 退出
        code = p.wait()
        log(f"隧道断开 (exit {code}), 3 秒后自动重连...")
        time.sleep(3)


def main():
    ap = argparse.ArgumentParser(description="serveo 公网转发一键启动")
    ap.add_argument("--pair-code", default="131420", help="配对码 (默认 131420)")
    ap.add_argument("--local-port", type=int, default=47810, help="本地 server.py 端口 (默认 47810)")
    args = ap.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    python = sys.executable

    # 1) 确保 server.py
    ensure_server(args.local_port, args.pair_code, python, script_dir)

    # 2) serveo 隧道 (免注册, 随机端口)
    ssh_cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-o", "ExitOnForwardFailure=yes",
        "-N", "-R", f"0:localhost:{args.local_port}",
        "serveo.net",
    ]
    return run_tunnel(ssh_cmd, args.local_port, args.pair_code)


if __name__ == "__main__":
    sys.exit(main())
