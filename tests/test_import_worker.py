"""导入 worker 回归测试 — 防止「worker GC / match_table 参数不匹配」复发"""

import csv
import os
import tempfile

import pandas as pd
import pytest
from sqlalchemy import create_engine, text

from src.db.repository import DataRepository
from src.importer.matcher import match_table


@pytest.fixture()
def repo():
    """临时库，含 index_daily（英文行情）与一张中文宏观表"""
    tmp = tempfile.mktemp(suffix=".db")
    eng = create_engine(f"sqlite:///{tmp}")
    r = DataRepository(eng)
    with eng.begin() as c:
        c.execute(text(
            'CREATE TABLE index_daily (id INTEGER PRIMARY KEY AUTOINCREMENT, '
            '"date" TEXT, "open" TEXT, "high" TEXT, "low" TEXT, "close" TEXT, '
            '"volume" TEXT, created_at TEXT)'))
        c.execute(text(
            'CREATE TABLE macro_china_cpi_yearly ('
            'id INTEGER PRIMARY KEY AUTOINCREMENT, "商品" TEXT, "日期" TEXT, '
            '"今值" TEXT, "预测值" TEXT, "前值" TEXT, created_at TEXT)'))
    yield r
    eng.dispose()
    if os.path.exists(tmp):
        os.remove(tmp)


def test_match_table_accepts_worker_kwargs(repo):
    """回归：worker 传入 tables_meta/filename 不应 TypeError（曾导致识别失败）"""
    df = pd.DataFrame({"日期": ["2026-07-01"], "开盘": ["4.1"], "收盘": ["4.2"]})
    # 显式传 tables_meta=None + filename → 走内部 collect_tables
    res = match_table(df, repo, ai_client=None,
                      tables_meta=None, filename="etf.csv")
    assert res is not None
    assert isinstance(res.table_name, str)


def test_match_table_with_precollected_tables(repo):
    """预收集候选表 + filename 传入"""
    from src.importer.matcher import collect_tables
    tables = collect_tables(repo)
    df = pd.DataFrame({"date": ["2026-01-01"], "open": ["1"], "close": ["2"]})
    res = match_table(df, repo, ai_client=None,
                      tables_meta=tables, filename="index.csv")
    assert res is not None
    assert res.table_name == "index_daily"


def test_match_table_english_quote_rule(repo):
    """英文行情列 → 规则命中 index_daily（无需 AI）"""
    df = pd.DataFrame({"date": ["2026-01-01"], "open": ["1"], "high": ["2"],
                       "low": ["0.5"], "close": ["1.5"], "volume": ["100"]})
    res = match_table(df, repo, ai_client=None)
    assert res.table_name == "index_daily"
    assert res.method == "rules"


def test_match_table_ai_fallback(repo):
    """中文行情列（日期/开盘/收盘）规则低置信 → 有 AI 时走 AI 路径"""
    class _FakeAI:
        def is_available(self):
            return True

        def identify_table(self, columns, sample_values, tables_desc):
            return {"table": "index_daily", "confidence": 0.9,
                    "reason": "行情列匹配"}

    df = pd.DataFrame({"日期": ["2026-07-01"], "开盘": ["4.1"], "收盘": ["4.2"]})
    res = match_table(df, repo, ai_client=_FakeAI())
    assert res.table_name == "index_daily"
    assert res.method == "ai"
    assert res.is_ai is True


def test_match_table_ai_rejected_if_unknown(repo):
    """AI 返回非法表名 → 拒绝，退回规则"""
    class _BadAI:
        def is_available(self):
            return True

        def identify_table(self, columns, sample_values, tables_desc):
            return {"table": "not_a_real_table", "confidence": 0.99}

    df = pd.DataFrame({"日期": ["2026-07-01"], "今值": ["3.5"]})
    res = match_table(df, repo, ai_client=_BadAI())
    # AI 表名非法 → 退回规则 top1
    assert res.table_name != "not_a_real_table"
    assert res.table_name in ("macro_china_cpi_yearly", "index_daily")


def test_batch_identify_worker_runs(repo):
    """回归：worker 完整识别流程不应抛 AttributeError

    曾因 BatchIdentifyWorker 读取 MatchResult 上不存在的 low_confidence
    属性，导致任何导入都报「'MatchResult' object has no attribute 'low_confidence'」。
    """
    from src.gui.dialogs.import_dialog import BatchIdentifyWorker

    # 写一个临时 CSV（英文行情列；文件名不含基金代码 → 走规则识别命中 index_daily）
    tmp_dir = tempfile.mkdtemp()
    csv_path = os.path.join(tmp_dir, "quote_data.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "open", "high", "low", "close", "volume"])
        w.writerow(["2026-08-03", "100", "105", "99", "104", "5000"])

    results = {}
    worker = BatchIdentifyWorker(
        repo, schema_mgr=None, ai_client=None, paths=[csv_path])
    worker.file_done.connect(lambda path, item: results.update({path: item}))
    worker.run()

    item = results[csv_path]
    assert item.table_name == "index_daily"   # 识别成功
    assert not item.error                      # 无错误
    assert item.plan is not None
    assert item.plan.low_confidence is False


def test_batch_identify_worker_fund_routing(repo):
    """文件名含基金代码 → 路由到 fund_etf_daily 并注入 code 列

    对应真实场景：159001.csv / 159003.csv / 159005.csv 等每基金一个文件。
    """
    from src.gui.dialogs.import_dialog import BatchIdentifyWorker

    tmp_dir = tempfile.mkdtemp()
    csv_path = os.path.join(tmp_dir, "159001.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["trade_time", "open", "high", "low", "close", "vol", "amount"])
        w.writerow(["2026-08-03", "100.0", "100.5", "99.5", "100.2", "10000", "1000000"])

    results = {}
    worker = BatchIdentifyWorker(
        repo, schema_mgr=None, ai_client=None, paths=[csv_path])
    worker.file_done.connect(lambda path, item: results.update({path: item}))
    worker.run()

    item = results[csv_path]
    assert item.table_name == "fund_etf_daily"   # 路由到基金表
    assert not item.error
    assert "code" in item.df.columns              # 已注入 code 列
    assert item.df["code"].iloc[0] == "159001"
    assert item.unique_key == ["code", "date"]    # (code,date) 去重键
    # 列映射到规范列：trade_time→date、vol→volume
    assert item.plan.mapping.get("trade_time") == "date"
    assert item.plan.mapping.get("vol") == "volume"


def test_detect_fund_code():
    """基金代码识别：文件名/前缀过滤"""
    from src.importer.matcher import detect_fund_code

    assert detect_fund_code("159001.csv") == "159001"
    assert detect_fund_code("159003.SZ.csv") == "159003"
    assert detect_fund_code("Data_518880.SH.csv") == "518880"
    assert detect_fund_code("etf日线/159005.csv") == "159005"
    # 非基金：无 6 位代码 或 股票代码前缀
    assert detect_fund_code("2026-06-22ETF.csv") is None
    assert detect_fund_code("600519.csv") is None
    assert detect_fund_code("000001.csv") is None
    assert detect_fund_code(None) is None


def test_match_table_cached_same_header(repo):
    """同表头文件只识别一次：第二次命中会话缓存，不重复调 AI"""
    from src.importer.matcher import clear_identification_cache, match_table

    class _CountingAI:
        def __init__(self):
            self.calls = 0

        def is_available(self):
            return True

        def identify_table(self, columns, sample_values, tables_desc):
            self.calls += 1
            return {"table": "index_daily", "confidence": 0.9, "reason": "行情列匹配"}

    ai = _CountingAI()
    clear_identification_cache()  # 排除其他用例的缓存残留
    # 同表头（中文行情列 → 规则低置信触发 AI），仅数据行不同
    df1 = pd.DataFrame({"日期": ["2026-07-01"], "开盘": ["4.1"], "收盘": ["4.2"]})
    df2 = pd.DataFrame({"日期": ["2026-07-02"], "开盘": ["5.1"], "收盘": ["5.2"]})

    r1 = match_table(df1, repo, ai_client=ai)
    r2 = match_table(df2, repo, ai_client=ai)

    assert ai.calls == 1              # 只调一次 AI
    assert r1.table_name == "index_daily"
    assert r2.table_name == r1.table_name


def test_batch_identify_worker_reuses_table_snapshot(repo, monkeypatch):
    """同目标表的多个同表头文件只全表加载一次（批量导入性能优化）"""
    from src.gui.dialogs.import_dialog import BatchIdentifyWorker
    from src.db.repository import DataRepository

    calls: list[str] = []
    orig_query = DataRepository.query

    def counting_query(self, table_name, *a, **k):
        calls.append(table_name)
        return orig_query(self, table_name, *a, **k)

    monkeypatch.setattr(DataRepository, "query", counting_query)

    tmp_dir = tempfile.mkdtemp()
    paths = []
    for i, day in enumerate(["2026-08-01", "2026-08-02"]):
        p = os.path.join(tmp_dir, f"file{i}.csv")
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["date", "open", "high", "low", "close", "volume"])
            w.writerow([day, "100", "105", "99", "104", "5000"])
        paths.append(p)

    results = {}
    worker = BatchIdentifyWorker(repo, schema_mgr=None, ai_client=None, paths=paths)
    worker.file_done.connect(lambda path, item: results.update({path: item}))
    worker.run()

    for p in paths:
        it = results[p]
        assert it.table_name == "index_daily"
        assert not it.error

    index_loads = [t for t in calls if t == "index_daily"]
    assert len(index_loads) == 1, f"目标表应只全表加载一次，实际 {len(index_loads)} 次"


def test_canonicalize_fund_df():
    """akshare 基金中文列 → 规范英文列 + code 注入（API 与 CSV 合并去重的关键）"""
    from src.core.sync_engine import _canonicalize_fund_df
    import pandas as pd

    df = pd.DataFrame({
        "日期": ["2026-08-03"],
        "开盘": ["100.0"], "收盘": ["100.2"],
        "最高": ["100.5"], "最低": ["99.5"],
        "成交量": ["10000"], "成交额": ["1000000"],
        "振幅": ["0.5"], "换手率": ["1.2"],   # 额外列应被丢弃
    })
    out = _canonicalize_fund_df(df, "510050")
    assert list(out.columns) == ["date", "open", "high", "low", "close",
                                 "volume", "amount", "code"]
    assert out["code"].iloc[0] == "510050"
    assert out["date"].iloc[0] == "2026-08-03"


def test_apply_column_map_stock_symbol_injection():
    """股票行情中文列 → 规范列 + symbol 注入（stock.a_daily 的 column_map 路径）"""
    from src.core.sync_engine import _apply_column_map

    df = pd.DataFrame({
        "日期": ["2026-08-03"], "股票代码": ["000001"],
        "开盘": ["10.0"], "收盘": ["10.5"], "最高": ["10.6"], "最低": ["9.9"],
        "成交量": ["1000000"], "成交额": ["10000000"],
        "振幅": ["0.1"], "换手率": ["0.2"],   # 未映射 → 丢弃
    })
    cm = {"日期": "date", "股票代码": "symbol", "开盘": "open", "收盘": "close",
          "最高": "high", "最低": "low", "成交量": "volume", "成交额": "amount"}
    out = _apply_column_map(df, cm, "000001", "symbol")
    assert list(out.columns) == ["date", "open", "high", "low", "close",
                                 "volume", "amount", "symbol"]
    assert out["symbol"].iloc[0] == "000001"
    assert "振幅" not in out.columns


def test_query_date_filter_and_distinct(repo):
    """本地查询：日期区间过滤 + 已有代码去重取值"""
    from src.db.repository import DataRepository
    from sqlalchemy import text

    # 建表 + 插数
    with repo.engine.begin() as c:
        c.execute(text('CREATE TABLE fund_etf_daily (id INTEGER PRIMARY KEY AUTOINCREMENT, '
                       '"date" TEXT, "close" TEXT, "code" TEXT, created_at TEXT)'))
        c.execute(text('INSERT INTO fund_etf_daily ("date", "close", "code") VALUES '
                       '("2026-08-01", "1.0", "159001"), '
                       '("2026-08-02", "1.1", "159001"), '
                       '("2026-08-03", "1.2", "159003")'))

    # 日期过滤
    df = repo.query("fund_etf_daily", date_from="2026-08-02", date_to="2026-08-03")
    assert len(df) == 2
    # 代码过滤
    df = repo.query("fund_etf_daily", filters={"code": "159001"})
    assert len(df) == 2
    # 已有代码去重
    codes = repo.get_distinct_values("fund_etf_daily", "code")
    assert codes == ["159001", "159003"]


def test_get_max_date():
    """get_max_date：按 code / 无 code / 空表"""
    import tempfile as tf
    from sqlalchemy import create_engine, text
    from src.db.repository import DataRepository

    tmp = tf.NamedTemporaryFile(suffix=".db", delete=False); tmp.close()
    eng = create_engine(f"sqlite:///{tmp.name}")
    r = DataRepository(eng)
    with eng.begin() as c:
        c.execute(text('CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, '
                       '"date" TEXT, "code" TEXT)'))
        c.execute(text('INSERT INTO t ("date", "code") VALUES '
                       '("2026-06-22", "159001"), ("2026-08-03", "159001"), '
                       '("2026-07-01", "159003")'))
    assert r.get_max_date("t", "code", "159001") == "2026-08-03"
    assert r.get_max_date("t", "code", "159003") == "2026-07-01"
    assert r.get_max_date("t", "code", "999999") is None
    # 无 code 过滤 → 全表 max
    assert r.get_max_date("t") == "2026-08-03"
    assert r.get_max_date("missing_table", "code", "1") is None
    eng.dispose()
    if os.path.exists(tmp.name):
        os.remove(tmp.name)


def test_run_params_override_incremental_start():
    """run(params_override) 用用户代码 + 按该 code 实际数据做增量起点"""
    from datetime import date, timedelta
    import tempfile as tf
    import pandas as pd
    from sqlalchemy import create_engine, text
    from src.db.repository import DataRepository
    from src.core.dynamic_schema import DynamicSchemaManager
    from src.core.sync_engine import SyncEngine
    from src.utils.config import ConfigManager

    tmp = tf.NamedTemporaryFile(suffix=".db", delete=False); tmp.close()
    eng = create_engine(f"sqlite:///{tmp.name}")
    repo = DataRepository(eng); repo.create_tables()
    schema = DynamicSchemaManager(repo)
    with eng.begin() as c:
        c.execute(text('CREATE TABLE fund_etf_daily (id INTEGER PRIMARY KEY AUTOINCREMENT, '
                       '"date" TEXT, "close" TEXT, "code" TEXT, created_at TEXT)'))
        c.execute(text("INSERT INTO fund_etf_daily (date, close, code) VALUES "
                       "('2026-06-22', '1.0', '159001')"))

    class FakeFetcher:
        def __init__(self): self.calls = []
        def fetch(self, source_cfg, params):
            self.calls.append(dict(params))
            sd = date.fromisoformat(params['start_date'])
            return pd.DataFrame({"日期": [sd.isoformat()],
                                 "开盘": ["1"], "收盘": ["1"], "最高": ["1"],
                                 "最低": ["1"], "成交量": ["1"], "成交额": ["1"]})

    ff = FakeFetcher()
    eng_sync = SyncEngine(ff, repo, schema, ConfigManager())
    eng_sync.run('fund.etf_daily', 1, params_override={'symbol': '159001'})
    assert ff.calls[0]['symbol'] == '159001'
    # 增量起点 = 159001 实际 max(6-22) + 1 天 = 6-23
    assert ff.calls[0]['start_date'] == (date(2026, 6, 22) + timedelta(days=1)).isoformat()
    eng.dispose()
    if os.path.exists(tmp.name):
        os.remove(tmp.name)
