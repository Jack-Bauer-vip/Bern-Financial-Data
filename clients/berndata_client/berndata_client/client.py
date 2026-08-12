"""berndata_client — Bern_Financial_Data 本地金融数据中台 Python 客户端(薄封装)。

薄封装本地 FastAPI 数据服务(http://127.0.0.1:8765/api/v1),给下游程序/AI 脚本取数用:
自动带鉴权头、统一信封解包、日期归一、DataFrame 惰性转换、错误抛出。

用法:
    from berndata_client import BernDataClient

    client = BernDataClient()                # 自动读 API_HOST/API_PORT/API_TOKEN 环境变量(含 .env)
    resp = client.indicator("us.cpi", transform="yoy")
    print(resp.data, resp.meta)              # 列表 + meta(口径/分页)

    df = client.data("fund_etf_daily", code="518880", adj="qfq").df   # 惰性转 pandas DataFrame

契约与 SKILL.md 对齐:端点、参数、返回结构见 skills/bern-financial-data/SKILL.md。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional

import httpx

__all__ = ["BernDataClient", "BernDataResponse", "BernDataError"]
__version__ = "0.1.0"

DEFAULT_BASE_URL = "http://127.0.0.1:8765/api/v1"


def _load_env_file() -> None:
    """尽力从当前目录 .env 读环境变量(与中台服务一致); 无 dotenv/.env 时静默跳过。"""
    try:
        from dotenv import load_dotenv

        load_dotenv(override=False)
    except Exception:  # pragma: no cover - 环境无关的防御
        pass


_load_env_file()


class BernDataError(RuntimeError):
    """API 调用失败(HTTP 非 2xx、服务端 code 非 200 或连接失败)。"""

    def __init__(self, status_code: int, message: str, body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.body = body

    def __repr__(self) -> str:  # pragma: no cover
        return f"BernDataError(status_code={self.status_code}, message={self.message!r})"


def _fmt_date(value: Any) -> Optional[str]:
    """把 date/datetime/YYYYMMDD/YYYY-MM-DD 归一为 API 要求的 8 位 YYYYMMDD。"""
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y%m%d")
    s = str(value).strip()
    if len(s) == 8 and s.isdigit():
        return s
    for sep in ("-", "/", "."):
        if sep in s:
            parts = s.split(sep)
            if len(parts) == 3 and all(p.isdigit() for p in parts):
                return f"{parts[0]}{int(parts[1]):02d}{int(parts[2]):02d}"
    raise ValueError(
        f"无法解析日期: {value!r} (支持 YYYYMMDD / YYYY-MM-DD / date / datetime)"
    )


def _clean_params(**kwargs: Any) -> dict:
    """去掉 None 参数, 并把 start_date/end_date 归一为 YYYYMMDD。"""
    out: dict[str, Any] = {}
    for key, value in kwargs.items():
        if value is None:
            continue
        if key in ("start_date", "end_date"):
            value = _fmt_date(value)
        out[key] = value
    return out


@dataclass
class BernDataResponse:
    """统一 DataResponse 信封解包结果。data 为 list[dict]; .df 惰性转 pandas DataFrame。"""

    data: list[dict]
    total: int = 0
    source: str = "local_db"
    data_status: str = "active"
    meta: Optional[dict] = None
    message: str = "ok"
    _df: Any = field(default=None, repr=False)

    @property
    def df(self) -> Any:
        """惰性转 pandas DataFrame(需要 pandas, 见 optional-dependencies df)。"""
        if self._df is None:
            try:
                import pandas as pd
            except ImportError as exc:  # pragma: no cover
                raise ImportError(
                    "转 DataFrame 需要 pandas: pip install 'berndata-client[df]'"
                ) from exc
            self._df = pd.DataFrame(self.data)
        return self._df

    def to_df(self) -> Any:
        return self.df


class BernDataClient:
    """Bern_Financial_Data 本地数据中台客户端。

    base_url 解析优先级: 显式参数 > 环境变量 BERN_DATA_BASE_URL > API_HOST+API_PORT > 默认。
    api_key 解析优先级: 显式参数 > BERN_DATA_API_KEY > API_TOKEN(与中台 .env 一致); 均无则不鉴权。
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: float = 30.0,
        transport: Optional[httpx.BaseTransport] = None,
    ):
        if base_url is None:
            base_url = (
                os.getenv("BERN_DATA_BASE_URL")
                or _default_base_url()
                or DEFAULT_BASE_URL
            )
        self.base_url = base_url.rstrip("/")
        if api_key is None:
            api_key = os.getenv("BERN_DATA_API_KEY") or os.getenv("API_TOKEN") or None
        self.api_key = api_key or None
        self.timeout = timeout
        self._client = httpx.Client(
            base_url=self.base_url, timeout=timeout, transport=transport
        )

    # ---------- 基础设施 ----------

    def _headers(self) -> dict:
        return {"X-API-Key": self.api_key} if self.api_key else {}

    def _request(self, method: str, path: str, params: Optional[dict] = None) -> dict:
        try:
            resp = self._client.request(method, path, params=params, headers=self._headers())
        except httpx.HTTPError as exc:
            raise BernDataError(0, f"连接本地数据服务失败(服务未启动?): {exc}") from exc
        if resp.status_code >= 400:
            raise BernDataError(resp.status_code, self._extract_error(resp), body=resp.text)
        body = resp.json()
        # 防御: HTTP 200 但服务端 code 非 200(不应出现, 但防漂移)
        if isinstance(body, dict) and "code" in body and body["code"] != 200:
            raise BernDataError(
                resp.status_code, body.get("message") or "未知服务端错误", body=body
            )
        return body

    def request(
        self, method: str, path: str, params: Optional[dict] = None, as_df: bool = False
    ) -> Any:
        """通用逃生舱: 调任意端点。DataResponse 信封自动解包, 其余原样返回。"""
        body = self._request(method, path, params=params)
        if isinstance(body, dict) and "code" in body and "data" in body:
            resp = self._unpack(body)
            return resp.df if as_df else resp
        return body

    def csv(self, path: str, params: Optional[dict] = None) -> bytes:
        """下载 CSV 端点(format=csv, 如 /data/{table})为原始字节, 可 pd.read_csv(io.BytesIO(b))。"""
        try:
            resp = self._client.get(path, params=params, headers=self._headers())
        except httpx.HTTPError as exc:
            raise BernDataError(0, f"连接本地数据服务失败(服务未启动?): {exc}") from exc
        if resp.status_code >= 400:
            raise BernDataError(resp.status_code, self._extract_error(resp), body=resp.text)
        return resp.content

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "BernDataClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @staticmethod
    def _unpack(body: dict) -> BernDataResponse:
        return BernDataResponse(
            data=body.get("data") or [],
            total=body.get("total", 0),
            source=body.get("source", "local_db"),
            data_status=body.get("data_status", "active"),
            meta=body.get("meta"),
            message=body.get("message", "ok"),
        )

    @staticmethod
    def _extract_error(resp: httpx.Response) -> str:
        try:
            body = resp.json()
        except Exception:
            return f"HTTP {resp.status_code}"
        if isinstance(body, dict):
            if "message" in body:
                return str(body["message"])
            if "detail" in body:
                detail = body["detail"]
                if isinstance(detail, list):  # FastAPI 422 校验错误
                    return "; ".join(str(d.get("msg", d)) for d in detail)
                return str(detail)
        return f"HTTP {resp.status_code}"

    # ---------- 系统 ----------

    def health(self) -> dict:
        """服务健康状态 + 数据新鲜度(stale_sources)。"""
        return self._request("GET", "/health")

    def connections(self) -> dict:
        return self._request("GET", "/connections")

    # ---------- 元数据(先发现, 再查询) ----------

    def sources(self, include_deprecated: bool = True) -> BernDataResponse:
        """全部数据源清单: {source_key, name, api_function, table_name, has_incremental, data_status}。"""
        return self._unpack(
            self._request(
                "GET",
                "/sources",
                params={"include_deprecated": "true" if include_deprecated else "false"},
            )
        )

    def source_status(self, source_key: str) -> dict:
        return self._request("GET", f"/sources/{source_key}/status")

    def tables(self) -> list:
        """当前可查询的全部表名(含导入表, 排除 meta_ 元数据表)。"""
        body = self._request("GET", "/data/tables")
        return [item["table_name"] for item in (body.get("data") or [])]

    def indicators(self) -> BernDataResponse:
        """归一化指标清单: {indicator_key, preferred_table, source_api, date_column, value_column, unit_type, unit_desc}。"""
        return self._unpack(self._request("GET", "/indicator"))

    def boards(self) -> BernDataResponse:
        """主题看板清单: {key, name, description, item_count, date_start, date_end}。"""
        return self._unpack(self._request("GET", "/boards"))

    # ---------- 数据分发 ----------

    def data(
        self,
        table: str,
        start_date: Any = None,
        end_date: Any = None,
        limit: int = 200,
        fields: Optional[str] = None,
        adj: Optional[str] = None,
        code: Optional[str] = None,
        as_df: bool = False,
    ) -> Any:
        """通用数据表查询。adj=qfq|hfq 仅股票/ETF 行情表; code 按 code 列精确过滤(带 code 走 SQL 下沉)。

        大表(如 fund_etf_daily 300 万行)务必带 code 或日期区间, 否则仅返回前 limit 条切片。
        """
        params = _clean_params(
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            fields=fields,
            adj=adj,
            code=code,
        )
        resp = self._unpack(self._request("GET", f"/data/{table}", params=params))
        return resp.df if as_df else resp

    def stock_daily(
        self,
        symbol: str,
        start_date: Any = None,
        end_date: Any = None,
        limit: int = 5000,
        as_df: bool = False,
    ) -> Any:
        """A股日线(按代码)。symbol 必填; 无复权/分页。"""
        params = _clean_params(
            symbol=symbol, start_date=start_date, end_date=end_date, limit=limit
        )
        resp = self._unpack(self._request("GET", "/stock/daily", params=params))
        return resp.df if as_df else resp

    def macro(
        self,
        table: str,
        start_date: Any = None,
        end_date: Any = None,
        limit: int = 500,
        as_df: bool = False,
    ) -> Any:
        """宏观表查询(白名单)。表名可带或不带 macro_ 前缀。"""
        params = _clean_params(start_date=start_date, end_date=end_date, limit=limit)
        resp = self._unpack(self._request("GET", f"/macro/{table}", params=params))
        return resp.df if as_df else resp

    def macro_cpi(
        self,
        indicator: Optional[str] = None,
        start_date: Any = None,
        end_date: Any = None,
        limit: int = 500,
        as_df: bool = False,
    ) -> Any:
        """CPI 聚合(多张 CPI 表归并), indicator 名称模糊过滤。"""
        params = _clean_params(
            indicator=indicator, start_date=start_date, end_date=end_date, limit=limit
        )
        resp = self._unpack(self._request("GET", "/macro/cpi", params=params))
        return resp.df if as_df else resp

    # ---------- 指标(归一化, 宏观消费优先) ----------

    def indicator(
        self,
        key: str,
        transform: Optional[str] = None,
        start_date: Any = None,
        end_date: Any = None,
        limit: int = 5000,
        page: Optional[int] = None,
        as_df: bool = False,
    ) -> Any:
        """统一指标序列 {date, value}。transform=level|yoy|mom|pct; meta 含 unit_type/unit_desc/分页。"""
        params = _clean_params(
            transform=transform,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            page=page,
        )
        resp = self._unpack(self._request("GET", f"/indicator/{key}", params=params))
        return resp.df if as_df else resp

    # ---------- 主题看板 ----------

    def board_snapshot(
        self, board_key: str, transform: Optional[str] = None, as_df: bool = False
    ) -> Any:
        """每日快照: 每指标一行(最新值/环比/同比/近3期)。"""
        params = _clean_params(transform=transform)
        resp = self._unpack(
            self._request("GET", f"/boards/{board_key}/snapshot", params=params)
        )
        return resp.df if as_df else resp

    def board_series(
        self,
        board_key: str,
        start_date: Any = None,
        end_date: Any = None,
        transform: Optional[str] = None,
        limit: int = 5000,
        page: Optional[int] = None,
        as_df: bool = False,
    ) -> Any:
        """主题时序宽表: date 为行, 每指标一列。"""
        params = _clean_params(
            start_date=start_date,
            end_date=end_date,
            transform=transform,
            limit=limit,
            page=page,
        )
        resp = self._unpack(
            self._request("GET", f"/boards/{board_key}", params=params)
        )
        return resp.df if as_df else resp

    # ---------- 同步控制(管理操作) ----------

    def trigger_sync(self, source_key: str) -> dict:
        """触发异步同步(202 accepted)。管理操作, 非消费场景勿随意调用。"""
        return self._request("POST", f"/sync/{source_key}")


def _default_base_url() -> Optional[str]:
    """从 API_HOST/API_PORT 环境变量拼默认 base_url。"""
    host = os.getenv("API_HOST")
    port = os.getenv("API_PORT")
    if host and port:
        return f"http://{host}:{port}/api/v1"
    return None
