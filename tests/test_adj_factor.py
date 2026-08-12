# -*- coding: utf-8 -*-
"""行情复权纯函数测试 — apply_adjustment / derive_factor_from_prices / is_tushare_permission_error

数学正确性：qfq = raw × factor / latest_factor、hfq = raw × factor。
因子缺失行丢弃、组内 ffill、多 code 各自 latest、端到端回放 akshare 口径。
"""

import pandas as pd
import pytest

from src.core.adj_factor import (
    ADJ_TYPES,
    apply_adjustment,
    derive_factor_from_prices,
    is_tushare_permission_error,
)


# ---------------------------------------------------------------------------
# apply_adjustment — 单 code 数学正确性
# ---------------------------------------------------------------------------


def _single_factor() -> pd.DataFrame:
    """单 code 因子表：factor 从 1.0 升到 2.0，latest = 2.0"""
    return pd.DataFrame({
        "code": ["159915"] * 3,
        "date": ["2026-08-01", "2026-08-02", "2026-08-03"],
        "factor": [1.0, 1.5, 2.0],
    })


def _single_prices() -> pd.DataFrame:
    """单 code 行情：close=10 恒定"""
    return pd.DataFrame({
        "code": ["159915"] * 3,
        "date": ["2026-08-01", "2026-08-02", "2026-08-03"],
        "open": [9.0, 10.0, 11.0],
        "high": [9.5, 10.5, 11.5],
        "low": [8.5, 9.5, 10.5],
        "close": [10.0, 10.0, 10.0],
        "volume": [100.0, 100.0, 100.0],
    })


def test_apply_adjustment_qfq_math():
    """qfq = price × factor / latest(2.0)：close 10 → [5.0, 7.5, 10.0]"""
    out = apply_adjustment(_single_prices(), "qfq", _single_factor())
    assert out["close"].tolist() == pytest.approx([5.0, 7.5, 10.0])
    # open 同样被乘：9 → 4.5、10 → 7.5、11 → 11
    assert out["open"].tolist() == pytest.approx([4.5, 7.5, 11.0])
    # volume 不处理
    assert out["volume"].tolist() == [100.0, 100.0, 100.0]


def test_apply_adjustment_hfq_math():
    """hfq = price × factor：close 10 → [10.0, 15.0, 20.0]"""
    out = apply_adjustment(_single_prices(), "hfq", _single_factor())
    assert out["close"].tolist() == pytest.approx([10.0, 15.0, 20.0])


def test_apply_adjustment_ffill_between_factor_days():
    """因子只在除权日有记录（稀疏）→ 中间行情日组内 ffill"""
    factor = pd.DataFrame({
        "code": ["159915"] * 2,
        "date": ["2026-08-01", "2026-08-03"],
        "factor": [1.0, 2.0],
    })
    out = apply_adjustment(_single_prices(), "hfq", factor)
    # 08-02 无因子记录 → ffill 用 08-01 的 1.0；08-03 用 2.0
    assert out["close"].tolist() == pytest.approx([10.0, 10.0, 20.0])


def test_apply_adjustment_drops_rows_before_first_factor():
    """早于首个因子日的行情行（无 factor 可 ffill）→ 丢弃"""
    prices = pd.DataFrame({
        "code": ["159915"] * 2,
        "date": ["2026-07-31", "2026-08-01"],
        "close": [10.0, 10.0],
    })
    factor = pd.DataFrame({
        "code": ["159915"],
        "date": ["2026-08-01"],
        "factor": [2.0],
    })
    out = apply_adjustment(prices, "hfq", factor)
    assert len(out) == 1
    assert out["date"].tolist() == ["2026-08-01"]


def test_apply_adjustment_multi_code_each_latest():
    """多 code：各自用自己全序列的 latest 归一化"""
    prices = pd.DataFrame({
        "code": ["A", "A", "B", "B"],
        "date": ["2026-08-01", "2026-08-02"] * 2,
        "close": [10.0, 10.0, 20.0, 20.0],
    })
    factor = pd.DataFrame({
        "code": ["A", "A", "B", "B"],
        "date": ["2026-08-01", "2026-08-02"] * 2,
        "factor": [1.0, 2.0, 1.0, 4.0],
    })
    out = apply_adjustment(prices, "qfq", factor, code_col="code")
    # A: latest=2 → [5.0, 10.0]；B: latest=4 → [5.0, 20.0]
    a = out[out["code"] == "A"]["close"].tolist()
    b = out[out["code"] == "B"]["close"].tolist()
    assert a == pytest.approx([5.0, 10.0])
    assert b == pytest.approx([5.0, 20.0])


def test_apply_adjustment_unknown_type_raises():
    with pytest.raises(ValueError):
        apply_adjustment(_single_prices(), "bogus", _single_factor())


def test_apply_adjustment_empty_factor_raises():
    with pytest.raises(ValueError):
        apply_adjustment(_single_prices(), "qfq", pd.DataFrame())


def test_apply_adjustment_empty_df_returns_copy():
    out = apply_adjustment(pd.DataFrame(), "qfq", _single_factor())
    assert out.empty


# ---------------------------------------------------------------------------
# derive_factor_from_prices — akshare 回退反推因子
# ---------------------------------------------------------------------------


def _mk_series(factor_fn, dates):
    """构造价格序列：close = factor_fn(d) × base（base=10）"""
    return pd.DataFrame({
        "date": dates,
        "close": [factor_fn(d) * 10 for d in dates],
    })


def test_derive_factor_hfq_then_apply_replays_akshare():
    """回退链路端到端：hfq 序列 ÷ raw 序列 → 因子 → apply_adjustment
    精确复现 akshare qfq 与 hfq 口径"""
    dates = ["2026-08-01", "2026-08-02", "2026-08-03"]
    # 真实累计因子 F = [1.0, 1.5, 2.0]（相对首日归一化）
    F = {"2026-08-01": 1.0, "2026-08-02": 1.5, "2026-08-03": 2.0}
    raw = _mk_series(lambda d: 1.0, dates)          # raw 恒 10
    hfq = _mk_series(lambda d: F[d], dates)         # hfq = 10×F(t)

    factor = derive_factor_from_prices(hfq, raw)
    assert factor["date"].tolist() == dates
    assert factor["factor"].tolist() == pytest.approx([1.0, 1.5, 2.0])

    # qfq 查询 = raw × factor/latest = 10 × F(t)/2.0 → [5, 7.5, 10]
    out_qfq = apply_adjustment(raw, "qfq", factor)
    assert out_qfq["close"].tolist() == pytest.approx([5.0, 7.5, 10.0])
    # hfq 查询 = raw × factor = 10 × F(t) → [10, 15, 20]
    out_hfq = apply_adjustment(raw, "hfq", factor)
    assert out_hfq["close"].tolist() == pytest.approx([10.0, 15.0, 20.0])


def test_derive_factor_skips_zero_close():
    """raw 收盘价为 0（异常/停牌空值）→ 该行不参与反推，避免除零"""
    dates = ["2026-08-01", "2026-08-02"]
    raw = pd.DataFrame({"date": dates, "close": [0.0, 10.0]})
    hfq = pd.DataFrame({"date": dates, "close": [0.0, 20.0]})
    factor = derive_factor_from_prices(hfq, raw)
    assert len(factor) == 1
    assert factor["date"].tolist() == ["2026-08-02"]
    assert factor["factor"].iloc[0] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# is_tushare_permission_error
# ---------------------------------------------------------------------------


def test_permission_error_positive():
    for msg in ("积分不足", "权限不足", "抱歉，您没有权限", "no permission",
                "无此接口调用权限"):
        assert is_tushare_permission_error(Exception(msg))


def test_permission_error_negative():
    for msg in ("连接超时", "网络错误", "trade_cal 拉取失败"):
        assert not is_tushare_permission_error(Exception(msg))


# ---------------------------------------------------------------------------
# API ?adj= 集成（临时库 + TestClient）
# ---------------------------------------------------------------------------


@pytest.fixture()
def api_env(tmp_path):
    """临时库:fund_etf_daily(多 code 多日) + asset_adj_factor(因子表)

    因子: 159915 → [1.0(08-01), 2.0(08-02), 3.0(08-03)]；raw close=10 恒定。
    qfq 应得 [3.33, 6.67, 10.0]（factor/latest=3.0）；hfq 应得 [10, 20, 30]。
    """
    import tempfile
    import os
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine, text
    from src.api.server import FastAPIServer
    from src.db.repository import DataRepository

    tmp = tempfile.mktemp(suffix=".db")
    eng = create_engine(f"sqlite:///{tmp}")
    repo = DataRepository(eng)
    repo.create_tables()
    with eng.begin() as c:
        c.execute(text(
            'CREATE TABLE fund_etf_daily (id INTEGER PRIMARY KEY AUTOINCREMENT, '
            '"date" TEXT, open TEXT, high TEXT, low TEXT, close TEXT, '
            'volume TEXT, amount TEXT, code TEXT, created_at TEXT)'))
        c.execute(text(
            'CREATE TABLE asset_adj_factor (id INTEGER PRIMARY KEY AUTOINCREMENT, '
            '"asset_type" TEXT, code TEXT, date TEXT, factor TEXT, created_at TEXT)'))
        c.execute(text(
            'CREATE TABLE index_daily (id INTEGER PRIMARY KEY AUTOINCREMENT, '
            '"date" TEXT, open TEXT, high TEXT, low TEXT, close TEXT, symbol TEXT)'))
        # 行情:159915 close=10
        for d in ("2026-08-01", "2026-08-02", "2026-08-03"):
            c.execute(text(
                'INSERT INTO fund_etf_daily (date, open, close, volume, code) '
                'VALUES (:d, :o, :c, :v, :code)'),
                {"d": d, "o": "9.0", "c": "10.0", "v": "100", "code": "159915"})
        # 因子:fund → [1.0, 2.0, 3.0]
        for i, f in enumerate([1.0, 2.0, 3.0], start=1):
            c.execute(text(
                'INSERT INTO asset_adj_factor (asset_type, code, date, factor) '
                'VALUES (:at, :code, :d, :f)'),
                {"at": "fund", "code": "159915",
                 "d": f"2026-08-0{i}", "f": str(f)})
        # 指数:无因子归属
        c.execute(text(
            'INSERT INTO index_daily (date, open, close, symbol) '
            'VALUES ("2026-08-03", "3000", "3010", "sh000001")'))

    server = FastAPIServer(repo=repo)
    client = TestClient(server.app)

    yield repo, client
    eng.dispose()
    if os.path.exists(tmp):
        os.remove(tmp)


def test_api_data_adj_qfq(api_env):
    """?adj=qfq → close = 10 × factor/3.0 = [3.33, 6.67, 10.0]，meta.adj 透传"""
    repo, client = api_env
    r = client.get("/api/v1/data/fund_etf_daily?adj=qfq&start_date=20260801&end_date=20260803")
    assert r.status_code == 200
    body = r.json()
    assert body["meta"]["adj"] == "qfq"
    assert body["meta"]["adj_asset_type"] == "fund"
    closes = {rec["date"]: rec["close"] for rec in body["data"]}
    assert closes["2026-08-01"] == pytest.approx(10.0 / 3.0)
    assert closes["2026-08-02"] == pytest.approx(20.0 / 3.0)
    assert closes["2026-08-03"] == pytest.approx(10.0)


def test_api_data_adj_hfq(api_env):
    """?adj=hfq → close = 10 × factor = [10, 20, 30]"""
    repo, client = api_env
    r = client.get("/api/v1/data/fund_etf_daily?adj=hfq&start_date=20260801&end_date=20260803")
    assert r.status_code == 200
    body = r.json()
    closes = {rec["date"]: rec["close"] for rec in body["data"]}
    assert closes["2026-08-01"] == pytest.approx(10.0)
    assert closes["2026-08-02"] == pytest.approx(20.0)
    assert closes["2026-08-03"] == pytest.approx(30.0)


def test_api_data_adj_no_factor_source_422(api_env):
    """指数表无复权因子源 → ?adj= 422"""
    repo, client = api_env
    r = client.get("/api/v1/data/index_daily?adj=qfq")
    assert r.status_code == 422


def test_api_data_adj_invalid_value_422(api_env):
    """非法 adj 值 → FastAPI pattern 422"""
    repo, client = api_env
    r = client.get("/api/v1/data/fund_etf_daily?adj=bogus")
    assert r.status_code == 422


def test_api_data_adj_factor_table_empty_422(api_env):
    """因子表无数据（asset_type 查不到）→ 422 提示先同步因子"""
    repo, client = api_env
    from sqlalchemy import text
    with repo.engine.begin() as c:
        c.execute(text('DELETE FROM asset_adj_factor'))
    r = client.get("/api/v1/data/fund_etf_daily?adj=qfq&start_date=20260801&end_date=20260803")
    assert r.status_code == 422
    assert "同步" in r.json()["detail"]
