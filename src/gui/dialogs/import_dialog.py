"""导入对话框 — 批量选择、智能识别目标表、列映射、新字段建议、按唯一键更新+新增"""

import os
from dataclasses import dataclass, field
from typing import Any, Optional

import pandas as pd
from PySide6.QtCore import Qt, QObject, QThread, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGroupBox,
    QLabel, QPushButton, QFileDialog, QMessageBox,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QProgressBar, QComboBox, QAbstractItemView,
)

from src.importer.base import ImportError
from src.importer.csv_importer import CsvImporter
from src.importer.excel_importer import ExcelImporter

# 导入器注册表（与文件扩展名对应）
# 注意：openpyxl 不支持旧版 .xls 二进制，故仅注册 .xlsx
IMPORTERS = {
    ".csv": (CsvImporter, "CSV 文件 (*.csv)"),
    ".xlsx": (ExcelImporter, "Excel 文件 (*.xlsx)"),
}


def get_importer(path: str):
    """根据文件扩展名返回导入器实例"""
    ext = os.path.splitext(path)[1].lower()
    for key, (cls, _) in IMPORTERS.items():
        if ext == key:
            return cls()
    return None


# ---------------------------------------------------------------------------
# 后台识别 worker
# ---------------------------------------------------------------------------


@dataclass
class FileItem:
    """单个待导入文件的状态"""
    path: str
    df: Optional[pd.DataFrame] = None
    table_name: str = ""
    confidence: float = 0.0
    method: str = "rules"           # rules | ai
    low_confidence: bool = False
    plan: Any = None                # ColumnMappingResult
    unique_key: list = field(default_factory=list)
    analysis: dict = field(default_factory=dict)
    status: str = "待识别"
    error: str = ""
    reason: str = ""


class BatchIdentifyWorker(QObject):
    """批量识别工作器：逐个 read → 识别目标表 → 列映射 → 影响估算"""

    file_done = Signal(str, object)     # path, FileItem
    batch_done = Signal()

    def __init__(self, repo, schema_mgr, ai_client, paths: list[str],
                 suggested_table: str = "", template_store=None):
        super().__init__()
        self.repo = repo
        self.schema_mgr = schema_mgr
        self.ai_client = ai_client
        self.paths = paths
        self.suggested_table = suggested_table
        self.template_store = template_store
        # 识别阶段数据库不会变化 → 同一目标表只全表加载/统计一次
        self._table_snapshots: dict[str, pd.DataFrame] = {}
        self._dup_counts: dict[tuple, int] = {}

    def run(self) -> None:
        from src.importer.matcher import (
            match_table, collect_tables, detect_fund_code,
            FUND_TABLE, FUND_CANONICAL_COLUMNS,
        )
        from src.importer.column_mapper import map_columns
        from src.core.unique_key import infer_unique_cols_by_table

        tables_meta = collect_tables(self.repo)

        # 表头模板索引（统一表头批量导入时确定性路由，跳过规则评分与 AI）
        templates = None
        if self.template_store is not None:
            from src.importer.header_template import build_index
            templates = build_index(self.template_store.config, self.repo, tables_meta)

        for path in self.paths:
            item = FileItem(path=path)
            try:
                importer = get_importer(path)
                if importer is None:
                    raise ImportError("不支持的文件类型，请选择 CSV 或 Excel")
                item.df = importer.read(path)

                # 目标表识别（规则优先，AI 兜底）
                item.status = "识别中..."
                fund_code = detect_fund_code(path)
                if fund_code:
                    # ★ 文件名含 6 位基金代码 → 直接路由到基金表并注入 code 列，
                    #   使同一基金的 CSV 上传 / API 同步按 (code,date) 合并去重
                    item.table_name = FUND_TABLE
                    item.confidence = 1.0
                    item.method = "rules"
                    item.reason = f"从文件名识别基金代码 {fund_code}"
                    item.df["code"] = fund_code
                    table_cols = list(FUND_CANONICAL_COLUMNS)
                else:
                    res = match_table(
                        item.df,
                        self.repo,
                        tables_meta=tables_meta,
                        ai_client=self.ai_client,
                        filename=os.path.basename(path),
                        templates=templates,
                    )
                    item.table_name = res.table_name if res else ""
                    item.confidence = res.confidence if res else 0.0
                    item.method = res.method if res else "rules"
                    item.reason = res.reason if res else ""

                    if not item.table_name:
                        item.status = "错误"
                        item.error = "未能识别目标表，请在列表中手动选择"
                        self.file_done.emit(path, item)
                        continue
                    table_cols = self.repo.get_all_existing_columns(item.table_name)

                # 列映射 + 新字段检测
                item.plan = map_columns(
                    list(item.df.columns), table_cols, self.ai_client)
                # 低置信标记属于列映射结果（AI 映射置信度较低），不在 MatchResult 上
                item.low_confidence = item.plan.low_confidence

                # 唯一键
                item.unique_key = infer_unique_cols_by_table(item.table_name)

                # 影响估算（对映射后的 df）
                # 性能：同一目标表本批只全表加载/重复计数一次（识别阶段库不变），
                # 上千个同表头文件不再每文件 O(表大小) 扫全表。
                if item.table_name not in self._table_snapshots:
                    self._table_snapshots[item.table_name] = self.repo.query(
                        item.table_name, limit=None)
                cols = self.repo.resolve_date_columns(
                    item.table_name, item.unique_key or [])
                dup_key = (item.table_name, tuple(cols))
                if dup_key not in self._dup_counts:
                    self._dup_counts[dup_key] = self.repo.get_duplicate_count(
                        item.table_name, cols)
                mapped_df = item.plan.apply(item.df)
                item.analysis = self.repo.analyze_import(
                    item.table_name, mapped_df, item.unique_key or None,
                    existing_df=self._table_snapshots[item.table_name],
                    dup_in_db=self._dup_counts[dup_key])

                new_note = (f"，{len(item.plan.new_columns)}个新字段"
                            if item.plan.new_columns else "")
                item.status = f"就绪{new_note}"
            except Exception as exc:
                item.status = "错误"
                item.error = str(exc)
            self.file_done.emit(path, item)

        self.batch_done.emit()


# ---------------------------------------------------------------------------
# 对话框
# ---------------------------------------------------------------------------


class ImportDialog(QDialog):
    """数据导入对话框（批量 + 智能识别）

    目标表默认自动识别（规则+AI），用户可在列表中手动修改。
    导入策略：按唯一键更新已存在行、插入新行；新字段提示是否添加。
    """

    def __init__(self, repo, suggested_table_name: str = "",
                 suggested_unique_key: list[str] | None = None, parent=None):
        super().__init__(parent)
        self.repo = repo
        self.suggested_table = suggested_table_name or ""
        self.suggested_unique_key = suggested_unique_key or []

        # AI 客户端（不可用自动为 None → 全规则）
        from src.utils.config import ConfigManager
        from src.importer.ai_client import AiClient
        self.config = ConfigManager()
        self.ai_client = AiClient(self.config) if AiClient(self.config).is_available() else None

        # 表头记忆模板（同意表头 → 确定性路由，减少规则评分与 AI 依赖）
        from src.importer.header_template import HeaderTemplateStore
        self._template_store = (
            HeaderTemplateStore(self.config)
            if self.config.get("import.learn_templates", True) else None
        )

        from src.importer.matcher import collect_tables
        self._tables_meta = collect_tables(repo)
        self._table_names = [t.table_name for t in self._tables_meta]
        # 加入数据分类目录中定义、但尚未建表的数据源（如 fund_etf_daily），
        # 使用户可在下拉中手动选择目标表（首个文件导入时会自动建表）
        try:
            from src.core.fetcher_registry import FetcherRegistry
            from src.utils.config import ConfigManager
            for src in FetcherRegistry(ConfigManager()).get_all_sources():
                tn = src.get("table_name")
                if tn and tn not in self._table_names:
                    self._table_names.append(tn)
        except Exception:
            pass
        if self.suggested_table and self.suggested_table not in self._table_names:
            self._table_names.insert(0, self.suggested_table)

        self._items: dict[str, FileItem] = {}   # path -> FileItem
        self._paths: list[str] = []
        self._thread: QThread | None = None
        self._worker: BatchIdentifyWorker | None = None
        # 识别阶段按目标表缓存的全表快照 / 重复计数（识别完成后供手动改表复用，
        # 避免再次对大数据表全表加载 → 主线程卡死）
        self._table_snapshots: dict = {}
        self._dup_counts: dict = {}

        self.setWindowTitle("导入数据")
        self.setMinimumSize(760, 560)
        self.resize(860, 640)

        # 支持拖拽文件到窗口
        self.setAcceptDrops(True)

        self._setup_ui()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        # ---- ① 选择文件 ----
        file_group = QGroupBox("① 选择文件（可多选 / 拖拽文件到窗口）")
        file_layout = QHBoxLayout(file_group)
        self.file_label = QLabel("拖拽文件到此处，或点击浏览")
        self.file_label.setStyleSheet("color: #888;")
        self.browse_btn = QPushButton("浏览...")
        self.browse_btn.clicked.connect(self._browse)
        file_layout.addWidget(self.file_label, stretch=1)
        file_layout.addWidget(self.browse_btn)
        layout.addWidget(file_group)

        # ---- ② 文件列表 ----
        list_group = QGroupBox("② 文件与目标表（识别后可手动改）")
        list_layout = QVBoxLayout(list_group)
        self.file_table = QTableWidget()
        self.file_table.setColumnCount(6)
        self.file_table.setHorizontalHeaderLabels(
            ["文件名", "目标表", "识别", "新增", "更新", "状态"])
        self.file_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        self.file_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents)
        self.file_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents)
        self.file_table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents)
        self.file_table.horizontalHeader().setSectionResizeMode(
            4, QHeaderView.ResizeMode.ResizeToContents)
        self.file_table.horizontalHeader().setSectionResizeMode(
            5, QHeaderView.ResizeMode.ResizeToContents)
        self.file_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        self.file_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers)
        self.file_table.verticalHeader().setVisible(False)
        self.file_table.itemSelectionChanged.connect(self._on_selection_changed)
        list_layout.addWidget(self.file_table)
        layout.addWidget(list_group)

        # ---- ③ 数据预览 ----
        preview_group = QGroupBox("③ 数据预览（选中文件）")
        preview_layout = QVBoxLayout(preview_group)
        self.preview = QTableWidget()
        self.preview.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.preview.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        self.preview.verticalHeader().setVisible(False)
        self.preview.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        preview_layout.addWidget(self.preview)
        layout.addWidget(preview_group, stretch=1)

        # ---- ④ 影响估算 ----
        self.impact_label = QLabel("选择文件后自动识别目标表")
        self.impact_label.setStyleSheet("padding: 4px 0; color: #aaa;")
        self.impact_label.setWordWrap(True)
        layout.addWidget(self.impact_label)

        # 进度条
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        # ---- 按钮 ----
        btn_layout = QHBoxLayout()
        clear_btn = QPushButton("清除表头记忆")
        clear_btn.setToolTip("删除学习到的「表头→目标表」绑定，下次导入需重新识别")
        clear_btn.clicked.connect(self._clear_templates)
        btn_layout.addWidget(clear_btn)
        btn_layout.addStretch()
        self.import_btn = QPushButton("导入")
        self.import_btn.setEnabled(False)
        self.import_btn.clicked.connect(self._do_import)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(self.import_btn)
        layout.addLayout(btn_layout)

    # ------------------------------------------------------------------
    # 文件选择 / 识别
    # ------------------------------------------------------------------

    def _browse(self) -> None:
        """多选导入文件并启动识别"""
        if self._thread is not None:
            return
        filters = ("数据文件 (*.csv *.xlsx);;CSV 文件 (*.csv);;"
                   "Excel 文件 (*.xlsx)")
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择要导入的文件（可多选）", "", filters)
        if not paths:
            return
        self._start_identify(paths)

    def _start_identify(self, paths: list[str]) -> None:
        """对给定文件路径启动后台识别（浏览与拖拽共用）"""
        # 去重（已在列表中的跳过）
        new_paths = [p for p in paths if p not in self._items]
        if not new_paths:
            return

        self._paths.extend(new_paths)
        self.browse_btn.setEnabled(False)
        self.import_btn.setEnabled(False)
        self.file_label.setText(f"已选择 {len(self._paths)} 个文件，识别中...")
        self._populate_rows()

        # 启动识别线程
        self.progress.setVisible(True)
        # ★ 确定进度：按文件数推进（每个文件完成 +1），避免"看起来卡住"
        self.progress.setRange(0, len(self._paths))
        self.progress.setValue(len(self._paths) - len(new_paths))

        self._thread = QThread(self)
        # ★ 关键修复：worker 存为 self._worker 防止被 GC，
        #   否则 started.connect(worker.run) 槽断开 → batch_done 永不触发
        self._worker = BatchIdentifyWorker(
            self.repo, None, self.ai_client, new_paths, self.suggested_table,
            template_store=self._template_store)
        worker = self._worker
        worker.moveToThread(self._thread)

        worker.file_done.connect(self._on_file_done)
        worker.batch_done.connect(self._on_batch_done)
        self._thread.started.connect(worker.run)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

        # ★ 识别进行中禁用目标表下拉：防止误触触发主线程全表分析 →「无响应」
        self._set_combos_enabled(False)

    # ------------------------------------------------------------------
    # 拖拽上传
    # ------------------------------------------------------------------

    def dragEnterEvent(self, event) -> None:
        """接受拖入的文件（仅支持 csv/xlsx/xls）"""
        if self._thread is not None:
            return
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            if any(self._is_supported(u.toLocalFile()) for u in urls):
                event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        """拖入文件 → 追加到导入列表并启动识别"""
        paths = [u.toLocalFile() for u in event.mimeData().urls()]
        supported = [p for p in paths if self._is_supported(p)]
        if not supported:
            self.file_label.setText("不支持的文件类型，请拖入 CSV / Excel")
            self.file_label.setStyleSheet("color: #c62828;")
            return
        # 过滤掉已导入/正在识别的
        new_paths = [p for p in supported if p not in self._items]
        if not new_paths:
            self.file_label.setText("这些文件已在导入列表中")
            return
        self.file_label.setStyleSheet("color: #e0e0e0;")
        self._start_identify(new_paths)

    @staticmethod
    def _is_supported(path: str) -> bool:
        """判断文件扩展名是否受支持"""
        return os.path.splitext(path)[1].lower() in IMPORTERS

    def _populate_rows(self) -> None:
        """刷新文件列表行"""
        self.file_table.setRowCount(len(self._paths))
        for row, path in enumerate(self._paths):
            item = self._items.get(path)
            base = os.path.basename(path)
            self.file_table.setItem(row, 0, QTableWidgetItem(base))
            # 目标表下拉
            combo = QComboBox()
            combo.addItems([""] + self._table_names)
            if item and item.table_name:
                idx = combo.findText(item.table_name)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
            combo.currentTextChanged.connect(
                lambda text, p=path: self._on_table_changed(p, text))
            self.file_table.setCellWidget(row, 1, combo)
            self.file_table.setItem(row, 2, QTableWidgetItem(""))
            self.file_table.setItem(row, 3, QTableWidgetItem(""))
            self.file_table.setItem(row, 4, QTableWidgetItem(""))
            self.file_table.setItem(row, 5, QTableWidgetItem("待识别"))

    def _on_file_done(self, path: str, item: FileItem) -> None:
        """单个文件识别完成，更新对应行 + 推进进度条"""
        self._items[path] = item
        if path not in self._paths:
            self._paths.append(path)
        row = self._paths.index(path)
        self._update_row(row)
        # 已完成的文件数（含本次）
        done = sum(1 for p in self._paths if p in self._items)
        self.progress.setValue(done)

    def _on_batch_done(self) -> None:
        """全部识别完成"""
        self.browse_btn.setEnabled(True)
        self.progress.setValue(self.progress.maximum())
        self.progress.setVisible(False)
        ready = [i for i in self._items.values() if i.status.startswith("就绪")]
        errors = [i for i in self._items.values() if i.status == "错误"]
        self.file_label.setText(
            f"{len(ready)} 个文件识别完成"
            + (f"，{len(errors)} 个失败" if errors else ""))
        if ready:
            self.import_btn.setEnabled(True)
        # ★ 提升 worker 已加载的表快照/重复计数，供识别后手动改表复用
        #   （避免对大数据表再次全表加载 → 主线程卡死）。worker.run 已结束，
        #   主线程此刻读其属性无并发风险。
        if self._worker is not None:
            self._table_snapshots = getattr(self._worker, "_table_snapshots", {}) or {}
            self._dup_counts = getattr(self._worker, "_dup_counts", {}) or {}
        # 识别完成 → 重新启用目标表下拉
        self._set_combos_enabled(True)
        # 清理线程与 worker 引用
        if self._thread:
            self._thread.quit()
            self._thread = None
        self._worker = None

    def _set_combos_enabled(self, enabled: bool) -> None:
        """统一启用/禁用所有目标表下拉

        识别进行中禁用 → 用户误触不会触发主线程全表分析（避免「无响应」）。
        """
        for row in range(self.file_table.rowCount()):
            combo = self.file_table.cellWidget(row, 1)
            if isinstance(combo, QComboBox):
                combo.setEnabled(enabled)

    def _update_row(self, row: int) -> None:
        """刷新某行的识别结果"""
        path = self._paths[row]
        item = self._items.get(path)
        if not item:
            return
        self.file_table.setItem(row, 0, QTableWidgetItem(os.path.basename(path)))
        # 目标表下拉（保留当前选择）
        combo = self.file_table.cellWidget(row, 1)
        if combo and item.table_name:
            idx = combo.findText(item.table_name)
            if idx >= 0:
                combo.blockSignals(True)
                combo.setCurrentIndex(idx)
                combo.blockSignals(False)

        conf_text = f"{item.confidence:.0%}" if item.confidence else ""
        method_text = {"rules": "规则", "ai": "AI", "template": "模板"}.get(item.method, "")
        self.file_table.setItem(row, 2, QTableWidgetItem(
            f"{conf_text} {method_text}" if conf_text else ""))

        a = item.analysis or {}
        self.file_table.setItem(row, 3, QTableWidgetItem(str(a.get("to_insert", ""))))
        self.file_table.setItem(row, 4, QTableWidgetItem(str(a.get("to_update", ""))))

        status = item.status
        if item.error:
            status = f"错误: {item.error[:30]}"
        elif item.low_confidence:
            status += " ⚠低置信"
        self.file_table.setItem(row, 5, QTableWidgetItem(status))

    def _on_table_changed(self, path: str, table_name: str) -> None:
        """用户手动改目标表 → 同步重算列映射 + 影响估算（不调 AI）

        识别进行中忽略下拉变更（下拉已禁用，此为兜底）——否则主线程
        全表分析会把界面卡成「无响应」。
        """
        if self._thread is not None:
            return
        item = self._items.get(path)
        if not item or not table_name:
            return
        from src.importer.column_mapper import map_columns
        from src.core.unique_key import infer_unique_cols_by_table
        from src.importer.matcher import detect_fund_code, FUND_TABLE, FUND_CANONICAL_COLUMNS

        item.table_name = table_name
        table_cols = self.repo.get_all_existing_columns(table_name)
        if not table_cols and table_name == FUND_TABLE:
            # 基金表尚未创建 → 用规范列映射，并确保注入 code 列
            table_cols = list(FUND_CANONICAL_COLUMNS)
            if "code" not in item.df.columns:
                code = detect_fund_code(path)
                if code:
                    item.df["code"] = code
        item.plan = map_columns(list(item.df.columns), table_cols)  # 纯规则
        item.unique_key = infer_unique_cols_by_table(table_name)
        mapped_df = item.plan.apply(item.df)
        # ★ 复用识别阶段已加载的表快照，避免再次全表加载（大数据表会卡主线程）
        existing_df = self._table_snapshots.get(table_name)
        dup_in_db = None
        if existing_df is not None:
            try:
                cols = self.repo.resolve_date_columns(
                    table_name, item.unique_key or [])
                dup_in_db = self._dup_counts.get((table_name, tuple(cols)))
            except Exception:
                dup_in_db = None
        item.analysis = self.repo.analyze_import(
            table_name, mapped_df, item.unique_key or None,
            existing_df=existing_df, dup_in_db=dup_in_db)
        item.status = "就绪"
        item.low_confidence = False

        # ★ 用户手动指定目标表 → 记忆该表头 → 目标表，下次同表头直接命中模板
        if self._template_store is not None and item.df is not None:
            self._template_store.learn(
                list(item.df.columns), table_name,
                unique_key=item.unique_key, mapping=item.plan.mapping,
                source="user")

        row = self._paths.index(path)
        self._update_row(row)
        self._update_impact_label(path)
        # 有就绪项即可导入
        if any(i.status.startswith("就绪") for i in self._items.values()):
            self.import_btn.setEnabled(True)

    def _on_selection_changed(self) -> None:
        """选中行 → 预览该文件 + 影响估算"""
        row = self.file_table.currentRow()
        if row < 0 or row >= len(self._paths):
            return
        path = self._paths[row]
        item = self._items.get(path)
        if not item or item.df is None:
            return
        self._load_preview(item.df)
        self._update_impact_label(path)

    def _load_preview(self, df: pd.DataFrame) -> None:
        """预览前 50 行"""
        MAX_ROWS = 50
        preview = df.head(MAX_ROWS)
        self.preview.clear()
        self.preview.setColumnCount(len(preview.columns))
        self.preview.setHorizontalHeaderLabels([str(c) for c in preview.columns])
        self.preview.setRowCount(len(preview))
        for r in range(len(preview)):
            for c in range(len(preview.columns)):
                val = preview.iloc[r, c]
                text = "" if pd.isna(val) else str(val)
                self.preview.setItem(r, c, QTableWidgetItem(text))
        if len(df) > MAX_ROWS:
            self.impact_label.setText(f"预览前 {MAX_ROWS} 行（共 {len(df)} 行）")

    def _clear_templates(self) -> None:
        """清除学习到的表头模板（手动纠正错误记忆的出口）"""
        if self._template_store is None:
            return
        ret = QMessageBox.question(
            self, "清除表头记忆",
            "将删除所有学习到的「表头→目标表」绑定。\n"
            "之后同一表头需重新识别（可手动选择目标表后自动重新记忆）。\n是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret == QMessageBox.StandardButton.Yes:
            self._template_store.clear()

    def _update_impact_label(self, path: str) -> None:
        """更新影响估算 + 新字段提示"""
        item = self._items.get(path)
        if not item:
            return
        a = item.analysis or {}
        lines = [
            f"[{os.path.basename(path)}] → {item.table_name}",
            f"待导入 {a.get('total', 0)} 行  |  新增 {a.get('to_insert', 0)}  "
            f"|  更新 {a.get('to_update', 0)}"
        ]
        if a.get("to_skip"):
            lines.append(f"文件内重复跳过: {a['to_skip']} 行")
        if a.get("dup_in_db"):
            lines.append(f"⚠ 库内现存重复: {a['dup_in_db']} 行（导入时自动清理）")
        if item.plan and item.plan.new_columns:
            lines.append(
                f"⚠ 检测到新字段: {'、'.join(item.plan.new_columns)}（导入时询问是否添加）")
        self.impact_label.setText("\n".join(lines))

    # ------------------------------------------------------------------
    # 导入执行
    # ------------------------------------------------------------------

    def _do_import(self) -> None:
        """确认后批量导入"""
        ready = [i for i in self._items.values() if i.status.startswith("就绪")]
        if not ready:
            QMessageBox.warning(self, "提示", "没有可导入的文件")
            return

        # 1. 汇总确认 — 用可滚动确认框，文件再多也能滚着看完、确定键始终可点
        from src.gui.dialogs.confirm_list_dialog import ConfirmListDialog
        lines = []
        for it in ready:
            a = it.analysis or {}
            new_note = f"（+{len(it.plan.new_columns)}新字段）" if it.plan.new_columns else ""
            lines.append(
                f"  • {os.path.basename(it.path)} → {it.table_name}"
                f"  新增{a.get('to_insert', 0)}/更新{a.get('to_update', 0)}{new_note}")
        dlg = ConfirmListDialog(
            self, "确认导入",
            summary=f"将导入 {len(ready)} 个文件，是否继续？",
            lines=lines,
            confirm_text="导入",
        )
        if dlg.exec() != QDialog.Accepted:
            return

        # 2. 新字段逐个确认
        from src.core.dynamic_schema import DynamicSchemaManager
        schema = DynamicSchemaManager(self.repo)
        for it in ready:
            if not it.plan or not it.plan.new_columns:
                continue
            ret = QMessageBox.question(
                self, "新字段",
                f"文件 [{os.path.basename(it.path)}] 检测到新字段：\n"
                f"  {'、'.join(it.plan.new_columns)}\n\n"
                f"是否添加到表 [{it.table_name}]？\n"
                f"（选「否」则跳过这些字段不导入）",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if ret == QMessageBox.StandardButton.Yes:
                schema.ensure_columns(it.table_name, it.plan.new_columns)
            else:
                it.plan.skipped = list(it.plan.new_columns)

        # 3. 逐个导入
        self.import_btn.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, len(ready))

        total_processed = 0
        ok_files = 0
        # 本批按表头签名去重，只记忆一次（AI 识别被确认 → 下次同表头不再调 AI）
        learned_sigs: set = set()
        for idx, it in enumerate(ready):
            try:
                df = it.plan.apply(it.df)
                schema.ensure_table_exists(it.table_name, list(df.columns))
                added = self.repo.bulk_upsert(
                    it.table_name, df,
                    unique_columns=it.unique_key or None, batch_size=500)
                total_processed += added
                ok_files += 1
                if (self._template_store is not None
                        and it.method == "ai"
                        and it.df is not None):
                    from src.importer.header_template import header_name_signature
                    sig = header_name_signature(it.df.columns)
                    if sig not in learned_sigs:
                        learned_sigs.add(sig)
                        self._template_store.learn(
                            list(it.df.columns), it.table_name,
                            unique_key=it.unique_key, mapping=it.plan.mapping,
                            source="ai")
            except Exception as exc:
                QMessageBox.critical(
                    self, "导入失败",
                    f"文件 [{os.path.basename(it.path)}] 导入失败: {exc}")
            self.progress.setValue(idx + 1)

        self.progress.setVisible(False)
        self.import_btn.setEnabled(True)
        QMessageBox.information(
            self, "导入完成",
            f"成功导入 {ok_files}/{len(ready)} 个文件，共写入 {total_processed} 行")
        self.accept()

    # ------------------------------------------------------------------
    # 关闭清理
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        """关闭时停止识别线程，避免后台线程悬挂"""
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(2000)  # 最多等 2 秒
            self._thread = None
        self._worker = None
        super().closeEvent(event)
