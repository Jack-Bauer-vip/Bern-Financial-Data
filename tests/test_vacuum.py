# -*- coding: utf-8 -*-
"""VACUUM 瘦身脚本测试 — 阈值跳过 / 强制成功 / 缺失库 三分支

scripts_gen/ 非包（无 __init__.py），测试把其目录注入 sys.path 后导入。
脚本零依赖（仅 sqlite3/argparse/pathlib），不触发 src 模块加载。
"""

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts_gen"))
from vacuum_and_archive import vacuum_if_needed  # noqa: E402


def _make_db(path: Path) -> None:
    """建一张带数据的简单表 —— 确保 integrity_check 必然返回 ok"""
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany("INSERT INTO t (v) VALUES (?)",
                     [(f"row{i}",) for i in range(100)])
    conn.commit()
    conn.close()


def test_vacuum_skips_below_threshold():
    """低于阈值（默认 500MB）→ SKIP，不执行 VACUUM"""
    tmp = tempfile.mktemp(suffix=".db")
    _make_db(Path(tmp))
    try:
        status = vacuum_if_needed(Path(tmp), threshold_mb=500)
        assert "SKIP" in status
        assert "below threshold" in status
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def test_vacuum_force_succeeds():
    """--force 无视阈值 → VACUUM 成功返回 DONE"""
    tmp = tempfile.mktemp(suffix=".db")
    _make_db(Path(tmp))
    try:
        status = vacuum_if_needed(Path(tmp), threshold_mb=500, force=True)
        assert "DONE" in status
        assert "not found" not in status
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def test_vacuum_missing_db_skips():
    """库不存在 → SKIP not found"""
    status = vacuum_if_needed(Path("no_such_file_xyz.db"), threshold_mb=1)
    assert "SKIP" in status
    assert "not found" in status
