"""主题条目选择器 — 按数据分类树浏览, 选中源后添加指标或代码条目

- 左侧 QTreeWidget 渲染数据分类树(与主界面左侧树同款: 📁分类 / 📄数据源,
  deprecated 源带「 ⛔ 已停更」后缀并灰禁不可选); 顶部搜索框支持:
  - 名称/指标键过滤分类树(中文名、英文名、indicator 键、source_key 均匹配)
  - 代码直达: 输入基金/股票/指数代码(518880 / 518880.sh)时反查库内代码,
    命中即自动定位到该数据源并预填代码 → 直接「确定」
  - 指标直达: 输入唯一精确命中的宏观指标中文名/indicator 键时自动定位该源
- 右侧详情面板随选中源切换:
  - indicator 源(有 indicator 键): 显示指标键 + 显示名 → 「确定」
  - code 源(行情类, 基金/股票/指数): 选具体代码(get_distinct_values) + 值列
    (close/open/...) + 显示名 → 「确定」
- `result_item` 属性输出构造好的 item dict, 供 BoardManagerDialog 添加行。

条目类型判定 / 代码列解析统一走 src.core.boards 的模块级辅助
(classify_source / resolve_code_column / code_value_columns / collect_code_sources),
与校验同口径。
"""

import re

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QTreeWidget, QTreeWidgetItem, QComboBox, QWidget, QSplitter,
    QMessageBox,
)

from src.core.boards import (
    classify_source, code_value_columns,
    resolve_code_column,
)


class _NoWheelComboBox(QComboBox):
    """滚轮经过不切换当前项(防误触)"""

    def wheelEvent(self, e):
        e.ignore()


class BoardItemPickerDialog(QDialog):
    """分类树浏览 → 添加 indicator 或 code 条目(也用于 re-edit 回显)"""

    def __init__(self, repo, registry, existing_item: dict | None = None,
                 parent=None):
        super().__init__(parent)
        self.repo = repo
        self.registry = registry
        self.result_item: dict | None = None  # 确认后由调用方读取
        # source_key → QTreeWidgetItem 映射(供 re-edit 反查定位)
        self._key_to_item: dict[str, QTreeWidgetItem] = {}
        # 当前选中的叶源配置
        self._selected_source: dict | None = None

        self.setWindowTitle("选择指标/代码数据")
        self.setMinimumSize(760, 520)
        self.resize(820, 560)
        self._setup_ui()
        self._build_tree()
        self._apply_existing(existing_item)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        # 搜索框: 名称/指标/代码均可直达
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText(
            "🔍 搜名称/指标(如 CPI/失业率/基金), 或直接输代码(如 518880 / 600519)")
        self.search_edit.textChanged.connect(self._on_search)
        layout.addWidget(self.search_edit)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # 左侧: 分类树
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setColumnCount(1)
        self.tree.itemClicked.connect(self._on_item_clicked)
        splitter.addWidget(self.tree)

        # 右侧: 详情面板(随选中切换)
        self.detail = QWidget()
        self._build_detail_panel()
        splitter.addWidget(self.detail)
        splitter.setSizes([360, 400])
        layout.addWidget(splitter, stretch=1)

        # 底部
        bottom = QHBoxLayout()
        self.ok_btn = QPushButton("确定")
        self.ok_btn.setStyleSheet(
            "QPushButton { background-color:#1976D2; color:white; "
            "padding:6px 16px; border-radius:4px; font-weight:bold; }"
            "QPushButton:hover { background-color:#1565C0; }")
        self.ok_btn.clicked.connect(self._confirm)
        self.ok_btn.setEnabled(False)
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.clicked.connect(self.reject)
        bottom.addStretch()
        bottom.addWidget(self.ok_btn)
        bottom.addWidget(self.cancel_btn)
        layout.addLayout(bottom)

    def _build_detail_panel(self) -> None:
        from PySide6.QtWidgets import QGridLayout, QGroupBox

        self.detail_layout = QVBoxLayout(self.detail)
        self.detail_info = QLabel("请从左侧选择数据源")
        self.detail_info.setWordWrap(True)
        self.detail_info.setStyleSheet("color:#666; font-size:12px;")
        self.detail_layout.addWidget(self.detail_info)

        # --- indicator 源面板 ---
        self.ind_group = QGroupBox("宏观指标")
        g = QGridLayout(self.ind_group)
        g.addWidget(QLabel("指标键:"), 0, 0)
        self.ind_key_label = QLabel("-")
        self.ind_key_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        g.addWidget(self.ind_key_label, 0, 1)
        g.addWidget(QLabel("显示名:"), 1, 0)
        self.ind_name_edit = QLineEdit()
        self.ind_name_edit.setPlaceholderText("留空 = 指标键")
        g.addWidget(self.ind_name_edit, 1, 1)
        self.detail_layout.addWidget(self.ind_group)
        self.ind_group.hide()

        # --- code 源面板 ---
        self.code_group = QGroupBox("代码数据(基金/股票/指数)")
        g2 = QGridLayout(self.code_group)
        g2.addWidget(QLabel("数据表:"), 0, 0)
        self.code_table_label = QLabel("-")
        g2.addWidget(self.code_table_label, 0, 1)
        g2.addWidget(QLabel("代码:"), 1, 0)
        self.code_combo = _NoWheelComboBox()
        self.code_combo.setEditable(True)
        self.code_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.code_combo.setMinimumWidth(160)
        self.code_combo.setToolTip(
            "已同步过的代码下拉; 也可直接输入库内尚无的代码")
        g2.addWidget(self.code_combo, 1, 1)
        g2.addWidget(QLabel("值列:"), 2, 0)
        self.value_combo = _NoWheelComboBox()
        self.value_combo.setToolTip("用哪个 OHLC 列作值(默认 close)")
        g2.addWidget(self.value_combo, 2, 1)
        g2.addWidget(QLabel("显示名:"), 3, 0)
        self.code_name_edit = QLineEdit()
        self.code_name_edit.setPlaceholderText("留空 = 表:代码")
        g2.addWidget(self.code_name_edit, 3, 1)
        self.detail_layout.addWidget(self.code_group)
        self.code_group.hide()

        # --- 不可选提示 ---
        self.disabled_label = QLabel("")
        self.disabled_label.setWordWrap(True)
        self.disabled_label.setStyleSheet("color:#999;")
        self.detail_layout.addWidget(self.disabled_label)
        self.disabled_label.hide()

        self.detail_layout.addStretch()

    # ------------------------------------------------------------------
    # 分类树构建
    # ------------------------------------------------------------------

    def _build_tree(self) -> None:
        self.tree.clear()
        self._key_to_item.clear()
        categories = self.registry.get_categories()
        for cat in categories:
            item = QTreeWidgetItem(self.tree)
            item.setText(0, f"📁 {cat.get('name', '')}")
            item.setData(0, Qt.ItemDataRole.UserRole, None)  # 分类节点
            self._build_children(item, cat.get("children", []))
            item.setExpanded(True)
        self.tree.expandAll()

    def _build_children(self, parent_item: QTreeWidgetItem,
                        children: list[dict]) -> None:
        """递归构建子节点(判据照抄 DataTreeWidget._build_children)"""
        for child in children:
            item = QTreeWidgetItem(parent_item)
            name = child.get("name", "")
            if child.get("deprecated"):
                name = f"{name} ⛔ 已停更"
            item.setText(0, name)

            sub_children = child.get("children")
            if sub_children:
                item.setData(0, Qt.ItemDataRole.UserRole, None)  # 分类
                self._build_children(item, sub_children)
            else:
                source_key = child.get("source_key") or child.get("key", "")
                is_leaf = bool(child.get("api_function"))
                if is_leaf and source_key:
                    self._key_to_item[source_key] = item
                if child.get("deprecated"):
                    # 灰 + 禁点(deprecated 源不可选)
                    item.setForeground(0, QBrush(QColor("#999999")))
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                    item.setData(0, Qt.ItemDataRole.UserRole, None)
                else:
                    item.setData(0, Qt.ItemDataRole.UserRole, child)
                item.setToolTip(0, self._leaf_tooltip(child))

    @staticmethod
    def _leaf_tooltip(cfg: dict) -> str:
        """叶节点 tooltip: 表名 / 来源 / indicator / 代码列"""
        lines = [f"表: {cfg.get('table_name', '')}",
                 f"来源: {cfg.get('api_source', '')}"]
        if cfg.get("indicator"):
            lines.append(f"指标: {cfg.get('indicator')}")
        cc = cfg.get("code_column")
        if cc:
            lines.append(f"代码列: {cc}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 交互
    # ------------------------------------------------------------------

    def _on_item_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        cfg = item.data(0, Qt.ItemDataRole.UserRole)
        self._selected_source = cfg if isinstance(cfg, dict) else None
        if not self._selected_source:
            self._reset_detail("请从左侧选择数据源")
            self.ok_btn.setEnabled(False)
            return
        kind = classify_source(self._selected_source, self.repo)
        if kind is None:
            self._reset_detail("该数据源不可选(deprecated 或无法解析代码列)")
            self.ok_btn.setEnabled(False)
            return
        self._populate_detail(kind)

    def _populate_detail(self, kind: str) -> None:
        cfg = self._selected_source
        self.ind_group.hide()
        self.code_group.hide()
        self.disabled_label.hide()
        if kind == "indicator":
            self.ind_key_label.setText(cfg.get("indicator", ""))
            self.ind_name_edit.setText(cfg.get("name", ""))
            self.ind_group.show()
        elif kind == "code":
            table = cfg.get("table_name", "")
            code_col = resolve_code_column(cfg, self.repo) or "code"
            self.code_table_label.setText(f"{table} (代码列: {code_col})")
            # 代码下拉候选(表内已同步的, 原样值不剥前缀)
            self.code_combo.clear()
            try:
                codes = self.repo.get_distinct_values(table, code_col)
            except Exception:
                codes = []
            for c in codes:
                self.code_combo.addItem(str(c))
            if not codes:
                self.code_combo.addItem("")  # 空占位, 可手动输入
            # 值列候选
            self.value_combo.clear()
            self.value_combo.addItems(code_value_columns(cfg, self.repo))
            default = "close" if "close" in code_value_columns(cfg, self.repo) else 0
            idx = self.value_combo.findText("close")
            self.value_combo.setCurrentIndex(idx if idx >= 0 else 0)
            self.code_name_edit.setText(cfg.get("name", ""))
            self.code_group.show()
        self.ok_btn.setEnabled(True)

    def _reset_detail(self, text: str) -> None:
        self.ind_group.hide()
        self.code_group.hide()
        self.disabled_label.setText(text)
        self.disabled_label.show()

    def _on_search(self, text: str) -> None:
        """搜索: 名称/指标键过滤 + 代码直达 / 指标直达

        - 先按名称 contains 过滤分类树(中文名/英文名/indicator 键/source_key
          均匹配), 命中的叶子保留祖先路径;
        - 输入为代码格式(5-6 位数字, 可带 .sh/.sz 后缀)时, 额外做代码反查:
          命中库内该代码 → 自动定位到对应数据源并预填代码(代码直达);
        - 否则若唯一精确/部分命中某宏观指标 → 自动定位该源(指标直达)。
        """
        text = text.strip().lower()
        if not text:
            self._show_all_items(True)
            return
        self._filter_tree_by_name(text)
        code = self._normalize_code(text)
        if code:
            self._maybe_match_code(code)
        else:
            self._maybe_match_indicator(text)

    def _filter_tree_by_name(self, text: str) -> None:
        """名称过滤: 节点名 contains, 叶子额外匹配 indicator 键/source_key"""
        def walk(item: QTreeWidgetItem) -> bool:
            cfg = item.data(0, Qt.ItemDataRole.UserRole)
            visible = text in item.text(0).lower()
            if isinstance(cfg, dict):
                visible = (visible
                           or text in str(cfg.get("indicator") or "").lower()
                           or text in str(cfg.get("source_key") or "").lower())
            child_visible = False
            for i in range(item.childCount()):
                child_visible = walk(item.child(i)) or child_visible
            matched = visible or child_visible
            item.setHidden(not matched)
            if matched:
                item.setExpanded(True)
            return matched

        for i in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(i))

    @staticmethod
    def _normalize_code(text: str) -> str | None:
        """代码格式判定 + 归一: 518880 / 518880.sh → '518880'; 非代码 → None"""
        m = re.match(r"^(\d{5,6})(\.(sh|sz|bj|of|zs))?$", text.strip().lower())
        return m.group(1) if m else None

    def _maybe_match_code(self, code: str) -> None:
        """代码直达: 在 code 类数据源表内反查该代码, 命中即定位并预填"""
        for s in self.registry.get_all_sources():
            if classify_source(s, self.repo) != "code":
                continue  # deprecated / 非代码源, 与 picker 其余判定同口径
            table = s.get("table_name", "")
            code_col = resolve_code_column(s, self.repo) or "code"
            try:
                if self.repo.get_max_date(table, code_col, code):
                    self._select_source(s, "code")
                    self.code_combo.setCurrentText(code)
                    return
            except Exception:
                continue
        # 完整代码在所有 code 源中均不存在 → 清除直达残留(避免上次命中的
        # 代码误导), 明确提示用户当前输入无匹配
        self._reset_detail(f"未在已同步数据中找到代码 {code}; 可先选数据源后手动输入")
        self.ok_btn.setEnabled(False)
        self.code_combo.clear()

    def _maybe_match_indicator(self, text: str) -> None:
        """指标直达: 非代码输入唯一命中某宏观指标(名称/indicator 键)时定位"""
        sources = [s for s in self.registry.get_all_sources()
                   if s.get("indicator") and not s.get("deprecated")]

        def matches(s: dict) -> bool:
            return (text in str(s.get("name") or "").lower()
                    or text in str(s.get("indicator") or "").lower())

        exact = [s for s in sources
                 if text == str(s.get("name") or "").lower()
                 or text == str(s.get("indicator") or "").lower()]
        if len(exact) == 1:
            self._select_source(exact[0], "indicator")
            return
        contains = [s for s in sources if matches(s)]
        if len(contains) == 1:
            self._select_source(contains[0], "indicator")

    def _show_all_items(self, show: bool) -> None:
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            item.setHidden(not show)
            for j in range(item.childCount()):
                item.child(j).setHidden(not show)

    # ------------------------------------------------------------------
    # re-edit 回显
    # ------------------------------------------------------------------

    def _apply_existing(self, existing_item: dict | None) -> None:
        """打开时若传入已存条目, 定位到对应源并预填"""
        if not existing_item:
            return
        from src.core.boards import _item_type
        if _item_type(existing_item) == "code":
            table = existing_item.get("table", "")
            cfg = self._find_source_by_table(table)
            if cfg is None:
                return
            self._select_source(cfg, kind="code")
            # 预填 code / 值列 / 显示名
            self.code_combo.setCurrentText(existing_item.get("code", ""))
            idx = self.value_combo.findText(
                existing_item.get("value_column", "close"))
            if idx >= 0:
                self.value_combo.setCurrentIndex(idx)
            if existing_item.get("name"):
                self.code_name_edit.setText(existing_item["name"])
        else:
            ind = existing_item.get("indicator", "")
            cfg = self._find_source_by_indicator(ind)
            if cfg is None:
                return
            self._select_source(cfg, kind="indicator")
            if existing_item.get("name"):
                self.ind_name_edit.setText(existing_item["name"])

    def _select_source(self, cfg: dict, kind: str) -> None:
        """定位并展开到某源, 填充详情面板

        同时把该源及其祖先从隐藏中恢复(搜索过滤/代码直达/指标直达场景下,
        目标源可能被名称过滤成隐藏态)。
        """
        key = cfg.get("source_key", "")
        item = self._key_to_item.get(key)
        if item is not None:
            # 恢复可见 + 展开祖先
            p = item.parent()
            while p is not None:
                p.setHidden(False)
                p.setExpanded(True)
                p = p.parent()
            item.setHidden(False)
            self.tree.setCurrentItem(item)
        self._selected_source = cfg
        self._populate_detail(kind)

    def _find_source_by_table(self, table: str) -> dict | None:
        for s in self.registry.get_all_sources():
            if s.get("table_name") == table and not s.get("deprecated"):
                return s
        return None

    def _find_source_by_indicator(self, indicator: str) -> dict | None:
        for s in self.registry.get_all_sources():
            if s.get("indicator") == indicator and not s.get("deprecated"):
                return s
        return None

    # ------------------------------------------------------------------
    # 确认
    # ------------------------------------------------------------------

    def _confirm(self) -> None:
        cfg = self._selected_source
        if not cfg:
            return
        kind = classify_source(cfg, self.repo)
        if kind == "indicator":
            ind = cfg.get("indicator", "")
            name = self.ind_name_edit.text().strip()
            it = {"type": "indicator", "indicator": ind}
            if name:
                it["name"] = name
            self.result_item = it
            self.accept()
        elif kind == "code":
            # 下拉 popup 打开时点「确定」, Qt 关闭 popup 会把 lineEdit 回退到
            # 列表高亮行(type-ahead 高亮只移 view 不改 lineEdit), 先记录用户
            # 输入并恢复, 再取实际值
            if self.code_combo.view().isVisible():
                typed = self.code_combo.lineEdit().text()
                self.code_combo.hidePopup()
                if typed:
                    self.code_combo.lineEdit().setText(typed)
            code = self.code_combo.lineEdit().text().strip()
            if not code:
                QMessageBox.warning(self, "提示", "请输入/选择代码")
                return
            table = cfg.get("table_name", "")
            code_col = resolve_code_column(cfg, self.repo) or "code"
            value_col = self.value_combo.currentText().strip() or "close"
            name = self.code_name_edit.text().strip()
            it = {"type": "code", "table": table, "code_column": code_col,
                  "code": code, "value_column": value_col}
            if name:
                it["name"] = name
            self.result_item = it
            self.accept()
