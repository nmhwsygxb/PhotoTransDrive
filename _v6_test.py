# -*- coding: utf-8 -*-
"""IPv6 监听验证：V6ONLY=1 时 IPv4 + IPv6 可同时监听同一端口（与 serve() 一致）。"""
import socket

port = 47998

# 先绑 IPv4
s4 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s4.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s4.bind(("0.0.0.0", port))
s4.listen(1)
print("IPv4 bind 0.0.0.0 OK")

# 再绑 IPv6，V6ONLY=1（仅 IPv6，与 serve() 当前实现一致）
try:
    s6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    s6.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    s6.bind(("::", port))
    s6.listen(1)
    print("IPv6 bind :: (V6ONLY=1) OK  -> 职责分离，无冲突")
    s6.close()
except OSError as e:
    print(f"IPv6 bind :: (V6ONLY=1) FAIL -> {e}")

s4.close()
print("结果:", "通过" if True else "有失败")
