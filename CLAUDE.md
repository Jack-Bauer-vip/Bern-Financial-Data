# CLAUDE.md — Bern_Financial_Data 金融数据中台

本地化、增量更新的**桌面金融数据中台**(PySide6 GUI + 本地 FastAPI 数据服务)。
汇总数据(API / 文件上传 / HTML 抓取)入库 SQLite,供查询、导出、AI 分析、数据分发。
**全项目注释、输出、会话均使用简体中文。**

**项目定位(数据中台三件事)**:① **提取数据**——akshare/tushare/FRED 多源 + 文件导入 + HTML 抓取;② **整理存储**——增量同步、动态 schema、统一去重、指标归一层;③ **分发数据**——FastAPI 通用查询 `/api/v1/data/{table}`,供其他模块/分析系统调用。目标是为个人投研的多个下游系统提供统一、可信、最新的金融数据底座。

**已知下一步优先级**(2026-08-07 更新;口径/截止日/进度条/FRED 二次增量/停更源治理已落地,见 `docs/DATA_GOVERNANCE.md` 决策记录;主题看板+对外接口增强已落地):
1. ~~确认 CPI/PPI 口径~~ **已定案**:默认获信源 FRED 优先(level 值),下游消费强制 `?transform=` 派生 → 见 `docs/DATA_GOVERNANCE.md` §1-3
2. ~~GUI「全部同步」一键按钮~~ **已落地**:P2 状态栏进度条(见 main_window `_on_sync_progress`)
3. ~~4 个无 FRED 源指标停更监控~~ **闭环达成**:ISM制造业/ISM非制造业/CB信心/NFIB 已在 08-05 标 deprecated(在 20 个 deprecated 内),健康检查不再告警。2026-08-07 新增 `health_check_ignore` 静默机制(见下「健康检查静默」)承接同类需求
4. ~~验证 FRED 二次增量同步~~ **已验证(2026-08-07)**:20 个 FRED 源全部走增量模式只拉缺失区间;刚同步过的源再跑返回 0 行;债券日频补到 08-05、周频初请失业金 08-01;月频 7 月值官方未发布(FRED API 直接查证,约 8 月中旬)
5. ~~qwen2.5:7b 模型切换~~ **已完成(2026-08-07)**:与用户另一系统共用该模型,commit `b9458f9`
6. ~~指标管理中心每行显示数据截止日~~ **已实现**(第 5 列,`indicator_manager_dialog.py`)
7. ~~**FRED 7 月月频补数**~~ **已闭环(2026-08-14)**:7 月值 FRED 官方已发布(CPI 332.813 / 核心CPI 336.789 / PPI 156.927(PPIFID) / 失业率 4.1 / 非农 158858.0),20 个 FRED 源增量同步只拉缺失区间全部到位(PPI 表今天补 1 行,其余 07-01 已在前序同步入库);日频债券收益率官方最新仍停 08-12(美东下午发布,无遗漏)
8. ~~**主题看板(定制数据集)+ 对外接口增强**~~ **已落地(2026-08-07)**:`config/themes.yaml` 定义主题(指标+口径+日期窗口/始终最新),GUI「数据→📋 主题看板」建主题看每日快照(最新值/环比/同比/近3期,可下钻/导出/同步本主题);API 新增 `/api/v1/boards`(列表/快照/时序宽表),所有 `/indicator`、`/data`、`/boards` 端点支持 `?format=csv`,可选 `?page=` 分页(meta.pagination);OpenAPI 版本升至 0.2.0、/docs 按 8 个 tag 分组。核心在 `src/core/boards.py`(BoardStore 读写校验 + BoardService 快照/宽表,复用 `repo.get_indicator`+`compute_transform`)。**v2 扩展(同日)**:主题条目支持两类——①宏观指标(老配置无 type 兼容);②**代码类条目** `type: code`(基金/股票/指数日线: `table`+`code_column`+`code`+`value_column`,快照=该代码最新一条值列+日期,环比/同比留空,series 仍可 `?transform=`)。条目经**数据分类树选择器**添加(`src/gui/dialogs/board_item_picker_dialog.py`,可搜索/折叠,deprecated 灰禁; `board_manager_dialog.py` 行改为摘要+「…」重选)。类型判定/代码列解析统一走 `boards.py` 模块级 `_item_type`/`classify_source`/`resolve_code_column`/`collect_code_sources`/`code_value_columns`(目录 code_column 优先,否则探测表内 code/symbol 列;读侧不剥前缀),校验 `validate_board` 按类型分流。测试 +15(`tests/test_boards.py` 34 个,含 code fixture/校验/快照/时序/API/混合主题)
9. 🛠 **打包/分发到其他电脑(待决策,2026-08-07 评估)**:可行,项目对打包友好(GUI 零资源文件/data 自动创建/ollama 缺失优雅降级)。候选路线:PyInstaller `--onedir` 免安装(~800MB,需改造)或绿色 venv 便携包。**前置改造 3 处**:①`logger.py:54` `Path.cwd()` 改数据目录;②路径基于 `__file__` 的 root_dir 与打包只读资源分离(需加 frozen/_MEIPASS 检测);③`start_bern.bat` 硬编码路径改相对。**安全注意**:`.env` 含明文 FRED_API_KEY 且被 git 跟踪(与 .gitignore 声明不符),分发前处理。详见计划备忘录 `humble-greeting-stallman.md` 与记忆文件
10. 📊 **外部项目调研(2026-08-11,见 `docs/external_research_2026-08-11.md`)**:评估结论——本系统架构当前阶段正确,无需推倒重来。4 项执行项**全部落地(2026-08-11)**:①**行情复权** → `?adj=qfq|hfq` 派生 + 因子表批量同步(`src/core/adj_factor.py`、`scripts_gen/sync_adj_factor.py`、`asset_adj_factor` 表);②**Claude Code skill + 对外 SKILL.md 契约** → `skills/bern-financial-data/SKILL.md`(静态契约 + 动态发现:15 端点全文档化、可复制 curl/requests 示例、数据语义防误读),`scripts_gen/install_skill.py` 装到 `~/.claude/skills/bern-financial-data/` 供任意项目激活,`tests/test_skill_contract.py` 双向契约一致性(文档化路径 ⊆ api_router 注册路由 ⊆ 文档化);③**check_freshness 静默停更** → `meta_sync_jobs.last_note` + CLI 备注列;④**DuckDB ATTACH 演进路径** → 记入 `docs/project_overview.md` §8-13。**排除**:free-stockdb C++ 引擎/商业 MCP/OpenBB/DAG 编排。
11. ⏸ **分钟级数据接入(tdx2db/mootdx,2026-08-11 记入待办,暂时不做)**:若将来需要分钟级行情,tushare 数据路径(按交易日批量)或 tdx2db(通达信本地数据入库,增量幂等+AGENTS.md)/mootdx(TCP 直连)是成熟方案。**注意**:通达信本地数据有版权/合规与停更风险。执行前置:存储层先按待办 #5(P2)演进到 DuckDB/Parquet(分钟级数据量 SQLite 分析性能不足)。
12. ✅ **网页只读看板(理杏仁风,2026-08-12)**:`http://127.0.0.1:8765/dashboard`(免鉴权)。纯静态前端 `src/api/web/`(index.html/style.css/stats.js/chart.js/app.js + vendored ECharts 5.5.1),**复用 /api/v1 数据端点,零后端逻辑改动**——后端只加 StaticFiles 挂载 + AuthMiddleware 前缀豁免 `/dashboard`(见 `server.py` `PUBLIC_PREFIXES`)。**阶段一(同日落地)**:① 顶部 **通用/指数/ETF/股票 类型预设 tab**(`app.js` `PRESETS`;指数=index_daily 禁复权、ETF=fund_etf_daily、股票=stock_daily,切 tab 重置 code/口径,未同步表显示 `#empty-hint` 空态不崩溃;URL 带 `type` 深链,旧链接无 type 零破坏);② **代码搜索下拉**(250ms 防抖 → `GET /search`,代码/中文名/拼音分层匹配,↑/↓ 高亮、Enter 选中、Esc/点外关闭,手输 code 回车保留;`meta_asset_info` 名称由 `scripts_gen/sync_asset_names.py` 旁路同步,改名最长 5 分钟生效);③ 行情预设下统计卡换 **多周期涨跌幅**(今日/5日/20日/60日/年初至今/1年,`stats.js` `computePeriodReturns` 纯函数,红涨绿跌、基期日期提示,ytd 前一年末→当年首日→表内最早 三段回退)。**阶段二(下周,空桩)**:K线 MA5/10/20/60 + 区间统计面板(`renderRangePanel`)。通用版原 7 卡保留:任意表/指标 → 统计卡片(最新值/最新日期/环比/同比/最高/最低/均值)+ 折线或 K线+成交量(红涨绿跌 A股惯例)+ 历史表格(列排序/分页/CSV 导出)。列识别启发式与后端对齐(`stats.js`:时间/数值/code/OHLC 探测,主列 close→value→同比/今值/现值;日期解析兼容 `2026年06月份`、`2026年一季度` 中文数字季度、ISO),同比按前一年同月/同季度尽力配对。URL 深链接(参数入 `history.replaceState`,可复制分享),token 弹窗输入存 localStorage。测试 `tests/test_dashboard.py`(6 个:静态可达/trailing-slash/资源可达/echarts 完整/鉴权豁免/带 token 数据可用)+ `tests/test_dashboard_js.py`(node-eval 测 `computePeriodReturns`,无 node 则 skip)。**注意**:需重启服务(旧进程是旧代码)浏览器才能看到。
13. 📋 **数据加工改进点(低优先级,2026-08-12 盘点知识库 1326 张卡后列入;数据中台定位是获取/清洗/整理/更新/分派,不过度加工,故仅备选)**:① **winsorize 派生**——如需抗异常统计,做 `?transform=winsorize` 纯派生(不改 ODS),暂不做;② **同步异常值检测**——同步后可选标记离群值(知识库 7 种离群检测方法,`?outlier=` 派生),暂不做;③ **DuckDB memorymap 宽表加速**——大表宽表分析提速参考(知识库 KBE-20260807-WoT6),已并入 docs/project_overview.md §8-13 演进路径,暂缓;④ **缺失值替代**——指标缺失填充,影响数据可信度,暂不做。
14. ✅ **指数分类(理杏仁式,2026-08-12)**:参考理杏仁对指数分类为 宽基/行业/主题/风格/策略/债券/跨境/其他 8 类,**748 条**(732 境内 + 16 跨境)。**分类实现**:akshare 无指数分类接口 → 规则启发式为主(`src/core/index_categories.py` 纯函数 `classify_by_heuristics`,关键词+宽基白名单,优先级 宽基→央视→风格→债券→主题→策略→行业)+ `config/index_categories.yaml` 手动精修主流(~60 条)+ 兜底「其他」;写 `meta_index_category`(code/name/category/sub_category/source∈{auto,manual,curated},`scripts_gen/sync_index_category.py` 幂等生成)。**API**:`GET /api/v1/indices`(理杏仁式分类清单,`?category=` 精确过滤 `?q=` 代码/名称模糊(字母码大小写不敏感) `?limit=`;`meta.categories=[{category,count}]` 供看板 chip 计数;TTL 缓存 `index_categories`)。**跨境指数数据**(16 个,复用 `index_daily.symbol`,unique (symbol,date) 兼容):港股恒生系列走 `stock_hk_index_daily_sina`(2013+),全球非美股走 `index_global_hist_sina`(**2022 起**,新浪单次上限~1000 行;symbol 键是中文名如「法CAC40指数」「首尔综合指数」,`scripts_gen/sync_global_index.py` 维护 `GLOBAL_SINA_SYMBOL` 映射),美股 SPX/NDX/DJI 原走 `index_global_hist_em`(东财在本机被阻断),**已改用 FRED 官方收盘点位补数**(2026-08-13:脚本 `FRED_INDEX_SERIES` SPX→SP500/NDX→NASDAQ100/DJI→DJIA,`{date,value}` → index_daily close 列、OHLC 留空,SPX/DJI 2016-08 起、NDX 1986 起;东财将来可达后重跑 em 全量覆盖即可补 OHLC)。**双参数注入**:catalog `index.global` 的 params_template 同时带 `symbol`(中文名,API 调用用)+ `code`(拉丁码如 CAC,写库 symbol 列用),SyncEngine code_value 优先 `params.get("code")`。**看板**:指数 tab 加分类 chip 行(`#cat-chips`,全部/各分类带计数,点 chip 过滤候选下拉,URL 深链 `?category=`)。测试 `tests/test_index_category.py`(65)+ `tests/test_sync_global_index.py`(11:FRED 路由/落库只写 close/增量起点)。**2026-08-13 精修**:① 关键词盲区补齐(所有制全名/产业链/基本面F系列/市值规模/防御绩效龙头等,`其他` **127→8**,仅剩 B股×4/综合指数/I100/I300/创业基础,均合理保留;新增 manual 收益版 R/G/V 精修);② **跨境策划清单优先修复**——`build_category_rows` 对与 meta_asset_info 重叠的跨境码改为直接走 curated 行(此前被境内循环自动分类抢先,`sync_global_index` 把跨境码写入 meta_asset_info 后 13 条全球指数掉进「其他」)。**注意**:需重启服务(旧进程是旧代码)浏览器/API 才生效。
15. ✅ **网页看板板块热力图(指数版, 2026-08-13 重做)**:看板顶部 tab「板块热力图」——**日期筛选 + 概念/行业/核心指数三组 + 等大小色块**(不按成交量分面积,成交量仅悬停 tooltip)。**后端** `GET /api/v1/indices/heatmap`:新增 `?date=`(YYYYMMDD/YYYY-MM-DD,默认最新交易日,无效 422);三组映射 `_HEATMAP_GROUP_MAP` 宽基→核心指数/行业→行业/主题→概念,**风格/策略/债券/跨境/其他 不展示**;`pct_chg`=目标日收盘/该指数前交易日收盘-1(排除单日/目标日停牌),`volume`=目标日(仅供 tooltip);`meta={date, dates(可用交易日历倒序,供下拉), groups=[{key,name,count}], count_by_group}`;TTL 缓存按日期隔离(60s);构建逻辑 `routes.py::_build_index_heatmap(repo, date)` 纯函数 + `_ymd_to_iso` 归一。**前端**(`app.js` `loadHeatmap(date)/renderHeatmapGrid/hmCellHTML/contrastText/showHmTooltip/populateHeatmapDate`):三组各渲染网格(色块 `minmax(112px,1fr)` 等大,颜色=`pctColor` 红涨绿跌 ±5% 封顶饱和、`contrastText` 按底色深浅选黑/白字,`|pct|<0.3` 灰),自绘 tooltip(`#hm-tooltip` 悬停跟随,显示 收盘/成交量/分类/日期),点色块→`onSelectIndex` 跳指数预设视图;日期下拉从 `meta.dates` 填充(默认最新),切换重拉 + `syncUrl` 深链 `?type=heatmap&date=`;`renderRangePanel` 阶段二空桩不变。**契约**:SKILL.md 更新 `/indices/heatmap` 行 + curl 示例(三组/date/dates)。测试 `tests/test_heatmap.py`(4:纯函数三组映射+排除风格跨境单日/指定日期环比、端点全量+date 过滤+无效 422、空表 200 不 422)。**注意**:后端路由改动需重启服务(2026-08-13 已重启生效,PID 59148)。
16. ✅ **指数日线快速增量(tushare 按交易日批量 + akshare 兜底, 2026-08-13)**:原境内指数增量逐个 code 走 akshare `stock_zh_index_daily`(每次全量回传 ~8000 行历史只为补 1 天, 732 只串行 ~37 分钟)。改用 **tushare `index_daily(trade_date=)` 按交易日批量**(同 `run_fund_daily_batch` 模式:一次调用返回当天全市场 ~8000 行, 映射后 ~652 行入库, 补 N 天 N 次调用) + **akshare 逐只兜底**(tushare 未覆盖的境内指数补到批量前沿)。实现:`sync_engine.run_index_daily_batch()`(增量起点用 `get_last_date_where` **只按境内 sh/sz 子集取 max(date)**, 跨境 FRED 日期不拖后腿; 回溯模式跳过行数 ≥`INDEX_FULL_MARKET_THRESHOLD`(400)的已覆盖日; `_ts_code_to_index_symbol` 只映射 sh000/sz399, 93xxx/95xxx/H 系中证/海外代码丢弃, `_prepare_index_batch_df` 列规范)+ `scripts_gen/sync_index_quick.py` CLI(`--start-date/--end-date/--no-fallback/--dry-run/--limit`, 默认增量 境内最大日期+1→今天)。**兜底停更过滤**(同日补):akshare 兜底只补**近 30 天仍活跃**的落后指数(`_compute_stale` + `FALLBACK_LOOKBACK_DAYS=30`),数据停在数月/数年前的 186 只停更/退市死代码(如 sh000944 停 2021、sz399238 停 2025-12)不再每次无谓重试(省 ~9.5min)。**注意**:跨境 16 个走独立管道 sync_global_index(sina+FRED)不受影响。测试 `tests/test_index_batch.py`(19 个:ts_code→symbol 映射/列规范/全市场入库过滤/幂等跳过/首次回补/增量起点按境内/回溯跳过已覆盖/兜底停更过滤)。CLI 补 1 天 37min→~5min。
17. ✅ **股票日线迁移 tushare(2026-08-13)**:本机 akshare `stock_zh_a_hist`(东财)网络不稳定被阻断,`stock.a_daily` 就地改 **tushare `daily(ts_code=)`**(api_source/api_function/date_format='%Y%m%d'/column_map 英文列/params_template 改 ts_code,保留 code_column=symbol+adj_factor 归属;git 历史留 akshare 版,东财可达可改回)。tushare 多余列(ts_code/pre_close/change/pct_chg)由 `_apply_column_map` 自动丢弃,本地表结构不变(date/OHLC/volume/amount/symbol)。**注意**:股票同步仍按 ts_code 逐只增量(尚无全市场批量),A股 EOD 数据 tushare 傍晚才发布(白天跑 0 行正常);当日可用性测试脚本股票代表参数已改 `{"ts_code":"600519.SH"}`。测试 `tests/test_catalog_editor.py`+`tests/test_import_worker.py`(32 个 catalog 校验/列映射,迁移后全绿)。

## 快速命令

```bash
pip install -e .[dev]        # 安装(dev 含 pytest)
python src/main.py           # 启动桌面端(start_bern.bat 等效)
start_dashboard.bat          # 看板一键启动(自动探测 8765, 未起则后台拉起只读服务并开浏览器)
python scripts_gen/serve_api.py   # 无GUI只读 API 服务(看板/下游取数独立启动, 不开桌面端)
python -m pytest tests/ -q   # 跑测试(当前 476 个全绿)
python scripts_gen/gen_report.py --date 2026-08-04   # 日报 PDF
python scripts_gen/vacuum_and_archive.py             # DB 瘦身(默认 500MB 阈值,超才 VACUUM)
python scripts_gen/check_freshness.py --only-stale   # 数据新鲜度(退出码 1=有停更)
python scripts_gen/ensure_indexes.py                 # 为 catalog 表建普通索引(幂等)
python scripts_gen/migrate_index_daily.py            # 一次性修复 index_daily(2014→今)
python scripts_gen/sync_fund_batch.py                # 基金日线批量同步(按交易日补全市场)
python scripts_gen/sync_index_batch.py               # 指数日线批量同步(初始化全市场 732 个, 幂等; NO_PROXY=* 直连国内源)
python scripts_gen/sync_index_quick.py               # 指数日线快速增量(tushare 按交易日批量+akshare 兜底; 补1天 37min→~5min; 见待办#16)
python scripts_gen/sync_adj_factor.py --asset-type stock   # 复权因子批量同步(因子表入库)
python scripts_gen/sync_adj_factor.py --asset-type fund --fallback   # ETF因子 akshare 回退
python scripts_gen/test_daily_availability.py            # 当日可用性测试(ETF510300/股票600519/指数sh000001 + 全部启用宏观源 + tushare index_daily 功能探测(不写库); --no-macro 快速模式只测资产+探测; 定时探测当天数据发布用)
python scripts_gen/sync_asset_names.py --asset-type all    # 资产 code→名称 同步(看板搜索下拉用;幂等)
python scripts_gen/sync_index_category.py --dry-run        # 指数分类生成 meta_index_category(启发式+手动YAML+跨境;幂等)
python scripts_gen/sync_global_index.py --dry-run          # 跨境指数批量同步(港股/全球入 index_daily;美股走 FRED 收盘点位补数)
python scripts_gen/install_skill.py                        # 对外 SKILL.md 契约装到 ~/.claude/skills
pip install -e ./clients/berndata_client                   # 下游程序取数用 Python 客户端(薄封装, 独立可安装小包)
```

- 本地 API:`http://127.0.0.1:8765`,Swagger 在 `/docs`(版本 0.2.0)。鉴权用 `X-API-Key` 头(非 URL token),`.env` 配 `API_TOKEN`,留空则不鉴权。数据端点 `/indicator/{key}`、`/data/{table}`、`/boards/*` 支持 `?format=json|csv`(csv 走 `text/csv` 下载);`/indicator`、`/boards/*` 支持可选 `?page=&page_size=`(meta.pagination);`/data/{table}` 支持可选 `?adj=qfq|hfq` 复权派生(仅股票/ETF 行情表,指数/宏观 422;行情表存不复权原值,因子表 `asset_adj_factor` 派生)与可选 `?code=` 按代码列精确过滤(代码列支持 `code`/`symbol`/`ts_code` 任一,index/stock 的 symbol 列亦可;无代码列的表 422,带 code 绕过缓存下沉 SQL);`GET /api/v1/search` 表内代码搜索(`q`=代码/中文名/拼音,`table` 必填,`limit`≤50;只返回表内实际存在 code,名称来自 `meta_asset_info`,由 `sync_asset_names.py` 维护);`GET /api/v1/indices` **指数分类清单**(理杏仁式 宽基/行业/主题/风格/策略/债券/跨境/其他,748 条,`meta.categories` 分类计数,`?category=`/`?q=`/`?limit=`,数据来自 `meta_index_category` 由 `sync_index_category.py` 维护);`GET /api/v1/indices/heatmap` **指数板块热力图数据**(概念/行业/核心指数三组,等大小色块;`?date=` YYYYMMDD/YYYY-MM-DD 指定交易日默认最新,无效 422;涨跌幅=目标日收盘/前交易日收盘-1、`volume`=目标日仅供 tooltip,排除风格/策略/债券/跨境/其他;`meta={date, dates, groups, count_by_group}`,TTL 60s);主题看板见 `GET /api/v1/boards`、`GET /api/v1/boards/{key}`(时序宽表)、`GET /api/v1/boards/{key}/snapshot`(快照)。**对外 AI 契约**:`skills/bern-financial-data/SKILL.md`(装到 `~/.claude/skills/` 供任意 Claude Code 项目查询数据)。**Python 客户端(下游程序)**:`clients/berndata_client/` 独立可安装小包(`pip install -e ./clients/berndata_client`,可选 `[df]` 带 pandas),自动带鉴权头/统一信封解包(返回 `.data`+`.meta`,`.df` 惰性转 DataFrame)/日期归一(date、datetime、YYYYMMDD、ISO 皆可)/错误抛出(`BernDataError`),曲线方法覆盖全部数据端点(`data`/`stock_daily`/`macro`/`indicator`/`board_snapshot`/`board_series`/`sources`/`tables`/`health`),另有 `request()` 通用逃生舱与 `csv()` 字节下载;契约与 SKILL.md 对齐,测试 `tests/test_data_client.py`(22 个,httpx MockTransport 不依赖真实服务)。**网页只读看板**:`GET /dashboard`(`http://127.0.0.1:8765/dashboard`,免鉴权)——理杏仁风通用看板,纯静态复用数据端点。**阶段一(2026-08-12)**:顶部 通用/指数/ETF/股票 四类型预设 tab(指数=index_daily 禁复权、ETF=fund_etf_daily、股票=stock_daily;未同步表显示空态提示),代码搜索下拉(`/search`,代码/中文名/拼音,↑↓/Enter/Esc,手输 code 回车保留),行情预设下统计卡换为**多周期涨跌幅**(今日/5日/20日/60日/年初至今/1年,红涨绿跌、基期日期提示,`stats.js` 纯函数 `computePeriodReturns`,ytd 前一年末→当年首日→表内最早 三段回退)。**阶段二(下周,已留空桩)**:K线 MA5/10/20/60 + 区间统计面板。见待办 #12、`tests/test_dashboard.py`、`tests/test_dashboard_js.py`。**板块热力图(2026-08-13 重做)**:看板 tab「板块热力图」——**日期筛选 + 概念/行业/核心指数三组 + 等大小色块**(不按成交量分面积):三组各渲染网格,色块颜色=涨跌幅 红涨绿跌深浅(±5% 封顶、|pct|<0.3 灰),成交量/收盘/日期悬停 `#hm-tooltip` 展示,点色块跳指数预设视图定位该 code;日期下拉(`meta.dates`)默认最新,URL 深链 `?type=heatmap&date=`。数据来自 `GET /api/v1/indices/heatmap`。见待办 #15、`tests/test_heatmap.py`。
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
- 代码类源(股票/指数/基金)增量起点**无条件用该 code 的实际 max(date)**;该 code 无数据(如初始化新 code)→ **走全量回填**,**绝不用表级 last_sync_date 兜底**——否则新 code 会被表级日期误判为「已到最新」而跳过(2026-08-12 修复:初始化 732 个指数首轮全部被跳过)。表级 last_sync_date 仅对**无 code 参数**的源有意义(见 sync_engine.run 注释)。

## 指标归一层(meta_indicator)

- 概念:数据本体留在各源表,`meta_indicator` 只存「indicator 键 → 获信源表 + 日期列 + 数值列」,供统一查询 `repo.get_indicator()` 返回 `{date, value}`。
- 同一指标可有多个源表(如 `us.cpi` 有 akshare 表和 FRED 表),用户可在 GUI「指标管理中心」或 API `PUT /indicator/{key}` 手动选择获信源。
- **自动沿用**:同步成功时 `auto_adopt_indicator` 把首个能明确解析数值列(今值/现值/value 等关键词)的源设为获信源;不覆盖用户已手动选择。
- 数值列解析:`_resolve_value_column(strict=True)` 只认明确数值列名,避免把 OHLC 表第一数值列误当指标值。

## 模块地图

| 模块 | 职责 | 关键点 |
|---|---|---|
| `src/api/` | FastAPI 本地服务 | `server.py` 中间件顺序:连接追踪→鉴权→CORS;`routes.py` 表名白名单防注入、`_df_to_json_records` 清洗 NaN/NaT/Timestamp(防 pydantic 序列化 bug);`web/` 纯静态看板(index.html/style.css/stats.js/chart.js/app.js + vendor/echarts.min.js,StaticFiles 挂载 `/dashboard` + 鉴权豁免) |
| `src/core/` | 同步引擎、取数、FRED、调度、动态 schema | `sync_engine.py` 最核心;`transform.py` 指标派生;`adj_factor.py` 行情复权纯函数(apply_adjustment qfq/hfq + derive_factor_from_prices 回退反推);`boards.py` 主题看板(BoardStore 配置读写校验 + BoardService 快照/宽表,复用 `get_indicator`+`compute_transform`);`ttl_cache.py` 内存缓存;`freshness.py` 新鲜度纯函数 |
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

- 476 个测试全绿(~41s):`tests/` 覆盖 matcher、column_mapper、api、sync_engine、fred_client、importer、scraper、indicator、catalog_editor、header_template、boards(主题配置读写/校验/快照/宽表/API 端点)、adj_factor(复权纯函数/批量同步/API)、skill_contract(对外 SKILL.md 契约一致性 + ?code= 过滤行为 + /search 契约)、search(代码搜索端点 8 个)、asset_names(指数代码归一化/名称列提取)、sync_asset_names(mock akshare 幂等)、data_client(客户端 22 个)、dashboard(看板 6 个)、dashboard_js(node-eval 测多周期涨跌幅纯函数)、sync_global_index(11 个:FRED 路由/落库只写 close/增量起点)、index_category(65 个:关键词盲区补齐 + 跨境策划优先 + 收益版 manual)、heatmap(板块热力图 4 个:三组映射+排除风格跨境单日/指定日期环比/端点全量+date 过滤+无效 422/空表)、index_batch(指数批量 17 个:ts_code→symbol 映射/列规范/全市场入库过滤/幂等跳过/首次回补/增量起点按境内子集/回溯跳过已覆盖)、sync_engine 新 code 全量回填回归(多代码表初始化新 code 不被表级日期跳过)等。
- 测试用临时 SQLite(engine 层),不污染 `data/berndata.db`。
