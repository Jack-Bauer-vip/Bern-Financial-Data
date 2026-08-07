# CLAUDE.md — Bern_Financial_Data 金融数据中台

本地化、增量更新的**桌面金融数据中台**(PySide6 GUI + 本地 FastAPI 数据服务)。
汇总数据(API / 文件上传 / HTML 抓取)入库 SQLite,供查询、导出、AI 分析、数据分发。
**全项目注释、输出、会话均使用简体中文。**

**项目定位(数据中台三件事)**:① **提取数据**——akshare/tushare/FRED 多源 + 文件导入 + HTML 抓取;② **整理存储**——增量同步、动态 schema、统一去重、指标归一层;③ **分发数据**——FastAPI 通用查询 `/api/v1/data/{table}`,供其他模块/分析系统调用。目标是为个人投研的多个下游系统提供统一、可信、最新的金融数据底座。

**已知下一步优先级**(2026-08-07 更新;口径/截止日/进度条/FRED 二次增量/停更源治理已落地,见 `docs/DATA_GOVERNANCE.md` 决策记录):
1. ~~确认 CPI/PPI 口径~~ **已定案**:默认获信源 FRED 优先(level 值),下游消费强制 `?transform=` 派生 → 见 `docs/DATA_GOVERNANCE.md` §1-3
2. ~~GUI「全部同步」一键按钮~~ **已落地**:P2 状态栏进度条(见 main_window `_on_sync_progress`)
3. ~~4 个无 FRED 源指标停更监控~~ **闭环达成**:ISM制造业/ISM非制造业/CB信心/NFIB 已在 08-05 标 deprecated(在 20 个 deprecated 内),健康检查不再告警。2026-08-07 新增 `health_check_ignore` 静默机制(见下「健康检查静默」)承接同类需求
4. ~~验证 FRED 二次增量同步~~ **已验证(2026-08-07)**:20 个 FRED 源全部走增量模式只拉缺失区间;刚同步过的源再跑返回 0 行;债券日频补到 08-05、周频初请失业金 08-01;月频 7 月值官方未发布(FRED API 直接查证,约 8 月中旬)
5. ~~qwen2.5:7b 模型切换~~ **已完成(2026-08-07)**:与用户另一系统共用该模型,commit `b9458f9`
6. ~~指标管理中心每行显示数据截止日~~ **已实现**(第 5 列,`indicator_manager_dialog.py`)
7. ⏰ **FRED 7 月月频补数(约 8 月中旬)**:CPI/失业率/非农/PPI 等月频仍停 06-01,FRED 官方约 8 月中旬发布 7 月值,届时跑一次增量同步自动补(真增量只拉缺失区间)
8. 🛠 **打包/分发到其他电脑(待决策,2026-08-07 评估)**:可行,项目对打包友好(GUI 零资源文件/data 自动创建/ollama 缺失优雅降级)。候选路线:PyInstaller `--onedir` 免安装(~800MB,需改造)或绿色 venv 便携包。**前置改造 3 处**:①`logger.py:54` `Path.cwd()` 改数据目录;②路径基于 `__file__` 的 root_dir 与打包只读资源分离(需加 frozen/_MEIPASS 检测);③`start_bern.bat` 硬编码路径改相对。**安全注意**:`.env` 含明文 FRED_API_KEY 且被 git 跟踪(与 .gitignore 声明不符),分发前处理。详见计划备忘录 `humble-greeting-stallman.md` 与记忆文件

## 快速命令

```bash
pip install -e .[dev]        # 安装(dev 含 pytest)
python src/main.py           # 启动桌面端(start_bern.bat 等效)
python -m pytest tests/ -q   # 跑测试(当前 238 个全绿)
python scripts_gen/gen_report.py --date 2026-08-04   # 日报 PDF
python scripts_gen/vacuum_and_archive.py             # DB 瘦身(默认 500MB 阈值,超才 VACUUM)
python scripts_gen/check_freshness.py --only-stale   # 数据新鲜度(退出码 1=有停更)
python scripts_gen/ensure_indexes.py                 # 为 catalog 表建普通索引(幂等)
python scripts_gen/migrate_index_daily.py            # 一次性修复 index_daily(2014→今)
python scripts_gen/sync_fund_batch.py                # 基金日线批量同步(按交易日补全市场)
```

- 本地 API:`http://127.0.0.1:8765`,Swagger 在 `/docs`。鉴权用 `X-API-Key` 头(非 URL token),`.env` 配 `API_TOKEN`,留空则不鉴权。
- 数据源配置在 `config/data_catalog.yaml`(不是写死代码)。改数据源/新增数据源**改 YAML**,树形 GUI 在「分类管理」对话框。

## 架构总览(数据流水线)

```
data_catalog.yaml (分类树) ──FetcherRegistry 拍平──> source_key → {api_source, api_function, table_name, ...}
                                                          │
SyncEngine.run(source_key)                               │  同步/导入/抓取都走同一写入路径
  ├─ 增量起点: 有 code → 该 code 实际 max(date); 无 → meta_sync_jobs.last_sync_date
  ├─ DataFetcher.fetch()  → akshare / tushare / fred(_call_fred, api_function=FRED序列ID)
  ├─ 行情列规范化 _apply_column_map (中文列→date/open/high/low/close/volume/amount + 注入 code/symbol)
  ├─ DynamicSchemaManager.ensure_columns → 自动建表/加列(全 TEXT,动态列)
  ├─ _clean_data → 中文日期归一化 + 丢弃 NaT 行
  └─ repo.bulk_upsert(ON CONFLICT DO UPDATE, 唯一键由 unique_key.infer 启发式推断)
```

- **三路写入共用一套 `repository.bulk_upsert`**:API 同步(`SyncEngine`)、文件导入(`importer/`)、HTML 抓取(`scraper/`)。
- **每数据源互斥锁**:`sync_engine._get_sync_lock(table_name)` 进程级锁,防 GUI/API/调度并发同步同表。API 层还有 BoundedSemaphore(4) 限制并发同步线程。
- **动态列**:数据表列不是 ORM 定义的,由 `DynamicSchemaManager` 按 DataFrame 列自动 `ALTER TABLE ADD COLUMN`。列名来自导入/抓取等不可信输入,**写库 SQL 一律双引号转义**(`DataRepository._quote_table/_quote_column`)。
- **全 TEXT 存储**:建表列统一 TEXT,数值字符串存储(避免 SQLite DATE 类型兼容问题)。查询时 `pd.read_sql_query` 自动转回 pandas 类型。

## 数据分类树(data_catalog.yaml)

- 顶层分类:`全球宏观数据`(美/欧/日/英/中/澳,各国子节点,含「🇺🇸 FRED 官方数据」20 个 FRED 源)、`A股日线行情`、`指数日线`、`ETF基金日线`。共 78 个叶数据源(50 akshare / 20 fred / 1 tushare,其余为分类节点)。
- 叶节点关键字段:`source_key`(唯一)、`api_source`(akshare/tushare/fred)、`api_function`(函数名或 FRED 序列 ID)、`table_name`、`indicator`(指标归一层键)、`params`、`params_template`、`schedule_cron`、`column_map`(行情表源列→规范列)、`code_column`。
- 顶级分类节点有 `schedule_enabled` / `schedule_cron`,驱动 APScheduler 按板块启停。
- 校验逻辑在 `src/utils/catalog_io.py`(source_key 唯一性、叶节点必填、cron 5 段)。保存自动备份 `.bak`。

## 数据源

| api_source | 用法 | 增量 |
|---|---|---|
| `akshare` | `api_function`=akshare 函数名,`_call_ak` 按签名过滤参数 | 传 start_date/end_date,多数接口仍全量拉回再 upsert |
| `tushare` | token 在 `.env` TUSHARE_TOKEN;`_DataApi__http_url` 可指向代理(默认 ts.gyzcloud.top) | 同上 |
| `fred` | `api_function`=FRED 序列 ID(UNRATE/DGS10/CPIAUCSL…),key 在 `.env` FRED_API_KEY | **真增量**:observation_start/end,按 `{date,value}` 两列返回 |

- tushare 代码归一:`normalize_ts_code`(6 位补 .SH/.SZ/.BJ 后缀);写入 code 列前 `_strip_exchange` 去后缀,与 CSV 导入的 code 对齐。
- 代码类源(股票/指数/基金)增量起点**无条件用该 code 的实际 max(date)**,表级 last_sync_date 只在无 code 数据时兜底(见 sync_engine.run 注释)。

## 指标归一层(meta_indicator)

- 概念:数据本体留在各源表,`meta_indicator` 只存「indicator 键 → 获信源表 + 日期列 + 数值列」,供统一查询 `repo.get_indicator()` 返回 `{date, value}`。
- 同一指标可有多个源表(如 `us.cpi` 有 akshare 表和 FRED 表),用户可在 GUI「指标管理中心」或 API `PUT /indicator/{key}` 手动选择获信源。
- **自动沿用**:同步成功时 `auto_adopt_indicator` 把首个能明确解析数值列(今值/现值/value 等关键词)的源设为获信源;不覆盖用户已手动选择。
- 数值列解析:`_resolve_value_column(strict=True)` 只认明确数值列名,避免把 OHLC 表第一数值列误当指标值。

## 模块地图

| 模块 | 职责 | 关键点 |
|---|---|---|
| `src/api/` | FastAPI 本地服务 | `server.py` 中间件顺序:连接追踪→鉴权→CORS;`routes.py` 表名白名单防注入、`_df_to_json_records` 清洗 NaN/NaT/Timestamp(防 pydantic 序列化 bug) |
| `src/core/` | 同步引擎、取数、FRED、调度、动态 schema | `sync_engine.py` 最核心;`transform.py` 指标派生;`ttl_cache.py` 内存缓存;`freshness.py` 新鲜度纯函数 |
| `src/db/` | SQLAlchemy 引擎/元数据表/通用访问层 | `repository.py` bulk_upsert(`_sanitize_for_sql` 防 NaT/nan/NA 崩库)、去重、唯一/普通索引 |
| `src/export/` | CSV/Excel/PDF 导出 | — |
| `src/gui/` | PySide6 桌面界面 | `main_window.py` 1781 行(最大),`dialogs/` 各对话框;健康检查复用 `freshness.collect_source_freshness` |
| `src/importer/` | 文件导入、表识别、列映射、AI 兜底 | `matcher.py` 规则优先+AI 兜底+降级 |
| `src/scraper/` | HTML 抓取(httpx+BeautifulSoup) | 规则在 `config/scrapers.yaml`,GUI 可管理 |
| `src/utils/` | 配置、日志、catalog 读写、日期归一化 | `ConfigManager` 单例;`date_parse.py` 中文日期归一化(纯函数,无 PySide6 依赖) |

## 数据源生命周期 & 数据分发增强(2026-08-05 加固)

- **deprecated 标记**:`data_catalog.yaml` 叶节点加 `deprecated: true`。当前 **28 个**(live 验证):4 个无 FRED 的 akshare 美国源 + 16 个有 FRED 获信源的 akshare 美国宏观源 + **8 个欧元区源**(2026-08-07 验证,上游聚合站停更、数据停 2025-09,无 FRED 替代);**未标**:cpi_yoy(仍活 2026-07-01)、国债 3 个(新浪源仍活)。三处同步排除:`FetcherRegistry.get_all_enabled_sources()`(repository 委托它)、`scheduler._register_category` 跳过、`run_all` 不再拉。`/sources` 与 `/data/{table}` 响应带 `data_status`(active/deprecated/local)。**注意**:deprecated 源不作为 indicator 获信源候选。
- **health_check_ignore(健康检查静默白名单,2026-08-07 新增)**:`data_catalog.yaml` 叶节点加 `health_check_ignore: true`。源**保留 active 身份**(仍可查、可选为获信源、API 可见、继续调度同步),仅健康检查静默:CLI `check_freshness --only-stale`、`/health` stale_sources、GUI 健康对话框(显示为正常)三方过滤,不参与停更告警/退出码/webhook。**当前 7 个**:日本 4(从未同步的参考源)+ 中国 CPI年率/CPI月率/GDP年率(上游停更 300+ 天,保留作参考,无 FRED 替代)。区别于 deprecated:deprecated 是全方位排除,health_check_ignore 只静默告警。
- **指标派生视图**:`GET /api/v1/indicator/{key}?transform=level|yoy|mom|pct`。`src/core/transform.py` 按日期中位间隔推断频率,yoy/mom 百分比;transform!=level 时先拉全量再算(避免 limit 截断同比前值)。pct=mom 别名。**默认(无 transform 或 level)也 to_numeric 返回 float**(不返回库内字符串)。原值(ODS)永不改,只派生。
- **内存 TTL 缓存**:`src/core/ttl_cache.py` 进程内缓存 `/indicator`/`/macro`/`/data`(无日期时)。**失效单点**在 `SyncEngine.run()` 成功分支(GUI/全量/定时/API 四路都汇聚于此)。带日期参数时 /data 把区间过滤下沉 SQL(用 `ensure_index` 建的 date+code 复合索引),绕过缓存避免大表「先 limit 再过滤」裁错日期。
- **新鲜度**:`src/core/freshness.py` 纯函数(从 health_dialog 拆出),供 GUI 对话框/`/health`(返回 stale_sources/stale_count,排除 deprecated)/CLI `check_freshness.py` 三方共用。CLI 可配 webhook(env `FRESHNESS_WEBHOOK_URL`,钉钉/飞书/Server酱 通用)。**预期间隔优先按实际数据频率推断**(`infer_expected_days_from_dates` 复用 `transform.infer_frequency`:月频→32天/季频→95天),替代纯 cron 推断,避免 FRED 月频误报"停更"。
- **基金日线批量同步**:`SyncEngine.run_fund_daily_batch()`。fund_etf_daily 逐个 code 同步每次 2.2s(两段硬编码睡眠)、1000 只 ~37min;改为**按交易日批量**——tushare `fund_daily(trade_date=)` 一次返回当天全市场 ~2000 只,从表内最大日期+1 到今天逐交易日拉取,补 N 天只需 N 次调用。全市场入库(不限于库内已有 code)。GUI 同步「ETF基金日线」时(未选具体 code)自动走批量(`SyncFundBatchWorker`);CLI `scripts_gen/sync_fund_batch.py`。注意:今日盘后数据 tushare 晚间才发布,白天跑可能 0 行属正常。

## GUI 响应性(同步期间不卡)

- **根因**:`_refresh_current_table` 原在主线程直接 `repo.query` + `loadDataFrame`(全量 model reset)。逐 code 同步时每完成一个就触发一次,主线程被 N 次 DB 查询 + 重绘阻塞 → 拖动滚动条无响应。
- **修复**:`_refresh_current_table` 改为**300ms 单发 timer 防抖**合并同步期间的多次刷新,`_do_refresh_current_table` 用**后台 QueryWorker**查库(`_start_background_refresh`),主线程只在 `_apply_refresh_result` 里 `loadDataFrame`。`_refresh_in_progress`/`_refresh_pending` 保证同一时刻只跑一个查询、完成补刷一次。
- **table_view 优化**:`setUniformRowHeights(True)` + 固定行高 + ScrollPerPixel,降低 model reset 后的重建开销。

## 文件导入(importer)识别链路

```
表头模板精确命中(data/header_templates.json, 学习到可复用) → 直接路由
   ↓ 未命中
规则评分(match_table: 列名重叠/Jaccard/日期列/样本类型, 阈值 ai.rule_threshold=0.7)
   ↓ 低置信或并列歧义
AI 兜底(ollama deepseek-r1:14b 或 deepseek API; 返回表名必须在校验集合内防幻觉)
   ↓ AI 不可用
降级回规则 top1
```

- 基金行情文件:`detect_fund_code` 从文件名识别 6 位基金代码前缀(15/16/50/51/52/56/58/59)→ 路由到 `fund_etf_daily` 并注入 code 列。
- 会话级识别缓存:同表头+同候选表结构复用首次结果,避免批量导入重复调 AI。
- AI 不可用自动降级,不影响导入(`ai_client.py` 注释记录:ollama /api/chat 端点稳定,/api/generate 对 r1 不稳定)。

## 元数据表(meta_*,SQLAlchemy ORM)

- `meta_sync_jobs`:同步状态表,`module_name`(即 table_name)唯一。last_sync_date 驱动增量。
- `meta_column_registry`:动态加列登记(表/列/来源 API/首次发现时间)。
- `meta_cache_entries`:查询缓存(非核心,查询路径并未广泛使用)。
- `meta_indicator`:指标归一层(见上)。
- 数据表(`fund_etf_daily`、`macro_*` 等)是运行时动态建的,不在 models.py。

## 约定与注意事项

- **新增数据源**:编辑 `config/data_catalog.yaml`(有 GUI)。注意 `column_map` 只对行情类源配;宏观源表保持 API 原生列名。
- **日期格式**:akshare 常见 `2008年01月`(年+月无日,带"份"尾缀如 `2026年07月份`),统一走 `src/utils/date_parse.py` 的 `normalize_cn_date_str` 归一化再 to_datetime。写入路径(`_clean_data`)、读取路径(freshness 频率推断、`repository.get_last_date` 兜底)三处共用,保证同一格式行为一致;全部解析失败的行丢弃(防 NaT 崩库)。
- **SQL 安全**:表名/列名来自不可信输入(导入/抓取/用户),写 SQL 必须走 `_quote_table/_quote_column`;API 通用查询端点有白名单校验。
- **并发**:同步同一数据源受每源锁保护;加列有并发 IntegrityError 兜底(log_column_registry)。
- **.env 不入库**:TUSHARE_TOKEN / FRED_API_KEY / DEEPSEEK_API_KEY / API_TOKEN。模板见 `.env.example`。
- **git**:本地 master 领先 origin/master(13 commits)。`data/*.db`、`.env`、`*.bak`、`*.xlsx/pdf` 被 .gitignore 忽略;`data/cache`、`data/export`、`scripts_gen/` 目前未跟踪(其中 scripts_gen 是应入库的脚本,data/export 是导出产物)。

## 测试

- 149 个测试全绿(11s):`tests/` 覆盖 matcher、column_mapper、api、sync_engine、fred_client、importer、scraper、indicator、catalog_editor、header_template 等。
- 测试用临时 SQLite(engine 层),不污染 `data/berndata.db`。
