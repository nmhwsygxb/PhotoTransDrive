#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PhotoTrans 网盘 · 家里电脑中继客户端 (relay_client.py)
========================================================
定位：运行在【家里/公司】的网盘电脑上，让手机在外网也能连回本机 server.py。

原理：
    1. 主动连出到公网 relay.py（穿透 NAT，无需路由器端口映射）
    2. 注册后拿到分配的数据端口
    3. 建立隧道：连数据端口，中继把手机连接与隧道双向转发
    4. 本地桥：隧道 <-> 本机 server.py:47810，手机看到的就是本地网盘

拓扑：
    手机 ──► 公网 relay.py:数据端口 ──隧道──► 本机 relay_client ──► server.py:47810

用法：
    python relay_client.py --relay relay.example.com:47820 \
        --auth-key 你的密钥 --local 127.0.0.1:47810 --name my-pc

    # 自动重连 (断线/隧道关闭后等待 retry 秒重试)
    常驻运行, Ctrl+C 退出。

零第三方依赖, 仅 Python 标准库。
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import select
import socket
import sys
import threading
import time

if hasattr(sys.stdout, "reconfigure"):
    try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass
if hasattr(sys.stderr, "reconfigure"):
    try: sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass

BUF_SIZE = 65536
CTRL_TIMEOUT = 30.0
IDLE_TIMEOUT = 300.0           # 桥接 select 空闲超时 (秒): 超时重建隧道, 刷新 NAT 映射
HEARTBEAT_INTERVAL = 20.0      # 控制连接心跳间隔 (秒): 保持 NAT 映射活跃
HEARTBEAT_STALE_LIMIT = 6      # 连续心跳失败次数 → 判定中继链路死亡, 强制重连


def setup_logger() -> logging.Logger:
    """日志: 控制台 + 文件 (~/.phototransrelay/relay_client.log)。"""
    meta_dir = os.path.expanduser("~/.phototransrelay")
    os.makedirs(os.path.join(meta_dir, "logs"), exist_ok=True)
    log_path = os.path.join(meta_dir, "logs", "relay_client.log")
    logger = logging.getLogger("phototrans_relay_client")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    if logger.handlers:
        return logger
    fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    return logger


def recv_line(sock: socket.socket, timeout: float) -> str:
    """读一行 (\n 结尾)。"""
    sock.settimeout(timeout)
    buf = b""
    while b"\n" not in buf:
        chunk = sock.recv(BUF_SIZE)
        if not chunk:
            raise ConnectionError("连接断开")
        buf += chunk
        if len(buf) > 8192:
            raise ValueError("响应过长")
    return buf.split(b"\n", 1)[0].decode("utf-8", errors="replace").strip()


def connect_tcp(host: str, port: int, timeout: float = CTRL_TIMEOUT) -> socket.socket:
    """连接 TCP, 返回 socket。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect((host, port))
    s.settimeout(None)
    return s


def bridge(tun: socket.socket, local: socket.socket, logger: logging.Logger, tag: str) -> str:
    """双向桥接 tun <-> local。
    返回 'tunnel-closed' 表示隧道端断开 (需重建隧道), 'local-closed' 表示本地断开。
    注意: 返回后【不关闭隧道】(tun 由调用方管理), 只关闭 local。
    """
    tun.settimeout(IDLE_TIMEOUT)
    local.settimeout(IDLE_TIMEOUT)
    try:
        while True:
            r, _, _ = select.select([tun, local], [], [], IDLE_TIMEOUT)
            if not r:
                logger.info(f"[{tag}] 空闲超时, 断开")
                return "tunnel-closed"
            for s in r:
                try:
                    data = s.recv(BUF_SIZE)
                except (ConnectionError, OSError) as e:
                    logger.warning(f"[{tag}] recv 异常: {e}")
                    if s is tun:
                        return "tunnel-closed"
                    return "local-closed"
                if not data:
                    if s is tun:
                        logger.info(f"[{tag}] 隧道 EOF")
                        return "tunnel-closed"
                    logger.info(f"[{tag}] 本地 EOF")
                    return "local-closed"
                target = local if s is tun else tun
                target.sendall(data)
    except (ConnectionError, OSError):
        return "tunnel-closed"
    finally:
        try: local.close()
        except Exception: pass


def run_client(relay_host: str, relay_ctrl_port: int, auth_key: str,
               local_host: str, local_port: int, name: str,
               retry: float, logger: logging.Logger) -> None:
    """主循环: 注册 → 隧道 → 桥接 → 断开重连。"""
    logger.info("=" * 60)
    logger.info(f"PhotoTrans 中继客户端启动: {name}")
    logger.info(f"中继: {relay_host}:{relay_ctrl_port} → 本地 {local_host}:{local_port}")
    logger.info("=" * 60)

    while True:
        data_port: int | None = None
        ctrl: socket.socket | None = None
        tun: socket.socket | None = None
        hb_stop = threading.Event()
        hb_failed = [0]
        hb_thread: threading.Thread | None = None
        try:
            # 1) 注册
            logger.info("连接中继注册...")
            ctrl = connect_tcp(relay_host, relay_ctrl_port)
            ctrl.sendall(f"PT-RELAY-REG {name} {auth_key}\n".encode("utf-8"))
            resp = recv_line(ctrl, CTRL_TIMEOUT)
            if not resp.startswith("PT-RELAY-OK "):
                logger.error(f"注册失败: {resp}")
                time.sleep(retry)
                continue
            data_port = int(resp.split(" ")[1])
            logger.info(f"注册成功, 数据端口: {data_port}")

            # 2) 建立隧道
            tun = connect_tcp(relay_host, data_port)
            tun.sendall(f"PT-RELAY-TUNNEL {name}\n".encode("utf-8"))
            tresp = recv_line(tun, CTRL_TIMEOUT)
            if not tresp.startswith("PT-RELAY-OK"):
                logger.error(f"隧道建立失败: {tresp}")
                time.sleep(retry)
                continue
            logger.info(f"隧道就绪: {relay_host}:{data_port}")

            # 3) 心跳线程: 每 HEARTBEAT_INTERVAL 向控制连接发 PING, 保持 NAT 映射
            #    连续失败 HEARTBEAT_STALE_LIMIT 次 → 判定中继链路死亡:
            #    主动断开隧道, 让 bridge 立即返回 tunnel-closed → 主循环马上重建
            #    (否则主循环会困在 bridge 的 select 空闲超时里, 最长 300s 才重建)
            def _heartbeat(ctrl_sock: socket.socket, tun_sock: socket.socket,
                           fail_cnt: list[int], stop: threading.Event,
                           log: logging.Logger) -> None:
                while not stop.is_set():
                    if stop.wait(HEARTBEAT_INTERVAL):
                        break
                    try:
                        ctrl_sock.sendall(b"PT-RELAY-PING\n")
                        fail_cnt[0] = 0
                    except OSError:
                        fail_cnt[0] += 1
                        log.warning(f"心跳发送失败 ({fail_cnt[0]}): {name}")
                        if fail_cnt[0] >= HEARTBEAT_STALE_LIMIT:
                            log.warning("中继链路死亡, 主动断开隧道触发重建")
                            try:
                                tun_sock.shutdown(socket.SHUT_RDWR)
                            except OSError:
                                pass
                            try:
                                tun_sock.close()
                            except OSError:
                                pass
                            return

            hb_thread = threading.Thread(
                target=_heartbeat, args=(ctrl, tun, hb_failed, hb_stop, logger),
                daemon=True)
            hb_thread.start()

            # 4) 隧道就绪后, 等待手机数据: 桥接隧道 <-> 本地 server.py
            #    手机会话期间双向转发; 隧道断开 → 重建隧道; 本地断开 → 重连本地
            while True:
                try:
                    local = connect_tcp(local_host, local_port, timeout=10.0)
                    logger.info(f"桥接隧道 → 本地 {local_host}:{local_port}")
                    why = bridge(tun, local, logger, name)
                    if why == "tunnel-closed":
                        logger.info(f"隧道关闭, 重建: {name}")
                        break  # 隧道断开, 重新注册
                    logger.info(f"本地连接断开, 重连本地: {name}")
                    # local-closed: 隧道可能还有用, 直接重连本地继续桥
                    time.sleep(0.5)
                except ConnectionError as e:
                    logger.warning(f"本地 server.py 连接失败: {e} (server.py 在运行吗?)")
                    time.sleep(retry)
                except OSError as e:
                    logger.warning(f"桥接异常: {e}")
                    time.sleep(retry)
        except (ConnectionError, OSError, ValueError) as e:
            logger.warning(f"中继连接异常: {e}, {retry}s 后重试")
            time.sleep(retry)
        except KeyboardInterrupt:
            logger.info("收到中断, 退出")
            return
        finally:
            hb_stop.set()
            if hb_thread and hb_thread.is_alive():
                hb_thread.join(timeout=2.0)
            if ctrl is not None:
                try: ctrl.close()
                except Exception: pass
            if tun is not None:
                try: tun.close()
                except Exception: pass


def main() -> None:
    parser = argparse.ArgumentParser(description="PhotoTrans 家里电脑中继客户端")
    parser.add_argument("--relay", required=True, help="中继服务器地址+端口, 如 relay.example.com:47820")
    parser.add_argument("--auth-key", required=True, help="中继认证密钥 (与 relay.py 一致)")
    parser.add_argument("--local", default="127.0.0.1:47810", help="本地网盘地址 (默认 127.0.0.1:47810)")
    parser.add_argument("--name", default="my-pc", help="隧道名 (手机端标识, 默认 my-pc)")
    parser.add_argument("--retry", type=float, default=5.0, help="断线重试间隔秒 (默认 5)")
    args = parser.parse_args()

    # 解析 --relay host:port
    relay_part = args.relay.rsplit(":", 1)
    relay_host = relay_part[0]
    relay_port = int(relay_part[1]) if len(relay_part) == 2 else 47820
    local_part = args.local.rsplit(":", 1)
    local_host = local_part[0]
    local_port = int(local_part[1]) if len(local_part) == 2 else 47810

    logger = setup_logger()
    run_client(relay_host, relay_port, args.auth_key, local_host, local_port,
               args.name, args.retry, logger)


if __name__ == "__main__":
    main()
