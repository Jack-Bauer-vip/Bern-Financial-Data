"""动态查询参数面板 — 根据数据源的 params_template 自动渲染控件 + 常驻日期范围"""

from datetime import date, timedelta

from PySide6.QtCore import Qt, Signal, QDate
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QGroupBox, QLabel, QLineEdit, QDateEdit,
    QComboBox, QPushButton, QMenu, QCheckBox,
)


class ParamPanel(QWidget):
    """动态参数面板，根据数据源配置自动生成对应控件"""

    queryClicked = Signal()
    syncClicked = Signal()
    initClicked = Signal()
    testClicked = Signal()

    # 控件类型映射
    CONTROL_TYPE_MAP = {
        "text": "_build_text_input",
        "date": "_build_date_input",
        "select": "_build_combo",
        "checkbox": "_build_checkbox",
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        # 动态生成的控件引用 {param_name: widget}
        self._dynamic_widgets: dict[str, QWidget] = {}
        self._dynamic_labels: dict[str, QLabel] = {}
        self._current_template: dict = {}
        self.setup_ui()

    def setup_ui(self) -> None:
        """构建静态框架 + 动态控件容器 + 常驻日期范围"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # ----------------------------------------------------------------
        # 参数区
        # ----------------------------------------------------------------
        self.param_group = QGroupBox("查询参数")
        self.form_layout = QFormLayout(self.param_group)
        self.form_layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self.param_group)

        # 无参数提示（默认显示，被动态控件覆盖）
        self._no_param_label = None

        # ★ 常驻日期范围 — 所有数据源都可见
        self._date_group = QGroupBox("日期范围")
        date_layout = QHBoxLayout(self._date_group)

        self.date_start = QDateEdit()
        self.date_start.setCalendarPopup(True)
        self.date_start.setDisplayFormat("yyyy-MM-dd")
        self.date_start.setDate(QDate.currentDate().addYears(-1))  # ★ 默认 1 年

        self.date_end = QDateEdit()
        self.date_end.setCalendarPopup(True)
        self.date_end.setDisplayFormat("yyyy-MM-dd")
        self.date_end.setDate(QDate.currentDate())

        date_layout.addWidget(QLabel("从"))
        date_layout.addWidget(self.date_start)
        date_layout.addWidget(QLabel("到"))
        date_layout.addWidget(self.date_end)

        layout.addWidget(self._date_group)

        # ----------------------------------------------------------------
        # 按钮区
        # ----------------------------------------------------------------
        btn_layout = QHBoxLayout()

        self.query_btn = QPushButton("查询")
        self.query_btn.setStyleSheet(
            "QPushButton { background-color: #1976D2; color: white; "
            "padding: 6px 16px; border-radius: 4px; font-weight: bold; }"
            "QPushButton:hover { background-color: #1565C0; }"
        )
        self.query_btn.clicked.connect(self.queryClicked.emit)

        self.sync_btn = QPushButton("增量同步")
        self.sync_btn.clicked.connect(self.syncClicked.emit)

        self.init_btn = QPushButton("初始化向导")
        self.init_btn.clicked.connect(self.initClicked.emit)

        self.test_btn = QPushButton("测试接口")
        self.test_btn.setStyleSheet(
            "QPushButton { background-color: #E65100; color: white; "
            "padding: 4px 10px; border-radius: 4px; font-size: 11px; }"
            "QPushButton:hover { background-color: #BF360C; }"
        )
        self.test_btn.clicked.connect(self.testClicked.emit)

        self.export_btn = QPushButton("导出 ▼")
        export_menu = QMenu(self)
        self.export_csv_action = export_menu.addAction("CSV")
        self.export_excel_action = export_menu.addAction("Excel (.xlsx)")
        self.export_pdf_action = export_menu.addAction("PDF")
        self.export_btn.setMenu(export_menu)

        btn_layout.addWidget(self.query_btn)
        btn_layout.addWidget(self.sync_btn)
        btn_layout.addWidget(self.init_btn)
        btn_layout.addWidget(self.test_btn)
        btn_layout.addStretch()
        btn_layout.addWidget(self.export_btn)

        layout.addLayout(btn_layout)

    # ------------------------------------------------------------------
    # 动态控件构建
    # ------------------------------------------------------------------

    def load_template(self, params_template: dict) -> None:
        """根据 params_template 动态渲染参数控件

        Args:
            params_template: data_catalog.yaml 中的参数模板
                格式: { "field_name": {"type": "text", "label": "名称", "default": "..."}, ... }
        """
        # 清空旧控件（保留日期范围）
        self._clear_dynamic_widgets()
        self._current_template = params_template or {}

        if not self._current_template:
            return

        for field_name, field_cfg in self._current_template.items():
            control_type = field_cfg.get("type", "text")
            builder = self.CONTROL_TYPE_MAP.get(control_type)
            if builder:
                getattr(self, builder)(field_name, field_cfg)

    def _build_text_input(self, field_name: str, field_cfg: dict) -> None:
        label = field_cfg.get("label", field_name)
        default = field_cfg.get("default", "")
        placeholder = field_cfg.get("placeholder", f"请输入{label}")
        widget = QLineEdit()
        widget.setPlaceholderText(placeholder)
        if default:
            widget.setText(str(default))
        self._dynamic_widgets[field_name] = widget
        self._dynamic_labels[field_name] = QLabel(f"{label}:")
        self.form_layout.addRow(self._dynamic_labels[field_name], widget)

    def _build_date_input(self, field_name: str, field_cfg: dict) -> None:
        """动态日期输入（只用于非日期范围的 date 字段）"""
        label = field_cfg.get("label", field_name)
        default_years = int(field_cfg.get("default_years", 0))

        widget = QDateEdit()
        widget.setCalendarPopup(True)
        widget.setDisplayFormat("yyyy-MM-dd")

        if default_years < 0:
            widget.setDate(QDate.currentDate().addYears(default_years))
        elif field_cfg.get("default") == "today":
            widget.setDate(QDate.currentDate())
        else:
            widget.setDate(QDate.currentDate())

        self._dynamic_widgets[field_name] = widget
        self._dynamic_labels[field_name] = QLabel(f"{label}:")
        self.form_layout.addRow(self._dynamic_labels[field_name], widget)

    def _build_combo(self, field_name: str, field_cfg: dict) -> None:
        label = field_cfg.get("label", field_name)
        options = field_cfg.get("options", [])
        default = field_cfg.get("default", "")
        widget = QComboBox()
        for opt in options:
            widget.addItem(opt)
        if default:
            idx = widget.findText(str(default))
            if idx >= 0:
                widget.setCurrentIndex(idx)
        self._dynamic_widgets[field_name] = widget
        self._dynamic_labels[field_name] = QLabel(f"{label}:")
        self.form_layout.addRow(self._dynamic_labels[field_name], widget)

    def _build_checkbox(self, field_name: str, field_cfg: dict) -> None:
        label = field_cfg.get("label", field_name)
        default = field_cfg.get("default", False)
        widget = QCheckBox(label)
        widget.setChecked(bool(default))
        self._dynamic_widgets[field_name] = widget
        self.form_layout.addRow("", widget)

    def _clear_dynamic_widgets(self) -> None:
        while self.form_layout.rowCount() > 0:
            self.form_layout.removeRow(0)
        self._dynamic_widgets.clear()
        self._dynamic_labels.clear()
        self._current_template = {}

    # ------------------------------------------------------------------
    # 公共方法
    # ------------------------------------------------------------------

    def clearParams(self) -> None:
        """重置所有动态参数为模板默认值（日期范围不变）"""
        if not self._current_template:
            return
        for field_name, widget in self._dynamic_widgets.items():
            field_cfg = self._current_template.get(field_name, {})
            default = field_cfg.get("default", "")
            if isinstance(widget, QLineEdit):
                widget.setText(str(default) if default else "")
            elif isinstance(widget, QDateEdit):
                if default == "today":
                    widget.setDate(QDate.currentDate())
                else:
                    dy = field_cfg.get("default_years", 0)
                    widget.setDate(QDate.currentDate().addYears(dy))
            elif isinstance(widget, QComboBox):
                if default:
                    idx = widget.findText(str(default))
                    if idx >= 0:
                        widget.setCurrentIndex(idx)
                    else:
                        widget.setCurrentIndex(0)
            elif isinstance(widget, QCheckBox):
                widget.setChecked(bool(default))

    def getParams(self) -> dict:
        """获取所有参数值（含常驻日期范围）"""
        params = {}

        # ★ 常驻日期范围始终包含
        params["start_date"] = self.date_start.date().toString("yyyyMMdd")
        params["end_date"] = self.date_end.date().toString("yyyyMMdd")

        # 动态参数
        for field_name, widget in self._dynamic_widgets.items():
            if isinstance(widget, QLineEdit):
                params[field_name] = widget.text().strip()
            elif isinstance(widget, QDateEdit):
                params[field_name] = widget.date().toString("yyyyMMdd")
            elif isinstance(widget, QComboBox):
                params[field_name] = widget.currentText()
            elif isinstance(widget, QCheckBox):
                params[field_name] = widget.isChecked()
        return params

    def setParams(self, params: dict) -> None:
        """设置参数值（用于恢复记忆的参数）"""
        if not params:
            return
        for field_name, value in params.items():
            # 日期范围也支持恢复记忆
            if field_name == "start_date" and value:
                qd = QDate.fromString(str(value), "yyyyMMdd")
                if qd.isValid():
                    self.date_start.setDate(qd)
                    continue
            if field_name == "end_date" and value:
                qd = QDate.fromString(str(value), "yyyyMMdd")
                if qd.isValid():
                    self.date_end.setDate(qd)
                    continue

            widget = self._dynamic_widgets.get(field_name)
            if widget is None:
                continue
            if isinstance(widget, QLineEdit):
                widget.setText(str(value))
            elif isinstance(widget, QDateEdit) and value:
                qd = QDate.fromString(str(value)[:10], "yyyy-MM-dd")
                if not qd.isValid():
                    qd = QDate.fromString(str(value), "yyyyMMdd")
                if qd.isValid():
                    widget.setDate(qd)
            elif isinstance(widget, QComboBox) and value:
                idx = widget.findText(str(value))
                if idx >= 0:
                    widget.setCurrentIndex(idx)
            elif isinstance(widget, QCheckBox):
                widget.setChecked(bool(value))

    def has_params(self) -> bool:
        """是否有动态参数"""
        return len(self._dynamic_widgets) > 0

    def get_template(self) -> dict:
        return self._current_template
