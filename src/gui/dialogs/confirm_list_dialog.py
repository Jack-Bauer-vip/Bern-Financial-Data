"""可滚动确认对话框 — 用于展示很长的逐项列表（如批量导入上千个文件）

QMessageBox 不支持滚动，条目多时窗口会高过屏幕、按钮被挤到看不见。
本对话框把列表放进只读的 QPlainTextEdit（自带滚动），窗口高度封顶，
无论多少条目「确定/取消」都始终可见可点。

参照 analyze_dialog.py 的只读 QPlainTextEdit 模式；暗色主题（app.py QSS）
已覆盖 QDialog / QPlainTextEdit / QPushButton，无需额外样式。
"""

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPlainTextEdit,
    QDialogButtonBox,
)


class ConfirmListDialog(QDialog):
    """通用可滚动确认对话框

    Parameters
    ----------
    parent : QWidget | None
        父窗口
    title : str
        窗口标题
    summary : str
        顶部摘要（如「将导入 N 个文件：…」），可为空字符串
    lines : list[str] | str
        逐项列表；传字符串时原样展示，传列表时按行拼接
    confirm_text : str
        确认按钮文字（默认「确定」），取消按钮为「取消」
    """

    def __init__(self, parent=None, title: str = "确认",
                 summary: str = "", lines=None,
                 confirm_text: str = "确定"):
        super().__init__(parent)
        self.setWindowTitle(title)
        # 高度封顶：无论多少条目，都能滚到底部的按钮
        self.setMinimumSize(560, 420)
        self.resize(640, 480)

        if isinstance(lines, str):
            body = lines
        else:
            body = "\n".join(lines or [])

        layout = QVBoxLayout(self)

        if summary:
            head = QLabel(summary)
            head.setWordWrap(True)
            head.setStyleSheet("font-weight: bold;")
            layout.addWidget(head)

        self.text_view = QPlainTextEdit()
        self.text_view.setReadOnly(True)
        self.text_view.setPlainText(body)
        layout.addWidget(self.text_view, stretch=1)

        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        ok_btn = btn_box.button(QDialogButtonBox.StandardButton.Ok)
        if ok_btn is not None:
            ok_btn.setText(confirm_text)
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)
