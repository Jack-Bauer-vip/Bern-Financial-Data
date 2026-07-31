"""增量同步引擎 — 核心数据流水线"""

import random
import time
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd
from PySide6.QtCore import QObject, Signal

from src.core.data_fetcher import DataFetcher
from src.core.dynamic_schema import DynamicSchemaManager
from src.core.exceptions import BernError, DataFetchError
from src.db.repository import DataRepository
from src.utils.config import ConfigManager
from src.utils.logger import logger


class SyncEngine(QObject):
    """增量同步引擎，协调获取、清洗、写入全流程

    Signals
    -------
    sync_started(source_key: str)
        开始同步某数据源时触发
    sync_progress(source_key: str, current: int, total: int)
        同步进度更新
    sync_completed(source_key: str, added_rows: int)
        同步完成，返回写入行数
    sync_error(source_key: str, error_msg: str)
        同步过程中发生错误
    log_message(level: str, message: str)
        内部日志消息（用于 GUI 日志控件）
    """

    sync_started = Signal(str)
    sync_progress = Signal(str, int, int)
    sync_completed = Signal(str, int)
    sync_error = Signal(str, str)
    log_message = Signal(str, str)

    def __init__(
        self,
        fetcher: DataFetcher,
        repo: DataRepository,
        schema_mgr: DynamicSchemaManager,
        config: ConfigManager,
    ):
        super().__init__()
        self.fetcher = fetcher
        self.repo = repo
        self.schema_mgr = schema_mgr
        self.config = config
        self._running = False
        self._stop_requested = False

    # ------------------------------------------------------------------
    # 运行控制
    # ------------------------------------------------------------------

    def stop(self) -> None:
        """请求停止后续同步（设置停止标志）"""
        self._stop_requested = True
        self._log("INFO", "同步引擎收到停止请求")

    @property
    def is_running(self) -> bool:
        return self._running

    # ------------------------------------------------------------------
    # 单数据源同步
    # ------------------------------------------------------------------

    def run(
        self,
        source_key: str,
        history_years: int = 3,
        full_refresh: bool = False,
    ) -> int | None:
        """执行单个数据源的增量同步

        Parameters
        ----------
        source_key : str
            data_catalog.yaml 中定义的数据源唯一标识
        history_years : int
            首次同步时拉取的历史年数，默认 3 年
        full_refresh : bool
            是否全部重新拉取（忽略 last_sync_date），默认 False

        Returns
        -------
        int | None
            本次写入行数，失败返回 None
        """
        self._running = True
        self._stop_requested = False

        try:
            # 1. 获取数据源配置
            source_cfg = self.repo.get_source_config(source_key)
            if source_cfg is None:
                raise DataFetchError(f"未找到数据源配置: {source_key}")

            table_name = source_cfg.get("table_name", source_key)
            func_name = source_cfg.get("api_function", "")
            api_source = source_cfg.get("api_source", "")

            self._log("INFO", f"开始同步 [{source_key}] -> {table_name}")
            self.sync_started.emit(source_key)

            # 2. 查询上次同步状态
            sync_job = self.repo.get_sync_job(table_name)
            last_date: date | None = sync_job.last_sync_date if sync_job else None

            # 3. 构建请求参数
            params: dict[str, Any] = {}

            # ★ 注入代码类参数（指数/基金等需要 symbol/code 的源）：
            #   从 params_template 的 default 取，避免同步时缺代码静默拉错数据
            for pkey in ("symbol", "code"):
                if pkey in (source_cfg.get("params_template") or {}):
                    default = source_cfg["params_template"][pkey].get("default", "")
                    if default:
                        params[pkey] = str(default)

            if full_refresh or last_date is None:
                # 首次或无历史 -> 回退到固定历史区间
                params["start_date"] = (date.today() - timedelta(days=365 * history_years)).isoformat()
                params["end_date"] = date.today().isoformat()
                self._log("INFO", f"全量模式: {params['start_date']} ~ {params['end_date']}")
            else:
                # 增量模式：从上一次同步日期的次日起
                start = last_date + timedelta(days=1)
                if start >= date.today():
                    self._log("INFO", "上次同步日期为今天或以后，无需增量同步")
                    self.sync_completed.emit(source_key, 0)
                    return 0
                params["start_date"] = start.isoformat()
                params["end_date"] = date.today().isoformat()
                self._log("INFO", f"增量模式: {params['start_date']} ~ {params['end_date']}")

            # 统一参数名映射（akshare 常用 start_date/end_date）
            # 若配置中指定了字段映射，则覆盖
            param_map = source_cfg.get("param_map", {})
            for old_k, new_k in param_map.items():
                if old_k in params:
                    params[new_k] = params.pop(old_k)

            # 4. 获取数据（带重试）
            self._log("INFO", f"正在拉取 {func_name} ...")
            df = self._fetch_with_retry(source_cfg, params, max_retries=3)

            if df is None or df.empty:
                self._log("INFO", f"[{source_key}] 无新数据")
                self.sync_completed.emit(source_key, 0)
                return 0

            self.sync_progress.emit(source_key, 0, len(df))
            self._log("INFO", f"获取到 {len(df)} 行原始数据")

            # 5. 动态扩展列
            all_columns = self.schema_mgr.ensure_columns(
                table_name,
                list(df.columns),
                source_api=f"{api_source}.{func_name}",
            )

            # 6. 数据清洗
            df = self._clean_data(df)

            # 7. 批量写入
            unique_cols = self._get_unique_cols(source_cfg)
            batch_size = self.config.get("sync.batch_size", 500)
            added = self.repo.bulk_upsert(
                table_name,
                df,
                unique_columns=unique_cols,
                batch_size=batch_size,
            )

            # 8. 更新同步任务状态
            max_date = self._extract_max_date(df)
            row_count = self.repo.count_rows(table_name)

            self.repo.update_sync_job(table_name, {
                "display_name": source_cfg.get("name", source_key),
                "category": source_cfg.get("category", ""),
                "api_source": api_source,
                "api_function": func_name,
                "last_sync_time": datetime.now(),
                "last_sync_date": max_date,
                "row_count": row_count,
                "status": "completed",
                "error_message": None,
                "enabled": True,
            })

            self._log("INFO", f"[{source_key}] 同步完成，新增 {added} 条")
            self.sync_completed.emit(source_key, added)
            return added

        except BernError as exc:
            err_msg = str(exc)
            self._log("ERROR", f"[{source_key}] 同步失败: {err_msg}")
            self.sync_error.emit(source_key, err_msg)
            self._update_job_error(table_name, source_cfg, err_msg, api_source, func_name)
            return None
        except Exception as exc:
            err_msg = f"{type(exc).__name__}: {exc}"
            self._log("ERROR", f"[{source_key}] 未预期异常: {err_msg}")
            self.sync_error.emit(source_key, err_msg)
            self._update_job_error(
                table_name if 'table_name' in dir() else source_key,
                source_cfg if 'source_cfg' in dir() else {},
                err_msg,
                source_cfg.get("api_source", "") if 'source_cfg' in dir() else "",
                source_cfg.get("api_function", "") if 'source_cfg' in dir() else "",
            )
            return None
        finally:
            self._running = False

    # ------------------------------------------------------------------
    # 批量同步
    # ------------------------------------------------------------------

    def run_all(self) -> None:
        """顺序执行所有启用的数据源同步"""
        self._running = True
        self._stop_requested = False

        sources = self.repo.get_all_enabled_sources()
        total = len(sources)
        self._log("INFO", f"开始全量同步，共 {total} 个数据源")

        for i, src in enumerate(sources, 1):
            if self._stop_requested:
                self._log("WARNING", "用户中断，停止后续同步")
                break

            key = src.get("source_key") or src.get("table_name", "")
            self.sync_progress.emit(key, i - 1, total)
            self.run(key)

            if i < total and not self._stop_requested:
                gap = random.uniform(1.0, 3.0)
                time.sleep(gap)

        self._log("INFO", "全量同步结束")
        self._running = False

    # ------------------------------------------------------------------
    # 内部：网络请求重试
    # ------------------------------------------------------------------

    def _fetch_with_retry(
        self,
        source_cfg: dict,
        params: dict | None,
        max_retries: int = 3,
    ) -> pd.DataFrame | None:
        """指数退避重试获取数据"""
        last_exc: Exception | None = None

        for attempt in range(max_retries):
            try:
                # 请求前随机延迟，降低并发压力
                time.sleep(random.uniform(0.5, 2.0))
                return self.fetcher.fetch(source_cfg, params)
            except BernError as exc:
                last_exc = exc
                if attempt < max_retries - 1:
                    wait = 2**attempt + random.uniform(0, 1.0)
                    self._log(
                        "WARNING",
                        f"重试 {attempt + 1}/{max_retries}: {exc} (等待 {wait:.1f}s)",
                    )
                    time.sleep(wait)
                else:
                    self._log("ERROR", f"已达最大重试次数 {max_retries}，放弃: {exc}")
                    raise
            except Exception as exc:
                last_exc = exc
                if attempt < max_retries - 1:
                    wait = 2**attempt + random.uniform(0, 1.0)
                    self._log(
                        "WARNING",
                        f"重试 {attempt + 1}/{max_retries}: {exc} (等待 {wait:.1f}s)",
                    )
                    time.sleep(wait)
                else:
                    self._log("ERROR", f"非预期异常已达最大重试次数: {exc}")
                    raise DataFetchError(str(exc)) from exc

        return None  # 不应到达此处

    # ------------------------------------------------------------------
    # 内部：唯一键推断
    # ------------------------------------------------------------------

    @staticmethod
    def _get_unique_cols(source_cfg: dict) -> list[str]:
        """根据表名启发式推断 upsert 唯一键（共享逻辑）"""
        from src.core.unique_key import infer_unique_cols
        return infer_unique_cols(source_cfg)

    # ------------------------------------------------------------------
    # 内部：数据清洗
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_data(df: pd.DataFrame) -> pd.DataFrame:
        """标准化数据：删除全空行、转换日期列"""
        if df.empty:
            return df

        # 删除完全为空的行
        df = df.dropna(how="all")

        # 尝试识别并转换日期列
        date_keywords = ["date", "日期", "时间", "trade_date", "datetime"]
        for col in df.columns:
            col_lower = col.lower().strip()
            if any(kw in col_lower for kw in date_keywords):
                try:
                    df[col] = pd.to_datetime(df[col], errors="coerce")
                    # 统一为 date 类型（不含时间部分）
                    if df[col].dt.time is not None:
                        # 若全部为 00:00:00 则转为 date
                        if (df[col].dropna().dt.time == pd.Timestamp("00:00:00").time()).all():
                            df[col] = df[col].dt.date
                except Exception:
                    pass  # 转换失败则保留原值

        return df

    # ------------------------------------------------------------------
    # 内部：提取最大日期
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_max_date(df: pd.DataFrame) -> date | None:
        """从 DataFrame 中自动检测日期列并提取最大日期"""
        date_candidates = ["date", "日期", "trade_date", "datetime", "end_date", "start_date"]
        for col in date_candidates:
            if col in df.columns and not df[col].isna().all():
                try:
                    # 若已是 date 类型
                    if pd.api.types.is_datetime64_any_dtype(df[col]):
                        return df[col].max().date()
                    if pd.api.types.is_object_dtype(df[col]):
                        parsed = pd.to_datetime(df[col], errors="coerce")
                        if parsed.notna().any():
                            return parsed.max().date()
                except Exception:
                    continue
        return date.today()

    # ------------------------------------------------------------------
    # 内部：错误状态回写
    # ------------------------------------------------------------------

    def _update_job_error(
        self,
        table_name: str,
        source_cfg: dict,
        error_msg: str,
        api_source: str,
        api_function: str,
    ) -> None:
        """同步失败时将任务状态回写为 error"""
        try:
            self.repo.update_sync_job(table_name, {
                "display_name": source_cfg.get("name", table_name),
                "category": source_cfg.get("category", ""),
                "api_source": api_source,
                "api_function": api_function,
                "last_sync_time": datetime.now(),
                "status": "error",
                "error_message": error_msg[:500],
                "enabled": True,
            })
        except Exception as exc:
            logger.error("回写同步错误状态失败: %s", exc)

    # ------------------------------------------------------------------
    # 内部：日志
    # ------------------------------------------------------------------

    def _log(self, level: str, message: str) -> None:
        """统一日志：同时写入 logger 和 Qt 信号"""
        level_upper = level.upper()
        if level_upper == "ERROR":
            logger.error(message)
        elif level_upper == "WARNING":
            logger.warning(message)
        else:
            logger.info(message)
        self.log_message.emit(level_upper, message)
