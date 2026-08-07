"""主窗口 — 组装所有子组件（Phase 2: 动态参数/API查询/参数记忆）"""

import sys
import os
import queue
from typing import Any
from sqlalchemy import text

import pandas as pd
from PySide6.QtCore import QObject, QThread, Signal, Qt, QTimer
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QLabel, QDialog, QMessageBox,
    QFileDialog, QApplication, QProgressBar, QFrame,
)
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QSystemTrayIcon, QMenu, QStatusBar

from src.utils.config import ConfigManager
from src.utils.logger import LoggerFactory, logger
from src.db.engine import get_engine
from src.db.repository import DataRepository
from src.core.data_fetcher import DataFetcher
from src.core.sync_engine import SyncEngine
from src.core.fetcher_registry import FetcherRegistry
from src.core.dynamic_schema import DynamicSchemaManager
from src.core.scheduler import DataScheduler
from src.api.server import FastAPIServer

from src.gui.tree_widget import DataTreeWidget
from src.gui.table_view import DataTableView, sort_df_by_date_desc
from src.gui.log_widget import LogWidget
from src.gui.param_panel import ParamPanel
from src.gui.init_wizard import InitWizard
from src.gui.dialogs.about_dialog import show_about_dialog
from src.gui.dialogs.settings_dialog import SettingsDialog
from src.gui.dialogs.schedule_dialog import ScheduleDialog
from src.gui.dialogs.export_dialog import ExportDialog
from src.gui.dialogs.health_dialog import HealthDialog
from src.gui.dialogs.indicator_manager_dialog import IndicatorManagerDialog


# ---------------------------------------------------------------------------
# 后台工作线程
# ---------------------------------------------------------------------------


class SyncWorker(QObject):
    """单个数据源同步工作器"""

    finished = Signal()
    error = Signal(str)

    def __init__(self, sync_engine: SyncEngine, source_key: str, history_years: int = 3):
        super().__init__()
        self.sync_engine = sync_engine
        self.source_key = source_key
        self.history_years = history_years

    def run(self) -> None:
        try:
            self.sync_engine.run(self.source_key, self.history_years)
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            self.finished.emit()


def _build_sync_jobs(
    source_keys: list[str],
    src: dict | None,
    repo,
    param_panel,
    local_code_column_fn,
) -> list[tuple[str, dict | None]]:
    """构建同步计划 [(source_key, params_override), ...]

    对「含多个代码」的数据源（基金/股票/指数）：
    - 本地筛选下拉选了具体代码 → 只同步该代码
    - 选「全部」→ 遍历表中已有的每个代码逐个同步（每次一个 job）
    - 无 code 列 / 表无数据 → 用输入框/默认代码同步一次（params=None）

    Returns
    -------
    list[tuple[str, dict | None]]
        (source_key, params_override) 列表
    """
    if not source_keys or not src:
        return [(k, None) for k in source_keys]

    pt = src.get("params_template") or {}
    code_param = next((k for k in ("ts_code", "symbol", "code") if k in pt), None)
    code_col = local_code_column_fn(src.get("table_name")) if src.get("table_name") else None
    if not code_param or not code_col:
        # 非代码型源 → 每源同步一次（默认代码）
        return [(k, None) for k in source_keys]

    def _mk_params(code: str) -> dict:
        if code_param == "ts_code":
            from src.core.sync_engine import normalize_ts_code
            return {"ts_code": normalize_ts_code(code)}
        return {code_param: code}

    local_code = param_panel.getSelectedLocalCode()
    if local_code:
        # 下拉选了具体代码 → 每个源同步该代码
        p = _mk_params(local_code)
        return [(k, p) for k in source_keys]

    # 选「全部」→ 遍历表中已有代码
    codes = repo.get_distinct_values(src["table_name"], code_col)
    if not codes:
        return [(k, None) for k in source_keys]
    return [(k, _mk_params(c)) for c in codes for k in source_keys]


class SyncListWorker(QObject):
    """批量同步工作器 — 按同步计划列表顺序执行

    单个数据源失败不中断后续；每个源的结果仍通过
    SyncEngine 的信号（sync_completed / sync_error）上报。
    """

    finished = Signal()
    error = Signal(str)

    def __init__(self, sync_engine: SyncEngine, jobs: list[tuple[str, dict | None]],
                 history_years: int = 3):
        super().__init__()
        self.sync_engine = sync_engine
        self.jobs = jobs  # [(source_key, params_override), ...]
        self.history_years = history_years

    def run(self) -> None:
        try:
            for key, params in self.jobs:
                try:
                    self.sync_engine.run(
                        key, self.history_years, params_override=params or None)
                except Exception as exc:
                    self.error.emit(f"{key}: {exc}")
        finally:
            self.finished.emit()


class SyncAllWorker(QObject):
    """全量同步工作器"""

    finished = Signal()
    error = Signal(str)

    def __init__(self, sync_engine: SyncEngine):
        super().__init__()
        self.sync_engine = sync_engine

    def run(self) -> None:
        try:
            self.sync_engine.run_all()
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            self.finished.emit()


class SyncFundBatchWorker(QObject):
    """基金日线批量同步工作器 — 按交易日补全市场（替代逐个 code）"""

    finished = Signal()
    error = Signal(str)

    def __init__(self, sync_engine: SyncEngine):
        super().__init__()
        self.sync_engine = sync_engine

    def run(self) -> None:
        try:
            self.sync_engine.run_fund_daily_batch()
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            self.finished.emit()


class QueryWorker(QObject):
    """后台查询工作器 — 只查数据库"""

    finished = Signal(pd.DataFrame)
    error = Signal(str)

    def __init__(self, repo: DataRepository, table_name: str, filters: dict = None,
                 date_from: str = None, date_to: str = None, limit: int = 5000):
        super().__init__()
        self.repo = repo
        self.table_name = table_name
        self.filters = filters
        self.date_from = date_from
        self.date_to = date_to
        self.limit = limit

    def run(self) -> None:
        try:
            df = self.repo.query(
                self.table_name, filters=self.filters, limit=self.limit,
                date_from=self.date_from, date_to=self.date_to)
            # ★ 后台线程完成日期倒序排序，主线程只做 model reset（省 ~100ms/次）
            df = sort_df_by_date_desc(df)
            self.finished.emit(df)
        except Exception as exc:
            self.error.emit(str(exc))


class ApiFetchWorker(QObject):
    """实时 API 获取工作器 — 线程内独立创建 DataFetcher"""

    finished = Signal(pd.DataFrame)
    error = Signal(str)

    def __init__(self, source_cfg: dict, params: dict):
        super().__init__()
        self.source_cfg = source_cfg
        self.params = params

    def run(self) -> None:
        try:
            from src.core.data_fetcher import DataFetcher
            from src.utils.config import ConfigManager
            fetcher = DataFetcher(ConfigManager())
            df = fetcher.fetch(self.source_cfg, self.params)
            self.finished.emit(df if df is not None else pd.DataFrame())
        except Exception as exc:
            self.error.emit(str(exc))


class TestWorker(QObject):
    """API 连接测试工作器 — 线程内独立创建 DataFetcher"""

    finished = Signal(dict)

    def run(self) -> None:
        import time
        from src.core.data_fetcher import DataFetcher
        from src.utils.config import ConfigManager
        fetcher = DataFetcher(ConfigManager())

        test_cases = [
            ("macro_china_cpi", {}, "中国CPI"),
            ("stock_zh_a_hist", {"symbol": "000001", "period": "daily",
                                 "start_date": "20260701", "end_date": "20260728"}, "A股日线"),
        ]

        results = []
        for func_name, params, label in test_cases:
            start = time.time()
            try:
                cfg = {"api_source": "akshare", "api_function": func_name}
                df = fetcher.fetch(cfg, params)
                elapsed = time.time() - start
                if df is not None and not df.empty:
                    results.append(dict(name=label, func=func_name, ok=True,
                                        elapsed=round(elapsed, 1),
                                        message=f"{len(df)}行×{len(df.columns)}列"))
                else:
                    results.append(dict(name=label, func=func_name, ok=True,
                                        elapsed=round(elapsed, 1),
                                        message="返回空数据"))
            except Exception as e:
                elapsed = time.time() - start
                results.append(dict(name=label, func=func_name, ok=False,
                                    elapsed=round(elapsed, 1),
                                    message=f"{type(e).__name__}: {str(e)[:80]}"))

        self.finished.emit({"results": results})


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------


class MainWindow(QMainWindow):
    """Bern_Financial_Data 主窗口"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Bern_Financial_Data - 金融数据中台")
        self.resize(1280, 800)

        # ----------------------------------------------------------------
        # 初始化依赖
        # ----------------------------------------------------------------
        self.config = ConfigManager()
        self.engine = get_engine()
        self.repo = DataRepository(self.engine)
        self.registry = FetcherRegistry(self.config)
        self.fetcher = DataFetcher(self.config)
        self.schema_mgr = DynamicSchemaManager(self.repo)
        self.sync_engine = SyncEngine(
            self.fetcher, self.repo, self.schema_mgr, self.config
        )

        # 参数记忆: {source_key: {field: value, ...}}
        self._param_memory: dict[str, dict] = {}

        # 当前选中的 source_key
        self._current_source_key: str | None = None

        # 后台线程引用（防止GC）
        self._threads: list[QThread] = []

        # ★ 表格刷新防抖：同步期间每个 source/code 完成都会触发刷新，
        #   用 300ms 单发 timer 合并多次请求，只在空闲后刷一次，且查询放后台
        #   线程——避免主线程被 repo.query + 全量 model reset 阻塞导致 UI 无响应
        self._refresh_in_progress = False
        self._refresh_pending = False
        # 全量同步进行中标记：控制状态栏进度条的显示/隐藏（on_sync_completed
        #   只在不属于 run_all 的普通同步完成时隐藏进度条）
        self._sync_all_active = False
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(300)
        self._refresh_timer.timeout.connect(self._do_refresh_current_table)

        # ★ 跨线程结果队列
        self._result_queue: queue.Queue = queue.Queue()
        self._result_timer = QTimer(self)
        self._result_timer.timeout.connect(self._process_result_queue)
        self._result_timer.start(200)  # 每 200ms 轮询一次

        # ★ 定时调度引擎
        self.scheduler = DataScheduler()

        # ★ FastAPI 本地数据服务（注入调度器，供 /health 上报状态）
        self.api_server = FastAPIServer(repo=self.repo, scheduler=self.scheduler)
        self._api_timer = QTimer(self)
        self._api_timer.timeout.connect(self._update_api_status)
        self._api_timer.start(2000)  # 每 2 秒刷新连接数

        # ----------------------------------------------------------------
        # 构建 UI
        # ----------------------------------------------------------------
        self._init_central_widget()
        self._init_menu_bar()
        self._init_signals()
        self._init_status_bar()
        self._init_system_tray()
        self._init_scheduler()

        # 注册 GUI 日志回调
        LoggerFactory.set_gui_callback(self.log_widget.write)

    # ------------------------------------------------------------------
    # 中心控件
    # ------------------------------------------------------------------

    def _init_central_widget(self) -> None:
        """构建中心区域：左侧树 | 右侧参数+表格+日志"""
        central = QWidget()
        self.setCentralWidget(central)

        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)

        splitter = QSplitter(Qt.Orientation.Horizontal, central)

        # 左侧：数据分类树
        self.tree_widget = DataTreeWidget(self.registry)
        splitter.addWidget(self.tree_widget)

        # 右侧：参数面板 + 表格 + 日志
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(4, 4, 4, 4)
        right_layout.setSpacing(4)

        self.param_panel = ParamPanel()
        # 数据使用警告条（默认隐藏）：fund_etf_daily 等含早期封闭式/LOF 的表
        # 加载时提示，防止下游按 ETF 口径误用。非模态 QFrame 常驻布局，
        # show/hide 由 _update_data_warning 按当前源 data_warning 字段控制。
        self._data_warning_bar = QFrame()
        self._data_warning_bar.setObjectName("dataWarningBar")
        self._data_warning_bar.setStyleSheet(
            "QFrame#dataWarningBar { background: #fff3cd;"
            " border: 1px solid #ffc107; border-radius: 3px; }"
            "QLabel { color: #8a6d00; font-size: 12px; padding: 2px 8px; }")
        self._data_warning_bar.setMaximumHeight(40)  # 限高，防止把表格压得太靠下
        bar_layout = QHBoxLayout(self._data_warning_bar)
        bar_layout.setContentsMargins(4, 2, 4, 2)
        self._data_warning_label = QLabel()
        self._data_warning_label.setWordWrap(True)
        bar_layout.addWidget(self._data_warning_label)
        self._data_warning_bar.setVisible(False)

        self.table_view = DataTableView()
        self.log_widget = LogWidget()
        self.log_widget.setMaximumHeight(180)

        right_layout.addWidget(self.param_panel)
        right_layout.addWidget(self._data_warning_bar)
        right_layout.addWidget(self.table_view, stretch=1)
        right_layout.addWidget(self.log_widget)

        splitter.addWidget(right_panel)

        # 设置初始比例
        splitter.setSizes([250, 1030])

        main_layout.addWidget(splitter)

    # ------------------------------------------------------------------
    # 菜单栏
    # ------------------------------------------------------------------

    def _init_menu_bar(self) -> None:
        menu_bar = self.menuBar()

        # ---- 文件 ----
        file_menu = menu_bar.addMenu("文件")

        self.import_data_action = QAction("导入数据...", self)
        self.import_data_action.triggered.connect(self._open_import_dialog)
        file_menu.addAction(self.import_data_action)

        file_menu.addSeparator()

        self.export_csv_action = QAction("导出 CSV", self)
        self.export_csv_action.triggered.connect(self._export_csv)
        file_menu.addAction(self.export_csv_action)

        self.export_excel_action = QAction("导出 Excel", self)
        self.export_excel_action.triggered.connect(self._export_excel)
        file_menu.addAction(self.export_excel_action)

        self.export_pdf_action = QAction("导出 PDF", self)
        self.export_pdf_action.triggered.connect(self._export_pdf)
        file_menu.addAction(self.export_pdf_action)

        file_menu.addSeparator()

        self.exit_action = QAction("退出", self)
        self.exit_action.setShortcut("Ctrl+Q")
        self.exit_action.triggered.connect(self.close)
        file_menu.addAction(self.exit_action)

        # ---- 数据 ----
        data_menu = menu_bar.addMenu("数据")

        self.sync_current_action = QAction("同步当前数据源", self)
        self.sync_current_action.triggered.connect(self.on_sync_current)
        data_menu.addAction(self.sync_current_action)

        self.sync_all_action = QAction("同步所有数据源", self)
        self.sync_all_action.triggered.connect(self.on_sync_all)
        data_menu.addAction(self.sync_all_action)

        data_menu.addSeparator()

        self.init_wizard_action = QAction("初始化向导", self)
        self.init_wizard_action.triggered.connect(self.on_init_wizard)
        data_menu.addAction(self.init_wizard_action)

        data_menu.addSeparator()

        self.schedule_mgr_action = QAction("⏰ 定时任务管理", self)
        self.schedule_mgr_action.triggered.connect(self._show_schedule_manager)
        data_menu.addAction(self.schedule_mgr_action)

        data_menu.addSeparator()

        self.health_check_action = QAction("🩺 数据源健康检查", self)
        self.health_check_action.triggered.connect(self._show_health_check)
        data_menu.addAction(self.health_check_action)

        self.indicator_mgr_action = QAction("📊 指标管理中心", self)
        self.indicator_mgr_action.triggered.connect(self._show_indicator_manager)
        data_menu.addAction(self.indicator_mgr_action)

        data_menu.addSeparator()

        self.scrape_all_action = QAction("🕷 抓取所有数据源", self)
        self.scrape_all_action.triggered.connect(self._on_scrape_all)
        data_menu.addAction(self.scrape_all_action)

        self.scrape_mgr_action = QAction("🕸 抓取规则管理", self)
        self.scrape_mgr_action.triggered.connect(self._show_scrape_manager)
        data_menu.addAction(self.scrape_mgr_action)

        data_menu.addSeparator()

        self.catalog_mgr_action = QAction("🗂 数据分类管理", self)
        self.catalog_mgr_action.triggered.connect(self._show_catalog_editor)
        data_menu.addAction(self.catalog_mgr_action)

        # ---- 视图 ----
        view_menu = menu_bar.addMenu("视图")

        self.expand_all_action = QAction("展开全部", self)
        self.expand_all_action.triggered.connect(
            lambda: self.tree_widget.expandAll()
        )
        view_menu.addAction(self.expand_all_action)

        self.collapse_all_action = QAction("折叠全部", self)
        self.collapse_all_action.triggered.connect(
            lambda: self.tree_widget.collapseAll()
        )
        view_menu.addAction(self.collapse_all_action)

        # ---- 工具 ----
        tool_menu = menu_bar.addMenu("工具")

        self.api_start_action = QAction("▶ 启动 API 服务", self)
        self.api_start_action.triggered.connect(self._start_api_server)
        tool_menu.addAction(self.api_start_action)

        self.api_stop_action = QAction("⏹ 停止 API 服务", self)
        self.api_stop_action.triggered.connect(self._stop_api_server)
        tool_menu.addAction(self.api_stop_action)

        self.api_restart_action = QAction("🔄 重启 API 服务", self)
        self.api_restart_action.triggered.connect(self._restart_api_server)
        tool_menu.addAction(self.api_restart_action)

        tool_menu.addSeparator()

        self.api_conn_action = QAction("👁 API 连接监控", self)
        self.api_conn_action.triggered.connect(self._show_api_connections)
        tool_menu.addAction(self.api_conn_action)

        tool_menu.addSeparator()

        self.analyze_action = QAction("🤖 AI 智能分析", self)
        self.analyze_action.triggered.connect(self._show_ai_analyze)
        tool_menu.addAction(self.analyze_action)

        # ---- 帮助 ----
        help_menu = menu_bar.addMenu("帮助")

        self.settings_action = QAction("设置", self)
        self.settings_action.triggered.connect(self._show_settings)
        help_menu.addAction(self.settings_action)

        help_menu.addSeparator()

        self.about_action = QAction("关于", self)
        self.about_action.triggered.connect(
            lambda: show_about_dialog(self)
        )
        help_menu.addAction(self.about_action)

    # ------------------------------------------------------------------
    # 信号连接
    # ------------------------------------------------------------------

    def _init_signals(self) -> None:
        """连接各组件信号"""
        self.tree_widget.itemSelected.connect(self.on_tree_node_selected)

        self.param_panel.queryClicked.connect(self.on_query)
        self.param_panel.syncClicked.connect(self.on_sync_current)
        self.param_panel.updateMethodSelected.connect(self.on_update_method)
        self.param_panel.initClicked.connect(self.on_init_wizard)
        self.param_panel.testClicked.connect(self._on_test_connection)
        self.param_panel.trustClicked.connect(self._on_trust_clicked)
        self.param_panel.export_csv_action.triggered.connect(self._open_export_dialog)
        self.param_panel.export_excel_action.triggered.connect(self._open_export_dialog)
        self.param_panel.export_pdf_action.triggered.connect(self._open_export_dialog)
        self.tree_widget.scheduleToggled.connect(self.on_schedule_toggle)
        self.tree_widget.trustRequested.connect(self._on_tree_trust_requested)

        self.sync_engine.sync_completed.connect(self.on_sync_completed)
        self.sync_engine.sync_error.connect(self.on_sync_error)
        self.sync_engine.sync_progress.connect(self._on_sync_progress)
        self.sync_engine.log_message.connect(self.log_widget.write)

    # ------------------------------------------------------------------
    # 状态栏
    # ------------------------------------------------------------------

    def _init_status_bar(self) -> None:
        status_bar = self.statusBar()
        status_bar.showMessage("就绪", 5000)

        # 全量同步进度条（默认隐藏，run_all 期间按 sync_progress 信号推进）。
        # ★ 必须用 addPermanentWidget：QStatusBar 显示临时消息(showMessage)时
        #   会隐藏 addWidget 添加的普通控件，常驻控件不受影响，否则进度条
        #   在消息出现时被压掉永远看不见。
        self.sync_progress_bar = QProgressBar()
        self.sync_progress_bar.setFixedWidth(160)
        self.sync_progress_bar.setTextVisible(False)
        self.sync_progress_bar.setVisible(False)
        status_bar.addPermanentWidget(self.sync_progress_bar)

        self.row_count_label = QLabel("行数: 0")
        status_bar.addPermanentWidget(self.row_count_label)

        self.schedule_status_label = QLabel("⏰ 定时: 运行中")
        self.schedule_status_label.setStyleSheet("color: #2e7d32; padding: 0 8px;")
        status_bar.addPermanentWidget(self.schedule_status_label)

        self.api_status_label = QLabel("🌐 API: 启动中...")
        self.api_status_label.setStyleSheet("padding: 0 8px;")
        status_bar.addPermanentWidget(self.api_status_label)

    def _update_row_count(self, df: pd.DataFrame) -> None:
        """更新状态栏行数"""
        count = len(df)
        self.row_count_label.setText(f"行数: {count}")

    # ------------------------------------------------------------------
    # 跨线程结果队列
    # ------------------------------------------------------------------

    def _process_result_queue(self) -> None:
        """处理子线程返回的结果（主线程调用，由 QTimer 驱动）"""
        while True:
            try:
                callback = self._result_queue.get_nowait()
                callback()
            except queue.Empty:
                break

    # ------------------------------------------------------------------
    # 系统托盘
    # ------------------------------------------------------------------

    def _init_system_tray(self) -> None:
        """初始化系统托盘图标"""
        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(
            self.style().standardIcon(
                self.style().StandardPixmap.SP_ComputerIcon
            )
        )
        self.tray_icon.setToolTip("Bern_Financial_Data")

        tray_menu = QMenu(self)
        show_action = tray_menu.addAction("显示主窗口")
        show_action.triggered.connect(self.show)

        sync_action = tray_menu.addAction("同步所有")
        sync_action.triggered.connect(self.on_sync_all)

        schedule_menu = tray_menu.addMenu("⏰ 定时调度")
        self.tray_schedule_enable = schedule_menu.addAction("🟢 启用所有定时")
        self.tray_schedule_enable.triggered.connect(
            lambda: self._toggle_all_schedules(True))
        self.tray_schedule_disable = schedule_menu.addAction("🔴 暂停所有定时")
        self.tray_schedule_disable.triggered.connect(
            lambda: self._toggle_all_schedules(False))

        tray_menu.addSeparator()

        quit_action = tray_menu.addAction("退出")
        quit_action.triggered.connect(QApplication.instance().quit)

        self.tray_icon.setContextMenu(tray_menu)

        self.tray_icon.activated.connect(
            lambda reason: self.show()
            if reason == QSystemTrayIcon.ActivationReason.DoubleClick
            else None
        )

        self.tray_icon.show()

    # ------------------------------------------------------------------
    # 定时调度
    # ------------------------------------------------------------------

    def _init_scheduler(self) -> None:
        """初始化定时调度引擎"""
        categories = self.registry.get_categories()
        self.scheduler.register_category_jobs(categories, self._on_schedule_trigger)

        # ★ 注册抓取规则的定时任务（scrapers.yaml 中带 schedule 的）
        self._register_scraper_jobs()

        # ★ 从持久化文件恢复定时开关状态
        self._restore_schedule_state()

        self._save_schedule_state()  # 刷新文件

        self.scheduler.start()

        running = self.scheduler.is_running()
        job_count = len(self.scheduler.get_jobs())
        logger.info(f"定时调度: {'运行中' if running else '已停止'}, {job_count} 个任务")

        # ★ 自动启动 API 服务
        self._start_api_server()

    def _register_scraper_jobs(self) -> None:
        """注册抓取规则的定时任务（scrapers.yaml 中带 schedule 的）"""
        try:
            from src.scraper.engine import ScrapeEngine
            engine = ScrapeEngine(repo=self.repo)
            rules = engine.load_rules()
            count = 0
            for rule in rules:
                cron = rule.get("schedule", "").strip()
                name = rule.get("name", "")
                if not cron or rule.get("enabled") is False:
                    continue
                # 注册到调度器（回调在 APScheduler 线程中执行）
                job_id = self.scheduler.add_job(
                    f"scrape_{name}", cron, f"抓取[{name}]",
                    self._make_scrape_callback(engine, name))
                if job_id:
                    count += 1
            if count:
                logger.info(f"已注册 {count} 个抓取定时任务")
        except Exception as exc:
            logger.warning(f"注册抓取定时任务失败: {exc}")

    @staticmethod
    def _make_scrape_callback(engine, name: str):
        """构造抓取定时回调（返回可调用对象）"""
        from src.utils.logger import logger as _logger

        def _cb(*args, **kwargs):
            try:
                added = engine.run_by_name(name)
                _logger.info("定时抓取 [%s] 完成: %d 行", name, added)
            except Exception as exc:
                _logger.error("定时抓取 [%s] 失败: %s", name, exc)
        return _cb

    def _on_schedule_trigger(self, source_key: str, category_name: str) -> None:
        """定时任务触发回调（在 APScheduler 线程中运行）

        通过结果队列回到主线程执行同步
        """
        import threading
        from src.core.data_fetcher import DataFetcher
        from src.utils.config import ConfigManager

        def run_sync():
            try:
                fetcher = DataFetcher(ConfigManager())
                source = self.registry.get_source(source_key)
                if source:
                    from src.core.sync_engine import SyncEngine
                    from src.core.dynamic_schema import DynamicSchemaManager
                    from src.db.engine import get_engine
                    from src.db.repository import DataRepository
                    engine = get_engine()
                    repo = DataRepository(engine)
                    schema = DynamicSchemaManager(repo)
                    sync = SyncEngine(fetcher, repo, schema, self.config)
                    self._result_queue.put(
                        lambda: self.log_widget.write("INFO",
                            f"⏰ 定时任务 [{category_name}] 开始同步 {source_key}"))
                    sync.run(source_key)
                    self._result_queue.put(
                        lambda: (self.on_sync_completed(source_key, 0),
                                 self.log_widget.write("SUCCESS",
                                    f"⏰ 定时同步完成 [{category_name}]")))
            except Exception as e:
                self._result_queue.put(
                    lambda: self.log_widget.write("ERROR",
                        f"⏰ 定时同步失败 [{category_name}]: {e}"))

        t = threading.Thread(target=run_sync, daemon=True)
        t.start()

    def _schedule_state_path(self) -> str:
        """定时状态持久化文件路径"""
        return str(self.config.root_dir / "data" / "schedule_state.json")

    def _restore_schedule_state(self) -> None:
        """从文件恢复定时开关状态"""
        state_path = self._schedule_state_path()
        try:
            if not os.path.exists(state_path):
                return
            import json
            with open(state_path, "r", encoding="utf-8") as f:
                states = json.load(f)
            for cat_name, enabled in states.items():
                if enabled:
                    self.scheduler.enable_category(cat_name)
                else:
                    self.scheduler.disable_category(cat_name)
                self.tree_widget.refresh_schedule_icon(cat_name, enabled)
            logger.info(f"已恢复 {len(states)} 个分类的定时状态")
        except Exception as e:
            logger.debug(f"恢复定时状态失败（可忽略）: {e}")

    def _save_schedule_state(self) -> None:
        """保存定时开关状态到文件"""
        try:
            info = self.tree_widget.get_category_schedule_info()
            states = {c["name"]: c["schedule_enabled"] for c in info}
            import json
            state_path = self._schedule_state_path()
            os.makedirs(os.path.dirname(state_path), exist_ok=True)
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump(states, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.debug(f"保存定时状态失败（可忽略）: {e}")

    def on_schedule_toggle(self, category_name: str, enabled: bool) -> None:
        """切换板块的定时更新开关"""
        if enabled:
            self.scheduler.enable_category(category_name)
            self.log_widget.write("INFO",
                f"🟢 已启用 [{category_name}] 的定时更新")
        else:
            self.scheduler.disable_category(category_name)
            self.log_widget.write("INFO",
                f"🔴 已暂停 [{category_name}] 的定时更新")
        self.statusBar().showMessage(
            f"{'启用' if enabled else '暂停'} [{category_name}] 定时更新", 5000)
        self._save_schedule_state()

    def _toggle_all_schedules(self, enabled: bool) -> None:
        """启用/暂停所有分类的定时更新"""
        categories = self.tree_widget.get_category_schedule_info()
        for cat in categories:
            self.scheduler.set_category_enabled(cat["name"], enabled)
            self.tree_widget.refresh_schedule_icon(cat["name"], enabled)
        action = "启用" if enabled else "暂停"
        self.log_widget.write("INFO", f"已{action}所有板块的定时更新")
        self.statusBar().showMessage(f"已{action}所有定时任务", 5000)

    # ------------------------------------------------------------------
    # 事件处理
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        """关闭窗口时清理资源"""
        self.sync_engine.stop()
        self.scheduler.stop()
        self.api_server.stop()
        event.accept()

    # ------------------------------------------------------------------
    # ★ 树节点选择 — 动态参数 + 参数记忆
    # ------------------------------------------------------------------

    def _update_data_warning(self) -> None:
        """按当前数据源显示/隐藏数据使用警告条

        取 source 的 data_warning 字段（catalog 配置，见 docs/DATA_GOVERNANCE.md §5）。
        有 → 显示黄色警告条并限高；无 → 隐藏（hidden 控件不占布局空间，表格间距自动恢复）。
        """
        warning = ""
        if self._current_source_key:
            source = self.registry.get_source(self._current_source_key)
            warning = (source or {}).get("data_warning") or ""
        if warning:
            self._data_warning_label.setText(f"⚠️ {warning}")
            self._data_warning_bar.setVisible(True)
        else:
            self._data_warning_bar.setVisible(False)

    def on_tree_node_selected(self, source_key: str) -> None:
        """树节点选中：保存旧参数 → 加载新模板 → 恢复记忆参数 → 加载DB数据"""
        # 1. 保存当前参数到记忆
        if self._current_source_key and self._current_source_key != source_key:
            old_params = self.param_panel.getParams()
            if old_params:
                self._param_memory[self._current_source_key] = old_params

        self._current_source_key = source_key
        source = self.registry.get_source(source_key)
        self._update_data_warning()

        if not source:
            self.table_view.clear()
            self._update_row_count(pd.DataFrame())
            self.param_panel.load_template({})
            return

        # 2. 加载参数模板（动态渲染控件）
        params_template = source.get("params_template", {})
        self.param_panel.load_template(params_template)

        # 3. 恢复记忆参数（如果有）
        if source_key in self._param_memory:
            self.param_panel.setParams(self._param_memory[source_key])
        else:
            self.param_panel.clearParams()

        # 3.5 指标获信区：当前源有 indicator 键才显示，并展示已设获信源
        indicator_key = source.get("indicator", "")
        mapping = None
        if indicator_key:
            try:
                mapping = self.repo.get_indicator_map(indicator_key)
            except Exception:
                mapping = None
        self.param_panel.setIndicatorContext(indicator_key, mapping)

        # 4. 从数据库加载已有数据
        table_name = source.get("table_name")
        # 刷新「本地已有代码」下拉（读表去重）
        code_col = self._local_code_column(table_name) if table_name else None
        codes = self.repo.get_distinct_values(table_name, code_col) if (table_name and code_col) else []
        self.param_panel.setLocalCodes(codes)
        if table_name:
            try:
                df = self.repo.query(table_name, limit=5000)
                self.table_view.loadDataFrame(df)
                self._update_row_count(df)
                self.log_widget.write(
                    "INFO",
                    f"已加载 [{source_key}] 共 {len(df)} 条记录",
                )
            except Exception as exc:
                self.log_widget.write(
                    "ERROR",
                    f"加载数据失败 [{source_key}]: {exc}",
                )
                self.table_view.clear()
                self._update_row_count(pd.DataFrame())
        else:
            self.table_view.clear()
            self._update_row_count(pd.DataFrame())

    # ------------------------------------------------------------------
    # ★ 查询 — 有 api_function 就调 API 实时获取，支持日期过滤
    # ------------------------------------------------------------------

    def on_query(self) -> None:
        """查询本地表（纯本地，不联网）。联网获取改由「更新」承担。"""
        source_key = self._current_source_key
        if not source_key:
            self.log_widget.write("WARNING", "请先选择一个数据源")
            return

        source = self.registry.get_source(source_key)
        if not source:
            self.log_widget.write("WARNING", f"未知数据源: {source_key}")
            return

        # 获取参数（含常驻日期范围）
        params = self.param_panel.getParams()

        # ★ 日期验证：检查是否反向
        sd = params.get("start_date", "")
        ed = params.get("end_date", "")
        if sd and ed and sd > ed:
            self.log_widget.write("WARNING",
                f"日期范围有误: 开始日期 {sd} 晚于结束日期 {ed}，请调整后重试")
            self.statusBar().showMessage("日期范围有误: 开始日期不能晚于结束日期", 5000)
            return

        table_name = source.get("table_name")
        if not table_name:
            self.log_widget.write("WARNING", f"数据源 [{source_key}] 未配置表名")
            return

        # 代码筛选：优先本地代码下拉，其次输入的 ts_code/symbol/code 参数。
        # 注意：本地表 code 列存纯数字（如 510300），ts_code 常带 .SH/.SZ 后缀，
        # 查询前去掉后缀再匹配，否则键入 ts_code 无法命中本地数据。
        filters: dict = {}
        code_col = self._local_code_column(table_name)
        if code_col:
            local_code = self.param_panel.getSelectedLocalCode()
            if local_code:
                filters[code_col] = local_code
            else:
                for pkey in ("ts_code", "symbol", "code"):
                    if params.get(pkey):
                        filters[code_col] = self._strip_code_suffix(params[pkey])
                        break

        self.log_widget.write("INFO", f"正在查询本地表 [{table_name}] ...")
        self.statusBar().showMessage(f"查询本地: {source_key} ...")
        self._query_local_table(source_key, table_name, filters, sd, ed)

    def _local_code_column(self, table_name: str) -> str | None:
        """本地表里作为代码标识的列（code/symbol，取存在的那个）"""
        existing = self.repo.get_all_existing_columns(table_name)
        for c in ("code", "symbol"):
            if c in existing:
                return c
        return None

    @staticmethod
    def _strip_code_suffix(code: str) -> str:
        """去掉 tushare 代码交易所后缀：159001.SZ → 159001；原样返回非代码串"""
        from src.core.sync_engine import _strip_exchange
        return _strip_exchange(code)

    def _query_local_table(
        self,
        source_key: str,
        table_name: str,
        filters: dict,
        date_from: str,
        date_to: str,
    ) -> None:
        """在后台线程查询本地表（筛选 + 日期区间）"""
        thread = QThread(self)
        worker = QueryWorker(
            self.repo, table_name, filters,
            date_from=date_from or None, date_to=date_to or None, limit=5000)
        # ★ 防 GC：worker 是局部变量，函数返回后被回收会导致 started 连接失效
        #   （worker.run 永不执行）。挂到 thread 上保活，thread 由 self._threads 持有。
        thread._worker_ref = worker
        worker.moveToThread(thread)

        worker.finished.connect(
            lambda df: self._on_query_result(source_key, df))
        worker.error.connect(
            lambda err: self._on_api_error(source_key, err))
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        thread.started.connect(worker.run)
        thread.start()
        self._threads.append(thread)
        thread.finished.connect(lambda: self._cleanup_thread(thread))

    def _on_api_result(self, source_key: str, source_cfg: dict,
                       df: pd.DataFrame, params: dict) -> None:
        """API 实时获取结果处理（含客户端日期过滤）"""
        if df.empty:
            self.log_widget.write("WARNING",
                f"API 返回为空 [{source_key}]")
            self.statusBar().showMessage("查询完成: 无数据", 5000)
            return

        # ★ 客户端日期过滤（akShare 某些接口不支持 date 参数，返回全量数据）
        df_filtered = self._filter_by_date(df, params)

        rows_before = len(df)
        rows_after = len(df_filtered)

        # 显示到表格（显示过滤后的数据）
        self.table_view.loadDataFrame(df_filtered)
        self._update_row_count(df_filtered)

        cols = len(df_filtered.columns)
        status_msg = f"查询完成: {rows_after} 条"
        if rows_before != rows_after:
            status_msg += f" (API返回{rows_before}条，按日期过滤后{rows_after}条)"

        self.log_widget.write("SUCCESS",
            f"API 获取成功 [{source_key}] {status_msg}")
        self.statusBar().showMessage(status_msg, 8000)

        # 将 API 返回的全量数据缓存到数据库（而不是过滤后的）
        table_name = source_cfg.get("table_name")
        if table_name and not df.empty:
            try:
                # ★ 先确保表存在，再检查列
                self._ensure_data_table(table_name, df)
                self.schema_mgr.ensure_columns(
                    table_name, list(df.columns),
                    source_cfg.get("api_function", ""))
                unique_cols = ["date"]
                if "symbol" in df.columns:
                    unique_cols = ["symbol", "date"]
                elif "code" in df.columns:
                    unique_cols = ["code", "date"]
                added = self.repo.bulk_upsert(
                    table_name, df, unique_cols, batch_size=500)
                self.log_widget.write("INFO",
                    f"已缓存 {added} 条到 [{table_name}]")
            except Exception as exc:
                self.log_widget.write("WARNING",
                    f"数据缓存失败: {exc}")

    def _on_api_error(self, source_key: str, error: str) -> None:
        """API 查询错误处理"""
        self.log_widget.write("ERROR", f"查询失败 [{source_key}]: {error}")
        self.statusBar().showMessage(f"查询失败: {source_key}", 5000)

    # ------------------------------------------------------------------
    # 测试接口连接
    # ------------------------------------------------------------------

    def _on_test_connection(self) -> None:
        """测试当前数据源和 akShare 连通性（使用 threading.Thread）"""
        source_key = self._current_source_key
        source = self.registry.get_source(source_key) if source_key else None
        api_name = source.get("api_function", "?") if source else "?"

        # 禁用按钮防重复点击
        self.param_panel.test_btn.setEnabled(False)
        self.param_panel.test_btn.setText("测试中...")

        self.log_widget.write("INFO", "=" * 50)
        self.log_widget.write("INFO", "开始接口连通性测试...")
        if source_key:
            self.log_widget.write("INFO", f"当前数据源: {source_key} → {api_name}")
        else:
            self.log_widget.write("INFO", "未选择数据源，将测试 2 个默认接口")
        self.log_widget.write("INFO", "正在测试: 中国CPI + A股日线...")
        self.statusBar().showMessage("正在测试 API 连通性...")

        # 使用 Python threading.Thread 替代 QThread
        import threading
        from src.core.data_fetcher import DataFetcher
        from src.utils.config import ConfigManager

        def run_test():
            import time
            try:
                fetcher = DataFetcher(ConfigManager())
                test_cases = [
                    ("macro_china_cpi", {}, "中国CPI"),
                    ("stock_zh_a_hist", {"symbol": "000001", "period": "daily",
                                         "start_date": "20260701", "end_date": "20260728"}, "A股日线"),
                ]
                results = []
                for func_name, params, label in test_cases:
                    start = time.time()
                    try:
                        cfg = {"api_source": "akshare", "api_function": func_name}
                        df = fetcher.fetch(cfg, params)
                        elapsed = time.time() - start
                        if df is not None and not df.empty:
                            results.append(dict(name=label, func=func_name, ok=True,
                                                elapsed=round(elapsed, 1),
                                                message=f"{len(df)}行×{len(df.columns)}列"))
                        else:
                            results.append(dict(name=label, func=func_name, ok=True,
                                                elapsed=round(elapsed, 1),
                                                message="返回空数据"))
                    except Exception as e:
                        elapsed = time.time() - start
                        results.append(dict(name=label, func=func_name, ok=False,
                                            elapsed=round(elapsed, 1),
                                            message=f"{e}"))
                # 通过队列回到主线程
                self._result_queue.put(
                    lambda: self._on_test_result({"results": results}, source_key))
            except Exception as e:
                self._result_queue.put(
                    lambda: self._on_test_timeout(source_key))

        t = threading.Thread(target=run_test, daemon=True)
        t.start()

    def _on_test_timeout(self, source_key: str) -> None:
        """测试超时处理"""
        self.param_panel.test_btn.setEnabled(True)
        self.param_panel.test_btn.setText("测试接口")
        self.log_widget.write("ERROR", "接口测试超时（>60秒），请检查网络连接后重试")
        self.statusBar().showMessage("接口测试超时", 5000)

    def _on_test_result(self, result: dict, source_key: str) -> None:
        """测试结果处理"""
        self.param_panel.test_btn.setEnabled(True)
        self.param_panel.test_btn.setText("测试接口")

        results = result.get("results", [])
        all_ok = all(r["ok"] for r in results)

        self.log_widget.write("INFO", "─" * 50)
        self.log_widget.write("INFO", "测试结果:")
        for r in results:
            icon = "✅" if r["ok"] else "❌"
            self.log_widget.write(
                "INFO" if r["ok"] else "ERROR",
                f"  {icon} [{r['name']}] {r['func']}: "
                f"{r['message']} ({r['elapsed']}s)"
            )
        self.log_widget.write("INFO", "─" * 50)

        if all_ok:
            self.log_widget.write("SUCCESS", "所有接口测试通过 ✓")
            self.statusBar().showMessage("接口测试全部通过", 5000)
        else:
            self.log_widget.write("WARNING", "部分接口测试失败，请检查网络连接")
            self.statusBar().showMessage("接口测试有失败项", 5000)

    def _ensure_data_table(self, table_name: str, df: pd.DataFrame) -> None:
        """确保数据表存在，如果不存在则根据 DataFrame 列创建"""
        if self.repo.table_exists(table_name):
            return
        try:
            cols = []
            for col in df.columns:
                # 日期列用 DATE 类型
                if "date" in col.lower() or "时间" in col or "日期" in col or "月份" in col:
                    cols.append(f'"{col}" DATE')
                else:
                    cols.append(f'"{col}" TEXT')
            sql = f'CREATE TABLE IF NOT EXISTS "{table_name}" (\n    id INTEGER PRIMARY KEY AUTOINCREMENT,\n    ' + \
                  ',\n    '.join(cols) + ',\n    created_at DATETIME DEFAULT CURRENT_TIMESTAMP' + '\n)'
            with self.engine.connect() as conn:
                conn.execute(text(sql))
                conn.commit()
            logger.info(f"已自动创建数据表: {table_name} ({len(cols)} 列)")
        except Exception as e:
            logger.warning(f"创建表 {table_name} 失败: {e}")

    @staticmethod
    def _filter_by_date(df: pd.DataFrame, params: dict) -> pd.DataFrame:
        """客户端按日期范围过滤 DataFrame

        自动识别日期列名（date/日期/时间/TRADE_DATE 等），
        无日期列时返回原数据。
        """
        if df.empty:
            return df

        # 自动识别日期列
        date_col = None
        for candidate in ["date", "日期", "时间", "TRADE_DATE", "trade_date"]:
            if candidate in df.columns:
                date_col = candidate
                break

        if date_col is None:
            return df

        start_str = params.get("start_date", "")
        end_str = params.get("end_date", "")

        if not start_str and not end_str:
            return df

        # 统一转为 datetime
        try:
            if not pd.api.types.is_datetime64_any_dtype(df[date_col]):
                df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
        except Exception:
            return df

        mask = pd.Series(True, index=df.index)
        if start_str:
            try:
                start_dt = pd.to_datetime(start_str)
                mask &= (df[date_col] >= start_dt)
            except Exception:
                pass
        if end_str:
            try:
                # 包含结束日期当天
                end_dt = pd.to_datetime(end_str) + pd.Timedelta(days=1)
                mask &= (df[date_col] < end_dt)
            except Exception:
                pass

        return df[mask].copy()

    def _on_query_result(self, source_key: str, df: pd.DataFrame) -> None:
        """数据库查询结果处理"""
        # QueryWorker 已在后台排好序，跳过主线程排序
        self.table_view.loadDataFrame(df, already_sorted=True)
        self._update_row_count(df)
        self.log_widget.write(
            "INFO",
            f"查询完成 [{source_key}] 共 {len(df)} 条记录",
        )

    # ------------------------------------------------------------------
    # 数据抓取
    # ------------------------------------------------------------------

    def _on_scrape_all(self) -> None:
        """抓取所有启用的抓取规则（后台线程，避免卡 UI）"""
        from src.scraper.engine import ScrapeEngine
        engine = ScrapeEngine(repo=self.repo)
        rules = engine.load_rules()
        enabled = [r for r in rules if r.get("enabled") is not False]
        if not enabled:
            self.log_widget.write("WARNING",
                "没有启用的抓取规则，请检查 config/scrapers.yaml")
            return

        names = "\n".join(f"  • {r.get('name')}" for r in enabled)
        ret = QMessageBox.question(
            self, "确认抓取",
            f"将抓取以下 {len(enabled)} 个数据源：\n\n{names}\n\n是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return

        self.log_widget.write("INFO", f"开始抓取 {len(enabled)} 个数据源...")

        import threading
        def run_scrape():
            try:
                results = engine.run_all_enabled()
                def report():
                    ok = sum(1 for v in results.values() if v >= 0)
                    self.log_widget.write(
                        "SUCCESS",
                        f"抓取完成: {ok}/{len(results)} 成功  |  "
                        + "  ".join(f"{k}={v}行" for k, v in results.items()))
                    self._refresh_current_table()
                self._result_queue.put(report)
            except Exception as exc:
                self._result_queue.put(
                    lambda: self.log_widget.write("ERROR", f"抓取失败: {exc}"))

        t = threading.Thread(target=run_scrape, daemon=True)
        t.start()

    def _show_scrape_manager(self) -> None:
        """打开抓取规则管理器"""
        from src.scraper.engine import ScrapeEngine
        from src.gui.dialogs.scrape_manager import ScrapeManagerDialog
        engine = ScrapeEngine(repo=self.repo)
        dialog = ScrapeManagerDialog(engine, self)
        dialog.exec()
        # 规则可能已改，提示
        self.log_widget.write("INFO", "抓取规则已刷新，可点「抓取所有数据源」执行")

    def _show_catalog_editor(self) -> None:
        """打开数据分类管理，保存后刷新导航树与注册表"""
        from src.gui.dialogs.catalog_editor import CatalogEditorDialog
        dialog = CatalogEditorDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            # 刷新数据源注册表 + 左侧导航树 + 定时调度
            try:
                self.registry.refresh()
                self.tree_widget.registry = self.registry
                self.tree_widget.rebuild()
                self.log_widget.write("INFO",
                    "数据分类已更新，导航树已刷新")
                # 定时调度重新注册（分类可能变化）
                self._reload_scheduler()
            except Exception as exc:
                self.log_widget.write("ERROR",
                    f"刷新导航树失败: {exc}")

    def _reload_scheduler(self) -> None:
        """重新加载定时调度（分类/定时变化后）"""
        try:
            # 停止旧任务，重新注册
            self.scheduler.scheduler.remove_all_jobs()
            self.scheduler._category_jobs.clear()
            categories = self.registry.get_categories()
            self.scheduler.register_category_jobs(categories, self._on_schedule_trigger)
            self._register_scraper_jobs()
            self.log_widget.write("INFO", "定时调度已重新加载")
        except Exception as exc:
            self.log_widget.write("WARNING", f"重新加载定时调度失败: {exc}")

    # ------------------------------------------------------------------
    # 同步
    # ------------------------------------------------------------------

    def on_sync_current(self) -> None:
        """同步数据源 — 优先同步左侧勾选的数据源，否则同步当前选中项

        同步前弹出确认对话框，列出待同步的数据源，确认后才执行。
        """
        # 1. 确定待同步列表（勾选优先）
        checked = self.tree_widget.getCheckedSourceKeys()
        if checked:
            source_keys = checked
        else:
            current = self._current_source_key
            if not current:
                self.log_widget.write(
                    "WARNING", "请先选择或勾选要同步的数据源")
                return
            source_keys = [current]

        # ★ 基金日线源 + 未选具体 code → 走批量（按交易日补全市场，一次调用
        #   返回全市场 ~2000 只，替代逐个 code 的上千次调用，提速数百倍）
        if (len(source_keys) == 1 and source_keys[0] == "fund.etf_daily"
                and not self.param_panel.getSelectedLocalCode()):
            self._run_fund_daily_batch()
            return

        # 2. 可读名称清单（列表过长时截断显示）
        names = []
        for k in source_keys:
            src = self.registry.get_source(k)
            names.append(src.get("name", k) if src else k)
        shown = names[:12]
        preview = "\n".join(f"  • {n}" for n in shown)
        if len(names) > len(shown):
            preview += f"\n  … 等共 {len(names)} 个数据源"

        # 3. 确认对话框（默认「否」，避免误触）
        ret = QMessageBox.question(
            self,
            "确认同步",
            f"将同步以下 {len(names)} 个数据源：\n\n{preview}\n\n是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            self.log_widget.write("INFO", "已取消同步")
            return

        # 4. 构建同步计划：优先本地筛选下拉选中的代码；选「全部」则遍历表中
        #    每个已有代码逐个同步（基金/股票等多代码源）
        src = self.registry.get_source(source_keys[0]) if source_keys else None
        jobs = _build_sync_jobs(
            source_keys, src, self.repo, self.param_panel, self._local_code_column)
        if src and not any(p for _, p in jobs if p):
            # 无明确代码（非代码型源或表无数据）→ 用输入框/默认参数
            params = self.param_panel.getParams()
            for pkey in ("ts_code", "symbol", "code"):
                val = str(params.get(pkey, "")).strip()
                if val and pkey in (src.get("params_template") or {}):
                    jobs = [(k, {pkey: val}) for k in source_keys]
                    break

        # 5. 后台顺序同步
        code_info = ""
        codes_shown = [p.get("ts_code") or p.get("symbol") or p.get("code")
                       for _, p in jobs if p]
        if codes_shown:
            shown = codes_shown[:8]
            code_info = f" ({len(jobs)}个任务, 代码={'/'.join(shown)}"
            code_info += "…" if len(codes_shown) > len(shown) else ")"
        self.log_widget.write(
            "INFO", f"开始同步 {len(jobs)} 个任务{code_info} ...")
        thread = QThread(self)
        worker = SyncListWorker(self.sync_engine, jobs)
        # ★ 防 GC：worker 是局部变量，函数返回后被回收会导致 started 连接失效
        #   （worker.run 永不执行）。挂到 thread 上保活，thread 由 self._threads 持有。
        thread._worker_ref = worker
        worker.moveToThread(thread)

        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.error.connect(
            lambda err: self.log_widget.write("ERROR", f"同步失败: {err}"))
        thread.finished.connect(thread.deleteLater)

        thread.started.connect(worker.run)
        thread.start()
        self._threads.append(thread)
        thread.finished.connect(lambda: self._cleanup_thread(thread))

    def on_sync_all(self) -> None:
        """同步所有启用的数据源（含二次确认，防误触；run_all 顺序执行+每源锁）"""
        try:
            n = len(self.repo.get_all_enabled_sources())
        except Exception:
            n = 0
        ret = QMessageBox.question(
            self,
            "确认全量同步",
            f"将顺序同步全部 {n} 个启用数据源（不含已弃用源）。\n\n"
            "可能耗时较长，可中途停止。是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            self.log_widget.write("INFO", "已取消全量同步")
            return

        self.log_widget.write("INFO", f"开始全量同步 {n} 个数据源...")
        self._sync_all_active = True
        thread = QThread(self)
        worker = SyncAllWorker(self.sync_engine)
        # ★ 防 GC：worker 局部变量函数返回后被回收，started 连接失效
        thread._worker_ref = worker
        worker.moveToThread(thread)

        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        # 全量结束 → 收起状态栏进度条（queued 投递回主线程）
        worker.finished.connect(self._on_sync_all_finished)
        thread.finished.connect(thread.deleteLater)

        thread.started.connect(worker.run)
        thread.start()
        self._threads.append(thread)
        thread.finished.connect(lambda: self._cleanup_thread(thread))

    def _run_fund_daily_batch(self) -> None:
        """批量更新基金日线（按交易日补全市场）— 替代逐个 code 的上千次同步"""
        ret = QMessageBox.question(
            self,
            "批量更新基金日线",
            "将按交易日批量补全市场基金数据（每个交易日一次调用返回全市场，\n"
            "比逐个基金同步快数百倍，无需逐个等待）。\n\n是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            self.log_widget.write("INFO", "已取消批量更新")
            return

        self.log_widget.write("INFO", "开始批量更新基金日线（按交易日补全市场）...")
        thread = QThread(self)
        worker = SyncFundBatchWorker(self.sync_engine)
        # ★ 防 GC：worker 局部变量函数返回后被回收，started 连接失效
        thread._worker_ref = worker
        worker.moveToThread(thread)

        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        # 批量更新走 run_fund_daily_batch（无 sync_progress），结束后顺手收起进度条
        worker.finished.connect(self._on_sync_all_finished)
        worker.error.connect(
            lambda err: self.log_widget.write("ERROR", f"批量更新失败: {err}"))
        thread.finished.connect(thread.deleteLater)

        thread.started.connect(worker.run)
        thread.start()
        self._threads.append(thread)
        thread.finished.connect(lambda: self._cleanup_thread(thread))

    def on_sync_completed(self, source_key: str, added: int) -> None:
        """同步完成回调"""
        self.statusBar().showMessage(
            f"同步完成: {source_key} 新增 {added} 条", 5000
        )
        # 单源同步完成 → 收起进度条；run_all 期间保持显示（由 run_all 结束信号收尾）
        if not self._sync_all_active:
            self.sync_progress_bar.setVisible(False)
        self._refresh_current_table()

    def on_sync_error(self, source_key: str, error: str) -> None:
        """同步错误回调"""
        self.statusBar().showMessage(f"同步失败: {source_key}", 5000)
        self.log_widget.write("ERROR", f"同步失败 [{source_key}]: {error}")

    def _on_sync_progress(self, source_key: str, current: int, total: int) -> None:
        """全量同步进度槽 — 更新状态栏进度条 + 文本

        run_all() 逐源发射 sync_progress(key, i-1, total)：current 为 0 基下标。
        单源 run() 发射 sync_progress(key, 0, len(df))：total 是行数而非源数，
        用 _sync_all_active 区分 —— run_all 中显示确定进度，单源只做忙碌指示。
        """
        bar = self.sync_progress_bar
        bar.setVisible(True)
        if self._sync_all_active:
            bar.setRange(0, max(total, 1))
            bar.setValue(min(current + 1, max(total, 1)))
            self.statusBar().showMessage(
                f"正在同步 ({current + 1}/{total}): {source_key}", 0)
        else:
            # 单源同步：行数无进度意义 → 忙碌指示 + 当前源名
            bar.setRange(0, 0)
            self.statusBar().showMessage(f"正在同步: {source_key}", 0)

    def _on_sync_all_finished(self) -> None:
        """全量同步（run_all）结束：收起进度条、复位状态"""
        self._sync_all_active = False
        self.sync_progress_bar.setVisible(False)
        self.statusBar().showMessage("全量同步完成", 5000)

    def _refresh_current_table(self) -> None:
        """防抖刷新当前表：合并同步期间的多次请求，空闲 300ms 后后台查库

        原实现在主线程直接 repo.query + loadDataFrame(全量 model reset)，
        逐 code 同步时每个完成都触发一次，主线程被 N 次 DB 查询 + 重绘阻塞
        → 用户拖动滚动条时无响应。现改为：单发 timer 防抖 + 后台 QueryWorker。
        """
        if not self._current_source_key:
            return
        # 若已有后台查询在跑，标记 pending，完成后补刷一次
        if self._refresh_in_progress:
            self._refresh_pending = True
            return
        self._refresh_timer.start()

    def _do_refresh_current_table(self) -> None:
        """防抖到期：启动后台查询（真正执行刷新）"""
        if self._refresh_in_progress:
            self._refresh_pending = True
            return
        source = self.registry.get_source(self._current_source_key or "")
        if not source or not source.get("table_name"):
            return
        params = self.param_panel.getParams()
        filters: dict = {}
        code_col = self._local_code_column(source["table_name"])
        if code_col:
            local_code = self.param_panel.getSelectedLocalCode()
            if local_code:
                filters[code_col] = local_code
        self._refresh_in_progress = True
        self._start_background_refresh(
            source["table_name"], filters,
            params.get("start_date") or None, params.get("end_date") or None)

    def _start_background_refresh(
        self, table_name: str, filters: dict,
        date_from: str | None, date_to: str | None,
    ) -> None:
        """在后台线程查当前表（复用 QueryWorker），主线程只负责加载结果"""
        thread = QThread(self)
        worker = QueryWorker(
            self.repo, table_name, filters,
            date_from=date_from, date_to=date_to, limit=5000)
        # ★ 防 GC：worker 局部变量函数返回后被回收，started 连接失效
        thread._worker_ref = worker
        worker.moveToThread(thread)

        worker.finished.connect(lambda df: self._apply_refresh_result(df))
        worker.error.connect(
            lambda err: self._on_refresh_error(err))
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        thread.started.connect(worker.run)
        thread.start()
        self._threads.append(thread)
        thread.finished.connect(lambda: self._cleanup_thread(thread))

    def _apply_refresh_result(self, df) -> None:
        """主线程应用后台查询结果（loadDataFrame）"""
        self._refresh_in_progress = False
        # 查询期间又有刷新请求 → 补刷一次
        if self._refresh_pending:
            self._refresh_pending = False
            self._refresh_timer.start()
        # QueryWorker 已在后台排好序，跳过主线程排序
        self.table_view.loadDataFrame(df, already_sorted=True)
        self._update_row_count(df)

    def _on_refresh_error(self, err: str) -> None:
        """后台刷新失败：复位标志，保证后续刷新不被卡住"""
        self._refresh_in_progress = False
        self.log_widget.write("ERROR", f"刷新数据失败: {err}")

    # ------------------------------------------------------------------
    # 更新方式（更新 ▼ 菜单）
    # ------------------------------------------------------------------

    def on_update_method(self, method: str) -> None:
        """按用户选择的更新方式执行：api / file / scrape"""
        if method == "api":
            self.on_sync_current()
        elif method == "file":
            self._open_import_dialog()
        elif method == "scrape":
            self._run_scrape_for_current_table()
        else:
            self.log_widget.write("WARNING", f"未知更新方式: {method}")

    def _run_scrape_for_current_table(self) -> None:
        """抓取与当前表匹配的抓取规则；无匹配则打开抓取管理"""
        table_name = None
        if self._current_source_key:
            source = self.registry.get_source(self._current_source_key)
            table_name = source.get("table_name") if source else None
        if not table_name:
            self.log_widget.write("WARNING", "请先选择一个数据源")
            return
        from src.scraper.engine import ScrapeEngine
        engine = ScrapeEngine(self.repo)
        try:
            rules = [r for r in engine.load_rules()
                     if r.get("table_name") == table_name]
        except Exception as exc:
            self.log_widget.write("ERROR", f"加载抓取规则失败: {exc}")
            return
        if not rules:
            self.log_widget.write(
                "INFO", f"没有针对表 [{table_name}] 的抓取规则，打开抓取管理")
            self._show_scrape_manager()
            return
        self.log_widget.write(
            "INFO", f"正在抓取 {len(rules)} 条规则 → {table_name} ...")
        for rule in rules:
            try:
                added = engine.run_by_name(rule.get("name"))
                self.log_widget.write(
                    "INFO", f"抓取 [{rule.get('name')}] 完成，写入 {added} 行")
            except Exception as exc:
                self.log_widget.write(
                    "ERROR", f"抓取 [{rule.get('name')}] 失败: {exc}")
        self._refresh_current_table()

    # ------------------------------------------------------------------
    # 初始化向导
    # ------------------------------------------------------------------

    def on_init_wizard(self) -> None:
        """打开初始化向导"""
        wizard = InitWizard(self.registry, self.config, self)
        if wizard.exec() == QDialog.DialogCode.Accepted:
            modules = wizard.getSelectedModules()
            years = wizard.getHistoryYears()

            self.log_widget.write(
                "INFO",
                f"初始化向导完成，将在后台下载 {len(modules)} 个模块",
            )

            if modules:
                self._run_init_sync(modules, years)

    def _run_init_sync(self, modules: list[str], history_years: int) -> None:
        """在后台线程中顺序同步多个模块"""
        thread = QThread(self)

        class _InitSyncWorker(QObject):
            finished = Signal()

            def __init__(self, sync_engine, modules, history_years):
                super().__init__()
                self.sync_engine = sync_engine
                self.modules = modules
                self.history_years = history_years

            def run(self):
                for mod in self.modules:
                    self.sync_engine.run(mod, self.history_years)
                self.finished.emit()

        worker = _InitSyncWorker(
            self.sync_engine, modules, history_years
        )
        # ★ 防 GC：worker 局部变量函数返回后被回收，started 连接失效
        thread._worker_ref = worker
        worker.moveToThread(thread)

        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        thread.started.connect(worker.run)
        thread.start()
        self._threads.append(thread)
        thread.finished.connect(lambda: self._cleanup_thread(thread))

    # ------------------------------------------------------------------
    # 导入
    # ------------------------------------------------------------------

    def _open_import_dialog(self) -> None:
        """打开导入对话框

        有选中数据源 → 以其表为首选项（可改）；无选中 → 纯自动识别。
        """
        table_name = ""
        unique_key = []
        if self._current_source_key:
            source = self.registry.get_source(self._current_source_key)
            table_name = (source or {}).get("table_name", "")
            # 推断唯一键（复用同步用的同一套逻辑）
            unique_key = self.sync_engine._get_unique_cols(source or {})

        from src.gui.dialogs.import_dialog import ImportDialog
        dialog = ImportDialog(self.repo, table_name, unique_key, self)
        dialog.exec()
        self._refresh_current_table()  # 导入后刷新当前表格

    # 导出
    # ------------------------------------------------------------------

    def _open_export_dialog(self) -> None:
        """打开导出对话框"""
        # 已设获信源 → 导出获信指标序列（统一数据）；否则导出当前表格
        src = None
        if self._current_source_key:
            src = self.registry.get_source(self._current_source_key)
        trusted_df = self._trusted_indicator_df(src)
        if trusted_df is not None:
            df = trusted_df
            source_key = src["indicator"] or "indicator"
        else:
            df = self.table_view.pandas_model.getDataFrame()
            if df.empty:
                QMessageBox.information(self, "提示", "当前没有可导出的数据")
                return
            source_key = self._current_source_key or "data"
        dialog = ExportDialog(df, source_key, self)
        dialog.exec()

    def _export_csv(self) -> None:
        """快捷导出 CSV（打开导出对话框，默认 CSV 格式）"""
        self._open_export_dialog()

    def _export_excel(self) -> None:
        """快捷导出 Excel（打开导出对话框，默认 Excel 格式）"""
        self._open_export_dialog()

    def _export_pdf(self) -> None:
        """快捷导出 PDF（打开导出对话框，默认 PDF 格式）"""
        self._open_export_dialog()

    # ------------------------------------------------------------------
    # 设置
    # ------------------------------------------------------------------

    def _show_settings(self) -> None:
        """打开设置对话框"""
        dialog = SettingsDialog(self.config, self)
        dialog.exec()

    def _show_schedule_manager(self) -> None:
        """打开定时任务管理对话框"""
        categories = self.tree_widget.get_category_schedule_info()

        if not categories:
            self.log_widget.write("INFO", "当前没有注册的分类")
            return

        dialog = ScheduleDialog(self.scheduler, categories, self)
        dialog.exec()

    def _show_health_check(self) -> None:
        """打开数据源健康检查对话框"""
        dialog = HealthDialog(self.repo, self.registry, self.scheduler, self)
        dialog.exec()

    def _show_indicator_manager(self) -> None:
        """打开指标管理中心对话框（集中查看/切换获信源）"""
        dialog = IndicatorManagerDialog(self.repo, self.registry, self)
        dialog.exec()

    def _trusted_indicator_df(self, source) -> "pd.DataFrame | None":
        """若当前源有 indicator 且已设获信源 → 返回获信 {date,value}；否则 None"""
        if not source:
            return None
        ind = source.get("indicator", "")
        if not ind:
            return None
        if self.repo.get_indicator_map(ind) is None:
            return None
        try:
            df = self.repo.get_indicator(ind, limit=None)
        except Exception:
            return None
        return df if not df.empty else None

    def _set_as_trusted_source(self, source) -> bool:
        """把指定数据源设为该指标的获信（首选）来源；成功返回 True"""
        if not source:
            return False
        indicator_key = source.get("indicator", "")
        table_name = source.get("table_name", "")
        if not indicator_key or not table_name:
            QMessageBox.information(
                self, "提示", "该数据源未定义 indicator，无法设为获信源")
            return False

        record = self.repo.set_indicator(indicator_key, table_name)
        if record is None:
            QMessageBox.warning(
                self, "设置失败",
                f"无法把表 [{table_name}] 设为获信源：\n"
                "未能解析日期列/数值列，或表不存在。")
            return False

        # 刷新指标获信区 + 日志
        mapping = self.repo.get_indicator_map(indicator_key)
        self.param_panel.setIndicatorContext(indicator_key, mapping)
        self.log_widget.write(
            "INFO", f"已设 [{table_name}] 为指标 {indicator_key} 的获信源")
        QMessageBox.information(
            self, "已设为获信源",
            f"指标 {indicator_key}\n获信源: {table_name}\n\n"
            "之后 AI 分析 / 导出 / /macro/cpi 将统一读取获信源数据。")
        return True

    def _on_trust_clicked(self) -> None:
        """把当前数据源设为该指标的获信（首选）来源"""
        source_key = self._current_source_key
        if not source_key:
            return
        source = self.registry.get_source(source_key)
        if source:
            self._set_as_trusted_source(source)

    def _on_tree_trust_requested(self, source_key: str) -> None:
        """树右键「设为获信源」→ 设为获信（不改变当前选中）"""
        if not source_key:
            return
        source = self.registry.get_source(source_key)
        if source:
            self._set_as_trusted_source(source)

    def _show_ai_analyze(self) -> None:
        """打开 AI 智能分析对话框（分析当前表格数据）"""
        # 当前源及其指标获信数据（若已设获信源 → 分析获信序列，保证统一）
        src = None
        if self._current_source_key:
            src = self.registry.get_source(self._current_source_key)
        trusted_df = self._trusted_indicator_df(src)
        if trusted_df is not None:
            df = trusted_df
            mapping = self.repo.get_indicator_map(src["indicator"])
            table_name = f"{src['indicator']} · 获信源 {mapping['preferred_table']}"
        else:
            # 当前表格数据（无论来自查询还是数据库加载）
            df = self.table_view.pandas_model.getDataFrame()
            if df is None or df.empty:
                QMessageBox.information(self, "提示",
                    "当前表格没有可分析的数据\n\n"
                    "请先在左侧选择数据源并查询/同步数据，再执行 AI 分析")
                return
            # 目标表名
            table_name = self._current_source_key or "当前数据"
            if src and src.get("table_name"):
                table_name = src["table_name"]

        # AI 客户端（不可用则提示）
        from src.importer.ai_client import AiClient
        ai_client = AiClient()
        if not ai_client.is_available():
            ret = QMessageBox.question(
                self, "AI 未就绪",
                "未检测到本地 AI 服务（ollama）。\n\n"
                "仍要尝试分析吗？（可能需要较长时间或失败）",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ret != QMessageBox.StandardButton.Yes:
                return

        from src.gui.dialogs.analyze_dialog import AnalyzeDialog
        dialog = AnalyzeDialog(df, table_name, ai_client, self)
        dialog.exec()

    # ------------------------------------------------------------------
    # 🌐 API 本地服务
    # ------------------------------------------------------------------

    def _start_api_server(self) -> None:
        """启动 API 本地服务"""
        if self.api_server.is_running:
            self.log_widget.write("INFO", f"API 服务已在运行: {self.api_server.url}")
            self._update_api_status()
            return

        try:
            ok = self.api_server.start(self.config.api_host, self.config.api_port)
            # 首次启动，等待 2 秒后由 _update_api_status 自动更新标签
            from PySide6.QtCore import QTimer
            QTimer.singleShot(2000, self._update_api_status)
        except Exception as e:
            self.api_status_label.setText("🌐 API: ❌ 错误")
            self.api_status_label.setStyleSheet("color: #c62828; padding: 0 8px;")
            self.log_widget.write("ERROR", f"API 服务启动失败: {e}")

    def _stop_api_server(self) -> None:
        """停止 API 本地服务"""
        self.api_server.stop()
        self.api_status_label.setText("🌐 API: ⏹️ 已停止")
        self.api_status_label.setStyleSheet("color: #888; padding: 0 8px;")
        self.log_widget.write("INFO", "API 服务已停止")

    def _restart_api_server(self) -> None:
        """重启 API 服务"""
        self._stop_api_server()
        import time
        time.sleep(0.5)
        self._start_api_server()

    def _update_api_status(self) -> None:
        """定时刷新 API 状态显示（每 2 秒）"""
        if self.api_server.is_running:
            conns = self.api_server.connection_count
            total_req = self.api_server.total_requests
            self.api_status_label.setText(
                f"🌐 API: ✅ {self.api_server.url}  |  "
                f"{conns} 连接 | {total_req} 请求"
            )
        else:
            # 还没启动好，保持等待状态
            pass

    def _show_api_connections(self) -> None:
        """显示 API 连接监控面板"""
        if not self.api_server.is_running:
            QMessageBox.information(self, "API 服务",
                "API 服务未运行\n\n菜单 「工具」→「启动 API 服务」")
            return

        conns = self.api_server.get_connections()

        if not conns:
            QMessageBox.information(self, "API 连接监控",
                "暂无历史连接记录\n\n"
                "其他项目访问以下地址后就会显示在这里:\n"
                f"{self.api_server.url}/api/v1/health")
            return

        total_req = self.api_server.total_requests
        msg = f"🌐 API 连接监控\n"
        msg += f"地址: {self.api_server.url}\n"
        msg += f"总连接: {len(conns)} 个 | 总请求: {total_req}\n\n"

        for c in conns:
            msg += f"▸ {c['client_ip']}\n"
            msg += f"  代理: {c['user_agent'][:60]}\n"
            ua_short = c.get("user_agent", "unknown")[:60]
            msg += f"  请求: {c['request_count']} 次\n"
            msg += f"  最后: {c['last_seen'][:19]}\n"
            msg += f"  路径: {', '.join(c['paths'][:5])}\n\n"

        msg += "💡 点击「工具」菜单可重启 API 服务"

        QMessageBox.information(self, "API 连接监控", msg)

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    def _cleanup_thread(self, thread: QThread) -> None:
        """清理已完成的线程引用"""
        if thread in self._threads:
            self._threads.remove(thread)
