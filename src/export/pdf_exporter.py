"""PDF 导出器 — fpdf2 纯 Python 实现，支持中文"""

import os
from datetime import datetime

import pandas as pd
from fpdf import FPDF

from src.export.base import BaseExporter, ExportError


class BernPdf(FPDF):
    """自定义 PDF 样式（A4 横向，支持中文）"""

    def __init__(self):
        super().__init__(orientation="L", unit="mm", format="A4")
        self.title = "数据导出报告"
        self.set_auto_page_break(auto=True, margin=15)
        # 注册 Windows 中文字体
        font_path = "C:/Windows/Fonts/msyh.ttc"
        if os.path.exists(font_path):
            self.add_font("YaHei", "", font_path, uni=True)
            bold_path = "C:/Windows/Fonts/msyhbd.ttc"
            if os.path.exists(bold_path):
                self.add_font("YaHei", "B", bold_path, uni=True)
            self._cn_font = "YaHei"
        else:
            self._cn_font = "Helvetica"

    def header(self):
        """每页顶部（首页不显示）"""
        if self.page_no() > 1:
            self.set_font(self._cn_font, "B", 10)
            self.set_text_color(68, 114, 196)
            self.cell(0, 8, self.title, new_x="LMARGIN", new_y="NEXT", align="L")
            self.set_draw_color(68, 114, 196)
            self.line(self.l_margin, self.get_y(),
                      self.w - self.r_margin, self.get_y())
            self.ln(3)

    def footer(self):
        """每页底部"""
        self.set_y(-15)
        self.set_font(self._cn_font, "", 7)
        self.set_text_color(170, 170, 170)
        self.cell(0, 10,
            f"Bern_Financial_Data v0.1.0 | 第 {self.page_no()} 页",
            align="C")


class PdfExporter(BaseExporter):
    """PDF 格式导出器"""

    extension = ".pdf"
    file_filter = "PDF 文件 (*.pdf)"

    def __init__(self, df: pd.DataFrame, options: dict = None):
        super().__init__(df, options or {})
        self.title = self.options.get("title", "数据导出报告")

    def export(self, path: str) -> str:
        path = self.ensure_extension(path, ".pdf")
        try:
            pdf = BernPdf()
            pdf.title = self.title
            pdf.add_page()

            # ---- 元信息 ----
            pdf.set_font(pdf._cn_font, "", 7)
            pdf.cell(0, 5,
                f"导出时间: {datetime.now():%Y-%m-%d %H:%M:%S}  "
                f"| 记录: {len(self.df)} 行  | 字段: {len(self.df.columns)} 列",
                new_x="LMARGIN", new_y="NEXT")
            pdf.ln(3)

            # ---- 列宽计算 ----
            col_count = len(self.df.columns)
            usable_width = 270
            col_width = max(min(usable_width // col_count, 45), 18)

            # ---- 表头 ----
            pdf.set_font(pdf._cn_font, "B", 7)
            pdf.set_fill_color(68, 114, 196)
            pdf.set_text_color(255, 255, 255)
            for col in self.df.columns:
                text = self._truncate(str(col), col_width)
                pdf.cell(col_width, 8, text, border=1, align="C",
                         fill=True, new_x="RIGHT", new_y="TOP")
            pdf.ln()

            # ---- 数据行 ----
            pdf.set_font(pdf._cn_font, "", 7)
            pdf.set_text_color(51, 51, 51)
            for row_idx, (_, row) in enumerate(self.df.iterrows()):
                if row_idx % 2 == 0:
                    pdf.set_fill_color(245, 248, 255)
                else:
                    pdf.set_fill_color(255, 255, 255)

                for val in row:
                    text = self._truncate(self._fmt(val), col_width)
                    pdf.cell(col_width, 6, text, border=1, align="C",
                             fill=True, new_x="RIGHT", new_y="TOP")
                pdf.ln()

                # 每 42 行分页并重复表头
                if row_idx > 0 and row_idx % 42 == 0:
                    pdf.add_page()
                    pdf.set_font(pdf._cn_font, "B", 7)
                    pdf.set_fill_color(68, 114, 196)
                    pdf.set_text_color(255, 255, 255)
                    for col in self.df.columns:
                        text = self._truncate(str(col), col_width)
                        pdf.cell(col_width, 8, text, border=1, align="C",
                                 fill=True, new_x="RIGHT", new_y="TOP")
                    pdf.ln()
                    pdf.set_font(pdf._cn_font, "", 7)
                    pdf.set_text_color(51, 51, 51)

            pdf.output(path)
            return path

        except Exception as e:
            raise ExportError(f"PDF 导出失败: {e}") from e

    @staticmethod
    def _fmt(val) -> str:
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return ""
        if isinstance(val, float):
            return str(int(val)) if val == int(val) else f"{val:.4f}"
        return str(val)

    @staticmethod
    def _truncate(text: str, max_width: int) -> str:
        """按视觉宽度截断（中文字符占 2）"""
        length = 0
        result = []
        for ch in str(text):
            char_width = 2 if ord(ch) > 127 else 1
            if length + char_width > max_width - 2:
                result.append("..")
                break
            result.append(ch)
            length += char_width
        return "".join(result)
