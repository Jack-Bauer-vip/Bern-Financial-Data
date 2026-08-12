# Bern_Financial_Data — 金融数据中台 · 项目说明文档

> 本文件是**项目全貌的静态快照**,供外部 AI / 工程师在不读源码的情况下评估项目质量、发现问题、提出升级方向。
> 快照日期:**2026-08-06**。所有数字均为当日实测。若需最新细节,以 `CLAUDE.md` 与源码为准。

---

## 1. 项目定位

**个人投资者的本地桌面金融数据中台**(PySide6 GUI + 本地 FastAPI 数据服务 + SQLite)。

数据中台三件事:
1. **提取数据** — akshare / tushare / FRED 多数据源 + 文件导入(CSV/Excel)+ HTML 抓取;
2. **整理存储** — 增量同步、动态 schema、统一去重、指标归一层;
3. **分发数据** — FastAPI 通用查询 `/api/v1/data/{table}`,供个人投研的多个下游系统(ETF 看板、知识库、策略系统)调用。

设计目标:**统一、可信、最新的金融数据底座**,一库供多方。

---

## 2. 技术栈

| 层 | 技术 |
|---|---|
| 桌面端 | **PySide6** GUI(主窗口 ~1800 行)|
| 本地服务 | **FastAPI** + uvicorn,`http://127.0.0.1:8765`,Swagger `/docs`,鉴权 `X-API-Key` |
| 存储 | **SQLite**(WAL 模式),SQLAlchemy 动态建表 |
| 数据源 SDK | akshare / tushare / FRED(requests)|
| 抓取 | httpx + BeautifulSoup(规则在 `config/scrapers.yaml`)|
| 调度 | APScheduler(按顶层分类启停)|
| 依赖管理 | `pip install -e .[dev]`,pyproject.toml |
| 测试 | pytest(当前 **209 个全绿**,18 个测试文件,耗时 ~21s)|

**运行环境**:Windows 11,Python 3.12。git 仓库,本地 master 与 origin/master 同步。

---

## 3. 数据规模(2026-08-06 实测)

**库文件**:`data/berndata.db`。

| 指标 | 值 |
|---|---|
| 表数量 | 65(其中数据表 61、元数据表 4)|
| 基金日线 `fund_etf_daily` | **3,060,227 行**,2727 只 code,覆盖 **2005-01-04 → 2026-08-06**(全市场逐日完整)|
| 指数日线 `index_daily` | 有(2014 迁移修复过,见 commit c618e0a)|
| 宏观表 | 61 张 `macro_*`(中国/美国/欧洲/英国/日本/澳大利亚 + FRED 序列)|
| 元数据表 | `meta_sync_jobs`(63 行)、`meta_indicator`(20 行)、`meta_column_registry`(4 行)、`meta_cache_entries`(0 行)|

**数据源配置**(`config/data_catalog.yaml`,71 个叶节点):
- **akshare 50**、**fred 20**、**tushare 1**(基金日线)。
- **20 个 deprecated**(akshare 美国宏观源,上游聚合站停更;有 FRED 获信源的消费端不受影响)。
- 顶层分类:全球宏观数据 / 股票数据 / 指数数据 / 基金数据。
- **仅「全球宏观数据」启用定时调度**;股票/指数/基金靠 GUI 手动触发或 API。

---

## 4. 架构与数据流水线

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

**关键机制**:

1. **三路写入共用一套 `repository.bulk_upsert`**:API 同步 / 文件导入 / HTML 抓取,语义一致。
   - 唯一键:调用方显式传,或 `unique_key.py` 启发式推断(表名含 fund → `["code","date"]`)。
   - `ensure_unique_index` 先建唯一索引(有重复自动 `dedupe_rows` 清重),再 `ON CONFLICT DO UPDATE` 覆盖非键列。
   - `_sanitize_for_sql` 把 NaT/NaN/NA 转 None,防崩库。
2. **每数据源互斥锁**:`_get_sync_lock(table_name)` 进程级锁,防 GUI/API/调度并发同步同表;API 层还有 BoundedSemaphore(4)。
3. **动态列全 TEXT**:表列由 DataFrame 自动 `ALTER TABLE ADD COLUMN`;列名来自不可信输入,**写 SQL 一律 `_quote_table/_quote_column` 双引号转义**;API 通用查询有表名白名单防注入。
4. **指标归一层 `meta_indicator`**:数据本体留各源表,元表只存「indicator 键 → 获信源表+日期列+数值列」;`GET /api/v1/indicator/{key}` 统一返回 `{date,value}`;同步成功 `auto_adopt_indicator` 自动沿用首个能解析数值列的源(不覆盖手动选择)。
5. **指标派生视图**:`/indicator/{key}?transform=level|yoy|mom|pct`(`src/core/transform.py`,按日期中位间隔推断频率);原值(ODS)永不改,只派生。
6. **内存 TTL 缓存**:`src/core/ttl_cache.py` 缓存 `/indicator`/`/macro`/`/data`(无日期时);失效单点在 `SyncEngine.run()` 成功分支;**带日期参数时 /data 把区间过滤下沉 SQL**,绕缓存避免大表「先 limit 再过滤」裁错日期。
7. **数据新鲜度**:`src/core/freshness.py` 纯函数,供 GUI 健康检查 / `/health`(返回 stale_sources,排除 deprecated)/ CLI `check_freshness.py` 三方共用;**预期间隔按实际数据频率推断**(月频→32天/季频→95天),替代纯 cron 推断,避免 FRED 月频误报停更。CLI 可配 webhook 推送(env `FRESHNESS_WEBHOOK_URL`)。
8. **基金日线批量同步**:`SyncEngine.run_fund_daily_batch(start_date=None, end_date=None)`。
   - 原理:tushare `fund_daily(trade_date=)` 一次返回当天全市场 ~2000 只,按交易日拉取(替代逐个 code 的 1000 次调用,提速数百倍)。
   - **增量模式**(不传参):起点 = 表内 max(date)+1 → 今天。
   - **回溯模式**(传 start_date):拉历史全市场,**跳过已全市场覆盖日期**(`count_rows_by_date` 该日行数 >= `FUND_FULL_MARKET_THRESHOLD=1000`),幂等。
   - CLI:`scripts_gen/sync_fund_batch.py --start-date 2005-01-01 [--end-date]`,与 `--days N`(先清再拉最近 N 交易日)互斥。

---

## 5. 模块地图

| 模块 | 职责 | 关键点 |
|---|---|---|
| `src/api/` | FastAPI 本地服务 | `server.py` 中间件顺序:连接追踪→鉴权→CORS;`routes.py` 表名白名单、`_df_to_json_records` 清洗 NaN/NaT/Timestamp |
| `src/core/` | 同步引擎、取数、FRED、调度、动态 schema | `sync_engine.py` 最核心(含基金批量);`transform.py` 指标派生;`ttl_cache.py` 内存缓存;`freshness.py` 纯函数 |
| `src/db/` | SQLAlchemy 引擎/元数据表/通用访问层 | `repository.py` bulk_upsert、去重、唯一/普通索引、`count_rows_by_date` |
| `src/export/` | CSV/Excel/PDF 导出 | — |
| `src/gui/` | PySide6 桌面界面 | `main_window.py` 最大(~1800 行);`dialogs/` 各对话框(导入/分析/健康/分类管理等) |
| `src/importer/` | 文件导入、表识别、列映射、AI 兜底 | `matcher.py` 规则优先+AI 兜底+降级;表头模板缓存 |
| `src/scraper/` | HTML 抓取(httpx+BeautifulSoup) | 规则在 `config/scrapers.yaml`,GUI 可管理 |
| `src/utils/` | 配置、日志、catalog 读写 | `ConfigManager` 单例;`catalog_io.py` 校验(source_key 唯一/cron 5 段/保存自动 .bak) |

**GUI 结构**:
- 左侧数据分类树(DataTreeWidget,双击加载本地表、右键设为指标获信源)→ 右侧:参数面板(动态渲染 params_template)+ 数据表格(DataTableView)+ 底部彩色日志(LogWidget,跨线程信号安全)。
- **表格性能**(2026-08-06 修复):`PandasModel.data()` 原每格 `iloc[:, col].dtype`(复制整列 O(行数)),5000 行大表滚动卡顿;改为 setDataFrame 时一次性 `tolist()` 缓存列值 + 数值列标志,绘制路径纯列表索引 O(1),实测提速 6.5x;日期排序下沉到后台 QueryWorker,主线程只做 model reset。
- 同步期间防卡:300ms 单发 timer 防抖 + 后台 QueryWorker + 结果队列。

---

## 6. 文件导入识别链路(importer)

```
表头模板精确命中(data/header_templates.json) → 直接路由
   ↓ 未命中
规则评分(match_table: 列名重叠/Jaccard/日期列/样本类型, 阈值 0.7)
   ↓ 低置信或并列歧义
AI 兜底(ollama deepseek-r1:14b 或 deepseek API; 返回表名必须在校验集合内防幻觉)
   ↓ AI 不可用
降级回规则 top1
```
- 基金行情文件:`detect_fund_code` 从文件名识别 6 位基金代码前缀 → 路由 `fund_etf_daily` 注入 code 列。
- 会话级识别缓存,批量导入不重复调 AI。

---

## 7. 近期工作(commit 历史,均已在 origin/master)

| commit | 内容 |
|---|---|
| `dc342f5` | 数据中台三天加固:季度日期修复 / NaT 防御 / 指标派生视图 / 内存缓存 / data_status / 新鲜度 CLI |
| `c618e0a` | index_daily 迁移脚本 + /data 大表日期过滤下沉 SQL + 文档 |
| `8df4437` | **基金日线批量同步**(按交易日补全市场,替代逐个 code,提速数百倍)|
| `b4c04a3` | 三个真实信号修复:akshare 美国宏观 16 源标 deprecated / 新鲜度按数据频率 / indicator 默认返回 float |
| `28fdd3f` | 同步期间 GUI 无响应 — 表格刷新改后台+防抖 |
| `f9df262` | 桌面端启动即崩溃 — setUniformRowHeights 在 PySide6 此版本不存在(改固定行高)|
| `7fd6a87` | **表格卡顿根因修复** — PandasModel 绘制路径 O(N)→O(1) + 排序下沉后台(实测 6.5x)|
| `d32c8b6` | **基金批量支持历史回溯** — 补全 2023-2025(后扩至 2005-2022)全市场 ETF 历史 |

**本次数据补齐成果**(2026-08-06 两次回溯):
- 2023-2025 全市场 ETF 历史 + 2026 补全(总 code 2089→2111)。
- **2005-2022 全市场历史**(此前严重缺失:2016 年库内 123 vs tushare 全市场 588)。
- 抽查 2005/2010/2016/2022 各一交易日,库内行数与 tushare 全市场**逐一分毫不差**。
- 总表从 116 万行 → **306 万行**,覆盖 2005-01-04 至今。
- 注意:早期年份含封闭式基金/LOF(tushare `fund_daily` 是全市场口径),非纯 ETF。

**2026-08-11 更新(外部调研执行项落地,详见 `docs/external_research_2026-08-11.md`)**:
- **行情复权**:`?adj=qfq|hfq` 派生 + 独立因子表 `asset_adj_factor`(ODS 原值不动);股票/ETF 支持,指数/宏观 422。
- **对外 Claude Code skill 契约**:`skills/bern-financial-data/SKILL.md` —— 静态契约(15 端点全文档化 + curl/requests 可复制示例 + 数据语义防误读)+ **动态发现**(`/sources`、`/data/tables`、`/indicator`、`/boards`、`/health` 活清单)。`scripts_gen/install_skill.py` 装到 `~/.claude/skills/bern-financial-data/`。`tests/test_skill_contract.py` 双向契约一致性(文档化路径 ⊆ api_router 注册路由 ⊆ 文档化),防契约与代码漂移。`/data/{table}` 新增 `?code=` 单标的过滤。
- **check_freshness 静默停更识别**、**DuckDB ATTACH 演进路径**见 §8-13。

---

## 8. 已知问题 / 未决事项(供分析重点)

### 数据口径
1. **CPI/PPI 口径已语义化(2026-08-06 P0)**:FRED 源存**指数水平值**、akshare 源存**变化率**,已通过 `unit_type`/`unit_desc` 贯穿 DB→catalog→repository→sync engine→API→GUI 显式标注(FRED=level,akshare 同比表=yoy、环比表=mom),`/indicator/{key}` 响应带 `meta:{unit_type,unit_desc,transform}` 供下游识别。**仍待**:默认获信源的最终定案(手动切换即可)与下游分析确认消费口径。
2. **4 个无 FRED 源的美国指标仍在监控**:ISM 制造业/非制造业、CB 消费者信心、NFIB 小企业乐观(走 akshare 全量重拉,上游可能已停更)。
3. **FRED 7 月月频数据停更**:CPI/失业率/非农/PPI 等月频仍停 2026-06-01,因官方约 8 月中旬才发布 7 月值(属正常等待,届时增量同步自动补)。

### 功能缺口
4. **GUI「全部同步」进度显示已补(2026-08-06 P2)**:「全部同步」一键入口(菜单)已有,现连接 `sync_progress` 信号 —— 状态栏进度条 + 「正在同步 (n/63)」文本,run_all 逐源推进,结束自动收起(经离屏冒烟验证)。
5. **指标管理中心每行未显示数据截止日**(GUI 增强)。
6. **AI 模型切换未落地**:本地 ollama 用 deepseek-r1:14b;用户希望切 qwen2.5:7b(改 `default.yaml` 一行),未执行。

### 工程 / 运维观察
7. **后台长任务被系统 kill**:本次 2005-2022 回溯期间,后台任务先后 3 次被终止(运行 1h48m / 1.5m / 后又续传)。断点续传设计(upsert 幂等)兜住了,但**根因未排查**(超时?内存?系统策略?)。**2026-08-06 P1 已补可观测性**:长任务每交易日写 `last_heartbeat`,`meta_sync_jobs` 增 `running_status`/`last_heartbeat`,健康检查对「running 且 >10min 无心跳」标「疑似僵死」——若再被杀,健康表直接可见,不再靠猜。
8. **`meta_cache_entries` 0 行**:查询缓存路径"非核心,未广泛使用"——缓存机制存在但实际利用率低。
9. **`meta_column_registry` 仅 4 行**:动态加列登记机制存在,但登记记录很少(可能是动态列使用不频繁,或登记逻辑未充分触发)。
10. **早期数据语义**:2005-2017 的 fund_etf_daily 含封闭式基金/LOF,若下游按「ETF」口径使用需过滤,否则会污染统计。
11. **表结构与类型**:全部 TEXT 存储(数值字符串),查询时靠 pandas 自动转类型;大表 ORDER BY / 日期过滤依赖索引(`ensure_indexes.py` 建普通索引,`/data` 大表按 date+code 复合索引过滤)。SQLite 单写者 + WAL,高并发写受限。
12. **日期存储改 YYYYMMDD 已评估并拒绝(P3)**:当前定长 ISO TEXT(YYYY-MM-DD)+ 复合索引在 300 万行量级无瓶颈;改存储需迁移 3M 行存量 + 重写整条日期解析/比较链路,属负优化,维持现状。
13. **存储演进路径(2026-08-11 调研,见 `docs/external_research_2026-08-11.md`)**:当前 SQLite 对日频/月频 + 增量小写入是**正确选择**(单行 INSERT ~1ms、WAL 单写多读;DuckDB 单行插入是反模式 10-50ms)。**当下不迁移**。若将来上分钟级/tick 数据:①DuckDB 可**只读 `ATTACH` SQLite**(`TYPE sqlite, READ_ONLY`)零迁移直接做分析(列式聚合比 SQLite 快 10-100 倍);②规范存储层用 Parquet 分区(hive 分区按日期,分区剪枝 + ZSTD 压缩约 CSV 1/8),旧分区不可变、幂等增量;③增量采集仍留 SQLite(WAL)负责实时写入。分钟级接入路径(tdx2db/mootdx)已记入 CLAUDE.md 待办,暂不做。

### 已修复、待真机验证
13. **GUI 表格卡顿修复(7fd6a87)** 与 **同步防卡(28fdd3f)**:代码级已修复并提交,但**用户在真机上拖动/同步验证**尚未确认。

---

## 9. 快速命令

```bash
pip install -e .[dev]                    # 安装(dev 含 pytest)
python src/main.py                       # 启动桌面端(start_bern.bat 等效)
python -m pytest tests/ -q               # 测试(223 全绿,~21s)
python scripts_gen/gen_report.py --date 2026-08-04   # 日报 PDF
python scripts_gen/check_freshness.py --only-stale    # 数据新鲜度(退出码 1=有停更)
python scripts_gen/ensure_indexes.py                  # 为 catalog 表建普通索引(幂等)
python scripts_gen/migrate_index_daily.py             # 一次性修复 index_daily(2014→今)
python scripts_gen/sync_fund_batch.py                 # 基金日线增量批量(表内 max+1 → 今天)
python scripts_gen/sync_fund_batch.py --start-date 2005-01-01   # 历史回溯(跳过已全市场覆盖)
```

- 本地 API:`http://127.0.0.1:8765`,Swagger `/docs`,鉴权 `X-API-Key` 头(非 URL token),`.env` 配 `API_TOKEN`,留空不鉴权。
- `.env` 不入库:TUSHARE_TOKEN / FRED_API_KEY / DEEPSEEK_API_KEY / API_TOKEN(模板 `.env.example`)。
- 新增/改数据源**改 `config/data_catalog.yaml`**(不写代码),GUI「分类管理」可视化编辑。

---

## 10. 给分析 AI 的提示

- 这份文档是**静态快照**;如需代码级细节,请求时请说明关注点(如:并发安全 / SQL 注入面 / 缓存一致性 / GUI 性能 / 数据正确性),以便定位到具体文件。
- 已知的 11 个未决事项(§8)是分析重点,可逐条给出**根因判断 + 修复优先级 + 建议方案**。
- 架构层面的可讨论点(供发散):全 TEXT 存储的取舍、SQLite 单写者上限、多源异构(FRED 水平值 vs akshare 变化率)的口径归一策略、指标获信源的自动化、长任务的断点续传与任务编排、GUI 线程模型、测试覆盖缺口(目前 223 个测试主要覆盖 importer/api/sync_engine/fred/indicator/fund_batch,2026-08-06 P4 补了 PandasModel 绘制路径与同步锁回归,对 GUI 其他路径覆盖仍少)。
