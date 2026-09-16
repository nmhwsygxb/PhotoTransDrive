# -*- coding: utf-8 -*-
"""safe_resolve 路径穿越防护验证。"""
import tempfile
from pathlib import Path
from server import safe_resolve

base = Path(tempfile.mkdtemp(prefix="pt_escape_"))
root = base / "data"
root.mkdir()
(root / "sub").mkdir()
# 前缀陷阱目录 data2（在 data 的父目录下）
prefix_trap = base / "data2"
prefix_trap.mkdir()
(prefix_trap / "secret.txt").write_text("secret", encoding="utf-8")
# 兄弟目录
sibling = base / "other"
sibling.mkdir()

tests = [
    ("/", True),
    ("/sub", True),
    ("sub", True),
    ("../data2", False),          # 前缀陷阱：应拒绝
    ("../data2/secret.txt", False),
    ("../other", False),          # 兄弟目录：应拒绝
    ("..", False),
    ("../../etc", False),
    ("/sub/../sub", True),        # 内部往返：应允许
]
all_ok = True
for raw, expect_ok in tests:
    r = safe_resolve(root, raw)
    is_ok = r is not None
    status = "PASS" if is_ok == expect_ok else "FAIL"
    if is_ok != expect_ok:
        all_ok = False
    print(f"  [{status}] safe_resolve({raw!r}) -> {r}")

# 符号链接逃逸：root 内 symlink 指向 root 外目录，应被 is_symlink 检查拒绝
try:
    outside = base / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret2", encoding="utf-8")
    evil_link = root / "evil"
    evil_link.symlink_to(outside, target_is_directory=True)
    r = safe_resolve(root, "evil/secret.txt")
    is_ok = r is None
    status = "PASS" if is_ok else "FAIL"
    if not is_ok:
        all_ok = False
    print(f"  [{status}] safe_resolve('evil/secret.txt') via symlink -> {r}")
except (OSError, NotImplementedError):
    print("  [SKIP] 符号链接逃逸测试（当前系统无创建 symlink 权限）")

print("结果:", "全部通过" if all_ok else "有失败")
