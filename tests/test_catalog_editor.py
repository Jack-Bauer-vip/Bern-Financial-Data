"""数据分类管理测试 — catalog_io 纯逻辑（不实例化 Qt）"""

import os

import pytest
import yaml

from src.utils.config import ConfigManager
from src.utils import catalog_io


@pytest.fixture()
def cm(tmp_path, monkeypatch):
    """把 ConfigManager 的 catalog_path 指到临时目录，避免污染真实配置"""
    cfg = ConfigManager()  # 单例
    tmp_catalog = tmp_path / "data_catalog.yaml"
    monkeypatch.setattr(cfg, "catalog_path", tmp_catalog)
    return cfg


def _sample_catalog():
    """构造一个含分类和数据源的样例分类树"""
    return {
        "categories": [
            {
                "name": "全球宏观数据",
                "children": [
                    {
                        "name": "🇺🇸 美国经济",
                        "source_key": "macro.us",
                        "children": [
                            {"name": "美国CPI年率",
                             "source_key": "macro.us.cpi_yoy",
                             "api_source": "akshare",
                             "api_function": "macro_usa_cpi_yoy",
                             "table_name": "macro_usa_cpi_yoy",
                             "schedule_cron": "0 22 * * 1-5"},
                        ],
                    },
                ],
            },
            {"name": "股票数据", "children": []},
        ],
    }


# ---------------------------------------------------------------------------
# 读写往返
# ---------------------------------------------------------------------------


def test_yaml_roundtrip(cm):
    catalog = _sample_catalog()
    assert catalog_io.save_catalog(catalog) is True
    reloaded = catalog_io.load_catalog()
    assert reloaded == catalog


def test_save_creates_backup(cm):
    catalog = _sample_catalog()
    assert catalog_io.save_catalog(catalog) is True
    # 改一次再存 → .bak 存在且是第一次内容
    catalog["categories"][0]["name"] = "改名"
    assert catalog_io.save_catalog(catalog) is True
    bak_path = str(cm.catalog_path) + ".bak"
    assert os.path.exists(bak_path)
    with open(bak_path, "r", encoding="utf-8") as f:
        bak_content = yaml.safe_load(f)
    assert bak_content["categories"][0]["name"] == "全球宏观数据"


def test_save_preserves_header(cm):
    """保存后文件头注释保留"""
    assert catalog_io.save_catalog(_sample_catalog()) is True
    with open(cm.catalog_path, "r", encoding="utf-8") as f:
        head = f.read()[:60]
    assert "# Bern_Financial_Data" in head


def test_save_no_tmp_leftover(cm):
    assert catalog_io.save_catalog(_sample_catalog()) is True
    assert not os.path.exists(str(cm.catalog_path) + ".tmp")


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


def test_validate_ok(cm):
    assert catalog_io.validate_catalog(_sample_catalog()) is None


def test_validate_duplicate_source_key(cm):
    catalog = _sample_catalog()
    # 两个数据源用同一 source_key
    catalog["categories"][0]["children"][0]["children"].append(
        {"name": "重复源", "source_key": "macro.us.cpi_yoy",
         "api_function": "x", "table_name": "x"})
    errors = catalog_io.validate_catalog_errors(catalog)
    assert any("source_key" in e and "重复" in e for e in errors)
    # save 应拒绝
    assert catalog_io.save_catalog(catalog) is False


def test_validate_missing_required_fields(cm):
    """数据源缺 table_name / source_key → 报错"""
    catalog = {
        "categories": [
            {"name": "分类", "children": [
                {"name": "缺table源", "source_key": "a.b",
                 "api_function": "func"}   # 缺 table_name
            ]},
        ],
    }
    errors = catalog_io.validate_catalog_errors(catalog)
    assert any("table_name" in e for e in errors)


def test_validate_neither_cat_nor_source(cm):
    """既无 children 也无 api_function → 报"不是分类也不是数据源" """
    catalog = {"categories": [{"name": "孤立节点"}]}
    errors = catalog_io.validate_catalog_errors(catalog)
    assert any("既不是分类也不是数据源" in e for e in errors)


def test_validate_bad_cron(cm):
    catalog = _sample_catalog()
    catalog["categories"][0]["children"][0]["children"][0]["schedule_cron"] = "bad"
    errors = catalog_io.validate_catalog_errors(catalog)
    assert any("schedule_cron" in e for e in errors)


def test_validate_missing_categories(cm):
    assert catalog_io.validate_catalog({}) is not None


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def test_collect_source_keys(cm):
    keys = catalog_io.collect_source_keys(_sample_catalog()["categories"])
    assert "macro.us" in keys
    assert "macro.us.cpi_yoy" in keys


def test_invalid_save_does_not_overwrite(cm):
    """校验失败的保存不应覆盖原文件"""
    catalog = _sample_catalog()
    assert catalog_io.save_catalog(catalog) is True
    original = catalog_io.load_catalog()

    bad = _sample_catalog()
    bad["categories"][0]["children"][0]["children"].append(
        {"name": "dup", "source_key": "macro.us.cpi_yoy",
         "api_function": "x", "table_name": "x"})
    assert catalog_io.save_catalog(bad) is False
    # 原文件未变
    assert catalog_io.load_catalog() == original
