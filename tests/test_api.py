"""API 路由冒烟测试 — 覆盖 /health、/sources、/macro 日期过滤、/sync 校验

运行: python -m pytest tests/test_api.py -v
依赖: pytest、fastapi.testclient（随项目依赖安装）
"""

import os

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
    """从一条记录中取日期字符串（兼容多列名）；跳过 None/NaT 的列"""
    for col in ("时间", "日期", "date"):
        v = rec.get(col)
        if v not in (None, "NaT", ""):
            return str(v)[:10]
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
    """不存在的表 → 404（防任意表枚举）"""
    r = client.get("/api/v1/macro/not_a_real_table_xyz")
    assert r.status_code == 404


def test_macro_rejects_meta_table(client):
    """meta 元数据表不可通过 /macro 查询（防信息泄露）"""
    r = client.get("/api/v1/macro/meta_sync_jobs")
    assert r.status_code == 404


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


def test_data_query_chinese_date_column(client):
    """回归 2026-08-12: 中文「月份」列(2026年06月份)表带日期过滤不再误杀。

    此前 _filter_by_date_range 用 pd.to_datetime 直接解析中文日期得 NaT,
    兜底过滤把整表清空(total=0); 修复为先 normalize_cn_date_str 再比较。
    """
    plain = client.get("/api/v1/data/macro_china_cpi", params={"limit": 5})
    if plain.status_code != 200 or plain.json().get("total", 0) == 0:
        pytest.skip("数据库无 macro_china_cpi 数据")
    r = client.get(
        "/api/v1/data/macro_china_cpi",
        params={"start_date": "19000101", "end_date": "20991231", "limit": 1000},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] > 0, "中文日期列被兜底过滤误杀为 0"
    # 带日期过滤时「月份」归一化为 ISO(YYYY-MM-DD), 应落在区间内
    for rec in body["data"]:
        m = rec.get("月份")
        if m:
            assert "1900-01-01" <= str(m)[:10] <= "2099-12-31"


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


# ---------------------------------------------------------------------------
# token 鉴权
# ---------------------------------------------------------------------------


@pytest.fixture()
def client_with_token():
    """设置了 API_TOKEN 的 API 客户端"""
    old = os.environ.get("API_TOKEN")
    os.environ["API_TOKEN"] = "test-secret-123"
    repo = DataRepository(get_engine())
    server = FastAPIServer(repo=repo)
    yield TestClient(server.app)
    if old is None:
        os.environ.pop("API_TOKEN", None)
    else:
        os.environ["API_TOKEN"] = old


def test_auth_missing_token_401(client_with_token):
    r = client_with_token.get("/api/v1/data/tables")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# data_status（deprecated 源标注）
# ---------------------------------------------------------------------------


def test_sources_have_data_status(client):
    """/sources 每个源带 data_status，4 个 deprecated 源标记正确"""
    r = client.get("/api/v1/sources")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] > 0
    by_key = {s["source_key"]: s for s in body["data"]}
    assert all("data_status" in s for s in body["data"])
    for key in ("macro.us.ism_pmi", "macro.us.ism_non_pmi",
                "macro.us.cb_consumer_confidence",
                "macro.us.nfib_small_business"):
        assert by_key.get(key, {}).get("data_status") == "deprecated"


def test_data_endpoint_data_status_deprecated(client):
    """/data 对 deprecated 表返回 data_status=deprecated"""
    r = client.get("/api/v1/data/macro_usa_ism_pmi", params={"limit": 5})
    assert r.status_code == 200
    assert r.json()["data_status"] == "deprecated"


def test_data_endpoint_data_status_active(client):
    """/data 对正常源返回 data_status=active"""
    r = client.get("/api/v1/data/macro_usa_cpi_yoy", params={"limit": 5})
    assert r.status_code == 200
    assert r.json()["data_status"] == "active"


def test_health_has_stale_sources(client):
    """/health 含 stale_sources 且不含 deprecated 源"""
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert "stale_sources" in body and "stale_count" in body
    # 兼容旧字段仍在
    assert body["status"] == "ok"
    assert "db_rows" in body
    # deprecated 源（ism_pmi 等）不应出现在 stale_sources（避免假"停更"）
    dep_keys = {"macro.us.ism_pmi", "macro.us.ism_non_pmi",
                "macro.us.cb_consumer_confidence",
                "macro.us.nfib_small_business"}
    for s in body["stale_sources"]:
        assert s["source_key"] not in dep_keys


# ---------------------------------------------------------------------------
# indicator transform 派生视图
# ---------------------------------------------------------------------------


def test_indicator_transform_yoy(client):
    """/indicator 支持 transform=yoy，返回同比序列"""
    r = client.get("/api/v1/indicator/us.unemployment",
                   params={"transform": "yoy", "limit": 100})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] >= 0
    # yoy 返回百分比；检查有值记录均为数值或 null
    for rec in body["data"]:
        assert "date" in rec and "value" in rec
        assert rec["value"] is None or isinstance(rec["value"], (int, float))


def test_indicator_transform_invalid(client):
    """非法 transform 值 → 422"""
    r = client.get("/api/v1/indicator/us.unemployment",
                   params={"transform": "bogus"})
    assert r.status_code == 422


def test_indicator_default_level_is_numeric(client):
    """不带 transform 返回 float（而非库内 TEXT 字符串）"""
    r = client.get("/api/v1/indicator/us.unemployment", params={"limit": 20})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] > 0
    for rec in body["data"]:
        if rec["value"] is not None:
            assert isinstance(rec["value"], (int, float)), \
                f"value 应为数值, got {type(rec['value'])}: {rec['value']}"


def test_auth_wrong_token_401(client_with_token):
    r = client_with_token.get(
        "/api/v1/data/tables", headers={"X-API-Key": "wrong"})
    assert r.status_code == 401


def test_auth_correct_header_200(client_with_token):
    r = client_with_token.get(
        "/api/v1/data/tables", headers={"X-API-Key": "test-secret-123"})
    assert r.status_code == 200


def test_auth_query_param_rejected_401(client_with_token):
    """token 放查询参数会被拒绝（防 URL 泄露到访问日志）"""
    r = client_with_token.get("/api/v1/data/tables?token=test-secret-123")
    assert r.status_code == 401


def test_auth_public_paths_exempt(client_with_token):
    """/health、/docs、/ 免鉴权"""
    assert client_with_token.get("/api/v1/health").status_code == 200
    assert client_with_token.get("/").status_code == 200
    assert client_with_token.get("/docs").status_code == 200


def test_auth_no_token_configured_allows(client):
    """未配置 token → 全部放行（默认行为）"""
    assert client.get("/api/v1/data/tables").status_code == 200
