"""数据抓取引擎测试 — HTML 解析、入库去重、配置加载"""

import os
import tempfile

import pandas as pd
import pytest
from sqlalchemy import create_engine, text

from src.db.repository import DataRepository
from src.scraper.engine import ScrapeEngine, ScraperError

HTML_SAMPLE = """
<html><body>
<table id="rates">
<tr><th>日期</th><th>利率</th><th>涨跌</th></tr>
<tr><td>2026-07-01</td><td>3.45</td><td>+0.05</td></tr>
<tr><td>2026-07-02</td><td>3.50</td><td>+0.05</td></tr>
<tr><td>2026-07-03</td><td>3.48</td><td>-0.02</td></tr>
</table>
</body></html>
"""

RULE = {
    "name": "利率表",
    "rows": "table tr:not(:first-child)",
    "columns": [
        {"name": "日期", "index": 0},
        {"name": "利率", "index": 1},
        {"name": "涨跌", "index": 2},
    ],
    "date_column": "日期",
    "table_name": "scrape_rates",
}


@pytest.fixture()
def repo():
    """临时库"""
    tmp = tempfile.mktemp(suffix=".db")
    eng = create_engine(f"sqlite:///{tmp}")
    yield DataRepository(eng)
    eng.dispose()
    if os.path.exists(tmp):
        os.remove(tmp)


def test_parse_html_table(repo):
    """HTML 表格解析为 DataFrame"""
    df = ScrapeEngine(repo).parse_html(HTML_SAMPLE, RULE)
    assert len(df) == 3
    assert list(df.columns) == ["日期", "利率", "涨跌"]
    assert df.iloc[0]["日期"] == "2026-07-01"
    assert df.iloc[0]["利率"] == "3.45"


def test_parse_empty_no_match(repo):
    """选择器无匹配 → 空 DataFrame"""
    rule = dict(RULE, rows="table tr:not(:first-child) tr")
    df = ScrapeEngine(repo).parse_html(HTML_SAMPLE, rule)
    assert df.empty


def test_scrape_save_dedup(repo):
    """抓取结果入库 + 重复抓取按日期去重"""
    engine = ScrapeEngine(repo)
    df = engine.parse_html(HTML_SAMPLE, RULE)
    added = engine.save(RULE, df)
    assert added == 3
    # 重复保存 → 更新不新增（返回值是真新增行数，2026-08-12 修正语义）
    added2 = engine.save(RULE, df)
    assert added2 == 0
    with repo.engine.connect() as c:
        n = c.execute(text("SELECT COUNT(*) FROM scrape_rates")).scalar()
    assert n == 3  # 去重后仍 3 行


def test_save_empty_returns_zero(repo):
    """空数据入库返回 0"""
    assert ScrapeEngine(repo).save(RULE, pd.DataFrame()) == 0


def test_load_rules_from_config(repo):
    """从 scrapers.yaml 加载规则"""
    rules = ScrapeEngine(repo).load_rules()
    assert isinstance(rules, list)
    # 至少能加载配置里的规则
    names = [r.get("name") for r in rules]
    assert "示例数据表" in names


def test_get_rule_by_name(repo):
    engine = ScrapeEngine(repo)
    rule = engine.get_rule("示例数据表")
    assert rule is not None
    assert rule.get("enabled") is False


def test_run_disabled_rule_skips(repo):
    """禁用规则 → run_by_name 返回 0"""
    engine = ScrapeEngine(repo)
    # 示例数据表 enabled=false
    assert engine.run_by_name("示例数据表") == 0


def test_run_unknown_rule_raises(repo):
    engine = ScrapeEngine(repo)
    with pytest.raises(ScraperError):
        engine.run_by_name("不存在的规则")
