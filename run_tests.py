#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PhotoTrans 网盘 · 一键回归测试。

依次运行：
1. _escape_test.py      路径穿越 + 符号链接逃逸防护
2. _smoke_test.py       TCP 协议冒烟（双配对码权限 / 文件操作 / 边界）
3. _web_test.py         Web UI（权限分级 / 暴力破解锁定）
4. _concurrency_test.py 并发限制（MAX_PER_IP 防 DoS）
5. _v6_test.py          IPv6 双栈共存（V6ONLY=1）

用法：python run_tests.py
"""
import subprocess
import sys
from pathlib import Path

TESTS = [
    ("_escape_test.py", "路径穿越 + symlink 防护"),
    ("_smoke_test.py", "TCP 协议冒烟（权限/文件操作）"),
    ("_web_test.py", "Web UI（权限/锁定）"),
    ("_concurrency_test.py", "并发限制（防 DoS）"),
    ("_v6_test.py", "IPv6 双栈共存（V6ONLY=1）"),
]


def main():
    here = Path(__file__).parent
    all_ok = True
    print("=" * 60)
    print("  PhotoTrans 网盘 · 回归测试套件")
    print("=" * 60)

    for script, desc in TESTS:
        print(f"\n── {desc}（{script}）──")
        result = subprocess.run(
            [sys.executable, str(here / script)],
            cwd=str(here),
        )
        if result.returncode != 0:
            all_ok = False
            print(f"  !!! {script} 失败")

    print("\n" + "=" * 60)
    print("  套件结果:", "全部通过" if all_ok else "有失败")
    print("=" * 60)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
