# -*- coding: utf-8 -*-
"""每数据源互斥锁测试 — 锁定进程级共享锁 + busy 检测

GUI 手动同步 / API 触发 / 定时调度各自持有独立 SyncEngine 实例，单靠实例内
_running 无法互斥同一数据源。_get_sync_lock 是**模块级**函数（共享模块级
_sync_locks 表），按 table_name 返回进程级锁——SyncEngine.run 内部直接引用它，
因此与实例数量无关，同一数据表在所有调用方之间互斥。本测试断言：
1. 两个独立 SyncEngine 实例经模块级函数拿到同一个锁对象；
2. 线程持有锁时 _is_source_busy 返回 True，释放后返回 False。
"""

import threading
import time

from src.core import sync_engine
from src.core.sync_engine import (
    SyncEngine, _get_sync_lock, _is_source_busy)


def _make_engine():
    """构造最小 SyncEngine 实例（互斥锁测试只用到模块级函数）"""
    return SyncEngine(fetcher=None, repo=None, schema_mgr=None, config=None)


def test_two_engines_share_same_lock():
    """两个独立 SyncEngine 实例经模块级 _get_sync_lock 拿到同一个锁对象"""
    e1, e2 = _make_engine(), _make_engine()
    assert e1 is not e2  # 确为两个独立实例

    # run() 内部引用的 _get_sync_lock 正是测试拿到的模块级函数
    assert SyncEngine.run.__globals__["_get_sync_lock"] is _get_sync_lock

    lock1 = _get_sync_lock("fund_etf_daily")
    lock2 = _get_sync_lock("fund_etf_daily")
    assert lock1 is lock2


def test_different_tables_have_different_locks():
    """不同数据表互不影响"""
    lock_a = _get_sync_lock("macro_usa_cpi_yoy")
    lock_b = _get_sync_lock("fund_etf_daily")
    assert lock_a is not lock_b


def test_is_source_busy_while_held():
    """线程持有锁时 _is_source_busy 为 True，释放后为 False"""
    table = "macro_usa_cpi_yoy"
    lock = _get_sync_lock(table)
    release = threading.Event()

    def hold():
        with lock:
            release.wait(5)  # 持锁直到主线程确认 busy 后释放

    t = threading.Thread(target=hold)
    t.start()

    # 轮询等持锁线程进入临界区（跨线程可见 busy）
    deadline = time.time() + 5
    while time.time() < deadline and not _is_source_busy(table):
        time.sleep(0.01)
    assert _is_source_busy(table) is True   # 持锁期间 busy

    release.set()
    t.join(timeout=5)
    assert _is_source_busy(table) is False  # 释放后空闲


def test_instance_running_flag_independent():
    """实例级 _running 独立：一个实例 busy 不影响另一实例的 is_running 属性

    这正说明实例级标志不能跨实例互斥——必须靠模块级进程锁。
    """
    e1, e2 = _make_engine(), _make_engine()
    e1._running = True
    assert e1.is_running is True
    assert e2.is_running is False
