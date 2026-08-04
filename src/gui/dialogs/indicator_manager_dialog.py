"""指标管理中心对话框 — 集中查看/切换每个指标的获信（首选）来源

数据本体留在各源表；本对话框管理 meta_indicator 映射：
- 列出目录中所有带 indicator 键的指标及其候选来源（akshare/FRED）
- 每行一个下拉选择获信源，切换即持久化（set_indicator）
- 显示获信源最新数据日期，便于判断该选哪个源
参照 health_dialog.py 的表格 + 刷新 + 统计模式。
"""

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QTableWidget, QTableWidgetItem,
    QHeaderView, QComboBox, QMessageBox,
)


class IndicatorManagerDialog(QDialog):
    """指标获信源管理中心"""

    def __init__(self, repo, registry, parent=None):
        super().__init__(parent)
        self.repo = repo
        self.registry = registry
        self.setWindowTitle("指标管理中心")
        self.setMinimumSize(820, 520)
        self.resize(940, 600)
        self._setup_ui()
        self._load_data()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        title = QLabel("指标管理中心 — 查看/切换每个指标的获信来源")
        title.setStyleSheet("font-size: 15px; font-weight: bold; padding: 8px 0;")
        layout.addWidget(title)

        subtitle = QLabel(
            "同一指标可能有多个来源（akshare / FRED）。「获信源」= 该指标统一查询"
            "（AI 分析 / 导出 / /macro/cpi）时使用的首选数据。切换下拉即生效并持久化。"
        )
        subtitle.setStyleSheet("color: #888; font-size: 11px; padding-bottom: 8px;")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(
            ["指标", "候选来源", "当前获信源", "最新数据日期"])
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        layout.addWidget(self.table)

        self.summary_label = QLabel("")
        self.summary_label.setStyleSheet("padding: 4px 0; color: #aaa;")
        layout.addWidget(self.summary_label)

        btn_layout = QHBoxLayout()
        self.refresh_btn = QPushButton("🔄 刷新")
        self.refresh_btn.clicked.connect(self._load_data)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        btn_layout.addWidget(self.refresh_btn)
        btn_layout.addStretch()
        btn_layout.addWidget(close_btn)
        layout.addLayout(btn_layout)

    # ------------------------------------------------------------------
    # 数据
    # ------------------------------------------------------------------

    def _collect_indicators(self) -> dict:
        """从目录收集所有指标及其候选来源"""
        groups: dict = {}
        for s in self.registry.get_all_sources():
            ind = s.get("indicator")
            if not ind:
                continue
            groups.setdefault(ind, []).append({
                "table_name": s.get("table_name", ""),
                "api_source": s.get("api_source", ""),
                "name": s.get("name", ""),
            })
        return groups

    def _get_last_date(self, table_name: str) -> str:
        """查表的最新数据日期（meta_sync_jobs 优先，其次表内日期列）"""
        if not table_name:
            return ""
        try:
            job = self.repo.get_sync_job(table_name)
            if job and job.last_sync_date:
                return str(job.last_sync_date)
            if self.repo.table_exists(table_name):
                for c in ("date", "日期", "时间", "trade_date", "datetime", "月份"):
                    d = self.repo.get_last_date(table_name, c)
                    if d:
                        return str(d)
        except Exception:
            pass
        return ""

    def _load_data(self) -> None:
        """加载全部指标 → 填充表格"""
        groups = self._collect_indicators()
        indicators = sorted(groups.keys())

        self.table.setRowCount(len(indicators))
        for row, ind in enumerate(indicators):
            cands = groups[ind]
            mapping = self.repo.get_indicator_map(ind)
            preferred = mapping.get("preferred_table") if mapping else None

            # 列0: 指标
            self.table.setItem(row, 0, QTableWidgetItem(ind))

            # 列1: 候选来源描述
            desc = " / ".join(
                f"{c['api_source']}:{c['table_name']}" for c in cands)
            self.table.setItem(row, 1, QTableWidgetItem(desc))

            # 列2: 获信源下拉（先设项/当前值，再连信号，避免初始化误触发）。
            # 未设获信源 → 显示「(未设置)」占位项，用户主动选具体源才持久化
            combo = QComboBox()
            if preferred:
                combo.addItems([c["table_name"] for c in cands])
                idx = combo.findText(preferred)
                combo.setCurrentIndex(idx if idx >= 0 else 0)
            else:
                combo.addItem("(未设置)")
                combo.addItems([c["table_name"] for c in cands])
                combo.setCurrentIndex(0)
            combo.currentTextChanged.connect(
                lambda text, r=row, k=ind: self._on_trust_change(k, text, r))
            self.table.setCellWidget(row, 2, combo)

            # 列3: 最新数据日期（优先获信源表）
            pref_table = preferred or cands[0]["table_name"]
            self.table.setItem(
                row, 3, QTableWidgetItem(self._get_last_date(pref_table) or "从未同步"))

        # 统计
        n_mapped = sum(1 for ind in indicators
                       if self.repo.get_indicator_map(ind) is not None)
        self.summary_label.setText(
            f"共 {len(indicators)} 个指标  |  {n_mapped} 已设获信源  |  "
            f"{len(indicators) - n_mapped} 未设（自动沿用首个同步的源）")
        self.table.resizeColumnsToContents()

    def _on_trust_change(self, indicator_key: str, table_name: str, row: int) -> None:
        """获信源下拉变更 → 持久化并刷新该行"""
        if not table_name or table_name == "(未设置)":
            return
        record = self.repo.set_indicator(indicator_key, table_name)
        if record is None:
            QMessageBox.warning(
                self, "设置失败",
                f"无法把表 [{table_name}] 设为获信源：\n"
                "未能解析日期列/数值列，或表不存在。")
            return
        self.table.setItem(
            row, 3, QTableWidgetItem(self._get_last_date(table_name) or "从未同步"))
        # 更新统计
        n_mapped = sum(1 for r in range(self.table.rowCount())
                       if self.repo.get_indicator_map(
                           self.table.item(r, 0).text()) is not None)
        self.summary_label.setText(
            f"共 {self.table.rowCount()} 个指标  |  {n_mapped} 已设获信源")
