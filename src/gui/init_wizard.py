"""首次初始化向导 — 模块选择、历史范围、下载设置、进度跟踪

使用 QProcess 在子进程中执行初始化，崩溃不影响主程序。
"""

import json
import os
import sys
from typing import Any

from PySide6.QtCore import Qt, QProcess, QByteArray
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QGroupBox,
    QLabel,
    QComboBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QProgressBar,
    QMessageBox,
)

from src.utils.config import ConfigManager
from src.core.fetcher_registry import FetcherRegistry
from src.gui.log_widget import LogWidget


class InitWizard(QDialog):
    """首次初始化向导对话框"""

    def __init__(
        self,
        registry: FetcherRegistry,
        config: ConfigManager,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Bern_Financial_Data 首次初始化")
        self.setMinimumSize(600, 500)

        self.registry = registry
        self.config = config
        self._process: QProcess | None = None
        self._modules: list[str] = []

        self.setup_ui()

    def setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        # ----------------------------------------------------------------
        # 标题
        # ----------------------------------------------------------------
        title = QLabel("欢迎使用 Bern_Financial_Data")
        title_font = title.font()
        title_font.setPointSize(16)
        title_font.setBold(True)
        title.setFont(title_font)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("color: #2c3e50; margin-bottom: 10px;")
        layout.addWidget(title)

        # ----------------------------------------------------------------
        # Step 1: 选择模块
        # ----------------------------------------------------------------
        step1_group = QGroupBox("Step 1: 选择数据模块")
        step1_layout = QVBoxLayout(step1_group)

        self.module_tree = QTreeWidget()
        self.module_tree.setHeaderLabels(["数据模块"])
        self.module_tree.setAlternatingRowColors(True)
        self.module_tree.setAnimated(True)
        self._build_module_tree()
        step1_layout.addWidget(self.module_tree)

        layout.addWidget(step1_group)

        # ----------------------------------------------------------------
        # Step 2: 历史范围
        # ----------------------------------------------------------------
        step2_group = QGroupBox("Step 2: 历史数据范围")
        step2_layout = QHBoxLayout(step2_group)

        step2_layout.addWidget(QLabel("拉取历史:"))
        self.history_combo = QComboBox()
        self.history_combo.addItems([
            "近 1 年",
            "近 3 年 (推荐)",
            "近 5 年",
            "全部历史",
        ])
        self.history_combo.setCurrentIndex(1)
        step2_layout.addWidget(self.history_combo)
        step2_layout.addStretch()

        layout.addWidget(step2_group)

        # ----------------------------------------------------------------
        # Step 3: 下载设置
        # ----------------------------------------------------------------
        step3_group = QGroupBox("Step 3: 下载设置")
        step3_layout = QHBoxLayout(step3_group)

        step3_layout.addWidget(QLabel("请求间隔:"))
        self.delay_combo = QComboBox()
        self.delay_combo.addItems(["0.5s", "1s", "2s", "3s"])
        self.delay_combo.setCurrentIndex(1)
        step3_layout.addWidget(self.delay_combo)

        step3_layout.addSpacing(20)

        step3_layout.addWidget(QLabel("重试次数:"))
        self.retry_combo = QComboBox()
        self.retry_combo.addItems(["1", "2", "3", "5"])
        self.retry_combo.setCurrentIndex(2)
        step3_layout.addWidget(self.retry_combo)

        step3_layout.addStretch()

        layout.addWidget(step3_group)

        # ----------------------------------------------------------------
        # Progress area (initially hidden)
        # ----------------------------------------------------------------
        self.progress_group = QGroupBox("初始化进度")
        progress_layout = QVBoxLayout(self.progress_group)

        self.current_module_label = QLabel("准备中...")
        progress_layout.addWidget(self.current_module_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        progress_layout.addWidget(self.progress_bar)

        self.progress_log = LogWidget(self)
        self.progress_log.setMaximumHeight(120)
        progress_layout.addWidget(self.progress_log)

        self.progress_group.setVisible(False)
        layout.addWidget(self.progress_group)

        # ----------------------------------------------------------------
        # Buttons
        # ----------------------------------------------------------------
        btn_layout = QHBoxLayout()

        self.skip_btn = QPushButton("跳过")
        self.skip_btn.clicked.connect(self.reject)

        self.cancel_btn = QPushButton("停止")
        self.cancel_btn.setVisible(False)
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._stop_init)

        self.start_btn = QPushButton("开始初始化")
        self.start_btn.setStyleSheet("""
            QPushButton {
                background-color: #27ae60;
                color: white;
                font-weight: bold;
                padding: 8px 20px;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #2ecc71;
            }
            QPushButton:disabled {
                background-color: #95a5a6;
            }
        """)

        btn_layout.addStretch()
        btn_layout.addWidget(self.cancel_btn)
        btn_layout.addWidget(self.skip_btn)
        btn_layout.addWidget(self.start_btn)

        layout.addLayout(btn_layout)

        # ----------------------------------------------------------------
        # Connections
        # ----------------------------------------------------------------
        self.start_btn.clicked.connect(self._start_init)

    # ------------------------------------------------------------------
    # 模块树构建
    # ------------------------------------------------------------------

    def _build_module_tree(self) -> None:
        """从注册表构建可勾选的模块树"""
        self.module_tree.clear()
        categories = self.registry.get_categories()

        for cat in categories:
            top_item = QTreeWidgetItem(self.module_tree)
            top_item.setText(0, cat.get("name", ""))
            top_item.setFlags(
                top_item.flags()
                | Qt.ItemFlag.ItemIsAutoTristate
                | Qt.ItemFlag.ItemIsUserCheckable
            )

            children = cat.get("children", [])
            if children:
                self._build_module_children(top_item, children)

            if cat.get("name") == "全球宏观数据":
                top_item.setCheckState(0, Qt.CheckState.Checked)
            else:
                top_item.setCheckState(0, Qt.CheckState.Unchecked)

        self.module_tree.expandAll()

    def _build_module_children(
        self,
        parent_item: QTreeWidgetItem,
        children: list[dict],
    ) -> None:
        """递归构建子节点"""
        for child in children:
            item = QTreeWidgetItem(parent_item)
            item.setText(0, child.get("name", ""))

            source_key = child.get("source_key") or child.get("key", "")
            sub_children = child.get("children")

            if sub_children:
                item.setFlags(
                    item.flags()
                    | Qt.ItemFlag.ItemIsAutoTristate
                    | Qt.ItemFlag.ItemIsUserCheckable
                )
                item.setCheckState(0, Qt.CheckState.Unchecked)
                self._build_module_children(item, sub_children)
            else:
                is_leaf = bool(child.get("api_function"))
                if is_leaf and source_key:
                    item.setData(0, Qt.ItemDataRole.UserRole, source_key)
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setCheckState(0, Qt.CheckState.Unchecked)

    # ------------------------------------------------------------------
    # 公共查询方法
    # ------------------------------------------------------------------

    def getSelectedModules(self) -> list[str]:
        """返回所有已勾选叶节点的 source_key 列表"""
        keys: list[str] = []

        def _collect(item: QTreeWidgetItem) -> None:
            if item.childCount() == 0:
                if item.checkState(0) == Qt.CheckState.Checked:
                    key = item.data(0, Qt.ItemDataRole.UserRole)
                    if key:
                        keys.append(key)
            else:
                for i in range(item.childCount()):
                    _collect(item.child(i))

        for i in range(self.module_tree.topLevelItemCount()):
            _collect(self.module_tree.topLevelItem(i))

        return keys

    def getHistoryYears(self) -> int:
        mapping = {
            "近 1 年": 1,
            "近 3 年 (推荐)": 3,
            "近 5 年": 5,
            "全部历史": 999,
        }
        return mapping.get(self.history_combo.currentText(), 3)

    # ------------------------------------------------------------------
    # 初始化流程（QProcess 子进程）
    # ------------------------------------------------------------------

    def _start_init(self) -> None:
        """启动后台初始化进程"""
        modules = self.getSelectedModules()

        if not modules:
            QMessageBox.warning(self, "提示", "请至少选择一个数据模块")
            return

        self._modules = modules

        # 切换到进度模式
        self.start_btn.setEnabled(False)
        self.start_btn.setText("初始化中...")
        self.skip_btn.setVisible(False)
        self.cancel_btn.setVisible(True)
        self.cancel_btn.setEnabled(True)
        self.progress_group.setVisible(True)
        self.current_module_label.setText("正在启动后台进程...")

        history_years = self.getHistoryYears()
        delay = self.getRequestDelay()
        retry_count = self.getRetryCount()

        self.progress_log.write(
            "INFO",
            f"开始初始化，共 {len(modules)} 个模块，历史 {history_years} 年，"
            f"间隔 {delay}s，重试 {retry_count} 次",
        )

        # ★ 使用 QProcess 启动子进程
        self._process = QProcess(self)
        self._process.setProcessChannelMode(QProcess.ProcessChannelMode.ForwardedErrorChannel)

        # 连接信号
        self._process.readyReadStandardOutput.connect(self._on_process_stdout)
        self._process.finished.connect(self._on_process_finished)

        # 构建参数
        worker_script = os.path.join(os.path.dirname(__file__), "..", "core", "init_worker.py")
        worker_script = os.path.abspath(worker_script)
        args_json = json.dumps({"modules": modules, "history_years": history_years})
        python_exe = sys.executable

        self.progress_log.write("INFO", f"启动进程: {python_exe} {worker_script}")
        self._process.start(python_exe, [worker_script, args_json])

        if not self._process.waitForStarted(5000):
            self.progress_log.write("ERROR", f"子进程启动失败")
            self._reset_ui()

    def _on_process_stdout(self) -> None:
        """处理子进程的标准输出（逐行解析 JSON）"""
        data = self._process.readAllStandardOutput().data().decode("utf-8", errors="replace")

        for line in data.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                msg_type = msg.get("type", "")

                if msg_type == "log":
                    self.progress_log.write(msg.get("level", "INFO"), msg.get("message", ""))
                elif msg_type == "started":
                    sk = msg.get("source_key", "")
                    idx = msg.get("index", 0)
                    total = msg.get("total", 1)
                    self.current_module_label.setText(f"正在下载: {sk}")
                    if total > 0:
                        self.progress_bar.setValue(int((idx + 1) / total * 100))
                elif msg_type == "result":
                    sk = msg.get("source_key", "")
                    status = msg.get("status", "")
                    if status == "ok":
                        added = msg.get("added", 0)
                        self.progress_log.write("SUCCESS", f"模块完成: {sk} 新增 {added} 条")
                    elif status == "no_data":
                        self.progress_log.write("INFO", f"模块: {sk} 无新数据")
                    else:
                        err = msg.get("error", "未知错误")
                        self.current_module_label.setText(f"❌ {sk} 失败")
                        self.progress_log.write("ERROR", f"模块失败: {sk} - {err}")
                elif msg_type == "error":
                    self.progress_log.write("ERROR", msg.get("message", ""))
                    tb = msg.get("traceback", "")
                    if tb:
                        for tb_line in tb.split("\n"):
                            self.progress_log.write("ERROR", f"  {tb_line}")

            except json.JSONDecodeError:
                pass  # 非 JSON 输出忽略（如 akShare 的进度条）

    def _on_process_finished(self, exit_code: int, exit_status: QProcess.ExitStatus) -> None:
        """子进程结束回调"""
        if exit_status == QProcess.ExitStatus.CrashExit:
            self.progress_log.write("WARNING",
                f"子进程异常退出（exit_code={exit_code}），可能是某个数据源超时或崩溃")
            self.progress_log.write("INFO", "已下载的数据已保存到数据库，您可以关闭或继续使用")
        else:
            self.progress_log.write("SUCCESS", "所有模块处理完成!")

        self.progress_bar.setValue(100)
        self._reset_ui()
        self.accept()

    def _stop_init(self) -> None:
        """停止子进程"""
        self.progress_log.write("WARNING", "正在停止后台进程...")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setText("正在停止...")

        if self._process and self._process.state() != QProcess.ProcessState.NotRunning:
            self._process.kill()
            self._process.waitForFinished(3000)

        self.progress_log.write("WARNING", "初始化已中断")
        self._reset_ui()

    def _reset_ui(self) -> None:
        """恢复界面到可操作状态"""
        self.start_btn.setText("重新开始")
        self.start_btn.setEnabled(True)
        self.cancel_btn.setVisible(False)
        self.skip_btn.setVisible(True)
        self.skip_btn.setEnabled(True)

    def getRequestDelay(self) -> float:
        text = self.delay_combo.currentText()
        return float(text.replace("s", ""))

    def getRetryCount(self) -> int:
        return int(self.retry_combo.currentText())

    def closeEvent(self, event) -> None:
        """关闭时清理子进程"""
        if self._process and self._process.state() != QProcess.ProcessState.NotRunning:
            self._process.kill()
            self._process.waitForFinished(3000)
        super().closeEvent(event)
