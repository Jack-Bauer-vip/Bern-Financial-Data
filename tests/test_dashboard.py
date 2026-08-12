"""网页只读看板 /dashboard 冒烟测试 — 静态可达 + 鉴权豁免。

数据看板是纯静态前端(src/api/web/), 复用 /api/v1 数据端点, 后端只多了
StaticFiles 挂载与 AuthMiddleware 前缀豁免, 这里验证挂载与豁免生效。
"""

from fastapi.testclient import TestClient

from src.api.server import FastAPIServer
from src.db.engine import get_engine
from src.db.repository import DataRepository


def _make_client():
    repo = DataRepository(get_engine())
    server = FastAPIServer(repo=repo)
    return TestClient(server.app)


def test_dashboard_index_served():
    with _make_client() as client:
        r = client.get("/dashboard")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "Bern 数据看板" in r.text
        assert 'id="table-select"' in r.text
        # 阶段一: 类型预设 tab + 代码搜索下拉容器 + 空态提示条
        assert 'id="type-tabs"' in r.text
        assert 'data-type="general"' in r.text
        assert 'data-type="index"' in r.text
        assert 'data-type="etf"' in r.text
        assert 'data-type="stock"' in r.text
        assert 'id="code-dropdown"' in r.text
        assert 'id="empty-hint"' in r.text
        # 阶段二(指数分类): chip 行容器
        assert 'id="cat-chips"' in r.text


def test_dashboard_index_with_trailing_slash():
    with _make_client() as client:
        r = client.get("/dashboard/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]


def test_dashboard_static_assets_reachable():
    with _make_client() as client:
        for path in ("/dashboard/style.css", "/dashboard/stats.js",
                     "/dashboard/chart.js", "/dashboard/app.js",
                     "/dashboard/vendor/echarts.min.js"):
            r = client.get(path)
            assert r.status_code == 200, f"{path} → {r.status_code}"


def test_echarts_is_real_js():
    with _make_client() as client:
        r = client.get("/dashboard/vendor/echarts.min.js")
        assert r.status_code == 200
        assert len(r.content) > 500_000  # ~1MB vendored 完整包
        assert b"Apache" in r.content[:2000] or b"echarts" in r.content[:2000]


def test_dashboard_exempt_from_auth(monkeypatch):
    """设 API_TOKEN 后, 数据端点 401 但 /dashboard 页面与静态资源仍可访问"""
    monkeypatch.setenv("API_TOKEN", "dashboard-test-token")
    # 未带 token 的 API 请求应被拒
    with _make_client() as client:
        r = client.get("/api/v1/data/tables")
        assert r.status_code == 401
        # 看板页面 + 静态资源豁免
        assert client.get("/dashboard").status_code == 200
        assert client.get("/dashboard/app.js").status_code == 200
        assert client.get("/dashboard/vendor/echarts.min.js").status_code == 200


def test_api_data_still_works_with_token(monkeypatch):
    monkeypatch.setenv("API_TOKEN", "dashboard-test-token")
    with _make_client() as client:
        r = client.get("/api/v1/data/tables", headers={"X-API-Key": "dashboard-test-token"})
        assert r.status_code == 200
        assert r.json()["code"] == 200
