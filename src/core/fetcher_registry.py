"""数据源注册表 — 解析 data_catalog.yaml 管理所有数据源"""

from typing import Any

from src.utils.config import ConfigManager
from src.utils.logger import logger


class FetcherRegistry:
    """数据源注册表，将 data_catalog.yaml 树形分类拍平为源键 -> 配置的映射"""

    def __init__(self, config: ConfigManager):
        self.config = config
        self._flat_sources: dict[str, dict] = {}
        self.refresh()

    # ------------------------------------------------------------------
    # 目录拍平
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """重新从 config.data_catalog 加载并拍平"""
        self._flat_sources = {}
        catalog = self.config.catalog
        categories = catalog.get("categories", [])
        self._flat_sources = self._flatten_catalog(categories)
        logger.debug("数据源注册表已刷新，共 %d 个叶节点", len(self._flat_sources))

    def _flatten_catalog(
        self,
        nodes: list[dict],
        parent_key: str = "",
    ) -> dict[str, dict]:
        """递归遍历 catalog 树，收集叶节点（含 api_function 的节点）

        Parameters
        ----------
        nodes : list[dict]
            当前层级的节点列表
        parent_key : str
            父级层级路径，用于构建带前缀的 source_key

        Returns
        -------
        dict[str, dict]
            source_key -> 完整节点配置的映射
        """
        result: dict[str, dict] = {}

        for node in nodes:
            children = node.get("children")
            node_key = node.get("source_key") or node.get("key", "")

            if children:
                # 有 children：递归进入子树
                prefix = node_key or parent_key
                nested = self._flatten_catalog(children, prefix)
                result.update(nested)
            else:
                # 叶节点：必须有 api_function 才视为有效数据源
                if node.get("api_function") and node.get("table_name"):
                    key = node.get("source_key") or node.get("key", "")
                    if not key:
                        # 用父级+名称回退合成键
                        name = node.get("name", "").replace(" ", "_").lower()
                        key = f"{parent_key}_{name}" if parent_key else name
                    result[key] = dict(node)
                    # 确保 source_key 写入副本
                    result[key]["source_key"] = key

        return result

    # ------------------------------------------------------------------
    # 查询方法
    # ------------------------------------------------------------------

    def get_source(self, source_key: str) -> dict | None:
        """根据 source_key 返回完整数据源配置"""
        return self._flat_sources.get(source_key)

    def get_all_sources(self, include_deprecated: bool = True) -> list[dict]:
        """返回所有已注册的数据源列表

        include_deprecated=True（默认）包含 deprecated 源，供 /sources 标注
        data_status；False 则排除（供候选收集/import 匹配）。
        """
        sources = list(self._flat_sources.values())
        if include_deprecated:
            return sources
        return [s for s in sources if not s.get("deprecated")]

    def get_all_enabled_sources(self) -> list[dict]:
        """返回可同步的数据源（排除 deprecated）——供 run_all / 候选收集"""
        return self.get_all_sources(include_deprecated=False)

    def get_macro_sources(self) -> list[dict]:
        """返回分类路径中包含 'macro' 的宏观数据源"""
        results: list[dict] = []
        for key, cfg in self._flat_sources.items():
            category = (cfg.get("category") or "").lower()
            name = (cfg.get("name") or "").lower()
            if "macro" in category or "macro" in key.lower() or "macro" in name:
                results.append(cfg)
        return results

    def get_categories(self) -> list[dict]:
        """返回 data_catalog.yaml 中的原始分类树（用于 GUI 树形控件）"""
        catalog = self.config.catalog
        return catalog.get("categories", [])

    def get_category_schedule_info(self) -> list[dict]:
        """返回每个顶级分类的定时开关状态

        Returns:
            [{"name": "全球宏观数据", "source_key": ..., "schedule_enabled": True, "cron": "0 22 * 1-5"}]
        """
        result = []
        for cat in self.get_categories():
            result.append({
                "name": cat.get("name", ""),
                "source_key": cat.get("source_key", ""),
                "icon": cat.get("icon", ""),
                "schedule_enabled": cat.get("schedule_enabled", True),
                "schedule_cron": cat.get("schedule_cron", ""),
                "child_count": len(cat.get("children", [])),
            })
        return result

    def update_category_schedule(self, category_name: str, enabled: bool) -> bool:
        """更新分类的定时开关状态（只影响运行时，不写回 YAML）"""
        # YAML 配置是只读的，运行时状态通过 DataScheduler 管理
        return True

    def __len__(self) -> int:
        return len(self._flat_sources)

    def __contains__(self, source_key: str) -> bool:
        return source_key in self._flat_sources

    def __iter__(self):
        return iter(self._flat_sources.items())
