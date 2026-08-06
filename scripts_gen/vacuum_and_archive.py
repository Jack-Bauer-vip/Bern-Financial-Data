# -*- coding: utf-8 -*-
"""SQLite 数据库瘦身脚本 — VACUUM + WAL checkpoint（异常膨胀时手动）

用法：
  python scripts_gen/vacuum_and_archive.py                 # 默认阈值 500MB，低于则 SKIP
  python scripts_gen/vacuum_and_archive.py --threshold-mb 400
  python scripts_gen/vacuum_and_archive.py --force          # 无视阈值强制瘦身

安全阀（双保险）：
  - 低于阈值不跑，避免频繁空跑浪费 IO（当前生产库 435MB < 500 默认即 SKIP）
  - 先 PRAGMA integrity_check，非 ok 中止，防 VACUUM 损坏已损库
  - 先 wal_checkpoint(TRUNCATE) 截断 WAL 日志（长任务批量同步 3M 行后的日常维护）

设计说明：SQLite WAL 模式空闲页会被自动回收，VACUUM 主要压缩表碎片而非"瘦身"，
本脚本定位是「文件异常膨胀时手动执行」，不做 GUI 开关、不自动挂调度。
若需定时，可用系统调度器每周日跑一次（见 docs/DATA_GOVERNANCE.md §7）。
"""

import argparse
import sqlite3
from pathlib import Path

# 生产库定位：脚本在 scripts_gen/ 下，项目根在其父目录
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "berndata.db"


def _db_size_mb(path: Path) -> float:
    """数据库文件大小（MB）"""
    return path.stat().st_size / (1024 * 1024)


def vacuum_if_needed(db_path: Path, threshold_mb: int = 500,
                     force: bool = False) -> str:
    """按安全阀执行瘦身，返回状态文本（便于 CLI 输出与测试断言）

    - 文件不存在 → SKIP
    - 低于阈值且非 force → SKIP
    - integrity_check 非 ok → ERROR 中止
    - 否则 wal_checkpoint(TRUNCATE) + VACUUM → DONE
    """
    if not db_path.exists():
        print(f"[SKIP] {db_path} 不存在")
        return "[SKIP] not found"

    size = _db_size_mb(db_path)
    print(f"[INFO] 当前 DB 大小: {size:.2f} MB")

    if not force and size < threshold_mb:
        print(f"[SKIP] 低于阈值 ({threshold_mb} MB)，无需 VACUUM")
        return "[SKIP] below threshold"

    conn = sqlite3.connect(str(db_path))
    try:
        # 1. 完整性检查（双保险）：防止对已损坏库执行 VACUUM
        row = conn.execute("PRAGMA integrity_check").fetchone()
        if not row or row[0] != "ok":
            print("[ERROR] 数据库完整性检查失败！中止 VACUUM。")
            return "[ERROR] integrity check failed"

        # 2. 先截断 WAL 日志（WAL 模式 checkpoint；失败不阻塞后续 VACUUM）
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError:
            pass

        # 3. VACUUM 压缩表碎片
        print("[VACUUM] 开始（可能耗时数秒）...")
        conn.execute("VACUUM")
    finally:
        conn.close()

    new_size = _db_size_mb(db_path)
    print(f"[DONE] 瘦身完成: {new_size:.2f} MB（释放 {size - new_size:.2f} MB）")
    return f"[DONE] {new_size:.2f}"


def main() -> None:
    parser = argparse.ArgumentParser(description="SQLite 数据库瘦身（VACUUM + WAL checkpoint）")
    parser.add_argument("--threshold-mb", type=int, default=500,
                        help="仅当 DB 超过此阈值(MB)时执行 VACUUM，默认 500")
    parser.add_argument("--force", action="store_true",
                        help="无视阈值强制执行")
    args = parser.parse_args()
    vacuum_if_needed(DB_PATH, threshold_mb=args.threshold_mb, force=args.force)


if __name__ == "__main__":
    main()
