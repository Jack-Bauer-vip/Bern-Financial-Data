"""主题看板测试 — BoardStore 读写/校验 + BoardService 快照/时序 + API 端点

数据源复用 test_indicator 的临时库模式(全离线,不碰 data/berndata.db)。
"""

import os
import tempfile
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine, text

from src.core.boards import BoardService, BoardStore, board_sync_targets
from src.db.repository import DataRepository


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def repo():
    """临时库:两张月度 {date,value} 表(各整两年, 24 行)

    CPI:   2025 全年 100.0 / 2026 全年 105.0 → yoy=5%、mom=0%
    unemployment: 2025 全年 4.5 / 2026 全年 4.2 → yoy=-6.67%、mom=0%
    干净数值便于断言快照/派生的 yoy/mom。
    """
    tmp = tempfile.mktemp(suffix=".db")
    eng = create_engine(f"sqlite:///{tmp}")
    r = DataRepository(eng)
    r.create_tables()
    with eng.begin() as c:
        c.execute(text(
            'CREATE TABLE macro_fred_cpi (id INTEGER PRIMARY KEY AUTOINCREMENT, '
            '"date" TEXT, "value" TEXT, created_at TEXT)'))
        c.execute(text(
            'CREATE TABLE macro_fred_unemp (id INTEGER PRIMARY KEY AUTOINCREMENT, '
            '"date" TEXT, "value" TEXT, created_at TEXT)'))
        cpi_rows = (
            [{"d": f"2025-{m:02d}-01", "v": "100.0"} for m in range(1, 13)] +
            [{"d": f"2026-{m:02d}-01", "v": "105.0"} for m in range(1, 13)])
        unemp_rows = (
            [{"d": f"2025-{m:02d}-01", "v": "4.5"} for m in range(1, 13)] +
            [{"d": f"2026-{m:02d}-01", "v": "4.2"} for m in range(1, 13)])
        c.execute(text('INSERT INTO macro_fred_cpi (date, value) VALUES (:d, :v)'),
                  cpi_rows)
        c.execute(
            text('INSERT INTO macro_fred_unemp (date, value) VALUES (:d, :v)'),
            unemp_rows)
    r.set_indicator("us.cpi", "macro_fred_cpi", unit_type="level",
                    unit_desc="CPI 指数")
    r.set_indicator("us.unemployment", "macro_fred_unemp", unit_type="level",
                    unit_desc="失业率%")
    yield r
    eng.dispose()
    if os.path.exists(tmp):
        os.remove(tmp)


@pytest.fixture()
def themes_path():
    """临时 themes.yaml 路径(测读写,不碰 config/ 真实文件)"""
    with tempfile.TemporaryDirectory() as d:
        yield Path(d) / "themes.yaml"


SAMPLE_BOARD = {
    "key": "daily",
    "name": "每日观察",
    "description": "核心宏观",
    "date_start": None,
    "date_end": None,
    "items": [
        {"indicator": "us.cpi", "name": "美国CPI", "transform": "yoy"},
        {"indicator": "us.unemployment", "name": "失业率", "transform": "level"},
    ],
}


# ---------------------------------------------------------------------------
# BoardStore 读写/校验
# ---------------------------------------------------------------------------


def test_store_empty_when_file_missing(themes_path):
    store = BoardStore(themes_path)
    assert store.load() == {"boards": []}
    assert store.list_boards() == []


def test_store_add_and_load(themes_path):
    store = BoardStore(themes_path)
    assert store.add_board(SAMPLE_BOARD) is True
    assert store.get_board("daily") == SAMPLE_BOARD
    boards = store.load()["boards"]
    assert len(boards) == 1
    assert boards[0]["key"] == "daily"


def test_store_duplicate_key_rejected(themes_path):
    store = BoardStore(themes_path)
    assert store.add_board(SAMPLE_BOARD) is True
    assert store.add_board(SAMPLE_BOARD) is False


def test_store_update_and_delete(themes_path):
    store = BoardStore(themes_path)
    store.add_board(SAMPLE_BOARD)
    changed = dict(SAMPLE_BOARD, name="改名")
    assert store.update_board("daily", changed) is True
    assert store.get_board("daily")["name"] == "改名"
    assert store.delete_board("daily") is True
    assert store.get_board("daily") is None
    assert store.delete_board("daily") is False


def test_validate_ok():
    assert BoardStore.validate_board(SAMPLE_BOARD, all_boards=[SAMPLE_BOARD],
                                     known_indicators={"us.cpi", "us.unemployment"}) == []


def test_validate_bad_key_and_missing_name():
    bad = dict(SAMPLE_BOARD, key="Has Upper", name="")
    errors = BoardStore.validate_board(bad, all_boards=[bad],
                                       known_indicators={"us.cpi"})
    assert any("key" in e for e in errors)
    assert any("名称" in e for e in errors)


def test_validate_unknown_indicator():
    bad = dict(SAMPLE_BOARD, items=[{"indicator": "not.exist"}])
    errors = BoardStore.validate_board(bad, all_boards=[bad],
                                       known_indicators={"us.cpi"})
    assert any("not.exist" in e for e in errors)


def test_validate_bad_transform():
    bad = dict(SAMPLE_BOARD, items=[{"indicator": "us.cpi", "transform": "foo"}])
    errors = BoardStore.validate_board(bad, all_boards=[bad],
                                       known_indicators={"us.cpi"})
    assert any("transform" in e for e in errors)


def test_validate_empty_items():
    bad = dict(SAMPLE_BOARD, items=[])
    errors = BoardStore.validate_board(bad, all_boards=[bad],
                                       known_indicators={"us.cpi"})
    assert any("指标" in e for e in errors)


# ---------------------------------------------------------------------------
# BoardService — 快照
# ---------------------------------------------------------------------------


def test_snapshot_latest_values(repo):
    svc = BoardService(repo)
    df = svc.snapshot(SAMPLE_BOARD)
    # 两行(两指标)
    assert len(df) == 2
    by_key = dict(zip(df["指标键"], df["指标"]))
    assert by_key["us.cpi"] == "美国CPI"
    assert by_key["us.unemployment"] == "失业率"

    # 最新日期都是 2026-12-01
    assert (df["最新日期"] == "2026-12-01").all()
    # CPI 主口径 = item.transform=yoy → 最新值 5.0((105/100-1)*100)
    cpi = df[df["指标键"] == "us.cpi"].iloc[0]
    assert cpi["最新值"] == 5.0
    # 环比恒 0(全年常数 105); 同比 = 主口径 = 5.0
    assert cpi["环比%"] == 0.0
    assert cpi["同比%"] == 5.0
    # 近3期含最新
    assert cpi["近3期"].endswith("105.00")
    # 失业率主口径 = level → 4.2
    unemp = df[df["指标键"] == "us.unemployment"].iloc[0]
    assert unemp["最新值"] == 4.2


def test_snapshot_item_window_overrides_board(repo):
    """item 级 date_start 覆盖看板级:只取区间内数据,快照为该区间最新"""
    board = dict(SAMPLE_BOARD, date_start=None, date_end=None)
    board["items"] = [{
        "indicator": "us.cpi", "name": "美国CPI", "transform": "level",
        "date_start": "2025-01-01", "date_end": "2025-12-31",
    }]
    df = BoardService(repo).snapshot(board)
    assert len(df) == 1
    assert df.iloc[0]["最新日期"] == "2025-12-01"
    assert df.iloc[0]["最新值"] == 100.0  # 2025 全年常数 100.0


def test_snapshot_empty_indicator(repo):
    """无获信映射的指标被跳过(不报错)"""
    board = dict(SAMPLE_BOARD, items=[{"indicator": "no.map"}])
    df = BoardService(repo).snapshot(board)
    assert df.empty


# ---------------------------------------------------------------------------
# BoardService — 时序宽表
# ---------------------------------------------------------------------------


def test_series_wide_table(repo):
    svc = BoardService(repo)
    df = svc.series(SAMPLE_BOARD)
    assert "date" in df.columns
    assert "美国CPI" in df.columns and "失业率" in df.columns
    # 行数 = 月度去重日期数(24)
    assert len(df) == 24
    # 日期升序
    assert df["date"].is_monotonic_increasing
    # 各 item 用自身有效口径:CPI=yoy(2025-01 无前值→NaN), 失业率=level
    assert pd.isna(df["美国CPI"].iloc[0])
    assert df["美国CPI"].iloc[-1] == pytest.approx(5.0, abs=0.01)  # yoy
    assert df["失业率"].iloc[-1] == 4.2                            # level


def test_series_transform_override(repo):
    """transform 覆盖所有 item → 值变派生口径"""
    df = BoardService(repo).series(SAMPLE_BOARD, transform="yoy")
    # 最后一行 CPI yoy = 5.0; 失业率 yoy = (4.2/4.5-1)*100 ≈ -6.67
    assert df["美国CPI"].iloc[-1] == pytest.approx(5.0, abs=0.01)
    assert df["失业率"].iloc[-1] == pytest.approx(-6.67, abs=0.01)


def test_series_date_window(repo):
    """显式日期窗口限制行数"""
    df = BoardService(repo).series(
        SAMPLE_BOARD, start_date="20260101", end_date="20260401")
    assert set(df["date"]) == {"2026-01-01", "2026-02-01",
                               "2026-03-01", "2026-04-01"}


# ---------------------------------------------------------------------------
# API 端点
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(repo, themes_path, monkeypatch):
    """注入临时库 + 临时 themes.yaml 的 TestClient"""
    from src.api.server import FastAPIServer
    from fastapi.testclient import TestClient
    import src.core.boards as boards_mod

    monkeypatch.setattr(boards_mod, "default_themes_path",
                        lambda: themes_path)
    server = FastAPIServer(repo=repo)
    return TestClient(server.app)


def test_api_boards_list_empty(client):
    r = client.get("/api/v1/boards")
    assert r.status_code == 200
    assert r.json()["data"] == []


def test_api_boards_snapshot(client, themes_path):
    BoardStore(themes_path).add_board(SAMPLE_BOARD)
    r = client.get("/api/v1/boards/daily/snapshot")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    recs = {rec["指标键"]: rec for rec in body["data"]}
    assert recs["us.cpi"]["最新值"] == 5.0
    assert recs["us.cpi"]["最新日期"] == "2026-12-01"


def test_api_boards_series(client, themes_path):
    BoardStore(themes_path).add_board(SAMPLE_BOARD)
    r = client.get("/api/v1/boards/daily", params={"limit": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] > 0
    # 最新在前(倒序)
    dates = [rec["date"] for rec in body["data"]]
    assert dates == sorted(dates, reverse=True)


def test_api_boards_unknown_404(client):
    r = client.get("/api/v1/boards/not_exist")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 代码类条目(type: code) — 分类树选择器/校验/快照/时序共用同一口径
# ---------------------------------------------------------------------------


@pytest.fixture()
def repo_with_code(repo):
    """在基础 repo 上追加 fund_etf_daily 代码表(两个 code, 各 4 天)

    - 510300 close: 08-01 4.0 → 08-04 4.3(线性爬升)
    - 159001 close: 08-01 0.90 → 08-04 1.05
    日期列/数值列全 TEXT(与真实库一致), 快照/时序内部 to_numeric。
    """
    eng = repo.engine
    with eng.begin() as c:
        c.execute(text(
            'CREATE TABLE fund_etf_daily (id INTEGER PRIMARY KEY AUTOINCREMENT, '
            '"date" TEXT, "code" TEXT, "open" TEXT, "high" TEXT, "low" TEXT, '
            '"close" TEXT, "volume" TEXT, created_at TEXT)'))
        closes = {"510300": ["4.0", "4.1", "4.2", "4.3"],
                  "159001": ["0.90", "0.95", "1.00", "1.05"]}
        rows = []
        for code, cs in closes.items():
            for i, cl in enumerate(cs):
                rows.append({"d": f"2026-08-0{i + 1}", "c": code,
                             "o": cl, "h": cl, "l": cl, "cl": cl, "v": "1000"})
        c.execute(text(
            'INSERT INTO fund_etf_daily '
            '(date, code, open, high, low, close, volume) '
            'VALUES (:d, :c, :o, :h, :l, :cl, :v)'), rows)
    yield repo


# 与 collect_code_sources 输出同构(table_name → 目录信息)
CODE_SOURCE = {
    "fund_etf_daily": {
        "source_key": "fund_etf",
        "name": "ETF基金日线",
        "table_name": "fund_etf_daily",
        "code_column": "code",
        "deprecated": False,
    },
}

CODE_BOARD = {
    "key": "funds",
    "name": "基金观察",
    "description": "",
    "date_start": None,
    "date_end": None,
    "items": [
        {"type": "code", "table": "fund_etf_daily", "code_column": "code",
         "code": "510300", "value_column": "close", "name": "沪深300ETF"},
    ],
}


def test_item_type_backward_compat():
    from src.core.boards import _item_type
    # 老条目无 type → indicator
    assert _item_type({}) == "indicator"
    assert _item_type({"indicator": "us.cpi"}) == "indicator"
    assert _item_type({"type": "indicator", "indicator": "us.cpi"}) == "indicator"
    assert _item_type({"type": "code", "table": "fund_etf_daily"}) == "code"
    assert _item_type({"type": "CODE"}) == "code"


def test_validate_code_item_ok(themes_path):
    store = BoardStore(themes_path)
    errors = store.validate_board(CODE_BOARD, all_boards=[CODE_BOARD],
                                  known_indicators=set(),
                                  known_code_sources=CODE_SOURCE)
    assert errors == []


def test_validate_code_item_missing_fields():
    bad = dict(CODE_BOARD, items=[{"type": "code", "table": "fund_etf_daily",
                                   "code": "510300"}])
    errors = BoardStore.validate_board(bad, all_boards=[bad],
                                       known_code_sources=CODE_SOURCE)
    assert any("value_column" in e for e in errors)
    assert any("code_column" in e for e in errors)


def test_validate_code_item_bad_value_column():
    bad = dict(CODE_BOARD, items=[{"type": "code", "table": "fund_etf_daily",
                                   "code_column": "code", "code": "510300",
                                   "value_column": "foo"}])
    errors = BoardStore.validate_board(bad, all_boards=[bad],
                                       known_code_sources=CODE_SOURCE)
    assert any("value_column" in e and "非法" in e for e in errors)


def test_validate_code_item_unknown_table():
    bad = dict(CODE_BOARD, items=[{"type": "code", "table": "nope",
                                   "code_column": "code", "code": "x",
                                   "value_column": "close"}])
    errors = BoardStore.validate_board(bad, all_boards=[bad],
                                       known_code_sources=CODE_SOURCE)
    assert any("nope" in e for e in errors)


def test_validate_code_item_deprecated():
    src = {"fund_etf_daily": dict(CODE_SOURCE["fund_etf_daily"],
                                  deprecated=True)}
    errors = BoardStore.validate_board(CODE_BOARD, all_boards=[CODE_BOARD],
                                       known_code_sources=src)
    assert any("停更" in e for e in errors)


def test_validate_code_item_wrong_code_column():
    bad = dict(CODE_BOARD, items=[{"type": "code", "table": "fund_etf_daily",
                                   "code_column": "symbol", "code": "510300",
                                   "value_column": "close"}])
    errors = BoardStore.validate_board(bad, all_boards=[bad],
                                       known_code_sources=CODE_SOURCE)
    assert any("代码列" in e for e in errors)


def test_snapshot_code_row(repo_with_code):
    df = BoardService(repo_with_code).snapshot(CODE_BOARD)
    assert len(df) == 1
    row = df.iloc[0]
    assert row["类型"] == "代码"
    assert row["指标"] == "沪深300ETF"
    assert row["指标键"] == "fund_etf_daily:510300"
    assert row["最新值"] == 4.3
    assert row["最新日期"] == "2026-08-04"
    assert row["环比%"] is None
    assert row["同比%"] is None
    assert row["近3期"] == "4.10, 4.20, 4.30"


def test_snapshot_code_empty_skipped(repo_with_code):
    """库内没有的代码 → 该行跳过(与指标一致, 不报错)"""
    board = dict(CODE_BOARD, items=[dict(CODE_BOARD["items"][0], code="999999")])
    df = BoardService(repo_with_code).snapshot(board)
    assert df.empty


def test_snapshot_code_window(repo_with_code):
    """item 级日期窗口限定该 code 的数据范围"""
    board = dict(CODE_BOARD, items=[dict(CODE_BOARD["items"][0],
                                         date_start="2026-08-01",
                                         date_end="2026-08-02")])
    row = BoardService(repo_with_code).snapshot(board).iloc[0]
    assert row["最新值"] == 4.1      # 区间内最新(08-02)
    assert row["最新日期"] == "2026-08-02"


def test_series_code_wide(repo_with_code):
    df = BoardService(repo_with_code).series(CODE_BOARD)
    assert "沪深300ETF" in df.columns
    assert list(df.columns) == ["date", "沪深300ETF"]
    assert len(df) == 4
    assert df["date"].is_monotonic_increasing
    # 值列 close 全量, 最新 4.3
    assert df["沪深300ETF"].iloc[-1] == pytest.approx(4.3)


def test_series_code_transform_override(repo_with_code):
    """?transform=mom 对代码条目也生效(环比收盘价)"""
    df = BoardService(repo_with_code).series(CODE_BOARD, transform="mom")
    assert df["沪深300ETF"].iloc[-1] == pytest.approx(
        2.380952, abs=0.01)  # (4.3/4.2-1)*100


def test_mixed_board_snapshot(repo_with_code):
    """指标 + 代码混合主题: 两行同构输出, 口径各按类型"""
    board = {
        "key": "mix", "name": "混合", "description": "",
        "date_start": None, "date_end": None,
        "items": [
            {"indicator": "us.cpi", "name": "美国CPI", "transform": "yoy"},
            {"type": "code", "table": "fund_etf_daily", "code_column": "code",
             "code": "510300", "value_column": "close", "name": "沪深300ETF"},
        ],
    }
    df = BoardService(repo_with_code).snapshot(board)
    assert len(df) == 2
    assert set(df["类型"]) == {"指标", "代码"}
    cpi = df[df["指标键"] == "us.cpi"].iloc[0]
    code = df[df["指标键"] == "fund_etf_daily:510300"].iloc[0]
    assert cpi["最新值"] == 5.0
    assert code["最新值"] == 4.3
    # 混合列里 None 被 pandas 提为 NaN(API 层 _df_to_json_records 会转回 None)
    assert pd.isna(code["环比%"]) and cpi["环比%"] == 0.0


@pytest.fixture()
def client_with_code(repo_with_code, themes_path, monkeypatch):
    """注入临时库(含代码表) + 临时 themes.yaml 的 TestClient"""
    from src.api.server import FastAPIServer
    from fastapi.testclient import TestClient
    import src.core.boards as boards_mod

    monkeypatch.setattr(boards_mod, "default_themes_path",
                        lambda: themes_path)
    server = FastAPIServer(repo=repo_with_code)
    return TestClient(server.app)


def test_api_boards_snapshot_code(client_with_code, themes_path):
    BoardStore(themes_path).add_board(CODE_BOARD)
    r = client_with_code.get("/api/v1/boards/funds/snapshot")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    rec = body["data"][0]
    assert rec["类型"] == "代码"
    assert rec["指标键"] == "fund_etf_daily:510300"
    assert rec["最新值"] == 4.3
    assert rec["最新日期"] == "2026-08-04"
    assert rec["环比%"] is None and rec["同比%"] is None


def test_api_boards_series_code(client_with_code, themes_path):
    BoardStore(themes_path).add_board(CODE_BOARD)
    r = client_with_code.get("/api/v1/boards/funds")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] >= 4
    # 最新在前(倒序)
    dates = [rec["date"] for rec in body["data"]]
    assert dates == sorted(dates, reverse=True)


# ---------------------------------------------------------------------------
# board_sync_targets — 主题「同步本主题」的可同步目标解析
# ---------------------------------------------------------------------------


class _FakeRegistry:
    """最小 registry: 只提供 get_all_sources(与真实 FetcherRegistry 同签名)"""

    def __init__(self, sources):
        self._sources = sources

    def get_all_sources(self, include_deprecated=True):
        if include_deprecated:
            return self._sources
        return [s for s in self._sources if not s.get("deprecated")]


_FUND_SRC = {
    "source_key": "fund_etf", "name": "ETF基金日线",
    "table_name": "fund_etf_daily", "api_function": "fund_daily",
    "code_column": "code",
    "params_template": {"ts_code": {"type": "text", "default": "159001.SZ"}},
}
_CPI_SRC = {
    "source_key": "fred_cpi", "name": "美国CPI",
    "table_name": "macro_fred_cpi", "api_function": "fred",
    "indicator": "us.cpi",
}
_STOCK_SRC = {
    "source_key": "stock", "name": "A股日线",
    "table_name": "stock_daily", "api_function": "stock_zh_a",
    "code_column": "code",
    "params_template": {"symbol": {"type": "text"}},
}


def test_sync_targets_code_only(repo):
    """code 条目 → (source_key, {ts_code: 归一化代码}); 多 code 各自保留"""
    reg = _FakeRegistry([_FUND_SRC])
    board = {"key": "t", "items": [
        {"type": "code", "table": "fund_etf_daily", "code": "510300",
         "value_column": "close"},
        {"type": "code", "table": "fund_etf_daily", "code": "159001",
         "value_column": "close"},
    ]}
    assert board_sync_targets(board, repo, reg) == [
        ("fund_etf", {"ts_code": "510300.SH"}),   # 5 开头 → SH
        ("fund_etf", {"ts_code": "159001.SZ"}),   # 15 开头 → SZ
    ]


def test_sync_targets_indicator(repo):
    """indicator 条目 → 获信源表所在目录源, params=None(同步整表)"""
    reg = _FakeRegistry([_CPI_SRC])
    board = {"key": "t", "items": [{"indicator": "us.cpi", "name": "美国CPI"}]}
    assert board_sync_targets(board, repo, reg) == [("fred_cpi", None)]


def test_sync_targets_mixed_dedup(repo):
    """混合主题两类并存; 同 code 同参去重"""
    reg = _FakeRegistry([_FUND_SRC, _CPI_SRC])
    board = {"key": "t", "items": [
        {"indicator": "us.cpi"},
        {"type": "code", "table": "fund_etf_daily", "code": "510300",
         "value_column": "close"},
        {"type": "code", "table": "fund_etf_daily", "code": "510300",
         "value_column": "open"},   # 同 code 同参 → 去重
    ]}
    assert board_sync_targets(board, repo, reg) == [
        ("fred_cpi", None),
        ("fund_etf", {"ts_code": "510300.SH"}),
    ]


def test_sync_targets_skips_deprecated_and_unmapped(repo):
    """deprecated 表 / 无获信源指标 / 目录不存在的表 → 跳过"""
    dep = dict(_FUND_SRC, deprecated=True)
    board_code = {"key": "t", "items": [
        {"type": "code", "table": "fund_etf_daily", "code": "510300"}]}
    assert board_sync_targets(board_code, repo, _FakeRegistry([dep])) == []

    board_ind = {"key": "t", "items": [{"indicator": "no.such"}]}
    assert board_sync_targets(board_ind, repo, _FakeRegistry([_CPI_SRC])) == []

    board_unknown = {"key": "t", "items": [
        {"type": "code", "table": "nope", "code": "510300"}]}
    assert board_sync_targets(board_unknown, repo, _FakeRegistry([_FUND_SRC])) == []


def test_sync_targets_symbol_param():
    """非 ts_code 源(如 symbol) → 原值注入"""
    reg = _FakeRegistry([_STOCK_SRC])
    board = {"key": "t", "items": [
        {"type": "code", "table": "stock_daily", "code": "600519"}]}
    assert board_sync_targets(board, None, reg) == [("stock", {"symbol": "600519"})]
