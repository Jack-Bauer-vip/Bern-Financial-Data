"""底部运行日志组件"""

from datetime import datetime

from PySide6.QtWidgets import QTextEdit
from PySide6.QtGui import QTextCursor


class LogWidget(QTextEdit):
    """运行日志显示组件，支持彩色分级日志输出"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.document().setMaximumBlockCount(5000)
        self.setStyleSheet("""
            QTextEdit {
                font-size: 12px;
                background-color: #1e1e1e;
                color: #d4d4d4;
                border: 1px solid #3c3c3c;
            }
        """)
        self._auto_scroll = True

    def write(self, level: str, message: str) -> None:
        """写入一条带颜色的日志

        Parameters
        ----------
        level : str
            日志级别: INFO, WARNING, ERROR, SUCCESS, DEBUG
        message : str
            日志内容
        """
        color_map = {
            "INFO": "#d4d4d4",
            "WARNING": "#ffa500",
            "ERROR": "#ff4444",
            "SUCCESS": "#4ec94e",
            "DEBUG": "#888888",
        }
        color = color_map.get(level.upper(), "#d4d4d4")
        timestamp = datetime.now().strftime("%H:%M:%S")
        html = (
            f'<span style="color:{color}">'
            f"[{timestamp}] {level}: {message}"
            f"</span><br>"
        )
        self.append_html(html)

    def append_html(self, html: str) -> None:
        """在末尾追加 HTML 内容并自动滚动"""
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertHtml(html)
        if self._auto_scroll:
            scrollbar = self.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())
