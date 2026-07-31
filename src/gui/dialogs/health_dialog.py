"""数据源健康检查对话框 — 检测停更状态、最新日期、备用推荐"""

from datetime import datetime, date, timedelta
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QTableWidget, QTableWidgetItem,
    QHeaderView, QMessageBox,
)
from PySide6.QtGui import QColor, QBrush


# Cron 表达式到预期更新间隔（天）的映射
CRON_TO_EXPECTED_DAYS = {
    # 每天
    "0 0 * * *": 1, "0 6 * * *": 1, "0 8 * * *": 1,
    "0 10 * * *": 1, "0 14 * * *": 1, "0 15 * * *": 1,
    "0 22 * * *": 1,
    # 工作日
    "0 0 * * 1-5": 1.5, "0 8 * * 1-5": 1.5,
    "0 10 * * 1-5": 1.5, "0 14 * * 1-5": 1.5,
    "0 15 * * 1-5": 1.5, "0 18 * * 1-5": 1.5,
    "0 22 * * 1-5": 1.5, "30 18 * * 1-5": 1.5,
    "30 19 * * 1-5": 1.5,
    # 其他
    "0 */6 * * *": 0.25,
    "0 */12 * * *": 0.5,
}

DEFAULT_EXPECTED_DAYS = 7  # 默认认为 7 天内算正常


def get_expected_interval_days(cron: str) -> float:
    """从 cron 表达式推算预期更新间隔（天）"""
    if not cron:
        return DEFAULT_EXPECTED_DAYS
    cron = cron.strip()
    return CRON_TO_EXPECTED_DAYS.get(cron, DEFAULT_EXPECTED_DAYS)


def get_stale_status(
    last_date: Optional[date],
    cron: str,
    today: date = None,
) -> tuple[str, str, str]:
    """判断数据源健康状态

    Returns:
        (status_label, status_color_hex, days_since_update_str)
    """
    if today is None:
        today = date.today()

    if last_date is None:
        return ("未同步", "#888888", "从未同步")

    days_since = (today - last_date).days
    expected_days = get_expected_interval_days(cron)
    # 松一点：允许 2 倍预期间隔
    stale_threshold = max(expected_days * 2, 3)

    if days_since < 0:
        return ("✅ 正常", "#2e7d32", f"未来数据 ({days_since}天)")
    elif days_since <= stale_threshold:
        return ("✅ 正常", "#2e7d32", f"{days_since} 天前")
    elif days_since <= stale_threshold * 2:
        return ("⚠️ 滞后", "#e65100", f"{days_since} 天前")
    else:
        return ("🔴 停更", "#c62828", f"{days_since} 天前")


class HealthDialog(QDialog):
    """数据源健康检查对话框"""

    def __init__(self, repo, registry, scheduler, parent=None):
        super().__init__(parent)
        self.repo = repo
        self.registry = registry
        self.scheduler = scheduler
        self.setWindowTitle("数据源健康检查")
        self.setMinimumSize(800, 500)
        self.resize(900, 600)
        self._setup_ui()
        self._load_data()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        # 标题
        title = QLabel("数据源健康状况 — 检查停更数据")
        title.setStyleSheet("font-size: 16px; font-weight: bold; padding: 8px 0;")
        layout.addWidget(title)

        subtitle = QLabel(
            "✅ 正常 = 最近有更新  |  ⚠️ 滞后 = 超过预期更新周期未更新  |  "
            "🔴 停更 = 数据可能已断更，建议查找备用数据源"
        )
        subtitle.setStyleSheet("color: #888; font-size: 11px; padding-bottom: 8px;")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        # 表格
        self.table = QTableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels([
            "数据源", "经济体", "最新数据日期", "状态",
            "距今天数", "更新频率", "建议操作"
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
        self.table.horizontalHeader().setSectionResizeMode(
            5, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(
            6, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        layout.addWidget(self.table)

        # 统计信息
        self.summary_label = QLabel("")
        self.summary_label.setStyleSheet("padding: 4px 0; color: #aaa;")
        layout.addWidget(self.summary_label)

        # 按钮
        btn_layout = QHBoxLayout()
        self.refresh_btn = QPushButton("🔄 刷新检查")
        self.refresh_btn.clicked.connect(self._load_data)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        btn_layout.addWidget(self.refresh_btn)
        btn_layout.addStretch()
        btn_layout.addWidget(close_btn)
        layout.addLayout(btn_layout)

    def _load_data(self) -> None:
        """加载并分析所有数据源的健康状况"""
        sources = self.registry.get_all_sources()
        today = date.today()

        rows = []
        for src in sources:
            table_name = src.get("table_name", "")
            source_key = src.get("source_key", "")
            name = src.get("name", source_key)
            api_func = src.get("api_function", "")
            cron = src.get("schedule_cron", "")

            # 从 meta_sync_jobs 获取同步状态
            sync_job = None
            try:
                # ★ source_key 和 table_name 都可能作为 module_name
                # 先按 table_name 查（sync_engine 的默认存储 key）
                if table_name:
                    sync_job = self.repo.get_sync_job(table_name)
                if sync_job is None and source_key != table_name:
                    sync_job = self.repo.get_sync_job(source_key)
            except Exception:
                pass

            last_date = None
            if sync_job and sync_job.last_sync_date:
                last_date = sync_job.last_sync_date
            elif table_name and self.repo.table_exists(table_name):
                try:
                    # 尝试多个可能的日期列名
                    last_date = self.repo.get_last_date(table_name, "date")
                    if last_date is None:
                        for _c in ("日期", "时间", "trade_date", "datetime", "月份"):
                            last_date = self.repo.get_last_date(table_name, _c)
                            if last_date is not None:
                                break
                except Exception:
                    pass

            status_label, status_color, days_text = get_stale_status(
                last_date, cron, today)

            # 经济体归类
            economy = self._guess_economy(source_key)

            # 建议操作
            suggestion = self._get_suggestion(status_label, api_func, source_key)

            rows.append({
                "name": name,
                "economy": economy,
                "last_date": last_date.strftime("%Y-%m-%d") if last_date else "从未同步",
                "status": status_label,
                "status_color": status_color,
                "days": days_text,
                "cron": self._describe_cron(cron),
                "suggestion": suggestion,
                "api_func": api_func,
            })

        # 按状态排序：停更 > 滞后 > 正常 > 未同步
        status_order = {"🔴 停更": 0, "⚠️ 滞后": 1, "✅ 正常": 2, "未同步": 3}
        rows.sort(key=lambda r: (
            status_order.get(r["status"], 9),
            r["last_date"] if r["last_date"] != "从未同步" else "",
        ))

        # 填充表格
        self.table.setRowCount(len(rows))
        for row_idx, r in enumerate(rows):
            self.table.setItem(row_idx, 0, QTableWidgetItem(r["name"]))
            self.table.setItem(row_idx, 1, QTableWidgetItem(r["economy"]))
            self.table.setItem(row_idx, 2, QTableWidgetItem(str(r["last_date"])))

            status_item = QTableWidgetItem(r["status"])
            status_item.setForeground(QBrush(QColor(r["status_color"])))
            self.table.setItem(row_idx, 3, status_item)

            self.table.setItem(row_idx, 4, QTableWidgetItem(r["days"]))
            self.table.setItem(row_idx, 5, QTableWidgetItem(r["cron"]))
            self.table.setItem(row_idx, 6, QTableWidgetItem(r["suggestion"]))

        # 统计
        total = len(rows)
        stale = sum(1 for r in rows if r["status"] == "🔴 停更")
        lagging = sum(1 for r in rows if r["status"] == "⚠️ 滞后")
        ok = sum(1 for r in rows if r["status"] == "✅ 正常")
        never = sum(1 for r in rows if r["status"] == "未同步")

        self.summary_label.setText(
            f"共 {total} 个数据源  |  "
            f"🟢 {ok} 正常  |  🟡 {lagging} 滞后  |  "
            f"🔴 {stale} 停更  |  ⚪ {never} 未同步"
        )

        self.table.resizeColumnsToContents()

    def _guess_economy(self, source_key: str) -> str:
        """从 source_key 推测经济体"""
        key = source_key.lower()
        if "us" in key or "usa" in key:
            return "🇺🇸 美国"
        if "eu" in key or "euro" in key:
            return "🇪🇺 欧元区"
        if "jp" in key or "japan" in key:
            return "🇯🇵 日本"
        if "uk" in key or "britain" in key:
            return "🇬🇧 英国"
        if "cn" in key or "china" in key or "shibor" in key or "lpr" in key:
            return "🇨🇳 中国"
        if "au" in key or "australia" in key:
            return "🇦🇺 澳洲"
        if "stock" in key:
            return "📈 股票"
        if "index" in key:
            return "📊 指数"
        if "fund" in key:
            return "💰 基金"
        return "🌍 其他"

    def _describe_cron(self, cron: str) -> str:
        """将 cron 表达式转为可读描述"""
        if not cron:
            return "手动"
        parts = cron.strip().split()
        if len(parts) != 5:
            return cron
        hour, minute, dom, month, dow = parts[1], parts[0], parts[2], parts[3], parts[4]
        if dow == "*" and dom == "*":
            return f"每天 {hour}:{minute.zfill(2)}"
        if dow != "*" and dom == "*":
            days = {"0":"日","1":"一","2":"二","3":"三","4":"四","5":"五","6":"六"}
            ds = "".join(days.get(d, d) for d in dow.split(","))
            return f"周{ds} {hour}:{minute.zfill(2)}"
        return cron

    def _get_suggestion(
        self, status_label: str, api_func: str, source_key: str
    ) -> str:
        """根据状态给出建议"""
        if status_label == "未同步":
            return "点击「增量同步」首次获取"
        if status_label == "🔴 停更":
            # 推荐备用数据源
            alt = self._find_alternative(source_key)
            return f"数据可能已停更，备用: {alt}" if alt else "考虑查找其他数据源"
        if status_label == "⚠️ 滞后":
            return "建议手动触发增量同步"
        return "自动更新中"

    def _find_alternative(self, source_key: str) -> str:
        """查找同类型备用数据源"""
        key = source_key.lower()
        alternatives = {
            "macro_usa_cpi_yoy": "macro_usa_core_cpi_monthly (核心CPI)",
            "macro_usa_core_cpi_monthly": "macro_usa_cpi_yoy (CPI年率)",
            "macro_china_cpi_yearly": "macro_china_cpi_monthly (CPI月率)",
            "macro_china_cpi_monthly": "macro_china_cpi_yearly (CPI年率)",
            "macro_usa_gdp_monthly": "macro_euro_gdp_yoy (欧元区GDP)",
            "macro_euro_gdp_yoy": "macro_usa_gdp_monthly (美国GDP)",
            "macro_usa_non_farm": "macro_usa_unemployment_rate (失业率)",
        }
        for k, v in alternatives.items():
            if k in key:
                return v
        # 通用查找：同 prefix
        prefix = key.split(".")[-1] if "." in key else ""
        if prefix:
            sources = self.registry.get_all_sources()
            similar = [s.get("name", "") for s in sources
                       if prefix in s.get("source_key", "").lower()
                       and s.get("source_key") != source_key]
            if similar:
                return similar[0]
        return ""
