"""抓取规则管理器对话框 — 可视化查看/添加/编辑/删除/测试抓取规则"""

import json
from dataclasses import dataclass, field

from PySide6.QtCore import Qt, QObject, QThread, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QMessageBox,
    QFormLayout, QLineEdit, QComboBox, QCheckBox, QTextEdit,
    QGroupBox,
)

from src.utils.logger import logger


# ---------------------------------------------------------------------------
# 测试抓取 worker
# ---------------------------------------------------------------------------


class ScrapeTestWorker(QObject):
    """后台测试抓取工作器"""

    finished = Signal(str, int)   # rule_name, 行数（-1=失败）
    error = Signal(str, str)      # rule_name, 错误信息

    def __init__(self, engine, rule_name: str):
        super().__init__()
        self.engine = engine
        self.rule_name = rule_name

    def run(self) -> None:
        try:
            rule = self.engine.get_rule(self.rule_name)
            if not rule:
                self.error.emit(self.rule_name, "规则不存在")
                return
            df = self.engine.scrape(rule)
            self.finished.emit(self.rule_name, len(df))
        except Exception as exc:
            self.error.emit(self.rule_name, str(exc))


# ---------------------------------------------------------------------------
# 对话框
# ---------------------------------------------------------------------------


class ScrapeManagerDialog(QDialog):
    """抓取规则管理器"""

    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.rules: list[dict] = []
        self._thread: QThread | None = None
        self._worker: ScrapeTestWorker | None = None

        self.setWindowTitle("抓取规则管理")
        self.setMinimumSize(760, 520)
        self.resize(860, 580)

        self._setup_ui()
        self._reload_rules()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        title = QLabel("🕷 数据抓取规则管理")
        title.setStyleSheet("font-size: 15px; font-weight: bold; padding: 4px 0;")
        layout.addWidget(title)

        # ---- 规则列表 ----
        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(
            ["名称", "URL", "目标表", "定时", "启用", "状态"])
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(
            4, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(
            5, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        layout.addWidget(self.table, stretch=1)

        # ---- 操作按钮 ----
        btn_row = QHBoxLayout()
        self.add_btn = QPushButton("➕ 添加规则")
        self.add_btn.clicked.connect(self._on_add)
        self.edit_btn = QPushButton("✏️ 编辑")
        self.edit_btn.setEnabled(False)
        self.edit_btn.clicked.connect(self._on_edit)
        self.delete_btn = QPushButton("🗑 删除")
        self.delete_btn.setEnabled(False)
        self.delete_btn.clicked.connect(self._on_delete)
        self.test_btn = QPushButton("🧪 测试抓取")
        self.test_btn.setEnabled(False)
        self.test_btn.clicked.connect(self._on_test)

        self.toggle_btn = QPushButton("⏸ 禁用")
        self.toggle_btn.setEnabled(False)
        self.toggle_btn.clicked.connect(self._on_toggle)

        btn_row.addWidget(self.add_btn)
        btn_row.addWidget(self.edit_btn)
        btn_row.addWidget(self.delete_btn)
        btn_row.addWidget(self.test_btn)
        btn_row.addWidget(self.toggle_btn)
        btn_row.addStretch()

        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        # 状态提示
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #aaa;")
        layout.addWidget(self.status_label)

    # ------------------------------------------------------------------
    # 数据加载 / 列表刷新
    # ------------------------------------------------------------------

    def _reload_rules(self) -> None:
        """从 yaml 加载规则并刷新列表"""
        self.rules = self.engine.load_rules()
        self.table.setRowCount(len(self.rules))
        for row, rule in enumerate(self.rules):
            self._set_row(row, rule)
        self.status_label.setText(
            f"共 {len(self.rules)} 条规则  |  配置: config/scrapers.yaml")

    def _set_row(self, row: int, rule: dict) -> None:
        name = rule.get("name", "")
        self.table.setItem(row, 0, QTableWidgetItem(name))
        self.table.setItem(row, 1, QTableWidgetItem(rule.get("url", "")))
        self.table.setItem(row, 2, QTableWidgetItem(
            rule.get("table_name", "")))
        self.table.setItem(row, 3, QTableWidgetItem(rule.get("schedule", "")))
        enabled = rule.get("enabled") is not False
        self.table.setItem(row, 4, QTableWidgetItem("✅" if enabled else "⏸"))
        self.table.setItem(row, 5, QTableWidgetItem(""))

    def _selected_rule(self) -> dict | None:
        row = self.table.currentRow()
        if 0 <= row < len(self.rules):
            return self.rules[row]
        return None

    def _on_selection_changed(self) -> None:
        has = self._selected_rule() is not None
        for btn in (self.edit_btn, self.delete_btn, self.test_btn,
                    self.toggle_btn):
            btn.setEnabled(has)
        if has:
            rule = self._selected_rule()
            enabled = rule.get("enabled") is not False
            self.toggle_btn.setText("⏸ 禁用" if enabled else "▶ 启用")

    # ------------------------------------------------------------------
    # 操作
    # ------------------------------------------------------------------

    def _on_add(self) -> None:
        """添加规则"""
        dialog = _RuleEditDialog(None, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            rule = dialog.get_rule()
            if self._name_exists(rule["name"]):
                QMessageBox.warning(self, "提示", f"规则名「{rule['name']}」已存在")
                return
            self.rules.append(rule)
            self._save()

    def _on_edit(self) -> None:
        rule = self._selected_rule()
        if not rule:
            return
        dialog = _RuleEditDialog(dict(rule), self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            new_rule = dialog.get_rule()
            row = self.table.currentRow()
            self.rules[row] = new_rule
            self._save()

    def _on_delete(self) -> None:
        rule = self._selected_rule()
        if not rule:
            return
        ret = QMessageBox.question(
            self, "确认删除",
            f"删除规则「{rule.get('name')}」？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        row = self.table.currentRow()
        self.rules.pop(row)
        self._save()

    def _on_toggle(self) -> None:
        rule = self._selected_rule()
        if not rule:
            return
        rule["enabled"] = not (rule.get("enabled") is not False)
        row = self.table.currentRow()
        self._set_row(row, rule)
        self._save()
        self._on_selection_changed()

    def _on_test(self) -> None:
        """后台测试抓取当前规则"""
        rule = self._selected_rule()
        if not rule:
            return
        name = rule.get("name", "")
        row = self.table.currentRow()
        self.table.setItem(row, 5, QTableWidgetItem("测试中..."))
        self.test_btn.setEnabled(False)
        self.status_label.setText(f"测试抓取 [{name}] ...")

        self._thread = QThread(self)
        # ★ 防 GC（与导入对话框同款坑）
        self._worker = ScrapeTestWorker(self.engine, name)
        worker = self._worker
        worker.moveToThread(self._thread)
        worker.finished.connect(self._on_test_done)
        worker.error.connect(self._on_test_error)
        self._thread.started.connect(worker.run)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    def _on_test_done(self, name: str, rows: int) -> None:
        row = self._find_row_by_name(name)
        if row >= 0:
            self.table.setItem(row, 5, QTableWidgetItem(f"✅ {rows} 行"))
        self.status_label.setText(f"[{name}] 抓取成功，共 {rows} 行")
        self._cleanup_thread()
        self._on_selection_changed()

    def _on_test_error(self, name: str, err: str) -> None:
        row = self._find_row_by_name(name)
        if row >= 0:
            self.table.setItem(row, 5, QTableWidgetItem("❌ 失败"))
        self.status_label.setText(f"[{name}] 抓取失败: {err[:80]}")
        QMessageBox.warning(self, "抓取失败", f"[{name}] {err}")
        self._cleanup_thread()
        self._on_selection_changed()

    def _find_row_by_name(self, name: str) -> int:
        for i, rule in enumerate(self.rules):
            if rule.get("name") == name:
                return i
        return -1

    def _name_exists(self, name: str) -> bool:
        return any(r.get("name") == name for r in self.rules)

    def _save(self) -> None:
        if self.engine.save_rules(self.rules):
            self._reload_rules()

    def _cleanup_thread(self) -> None:
        if self._thread:
            self._thread.quit()
            self._thread = None
        self._worker = None

    def closeEvent(self, event) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(2000)
            self._thread = None
        self._worker = None
        super().closeEvent(event)


class _RuleEditDialog(QDialog):
    """规则编辑/添加表单"""

    def __init__(self, rule: dict | None, parent=None):
        super().__init__(parent)
        self.rule = rule or {}
        self.setWindowTitle("编辑规则" if rule else "添加规则")
        self.setMinimumSize(520, 480)

        form = QFormLayout(self)

        self.name_edit = QLineEdit(self.rule.get("name", ""))
        self.url_edit = QLineEdit(self.rule.get("url", ""))
        self.method_combo = QComboBox()
        self.method_combo.addItems(["GET", "POST"])
        self.method_combo.setCurrentText(self.rule.get("method", "GET"))
        self.rows_edit = QLineEdit(self.rule.get("rows", ""))
        self.rows_edit.setPlaceholderText("CSS 选择器，如 table tr")
        self.columns_edit = QTextEdit()
        self.columns_edit.setPlaceholderText(
            'JSON 数组，如:\n[{"name": "日期", "index": 0}, {"name": "数值", "index": 1}]')
        cols = self.rule.get("columns")
        if cols:
            self.columns_edit.setPlainText(json.dumps(cols, ensure_ascii=False, indent=1))
        self.table_edit = QLineEdit(self.rule.get("table_name", ""))
        self.date_col_edit = QLineEdit(self.rule.get("date_column", ""))
        self.date_col_edit.setPlaceholderText("日期列名（用于去重）")
        self.schedule_edit = QLineEdit(self.rule.get("schedule", ""))
        self.schedule_edit.setPlaceholderText("cron，如 0 8 * * 1-5（留空不定时）")
        self.enabled_check = QCheckBox("启用")
        self.enabled_check.setChecked(self.rule.get("enabled") is not False)

        form.addRow("名称 *", self.name_edit)
        form.addRow("URL *", self.url_edit)
        form.addRow("方法", self.method_combo)
        form.addRow("行选择器", self.rows_edit)
        form.addRow("列定义", self.columns_edit)
        form.addRow("目标表", self.table_edit)
        form.addRow("日期列", self.date_col_edit)
        form.addRow("定时(cron)", self.schedule_edit)
        form.addRow("", self.enabled_check)

        btns = QHBoxLayout()
        save_btn = QPushButton("保存")
        save_btn.clicked.connect(self._on_save)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btns.addStretch()
        btns.addWidget(cancel_btn)
        btns.addWidget(save_btn)
        form.addRow(btns)

    def _on_save(self) -> None:
        name = self.name_edit.text().strip()
        url = self.url_edit.text().strip()
        if not name or not url:
            QMessageBox.warning(self, "提示", "名称和 URL 不能为空")
            return
        try:
            columns = json.loads(self.columns_edit.toPlainText() or "[]")
        except json.JSONDecodeError:
            QMessageBox.warning(self, "提示", "列定义不是合法的 JSON")
            return
        if not isinstance(columns, list):
            QMessageBox.warning(self, "提示", "列定义必须是 JSON 数组")
            return
        self.accept()

    def get_rule(self) -> dict:
        """返回编辑后的规则"""
        try:
            columns = json.loads(self.columns_edit.toPlainText() or "[]")
        except json.JSONDecodeError:
            columns = []
        rule = {
            "name": self.name_edit.text().strip(),
            "url": self.url_edit.text().strip(),
            "method": self.method_combo.currentText(),
            "rows": self.rows_edit.text().strip(),
            "columns": columns,
            "table_name": self.table_edit.text().strip(),
            "date_column": self.date_col_edit.text().strip(),
            "schedule": self.schedule_edit.text().strip(),
            "enabled": self.enabled_check.isChecked(),
        }
        # 保留原有但未在表单中的字段（如 headers/params）
        for k, v in self.rule.items():
            if k not in rule or not rule[k]:
                rule.setdefault(k, v)
        return rule
