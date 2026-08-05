# -*- coding: utf-8 -*-
"""进程内 TTL 缓存测试"""

import threading
import time

import pytest

from src.core.ttl_cache import TTLCache


def test_miss_returns_none():
    c = TTLCache()
    assert c.get("nope") is None


def test_set_get_hit():
    c = TTLCache()
    c.set("a", {"x": 1})
    assert c.get("a") == {"x": 1}


def test_ttl_expiry(monkeypatch):
    c = TTLCache(default_ttl=5)
    now = [100.0]

    def fake_monotonic():
        return now[0]

    monkeypatch.setattr(c, "_now", fake_monotonic)
    c.set("a", 1)
    assert c.get("a") == 1
    now[0] = 104.9
    assert c.get("a") == 1
    now[0] = 105.1
    assert c.get("a") is None


def test_clear_prefix_only():
    c = TTLCache()
    c.set("ind:us.cpi", 1)
    c.set("ind:us.gdp", 2)
    c.set("macro:us.cpi", 3)
    n = c.clear(prefix="ind:")
    assert n == 2
    assert c.get("ind:us.cpi") is None
    assert c.get("ind:us.gdp") is None
    assert c.get("macro:us.cpi") == 3


def test_clear_all():
    c = TTLCache()
    c.set("a", 1)
    c.set("b", 2)
    assert c.clear() == 2
    assert c.get("a") is None and c.get("b") is None


def test_max_entries_evicts_oldest():
    c = TTLCache(max_entries=3)
    c.set("a", 1)
    c.set("b", 2)
    c.set("c", 3)
    c.set("d", 4)  # 满 → 淘汰最旧 a
    assert c.get("a") is None
    assert c.get("b") == 2 and c.get("c") == 3 and c.get("d") == 4


def test_concurrent_set_get():
    """多线程并发 set/get 不丢不崩"""
    c = TTLCache()
    errors = []

    def worker(i):
        try:
            for j in range(50):
                key = f"k{i % 4}"
                c.set(key, j)
                c.get(key)
                c.clear(prefix="k")
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
