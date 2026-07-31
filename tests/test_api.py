"""API 路由冒烟测试 — 覆盖 /health、/sources、/macro 日期过滤、/sync 校验

运行: python -m pytest tests/test_api.py -v
依赖: pytest、fastapi.testclient（随项目依赖安装）
"""

import pytest
from fastapi.testclient import TestClient

from src.db.engine import get_engine
from src.db.repository import DataRepository
from src.api.server import FastAPIServer


class FakeScheduler:
    """测试用调度器桩 — 模拟 DataScheduler.is_running()"""

    def __init__(self, running: bool = True):
        self._running = running

    def is_running(self) -> bool:
        return self._running


@pytest.fixture()
def client():
    """无 scheduler 注入的 API 客户端（用项目数据库，只读端点安全）"""
    repo = DataRepository(get_engine())
    server = FastAPIServer(repo=repo)
    return TestClient(server.app)


@pytest.fixture()
def client_with_scheduler():
    """注入了运行中 scheduler 的 API 客户端"""
    repo = DataRepository(get_engine())
    server = FastAPIServer(repo=repo, scheduler=FakeScheduler(running=True))
    return TestClient(server.app)


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------


def test_health(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["scheduler_running"] is False


def test_health_scheduler_running(client_with_scheduler):
    r = client_with_scheduler.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["scheduler_running"] is True


# ---------------------------------------------------------------------------
# 根路径 / 连接监控
# ---------------------------------------------------------------------------


def test_root(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.json()["service"].startswith("Bern_Financial_Data")


def test_connections(client):
    r = client.get("/api/v1/connections")
    assert r.status_code == 200
    body = r.json()
    assert "total_connections" in body
    assert "total_requests" in body
    assert isinstance(body["connections"], list)


# ---------------------------------------------------------------------------
# /sources
# ---------------------------------------------------------------------------


def test_sources(client):
    r = client.get("/api/v1/sources")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] > 0
    # 每个源都有 source_key
    assert all("source_key" in s for s in body["data"])


# ---------------------------------------------------------------------------
# /macro 日期区间过滤
# ---------------------------------------------------------------------------


def _first_date(rec) -> str:
    """从一条记录中取日期字符串（兼容多列名）"""
    for col in ("时间", "日期", "date"):
        if col in rec:
            return str(rec[col])[:10]
    return ""


def test_macro_cpi_date_range(client):
    """/macro/cpi 日期区间过滤：结果都在区间内"""
    r = client.get(
        "/api/v1/macro/cpi",
        params={"start_date": "20260101", "end_date": "20261231"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] >= 0
    for rec in body["data"]:
        d = _first_date(rec)
        assert d == "" or "2026-01-01" <= d <= "2026-12-31"


def test_macro_generic_date_range_desc(client):
    """/macro/{table_name} 日期区间过滤：倒序排列且都在区间内"""
    r = client.get(
        "/api/v1/macro/usa_cpi_yoy",
        params={"start_date": "20260101", "end_date": "20261231"},
    )
    assert r.status_code == 200
    body = r.json()
    dates = [_first_date(rec) for rec in body["data"]]
    dates = [d for d in dates if d]  # 去掉无日期记录
    assert dates, "该表应有 2026 年数据"
    # 倒序
    assert dates == sorted(dates, reverse=True), "结果应按日期倒序排列"
    # 都在区间内
    assert all("2026-01-01" <= d <= "2026-12-31" for d in dates)


def test_macro_unknown_table(client):
    """不存在的表返回空而非报错"""
    r = client.get("/api/v1/macro/not_a_real_table_xyz")
    assert r.status_code == 200
    assert r.json()["data"] == []


# ---------------------------------------------------------------------------
# 通用数据接口（数据分发）
# ---------------------------------------------------------------------------


def test_data_tables_list(client):
    """列出可查询的数据表"""
    r = client.get("/api/v1/data/tables")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] > 0
    assert all("table_name" in t for t in body["data"])


def test_data_query_generic(client):
    """通用按表查询"""
    r = client.get("/api/v1/data/macro_usa_cpi_yoy")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] > 0
    # 最新数据在前（日期倒序）
    if body["data"]:
        first = body["data"][0]
        assert any(k in first for k in ("时间", "日期", "date"))


def test_data_query_date_range(client):
    """通用查询的日期区间过滤"""
    r = client.get(
        "/api/v1/data/macro_usa_cpi_yoy",
        params={"start_date": "20260601", "end_date": "20261231"},
    )
    assert r.status_code == 200
    body = r.json()
    for rec in body["data"]:
        d = _first_date(rec)
        assert d == "" or "2026-06-01" <= d <= "2026-12-31"


def test_data_query_fields(client):
    """字段选择"""
    r = client.get(
        "/api/v1/data/index_daily",
        params={"fields": "date,close", "limit": 5},
    )
    assert r.status_code == 200
    body = r.json()
    if body["data"]:
        assert set(body["data"][0].keys()) == {"date", "close"}


def test_data_query_unknown_table_404(client):
    """非法表名 → 404（防注入）"""
    r = client.get("/api/v1/data/not_a_real_table_xyz")
    assert r.status_code == 404


def test_data_query_injection_404(client):
    """注入尝试 → 404"""
    r = client.get("/api/v1/data/macro_usa_cpi_yoy;DROP%20TABLE%20x")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# /sync 校验
# ---------------------------------------------------------------------------


def test_sync_unknown_source_404(client):
    """未知数据源 → 404，且不会启动后台同步"""
    r = client.post("/api/v1/sync/not_exist_source")
    assert r.status_code == 404


def test_sync_known_source_202(client, monkeypatch):
    """已知数据源 → 202 + SyncTriggerResponse 结构；monkeypatch 阻止真实同步写库"""
    import src.core.sync_engine as sync_mod

    calls = []
    monkeypatch.setattr(
        sync_mod.SyncEngine, "run",
        lambda self, key, *a, **k: calls.append(key),
    )
    r = client.post("/api/v1/sync/macro.us.cpi_yoy")
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "accepted"
    assert body["source_key"] == "macro.us.cpi_yoy"
    assert body["message"]
