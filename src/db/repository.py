"""通用数据访问层 — CRUD、批量写入、缓存、动态列管理"""

import json
import logging
from datetime import date, datetime, timezone
from typing import Any

import pandas as pd
from sqlalchemy import (
    MetaData,
    Table,
    insert,
    select,
    text,
    delete,
    func,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.utils.config import ConfigManager
from src.utils.date_parse import normalize_cn_date_str
from src.db.models import (
    Base, SyncJob, ColumnRegistry, CacheEntry, IndicatorMap, utcnow,
)

logger = logging.getLogger("bern.db.repository")

# 已注册 SQLite 归一化函数的引擎 id（避免同一引擎重复注册 listener）
_registered_cn_fn_engines: set[int] = set()


def _now_utc() -> datetime:
    """当前 UTC 时间（naive）"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class DataRepository:
    """通用数据仓库 — 封装 CRUD、缓存、动态列管理"""

    def __init__(self, engine: Engine):
        self.engine = engine
        self._register_cn_date_fn(engine)

    @staticmethod
    def _register_cn_date_fn(engine: Engine) -> None:
        """为 SQLite 连接注册中文日期归一化自定义函数

        query() 的日期区间过滤对中文「月份/季度」列（"2026年06月份"）需先
        normalize 成 ISO 再比较——否则 SQL 字符串比较里 '年'(U+5E74) > '-'(U+2D)，
        同年数据全部被判为大于 ISO 日期，date_to 误杀。注册在每个连接上，
        幂等（create_function 覆盖同名函数）。
        """
        from sqlalchemy import event
        eid = id(engine)
        if eid in _registered_cn_fn_engines:
            return
        _registered_cn_fn_engines.add(eid)

        @event.listens_for(engine, "connect")
        def _register(dbapi_connection, connection_record):
            dbapi_connection.create_function(
                "normalize_cn_date_str", 1, normalize_cn_date_str)

    # ------------------------------------------------------------------
    # 建表
    # ------------------------------------------------------------------

    def create_tables(self) -> None:
        """创建所有 ORM 元数据表（幂等）"""
        Base.metadata.create_all(self.engine)
        logger.info("元数据表已就绪")

    # ------------------------------------------------------------------
    # 数据目录
    # ------------------------------------------------------------------

    def get_source_config(self, source_key: str) -> dict | None:
        """根据 source_key 在 data_catalog.yaml 中查找数据源配置"""
        config = ConfigManager()
        catalog = config.catalog
        categories = catalog.get("categories", [])

        def _search(nodes: list) -> dict | None:
            for node in nodes:
                children = node.get("children")
                if children:
                    result = _search(children)
                    if result is not None:
                        return result
                # 叶节点判断：包含 api_source 或 source_key 精确匹配
                if node.get("source_key") == source_key:
                    # 返回完整节点的副本
                    return dict(node)
                if node.get("source_key") and node.get("api_source") and node.get("api_function"):
                    if node["source_key"] == source_key:
                        return dict(node)
            return None

        return _search(categories)

    def get_all_enabled_sources(self) -> list[dict]:
        """收集可同步的数据源叶节点（排除 deprecated）

        委托 FetcherRegistry（唯一 canonical 遍历），避免与 fetcher_registry
        的拍平逻辑分叉导致 deprecated 源仍被 run_all 同步。
        """
        from src.core.fetcher_registry import FetcherRegistry
        from src.utils.config import ConfigManager
        return FetcherRegistry(ConfigManager()).get_all_enabled_sources()

    def _deprecated_table_names(self) -> set[str]:
        """目录中标 deprecated 的数据表名集合（获信源校验用）"""
        from src.core.fetcher_registry import FetcherRegistry
        from src.utils.config import ConfigManager
        reg = FetcherRegistry(ConfigManager())
        return {
            str(s.get("table_name"))
            for s in reg.get_all_sources(include_deprecated=True)
            if s.get("deprecated") and s.get("table_name")
        }

    # ------------------------------------------------------------------
    # 指标归一层（meta_indicator）
    # ------------------------------------------------------------------

    def list_indicators(self) -> list[dict]:
        """读取全部指标映射（indicator → 获信源表 + 列映射 + 口径语义）"""
        with Session(self.engine) as session:
            rows = session.execute(
                select(IndicatorMap).order_by(IndicatorMap.indicator_key)
            ).scalars().all()
        return [
            {
                "indicator_key": r.indicator_key,
                "preferred_table": r.preferred_table,
                "source_api": r.source_api,
                "date_column": r.date_column,
                "value_column": r.value_column,
                "unit_type": r.unit_type,
                "unit_desc": r.unit_desc,
            }
            for r in rows
        ]

    def _resolve_value_column(self, table_name: str, strict: bool = False) -> str | None:
        """解析表的数值列：优先中文数值列名，其次第一个数值类型列

        strict=True 只接受明确的数值列名（今值/现值/value 等关键词命中），
        不做"第一个数值列"的启发式——用于自动沿用获信源，避免把 OHLC 行情
        表的第一个数值列误当指标值。
        """
        from src.importer.matcher import sample_dtype
        cols = self.get_all_existing_columns(table_name)
        if not cols:
            return None
        # 「同比增长」置顶：中国官方宏观表数值列是「全国-同比增长」「当月同比增长」等，
        # 若排在「环比增长」前先命中（目标表均无环比先于同比的情况）。
        value_keywords = ("同比增长", "同比", "增长",
                          "今值", "现值", "数值", "value", "当前值")
        for col in cols:
            if any(kw in col.lower() for kw in value_keywords):
                return col
        if strict:
            return None
        # 没有明确数值列名 → 用第一个数值类型的列（排除日期列）
        date_col = self._find_date_column(table_name)
        try:
            df = self.query(table_name, limit=50)
            if df.empty:
                return None
        except Exception:
            return None
        for col in cols:
            if col == date_col:
                continue
            try:
                if sample_dtype(df, col) == "numeric":
                    return col
            except Exception:
                continue
        return None

    def set_indicator(
        self,
        indicator_key: str,
        table_name: str,
        unit_type: str | None = None,
        unit_desc: str | None = None,
    ) -> dict | None:
        """手动选择某指标的获信源表 → 解析列映射并 UPSERT 到 meta_indicator

        - 表已同步：解析日期/数值列后存储。
        - 目录中声明但尚未同步的表：允许「预配置」获信源（date/value 列留空，
          首次同步后 get_indicator 动态解析）。这使 FRED 等新表未同步时也能先选。
        - 无效表（目录里都没有）→ None。
        - unit_type/unit_desc：存储值口径语义（level/yoy/mom + 人类可读说明），
          从 data_catalog.yaml 源节点透传；未传则保留库内现值（或默认 level）。

        Returns:
            新映射 dict；表既不存在也不在目录 → None
        """
        # 已定案口径：deprecated(停更)源不得作为指标获信源——其数据已冻结在
        # 停更日、口径可能还是 mom/yoy，作为统一查询锚会误导下游。GUI 下拉
        # 也已过滤，这里是防御性兜底（API/auto_adopt 等任何入口都拦）。
        if table_name in self._deprecated_table_names():
            logger.warning("拒绝把 deprecated 表设为获信源: %s (indicator=%s)",
                           table_name, indicator_key)
            return None
        date_col, value_col = "", ""
        if self.table_exists(table_name):
            date_col = self._find_date_column(table_name) or ""
            value_col = self._resolve_value_column(table_name) or ""
            if not date_col or not value_col:
                return None
        else:
            # 未同步但目录中声明过 → 允许预配置（列留空，同步后动态解析）
            known = {s.get("table_name") for s in self.get_all_enabled_sources()}
            if table_name not in known:
                return None

        # 首选源的 api_source（从目录取）
        source_api = ""
        for src in self.get_all_enabled_sources():
            if src.get("table_name") == table_name:
                source_api = str(src.get("api_source", ""))
                break

        # 未显式传 unit 时，尝试从目录源节点取（保持与同步路径一致）
        if unit_type is None or unit_desc is None:
            for src in self.get_all_enabled_sources():
                if src.get("table_name") == table_name:
                    if unit_type is None:
                        unit_type = str(src.get("unit_type", "level"))
                    if unit_desc is None:
                        unit_desc = src.get("unit_desc") or ""
                    break

        record = {
            "indicator_key": indicator_key,
            "preferred_table": table_name,
            "source_api": source_api,
            "date_column": date_col,
            "value_column": value_col,
            "unit_type": unit_type or "level",
            "unit_desc": unit_desc or None,
        }
        now = _now_utc()
        with self.engine.connect() as conn:
            table = Table(
                "meta_indicator", MetaData(), autoload_with=conn)
            # 直接 Table 插入不会触发 ORM 列默认值，需显式给 created_at/updated_at
            stmt = sqlite_insert(table).values(**record, created_at=now, updated_at=now)
            stmt = stmt.on_conflict_do_update(
                index_elements=["indicator_key"],
                set_={
                    "preferred_table": stmt.excluded.preferred_table,
                    "source_api": stmt.excluded.source_api,
                    "date_column": stmt.excluded.date_column,
                    "value_column": stmt.excluded.value_column,
                    "unit_type": stmt.excluded.unit_type,
                    "unit_desc": stmt.excluded.unit_desc,
                    "updated_at": now,
                },
            )
            conn.execute(stmt)
            conn.commit()
        return record

    def get_indicator_map(self, indicator_key: str) -> dict | None:
        """读取单个指标的获信映射（含口径语义）；无则 None"""
        with Session(self.engine) as session:
            m = session.execute(
                select(IndicatorMap).where(
                    IndicatorMap.indicator_key == indicator_key)
            ).scalar_one_or_none()
        if m is None:
            return None
        return {
            "indicator_key": m.indicator_key,
            "preferred_table": m.preferred_table,
            "source_api": m.source_api,
            "date_column": m.date_column,
            "value_column": m.value_column,
            "unit_type": m.unit_type,
            "unit_desc": m.unit_desc,
        }

    def auto_adopt_indicator(
        self,
        indicator_key: str,
        table_name: str,
        unit_type: str | None = None,
        unit_desc: str | None = None,
    ) -> dict | None:
        """同步成功后自动沿用：该指标未设获信源、且当前表能明确解析数值列时，
        自动把当前表设为获信源。不覆盖用户已手动的选择。

        strict 解析：只接受明确的数值列名（今值/现值/value 等），避免把
        OHLC 行情表（open/close/volume）第一个数值列误当指标值。
        unit_type/unit_desc 透传给 set_indicator（从源节点配置取存储值口径）。
        """
        if not indicator_key or not self.table_exists(table_name):
            return None
        if self.get_indicator_map(indicator_key) is not None:
            return None  # 已有获信源，不覆盖
        date_col = self._find_date_column(table_name)
        value_col = self._resolve_value_column(table_name, strict=True)
        if not date_col or not value_col:
            return None
        return self.set_indicator(
            indicator_key, table_name,
            unit_type=unit_type, unit_desc=unit_desc)

    def get_indicator(
        self,
        indicator_key: str,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int | None = None,
    ) -> pd.DataFrame:
        """统一查询指标：读 meta_indicator 获信源表 → 返回 {date, value} 两列

        无映射 / 首选表未同步 / 表不存在 → 空 DataFrame（两列）。
        预配置的获信源（date/value 列未存，表尚未同步）在表存在后动态解析列。
        """
        empty = pd.DataFrame(columns=["date", "value"])
        mapping = None
        with Session(self.engine) as session:
            mapping = session.execute(
                select(IndicatorMap).where(
                    IndicatorMap.indicator_key == indicator_key)
            ).scalar_one_or_none()
        if mapping is None:
            return empty

        # 预配置（列未存）→ 首次查询时动态解析
        date_col = mapping.date_column or self._find_date_column(mapping.preferred_table)
        value_col = mapping.value_column or self._resolve_value_column(mapping.preferred_table)
        if not date_col or not value_col:
            return empty

        df = self.query(
            mapping.preferred_table,
            limit=limit,
            date_from=start_date,
            date_to=end_date,
        )
        if df.empty or date_col not in df.columns \
                or value_col not in df.columns:
            return empty

        out = pd.DataFrame({
            # 中文月份/季度列（"2026年06月份"）归一化后再返回，否则下游
            # compute_transform 的 pd.to_datetime 会全 NaT → 指标返回空。
            # 对无中文日期字样的 ISO 日期原样返回，不影响 FRED 源。
            "date": df[date_col].map(normalize_cn_date_str),
            "value": df[value_col],
        })
        return out.sort_values("date", ascending=False).reset_index(drop=True)

    def indicator_candidates(self, indicator_key: str) -> list[dict]:
        """收集目录中含指定 indicator 键的所有源（供 UI 选择获信源）

        附带源节点的口径语义（unit_type/unit_desc），UI 切换获信源时一并带上。
        """
        out = []
        for src in self.get_all_enabled_sources():
            if src.get("indicator") == indicator_key:
                out.append({
                    "table_name": src.get("table_name", ""),
                    "name": src.get("name", ""),
                    "source_key": src.get("source_key", ""),
                    "api_source": src.get("api_source", ""),
                    "unit_type": str(src.get("unit_type", "level")),
                    "unit_desc": src.get("unit_desc") or "",
                })
        return out

    # ------------------------------------------------------------------
    # 同步任务
    # ------------------------------------------------------------------

    def get_sync_job(self, module_name: str) -> SyncJob | None:
        """根据 module_name 查询同步任务"""
        with Session(self.engine) as session:
            return session.get(SyncJob, module_name) if False else (
                session.execute(
                    select(SyncJob).where(SyncJob.module_name == module_name)
                ).scalar_one_or_none()
            )

    def update_sync_job(self, module_name: str, data: dict) -> SyncJob:
        """更新或插入同步任务（upsert）"""
        with Session(self.engine) as session:
            existing = session.execute(
                select(SyncJob).where(SyncJob.module_name == module_name)
            ).scalar_one_or_none()

            if existing:
                for key, value in data.items():
                    if hasattr(existing, key):
                        setattr(existing, key, value)
                existing.updated_at = _now_utc()
                job = existing
            else:
                data.setdefault("created_at", _now_utc())
                data.setdefault("updated_at", _now_utc())
                data["module_name"] = module_name
                job = SyncJob(**data)
                session.add(job)

            session.commit()
            session.refresh(job)
            return job

    def get_all_enabled_sync_jobs(self) -> list[SyncJob]:
        """获取所有启用的同步任务"""
        with Session(self.engine) as session:
            return list(
                session.execute(
                    select(SyncJob).where(SyncJob.enabled.is_(True))
                ).scalars().all()
            )

    def update_sync_heartbeat(
        self,
        module_name: str,
        running_status: str,
        heartbeat: "datetime | None" = None,
    ) -> bool:
        """轻量更新长任务运行状态/心跳（P1）

        仅当任务行已存在时更新（不存在跳过返回 False）——长任务启动时行可能
        尚未建立，最终 update_sync_job 会完整建档。逐交易日刷新一次，开销极小。
        """
        with Session(self.engine) as session:
            existing = session.execute(
                select(SyncJob).where(SyncJob.module_name == module_name)
            ).scalar_one_or_none()
            if existing is None:
                return False
            existing.running_status = running_status
            if heartbeat is not None:
                existing.last_heartbeat = heartbeat
            existing.updated_at = _now_utc()
            session.commit()
            return True

    # ------------------------------------------------------------------
    # 查询辅助
    # ------------------------------------------------------------------

    def table_exists(self, table_name: str) -> bool:
        """检查指定数据表是否存在"""
        quoted = self._quote_table(table_name)
        try:
            with self.engine.connect() as conn:
                conn.execute(text(f"SELECT 1 FROM {quoted} LIMIT 0"))
                return True
        except Exception:
            return False

    def count_rows(self, table_name: str) -> int:
        """统计指定数据表的行数（表不存在返回 0）"""
        if not self.table_exists(table_name):
            return 0
        with self.engine.connect() as conn:
            safe = self._quote_table(table_name)
            result = conn.execute(text(f"SELECT COUNT(*) FROM {safe}"))
            return result.scalar() or 0

    def count_rows_by_date(self, table_name: str, date_column: str, value: str) -> int:
        """统计某日期列等于指定值的行数（表不存在返回 0）

        供基金批量回溯判断某交易日是否已全市场覆盖（避免重复拉取）。
        """
        if not self.table_exists(table_name):
            return 0
        safe_t = self._quote_table(table_name)
        safe_c = self._quote_column(date_column)
        with self.engine.connect() as conn:
            result = conn.execute(
                text(f"SELECT COUNT(*) FROM {safe_t} WHERE {safe_c} = :v"),
                {"v": value})
            return result.scalar() or 0

    def get_last_date(self, table_name: str, date_column: str = "date") -> date | None:
        """查询指定时间列的最大值（表不存在返回 None）"""
        if not self.table_exists(table_name):
            return None
        with self.engine.connect() as conn:
            safe_t = self._quote_table(table_name)
            safe_c = self._quote_column(date_column)
            result = conn.execute(
                text(f"SELECT MAX({safe_c}) FROM {safe_t}")
            )
            val = result.scalar()
            if val is None:
                return None
            if isinstance(val, str):
                # 标准 ISO 日期直接解析；中文日期（akshare 宏观源常见
                # "2026年07月份"）经归一化兜底，解析失败返回 None（不抛，
                # 避免 freshness / 指标截止日对话框崩溃）。
                try:
                    return date.fromisoformat(val)
                except ValueError:
                    pass
                norm = normalize_cn_date_str(val)
                try:
                    return pd.to_datetime(norm, errors="raise").date()
                except (ValueError, TypeError):
                    return None
            if isinstance(val, datetime):
                return val.date()
            return val

    # ------------------------------------------------------------------
    # 唯一索引 / 去重
    # ------------------------------------------------------------------

    def _safe_index_name(self, table_name: str, columns: list[str]) -> str:
        """生成安全的唯一索引名（uix_表名_列1_列2）"""
        safe = table_name.replace('"', "").replace(".", "_")
        cols = "_".join(c.replace('"', "").replace(".", "_") for c in columns)
        return f"uix_{safe}_{cols}"

    # 日期类列名候选（用于把逻辑列名 date 解析到真实列名）
    DATE_COLUMN_KEYWORDS = ("date", "日期", "时间", "月份", "季度",
                            "trade_date", "datetime")

    def resolve_date_columns(self, table_name: str, columns: list[str]) -> list[str]:
        """把唯一键中的逻辑日期列（date/时间/日期/月份）解析到表中真实存在的列名

        例如键 ["date"] 对 macro_usa_cpi_yoy（真实日期列是「时间」）解析为 ["时间"]；
        index_daily 无 symbol 列时键 ["symbol","date"] 解析为 ["date"]。
        解析不出的逻辑列（如确实没有的 symbol）会被丢弃，返回空列表表示无可用唯一键。
        匹配大小写不敏感（如 TRADE_DATE 可匹配 trade_date）。
        """
        if not self.table_exists(table_name):
            return []
        existing = self.get_all_existing_columns(table_name)
        lower_keywords = tuple(k.lower() for k in self.DATE_COLUMN_KEYWORDS)
        result = []
        for col in columns:
            if col in existing:
                result.append(col)
                continue
            # 逻辑日期列 → 匹配真实日期列（大小写不敏感）
            col_lower = col.lower()
            if col_lower in ("date", "时间", "日期", "月份", "datetime") or \
                    any(kw in col_lower for kw in lower_keywords):
                for real in existing:
                    if any(kw in real.lower() for kw in lower_keywords):
                        result.append(real)
                        break
            # 其它逻辑列（symbol/code）在表中不存在时直接丢弃
        return result

    def index_exists(self, table_name: str, columns: list[str]) -> bool:
        """检查表上是否已存在覆盖指定列的唯一索引"""
        cols = self.resolve_date_columns(table_name, columns)
        if not cols:
            return False
        idx_name = self._safe_index_name(table_name, cols)
        with self.engine.connect() as conn:
            row = conn.execute(text(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name=:n"
            ), {"n": idx_name}).scalar_one_or_none()
            return row is not None

    def get_duplicate_count(self, table_name: str, columns: list[str]) -> int:
        """按唯一键统计重复组数（表不存在或列缺失返回 0）"""
        if not columns or not self.table_exists(table_name):
            return 0
        cols = self.resolve_date_columns(table_name, columns)
        if not cols:
            return 0
        cols_sql = ", ".join(self._quote_column(c) for c in cols)
        with self.engine.connect() as conn:
            try:
                result = conn.execute(text(
                    f"SELECT COUNT(*) FROM (SELECT {cols_sql}, COUNT(*) c "
                    f"FROM {self._quote_table(table_name)} "
                    f"GROUP BY {cols_sql} HAVING c > 1)"
                ))
                return result.scalar() or 0
            except Exception:
                return 0

    def dedupe_rows(self, table_name: str, columns: list[str]) -> int:
        """按唯一键删除重复行（保留每组 id 最小的一行），返回删除行数

        在创建唯一索引前调用，避免因已有重复数据导致建索引失败。
        """
        if not columns or not self.table_exists(table_name):
            return 0
        cols = self.resolve_date_columns(table_name, columns)
        if not cols:
            return 0
        cols_sql = ", ".join(self._quote_column(c) for c in cols)
        with self.engine.connect() as conn:
            try:
                result = conn.execute(text(
                    f"DELETE FROM {self._quote_table(table_name)} "
                    f"WHERE id NOT IN ("
                    f"  SELECT MIN(id) FROM {self._quote_table(table_name)} "
                    f"  GROUP BY {cols_sql})"
                ))
                conn.commit()
                deleted = result.rowcount or 0
                if deleted:
                    logger.info("去重 %s: 删除 %d 行重复数据", table_name, deleted)
                return deleted
            except Exception as exc:
                logger.warning("去重 %s 失败: %s", table_name, exc)
                return 0

    def ensure_unique_index(
        self,
        table_name: str,
        columns: list[str] | None = None,
        dedupe: bool = True,
    ) -> bool:
        """确保表上存在覆盖指定列的唯一索引

        列缺失时返回 False（无法建索引）。若表上已有重复行导致建索引失败，
        按 dedupe 开关选择是否先清理重复行再建索引。

        Parameters
        ----------
        table_name : str
            目标表名
        columns : list[str] | None
            唯一键列。None 或空 → 不建索引，返回 True（无操作）
        dedupe : bool
            建索引前是否自动清理已有重复行，默认 True
        """
        if not columns:
            return True
        # 解析唯一键到表中真实存在的列（日期列名归一化，缺失列丢弃）
        cols = self.resolve_date_columns(table_name, columns)
        if not cols:
            logger.warning("无法建唯一索引 %s: 表中无这些列 %s", table_name, columns)
            return False
        if self.index_exists(table_name, cols):
            return True

        idx_name = self._safe_index_name(table_name, cols)
        cols_sql = ", ".join(self._quote_column(c) for c in cols)
        sql = (f"CREATE UNIQUE INDEX IF NOT EXISTS {self._quote_column(idx_name)} "
               f"ON {self._quote_table(table_name)} ({cols_sql})")

        try:
            with self.engine.connect() as conn:
                conn.execute(text(sql))
                conn.commit()
            logger.info("已创建唯一索引 %s (%s)", idx_name, ", ".join(cols))
            return True
        except Exception as exc:
            # 建索引失败：通常是已有重复行
            if not dedupe:
                logger.warning("创建唯一索引 %s 失败（已有重复数据）: %s", idx_name, exc)
                return False
            deleted = self.dedupe_rows(table_name, cols)
            if deleted == 0:
                logger.warning("创建唯一索引 %s 失败且无法自动去重: %s", idx_name, exc)
                return False
            try:
                with self.engine.connect() as conn:
                    conn.execute(text(sql))
                    conn.commit()
                logger.info("去重后已创建唯一索引 %s (%s)", idx_name, ", ".join(cols))
                return True
            except Exception as exc2:
                logger.warning("去重后建唯一索引 %s 仍失败: %s", idx_name, exc2)
                return False

    # ------------------------------------------------------------------
    # 普通索引（非唯一）— 加速 /data 等通用查询
    # ------------------------------------------------------------------

    def ensure_index(self, table_name: str, columns: list[str]) -> bool:
        """创建非唯一普通索引（如 date 或 date+code），加速通用查询

        逻辑列名经 resolve_date_columns 解析（日期列归一化、缺失列丢弃）；
        无有效列返回 False。SQLite TEXT 列建普通索引即可提速范围过滤。
        """
        if not columns or not self.table_exists(table_name):
            return False
        cols = self.resolve_date_columns(table_name, columns)
        if not cols:
            return False
        safe = table_name.replace('"', "").replace(".", "_")
        idx_name = "idx_" + safe + "_" + "_".join(
            c.replace('"', "").replace(".", "_") for c in cols)
        cols_sql = ", ".join(self._quote_column(c) for c in cols)
        sql = (f"CREATE INDEX IF NOT EXISTS {self._quote_column(idx_name)} "
               f"ON {self._quote_table(table_name)} ({cols_sql})")
        try:
            with self.engine.connect() as conn:
                conn.execute(text(sql))
                conn.commit()
            logger.info("已创建普通索引 %s (%s)", idx_name, ", ".join(cols))
            return True
        except Exception as exc:
            logger.warning("创建普通索引 %s 失败: %s", idx_name, exc)
            return False

    # ------------------------------------------------------------------
    # 批量写入
    # ------------------------------------------------------------------

    def bulk_upsert(
        self,
        table_name: str,
        df: pd.DataFrame,
        unique_columns: list[str] | None = None,
        batch_size: int = 500,
    ) -> int:
        """批量写入 DataFrame，返回写入行数

        指定 unique_columns 时：
          - 写入前确保表上存在覆盖这些列的唯一索引（含去重已有重复行）
          - 用 ON CONFLICT DO UPDATE 实现真 upsert：唯一键已存在的行更新、
            新行插入，保留原 id 与 created_at（比 OR REPLACE 更正确）
        未指定时退回 INSERT OR REPLACE（保持旧行为）。

        Parameters
        ----------
        table_name : str
            目标表名
        df : pd.DataFrame
            待写入数据
        unique_columns : list[str] | None
            唯一键列（逻辑名，如 date 会解析到真实日期列）
        batch_size : int
            每批行数，默认 500
        """
        if df.empty:
            return 0

        # 防御：把 pandas 的 NaT/NaN/NA 转成 None，避免 SQLite 无法绑定
        # （同步/导入/抓取等路径的数据都可能带缺失值，NaT 尤其常见于日期列）
        # pandas 3.x 下 .where(mask, None) 对 datetime64/float/str(含 pd.NA)
        # 列会把 None 强转回 NaT/nan/NA，故先对含缺失的列整体 astype(object)
        df = self._sanitize_for_sql(df)

        # 解析唯一键到表内真实列（如 date -> 时间）
        unique_cols = self.resolve_date_columns(table_name, unique_columns or [])
        if unique_cols:
            self.ensure_unique_index(table_name, unique_cols, dedupe=True)

        total = 0        # 处理行数（日志口径，含已存在的更新）
        new_rows = 0     # 真正新增行数（返回值；静默停更识别等依赖准确口径）
        metadata = MetaData()
        records = df.to_dict(orient="records")

        with self.engine.connect() as conn:
            # 反射目标表结构
            table = Table(
                table_name,
                metadata,
                autoload_with=conn,
            )

            for i in range(0, len(records), batch_size):
                batch = records[i : i + batch_size]
                stmt = sqlite_insert(table).values(batch)
                if unique_cols:
                    # 预查批内唯一键哪些已存在 → 统计真正新增行数。
                    # ON CONFLICT DO UPDATE 的 rowcount 连「同值 no-op 更新」也计 1
                    #（sqlite changes() 语义），无法区分新增/更新，故必须显式预查。
                    # 这是静默停更识别的数据基础：拉回 N 行但 0 新增 = 全是重复/源站未更新。
                    new_rows += self._count_new_unique_keys(
                        conn, table_name, unique_cols, batch)
                    # 唯一键之外的列在冲突时更新（保留原 id / created_at）
                    set_cols = {
                        c: getattr(stmt.excluded, c)
                        for c in df.columns
                        if c not in unique_cols
                    }
                    if set_cols:
                        stmt = stmt.on_conflict_do_update(
                            index_elements=unique_cols, set_=set_cols)
                    else:
                        stmt = stmt.on_conflict_do_nothing(
                            index_elements=unique_cols)
                else:
                    # 无唯一键：OR REPLACE 全量按新增计
                    new_rows += len(batch)
                    stmt = stmt.prefix_with("OR REPLACE")
                conn.execute(stmt)
                total += len(batch)

            conn.commit()

        logger.info("bulk_upsert %s: %d 行已处理（唯一键=%s）",
                    table_name, total, unique_cols)
        return new_rows

    @staticmethod
    def _count_new_unique_keys(
        conn,
        table_name: str,
        unique_cols: list[str],
        records: list[dict],
    ) -> int:
        """批内唯一键已存在于表中的行数 → 返回真正新增行数（批内去重）

        静默停更识别的数据基础：拉回 N 行但 0 新增 = 全是重复/源站未更新。
        手工拼行值 IN（SQLite 支持 `(c1,c2) IN ((v1,v2),...)`，但 SQLAlchemy
        元组表达式在老 SQLite 方言下编译失败）；列名走 _quote_column 防注入。
        键值统一转 str：表内 TEXT 亲和会把数字绑参归一，Python 侧也按 str 比较。
        按 ~250 键/次分块，避开 SQLite 绑定参数上限（旧版 999）。
        """
        if not records or not unique_cols:
            return 0
        # 批内去重（同批重复的唯一键只算一次新增）。
        # 唯一键含 NULL 的行：SQLite 唯一索引对 NULL 互不冲突，ON CONFLICT 永不
        # 匹配 → 每次写入都是真新增（NaT 日期行同理），单独计数不参与预查。
        unique_keys: set[tuple] = set()
        null_key_rows = 0
        for r in records:
            if all(r.get(c) is not None for c in unique_cols):
                unique_keys.add(tuple(str(r.get(c)) for c in unique_cols))
            else:
                null_key_rows += 1
        if not unique_keys:
            return null_key_rows

        cols_sql = ", ".join(DataRepository._quote_column(c) for c in unique_cols)
        tbl = DataRepository._quote_table(table_name)
        existing: set[tuple] = set()
        keys_list = list(unique_keys)
        for start in range(0, len(keys_list), 250):
            chunk = keys_list[start:start + 250]
            placeholders = []
            params: dict[str, object] = {}
            for idx, k in enumerate(chunk):
                placeholders.append(
                    "(" + ",".join(f":_k{start + idx}_{j}" for j in range(len(k))) + ")")
                for j, v in enumerate(k):
                    params[f"_k{start + idx}_{j}"] = v
            sql = (f"SELECT {cols_sql} FROM {tbl} "
                   f"WHERE ({cols_sql}) IN ({', '.join(placeholders)})")
            rows = conn.execute(text(sql), params).fetchall()
            existing.update(tuple(r) for r in rows)
        return null_key_rows + sum(1 for k in unique_keys if k not in existing)

    def analyze_import(
        self,
        table_name: str,
        df: pd.DataFrame,
        unique_columns: list[str] | None = None,
        existing_df: pd.DataFrame | None = None,
        dup_in_db: int | None = None,
    ) -> dict:
        """估算导入影响：更新/新增/文件内重复/库内现存重复

        用于导入对话框预览。返回:
            total, to_insert, to_update, to_skip,
            dup_in_db, unique_columns, missing_unique_cols

        可选参数（批量导入性能优化，识别阶段库不变时由调用方缓存传入）:
            existing_df : 目标表已预加载的全量 DataFrame，传入则跳过内部全表查询
            dup_in_db   : 已计算的库内重复组数，传入则跳过重复计数查询
        """
        result = {
            "total": 0 if df is None else len(df),
            "to_insert": 0,
            "to_update": 0,
            "to_skip": 0,
            "dup_in_db": 0,
            "unique_columns": [],
            "missing_unique_cols": [],
        }
        if df is None or df.empty:
            return result

        cols = self.resolve_date_columns(table_name, unique_columns or [])
        result["unique_columns"] = cols

        # 文件缺少的唯一键列
        result["missing_unique_cols"] = [
            c for c in (unique_columns or []) if c not in df.columns
        ]

        # 库内现存重复（若建唯一索引会自动清理）
        if dup_in_db is not None:
            result["dup_in_db"] = dup_in_db
        elif cols:
            result["dup_in_db"] = self.get_duplicate_count(table_name, cols)

        if not cols:
            # 无唯一键：全部新增
            result["to_insert"] = len(df)
            return result

        # 只保留文件里存在的键列（缺列的那一个无法比对）
        use_cols = [c for c in cols if c in df.columns]
        if not use_cols:
            result["to_insert"] = len(df)
            return result

        # 文件内部按唯一键去重：重复的记为 skip
        key_df = df[use_cols].astype(str)
        uniq = key_df.drop_duplicates(keep="first")
        result["to_skip"] = len(key_df) - len(uniq)

        # 与库内已有键比对（existing_df 由调用方预加载传入时跳过全表查询）
        if existing_df is not None:
            try:
                existing = existing_df[use_cols]
            except Exception:
                existing = pd.DataFrame(columns=use_cols)
        else:
            try:
                existing = self.query(table_name, limit=None)[use_cols]
            except Exception:
                existing = pd.DataFrame(columns=use_cols)
        existing_keys = set(
            existing.astype(str).apply(tuple, axis=1)) if not existing.empty else set()
        file_keys = set(uniq.apply(tuple, axis=1))

        result["to_insert"] = len(file_keys - existing_keys)
        result["to_update"] = len(uniq) - result["to_insert"]
        return result

    # ------------------------------------------------------------------
    # 通用查询
    # ------------------------------------------------------------------

    def _find_date_column(self, table_name: str) -> str | None:
        """从表中找第一个日期类列（无则返回 None）"""
        existing = self.get_all_existing_columns(table_name)
        lower_keywords = tuple(k.lower() for k in self.DATE_COLUMN_KEYWORDS)
        for col in existing:
            cl = col.lower()
            if cl in ("date", "时间", "日期", "月份", "datetime") or \
                    any(kw in cl for kw in lower_keywords):
                return col
        return None

    @staticmethod
    def _to_iso_date(value: str | None) -> str | None:
        """YYYYMMDD → YYYY-MM-DD；已带横线/其他格式原样返回"""
        if not value:
            return None
        v = str(value).strip()
        if len(v) == 8 and v.isdigit():
            return f"{v[:4]}-{v[4:6]}-{v[6:]}"
        return v

    def query(
        self,
        table_name: str,
        filters: dict | None = None,
        order_by: str | None = None,
        limit: int | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> pd.DataFrame:
        """查询数据表返回 DataFrame（表不存在返回空 DataFrame）

        date_from/date_to : 可选，按自动探测的日期列做区间过滤。
        接受 YYYYMMDD 或 YYYY-MM-DD；中文「月份/季度」列自动归一化比较。
        """
        if not self.table_exists(table_name):
            return pd.DataFrame()
        safe_t = self._quote_table(table_name)
        where_clauses: list[str] = []
        params: dict[str, Any] = {}

        if filters:
            for key, value in filters.items():
                safe_k = key.replace("\"", "\"\"")
                where_clauses.append(f'"{safe_k}" = :_{safe_k}')
                params[f"_{safe_k}"] = value

        # 日期区间过滤（按表内第一个日期类列）
        # 参数先统一转 ISO（GUI/API 传入 YYYYMMDD 或已带横线均可）：
        #   - ISO 列（date/datetime/trade_date）直接用 SQL 字符串比较（走日期索引）
        #   - 中文「月份/季度」列（"2026年06月份"）需先 normalize 再比较，
        #     否则 '年'(U+5E74) > '-'(U+2D) 导致同年数据被 date_to 误杀
        date_filtered = False
        date_col = None
        date_expr = None
        if date_from is not None or date_to is not None:
            date_filtered = True
        # 探测日期列：带 LIMIT 的任何查询（日期区间 / code 过滤 / 纯 limit 切片）
        # 都可能需要按日期倒序取最新，统一在此探测 date_expr。
        if (date_filtered or filters or limit is not None) and date_col is None:
            date_col = self._find_date_column(table_name)
        if date_col:
            safe_dc = date_col.replace("\"", "\"\"")
            cn_col = any(k in date_col for k in ("月份", "季度"))
            date_expr = (f'normalize_cn_date_str("{safe_dc}")'
                         if cn_col else f'"{safe_dc}"')
            if date_filtered:
                iso_from = self._to_iso_date(date_from)
                iso_to = self._to_iso_date(date_to)
                if iso_from is not None:
                    where_clauses.append(f"{date_expr} >= :_d_from")
                    params["_d_from"] = iso_from
                if iso_to is not None:
                    where_clauses.append(f"{date_expr} <= :_d_to")
                    params["_d_to"] = iso_to

        sql = f"SELECT * FROM {safe_t}"
        if where_clauses:
            sql += " WHERE " + " AND ".join(where_clauses)
        # 任何带 LIMIT 的查询（日期区间 / code 过滤 / 纯 limit 切片）而无显式排序时，
        # 都默认按日期倒序：让 LIMIT 取**最新**的行（日期过滤走索引；code 过滤同理）。
        # 否则 SQLite 按插入序返回前 N 行 → 纯 limit 切片会拿到库内**最旧**的数据
        # （2026-08-12 修复：skill/用户「取最新」时不再返回 2014 年旧行）。
        order = order_by
        if not order and limit is not None and date_expr:
            order = f"{date_expr} DESC"
        if order:
            sql += f" ORDER BY {order}"
        if limit is not None:
            sql += f" LIMIT {limit}"

        with self.engine.connect() as conn:
            return pd.read_sql_query(sql, conn, params=params)

    def get_distinct_values(self, table_name: str, column: str, limit: int = 1000) -> list[str]:
        """返回某列非空去重值列表（供「已有代码」下拉）；表或列不存在返回 []"""
        if not self.table_exists(table_name):
            return []
        safe_t = self._quote_table(table_name)
        safe_c = column.replace("\"", "\"\"")
        try:
            with self.engine.connect() as conn:
                rows = conn.execute(text(
                    f"SELECT DISTINCT \"{safe_c}\" FROM {safe_t} "
                    f"WHERE \"{safe_c}\" IS NOT NULL AND \"{safe_c}\" != '' "
                    f"ORDER BY \"{safe_c}\" LIMIT {int(limit)}"
                )).fetchall()
            return [str(r[0]) for r in rows]
        except Exception:
            return []

    # 代码列候选（与前端 stats.js CODE_KEYWORDS 对齐，集中一处防漂移）
    CODE_COLUMN_CANDIDATES = ("code", "symbol", "ts_code")

    def detect_code_column(self, table_name: str) -> str | None:
        """探测表的代码列名（code/symbol/ts_code 按优先级）；表不存在返回 None

        供 API 侧 ?code= 过滤与 /search 统一解析代码列。表不存在时
        get_all_existing_columns 的 PRAGMA 返回空列表（不抛异常），仍 try/except 兜底。
        """
        if not self.table_exists(table_name):
            return None
        try:
            cols = self.get_all_existing_columns(table_name)
        except Exception:
            return None
        for c in self.CODE_COLUMN_CANDIDATES:
            if c in cols:
                return c
        return None

    def get_asset_names(self, asset_type: str) -> dict[str, str]:
        """读取 meta_asset_info 的 code→名称 映射（按资产类型）；表不存在返回 {}

        供 /search 名称补全。表由 scripts_gen/sync_asset_names.py 维护，
        未同步/不存在时优雅降级为纯 code 匹配。
        """
        if not self.table_exists("meta_asset_info"):
            return {}
        try:
            with self.engine.connect() as conn:
                rows = conn.execute(text(
                    "SELECT code, name FROM meta_asset_info WHERE asset_type = :at"
                ), {"at": asset_type}).fetchall()
            return {str(r[0]): (str(r[1]) if r[1] is not None else "") for r in rows}
        except Exception:
            return {}

    # ------------------------------------------------------------------
    # 复权因子表（asset_adj_factor）只读查询
    # ------------------------------------------------------------------

    def query_adj_factors(
        self,
        asset_type: str,
        codes: list[str],
        limit: int | None = None,
    ) -> pd.DataFrame:
        """查询复权因子（asset_type + 多个 code 的全量因子序列，按日期升序）

        供 /data?adj= 派生：需该 code 全序列因子（qfq 的 latest 归一化点）。
        codes 为空 / 表不存在 → 空 DataFrame。code 值来自白名单表内已有值，
        仍走 _quote_table 防注入。
        """
        codes = [c for c in (codes or []) if c is not None and str(c).strip()]
        if not codes or not self.table_exists("asset_adj_factor"):
            return pd.DataFrame()
        safe_t = self._quote_table("asset_adj_factor")
        placeholders = ",".join(f":_c{i}" for i in range(len(codes)))
        params: dict[str, Any] = {"_at": asset_type}
        params.update({f"_c{i}": c for i, c in enumerate(codes)})
        sql = (f"SELECT * FROM {safe_t} WHERE asset_type = :_at "
               f"AND code IN ({placeholders}) ORDER BY date ASC")
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        with self.engine.connect() as conn:
            return pd.read_sql_query(sql, conn, params=params)

    def get_adj_factor_max_date(self, asset_type: str):
        """取某 asset_type 复权因子的最大日期（date 对象）；表不存在/无数据 → None

        增量起点用——两 asset_type 写同一 asset_adj_factor 表，若用
        meta_sync_jobs.last_sync_date 会互相污染，故按 asset_type 分列取 MAX。
        """
        if not self.table_exists("asset_adj_factor"):
            return None
        try:
            with self.engine.connect() as conn:
                val = conn.execute(
                    text('SELECT MAX(date) FROM asset_adj_factor '
                         'WHERE asset_type = :_at'),
                    {"_at": asset_type},
                ).scalar()
            if not val:
                return None
            return date.fromisoformat(str(val)[:10])
        except Exception:
            return None

    def get_max_date(
        self,
        table_name: str,
        code_col: str | None = None,
        code: str | None = None,
    ) -> str | None:
        """按（可选）code 取表中实际最大日期值（ISO 字符串）

        用于多 code 表（fund/stock）的增量起点：比 meta_sync_jobs.last_sync_date
        （表级）更可靠，即使 last_sync_date 被污染也能从该 code 真实数据之后开始。
        无日期列或无数据返回 None。
        """
        if not self.table_exists(table_name):
            return None
        date_col = self._find_date_column(table_name)
        if not date_col:
            return None
        where = f'"{date_col.replace(chr(34), chr(34)*2)}" IS NOT NULL AND "{date_col.replace(chr(34), chr(34)*2)}" != ""'
        params: dict[str, str] = {}
        if code and code_col:
            safe_c = code_col.replace("\"", "\"\"")
            where += f' AND "{safe_c}" = :code'
            params["code"] = code
        sql = f'SELECT MAX("{date_col.replace(chr(34), chr(34)*2)}") FROM {self._quote_table(table_name)} WHERE {where}'
        try:
            with self.engine.connect() as conn:
                val = conn.execute(text(sql), params).scalar()
            return str(val) if val else None
        except Exception:
            return None

    # ------------------------------------------------------------------
    # 缓存
    # ------------------------------------------------------------------

    def get_cache_entry(self, cache_key: str) -> CacheEntry | None:
        """根据 cache_key 获取缓存条目"""
        with Session(self.engine) as session:
            return session.execute(
                select(CacheEntry).where(CacheEntry.cache_key == cache_key)
            ).scalar_one_or_none()

    def upsert_cache_entry(
        self,
        cache_key: str,
        params: str | dict | None,
        data: pd.DataFrame,
        ttl: int = 300,
    ) -> CacheEntry:
        """写入或更新缓存条目"""
        query_params: str | None = None
        if params is not None:
            query_params = json.dumps(params, ensure_ascii=False) if isinstance(params, dict) else params

        data_preview: str | None = None
        row_count = 0
        if data is not None and not data.empty:
            row_count = len(data)
            data_preview = data.head(5).to_string(max_colwidth=80)

        now = _now_utc()
        expires_at = now.timestamp() + ttl
        expires_dt = _now_utc()  # fallback

        with Session(self.engine) as session:
            existing = session.execute(
                select(CacheEntry).where(CacheEntry.cache_key == cache_key)
            ).scalar_one_or_none()

            if existing:
                existing.query_params = query_params
                existing.data_preview = data_preview
                existing.row_count = row_count
                existing.fetched_at = now
                existing.expires_at = datetime.fromtimestamp(expires_at)
                existing.ttl_seconds = ttl
                entry = existing
            else:
                entry = CacheEntry(
                    cache_key=cache_key,
                    query_params=query_params,
                    data_preview=data_preview,
                    row_count=row_count,
                    fetched_at=now,
                    expires_at=datetime.fromtimestamp(expires_at),
                    ttl_seconds=ttl,
                )
                session.add(entry)

            session.commit()
            session.refresh(entry)
            return entry

    def delete_cache_entry(self, cache_key: str) -> None:
        """删除指定缓存条目"""
        with Session(self.engine) as session:
            session.execute(
                delete(CacheEntry).where(CacheEntry.cache_key == cache_key)
            )
            session.commit()

    def delete_cache_by_table(self, table_name: str) -> int:
        """删除 cache_key 以 table_name 开头的所有缓存条目，返回删除数"""
        with Session(self.engine) as session:
            result = session.execute(
                delete(CacheEntry).where(CacheEntry.cache_key.like(f"{table_name}%"))
            )
            session.commit()
            deleted = result.rowcount
            if deleted:
                logger.info("已清除 %s 相关的 %d 条缓存", table_name, deleted)
            return deleted

    # ------------------------------------------------------------------
    # 动态列管理
    # ------------------------------------------------------------------

    def get_all_existing_columns(self, table_name: str) -> list[str]:
        """返回指定数据表当前所有的列名列表"""
        safe_t = self._quote_table(table_name)
        with self.engine.connect() as conn:
            # SQLite PRAGMA
            result = conn.execute(text(f"PRAGMA table_info({safe_t})"))
            return [row[1] for row in result.fetchall()]

    def add_column_to_table(
        self,
        table_name: str,
        column_name: str,
        col_type: str = "TEXT",
    ) -> None:
        """给数据表添加新列（幂等）"""
        existing = self.get_all_existing_columns(table_name)
        if column_name in existing:
            logger.debug("列 %s.%s 已存在，跳过", table_name, column_name)
            return

        safe_t = self._quote_table(table_name)
        safe_c = self._quote_column(column_name)
        sql = text(f"ALTER TABLE {safe_t} ADD COLUMN {safe_c} {col_type}")
        with self.engine.connect() as conn:
            conn.execute(sql)
            conn.commit()
        logger.info("已添加列 %s.%s (%s)", table_name, column_name, col_type)

    def log_column_registry(
        self,
        table_name: str,
        column_name: str,
        source_api: str = "",
    ) -> None:
        """在 meta_column_registry 中记录列信息"""
        with Session(self.engine) as session:
            existing = session.execute(
                select(ColumnRegistry).where(
                    ColumnRegistry.table_name == table_name,
                    ColumnRegistry.column_name == column_name,
                )
            ).scalar_one_or_none()

            if not existing:
                # 手动生成 ID（SQLite INTEGER vs BIGINT 兼容）
                max_id = session.execute(
                    select(func.coalesce(func.max(ColumnRegistry.id), 0) + 1)
                ).scalar()
                entry = ColumnRegistry(
                    id=max_id,
                    table_name=table_name,
                    column_name=column_name,
                    column_type="TEXT",
                    is_auto_added=True,
                    source_api=source_api or None,
                    first_seen_at=_now_utc(),
                )
                session.add(entry)
                try:
                    session.commit()
                except IntegrityError:
                    # 并发加列：另一线程已写入同一 (table_name, column_name)
                    # 或抢占了同一 id，回滚忽略，功能等价
                    session.rollback()

    def get_column_registry(self, table_name: str) -> list[ColumnRegistry]:
        """查询某张表所有已注册的动态列"""
        with Session(self.engine) as session:
            return list(
                session.execute(
                    select(ColumnRegistry).where(
                        ColumnRegistry.table_name == table_name
                    ).order_by(ColumnRegistry.column_name)
                ).scalars().all()
            )

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    @staticmethod
    def _quote_table(name: str) -> str:
        """SQLite-safe 双引号表名"""
        safe = name.replace("\"", "\"\"")
        return f'"{safe}"'

    @staticmethod
    def _sanitize_for_sql(df: pd.DataFrame) -> pd.DataFrame:
        """NaT/NaN/NA → None，适配 SQLite TEXT 绑参（防 NaTType/NAType 崩溃）

        只处理含缺失值的列（快路径不 copy）；这些列先整体 astype(object) 再
        .where(mask, None)——pandas 3.x 下若保持 datetime64/float/str dtype，
        None 会被强转回 NaT/nan/NA，只有 object 列收得住 None。
        """
        if df is None or df.empty:
            return df
        missing = [c for c in df.columns if df[c].isna().any()]
        if not missing:
            return df
        df = df.copy()
        for c in missing:
            df[c] = df[c].astype(object)
        return df.where(pd.notnull(df), None)

    @staticmethod
    def _quote_column(name: str) -> str:
        """SQLite-safe 双引号列名"""
        safe = name.replace("\"", "\"\"")
        return f'"{safe}"'
