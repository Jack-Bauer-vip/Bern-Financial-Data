"""主题看板(定制数据集)对话框 — 建主题、选指标/代码、定日期窗口、看每日快照

- 左侧:主题列表(新建/复制/删除)
- 右侧「编辑」页:名称/描述/看板级日期窗口 + 条目清单(条目/显示名/口径/单项窗口),
  条目经分类树选择器添加(BoardItemPickerDialog):宏观指标或代码数据(基金/股票/指数)
- 右侧「快照」页:每日快照概览(每条目一行最新值),可导出 CSV/Excel
- 底部:保存(校验写回 themes.yaml)/ 同步本主题(后台增量同步)/ 导出

配置存 config/themes.yaml,由 src.core.boards.BoardStore 读写。
"""

import re
from datetime import datetime

from PySide6.QtCore import Qt, QDate, QObject, QThread, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QListWidget, QListWidgetItem, QTableWidget,
    QComboBox, QHeaderView, QCheckBox, QDateEdit, QTabWidget,
    QSplitter, QWidget, QMessageBox,
)

from src.core.boards import (
    BoardService, BoardStore, _item_type, board_sync_targets,
    collect_code_sources, collect_indicator_keys,
)
from src.gui.dialogs.board_item_picker_dialog import BoardItemPickerDialog
from src.gui.table_view import DataTableView


class _NoWheelComboBox(QComboBox):
    """禁用滚轮切换的下拉(防误触)：必须点开手动选择"""

    def wheelEvent(self, event):
        event.ignore()


class _ItemSummaryCell(QWidget):
    """主题条目摘要单元格: 只读文本 + 「…」重新选择按钮

    点击「…」打开分类树选择器(existing_item=该行条目), 确认后经
    edit_requested 回调由对话框更新 _row_items 并重绘。
    """

    edit_requested = Signal(int)

    def __init__(self, text: str, row: int, parent=None):
        super().__init__(parent)
        self.row = row
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self.label = QLineEdit(text)
        self.label.setReadOnly(True)
        self.label.setFrame(False)
        self.label.setStyleSheet("background: transparent;")
        self.label.setToolTip(text)
        self.btn = QPushButton("…")
        self.btn.setFixedWidth(32)
        self.btn.setToolTip("重新选择指标/代码数据")
        self.btn.clicked.connect(lambda: self.edit_requested.emit(self.row))
        lay.addWidget(self.label, stretch=1)
        lay.addWidget(self.btn)


# ---------------------------------------------------------------------------
# 后台同步工作器(「同步本主题」不阻塞对话框)
# ---------------------------------------------------------------------------


class BoardSyncWorker(QObject):
    """逐个同步主题所引用数据源(indicator 获信源表 / code 条目所在表)"""

    finished = Signal()
    error = Signal(str)

    def __init__(self, sync_engine, sync_targets: list[tuple[str, dict | None]]):
        super().__init__()
        self.sync_engine = sync_engine
        self.sync_targets = sync_targets

    def run(self) -> None:
        for key, params in self.sync_targets:
            try:
                self.sync_engine.run(key, params_override=params)
            except Exception as exc:
                self.error.emit(f"{key}: {exc}")
        self.finished.emit()


class BoardManagerDialog(QDialog):
    """主题看板(定制数据集)管理"""

    def __init__(self, repo, registry, sync_engine=None, log_fn=None, parent=None):
        super().__init__(parent)
        self.repo = repo
        self.registry = registry
        self.sync_engine = sync_engine
        self.log_fn = log_fn or (lambda *a, **k: None)
        self.store = BoardStore()
        self.service = BoardService(repo)
        self.known_indicators = collect_indicator_keys(registry)
        # 代码类数据源表清单(table_name → 目录信息, 含 deprecated 供校验 reject)
        self.known_code_sources = collect_code_sources(registry, repo)
        # 条目行数据源(与 items_table 行平行; 树选择器产出)
        self._row_items: list[dict] = []

        self._current_key: str | None = None   # 正在编辑的主题 key(None=新建)
        self._thread = None

        self.setWindowTitle("主题看板(定制数据集)")
        self.setMinimumSize(980, 620)
        self.resize(1100, 680)
        self._setup_ui()
        self._load_boards()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        title = QLabel("📊 主题看板 — 把常用指标组合成定制数据集, 每天打开看快照")
        title.setStyleSheet("font-size: 15px; font-weight: bold; padding: 6px 0;")
        layout.addWidget(title)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # ---- 左:主题列表 ----
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 8, 0)
        self.board_list = QListWidget()
        self.board_list.itemClicked.connect(self._on_board_selected)
        left_layout.addWidget(QLabel("主题:"))
        left_layout.addWidget(self.board_list, stretch=1)
        btn_row = QHBoxLayout()
        self.add_btn = QPushButton("＋ 新建")
        self.add_btn.clicked.connect(self._new_board)
        self.copy_btn = QPushButton("复制")
        self.copy_btn.clicked.connect(self._copy_board)
        self.del_btn = QPushButton("删除")
        self.del_btn.clicked.connect(self._delete_board)
        btn_row.addWidget(self.add_btn)
        btn_row.addWidget(self.copy_btn)
        btn_row.addWidget(self.del_btn)
        left_layout.addLayout(btn_row)
        splitter.addWidget(left)

        # ---- 右:编辑 / 快照 两个页签 ----
        tabs = QTabWidget()
        tabs.addTab(self._build_edit_tab(), "编辑")
        tabs.addTab(self._build_view_tab(), "快照")
        splitter.addWidget(tabs)
        splitter.setSizes([240, 760])
        layout.addWidget(splitter, stretch=1)

        # ---- 底部操作 ----
        bottom = QHBoxLayout()
        self.save_btn = QPushButton("💾 保存")
        self.save_btn.setStyleSheet(
            "QPushButton { background-color:#1976D2; color:white; "
            "padding:6px 16px; border-radius:4px; font-weight:bold; }"
            "QPushButton:hover { background-color:#1565C0; }")
        self.save_btn.clicked.connect(self._save_current)
        self.sync_btn = QPushButton("🔁 同步本主题")
        self.sync_btn.setStyleSheet(
            "QPushButton { background-color:#2E7D32; color:white; "
            "padding:6px 12px; border-radius:4px; }"
            "QPushButton:hover { background-color:#1B5E20; }")
        self.sync_btn.clicked.connect(self._sync_board)
        self.sync_btn.setEnabled(bool(self.sync_engine))
        self.export_btn = QPushButton("导出 ▼")
        self.export_btn.clicked.connect(self._export_snapshot)
        self.close_btn = QPushButton("关闭")
        self.close_btn.clicked.connect(self.accept)
        bottom.addWidget(self.save_btn)
        bottom.addWidget(self.sync_btn)
        bottom.addStretch()
        bottom.addWidget(self.export_btn)
        bottom.addWidget(self.close_btn)
        layout.addLayout(bottom)

    def _build_edit_tab(self) -> QWidget:
        tab = QWidget()
        v = QVBoxLayout(tab)

        # key:主题唯一标识(小写字母/数字/下划线)。新建可编辑,保存后(编辑态)只读。
        key_row = QHBoxLayout()
        key_row.addWidget(QLabel("key:"))
        self.key_edit = QLineEdit()
        self.key_edit.setPlaceholderText("如 daily_macro(小写字母/数字/下划线)")
        self.key_edit.setToolTip(
            "主题唯一标识。新建时留空会按名称自动生成;保存后只读不可改。")
        key_row.addWidget(self.key_edit, stretch=1)
        v.addLayout(key_row)

        form = QHBoxLayout()
        form.addWidget(QLabel("名称:"))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("如 每日宏观")
        form.addWidget(self.name_edit, stretch=1)
        form.addWidget(QLabel("描述:"))
        self.desc_edit = QLineEdit()
        self.desc_edit.setPlaceholderText("可选")
        form.addWidget(self.desc_edit, stretch=2)
        v.addLayout(form)
        # 名称变化且 key 仍空(新建)时,自动给一个建议 key,用户可改
        self.name_edit.textChanged.connect(self._auto_suggest_key)

        # 看板级日期窗口
        date_row = QHBoxLayout()
        self.always_latest = QCheckBox("始终最新")
        self.always_latest.setChecked(True)
        self.always_latest.toggled.connect(self._on_always_latest_toggled)
        self.date_start = QDateEdit()
        self.date_start.setCalendarPopup(True)
        self.date_start.setDisplayFormat("yyyy-MM-dd")
        self.date_start.setDate(QDate.currentDate().addYears(-1))
        self.date_end = QDateEdit()
        self.date_end.setCalendarPopup(True)
        self.date_end.setDisplayFormat("yyyy-MM-dd")
        self.date_end.setDate(QDate.currentDate())
        date_row.addWidget(self.always_latest)
        date_row.addWidget(QLabel("从"))
        date_row.addWidget(self.date_start)
        date_row.addWidget(QLabel("到"))
        date_row.addWidget(self.date_end)
        date_row.addStretch()
        # 看板级日期窗口变化 → 刷新条目日期列「继承」提示
        self.date_start.dateChanged.connect(self._update_item_date_placeholders)
        self.date_end.dateChanged.connect(self._update_item_date_placeholders)
        self._on_always_latest_toggled(True)
        v.addLayout(date_row)

        v.addWidget(QLabel(
            "条目清单:点「＋ 添加条目」从数据分类树选择宏观指标或代码数据"
            "(基金/股票/指数); 点每行「…」可重选。留空开始/结束日期 = 继承看板级窗口):"))

        self.items_table = QTableWidget(0, 5)
        self.items_table.setHorizontalHeaderLabels(
            ["条目", "显示名", "口径", "开始日期", "结束日期"])
        self.items_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        self.items_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents)
        self.items_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        self.items_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers)
        self.items_table.verticalHeader().setVisible(False)
        self.items_table.setAlternatingRowColors(True)
        v.addWidget(self.items_table, stretch=1)

        item_btns = QHBoxLayout()
        self.add_item_btn = QPushButton("＋ 添加条目")
        self.add_item_btn.setToolTip("从数据分类树选择宏观指标或代码数据(基金/股票/指数)")
        self.add_item_btn.clicked.connect(self._add_item_clicked)
        self.remove_item_btn = QPushButton("删除选中")
        self.remove_item_btn.clicked.connect(self._remove_item_row)
        self.move_up_btn = QPushButton("上移")
        self.move_up_btn.clicked.connect(lambda: self._move_item(-1))
        self.move_down_btn = QPushButton("下移")
        self.move_down_btn.clicked.connect(lambda: self._move_item(1))
        item_btns.addWidget(self.add_item_btn)
        item_btns.addWidget(self.remove_item_btn)
        item_btns.addWidget(self.move_up_btn)
        item_btns.addWidget(self.move_down_btn)
        item_btns.addStretch()
        v.addLayout(item_btns)
        return tab

    def _build_view_tab(self) -> QWidget:
        tab = QWidget()
        v = QVBoxLayout(tab)
        tip = QLabel("每日快照:每条目一行(指标=最新值/环比/同比/近3期; 代码=最新价+日期)。")
        tip.setStyleSheet("color:#888; font-size:11px;")
        v.addWidget(tip)
        self.view_table = DataTableView(tab)
        v.addWidget(self.view_table, stretch=1)
        refresh_row = QHBoxLayout()
        self.refresh_view_btn = QPushButton("🔄 刷新快照")
        self.refresh_view_btn.clicked.connect(self._refresh_snapshot)
        refresh_row.addWidget(self.refresh_view_btn)
        refresh_row.addStretch()
        v.addLayout(refresh_row)
        return tab

    # ------------------------------------------------------------------
    # 主题列表
    # ------------------------------------------------------------------

    def _load_boards(self) -> None:
        """刷新左侧主题列表(保留当前选中)"""
        current = self._current_key
        self.board_list.blockSignals(True)
        self.board_list.clear()
        for b in self.store.list_boards():
            item = QListWidgetItem(f"{b['name']}  ({b['key']})")
            item.setData(Qt.ItemDataRole.UserRole, b["key"])
            item.setToolTip(
                f"key={b['key']}\n描述: {b['description'] or '—'}\n"
                f"指标数: {b['item_count']}")
            self.board_list.addItem(item)
        self.board_list.blockSignals(False)
        if current:
            for i in range(self.board_list.count()):
                if self.board_list.item(i).data(Qt.ItemDataRole.UserRole) == current:
                    self.board_list.setCurrentRow(i)
                    break

    def _on_board_selected(self, item: QListWidgetItem) -> None:
        key = item.data(Qt.ItemDataRole.UserRole)
        self._current_key = key
        board = self.store.get_board(key)
        if board:
            self._populate_editor(board)

    def _new_board(self) -> None:
        """清空编辑器进入新建态(不立即写盘,点保存才建)"""
        if not self._confirm_discard():
            return
        self._current_key = None
        self._populate_editor({
            "key": "", "name": "", "description": "",
            "date_start": None, "date_end": None, "items": [],
        })

    def _copy_board(self) -> None:
        """复制当前主题为副本(新 key, 名称带「副本」)"""
        if not self._current_key:
            QMessageBox.information(self, "提示", "请先选择要复制的主题")
            return
        src = self.store.get_board(self._current_key)
        import copy
        new_key = f"{src['key']}_copy"
        n = 2
        while self.store.get_board(new_key) is not None:
            new_key = f"{src['key']}_copy{n}"
            n += 1
        clone = copy.deepcopy(src)
        clone["key"] = new_key
        clone["name"] = f"{src.get('name', '')} 副本"
        self.store.add_board(clone)
        self._current_key = new_key
        self._load_boards()
        self._populate_editor(clone)
        self.log_fn("INFO", f"已复制主题 {src['key']} → {new_key}")

    def _delete_board(self) -> None:
        if not self._current_key:
            QMessageBox.information(self, "提示", "请先选择要删除的主题")
            return
        key = self._current_key
        if QMessageBox.question(
                self, "删除确认",
                f"确定删除主题「{key}」吗?(仅删配置,不影响数据)") != QMessageBox.StandardButton.Yes:
            return
        if self.store.delete_board(key):
            self.log_fn("INFO", f"已删除主题 {key}")
            self._current_key = None
            self._load_boards()
            self._populate_editor({
                "key": "", "name": "", "description": "",
                "date_start": None, "date_end": None, "items": []})

    # ------------------------------------------------------------------
    # 编辑区
    # ------------------------------------------------------------------

    def _populate_editor(self, board: dict) -> None:
        """把主题 dict 渲染到编辑控件"""
        editing = self._current_key is not None
        key = str(board.get("key", "") or "")
        # 先设 key 再设名称:textChanged 触发 _auto_suggest_key 时,若 key 已有
        # (编辑态回显 / 用户已填)不会被建议值覆盖;仅新建且 key 空才按名称建议。
        self.key_edit.setText(key)
        self.key_edit.setReadOnly(editing)
        self.name_edit.setText(board.get("name", ""))
        self.desc_edit.setText(board.get("description", ""))
        always = board.get("date_start") is None and board.get("date_end") is None
        self.always_latest.setChecked(always)
        ds = board.get("date_start")
        de = board.get("date_end")
        if ds:
            qd = QDate.fromString(str(ds)[:10], "yyyy-MM-dd")
            if qd.isValid():
                self.date_start.setDate(qd)
        if de:
            qd = QDate.fromString(str(de)[:10], "yyyy-MM-dd")
            if qd.isValid():
                self.date_end.setDate(qd)
        self._on_always_latest_toggled(always)
        self._row_items = []
        self.items_table.setRowCount(0)
        for it in board.get("items", []):
            self._add_item_row(it)

    def _on_always_latest_toggled(self, checked: bool) -> None:
        self.date_start.setEnabled(not checked)
        self.date_end.setEnabled(not checked)
        self._update_item_date_placeholders()

    def _auto_suggest_key(self) -> None:
        """新建且 key 为空时,按名称给一个建议 key(用户可改;已填则不覆盖)"""
        if self._current_key is not None:
            return  # 编辑态不自动改
        if self.key_edit.text().strip():
            return  # 用户已填过
        name = self.name_edit.text().strip()
        if not name:
            return
        existing = {b["key"] for b in self.store.list_boards()}
        self.key_edit.setText(self._suggest_board_key(name, existing))

    @staticmethod
    def _suggest_board_key(name: str, existing_keys) -> str:
        """从名称生成主题 key:ASCII 段转小写下划线;无 ASCII 用时间戳;冲突加后缀

        e.g. 「每日宏观 Daily」→ daily;「测试」→ board_20260810_140000;
        daily 已存在 → daily_2。
        """
        slug = re.sub(r"\s+", "_", re.sub(r"[^A-Za-z0-9_ ]", "", name)).strip("_").lower()
        if not slug:
            slug = f"board_{datetime.now():%Y%m%d_%H%M%S}"
        base = slug
        n = 2
        while slug in existing_keys:
            slug = f"{base}_{n}"
            n += 1
        return slug

    def _update_item_date_placeholders(self) -> None:
        """条目行开始/结束日期列 placeholder 显示实际继承的看板级日期窗口"""
        if self.always_latest.isChecked():
            inherit = "继承看板级(始终最新)"
        else:
            ds = self.date_start.date().toString("yyyy-MM-dd")
            de = self.date_end.date().toString("yyyy-MM-dd")
            inherit = f"继承 {ds}~{de}"
        items_table = getattr(self, "items_table", None)
        if items_table is None:
            return  # _build_edit_tab 里日期控件先建、条目表格后建
        for row in range(items_table.rowCount()):
            for col in (3, 4):
                w = items_table.cellWidget(row, col)
                if isinstance(w, QLineEdit):
                    w.setPlaceholderText(inherit)

    def _add_item_clicked(self) -> None:
        """「＋ 添加条目」: 开分类树选择器, 确认后追加一行"""
        dlg = BoardItemPickerDialog(self.repo, self.registry, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_item:
            self._add_item_row(dlg.result_item)

    def _edit_item_row(self, row: int) -> None:
        """点行内「…」: 开选择器回显该行条目, 确认后原位更新(保留日期窗口)"""
        item = self._row_items[row] if 0 <= row < len(self._row_items) else None
        dlg = BoardItemPickerDialog(self.repo, self.registry,
                                    existing_item=item, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_item:
            new_item = dict(dlg.result_item)
            # 日期窗口在编辑区统一填, 重选时保留
            old = self._row_items[row]
            new_item.setdefault("date_start", old.get("date_start"))
            new_item.setdefault("date_end", old.get("date_end"))
            self._row_items[row] = new_item
            self._refresh_item_row(row)

    def _refresh_item_row(self, row: int) -> None:
        """按 _row_items[row] 重绘 0-2 列(摘要/显示名/口径); 日期列保留"""
        self._populate_item_cells(row)

    @staticmethod
    def _item_summary(item: dict) -> str:
        """条目摘要文本: indicator=`指标名 (key)`, code=`表 · 代码 · 值列`"""
        if _item_type(item) == "code":
            nm = item.get("name") or ""
            core = (f"{item.get('table', '')} · {item.get('code', '')}"
                    f" · {item.get('value_column', 'close')}")
            return f"{nm}  ({core})" if nm else core
        nm = item.get("name") or ""
        ind = item.get("indicator", "")
        return f"{nm} ({ind})" if nm else ind

    def _add_item_row(self, item: dict | None = None) -> int:
        """追加一行条目(树选择器产出的 item dict; 老配置兼容)"""
        item = dict(item or {})
        row = self.items_table.rowCount()
        self.items_table.insertRow(row)
        self._row_items.append(item)
        self._populate_item_cells(row)

        start_edit = QLineEdit(str(item.get("date_start") or ""))
        self.items_table.setCellWidget(row, 3, start_edit)
        end_edit = QLineEdit(str(item.get("date_end") or ""))
        self.items_table.setCellWidget(row, 4, end_edit)
        # 占位符显示实际继承的看板级窗口(始终最新 → 「继承看板级(始终最新)」)
        self._update_item_date_placeholders()
        return row

    def _populate_item_cells(self, row: int) -> None:
        """渲染第 row 行的 0-2 列(摘要/显示名/口径), 3-4 日期列不动"""
        item = self._row_items[row]
        kind = _item_type(item)

        # 0 摘要 + 「…」编辑
        cell = _ItemSummaryCell(self._item_summary(item), row)
        cell.edit_requested.connect(self._edit_item_row)
        self.items_table.setCellWidget(row, 0, cell)

        # 1 显示名
        name_edit = QLineEdit(str(item.get("name") or ""))
        name_edit.setPlaceholderText("留空=默认")
        self.items_table.setCellWidget(row, 1, name_edit)

        # 2 口径(仅 indicator 可选; 代码条目是价格, 无派生口径)
        trans = _NoWheelComboBox()
        if kind == "code":
            trans.addItem("—")
            trans.setEnabled(False)
            trans.setToolTip("代码条目无需口径(值是价格)")
        else:
            trans.addItems(["", "level", "yoy", "mom"])
            t = str(item.get("transform") or "")
            idx = trans.findText(t)
            trans.setCurrentIndex(idx if idx >= 0 else 0)
            trans.setToolTip("口径: 空=按获信源存储口径")
        self.items_table.setCellWidget(row, 2, trans)

    def _remove_item_row(self) -> None:
        row = self.items_table.currentRow()
        if 0 <= row < len(self._row_items):
            self._row_items.pop(row)
            self.items_table.removeRow(row)

    def _move_item(self, delta: int) -> None:
        row = self.items_table.currentRow()
        target = row + delta
        if row < 0 or target < 0 or target >= self.items_table.rowCount():
            return
        # 整行数据(所有 cellWidget)互换
        self._swap_rows(row, target)
        self.items_table.setCurrentCell(target, 0)

    def _swap_rows(self, a: int, b: int) -> None:
        self._row_items[a], self._row_items[b] = self._row_items[b], self._row_items[a]
        wa = [self.items_table.cellWidget(a, c) for c in range(self.items_table.columnCount())]
        wb = [self.items_table.cellWidget(b, c) for c in range(self.items_table.columnCount())]
        for c in range(self.items_table.columnCount()):
            self.items_table.setCellWidget(a, c, wb[c])
            self.items_table.setCellWidget(b, c, wa[c])
        # 摘要单元格持有行号, 互换后修正
        for w in (self.items_table.cellWidget(a, 0), self.items_table.cellWidget(b, 0)):
            if isinstance(w, _ItemSummaryCell):
                w.row = self.items_table.row(w)

    # ------------------------------------------------------------------
    # 保存 / 校验
    # ------------------------------------------------------------------

    def _collect_board(self) -> dict:
        """从控件收集主题 dict(未校验; 条目结构以 _row_items 为源, 显示名/口径/日期取控件)"""
        items = []
        for row in range(self.items_table.rowCount()):
            base = self._row_items[row] if row < len(self._row_items) else {}
            name_edit = self.items_table.cellWidget(row, 1)
            trans = self.items_table.cellWidget(row, 2)
            start_edit = self.items_table.cellWidget(row, 3)
            end_edit = self.items_table.cellWidget(row, 4)
            kind = _item_type(base)
            if kind == "code":
                it = {"type": "code"}
                for f in ("table", "code_column", "code", "value_column"):
                    if base.get(f):
                        it[f] = base[f]
                if not (it.get("table") and it.get("code")):
                    continue  # 关键字段缺失(理论不该发生), 跳过
            else:
                ind = base.get("indicator", "")
                if not ind:
                    continue
                it = {"indicator": ind}  # 老条目不强制写 type, 最小 diff
            nm = name_edit.text().strip() if name_edit else ""
            if nm:
                it["name"] = nm
            if kind != "code":
                t = trans.currentText() if trans else ""
                if t:
                    it["transform"] = t
            for field, w in (("date_start", start_edit), ("date_end", end_edit)):
                v = (w.text().strip() if w else "")
                if v:
                    it[field] = v
            items.append(it)

        always = self.always_latest.isChecked()
        board = {
            "key": self._current_key or self.key_edit.text().strip(),
            "name": self.name_edit.text().strip(),
            "description": self.desc_edit.text().strip(),
            "date_start": None if always else self.date_start.date().toString("yyyy-MM-dd"),
            "date_end": None if always else self.date_end.date().toString("yyyy-MM-dd"),
            "items": items,
        }
        return board

    def _save_current(self) -> None:
        board = self._collect_board()
        all_boards = self.store.load().get("boards", [])
        # 新建且 key 仍为空(用户没填也没触发建议):自动兜底生成,保证不卡住
        if not board["key"]:
            existing = {b["key"] for b in all_boards}
            board["key"] = self._suggest_board_key(self.name_edit.text().strip(), existing)
            self.key_edit.setText(board["key"])
        errors = self.store.validate_board(
            board, all_boards, self.known_indicators, self.known_code_sources)
        if errors:
            QMessageBox.warning(self, "校验未通过",
                                "无法保存:\n- " + "\n- ".join(errors[:8]))
            return
        key = board["key"]
        ok = (self.store.update_board(key, board)
              if self.store.get_board(key) is not None
              else self.store.add_board(board))
        if ok:
            self._current_key = key
            self._load_boards()
            self.log_fn("INFO", f"已保存主题 {key}({len(board['items'])} 个指标)")
            QMessageBox.information(self, "保存成功",
                                    f"主题「{key}」已保存到 config/themes.yaml")
        else:
            QMessageBox.warning(self, "保存失败", "写回 themes.yaml 失败, 见日志")

    # ------------------------------------------------------------------
    # 快照 / 同步 / 导出
    # ------------------------------------------------------------------

    def _refresh_snapshot(self) -> None:
        key = self._current_key
        if not key:
            QMessageBox.information(self, "提示", "请先选择一个主题")
            return
        board = self.store.get_board(key)
        if not board:
            return
        try:
            df = self.service.snapshot(board)
        except Exception as exc:
            QMessageBox.warning(self, "快照失败", f"{exc}")
            return
        self.view_table.loadDataFrame(df)
        status = f"快照刷新: {len(df)} 个条目, {df['最新日期'].nunique() if not df.empty else 0} 个数据日期"
        self.log_fn("INFO", status)

    def _sync_board(self) -> None:
        if not self.sync_engine:
            return
        if not self._current_key:
            QMessageBox.information(self, "提示", "请先选择一个主题")
            return
        board = self.store.get_board(self._current_key)
        if not board:
            return
        targets = board_sync_targets(board, self.repo, self.registry)
        if not targets:
            missing = [it.get("indicator") for it in board.get("items", [])
                       if it.get("indicator")
                       and not (self.repo.get_indicator_map(it["indicator"])
                                if hasattr(self.repo, "get_indicator_map") else None)]
            if missing:
                QMessageBox.information(
                    self, "提示",
                    "以下指标未设获信源(从未同步过或已在指标管理中心被清除), "
                    "请在指标管理中心设获信源后重试:\n- " + "\n- ".join(missing[:8]))
            else:
                QMessageBox.information(self, "提示", "主题无可同步条目(为空或表不在目录中)")
            return
        self.sync_btn.setEnabled(False)
        self.save_btn.setEnabled(False)
        self.log_fn("INFO", f"开始同步主题 {board['key']}: {len(targets)} 个目标")

        self._thread = QThread(self)
        self._worker = BoardSyncWorker(self.sync_engine, targets)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._on_sync_done)
        self._worker.error.connect(lambda msg: self.log_fn("ERROR", f"同步失败: {msg}"))
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.start()

    def _on_sync_done(self) -> None:
        self.sync_btn.setEnabled(bool(self.sync_engine))
        self.save_btn.setEnabled(True)
        self.log_fn("INFO", "主题同步完成")
        # 同步后刷新快照
        self._refresh_snapshot()

    def _export_snapshot(self) -> None:
        df = self.view_table.pandas_model.getDataFrame()
        if df is None or df.empty:
            QMessageBox.information(self, "提示", "快照为空, 请先刷新")
            return
        from src.gui.dialogs.export_dialog import ExportDialog
        dlg = ExportDialog(df, source_name="主题快照", parent=self)
        dlg.exec()

    def _confirm_discard(self) -> bool:
        """切换主题前若有未保存改动, 提示(简单起见: 无改动判定, 直接放行)"""
        return True
