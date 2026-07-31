"""数据分类管理对话框 — 可视化编辑 data_catalog.yaml 的分类树和数据源

功能：
- 左侧分类树：创建/重命名/删除分类，创建/删除/移动数据源
- 右侧详情面板：编辑选中节点（分类属性或数据源字段）
- 保存：写回 data_catalog.yaml（带校验+备份）
- 关闭后主窗口刷新导航树
"""

from dataclasses import dataclass, field

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTreeWidget, QTreeWidgetItem, QMessageBox, QFormLayout,
    QLineEdit, QComboBox, QCheckBox, QGroupBox, QSplitter,
    QScrollArea, QWidget, QSpinBox,
)

from src.utils.catalog_io import (
    load_catalog, save_catalog, validate_catalog, collect_source_keys,
)
from src.utils.logger import logger


# 树节点自定义角色
ROLE_KIND = Qt.ItemDataRole.UserRole + 1      # "category" | "source"
ROLE_NAME = Qt.ItemDataRole.UserRole + 2
ROLE_NODE_ID = Qt.ItemDataRole.UserRole + 3   # id(node) — 经 _node_map 查真实引用

# 注意：PySide6 的 itemData/data() 存储 dict 会做拷贝，取回不是原引用。
# 因此 ROLE_NODE_ID 只存整数 id(node)，真实引用统一放 self._node_map。


class CatalogEditorDialog(QDialog):
    """数据分类管理对话框"""

    # 可编辑的数据源字段
    SOURCE_FIELDS = [
        ("name", "名称"),
        ("source_key", "标识(唯一)"),
        ("api_source", "API源"),
        ("api_function", "接口函数"),
        ("table_name", "数据表名"),
        ("schedule_cron", "定时(cron)"),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.catalog = load_catalog()
        self._modified = False
        self._existing_keys = set(collect_source_keys(
            self.catalog.get("categories", [])))
        # id(node) → 真实节点引用（PySide6 data() 存 dict 会拷贝，必须用 id 映射）
        self._node_map: dict[int, dict] = {}
        # 移动目标并行列表（真实引用，不进 Qt 存储）
        self._move_targets: list[dict | None] = [None]

        self.setWindowTitle("数据分类管理")
        self.setMinimumSize(820, 560)
        self.resize(900, 620)

        self._setup_ui()
        self._build_tree()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        title = QLabel("🗂 数据分类管理")
        title.setStyleSheet("font-size: 15px; font-weight: bold; padding: 4px 0;")
        layout.addWidget(title)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)

        # ---- 左侧：分类树 + 操作按钮 ----
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["分类 / 数据源"])
        self.tree.header().setStretchLastSection(True)
        self.tree.itemClicked.connect(self._on_tree_clicked)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        left_layout.addWidget(self.tree, stretch=1)

        # 操作按钮
        btn_row = QHBoxLayout()
        self.add_cat_btn = QPushButton("➕ 分类")
        self.add_cat_btn.clicked.connect(lambda: self._add_category())
        self.add_src_btn = QPushButton("➕ 数据源")
        self.add_src_btn.clicked.connect(self._add_source)
        self.del_btn = QPushButton("🗑 删除")
        self.del_btn.clicked.connect(self._delete_node)
        btn_row.addWidget(self.add_cat_btn)
        btn_row.addWidget(self.add_src_btn)
        btn_row.addWidget(self.del_btn)
        btn_row.addStretch()
        left_layout.addLayout(btn_row)

        splitter.addWidget(left_panel)

        # ---- 右侧：详情面板 ----
        self.detail_group = QGroupBox("详情")
        detail_layout = QVBoxLayout(self.detail_group)
        self.detail_label = QLabel("← 选择一个节点编辑")
        self.detail_label.setStyleSheet("color: #888;")
        detail_layout.addWidget(self.detail_label)
        self.detail_form = QFormLayout()
        detail_layout.addLayout(self.detail_form)
        self.move_label = QLabel("")
        self.move_label.setVisible(False)
        detail_layout.addWidget(self.move_label)
        detail_layout.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.detail_group)
        splitter.addWidget(scroll)

        splitter.setSizes([380, 520])
        layout.addWidget(splitter, stretch=1)

        # ---- 底部：保存/取消 ----
        bottom = QHBoxLayout()
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #aaa;")
        bottom.addWidget(self.status_label)
        bottom.addStretch()
        save_btn = QPushButton("💾 保存")
        save_btn.setStyleSheet(
            "QPushButton { background-color: #1976D2; color: white; "
            "padding: 6px 20px; border-radius: 4px; font-weight: bold; }"
            "QPushButton:hover { background-color: #1565C0; }"
        )
        save_btn.clicked.connect(self._save)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        bottom.addWidget(cancel_btn)
        bottom.addWidget(save_btn)
        layout.addLayout(bottom)

    # ------------------------------------------------------------------
    # 树构建
    # ------------------------------------------------------------------

    def _build_tree(self) -> None:
        """从 self.catalog 重建分类树"""
        self.tree.clear()
        self._node_map = {}
        for cat in self.catalog.get("categories", []):
            item = QTreeWidgetItem(self.tree)
            item.setText(0, self._cat_display(cat))
            item.setData(0, ROLE_KIND, "category")
            item.setData(0, ROLE_NAME, cat.get("name", ""))
            self._store_node(item, cat)
            item.setExpanded(True)
            for child in cat.get("children", []):
                self._add_tree_node(item, child)
        self._clear_detail()

    def _add_tree_node(self, parent_item: QTreeWidgetItem, node: dict) -> None:
        """递归添加一个分类/数据源节点"""
        item = QTreeWidgetItem(parent_item)
        is_category = "children" in node
        children = node.get("children", [])
        if is_category:
            item.setText(0, self._cat_display(node))
            item.setData(0, ROLE_KIND, "category")
            item.setData(0, ROLE_NAME, node.get("name", ""))
            self._store_node(item, node)
            item.setExpanded(True)
            for child in children:
                self._add_tree_node(item, child)
        else:
            item.setText(0, self._src_display(node))
            item.setData(0, ROLE_KIND, "source")
            item.setData(0, ROLE_NAME, node.get("name", ""))
            self._store_node(item, node)
        parent_item.addChild(item)

    def _store_node(self, item: QTreeWidgetItem, node: dict) -> None:
        """把节点引用登记进 _node_map，item 只存 id"""
        self._node_map[id(node)] = node
        item.setData(0, ROLE_NODE_ID, id(node))

    @staticmethod
    def _cat_display(node: dict) -> str:
        """分类节点显示文本"""
        icon = node.get("icon", "")
        name = node.get("name", "")
        prefix = f"{icon} " if icon else ""
        return f"{prefix}📁 {name}"

    @staticmethod
    def _src_display(node: dict) -> str:
        """数据源节点显示文本"""
        name = node.get("name", "")
        table = node.get("table_name", "")
        return f"📄 {name}  [{table}]"

    # ------------------------------------------------------------------
    # 树操作
    # ------------------------------------------------------------------

    def _selected_item(self) -> QTreeWidgetItem | None:
        return self.tree.currentItem()

    def _selected_kind(self) -> str | None:
        item = self._selected_item()
        return item.data(0, ROLE_KIND) if item else None

    def _add_category(self) -> None:
        """在当前选中节点下新建子分类（无选中则加到顶级）"""
        parent_item = self._selected_item()
        # 数据源节点不能作为父级 → 用它的父
        if parent_item and parent_item.data(0, ROLE_KIND) == "source":
            parent_item = parent_item.parent()

        name, ok = self._ask_text("新建分类", "分类名称:")
        if not ok or not name.strip():
            return
        node = {"name": name.strip()}
        # 加到数据模型
        if parent_item is None:
            self.catalog.setdefault("categories", []).append(node)
        else:
            parent_node = self._node_for_item(parent_item)
            parent_node.setdefault("children", []).append(node)
        self._modified = True
        self._build_tree()
        self._select_name(name.strip())
        self._set_status("已添加分类，点击「保存」生效")

    def _add_source(self) -> None:
        """在当前分类下新建数据源（必须有分类父节点）"""
        parent_item = self._selected_item()
        if parent_item and parent_item.data(0, ROLE_KIND) == "source":
            parent_item = parent_item.parent()
        if parent_item is None or parent_item.data(0, ROLE_KIND) != "category":
            QMessageBox.information(self, "提示", "请先选择一个分类，再添加数据源")
            return

        # 默认 source_key 前缀
        parent_node = self._node_for_item(parent_item)
        parent_key = parent_node.get("source_key", "")
        prefix = parent_key + "." if parent_key else ""

        name, ok = self._ask_text("新建数据源", "数据源名称:")
        if not ok or not name.strip():
            return
        src_key, ok2 = self._ask_text(
            "新建数据源", "source_key（唯一标识）:",
            default=f"{prefix}new_source")
        if not ok2 or not src_key.strip():
            return
        if src_key.strip() in self._existing_keys:
            QMessageBox.warning(self, "提示", f"source_key「{src_key}」已存在")
            return

        node = {
            "name": name.strip(),
            "source_key": src_key.strip(),
            "api_source": "akshare",
            "api_function": "",
            "table_name": "",
        }
        parent_node.setdefault("children", []).append(node)
        self._existing_keys.add(src_key.strip())
        self._modified = True
        self._build_tree()
        self._select_name(name.strip())
        self._set_status("已添加数据源，请在右侧填写字段后保存")

    def _delete_node(self) -> None:
        """删除选中节点（分类删除整个子树）"""
        item = self._selected_item()
        if item is None:
            return
        kind = item.data(0, ROLE_KIND)
        name = item.data(0, ROLE_NAME)
        label = "分类" if kind == "category" else "数据源"
        ret = QMessageBox.question(
            self, "确认删除",
            f"删除{label}「{name}」？\n\n"
            + ("（其下所有数据源将一并删除）" if kind == "category" else ""),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return

        # 从数据模型移除
        if self._remove_item_from_model(item):
            self._modified = True
            self._build_tree()
            self._set_status("已删除，点击「保存」生效")

    def _remove_item_from_model(self, item: QTreeWidgetItem) -> bool:
        """从 catalog 数据模型移除节点对应的 dict，返回是否成功"""
        node = self._node_for_item(item)
        if node is None:
            return False
        kind = item.data(0, ROLE_KIND)
        parent_item = item.parent()

        if parent_item is None:
            # 顶级节点
            cats = self.catalog.get("categories", [])
            if node in cats:
                cats.remove(node)
                return True
        else:
            parent_node = self._node_for_item(parent_item)
            children = parent_node.get("children", [])
            if node in children:
                children.remove(node)
                # 移除占用的 source_key
                if kind == "source" and node.get("source_key"):
                    self._existing_keys.discard(node["source_key"])
                return True
        return False

    # ------------------------------------------------------------------
    # 详情面板
    # ------------------------------------------------------------------

    def _on_tree_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        """选中节点 → 显示详情编辑"""
        self._clear_detail()
        kind = item.data(0, ROLE_KIND)
        name = item.data(0, ROLE_NAME)

        # 找到对应节点 dict
        node = self._node_for_item(item)
        if node is None:
            return

        if kind == "category":
            self.detail_label.setText(f"📁 分类: {name}")
            self._build_category_form(node, item)
        else:
            self.detail_label.setText(f"📄 数据源: {name}")
            self._build_source_form(node, item)

    def _build_category_form(self, node: dict, item: QTreeWidgetItem) -> None:
        """分类详情：名称、图标、定时启用"""
        name_edit = QLineEdit(node.get("name", ""))
        icon_edit = QLineEdit(node.get("icon", ""))
        icon_edit.setPlaceholderText("可选 emoji，如 🇺🇸")
        sched_check = QCheckBox("启用定时更新")
        sched_check.setChecked(node.get("schedule_enabled") is not False)

        self.detail_form.addRow("名称", name_edit)
        self.detail_form.addRow("图标", icon_edit)
        self.detail_form.addRow("", sched_check)

        def _save():
            node["name"] = name_edit.text().strip()
            node["icon"] = icon_edit.text().strip()
            if sched_check.isChecked():
                node["schedule_enabled"] = True
            else:
                node["schedule_enabled"] = False
            item.setText(0, self._cat_display(node))
            item.setData(0, ROLE_NAME, node["name"])
            self._modified = True
            self._set_status("已修改分类，点击「保存」生效")

        self._add_save_button(_save)

    def _build_source_form(self, node: dict, item: QTreeWidgetItem) -> None:
        """数据源详情：所有字段"""
        # 可移动：目标分类下拉
        edits: dict[str, QLineEdit] = {}
        for field, label in self.SOURCE_FIELDS:
            edit = QLineEdit(str(node.get(field, "")))
            edits[field] = edit
            self.detail_form.addRow(label, edit)

        # 移动分类
        self.move_label.setText("移动到分类:")
        self.move_label.setVisible(True)
        move_combo = QComboBox()
        move_combo.addItem("（保持当前位置）")
        # ★ 并行列表存真实节点引用（PySide6 itemData 对 dict 会拷贝，不能用）
        self._move_targets: list[dict | None] = [None]
        self._populate_categories(move_combo, self._move_targets, item)
        self.detail_form.addRow("", move_combo)

        def _save():
            for field, edit in edits.items():
                val = edit.text().strip()
                if val:
                    node[field] = val
                else:
                    node.pop(field, None)
            item.setText(0, self._src_display(node))
            self._modified = True
            self._set_status("已修改数据源，点击「保存」生效")

        self._add_save_button(_save)
        self._pending = {"node": node, "item": item, "move_combo": move_combo}

    def _populate_categories(self, combo: QComboBox, targets: list,
                             current_item: QTreeWidgetItem) -> None:
        """把所有分类填进下拉（排除当前节点自身）

        targets 是与 combo 条目平行的真实节点引用列表。
        （PySide6 的 itemData 存储 dict 会拷贝，不能用来拿引用）
        """
        current_name = current_item.data(0, ROLE_NAME)

        def _add(nodes, parent_path=""):
            for node in nodes:
                if "children" in node:   # 分类节点（含空 children）
                    name = node.get("name", "")
                    combo.addItem(parent_path + name)
                    targets.append(node)
                    _add(node["children"], parent_path + name + "/")

        _add(self.catalog.get("categories", []))

    def _add_save_button(self, callback) -> None:
        btn = QPushButton("✔ 应用到树")
        btn.clicked.connect(callback)
        self.detail_form.addRow("", btn)
        self._detail_save_btn = btn

    def _clear_detail(self) -> None:
        """清空详情面板"""
        while self.detail_form.rowCount() > 0:
            self.detail_form.removeRow(0)
        self.detail_label.setText("← 选择一个节点编辑")
        self.move_label.setVisible(False)

    def _apply_move(self) -> None:
        """把数据源移动到其他分类（在保存按钮触发时调用）"""
        pending = getattr(self, "_pending", None)
        if not pending:
            return
        # 用并行 targets 列表取真实节点引用（PySide6 itemData 会拷贝 dict）
        targets = self._move_targets
        idx = pending["move_combo"].currentIndex()
        if idx < 0 or idx >= len(targets):
            return
        target = targets[idx]
        if target is None:
            return
        node = pending["node"]
        item = pending["item"]
        # 从原父节点移除
        self._remove_item_from_model(item)
        # 添加到目标
        target.setdefault("children", []).append(node)
        self._modified = True
        self._build_tree()
        self._set_status("已移动数据源，点击「保存」生效")

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    def _node_for_item(self, item: QTreeWidgetItem) -> dict | None:
        """返回树节点对应的 catalog 数据 dict（从 _node_map 查真实引用）"""
        if item is None:
            return None
        node_id = item.data(0, ROLE_NODE_ID)
        if not isinstance(node_id, int):
            return None
        return self._node_map.get(node_id)

    def _select_name(self, name: str) -> None:
        """选中名称匹配的树节点"""
        def _walk(item: QTreeWidgetItem):
            if item.data(0, ROLE_NAME) == name:
                self.tree.setCurrentItem(item)
                self.tree.itemClicked.emit(item, 0)
                return True
            for i in range(item.childCount()):
                if _walk(item.child(i)):
                    return True
            return False

        for i in range(self.tree.topLevelItemCount()):
            if _walk(self.tree.topLevelItem(i)):
                break

    def _ask_text(self, title: str, label: str, default: str = "") -> tuple[str, bool]:
        """弹输入框，返回 (文本, 是否确认)"""
        from PySide6.QtWidgets import QInputDialog
        text, ok = QInputDialog.getText(self, title, label, text=default)
        return text, ok

    def _set_status(self, msg: str) -> None:
        self.status_label.setText(msg)

    # ------------------------------------------------------------------
    # 保存
    # ------------------------------------------------------------------

    def _save(self) -> None:
        """保存并关闭"""
        # 先应用待定的移动
        self._apply_move()

        # 校验
        err = validate_catalog(self.catalog)
        if err:
            QMessageBox.warning(self, "保存失败", err)
            return

        if save_catalog(self.catalog):
            self._modified = False
            QMessageBox.information(
                self, "保存成功",
                "数据分类已保存，左侧导航树将刷新。")
            self.accept()
        else:
            QMessageBox.critical(self, "保存失败", "请检查控制台日志")

    def closeEvent(self, event) -> None:
        """未保存修改时提醒"""
        if self._modified:
            ret = QMessageBox.question(
                self, "未保存",
                "有未保存的修改，确定关闭？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ret != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        super().closeEvent(event)
