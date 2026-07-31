"""AI 客户端测试 — 用 mock 替换 httpx，不真连 ollama"""

import pytest

from src.importer.ai_client import AiClient, extract_json, strip_think


# ---------------------------------------------------------------------------
# strip_think / extract_json
# ---------------------------------------------------------------------------


def test_strip_think():
    assert strip_think("<think>推理过程</think>{\"a\":1}") == "{\"a\":1}"
    assert strip_think("{\"a\":1}") == "{\"a\":1}"
    assert strip_think("") == ""


def test_extract_json_plain():
    assert extract_json('{"table":"x","confidence":0.9}') == {
        "table": "x", "confidence": 0.9}


def test_extract_json_with_think():
    assert extract_json('<think>...</think>{"table":"x"}') == {"table": "x"}


def test_extract_json_fenced():
    assert extract_json('```json\n{"table":"x"}\n```') == {"table": "x"}


def test_extract_json_surrounded_text():
    # 前后附带文字
    assert extract_json('结果是 {"table":"x","c":1} 就是这样') == {
        "table": "x", "c": 1}


def test_extract_json_garbage():
    assert extract_json("完全不是 JSON") is None
    assert extract_json("") is None
    assert extract_json(None) is None


def test_extract_json_trailing_comma():
    # 容错：尾随逗号
    assert extract_json('{"table":"x","c":1,}') == {"table": "x", "c": 1}


# ---------------------------------------------------------------------------
# AiClient（mock httpx）
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


def _make_client(monkeypatch, response, available=True):
    """构造 AiClient，monkeypatch 探测与请求"""
    client = AiClient()
    monkeypatch.setattr(
        client, "is_available", lambda: available)
    monkeypatch.setattr(
        "httpx.post", lambda *a, **k: response)
    return client


def test_identify_table_parses(monkeypatch):
    resp = _FakeResponse({"message": {"content": '{"table":"macro_china_cpi_yearly","confidence":0.9,"reason":"匹配"}'}})
    client = _make_client(monkeypatch, resp)
    result = client.identify_table(["商品"], {"商品": ["中国CPI"]}, "候选表清单")
    assert result is not None
    assert result["table"] == "macro_china_cpi_yearly"
    assert result["confidence"] == 0.9


def test_identify_table_think_content(monkeypatch):
    resp = _FakeResponse({"message": {"content": '<think>推理</think>{"table":"index_daily","confidence":0.8}'}})
    client = _make_client(monkeypatch, resp)
    result = client.identify_table(["date"], {}, "候选表清单")
    assert result is not None
    assert result["table"] == "index_daily"


def test_identify_table_fails_returns_none(monkeypatch):
    """AI 返回无效内容 → None（上层降级回规则）"""
    resp = _FakeResponse({"message": {"content": "无法识别"}})
    client = _make_client(monkeypatch, resp)
    assert client.identify_table(["a"], {}, "候选表清单") is None


def test_identify_table_http_error(monkeypatch):
    resp = _FakeResponse({}, status=500)
    client = _make_client(monkeypatch, resp)
    assert client.identify_table(["a"], {}, "候选表清单") is None


def test_identify_table_connect_error(monkeypatch):
    """连接异常 → 返回 None（不抛异常）"""
    client = AiClient()
    monkeypatch.setattr(client, "is_available", lambda: True)

    def _boom(*a, **k):
        import httpx
        raise httpx.ConnectError("connection refused")
    monkeypatch.setattr("httpx.post", _boom)
    assert client.identify_table(["a"], {}, "候选表清单") is None


def test_map_columns_returns_mapping(monkeypatch):
    resp = _FakeResponse({"message": {"content": '{"日期":"时间","今值":"数值"}'}})
    client = _make_client(monkeypatch, resp)
    result = client.map_columns(["日期", "今值"], ["时间", "数值"])
    assert result == {"日期": "时间", "今值": "数值"}
