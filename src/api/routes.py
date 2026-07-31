"""API 路由 — 数据查询、元数据、连接监控、同步控制"""

import time
from datetime import datetime

from fastapi import APIRouter, Query, Depends, HTTPException

from src.api.schemas import (
    DataResponse, ErrorResponse, HealthResponse,
    ConnectionInfo, ConnectionListResponse,
    StockQueryParams, SyncTriggerResponse,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# 依赖注入：获取数据库引擎（由 server.py 在启动时注入）
# ---------------------------------------------------------------------------

_repo = None
_scheduler = None


def set_repo(repo):
    """由 server.py 在初始化时注入"""
    global _repo
    _repo = repo


def set_scheduler(scheduler):
    """由 server.py 在初始化时注入定时调度器"""
    global _scheduler
    _scheduler = scheduler


def get_repo():
    if _repo is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")
    return _repo


# ---------------------------------------------------------------------------
# 连接监控（由 middleware 写入）
# ---------------------------------------------------------------------------

_client_stats: dict[str, dict] = {}  # client_ip -> stats
_stats_lock = None  # threading.Lock, set by server.py


def set_stats_lock(lock):
    global _stats_lock
    _stats_lock = lock


def record_request(client_ip: str, user_agent: str, path: str):
    """记录一次客户端请求"""
    if _stats_lock is None:
        return
    with _stats_lock:
        now = datetime.now().isoformat()
        if client_ip not in _client_stats:
            _client_stats[client_ip] = {
                "client_ip": client_ip,
                "user_agent": user_agent or "unknown",
                "first_seen": now,
                "last_seen": now,
                "request_count": 0,
                "paths": [],
            }
        stats = _client_stats[client_ip]
        stats["last_seen"] = now
        stats["request_count"] += 1
        if path not in stats["paths"]:
            stats["paths"].append(path)
            if len(stats["paths"]) > 20:
                stats["paths"] = stats["paths"][-20:]


# ---------------------------------------------------------------------------
# 健康检查
# ---------------------------------------------------------------------------


@router.get("/health", response_model=HealthResponse, tags=["系统"])
async def health_check(repo=Depends(get_repo)):
    """系统健康状态"""
    try:
        row_count = repo.count_rows("meta_sync_jobs")
    except Exception:
        row_count = 0

    scheduler_running = bool(_scheduler and _scheduler.is_running())

    return HealthResponse(
        status="ok",
        timestamp=datetime.now().isoformat(),
        db_rows=row_count,
        scheduler_running=scheduler_running,
    )


# ---------------------------------------------------------------------------
# 连接监控
# ---------------------------------------------------------------------------


@router.get("/connections", response_model=ConnectionListResponse, tags=["系统"])
async def list_connections():
    """查看当前连接的客户端"""
    total_requests = 0
    conns = []
    for ip, stats in _client_stats.items():
        total_requests += stats["request_count"]
        conns.append(ConnectionInfo(**stats))

    conns.sort(key=lambda c: c.last_seen, reverse=True)

    return ConnectionListResponse(
        total_connections=len(conns),
        total_requests=total_requests,
        connections=conns,
    )


# ---------------------------------------------------------------------------
# 数据源元数据
# ---------------------------------------------------------------------------


@router.get("/sources", tags=["元数据"])
async def list_sources(repo=Depends(get_repo)):
    """列出所有可用数据源"""
    from src.utils.config import ConfigManager
    from src.core.fetcher_registry import FetcherRegistry
    registry = FetcherRegistry(ConfigManager())
    all_src = registry.get_all_sources()

    return DataResponse(
        data=[{
            "source_key": s["source_key"],
            "name": s.get("name", ""),
            "api_function": s.get("api_function", ""),
            "table_name": s.get("table_name", ""),
            "has_incremental": s.get("has_incremental", False),
        } for s in all_src],
        total=len(all_src),
    )


@router.get("/sources/{source_key}/status", tags=["元数据"])
async def get_source_status(source_key: str, repo=Depends(get_repo)):
    """查询单个数据源的同步状态"""
    from src.db.models import SyncJob
    from sqlalchemy import select

    stmt = select(SyncJob).where(SyncJob.module_name == source_key)
    with repo.engine.connect() as conn:
        result = conn.execute(stmt).fetchone()

    if result is None:
        return DataResponse(data=[], total=0, message="未找到同步记录")

    # 转 dict
    from sqlalchemy.engine import Row
    row = result._asdict() if hasattr(result, '_asdict') else dict(result)
    return DataResponse(data=[row], total=1)


# ---------------------------------------------------------------------------
# 宏观数据
# ---------------------------------------------------------------------------


def _filter_by_indicator(df, indicator: str = None):
    """按指标名称过滤 DataFrame（自动识别指标名列）"""
    if not indicator:
        return df
    ind_col = None
    for c in df.columns:
        if any(kw in c for kw in ("指标", "indicator", "名称")):
            ind_col = c
            break
    if ind_col is not None:
        df = df[df[ind_col].astype(str).str.contains(indicator, na=False)]
    return df


def _filter_by_date_range(df, start_date: str = None, end_date: str = None):
    """按日期区间过滤 DataFrame（自动识别日期列）

    start_date/end_date 为 YYYYMMDD 字符串；无日期列时原样返回。
    """
    import pandas as pd

    if df is None or df.empty:
        return df
    if not start_date and not end_date:
        return df

    date_col = None
    for candidate in ("date", "日期", "时间", "月份", "trade_date", "TRADE_DATE"):
        if candidate in df.columns:
            date_col = candidate
            break
    if date_col is None:
        return df

    try:
        if not pd.api.types.is_datetime64_any_dtype(df[date_col]):
            df = df.copy()
            df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
    except Exception:
        return df

    mask = pd.Series(True, index=df.index)
    if start_date:
        try:
            mask &= df[date_col] >= pd.to_datetime(start_date)
        except Exception:
            pass
    if end_date:
        try:
            # 包含结束日期当天
            mask &= df[date_col] < pd.to_datetime(end_date) + pd.Timedelta(days=1)
        except Exception:
            pass
    return df[mask].copy()


@router.get("/macro/cpi", tags=["宏观数据"])
async def query_cpi(
    indicator: str = Query(None, description="指标名称"),
    start_date: str = Query(None, pattern=r"^\d{8}$"),
    end_date: str = Query(None, pattern=r"^\d{8}$"),
    limit: int = Query(500, le=10000),
    repo=Depends(get_repo),
):
    """查询CPI数据（支持按指标名称与日期区间过滤）"""
    import pandas as pd

    tables = [
        "macro_china_cpi_yearly",
        "macro_china_cpi_monthly",
        "macro_usa_cpi_yoy",
        "macro_usa_core_cpi_monthly",
        "macro_euro_cpi_yoy",
        "macro_japan_cpi_yearly",
        "macro_uk_cpi_yearly",
        "macro_australia_cpi_yearly",
        "macro_japan_core_cpi_yearly",
    ]

    # ★ 每张表先用各自的日期列/指标列独立过滤，再拼接
    #   （各表日期列名不统一：有的叫"时间"、有的叫"日期"，concat 后无法统一过滤）
    all_data = []
    for tbl in tables:
        try:
            df = repo.query(tbl, limit=limit)
            if df.empty:
                continue
            if indicator:
                df = _filter_by_indicator(df, indicator)
            df = _filter_by_date_range(df, start_date, end_date)
            if not df.empty:
                all_data.append(df)
        except Exception:
            pass

    if not all_data:
        return DataResponse(data=[], total=0, message="暂无CPI数据")

    combined = pd.concat(all_data, ignore_index=True)

    # 按日期列倒序排列（最新数据在前）
    if not combined.empty:
        for _col in combined.columns:
            if any(kw in _col.lower() for kw in ("date", "时间", "日期", "trade")):
                combined = combined.sort_values(by=_col, ascending=False)
                break
    total = len(combined)
    data = combined.head(limit).to_dict(orient="records")

    return DataResponse(data=data, total=total)


@router.get("/macro/{table_name}", tags=["宏观数据"])
async def query_macro_generic(
    table_name: str,
    start_date: str = Query(None, pattern=r"^\d{8}$"),
    end_date: str = Query(None, pattern=r"^\d{8}$"),
    limit: int = Query(500, le=10000),
    repo=Depends(get_repo),
):
    """通用宏观数据查询（按表名，支持日期区间过滤）"""
    try:
        df = repo.query(f"macro_{table_name}", limit=limit)
    except Exception:
        try:
            df = repo.query(table_name, limit=limit)
        except Exception:
            return DataResponse(data=[], total=0, message=f"表 {table_name} 不存在或无数据")

    if not df.empty:
        # 日期区间过滤
        df = _filter_by_date_range(df, start_date, end_date)
        # 按日期列倒序排列
        if not df.empty:
            for _col in df.columns:
                if any(kw in _col.lower() for kw in ("date", "时间", "日期", "trade")):
                    df = df.sort_values(by=_col, ascending=False)
                    break

    return DataResponse(
        data=df.to_dict(orient="records") if not df.empty else [],
        total=len(df),
    )


# ---------------------------------------------------------------------------
# 股票数据
# ---------------------------------------------------------------------------


@router.get("/stock/daily", tags=["股票数据"])
async def query_stock_daily(
    symbol: str = Query(..., description="股票代码"),
    start_date: str = Query(None, pattern=r"^\d{8}$"),
    end_date: str = Query(None, pattern=r"^\d{8}$"),
    limit: int = Query(5000, le=50000),
    repo=Depends(get_repo),
):
    """查询A股日线行情（需先同步 stock_daily 表）"""
    filters = {"symbol": symbol}
    df = repo.query("stock_daily", filters=filters, limit=limit)

    if df.empty:
        return DataResponse(data=[], total=0,
                            message="暂无数据，请先在桌面端同步该股票")

    # 按日期列倒序排列
    for _col in df.columns:
        if any(kw in _col.lower() for kw in ("date", "时间", "日期", "trade")):
            df = df.sort_values(by=_col, ascending=False)
            break

    return DataResponse(
        data=df.to_dict(orient="records"),
        total=len(df),
    )


# ---------------------------------------------------------------------------
# 同步控制
# ---------------------------------------------------------------------------


@router.post("/sync/{source_key}", response_model=SyncTriggerResponse,
             status_code=202, tags=["同步控制"])
async def trigger_sync(source_key: str):
    """触发指定数据源的同步（异步执行）"""
    from src.core.data_fetcher import DataFetcher
    from src.core.sync_engine import SyncEngine
    from src.core.dynamic_schema import DynamicSchemaManager
    from src.core.fetcher_registry import FetcherRegistry
    from src.db.engine import get_engine
    from src.db.repository import DataRepository
    from src.utils.config import ConfigManager
    import threading

    # ★ 先校验数据源是否存在，避免后台线程空跑报错
    registry = FetcherRegistry(ConfigManager())
    if not registry.get_source(source_key):
        raise HTTPException(
            status_code=404,
            detail=f"未找到数据源: {source_key}，可用源见 /sources",
        )

    config = ConfigManager()
    engine = get_engine()
    repo = DataRepository(engine)
    schema = DynamicSchemaManager(repo)
    fetcher = DataFetcher(config)
    sync = SyncEngine(fetcher, repo, schema, config)

    def _run():
        sync.run(source_key)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    return SyncTriggerResponse(
        source_key=source_key,
        status="accepted",
        message=f"同步任务已提交: {source_key}",
    )
