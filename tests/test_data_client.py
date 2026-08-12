"""berndata_client 测试 —— 用 httpx.MockTransport 模拟本地数据服务, 不依赖真实服务/数据库。

覆盖: 信封解包、鉴权头、日期归一、错误抛出(HTTP/422/code非200/连接失败)、
CSV 字节、df 转换、分页 meta、曲线方法路由与参数透传、默认 base_url 解析。
"""

import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "clients" / "berndata_client"))

import httpx
import pytest

from berndata_client import BernDataClient, BernDataError, BernDataResponse
from berndata_client.client import _fmt_date

ENVELOPE_OK = {
    "code": 200,
    "message": "ok",
    "data": [{"date": "2026-08-11", "close": "9.009"}],
    "total": 1,
    "source": "local_db",
    "data_status": "active",
    "meta": {"unit_type": "level"},
}


def make_client(handler, api_key="tk"):
    transport = httpx.MockTransport(handler)
    return BernDataClient(base_url="http://test/api/v1", api_key=api_key, transport=transport)


def json_ok(**overrides):
    body = {"code": 200, "data": [], "total": 0}
    body.update(overrides)
    return httpx.Response(200, json=body)


# ---------- 信封解包 ----------

def test_data_unpacks_envelope():
    r = make_client(lambda req: httpx.Response(200, json=ENVELOPE_OK)).data(
        "fund_etf_daily", code="518880"
    )
    assert isinstance(r, BernDataResponse)
    assert r.data[0]["close"] == "9.009"
    assert r.total == 1
    assert r.source == "local_db"
    assert r.data_status == "active"
    assert r.meta == {"unit_type": "level"}


def test_health_returns_raw_dict():
    c = make_client(lambda req: httpx.Response(200, json={"status": "ok", "stale_count": 3}))
    assert c.health()["status"] == "ok"


def test_tables_extracts_names():
    c = make_client(
        lambda req: httpx.Response(200, json={"code": 200, "data": [{"table_name": "a"}, {"table_name": "b"}]})
    )
    assert c.tables() == ["a", "b"]


def test_as_df_returns_dataframe():
    r = make_client(lambda req: httpx.Response(200, json=ENVELOPE_OK)).data(
        "fund_etf_daily", code="518880", as_df=True
    )
    assert list(r.columns) == ["date", "close"]


def test_df_property_lazy_and_cached():
    c = make_client(lambda req: httpx.Response(200, json=ENVELOPE_OK))
    r = c.data("fund_etf_daily", code="518880")
    assert r._df is None
    df1 = r.df
    df2 = r.df
    assert df1 is df2 and list(df1.columns) == ["date", "close"]


# ---------- 鉴权 ----------

def test_auth_header_sent():
    captured = {}

    def handler(req):
        captured["key"] = req.headers.get("X-API-Key")
        return json_ok()

    make_client(handler).sources()
    assert captured["key"] == "tk"


def test_no_auth_header_when_no_key(monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.delenv("BERN_DATA_API_KEY", raising=False)
    captured = {}

    def handler(req):
        captured["key"] = req.headers.get("X-API-Key")
        return json_ok()

    c = BernDataClient(
        base_url="http://test/api/v1", api_key=None, transport=httpx.MockTransport(handler)
    )
    c.sources()
    assert captured["key"] is None


# ---------- 日期归一 ----------

def test_fmt_date_variants():
    assert _fmt_date(None) is None
    assert _fmt_date("20260811") == "20260811"
    assert _fmt_date("2026-08-11") == "20260811"
    assert _fmt_date("2026/8/1") == "20260801"
    assert _fmt_date(date(2026, 8, 11)) == "20260811"
    assert _fmt_date(datetime(2026, 8, 12, 9, 30)) == "20260812"
    with pytest.raises(ValueError):
        _fmt_date("not-a-date")


def test_date_params_normalized():
    captured = {}

    def handler(req):
        captured["params"] = dict(req.url.params)
        return json_ok()

    c = make_client(handler)
    c.data("fund_etf_daily", start_date=date(2026, 8, 1), end_date=datetime(2026, 8, 12))
    assert captured["params"]["start_date"] == "20260801"
    assert captured["params"]["end_date"] == "20260812"
    c.indicator("us.cpi", start_date="2026-01-01")
    assert captured["params"]["start_date"] == "20260101"


# ---------- 错误 ----------

def test_http_error_raised():
    def handler(req):
        return httpx.Response(401, json={"code": 401, "message": "无效或缺失的 API token"})

    with pytest.raises(BernDataError) as ei:
        make_client(handler).data("fund_etf_daily")
    assert ei.value.status_code == 401
    assert "API token" in ei.value.message


def test_422_detail_list_error_message():
    def handler(req):
        return httpx.Response(
            422, json={"detail": [{"msg": "日期格式应为 YYYYMMDD"}]}
        )

    with pytest.raises(BernDataError) as ei:
        make_client(handler).indicator("us.cpi", start_date="20260812")
    assert "YYYYMMDD" in ei.value.message


def test_envelope_code_non_200_raises():
    def handler(req):
        return httpx.Response(200, json={"code": 500, "message": "内部错误", "data": []})

    with pytest.raises(BernDataError):
        make_client(handler).data("x")


def test_connection_failure_raises():
    c = BernDataClient(base_url="http://127.0.0.1:1/api/v1", api_key=None, timeout=0.1)
    with pytest.raises(BernDataError) as ei:
        c.health()
    assert ei.value.status_code == 0
    c.close()


# ---------- CSV ----------

def test_csv_returns_bytes():
    def handler(req):
        return httpx.Response(
            200,
            content="date,close\n2026-08-11,9.009".encode("utf-8-sig"),
            headers={"Content-Type": "text/csv"},
        )

    buf = make_client(handler).csv("/data/fund_etf_daily", params={"code": "518880"})
    assert b"9.009" in buf


def test_csv_error_raised():
    def handler(req):
        return httpx.Response(404, json={"code": 404, "message": "表不存在"})

    with pytest.raises(BernDataError):
        make_client(handler).csv("/data/not_a_table")


# ---------- 分页 meta ----------

def test_pagination_meta_passthrough():
    def handler(req):
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": [{"date": "2026-08-11", "value": 1}],
                "total": 5000,
                "meta": {"pagination": {"page": 2, "page_size": 100, "has_more": True}},
            },
        )

    r = make_client(handler).indicator("us.cpi", page=2, limit=100)
    assert r.meta["pagination"]["has_more"] is True


# ---------- 曲线方法路由 & 参数透传 ----------

def test_curated_methods_route_and_params():
    captured = []

    def handler(req):
        # base_url 为 http://test/api/v1, 请求实际路径带 /api/v1 前缀
        captured.append((req.url.path[len("/api/v1"):], dict(req.url.params)))
        return json_ok()

    c = make_client(handler)
    c.stock_daily("600519", limit=10)
    c.macro("macro_us_fred_cpi")
    c.macro_cpi(indicator="同比")
    c.data("fund_etf_daily", code="518880", adj="qfq", fields="date,close")
    c.board_snapshot("macro_dashboard")
    c.board_series("macro_dashboard", transform="yoy", page=1)
    c.trigger_sync("macro.us.fred.cpi")
    c.sources(include_deprecated=False)
    c.source_status("macro.us.fred.cpi")

    paths = [p for p, _ in captured]
    assert paths == [
        "/stock/daily",
        "/macro/macro_us_fred_cpi",
        "/macro/cpi",
        "/data/fund_etf_daily",
        "/boards/macro_dashboard/snapshot",
        "/boards/macro_dashboard",
        "/sync/macro.us.fred.cpi",
        "/sources",
        "/sources/macro.us.fred.cpi/status",
    ]
    assert captured[3][1]["adj"] == "qfq"
    assert captured[3][1]["fields"] == "date,close"
    assert captured[5][1]["transform"] == "yoy"
    assert captured[7][1]["include_deprecated"] == "false"


def test_data_drops_none_params():
    captured = {}

    def handler(req):
        captured["params"] = dict(req.url.params)
        return json_ok()

    make_client(handler).data("fund_etf_daily", code=None, limit=200)
    assert captured["params"] == {"limit": "200"}


# ---------- 默认 base_url 解析 ----------

def test_default_base_url_from_env(monkeypatch):
    monkeypatch.setenv("API_HOST", "10.0.0.5")
    monkeypatch.setenv("API_PORT", "9999")
    monkeypatch.delenv("BERN_DATA_BASE_URL", raising=False)
    c = BernDataClient(api_key=None)
    assert c.base_url == "http://10.0.0.5:9999/api/v1"
    c.close()


def test_default_base_url_fallback():
    c = BernDataClient(base_url=None, api_key=None)
    assert c.base_url == "http://127.0.0.1:8765/api/v1"
    c.close()


# ---------- 通用逃生舱 ----------

def test_generic_request_unpacks_envelope():
    c = make_client(lambda req: httpx.Response(200, json=ENVELOPE_OK))
    r = c.request("GET", "/data/fund_etf_daily", params={"code": "518880"})
    assert isinstance(r, BernDataResponse)


def test_context_manager_closes():
    c = make_client(lambda req: json_ok())
    with c as client:
        assert client is c
    assert c._client.is_closed
