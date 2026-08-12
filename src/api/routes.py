"""API 路由 — 数据查询、元数据、连接监控、同步控制、主题看板"""

import io
import threading
import time
from datetime import datetime

from fastapi import APIRouter, Query, Depends, HTTPException
from fastapi.responses import StreamingResponse

from src.api.schemas import (
    DataResponse, ErrorResponse, HealthResponse,
    ConnectionInfo, ConnectionListResponse,
    StockQueryParams, SyncTriggerResponse,
)

router = APIRouter()

# 限制同时进行的同步线程数，防止每次 /sync 请求都无上限创建线程
_sync_semaphore = threading.BoundedSemaphore(4)

# 行情表 → 资产类型（/search 读取 meta_asset_info 名称用；index 无 adj_factor_asset_type）
TABLE_ASSET_TYPE = {
    "fund_etf_daily": "fund",
    "stock_daily": "stock",
    "index_daily": "index",
}


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
    """系统健康状态（含数据新鲜度——滞后/停更源列表，不含 deprecated）"""
    try:
        row_count = repo.count_rows("meta_sync_jobs")
    except Exception:
        row_count = 0

    scheduler_running = bool(_scheduler and _scheduler.is_running())

    stale_list: list[dict] = []
    try:
        from src.core.freshness import collect_source_freshness
        from src.core.fetcher_registry import FetcherRegistry
        from src.utils.config import ConfigManager
        registry = FetcherRegistry(ConfigManager())
        for f in collect_source_freshness(repo, registry):
            if f.status_label != "✅ 正常" and not f.health_check_ignore:
                stale_list.append({
                    "source_key": f.source_key,
                    "name": f.name,
                    "status": f.status_label,
                    "last_date": f.last_date.isoformat() if f.last_date else None,
                    "days_since": f.days_since,
                })
    except Exception:
        pass

    return HealthResponse(
        status="ok",
        timestamp=datetime.now().isoformat(),
        db_rows=row_count,
        scheduler_running=scheduler_running,
        stale_sources=stale_list,
        stale_count=len(stale_list),
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
async def list_sources(
    include_deprecated: bool = Query(
        True, description="是否包含 deprecated 源（上游停更、保留历史的源）"),
    repo=Depends(get_repo),
):
    """列出所有可用数据源

    include_deprecated=False 时过滤 data_status == deprecated 的源
    （默认 True 保持现状，零破坏）。
    """
    from src.utils.config import ConfigManager
    from src.core.fetcher_registry import FetcherRegistry
    registry = FetcherRegistry(ConfigManager())
    all_src = registry.get_all_sources()

    if not include_deprecated:
        all_src = [s for s in all_src if not s.get("deprecated")]

    return DataResponse(
        data=[{
            "source_key": s["source_key"],
            "name": s.get("name", ""),
            "api_function": s.get("api_function", ""),
            "table_name": s.get("table_name", ""),
            "has_incremental": s.get("has_incremental", False),
            "data_status": "deprecated" if s.get("deprecated") else "active",
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
    for candidate in ("date", "日期", "时间", "月份", "季度", "trade_date", "TRADE_DATE"):
        if candidate in df.columns:
            date_col = candidate
            break
    if date_col is None:
        return df

    try:
        if not pd.api.types.is_datetime64_any_dtype(df[date_col]):
            df = df.copy()
            # 中文「2026年06月份 / 2026年一季度」先归一化为 ISO(2026-06 / 2026-03),
            # 否则 pd.to_datetime 直接解析中文日期全变 NaT → 兜底过滤把整表误杀
            from src.utils.date_parse import normalize_cn_date_str
            df[date_col] = pd.to_datetime(
                df[date_col].map(lambda v: normalize_cn_date_str(v)
                                 if isinstance(v, str) else v),
                errors="coerce")
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


def _adj_factor_info(table_name: str) -> tuple | None:
    """查 catalog 找该表的复权因子归属，返回 (asset_type, factor_table, code_col)；无 → None

    仅带 adj_factor_asset_type 的行情表（股票/ETF）支持复权；指数/宏观/导入表
    无此字段 → None（调用方对 ?adj= 抛 422）。
    """
    try:
        from src.core.fetcher_registry import FetcherRegistry
        from src.utils.config import ConfigManager
        for s in FetcherRegistry(ConfigManager()).get_all_sources():
            if s.get("table_name") != table_name:
                continue
            asset_type = s.get("adj_factor_asset_type")
            if not asset_type:
                return None
            code_col = s.get("code_column") or "code"
            return (str(asset_type), str(s.get("adj_factor_table", "asset_adj_factor")),
                    code_col)
    except Exception:
        pass
    return None


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
        "macro_china_cpi",
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
            # 指标归一层：该表对应指标已设获信源 → 用获信数据（统一接口），
            #   否则退回原始表。如 us.cpi 获信源设为 FRED → 这里返回 FRED 数据
            ind = _indicator_for_table(tbl)
            if ind:
                mapping = repo.get_indicator_map(ind)
                if mapping:
                    df = repo.get_indicator(ind, limit=limit)
                    if not df.empty:
                        df = _filter_by_date_range(df, start_date, end_date)
                        if not df.empty:
                            all_data.append(df)
                    continue
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
    data = _df_to_json_records(combined, limit)

    return DataResponse(data=data, total=total)


def _indicator_for_table(table_name: str) -> str | None:
    """查数据目录中该表对应的 indicator 键（无则 None）

    用于让 /macro/cpi 等聚合端点走指标归一层：某表对应指标已设获信源时，
    用获信数据替代原始表。
    """
    try:
        from src.core.fetcher_registry import FetcherRegistry
        from src.utils.config import ConfigManager
        for s in FetcherRegistry(ConfigManager()).get_all_sources():
            if s.get("table_name") == table_name and s.get("indicator"):
                return str(s["indicator"])
    except Exception:
        return None
    return None


def _macro_table_names() -> set[str]:
    """收集宏观数据表名白名单（来自数据分类目录的 macro 分类）

    用于 /macro/{table_name} 白名单校验，防止把任意表（尤其 meta_* 元数据表）
    暴露为可查询对象。
    """
    try:
        from src.core.fetcher_registry import FetcherRegistry
        from src.utils.config import ConfigManager
        reg = FetcherRegistry(ConfigManager())
        return {
            cfg.get("table_name")
            for cfg in reg.get_macro_sources()
            if cfg.get("table_name")
        }
    except Exception:
        return set()


@router.get("/macro/{table_name}", tags=["宏观数据"])
async def query_macro_generic(
    table_name: str,
    start_date: str = Query(None, pattern=r"^\d{8}$"),
    end_date: str = Query(None, pattern=r"^\d{8}$"),
    limit: int = Query(500, le=10000),
    repo=Depends(get_repo),
):
    """通用宏观数据查询（按表名，支持日期区间过滤）

    表名必须命中宏观数据白名单（macro_ 前缀数据表），否则 404，
    防止通过本端点枚举/查询任意表（含 meta_* 元数据表）。
    """
    # 白名单校验：只允许宏观数据表（兼容传入完整表名或去掉 macro_ 前缀）
    macro_tables = _macro_table_names()
    if f"macro_{table_name}" in macro_tables:
        query_name = f"macro_{table_name}"
    elif table_name in macro_tables:
        query_name = table_name
    else:
        raise HTTPException(
            status_code=404,
            detail=f"宏观数据表 {table_name} 不存在或不可查询",
        )

    # 带日期参数时绕过缓存（同 /data：先 limit 再过滤，缓存固定切片会裁错日期）
    from src.core.ttl_cache import cache

    use_cache = not (start_date or end_date)
    cache_key = f"macro:{query_name}:{limit}"
    df = None
    if use_cache:
        df = cache.get(cache_key)
    if df is None:
        try:
            df = repo.query(query_name, limit=limit)
        except Exception:
            # 白名单内的表可能尚未同步（库中还没有该表），优雅返回空
            return DataResponse(data=[], total=0, message=f"表 {table_name} 暂无数据")
        if use_cache and len(df) <= 100_000:
            cache.set(cache_key, df)

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
        data=_df_to_json_records(df) if not df.empty else [],
        total=len(df),
    )


# ---------------------------------------------------------------------------
# 指标归一层（indicator）— 统一指标查询，隐藏跨源重复
# ---------------------------------------------------------------------------


@router.get("/indicator", tags=["指标"])
async def list_indicators(repo=Depends(get_repo)):
    """列出全部指标映射（indicator → 获信源表 + 列映射）"""
    return DataResponse(data=repo.list_indicators(), total=len(repo.list_indicators()))


def _ymd_to_iso(value: str | None) -> str | None:
    """YYYYMMDD → YYYY-MM-DD（SQL 侧日期比较用，库内 TEXT 为 ISO 格式）"""
    if not value:
        return None
    v = str(value).strip()
    if len(v) == 8 and v.isdigit():
        return f"{v[:4]}-{v[4:6]}-{v[6:]}"
    return v


def _df_to_json_records(df, limit: int | None = None) -> list[dict]:
    """DataFrame → records，NaN/NaT→None、Timestamp/date→ISO 字符串

    Starlette JSONResponse 用 json.dumps(allow_nan=False)，NaN/Inf 直接序列化
    会 500；pd.Timestamp/pd.NaT 混入 record 还会触发 pydantic 对 list[dict] 的
    序列化异常（dict 结构不一致时）。统一清洗成 JSON 安全类型：
    None / NaN / NaT → None；Timestamp/date/datetime → ISO 日期字符串。
    """
    import pandas as pd

    if df is None or df.empty:
        return []
    data = df.head(limit) if limit is not None else df
    out = []
    for rec in data.to_dict(orient="records"):
        cleaned = {}
        for k, v in rec.items():
            if v is None:
                cleaned[k] = None
            elif isinstance(v, pd.Timestamp):
                cleaned[k] = None if pd.isna(v) else v.isoformat()[:10]
            elif isinstance(v, (datetime,)):
                # pd.NaT 是 datetime 子类，其 isoformat() 返回 "NaT"，先判缺失
                cleaned[k] = None if pd.isna(v) else v.isoformat()[:10]
            elif isinstance(v, float) and pd.isna(v):
                cleaned[k] = None
            else:
                # 兜底：pd.NaT 等其它 pandas 标量缺失值
                try:
                    r = pd.isna(v)
                    cleaned[k] = None if isinstance(r, bool) and r else v
                except Exception:
                    cleaned[k] = v
        out.append(cleaned)
    return out


def _csv_bytes(df) -> bytes:
    """DataFrame → CSV bytes(utf-8-sig,带 BOM 供 Excel 直接打开)"""
    import pandas as pd
    if df is None or df.empty:
        df = pd.DataFrame()
    buf = io.StringIO()
    df.to_csv(buf, index=False, encoding="utf-8-sig")
    return buf.getvalue().encode("utf-8-sig")


def _csv_response(df, filename: str) -> StreamingResponse:
    """把 DataFrame 以附件形式返回(stream 流式,大表不占额外内存)"""
    data = _csv_bytes(df)
    return StreamingResponse(
        iter([data]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


def _paginate(df, page: int | None, page_size: int | None):
    """可选分页切片,返回 (切片 DataFrame, 分页元数据 dict | None)

    page_size 恒为 limit(页大小)。page=None 时仅 head(limit) 截断(维持旧行为,
    无分页元数据);page 给出时按 offset 切片并附带 meta.pagination。
    """
    if page_size is None or page_size <= 0:
        return df, None
    if page is None:
        return df.head(page_size), None
    total = len(df)
    offset = (page - 1) * page_size
    sliced = df.iloc[offset: offset + page_size].copy() if offset < total \
        else df.iloc[0:0].copy()
    has_more = offset + page_size < total
    return sliced, {"page": page, "page_size": page_size, "has_more": has_more}


def _format_param() -> str:
    """format 查询参数的公共定义(json|csv,缺省 json)"""
    return Query("json", pattern=r"^(json|csv)$",
                 description="响应格式: json(默认) | csv(下载, 带 BOM)")


def _page_param() -> int | None:
    """分页参数公共定义(可选,传了才分页;与 limit 配合做页大小)"""
    return Query(None, ge=1, description="页码(1 起); 不传则不分页, 返回全部/limit 行")


@router.get("/indicator/{indicator_key}", tags=["指标"])
async def get_indicator(
    indicator_key: str,
    transform: str = Query(None, pattern=r"^(level|yoy|mom|pct)$",
                           description="派生视图：level 原始(默认) | yoy 同比% | mom 环比% | pct=环比别名"),
    start_date: str = Query(None, pattern=r"^\d{8}$"),
    end_date: str = Query(None, pattern=r"^\d{8}$"),
    limit: int = Query(5000, le=100000),
    format: str = _format_param(),
    page: int | None = _page_param(),
    repo=Depends(get_repo),
):
    """统一查询指标（按获信源表），返回 {date, value}

    指标键如 us.unemployment、us.cpi。无获信映射 → 空。
    统一经 compute_transform 数值化：**默认(不带 transform 或 level)也 to_numeric
    返回 float**（此前返回库内 TEXT 字符串，消费端要自己 float()）；yoy/mom/pct
    为派生视图。缓存原始 {date,value}（同步成功后由 SyncEngine 单点失效）。

    响应 meta 携带**存储值口径**（unit_type/unit_desc，来自获信源所在 catalog 节点）
    与本次请求的 transform。**守则：transform 与派生逻辑完全不变，unit 只描述
    原始存储值**——transform=level 时 data 即原始存储口径；transform=yoy/mom 时
    是派生结果。二者互不干扰。
    """
    from src.core.ttl_cache import cache

    cache_key = f"ind:{indicator_key}"
    df = cache.get(cache_key)
    if df is None:
        df = repo.get_indicator(indicator_key)  # 全量，供派生对齐前值/级别限制
        if len(df) <= 100_000:
            cache.set(cache_key, df)

    # 存储值口径：从获信源映射取（P0）
    unit_type, unit_desc = "level", None
    mapping = repo.get_indicator_map(indicator_key)
    if mapping:
        unit_type = mapping.get("unit_type") or "level"
        unit_desc = mapping.get("unit_desc")

    if df.empty:
        return DataResponse(
            data=[], total=0, source=f"indicator:{indicator_key}",
            meta={"unit_type": unit_type, "unit_desc": unit_desc,
                  "transform": transform or "level"})

    # 统一数值化：默认 level 也 to_numeric → float；yoy/mom/pct 派生
    from src.core.transform import compute_transform
    df = compute_transform(df, transform or "level")

    if not df.empty:
        df = _filter_by_date_range(df, start_date, end_date)
        df = df.sort_values("date", ascending=False).reset_index(drop=True)

    # 对外协商：format=csv 下载 / ?page= 分页(meta.pagination, total 恒为全量)
    if format == "csv":
        return _csv_response(df, f"{indicator_key}.csv")
    sliced, pagination = _paginate(df, page, limit)
    meta = {"unit_type": unit_type, "unit_desc": unit_desc,
            "transform": transform or "level"}
    if pagination:
        meta["pagination"] = pagination
    return DataResponse(
        data=_df_to_json_records(sliced),
        total=len(df),
        source=f"indicator:{indicator_key}",
        meta=meta,
    )


@router.put("/indicator/{indicator_key}", tags=["指标"])
async def set_indicator(
    indicator_key: str,
    body: dict = {},
    repo=Depends(get_repo),
):
    """手动设置某指标的获信源表

    body: {"table": "macro_fred_unemployment"}
    """
    table = (body or {}).get("table")
    if not table:
        raise HTTPException(status_code=400, detail="缺少 table 字段")
    record = repo.set_indicator(indicator_key, table)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"表 {table} 不存在或无法解析日期/数值列",
        )
    return DataResponse(data=[record], total=1)


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
    df = repo.query(
        "stock_daily", filters=filters, limit=limit,
        date_from=_ymd_to_iso(start_date), date_to=_ymd_to_iso(end_date))

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
# 通用数据接口（数据分发）
# ---------------------------------------------------------------------------


def _resolve_code_column(repo, table_name: str) -> str | None:
    """解析表的代码列（code/symbol/ts_code 按优先级探测命中表内实际列）。

    委托 repository.detect_code_column（候选列集中定义，防与前端 CODE_KEYWORDS 漂移）。
    表不存在 → None；调用方据此决定 422 或空结果。
    """
    return repo.detect_code_column(table_name)


def _valid_table_names(repo) -> list[str]:
    """收集所有可查询的数据表名（排除 meta_ 表）"""
    try:
        from src.importer.matcher import collect_tables
        return [t.table_name for t in collect_tables(repo)]
    except Exception:
        return []


@router.get("/data/tables", tags=["数据分发"])
async def list_data_tables(repo=Depends(get_repo)):
    """列出所有可查询的数据表（供其他模块发现可用数据）"""
    tables = _valid_table_names(repo)
    return DataResponse(data=[{"table_name": t} for t in tables], total=len(tables))


@router.get("/data/{table_name}", tags=["数据分发"])
async def query_data_table(
    table_name: str,
    start_date: str = Query(None, pattern=r"^\d{8}$"),
    end_date: str = Query(None, pattern=r"^\d{8}$"),
    limit: int = Query(200, le=100000),
    fields: str = Query(None, description="逗号分隔的字段列表"),
    format: str = _format_param(),
    adj: str = Query(None, pattern=r"^(qfq|hfq)$",
                     description="复权：qfq前复权 | hfq后复权（仅股票/ETF行情表）"),
    code: str = Query(None, description="按代码列精确过滤（code/symbol/ts_code，仅对有代码列的表有效）"),
    repo=Depends(get_repo),
):
    """通用数据表查询（数据分发）

    按表名查询任意数据表，支持日期区间、代码过滤、字段选择、行数限制、复权派生。
    表名必须存在于候选集合（防注入）。
    data_status：catalog 源 active/deprecated；非 catalog 导入表 local。
    未传日期区间时默认只返回 limit 条并提示（大表建议带 start_date/end_date）。

    adj=qfq|hfq：对股票/ETF 行情表 OHLC 做前/后复权派生（行情表存不复权
    原始价，复权因子表 asset_adj_factor 派生）。指数/宏观等无因子源表 → 422。
    code=：单标的精确过滤（如 fund_etf_daily?code=159915、index_daily?code=sh000001），
    代码列自动识别 code/symbol/ts_code；无任何代码列的表 → 422。
    """
    # 表名白名单校验（防注入）
    valid = _valid_table_names(repo)
    if table_name not in valid:
        raise HTTPException(
            status_code=404,
            detail=f"数据表 {table_name} 不存在或不可查询，可用表见 /data/tables",
        )

    # data_status：从 catalog 查表归属；非 catalog 表（CSV 导入等）→ local
    data_status = "local"
    try:
        from src.core.fetcher_registry import FetcherRegistry
        from src.utils.config import ConfigManager
        for s in FetcherRegistry(ConfigManager()).get_all_sources():
            if s.get("table_name") == table_name:
                data_status = "deprecated" if s.get("deprecated") else "active"
                break
    except Exception:
        pass

    # 查询：带日期参数时把区间过滤下沉到 SQL（用日期索引，避免「先 limit 再
    # pandas 过滤」在大表上把目标日期裁掉）；无日期参数走缓存（固定 limit 切片）。
    # code= 同日期参数：下沉 SQL 且绕过缓存（缓存里是别的 code 的切片）。
    from src.core.ttl_cache import cache

    # 代码列解析：兼容 code/symbol/ts_code（index_daily/stock_daily 用 symbol）。
    # 表不存在 → 白名单校验已在上方 404，到不了这里；存在但无代码列 → 422。
    code_col = None
    if code:
        code_col = _resolve_code_column(repo, table_name)
        if code_col is None:
            raise HTTPException(
                status_code=422,
                detail=f"表 {table_name} 无代码列(code/symbol/ts_code)，不支持按代码过滤",
            )

    # 缓存键含 adj 与代码列：复权是派生口径、不同代码列口径的缓存结果必须隔离
    use_cache = not (start_date or end_date or code)
    cache_key = f"data:{table_name}:{limit}:{adj or 'raw'}:{code or ''}:{code_col or ''}"
    df = None
    if use_cache:
        df = cache.get(cache_key)
    if df is None:
        try:
            df = repo.query(
                table_name, filters={code_col: code} if code else None, limit=limit,
                date_from=_ymd_to_iso(start_date),
                date_to=_ymd_to_iso(end_date),
            )
        except Exception:
            raise HTTPException(status_code=500, detail=f"查询表 {table_name} 失败")
        if use_cache and len(df) <= 100_000:
            cache.set(cache_key, df)

    # 日期区间过滤（SQL 已过滤时此步是幂等兜底）——先过滤再派生/字段选择，
    # 避免 fields 先丢弃日期列导致后续过滤失效
    df = _filter_by_date_range(df, start_date, end_date)

    # 复权派生：?adj=qfq|hfq 对股票/ETF 行情表 OHLC 应用因子（ODS 原值不动）
    meta = None
    if adj and not df.empty:
        info = _adj_factor_info(table_name)
        if info is None:
            raise HTTPException(
                status_code=422,
                detail=f"表 {table_name} 无复权因子源（指数/宏观等价格指数不支持复权）",
            )
        asset_type, factor_table, code_col = info
        if code_col not in df.columns:
            raise HTTPException(status_code=422,
                                detail=f"查询结果缺少代码列 {code_col}，无法复权")
        codes = [str(c) for c in df[code_col].dropna().unique().tolist() if c]
        if not codes:
            raise HTTPException(status_code=422, detail="查询结果无代码值，无法复权")
        try:
            from src.core.adj_factor import apply_adjustment
            factor_df = repo.query_adj_factors(asset_type, codes)
            if factor_df.empty:
                raise HTTPException(
                    status_code=422,
                    detail=f"复权因子未同步（asset_type={asset_type}），请先同步复权因子")
            df = apply_adjustment(df, adj, factor_df, code_col=code_col)
            meta = {"adj": adj, "adj_asset_type": asset_type}
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500,
                                detail=f"复权派生失败: {type(exc).__name__}") from exc

    # 字段选择
    if fields:
        wanted = [f.strip() for f in fields.split(",") if f.strip()]
        existing = list(df.columns)
        keep = [f for f in wanted if f in existing]
        if keep:
            df = df[keep]

    # 按日期列倒序（最新在前）
    if not df.empty:
        for _col in df.columns:
            if any(kw in _col.lower() for kw in ("date", "时间", "日期", "月份", "trade")):
                df = df.sort_values(by=_col, ascending=False)
                break

    message = "ok"
    if not start_date and not end_date and len(df) >= limit:
        message = ("未指定日期区间，仅返回前 %d 条；建议带 start_date/end_date"
                   "（YYYYMMDD）以提升性能与精确性" % limit)

    # 对外协商：format=csv 下载(注意 /data 的 limit 已下沉 SQL,不做内存分页)
    if format == "csv":
        return _csv_response(df, f"{table_name}.csv")

    return DataResponse(
        data=_df_to_json_records(df) if not df.empty else [],
        total=len(df),
        message=message,
        data_status=data_status,
        meta=meta,
    )


@router.get("/search", tags=["数据分发"])
async def search_codes(
    q: str = Query("", description="搜索词（代码/中文名/拼音；空=返回前 limit 个代码供下拉初始化）"),
    table: str = Query(..., description="限定搜索范围的数据表名"),
    limit: int = Query(20, ge=1, le=50),
    repo=Depends(get_repo),
):
    """按代码/中文名/拼音模糊搜索某表内的代码（供看板代码搜索下拉）

    - **只返回表内实际存在的 code**（搜到的必有数据；meta_asset_info 里不在表的 code 不返回）
    - 名称来自 meta_asset_info（scripts_gen/sync_asset_names.py 维护）；未同步则名称空、退化为纯 code 匹配
    - 无代码列的表 → 200 空数组（meta.code_column=null，搜索是只读发现，空态比报错友好）
    - 匹配分层（稳定排序）：code 精确(0) > code 前缀(1) > 名称前缀(2) > 名称子串(3) > 拼音(4)
      —— 拼音依赖 pypinyin 可导入，未安装则跳过该层
    """
    # 表名白名单校验（防注入，同 /data/{table}）
    valid = _valid_table_names(repo)
    if table not in valid:
        raise HTTPException(
            status_code=404,
            detail=f"数据表 {table} 不存在或不可查询，可用表见 /data/tables",
        )

    code_col = _resolve_code_column(repo, table)
    if code_col is None:
        return DataResponse(
            data=[], total=0, meta={"code_column": None, "matched_by": ""},
        )

    from src.core.ttl_cache import cache

    # 全量 code 集（缓存；SyncEngine 同步成功会 cache.clear() 全清，无需额外接线）
    # 注意 get_distinct_values 默认 limit=1000，fund 有 2733 个 code，必须显式传大值
    cache_key = f"search_codes:{table}"
    all_codes = cache.get(cache_key)
    if all_codes is None:
        all_codes = repo.get_distinct_values(table, code_col, limit=100_000)
        cache.set(cache_key, all_codes)

    # 名称映射（meta_asset_info 按资产类型读取；表不存在 → {}，优雅降级为纯 code 匹配）
    names: dict[str, str] = {}
    asset_type = TABLE_ASSET_TYPE.get(table)
    if asset_type:
        names_key = f"asset_names:{asset_type}"
        names = cache.get(names_key)
        if names is None:
            names = repo.get_asset_names(asset_type)
            cache.set(names_key, names)

    q = (q or "").strip()
    if not q:
        # 空搜索词：直接返回前 limit 个 code（下拉初始化候选）
        picked = all_codes[:limit]
        return DataResponse(
            data=[{"code": c, "name": names.get(c, ""), "table": table} for c in picked],
            total=len(picked),
            meta={"code_column": code_col, "matched_by": ""},
        )

    query = q.lower()
    ranked: list[tuple[int, str]] = []  # (rank, code)
    for c in all_codes:
        cl = c.lower()
        if cl == query:
            ranked.append((0, c))
        elif cl.startswith(query):
            ranked.append((1, c))
        else:
            name = names.get(c, "")
            if name:
                nl = name.lower()
                if nl.startswith(query):
                    ranked.append((2, c))
                elif query in nl:
                    ranked.append((3, c))

    # 拼音匹配（可选依赖，未安装自动降级）
    try:
        from pypinyin import lazy_pinyin
        for c, name in names.items():
            if not name:
                continue
            py = "".join(lazy_pinyin(name)).lower()
            if py.startswith(query) or query in py:
                if all(r[1] != c for r in ranked):
                    ranked.append((4, c))
    except ImportError:
        pass

    ranked.sort(key=lambda r: (r[0], r[1]))
    rank_label = {0: "code", 1: "code", 2: "name", 3: "name", 4: "pinyin"}
    results = [
        {"code": c, "name": names.get(c, ""), "table": table}
        for rank, c in ranked[:limit]
    ]
    matched_by = rank_label[ranked[0][0]] if ranked else ""
    return DataResponse(
        data=results,
        total=len(results),
        meta={"code_column": code_col, "matched_by": matched_by},
    )


@router.get("/indices", tags=["数据分发"])
async def list_indices(
    category: str = Query(None, description="分类过滤（宽基/行业/主题/风格/策略/债券/跨境/其他）"),
    q: str = Query("", description="代码/名称模糊搜索"),
    limit: int = Query(500, ge=1, le=1000),
    repo=Depends(get_repo),
):
    """指数分类清单（理杏仁式，供看板指数 tab 分类筛选）

    - 返回全部已分类指数：境内 732（宽基/行业/主题/风格/策略/债券/其他）
      + 跨境策划清单（恒生/日经/标普等，与行情数据解耦——未入库也先分类）
    - `meta.categories` = [{category, count}]（看板 chip 计数）
    - 数据由 scripts_gen/sync_index_category.py 维护；旁路脚本不触发 TTL 缓存失效，
      更新后最长滞后 5 分钟（同 /search 名称）
    """
    from src.core.ttl_cache import cache

    cache_key = "index_categories"
    items = cache.get(cache_key)
    if items is None:
        items = repo.get_index_categories()
        cache.set(cache_key, items)

    # 分类计数（全量口径，供 chip 显示；不受本次过滤影响）
    counts: dict[str, int] = {}
    for it in items:
        counts[it["category"]] = counts.get(it["category"], 0) + 1
    cats = [{"category": k, "count": v} for k, v in sorted(counts.items())]

    q = (q or "").strip().lower()
    if category:
        items = [it for it in items if it["category"] == category]
    if q:
        items = [it for it in items
                 if q in it["code"].lower() or q in it["name"].lower()]
    items = items[:limit]
    return DataResponse(
        data=items,
        total=len(items),
        source="index_categories",
        meta={"categories": cats, "category_filter": category or ""},
    )


# ---------------------------------------------------------------------------
# 主题看板(定制数据集)— 预定义指标组合, 按主题键一键拉取
# ---------------------------------------------------------------------------


def _board_store():
    """懒加载 BoardStore(单例路径;测试可 monkeypatch default_themes_path)"""
    from src.core.boards import BoardStore
    return BoardStore()


@router.get("/boards", tags=["主题看板"])
async def list_boards(repo=Depends(get_repo)):
    """列出全部主题(定制数据集): {key, name, description, item_count, date_start, date_end}

    下游系统先 GET /boards 发现可用主题, 再按 key 拉 /boards/{key} 或 /snapshot。
    """
    boards = _board_store().list_boards()
    return DataResponse(data=boards, total=len(boards), source="boards")


@router.get("/boards/{board_key}/snapshot", tags=["主题看板"])
async def board_snapshot(
    board_key: str,
    transform: str = Query(None, pattern=r"^(level|yoy|mom|pct)$",
                           description="全局覆盖各指标口径(缺省各自配置)"),
    format: str = _format_param(),
    repo=Depends(get_repo),
):
    """主题每日快照:每指标一行 [指标, 最新日期, 最新值, 环比%, 同比%, 近3期]

    每天打开主题看的核心视图。指标取各自有效口径的最新值, 环比/同比恒派生。
    """
    board = _board_store().get_board(board_key)
    if board is None:
        raise HTTPException(status_code=404, detail=f"主题 {board_key} 不存在")
    from src.core.boards import BoardService
    df = BoardService(repo).snapshot(board, transform)
    if format == "csv":
        return _csv_response(df, f"{board_key}_snapshot.csv")
    return DataResponse(
        data=_df_to_json_records(df) if not df.empty else [],
        total=len(df),
        source=f"board:{board_key}:snapshot",
    )


@router.get("/boards/{board_key}", tags=["主题看板"])
async def board_series(
    board_key: str,
    start_date: str = Query(None, pattern=r"^\d{8}$"),
    end_date: str = Query(None, pattern=r"^\d{8}$"),
    transform: str = Query(None, pattern=r"^(level|yoy|mom|pct)$",
                           description="全局覆盖各指标口径(缺省各自配置)"),
    limit: int = Query(5000, le=100000),
    format: str = _format_param(),
    page: int | None = _page_param(),
    repo=Depends(get_repo),
):
    """主题时间序列(宽表):date 为行、每指标一列, 按日期对齐

    适合看多指标联动走势 / 作为下游系统的时间序列数据源。
    显式 start_date/end_date 覆盖各 item 级窗口; 无窗口则返回全量(最新在前)。
    """
    board = _board_store().get_board(board_key)
    if board is None:
        raise HTTPException(status_code=404, detail=f"主题 {board_key} 不存在")
    from src.core.boards import BoardService
    df = BoardService(repo).series(
        board, start_date=start_date, end_date=end_date, transform=transform)
    if not df.empty:
        df = df.sort_values("date", ascending=False).reset_index(drop=True)
    if format == "csv":
        return _csv_response(df, f"{board_key}.csv")
    sliced, pagination = _paginate(df, page, limit)
    meta = {"pagination": pagination} if pagination else None
    return DataResponse(
        data=_df_to_json_records(sliced) if not sliced.empty else [],
        total=len(df),
        source=f"board:{board_key}",
        meta=meta,
    )


# ---------------------------------------------------------------------------
# 同步控制
# ---------------------------------------------------------------------------


@router.post("/sync/{source_key}", response_model=SyncTriggerResponse,
             status_code=202, tags=["同步控制"])
async def trigger_sync(source_key: str):
    """触发指定数据源的同步（异步执行）"""
    from src.core.data_fetcher import DataFetcher
    from src.core.sync_engine import SyncEngine, _is_source_busy
    from src.core.dynamic_schema import DynamicSchemaManager
    from src.core.fetcher_registry import FetcherRegistry
    from src.db.engine import get_engine
    from src.db.repository import DataRepository
    from src.utils.config import ConfigManager

    # ★ 先校验数据源是否存在，避免后台线程空跑报错
    registry = FetcherRegistry(ConfigManager())
    source_cfg = registry.get_source(source_key)
    if not source_cfg:
        raise HTTPException(
            status_code=404,
            detail=f"未找到数据源: {source_key}，可用源见 /sources",
        )

    # 该数据源正在同步 → 409（SyncEngine 内部还有每源锁兜底）
    table_name = source_cfg.get("table_name", source_key)
    if _is_source_busy(table_name):
        raise HTTPException(
            status_code=409,
            detail=f"数据源 {source_key} 正在同步中，请稍后再试",
        )

    # 并发同步线程已达上限 → 429
    if not _sync_semaphore.acquire(blocking=False):
        raise HTTPException(
            status_code=429,
            detail="并发同步任务过多，请稍后再试",
        )

    config = ConfigManager()
    engine = get_engine()
    repo = DataRepository(engine)
    schema = DynamicSchemaManager(repo)
    fetcher = DataFetcher(config)
    sync = SyncEngine(fetcher, repo, schema, config)

    def _run():
        try:
            sync.run(source_key)
        finally:
            _sync_semaphore.release()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    return SyncTriggerResponse(
        source_key=source_key,
        status="accepted",
        message=f"同步任务已提交: {source_key}",
    )
