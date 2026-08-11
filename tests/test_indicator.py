"""指标归一层测试 — meta_indicator 映射、统一查询、候选收集（全离线）"""

import os
import tempfile

import pandas as pd
import pytest
from sqlalchemy import create_engine, text

from src.db.repository import DataRepository


@pytest.fixture()
def repo():
    """临时库：FRED 风格 {date,value} 表 + akshare 风格 {日期,今值} 表"""
    tmp = tempfile.mktemp(suffix=".db")
    eng = create_engine(f"sqlite:///{tmp}")
    r = DataRepository(eng)
    r.create_tables()
    with eng.begin() as c:
        c.execute(text(
            'CREATE TABLE macro_fred_x (id INTEGER PRIMARY KEY AUTOINCREMENT, '
            '"date" TEXT, "value" TEXT, created_at TEXT)'))
        c.execute(text(
            'CREATE TABLE macro_ak_x (id INTEGER PRIMARY KEY AUTOINCREMENT, '
            '"日期" TEXT, "今值" TEXT, "前值" TEXT, created_at TEXT)'))
        # 插数据（SQLAlchemy 2.0 executemany 需 dict 列表）
        c.execute(
            text('INSERT INTO macro_fred_x (date, value) VALUES (:d, :v)'),
            [{"d": "2026-01-01", "v": "4.3"}, {"d": "2026-02-01", "v": "4.2"}])
        c.execute(
            text('INSERT INTO macro_ak_x ("日期", "今值") VALUES (:d, :v)'),
            [{"d": "2026-01-01", "v": "4.4"}, {"d": "2026-02-01", "v": "4.1"}])
    yield r
    eng.dispose()
    if os.path.exists(tmp):
        os.remove(tmp)


def test_set_and_get_indicator_fred(repo):
    """set_indicator 解析列映射 → get_indicator 返回首选表 {date,value}"""
    rec = repo.set_indicator("us.x", "macro_fred_x")
    assert rec is not None
    assert rec["preferred_table"] == "macro_fred_x"
    assert rec["date_column"] == "date"
    assert rec["value_column"] == "value"

    df = repo.get_indicator("us.x")
    assert list(df.columns) == ["date", "value"]
    # 倒序（最新在前）；值为 TEXT 列读出为字符串
    assert df.iloc[0]["date"] == "2026-02-01"
    assert df.iloc[0]["value"] == "4.2"


def test_set_indicator_switch_preferred(repo):
    """切换首选源到 akshare 表 → get_indicator 返回 akshare 数据且列为 date/value"""
    repo.set_indicator("us.x", "macro_fred_x")
    rec = repo.set_indicator("us.x", "macro_ak_x")
    assert rec["preferred_table"] == "macro_ak_x"
    assert rec["date_column"] == "日期"
    assert rec["value_column"] == "今值"

    df = repo.get_indicator("us.x")
    assert list(df.columns) == ["date", "value"]
    # akshare 数据（TEXT 列）
    assert df.iloc[0]["value"] == "4.1"


def test_get_indicator_no_mapping(repo):
    """无映射 → 空 DataFrame（仍两列）"""
    df = repo.get_indicator("us.nonexistent")
    assert df.empty
    assert list(df.columns) == ["date", "value"]


def test_get_indicator_date_range(repo):
    """支持日期区间过滤"""
    repo.set_indicator("us.x", "macro_fred_x")
    df = repo.get_indicator("us.x", start_date="2026-02-01", end_date="2026-02-28")
    assert len(df) == 1
    assert df.iloc[0]["date"] == "2026-02-01"


def test_list_indicators_roundtrip(repo):
    """list_indicators 往返"""
    assert repo.list_indicators() == []
    repo.set_indicator("us.x", "macro_fred_x")
    items = repo.list_indicators()
    assert len(items) == 1
    assert items[0]["indicator_key"] == "us.x"
    assert items[0]["preferred_table"] == "macro_fred_x"


def test_set_indicator_invalid_table(repo):
    """表不存在 → None"""
    assert repo.set_indicator("us.x", "no_such_table") is None


def test_set_indicator_preconfig_unsynced_declared(repo):
    """目录中声明但未同步的表 → 允许预配置（列留空），get_indicator 优雅返回空"""
    # macro_fred_unemployment 在目录中声明但临时库未建表
    rec = repo.set_indicator("us.unemployment", "macro_fred_unemployment")
    assert rec is not None
    assert rec["preferred_table"] == "macro_fred_unemployment"
    assert rec["date_column"] == ""
    assert rec["value_column"] == ""
    # 未同步 → get_indicator 空（不崩）
    df = repo.get_indicator("us.unemployment")
    assert df.empty
    assert list(df.columns) == ["date", "value"]


def test_set_indicator_rejects_deprecated_table(repo):
    """deprecated(停更)源不得设为获信源——已定案口径

    即使表存在于库中(akshare 美国宏观 deprecated 表已入库)，set_indicator
    也必须拒绝，防止 GUI/API/auto_adopt 任何入口把获信源切到停更源。
    """
    # macro_usa_core_cpi_monthly 在真实目录中标 deprecated
    assert repo.set_indicator("us.core_cpi", "macro_usa_core_cpi_monthly",
                              unit_type="mom", unit_desc="环比") is None
    # 拒绝后不污染映射
    assert repo.get_indicator_map("us.core_cpi") is None
    # active 的 FRED 表不受影响
    rec = repo.set_indicator("us.core_cpi", "macro_fred_core_cpi")
    assert rec is not None and rec["preferred_table"] == "macro_fred_core_cpi"


def test_meta_indicator_excluded_from_data_tables(repo):
    """meta_indicator 是元数据表，collect_tables 不应暴露为数据表"""
    from src.importer.matcher import collect_tables
    repo.set_indicator("us.x", "macro_fred_x")
    tables = collect_tables(repo)
    names = {t.table_name for t in tables}
    assert "macro_fred_x" in names
    assert "macro_ak_x" in names
    assert "meta_indicator" not in names


def test_indicator_candidates(repo):
    """indicator_candidates 从目录收集同一指标的所有源（deprecated 源排除）

    akshare 美国宏观兜底已标 deprecated（上游停更），不作为获信源候选，
    只剩 FRED 官方源。
    """
    cands = repo.indicator_candidates("us.unemployment")
    tables = {c["table_name"] for c in cands}
    assert tables == {"macro_fred_unemployment"}
    # 无 indicator 键的指标 → 空
    assert repo.indicator_candidates("us.ism") == []


def test_catalog_fred_rollout_pairs():
    """FRED 铺开：us.* 20 个指标 akshare+FRED 成对 + cn.* 4 个官方源，FRED 源共 20 个"""
    from src.core.fetcher_registry import FetcherRegistry
    from src.utils.config import ConfigManager
    from collections import defaultdict
    reg = FetcherRegistry(ConfigManager())
    groups = defaultdict(list)
    for s in reg.get_all_sources():
        if s.get("indicator"):
            groups[s["indicator"]].append(s["api_source"])
    # us.* 20 个指标全部 akshare+fred 成对
    us_groups = {k: v for k, v in groups.items() if k.startswith("us.")}
    assert len(us_groups) == 20
    assert all(len(v) == 2 and set(v) == {"akshare", "fred"}
               for v in us_groups.values())
    # cn.* 4 个官方活源，仅 akshare（无 FRED 中国源，存官方同比列 unit_type=yoy）
    cn_groups = {k: v for k, v in groups.items() if k.startswith("cn.")}
    assert set(cn_groups) == {"cn.cpi", "cn.gdp", "cn.ppi", "cn.m2"}
    assert all(set(v) == {"akshare"} for v in cn_groups.values())
    # FRED 源总数 20（5 原 + 15 新）
    fred_count = sum(1 for s in reg.get_all_sources() if s.get("api_source") == "fred")
    assert fred_count == 20
    # cn.* 源在目录中声明 unit_type=yoy（存储值即官方同比）
    cn_sources = {s["indicator"]: s for s in reg.get_all_sources()
                  if s.get("indicator", "").startswith("cn.")}
    assert all(cn_sources[k].get("unit_type") == "yoy" for k in cn_groups)


def test_resolve_value_column_prioritizes_yoy():
    """中国官方宏观表数值列解析：'全国-同比增长' 优先于其他含 '增长' 的列"""
    import tempfile
    from sqlalchemy import create_engine, text
    tmp = tempfile.mktemp(suffix=".db")
    eng = create_engine(f"sqlite:///{tmp}")
    r = DataRepository(eng)
    r.create_tables()
    with eng.begin() as c:
        c.execute(text(
            'CREATE TABLE macro_cn_cpi (id INTEGER PRIMARY KEY AUTOINCREMENT, '
            '"月份" TEXT, "全国-当月" TEXT, "全国-同比增长" TEXT, '
            '"全国-环比增长" TEXT, "全国-累计" TEXT, created_at TEXT)'))
    try:
        # strict=True：只认明确数值列名，且「同比增长」命中
        assert r._resolve_value_column("macro_cn_cpi", strict=True) == "全国-同比增长"
    finally:
        eng.dispose()
        if os.path.exists(tmp):
            os.remove(tmp)


def test_find_date_column_recognizes_quarter():
    """GDP 表的「季度」中文日期列可被识别为日期列"""
    import tempfile
    from sqlalchemy import create_engine, text
    tmp = tempfile.mktemp(suffix=".db")
    eng = create_engine(f"sqlite:///{tmp}")
    r = DataRepository(eng)
    r.create_tables()
    with eng.begin() as c:
        c.execute(text(
            'CREATE TABLE macro_cn_gdp (id INTEGER PRIMARY KEY AUTOINCREMENT, '
            '"季度" TEXT, "国内生产总值-同比增长" TEXT, created_at TEXT)'))
    try:
        assert r._find_date_column("macro_cn_gdp") == "季度"
    finally:
        eng.dispose()
        if os.path.exists(tmp):
            os.remove(tmp)


def test_get_indicator_chinese_month_table(repo):
    """get_indicator 读取中文「月份」表：日期归一化后 level 可解析，最新值正确

    回归：此前读路径不归一化中文日期 → 下游 to_datetime 全 NaT → cn.* 返回空。
    """
    with repo.engine.begin() as c:
        c.execute(text(
            'CREATE TABLE macro_cn_cpi (id INTEGER PRIMARY KEY AUTOINCREMENT, '
            '"月份" TEXT, "全国-同比增长" TEXT, created_at TEXT)'))
        c.execute(text(
            "INSERT INTO macro_cn_cpi (月份, \"全国-同比增长\") VALUES "
            "('2026年05月份','0.3'),('2026年06月份','1.0')"))
    repo.set_indicator("cn.cpi", "macro_cn_cpi")
    df = repo.get_indicator("cn.cpi")
    assert list(df.columns) == ["date", "value"]
    # 日期已归一化为 pd.to_datetime 可解析的 ISO 前缀（最新在前）
    assert df.iloc[0]["date"] == "2026-06"
    assert str(df.iloc[0]["value"]) == "1.0"
    # compute_transform("level") 能正常解析（日期已是 Timestamp 兼容格式）；
    # 内部升序，末行即最新
    from src.core.transform import compute_transform
    out = compute_transform(df, "level")
    assert not out.empty
    assert out.iloc[-1]["date"] == pd.Timestamp("2026-06-01")
    assert out.iloc[-1]["value"] == 1.0


# ---------------------------------------------------------------------------
# 集成：get_indicator_map / auto_adopt / /macro/cpi 走统一接口
# ---------------------------------------------------------------------------


def test_get_indicator_map_roundtrip(repo):
    """get_indicator_map 往返"""
    assert repo.get_indicator_map("us.x") is None
    repo.set_indicator("us.x", "macro_fred_x")
    m = repo.get_indicator_map("us.x")
    assert m["preferred_table"] == "macro_fred_x"
    assert m["date_column"] == "date"
    assert m["value_column"] == "value"


def test_auto_adopt_first_source(repo):
    """auto_adopt：未设获信源 + 数值列明确（今值）→ 自动采用"""
    rec = repo.auto_adopt_indicator("us.x", "macro_ak_x")
    assert rec is not None
    assert rec["preferred_table"] == "macro_ak_x"
    assert rec["value_column"] == "今值"
    assert repo.get_indicator_map("us.x")["preferred_table"] == "macro_ak_x"


def test_auto_adopt_not_override_existing(repo):
    """auto_adopt 不覆盖已手动的获信源"""
    repo.set_indicator("us.x", "macro_fred_x")
    assert repo.auto_adopt_indicator("us.x", "macro_ak_x") is None
    assert repo.get_indicator_map("us.x")["preferred_table"] == "macro_fred_x"


def test_auto_adopt_ohlc_table_skipped(repo):
    """auto_adopt 对 OHLC 表（无 今值/现值/value 列）不自动采用"""
    with repo.engine.begin() as c:
        c.execute(text(
            'CREATE TABLE macro_ohlc (id INTEGER PRIMARY KEY AUTOINCREMENT, '
            '"date" TEXT, "open" TEXT, "close" TEXT, created_at TEXT)'))
        c.execute(text(
            "INSERT INTO macro_ohlc (date, open, close) VALUES "
            "('2026-01-01','1.0','1.1')"))
    assert repo.auto_adopt_indicator("us.ohlc", "macro_ohlc") is None
    assert repo.get_indicator_map("us.ohlc") is None


def test_macro_cpi_uses_trusted_indicator(repo):
    """/macro/cpi：us.cpi 获信源设为 FRED 表 → 端点返回 FRED 数据"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.api import routes

    # 建 FRED 风格 CPI 表并设为 us.cpi 获信源
    with repo.engine.begin() as c:
        c.execute(text(
            'CREATE TABLE macro_fred_cpi (id INTEGER PRIMARY KEY AUTOINCREMENT, '
            '"date" TEXT, "value" TEXT, created_at TEXT)'))
        c.execute(text(
            "INSERT INTO macro_fred_cpi (date, value) VALUES "
            "('2026-01-01','310.0'),('2026-02-01','312.0')"))
    repo.set_indicator("us.cpi", "macro_fred_cpi")

    routes.set_repo(repo)
    app = FastAPI()
    app.include_router(routes.router)
    client = TestClient(app)

    resp = client.get("/macro/cpi")
    assert resp.status_code == 200
    data = resp.json()["data"]
    # 获信源 FRED 数据应出现在结果里
    assert any(
        isinstance(r, dict) and r.get("date") == "2026-01-01"
        and str(r.get("value")) == "310.0"
        for r in data
    )
