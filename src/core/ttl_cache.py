# -*- coding: utf-8 -*-
"""进程内 TTL 缓存 — 零依赖，供 API 高频查询用

- 线程安全（RLock）：GUI/API/调度同进程多线程共享
- time.monotonic() 判断过期（免疫时钟回拨）
- max_entries 兜底淘汰最旧（dict 保序）
- clear(prefix) 支持按前缀失效（指标/表名），供同步成功后局部清空

设计说明：缓存**原始未过滤查询结果**，transform/日期过滤/limit/fields 每次
请求现算（派生对 ≤5000 行是微秒级），这样 key 最少、失效最简单。
"""

import threading
import time
from typing import Any


class TTLCache:
    """带 TTL 的进程内缓存"""

    def __init__(self, default_ttl: float = 300.0, max_entries: int = 256):
        self.default_ttl = default_ttl
        self.max_entries = max_entries
        self._store: dict[str, tuple[Any, float]] = {}
        self._lock = threading.RLock()

    def _now(self) -> float:
        return time.monotonic()

    def get(self, key: str) -> Any | None:
        """取缓存；不存在/已过期 → None（过期即删除）"""
        with self._lock:
            item = self._store.get(key)
            if item is None:
                return None
            value, expires_at = item
            if self._now() >= expires_at:
                del self._store[key]
                return None
            return value

    def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        """写缓存；满时先清过期，仍满按插入序丢最旧"""
        with self._lock:
            if len(self._store) >= self.max_entries:
                self._evict_expired()
            if len(self._store) >= self.max_entries:
                self._evict_oldest()
            self._store[key] = (value, self._now() + (ttl if ttl is not None else self.default_ttl))

    def clear(self, prefix: str | None = None) -> int:
        """清缓存；prefix 指定时只清以该前缀开头的 key，返回清除数"""
        with self._lock:
            if prefix is None:
                n = len(self._store)
                self._store.clear()
                return n
            keys = [k for k in self._store if k.startswith(prefix)]
            for k in keys:
                del self._store[k]
            return len(keys)

    def _evict_expired(self) -> None:
        now = self._now()
        expired = [k for k, (_, exp) in self._store.items() if now >= exp]
        for k in expired:
            del self._store[k]

    def _evict_oldest(self) -> None:
        if self._store:
            # dict 保序：最早插入的在最前
            oldest = next(iter(self._store))
            del self._store[oldest]

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)


# 模块级单例：API 端点与 SyncEngine 共享
cache = TTLCache()
