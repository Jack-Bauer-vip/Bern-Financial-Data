# -*- coding: utf-8 -*-
"""中文日期字符串归一化 — 纯函数模块（无外部依赖，不引入 PySide6）

akshare 宏观源常见中文日期格式（"2008年01月"、"2026年07月份"、"2009第4季度"），
pandas 的 pd.to_datetime 无法直接解析。本模块提供统一的归一化入口，
供写入路径（sync_engine._clean_data）、读取路径（freshness 新鲜度推断、
repository.get_last_date 兜底）复用，保证各链路对同一格式行为一致。
"""

import re

# 季度序号映射（汉字 + 阿拉伯数字）；用于 akshare 季度日期归一化
_QUARTER_NUM = {"一": 1, "二": 2, "三": 3, "四": 4,
                "1": 1, "2": 2, "3": 3, "4": 4}
_QUARTER_RE = re.compile(r"(?:第?\s*([1-4一二三四])\s*季度|[Qq]\s*([1-4]))")


def normalize_cn_date_str(value) -> str:
    """把中文年月日/季度字符串归一化为 pd.to_datetime 可解析的形式

    覆盖 akshare 常见格式：
    2008年01月 / 2008年1月        → 2008-01（年+月、无日）
    2026年07月份 / 2026年7月份     → 2026-07（带"份"尾缀）
    2008年01月15日               → 2008-01-15
    2009第4季度 / 2009年第4季度    → 2009-12（季度末月）
    2009年第四季度 / 2009Q4       → 2009-12
    非字符串 / 无中文日期字样的原样返回。
    """
    if not isinstance(value, str):
        return value
    s = value.strip()
    # 季度优先：显式解析为季度末月（3/6/9/12）。
    # 注意 replace("年","-") 链对 "2009第4季度" 产不出 ISO，且该串不含"年月日"
    # 会被下方的中文检测挡掉，故需在此先行匹配（含纯 ASCII 的 2009Q4）。
    m = _QUARTER_RE.search(s)
    if m:
        ym = re.search(r"(\d{4})", s)
        if ym:
            q = _QUARTER_NUM.get(m.group(1) or m.group(2), 1)
            return f"{ym.group(1)}-{q * 3:02d}"
    if not any(c in s for c in "年月日"):
        return s
    # 先摘掉"份"（"2026年07月份" 的"月"被替换后"份"会残留成非法串）
    s = s.replace("份", "")
    s = s.replace("年", "-").replace("月", "-").replace("日", "")
    # 去掉尾部残留分隔符（无日期的年月会留下 "2008-01-"）
    s = re.sub(r"[-/]+$", "", s).strip()
    return s
