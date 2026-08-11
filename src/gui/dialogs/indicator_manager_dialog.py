"""指标管理中心对话框 — 集中查看/切换每个指标的获信（首选）来源

数据本体留在各源表；本对话框管理 meta_indicator 映射：
- 列出目录中所有带 indicator 键的指标及其候选来源（akshare/FRED）
- 每行一个下拉选择获信源，**改动只收集为「待保存」，底部统一确认后才写库**
  （滚轮经过下拉只改显示、不写库；确认框列出每一项「旧 → 新」内容）
- 排除 deprecated(停更)源：已定案口径「deprecated 源不作为获信源候选」
- 显示获信源最新数据日期，便于判断该选哪个源
参照 health_dialog.py 的表格 + 刷新 + 统计模式。
"""

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QTableWidget, QTableWidgetItem,
    QHeaderView, QComboBox, QMessageBox,
)
from PySide6.QtCore import Qt


class _NoWheelComboBox(QComboBox):
    """禁用滚轮切换的下拉(防误触)

    鼠标滚轮经过 QComboBox 会循环切换选项——在「逐行改获信源」的表格里极易
    误触。必须点开下拉手动选择才生效。
    """

    def wheelEvent(self, event):
        event.ignore()


class IndicatorManagerDialog(QDialog):
    """指标获信源管理中心"""

    def __init__(self, repo, registry, parent=None):
        super().__init__(parent)
        self.repo = repo
        self.registry = registry
        # 待保存的获信源变更: indicator_key → 新表名。改动只进这里，点「保存变更」才写库。
        self._pending: dict[str, str] = {}
        # 各指标初始获信源(加载时记录, 保存确认框显示「旧 → 新」用)
        self._original: dict[str, str] = {}
        self.setWindowTitle("指标管理中心")
        self.setMinimumSize(860, 540)
        self.resize(960, 600)
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
            "（AI 分析 / 导出 / /macro/cpi）时使用的首选数据。\n"
            "改动下拉只收集为「待保存」，点底部「💾 保存变更」会先列出每项变更内容、"
            "确认后才写库；直接关闭则放弃所有改动。"
        )
        subtitle.setStyleSheet("color: #888; font-size: 11px; padding-bottom: 8px;")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(
            ["指标", "候选来源", "当前获信源", "口径", "最新数据日期"])
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch)
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

        self.summary_label = QLabel("")
        self.summary_label.setStyleSheet("padding: 4px 0; color: #aaa;")
        layout.addWidget(self.summary_label)

        btn_layout = QHBoxLayout()
        self.refresh_btn = QPushButton("🔄 刷新")
        self.refresh_btn.clicked.connect(self._on_refresh)
        self.save_btn = QPushButton("💾 保存变更")
        self.save_btn.setStyleSheet(
            "QPushButton { background-color:#1976D2; color:white; "
            "padding:5px 14px; border-radius:4px; font-weight:bold; }"
            "QPushButton:hover { background-color:#1565C0; }"
            "QPushButton:disabled { background-color:#bbb; color:#eee; }")
        self.save_btn.clicked.connect(self._save_changes)
        self.save_btn.setEnabled(False)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        btn_layout.addWidget(self.refresh_btn)
        btn_layout.addWidget(self.save_btn)
        btn_layout.addStretch()
        btn_layout.addWidget(close_btn)
        layout.addLayout(btn_layout)

    # ------------------------------------------------------------------
    # 数据
    # ------------------------------------------------------------------

    def _collect_indicators(self) -> dict:
        """从目录收集所有指标及其候选来源（含口径语义 unit_type/unit_desc）

        排除 deprecated(停更)源：已定案口径「deprecated 源不作为获信源候选」。
        未 deprecated 但可选的 akshare 源(如 us.cpi 同比表)保留在候选中。
        """
        groups: dict = {}
        for s in self.registry.get_all_sources(include_deprecated=False):
            ind = s.get("indicator")
            if not ind:
                continue
            groups.setdefault(ind, []).append({
                "table_name": s.get("table_name", ""),
                "api_source": s.get("api_source", ""),
                "name": s.get("name", ""),
                "unit_type": str(s.get("unit_type", "level")),
                "unit_desc": s.get("unit_desc") or "",
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
        """加载全部指标 → 填充表格（pending/original 随加载重置）"""
        self._pending.clear()
        self._original.clear()
        groups = self._collect_indicators()
        indicators = sorted(groups.keys())

        self.table.setRowCount(len(indicators))
        for row, ind in enumerate(indicators):
            cands = groups[ind]
            mapping = self.repo.get_indicator_map(ind)
            preferred = mapping.get("preferred_table") if mapping else None
            self._original[ind] = preferred or ""

            # 列0: 指标
            self.table.setItem(row, 0, QTableWidgetItem(ind))

            # 列1: 候选来源描述
            desc = " / ".join(
                f"{c['api_source']}:{c['table_name']}" for c in cands)
            self.table.setItem(row, 1, QTableWidgetItem(desc))

            # 列2: 获信源下拉（先设项/当前值，再连信号，避免初始化误触发）。
            # 未设获信源 → 显示「(未设置)」占位项，用户主动选具体源才生效
            combo = _NoWheelComboBox()
            if preferred:
                combo.addItems([c["table_name"] for c in cands])
                idx = combo.findText(preferred)
                combo.setCurrentIndex(idx if idx >= 0 else 0)
            else:
                combo.addItem("(未设置)")
                combo.addItems([c["table_name"] for c in cands])
                combo.setCurrentIndex(0)
            combo.setToolTip(
                "改动先收集为「待保存」，点底部「💾 保存变更」确认后才写库")
            combo.currentTextChanged.connect(
                lambda text, r=row, k=ind: self._on_trust_change(k, text, r))
            self.table.setCellWidget(row, 2, combo)

            # 列3/列4: 当前选择的口径与最新数据日期
            self._update_row_meta(row, ind, combo.currentText(), cands)

        self._update_summary()
        self.table.resizeColumnsToContents()
        self._update_save_state()

    def _update_row_meta(self, row: int, indicator_key: str,
                         table_name: str, cands: list[dict] | None = None) -> None:
        """刷新某行「口径」与「最新数据日期」两列（切下拉时即时预览）"""
        cands = cands or self._collect_indicators().get(indicator_key, [])
        unit_desc = next((c["unit_desc"] for c in cands
                          if c["table_name"] == table_name), "")
        unit_type = next((c["unit_type"] for c in cands
                          if c["table_name"] == table_name), "")
        unit_cell = (f"{unit_desc} ({unit_type})" if unit_desc else "—")
        self.table.setItem(row, 3, QTableWidgetItem(unit_cell))
        self.table.setItem(
            row, 4, QTableWidgetItem(self._get_last_date(table_name) or "从未同步"))

    def _on_trust_change(self, indicator_key: str, table_name: str, row: int) -> None:
        """下拉变更 → 只记录为「待保存」，不写库

        滚轮经过/键盘循环只改显示；点「保存变更」统一确认后才 set_indicator。
        """
        if not table_name or table_name == "(未设置)":
            return
        orig = self._original.get(indicator_key, "")
        if table_name == orig:
            self._pending.pop(indicator_key, None)
        else:
            self._pending[indicator_key] = table_name
        self._update_row_meta(row, indicator_key, table_name)
        self._update_save_state()

    # ------------------------------------------------------------------
    # 保存 / 刷新
    # ------------------------------------------------------------------

    def _update_save_state(self) -> None:
        n = len(self._pending)
        self.save_btn.setEnabled(n > 0)
        self.save_btn.setText(f"💾 保存变更 ({n})" if n else "💾 保存变更")

    def _save_changes(self) -> None:
        """列出全部待变更内容 → 确认 → 逐项写库"""
        if not self._pending:
            return
        lines = []
        for k in sorted(self._pending):
            new_t = self._pending[k]
            old_t = self._original.get(k, "(无)")
            marker = "＋新增" if not old_t else "切换"
            lines.append(f"• {k}: {marker}  {old_t} → {new_t}")
        msg = ("确认保存以下获信源变更？\n\n"
               + "\n".join(lines)
               + "\n\n保存后立即对所有下游查询(AI 分析/导出/主题看板/API)生效。")
        if QMessageBox.question(self, "确认变更", msg) != QMessageBox.StandardButton.Yes:
            return
        ok, failed = 0, 0
        for k, new_t in list(self._pending.items()):
            unit_type = unit_desc = None
            for c in self._collect_indicators().get(k, []):
                if c["table_name"] == new_t:
                    unit_type, unit_desc = c["unit_type"], c["unit_desc"]
                    break
            rec = self.repo.set_indicator(
                k, new_t, unit_type=unit_type, unit_desc=unit_desc)
            if rec is None:
                failed += 1
                QMessageBox.warning(
                    self, "保存失败",
                    f"无法把表 [{new_t}] 设为 {k} 的获信源：\n"
                    "未能解析日期列/数值列，或表不存在/deprecated。")
            else:
                ok += 1
        if ok:
            self._load_data()  # 重置 pending/original, 刷新全部行
            QMessageBox.information(
                self, "保存完成", f"已保存 {ok} 项获信源变更。"
                + (f" {failed} 项失败。" if failed else ""))
        elif failed:
            self._load_data()

    def _on_refresh(self) -> None:
        """刷新：有未保存变更时先提示（刷新会丢弃待保存改动）"""
        if self._pending:
            if QMessageBox.question(
                    self, "丢弃待保存变更",
                    f"有 {len(self._pending)} 项改动尚未保存，刷新将丢弃它们。继续？") != QMessageBox.StandardButton.Yes:
                return
        self._load_data()

    def _update_summary(self) -> None:
        indicators = [self.table.item(r, 0).text()
                      for r in range(self.table.rowCount())]
        n_mapped = sum(1 for ind in indicators
                       if self.repo.get_indicator_map(ind) is not None)
        self.summary_label.setText(
            f"共 {len(indicators)} 个指标  |  {n_mapped} 已设获信源  |  "
            f"{len(indicators) - n_mapped} 未设（自动沿用首个同步的源）")
