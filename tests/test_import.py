"""导入功能测试 — 文件读取、影响估算、按唯一键更新+新增

用临时 SQLite 库隔离，不碰生产数据。
运行: python -m pytest tests/test_import.py -v
"""

import os
import tempfile

import pandas as pd
import pytest
from sqlalchemy import create_engine, text

from src.db.repository import DataRepository
from src.importer.csv_importer import CsvImporter
from src.importer.excel_importer import ExcelImporter

DDL_CPI = (
    'CREATE TABLE macro_usa_cpi_yoy ('
    'id INTEGER PRIMARY KEY AUTOINCREMENT,'
    '"时间" TEXT, "数值" TEXT, "前值" TEXT, created_at TEXT)'
)


@pytest.fixture()
def repo():
    tmp = tempfile.mktemp(suffix=".db")
    eng = create_engine(f"sqlite:///{tmp}")
    r = DataRepository(eng)
    with eng.begin() as c:
        c.execute(text(DDL_CPI))
    yield r
    eng.dispose()
    if os.path.exists(tmp):
        os.remove(tmp)


# ---------------------------------------------------------------------------
# 导入器
# ---------------------------------------------------------------------------


def test_csv_importer_utf8_sig(repo):
    """utf-8-sig 编码 CSV 可读"""
    tmp = tempfile.mktemp(suffix=".csv")
    pd.DataFrame({"时间": ["2026-07-01"], "数值": [3.5]}).to_csv(
        tmp, index=False, encoding="utf-8-sig")
    df = CsvImporter().read(tmp)
    assert list(df.columns) == ["时间", "数值"]
    assert len(df) == 1
    os.remove(tmp)


def test_csv_importer_gbk(repo):
    """gbk 编码 CSV 可读（中文 Excel 导出常见）"""
    tmp = tempfile.mktemp(suffix=".csv")
    pd.DataFrame({"日期": ["2026-07-01"], "数值": ["3.5"]}).to_csv(
        tmp, index=False, encoding="gbk")
    df = CsvImporter().read(tmp)
    assert list(df.columns) == ["日期", "数值"]
    os.remove(tmp)


def test_excel_importer(repo):
    """xlsx 可读（openpyxl）"""
    tmp = tempfile.mktemp(suffix=".xlsx")
    pd.DataFrame({"时间": ["2026-07-01"], "数值": [3.5]}).to_excel(
        tmp, index=False, engine="openpyxl")
    df = ExcelImporter().read(tmp)
    assert len(df) == 1
    os.remove(tmp)


# ---------------------------------------------------------------------------
# 影响估算
# ---------------------------------------------------------------------------


def test_analyze_import_skip_and_insert(repo):
    """文件内重复 → skip；库中已有键 → update；新键 → insert"""
    with repo.engine.begin() as c:
        c.execute(text(
            "INSERT INTO macro_usa_cpi_yoy (\"时间\",\"数值\") VALUES "
            "('2026-06-01','3.2')"))
    df = pd.DataFrame({
        "时间": ["2026-07-01", "2026-07-01", "2026-06-01"],
        "数值": ["3.5", "3.6", "9.9"],   # 06-01 在库中已有 → 更新
    })
    a = repo.analyze_import("macro_usa_cpi_yoy", df, ["date"])
    assert a["total"] == 3
    assert a["to_insert"] == 1      # 07-01 唯一键
    assert a["to_update"] == 1      # 06-01 已存在
    assert a["to_skip"] == 1        # 文件内 07-01 重复
    assert a["unique_columns"] == ["时间"]  # date 解析到真实列


def test_analyze_import_missing_key_col(repo):
    """文件缺少唯一键列 → 提示 missing"""
    df = pd.DataFrame({"数值": ["3.5"]})   # 缺日期列
    a = repo.analyze_import("macro_usa_cpi_yoy", df, ["date"])
    assert a["missing_unique_cols"] == ["date"]


# ---------------------------------------------------------------------------
# 导入写入
# ---------------------------------------------------------------------------


def test_import_roundtrip_no_duplicate(repo):
    """同一文件导入两次：第二次全部更新，不新增，无重复"""
    df = pd.DataFrame({
        "时间": ["2026-07-01", "2026-06-01"],
        "数值": ["3.5", "3.2"],
    })
    repo.bulk_upsert("macro_usa_cpi_yoy", df, unique_columns=["date"])
    repo.bulk_upsert("macro_usa_cpi_yoy", df, unique_columns=["date"])

    assert repo.get_duplicate_count("macro_usa_cpi_yoy", ["date"]) == 0
    with repo.engine.connect() as c:
        n = c.execute(text("SELECT COUNT(*) FROM macro_usa_cpi_yoy")).scalar()
    assert n == 2


def test_import_updates_existing_value(repo):
    """重复键导入更新值而非新增"""
    with repo.engine.begin() as c:
        c.execute(text(
            "INSERT INTO macro_usa_cpi_yoy (\"时间\",\"数值\") VALUES "
            "('2026-07-01','3.5')"))
    df = pd.DataFrame({"时间": ["2026-07-01"], "数值": ["3.9"]})
    repo.bulk_upsert("macro_usa_cpi_yoy", df, unique_columns=["date"])
    with repo.engine.connect() as c:
        n = c.execute(text("SELECT COUNT(*) FROM macro_usa_cpi_yoy")).scalar()
        v = c.execute(text("SELECT \"数值\" FROM macro_usa_cpi_yoy")).scalar()
    assert n == 1
    assert v == "3.9"


def test_import_new_table_no_unique_key(repo):
    """无唯一键 / 新表：全部插入（表需已存在，导入对话框会先建表）"""
    from src.core.dynamic_schema import DynamicSchemaManager
    schema = DynamicSchemaManager(repo)
    schema.ensure_table_exists("brand_new_table", ["a", "b"])
    df = pd.DataFrame({"a": [1, 2], "b": ["x", "y"]})
    added = repo.bulk_upsert("brand_new_table", df)
    assert added == 2
