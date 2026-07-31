"""SQLAlchemy ORM 模型定义"""

from datetime import date, datetime, timezone
from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    """返回当前 UTC 时间（不含时区信息，适配 SQLite）"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    """声明式基类"""
    pass


# ---------------------------------------------------------------------------
# 通用数据表 Mixin
# ---------------------------------------------------------------------------

class BaseDataModel:
    """数据表通用字段 mixin — 子类继承后自动获得 id / created_at / updated_at"""

    __abstract__ = True

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


# ---------------------------------------------------------------------------
# 元数据表
# ---------------------------------------------------------------------------

class SyncJob(Base):
    """同步任务注册表 — 记录每个模块的最后同步状态"""

    __tablename__ = "meta_sync_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    module_name: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(String(128))
    category: Mapped[str] = mapped_column(String(32))
    api_source: Mapped[str] = mapped_column(String(16))
    api_function: Mapped[str] = mapped_column(String(128))
    last_sync_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_sync_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    schedule_cron: Mapped[str | None] = mapped_column(String(32), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    def __repr__(self) -> str:
        return f"<SyncJob {self.module_name} status={self.status}>"


class ColumnRegistry(Base):
    """动态列注册表 — 记录自动添加到数据表的列信息"""

    __tablename__ = "meta_column_registry"
    __table_args__ = (
        UniqueConstraint("table_name", "column_name", name="uq_table_column"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    table_name: Mapped[str] = mapped_column(String(64))
    column_name: Mapped[str] = mapped_column(String(64))
    column_type: Mapped[str] = mapped_column(String(32), default="TEXT")
    is_auto_added: Mapped[bool] = mapped_column(Boolean, default=True)
    source_api: Mapped[str | None] = mapped_column(String(128), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    description: Mapped[str | None] = mapped_column(String(256), nullable=True)

    def __repr__(self) -> str:
        return f"<ColumnRegistry {self.table_name}.{self.column_name}>"


class CacheEntry(Base):
    """查询缓存条目 — 避免重复拉取相同 API"""

    __tablename__ = "meta_cache_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cache_key: Mapped[str] = mapped_column(String(128), unique=True)
    query_params: Mapped[str | None] = mapped_column(Text, nullable=True)
    data_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    ttl_seconds: Mapped[int] = mapped_column(Integer, default=300)

    def __repr__(self) -> str:
        return f"<CacheEntry {self.cache_key} rows={self.row_count}>"
