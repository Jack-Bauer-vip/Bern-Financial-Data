"""统一的数据获取接口 — akShare + TuShare"""

import importlib
import inspect
import random
import time
from typing import Any

import pandas as pd
import requests

from src.utils.config import ConfigManager
from src.utils.logger import logger
from src.core.exceptions import DataFetchError, NetworkError, RateLimitError


class DataFetcher:
    """统一数据获取器，封装 akShare 与 TuShare 的数据接口调用"""

    def __init__(self, config: ConfigManager):
        self.config = config
        self._tushare_pro = None
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
        })

    # ------------------------------------------------------------------
    # TuShare 懒初始化
    # ------------------------------------------------------------------

    @property
    def tushare_pro(self) -> Any | None:
        """TuShare pro 对象（懒加载），无 token 时返回 None"""
        if self._tushare_pro is not None:
            return self._tushare_pro

        token = self.config.tushare_token
        if not token:
            logger.warning("TUSHARE_TOKEN 未配置，TuShare 接口不可用")
            return None

        try:
            tushare = importlib.import_module("tushare")
            tushare.set_token(token)
            self._tushare_pro = tushare.pro_api()
            # 指向自定义 API 地址（第三方代理/官方），使 tushare 数据可访问
            api_url = self.config.tushare_api_url
            if api_url:
                self._tushare_pro._DataApi__http_url = api_url
            logger.info("TuShare pro_api 初始化完成 (API=%s)", api_url)
            return self._tushare_pro
        except Exception as exc:
            logger.error("TuShare 初始化失败: %s", exc)
            return None

    # ------------------------------------------------------------------
    # 统一入口
    # ------------------------------------------------------------------

    def fetch(self, source_cfg: dict, params: dict | None = None) -> pd.DataFrame:
        """根据 source_cfg 调用对应的数据源接口

        Parameters
        ----------
        source_cfg : dict
            数据源配置，需包含:
            - api_source : "akshare" | "tushare"
            - api_function : str  接口函数名
        params : dict | None
            传递给 API 函数的参数字典

        Returns
        -------
        pd.DataFrame
            接口返回的原始数据
        """
        api_source = source_cfg.get("api_source", "").lower()
        func_name = source_cfg.get("api_function", "")

        if not func_name:
            raise DataFetchError("source_cfg 中缺少 api_function")

        if api_source == "akshare":
            return self._call_ak(func_name, params)
        elif api_source == "tushare":
            return self._call_ts(func_name, params)
        elif api_source == "fred":
            return self._call_fred(func_name, params)
        else:
            raise DataFetchError(f"不支持的 api_source: {api_source}")

    # ------------------------------------------------------------------
    # akShare 调用
    # ------------------------------------------------------------------

    def _call_ak(self, func_name: str, params: dict | None = None) -> pd.DataFrame:
        """调用 akShare 接口函数"""
        params = params or {}
        try:
            akshare = importlib.import_module("akshare")
        except ImportError:
            raise DataFetchError("akshare 库未安装，请执行 pip install akshare")

        func = getattr(akshare, func_name, None)
        if func is None:
            raise DataFetchError(f"akshare 中不存在函数: {func_name}")

        # 只传递目标函数签名中接受的参数
        filtered = self._filter_params(func, params)

        # 随机延迟，避免触发反爬
        delay = random.uniform(0.5, 1.5)
        logger.debug("akshare 调用 %s (延迟 %.2fs, 参数=%s)", func_name, delay, filtered)
        time.sleep(delay)

        try:
            df = func(**filtered)
        except Exception as exc:
            raise DataFetchError(f"akshare {func_name} 调用失败: {exc}") from exc

        if df is None or (isinstance(df, pd.DataFrame) and df.empty):
            logger.info("akshare %s 返回空数据", func_name)
            return pd.DataFrame()

        return df

    # ------------------------------------------------------------------
    # TuShare 调用
    # ------------------------------------------------------------------

    def _call_ts(self, func_name: str, params: dict | None = None) -> pd.DataFrame:
        """调用 TuShare pro_api 接口函数"""
        params = params or {}
        pro = self.tushare_pro
        if pro is None:
            raise DataFetchError("TuShare 不可用（token 未配置或初始化失败）")

        func = getattr(pro, func_name, None)
        if func is None:
            raise DataFetchError(f"tushare pro_api 中不存在函数: {func_name}")

        filtered = self._filter_params(func, params)

        delay = random.uniform(0.3, 1.0)
        logger.debug("tushare 调用 %s (延迟 %.2fs, 参数=%s)", func_name, delay, filtered)
        time.sleep(delay)

        try:
            df = func(**filtered)
        except Exception as exc:
            err_msg = str(exc)
            if "频率限制" in err_msg or "rate limit" in err_msg.lower():
                raise RateLimitError(f"TuShare 频率限制: {exc}") from exc
            raise DataFetchError(f"tushare {func_name} 调用失败: {exc}") from exc

        if df is None or (isinstance(df, pd.DataFrame) and df.empty):
            logger.info("tushare %s 返回空数据", func_name)
            return pd.DataFrame()

        return df

    # ------------------------------------------------------------------
    # FRED 调用（api_function 即 FRED 序列 ID）
    # ------------------------------------------------------------------

    def _call_fred(self, func_name: str, params: dict | None = None) -> pd.DataFrame:
        """调用 FRED API 拉取序列观测值

        func_name 是 FRED 序列 ID（如 UNRATE / DGS10 / CPIAUCSL）。
        读取同步引擎注入的 start_date/end_date（date_format=%Y-%m-%d，FRED
        原生接受）作为 observation_start/observation_end → 支持真正增量更新。
        """
        params = params or {}
        api_key = self.config.fred_api_key
        if not api_key:
            raise DataFetchError("FRED_API_KEY 未配置，请在 .env 中设置")

        from src.core.fred_client import fetch_series

        start = params.get("start_date") or params.get("observation_start")
        end = params.get("end_date") or params.get("observation_end")
        timeout = float(self.config.get("sync.request_timeout", 30))

        logger.debug("FRED 调用 %s (区间 %s ~ %s)",
                     func_name, start or "全量", end or "至今")
        return fetch_series(
            func_name, api_key,
            observation_start=start, observation_end=end,
            base_url=self.config.fred_api_url,
            timeout=timeout,
        )

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    @staticmethod
    def _filter_params(func: callable, params: dict) -> dict:
        """只保留目标函数签名中接受的参数

        对于无法 inspect 的 C 扩展函数，直接返回原参数。
        """
        try:
            sig = inspect.signature(func)
            valid = set(sig.parameters.keys())
            # 检查是否接受 **kwargs
            has_kwargs = any(
                p.kind == inspect.Parameter.VAR_KEYWORD
                for p in sig.parameters.values()
            )
            if has_kwargs:
                return dict(params)
            return {k: v for k, v in params.items() if k in valid}
        except (ValueError, TypeError):
            # inspect 失败（C 扩展函数），返回原参数
            return dict(params)
