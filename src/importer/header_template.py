"""表头记忆模板 — 持久化「统一表头 → 目标表」，让同意表头的导入走确定性 O(1) 路由

背景：导入数据通常使用固定的同意表头。目标表识别目前靠规则评分，规则拿不准时
调本地 AI（5-20 秒/次）。同一表头反复导入时，规则与 AI 都是重复劳动。

本模块提供「表头记忆」作为 AI 识别的确定性替代：
- 表头签名 = 归一化列名集合（顺序无关）——「同意表头」约定的就是列名集合
- 模板 = {签名 → 目标表 + 唯一键 + 列映射}，learned 模板持久化到 JSON
- 种子：data_catalog.yaml 中声明了 column_map 的数据源、以及库里已存在的表
  （每张表用自身列名作为期望表头）
- 学习：用户手动改目标表 / AI 识别被确认后，把该表头与目标表绑定并持久化
- 路由：导入前先精确命中表头 → 直接确定目标表，不跑规则评分、不调 AI

优先级：learned（用户明确绑定）> catalog/db 种子。同一签名多个候选（如 20 张
同结构宏观表）保持歧义，交给原有规则/AI 流程。
"""

import json
import os
import re
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

_NORM_RE = re.compile(r"[^a-z0-9一-鿿]")


def normalize_col(name: str) -> str:
    """列名归一化（与 matcher / column_mapper 一致）"""
    return _NORM_RE.sub("", str(name).strip().lstrip("﻿").lower())


def header_name_signature(columns) -> tuple[str, ...]:
    """表头签名：归一化列名集合（排序，顺序无关）

    「同意表头」约定的是列名集合，与列顺序无关；
    同一批同表头文件签名相同 → 复用同一模板。
    """
    return tuple(sorted({normalize_col(c) for c in columns}))


def _signature_key(sig) -> tuple[str, ...]:
    """把签名统一为可哈希的排序元组（兼容 list/tuple 入参）"""
    return tuple(sorted(sig))


@dataclass
class HeaderTemplate:
    """表头 → 目标表 的绑定"""
    signature: tuple = ()            # header_name_signature 结果（归一化列名排序元组）
    table_name: str = ""
    unique_key: list = field(default_factory=list)   # 目标表唯一键（仅供参考，worker 会重算）
    mapping: dict = field(default_factory=dict)      # 文件列 → 表列（可选）
    source: str = "learned"          # catalog | db | user | ai
    updated_at: str = ""


class HeaderTemplateStore:
    """表头模板库：加载 / 保存 / 学习 / 清除 / 建索引

    - learned 模板持久化到 JSON（config import.template_file，默认 data/header_templates.json）
    - catalog/db 种子每次动态重建，不落盘
    - 建索引时 learned 优先于种子；同一签名多表（同构宏观表）保持歧义
    """

    def __init__(self, config, path: Path | str | None = None):
        self.config = config
        if path is None:
            rel = config.get("import.template_file", "data/header_templates.json")
            self.path = Path(config.root_dir) / rel
        else:
            self.path = Path(path)

    # ------------------------------------------------------------------
    # 持久化（learned 模板）
    # ------------------------------------------------------------------

    def load_learned(self) -> dict[tuple, HeaderTemplate]:
        """读取已学习的模板，返回 {签名: HeaderTemplate}；损坏/缺失时返回空"""
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        out: dict[tuple, HeaderTemplate] = {}
        for item in data.get("templates", []):
            try:
                t = HeaderTemplate(
                    signature=_signature_key(item["signature"]),
                    table_name=item["table_name"],
                    unique_key=list(item.get("unique_key", [])),
                    mapping=dict(item.get("mapping", {})),
                    source=item.get("source", "learned"),
                    updated_at=item.get("updated_at", ""),
                )
            except Exception:
                continue
            out[t.signature] = t
        return out

    def save_learned(self, templates: dict[tuple, HeaderTemplate]) -> None:
        """原子写入 learned 模板（临时文件 + os.replace）"""
        payload = {"version": 1, "templates": [asdict(t) for t in templates.values()]}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def learn(self, columns, table_name: str, unique_key: list | None = None,
              mapping: dict | None = None, source: str = "learned") -> HeaderTemplate:
        """绑定表头 → 目标表 并持久化（同签名覆盖）

        Parameters
        ----------
        columns : 文件列名列表（用于计算表头签名）
        table_name : 目标表
        unique_key / mapping : 可选，存作参考
        source : "user"（用户手动指定）| "ai"（AI 识别被确认）
        """
        t = HeaderTemplate(
            signature=_signature_key(header_name_signature(columns)),
            table_name=table_name,
            unique_key=list(unique_key or []),
            mapping=dict(mapping or {}),
            source=source,
            updated_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        templates = self.load_learned()
        templates[t.signature] = t
        self.save_learned(templates)
        return t

    def clear(self) -> None:
        """删除所有 learned 模板"""
        if self.path.exists():
            try:
                self.path.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# 种子生成
# ---------------------------------------------------------------------------

# 建模板时忽略的库表元数据列（归一化后比较）
_META_COLS = {"id", "createdat", "updatedat"}


def seed_from_catalog(config) -> list[HeaderTemplate]:
    """从 data_catalog.yaml 中声明了 column_map 的数据源生成模板

    文件表头 = column_map 的键（同意中文列），目标表 = 该数据源的 table_name。
    目前 stock.a_daily / fund.etf_daily 声明了 column_map。
    """
    out: list[HeaderTemplate] = []
    try:
        from src.core.fetcher_registry import FetcherRegistry
        for src in FetcherRegistry(config).get_all_sources():
            cmap = src.get("column_map") or {}
            if not cmap or not src.get("table_name"):
                continue
            out.append(HeaderTemplate(
                signature=header_name_signature(cmap.keys()),
                table_name=src["table_name"],
                mapping=dict(cmap),
                source="catalog",
            ))
    except Exception:
        pass
    return out


def seed_from_db(tables_meta: list) -> list[HeaderTemplate]:
    """从现有数据库表生成模板：每张表用自身列名（去掉 id/created_at 等元列）作为期望表头

    这是最重要的种子：只要文件表头与某张表自身的列名集合一致，就确定性路由过去。
    同结构宏观表（列集合相同）会生成多个同签名模板 → 保持歧义，交给规则/AI/用户。
    """
    out: list[HeaderTemplate] = []
    for meta in tables_meta:
        cols = [c for c in getattr(meta, "columns", []) or []
                if normalize_col(c) not in _META_COLS]
        if not cols:
            continue
        out.append(HeaderTemplate(
            signature=header_name_signature(cols),
            table_name=meta.table_name,
            source="db",
        ))
    return out


def build_index(config, repo=None, tables_meta: list | None = None
                ) -> dict[tuple, list[HeaderTemplate]]:
    """组合索引：{表头签名 → 候选模板列表}

    优先级：learned（用户明确绑定，优先，同签名时覆盖种子）> catalog/db 种子。
    只保留目标表仍存在的模板；丢弃指向已删除表的 learned 模板。
    """
    from itertools import chain

    if tables_meta is None:
        if repo is None:
            tables_meta = []
        else:
            from src.importer.matcher import collect_tables
            tables_meta = collect_tables(repo)

    # 已知表集合：库里存在的表 + 数据目录里定义的表
    known = {t.table_name for t in tables_meta}
    try:
        from src.core.fetcher_registry import FetcherRegistry
        known |= {s.get("table_name") for s in FetcherRegistry(config).get_all_sources()
                  if s.get("table_name")}
    except Exception:
        pass

    store = HeaderTemplateStore(config)
    index: dict[tuple, list[HeaderTemplate]] = {}
    learned_sigs: set = set()

    # 1. learned（用户明确绑定）优先：同签名时权威覆盖所有种子
    for sig, t in store.load_learned().items():
        if t.table_name in known:
            index.setdefault(sig, []).append(t)
            learned_sigs.add(sig)

    # 2. 种子补充未被 learned 覆盖的签名（同结构表共享签名 → 多个候选保持歧义）
    for t in chain(seed_from_catalog(config), seed_from_db(tables_meta)):
        if t.table_name not in known or t.signature in learned_sigs:
            continue
        index.setdefault(t.signature, []).append(t)

    return index


def match_template(columns, index: dict) -> Optional[HeaderTemplate]:
    """精确命中表头模板：签名唯一 → 返回模板；多个候选（歧义）→ None

    返回 None 时上层继续走原有规则评分 → AI 兜底流程。
    """
    sig = header_name_signature(columns)
    cands = index.get(sig)
    if cands and len(cands) == 1:
        return cands[0]
    return None
