"""初始化后台工作进程（单独的子进程，崩溃不影响主程序）"""

import json
import os
import random
import sys
import time
from datetime import date, datetime, timedelta

import pandas as pd

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.db.engine import get_engine
from src.db.repository import DataRepository
from src.core.data_fetcher import DataFetcher
from src.core.dynamic_schema import DynamicSchemaManager
from src.utils.config import ConfigManager


def sync_one(
    source_key: str,
    history_years: int,
    repo: DataRepository,
    fetcher: DataFetcher,
    schema_mgr: DynamicSchemaManager,
) -> dict:
    """单数据源同步，返回结果字典"""
    result = {"source_key": source_key, "status": "ok", "added": 0, "error": None}

    try:
        source_cfg = repo.get_source_config(source_key)
        if source_cfg is None:
            raise ValueError(f"未找到数据源配置: {source_key}")

        table_name = source_cfg.get("table_name", source_key)

        params = {}
        params["start_date"] = (date.today() - timedelta(days=365 * history_years)).isoformat()
        params["end_date"] = date.today().isoformat()

        param_map = source_cfg.get("param_map", {})
        for old_k, new_k in param_map.items():
            if old_k in params:
                params[new_k] = params.pop(old_k)

        # 重试 3 次
        df = None
        last_exc = None
        for attempt in range(3):
            try:
                time.sleep(random.uniform(0.5, 2.0))
                df = fetcher.fetch(source_cfg, params)
                break
            except Exception as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(2 ** attempt + random.uniform(0, 1.0))
                else:
                    raise exc

        if df is None or df.empty:
            result["status"] = "no_data"
            return result

        all_columns = schema_mgr.ensure_columns(
            table_name,
            list(df.columns),
            source_api=f"{source_cfg.get('api_source','')}.{source_cfg.get('api_function','')}",
        )

        df = df.dropna(how="all")

        inc_field = source_cfg.get("incremental_field", "date")
        unique_cols = source_cfg.get("unique_columns", [inc_field])
        added = repo.bulk_upsert(table_name, df, unique_columns=unique_cols, batch_size=500)

        # 更新同步状态
        max_date = date.today()
        date_candidates = ["date", "日期", "trade_date", "datetime"]
        for dc in date_candidates:
            if dc in df.columns and not df[dc].isna().all():
                try:
                    parsed = pd.to_datetime(df[dc], errors="coerce")
                    if parsed.notna().any():
                        max_date = parsed.max().date()
                        break
                except Exception:
                    pass

        from sqlalchemy import select
        from sqlalchemy.orm import Session
        from src.db.models import SyncJob

        with Session(repo.engine) as session:
            existing = session.execute(
                select(SyncJob).where(SyncJob.module_name == table_name)
            ).scalar_one_or_none()
            now = datetime.now()
            if existing:
                existing.display_name = source_cfg.get("name", source_key)
                existing.category = source_cfg.get("category", "")
                existing.api_source = source_cfg.get("api_source", "")
                existing.api_function = source_cfg.get("api_function", "")
                existing.last_sync_time = now
                existing.last_sync_date = max_date
                existing.row_count = repo.count_rows(table_name)
                existing.status = "completed"
                existing.error_message = None
            else:
                session.add(SyncJob(
                    module_name=table_name,
                    display_name=source_cfg.get("name", source_key),
                    category=source_cfg.get("category", ""),
                    api_source=source_cfg.get("api_source", ""),
                    api_function=source_cfg.get("api_function", ""),
                    last_sync_time=now,
                    last_sync_date=max_date,
                    row_count=repo.count_rows(table_name),
                    status="completed",
                    enabled=True,
                ))
            session.commit()

        result["added"] = added
        result["status"] = "ok"
        return result

    except Exception as exc:
        result["status"] = "error"
        result["error"] = f"{type(exc).__name__}: {str(exc)}"
        return result


def main():
    """入口：从命令行参数读取任务，逐个执行，输出 JSON 结果到 stdout"""
    args = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    modules = args.get("modules", [])
    history_years = args.get("history_years", 3)

    print(json.dumps({"type": "log", "message": f"子进程启动，处理 {len(modules)} 个模块...", "level": "INFO"}))
    sys.stdout.flush()

    try:
        engine = get_engine()
        repo = DataRepository(engine)
        fetcher = DataFetcher(ConfigManager())
        schema_mgr = DynamicSchemaManager(repo)

        for i, sk in enumerate(modules):
            print(json.dumps({"type": "started", "source_key": sk, "index": i, "total": len(modules)}))
            sys.stdout.flush()

            result = sync_one(sk, history_years, repo, fetcher, schema_mgr)

            print(json.dumps({"type": "result", **result}))
            sys.stdout.flush()

            if i < len(modules) - 1:
                time.sleep(1.0)

    except Exception as exc:
        import traceback
        print(json.dumps({"type": "error", "message": f"子进程异常: {exc}", "traceback": traceback.format_exc()}))
        sys.stdout.flush()


if __name__ == "__main__":
    main()
