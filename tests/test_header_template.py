"""表头记忆模板测试 — 统一表头 → 确定性 O(1) 路由，不依赖规则评分与 AI"""

import csv
import os
import tempfile

import pandas as pd
import pytest
from sqlalchemy import create_engine, text

from src.db.repository import DataRepository
from src.importer.matcher import match_table
from src.importer.header_template import (
    HeaderTemplateStore,
    build_index,
    header_name_signature,
)
from src.utils.config import ConfigManager


class _TestConfig:
    """包装真实 ConfigManager，仅把 import.template_file 指向临时目录"""

    def __init__(self, tmp_path):
        self._real = ConfigManager()
        self.root_dir = self._real.root_dir
        self._tmp = tmp_path

    def get(self, key, default=None):
        if key == "import.template_file":
            return str(self._tmp / "templates.json")
        return self._real.get(key, default)

    def get_env(self, key, default=None):
        return self._real.get_env(key, default)

    @property
    def catalog(self):
        return self._real.catalog


class _RaisingAI:
    """一旦被调用即测试失败——证明模板路径完全绕过了 AI"""

    def is_available(self):
        return True

    def identify_table(self, *args, **kwargs):
        raise AssertionError("表头模板命中时不应调用 AI")


@pytest.fixture()
def repo():
    """临时库：index_daily（英文行情）+ 两张同结构中文宏观表（歧义场景）"""
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
        c.execute(text(
            'CREATE TABLE macro_china_cpi_monthly ('
            'id INTEGER PRIMARY KEY AUTOINCREMENT, "商品" TEXT, "日期" TEXT, '
            '"今值" TEXT, "预测值" TEXT, "前值" TEXT, created_at TEXT)'))
    yield r
    eng.dispose()
    if os.path.exists(tmp):
        os.remove(tmp)


MACRO_COLS = ["商品", "日期", "今值", "预测值", "前值"]


def _macro_df():
    return pd.DataFrame({"商品": ["中国CPI"], "日期": ["2026-07-01"],
                         "今值": ["1.2"], "预测值": ["1.1"], "前值": ["1.0"]})


# ---------------------------------------------------------------------------
# 表头签名
# ---------------------------------------------------------------------------


def test_header_name_signature_order_insensitive():
    """签名与列顺序无关，且归一化大小写/空白"""
    assert header_name_signature(["日期", "今值"]) == \
        header_name_signature(["今值", "日期"])
    assert header_name_signature(["Date", "OPEN"]) == \
        header_name_signature(["date", "open"])
    assert header_name_signature([" date ", "Date"]) == ("date",)


# ---------------------------------------------------------------------------
# 存储：保存 / 读取 / 覆盖 / 清除
# ---------------------------------------------------------------------------


def test_store_roundtrip_and_clear(tmp_path):
    config = _TestConfig(tmp_path)
    path = tmp_path / "templates.json"
    store = HeaderTemplateStore(config, path)

    store.learn(["日期", "今值"], "macro_china_cpi_yearly",
                unique_key=["日期"], mapping={"日期": "日期"}, source="user")

    reloaded = HeaderTemplateStore(config, path).load_learned()
    sig = header_name_signature(["今值", "日期"])  # 顺序无关
    assert reloaded[sig].table_name == "macro_china_cpi_yearly"
    assert reloaded[sig].source == "user"
    assert reloaded[sig].unique_key == ["日期"]

    # 同签名覆盖
    store.learn(["日期", "今值"], "macro_china_cpi_monthly", source="user")
    assert HeaderTemplateStore(config, path).load_learned()[sig].table_name == \
        "macro_china_cpi_monthly"

    # 清除
    store.clear()
    assert not path.exists()
    assert HeaderTemplateStore(config, path).load_learned() == {}


# ---------------------------------------------------------------------------
# 索引构建：learned 优先；无 learned 时同结构宏观表保持歧义
# ---------------------------------------------------------------------------


def test_build_index_learned_overrides_ambiguous_seeds(repo, tmp_path):
    config = _TestConfig(tmp_path)
    HeaderTemplateStore(config, tmp_path / "templates.json").learn(
        MACRO_COLS, "macro_china_cpi_yearly", source="user")

    index = build_index(config, repo)
    assert len(index[header_name_signature(MACRO_COLS)]) == 1
    assert index[header_name_signature(MACRO_COLS)][0].table_name == \
        "macro_china_cpi_yearly"


def test_build_index_db_seed_deterministic(repo, tmp_path):
    """index_daily 列集合唯一 → 单候选确定性路由（无需 learned）"""
    config = _TestConfig(tmp_path)
    index = build_index(config, repo)
    sig = header_name_signature(["date", "open", "high", "low", "close", "volume"])
    assert sig in index
    assert [t.table_name for t in index[sig]] == ["index_daily"]


def test_build_index_ambiguous_without_learned(repo, tmp_path):
    """两张同结构宏观表 → 同一签名多候选，保持歧义（交给规则/AI）"""
    config = _TestConfig(tmp_path)
    index = build_index(config, repo)
    cands = index.get(header_name_signature(MACRO_COLS))
    assert cands is not None and len(cands) == 2


# ---------------------------------------------------------------------------
# match_table 路由
# ---------------------------------------------------------------------------


def test_match_table_template_hit_no_ai(repo, tmp_path):
    """learned 命中 → method=template，AI 不被调用"""
    config = _TestConfig(tmp_path)
    HeaderTemplateStore(config, tmp_path / "templates.json").learn(
        MACRO_COLS, "macro_china_cpi_yearly", source="user")
    index = build_index(config, repo)

    res = match_table(_macro_df(), repo, ai_client=_RaisingAI(), templates=index)
    assert res.table_name == "macro_china_cpi_yearly"
    assert res.method == "template"
    assert res.confidence == 1.0


def test_match_table_db_seed_hit(repo, tmp_path):
    """无 learned，但文件表头 == index_daily 自身列 → db 种子确定性路由"""
    config = _TestConfig(tmp_path)
    index = build_index(config, repo)
    df = pd.DataFrame({"date": ["2026-01-01"], "open": ["1"], "high": ["2"],
                       "low": ["0.5"], "close": ["1.5"], "volume": ["100"]})
    res = match_table(df, repo, ai_client=None, templates=index)
    assert res.table_name == "index_daily"
    assert res.method == "template"


def test_match_table_ambiguous_falls_through(repo, tmp_path):
    """无 learned、同结构宏观表 → 模板不命中，走规则低置信回退（不调 AI）"""
    config = _TestConfig(tmp_path)
    index = build_index(config, repo)
    res = match_table(_macro_df(), repo, ai_client=None, templates=index)
    assert res.method != "template"
    assert res.table_name.startswith("macro_")


# ---------------------------------------------------------------------------
# worker 集成：学习后的同表头批量文件走模板，不调 AI
# ---------------------------------------------------------------------------


def test_worker_uses_template_no_ai(repo, tmp_path):
    from src.gui.dialogs.import_dialog import BatchIdentifyWorker

    config = _TestConfig(tmp_path)
    store = HeaderTemplateStore(config, tmp_path / "templates.json")
    store.learn(MACRO_COLS, "macro_china_cpi_yearly", source="user")

    tmp_dir = tempfile.mkdtemp()
    csv_path = os.path.join(tmp_dir, "cpi.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(MACRO_COLS)
        w.writerow(["中国CPI", "2026-07-01", "1.2", "1.1", "1.0"])

    results = {}
    worker = BatchIdentifyWorker(
        repo, None, _RaisingAI(), [csv_path], template_store=store)
    worker.file_done.connect(lambda p, item: results.update({p: item}))
    worker.run()

    item = results[csv_path]
    assert item.table_name == "macro_china_cpi_yearly"
    assert item.method == "template"
    assert item.status.startswith("就绪")


def test_worker_batch_same_header_matches_once(repo, tmp_path):
    """同表头批量：learned 后每个文件都命中模板（全部 method=template）"""
    from src.gui.dialogs.import_dialog import BatchIdentifyWorker

    config = _TestConfig(tmp_path)
    store = HeaderTemplateStore(config, tmp_path / "templates.json")
    store.learn(MACRO_COLS, "macro_china_cpi_yearly", source="user")

    tmp_dir = tempfile.mkdtemp()
    paths = []
    for i in range(3):
        p = os.path.join(tmp_dir, f"cpi_{i}.csv")
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(MACRO_COLS)
            w.writerow(["中国CPI", f"2026-07-0{i+1}", "1.2", "1.1", "1.0"])
        paths.append(p)

    results = {}
    worker = BatchIdentifyWorker(
        repo, None, _RaisingAI(), paths, template_store=store)
    worker.file_done.connect(lambda p, item: results.update({p: item}))
    worker.run()

    assert len(results) == 3
    for item in results.values():
        assert item.method == "template"
        assert item.table_name == "macro_china_cpi_yearly"
