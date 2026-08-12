# -*- coding: utf-8 -*-
"""无 GUI 独立 API 服务 — 供网页看板 /dashboard 与下游程序只读查询。

用法：
  python scripts_gen/serve_api.py                    # 默认 host/port 取自 .env(127.0.0.1:8765)
  python scripts_gen/serve_api.py --port 9000        # 指定端口

与桌面端的区别：
  - 不带 PySide6 GUI, 不启动调度器(不自动同步), 纯只读查询服务
  - 若桌面端已在跑(8765 已被占用), 请直接关掉本服务, 用桌面端即可
  - 看板页面/静态资源免鉴权; /api/v1 数据请求仍按 .env API_TOKEN 校验

配套启动器: 根目录 start_dashboard.bat(自动探测端口, 未起则后台拉起本脚本再开浏览器)。
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Windows GBK 控制台兜底：避免 ✓/✗ 等符号 UnicodeEncodeError 崩溃
for _stream in (sys.stdout, sys.stderr):
    if _stream and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

from src.api.server import FastAPIServer
from src.db.engine import get_engine
from src.db.repository import DataRepository
from src.utils.config import ConfigManager


def main() -> None:
    config = ConfigManager()
    parser = argparse.ArgumentParser(description="Bern 数据中台 无GUI 只读 API 服务")
    parser.add_argument("--host", default=config.api_host, help=f"监听地址(默认 {config.api_host})")
    parser.add_argument("--port", type=int, default=config.api_port, help=f"监听端口(默认 {config.api_port})")
    args = parser.parse_args()

    repo = DataRepository(get_engine())
    try:
        repo.create_tables()  # 幂等; 防全新 DB 无表
    except Exception:
        pass

    # scheduler=None: 只读查询, 不启动自动同步(同步请走桌面端)
    server = FastAPIServer(repo=repo)
    ok = server.start(args.host, args.port)
    if not ok:
        print(f"[serve_api] 端口 {args.port} 启动失败(可能已被桌面端占用), 退出。", file=sys.stderr)
        sys.exit(1)

    print(f"[serve_api] 只读 API 服务已启动:")
    print(f"   看板:   http://{args.host}:{args.port}/dashboard")
    print(f"   接口:   http://{args.host}:{args.port}/api/v1/health")
    print(f"   文档:   http://{args.host}:{args.port}/docs")
    print("[serve_api] 按 Ctrl+C 停止服务。")

    try:
        while True:
            import time
            time.sleep(3600)
    except KeyboardInterrupt:
        server.stop()
        print("\n[serve_api] 已停止。")


if __name__ == "__main__":
    main()
