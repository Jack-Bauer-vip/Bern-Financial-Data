# -*- coding: utf-8 -*-
"""对外 SKILL.md 契约一致性测试

契约 = skills/bern-financial-data/SKILL.md。防「契约文档」与「API 代码」漂移:
- frontmatter 四字段齐全、description 含「何时不用」激活边界(仿 a-stock-data)
- 文档中出现的每个 /api/v1/... 路径(模板如 {table_name} 或示例实例如 fund_etf_daily)
  都必须匹配到 src/api/routes.py 的 api_router 注册路由(路径模式, {param}→[^/]+)
- scripts_gen/install_skill.py 幂等复制契约到目标目录
- 契约文档化的 ?code= 过滤参数真实生效(仅含 code 列的表)
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKILL_FILE = ROOT / "skills" / "bern-financial-data" / "SKILL.md"


def _skill_text() -> str:
    return SKILL_FILE.read_text(encoding="utf-8")


def _doc_paths(text: str) -> set[str]:
    """提取文档中全部 /api/v1/... 路径(去重)。停在 ? / " 等分隔符前。"""
    return set(re.findall(r"/api/v1/[A-Za-z0-9_{}/.]+", text))


def _registered_patterns() -> list[re.Pattern]:
    """api_router 注册路由 → 加 /api/v1 前缀并转路径模式(模板参数→[^/]+)"""
    from src.api.routes import router

    pats = []
    for r in router.routes:
        p = getattr(r, "path", None)
        if not p:
            continue
        full = f"/api/v1{p}"
        pats.append(re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", full) + "$"))
    return pats


# ---------------------------------------------------------------------------
# frontmatter
# ---------------------------------------------------------------------------


def test_skill_file_exists():
    assert SKILL_FILE.exists(), f"缺少对外契约源文件: {SKILL_FILE}"


def test_frontmatter_fields():
    text = _skill_text()
    assert text.startswith("---\n"), "SKILL.md 必须以 frontmatter 开头"
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert m, "frontmatter 未闭合"
    fm = m.group(1)
    for field in ("name", "description", "origin", "version"):
        assert re.search(rf"^{field}:", fm, re.M), f"frontmatter 缺字段 {field}"
    assert re.search(r"^name: bern-financial-data$", fm, re.M), \
        "frontmatter name 应为 bern-financial-data"
    assert re.search(r"^origin: custom$", fm, re.M), "frontmatter origin 应为 custom"


def test_description_has_activation_boundary():
    """description 必须写清「何时不用」激活边界(避免无谓加载)"""
    text = _skill_text()
    m = re.search(r"^description: (.+)$", text, re.M)
    assert m, "缺 description"
    assert "不要加载" in m.group(1), \
        "description 需声明「无需查询本地数据库的话题不要加载本 skill」"


# ---------------------------------------------------------------------------
# 端点一致性(契约 ⊇ 路由 的方向:文档化路径必须真实存在)
# ---------------------------------------------------------------------------


def test_documented_endpoints_registered():
    patterns = _registered_patterns()
    assert patterns, "api_router 无路由"
    doc = _doc_paths(_skill_text())
    assert doc, "SKILL.md 未文档化任何 /api/v1 路径"

    unmapped = sorted(p for p in doc if not any(pat.match(p) for pat in patterns))
    assert not unmapped, \
        f"SKILL.md 文档化了 api_router 不存在的端点: {unmapped}"


def test_every_registered_endpoint_documented():
    """反方向: 业务端点必须都在契约里(防止新端点漏文档)"""
    from src.api.routes import router

    doc = _doc_paths(_skill_text())
    missing = []
    for r in router.routes:
        p = getattr(r, "path", None)
        if not p:
            continue
        full = f"/api/v1{p}"
        pat = re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", full) + "$")
        if not any(pat.match(d) for d in doc):
            missing.append(full)
    assert not missing, f"以下端点已注册但 SKILL.md 未文档化: {missing}"


# ---------------------------------------------------------------------------
# 安装器
# ---------------------------------------------------------------------------


def test_installer_copies_to_target(tmp_path):
    from scripts_gen.install_skill import install

    dest = install(tmp_path)
    assert dest.name == "SKILL.md"
    assert dest.parent.name == "bern-financial-data"
    assert dest.read_bytes() == SKILL_FILE.read_bytes()


def test_installer_idempotent_same_content(tmp_path):
    from scripts_gen.install_skill import install

    d1 = install(tmp_path)
    d2 = install(tmp_path)  # 内容一致 → 跳过
    assert d1 == d2 and d2.exists()


def test_installer_refuses_different_without_force(tmp_path):
    from scripts_gen.install_skill import install

    dest = install(tmp_path)
    dest.write_text("旧版本", encoding="utf-8")
    d2 = install(tmp_path)  # 内容不同且无 --force → 不覆盖
    assert d2.read_text(encoding="utf-8") == "旧版本"


def test_installer_force_overwrites(tmp_path):
    from scripts_gen.install_skill import install

    dest = install(tmp_path)
    dest.write_text("旧版本", encoding="utf-8")
    install(tmp_path, force=True)
    assert dest.read_bytes() == SKILL_FILE.read_bytes()


# ---------------------------------------------------------------------------
# 契约文档化的 ?code= 过滤真实生效
# ---------------------------------------------------------------------------


@pytest.fixture()
def code_filter_env(tmp_path):
    """临时库: fund_etf_daily 两张表、两 code、带 code 列; index_daily 无 code 列"""
    import os
    import tempfile

    import pandas as pd
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine, text

    from src.api.server import FastAPIServer
    from src.db.repository import DataRepository

    db = tempfile.mktemp(suffix=".db")
    eng = create_engine(f"sqlite:///{db}")
    repo = DataRepository(eng)
    repo.create_tables()
    with eng.begin() as c:
        c.execute(text(
            'CREATE TABLE fund_etf_daily (id INTEGER PRIMARY KEY AUTOINCREMENT, '
            '"date" TEXT, close TEXT, code TEXT, created_at TEXT)'))
        c.execute(text(
            'CREATE TABLE index_daily (id INTEGER PRIMARY KEY AUTOINCREMENT, '
            '"date" TEXT, close TEXT, symbol TEXT, created_at TEXT)'))
        for code in ("159915", "510300"):
            for d, close in (("2026-08-01", "10.0"), ("2026-08-02", "10.5")):
                c.execute(text(
                    'INSERT INTO fund_etf_daily (date, close, code) VALUES (:d, :c, :code)'),
                    {"d": d, "c": close, "code": code})
    server = FastAPIServer(repo=repo)
    yield TestClient(server.app)
    eng.dispose()
    if os.path.exists(db):
        os.remove(db)


def test_data_code_filter_returns_only_that_code(code_filter_env):
    r = code_filter_env.get("/api/v1/data/fund_etf_daily?code=159915&limit=100")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    assert {rec["code"] for rec in body["data"]} == {"159915"}


def test_data_code_filter_limit_takes_latest(code_filter_env):
    """code 过滤 + LIMIT 必须取最新行（默认按日期倒序），不能截断到旧日期"""
    r = code_filter_env.get("/api/v1/data/fund_etf_daily?code=159915&limit=1")
    assert r.status_code == 200
    body = r.json()
    assert len(body["data"]) == 1
    assert body["data"][0]["date"] == "2026-08-02"


def test_data_code_filter_on_table_without_code_col_422(code_filter_env):
    r = code_filter_env.get("/api/v1/data/index_daily?code=159915")
    assert r.status_code == 422
    assert "code" in r.json()["detail"]
