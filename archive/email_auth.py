#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PhotoTrans 网盘 · 邮件授权模块
================================
功能:
  1. IMAP 轮询收件箱, 解析 [PT-REQ] 主题的申请邮件
  2. 校验发件人白名单, 自动生成一次性令牌
  3. SMTP 回发邮件, 附带令牌
  4. 提供 verify_token() 供 server.py 协议层调用

设计:
  - 令牌: 32 位 hex (128 bit), 单次使用, 15 分钟过期
  - 存储: 内存 dict (重启丢失, 初步实现可接受)
  - IMAP: 30-60 秒轮询, 建议用 Gmail 应用专用密码
  - 回发: SMTP 自动回复, 不发通知弹窗 (托盘后续加)

用法:
  在 server.py 中调用 EmailAuthManager 即可.
"""

import email
import imaplib
import json
import logging
import os
import re
import secrets
import smtplib
import threading
import time
from datetime import datetime, timedelta
from email.header import decode_header
from email.message import EmailMessage
from pathlib import Path

logger = logging.getLogger("phototrans_drive")

# ─── 邮件指令格式 ───
# 申请邮件:
#   主题: [PT-REQ]
#   正文: requestId=<uuid> filename=<name>
#   发件人: 必须在白名单
#
# 回复邮件:
#   主题: [PT-TOKEN] requestId=<uuid>
#   正文: 令牌: <token>
#          有效期: 15 分钟
#          使用说明: ...


class EmailAuthManager:
    """邮件授权管理器: IMAP 收申请 + SMTP 回发 + 令牌校验."""

    TOKEN_TTL_SECONDS = 900  # 15 分钟
    IMAP_POLL_INTERVAL = 60  # 60 秒轮询 (Gmail 推荐 >=1min)

    def __init__(self, config: dict, root: Path, logger: logging.Logger,
                 ipv6_addr: str | None = None, port: int = 47810):
        """
        config 字段:
          imap_host: IMAP 服务器 (如 imap.gmail.com)
          imap_port: IMAP 端口 (默认 993)
          imap_user: 邮箱账号
          imap_pass: 应用专用密码
          smtp_host: SMTP 服务器 (如 smtp.gmail.com)
          smtp_port: SMTP 端口 (默认 587)
          smtp_user: SMTP 账号 (通常同 imap_user)
          smtp_pass: SMTP 密码 (通常同 imap_pass)
          from_addr: 发件人显示地址
          allowed_senders: 白名单发件人列表 (逗号分隔的邮箱)
          auto_approve: 白名单命中是否自动批准 (默认 True)
        """
        self.imap_host = config.get("imap_host", "")
        self.imap_port = config.get("imap_port", 993)
        self.imap_user = config.get("imap_user", "")
        self.imap_pass = config.get("imap_pass", "")
        self.smtp_host = config.get("smtp_host", "")
        self.smtp_port = config.get("smtp_port", 587)
        self.smtp_user = config.get("smtp_user", self.imap_user)
        self.smtp_pass = config.get("smtp_pass", self.imap_pass)
        self.from_addr = config.get("from_addr", self.imap_user)
        self.allowed_senders = set(
            s.strip().lower() for s in config.get("allowed_senders", "").split(",") if s.strip()
        )
        self.auto_approve = config.get("auto_approve", True)
        self.IMAP_POLL_INTERVAL = config.get("imap_poll_interval", 60)
        self.root = root
        self.log = logger
        self.ipv6_addr = ipv6_addr  # IPv6 地址 (用于异地连接)
        self.port = port  # 服务端口

        # 令牌存储: {token: {"request_id":..., "filename":..., "expires_at":..., "used":False}}
        self._tokens: dict = {}
        self._lock = threading.Lock()
        self._seen_uids: list = []  # 已处理的邮件 UID (列表保持顺序)
        self._seen_set: set = set()  # 快速查找
        # 元数据文件放 ~/.phototransdrive/ (不混入用户文件目录)
        self._seen_file = Path.home() / ".phototransdrive" / ".email_seen_uids.json"
        self._load_seen_uids()

        self._running = False
        self._thread = None

    # ─── 生命周期 ───

    def start(self):
        """启动 IMAP 轮询线程."""
        if not self.imap_host or not self.imap_user:
            self.log.warning("邮件授权未配置 (imap_host/imap_user 为空), 跳过启动")
            return False
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        self.log.info(f"邮件授权已启动: IMAP={self.imap_host}:{self.imap_port} "
                       f"白名单={len(self.allowed_senders)}人 自动批准={self.auto_approve}")
        return True

    def stop(self):
        """停止 IMAP 轮询."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    # ─── 令牌校验 (协议层调用) ───

    def verify_token(self, token: str) -> str | None:
        """
        校验邮件令牌.
        返回: 文件名 (成功) 或 None (失败: 不存在/过期/已用)
        """
        with self._lock:
            rec = self._tokens.get(token)
            if rec is None:
                return None
            if rec["used"]:
                return None
            if time.time() > rec["expires_at"]:
                self._tokens.pop(token, None)
                return None
            # 标记为已用 (单次)
            rec["used"] = True
            return rec["filename"]

    def rollback_token(self, token: str):
        """回滚已消耗的令牌 (持久化失败时允许客户端重试)."""
        with self._lock:
            rec = self._tokens.get(token)
            if rec:
                rec["used"] = False

    def cleanup_expired_tokens(self):
        """清理已过期或已使用的令牌, 防止内存泄漏."""
        with self._lock:
            now = time.time()
            to_remove = [t for t, rec in self._tokens.items()
                         if rec["used"] or now > rec["expires_at"]]
            for t in to_remove:
                del self._tokens[t]
            if to_remove:
                self.log.info(f"清理令牌: 移除 {len(to_remove)} 个 (剩余 {len(self._tokens)})")
            return len(to_remove)

    # ─── IMAP 轮询 ───

    MAX_RETRIES = 3  # IMAP/SMTP 最大重试次数
    RETRY_DELAY = 5  # 重试间隔 (秒)

    def _poll_loop(self):
        while self._running:
            for attempt in range(1, self.MAX_RETRIES + 1):
                try:
                    self._poll_once()
                    break  # 成功则跳出重试
                except Exception as e:
                    if attempt < self.MAX_RETRIES:
                        self.log.warning(f"IMAP 轮询第 {attempt} 次失败, {self.RETRY_DELAY}s 后重试: {e}")
                        time.sleep(self.RETRY_DELAY)
                    else:
                        self.log.error(f"IMAP 轮询 {self.MAX_RETRIES} 次均失败, 本轮跳过: {e}")
            # 每轮清理过期令牌
            self.cleanup_expired_tokens()
            # 轮询间隔
            for _ in range(self.IMAP_POLL_INTERVAL):
                if not self._running:
                    break
                time.sleep(1)

    def _poll_once(self):
        """单次 IMAP 轮询: 拉取新邮件, 解析 [PT-REQ] 申请."""
        conn = None
        try:
            conn = imaplib.IMAP4_SSL(self.imap_host, self.imap_port)
            conn.login(self.imap_user, self.imap_pass)
            conn.select("INBOX")
            # 只拉未读邮件
            status, data = conn.search(None, "UNSEEN")
            if status != "OK":
                return
            uids = data[0].split()
            new_uids = [u for u in uids if u not in self._seen_set]

            for uid in new_uids:
                try:
                    status, msg_data = conn.fetch(uid, "(RFC822)")
                    if status != "OK" or not msg_data or not msg_data[0]:
                        continue
                    raw = msg_data[0][1]
                    msg = email.message_from_bytes(raw)
                    # 只有真正处理了的 PT-REQ 邮件才标记 (防丢失)
                    handled = self._handle_incoming(msg, uid.decode())
                    if handled:
                        self._seen_set.add(uid)
                        self._seen_uids.append(uid)
                except Exception as e:
                    self.log.error(f"处理邮件 uid={uid} 异常: {e}")
                    # 不标记, 下次轮询会重试

            self._save_seen_uids()
        except Exception as e:
            self.log.error(f"IMAP 轮询异常: {e}")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
                try:
                    conn.logout()
                except Exception:
                    pass

    def _handle_incoming(self, msg, uid) -> bool:
        """解析单封入站邮件: 检查主题 [PT-REQ], 校验白名单, 生成令牌, 回发.
        返回 True = 邮件已被处理 (含签发令牌), 可标记 _seen_uids;
        返回 False = 非 PT-REQ 或无效邮件, 不标记 (下次轮询不重试)."""
        # 解析主题
        subject = self._decode_header(msg.get("Subject", ""))
        if "[PT-REQ]" not in subject:
            return False  # 非申请邮件, 忽略 (不标记)

        # 解析发件人
        from_header = msg.get("From", "")
        sender = self._parse_email(from_header).lower()
        if not sender:
            self.log.warning(f"邮件 uid={uid}: 无法解析发件人 '{from_header}'")
            return False

        # 白名单校验
        if sender not in self.allowed_senders:
            self.log.warning(f"邮件 uid={uid}: 发件人 {sender} 不在白名单, 拒绝")
            # SMTP 成功才标记; 失败不标记, 下次轮询重试
            if self._reply_reject(sender, "发件人未授权"):
                return True
            else:
                return False

        # 解析正文: requestId + filename
        body = self._decode_body(msg)
        request_id = self._extract_field(body, "requestId")
        filename = self._extract_field(body, "filename")
        if not request_id or not filename:
            self.log.warning(f"邮件 uid={uid}: 缺少 requestId 或 filename, 正文={body[:200]}")
            return False  # 无效邮件, 不标记 (下次轮询可重试)

        # 校验文件名合法性 (防路径穿越)
        filename_clean = os.path.basename(filename.strip())
        if not filename_clean or filename_clean in (".", ".."):
            self.log.warning(f"邮件 uid={uid}: 文件名非法 '{filename}'")
            return False
        if len(filename_clean) > 200:
            self.log.warning(f"邮件 uid={uid}: 文件名过长 ({len(filename_clean)} 字符), 截断")
            filename_clean = filename_clean[:200]
        # Windows 非法字符
        if re.search(r'[\\/:*?"<>|]', filename_clean):
            filename_clean = re.sub(r'[\\/:*?"<>|]', "_", filename_clean)

        # 生成令牌
        token = secrets.token_hex(16)
        expires_at = time.time() + self.TOKEN_TTL_SECONDS
        with self._lock:
            self._tokens[token] = {
                "request_id": request_id,
                "filename": filename_clean,
                "sender": sender,
                "issued_at": datetime.now().isoformat(),
                "expires_at": expires_at,
                "used": False,
            }

        self.log.info(f"邮件授权: 签发令牌 uid={uid} sender={sender} "
                       f"file={filename_clean} requestId={request_id}")
        # 先发邮件, 成功才标记 (防令牌已签发但用户收不到)
        if self._reply_token(sender, request_id, token, filename_clean):
            return True
        else:
            # SMTP 失败: 回滚令牌
            with self._lock:
                self._tokens.pop(token, None)
            return False

    # ─── 邮件回复 ───

    def _reply_token(self, to_addr: str, request_id: str, token: str, filename: str) -> bool:
        """SMTP 回发邮件, 附带令牌. 返回 True=发送成功."""
        try:
            msg = EmailMessage()
            msg["From"] = self.from_addr
            msg["To"] = to_addr
            msg["Subject"] = f"[PT-TOKEN] requestId={request_id}"
            
            # 构建连接地址 (IPv4 + IPv6)
            conn_info = ""
            if self.ipv6_addr:
                conn_info = f"\n\n异地连接地址 (IPv6):\n  [{self.ipv6_addr}]:{self.port}\n"
                conn_info += f"  (需双方都有 IPv6 网络, 或电脑端开启打洞服务)\n"
            
            msg.set_content(
                f"PhotoTrans 网盘令牌\n"
                f"===================\n\n"
                f"文件: {filename}\n"
                f"令牌: {token}\n"
                f"有效期: 15 分钟\n"
                f"端口: {self.port}\n\n"
                f"使用说明:\n"
                f"  1. 打开 PhotoTrans App → 网盘\n"
                f"  2. 选择「令牌连接」\n"
                f"  3. 输入电脑地址 + 上述令牌\n"
                f"  4. 令牌单次有效, 用后即焚\n"
                f"{conn_info}\n"
                f"—— 此邮件由 PhotoTrans 网盘自动生成\n"
            )
            if not self._smtp_send(msg, to_addr):
                return False
            self.log.info(f"令牌邮件已发送: {to_addr}")
            return True
        except Exception as e:
            self.log.error(f"令牌邮件发送失败: {e}")
            return False

    def _reply_reject(self, to_addr: str, reason: str) -> bool:
        """回复拒绝通知."""
        try:
            msg = EmailMessage()
            msg["From"] = self.from_addr
            msg["To"] = to_addr
            msg["Subject"] = "[PT-REJECT] PhotoTrans 请求已拒绝"
            msg.set_content(
                f"PhotoTrans 网盘: 请求被拒绝\n"
                f"原因: {reason}\n\n"
                f"如认为有误, 请确认:\n"
                f"  - 发件人是否在电脑端白名单中\n"
                f"  - 邮件格式是否正确 ([PT-REQ] 主题 + requestId + filename 正文)\n"
            )
            if not self._smtp_send(msg, to_addr):
                return False
            return True
        except Exception as e:
            self.log.error(f"拒绝邮件发送失败: {e}")
            return False

    def _smtp_send(self, msg: EmailMessage, to_addr: str) -> bool:
        """发送 SMTP 邮件 (带自动重试). 返回 True=发送成功, False=全部失败."""
        if not self.smtp_host:
            self.log.error("SMTP 未配置 (smtp_host 为空)")
            return False
        last_error = None
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30) as s:
                    s.ehlo()
                    if self.smtp_port == 587:
                        s.starttls()
                        s.ehlo()
                    s.login(self.smtp_user, self.smtp_pass)
                    s.send_message(msg)
                    return True
            except Exception as e:
                last_error = e
                if attempt < self.MAX_RETRIES:
                    self.log.warning(f"SMTP 发送第 {attempt} 次失败, {self.RETRY_DELAY}s 后重试: {e}")
                    time.sleep(self.RETRY_DELAY)
        self.log.error(f"SMTP 发送 {self.MAX_RETRIES} 次均失败: {last_error}")
        return False

    # ─── 辅助方法 ───

    @staticmethod
    def _decode_header(value: str) -> str:
        """解码邮件头 (处理中文等)."""
        if not value:
            return ""
        parts = decode_header(value)
        decoded = []
        for text, enc in parts:
            if isinstance(text, bytes):
                try:
                    decoded.append(text.decode(enc or "utf-8", errors="replace"))
                except Exception:
                    decoded.append(text.decode("utf-8", errors="replace"))
            else:
                decoded.append(text)
        return " ".join(decoded)

    @staticmethod
    def _decode_body(msg) -> str:
        """提取邮件正文 (纯文本部分). 多部分邮件合并所有 text/plain."""
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                if ctype == "text/plain":
                    try:
                        payload = part.get_payload(decode=True)
                        if payload:
                            charset = part.get_content_charset() or "utf-8"
                            body += payload.decode(charset, errors="replace") + "\n"
                    except Exception:
                        pass
        else:
            try:
                payload = msg.get_payload(decode=True)
                if payload:
                    charset = msg.get_content_charset() or "utf-8"
                    body = payload.decode(charset, errors="replace")
            except Exception:
                body = msg.get_payload() or ""
        return body

    @staticmethod
    def _extract_field(text: str, field: str) -> str:
        """从文本中提取 field=value 格式的值.
        匹配到行尾或下一个字段标记 (支持带空格的值)."""
        m = re.search(rf'{field}\s*[=:]\s*(.*?)(?=\s+\w+\s*[=:]|\n|$)',
                      text, re.IGNORECASE | re.MULTILINE)
        return m.group(1).strip() if m else ""

    @staticmethod
    def _parse_email(addr: str) -> str:
        """从 'Name <email@example.com>' 格式中提取邮箱."""
        m = re.search(r'<([^>]+)>', addr)
        if m:
            return m.group(1).strip()
        # 无尖括号, 直接返回 (可能是裸邮箱)
        return addr.strip().lower()

    # ─── 已处理 UID 持久化 (重启不重复处理) ───

    def _save_seen_uids(self):
        try:
            # 只保留最近 1000 条, 防止无限增长 (列表保持时间顺序)
            trimmed = self._seen_uids[-1000:]
            self._seen_uids = trimmed
            self._seen_set = set(trimmed)
            # bytes → str 以便 JSON 序列化
            str_uids = [u.decode() if isinstance(u, bytes) else str(u) for u in trimmed]
            self._seen_file.parent.mkdir(parents=True, exist_ok=True)
            # 原子写入 (防崩溃损坏)
            import os
            tmp_path = self._seen_file.with_suffix(".json.tmp")
            tmp_path.write_text(
                json.dumps({"uids": str_uids}, ensure_ascii=False), encoding="utf-8"
            )
            os.replace(str(tmp_path), str(self._seen_file))
        except Exception as e:
            self.log.error(f"保存已处理 UID 失败: {e}")

    def _load_seen_uids(self):
        try:
            if self._seen_file.exists():
                data = json.loads(self._seen_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    # str → bytes 还原
                    raw = data.get("uids", [])
                    self._seen_uids = [u.encode() if isinstance(u, str) else u for u in raw]
                    self._seen_set = set(self._seen_uids)
                else:
                    self.log.warning(f"已处理 UID 文件格式错误 (期望 dict, 实际 {type(data).__name__}), 已重置")
        except Exception:
            self._seen_uids = []
            self._seen_set = set()
