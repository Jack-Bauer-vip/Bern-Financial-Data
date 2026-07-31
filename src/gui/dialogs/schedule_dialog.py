"""定时任务管理对话框 — 查看调度状态、自定义 cron 表达式"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGroupBox,
    QLabel, QLineEdit, QPushButton, QTableWidget,
    QTableWidgetItem, QHeaderView, QMessageBox, QComboBox,
    QCheckBox, QWidget, QGridLayout,
)

from src.utils.logger import logger


# 常用 cron 预设
CRON_PRESETS = {
    "自定义": "",
    "每天 0:00": "0 0 * * *",
    "每天 6:00": "0 6 * * *",
    "每天 8:00": "0 8 * * *",
    "每天 10:00": "0 10 * * *",
    "每天 14:00": "0 14 * * *",
    "每天 15:00": "0 15 * * *",
    "每天 22:00": "0 22 * * *",
    "工作日 8:00": "0 8 * * 1-5",
    "工作日 10:00": "0 10 * * 1-5",
    "工作日 15:00": "0 15 * * 1-5",
    "工作日 18:00": "0 18 * * 1-5",
    "工作日 22:00": "0 22 * * 1-5",
    "每小时": "0 * * * *",
    "每 6 小时": "0 */6 * * *",
    "每 12 小时": "0 */12 * * *",
}

# 中文星期简写
WEEKDAY_MAP = {
    "0": "日", "1": "一", "2": "二", "3": "三", "4": "四",
    "5": "五", "6": "六", "*": "每天",
}


def format_cron_display(cron: str) -> str:
    """将 cron 表达式转成人类可读"""
    if not cron or cron.strip() == "":
        return "未设置"
    parts = cron.strip().split()
    if len(parts) != 5:
        return cron
    minute, hour, dom, month, dow = parts
    dow_name = WEEKDAY_MAP.get(dow, f"周{dow}")
    if dom != "*":
        return f"每月{dom}日 {hour}:{minute.zfill(2)}"
    if dow == "*":
        return f"每天 {hour}:{minute.zfill(2)}"
    return f"每周{dow_name} {hour}:{minute.zfill(2)}"


class ScheduleDialog(QDialog):
    """定时任务管理对话框"""

    def __init__(self, scheduler, categories_info, parent=None):
        super().__init__(parent)
        self.scheduler = scheduler
        # 深拷贝，避免后续修改影响原始数据
        self.categories_info = [dict(c) for c in categories_info]
        self.setWindowTitle("⏰ 定时任务管理")
        self.setMinimumSize(650, 500)
        self.resize(700, 550)
        self._setup_ui()
        self._load_data()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        # ---- 状态概览 ----
        status_layout = QHBoxLayout()
        self.running_label = QLabel()
        self.running_label.setStyleSheet("font-weight: bold; font-size: 14px;")
        status_layout.addWidget(self.running_label)
        status_layout.addStretch()
        layout.addLayout(status_layout)

        # ---- 任务列表表格 ----
        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels([
            "板块", "状态", "Cron 表达式", "执行时间", "操作"
        ])
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(
            4, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        layout.addWidget(self.table)

        # ---- 编辑区 ----
        edit_group = QGroupBox("编辑定时设置")
        edit_grid = QGridLayout(edit_group)

        edit_grid.addWidget(QLabel("选择板块:"), 0, 0)
        self.category_combo = QComboBox()
        edit_grid.addWidget(self.category_combo, 0, 1)

        edit_grid.addWidget(QLabel("启用定时:"), 1, 0)
        self.enable_check = QCheckBox("已启用")
        self.enable_check.setChecked(True)
        edit_grid.addWidget(self.enable_check, 1, 1)

        edit_grid.addWidget(QLabel("Cron 预设:"), 2, 0)
        self.preset_combo = QComboBox()
        for name in CRON_PRESETS:
            self.preset_combo.addItem(name)
        self.preset_combo.currentTextChanged.connect(self._on_preset_changed)
        edit_grid.addWidget(self.preset_combo, 2, 1)

        edit_grid.addWidget(QLabel("自定义 Cron:"), 3, 0)
        self.cron_input = QLineEdit()
        self.cron_input.setPlaceholderText(
            "分 时 日 月 周，如: 0 22 * * 1-5")
        edit_grid.addWidget(self.cron_input, 3, 1)

        edit_grid.addWidget(QLabel("说明:"), 4, 0)
        self.cron_desc = QLabel("选择预设或直接输入 cron 表达式")
        self.cron_desc.setStyleSheet("color: #666;")
        edit_grid.addWidget(self.cron_desc, 4, 1)

        btn_layout = QHBoxLayout()
        apply_btn = QPushButton("应用到板块")
        apply_btn.clicked.connect(self._apply_settings)
        reset_btn = QPushButton("恢复默认")
        reset_btn.clicked.connect(self._reset_default)
        btn_layout.addWidget(apply_btn)
        btn_layout.addStretch()
        btn_layout.addWidget(reset_btn)
        edit_grid.addLayout(btn_layout, 5, 0, 1, 2)

        layout.addWidget(edit_group)

        # ---- 底部按钮 ----
        bottom_layout = QHBoxLayout()
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        bottom_layout.addStretch()
        bottom_layout.addWidget(close_btn)
        layout.addLayout(bottom_layout)

    def _load_data(self) -> None:
        """加载数据到表格（从父窗口读取最新状态）"""
        # 从父 MainWindow 刷新分类状态
        parent = self.parent()
        while parent:
            if hasattr(parent, 'tree_widget'):
                fresh = parent.tree_widget.get_category_schedule_info()
                for f in fresh:
                    for c in self.categories_info:
                        if c["name"] == f["name"]:
                            c["schedule_enabled"] = f["schedule_enabled"]
                            break
                break
            parent = parent.parent()

        jobs = self.scheduler.get_jobs()

        # 状态
        is_running = self.scheduler.is_running()
        self.running_label.setText(
            f"{'✅' if is_running else '⛔'} "
            f"调度器状态: {'运行中' if is_running else '已停止'}  "
            f"| 注册任务数: {len(jobs)}"
        )

        # 填充表格
        self.table.setRowCount(len(self.categories_info))
        for row, cat in enumerate(self.categories_info):
            name = cat["name"]
            enabled = cat["schedule_enabled"]

            # 板块名
            self.table.setItem(row, 0, QTableWidgetItem(name))

            # 状态
            status_item = QTableWidgetItem("🟢 运行" if enabled else "🔴 暂停")
            status_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, 1, status_item)

            # 从 job 中查找 cron
            cron = self._find_cron_for_category(name)
            self.table.setItem(row, 2, QTableWidgetItem(cron or "未设置"))

            # 显示时间
            next_run = self._find_next_run_for_category(name)
            display = format_cron_display(cron) if cron else "未设置"
            self.table.setItem(row, 3, QTableWidgetItem(
                next_run if next_run else display))

            # 操作按钮
            btn_widget = QWidget()
            btn_layout = QHBoxLayout(btn_widget)
            btn_layout.setContentsMargins(4, 0, 4, 0)
            toggle_btn = QPushButton(
                "暂停" if enabled else "启用")
            toggle_btn.setFixedWidth(50)
            toggle_btn.clicked.connect(
                lambda checked, n=name, e=enabled:
                self._toggle_category(n, not e))
            btn_layout.addWidget(toggle_btn)
            self.table.setCellWidget(row, 4, btn_widget)

        # 填充板块下拉
        self.category_combo.clear()
        for cat in self.categories_info:
            self.category_combo.addItem(cat["name"])

        self.table.resizeColumnsToContents()

    def _find_cron_for_category(self, category_name: str) -> str:
        """从已注册的任务中查找分类对应的 cron 表达式"""
        jobs = self.scheduler.get_jobs()
        names = [j["name"] for j in jobs
                 if category_name in j["name"]]
        if names:
            return "标准 " + str(len(names)) + " 个任务"
        return ""

    def _find_next_run_for_category(self, category_name: str) -> str:
        """查找分类的下次执行时间"""
        jobs = self.scheduler.get_jobs()
        for j in jobs:
            if category_name in j["name"] and j.get("next_run"):
                return j["next_run"][:19].replace("T", " ")
        return "待定"

    def _toggle_category(self, name: str, target_enabled: bool) -> None:
        """切换分类定时状态"""
        try:
            if target_enabled:
                self.scheduler.enable_category(name)
            else:
                self.scheduler.disable_category(name)
            # 更新本地缓存
            for cat in self.categories_info:
                if cat["name"] == name:
                    cat["schedule_enabled"] = target_enabled
                    break
            # 通知父窗口更新树图标
            parent = self.parent()
            while parent:
                if hasattr(parent, 'tree_widget'):
                    parent.tree_widget.refresh_schedule_icon(name, target_enabled)
                    break
                parent = parent.parent()
            self._load_data()
        except Exception as exc:
            from src.utils.logger import logger
            logger.error(f"切换定时失败 [{name}]: {exc}")

    def _on_preset_changed(self, preset_name: str) -> None:
        """预设选择变化"""
        cron = CRON_PRESETS.get(preset_name, "")
        self.cron_input.setText(cron)
        if cron:
            self.cron_desc.setText(format_cron_display(cron))
        else:
            self.cron_desc.setText("手动输入 cron 表达式")

    def _apply_settings(self) -> None:
        """应用到板块"""
        name = self.category_combo.currentText()
        enabled = self.enable_check.isChecked()
        cron = self.cron_input.text().strip()

        if not name:
            return

        if cron:
            # 验证 cron 格式
            parts = cron.split()
            if len(parts) != 5:
                QMessageBox.warning(
                    self, "格式错误",
                    "Cron 表达式需要 5 个字段:\n"
                    "分 时 日 月 周\n"
                    "示例: 0 22 * * 1-5  (工作日22:00)"
                )
                return

        # 先清空原有的，再重新注册
        self.scheduler.remove_category_jobs(name)

        if enabled and cron:
            # 从 data_catalog 找到该分类下的 source_key
            from src.utils.config import ConfigManager
            config = ConfigManager()
            for cat in config.catalog.get("categories", []):
                if cat.get("name") == name:
                    self._register_children(cat, cron)
                    break

        # 更新树图标
        parent = self.parent()
        while parent:
            if hasattr(parent, 'tree_widget'):
                parent.tree_widget.refresh_schedule_icon(name, enabled)
                parent.log_widget.write(
                    "INFO",
                    f"定时设置已更新 [{name}]: "
                    f"{'启用' if enabled else '禁用'} "
                    f"{f'cron={cron}' if cron else ''}"
                )
                break
            parent = parent.parent()

        self._load_data()
        QMessageBox.information(self, "完成", f"已更新 [{name}] 的定时设置")

    def _register_children(self, category: dict, cron: str) -> None:
        """重新注册子节点"""
        from src.core.scheduler import DataScheduler
        children = category.get("children", [])
        for child in children:
            sub = child.get("children")
            if sub:
                self._register_children(child, cron)
            elif child.get("api_function"):
                sk = child.get("source_key", "")
                if sk:
                    self.scheduler.add_job(
                        sk, cron, child.get("name", ""),
                        self._find_callback()
                    )

    def _find_callback(self):
        """找到 MainWindow 的定时回调"""
        parent = self.parent()
        while parent:
            if hasattr(parent, '_on_schedule_trigger'):
                return parent._on_schedule_trigger
            parent = parent.parent()
        return lambda sk, n: None

    def _reset_default(self) -> None:
        """恢复默认 cron 配置"""
        name = self.category_combo.currentText()
        if not name:
            return
        # 重新从 YAML 注册
        from src.utils.config import ConfigManager
        config = ConfigManager()
        for cat in config.catalog.get("categories", []):
            if cat.get("name") == name:
                self.scheduler.remove_category_jobs(name)
                self.scheduler._register_category(cat, self._find_callback())
                break

        parent = self.parent()
        while parent:
            if hasattr(parent, 'tree_widget'):
                parent.log_widget.write(
                    "INFO", f"已恢复 [{name}] 的默认定时设置")
                break
            parent = parent.parent()

        self._load_data()
        QMessageBox.information(self, "完成", f"已恢复 [{name}] 的默认定时设置")
