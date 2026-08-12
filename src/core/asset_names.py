# -*- coding: utf-8 -*-
"""资产名称工具 — 纯函数，无网络依赖（供 scripts_gen/sync_asset_names.py 与单测使用）

- 指数代码归一化：akshare/聚宽 指数代码 → 库内 index_daily.symbol 格式（sh/sz 前缀）
- 列名防御提取：akshare 接口列名随版本变动，按关键词健壮匹配「代码列/名称列」，
  匹配失败时由调用方跳过该资产类型并提示微调
"""


def normalize_joinquant_index_code(code) -> str | None:
    """聚宽指数代码 000001.XSHG → sh000001（库内 index_daily.symbol 格式）。

    XSHG→sh / XSHE→sz / XBJE→bj / XSN→sh；未知后缀或格式异常 → None（调用方 drop）。
    """
    if not isinstance(code, str):
        code = str(code)
    code = code.strip()
    if "." not in code:
        return None
    num, suffix = code.rsplit(".", 1)
    prefix = {"XSHG": "sh", "XSHE": "sz", "XBJE": "bj", "XSN": "sh"}.get(suffix.upper())
    if prefix is None or not num.isdigit():
        return None
    return prefix + num


def normalize_akshare_index_code(code) -> str | None:
    """akshare 指数代码（无交易所后缀）→ sh/sz 前缀；无法判断 → None。

    沪深惯例：000 开头→上证(sh)、399 开头→深证(sz)。实测 akshare
    index_stock_info() 732 个指数全部是裸代码，前缀分布 000(313)/399(419)，
    本函数可全覆盖。
    """
    if not isinstance(code, str):
        code = str(code)
    code = code.strip()
    if not code.isdigit():
        return None
    if code.startswith(("000", "880", "950")):
        return "sh" + code
    if code.startswith("399"):
        return "sz" + code
    return None


def normalize_index_code(code) -> str | None:
    """指数代码 → 库内 index_daily.symbol 格式（sh/sz 前缀）。

    兼容带交易所后缀（聚宽 index_stock_info 老版本 000001.XSHG）
    与裸代码（实测当前版本全部裸代码 000001/399001）。
    """
    return normalize_joinquant_index_code(code) or normalize_akshare_index_code(code)


def _pick_col(columns, keywords):
    """按关键词列表命中列名（大小写不敏感）；返回第一个命中或 None"""
    for c in columns:
        cl = str(c).lower()
        if any(kw in cl for kw in keywords):
            return c
    return None


def pick_fund_name_columns(columns) -> tuple:
    """基金名称表列名：代码列(基金代码) + 名称列(基金简称)"""
    code_col = _pick_col(columns, ("基金代码", "code", "代码"))
    name_col = _pick_col(columns, ("基金简称", "简称", "名称", "name"))
    return code_col, name_col


def pick_stock_name_columns(columns) -> tuple:
    """A股股票名称表列名：代码列(code/证券代码) + 名称列(name/证券简称)"""
    code_col = _pick_col(columns, ("code", "证券代码", "代码"))
    name_col = _pick_col(columns, ("name", "证券简称", "名称", "简称"))
    return code_col, name_col


def pick_index_name_columns(columns) -> tuple:
    """指数名称表列名：代码列(index_code) + 名称列(display_name)"""
    code_col = _pick_col(columns, ("index_code", "代码", "code"))
    name_col = _pick_col(columns, ("display_name", "名称", "name"))
    return code_col, name_col
