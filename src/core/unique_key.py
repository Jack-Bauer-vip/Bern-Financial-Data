"""唯一键推断 — 共享逻辑（SyncEngine 与导入功能共用）"""


def infer_unique_cols(source_cfg: dict | None = None) -> list[str]:
    """根据数据源配置启发式推断 upsert 唯一键

    优先使用配置中显式指定的 unique_columns，否则按表名约定推断：
    - stock / index → ["symbol", "date"]
    - fund → ["code", "date"]
    - 其余（宏观）→ incremental_field（默认 date）
    """
    source_cfg = source_cfg or {}
    cfg_cols = source_cfg.get("unique_columns")
    if cfg_cols:
        return cfg_cols if isinstance(cfg_cols, list) else [cfg_cols]

    table_name = (source_cfg.get("table_name") or "").lower()
    if any(kw in table_name for kw in ("stock", "个股", "index")):
        return ["symbol", "date"]
    if "fund" in table_name or "基金" in table_name:
        return ["code", "date"]

    return [source_cfg.get("incremental_field", "date")]


def infer_unique_cols_by_table(table_name: str) -> list[str]:
    """按表名直接推断唯一键（无 source_cfg 时用）"""
    return infer_unique_cols({"table_name": table_name})
