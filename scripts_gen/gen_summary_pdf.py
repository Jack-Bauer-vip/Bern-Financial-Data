# -*- coding: utf-8 -*-
"""生成《2026-08-04 系统升级工作总结》PDF"""
import os
from fpdf import FPDF

OUT = os.path.join("data", "export", "2026-08-04_系统升级工作总结.pdf")
FONT = "C:/Windows/Fonts/msyh.ttc"
FONT_B = "C:/Windows/Fonts/msyhbd.ttc"


def clean(s):
    """微软雅黑不含彩色 emoji，PDF 渲染前替换为文本符号"""
    return (s.replace("✅", "[OK]").replace("🔴", "[停更]").replace("⚠", "[警告]")
             .replace("🟢", "[正常]").replace("🟡", "[滞后]").replace("⚪", "[未同步]")
             .replace("🇺🇸", "(US)").replace("→", "->"))


class Report(FPDF):
    def __init__(self):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.set_auto_page_break(auto=True, margin=18)
        self.add_font("YaHei", "", FONT, uni=True)
        self.add_font("YaHei", "B", FONT_B, uni=True)

    def footer(self):
        self.set_y(-15)
        self.set_font("YaHei", "", 7)
        self.set_text_color(160, 160, 160)
        self.cell(0, 8, "第 %d 页" % self.page_no(), align="C")

    def h1(self, text):
        self.set_font("YaHei", "B", 16)
        self.set_text_color(30, 60, 110)
        self.multi_cell(0, 8, text, align="L")
        self.ln(1)
        self.set_draw_color(40, 90, 160)
        self.set_line_width(0.6)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(4)

    def h2(self, text):
        self.ln(3)
        self.set_font("YaHei", "B", 12.5)
        self.set_text_color(30, 60, 110)
        self.cell(0, 7, text, new_x="LMARGIN", new_y="NEXT")
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
        self.multi_cell(0, 5.2, clean(text))
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

    def table(self, headers, rows, widths):
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


pdf = Report()
pdf.add_page()

# ===== 标题区 =====
pdf.set_font("YaHei", "B", 22)
pdf.set_text_color(30, 60, 110)
pdf.multi_cell(0, 11, "2026-08-04 系统升级工作总结", align="C")
pdf.ln(2)
pdf.set_font("YaHei", "", 9.5)
pdf.set_text_color(120, 120, 120)
pdf.cell(0, 6, "项目：Bern_Financial_Data 金融数据中台  |  环境：Windows 11 / Python 3.12",
         new_x="LMARGIN", new_y="NEXT", align="C")
pdf.set_draw_color(40, 90, 160)
pdf.set_line_width(0.8)
pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
pdf.ln(5)

# ===== 1 =====
pdf.h2("1. 今日一句话总结")
pdf.body("完成 FRED 数据源的全面铺开与落地：目录新增至 20 个 FRED 源并全部真实同步进生产库（3010 行、自动沿用为获信源），同时交付指标管理中心 GUI 与数据源树「设为获信源」快捷入口，实现美国宏观数据从「akshare 全量重拉」到「FRED 官方增量」的切换。")

# ===== 2 =====
pdf.h2("2. 分项目工作总结")

pdf.h2("  2.1 宏观数据源（FRED 接入与铺开）")
pdf.kv("已完成并验证：",
       "新增 src/core/fred_client.py（FRED API 客户端，支持 observation_start/end 真正增量更新），DataFetcher 增加 fred 分发。"
       "目录新增 20 个 FRED 源（原 5 个试点 + 新增 15 个：核心CPI/零售/初请/PPI/核心PCE/工厂订单/耐用品/贸易帐/工业产出/新屋开工/成屋销售/个人支出/密歇根信心/30Y/2Y国债），"
       "全部经 FRED 官方 API 逐一验证序列存在后加入。20 个 FRED 源已真实同步进生产库 data/berndata.db："
       "20 张 macro_fred_* 表全部建出，共 3010 行，同步零失败。")
pdf.kv("当前状态：", "20 个指标全部自动沿用 FRED 为获信源；get_indicator 统一查询可用（实测 us.unemployment、us.bond_yield_10y 返回真实数据）。")
pdf.kv("涉及文件：", "src/core/fred_client.py、src/core/data_fetcher.py、config/data_catalog.yaml、.env.example、config/default.yaml、src/utils/config.py")
pdf.kv("后续注意：", "ISM制造业/非制造业、Conference Board信心、NFIB 共 4 个指标 FRED 无对应序列，保留 akshare（仍全量重拉）；口径差异见风险节。")

pdf.h2("  2.2 指标归一层（meta_indicator）")
pdf.kv("已完成并验证：", "meta_indicator 映射表 + get_indicator/set_indicator/indicator_candidates/list_indicators 统一接口 + /indicator API（GET/PUT/LIST）；同步后自动沿用获信源（auto_adopt）；set_indicator 支持对目录声明但未同步的表预配置，get_indicator 对空列映射动态解析。")
pdf.kv("当前状态：", "20 个指标全部映射到 FRED 表，数据本体仍在各源表（不复制）。")
pdf.kv("涉及文件：", "src/db/models.py、src/db/repository.py、src/api/routes.py")

pdf.h2("  2.3 指标管理中心 GUI（优先级2）")
pdf.kv("已完成并验证：", "新增 IndicatorManagerDialog（菜单「数据->指标管理中心」），表格列出全部 20 个指标、候选来源、获信源下拉（切换即持久化）、最新数据日期与统计；未设获信源显示占位项。")
pdf.kv("涉及文件：", "src/gui/dialogs/indicator_manager_dialog.py（新）、src/gui/main_window.py")

pdf.h2("  2.4 数据源树快捷入口（优先级4）")
pdf.kv("已完成并验证：", "数据源树右键叶节点（仅 indicator 源）出现「设为获信源」，复用 _set_as_trusted_source 公共逻辑。")
pdf.kv("涉及文件：", "src/gui/tree_widget.py、src/gui/main_window.py")

pdf.h2("  2.5 数据导入体验与日期解析（承接此前）")
pdf.kv("已完成并验证：", "表头记忆模板（统一表头 O(1) 路由）、导入确认框改可滚动 ConfirmListDialog、基金代码筛选可输入、修复导入识别时误触目标表下拉导致「无响应」（识别期禁用下拉+忽略变更+复用表快照三层防御）、中文年月日解析（2008年01月 -> 2008-01）修复 NaT 崩溃。")
pdf.kv("涉及文件：", "src/importer/header_template.py（新）、src/gui/dialogs/import_dialog.py、src/gui/dialogs/confirm_list_dialog.py（新）、src/importer/matcher.py、src/core/sync_engine.py")

pdf.h2("  2.6 测试与验证 / 2.7 Git 版本管理")
pdf.body("全量测试 149 passed，5 warnings（既有 deprecation 提示，无失败）。FRED 同步 20/20 成功，生产库实测通过（详见第 4 节）。")
pdf.body("今日两次提交，工作区已干净（仅剩 data/ 运行期目录，按 .gitignore 不入库）："
         "a87152d (16:02) 多轮综合改进——FRED 接入、指标归一层、导入体验、日期解析修复（21 文件，+2238/-105）；"
         "73e297d (16:39) FRED 铺开 + 指标管理中心 + 树右键设为获信源（6 文件，+428/-19）。")

# ===== 3 =====
pdf.h2("3. 改动文件与输出文件")
pdf.h2("  核心代码文件")
for f in ["src/core/fred_client.py（新）", "src/core/data_fetcher.py", "src/core/sync_engine.py",
          "src/db/models.py", "src/db/repository.py", "src/api/routes.py",
          "src/importer/header_template.py（新）", "src/importer/matcher.py", "src/utils/config.py",
          "src/gui/main_window.py", "src/gui/param_panel.py", "src/gui/tree_widget.py",
          "src/gui/dialogs/import_dialog.py", "src/gui/dialogs/confirm_list_dialog.py（新）",
          "src/gui/dialogs/indicator_manager_dialog.py（新）"]:
    pdf.bullet("  - " + f)
pdf.h2("  配置文件")
for f in ["config/data_catalog.yaml（20 个 FRED 源 + indicator 配对键）",
          "config/default.yaml（fred: 块、import: 块）",
          ".env.example（FRED_API_KEY 占位；实际 .env 已配置，敏感内容不写入本总结）"]:
    pdf.bullet("  - " + f)
pdf.h2("  测试文件")
for f in ["tests/test_fred_client.py", "tests/test_header_template.py", "tests/test_indicator.py",
          "tests/test_import_worker.py", "tests/test_sync_engine.py"]:
    pdf.bullet("  - " + f)
pdf.h2("  数据文件（运行期，不入 git）")
pdf.bullet("  - data/berndata.db：20 张 macro_fred_* 表，3010 行。数据截至：失业率 2026-06-01（月频）、10Y 国债 2026-07-31（日频）、GDP 2026-04-01（季频）等，均 status=completed")
pdf.bullet("  - Prompt / 报告 / PDF：今日无相关输出（未提供）。")

# ===== 4 =====
pdf.h2("4. 测试与验证结果")
pdf.table(
    ["验证项", "方式", "结果"],
    [
        ["全量回归", "python -m pytest -q", "149 passed, 5 warnings"],
        ["FRED 客户端单测", "test_fred_client.py", "11 passed"],
        ["表头记忆", "test_header_template.py", "通过"],
        ["指标层", "test_indicator.py", "15 passed"],
        ["NaT 日期解析", "test_sync_engine.py", "6 passed"],
        ["导入 worker", "test_import_worker + import", "27 passed"],
        ["FRED 序列存在性", "实时 series/info, 19 候选", "15 确认 + 5 既有；4 无对应"],
        ["FRED 全量同步", "一次性脚本, 生产库", "20/20 成功, 3010 行"],
        ["自动沿用获信源", "get_indicator_map", "20/20 指向 FRED 表"],
        ["统一查询", "get_indicator('us.unemployment')", "真实数据(2023-12 失业率 3.8)"],
        ["导入无响应修复", "offscreen 冒烟", "三路径通过"],
        ["GUI 新对话框", "offscreen 冒烟", "构造、信号正常"],
    ],
    [45, 60, 68],
)
pdf.body("未能验证的项目与原因：qwen2.5:7b 模型对比——仅给评估结论，未实施（用户另项目下载中）；15 个新 FRED 源二次增量同步——增量参数注入与 last_sync_date 记录已验证，但首次同步刚完成，未实测第二次增量。")

# ===== 5 =====
pdf.h2("5. 风险和遗留问题")
pdf.h2("  影响数据口径/日更的风险")
pdf.bullet("  - 口径差异（需人工确认）：CPI/PPI/核心CPI/核心PCE 的 FRED 表存指数水平值（如 1982-84=100），akshare 表存环比/同比变化率。当前 20 个指标获信源=FRED，消费端看到水平值；若需变化率，需切回 akshare 获信源或增设变化率计算。")
pdf.bullet("  - 4 个指标仍依赖 akshare（ISM制造业/非制造业、Conference Board信心、NFIB）：akshare 美国宏观函数无日期参数，仍每次全量重拉；有聚合站停更风险。")
pdf.h2("  需人工确认的数据")
pdf.bullet("  - 获信源口径选择（水平值 vs 变化率）——当前默认 FRED 水平值。")
pdf.h2("  已设计但尚未实施")
pdf.bullet("  - 指标变化率/同比环比视图；")
pdf.bullet("  - GUI「全部同步」一键按钮（今日用脚本完成同步，未做成界面入口）；")
pdf.bullet("  - 表头记忆 expected_columns 目录声明（优先级3）——经评估放弃：已有表由 seed_from_db 覆盖、独特结构源由 column_map 覆盖、20 个 FRED 源列结构相同会互相歧义，声明无收益；")
pdf.bullet("  - qwen2.5:7b 模型切换（仅改 config/default.yaml 一行，用户下载后自行切换）。")

# ===== 6 =====
pdf.h2("6. 下一步优先级")
for i, t in enumerate([
    ("确认 CPI/PPI 类指标口径（高优先）", "明确要水平值还是变化率，决定是否给 4 个价格类指标切换获信源或加变化率视图。影响所有后续分析和导出。"),
    ("GUI「全部同步」按钮（中优先）", "今日同步靠脚本完成，做成界面入口后 FRED 铺开才对日常使用闭合。"),
    ("4 个无 FRED 源指标的停更监控（中优先）", "akshare 聚合源有停更风险，纳入健康检查备用源逻辑。"),
    ("验证 FRED 二次增量同步（低优先）", "实测 15 个新源第二次同步只拉新数据，坐实增量收益。"),
    ("qwen2.5:7b 实测（低优先）", "用户下载完成后切换模型，真实跑一次识别+分析，验证格式稳定性与速度。"),
    ("指标管理中心增强（低优先）", "每行显示数据截止日，便于判断该选哪个源。"),
], 1):
    pdf.kv("%d. %s" % (i, t[0]), t[1])

# ===== 7 =====
pdf.h2("7. 可复制简短汇报版（150-300 字）")
pdf.set_font("YaHei", "", 9.5)
pdf.multi_cell(0, 5.2, clean(
    "【2026-08-04 系统升级】今日完成 FRED 数据源全面落地：数据目录新增至 20 个 FRED 官方源"
    "（核心CPI/PPI/核心PCE/零售/初请/工业产出/国债等，序列均经 FRED API 验证），并全部真实同步进生产库"
    "（20 张新表、共 3010 行，同步零失败），20 个美国指标自动沿用 FRED 为获信源，实现美国宏观从 akshare 全量重拉"
    "切换到官方增量。同时交付「指标管理中心」GUI（集中查看/切换获信源）和数据源树右键「设为获信源」快捷入口。"
    "系统代码今日两次提交（a87152d、73e297d），工作区已干净，全量测试 149 通过。"
    "说明：CPI/PPI 类 FRED 存指数水平值、akshare 存变化率，口径差异待确认；"
    "ISM/CB信心/NFIB 四个指标无 FRED 序列仍走 akshare。"
    "下一步：确认价格类指标口径、做 GUI 一键全量同步按钮。"
    "（数据截至 2026-08-04，同步状态 completed）"
))
pdf.ln(2)
pdf.set_font("YaHei", "", 8)
pdf.set_text_color(150, 150, 150)
pdf.multi_cell(0, 4.5, "本文档由 Claude Code 依据当日对话、Git 提交、测试结果与生产库同步日志自动汇总生成；涉及金融数据均已标明口径与截止日期，未将 AI 生成内容表述为已验证事实。")

pdf.output(OUT)
print("PDF 已生成:", OUT)
print("大小:", os.path.getsize(OUT), "bytes")
