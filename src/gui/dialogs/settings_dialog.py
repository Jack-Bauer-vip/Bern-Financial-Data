"""设置对话框 — API 配置与应用设置"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QSpinBox,
    QCheckBox,
    QPushButton,
    QMessageBox,
)

from src.utils.config import ConfigManager


class SettingsDialog(QDialog):
    """应用设置对话框"""

    def __init__(self, config: ConfigManager, parent=None):
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("设置")
        self.setMinimumWidth(450)

        self.setup_ui()
        self.load_settings()

    def setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        # ----------------------------------------------------------------
        # API 配置
        # ----------------------------------------------------------------
        api_group = QGroupBox("API 配置")
        api_form = QFormLayout(api_group)

        self.token_input = QLineEdit()
        self.token_input.setPlaceholderText("输入 TuShare API Token")
        self.token_input.setEchoMode(QLineEdit.EchoMode.Password)
        api_form.addRow("TuShare Token:", self.token_input)

        self.show_token_cb = QCheckBox("显示 Token")
        self.show_token_cb.stateChanged.connect(self._toggle_token_visibility)
        api_form.addRow("", self.show_token_cb)

        layout.addWidget(api_group)

        # ----------------------------------------------------------------
        # 服务配置
        # ----------------------------------------------------------------
        server_group = QGroupBox("服务配置")
        server_form = QFormLayout(server_group)

        self.host_input = QLineEdit()
        self.host_input.setPlaceholderText("127.0.0.1")
        server_form.addRow("API 主机:", self.host_input)

        self.port_spin = QSpinBox()
        self.port_spin.setRange(1024, 65535)
        self.port_spin.setValue(8765)
        server_form.addRow("API 端口:", self.port_spin)

        layout.addWidget(server_group)

        # ----------------------------------------------------------------
        # 应用设置
        # ----------------------------------------------------------------
        app_group = QGroupBox("应用设置")
        app_form = QFormLayout(app_group)

        self.auto_start_cb = QCheckBox("启动时自动同步")
        app_form.addRow("", self.auto_start_cb)

        self.minimize_tray_cb = QCheckBox("最小化到系统托盘")
        app_form.addRow("", self.minimize_tray_cb)

        layout.addWidget(app_group)

        # ----------------------------------------------------------------
        # 按钮
        # ----------------------------------------------------------------
        btn_layout = QHBoxLayout()

        self.save_btn = QPushButton("保存")
        self.save_btn.clicked.connect(self._save_settings)

        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.clicked.connect(self.reject)

        btn_layout.addStretch()
        btn_layout.addWidget(self.save_btn)
        btn_layout.addWidget(self.cancel_btn)

        layout.addLayout(btn_layout)

    def _toggle_token_visibility(self, state: int) -> None:
        """切换 Token 的显示/隐藏"""
        if state == Qt.CheckState.Checked.value:
            self.token_input.setEchoMode(QLineEdit.EchoMode.Normal)
        else:
            self.token_input.setEchoMode(QLineEdit.EchoMode.Password)

    def load_settings(self) -> None:
        """从配置加载当前设置"""
        token = self.config.tushare_token
        if token:
            self.token_input.setText(token)

        self.host_input.setText(self.config.api_host)
        self.port_spin.setValue(self.config.api_port)

    def _save_settings(self) -> None:
        """保存设置（通过环境变量实际无法持久化，仅做演示）"""
        token = self.token_input.text().strip()
        if token:
            QMessageBox.information(
                self,
                "提示",
                "Token 已记录（当前为演示模式，实际持久化需写入 .env 文件）",
            )
        self.accept()
