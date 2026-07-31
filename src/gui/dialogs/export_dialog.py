"""导出对话框 — 选择格式、字段、日期范围、导出选项"""

from datetime import datetime

import pandas as pd
from PySide6.QtCore import Qt, QDate
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGroupBox,
    QLabel, QPushButton, QCheckBox, QComboBox,
    QDateEdit, QLineEdit, QFileDialog, QMessageBox,
    QScrollArea, QWidget, QGridLayout, QProgressBar,
)


class ExportDialog(QDialog):
    """数据导出对话框"""

    FORMATS = {
        "CSV": {"cls": "CsvExporter", "ext": ".csv", "desc": "CSV 文件 (*.csv)"},
        "Excel": {"cls": "ExcelExporter", "ext": ".xlsx", "desc": "Excel 文件 (*.xlsx)"},
        "PDF": {"cls": "PdfExporter", "ext": ".pdf", "desc": "PDF 文件 (*.pdf)"},
    }

    def __init__(self, df: pd.DataFrame, source_name: str = "", parent=None):
        super().__init__(parent)
        self.df = df
        self.source_name = source_name or "data"
        self.setWindowTitle(f"导出数据 — {source_name}")
        self.setMinimumSize(500, 500)
        self.resize(550, 600)

        # 所有列的勾选状态
        self._column_checks: dict[str, bool] = {
            col: True for col in df.columns
        }

        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        # ---- ① 导出格式 ----
        fmt_group = QGroupBox("导出格式")
        fmt_layout = QHBoxLayout(fmt_group)
        self.format_combo = QComboBox()
        self.format_combo.addItems(["CSV", "Excel", "PDF"])
        self.format_combo.currentTextChanged.connect(self._on_format_changed)
        fmt_layout.addWidget(self.format_combo)
        fmt_layout.addStretch()
        layout.addWidget(fmt_group)

        # ---- ② 字段选择 ----
        field_group = QGroupBox("选择导出字段")
        field_layout = QVBoxLayout(field_group)

        select_all_row = QHBoxLayout()
        self.select_all_btn = QPushButton("全选")
        self.select_all_btn.clicked.connect(self._select_all)
        self.deselect_all_btn = QPushButton("取消全选")
        self.deselect_all_btn.clicked.connect(self._deselect_all)
        select_all_row.addWidget(self.select_all_btn)
        select_all_row.addWidget(self.deselect_all_btn)
        select_all_row.addStretch()
        field_layout.addLayout(select_all_row)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_widget = QWidget()
        self.column_grid = QGridLayout(scroll_widget)
        self.column_grid.setSpacing(2)

        self._checkboxes: dict[str, QCheckBox] = {}
        for i, col in enumerate(self.df.columns):
            cb = QCheckBox(col)
            cb.setChecked(True)
            cb.stateChanged.connect(self._on_check_changed)
            self.column_grid.addWidget(cb, i // 3, i % 3)
            self._checkboxes[col] = cb

        scroll.setWidget(scroll_widget)
        field_layout.addWidget(scroll)
        layout.addWidget(field_group)

        # ---- ③ CSV 选项 ----
        self.csv_options = QGroupBox("CSV 选项")
        csv_layout = QGridLayout(self.csv_options)
        csv_layout.addWidget(QLabel("编码:"), 0, 0)
        self.encoding_combo = QComboBox()
        self.encoding_combo.addItems(["utf-8-sig (推荐)", "utf-8", "gbk"])
        csv_layout.addWidget(self.encoding_combo, 0, 1)
        csv_layout.addWidget(QLabel("分隔符:"), 1, 0)
        self.separator_combo = QComboBox()
        self.separator_combo.addItems([", (逗号)", "; (分号)", "\t (制表符)"])
        csv_layout.addWidget(self.separator_combo, 1, 1)
        csv_layout.addWidget(QLabel("包含行号:"), 2, 0)
        self.index_check = QCheckBox()
        csv_layout.addWidget(self.index_check, 2, 1)
        layout.addWidget(self.csv_options)

        # ---- ④ Excel/PDF 选项 ----
        self.doc_options = QGroupBox("文档选项")
        doc_layout = QGridLayout(self.doc_options)
        doc_layout.addWidget(QLabel("工作表/标题:"), 0, 0)
        self.title_input = QLineEdit(self.source_name)
        doc_layout.addWidget(self.title_input, 0, 1)
        doc_layout.addWidget(QLabel("冻结首行:"), 1, 0)
        self.freeze_check = QCheckBox()
        self.freeze_check.setChecked(True)
        doc_layout.addWidget(self.freeze_check, 1, 1)
        layout.addWidget(self.doc_options)

        # ---- ⑤ 信息统计 ----
        info_label = QLabel(
            f"数据预览: {len(self.df)} 行 × {len(self.df.columns)} 列  |  "
            f"已选 {self._count_checked()} 列"
        )
        info_label.setStyleSheet("color: #666;")
        layout.addWidget(info_label)

        # ---- 进度条 ----
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        # ---- 按钮 ----
        btn_layout = QHBoxLayout()
        self.export_btn = QPushButton("导出")
        self.export_btn.setStyleSheet(
            "QPushButton { background-color: #1976D2; color: white; "
            "padding: 8px 24px; border-radius: 4px; font-weight: bold; }"
            "QPushButton:hover { background-color: #1565C0; }"
        )
        self.export_btn.clicked.connect(self._do_export)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)

        btn_layout.addStretch()
        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(self.export_btn)
        layout.addLayout(btn_layout)

        self._on_format_changed(self.format_combo.currentText())

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------

    def _on_format_changed(self, fmt: str) -> None:
        """格式切换时显示对应选项"""
        self.csv_options.setVisible(fmt == "CSV")
        self.doc_options.setVisible(fmt in ("Excel", "PDF"))

    def _on_check_changed(self) -> None:
        """勾选变化时更新信息"""
        self._update_info()

    def _select_all(self) -> None:
        for cb in self._checkboxes.values():
            cb.setChecked(True)

    def _deselect_all(self) -> None:
        for cb in self._checkboxes.values():
            cb.setChecked(False)

    def _count_checked(self) -> int:
        return sum(1 for cb in self._checkboxes.values() if cb.isChecked())

    def _update_info(self) -> None:
        """更新状态信息"""
        parent = self.layout().itemAt(5)
        if parent and parent.widget():
            label = parent.widget()
            checked = self._count_checked()
            label.setText(
                f"数据预览: {len(self.df)} 行 × {len(self.df.columns)} 列  |  "
                f"已选 {checked} 列"
            )

    # ------------------------------------------------------------------
    # 导出执行
    # ------------------------------------------------------------------

    def _do_export(self) -> None:
        """执行导出"""
        fmt = self.format_combo.currentText()
        selected_cols = [
            col for col in self.df.columns
            if self._checkboxes[col].isChecked()
        ]

        if not selected_cols:
            QMessageBox.warning(self, "提示", "请至少选择一列导出")
            return

        # 过滤列
        export_df = self.df[selected_cols].copy()

        # 组装选项
        options = {}
        if fmt == "CSV":
            enc = self.encoding_combo.currentText().split(" ")[0]
            sep_raw = self.separator_combo.currentText().split(" ")[0]
            options = {
                "encoding": enc,
                "separator": sep_raw,
                "include_index": self.index_check.isChecked(),
            }
        elif fmt == "Excel":
            options = {
                "sheet_name": self.title_input.text() or "Sheet1",
                "freeze_panes": self.freeze_check.isChecked(),
            }
        elif fmt == "PDF":
            options = {
                "title": self.title_input.text() or "数据导出报告",
            }

        # 获取保存路径
        fmt_info = self.FORMATS[fmt]
        default_name = f"{self.source_name}_{datetime.now():%Y%m%d_%H%M%S}{fmt_info['ext']}"

        path, _ = QFileDialog.getSaveFileName(
            self, f"导出 {fmt}", default_name, fmt_info["desc"]
        )

        if not path:
            return

        # 执行导出（非阻塞）
        self.export_btn.setEnabled(False)
        self.export_btn.setText("导出中...")
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)  # 不确定进度

        try:
            if fmt == "CSV":
                from src.export.csv_exporter import CsvExporter
                exporter = CsvExporter(export_df, options)
            elif fmt == "Excel":
                from src.export.excel_exporter import ExcelExporter
                exporter = ExcelExporter(export_df, options)
            elif fmt == "PDF":
                from src.export.pdf_exporter import PdfExporter
                exporter = PdfExporter(export_df, options)

            result_path = exporter.export(path)

            self.progress.setRange(0, 100)
            self.progress.setValue(100)

            QMessageBox.information(
                self, "导出成功",
                f"数据已成功导出到:\n{result_path}"
            )
            self.accept()

        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))
        finally:
            self.export_btn.setEnabled(True)
            self.export_btn.setText("导出")
            self.progress.setVisible(False)
