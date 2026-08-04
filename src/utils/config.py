"""配置管理 — 加载 YAML 和 .env"""

import os
import yaml
from pathlib import Path
from dotenv import load_dotenv


class ConfigManager:
    """全局配置管理器（单例）"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._config = {}
        self._reload()

    def _reload(self):
        """重新加载所有配置"""
        load_dotenv()
        # 项目根目录
        self.root_dir = Path(__file__).resolve().parent.parent.parent

        # 加载 default.yaml
        default_path = self.root_dir / "config" / "default.yaml"
        if default_path.exists():
            with open(default_path, "r", encoding="utf-8") as f:
                self._config = yaml.safe_load(f) or {}

        # 加载 data_catalog.yaml
        self.catalog_path = self.root_dir / "config" / "data_catalog.yaml"

    @property
    def catalog(self) -> dict:
        """获取数据分类树配置"""
        if self.catalog_path.exists():
            with open(self.catalog_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        return {"categories": []}

    def get(self, key: str, default=None):
        """点号分隔的键路径取值，如 'sync.batch_size'"""
        keys = key.split(".")
        val = self._config
        for k in keys:
            if isinstance(val, dict):
                val = val.get(k)
            else:
                return default
        return val if val is not None else default

    def get_env(self, key: str, default=None):
        """从环境变量取值"""
        return os.getenv(key, default)

    @property
    def tushare_token(self) -> str:
        return self.get_env("TUSHARE_TOKEN", "")

    @property
    def tushare_api_url(self) -> str:
        """tushare API 地址（第三方代理或官方）。

        TUSHARE_API_URL 环境变量优先；否则用 default.yaml 的 tushare.api_url。
        """
        return self.get_env("TUSHARE_API_URL") or self.get(
            "tushare.api_url", "https://ts.gyzcloud.top/api")

    @property
    def fred_api_key(self) -> str:
        """FRED API key（圣路易斯联储官方美国数据）"""
        return self.get_env("FRED_API_KEY", "")

    @property
    def fred_api_url(self) -> str:
        """FRED API 地址。

        FRED_API_URL 环境变量优先；否则用 default.yaml 的 fred.api_url。
        """
        return self.get_env("FRED_API_URL") or self.get(
            "fred.api_url", "https://api.stlouisfed.org")

    @property
    def db_path(self) -> Path:
        path = self.get_env("DB_PATH") or self.get("app.db_path", "data/berndata.db")
        return self.root_dir / path

    @property
    def api_host(self) -> str:
        return self.get_env("API_HOST") or self.get("api.host", "127.0.0.1")

    @property
    def api_port(self) -> int:
        return int(self.get_env("API_PORT") or self.get("api.port", 8765))

    @property
    def log_level(self) -> str:
        return self.get_env("LOG_LEVEL") or "INFO"

    @property
    def cache_ttl(self) -> int:
        return int(self.get_env("CACHE_TTL_SECONDS") or self.get("gui.cache_ttl_seconds", 300))
