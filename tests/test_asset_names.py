# -*- coding: utf-8 -*-
"""资产名称工具纯函数测试(src/core/asset_names.py, 无网络)

覆盖指数代码归一化(聚宽后缀 / akshare 裸代码)与 akshare 名称表列名防御提取。
"""

import pytest

from src.core.asset_names import (
    normalize_akshare_index_code,
    normalize_index_code,
    normalize_joinquant_index_code,
    pick_fund_name_columns,
    pick_index_name_columns,
    pick_stock_name_columns,
)


# ---------------------------------------------------------------------------
# 聚宽后缀 → 库内格式
# ---------------------------------------------------------------------------


def test_joinquant_suffix_mapping():
    assert normalize_joinquant_index_code("000001.XSHG") == "sh000001"
    assert normalize_joinquant_index_code("399001.XSHE") == "sz399001"
    assert normalize_joinquant_index_code("430001.XBJE") == "bj430001"
    assert normalize_joinquant_index_code("000001.XSN") == "sh000001"


def test_joinquant_unknown_suffix_none():
    assert normalize_joinquant_index_code("000001.XNYS") is None
    assert normalize_joinquant_index_code("abc.XSHG") is None


def test_joinquant_no_dot_none():
    assert normalize_joinquant_index_code("000001") is None


# ---------------------------------------------------------------------------
# akshare 裸代码(实测 index_stock_info 返回裸代码)
# ---------------------------------------------------------------------------


def test_akshare_bare_code_prefix():
    assert normalize_akshare_index_code("000001") == "sh000001"
    assert normalize_akshare_index_code("880001") == "sh880001"
    assert normalize_akshare_index_code("950001") == "sh950001"
    assert normalize_akshare_index_code("399001") == "sz399001"


def test_akshare_unrecognized_none():
    assert normalize_akshare_index_code("600000") is None
    assert normalize_akshare_index_code("123456") is None
    assert normalize_akshare_index_code("abc") is None
    assert normalize_akshare_index_code("") is None


def test_akshare_int_input():
    assert normalize_akshare_index_code(399001) == "sz399001"


# ---------------------------------------------------------------------------
# 组合(带后缀与裸代码都兼容)
# ---------------------------------------------------------------------------


def test_normalize_index_code_both():
    assert normalize_index_code("000001.XSHG") == "sh000001"
    assert normalize_index_code("000001") == "sh000001"
    assert normalize_index_code("399001") == "sz399001"
    assert normalize_index_code("600000") is None


# ---------------------------------------------------------------------------
# 名称表列名防御提取
# ---------------------------------------------------------------------------


def test_pick_fund_name_columns():
    assert pick_fund_name_columns(["基金代码", "基金简称", "日期"]) == ("基金代码", "基金简称")
    assert pick_fund_name_columns(["code", "name"]) == ("code", "name")


def test_pick_stock_name_columns():
    assert pick_stock_name_columns(["证券代码", "证券简称"]) == ("证券代码", "证券简称")
    assert pick_stock_name_columns(["code", "name"]) == ("code", "name")


def test_pick_index_name_columns():
    assert pick_index_name_columns(["index_code", "display_name"]) == ("index_code", "display_name")
    assert pick_index_name_columns(["代码", "名称"]) == ("代码", "名称")


def test_pick_cols_missing_return_none():
    assert pick_fund_name_columns(["日期", "收盘"]) == (None, None)
