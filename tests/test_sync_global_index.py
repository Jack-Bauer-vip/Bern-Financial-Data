# -*- coding: utf-8 -*-
"""跨境指数同步脚本测试(scripts_gen/sync_global_index.py)

- 纯函数：build_sync_plan 三路路由(港股 symbol=code / 全球 symbol=中文名+code / em 待补跳过)
         + asset_name_rows 名称行全量(含 em 待补)。
- 集成：FakeFetcher + SyncEngine.run 验证双参注入 —— index.global 落库 symbol=拉丁码(CAC) 而非中文名；
        index.hk 落库 symbol=HSI；meta_asset_info 名称可回读。
"""

import os
import tempfile

import pandas as pd
import pytest
from sqlalchemy import create_engine

from src.core.dynamic_schema import DynamicSchemaManager
from src.core.fetcher_registry import FetcherRegistry
from src.core.sync_engine import SyncEngine
from src.db.repository import DataRepository
from src.utils.config import ConfigManager


def _import_script():
    import scripts_gen.sync_global_index as sgi
    return sgi


# ---------------------------------------------------------------------------
# 纯函数：同步计划路由
# ---------------------------------------------------------------------------


def test_plan_hk_symbol_is_code():
    """港股：symbol 即库内 code，走 index.hk 源"""
    sgi = _import_script()
    plan = sgi.build_sync_plan({
        "HSI": {"name": "恒生指数", "api_function": "stock_hk_index_daily_sina"},
    })
    assert plan == [{"code": "HSI", "source_key": "index.hk",
                     "params": {"symbol": "HSI"}, "fetchable": True}]


def test_plan_global_dual_param():
    """全球：symbol=中文名(给 API) + code=拉丁码(写库)"""
    sgi = _import_script()
    plan = sgi.build_sync_plan({
        "CAC": {"name": "法国CAC40指数", "api_function": "index_global_hist_sina"},
    })
    assert plan == [{"code": "CAC", "source_key": "index.global",
                     "params": {"symbol": "法CAC40指数", "code": "CAC"},
                     "fetchable": True}]


def test_plan_em_blocked():
    """em(东财)被墙：fetchable=False，仅分类待补数据"""
    sgi = _import_script()
    plan = sgi.build_sync_plan({
        "SPX": {"name": "标普500指数", "api_function": "index_global_hist_em"},
    })
    assert plan[0]["code"] == "SPX"
    assert plan[0]["fetchable"] is False


def test_plan_unknown_symbol_not_fetchable():
    """global 段有 code 但不在新浪映射表 → 不拉取"""
    sgi = _import_script()
    plan = sgi.build_sync_plan({
        "XYZ": {"name": "未知指数", "api_function": "index_global_hist_sina"},
    })
    assert plan[0]["code"] == "XYZ"
    assert plan[0]["fetchable"] is False


def test_asset_name_rows_all_codes():
    """名称行全量（含 em 待补），asset_type=index"""
    sgi = _import_script()
    rows = sgi.asset_name_rows({
        "HSI": {"name": "恒生指数"},
        "CAC": {"name": "法国CAC40指数"},
        "SPX": {"name": "标普500指数"},
    })
    assert len(rows) == 3
    assert rows[0] == {"code": "HSI", "name": "恒生指数", "asset_type": "index"}
    assert {r["code"] for r in rows} == {"HSI", "CAC", "SPX"}


# ---------------------------------------------------------------------------
# 集成：双参注入 + 落库
# ---------------------------------------------------------------------------


class FakeFetcher:
    """只实现 akshare 路径的 fetch —— 按 api_function 返回规范列假数据"""
    def fetch(self, source_cfg, params=None):
        fn = source_cfg.get("api_function", "")
        if fn == "index_global_hist_sina":
            df = pd.DataFrame({
                "date": ["2026-08-10", "2026-08-11"],
                "open": ["8000.0", "8100.0"],
                "high": ["8200.0", "8300.0"],
                "low": ["7900.0", "8050.0"],
                "close": ["8150.0", "8250.0"],
                "volume": ["100.0", "120.0"],
            })
        elif fn == "stock_hk_index_daily_sina":
            df = pd.DataFrame({
                "date": ["2026-08-10", "2026-08-11"],
                "open": ["25500.0", "25800.0"],
                "high": ["25900.0", "26000.0"],
                "low": ["25400.0", "25600.0"],
                "close": ["25668.0", "25937.0"],
                "volume": ["12717499785", "17567152874"],
                "amount": ["259685732142", "240279657390"],
            })
        else:
            df = pd.DataFrame()
        # 记录收到的参数（供断言 symbol/code 是否分离）
        df.attrs["last_params"] = dict(params or {})
        return df


@pytest.fixture()
def engine_env(tmp_path):
    """临时库 + 真实 catalog(FetcherRegistry) + FakeFetcher + SyncEngine"""
    db = tempfile.mktemp(suffix=".db", dir=str(tmp_path))
    eng = create_engine(f"sqlite:///{db}")
    repo = DataRepository(eng)
    repo.create_tables()
    schema = DynamicSchemaManager(repo)
    config = ConfigManager()
    fetcher = FakeFetcher()
    engine = SyncEngine(fetcher, repo, schema, config)
    yield repo, schema, engine, fetcher
    eng.dispose()
    if os.path.exists(db):
        os.remove(db)


def test_run_global_symbol_column_is_code_not_chinese(engine_env):
    """关键回归：落库 code 列必须是 CAC，绝不能是「法国CAC40指数」"""
    repo, schema, engine, fetcher = engine_env
    n = engine.run("index.global", history_years=30,
                   params_override={"symbol": "法国CAC40指数", "code": "CAC"})
    assert n == 2
    df = repo.query("index_daily", filters={"symbol": "CAC"})
    assert len(df) == 2
    assert set(df["symbol"]) == {"CAC"}
    assert not (df["symbol"].astype(str).str.contains("CAC40|指数").any())


def test_run_hk_injects_hsi(engine_env):
    """index.hk：symbol=HSI 直接落库"""
    repo, schema, engine, fetcher = engine_env
    n = engine.run("index.hk", history_years=30, params_override={"symbol": "HSI"})
    assert n == 2
    df = repo.query("index_daily", filters={"symbol": "HSI"})
    assert len(df) == 2
    assert df.iloc[0]["amount"] == "259685732142"  # amount 列保留


def test_script_names_write_and_read(engine_env):
    """asset_name_rows 写 meta_asset_info → get_asset_names 可回读"""
    repo, schema, engine, fetcher = engine_env
    sgi = _import_script()
    schema.ensure_table_exists("meta_asset_info", ["code", "name", "asset_type"])
    repo.ensure_unique_index("meta_asset_info", ["asset_type", "code"], dedupe=True)
    rows = sgi.asset_name_rows({
        "HSI": {"name": "恒生指数"},
        "SPX": {"name": "标普500指数"},  # em 待补也写名称
    })
    import pandas as pd
    df = pd.DataFrame(rows, columns=["code", "name", "asset_type"])
    repo.bulk_upsert("meta_asset_info", df, unique_columns=["asset_type", "code"])
    names = repo.get_asset_names("index")
    assert names["HSI"] == "恒生指数"
    assert names["SPX"] == "标普500指数"
