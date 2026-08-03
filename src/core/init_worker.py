"""初始化后台工作进程（单独的子进程，崩溃不影响主程序）

复用 src/core/sync_engine.SyncEngine 的单一同步实现，
通过 JSON 行协议向父进程（初始化向导）上报进度与结果。

此前这里复制了 sync_engine.run() 的整套逻辑并已漂移（不注入
params_template 的 symbol/code、不走增量、手动 upsert SyncJob），
现改为直接构造 SyncEngine 调用，确保两条同步路径完全一致。
"""

import json
import os
import sys
import time

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.core.data_fetcher import DataFetcher
from src.core.dynamic_schema import DynamicSchemaManager
from src.core.sync_engine import SyncEngine
from src.db.engine import get_engine
from src.db.repository import DataRepository
from src.utils.config import ConfigManager


def _emit_json(payload: dict) -> None:
    """向 stdout 输出一行 JSON 并刷新（父进程逐行解析）"""
    print(json.dumps(payload, ensure_ascii=False))
    sys.stdout.flush()


def sync_one(
    source_key: str,
    history_years: int,
    repo: DataRepository,
    fetcher: DataFetcher,
    schema_mgr: DynamicSchemaManager,
    config: ConfigManager,
) -> dict:
    """单数据源同步，返回结果字典（复用 SyncEngine 单一实现）"""
    engine = SyncEngine(fetcher, repo, schema_mgr, config)

    # 把 SyncEngine 的错误/日志信号转发为 JSON 行协议
    errors: list[str] = []
    engine.sync_error.connect(lambda _sk, msg: errors.append(msg))
    engine.log_message.connect(
        lambda level, msg: _emit_json({"type": "log", "level": level, "message": msg})
    )

    result = {"source_key": source_key, "status": "ok", "added": 0, "error": None}
    try:
        # full_refresh=True：初始化向导按用户选择的历史年数全量拉取
        # （SyncEngine.run 内部已处理 params 注入/重试/清洗/update_sync_job）
        added = engine.run(source_key, history_years, full_refresh=True)
        if added is None:
            result["status"] = "error"
            result["error"] = errors[-1] if errors else "同步失败"
        else:
            result["added"] = added
            if added == 0:
                result["status"] = "no_data"
    except Exception as exc:
        result["status"] = "error"
        result["error"] = f"{type(exc).__name__}: {str(exc)}"
    return result


def main():
    """入口：从命令行参数读取任务，逐个执行，输出 JSON 结果到 stdout"""
    args = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    modules = args.get("modules", [])
    history_years = args.get("history_years", 3)

    _emit_json({
        "type": "log",
        "message": f"子进程启动，处理 {len(modules)} 个模块...",
        "level": "INFO",
    })

    try:
        config = ConfigManager()
        engine = get_engine()
        repo = DataRepository(engine)
        fetcher = DataFetcher(config)
        schema_mgr = DynamicSchemaManager(repo)

        for i, sk in enumerate(modules):
            _emit_json({"type": "started", "source_key": sk, "index": i, "total": len(modules)})
            result = sync_one(sk, history_years, repo, fetcher, schema_mgr, config)
            _emit_json({"type": "result", **result})

            if i < len(modules) - 1:
                time.sleep(1.0)

    except Exception as exc:
        import traceback
        _emit_json({
            "type": "error",
            "message": f"子进程异常: {exc}",
            "traceback": traceback.format_exc(),
        })


if __name__ == "__main__":
    main()
