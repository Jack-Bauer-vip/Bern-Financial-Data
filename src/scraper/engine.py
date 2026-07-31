"""抓取引擎 — httpx 抓取 HTML + BeautifulSoup 解析 + 入库

支持：
- HTML 表格（rows 定位 <tr>，columns 按子元素 index 取值）
- CSS 选择器定位行元素，提取属性/文本
- 自动建表 + 按唯一键去重入库（复用 repository.bulk_upsert）
"""

import logging
from typing import Any

import httpx
import pandas as pd
from bs4 import BeautifulSoup

from src.utils.config import ConfigManager

logger = logging.getLogger("bern.scraper")


class ScraperError(Exception):
    """抓取异常"""
    pass


class ScrapeEngine:
    """通用 HTML 抓取引擎"""

    # 默认请求头
    DEFAULT_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    def __init__(self, repo=None, config: ConfigManager | None = None):
        self.config = config or ConfigManager()
        self.repo = repo
        self.timeout = float(self.config.get("scraper.timeout", 30))

    # ------------------------------------------------------------------
    # 配置加载
    # ------------------------------------------------------------------

    def load_rules(self) -> list[dict]:
        """从 config/scrapers.yaml 加载抓取规则"""
        import yaml
        from pathlib import Path

        path = Path(self.config.root_dir) / "config" / "scrapers.yaml"
        if not path.exists():
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            return data.get("scrapers", [])
        except Exception as exc:
            logger.error("加载抓取规则失败: %s", exc)
            return []

    def get_rule(self, name: str) -> dict | None:
        """按名称查找抓取规则"""
        for rule in self.load_rules():
            if rule.get("name") == name:
                return rule
        return None

    # ------------------------------------------------------------------
    # 抓取 + 解析
    # ------------------------------------------------------------------

    def fetch_html(self, rule: dict) -> str:
        """请求网页并返回 HTML 文本"""
        url = rule.get("url")
        if not url:
            raise ScraperError("规则缺少 url")

        headers = dict(self.DEFAULT_HEADERS)
        headers.update(rule.get("headers") or {})
        method = rule.get("method", "GET").upper()
        params = rule.get("params") or {}

        try:
            with httpx.Client(timeout=self.timeout, headers=headers,
                              follow_redirects=True) as client:
                if method == "POST":
                    resp = client.post(url, data=params)
                else:
                    resp = client.get(url, params=params)
                resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ScraperError(f"HTTP {exc.response.status_code}: {url}") from exc
        except httpx.RequestError as exc:
            raise ScraperError(f"请求失败 {url}: {exc}") from exc

        # 尝试多种编码（GBK 常见于国内站点）
        content = resp.content
        for enc in ("utf-8", "gbk", "gb18030"):
            try:
                return content.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return resp.text

    def parse_html(self, html: str, rule: dict) -> pd.DataFrame:
        """解析 HTML 为 DataFrame

        规则:
        - rows: CSS 选择器定位每行元素
        - columns: [{name, index(子元素序号), attr(可选属性)}]
        """
        soup = BeautifulSoup(html, "lxml")
        rows_selector = rule.get("rows")
        columns = rule.get("columns") or []

        if not rows_selector or not columns:
            raise ScraperError("规则缺少 rows 或 columns")

        elements = soup.select(rows_selector)
        if not elements:
            logger.warning("选择器 %s 未匹配到元素", rows_selector)
            return pd.DataFrame()

        records: list[dict] = []
        for el in elements:
            row: dict[str, Any] = {}
            for col in columns:
                name = col.get("name")
                if not name:
                    continue
                idx = col.get("index")
                attr = col.get("attr")
                if idx is not None:
                    children = el.find_all(recursive=False)
                    if idx < len(children):
                        node = children[idx]
                        row[name] = (node.get(attr) if attr else node.get_text(strip=True))
                else:
                    # 无 index → 在行元素自身取值
                    row[name] = el.get(attr) if attr else el.get_text(strip=True)
            if row:
                records.append(row)

        if not records:
            return pd.DataFrame()

        df = pd.DataFrame(records)
        # 清理空列
        df = df.dropna(axis=1, how="all")
        return df

    def scrape(self, rule: dict) -> pd.DataFrame:
        """抓取一个规则，返回 DataFrame"""
        html = self.fetch_html(rule)
        return self.parse_html(html, rule)

    # ------------------------------------------------------------------
    # 入库
    # ------------------------------------------------------------------

    def save(self, rule: dict, df: pd.DataFrame) -> int:
        """把抓取结果写入数据库，返回写入行数

        目标表 = rule.table_name；唯一键 = date_column（若配置）
        """
        if df is None or df.empty:
            logger.info("[%s] 抓取结果为空，跳过入库", rule.get("name"))
            return 0
        if self.repo is None:
            logger.warning("未注入 repo，无法入库")
            return 0

        table_name = rule.get("table_name") or rule.get("name")
        date_column = rule.get("date_column")

        # 确保表存在
        from src.core.dynamic_schema import DynamicSchemaManager
        schema = DynamicSchemaManager(self.repo)
        schema.ensure_table_exists(table_name, list(df.columns))

        # 唯一键（有日期列则按日期去重）
        unique_key = [date_column] if date_column else None

        added = self.repo.bulk_upsert(
            table_name, df, unique_columns=unique_key, batch_size=500)
        logger.info("[%s] 已写入 %d 行到 %s", rule.get("name"), added, table_name)
        return added

    def scrape_and_save(self, rule: dict) -> int:
        """抓取 + 入库一步完成"""
        df = self.scrape(rule)
        return self.save(rule, df)

    # ------------------------------------------------------------------
    # 批量/定时触发
    # ------------------------------------------------------------------

    def run_by_name(self, name: str) -> int:
        """按规则名抓取并入库（供菜单/定时调用）"""
        rule = self.get_rule(name)
        if not rule:
            raise ScraperError(f"未找到抓取规则: {name}")
        if rule.get("enabled") is False:
            logger.info("[%s] 规则已禁用，跳过", name)
            return 0
        return self.scrape_and_save(rule)

    def run_all_enabled(self) -> dict[str, int]:
        """抓取所有启用规则，返回 {name: 写入行数}"""
        results: dict[str, int] = {}
        for rule in self.load_rules():
            name = rule.get("name", "")
            if rule.get("enabled") is False:
                continue
            try:
                results[name] = self.run_by_name(name)
            except Exception as exc:
                logger.error("[%s] 抓取失败: %s", name, exc)
                results[name] = -1
        return results
