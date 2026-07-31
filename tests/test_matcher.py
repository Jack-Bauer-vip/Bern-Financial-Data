"""目标表识别 / 列名映射 测试（纯离线，不依赖 AI）"""

import pandas as pd

from src.importer.matcher import RuleMatcher, TableMeta, normalize_col, sample_dtype
from src.importer.column_mapper import suggest_mapping, detect_new_columns


# ---------------------------------------------------------------------------
# 归一化 / 类型推断
# ---------------------------------------------------------------------------


def test_normalize_col():
    assert normalize_col(" 日期 ") == "日期"
    assert normalize_col("Date") == "date"
    assert normalize_col("trade_date") == "tradedate"
    assert normalize_col("	开盘  ") == "开盘"


def test_sample_dtype_date_numeric_text():
    df = pd.DataFrame({"a": ["2026-07-01", "2026-07-02"]})
    assert sample_dtype(df, "a") == "date"
    df2 = pd.DataFrame({"b": [1.5, 2.5]})
    assert sample_dtype(df2, "b") == "numeric"
    df3 = pd.DataFrame({"c": ["x", "y", "z"]})
    assert sample_dtype(df3, "c") == "text"


# ---------------------------------------------------------------------------
# 规则匹配
# ---------------------------------------------------------------------------


def _tables():
    return [
        TableMeta("macro_usa_cpi_yoy",
                  ["id", "时间", "发布日期", "现值", "前值", "created_at"], 222),
        TableMeta("macro_usa_core_cpi_monthly",
                  ["id", "商品", "日期", "今值", "预测值", "前值", "created_at"], 669),
        TableMeta("macro_china_cpi_yearly",
                  ["id", "商品", "日期", "今值", "预测值", "前值", "created_at"], 477),
        TableMeta("index_daily",
                  ["id", "date", "open", "high", "low", "close", "volume", "created_at"], 3423),
        TableMeta("macro_china_money_supply",
                  ["id", "月份", "货币和准货币(M2)-数量(亿元)", "货币(M1)-数量(亿元)", "created_at"], 222),
    ]


def test_match_english_quote():
    """英文行情列唯一匹配 index_daily"""
    m = RuleMatcher(_tables())
    df = pd.DataFrame({"date": ["2026-01-01"], "open": ["1"], "high": ["2"],
                       "low": ["0.5"], "close": ["1.5"], "volume": ["100"]})
    res, scored = m.best(df)
    assert res is not None
    assert res.table_name == "index_daily"
    assert res.method == "rules"
    assert m._ambiguity is False


def test_match_us_cpi():
    """美国 CPI 列（时间/发布日期/现值/前值）匹配 macro_usa_cpi_yoy"""
    m = RuleMatcher(_tables())
    df = pd.DataFrame({"时间": ["2026-07-01"], "发布日期": ["2026-08-12"],
                       "现值": ["3.5"], "前值": ["3.4"]})
    res, _ = m.best(df)
    assert res is not None
    assert res.table_name == "macro_usa_cpi_yoy"


def test_match_cn_macro_ambiguous():
    """中文宏观标准列（商品/日期/今值/预测值/前值）→ 多张同结构表并列 → 触发 AI 兜底"""
    m = RuleMatcher(_tables())
    df = pd.DataFrame({"商品": ["中国CPI"], "日期": ["2026-07-01"],
                       "今值": ["3.5"], "预测值": ["3.4"], "前值": ["3.6"]})
    res, scored = m.best(df)
    # 多张同结构表 → 歧义 → 返回 None 交给 AI
    assert res is None
    assert m._ambiguity is True
    # 但仍有候选可供 AI 不可用时降级
    assert scored and scored[0][1] >= 0.7


def test_match_no_tables():
    """无候选表 → 返回 None 无歧义"""
    m = RuleMatcher([])
    df = pd.DataFrame({"a": [1]})
    res, scored = m.best(df)
    assert res is None
    assert scored == []


# ---------------------------------------------------------------------------
# 列名映射 / 新字段
# ---------------------------------------------------------------------------


def test_suggest_mapping_exact():
    file1 = ["商品", "日期", "今值", "预测值", "前值"]
    table1 = ["id", "商品", "日期", "今值", "预测值", "前值", "created_at"]
    m = suggest_mapping(file1, table1)
    assert m == {c: c for c in file1}


def test_suggest_mapping_aliases():
    """文件「日期/开盘/收盘/涨跌幅」→ 表「date/open/close/pct_chg」"""
    file3 = ["日期", "开盘", "收盘", "涨跌幅"]
    table3 = ["id", "date", "open", "close", "pct_chg", "created_at"]
    m = suggest_mapping(file3, table3)
    assert m["日期"] == "date"
    assert m["开盘"] == "open"
    assert m["收盘"] == "close"
    assert m["涨跌幅"] == "pct_chg"


def test_suggest_mapping_us_cpi():
    file4 = ["时间", "发布日期", "现值", "前值"]
    table4 = ["id", "时间", "发布日期", "现值", "前值", "created_at"]
    m = suggest_mapping(file4, table4)
    assert m["时间"] == "时间"
    assert m["现值"] == "现值"


def test_detect_new_columns():
    """文件有「备注」列 → 检测为新字段"""
    file5 = ["时间", "数值", "备注"]
    table5 = ["id", "时间", "数值", "created_at"]
    m = suggest_mapping(file5, table5)
    new = detect_new_columns(file5, table5, m)
    assert "备注" in new
    assert "时间" not in new
