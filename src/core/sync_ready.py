"""同步就绪标记 — D 每日更新完成后写 data/sync_ready.json，供 A/B 校验新鲜度

参考 B→A 流水线 `.ready` + `data_asof` 模式；D 侧 A/B 走 HTTP 8765 程序化取数，
就绪信息（data_asof）同时暴露到 `/api/v1/health`（见 routes.py），A/B 读前调
一次 /health 即可校验新鲜度——「宁简勿繁」只走 health 一个通道，本地文件作
为持久化就绪标记（含 data_asof / generated_at / tables）。

安全边界：文件写在 data/ 下（运行时产物），不涉及密钥。
"""

import json
from datetime import datetime
from pathlib import Path

from src.utils.logger import logger

# A/B 消费的核心行情表 —— data_asof 的兜底基准（无就绪标记时 /health 用它算）
CORE_READY_TABLES = ["fund_etf_daily", "index_daily"]

SYNC_READY_FILENAME = "sync_ready.json"


def sync_ready_path(data_dir: str | Path | None = None) -> Path:
    """就绪标记文件路径（默认 D 项目 data/ 目录）"""
    if data_dir is not None:
        return Path(data_dir) / SYNC_READY_FILENAME
    from src.utils.config import ConfigManager
    root = ConfigManager().root_dir
    return root / "data" / SYNC_READY_FILENAME


def compute_data_asof(repo, tables: list[str] | None = None) -> str | None:
    """给定表集合的最新交易日（跨表取 max），返回 ISO 字符串；表空/失败返回 None"""
    tables = tables or CORE_READY_TABLES
    last_dates = []
    for t in tables:
        try:
            d = repo.get_last_date(t, "date")
            if d:
                last_dates.append(d)
        except Exception:
            continue
    if not last_dates:
        return None
    return max(last_dates).isoformat()


def write_sync_ready(
    repo,
    tables: list[str] | None = None,
    data_dir: str | Path | None = None,
) -> Path | None:
    """写就绪标记 data/sync_ready.json（含 data_asof / generated_at / tables）

    任一同步完成后调用即可；data_asof 取涉及表的最新交易日。
    失败不抛异常（就绪标记是增强信息，不应阻断同步主流程）。

    Args:
        repo: DataRepository
        tables: 本次同步涉及的表名列表；None → CORE_READY_TABLES
        data_dir: 覆盖 data 目录（测试用）

    Returns:
        写成功返回 Path，失败返回 None
    """
    tables = list(tables or CORE_READY_TABLES)
    try:
        marker = {
            "data_asof": compute_data_asof(repo, tables),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "tables": tables,
        }
        path = sync_ready_path(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(marker, f, ensure_ascii=False, indent=2)
        tmp.replace(path)
        logger.debug("已写同步就绪标记: %s (data_asof=%s)", path, marker["data_asof"])
        return path
    except Exception as exc:
        logger.warning("写同步就绪标记失败: %s", exc)
        return None


def load_sync_ready(data_dir: str | Path | None = None) -> dict | None:
    """读取就绪标记文件；不存在或损坏返回 None"""
    try:
        path = sync_ready_path(data_dir)
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None
