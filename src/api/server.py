"""FastAPI 本地数据服务管理 — 连接追踪 + uvicorn 生命周期"""

import secrets
import threading
import time
from datetime import datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from src.utils.logger import logger


class ConnectionTrackingMiddleware(BaseHTTPMiddleware):
    """追踪每个客户端的请求信息"""

    def __init__(self, app):
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        from src.api.routes import record_request

        client_ip = request.client.host if request.client else "unknown"
        user_agent = request.headers.get("user-agent", "unknown")
        path = request.url.path

        # 不加锁：record_request 内部自己会加 _stats_lock
        record_request(client_ip, user_agent, path)

        response = await call_next(request)
        return response


class AuthMiddleware(BaseHTTPMiddleware):
    """API token 鉴权

    仅校验 X-API-Key 请求头（不再接受 ?token= 查询参数——token 放进
    URL 会泄露到访问日志 / 浏览器历史）。
    - 未配置 token（API_TOKEN 为空）→ 放行（兼容开发模式）
    - 已配置 token → 请求必须带正确 token，否则 401
    - 公开端点（/health、/docs、/openapi.json、/）豁免
    """

    # 无需鉴权的路径（前缀匹配）
    PUBLIC_PATHS = ("/health", "/docs", "/openapi.json", "/redoc")

    def __init__(self, app):
        super().__init__(app)
        from src.utils.config import ConfigManager
        # 从 .env 读 token（可能为空 = 未启用鉴权）
        self.api_token = (ConfigManager().get_env("API_TOKEN") or "").strip()

    async def dispatch(self, request: Request, call_next):
        # 未配置 token → 全部放行
        if not self.api_token:
            return await call_next(request)

        path = request.url.path
        # 公开端点豁免：精确匹配 / 或 以 /api/v1/ 前缀 + 公开路径结尾
        if path == "/":
            return await call_next(request)
        for p in self.PUBLIC_PATHS:
            if path == p or path == f"/api/v1{p}":
                return await call_next(request)

        # 校验 token（常数时间比较，防时序侧信道）
        provided = request.headers.get("x-api-key", "")

        if not provided or not secrets.compare_digest(provided, self.api_token):
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=401,
                content={"code": 401, "message": "无效或缺失的 API token"},
                headers={"WWW-Authenticate": "Bearer"},
            )

        return await call_next(request)


class FastAPIServer:
    """FastAPI 服务管理器 — 后台 uvicorn 线程"""

    def __init__(self, repo=None, scheduler=None):
        self.repo = repo
        self.scheduler = scheduler
        self._server = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._host = "127.0.0.1"
        self._port = 8765

        # 连接统计（由 routes._client_stats 共享，延迟初始化）
        self.client_stats: dict[str, dict] = {}
        self._stats_lock = threading.Lock()

        # 创建 FastAPI 应用
        self.app = FastAPI(
            title="Bern_Financial_Data API",
            version="0.1.0",
            description="本地金融数据查询服务 — 桌面数据中台对外接口",
        )

        self._setup_middleware()
        self._register_routes()

    def _setup_middleware(self) -> None:
        """注册中间件

        注意：Starlette 中后加的中间件先执行。
        顺序：请求 → 连接追踪 → 鉴权 → 路由
        """
        # 鉴权（先加 → 后执行，让连接追踪先记录）
        self.app.add_middleware(AuthMiddleware)
        # CORS：本地服务开放所有来源；不携带凭据（allow_credentials=True 与
        # allow_origins=["*"] 组合非法且会被浏览器拒绝）。鉴权走 X-API-Key 头。
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )
        # 连接追踪（后加 → 先执行）
        self.app.add_middleware(ConnectionTrackingMiddleware)

    def _register_routes(self) -> None:
        """注册路由，共享连接统计 dict 与调度器状态"""
        from src.api.routes import router as api_router
        from src.api.routes import set_repo, set_scheduler, set_stats_lock
        import src.api.routes as routes_mod

        if self.repo:
            set_repo(self.repo)
        if self.scheduler:
            set_scheduler(self.scheduler)
        set_stats_lock(self._stats_lock)

        # ★ GUI 读取 client_stats 时从 routes 共享 dict 拿数据
        self.client_stats = routes_mod._client_stats

        self.app.include_router(api_router, prefix="/api/v1")

        # 根路径
        @self.app.get("/")
        async def root():
            return {
                "service": "Bern_Financial_Data API",
                "version": "0.1.0",
                "docs": "/docs",
                "endpoints": [
                    "/api/v1/health",
                    "/api/v1/connections",
                    "/api/v1/sources",
                    "/api/v1/macro/{table_name}",
                    "/api/v1/macro/cpi",
                    "/api/v1/stock/daily",
                    "/api/v1/data/tables",
                    "/api/v1/data/{table_name}",
                    "/api/v1/sync/{source_key}",
                ],
            }

    # ------------------------------------------------------------------
    # 生命周期管理
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self._port}"

    @property
    def connection_count(self) -> int:
        return len(self.client_stats)

    @property
    def total_requests(self) -> int:
        with self._stats_lock:
            return sum(s["request_count"] for s in self.client_stats.values())

    def get_connections(self) -> list[dict]:
        """获取连接列表"""
        with self._stats_lock:
            return sorted(
                list(self.client_stats.values()),
                key=lambda x: x["last_seen"],
                reverse=True,
            )

    def start(self, host: str = "127.0.0.1", port: int = 8765) -> bool:
        """在后台线程启动 uvicorn"""
        if self._running:
            logger.info("API 服务已在运行（跳过）")
            return True

        self._host = host
        self._port = port

        import uvicorn

        config = uvicorn.Config(
            self.app,
            host=host,
            port=port,
            log_level="warning",
            loop="asyncio",
        )
        self._server = uvicorn.Server(config)

        def _run():
            self._running = True
            try:
                self._server.run()
            except Exception as e:
                logger.error(f"API 服务异常退出: {e}")
            finally:
                self._running = False
                self._server = None

        self._thread = threading.Thread(target=_run, daemon=True, name="FastAPI")
        self._thread.start()

        # 等待服务启动（最多 10 秒）
        for _ in range(100):
            if self._server and self._server.started:
                break
            time.sleep(0.1)

        if self._server and self._server.started:
            logger.info(f"API 服务已启动: http://{host}:{port}")
            self._running = True
        else:
            # 服务可能在后台继续启动中，超时不等于失败
            # 用 QTimer 在 main_window 层延迟检查
            logger.debug(f"API 服务正在后台启动中: http://{host}:{port}")

        return self._running

    def stop(self) -> None:
        """停止服务（等待 uvicorn 线程退出，避免端口未释放导致重启失败）"""
        if self._server:
            self._server.should_exit = True
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        self._running = False
        self._thread = None
        self._server = None
        logger.info("API 服务已停止")
