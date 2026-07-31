"""应用入口 — QApplication 初始化（深色统一主题）"""

import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QPalette, QColor
from PySide6.QtWidgets import QApplication


DARK_STYLESHEET = """
/* ===== 全局 ===== */
QWidget {
    background-color: #2b2b2b;
    color: #e0e0e0;
    font-family: "Microsoft YaHei", "Segoe UI", sans-serif;
    font-size: 12px;
}

/* ===== 菜单 ===== */
QMenuBar {
    background-color: #1e1e1e;
    color: #e0e0e0;
    border-bottom: 1px solid #3c3c3c;
    padding: 2px;
}
QMenuBar::item {
    padding: 4px 12px;
    background: transparent;
}
QMenuBar::item:selected {
    background-color: #3c3c3c;
    border-radius: 3px;
}
QMenu {
    background-color: #2d2d2d;
    color: #e0e0e0;
    border: 1px solid #444;
    padding: 4px 0;
}
QMenu::item {
    padding: 6px 30px 6px 20px;
}
QMenu::item:selected {
    background-color: #3a3a3a;
}
QMenu::separator {
    height: 1px;
    background: #444;
    margin: 4px 10px;
}

/* ===== 状态栏 ===== */
QStatusBar {
    background-color: #1e1e1e;
    color: #aaa;
    border-top: 1px solid #3c3c3c;
    font-size: 11px;
}
QStatusBar::item {
    border: none;
}

/* ===== 树控件 ===== */
QTreeWidget {
    background-color: #252526;
    color: #e0e0e0;
    border: 1px solid #3c3c3c;
    alternate-background-color: #2d2d2d;
    outline: none;
}
QTreeWidget::item {
    padding: 4px 2px;
    border: none;
}
QTreeWidget::item:selected {
    background-color: #094771;
    color: #ffffff;
}
QTreeWidget::item:hover {
    background-color: #2a2d2e;
}
QTreeWidget::branch {
    background-color: #252526;
}

/* ===== 表格 ===== */
QTableView {
    background-color: #1e1e1e;
    color: #e0e0e0;
    alternate-background-color: #2a2a2a;
    gridline-color: #3c3c3c;
    border: 1px solid #3c3c3c;
    selection-background-color: #094771;
    selection-color: #ffffff;
}
QTableView::item {
    padding: 2px 4px;
}
QTableView::item:hover {
    background-color: #2d2d2d;
}
QHeaderView::section {
    background-color: #333333;
    color: #e0e0e0;
    padding: 5px 8px;
    border: 1px solid #444;
    border-bottom: 2px solid #555;
    font-weight: bold;
    font-size: 11px;
}

/* ===== 分组框 ===== */
QGroupBox {
    background-color: #2b2b2b;
    border: 1px solid #3c3c3c;
    border-radius: 4px;
    margin-top: 12px;
    padding: 16px 8px 8px 8px;
    font-weight: bold;
    color: #e0e0e0;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 2px 10px;
    background-color: #3c3c3c;
    border-radius: 3px;
    color: #ffffff;
}

/* ===== 按钮 ===== */
QPushButton {
    background-color: #3c3c3c;
    color: #e0e0e0;
    border: 1px solid #555;
    border-radius: 3px;
    padding: 5px 14px;
    min-height: 22px;
}
QPushButton:hover {
    background-color: #4a4a4a;
    border-color: #666;
}
QPushButton:pressed {
    background-color: #2a2a2a;
}
QPushButton:disabled {
    background-color: #333;
    color: #666;
}

/* ===== 输入框 ===== */
QLineEdit, QDateEdit, QComboBox {
    background-color: #1e1e1e;
    color: #e0e0e0;
    border: 1px solid #555;
    border-radius: 3px;
    padding: 4px 6px;
    min-height: 22px;
}
QLineEdit:focus, QDateEdit:focus, QComboBox:focus {
    border-color: #0078d4;
}
QComboBox::drop-down {
    border: none;
    background-color: #3c3c3c;
    width: 20px;
}
QComboBox QAbstractItemView {
    background-color: #2d2d2d;
    color: #e0e0e0;
    border: 1px solid #555;
    selection-background-color: #094771;
}

/* ===== 进度条 ===== */
QProgressBar {
    background-color: #1e1e1e;
    border: 1px solid #444;
    border-radius: 3px;
    text-align: center;
    color: #e0e0e0;
    min-height: 18px;
}
QProgressBar::chunk {
    background-color: #0078d4;
    border-radius: 2px;
}

/* ===== 滚动条 ===== */
QScrollBar:vertical {
    background-color: #2b2b2b;
    width: 10px;
    border: none;
}
QScrollBar::handle:vertical {
    background-color: #555;
    border-radius: 4px;
    min-height: 30px;
}
QScrollBar::handle:vertical:hover {
    background-color: #777;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}
QScrollBar:horizontal {
    background-color: #2b2b2b;
    height: 10px;
    border: none;
}
QScrollBar::handle:horizontal {
    background-color: #555;
    border-radius: 4px;
    min-width: 30px;
}

/* ===== 微调 ===== */
QCheckBox, QRadioButton {
    spacing: 6px;
    color: #e0e0e0;
}
QSplitter::handle {
    background-color: #3c3c3c;
    width: 2px;
}
QLabel {
    color: #e0e0e0;
    background: transparent;
}
QTextEdit, QPlainTextEdit {
    background-color: #1e1e1e;
    color: #d4d4d4;
    border: 1px solid #3c3c3c;
}
QToolTip {
    color: #e0e0e0;
    background-color: #2d2d2d;
    border: 1px solid #555;
    padding: 4px;
}
QDialog {
    background-color: #2b2b2b;
}
QTableWidget {
    background-color: #1e1e1e;
    color: #e0e0e0;
    gridline-color: #3c3c3c;
    border: 1px solid #3c3c3c;
}
QTabWidget::pane {
    background-color: #2b2b2b;
    border: 1px solid #3c3c3c;
}
QTabBar::tab {
    background-color: #333;
    color: #aaa;
    padding: 6px 16px;
    border: 1px solid #3c3c3c;
    border-bottom: none;
}
QTabBar::tab:selected {
    background-color: #2b2b2b;
    color: #fff;
}
QScrollArea {
    background-color: #2b2b2b;
}
QListWidget {
    background-color: #1e1e1e;
    color: #e0e0e0;
    border: 1px solid #3c3c3c;
}
"""


def create_app() -> QApplication:
    """创建并配置 QApplication 实例（深色统一主题）"""
    app = QApplication(sys.argv)

    app.setApplicationName("Bern_Financial_Data")
    app.setOrganizationName("Bern")
    app.setApplicationVersion("0.1.0")

    # 跨平台统一风格
    app.setStyle("Fusion")

    # 深色系调色板
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(43, 43, 43))
    palette.setColor(QPalette.ColorRole.WindowText, Qt.GlobalColor.white)
    palette.setColor(QPalette.ColorRole.Base, QColor(30, 30, 30))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(42, 42, 42))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(45, 45, 45))
    palette.setColor(QPalette.ColorRole.ToolTipText, Qt.GlobalColor.white)
    palette.setColor(QPalette.ColorRole.Text, Qt.GlobalColor.white)
    palette.setColor(QPalette.ColorRole.Button, QColor(60, 60, 60))
    palette.setColor(QPalette.ColorRole.ButtonText, Qt.GlobalColor.white)
    palette.setColor(QPalette.ColorRole.BrightText, Qt.GlobalColor.red)
    palette.setColor(QPalette.ColorRole.Link, QColor(0, 120, 212))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(0, 120, 212))
    palette.setColor(QPalette.ColorRole.HighlightedText, Qt.GlobalColor.white)
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(127, 127, 127))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(127, 127, 127))

    app.setPalette(palette)
    app.setStyleSheet(DARK_STYLESHEET)

    return app
