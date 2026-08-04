"""FRED API 客户端测试 — 解析、错误映射、DataFetcher 分发、配置（全离线，mock 网络）"""

import pandas as pd
import pytest

from src.core.data_fetcher import DataFetcher
from src.core.exceptions import DataFetchError, NetworkError, RateLimitError
from src.core import fred_client
from src.core.fred_client import fetch_series
from src.utils.config import ConfigManager


class _FakeResp:
    """模拟 requests.Response"""

    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _observations_json(rows):
    """构建 FRED observations 响应体"""
    return {
        "realtime_start": "2026-01-01", "realtime_end": "2026-01-01",
        "observations": [
            {"realtime_start": "2026-01-01", "realtime_end": "2026-01-01",
             "date": date, "value": value}
            for date, value in rows
        ],
    }


# ---------------------------------------------------------------------------
# fetch_series 解析
# ---------------------------------------------------------------------------


def test_fetch_series_parses_observations(monkeypatch):
    """正常响应 → date/value 两列，'.' → NaN"""
    payload = _observations_json([
        ("2026-01-01", "3.4"),
        ("2026-02-01", "."),
        ("2026-03-01", "3.6"),
    ])
    monkeypatch.setattr(
        fred_client.requests, "get",
        lambda url, params, timeout: _FakeResp(200, payload))

    df = fetch_series("UNRATE", "KEY", observation_start="2026-01-01")
    assert list(df.columns) == ["date", "value"]
    assert len(df) == 3
    assert df["date"].tolist() == ["2026-01-01", "2026-02-01", "2026-03-01"]
    assert df["value"].tolist() == pytest.approx([3.4, float("nan"), 3.6], nan_ok=True)


def test_fetch_series_empty_observations(monkeypatch):
    """无观测 → 空 DataFrame（仍有两列）"""
    payload = {"observations": []}
    monkeypatch.setattr(
        fred_client.requests, "get",
        lambda url, params, timeout: _FakeResp(200, payload))
    df = fetch_series("UNRATE", "KEY")
    assert df.empty
    assert list(df.columns) == ["date", "value"]


def test_fetch_series_missing_series_id():
    """空序列 ID → DataFetchError"""
    with pytest.raises(DataFetchError):
        fetch_series("", "KEY")


def test_fetch_series_missing_key():
    """空 key → DataFetchError"""
    with pytest.raises(DataFetchError):
        fetch_series("UNRATE", "")


# ---------------------------------------------------------------------------
# 错误映射
# ---------------------------------------------------------------------------


def test_fetch_series_429_raises_rate_limit(monkeypatch):
    """429 → RateLimitError"""
    payload = {"error_code": 429, "error_message": "too many requests"}
    monkeypatch.setattr(
        fred_client.requests, "get",
        lambda url, params, timeout: _FakeResp(429, payload))
    with pytest.raises(RateLimitError):
        fetch_series("UNRATE", "KEY")


def test_fetch_series_4xx_raises_data_fetch(monkeypatch):
    """400 → DataFetchError，携带 error_message"""
    payload = {"error_code": 400, "error_message": "bad series_id"}
    monkeypatch.setattr(
        fred_client.requests, "get",
        lambda url, params, timeout: _FakeResp(400, payload))
    with pytest.raises(DataFetchError) as exc:
        fetch_series("UNRATE", "KEY")
    assert "bad series_id" in str(exc.value)


def test_fetch_series_network_error(monkeypatch):
    """连接异常 → NetworkError"""

    def _boom(url, params, timeout):
        raise fred_client.requests.ConnectionError("connection refused")

    monkeypatch.setattr(fred_client.requests, "get", _boom)
    with pytest.raises(NetworkError):
        fetch_series("UNRATE", "KEY")


# ---------------------------------------------------------------------------
# DataFetcher 分发
# ---------------------------------------------------------------------------


def _config_without_fred_key():
    cfg = ConfigManager()
    # 确保未配置 key（monkeypatch 优先于 .env）
    return cfg


def test_fetch_fred_without_key_raises(monkeypatch):
    """api_source=fred 但无 key → DataFetchError"""
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    fetcher = DataFetcher(_config_without_fred_key())
    with pytest.raises(DataFetchError) as exc:
        fetcher.fetch({"api_source": "fred", "api_function": "UNRATE"}, {})
    assert "FRED_API_KEY" in str(exc.value)


def test_fetch_fred_dispatch_calls_client(monkeypatch):
    """有 key + mock 客户端 → 返回解析后的 df，区间参数正确传递"""
    monkeypatch.setenv("FRED_API_KEY", "TESTKEY")

    def _fake_fetch_series(series_id, api_key, observation_start=None,
                           observation_end=None, base_url=None, timeout=None):
        assert series_id == "UNRATE"
        assert api_key == "TESTKEY"
        assert observation_start == "2024-01-01"
        assert observation_end == "2024-06-30"
        return pd.DataFrame({"date": ["2024-01-01"], "value": [3.4]})

    # _call_fred 内部是 `from src.core.fred_client import fetch_series` → patch 该模块
    monkeypatch.setattr("src.core.fred_client.fetch_series", _fake_fetch_series)
    fetcher = DataFetcher(_config_without_fred_key())
    df = fetcher.fetch(
        {"api_source": "fred", "api_function": "UNRATE"},
        {"start_date": "2024-01-01", "end_date": "2024-06-30"})
    assert list(df.columns) == ["date", "value"]
    assert df.iloc[0]["value"] == 3.4


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def test_config_fred_api_key_from_env(monkeypatch):
    """ConfigManager.fred_api_key 读取环境变量"""
    monkeypatch.setenv("FRED_API_KEY", "SECRET")
    cfg = ConfigManager()
    assert cfg.fred_api_key == "SECRET"


def test_config_fred_api_url_default_and_override(monkeypatch):
    """fred_api_url：默认官方地址；FRED_API_URL 覆盖"""
    monkeypatch.delenv("FRED_API_URL", raising=False)
    cfg = ConfigManager()
    assert cfg.fred_api_url == "https://api.stlouisfed.org"

    monkeypatch.setenv("FRED_API_URL", "https://example.com")
    assert cfg.fred_api_url == "https://example.com"
