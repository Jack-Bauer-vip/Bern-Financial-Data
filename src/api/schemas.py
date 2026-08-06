"""Pydantic 请求/响应模型 — FastAPI 数据服务"""

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# 通用响应
# ---------------------------------------------------------------------------


class DataResponse(BaseModel):
    """统一数据响应"""
    code: int = 200
    message: str = "ok"
    data: list[dict] = []
    total: int = 0
    source: str = "local_db"
    data_status: str = "active"  # active / deprecated / local
    meta: Optional[dict] = None  # 附加元信息（如指标口径 unit_type/unit_desc）


class ErrorResponse(BaseModel):
    """错误响应"""
    code: int
    message: str
    detail: Optional[str] = None


# ---------------------------------------------------------------------------
# 健康检查
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    status: str
    version: str = "0.1.0"
    timestamp: str
    db_rows: int = 0
    scheduler_running: bool = False
    stale_sources: list[dict] = []  # 滞后/停更的数据源（不含 deprecated）
    stale_count: int = 0


# ---------------------------------------------------------------------------
# 连接监控
# ---------------------------------------------------------------------------


class ConnectionInfo(BaseModel):
    client_ip: str
    user_agent: str
    first_seen: str
    last_seen: str
    request_count: int
    paths: list[str] = []


class ConnectionListResponse(BaseModel):
    total_connections: int
    total_requests: int
    connections: list[ConnectionInfo]


# ---------------------------------------------------------------------------
# 数据查询请求
# ---------------------------------------------------------------------------


class MacroQueryParams(BaseModel):
    indicator: Optional[str] = Field(None, description="指标名称筛选")
    start_date: Optional[str] = Field(None, pattern=r"^\d{8}$")
    end_date: Optional[str] = Field(None, pattern=r"^\d{8}$")
    limit: int = Field(100, le=10000)


class StockQueryParams(BaseModel):
    symbol: str = Field(..., description="股票代码")
    start_date: Optional[str] = Field(None, pattern=r"^\d{8}$")
    end_date: Optional[str] = Field(None, pattern=r"^\d{8}$")
    limit: int = Field(5000, le=50000)


class SyncTriggerResponse(BaseModel):
    source_key: str
    status: str
    message: str
