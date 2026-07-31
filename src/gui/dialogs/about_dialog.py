"""关于对话框"""

from PySide6.QtWidgets import QMessageBox


def show_about_dialog(parent=None) -> None:
    """显示关于信息对话框"""
    QMessageBox.about(
        parent,
        "关于 Bern_Financial_Data",
        (
            "<h2>Bern_Financial_Data</h2>"
            "<p>版本 1.0.0</p>"
            "<p>金融数据中台 — 一站式数据采集、存储、查询系统</p>"
            "<hr>"
            "<p><b>技术栈:</b></p>"
            "<ul>"
            "<li>Python 3.12 + PySide6</li>"
            "<li>pandas 3.0 + SQLAlchemy 2.0</li>"
            "<li>akShare + TuShare 数据源</li>"
            "</ul>"
            "<hr>"
            "<p>Copyright 2026 Bern. All rights reserved.</p>"
        ),
    )
