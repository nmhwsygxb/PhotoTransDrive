#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PhotoTrans 网盘 · Web UI (多用户)
==================================
浏览器访问的网盘界面，支持多用户同时登录。
不依赖手机 App，用配对码认证（普通=只读，管理员=完整权限）。

用法:
    python web_ui.py --root "D:/MyDrive" --port 8080

功能:
- 浏览器登录 (配对码)
- 文件列表/上传/下载/删除
- 多用户同时在线
- 轻量级 (仅 Python 标准库)
"""

import argparse
import http.server
import json
import mimetypes
import os
import secrets
import socket
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Optional

# 复用 server.py 的认证
sys.path.insert(0, str(Path(__file__).parent))
from server import AuthStore, get_ipv6_address, safe_resolve, sanitize_path, sanitize_filename, load_config, MAX_FILE_SIZE, IPRateLimiter


class WebDriveSession:
    """Web 会话 (模拟设备令牌)."""
    def __init__(self, device_id: str, device_name: str, token: str):
        self.device_id = device_id
        self.device_name = device_name
        self.token = token
        self.created_at = time.time()
        self.last_active = time.time()

    def touch(self):
        self.last_active = time.time()


class WebDriveHandler(http.server.BaseHTTPRequestHandler):
    """Web UI HTTP 处理器."""
    
    server_version = "PhotoTransDrive/1.0"
    
    def log_message(self, format, *args):
        # 简化日志
        self.server.log.info(f"[WEB] {self.client_address[0]} {format % args}")
    
    def send_json(self, data: dict, status: int = 200, set_cookie: str | None = None):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        # Set-Cookie 必须在 send_response 之后、end_headers 之前发送，
        # 否则会排在响应行之前导致 BadStatusLine。
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        # 不设 Access-Control-Allow-Origin：本地网盘同源访问即可，
        # 全开 CORS 会配合已登录会话放大 CSRF 面。
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))
    
    def send_error_json(self, message: str, status: int = 400):
        self.send_json({"error": message}, status)
    
    def send_html(self, html: str, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))
    
    def send_file(self, filepath: Path):
        try:
            self.send_response(200)
            content_type = mimetypes.guess_type(str(filepath))[0] or "application/octet-stream"
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(filepath.stat().st_size))
            # 防响应头注入：filename 用 RFC 5987 编码（quote 会转义 " 与 \r\n 等危险字符）
            encoded = urllib.parse.quote(filepath.name)
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{encoded}")
            self.end_headers()
            with open(filepath, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except OSError:
            # 文件在 stat/open 之间被删除等 TOCTOU 竞态：响应头可能已发，只能静默关闭
            pass
    
    def get_session(self) -> Optional[WebDriveSession]:
        """从 Cookie 获取会话（同时校验过期，过期则清除）。"""
        cookie = self.headers.get("Cookie", "")
        for item in cookie.split(";"):
            if "session=" in item:
                sid = item.split("session=")[1].strip()
                session = self.server.sessions.get(sid)
                if session is None:
                    return None
                # 会话过期（24 小时未活动）→ 清除并视为未登录
                if time.time() - session.last_active > WebDriveServer.SESSION_TTL:
                    self.server.sessions.pop(sid, None)
                    return None
                return session
        return None
    
    def _has_full_permission(self, session: WebDriveSession) -> bool:
        """检查会话是否完整权限（可写）。只读设备返回 False。"""
        return self.server.auth.device_permission(session.device_id) == "full"
    
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        
        if path == "/" or path == "/index.html":
            self.send_html(INDEX_HTML)
        elif path == "/api/status":
            self.handle_status()
        elif path == "/api/list":
            self.handle_list(parsed)
        elif path == "/api/download":
            self.handle_download(parsed)
        elif path == "/api/logout":
            self.handle_logout()
        elif path == "/favicon.ico":
            self.send_error(404)
        else:
            self.send_error_json("未知路径", 404)
    
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        
        if path == "/api/login":
            self.handle_login()
        elif path == "/api/upload":
            self.handle_upload(parsed)
        elif path == "/api/delete":
            # 破坏性操作用 POST，避免被 GET 触发（CSRF）
            self.handle_delete(parsed)
        else:
            self.send_error_json("未知路径", 404)
    
    def handle_status(self):
        session = self.get_session()
        data = {
            "connected": session is not None,
            "device_name": session.device_name if session else None,
            "port": self.server.port,
            "time": datetime.now().isoformat(),
        }
        # ipv6_addr 是服务端公网地址，未认证时不下发，避免泄露给局域网内未授权访问者
        if session:
            data["ipv6_addr"] = self.server.ipv6_addr
        self.send_json(data)
    
    def handle_login(self):
        content_length = int(self.headers.get("Content-Length", 0))
        # 登录 body 很小，限制 1MB 防内存 DoS
        if content_length > 1024 * 1024:
            self.send_error_json("请求体过大", 413)
            return
        try:
            body = self.rfile.read(content_length).decode("utf-8")
            params = json.loads(body)
            if not isinstance(params, dict):
                params = {}
        except (ValueError, json.JSONDecodeError):
            self.send_error_json("请求格式错误", 400)
            return
        
        pair_code = params.get("pair_code", "")
        device_name = params.get("device_name", "Web Browser")
        ip = self.client_address[0]
        
        # 暴力破解防护：锁定窗口内拒绝继续尝试（与 server.py 的 TCP 配对一致）
        if self.server._fail_limiter.is_locked(ip):
            self.server.log.warning(f"[WEB] {ip} 登录被锁定（失败过多）")
            self.send_error_json("尝试过多，请稍后再试", 429)
            return
        
        # 配对码登录
        if pair_code:
            device_id = f"web-{secrets.token_hex(8)}"
            token = self.server.auth.pair(device_id, pair_code, device_name)
            if token:
                self.server._fail_limiter.clear(ip)
                session = WebDriveSession(device_id, device_name, token)
                sid = secrets.token_hex(16)
                self.server.sessions[sid] = session
                self.send_json(
                    {"success": True, "device_name": device_name},
                    set_cookie=f"session={sid}; Path=/; HttpOnly; SameSite=Strict; Max-Age={WebDriveServer.SESSION_TTL}",
                )
                return
        
        self.server._fail_limiter.record_fail(ip)
        self.send_error_json("认证失败: 配对码无效", 401)
    
    def handle_logout(self):
        session = self.get_session()
        if session:
            sid = None
            for k, v in self.server.sessions.items():
                if v == session:
                    sid = k
                    break
            if sid:
                del self.server.sessions[sid]
        self.send_json({"success": True}, set_cookie="session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")
    
    def handle_list(self, parsed):
        session = self.get_session()
        if not session:
            self.send_error_json("未登录", 401)
            return
        
        # 验证令牌
        if not self.server.auth.verify(session.device_id, session.token):
            self.send_error_json("令牌无效或已过期", 401)
            return
        
        session.touch()
        
        path_param = urllib.parse.parse_qs(parsed.query).get("path", ["/"])[0]
        target = safe_resolve(self.server.root, path_param)
        if target is None:
            self.send_error_json("路径非法", 400)
            return
        if not target.is_dir():
            self.send_error_json("不是目录", 400)
            return
        
        items = []
        for entry in sorted(target.iterdir()):
            try:
                st = entry.stat()
                items.append({
                    "name": entry.name,
                    "isDir": entry.is_dir(),
                    "size": st.st_size if entry.is_file() else 0,
                    "mtime": int(st.st_mtime),
                })
            except OSError:
                continue
        
        self.send_json({"items": items, "path": path_param})
    
    def handle_upload(self, parsed):
        session = self.get_session()
        if not session:
            self.send_error_json("未登录", 401)
            return
        
        # 验证令牌
        if not self.server.auth.verify(session.device_id, session.token):
            self.send_error_json("令牌无效或已过期", 401)
            return
        
        session.touch()
        
        # 权限检查：只读设备不能上传
        if not self._has_full_permission(session):
            self.send_error_json("只读权限，不能修改或删除", 403)
            return
        
        path_param = urllib.parse.parse_qs(parsed.query).get("path", ["/"])[0]
        
        # 简化: 读取原始 body（文件字节），文件名走 X-Filename 头
        # 注意：前端 fetch 上传 File/Blob 时 Content-Type 是 file.type，并非 multipart，
        # 因此这里不做 Content-Type 校验，只按 Content-Length 读原始字节。
        content_length = int(self.headers.get("Content-Length", 0))
        # 大小限制：防磁盘/内存 DoS（与 server.py 的 TCP 上传保持一致）
        if content_length > MAX_FILE_SIZE:
            self.send_error_json("文件过大", 413)
            return
        data = self.rfile.read(content_length)
        
        # 提取文件名 + 净化（放入 try，sanitize_filename 对控制字符/超长/双向字符会抛 ValueError）
        try:
            filename = self.headers.get("X-Filename", "upload.bin")
            filename = sanitize_filename(filename)
            raw_safe = sanitize_path(path_param + "/" + filename)
            target = safe_resolve(self.server.root, raw_safe)
        except ValueError:
            self.send_error_json("文件名非法", 400)
            return
        
        if target is None:
            self.send_error_json("路径非法", 400)
            return
        
        # 写入文件（排他创建，防误覆盖；与 server.py 的 TCP 上传一致）
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "xb") as f:
                f.write(data)
            self.server.log.info(f"[WEB] 上传: {raw_safe} ({len(data)} bytes)")
            self.send_json({"success": True, "path": raw_safe, "size": len(data)})
        except FileExistsError:
            self.send_error_json("同名文件已存在（怕误覆盖，请改名重传）", 409)
        except Exception:
            self.server.log.exception("[WEB] 上传失败")
            # 不泄露内部异常（可能含文件路径等敏感信息）
            self.send_error_json("上传失败", 500)
    
    def handle_download(self, parsed):
        session = self.get_session()
        if not session:
            self.send_error_json("未登录", 401)
            return
        
        # 验证令牌
        if not self.server.auth.verify(session.device_id, session.token):
            self.send_error_json("令牌无效或已过期", 401)
            return
        
        session.touch()
        
        path_param = urllib.parse.parse_qs(parsed.query).get("path", ["/"])[0]
        target = safe_resolve(self.server.root, path_param)
        if target is None or not target.is_file():
            self.send_error_json("文件不存在", 404)
            return
        
        self.send_file(target)
    
    def handle_delete(self, parsed):
        session = self.get_session()
        if not session:
            self.send_error_json("未登录", 401)
            return
        
        # 验证令牌
        if not self.server.auth.verify(session.device_id, session.token):
            self.send_error_json("令牌无效或已过期", 401)
            return
        
        session.touch()
        
        # 权限检查：只读设备不能删除
        if not self._has_full_permission(session):
            self.send_error_json("只读权限，不能修改或删除", 403)
            return
        
        path_param = urllib.parse.parse_qs(parsed.query).get("path", ["/"])[0]
        target = safe_resolve(self.server.root, path_param)
        if target is None:
            self.send_error_json("路径非法", 400)
            return
        if target == self.server.root:
            self.send_error_json("不允许删除根目录", 400)
            return
        
        try:
            if target.is_dir():
                if any(target.iterdir()):
                    self.send_error_json("目录非空", 400)
                    return
                target.rmdir()
            else:
                target.unlink()
            self.server.log.info(f"[WEB] 删除: {path_param}")
            self.send_json({"success": True})
        except Exception:
            self.server.log.exception("[WEB] 删除失败")
            # 不泄露内部异常（可能含文件路径等敏感信息）
            self.send_error_json("删除失败", 500)


INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PhotoTrans 网盘</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f5f5f5; }
        .container { max-width: 800px; margin: 50px auto; padding: 20px; }
        .card { background: white; border-radius: 8px; padding: 30px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
        h1 { color: #333; margin-bottom: 20px; }
        .login-form { display: flex; flex-direction: column; gap: 15px; }
        input { padding: 12px; border: 1px solid #ddd; border-radius: 4px; font-size: 14px; }
        button { padding: 12px; background: #4a90d9; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 14px; }
        button:hover { background: #357abd; }
        .status { color: #666; font-size: 13px; margin-top: 15px; }
        .file-list { margin-top: 20px; }
        .file-item { display: flex; justify-content: space-between; padding: 10px; border-bottom: 1px solid #eee; cursor: pointer; }
        .file-item:hover { background: #f9f9f9; }
        .file-icon { margin-right: 10px; }
        .file-size { color: #999; font-size: 12px; }
        .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
        .btn-danger { background: #e74c3c; }
        .btn-danger:hover { background: #c0392b; }
        .path-bar { background: #f9f9f9; padding: 10px; border-radius: 4px; margin-bottom: 15px; }
        .upload-area { border: 2px dashed #ddd; padding: 30px; text-align: center; border-radius: 4px; margin-bottom: 15px; }
        .upload-area:hover { border-color: #4a90d9; }
        .toast { position: fixed; top: 20px; right: 20px; padding: 12px 20px; background: #333; color: white; border-radius: 4px; opacity: 0; transition: opacity 0.3s; }
        .toast.show { opacity: 1; }
    </style>
</head>
<body>
    <div class="container">
        <div class="card">
            <h1>📁 PhotoTrans 网盘</h1>
            
            <!-- 登录界面 -->
            <div id="login-screen" class="login-form">
                <input type="text" id="device-name" placeholder="设备名称 (如: 我的电脑)" value="Web Browser">
                <input type="password" id="pair-code" placeholder="配对码 (如: 123456)">
                <button onclick="login()">登录</button>
                <div class="status" id="server-status">连接中...</div>
            </div>
            
            <!-- 文件界面 -->
            <div id="file-screen" style="display: none;">
                <div class="header">
                    <span id="current-path">/</span>
                    <button class="btn-danger" onclick="logout()">退出</button>
                </div>
                <div class="path-bar">
                    <input type="text" id="file-name" placeholder="新文件名">
                    <button onclick="createFile()">新建文件</button>
                    <button onclick="uploadFile()">上传文件</button>
                    <input type="file" id="upload-input" style="display:none;" onchange="uploadFileInput()">
                </div>
                <div class="upload-area" onclick="document.getElementById('upload-input').click()">
                    点击或拖拽文件到此处上传
                </div>
                <div class="file-list" id="file-list"></div>
            </div>
        </div>
    </div>
    
    <div class="toast" id="toast"></div>
    
    <script>
        let serverStatus = null;
        
        // 获取服务器状态
        async function getStatus() {
            try {
                const resp = await fetch('/api/status');
                serverStatus = await resp.json();
                document.getElementById('server-status').innerHTML = 
                    `端口: ${serverStatus.port} | IPv6: ${serverStatus.ipv6_addr || '无'}<br>` +
                    `时间: ${serverStatus.time}`;
            } catch(e) {
                document.getElementById('server-status').textContent = '无法连接服务器';
            }
        }
        
        // 登录
        async function login() {
            const pairCode = document.getElementById('pair-code').value;
            const deviceName = document.getElementById('device-name').value;
            
            const body = JSON.stringify({
                pair_code: pairCode,
                device_name: deviceName
            });
            
            try {
                const resp = await fetch('/api/login', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: body
                });
                const data = await resp.json();
                if (data.success) {
                    document.getElementById('login-screen').style.display = 'none';
                    document.getElementById('file-screen').style.display = 'block';
                    loadFiles('/');
                    showToast('登录成功');
                } else {
                    showToast(data.error || '登录失败', true);
                }
            } catch(e) {
                showToast('登录失败: ' + e.message, true);
            }
        }
        
        // 退出
        async function logout() {
            await fetch('/api/logout');
            document.getElementById('login-screen').style.display = 'flex';
            document.getElementById('file-screen').style.display = 'none';
            showToast('已退出');
        }
        
        // 加载文件列表（用 DOM API + textContent，避免 innerHTML 拼接带来的 XSS）
        async function loadFiles(path) {
            document.getElementById('current-path').textContent = path;
            const resp = await fetch(`/api/list?path=${encodeURIComponent(path)}`);
            const data = await resp.json();
            
            const list = document.getElementById('file-list');
            list.innerHTML = '';
            
            if (path !== '/') {
                const parent = path.replace(/\\/g, '/').split('/').slice(0, -1).join('/') || '/';
                const up = document.createElement('div');
                up.className = 'file-item';
                up.dataset.path = parent;
                up.dataset.dir = 'true';
                up.innerHTML = '<span class="file-icon">⬆️</span><span>..</span><span class="file-size"></span>';
                list.appendChild(up);
            }
            
            for (const item of data.items) {
                const icon = item.isDir ? '📁' : '📄';
                const size = item.isDir ? '' : formatSize(item.size);
                const itemPath = path === '/' ? '/' + item.name : path + '/' + item.name;
                
                const div = document.createElement('div');
                div.className = 'file-item';
                div.dataset.path = itemPath;
                div.dataset.dir = item.isDir ? 'true' : 'false';
                
                const nameSpan = document.createElement('span');
                const iconSpan = document.createElement('span');
                iconSpan.className = 'file-icon';
                iconSpan.textContent = icon;
                nameSpan.appendChild(iconSpan);
                nameSpan.appendChild(document.createTextNode(item.name));
                
                const sizeSpan = document.createElement('span');
                sizeSpan.className = 'file-size';
                sizeSpan.textContent = size;
                
                div.appendChild(nameSpan);
                div.appendChild(sizeSpan);
                list.appendChild(div);
            }
        }
        
        // 文件列表点击：事件委托（用 dataset 传路径，避免内联 onclick 的注入风险）
        document.getElementById('file-list').addEventListener('click', function(e) {
            const item = e.target.closest('.file-item');
            if (!item || !item.dataset.path) return;
            if (item.dataset.dir === 'true') {
                loadFiles(item.dataset.path);
            } else {
                downloadFile(item.dataset.path);
            }
        });
        
        // 下载文件
        function downloadFile(path) {
            window.open(`/api/download?path=${encodeURIComponent(path)}`, '_blank');
        }
        
        // 上传文件
        async function uploadFileInput() {
            const input = document.getElementById('upload-input');
            if (input.files.length === 0) return;
            
            const file = input.files[0];
            const path = document.getElementById('current-path').textContent;
            
            const resp = await fetch(`/api/upload?path=${encodeURIComponent(path)}`, {
                method: 'POST',
                headers: { 'X-Filename': file.name },
                body: file
            });
            
            const data = await resp.json();
            if (data.success) {
                showToast('上传成功: ' + file.name);
                loadFiles(path);
            } else {
                showToast(data.error || '上传失败', true);
            }
            
            input.value = '';
        }
        
        // 新建文件
        async function createFile() {
            const name = document.getElementById('file-name').value;
            if (!name) { showToast('请输入文件名', true); return; }
            
            const path = document.getElementById('current-path').textContent;
            const resp = await fetch(`/api/upload?path=${encodeURIComponent(path)}`, {
                method: 'POST',
                headers: { 'X-Filename': name },
                body: ''
            });
            
            const data = await resp.json();
            if (data.success) {
                showToast('创建成功: ' + name);
                document.getElementById('file-name').value = '';
                loadFiles(path);
            } else {
                showToast(data.error || '创建失败', true);
            }
        }
        
        // 显示提示
        function showToast(msg, isError = false) {
            const toast = document.getElementById('toast');
            toast.textContent = msg;
            toast.style.background = isError ? '#e74c3c' : '#333';
            toast.classList.add('show');
            setTimeout(() => toast.classList.remove('show'), 3000);
        }
        
        // 格式化文件大小
        function formatSize(bytes) {
            if (bytes < 1024) return bytes + ' B';
            if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
            return (bytes / 1024 / 1024).toFixed(1) + ' MB';
        }
        
        // 初始化
        getStatus();
    </script>
</body>
</html>"""


class WebDriveServer(http.server.HTTPServer):
    """Web 网盘服务器."""
    
    SESSION_TTL = 86400      # 会话有效期（秒，24h，与 Cookie Max-Age 一致）
    
    def __init__(self, server_address, root: Path, auth: AuthStore, logger,
                 ipv6_addr: str | None = None):
        super().__init__(server_address, WebDriveHandler)
        self.root = root
        self.auth = auth
        self.log = logger
        self.ipv6_addr = ipv6_addr
        self.sessions: dict[str, WebDriveSession] = {}
        self.port = server_address[1]
        # 登录失败限流（防暴力破解配对码），复用 server.py 的共享逻辑
        self._fail_limiter = IPRateLimiter()


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="PhotoTrans Web UI")
    parser.add_argument("--root", default=str(Path.home() / "PhotoTransDrive"),
                        help="网盘根目录")
    parser.add_argument("--port", type=int, default=8080,
                        help="Web 端口 (默认 8080)")
    parser.add_argument("--pair-code", default=None,
                        help="普通配对码 (只读；默认读 server.py 配置)")
    parser.add_argument("--admin-code", default=None,
                        help="管理员配对码 (完整权限；默认读 server.py 配置)")
    args = parser.parse_args()
    
    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)
    
    # 设置日志
    import logging
    import logging.handlers
    
    meta_dir = Path.home() / ".phototransdrive"
    log_dir = meta_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    
    logger = logging.getLogger("phototrans_web")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    if not logger.handlers:
        fmt = logging.Formatter("%(asctime)s.%(msecs)03d [%(levelname)s] [%(name)s] %(message)s",
                                datefmt="%Y-%m-%d %H:%M:%S")
        fh = logging.handlers.RotatingFileHandler(log_dir / "phototrans_web.log",
                                                   maxBytes=10*1024*1024, backupCount=5, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.INFO)
        ch.setFormatter(fmt)
        logger.addHandler(ch)
    
    # 认证存储（配对码与 server.py 共用同一份 config.json）
    auth = AuthStore(meta_dir / "devices.json")
    cfg = load_config(meta_dir / "config.json")
    
    # 普通配对码（只读权限）
    if args.pair_code:
        auth.set_pair_code(args.pair_code)
    elif cfg.get("pair_code"):
        auth.set_pair_code(cfg["pair_code"])
    else:
        auth.set_pair_code(f"{secrets.randbelow(1000000):06d}")
    
    # 管理员配对码（完整权限）
    if args.admin_code:
        auth.set_admin_code(args.admin_code)
    elif cfg.get("admin_code"):
        auth.set_admin_code(cfg["admin_code"])
    else:
        auth.set_admin_code(f"{secrets.randbelow(1000000):06d}")
    
    # IPv6 检测
    ipv6_addr = get_ipv6_address()
    
    logger.info("=" * 60)
    logger.info(f"Web UI 启动: {root} 端口 {args.port}")
    if ipv6_addr:
        logger.info(f"IPv6: [{ipv6_addr}]:{args.port}")
    logger.info(f"访问地址: http://localhost:{args.port}")
    logger.info("=" * 60)
    
    # 启动 Web 服务器
    server = WebDriveServer(("0.0.0.0", args.port), root, auth, logger, ipv6_addr)
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("关闭 Web 服务")
        server.server_close()


if __name__ == "__main__":
    main()
