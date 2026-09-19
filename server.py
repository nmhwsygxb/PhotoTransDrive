#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PhotoTrans 网盘 · 电脑端服务端
===============================
定位：拿你自己的电脑当私有云。
- 手机通过配对码绑定后，把文件上传到本机指定文件夹（明文直存）。
- 协议：TCP 直连，端口 47810（网盘专用，与传文件 47808 分开）。
- 认证：PT-AUTH <accountId> <deviceToken>，配对时签发。
- 权限：普通配对码=只读（仅浏览/下载），管理员配对码=完整权限（上传/删除/改动）。

设计原则：最简单、配置成本最低、零第三方依赖（仅 Python 标准库）。
日志：全程写入 ~/.phototransdrive/logs/phototrans_drive.log + 控制台。

用法：
    python server.py --root "D:/PhotoTransDrive" [--port 47810] [--pair-code 123456] [--admin-code 888888]

协议（每行 \n 结尾，文件体裸字节）：
    配对: C→S: PT-PAIR <deviceId> <pairCode> <deviceName>
          S→C: PT-PAIR-OK <deviceToken> | PT-PAIR-FAIL <reason>

    认证: C→S: PT-AUTH <deviceId> <deviceToken>
          S→C: PT-AUTH-OK <deviceName>  |  PT-AUTH-FAIL <reason>

    C→S:  LIST /path
    S→C:  PT-JSON <len>\n<json>
    C→S:  MKDIR /path/name
    S→C:  PT-OK | PT-ERR <reason>
    C→S:  UPLOAD /path/name Content-Length <n>
    S→C:  PT-OK (就绪)   → 客户端发 <n> 字节文件体 → 服务端回 PT-OK | PT-ERR
    C→S:  DOWNLOAD /path/name
    S→C:  PT-OK Content-Length <n>\n  → <n> 字节文件体 |  PT-ERR <reason>
    C→S:  DELETE /path/name
    S→C:  PT-OK | PT-ERR <reason>

安全：路径规范化防 .. 逃逸；拒绝符号链接；文件名净化 Windows 非法字符/双向字符。
权限：只读设备执行 MKDIR/UPLOAD/DELETE 会收到 PT-ERR 只读权限。
"""

from __future__ import annotations

# BUG-264 修复: 强制 stdout/stderr 使用 UTF-8 输出。
# Windows 默认控制台代码页是 GBK(936)，Python print 中文启动面板/日志
# 会抛 UnicodeEncodeError 崩溃；此配置让输出始终 UTF-8，配合 bat 中 chcp 65001 正常显示。
import sys
if hasattr(sys.stdout, "reconfigure"):
    try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass
if hasattr(sys.stderr, "reconfigure"):
    try: sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass

import argparse
import json
import logging
import logging.handlers
import os
import secrets
import select
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# ─────────────────────────────────────────────────────────────────────
# IPv6 支持
# ─────────────────────────────────────────────────────────────────────

def get_ipv6_address() -> str | None:
    """获取本机 IPv6 地址（用于异地连接）。
    
    优先返回全球单播地址（2xxx/3xxx），其次返回链路本地（fe80::）。
    返回 None 表示无 IPv6 连接。
    """
    # Windows: 优先用 netsh 枚举，跳过临时地址（隐私扩展地址会过期变化）
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["netsh", "interface", "ipv6", "show", "addresses"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=10,
            ).stdout
            fallback: str | None = None
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[0] in ("Temporary", "Public", "Other", "临时", "公共", "公用", "其他"):
                    atype, addr = parts[0], parts[-1]
                    addr = addr.split("%")[0]
                    if addr.startswith("fe80") or addr == "::1":
                        continue
                    # 中英文环境：Temporary/临时=隐私扩展地址，Public/公共/公用=稳定地址
                    if atype in ("Public", "公共", "公用"):
                        return addr
                    if fallback is None:
                        fallback = addr
            if fallback:
                return fallback
        except Exception:
            pass

    # 尝试获取全球单播 IPv6
    try:
        s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        s.connect(("2001:4860:4860::8888", 53))  # Google DNS
        addr = s.getsockname()[0]
        s.close()
        if addr and not addr.startswith("fe80"):
            return addr
    except Exception:
        pass
    
    # 尝试获取链路本地 IPv6
    try:
        s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        s.bind(("::", 0))
        addr = s.getsockname()[0]
        s.close()
        if addr and addr.startswith("fe80"):
            return addr
    except Exception:
        pass
    
    return None


def has_ipv6_support() -> bool:
    """检查系统是否支持 IPv6。"""
    return socket.has_ipv6


def get_lan_ipv4_addresses() -> list[str]:
    """获取本机所有局域网 IPv4 地址（供手机直连使用）。

    优先返回默认路由对应的地址（通常最可能被手机访问到），
    再用 gethostbyname_ex 枚举所有网卡补全多网卡场景。
    过滤掉回环地址（127.*）和链路本地地址（169.254.*）。
    """
    addresses: list[str] = []

    # 方法一：UDP 连接法，拿到默认路由选择的本地地址（不实际发包）
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127.") and not ip.startswith("169.254."):
            addresses.append(ip)
    except Exception:
        pass

    # 方法二：枚举所有网卡地址，补全多网卡 / VPN 场景
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if (
                ip not in addresses
                and not ip.startswith("127.")
                and not ip.startswith("169.254.")
            ):
                addresses.append(ip)
    except Exception:
        pass

    return addresses


# ─────────────────────────────────────────────────────────────────────
# 常量
# ─────────────────────────────────────────────────────────────────────

DEFAULT_PORT: int = 47810
DEFAULT_UDP_PORT: int = 47811
BUF_SIZE: int = 65536
MAX_FILE_SIZE: int = 100 * 1024 * 1024 * 1024  # 单文件上限 100GB

# 文件名净化相关
MAX_FILENAME_LEN: int = 255  # Windows MAX_PATH 限制
WINDOWS_INVALID_CHARS: str = '\\/:*?"<>|'
SYSTEM_DIR_NAMES: set[str] = {"Users", "home", "root", "etc", "var", "tmp", "~"}

# 双向字符黑名单（Bidirectional Override，可欺骗显示）
BIDI_CHARS: list[str] = [
    '\u202a', '\u202b', '\u202c', '\u202d', '\u202e',  # LRE, RLE, PDF, LRO, RLO
    '\u2066', '\u2067', '\u2068', '\u2069',              # LRI, RLI, FSI, PDI
]

# 并发限制
MAX_CONCURRENT: int = 16      # 全局并发连接上限
MAX_PER_IP: int = 4           # 单 IP 并发连接上限（防单点占满）
AUTH_FAIL_LIMIT: int = 5      # 每 IP 认证失败上限
AUTH_FAIL_WINDOW: int = 300   # 锁定窗口（秒）

# BUG-270 防护：新连接速率限制（防 IPv6 轮换地址刷配对/刷失败计数）
CONN_RATE_LIMIT: int = 10     # 每 IP 每秒最多新建连接数
CONN_RATE_WINDOW: float = 1.0 # 速率窗口（秒）

# BUG-269 防护：连接/握手超时从 300s 收紧到 60s（防 slowloris 占满全局并发槽）
CONN_TIMEOUT: float = 60.0


# ─────────────────────────────────────────────────────────────────────
# 日志
# ─────────────────────────────────────────────────────────────────────

def setup_logger(meta_dir: Path) -> logging.Logger:
    """日志写到元数据目录（网盘根外），避免混入用户文件。
    
    meta_dir = ~/.phototransdrive
    """
    log_dir = meta_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "phototrans_drive.log"
    
    logger = logging.getLogger("phototrans_drive")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    
    if logger.handlers:
        return logger
    
    fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    
    # 日志轮转：10MB 上限，保留 5 个备份
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


# ─────────────────────────────────────────────────────────────────────
# 认证与设备
# ─────────────────────────────────────────────────────────────────────

class AuthStore:
    """绑定设备表：{deviceId: {deviceToken, deviceName, pairedAt}}"""

    def __init__(self, path: Path):
        self.path = path
        self._data: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._pair_code: str | None = None    # 普通配对码（只读权限）
        self._admin_code: str | None = None   # 管理员配对码（完整权限）
        self._load()

    def _load(self) -> None:
        """从磁盘加载绑定表。"""
        log = logging.getLogger("phototrans_drive")
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._data = data
                else:
                    log.warning(
                        f"绑定表格式错误（期望 dict，实际 {type(data).__name__}），已重置"
                    )
        except Exception as e:
            log.warning(f"加载绑定表失败：{e}")

    def _save(self) -> bool:
        """持久化绑定表（原子写入）。返回 True=保存成功。"""
        log = logging.getLogger("phototrans_drive")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # 先写临时文件，再原子重命名（防崩溃损坏）
            tmp_path = self.path.with_suffix(".json.tmp")
            tmp_path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(str(tmp_path), str(self.path))  # 原子操作
            return True
        except Exception as e:
            log.error(f"保存绑定表失败：{e}")
            return False

    def set_pair_code(self, code: str) -> None:
        """设置普通配对码（只读权限：仅浏览/下载，不能改/删）。"""
        self._pair_code = code

    def set_admin_code(self, code: str) -> None:
        """设置管理员配对码（完整权限：上传/删除/改动）。"""
        self._admin_code = code

    def pair(self, device_id: str, pair_code: str, device_name: str) -> str | None:
        """用配对码换取 deviceToken。普通配对码=只读，管理员配对码=完整权限。"""
        if len(device_id) > 256 or len(device_name) > 256:
            return None
        
        with self._lock:
            # 判断配对码类型：管理员优先，其次普通，都不匹配则拒绝
            if self._admin_code and secrets.compare_digest(self._admin_code, pair_code):
                permission = "full"
            elif self._pair_code and secrets.compare_digest(self._pair_code, pair_code):
                permission = "readonly"
            else:
                return None
            
            token = secrets.token_hex(32)
            self._data[device_id] = {
                "device_token": token,
                "device_name": device_name,
                "paired_at": datetime.now().isoformat(),
                "permission": permission,
            }
            
            if not self._save():
                # 持久化失败：回滚内存，防止重启后令牌丢失
                self._data.pop(device_id, None)
                logging.getLogger("phototrans_drive").error(
                    f"配对令牌持久化失败，已回滚：{device_id}"
                )
                return None
            
            return token

    def verify(self, device_id: str, token: str) -> dict | None:
        """校验设备令牌。成功返回设备信息 dict，失败返回 None。"""
        with self._lock:
            device = self._data.get(device_id)
            if device and secrets.compare_digest(device["device_token"], token):
                return device
        return None

    def device_permission(self, device_id: str) -> str:
        """获取设备权限：full=完整，readonly=只读。未找到默认 readonly（安全）。"""
        with self._lock:
            device = self._data.get(device_id)
            return device.get("permission", "readonly") if device else "readonly"

    def device_name(self, device_id: str) -> str:
        """获取设备名称。"""
        with self._lock:
            device = self._data.get(device_id)
            return device["device_name"] if device else "Unknown"

    def list_devices(self) -> list[dict]:
        """列出所有已绑定设备（供命令行管理用）。"""
        with self._lock:
            return [
                {
                    "device_id": device_id,
                    "device_name": info.get("device_name", ""),
                    "paired_at": info.get("paired_at", ""),
                    "permission": info.get("permission", "readonly"),
                }
                for device_id, info in self._data.items()
            ]

    def remove_device(self, device_id: str) -> bool:
        """解绑指定设备。返回 True=删除成功，False=设备不存在或保存失败。"""
        with self._lock:
            if device_id not in self._data:
                return False
            del self._data[device_id]
            return self._save()


# ─────────────────────────────────────────────────────────────────────
# 认证失败限流（防暴力破解，TCP 与 Web 共用）
# ─────────────────────────────────────────────────────────────────────

class IPRateLimiter:
    """per-IP 认证失败计数与锁定窗口。

    DriveServer（TCP 配对/认证）与 WebDriveServer（Web 登录）共用，
    避免同一"失败锁定"策略在两侧各维护一份导致漂移。
    """

    def __init__(self, limit: int = AUTH_FAIL_LIMIT, window: int = AUTH_FAIL_WINDOW):
        self.limit = limit
        self.window = window
        self._fail_count: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def is_locked(self, ip: str) -> bool:
        """检查 IP 是否在锁定窗口内；同时清理过期条目（防内存泄漏）。"""
        with self._lock:
            now = time.time()
            expired = [
                k for k, (_, start) in self._fail_count.items()
                if now - start > self.window
            ]
            for k in expired:
                del self._fail_count[k]

            cur = self._fail_count.get(ip)
            if not cur:
                return False
            count, start = cur
            if now - start > self.window:
                self._fail_count.pop(ip, None)
                return False
            return count >= self.limit

    def record_fail(self, ip: str) -> None:
        """记录一次失败。"""
        with self._lock:
            cur = self._fail_count.get(ip)
            if not cur or time.time() - cur[1] > self.window:
                self._fail_count[ip] = (1, time.time())
            else:
                self._fail_count[ip] = (cur[0] + 1, cur[1])

    def clear(self, ip: str) -> None:
        """成功后清除该 IP 的失败计数。"""
        with self._lock:
            self._fail_count.pop(ip, None)


class IPConnRateLimiter:
    """per-IP 新建连接速率限制（滑动窗口）。

    BUG-270: 抵御 IPv6 地址轮换绕过 —— 攻击者换地址可绕过 per-IP 并发上限
    与认证失败锁定，但无论地址怎么换，短时间内新建连接太多就会被拒。
    """

    def __init__(self, limit: int = CONN_RATE_LIMIT, window: float = CONN_RATE_WINDOW):
        self.limit = limit
        self.window = window
        self._timestamps: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, ip: str) -> bool:
        """是否允许该 IP 新建连接；允许则记录本次连接时间戳。"""
        with self._lock:
            now = time.time()
            stamps = [t for t in self._timestamps.get(ip, []) if now - t <= self.window]
            if len(stamps) >= self.limit:
                self._timestamps[ip] = stamps
                return False
            stamps.append(now)
            self._timestamps[ip] = stamps
            return True


# ─────────────────────────────────────────────────────────────────────
# 路径安全
# ─────────────────────────────────────────────────────────────────────

def safe_resolve(root: Path, raw: str) -> Path | None:
    """安全解析路径，防止 .. 逃逸和符号链接攻击。

    raw 中开头的 / 表示"网盘根目录"（协议约定），会先去掉再按相对路径拼接，
    避免 Windows 下 pathlib 把 /path 当成盘符绝对路径而逃逸 root。
    返回 None 表示路径非法。
    """
    try:
        # 统一分隔符；开头的 / 表示网盘根目录，去掉后按相对路径拼接
        raw = raw.replace("\\", "/").strip()
        raw = raw.lstrip("/")

        root_real = root.resolve()
        target_real = (root_real / raw).resolve() if raw else root_real

        # 确保目标在根目录内：用 relative_to（基于路径组件）而非字符串前缀，
        # 避免 D:\data 与 D:\data2 这类前缀误判导致 .. 逃逸。
        # relative_to 在目标不在根目录内时抛 ValueError，由外层 except 捕获返回 None。
        rel = target_real.relative_to(root_real)

        # 检查路径中的每一段是否为符号链接（防 symlink 逃逸）
        cur = root_real
        for part in rel.parts:
            if part:
                cur = cur / part
                if cur.is_symlink():
                    return None

        return target_real
    except Exception:
        return None


def sanitize_filename(name: str) -> str:
    """净化文件名：替换 Windows 非法字符，拒绝空名/纯点/尾部点/超长名/空字符/双向字符。"""
    # 检查长度
    if len(name) > MAX_FILENAME_LEN:
        raise ValueError(f"文件名过长（最大 {MAX_FILENAME_LEN} 字符）")
    
    # 拒绝控制字符（NUL、\r、\n 等，防日志注入 / HTTP 响应头注入 / 路径歧义）
    if any(ord(c) < 0x20 for c in name):
        raise ValueError("文件名包含非法控制字符")
    
    # 检查双向字符（Bidirectional Override，可欺骗显示）
    for c in name:
        if c in BIDI_CHARS:
            raise ValueError("文件名包含非法双向字符")
    
    # 替换非法字符
    cleaned = "".join("_" if c in WINDOWS_INVALID_CHARS else c for c in name)
    
    # 去除尾部空格和点（Windows 会静默去除，但我们显式处理）
    cleaned = cleaned.rstrip(" .")
    
    # 检查空名或纯点
    if not cleaned or cleaned in (".", ".."):
        raise ValueError("非法文件名")
    
    return cleaned


def sanitize_path(raw: str) -> str:
    """逐段净化路径中的文件名（保留 / 分隔），并拒绝系统目录名。"""
    parts = [sanitize_filename(p) for p in raw.strip().replace("\\", "/").split("/") if p]
    
    # 检查是否为系统目录名
    for p in parts:
        if p in SYSTEM_DIR_NAMES:
            raise ValueError(f"不允许使用系统目录名：{p}")
    
    return "/" + "/".join(parts)


# ─────────────────────────────────────────────────────────────────────
# 连接处理
# ─────────────────────────────────────────────────────────────────────

class DriveServer:
    """网盘服务端主类，处理 TCP 连接和协议。"""
    
    MAX_CONCURRENT = MAX_CONCURRENT
    MAX_PER_IP = MAX_PER_IP
    AUTH_FAIL_LIMIT = AUTH_FAIL_LIMIT
    AUTH_FAIL_WINDOW = AUTH_FAIL_WINDOW
    
    def __init__(
        self,
        root: Path,
        port: int,
        auth: AuthStore,
        logger: logging.Logger,
        ipv6_addr: str | None = None,
    ):
        self.root = root
        self.port = port
        self.auth = auth
        self.log = logger
        self.ipv6_addr = ipv6_addr
        self.running = True
        
        self._conn_count = 0
        self._conn_lock = threading.Lock()
        self._ip_conn_count: dict[str, int] = {}  # per-IP 并发计数
        
        # per-IP 认证失败限流（防暴力破解，共享逻辑见 IPRateLimiter）
        self._fail_limiter = IPRateLimiter(self.AUTH_FAIL_LIMIT, self.AUTH_FAIL_WINDOW)

        # BUG-270: per-IP 新建连接速率限制（防 IPv6 轮换地址绕过）
        self._conn_limiter = IPConnRateLimiter(CONN_RATE_LIMIT, CONN_RATE_WINDOW)

        self._servers: list[socket.socket] = []  # 所有监听 socket（IPv4 + IPv6）
    
    # ── 主循环 ──
    
    def serve(self) -> None:
        """启动服务器，监听 IPv4 和 IPv6 连接。"""
        self.root.mkdir(parents=True, exist_ok=True)
        
        # IPv4 监听（局域网）
        try:
            ipv4_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            ipv4_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            ipv4_server.bind(("0.0.0.0", self.port))
            ipv4_server.listen(64)
        except OSError as e:
            raise RuntimeError(f"端口 {self.port} 已被占用，无法启动") from e
        self._servers.append(ipv4_server)
        self.log.info(f"IPv4 监听：0.0.0.0:{self.port}（局域网）")
        
        # IPv6 监听（异地连接）
        if self.ipv6_addr and socket.has_ipv6:
            try:
                ipv6_server = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
                ipv6_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                ipv6_server.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)  # 仅 IPv6：IPv4 由上面独立 socket 处理，避免双栈冲突
                ipv6_server.bind(("::", self.port))
                ipv6_server.listen(64)
                self._servers.append(ipv6_server)
                self.log.info(f"IPv6 监听：[::{self.port}]（异地连接）")
            except OSError as e:
                self.log.warning(f"IPv6 监听失败：{e}（仅 IPv4）")
        
        self.log.info(f"并发上限 {self.MAX_CONCURRENT} · 单 IP {self.MAX_PER_IP}")
        addresses = f"0.0.0.0:{self.port}"
        if self.ipv6_addr:
            addresses += f" | [{self.ipv6_addr}]:{self.port}"
        self.log.info(f"可用地址：{addresses}")
        
        # 选择器（多 socket 监听）
        while self.running:
            try:
                ready_read, _, _ = select.select(self._servers, [], [], 1.0)
                for server in ready_read:
                    try:
                        conn, addr = server.accept()
                        self._accept_connection(conn, addr)
                    except OSError:
                        continue
            except OSError:
                break
        
        for server in self._servers:
            server.close()
    
    def _accept_connection(self, conn: socket.socket, addr: tuple) -> None:
        """接受新连接，检查并发限制。"""
        ip = addr[0]

        # BUG-270: 新建连接速率限制（IPv6 轮换地址无法绕过）
        if not self._conn_limiter.allow(ip):
            conn.close()
            self.log.warning(f"连接速率超限（{CONN_RATE_LIMIT}/秒），拒绝 {ip}")
            return

        with self._conn_lock:
            if self._conn_count >= self.MAX_CONCURRENT:
                conn.close()
                self.log.warning(f"达到全局并发上限（{self.MAX_CONCURRENT}），拒绝 {ip}")
                return
            
            ip_count = self._ip_conn_count.get(ip, 0)
            if ip_count >= self.MAX_PER_IP:
                conn.close()
                self.log.warning(f"达到单 IP 并发上限（{self.MAX_PER_IP}），拒绝 {ip}")
                return
            
            self._conn_count += 1
            self._ip_conn_count[ip] = ip_count + 1
        
        self.log.info(f"新连接：{ip}:{addr[1]}（全局 {self._conn_count}，{ip} {self._ip_conn_count[ip]}）")
        threading.Thread(target=self._handle_wrapper, args=(conn, addr), daemon=True).start()
    
    def _handle_wrapper(self, conn: socket.socket, addr: tuple) -> None:
        """处理连接包装器，确保计数正确递减。"""
        try:
            self.handle(conn, addr)
        finally:
            ip = addr[0]
            with self._conn_lock:
                self._conn_count = max(0, self._conn_count - 1)
                cur = self._ip_conn_count.get(ip, 0)
                if cur <= 1:
                    self._ip_conn_count.pop(ip, None)
                else:
                    self._ip_conn_count[ip] = cur - 1
    
    def stop(self) -> None:
        """停止服务器。"""
        self.running = False
    
    # ── 单连接处理 ──
    
    def handle(self, conn: socket.socket, addr: tuple) -> None:
        """处理单个连接。支持会话保持：认证后由 _command_loop 持续处理指令。"""
        # BUG-269: 300s → CONN_TIMEOUT(60s)，防慢速连接长占并发槽 (slowloris)
        conn.settimeout(CONN_TIMEOUT)
        
        try:
            while True:
                line = self._readline(conn)
                if not line:
                    self.log.info(f"[{addr[0]}] 连接关闭（无数据）")
                    break
                
                self.log.info(f"[{addr[0]}] 收到：{line[:200]}")
                parts = line.split()
                cmd = parts[0] if parts else ""
                
                if cmd == "PT-AUTH":
                    # 认证成功后内部进入 _command_loop（阻塞直到连接关闭）
                    self._do_auth(conn, addr, parts)
                elif cmd == "PT-PAIR":
                    self._do_pair(conn, addr, parts, raw_line=line)
                else:
                    self._send(conn, "PT-ERR 请先认证（PT-AUTH / PT-PAIR）")
        except Exception as e:
            self.log.error(f"[{addr[0]}] 处理异常：{e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass
    
    def _do_pair(self, conn: socket.socket, addr: tuple, parts: list[str], raw_line: str | None = None) -> None:
        """配对：PT-PAIR <deviceId> <pairCode> <deviceName> → PT-PAIR-OK <deviceToken>"""
        if len(parts) < 4:
            self._send(conn, "PT-PAIR-FAIL 参数不足")
            return
        
        if self._fail_limiter.is_locked(addr[0]):
            self.log.warning(f"[{addr[0]}] 配对被锁定（失败过多）")
            self._send(conn, "PT-PAIR-FAIL 尝试过多，请稍后再试")
            return
        
        device_id, pair_code = parts[1], parts[2]
        # 设备名可能含空格（如 "OPPO Find X6"），用原始行取第 4 个参数之后的所有内容
        if raw_line:
            toks = raw_line.split(None, 3)
            device_name = toks[3] if len(toks) >= 4 else parts[3]
        else:
            device_name = parts[3]
        token = self.auth.pair(device_id, pair_code, device_name)
        
        if token is None:
            self.log.warning(f"[{addr[0]}] 配对失败 deviceId={device_id[:16]}…")
            self._fail_limiter.record_fail(addr[0])
            self._send(conn, "PT-PAIR-FAIL 配对码无效")
            return
        
        self._fail_limiter.clear(addr[0])
        self.log.info(f"[{addr[0]}] 配对成功：{device_name}（{device_id[:16]}…）")
        self._send(conn, f"PT-PAIR-OK {token}")
    
    def _do_auth(self, conn: socket.socket, addr: tuple, parts: list[str]) -> None:
        """认证：PT-AUTH <deviceId> <deviceToken> → PT-AUTH-OK <deviceName>"""
        if len(parts) < 3:
            self._send(conn, "PT-AUTH-FAIL 参数不足")
            return
        
        if self._fail_limiter.is_locked(addr[0]):
            self.log.warning(f"[{addr[0]}] 认证被锁定（失败过多）")
            self._send(conn, "PT-AUTH-FAIL 尝试过多，请稍后再试")
            return
        
        account_id, device_token = parts[1], parts[2]
        
        if not self.auth.verify(account_id, device_token):
            self.log.warning(f"[{addr[0]}] 认证失败 account={account_id[:16]}…")
            self._fail_limiter.record_fail(addr[0])
            self._send(conn, "PT-AUTH-FAIL 令牌无效或已过期")
            return
        
        self._fail_limiter.clear(addr[0])
        name = self.auth.device_name(account_id)
        self.log.info(f"[{addr[0]}] 认证成功：{name}（{account_id[:16]}…）")
        self._send(conn, f"PT-AUTH-OK {name}")
        # 认证后进入指令循环
        self._command_loop(conn, addr, account_id)
    
    def _command_loop(self, conn: socket.socket, addr: tuple, account_id: str) -> None:
        """认证后的指令循环。"""
        # 只读设备：仅允许 LIST/DOWNLOAD，拒绝 MKDIR/UPLOAD/DELETE
        permission = self.auth.device_permission(account_id)
        if permission != "full":
            self.log.info(f"[{addr[0]}] 只读设备连接（仅浏览/下载）")
        
        while True:
            try:
                line = self._readline(conn)
            except socket.timeout:
                self.log.info(f"[{addr[0]}] 读超时，关闭")
                return
            
            if not line:
                self.log.info(f"[{addr[0]}] 对端关闭连接")
                return
            
            self.log.info(f"[{addr[0]}] 指令：{line[:300]}")
            parts = line.split()
            cmd = parts[0] if parts else ""
            
            # 写操作权限检查：只读设备拒绝一切修改/删除
            if cmd in ("MKDIR", "UPLOAD", "DELETE") and permission != "full":
                self._send(conn, "PT-ERR 只读权限，不能修改或删除")
                continue
            
            try:
                if cmd == "LIST":
                    self._cmd_list(conn, parts, raw_line=line)
                elif cmd == "MKDIR":
                    self._cmd_mkdir(conn, parts, raw_line=line)
                elif cmd == "UPLOAD":
                    self._cmd_upload(conn, parts, raw_line=line)
                elif cmd == "DOWNLOAD":
                    self._cmd_download(conn, parts, raw_line=line)
                elif cmd == "DELETE":
                    self._cmd_delete(conn, parts, raw_line=line)
                else:
                    self._send(conn, "PT-ERR 未知指令")
            except Exception as e:
                self.log.error(f"[{addr[0]}] 指令 {cmd} 异常：{e}")
                # 不泄露内部异常信息给客户端（可能包含文件路径等敏感信息）
                self._safe_send(conn, "PT-ERR 内部错误")
    
    # ── 指令实现 ──
    
    def _cmd_list(self, conn: socket.socket, parts: list[str], raw_line: str | None = None) -> None:
        """LIST /path → PT-JSON <len>\n<json>"""
        # 路径可能含空格，用原始行解析（与 DOWNLOAD/DELETE 一致）
        if raw_line:
            raw = raw_line[len("LIST"):].strip()
        else:
            raw = parts[1] if len(parts) > 1 else "/"
        target = safe_resolve(self.root, raw)
        
        if target is None:
            self._send(conn, "PT-ERR 路径非法")
            return
        
        if not target.exists():
            self._send(conn, "PT-ERR 路径不存在")
            return
        
        if not target.is_dir():
            self._send(conn, "PT-ERR 不是目录")
            return
        
        items = []
        try:
            for entry in sorted(target.iterdir(), key=lambda p: p.name.lower()):
                try:
                    st = entry.stat()  # 单次 stat，避免 TOCTOU 竞态
                    is_dir = (st.st_mode & 0o170000 == 0o040000)  # S_IFDIR
                    items.append({
                        "name": entry.name,
                        "isDir": is_dir,
                        "size": 0 if is_dir else st.st_size,
                        "mtime": int(st.st_mtime),
                    })
                except OSError:
                    continue
        except PermissionError:
            self._send(conn, "PT-ERR 无权限")
            return
        
        body = json.dumps(items, ensure_ascii=False)
        self.log.info(f"[LIST] {raw} → {len(items)} 项")
        self._send(conn, f"PT-JSON {len(body.encode('utf-8'))}")
        self._send_bytes(conn, body.encode("utf-8"))
    
    def _cmd_mkdir(self, conn: socket.socket, parts: list[str], raw_line: str | None = None) -> None:
        """MKDIR /path/name → PT-OK | PT-ERR <reason>"""
        # 路径可能含空格，用原始行解析
        if raw_line:
            raw = raw_line[len("MKDIR"):].strip()
        else:
            raw = parts[1] if len(parts) > 1 else ""
        
        try:
            safe = sanitize_path(raw)
        except ValueError:
            self._send(conn, "PT-ERR 文件名非法（含 Windows 保留字符或空名）")
            return
        
        target = safe_resolve(self.root, safe)
        
        if target is None:
            self._send(conn, "PT-ERR 路径非法")
            return
        
        try:
            target.mkdir(parents=True, exist_ok=True)
            self.log.info(f"[MKDIR] {safe}")
            self._send(conn, "PT-OK")
        except Exception as e:
            self.log.error(f"[MKDIR] 失败：{e}")
            # 不泄露内部异常信息给客户端（可能包含文件路径等敏感信息）
            self._send(conn, "PT-ERR 创建目录失败")
    
    def _cmd_upload(self, conn: socket.socket, parts: list[str], raw_line: str | None = None) -> None:
        """UPLOAD /path/name Content-Length <n> → PT-OK → 文件体 → PT-OK"""
        # 文件名可能包含空格，需要特殊解析
        if raw_line:
            # 从原始行解析：找最后一个 "Content-Length"（它在路径之后、数字之前）。
            # 用 rfind 而非 find，避免文件名本身含 "Content-Length" 时误判位置。
            idx = raw_line.rfind("Content-Length")
            if idx < 0:
                self._send(conn, "PT-ERR 格式错误：UPLOAD /path Content-Length <n>")
                return
            
            raw = raw_line[len("UPLOAD"):idx].strip()
            size_str = raw_line[idx + len("Content-Length"):].strip()
            
            try:
                size = int(size_str)
            except ValueError:
                self._send(conn, "PT-ERR Content-Length 非法")
                return
        else:
            # 兼容旧格式（无空格文件名）
            if len(parts) < 4 or parts[2] != "Content-Length":
                self._send(conn, "PT-ERR 格式错误：UPLOAD /path Content-Length <n>")
                return
            
            raw = parts[1]
            
            try:
                size = int(parts[3])
            except ValueError:
                self._send(conn, "PT-ERR Content-Length 非法")
                return
        
        if size < 0:
            self._send(conn, "PT-ERR Content-Length 不能为负数")
            return
        
        if size > MAX_FILE_SIZE:
            self._send(conn, "PT-ERR 文件过大")
            return
        
        # 路径净化（防 Windows 非法字符 / 空名）+ 安全解析
        try:
            raw_safe = sanitize_path(raw)
            raw = raw_safe
            name = sanitize_filename(Path(raw).name)
            parent_raw = str(Path(raw).parent).replace("\\", "/")
            parent = safe_resolve(self.root, parent_raw)
        except ValueError:
            self._send(conn, "PT-ERR 文件名非法")
            return
        
        if parent is None or not parent.is_dir():
            self._send(conn, "PT-ERR 目标目录非法或不存在")
            return
        
        target = parent / name
        self._send(conn, "PT-OK")  # 就绪
        
        written = 0
        try:
            # 排他创建：文件已存在则抛 FileExistsError，消除 TOCTOU 竞态
            f = open(target, "xb")
            try:
                while written < size:
                    chunk = conn.recv(min(BUF_SIZE, size - written))
                    if not chunk:
                        break
                    f.write(chunk)
                    written += len(chunk)
            finally:
                f.close()
            
            if written != size:
                self.log.error(f"[UPLOAD] 不完整：{written}/{size} → 删除残留")
                try:
                    target.unlink()
                except Exception:
                    pass
                self._safe_send(conn, "PT-ERR 接收不完整")
                return
            
            self.log.info(f"[UPLOAD] {raw} → {target}（{written} bytes）")
            self._safe_send(conn, "PT-OK")
        except FileExistsError:
            self._safe_send(conn, "PT-ERR 同名文件已存在（怕误覆盖，请改文件名重传）")
        except Exception as e:
            self.log.error(f"[UPLOAD] 失败：{e}")
            # 异常时删除残留文件（客户端断连等）
            try:
                if target.exists():
                    target.unlink()
            except Exception:
                pass
            # 不泄露内部异常信息给客户端（可能包含文件路径等敏感信息）
            self._safe_send(conn, "PT-ERR 上传失败")
    
    def _cmd_download(self, conn: socket.socket, parts: list[str], raw_line: str | None = None) -> None:
        """DOWNLOAD /path/name → PT-OK Content-Length <n>\n → 文件体"""
        # 文件名可能包含空格，用原始行解析（与 UPLOAD/DELETE 一致）
        if raw_line:
            raw = raw_line[len("DOWNLOAD"):].strip()
        else:
            raw = parts[1] if len(parts) > 1 else ""
        target = safe_resolve(self.root, raw)
        
        if target is None or not target.is_file():
            self._send(conn, "PT-ERR 文件不存在或路径非法")
            return
        
        try:
            with open(target, "rb") as f:
                size = os.fstat(f.fileno()).st_size  # 单次 fstat，在已打开的文件描述符上获取
                self._send(conn, f"PT-OK Content-Length {size}")
                
                while True:
                    chunk = f.read(BUF_SIZE)
                    if not chunk:
                        break
                    conn.sendall(chunk)
                
                self.log.info(f"[DOWNLOAD] {raw}（{size} bytes）")
        except Exception as e:
            self.log.error(f"[DOWNLOAD] 失败：{e}")
            # 不泄露内部异常信息给客户端（可能包含文件路径等敏感信息）
            self._safe_send(conn, "PT-ERR 下载失败")
    
    def _cmd_delete(self, conn: socket.socket, parts: list[str], raw_line: str | None = None) -> None:
        """DELETE /path/name → PT-OK | PT-ERR <reason>"""
        # 文件名可能包含空格，需要特殊解析
        if raw_line:
            raw = raw_line[len("DELETE"):].strip()
        else:
            raw = parts[1] if len(parts) > 1 else ""
        
        target = safe_resolve(self.root, raw)
        
        if target is None or target == self.root:
            self._send(conn, "PT-ERR 路径非法或不允许删除根目录")
            return
        
        try:
            if target.is_dir():
                # 只允许删除空目录，避免误删整棵树
                if any(target.iterdir()):
                    self._send(conn, "PT-ERR 目录非空")
                    return
                target.rmdir()
            else:
                target.unlink()
            
            self.log.info(f"[DELETE] {raw}")
            self._send(conn, "PT-OK")
        except Exception as e:
            self.log.error(f"[DELETE] 失败：{e}")
            # 不泄露内部异常信息给客户端（可能包含文件路径等敏感信息）
            self._send(conn, "PT-ERR 删除失败")
    
    # ── 底层 IO ──
    
    @staticmethod
    def _readline(conn: socket.socket, max_len: int = 8192) -> str | None:
        """读取一行。超过 max_len 抛 ValueError（防内存 DoS）。
        
        批量 recv 替代逐字节，减少系统调用。
        先读到换行再判长度，避免超长行残留字节丢失导致协议失步。
        """
        data = bytearray()
        
        while True:
            remaining = max_len - len(data)
            
            if remaining <= 0:
                # 已读满，再读 1 字节判断是否还有换行
                extra = bytearray(1)
                n = conn.recv_into(extra)
                if n == 0:
                    return None
                data.extend(extra[:n])
                
                if b"\n" in data:
                    nl = data.index(b"\n")
                    line = bytes(data[:nl]).decode("utf-8", errors="replace").strip("\r")
                    if len(line) > max_len:
                        raise ValueError("行长度超限")
                    return line
                
                raise ValueError("行长度超限")
            
            buf = bytearray(min(4096, remaining))
            n = conn.recv_into(buf)
            
            if n == 0:
                return None
            
            data.extend(buf[:n])
            
            if b"\n" in data:
                nl = data.index(b"\n")
                return bytes(data[:nl]).decode("utf-8", errors="replace").strip("\r")
    
    @staticmethod
    def _send(conn: socket.socket, text: str) -> None:
        """发送文本行（自动添加 \n）。"""
        conn.sendall((text + "\n").encode("utf-8"))
    
    @staticmethod
    def _send_bytes(conn: socket.socket, data: bytes) -> None:
        """发送原始字节。"""
        conn.sendall(data)
    
    @staticmethod
    def _safe_send(conn: socket.socket, text: str) -> None:
        """安全发送（忽略异常）。"""
        try:
            conn.sendall((text + "\n").encode("utf-8"))
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────
# 配置加载
# ─────────────────────────────────────────────────────────────────────

def load_config(config_path: Path) -> dict:
    """从 JSON 配置文件加载参数。CLI 参数优先，配置文件兜底。"""
    if not config_path.exists():
        return {}
    
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            return {}
        return cfg
    except Exception:
        return {}


def save_config(config_path: Path, cfg: dict) -> bool:
    """保存配置到 JSON 文件（原子写入，合并而非覆盖）。返回 True=保存成功。"""
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        # 合并旧配置，避免覆盖其他字段
        merged = load_config(config_path)
        merged.update(cfg)
        tmp_path = config_path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(str(tmp_path), str(config_path))
        return True
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────
# 邮件通知（BUG-281: 外部访问方案二 —— 邮箱传递连接信息）
# ─────────────────────────────────────────────────────────────────────

def guess_smtp_server(email: str) -> str:
    """根据收件邮箱域名猜测 SMTP 服务器 (QQ/163/Gmail/Outlook/126/新浪)。"""
    e = email.lower()
    if "qq.com" in e:
        return "smtp.qq.com"
    if "163.com" in e:
        return "smtp.163.com"
    if "126.com" in e:
        return "smtp.126.com"
    if "gmail.com" in e:
        return "smtp.gmail.com"
    if "outlook.com" in e or "hotmail.com" in e or "live.com" in e:
        return "smtp.office365.com"
    if "sina.com" in e:
        return "smtp.sina.com"
    if "139.com" in e:
        return "smtp.139.com"
    return "smtp." + e.split("@")[-1]


def guess_smtp_port(server: str, use_ssl: bool = True) -> int:
    """SMTP 端口猜测: SSL=465, STARTTLS=587; Gmail/Outlook 走 587。"""
    s = server.lower()
    if use_ssl and not (("gmail" in s) or ("office365" in s) or ("outlook" in s)):
        return 465
    return 587


def send_connection_email(
    to_addr: str,
    auth_code: str,
    smtp_user: str,
    smtp_server: str,
    smtp_port: int,
    ipv6_addr: str | None,
    lan_ips: list[str],
    port: int,
    pair_code: str,
    admin_code: str | None = None,
    logger: Optional[logging.Logger] = None,
) -> tuple[bool, str]:
    """发送网盘连接信息邮件（App 邮件模式可自动解析）。

    正文含 `PTDRIVE|<ip>|<port>|<pairCode>` 行, 手机端 DriveMailFetcher 可识别。
    返回 (是否成功, 错误信息)。
    """
    if not to_addr or not auth_code:
        return False, "未配置收件邮箱或授权码"
    if not smtp_server:
        smtp_server = guess_smtp_server(to_addr)
    if not smtp_port:
        smtp_port = guess_smtp_port(smtp_server)
    # SMTP 登录用户默认与收件邮箱一致 (QQ/163 需用授权码登录, 用户名=邮箱)
    if not smtp_user:
        smtp_user = to_addr

    log = logger or logging.getLogger("phototrans_drive")

    # 组装连接信息
    lines = ["PhotoTrans 网盘 · 连接信息", ""]
    # IPv6 优先 (异地可用), 其次局域网 IPv4
    if ipv6_addr:
        lines.append(f"PTDRIVE|{ipv6_addr}|{port}|{pair_code}")
    elif lan_ips:
        lines.append(f"PTDRIVE|{lan_ips[0]}|{port}|{pair_code}")
    else:
        lines.append(f"PTDRIVE|127.0.0.1|{port}|{pair_code}")
    lines.append("")
    lines.append("请在 PhotoTrans App → 网盘 → 邮件模式 中填写收件邮箱与授权码,")
    lines.append("App 会自动读取本邮件并填入连接信息。")
    lines.append("")
    if ipv6_addr:
        lines.append(f"远程地址: [{ipv6_addr}]:{port}  (公网 IPv6, 需手机所在网络支持 IPv6)")
    if lan_ips:
        lines.append(f"局域网地址: {', '.join(f'{ip}:{port}' for ip in lan_ips)}")
    lines.append(f"配对码: {pair_code} (只读权限)")
    lines.append("")
    lines.append(f"发送时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")
    # BUG-283 安全修复: 邮件不再携带管理员码 (与 BUG-268 二维码策略一致),
    # 防止邮件被转发/泄露时管理权限码外泄。管理码只通过 server.py --admin-code 在控制台查看。

    body = "\n".join(lines)
    try:
        import smtplib
        from email.header import Header
        from email.mime.text import MIMEText

        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = Header("PTDRIVE 网盘连接信息", "utf-8")
        msg["From"] = smtp_user
        msg["To"] = to_addr

        # SSL 465 优先, 失败回退 STARTTLS 587 (BUG-283: 尊重用户配置的 smtp_port)
        last_err: str = ""
        # 用户显式指定端口 → 按其端口+模式尝试; 未指定 → 465 SSL 失败后回退 587 STARTTLS
        if smtp_port in (465, 587):
            attempts = [(smtp_port == 465, smtp_port)]
        elif smtp_port:
            attempts = [(True, smtp_port), (False, smtp_port)]
        else:
            attempts = [(True, 465), (False, 587)]
        for ssl_use, use_port in attempts:
            try:
                if ssl_use:
                    s = smtplib.SMTP_SSL(smtp_server, use_port, timeout=20)
                else:
                    s = smtplib.SMTP(smtp_server, use_port, timeout=20)
                    s.starttls()
                s.login(smtp_user, auth_code)
                s.sendmail(smtp_user, [to_addr], msg.as_string())
                s.quit()
                log.info(f"连接信息邮件已发送到 {to_addr}")
                return True, ""
            except Exception as e:
                last_err = str(e)
                log.warning(f"邮件发送尝试失败 ({'SSL' if ssl_use else 'STARTTLS'} {use_port}): {e}")
        return False, last_err
    except Exception as e:
        log.error(f"邮件发送异常: {e}")
        return False, str(e)



def check_port_available(port: int) -> tuple[bool, str]:
    """检查 TCP 端口是否可监听。返回 (可用, 错误说明)。

    注意：不设置 SO_REUSEADDR。Windows 下 SO_REUSEADDR 允许重复 bind 同一端口，
    会掩盖真实的端口占用（甚至导致端口劫持），因此这里用默认选项做严格检测。
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("0.0.0.0", port))
        s.close()
        return True, ""
    except OSError as e:
        return False, str(e)


def human_size(n: int) -> str:
    """字节数转人类可读字符串（如 1.5 GB）。"""
    if n < 1024:
        return f"{n} B"
    value = n / 1024
    for unit in ("KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def get_dir_size(path: Path) -> int:
    """递归计算目录总大小（字节）。空目录或不存在返回 0。

    注意：目录文件很多时会慢，仅用于启动面板 / --status 这种一次性展示。
    """
    if not path.exists():
        return 0
    total = 0
    try:
        for dirpath, _dirnames, filenames in os.walk(path):
            for f in filenames:
                try:
                    total += (Path(dirpath) / f).stat().st_size
                except OSError:
                    continue
    except Exception:
        pass
    return total


def open_directory(path: Path) -> bool:
    """在系统文件管理器中打开目录（跨平台）。返回 True=成功。"""
    try:
        if sys.platform == "win32":
            os.startfile(str(path))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
        return True
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────
# UDP 发现服务
# ─────────────────────────────────────────────────────────────────────

def udp_discovery(udp_port: int, tcp_port: int, logger: logging.Logger) -> None:
    """监听 UDP 发现请求：收到 'PT-DISCOVER' 就回复 'PT-DRIVE|<port>' 给来源地址。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", udp_port))
        s.settimeout(300)
        logger.info(f"UDP 发现监听启动：{udp_port}（手机 '自动查找电脑' 用）")
        
        last_log = 0.0
        
        while True:
            try:
                data, addr = s.recvfrom(512)
                msg = data.decode("utf-8", errors="replace").strip()
                
                if msg == "PT-DISCOVER":
                    s.sendto(f"PT-DRIVE|{tcp_port}\n".encode(), addr)
                    
                    # 限流日志：同 IP 最多每 60s 记录一次（防日志洪水 DoS）
                    now = time.time()
                    if now - last_log > 60:
                        logger.info(f"UDP 发现：已回复 {addr[0]}")
                        last_log = now
            
            except socket.timeout:
                continue
    
    except Exception as e:
        logger.error(f"UDP 发现服务异常：{e}")


# ─────────────────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────────────────

def _generate_qr_code(content: str, save_path: Path) -> bool:
    """生成连接二维码 PNG（内容如 PTDRIVE|ip|port|pairCode）。
    
    优先用 qrcode 库；失败时尝试 PIL 手绘简易二维码；都失败返回 False。
    返回是否成功生成。
    """
    try:
        import qrcode
        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=10,
            border=2,
        )
        qr.add_data(content)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        save_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(str(save_path))
        return True
    except Exception:
        pass
    # 降级：PIL 手绘简单二维码（数据量小时可用）
    try:
        import qrcode
        qr = qrcode.QRCode(box_size=10, border=2)
        qr.add_data(content)
        qr.make()
        save_path.parent.mkdir(parents=True, exist_ok=True)
        qr.make_image().save(str(save_path))
        return True
    except Exception:
        return False


def _print_ascii_qr(content: str) -> None:
    """在终端打印 ASCII 二维码（用半块字符，手机可扫码）。"""
    try:
        import qrcode
        qr = qrcode.QRCode(box_size=1, border=1)
        qr.add_data(content)
        qr.make()
        matrix = qr.get_matrix()
        for row in matrix:
            print("".join("██" if c else "  " for c in row))
    except Exception:
        pass


def _print_startup_banner(root: Path, port: int, pair: str,
                          ipv6_addr: str | None, lan_ips: list[str],
                          admin_code: str | None = None,
                          qr_files: list[str] | None = None) -> None:
    """打印清晰的启动面板（用 print，直观无日志前缀）。
    
    极简风格：分成「手机连接」「网盘信息」两个区块，每块标题一行、
    内容左对齐，一眼看到手机端要填什么。
    """
    used = human_size(get_dir_size(root))
    W = 58  # 面板宽度（内容区）
    line = "=" * W

    rows: list[str] = [""]

    # ── 标题 ──
    rows.append("=" * W)
    rows.append("  PhotoTrans 网盘 · 运行中")
    rows.append("=" * W)
    rows.append("")

    # ── 区块1: 手机连接 ──
    rows.append(line)
    rows.append("  [1] 手机连接")
    rows.append(line)
    if lan_ips:
        # 只显示第一个 IPv4（最常用），其余折叠
        primary = lan_ips[0]
        rows.append(f"  局域网  : {primary}:{port}")
        if len(lan_ips) > 1:
            rows.append(f"           (其他: {', '.join(f'{ip}:{port}' for ip in lan_ips[1:])})")
    else:
        rows.append("  局域网  : 未检测到（请确认已连接 WiFi）")
    if ipv6_addr:
        rows.append(f"  远程    : [{ipv6_addr}]:{port}")
    rows.append("")
    rows.append(f"  配对码  : {pair}   (只读 · 浏览/下载)")
    if admin_code:
        rows.append(f"           {admin_code}  (完整 · 上传/删除)")
    rows.append("")
    rows.append("  ⚠ 配对码为明文 6 位数字，仅限可信网络使用；")
    rows.append("    请勿在公共 Wi-Fi / 公网 IPv6 上运行。")
    rows.append("")

    # ── 区块2: 网盘信息 ──
    rows.append(line)
    rows.append("  [2] 网盘信息")
    rows.append(line)
    rows.append(f"  目录    : {root}")
    rows.append(f"  已用    : {used}")

    # ── 区块3: 二维码文件（若已生成）──
    if qr_files:
        rows.append("")
        rows.append(line)
        rows.append("  [3] 二维码文件（手机可保存/传输此图再扫）")
        rows.append(line)
        for qf in qr_files:
            rows.append(f"  · {qf}")

    print("\n".join(rows))


def _permission_label(permission: str) -> str:
    """权限值 → 中文标签（完整 / 只读）。"""
    return "完整" if permission == "full" else "只读"


def _show_status(root: Path, port: int, pair: str, auth: AuthStore,
                 admin_code: str | None = None) -> None:
    """显示配置摘要 + 设备 + 磁盘占用（--status 命令），然后退出。"""
    devices = auth.list_devices()
    used = human_size(get_dir_size(root))
    W = 56
    line = "=" * W
    rows = [""]
    rows.append("=" * W)
    rows.append("  PhotoTrans 网盘 · 状态")
    rows.append("=" * W)
    rows.append("")
    rows.append(line)
    rows.append("  [1] 配置")
    rows.append(line)
    rows.append(f"  目录    : {root}")
    rows.append(f"  端口    : {port}")
    rows.append(f"  已用    : {used}")
    rows.append(f"  配对码  : {pair}   (只读)")
    if admin_code:
        rows.append(f"           {admin_code}  (完整)")
    rows.append("")
    rows.append(line)
    rows.append(f"  [2] 已绑设备 · {len(devices)} 台")
    rows.append(line)
    if not devices:
        rows.append("  （暂无，手机连接后自动绑定）")
    else:
        for d in devices:
            perm = _permission_label(d["permission"])
            rows.append(f"  · {d['device_name']}（{perm}）")
            rows.append(f"      ID: {d['device_id']} · {d['paired_at'][:19]}")
    rows.append("")
    print("\n".join(rows))


def _handle_device_management(meta_dir: Path, args) -> None:
    """处理设备管理命令（--list-devices / --remove-device），处理完退出。"""
    auth = AuthStore(meta_dir / "devices.json")

    if args.list_devices:
        devices = auth.list_devices()
        W = 56
        line = "=" * W
        rows = [""]
        rows.append("=" * W)
        rows.append(f"  PhotoTrans 网盘 · 已绑设备 ({len(devices)} 台)")
        rows.append("=" * W)
        if not devices:
            rows.append("  暂无绑定设备")
            rows.append("  启动服务后，手机用配对码连接即可自动绑定")
        else:
            for d in devices:
                perm = _permission_label(d["permission"])
                rows.append("")
                rows.append(f"  · {d['device_name']}  ({perm}权限)")
                rows.append(f"    ID: {d['device_id']}")
                rows.append(f"    绑定: {d['paired_at']}")
        rows.append("")
        rows.append("=" * W)
        rows.append("  解绑: python server.py --remove-device <设备ID>")
        rows.append("")
        print("\n".join(rows))

    if args.remove_device:
        if auth.remove_device(args.remove_device):
            print(f"已解绑设备：{args.remove_device}")
        else:
            print(f"解绑失败：设备「{args.remove_device}」不存在或保存失败", file=sys.stderr)
            sys.exit(1)


def main() -> None:
    """程序主入口。"""
    # Windows 下打包后的 EXE 用 GBK 编码输出，非 GBK 字符（如项目符号、叉号）会触发
    # UnicodeEncodeError 崩溃。统一把 stdout/stderr 的错误处理设为 replace，
    # 兜底避免因个别特殊字符导致程序退出（源码运行时也会生效，无副作用）。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    config_path = Path.home() / ".phototransdrive" / "config.json"
    cfg = load_config(config_path)
    
    parser = argparse.ArgumentParser(
        description="PhotoTrans 网盘 · 电脑端服务端",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python server.py                            # 启动服务（默认配置）\n"
            "  python server.py --root D:/PhotoTransDrive  # 指定网盘目录\n"
            "  python server.py --pair-code 123456         # 指定普通配对码（只读）\n"
            "  python server.py --admin-code 888888        # 指定管理员配对码（完整权限）\n"
            "  python server.py --open                     # 启动并打开网盘目录\n"
            "  python server.py --status                   # 查看网盘状态摘要\n"
            "  python server.py --list-devices             # 查看已绑定设备\n"
            "  python server.py --remove-device <设备ID>   # 解绑设备\n"
        ),
    )
    
    parser.add_argument(
        "--config",
        default=str(config_path),
        help=f"配置文件路径（默认：{config_path}）"
    )
    
    parser.add_argument(
        "--root",
        default=cfg.get("root", str(Path.home() / "PhotoTransDrive")),
        help="网盘根目录（默认：用户目录/PhotoTransDrive）"
    )
    
    parser.add_argument(
        "--port",
        type=int,
        default=int(cfg.get("port", DEFAULT_PORT)),
        help=f"监听端口（默认 {DEFAULT_PORT}）"
    )
    
    parser.add_argument(
        "--udp-port",
        type=int,
        default=int(cfg.get("udp_port", DEFAULT_UDP_PORT)),
        help=f"UDP 发现端口（默认 {DEFAULT_UDP_PORT}；手机'自动查找电脑'用）"
    )
    
    parser.add_argument(
        "--pair-code",
        default=cfg.get("pair_code"),
        help="普通配对码（只读权限：仅浏览/下载；默认自动生成并保存，重启不变）"
    )
    
    parser.add_argument(
        "--admin-code",
        default=cfg.get("admin_code"),
        help="管理员配对码（完整权限：上传/删除/改动；默认自动生成并保存）"
    )
    
    # ── 邮件通知（BUG-281: 外部访问方案二）──
    parser.add_argument(
        "--email-to",
        default=cfg.get("email_to", ""),
        help="收件邮箱（配置后启动时自动把 IPv6+配对码 发到该邮箱，手机 App 邮件模式可读取）"
    )
    
    parser.add_argument(
        "--email-auth",
        default=cfg.get("email_auth", ""),
        help="发件邮箱授权码（SMTP 授权码，非登录密码；QQ 邮箱在 设置→账号→开启 SMTP 获取）"
    )
    
    parser.add_argument(
        "--email-user",
        default=cfg.get("email_user", ""),
        help="SMTP 登录用户名（默认=收件邮箱；多数邮箱用授权码登录时用户名就是邮箱地址）"
    )
    
    parser.add_argument(
        "--email-server",
        default=cfg.get("email_server", ""),
        help="SMTP 服务器（留空自动: qq→smtp.qq.com, 163→smtp.163.com, gmail→smtp.gmail.com）"
    )
    
    parser.add_argument(
        "--email-port",
        type=int,
        default=int(cfg.get("email_port", 0) or 0),
        help="SMTP 端口（留空自动: 465 SSL, 失败回退 587 STARTTLS）"
    )
    
    # 设备管理命令（执行后退出，不启动服务）
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="列出所有已绑定设备后退出"
    )
    
    parser.add_argument(
        "--remove-device",
        metavar="DEVICE_ID",
        default=None,
        help="解绑指定设备后退出（设备 ID 用 --list-devices 查看）"
    )
    
    parser.add_argument(
        "--status",
        action="store_true",
        help="显示网盘状态摘要（配置/设备/占用空间）后退出"
    )
    
    parser.add_argument(
        "--open",
        action="store_true",
        help="启动服务后自动在文件管理器中打开网盘目录"
    )
    
    args = parser.parse_args()
    
    # 元数据独立目录：网盘根目录只放用户文件，config/logs 不混入
    meta_dir = Path.home() / ".phototransdrive"
    root = Path(args.root)
    auth = AuthStore(meta_dir / "devices.json")

    # ── 设备管理命令（不需要配对码，执行后退出）──
    if args.list_devices or args.remove_device:
        _handle_device_management(meta_dir, args)
        return
    
    # 普通配对码（只读权限）优先级：--pair-code > 配置文件 > 随机生成并持久化
    if args.pair_code:
        pair = args.pair_code
        if pair != cfg.get("pair_code"):
            save_config(config_path, {"pair_code": pair})
    elif cfg.get("pair_code"):
        pair = cfg["pair_code"]
    else:
        pair = f"{secrets.randbelow(1000000):06d}"
        save_config(config_path, {"pair_code": pair})
    
    # 管理员配对码（完整权限）优先级：--admin-code > 配置文件 > 随机生成并持久化
    if args.admin_code:
        admin_code = args.admin_code
        if admin_code != cfg.get("admin_code"):
            save_config(config_path, {"admin_code": admin_code})
    elif cfg.get("admin_code"):
        admin_code = cfg["admin_code"]
    else:
        admin_code = f"{secrets.randbelow(1000000):06d}"
        save_config(config_path, {"admin_code": admin_code})
    
    # 两个配对码不能相同，否则权限无法区分
    if pair == admin_code:
        print(f"× 普通配对码与管理员配对码相同（{pair}），无法区分权限。", file=sys.stderr)
        print("  请用 --pair-code 和 --admin-code 指定两个不同的配对码。", file=sys.stderr)
        sys.exit(1)
    
    auth.set_pair_code(pair)
    auth.set_admin_code(admin_code)
    
    # ── 状态命令（展示配置摘要，执行后退出）──
    if args.status:
        _show_status(root, args.port, pair, auth, admin_code=admin_code)
        return
    
    # ── 邮件通知配置（BUG-281: 保存并启动时发送连接信息）──
    email_to = (args.email_to or "").strip()
    email_auth = (args.email_auth or "").strip()
    if email_to or email_auth:
        # 持久化邮箱配置，重启后自动沿用
        save_config(config_path, {
            "email_to": email_to,
            "email_auth": email_auth,
            "email_user": (args.email_user or "").strip(),
            "email_server": (args.email_server or "").strip(),
            "email_port": int(args.email_port or 0),
        })
    
    # ── 启动服务 ──
    root.mkdir(parents=True, exist_ok=True)
    
    logger = setup_logger(meta_dir)

    # BUG-268 清理：删除历史遗留在网盘根目录的二维码 PNG
    # （旧版本把含管理码的 PNG 写在 root 下，只读设备可下载提权；升级后清除）
    for legacy_qr in root.glob("连接二维码_*.png"):
        try:
            legacy_qr.unlink(missing_ok=True)
            logger.info(f"已删除历史二维码文件：{legacy_qr.name}（已迁移到安全目录）")
        except Exception as e:
            logger.warning(f"删除历史二维码失败：{legacy_qr.name}: {e}")
    
    # 端口占用检测：提前给出友好提示，而不是让底层 bind 抛堆栈
    ok, err = check_port_available(args.port)
    if not ok:
        print(f"× 端口 {args.port} 已被占用，无法启动。", file=sys.stderr)
        print("  请用 --port <其他端口> 换一个端口，或关闭占用该端口的程序。", file=sys.stderr)
        print(f"  详细信息：{err}", file=sys.stderr)
        sys.exit(1)
    
    # IPv6 检测（用于异地连接）
    ipv6_addr = get_ipv6_address()
    
    # 启动日志
    logger.info("=" * 60)
    logger.info(f"网盘根目录：{root}")
    logger.info(f"监听端口：{args.port}（UDP 发现端口 {args.udp_port}）")
    logger.info(f"普通配对码（只读）：{pair}")
    logger.info(f"管理员配对码（完整）：{admin_code}")
    if ipv6_addr:
        logger.info(f"IPv6 地址：[{ipv6_addr}]:{args.port}（异地连接用）")
    else:
        logger.info(f"IPv6 地址：无（仅局域网连接）")
    logger.info("=" * 60)
    
    # 创建服务器
    server = DriveServer(root, args.port, auth, logger, ipv6_addr=ipv6_addr)
    
    # UDP 发现：手机广播查询时回复自己的 IP+端口（免手输 IP）
    udp_thread = threading.Thread(
        target=udp_discovery,
        args=(args.udp_port, args.port, logger),
        daemon=True
    )
    udp_thread.start()
    
    # ── 生成连接二维码（手机扫码自动填 IP/端口/配对码）──
    # BUG-268 修复：二维码内容只用「只读配对码 pair」，绝不再携带 admin_code。
    # 同时 PNG 保存到 meta_dir（~/.phototransdrive，网盘共享范围之外）：
    # 之前存 root 下会被只读设备直接下载 PNG 拿到管理码 → 权限提升。
    lan_ips = get_lan_ipv4_addresses()
    
    # ── 邮件通知（BUG-281）: 配置了收件邮箱则启动时自动发送连接信息 ──
    if email_to:
        ok, err = send_connection_email(
            to_addr=email_to,
            auth_code=email_auth,
            smtp_user=(args.email_user or "").strip(),
            smtp_server=(args.email_server or "").strip(),
            smtp_port=int(args.email_port or 0),
            ipv6_addr=ipv6_addr,
            lan_ips=lan_ips,
            port=args.port,
            pair_code=pair,
            admin_code=admin_code,
            logger=logger,
        )
        if ok:
            logger.info("启动邮件通知已发送（手机 App 邮件模式可读取连接信息）")
        else:
            logger.warning(f"启动邮件通知发送失败: {err}")
    
    qr_contents: list[str] = []
    if lan_ips:
        qr_contents.append(("局域网", f"PTDRIVE|{lan_ips[0]}|{args.port}|{pair}"))
    if ipv6_addr:
        qr_contents.append(("远程", f"PTDRIVE|{ipv6_addr}|{args.port}|{pair}"))
    qr_saved: list[str] = []
    qr_dir = meta_dir
    try:
        qr_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    for label, qc in qr_contents:
        qr_name = f"连接二维码_{label}.png"
        qr_path = qr_dir / qr_name
        if _generate_qr_code(qc, qr_path):
            qr_saved.append(f"{label}: {qr_path}")

    # 打印清晰的启动面板（手机端怎么连，一目了然）
    _print_startup_banner(root, args.port, pair, ipv6_addr, lan_ips,
                          admin_code=admin_code, qr_files=qr_saved)

    # 终端 ASCII 二维码（方便直接对着屏幕扫）
    if qr_contents:
        print("")
        print("=" * 58)
        print("  [3] 手机扫码连接（对着屏幕扫下面的二维码）")
        print("=" * 58)
        for label, qc in qr_contents:
            print(f"  [{label}] 内容: {qc}")
            _print_ascii_qr(qc)
        print("")
    
    # --open：在文件管理器中打开网盘目录（传完文件直接看）
    if args.open:
        if open_directory(root):
            print(f"已在文件管理器中打开网盘目录：{root}")
        else:
            print(f"无法自动打开目录，请手动打开：{root}")
    
    # 启动服务器
    try:
        server.serve()
    except KeyboardInterrupt:
        logger.info("收到 Ctrl+C，关闭服务")
        server.stop()
    except RuntimeError as e:
        # serve() 在 bind 失败时抛出，这里给出友好提示（预检测有竞态的兜底）
        print(f"× {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
