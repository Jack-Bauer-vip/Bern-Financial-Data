# -*- coding: utf-8 -*-
"""板块热力图(指数版)测试 — /api/v1/indices/heatmap 端点 + 数据构建纯函数

- _build_index_heatmap: 日期筛选(默认最新交易日)、三组映射
  (宽基→核心指数 / 行业→行业 / 主题→概念)、涨跌幅=目标日前收盘环比、
  排除风格/策略/债券/跨境/其他、排除单日(无前收盘)指数
- 端点: 全量 / ?date= 指定日期 / 无效日期 422 / 空表不 422
"""

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from src.api import routes
from src.api.server import FastAPIServer
from src.db.repository import DataRepository


@pytest.fixture(autouse=True)
def _clear_ttl_cache():
    """/indices/heatmap 用进程级 TTL 缓存(60s)，测试间清空防串扰"""
    from src.core.ttl_cache import cache
    cache.clear()
    yield
    cache.clear()


@pytest.fixture()
def heatmap_env(tmp_path):
    """临时库：meta_index_category(宽基/行业/主题/风格/跨境) + index_daily(3 交易日)"""
    db = str(tmp_path / "hm.db")
    eng = create_engine(f"sqlite:///{db}")
    repo = DataRepository(eng)
    repo.create_tables()
    with eng.begin() as c:
        c.execute(text(
            'CREATE TABLE meta_index_category (id INTEGER PRIMARY KEY AUTOINCREMENT, '
            'code TEXT, name TEXT, category TEXT, sub_category TEXT, source TEXT, created_at TEXT)'))
        rows = [
            ("sh000300", "沪深300", "宽基", "大盘", "auto"),
            ("sz399997", "中证白酒指数", "主题", "白酒", "auto"),
            ("sh000819", "有色金属", "行业", "有色金属", "auto"),
            ("sz399667", "创业板G", "风格", "成长", "auto"),      # 风格 → 三组不展示
            ("sh000998", "沪深300单日", "宽基", "大盘", "auto"),  # 单日行情 → 跳过
            ("HSI", "恒生指数", "跨境", "港股", "curated"),        # 跨境 → 不展示
        ]
        for code, name, cat, sub, src in rows:
            c.execute(text(
                'INSERT INTO meta_index_category (code, name, category, sub_category, source) '
                'VALUES (:c, :n, :cat, :sub, :src)'),
                {"c": code, "n": name, "cat": cat, "sub": sub, "src": src})
        # index_daily: 核心指数涨、概念跌、行业涨；风格/跨境/单日(无前收盘)应排除
        c.execute(text(
            'CREATE TABLE index_daily (id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, '
            'open TEXT, high TEXT, low TEXT, close TEXT, volume TEXT, amount TEXT, symbol TEXT)'))
        bars = [
            ("2026-08-07", "3950", "900", "sh000300"),
            ("2026-08-10", "4000", "1000", "sh000300"),
            ("2026-08-11", "4040", "1200", "sh000300"),   # 08-11 环比 +1.00%
            ("2026-08-07", "4980", "700", "sz399997"),
            ("2026-08-10", "5000", "800", "sz399997"),
            ("2026-08-11", "4950", "900", "sz399997"),    # -1.00%
            ("2026-08-07", "990", "90", "sh000819"),
            ("2026-08-10", "1000", "100", "sh000819"),
            ("2026-08-11", "1020", "150", "sh000819"),    # +2.00%
            ("2026-08-10", "2000", "300", "sz399667"),    # 风格 → 排除
            ("2026-08-11", "2010", "310", "sz399667"),
            ("2026-08-10", "20000", "5000", "HSI"),       # 跨境 → 排除
            ("2026-08-11", "20100", "5200", "HSI"),
            ("2026-08-11", "3000", "600", "sh000998"),    # 单日 → 排除(无前收盘)
        ]
        for date, close, vol, sym in bars:
            c.execute(text(
                'INSERT INTO index_daily (date, close, volume, symbol) VALUES (:d, :c, :v, :s)'),
                {"d": date, "c": close, "v": vol, "s": sym})
    server = FastAPIServer(repo=repo)
    yield TestClient(server.app), repo
    eng.dispose()
    if os.path.exists(db):
        os.remove(db)


# ---------------------------------------------------------------------------
# 纯函数：数据构建
# ---------------------------------------------------------------------------


def test_build_heatmap_groups(heatmap_env):
    """三组映射 + 涨跌幅环比 + 排除风格/跨境/单日/表外"""
    client, repo = heatmap_env
    body = routes._build_index_heatmap(repo)
    assert body["date"] == "2026-08-11"
    assert body["dates"][:3] == ["2026-08-11", "2026-08-10", "2026-08-07"]
    by_code = {it["code"]: it for it in body["items"]}
    assert set(by_code) == {"sh000300", "sz399997", "sh000819"}
    assert by_code["sh000300"]["group"] == "核心指数"
    assert by_code["sz399997"]["group"] == "概念"
    assert by_code["sh000819"]["group"] == "行业"
    assert by_code["sh000300"]["pct_chg"] == pytest.approx(1.0)
    assert by_code["sh000300"]["volume"] == pytest.approx(1200)
    assert by_code["sh000300"]["date"] == "2026-08-11"
    assert by_code["sz399997"]["pct_chg"] == pytest.approx(-1.0)
    assert by_code["sh000819"]["pct_chg"] == pytest.approx(2.0)
    # 组计数
    g = {x["key"]: x["count"] for x in body["groups"]}
    assert g == {"核心指数": 1, "行业": 1, "概念": 1}


def test_build_heatmap_specific_date(heatmap_env):
    """指定日期: 涨跌幅按该日对前收盘算, 全部 items.date = 该日"""
    client, repo = heatmap_env
    body = routes._build_index_heatmap(repo, "2026-08-10")
    assert body["date"] == "2026-08-10"
    assert all(it["date"] == "2026-08-10" for it in body["items"])
    by_code = {it["code"]: it for it in body["items"]}
    assert set(by_code) == {"sh000300", "sz399997", "sh000819"}
    assert by_code["sh000300"]["pct_chg"] == pytest.approx(round(4000 / 3950 * 100 - 100, 2))


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------


def test_heatmap_full(heatmap_env):
    r = heatmap_env[0].get("/api/v1/indices/heatmap")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    codes = {it["code"] for it in body["data"]}
    assert codes == {"sh000300", "sz399997", "sh000819"}
    assert "HSI" not in codes
    assert "sz399667" not in codes          # 风格不展示
    assert body["meta"]["date"] == "2026-08-11"
    assert set(body["meta"]["dates"]) == {"2026-08-11", "2026-08-10", "2026-08-07"}
    assert body["meta"]["count_by_group"] == {"核心指数": 1, "行业": 1, "概念": 1}


def test_heatmap_date_filter(heatmap_env):
    """?date=YYYYMMDD 指定交易日; 无效日期 422"""
    client = heatmap_env[0]
    r = client.get("/api/v1/indices/heatmap", params={"date": "20260810"})
    assert r.status_code == 200
    body = r.json()
    assert body["meta"]["date"] == "2026-08-10"
    assert all(it["date"] == "2026-08-10" for it in body["data"])
    # 无行情日期 → 422
    r2 = client.get("/api/v1/indices/heatmap", params={"date": "20260101"})
    assert r2.status_code == 422


def test_heatmap_empty(tmp_path):
    """meta_index_category 不存在 → 200 空（不 422）"""
    db = str(tmp_path / "empty_hm.db")
    eng = create_engine(f"sqlite:///{db}")
    repo = DataRepository(eng)
    repo.create_tables()
    server = FastAPIServer(repo=repo)
    client = TestClient(server.app)
    r = client.get("/api/v1/indices/heatmap")
    assert r.status_code == 200
    assert r.json()["data"] == []
