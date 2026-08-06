# DATA_GOVERNANCE — 数据治理规范

> 本文件定义 Bern_Financial_Data 中台对外的**数据使用规范**。所有下游消费端(AI 分析、导出、其他模块)应遵守本文档,保证对同一指标的理解一致。
> 架构与数据流水线见 `CLAUDE.md`;系统全貌与数据规模见 `docs/project_overview.md`。

---

## 1. 默认获信源:FRED 优先原则

- **宏观指标默认获信源优先锁定 FRED(level 值)**:FRED 为官方修订后数据,序列连续、语义稳定。当前 20 个美国宏观指标全部已自动沿用 FRED 为获信源。
- **akshare 源仅作候选/对照**:akshare 美国宏观接口存的是**变化率**(同比/环比),且依赖上游聚合站(20 个源已标 `deprecated`,保留历史但不再同步)。
- 获信源切换:在 GUI「指标管理中心」或 `PUT /api/v1/indicator/{key}` 手动选择,持久化于 `meta_indicator`。**切换前请确认消费端理解新源的 `unit_type` 语义**(见 §3)。

## 2. 消费强制派生(严禁直接读原始变化率)

- 下游统一走 `GET /api/v1/indicator/{key}` 获取指标序列。
- 需要**同比/环比**时,**必须**使用 `?transform=yoy|mom|pct` 由服务端派生,响应带 `meta:{unit_type, unit_desc, transform}` 供消费端校验。
- **严禁**直接把 akshare 源表的原始变化率列当最终数值消费 —— 库内值是 ODS 原始存储,语义由 `unit_type` 标注,不保证与 `transform` 派生结果口径一致。
- 原始数据(ODS)只存不改,派生只在查询时计算(`src/core/transform.py`)。

## 3. unit_type 语义表

| unit_type | 含义 | 代表源 |
|---|---|---|
| `level` | 水平值(指数/百分比/人数绝对值) | FRED 全量(CPI 指数 1982-84=100、失业率 4.2%、非农人数千人等) |
| `yoy` | 同比变化率(%) | akshare `macro_usa_cpi_yoy`、`gdp_monthly` 等 |
| `mom` | 环比变化率(%) | akshare `core_cpi_monthly`、`ppi`、`retail_sales` 等 |

同一指标(如 `us.cpi`)可有 level 与 yoy 两个源,消费端按需选择并**校验响应的 `meta.unit_type`**。

## 4. deprecated 源约定

- 20 个 deprecated 源(上游聚合站停更,仅保留历史)**:不作获信源候选**,`/sources` 与 `/data/{table}` 响应带 `data_status`(active/deprecated/local)。
- 消费端应处理 `data_status == "deprecated"` 的表,避免把已停更数据当最新。

## 5. 数据使用注意事项

- **`fund_etf_daily` 2005-2017 含封闭式基金/LOF**:若下游按「ETF」口径统计回测,需按标的类型过滤,否则污染统计。GUI 加载该表时顶部有黄色警告条提示。
- 基金日线批量同步按交易日补全市场,`2026-08-06` 起当日盘后数据 tushare 晚间才发布,白天同步可能 0 行,属正常。

## 6. 新鲜度 / 停更约定

- 预期间隔按**实际数据频率**推断(月频 32 天 / 季频 95 天),而非纯 cron 推断,避免 FRED 月频误报停更。
- FRED 7 月月频数据官方约 **8 月中旬**才发布,8 月初显示滞后属正常等待,届时增量同步自动补。
- 健康检查(CLI `check_freshness.py --webhook` / GUI 健康对话框)对「running 且 >10 分钟无心跳」标**疑似僵死**。

## 7. 已知运维观察(附注)

以下事项经评估**不改代码**,记录供后续决策:
- **后台长任务曾被系统 kill**(2005-2022 回溯期间 3 次):断点续传兜住、P1 心跳已可视化;根因疑为 Windows 控制台会话空闲/CPU 占用低被系统回收,**代码层无法根治**。物理方案:改 `pythonw.exe` 无窗口运行或 `schtasks` 以 SYSTEM 权限启动(按需采用)。
- **AI 模型切换 qwen2.5:7b**:当前 deepseek-r1:14b 已过 223 测试;ollama 切模型存在参数量差异导致内存溢出风险,标记为「按需手动」,不做自动化。
- **`meta_cache_entries` 0 行**:落库缓存表为早期设计,查询路径实际走内存 TTL 缓存(`src/core/ttl_cache.py`),该表保留但无运行时写入。
- **日期存储**:定长 ISO TEXT(YYYY-MM-DD)+ 复合索引,3M 行量级无瓶颈;已评估**拒绝**改 YYYYMMDD(迁移存量属负优化)。

## 8. 决策记录

| 日期 | 决策 | 依据 |
|------|------|------|
| 2026-08-06 | 默认获信源优先 FRED(level 值),下游消费强制走 `/indicator?transform=` 派生 | FRED 为官方修订后数据,akshare 变化率语义不一致;统一 ODS 层稳定性 |
| 2026-08-06 | `fund_etf_daily` 2005-2017 数据含封闭式基金/LOF,GUI 显示黄色警告 | 防止下游策略系统将早期非 ETF 产品计入回测,污染统计 |
| 2026-08-06 | VACUUM 做纯脚本 `scripts_gen/vacuum_and_archive.py`,不做 GUI 设置开关 | 避免撬动 settings_dialog 配置持久化链路;WAL 模式空闲页自动回收,VACUUM 仅作异常膨胀时手动瘦身 |

---

*首次建立:2026-08-06(v1.0-stable 基线)。新增规则时在 §8 追加决策记录。*
