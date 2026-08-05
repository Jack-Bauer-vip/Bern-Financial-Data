# -*- coding: utf-8 -*-
"""通用日报/周报 PDF 生成器 — Bern_Financial_Data

用法：
  python scripts_gen/gen_report.py --date 2026-08-04
  python scripts_gen/gen_report.py --date 2026-08-04 --md my_file.md --out out.pdf --no-test

内容来源：scripts_gen/reports/<date>.md（Markdown 子集），支持：
  # / ## / ###    标题
  -               列表
  **key：**值      键值行（key 加粗，值正文）
  | a | b | c |   表格（第二行 --- 为分隔符，自动跳过）
  普通段落        正文
  {{占位符}}       自动填充，支持：
    {{date}}         日期
    {{git_commits}}  当日 git 提交（自动采集）
    {{git_files}}    当日改动文件（自动采集）
    {{test_summary}} 全量测试结果（自动运行 pytest）
"""
import argparse
import os
import re
import subprocess
import sys
from datetime import date

from fpdf import FPDF

FONT = "C:/Windows/Fonts/msyh.ttc"
FONT_B = "C:/Windows/Fonts/msyhbd.ttc"
REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
EXPORT_DIR = os.path.join("data", "export")


def clean(s):
    """微软雅黑不含彩色 emoji，渲染前替换为文本符号"""
    return (s.replace("✅", "[OK]").replace("🔴", "[停更]").replace("⚠", "[警告]")
             .replace("🟢", "[正常]").replace("🟡", "[滞后]").replace("⚪", "[未同步]")
             .replace("🇺🇸", "(US)").replace("🇪🇺", "(EU)").replace("🇨🇳", "(CN)")
             .replace("→", "->").replace("…", "..."))


# ---------------------------------------------------------------------------
# PDF 样式（复用现有导出器的样式）
# ---------------------------------------------------------------------------

class Report(FPDF):
    def __init__(self, title):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.title = title
        self.set_auto_page_break(auto=True, margin=18)
        self.add_font("YaHei", "", FONT, uni=True)
        self.add_font("YaHei", "B", FONT_B, uni=True)

    def footer(self):
        self.set_y(-15)
        self.set_font("YaHei", "", 7)
        self.set_text_color(160, 160, 160)
        self.cell(0, 8, "第 %d 页" % self.page_no(), align="C")

    def h1(self, text):
        self.set_font("YaHei", "B", 20)
        self.set_text_color(30, 60, 110)
        self.multi_cell(0, 10, clean(text), align="C")
        self.ln(2)
        self.set_font("YaHei", "", 9.5)
        self.set_text_color(120, 120, 120)
        self.cell(0, 6, "项目：Bern_Financial_Data 金融数据中台  |  Windows 11 / Python 3.12",
                  new_x="LMARGIN", new_y="NEXT", align="C")
        self.set_draw_color(40, 90, 160)
        self.set_line_width(0.8)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(5)

    def h2(self, text):
        self.ln(3)
        self.set_font("YaHei", "B", 12.5)
        self.set_text_color(30, 60, 110)
        self.cell(0, 7, clean(text), new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(150, 170, 200)
        self.set_line_width(0.3)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(2)

    def body(self, text):
        self.set_font("YaHei", "", 9.5)
        self.set_text_color(40, 40, 40)
        self.multi_cell(0, 5.2, clean(text))
        self.ln(1)

    def bullet(self, text):
        self.set_font("YaHei", "", 9.5)
        self.set_text_color(40, 40, 40)
        self.set_x(self.l_margin + 3)
        self.multi_cell(0, 5.2, clean("  • " + text))
        self.ln(0.5)

    def kv(self, key, val):
        self.set_font("YaHei", "B", 9.5)
        self.set_text_color(30, 60, 110)
        self.set_x(self.l_margin + 4)
        self.multi_cell(0, 5.2, clean(key))
        self.set_font("YaHei", "", 9.5)
        self.set_text_color(40, 40, 40)
        self.set_x(self.l_margin + 8)
        self.multi_cell(0, 5.2, clean(val))
        self.ln(1)

    def table(self, headers, rows):
        n = len(headers)
        widths = [(self.w - 2 * self.l_margin) / n] * n
        self.set_font("YaHei", "B", 8.5)
        self.set_text_color(30, 60, 110)
        self.set_fill_color(235, 240, 248)
        for h, w in zip(headers, widths):
            self.cell(w, 7, clean(h), border=1, fill=True, align="C")
        self.ln()
        self.set_font("YaHei", "", 8.3)
        self.set_text_color(40, 40, 40)
        for row in rows:
            if self.get_y() > self.h - 28:
                self.add_page()
            for cell, w in zip(row, widths):
                self.cell(w, 6.5, clean(str(cell)), border=1, align="L")
            self.ln()
        self.ln(2)


# ---------------------------------------------------------------------------
# 自动采集
# ---------------------------------------------------------------------------

def _run(cmd):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", cwd=os.getcwd())
        return (r.stdout or "").strip()
    except Exception:
        return ""


def git_commits(day: str) -> list[str]:
    """当日 git 提交：['hash subject', ...]"""
    out = _run('git log --since="%s 00:00" --until="%s 23:59" --format="%%h %%s"'
               % (day, day))
    return [l for l in out.splitlines() if l.strip()]


def git_files(day: str) -> list[str]:
    """当日改动的文件清单（去重，去掉 data/ 运行期目录）"""
    out = _run('git log --since="%s 00:00" --until="%s 23:59" --name-only '
               '--pretty=format:' % (day, day))
    files = []
    seen = set()
    for l in out.splitlines():
        l = l.strip()
        if l and l not in seen and not l.startswith("data/"):
            seen.add(l)
            files.append(l)
    return files


def run_tests() -> str:
    """运行全量测试，返回摘要行；失败时给出错误数量"""
    out = _run("python -m pytest -q 2>&1")
    m = re.search(r"(\d+) passed.*?(\d+) failed", out)
    if m:
        return "%s passed, %s failed" % (m.group(1), m.group(2))
    m = re.search(r"(\d+) passed", out)
    if m:
        return "%s passed" % m.group(1)
    m = re.search(r"(\d+) failed", out)
    if m:
        return "0 passed, %s failed" % m.group(1)
    return "测试结果：未解析（命令输出为空或出错）"


# ---------------------------------------------------------------------------
# Markdown 渲染
# ---------------------------------------------------------------------------

def _render_table(pdf, rows):
    """rows: 去掉首行表头分隔符后的原始行"""
    if not rows:
        return
    parsed = []
    for line in rows:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        parsed.append(cells)
    if not parsed:
        return
    # 找分隔行（|---|）
    header_idx = None
    for i, cells in enumerate(parsed):
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
            header_idx = i
            break
    if header_idx is not None:
        headers = parsed[:header_idx][0]
        data = parsed[header_idx + 1:]
    else:
        headers = parsed[0]
        data = parsed[1:]
    pdf.table(headers, data)


def render_markdown(pdf, md: str, ctx: dict):
    """渲染 Markdown 子集。ctx 提供占位符替换。"""
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        line = raw.strip()

        # 占位符单独成行 → 展开为多行
        if line.startswith("{{") and line.endswith("}}"):
            key = line.strip("{}").strip()
            if key in ctx and ctx[key]:
                for sub in ctx[key].split("\n"):
                    if sub.strip():
                        pdf.bullet(sub.strip())
                i += 1
                continue

        # 空行
        if not line:
            i += 1
            continue
        # 标题
        if line.startswith("### "):
            pdf.h2(line[4:].strip())
        elif line.startswith("## "):
            pdf.h2(line[3:].strip())
        elif line.startswith("# "):
            pdf.h1(line[2:].strip())
        # 表格：收集连续 | 行
        elif line.startswith("|") and i + 1 < len(lines) and \
                lines[i + 1].strip().startswith("|"):
            block = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                block.append(lines[i])
                i += 1
            _render_table(pdf, block)
            continue
        # 键值行 **key：**值
        elif line.startswith("**") and "**" in line[2:]:
            m = re.match(r"^\*\*(.+?)\*\*\s*[:：]?\s*(.*)$", line)
            if m:
                key = m.group(1).strip()
                if not key.endswith(("：", ":")):
                    key += "："
                pdf.kv(key, m.group(2))
                i += 1
                continue
            pdf.body(line)
        # 列表
        elif line.startswith("- "):
            pdf.bullet(line[2:].strip())
        # 普通段落
        else:
            pdf.body(line)
        i += 1


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="生成日报/周报 PDF")
    ap.add_argument("--date", default=date.today().isoformat(),
                    help="日期 YYYY-MM-DD（默认今天）")
    ap.add_argument("--md", default=None, help="内容文件路径（默认 reports/<date>.md）")
    ap.add_argument("--out", default=None, help="输出 PDF 路径")
    ap.add_argument("--no-test", action="store_true", help="跳过自动运行测试")
    args = ap.parse_args()

    day = args.date
    md_path = args.md or os.path.join(REPORTS_DIR, "%s.md" % day)
    if not os.path.exists(md_path):
        print("内容文件不存在:", md_path)
        print("请先创建（参考 reports/_template.md）")
        sys.exit(1)

    with open(md_path, "r", encoding="utf-8") as f:
        md = f.read()

    # 上下文：自动采集
    commits = git_commits(day) or ["（当日无提交）"]
    files = git_files(day) or ["（当日无文件改动）"]
    ctx_inline = {
        "{{date}}": day,
        "{{test_summary}}": run_tests() if not args.no_test else "测试：跳过（--no-test）",
    }
    ctx_list = {
        "git_commits": "\n".join(commits),
        "git_files": "\n".join(files),
    }
    # 内联占位符替换（出现在普通句子中间的）
    for k, v in ctx_inline.items():
        md = md.replace(k, v)

    out = args.out or os.path.join(EXPORT_DIR, "%s_系统升级工作总结.pdf" % day)
    os.makedirs(os.path.dirname(out), exist_ok=True)

    pdf = Report("%s 系统升级工作总结" % day)
    pdf.add_page()
    pdf.h1("%s 系统升级工作总结" % day)
    # 单独成行的列表占位符（git_commits/git_files）由渲染器展开为列表
    render_markdown(pdf, md, ctx_list)
    pdf.output(out)
    print("PDF 已生成:", out)
    print("大小:", os.path.getsize(out), "bytes")


if __name__ == "__main__":
    main()
