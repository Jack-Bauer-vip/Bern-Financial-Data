"""数据表格视图 — Pandas DataFrame 模型与 QTableView 封装（Phase 2 增强版）"""

import pandas as pd
from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtWidgets import (
    QTableView, QAbstractItemView, QHeaderView, QMenu, QApplication
)
from PySide6.QtGui import QClipboard


class PandasModel(QAbstractTableModel):
    """将 pandas DataFrame 适配为 Qt Model/View 模型"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._data = pd.DataFrame()

    def setDataFrame(self, df: pd.DataFrame) -> None:
        """设置新数据并通知视图刷新"""
        self.beginResetModel()
        self._data = df.copy()
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return self._data.shape[0]

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return self._data.shape[1]

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None

        if role == Qt.ItemDataRole.DisplayRole:
            val = self._data.iloc[index.row(), index.column()]
            # 格式化浮点数
            if isinstance(val, float):
                return f"{val:.4f}" if abs(val) < 10000 else f"{val:.2f}"
            return str(val) if pd.notna(val) else ""

        if role == Qt.ItemDataRole.TextAlignmentRole:
            col_type = self._data.iloc[:, index.column()].dtype
            if pd.api.types.is_numeric_dtype(col_type):
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
                return str(self._data.columns[section])
            elif orientation == Qt.Orientation.Vertical:
                return str(self._data.index[section] + 1)
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
        # ★ 性能优化：统一行高 + 固定行高模式，QTableView 跳过逐行测量，
        #   model reset(loadDataFrame) 后重建更快，滚动更流畅
        self.setUniformRowHeights(True)
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

    def loadDataFrame(self, df: pd.DataFrame) -> None:
        """加载并显示 DataFrame 数据，完成后自动调整列宽

        自动按日期列倒序排列（最新数据在前）。
        """
        if not df.empty:
            # 自动识别日期列并按倒序排列
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
                        df = df.iloc[sort_series.argsort()[::-1]].reset_index(drop=True)
                except Exception:
                    pass
        self.pandas_model.setDataFrame(df)
        if not df.empty:
            self._resize_columns(df)

    def clear(self) -> None:
        """清空当前显示的数据"""
        self.pandas_model.setDataFrame(pd.DataFrame())

    def _resize_columns(self, df: pd.DataFrame) -> None:
        """智能调整列宽：内容宽 + 表头宽 取最大值"""
        header = self.horizontalHeader()
        for i, col_name in enumerate(df.columns):
            # 估算内容最大宽度（取前 100 行）
            sample = df.iloc[:100, i].dropna().astype(str)
            if len(sample) > 0:
                content_width = max(sample.apply(len).max(), len(str(col_name)))
            else:
                content_width = len(str(col_name))

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
