# -*- coding: utf-8 -*-
"""指数分类(理杏仁式)测试 — 纯函数 + /api/v1/indices 端点

纯函数（无网络无 mock）：
- classify_by_heuristics 按真实指数名称做关键词分类
- parse_index_category_config / build_category_rows 合并(手动覆盖→自动→其他 + 跨境)

端点（临时库 + TestClient）：
- 全量返回 + meta.categories 计数
- ?category= 过滤 / ?q= 名称/code 模糊（字母码大小写不敏感）
- 空表 → 200 空, 不 422
"""

import os
import tempfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from src.api.server import FastAPIServer
from src.core.index_categories import (
    build_category_rows,
    classify_by_heuristics,
    parse_index_category_config,
)
from src.db.repository import DataRepository


@pytest.fixture(autouse=True)
def _clear_ttl_cache():
    """/indices 用进程级 TTL 缓存，测试间清空防串扰"""
    from src.core.ttl_cache import cache
    cache.clear()
    yield
    cache.clear()

# ---------------------------------------------------------------------------
# 纯函数：启发式分类
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,category", [
    # 宽基白名单
    ("上证指数", "宽基"),
    ("中证1000指数", "宽基"),          # 去尾缀「指数」后命中白名单
    ("创业板指", "宽基"),
    ("科创50", "宽基"),
    ("中证A500", "宽基"),
    # 风格
    ("中证1000成长", "风格"),          # 白名单锚定全名，不误判宽基
    ("消费红利", "风格"),              # 红利 先于 消费(行业)
    ("300价值", "风格"),
    ("大盘低波", "风格"),
    ("红利指数", "风格"),
    # 央视系列整体归策略
    ("央视50", "策略"),
    ("央视成长", "策略"),              # 不被 成长(风格) 抢走
    # 债券
    ("国债指数", "债券"),
    ("中证转债", "债券"),
    ("碳中和债", "债券"),              # 债 先于 碳中和(主题)
    # 主题
    ("中证白酒指数", "主题"),
    ("新能源", "主题"),
    ("科创芯片", "主题"),
    ("金融科技", "主题"),              # 科技(主题) 先于 金融(行业)
    ("中证环境治理指数", "主题"),      # 环境(主题) 先于 治理(策略)
    # 行业
    ("中证银行指数", "行业"),
    ("中证军工", "行业"),
    ("中证全指证券公司指数", "行业"),
    ("上证金融", "行业"),
    ("国证食品", "行业"),            # 食品 → 行业
    ("IT指数", "行业"),              # IT → 行业(信息)
    # 所有制主题(红利类被风格优先级抢先)
    ("中证央企", "主题"),
    ("中证国企", "主题"),
    ("民企200", "主题"),
    ("央企100", "主题"),
    ("央企红利", "风格"),            # 风格 先于 主题
    ("国企红利", "风格"),
    ("民企红利", "风格"),
])
def test_classify_by_heuristics(name, category):
    cat, _ = classify_by_heuristics(name)
    assert cat == category, f"{name}: 期望 {category}, 实际 {cat}"


def test_heuristics_sub_category_style():
    """风格子类细分"""
    assert classify_by_heuristics("消费红利") == ("风格", "红利")
    assert classify_by_heuristics("大盘低波") == ("风格", "低波")
    assert classify_by_heuristics("大盘成长") == ("风格", "大盘成长")
    assert classify_by_heuristics("300价值") == ("风格", "价值")


def test_heuristics_unknown_falls_other():
    """无法归类 → None（由上层归「其他」）"""
    assert classify_by_heuristics("中国波指") is None
    assert classify_by_heuristics("上证海外") is None       # 手动 YAML 覆盖
    assert classify_by_heuristics("") is None


# ---------------------------------------------------------------------------
# 纯函数：config 解析 + 行合并
# ---------------------------------------------------------------------------

SAMPLE_YAML = """\
categories: [宽基, 行业, 主题, 风格, 策略, 债券, 跨境, 其他]
manual:
  sh000300: {category: 宽基, sub_category: 大盘}
  sh000999: {category: 跨境, sub_category: 两岸三地}
global:
  HSI: {name: 恒生指数, category: 跨境, sub_category: 港股, api_function: stock_hk_index_daily_sina}
"""


def test_parse_config():
    cfg = parse_index_category_config(SAMPLE_YAML)
    assert cfg["categories"][0] == "宽基"
    assert cfg["manual"]["sh000300"]["category"] == "宽基"
    assert cfg["global"]["HSI"]["api_function"] == "stock_hk_index_daily_sina"


def test_build_rows_manual_override_and_global():
    """手动覆盖优先于自动；跨境策划合并；无分类源 → 其他"""
    cfg = parse_index_category_config(SAMPLE_YAML)
    rows = build_category_rows({
        "sh000300": "沪深300",      # 自动=宽基，手动覆盖 sub=大盘
        "sh000999": "中证两岸三地500指数",  # 自动=其他，手动覆盖 跨境
        "sz399997": "中证白酒指数",  # 自动=主题
        "sh000188": "中国波指",      # 无法归类 → 其他
    }, cfg)
    by_code = {r["code"]: r for r in rows}
    assert by_code["sh000300"] == {
        "code": "sh000300", "name": "沪深300",
        "category": "宽基", "sub_category": "大盘", "source": "manual"}
    assert by_code["sh000999"]["category"] == "跨境"
    assert by_code["sz399997"]["category"] == "主题"
    assert by_code["sz399997"]["source"] == "auto"
    assert by_code["sh000188"]["category"] == "其他"
    # 跨境策划清单合并
    assert by_code["HSI"]["name"] == "恒生指数"
    assert by_code["HSI"]["category"] == "跨境"
    assert by_code["HSI"]["source"] == "curated"


def test_build_rows_default_categories_when_missing():
    """YAML 无 categories 段 → 用默认分类顺序"""
    cfg = parse_index_category_config("manual: {}\nglobal: {}\n")
    assert cfg["categories"] == ["宽基", "行业", "主题", "风格", "策略", "债券", "跨境", "其他"]


# ---------------------------------------------------------------------------
# 端点：/api/v1/indices
# ---------------------------------------------------------------------------


@pytest.fixture()
def indices_env(tmp_path):
    """临时库：meta_index_category 造 3 境内 + 1 跨境 + 1 表外"""
    db = str(tmp_path / "idx.db")
    eng = create_engine(f"sqlite:///{db}")
    repo = DataRepository(eng)
    repo.create_tables()
    with eng.begin() as c:
        c.execute(text(
            'CREATE TABLE meta_index_category (id INTEGER PRIMARY KEY AUTOINCREMENT, '
            'code TEXT, name TEXT, category TEXT, sub_category TEXT, source TEXT, created_at TEXT)'))
        rows = [
            ("sh000300", "沪深300", "宽基", "大盘", "auto"),
            ("sh000905", "中证500", "宽基", "中盘", "auto"),
            ("sz399997", "中证白酒指数", "主题", "白酒", "auto"),
            ("HSI", "恒生指数", "跨境", "港股", "curated"),
        ]
        for code, name, cat, sub, src in rows:
            c.execute(text(
                'INSERT INTO meta_index_category (code, name, category, sub_category, source) '
                'VALUES (:c, :n, :cat, :sub, :src)'),
                {"c": code, "n": name, "cat": cat, "sub": sub, "src": src})
    server = FastAPIServer(repo=repo)
    yield TestClient(server.app)
    eng.dispose()
    if os.path.exists(db):
        os.remove(db)


def test_indices_full_list(indices_env):
    r = indices_env.get("/api/v1/indices")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 4
    codes = [it["code"] for it in body["data"]]
    assert "sh000300" in codes and "HSI" in codes
    # meta.categories 计数
    cats = {c["category"]: c["count"] for c in body["meta"]["categories"]}
    assert cats == {"宽基": 2, "主题": 1, "跨境": 1}


def test_indices_category_filter(indices_env):
    r = indices_env.get("/api/v1/indices", params={"category": "宽基"})
    body = r.json()
    assert [it["code"] for it in body["data"]] == ["sh000300", "sh000905"]
    # 分类计数不受过滤影响（chip 全量口径）
    cats = {c["category"]: c["count"] for c in body["meta"]["categories"]}
    assert cats["宽基"] == 2


def test_indices_q_search_name(indices_env):
    r = indices_env.get("/api/v1/indices", params={"q": "白酒"})
    body = r.json()
    assert [it["code"] for it in body["data"]] == ["sz399997"]


def test_indices_q_search_code_case_insensitive(indices_env):
    """跨境字母码大小写不敏感（hsi → HSI）"""
    r = indices_env.get("/api/v1/indices", params={"q": "hsi"})
    body = r.json()
    assert [it["code"] for it in body["data"]] == ["HSI"]


def test_indices_empty_table(tmp_path):
    """meta_index_category 不存在 → 200 空 + 空 categories（不 422）"""
    db = str(tmp_path / "empty.db")
    eng = create_engine(f"sqlite:///{db}")
    repo = DataRepository(eng)
    repo.create_tables()
    server = FastAPIServer(repo=repo)
    client = TestClient(server.app)
    r = client.get("/api/v1/indices")
    assert r.status_code == 200
    body = r.json()
    assert body["data"] == []
    assert body["meta"]["categories"] == []
    eng.dispose()
    if os.path.exists(db):
        os.remove(db)
