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
from sqlalchemy.orm import Session

from src.utils.config import ConfigManager
from src.db.models import Base, SyncJob, ColumnRegistry, CacheEntry, utcnow

logger = logging.getLogger("bern.db.repository")


def _now_utc() -> datetime:
    """当前 UTC 时间（naive）"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class DataRepository:
    """通用数据仓库 — 封装 CRUD、缓存、动态列管理"""

    def __init__(self, engine: Engine):
        self.engine = engine

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
        """遍历 data_catalog.yaml 收集所有启用的数据源叶节点"""
        config = ConfigManager()
        catalog = config.catalog
        categories = catalog.get("categories", [])

        sources: list[dict] = []

        def _collect(nodes: list) -> None:
            for node in nodes:
                children = node.get("children")
                if children:
                    _collect(children)
                elif node.get("api_source") and node.get("table_name"):
                    # 叶节点
                    sources.append(dict(node))

        _collect(categories)
        return sources

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
                return date.fromisoformat(val)
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
    DATE_COLUMN_KEYWORDS = ("date", "日期", "时间", "月份", "trade_date", "datetime")

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

        # 解析唯一键到表内真实列（如 date -> 时间）
        unique_cols = self.resolve_date_columns(table_name, unique_columns or [])
        if unique_cols:
            self.ensure_unique_index(table_name, unique_cols, dedupe=True)

        total = 0
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
                    stmt = stmt.prefix_with("OR REPLACE")
                conn.execute(stmt)
                total += len(batch)

            conn.commit()

        logger.info("bulk_upsert %s: %d 行已处理（唯一键=%s）",
                    table_name, total, unique_cols)
        return total

    def analyze_import(
        self,
        table_name: str,
        df: pd.DataFrame,
        unique_columns: list[str] | None = None,
    ) -> dict:
        """估算导入影响：更新/新增/文件内重复/库内现存重复

        用于导入对话框预览。返回:
            total, to_insert, to_update, to_skip,
            dup_in_db, unique_columns, missing_unique_cols
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
        if cols:
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

        # 与库内已有键比对
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

    def query(
        self,
        table_name: str,
        filters: dict | None = None,
        order_by: str | None = None,
        limit: int | None = None,
    ) -> pd.DataFrame:
        """查询数据表返回 DataFrame（表不存在返回空 DataFrame）"""
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

        sql = f"SELECT * FROM {safe_t}"
        if where_clauses:
            sql += " WHERE " + " AND ".join(where_clauses)
        if order_by:
            sql += f" ORDER BY {order_by}"
        if limit is not None:
            sql += f" LIMIT {limit}"

        with self.engine.connect() as conn:
            return pd.read_sql_query(sql, conn, params=params)

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
                session.commit()

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
    def _quote_column(name: str) -> str:
        """SQLite-safe 双引号列名"""
        safe = name.replace("\"", "\"\"")
        return f'"{safe}"'
