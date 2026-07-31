"""数据分类树 — 从 FetcherRegistry 构建可勾选的分类树"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QTreeWidget, QTreeWidgetItem, QHeaderView, QMenu,
    QApplication,
)
from PySide6.QtGui import QAction

from src.core.fetcher_registry import FetcherRegistry


# 自定义 UserRole 常量
ROLE_SOURCE_KEY = Qt.ItemDataRole.UserRole
ROLE_SCHEDULE_ENABLED = Qt.ItemDataRole.UserRole + 1
ROLE_CATEGORY_NAME = Qt.ItemDataRole.UserRole + 2


class DataTreeWidget(QTreeWidget):
    """左侧数据分类导航树，支持勾选、点击选择、定时任务开关"""

    itemSelected = Signal(str)  # 发射 source_key
    scheduleToggled = Signal(str, bool)  # category_name, enabled

    def __init__(self, registry: FetcherRegistry, parent=None):
        super().__init__(parent)
        self.setHeaderLabels(["数据分类"])
        self.setHeaderHidden(False)
        self.registry = registry
        self._source_key_map: dict[QTreeWidgetItem, str] = {}

        # 样式
        self.setAlternatingRowColors(True)
        self.header().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.setAnimated(True)
        self.setIndentation(20)

        self.itemClicked.connect(self._on_item_clicked)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)
        self._build_tree()

    # ------------------------------------------------------------------
    # 树构建
    # ------------------------------------------------------------------

    def _build_tree(self) -> None:
        """从注册表加载分类树并构建树控件"""
        self.clear()
        self._source_key_map.clear()

        categories = self.registry.get_categories()
        for cat in categories:
            top_item = QTreeWidgetItem(self)
            name = cat.get("name", "")
            schedule_on = cat.get("schedule_enabled", True)
            display_text = self._make_display_text(name, schedule_on)
            top_item.setText(0, display_text)
            top_item.setData(0, ROLE_CATEGORY_NAME, name)
            top_item.setData(0, ROLE_SCHEDULE_ENABLED, schedule_on)

            top_item.setFlags(top_item.flags() | Qt.ItemFlag.ItemIsAutoTristate)
            top_item.setFlags(top_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            top_item.setCheckState(0, Qt.CheckState.Unchecked)

            children = cat.get("children", [])
            if children:
                self._build_children(top_item, children)
            elif cat.get("api_function"):
                source_key = cat.get("source_key", "")
                if source_key:
                    self._source_key_map[top_item] = source_key
                    top_item.setData(0, ROLE_SOURCE_KEY, source_key)

        self.expandAll()

    def _build_children(
        self,
        parent_item: QTreeWidgetItem,
        children: list[dict],
    ) -> None:
        """递归构建子节点"""
        for child in children:
            item = QTreeWidgetItem(parent_item)
            item.setText(0, child.get("name", ""))

            sub_children = child.get("children")
            if sub_children:
                item.setFlags(
                    item.flags()
                    | Qt.ItemFlag.ItemIsAutoTristate
                    | Qt.ItemFlag.ItemIsUserCheckable
                )
                item.setCheckState(0, Qt.CheckState.Unchecked)
                self._build_children(item, sub_children)
            else:
                source_key = child.get("source_key") or child.get("key", "")
                is_leaf = bool(child.get("api_function"))

                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(0, Qt.CheckState.Unchecked)

                if source_key and is_leaf:
                    self._source_key_map[item] = source_key
                    item.setData(0, ROLE_SOURCE_KEY, source_key)

    @staticmethod
    def _make_display_text(name: str, schedule_on: bool) -> str:
        """生成带定时状态图标的显示文本"""
        icon = "🟢" if schedule_on else "🔴"
        return f"{icon} {name}"

    def refresh_schedule_icon(self, category_name: str, enabled: bool) -> None:
        """刷新某个分类的定时状态图标"""
        for i in range(self.topLevelItemCount()):
            item = self.topLevelItem(i)
            if item.data(0, ROLE_CATEGORY_NAME) == category_name:
                item.setData(0, ROLE_SCHEDULE_ENABLED, enabled)
                name = category_name
                item.setText(0, self._make_display_text(name, enabled))
                break

    # ------------------------------------------------------------------
    # 信号处理
    # ------------------------------------------------------------------

    def _on_item_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        """点击叶节点时发射 source_key"""
        source_key = item.data(0, ROLE_SOURCE_KEY)
        if source_key:
            self.itemSelected.emit(source_key)

    # ------------------------------------------------------------------
    # 右键菜单
    # ------------------------------------------------------------------

    def _show_context_menu(self, pos) -> None:
        """右键菜单 — 定时任务开关"""
        item = self.itemAt(pos)
        if item is None:
            return

        category_name = item.data(0, ROLE_CATEGORY_NAME)
        if not category_name:
            # 子节点：找顶级父节点
            parent = item.parent()
            while parent and parent.parent():
                parent = parent.parent()
            if parent:
                category_name = parent.data(0, ROLE_CATEGORY_NAME)

        if not category_name:
            return

        schedule_on = item.data(0, ROLE_SCHEDULE_ENABLED)
        if schedule_on is None and item.parent():
            parent = item.parent()
            while parent and parent.parent():
                parent = parent.parent()
            if parent:
                schedule_on = parent.data(0, ROLE_SCHEDULE_ENABLED)

        menu = QMenu(self)

        if schedule_on:
            action = menu.addAction("🔴 暂停定时更新")
            action.setData(False)
        else:
            action = menu.addAction("🟢 启用定时更新")
            action.setData(True)

        menu.addSeparator()
        info_action = menu.addAction("📋 查看调度信息")
        info_action.setData(None)

        chosen = menu.exec(self.mapToGlobal(pos))
        if chosen is None:
            return

        new_state = chosen.data()
        if new_state is not None:
            self.scheduleToggled.emit(category_name, new_state)
            self.refresh_schedule_icon(category_name, new_state)

    # ------------------------------------------------------------------
    # 查询方法
    # ------------------------------------------------------------------

    def getCheckedSourceKeys(self) -> list[str]:
        """返回所有已勾选的叶节点的 source_key 列表"""
        keys: list[str] = []

        def _collect(item: QTreeWidgetItem) -> None:
            if item.childCount() == 0:
                if item.checkState(0) == Qt.CheckState.Checked:
                    key = item.data(0, ROLE_SOURCE_KEY)
                    if key:
                        keys.append(key)
            else:
                for i in range(item.childCount()):
                    _collect(item.child(i))

        for i in range(self.topLevelItemCount()):
            _collect(self.topLevelItem(i))

        return keys

    def selectAll(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked

        def _set_check(item: QTreeWidgetItem) -> None:
            item.setCheckState(0, state)
            for i in range(item.childCount()):
                _set_check(item.child(i))

        for i in range(self.topLevelItemCount()):
            _set_check(self.topLevelItem(i))

    def currentSourceKey(self) -> str | None:
        item = self.currentItem()
        if item:
            return item.data(0, ROLE_SOURCE_KEY)
        return None

    def get_category_schedule_info(self) -> list[dict]:
        """返回所有顶级分类的调度状态"""
        result = []
        for i in range(self.topLevelItemCount()):
            item = self.topLevelItem(i)
            result.append({
                "name": item.data(0, ROLE_CATEGORY_NAME),
                "schedule_enabled": item.data(0, ROLE_SCHEDULE_ENABLED),
            })
        return result
