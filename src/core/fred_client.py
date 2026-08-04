"""FRED API 客户端 — 圣路易斯联储官方宏观数据（美国）

FRED 免费 API key 申请：https://fred.stlouisfed.org/docs/api/api_key.html

接口：GET {base_url}/fred/series/observations
  - series_id        FRED 序列 ID（如 UNRATE、DGS10、CPIAUCSL）
  - api_key          免费 key（配置在 .env 的 FRED_API_KEY）
  - observation_start / observation_end  区间参数 → 支持真正增量更新
    （akshare 美国宏观函数不接受日期参数，每次只能全量拉取）
  - file_type=json

返回统一为 {date, value} 两列的 DataFrame；value 为 "."（缺失）→ NaN。
错误映射到 src.core.exceptions：429 → RateLimitError；连接/超时 → NetworkError；
其余 HTTP 错误 → DataFetchError（带上 FRED 返回的 error_message）。
"""

import pandas as pd
import requests

from src.core.exceptions import DataFetchError, NetworkError, RateLimitError

# FRED 序列观测值默认请求地址
DEFAULT_BASE_URL = "https://api.stlouisfed.org"


def _parse_error_message(resp: requests.Response) -> str:
    """从 FRED 错误响应里提取可读的 error_message"""
    try:
        data = resp.json()
        msg = (data or {}).get("error_message")
        if msg:
            return str(msg)
    except Exception:
        pass
    return f"HTTP {resp.status_code}"


def fetch_series(
    series_id: str,
    api_key: str,
    observation_start: str | None = None,
    observation_end: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    timeout: int = 30,
) -> pd.DataFrame:
    """拉取 FRED 序列的观测值，返回 {date, value} 两列 DataFrame

    Parameters
    ----------
    series_id : str
        FRED 序列 ID（如 "UNRATE"、"DGS10"）
    api_key : str
        FRED API key
    observation_start / observation_end : str | None
        YYYY-MM-DD 区间，实现增量更新（只拉区间内观测）
    base_url : str
        FRED API 根地址（默认官方）
    timeout : int
        请求超时秒数

    Returns
    -------
    pd.DataFrame
        列：date（观测日期）、value（观测值，缺失为 NaN）；无观测则空 DataFrame（两列）
    """
    if not series_id:
        raise DataFetchError("FRED 序列 ID 不能为空")
    if not api_key:
        raise DataFetchError("FRED_API_KEY 未配置，请在 .env 中设置")

    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
    }
    if observation_start:
        params["observation_start"] = observation_start
    if observation_end:
        params["observation_end"] = observation_end

    url = f"{base_url.rstrip('/')}/fred/series/observations"
    try:
        resp = requests.get(url, params=params, timeout=timeout)
    except requests.RequestException as exc:
        raise NetworkError(f"FRED 网络请求失败: {exc}") from exc

    if resp.status_code == 429:
        raise RateLimitError(f"FRED 频率限制: {_parse_error_message(resp)}")
    if resp.status_code >= 400:
        raise DataFetchError(f"FRED API 错误: {_parse_error_message(resp)}")

    try:
        data = resp.json()
    except ValueError as exc:
        raise DataFetchError(f"FRED 响应解析失败: {exc}") from exc

    observations = (data or {}).get("observations") or []
    rows = []
    for obs in observations:
        date = obs.get("date")
        value = obs.get("value")
        if not date:
            continue
        # FRED 用 "." 表示缺失值 → NaN
        try:
            v = float(value)
        except (TypeError, ValueError):
            v = float("nan")
        rows.append({"date": date, "value": v})

    return pd.DataFrame(rows, columns=["date", "value"])
