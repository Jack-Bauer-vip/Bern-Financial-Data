# -*- coding: utf-8 -*-
"""指数分类工具 — 纯函数，无网络依赖（供 scripts_gen/sync_index_category.py 与 /indices 端点使用）

分类体系（理杏仁式）：宽基 / 行业 / 主题 / 风格 / 策略 / 债券 / 跨境 / 其他

- `classify_by_heuristics(name)`：按名称关键词做启发式分类（自动底，无网络、确定性可单测）
- `parse_index_category_config(yaml_text)`：解析 config/index_categories.yaml
  （manual 手动精修覆盖 + global 跨境策划清单 + categories 固定顺序）
- `build_category_rows(codes_names, config)`：合并 手动覆盖 → 宽基白名单 → 启发式 → 其他

akshare 无现成指数分类字段（v1.18.81 实测 index_stock_info 仅 code/name/publish_date 三列），
故以「规则分类」为主方案：名称关键词 + 宽基全名白名单，主流指数再经 YAML 手动精修。
"""

from __future__ import annotations

import re
import yaml

# 分类固定顺序（看板 chip 顺序；YAML 可覆盖）
DEFAULT_CATEGORIES = ["宽基", "行业", "主题", "风格", "策略", "债券", "跨境", "其他"]

# ---------------------------------------------------------------------------
# 启发式关键词组（按优先级从高到低）
# ---------------------------------------------------------------------------

# 风格（价值/成长/红利/低波/高贝/质量/规模等；先于主题/行业，避免「300价值」「消费红利」误判）
_STYLE = (
    "红利", "股息", "低波", "低波动", "波动", "低贝", "高贝", "稳定",
    "动态", "动量", "质量", "蓝筹", "周期", "非周期", "非周",
    "大盘价值", "中盘价值", "小盘价值", "大盘成长", "中盘成长", "小盘成长",
    "大盘低波", "中盘低波", "小盘低波", "巨潮大盘", "巨潮中盘", "巨潮小盘",
    "防御", "波指", "价值", "成长",
)

# 策略（基本面 / ESG 治理责任 / 等权分层 / 现金流）
_STRATEGY = (
    "基本面", "基本", "ESG", "治理", "责任",
    "等权", "等权重", "EW", "分层", "现金流",
    "龙头", "绩效", "分析师",
)

# 央视系列单独前置（「央视成长/回报/治理」整体归策略，不让 成长/回报 抢先归风格）
_CCTV = "央视"

# 债券（含转债/信用债/企债；先于主题，避免「碳中和债」误判为主题）
_BOND = (
    "国债", "企债", "企业债", "转债", "信用债", "信用", "公司债", "债",
)

# 主题
_THEME = (
    "新能源", "光伏", "风电", "半导体", "芯片", "人工智能", "AI", "云计算",
    "大数据", "机器人", "物联网", "区块链", "网络安全", "信息安全", "安全",
    "碳中和", "低碳", "环境", "新基建", "稀土", "锂", "储能", "氢", "疫苗",
    "创新药", "医美", "免税", "白酒", "酒", "一带一路", "丝路", "乡村振兴",
    "数字经济", "元宇宙", "猪肉", "跨境电商", "专精特新", "小巨人", "高端装备",
    "智能制造", "工业4.0", "高铁", "TMT", "长三角", "珠三角", "环渤海", "大湾区",
    "长江", "皖江", "钱江", "养老", "体育", "互联网金融", "移动互联网", "智能",
    "次新股", "并购重组", "定向增发", "专利", "中关村", "安防", "科技", "创新",
    "新兴", "创投", "主题", "国企改革",
    "央企", "国企", "民企",   # 所有制主题（上证央企/中证国企/民企200…；红利类被风格优先级抢先）
    "国有企业", "国有", "民营", "地企", "沪企", "综企", "企综", "沪股通",  # 国企/民企/地企系列全名
    "高新", "技术领先", "小康", "持续产业", "文化", "海峡", "率先", "金牛",
    "兴全", "泰达", "银河", "GDP", "时钟", "投资品", "深报",
    "基金", "ETF", "乐富", "新能", "数字", "软件", "科研",
)

# 行业（官方宽口径：申万/中证/上证/深证/300/1000/全指行业）
_INDUSTRY = (
    "银行", "证券", "券商", "保险", "非银", "地产", "房地产", "煤炭", "石油",
    "有色", "钢铁", "化工", "电力", "电子", "计算机", "通信", "传媒", "国防",
    "军工", "汽车", "家电", "机械", "建筑", "建材", "基建", "环保", "公用",
    "交运", "运输", "物流", "航空", "旅游", "酒店", "餐饮", "商贸", "批零",
    "零售", "纺织", "轻工", "农业", "农林牧渔", "食品饮料", "食品", "医药", "生物",
    "医疗", "金融", "能源", "材料", "工业", "可选", "消费", "信息", "IT", "电信",
    "资源", "原材料", "有色金属", "大宗商品", "行业", "装备",
    "上游", "中游", "下游", "制造", "高装", "农林", "采矿", "水电", "公共",
    "商业", "商务", "服务", "原料", "商品", "大宗",
)

# 宽基全名白名单（自动底：去掉尾缀「指数」后精确匹配；主流另走 YAML manual）
_WIDE_BASE = frozenset({
    "上证指数", "A股指数", "新综指", "中型综指", "上证全指", "上证流通",
    "上证100", "上证150", "上证380", "上证180", "上证50", "超大盘",
    "上证中盘", "上证小盘", "上证中小",
    "沪深300",
    "中证A500", "中证100", "中证200", "中证500", "中证700", "中证800",
    "中证1000", "中证流通", "中证超大", "中证全指", "中证中小盘700",
    "科创综指", "科创50", "科创100", "科创200",
    "深证成指", "深证综指", "深证A指", "深证100", "深证300", "深证200",
    "深证700", "深证1000", "深证中小创新", "深市精选", "深主板50", "深证50",
    "中小100", "中小300", "中小综指",
    "创业板指", "创业板综合", "创业板综", "创业板50", "创业200", "创业小盘",
    "创业大盘", "创业300", "创精选88",
    "中创100", "中创400", "中创500", "深创100",
    "国证2000", "国证1000", "国证300", "国证50", "国证A指", "巨潮100",
    "大中盘", "中小盘",
})

# 风格子类细分（供 sub_category）
_STYLE_SUB = (
    ("红利", "红利"), ("股息", "红利"),
    ("低波", "低波"), ("低波动", "低波"), ("波动", "低波"), ("低贝", "低波"),
    ("稳定", "低波"),
    ("高贝", "高贝"), ("动态", "高贝"), ("动量", "高贝"),
    ("质量", "质量"),
    ("大盘价值", "大盘价值"), ("中盘价值", "中盘价值"), ("小盘价值", "小盘价值"),
    ("大盘成长", "大盘成长"), ("中盘成长", "中盘成长"), ("小盘成长", "小盘成长"),
    ("大盘低波", "大盘低波"), ("中盘低波", "中盘低波"), ("小盘低波", "小盘低波"),
    ("巨潮大盘", "大盘"), ("巨潮中盘", "中盘"), ("巨潮小盘", "小盘"),
    ("蓝筹", "蓝筹"),
    ("价值", "价值"), ("成长", "成长"),
)


def _sub_for_style(name: str) -> str:
    """风格子类细分：红利/低波/价值/成长/高贝/质量/规模…；未命中 → ''"""
    for kw, sub in _STYLE_SUB:
        if kw in name:
            return sub
    return ""


def _strip_idx_suffix(n: str) -> list[str]:
    """候选名（原样 + 去「指数」/「指」尾缀），供宽基白名单匹配

    注意不能用 str.rstrip("指数")——它按字符集剥离，会把「上证指数」剥成「上证」、
    「创业板指」剥成「创业板」，从而漏过白名单里的全名。这里只精确剥「指数」/「指」。
    """
    cands = [n]
    if n.endswith("指数"):
        cands.append(n[:-2])
    if n.endswith("指"):
        cands.append(n[:-1])
    return cands


def classify_by_heuristics(name: str) -> tuple[str, str] | None:
    """按名称做启发式分类；返回 (category, sub_category) 或 None（无法归类）

    优先级：宽基全名白名单 → 央视(策略) → 风格 → 债券 → 主题 → 策略 → 行业。
    - 宽基用「去尾缀精确匹配」白名单（锚定全名，避免「中证1000成长」误判宽基）
    - 央视系列前置（「央视成长/回报/治理」整体归策略，不拆给风格/主题）
    - 风格先于主题/行业（如「消费红利」→ 风格红利，「300价值」→ 风格）
    - 债券先于主题（如「碳中和债」→ 债券）；主题先于策略（如「中证环境治理指数」→ 主题）
    """
    if not name:
        return None
    n = str(name).strip()

    # 宽基白名单（原样 + 去「指数」/「指」尾缀候选匹配）
    if any(c in _WIDE_BASE for c in _strip_idx_suffix(n)):
        # 规模子类：超大/超大盘/大盘 → 大盘；小盘/中小 → 中小盘；其余综合
        if "超大盘" in n or "超大" in n:
            return "宽基", "大盘"
        if "小盘" in n or "中小" in n or "中盘" in n:
            return "宽基", "中小盘"
        return "宽基", ""

    if _CCTV in n:
        return "策略", ""
    for kw in _STYLE:
        if kw in n:
            return "风格", _sub_for_style(n)
    # 基本面加权 F 系列(上证F200/深证F120…): F+数字 → 策略 基本面
    if re.search(r"[Ff]\d{2,3}", n):
        return "策略", "基本面"
    for kw in _BOND:
        if kw in n:
            return "债券", ""
    for kw in _THEME:
        if kw in n:
            return "主题", ""
    for kw in _STRATEGY:
        if kw in n:
            return "策略", ""
    for kw in _INDUSTRY:
        if kw in n:
            return "行业", ""
    # 市值规模(宽基)放最后: 风格/主题/策略/行业关键词优先
    # (如 中小绩效→策略绩效、中小新兴→主题新兴、中小治理→策略治理);
    # 300沪市/500深市 区域子集、市值百强/中证超级大盘/财富大盘 → 宽基
    for kw in ("中小", "超大", "大盘", "小盘", "中盘", "百强", "沪市", "深市"):
        if kw in n:
            if "大盘" in n or "超大" in n or "百强" in n:
                return "宽基", "大盘"
            if "中小" in n or "中盘" in n:
                return "宽基", "中小盘"
            if "小盘" in n:
                return "宽基", "小盘"
            return "宽基", ""
    return None


# ---------------------------------------------------------------------------
# config/index_categories.yaml 解析
# ---------------------------------------------------------------------------


def parse_index_category_config(yaml_text: str) -> dict:
    """解析 config/index_categories.yaml → {categories, manual, global}

    - categories: 分类顺序（看板 chip 顺序）
    - manual: {code: {category, sub_category}} 手动精修（覆盖自动分类）
    - global: {code: {name, category, sub_category, api_function}}
    """
    data = yaml.safe_load(yaml_text) or {}
    return {
        "categories": data.get("categories") or list(DEFAULT_CATEGORIES),
        "manual": data.get("manual") or {},
        "global": data.get("global") or {},
    }


def build_category_rows(codes_names: dict[str, str], config: dict) -> list[dict]:
    """合并生成全量分类行：境内(手动覆盖→自动分类→其他) + 跨境(策划清单)

    Parameters
    ----------
    codes_names : {code: name} 境内指数（来自 meta_asset_info, asset_type=index）
    config : parse_index_category_config 的结果

    Returns
    -------
    [{code, name, category, sub_category, source}]
    """
    manual: dict = config.get("manual", {})
    global_cfg: dict = config.get("global", {})
    rows: list[dict] = []

    for code, name in codes_names.items():
        # 跨境策划清单统一在下方处理：避免与 meta_asset_info 重叠时
        # 被自动分类(如 HSI 恒生指数 → 其他)抢先,去重后策划行丢失
        if code in global_cfg:
            continue
        name = name or ""
        over = manual.get(code)
        if over:
            cat = over.get("category", "其他")
            sub = over.get("sub_category", "") or ""
            source = "manual"
        else:
            auto = classify_by_heuristics(name)
            if auto:
                cat, sub = auto
                source = "auto"
            else:
                cat, sub, source = "其他", "", "auto"
        rows.append({
            "code": code, "name": name,
            "category": cat, "sub_category": sub, "source": source,
        })

    # 跨境策划清单（分类与数据解耦：无需行情在库也可先分类；与境内重叠的码以策划为准）
    for code, item in global_cfg.items():
        rows.append({
            "code": code,
            "name": item.get("name", ""),
            "category": item.get("category", "跨境"),
            "sub_category": item.get("sub_category", "") or "",
            "source": "curated",
        })
    return rows
