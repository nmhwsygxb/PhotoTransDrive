#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PhotoTrans 网盘 · 公网中继服务器 (relay.py)
============================================
定位：让手机在【任何网络】(4G/5G/异地 Wi-Fi) 都能连回家里的网盘电脑。

原理（反向隧道 + 端口映射，穿透 NAT）：
    家里电脑运行 relay_client.py，主动连出到本中继注册隧道；
    手机连中继的某个数据端口，中继把该连接与家里电脑的隧道
    socket 做【双向盲转发】—— 手机看到的完全等同于直连家里
    server.py，App 端零改动。

拓扑：
    手机 ──PT-PAIR/PT-AUTH/LIST...──► relay.py:数据端口
                                        │  双向透传
    家里电脑 relay_client.py ◄──隧道── relay.py ◄──► 本地 server.py:47810

隧道生命周期（每条隧道 = 一个手机会话）：
    1. relay_client 连控制端口 → PT-RELAY-REG <name> <authKey> → 分配数据端口
    2. relay_client 连数据端口 → PT-RELAY-TUNNEL <name> → 隧道就绪
    3. 手机连该数据端口 → 中继双向转发手机 ↔ 隧道
    4. 手机断开 → 隧道关闭 → relay_client 检测断开, 重连 (循环)

协议（每行 \n 结尾）：
    注册:   C→S: PT-RELAY-REG <name> <authKey>
            S→C: PT-RELAY-OK <dataPort> | PT-RELAY-FAIL <reason>
    隧道:   C→S: PT-RELAY-TUNNEL <name>
            S→C: PT-RELAY-OK tunnel-ready | PT-RELAY-FAIL <reason>
    手机侧: 无握手, 字节原样透传 (兼容现有 App 零改动)

安全：
    - authKey 认证注册者, 防陌生人占用隧道/端口; 连续失败 5 次锁 IP。
    - 手机侧无需密钥 (配对码由家里 server.py 校验, 中继不落地业务数据)。
    - 数据不落盘, 仅内存转发。

用法：
    # 公网服务器上运行 (需开放控制端口 + 数据端口段)
    python relay.py --auth-key 你的密钥

    # 查看已注册隧道
    python relay.py --list --auth-key 你的密钥

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

# ─────────────────────────────────────────────────────────────────────
# 常量
# ─────────────────────────────────────────────────────────────────────

DEFAULT_CTRL_PORT = 47820       # 控制端口 (电脑注册隧道)
DEFAULT_DATA_START = 47830      # 数据端口段起始
DEFAULT_DATA_COUNT = 20         # 数据端口数量
BUF_SIZE = 65536
REG_TIMEOUT = 30.0              # 注册/隧道建立超时
IDLE_TIMEOUT = 7200.0           # 隧道空闲超时 (2h)
AUTH_FAIL_LIMIT = 5             # 认证失败上限 (防爆破)
AUTH_FAIL_WINDOW = 300          # 锁定窗口 (秒)


def setup_logger() -> logging.Logger:
    """日志: 控制台 + 文件 (~/.phototransrelay/relay.log)。"""
    meta_dir = os.path.expanduser("~/.phototransrelay")
    os.makedirs(os.path.join(meta_dir, "logs"), exist_ok=True)
    log_path = os.path.join(meta_dir, "logs", "relay.log")

    logger = logging.getLogger("phototrans_relay")
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


class RelayServer:
    """中继服务器: 管理电脑隧道注册, 转发手机连接。"""

    def __init__(self, ctrl_port: int, data_start: int, data_count: int,
                 auth_key: str, logger: logging.Logger):
        self.ctrl_port = ctrl_port
        self.data_start = data_start
        self.data_count = data_count
        self.auth_key = auth_key
        self.log = logger

        # {name: {"data_port": int, "ctrl_conn": socket, "peer": str, "registered_at": float}}
        self._regs: dict[str, dict] = {}
        # {data_port: name}
        self._port_map: dict[int, str] = {}
        # {data_port: {"tun_sock": socket, "peer": str, "ready_at": float}} 隧道就绪表
        self._tunnels: dict[int, dict] = {}
        self._lock = threading.Lock()
        # 认证失败计数 {ip: (count, first_fail_time)}
        self._fail_counts: dict[str, list] = {}
        self._stop = threading.Event()

    # ── 认证防护 ──
    def _is_locked(self, ip: str) -> bool:
        now = time.time()
        with self._lock:
            rec = self._fail_counts.get(ip)
            if rec:
                count, first = rec
                if now - first > AUTH_FAIL_WINDOW:
                    self._fail_counts[ip] = [1, now]
                    return False
                if count >= AUTH_FAIL_LIMIT:
                    return True
            return False

    def _record_fail(self, ip: str) -> None:
        now = time.time()
        with self._lock:
            rec = self._fail_counts.get(ip)
            if rec and now - rec[1] <= AUTH_FAIL_WINDOW:
                self._fail_counts[ip] = [rec[0] + 1, rec[1]]
            else:
                self._fail_counts[ip] = [1, now]

    def _clear_fail(self, ip: str) -> None:
        with self._lock:
            self._fail_counts.pop(ip, None)

    # ── 注册表管理 ──
    def _allocate_port(self) -> int | None:
        with self._lock:
            used = set(self._port_map.keys())
            for p in range(self.data_start, self.data_start + self.data_count):
                if p not in used:
                    return p
        return None

    def _register(self, name: str, data_port: int, ctrl_conn: socket.socket, peer: str) -> None:
        with self._lock:
            old = self._regs.get(name)
            if old:
                # 替换同名注册: 释放旧端口, 关闭旧控制连接
                if old["data_port"] != data_port:
                    self._port_map.pop(old["data_port"], None)
                    # 旧端口隧道一并清理 (若未被手机接管)
                    t = self._tunnels.get(old["data_port"])
                    if t and not t.get("taken"):
                        self._tunnels.pop(old["data_port"], None)
                if old["ctrl_conn"] is not ctrl_conn:
                    try: old["ctrl_conn"].close()
                    except Exception: pass
            self._regs[name] = {"data_port": data_port, "ctrl_conn": ctrl_conn,
                                "peer": peer, "registered_at": time.time()}
            self._port_map[data_port] = name

    def _unregister(self, name: str, ctrl_conn: socket.socket | None = None) -> None:
        """注销注册; 若传 ctrl_conn, 仅在当前条目属于该连接时才删除
        (防止旧 keepalive 线程误删新注册)。"""
        with self._lock:
            reg = self._regs.get(name)
            if not reg:
                return
            if ctrl_conn is not None and reg["ctrl_conn"] is not ctrl_conn:
                return  # 已被新注册替换, 旧线程不得删除
            self._regs.pop(name, None)
            self._port_map.pop(reg["data_port"], None)
            # 该端口的隧道条目一并清理 (若未被手机接管中)
            t = self._tunnels.get(reg["data_port"])
            if t and not t.get("taken"):
                self._tunnels.pop(reg["data_port"], None)

    def list_tunnels(self) -> list[dict]:
        with self._lock:
            result = []
            for name, reg in self._regs.items():
                t = self._tunnels.get(reg["data_port"])
                result.append({
                    "name": name, "peer": reg["peer"], "data_port": reg["data_port"],
                    "tunnel_ready": bool(t),
                    "registered_at": reg["registered_at"],
                    "age_sec": int(time.time() - reg["registered_at"]),
                })
            return result

    # ── 控制连接 (电脑注册) ──
    def _handle_reg_conn(self, conn: socket.socket, addr) -> None:
        ip = addr[0]
        conn.settimeout(REG_TIMEOUT)
        try:
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(BUF_SIZE)
                if not chunk:
                    self.log.warning(f"注册连接过早断开: {ip}")
                    return
                buf += chunk
                if len(buf) > 4096:
                    self._record_fail(ip)
                    return
            line = buf.split(b"\n", 1)[0].decode("utf-8", errors="replace").strip()
            parts = line.split(" ")
            if len(parts) != 3 or parts[0] != "PT-RELAY-REG":
                self._record_fail(ip)
                self.log.warning(f"非法注册请求: {ip}")
                self._send_line(conn, "PT-RELAY-FAIL bad-request")
                return
            name, key = parts[1], parts[2]
            if self._is_locked(ip):
                self.log.warning(f"认证失败过多, 拒绝: {ip}")
                self._send_line(conn, "PT-RELAY-FAIL locked")
                return
            if key != self.auth_key:
                self._record_fail(ip)
                self.log.warning(f"注册密钥错误: {ip} name={name}")
                self._send_line(conn, "PT-RELAY-FAIL bad-auth")
                return
            self._clear_fail(ip)
            if not name or len(name) > 64 or any(c in name for c in " \r\n\t"):
                self._send_line(conn, "PT-RELAY-FAIL bad-name")
                return
            data_port = self._allocate_port()
            if data_port is None:
                self.log.warning(f"数据端口已满: {name}")
                self._send_line(conn, "PT-RELAY-FAIL no-port")
                return
            self._register(name, data_port, conn, ip)
            self._send_line(conn, f"PT-RELAY-OK {data_port}")
            self.log.info(f"注册成功: {name} <- {ip}, 数据端口 {data_port}")
            self._keepalive(conn, name)
        except socket.timeout:
            self.log.warning(f"注册超时: {ip}")
        except Exception as e:
            self.log.warning(f"注册处理异常: {ip}: {e}")
        finally:
            try: conn.close()
            except Exception: pass
            if "name" in dir():
                self._unregister(name, conn)

    def _keepalive(self, conn: socket.socket, name: str) -> None:
        """注册连接保持打开; 断开则注销。"""
        conn.settimeout(60.0)
        try:
            while not self._stop.is_set():
                try:
                    data = conn.recv(BUF_SIZE)
                except socket.timeout:
                    continue
                if not data:
                    self.log.info(f"注册连接断开, 注销: {name}")
                    self._unregister(name, conn)
                    return
        except Exception:
            pass
        finally:
            self._unregister(name, conn)

    # ── 数据连接 ──
    def _handle_data_conn(self, conn: socket.socket, addr, data_port: int) -> None:
        """数据端口连接: 读首行区分 电脑隧道 vs 手机。"""
        ip = addr[0]
        conn.settimeout(REG_TIMEOUT)
        try:
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(BUF_SIZE)
                if not chunk:
                    return
                buf += chunk
                if len(buf) > 4096:
                    return
            line = buf.split(b"\n", 1)[0].decode("utf-8", errors="replace").strip()
            parts = line.split(" ")
            if len(parts) == 2 and parts[0] == "PT-RELAY-TUNNEL":
                name = parts[1]
                self._tunnel_establish(conn, data_port, name, ip)
            else:
                self._phone_connect(conn, data_port, buf, ip)
        except Exception as e:
            self.log.warning(f"数据连接异常: {ip}@{data_port}: {e}")
        finally:
            try: conn.close()
            except Exception: pass

    def _tunnel_establish(self, conn: socket.socket, data_port: int, name: str, ip: str) -> None:
        """电脑建立隧道: 校验注册存在且端口匹配。

        建立后本线程【不读写隧道数据】——隧道由手机连接全权接管 (_phone_connect)。
        本线程只等待两种退出条件:
          1) 隧道被手机接管 (taken=True) 或已被清除 (tunnels.pop)
          2) 隧道被新连接替换 (tun_sock 不再是本 conn)
        """
        with self._lock:
            reg = self._regs.get(name)
            if not reg or reg["data_port"] != data_port:
                self.log.warning(f"隧道建立失败(未注册/端口不符): {name}@{data_port} from {ip}")
                self._send_line(conn, "PT-RELAY-FAIL not-registered")
                return
            old_t = self._tunnels.get(data_port)
        if old_t and old_t["tun_sock"] is not conn:
            try: old_t["tun_sock"].close()
            except Exception: pass
        with self._lock:
            self._tunnels[data_port] = {"tun_sock": conn, "peer": ip,
                                        "ready_at": time.time(), "taken": False}
        self._send_line(conn, "PT-RELAY-OK tunnel-ready")
        self.log.info(f"隧道就绪: {name}@{data_port} <- {ip}")
        # 等待被接管/清除/替换 (不 recv 隧道数据, 避免与手机接管线程抢读)
        try:
            while not self._stop.is_set():
                time.sleep(0.3)
                with self._lock:
                    t = self._tunnels.get(data_port)
                    if t is None or t["tun_sock"] is not conn:
                        return
                    if t.get("taken"):
                        return
        finally:
            # 若尚未被手机接管且仍持有本隧道, 则清除
            with self._lock:
                t = self._tunnels.get(data_port)
                if t and t["tun_sock"] is conn and not t.get("taken"):
                    self._tunnels.pop(data_port, None)

    def _phone_connect(self, phone_sock: socket.socket, data_port: int, first_chunk: bytes, ip: str) -> None:
        """手机连接: 找隧道并全权接管做双向转发。

        生命周期约定:
          - 手机连接到来时, 隧道必须存在且未被占用 (taken=False)。
          - 接管后标记 taken=True, 隧道线程 (_tunnel_establish) 随即退出。
          - 手机会话结束 (relay 返回): 关闭隧道 socket, 通知 relay_client 重建。
          - 不直接 pop _tunnels —— 由 relay_client 重新注册时替换;
            若 relay_client 未及时重建, 端口保持"无隧道" (taken=True, sock 已关),
            新手机连接会得到 no-tunnel 并重试 (App 有重连机制)。
        """
        with self._lock:
            t = self._tunnels.get(data_port)
            name = self._port_map.get(data_port)
            if not t or not name or t.get("taken"):
                t = None
            else:
                t["taken"] = True  # 原子接管, 隧道线程随后退出
        if not t:
            self.log.warning(f"手机连接无可用隧道: {ip}@{data_port}")
            try: phone_sock.sendall(b"PT-RELAY-FAIL no-tunnel\n")
            except Exception: pass
            return
        tun_sock = t["tun_sock"]
        self.log.info(f"手机连接 {ip} → 隧道 {name}@{data_port}")
        self._relay(phone_sock, tun_sock, first_chunk, name, ip)
        # 手机会话结束: 关闭隧道, 通知 relay_client 重建
        try: tun_sock.close()
        except Exception: pass
        # 保留 taken=True 的条目 (端口仍映射到 name), relay_client 重建时替换

    def _relay(self, a: socket.socket, b: socket.socket, a_first: bytes, name: str, phone_ip: str) -> None:
        """双向盲转发 a <-> b。"""
        a.settimeout(IDLE_TIMEOUT)
        b.settimeout(IDLE_TIMEOUT)
        if a_first:
            try: b.sendall(a_first)
            except Exception: return
        try:
            while True:
                r, _, _ = select.select([a, b], [], [], IDLE_TIMEOUT)
                if not r:
                    self.log.info(f"隧道空闲超时, 关闭: {name}")
                    break
                for s in r:
                    data = s.recv(BUF_SIZE)
                    if not data:
                        return
                    target = b if s is a else a
                    target.sendall(data)
        except (ConnectionError, OSError):
            pass
        finally:
            self.log.info(f"转发结束: {name} ({phone_ip})")

    @staticmethod
    def _send_line(conn: socket.socket, line: str) -> None:
        try:
            conn.sendall((line + "\n").encode("utf-8"))
        except Exception:
            pass

    # ── 主服务 ──
    def run(self) -> None:
        self.log.info("=" * 60)
        self.log.info("PhotoTrans 中继服务器启动")
        self.log.info(f"控制端口: {self.ctrl_port} (电脑注册隧道)")
        self.log.info(f"数据端口: {self.data_start}-{self.data_start + self.data_count - 1}")
        self.log.info(f"认证密钥: {'已设置' if self.auth_key else '未设置(不安全!)'}")
        self.log.info("=" * 60)

        ctrl_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        ctrl_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            ctrl_sock.bind(("0.0.0.0", self.ctrl_port))
            ctrl_sock.listen(64)
        except OSError as e:
            self.log.error(f"控制端口 {self.ctrl_port} 绑定失败: {e}")
            sys.exit(1)
        self.log.info(f"控制端口监听中: 0.0.0.0:{self.ctrl_port}")

        data_socks: list[socket.socket] = []
        for p in range(self.data_start, self.data_start + self.data_count):
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("0.0.0.0", p))
                s.listen(64)
                data_socks.append((s, p))
                threading.Thread(target=self._data_listener, args=(s, p), daemon=True).start()
            except OSError as e:
                self.log.warning(f"数据端口 {p} 绑定失败: {e}")

        try:
            while not self._stop.is_set():
                try:
                    conn, addr = ctrl_sock.accept()
                except OSError:
                    break
                threading.Thread(target=self._handle_reg_conn, args=(conn, addr), daemon=True).start()
        finally:
            ctrl_sock.close()
            for s, _ in data_socks:
                try: s.close()
                except Exception: pass

    def _data_listener(self, sock: socket.socket, data_port: int) -> None:
        while not self._stop.is_set():
            try:
                conn, addr = sock.accept()
            except OSError:
                break
            threading.Thread(target=self._handle_data_conn, args=(conn, addr, data_port), daemon=True).start()


def main() -> None:
    parser = argparse.ArgumentParser(description="PhotoTrans 公网中继服务器")
    parser.add_argument("--port", type=int, default=DEFAULT_CTRL_PORT, help=f"控制端口 (默认 {DEFAULT_CTRL_PORT})")
    parser.add_argument("--data-start", type=int, default=DEFAULT_DATA_START, help=f"数据端口起始 (默认 {DEFAULT_DATA_START})")
    parser.add_argument("--data-count", type=int, default=DEFAULT_DATA_COUNT, help=f"数据端口数量 (默认 {DEFAULT_DATA_COUNT})")
    parser.add_argument("--auth-key", default=os.environ.get("PT_RELAY_AUTH", ""), help="注册认证密钥 (必填)")
    parser.add_argument("--list", action="store_true", help="列出已注册隧道后退出")
    args = parser.parse_args()

    if not args.auth_key:
        print("× 未设置 --auth-key (或环境变量 PT_RELAY_AUTH)。", file=sys.stderr)
        print("  建议: python -c \"import secrets; print(secrets.token_hex(16))\" 生成密钥。", file=sys.stderr)
        sys.exit(1)

    logger = setup_logger()

    if args.list:
        server = RelayServer(args.port, args.data_start, args.data_count, args.auth_key, logger)
        tunnels = server.list_tunnels()
        if not tunnels:
            print("当前无已注册隧道。")
        else:
            print(f"{'隧道名':<20} {'电脑地址':<40} {'数据端口':<8} {'隧道就绪':<6} {'注册时长'}")
            for t in tunnels:
                print(f"{t['name']:<20} {t['peer']:<40} {t['data_port']:<8} "
                      f"{'是' if t['tunnel_ready'] else '否':<6} {t['age_sec']}s")
        return

    server = RelayServer(args.port, args.data_start, args.data_count, args.auth_key, logger)
    try:
        server.run()
    except KeyboardInterrupt:
        logger.info("收到中断, 退出。")


if __name__ == "__main__":
    main()
