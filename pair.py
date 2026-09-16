#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PhotoTrans 网盘 · 配对辅助（已弃用）
==================================
⚠️ 本脚本已过时，请勿使用。

它原本用于生成 PT-BIND|<account>|<pair>|<port> 旧协议二维码，但当前
server.py 已不再处理 PT-BIND 协议。

当前的配对方式：
1. 启动服务端：python server.py（或 server.py --pair-code xxx --admin-code yyy）
2. 启动面板会打印：
   - 普通配对码（只读权限：仅浏览/下载）
   - 管理员配对码（完整权限：上传/删除/改动）
3. 手机 App 输入「局域网地址」+「配对码」即可绑定。

本脚本保留仅供历史参考。
"""


def main():
    print("=" * 50)
    print("PhotoTrans 网盘配对（已弃用）")
    print("=" * 50)
    print()
    print("本脚本已过时，无需使用。")
    print()
    print("请直接启动服务端：")
    print("  python server.py")
    print()
    print("启动面板会打印普通配对码（只读）与管理员配对码（完整权限），")
    print("手机 App 输入配对码即可绑定。")
    print("=" * 50)


if __name__ == "__main__":
    main()
