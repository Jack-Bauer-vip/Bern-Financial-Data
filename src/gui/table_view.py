"""数据表格视图 — Pandas DataFrame 模型与 QTableView 封装（Phase 2 增强版）"""

import numpy as np
import pandas as pd
from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtWidgets import (
    QTableView, QAbstractItemView, QHeaderView, QMenu, QApplication
)
from PySide6.QtGui import QClipboard


def sort_df_by_date_desc(df: pd.DataFrame) -> pd.DataFrame:
    """按日期列倒序排列（最新在前）；无日期列则原样返回

    从 loadDataFrame 抽出供后台 QueryWorker 使用——排序不再占用主线程。
    """
    if df is None or df.empty:
        return df
    date_col = None
    for col in df.columns:
        col_lower = col.lower().strip()
        if any(kw in col_lower for kw in
               ("date", "时间", "日期", "月份", "trade_date", "datetime")):
            date_col = col
            break
    if date_col is not None:
        try:
            # 统一转 datetime 再排序
            sort_series = pd.to_datetime(df[date_col], errors="coerce")
            if sort_series.notna().any():
                return df.iloc[sort_series.argsort()[::-1]].reset_index(drop=True)
        except Exception:
            pass
    return df


class PandasModel(QAbstractTableModel):
    """将 pandas DataFrame 适配为 Qt Model/View 模型

    ★ 性能要点：data() 是绘制路径上的热点，Qt 每画一个单元格都会调用它多次。
    早期实现对每个单元格都执行 self._data.iloc[:, col].dtype（复制整列 O(行数)）
    和 self._data.iloc[row, col]，5000 行大表滚动/重绘时明显卡顿。
    现改为 setDataFrame 时一次性把列值转成 Python 列表并缓存「是否数值列」标志，
    data() 只做纯列表索引（O(1)），绘制路径完全不再触碰 pandas。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._data = pd.DataFrame()
        self._columns: list = []
        self._col_values: list = []   # 列优先: _col_values[col][row]
        self._numeric_cols: list = []
        self._row_count = 0

    def setDataFrame(self, df: pd.DataFrame) -> None:
        """设置新数据并通知视图刷新"""
        self.beginResetModel()
        self._data = df.copy()
        self._row_count = df.shape[0]
        self._columns = list(df.columns)
        # ★ 一次性把列值转成 Python 列表（向量化 tolist），绘制路径用纯列表索引
        self._col_values = [
            df.iloc[:, i].tolist() for i in range(df.shape[1])
        ]
        self._numeric_cols = [
            pd.api.types.is_numeric_dtype(df.iloc[:, i])
            for i in range(df.shape[1])
        ]
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return self._row_count

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self._columns)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None

        if role == Qt.ItemDataRole.DisplayRole:
            val = self._col_values[index.column()][index.row()]
            # 格式化浮点数（NaN/None 一律显示为空串，与旧实现一致）
            if isinstance(val, (float, np.floating)):
                if pd.isna(val):
                    return ""
                v = float(val)
                return f"{v:.4f}" if abs(v) < 10000 else f"{v:.2f}"
            return str(val) if pd.notna(val) else ""

        if role == Qt.ItemDataRole.TextAlignmentRole:
            if self._numeric_cols[index.column()]:
                return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            return int(Qt.AlignmentFlag.AlignCenter)

        return None

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ):
        if role == Qt.ItemDataRole.DisplayRole:
            if orientation == Qt.Orientation.Horizontal:
                return str(self._columns[section])
            elif orientation == Qt.Orientation.Vertical:
                return str(section + 1)
        return None

    def getDataFrame(self) -> pd.DataFrame:
        """返回当前数据的副本"""
        return self._data.copy()


class DataTableView(QTableView):
    """封装了 PandasModel 的表格视图组件（支持排序/复制/列宽自适应）"""

    def __init__(self, parent=None):
        super().__init__(parent)

        self.pandas_model = PandasModel(self)
        self.setModel(self.pandas_model)

        # 基本设置
        self.setSortingEnabled(True)
        self.setAlternatingRowColors(True)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)

        # 表头行为
        horizontal_header = self.horizontalHeader()
        horizontal_header.setStretchLastSection(False)
        horizontal_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        horizontal_header.setHighlightSections(False)

        self.verticalHeader().setDefaultSectionSize(28)
        self.verticalHeader().setHighlightSections(False)
        # ★ 性能优化：固定行高模式（QTableView 默认逐行测量高度，改为固定
        #   行高可跳过逐行测量，model reset(loadDataFrame) 后重建更快）。
        #   注：PySide6 此版本无 setUniformRowHeights，用固定行高等效。
        self.verticalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Fixed)
        self.setVerticalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel)

        # 样式
        # 样式（由全局 app.py 深色主题控制）
        self.setShowGrid(True)

        # 启用上下文菜单
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

    # ------------------------------------------------------------------
    # 数据加载
    # ------------------------------------------------------------------

    def loadDataFrame(self, df: pd.DataFrame, already_sorted: bool = False) -> None:
        """加载并显示 DataFrame 数据，完成后自动调整列宽

        自动按日期列倒序排列（最新数据在前）。
        already_sorted=True：调用方（后台 QueryWorker）已排好序，跳过排序省主线程时间。
        """
        if not df.empty and not already_sorted:
            df = sort_df_by_date_desc(df)
        self.pandas_model.setDataFrame(df)
        if not df.empty:
            self._resize_columns(df)

    def clear(self) -> None:
        """清空当前显示的数据"""
        self.pandas_model.setDataFrame(pd.DataFrame())

    def _resize_columns(self, df: pd.DataFrame) -> None:
        """智能调整列宽：内容宽 + 表头宽 取最大值

        复用 PandasModel 已缓存的列值（纯 Python 列表），避免每列再做 pandas 提取。
        """
        header = self.horizontalHeader()
        col_values = getattr(self.pandas_model, "_col_values", [])
        sample = 60
        for i, col_name in enumerate(df.columns):
            values = col_values[i][:sample] if i < len(col_values) else []
            content_width = len(str(col_name))
            for v in values:
                if v is None:
                    continue
                if isinstance(v, (float, np.floating)) and pd.isna(v):
                    continue
                content_width = max(content_width, len(str(v)))

            # 每个字符约 10px + padding
            pixel_width = min(content_width * 10 + 20, 300)
            header.resizeSection(i, max(pixel_width, 60))

    # ------------------------------------------------------------------
    # 上下文菜单
    # ------------------------------------------------------------------

    def _show_context_menu(self, pos) -> None:
        """右键上下文菜单"""
        menu = QMenu(self)

        copy_action = menu.addAction("复制选中行")
        copy_action.triggered.connect(self._copy_selection)

        copy_all_action = menu.addAction("复制全部")
        copy_all_action.triggered.connect(self._copy_all)

        menu.addSeparator()

        clear_action = menu.addAction("清空表格")
        clear_action.triggered.connect(self.clear)

        menu.exec(self.mapToGlobal(pos))

    def _copy_selection(self) -> None:
        """复制选中的行到剪贴板（制表符分隔）"""
        df = self.pandas_model.getDataFrame()
        if df.empty:
            return

        indexes = self.selectedIndexes()
        if not indexes:
            return

        # 收集选中行
        rows = sorted(set(idx.row() for idx in indexes))
        cols = sorted(set(idx.column() for idx in indexes))

        lines = []
        # 表头
        lines.append("\t".join(str(df.columns[c]) for c in cols))
        # 数据行
        for r in rows:
            lines.append("\t".join(str(df.iloc[r, c]) for c in cols))

        text = "\n".join(lines)
        QApplication.clipboard().setText(text)

    def _copy_all(self) -> None:
        """复制全部数据到剪贴板（制表符分隔）"""
        df = self.pandas_model.getDataFrame()
        if df.empty:
            return

        text = df.to_csv(sep="\t", index=False, header=True)
        QApplication.clipboard().setText(text)
