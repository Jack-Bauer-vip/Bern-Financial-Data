"""AI 智能分析对话框 — 对当前数据生成中文分析摘要"""

from PySide6.QtCore import QObject, QThread, Signal, Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QPlainTextEdit, QProgressBar, QComboBox,
    QMessageBox,
)

from src.importer.ai_client import build_data_desc


class AnalyzeWorker(QObject):
    """后台分析工作器 — 在线程中调 AI，避免卡 UI"""

    finished = Signal(str)   # 分析结果文本
    error = Signal(str)

    def __init__(self, ai_client, table_name: str, data_desc: str, focus: str):
        super().__init__()
        self.ai_client = ai_client
        self.table_name = table_name
        self.data_desc = data_desc
        self.focus = focus

    def run(self) -> None:
        try:
            result = self.ai_client.summarize_data(
                self.table_name, self.data_desc, self.focus)
            if result:
                self.finished.emit(result)
            else:
                self.error.emit("AI 分析失败（服务不可用或返回空）")
        except Exception as exc:
            self.error.emit(str(exc))


class AnalyzeDialog(QDialog):
    """AI 智能分析对话框"""

    # 常用分析重点
    FOCUS_PRESETS = [
        "",                                     # 默认（综合）
        "最新趋势与环比变化",
        "近 6 个月走势",
        "历史极值与异常点",
        "未来风险提示",
        "与其他数据的联动",
    ]

    def __init__(self, df, table_name: str, ai_client, parent=None):
        super().__init__(parent)
        self.df = df
        self.table_name = table_name
        self.ai_client = ai_client
        self._thread: QThread | None = None
        self._worker: AnalyzeWorker | None = None

        self.setWindowTitle(f"AI 智能分析 — {table_name}")
        self.setMinimumSize(640, 480)
        self.resize(720, 560)

        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        # 标题
        title = QLabel(f"🤖 AI 智能分析 — {self.table_name}")
        title.setStyleSheet("font-size: 15px; font-weight: bold; padding: 6px 0;")
        layout.addWidget(title)

        # 分析重点
        focus_row = QHBoxLayout()
        focus_row.addWidget(QLabel("分析重点:"))
        self.focus_combo = QComboBox()
        self.focus_combo.addItems(self.FOCUS_PRESETS)
        focus_row.addWidget(self.focus_combo, stretch=1)
        self.analyze_btn = QPushButton("🔍 开始分析")
        self.analyze_btn.setStyleSheet(
            "QPushButton { background-color: #1976D2; color: white; "
            "padding: 6px 16px; border-radius: 4px; font-weight: bold; }"
            "QPushButton:hover { background-color: #1565C0; }"
        )
        self.analyze_btn.clicked.connect(self._start_analyze)
        focus_row.addWidget(self.analyze_btn)
        layout.addLayout(focus_row)

        # 结果区
        self.result_text = QPlainTextEdit()
        self.result_text.setReadOnly(True)
        self.result_text.setPlaceholderText(
            "点击「开始分析」后，本地 AI 将生成数据摘要…\n"
            f"数据: {len(self.df)} 行 × {len(self.df.columns)} 列")
        layout.addWidget(self.result_text, stretch=1)

        # 进度条
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.progress.setRange(0, 0)  # 不确定进度（AI 分析耗时不定）
        layout.addWidget(self.progress)

        # 按钮
        btn_layout = QHBoxLayout()
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        btn_layout.addStretch()
        btn_layout.addWidget(close_btn)
        layout.addLayout(btn_layout)

    def _start_analyze(self) -> None:
        """启动 AI 分析（后台线程）"""
        if self._thread is not None:
            return
        if self.df is None or self.df.empty:
            QMessageBox.information(self, "提示", "当前没有可分析的数据")
            return

        data_desc = build_data_desc(self.df)
        focus = self.focus_combo.currentText()

        self.analyze_btn.setEnabled(False)
        self.progress.setVisible(True)
        self.result_text.setPlainText("AI 分析中，请稍候（本地模型约 5-30 秒）...")

        self._thread = QThread(self)
        # ★ worker 存为 self._worker 防 GC（与导入对话框同样的坑）
        self._worker = AnalyzeWorker(
            self.ai_client, self.table_name, data_desc, focus)
        worker = self._worker
        worker.moveToThread(self._thread)

        worker.finished.connect(self._on_result)
        worker.error.connect(self._on_error)
        self._thread.started.connect(worker.run)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    def _on_result(self, text: str) -> None:
        """分析完成"""
        self.result_text.setPlainText(text)
        self.analyze_btn.setEnabled(True)
        self.progress.setVisible(False)
        self._cleanup_thread()

    def _on_error(self, msg: str) -> None:
        """分析失败"""
        self.result_text.setPlainText(f"⚠ {msg}\n\n请确认本地 ollama 已启动，或检查网络。")
        self.analyze_btn.setEnabled(True)
        self.progress.setVisible(False)
        self._cleanup_thread()

    def _cleanup_thread(self) -> None:
        if self._thread:
            self._thread.quit()
            self._thread = None
        self._worker = None

    def closeEvent(self, event) -> None:
        """关闭时清理线程"""
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(2000)
            self._thread = None
        self._worker = None
        super().closeEvent(event)
