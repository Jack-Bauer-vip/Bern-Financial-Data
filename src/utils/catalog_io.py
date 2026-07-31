"""data_catalog.yaml 的读写与校验 — 供分类管理 GUI 使用

与 config.py 的 catalog 属性不同，这里提供「读-改-写」能力：
- load_catalog(): 读取分类树
- save_catalog(): 写回 yaml（带备份 + 校验）
- 树操作辅助：source_key 唯一性校验、必填字段校验
"""

import os
import shutil
from pathlib import Path

import yaml

from src.utils.config import ConfigManager
from src.utils.logger import logger


# data_catalog.yaml 文件头（写回时保留）
CATALOG_HEADER = (
    "# Bern_Financial_Data 数据分类树和接口映射\n"
    "# 此文件驱动左侧导航树和数据流水线\n"
)


def catalog_path() -> Path:
    """data_catalog.yaml 的路径"""
    return ConfigManager().catalog_path


def load_catalog() -> dict:
    """读取分类树，返回 {categories: [...]}"""
    path = catalog_path()
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {"categories": []}
    return {"categories": []}


def save_catalog(catalog: dict) -> bool:
    """写回 data_catalog.yaml（原子替换 + 备份），返回是否成功

    流程：校验 → 备份 .bak → 写 .tmp（保留文件头）→ parse 验证 → os.replace
    """
    errors = validate_catalog_errors(catalog)
    if errors:
        logger.error("保存失败: %s", "；".join(errors))
        return False

    path = catalog_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 1. 备份
        if path.exists():
            shutil.copy2(path, str(path) + ".bak")
        # 2. 拼文件头 + 写临时文件
        text = CATALOG_HEADER + yaml.safe_dump(
            catalog, allow_unicode=True, sort_keys=False, indent=2)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        # 3. 校验临时文件能 parse 回来
        with open(tmp, "r", encoding="utf-8") as f:
            reloaded = yaml.safe_load(f)
        if not isinstance(reloaded, dict) or "categories" not in reloaded:
            raise ValueError("临时文件解析失败，取消保存")
        # 4. 原子替换
        os.replace(tmp, path)
        logger.info("已保存数据分类到 %s（备份: %s.bak）", path, path)
        return True
    except Exception as exc:
        logger.error("保存数据分类失败: %s", exc)
        return False


def validate_catalog(catalog: dict) -> str | None:
    """校验分类树，返回首个错误信息或 None"""
    errors = validate_catalog_errors(catalog)
    return errors[0] if errors else None


def validate_catalog_errors(catalog: dict) -> list[str]:
    """校验分类树，返回所有错误列表（空列表 = 通过）

    - categories 必须是列表
    - 所有 source_key 全局唯一（含分类和叶节点）
    - 分类节点 name 非空
    - 叶节点必填：name / source_key / api_function / table_name
    - schedule_cron 非空时必须是 5 段
    """
    errors: list[str] = []
    if not isinstance(catalog, dict) or "categories" not in catalog:
        return ["分类树格式错误：缺少 categories"]
    if not isinstance(catalog["categories"], list):
        return ["分类树格式错误：categories 必须是列表"]

    seen_keys: set[str] = set()

    def _walk(nodes: list, path: str) -> None:
        for node in nodes:
            if not isinstance(node, dict):
                errors.append(f"{path}: 节点必须是字典")
                continue
            name = node.get("name", "")
            source_key = node.get("source_key", "")
            is_category = "children" in node
            children = node.get("children")

            # 分类必填 name
            if is_category:
                if not name:
                    errors.append(f"{path}: 分类缺少 name")
                if not isinstance(children, list):
                    errors.append(f"{path}/{name}: children 必须是列表")
                    children = []
            # source_key 唯一
            if source_key:
                if source_key in seen_keys:
                    errors.append(
                        f"source_key 重复: {source_key}（{path}/{name}）")
                seen_keys.add(source_key)

            if is_category:
                _walk(children, f"{path}/{name}")
            elif node.get("api_function"):
                # 数据源叶节点必填
                if not name:
                    errors.append(f"{path}: 数据源缺少 name")
                if not source_key:
                    errors.append(f"{path}/{name}: 数据源缺少 source_key")
                if not node.get("table_name"):
                    errors.append(f"{path}/{name}: 数据源缺少 table_name")
                # cron 软校验
                cron = node.get("schedule_cron", "")
                if cron and len(str(cron).strip().split()) != 5:
                    errors.append(
                        f"{path}/{name}: schedule_cron 应为 5 段（分 时 日 月 周），"
                        f"当前: {cron}")
            else:
                # 既非分类（无 children）也非数据源（无 api_function）→ 非法
                errors.append(f"{path}/{name}: 节点既不是分类也不是数据源")

    _walk(catalog["categories"], "categories")
    return errors


def collect_source_keys(nodes: list) -> list[str]:
    """递归收集所有 source_key"""
    keys: list[str] = []

    def _walk(items: list):
        for node in items:
            if node.get("source_key"):
                keys.append(node["source_key"])
            if node.get("children"):
                _walk(node["children"])

    _walk(nodes)
    return keys
