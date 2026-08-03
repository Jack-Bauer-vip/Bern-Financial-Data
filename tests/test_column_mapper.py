"""列名映射组合入口（规则 + AI 兜底 + 新字段）测试"""

import pandas as pd

from src.importer.column_mapper import (
    map_columns, suggest_mapping, detect_new_columns, ColumnMappingResult,
)


def test_map_columns_rules_exact():
    result = map_columns(["商品", "日期", "今值"],
                         ["id", "商品", "日期", "今值", "created_at"])
    assert result.mapping["商品"] == "商品"
    assert result.mapping["日期"] == "日期"
    assert result.new_columns == []


def test_map_columns_rules_alias():
    result = map_columns(["日期", "开盘", "收盘"],
                         ["id", "date", "open", "close", "created_at"])
    assert result.mapping["日期"] == "date"
    assert result.mapping["开盘"] == "open"
    assert result.mapping["收盘"] == "close"
    assert result.new_columns == []


def test_map_columns_detects_new_field():
    result = map_columns(["时间", "数值", "备注"],
                         ["id", "时间", "数值", "created_at"])
    assert result.mapping["时间"] == "时间"
    assert result.mapping["数值"] == "数值"
    assert result.new_columns == ["备注"]


def test_map_columns_ai_fallback():
    """规则映射不上的列 → AI 兜底"""
    class _FakeAI:
        def is_available(self):
            return True

        def map_columns(self, file_cols, table_cols):
            return {"利率": "rate"}

    result = map_columns(["时间", "利率"],
                         ["id", "时间", "rate", "created_at"],
                         ai_client=_FakeAI())
    assert result.mapping["利率"] == "rate"
    assert result.low_confidence is True


def test_map_columns_ai_rejected_when_table_col_missing():
    """AI 返回的表列不存在 → 忽略，不进入 mapping"""
    class _FakeAI:
        def is_available(self):
            return True

        def map_columns(self, file_cols, table_cols):
            return {"利率": "nonexistent_col"}

    result = map_columns(["时间", "利率"],
                         ["id", "时间", "created_at"],
                         ai_client=_FakeAI())
    assert "利率" not in result.mapping
    assert "利率" in result.new_columns


def test_map_columns_no_ai():
    """无 AI 时规则映射 + 新字段"""
    result = map_columns(["日期", "额外列"],
                         ["id", "date", "created_at"])
    assert result.mapping["日期"] == "date"
    assert result.new_columns == ["额外列"]
    assert result.low_confidence is False


def test_apply_renames_and_skips():
    """apply 重命名列 + 丢弃 skipped"""
    result = ColumnMappingResult(
        mapping={"日期": "date", "今值": "value"},
        new_columns=["备注"])
    result.skipped = ["备注"]
    df = pd.DataFrame({"日期": ["2026-01-01"], "今值": ["3.5"], "备注": ["x"]})
    out = result.apply(df)
    assert list(out.columns) == ["date", "value"]
    assert out.iloc[0]["date"] == "2026-01-01"


def test_suggest_mapping_direct():
    m = suggest_mapping(["日期"], ["时间", "date"])
    # 表有两个日期类列 → 模糊不匹配，规则给出 date 精确命中前走别名
    assert "日期" in m  # 至少映射到一个


def test_map_columns_cached_same_header():
    """同列名文件只映射一次：第二次命中会话缓存，不重复调 AI"""
    from src.importer.column_mapper import clear_mapping_cache

    class _CountingAI:
        def __init__(self):
            self.calls = 0

        def is_available(self):
            return True

        def map_columns(self, file_cols, table_cols):
            self.calls += 1
            return {"利率": "rate"}

    ai = _CountingAI()
    clear_mapping_cache()  # 排除其他用例的缓存残留
    cols = ["时间", "利率"]
    table_cols = ["id", "时间", "rate", "created_at"]

    r1 = map_columns(cols, table_cols, ai_client=ai)
    r2 = map_columns(cols, table_cols, ai_client=ai)

    assert ai.calls == 1                      # 只调一次 AI
    assert r1.mapping["利率"] == "rate"
    assert r2.mapping["利率"] == r1.mapping["利率"]
