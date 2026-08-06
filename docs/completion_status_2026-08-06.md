# Bern_Financial_Data — 系统完成情况说明(供外部 AI 评估参考)

> 快照日期:**2026-08-06**。本说明用于向外部 AI 提供系统当前完成情况与遗留问题的准确基线,供其判断**是否还需要后续修改、改什么、优先改哪个**。
> 如需与代码逐行核对,以 `CLAUDE.md` 与源码为准;本文件所有数字均为当日实测。

---

## 1. 一句话现状

本地化、增量更新的**桌面金融数据中台**(PySide6 GUI + 本地 FastAPI 服务 + SQLite)已跑通「提取→整理存储→分发」全链路:71 个叶数据源、306 万行基金日线、61 张宏观表已入库;近期完成五轮加固(指标口径语义化 / 长任务心跳 / 全量同步进度 / 回归测试 / 接口规范)。**主体功能完整,剩余工作以"决策定案、边缘补强、运维健壮性"为主,无结构性重构需求。**

---

## 2. 数据规模(2026-08-06 实测)

| 项 | 数值 | 说明 |
|---|---|---|
| 数据库 | `data/berndata.db` 435 MB | SQLite WAL |
| 叶数据源 | **71** = 50 akshare + 20 FRED + 1 tushare | `config/data_catalog.yaml` |
| deprecated 源 | **20** | 上游停更、保留历史;同步/调度/健康检查/获信源候选全部排除 |
| 带 `indicator` 键的叶节点 | **40** | P0 后全部带 `unit_type`/`unit_desc` |
| `meta_indicator` | 20 行 | 指标→获信源表+日期列+数值列 |
| `meta_sync_jobs` | 63 个任务,全部 `completed` | 无失败残留 |
| 基金日线 `fund_etf_daily` | **3,060,227 行**,2005-01-04 → 2026-08-06,2727 只 | 含早期封闭式基金/LOF |
| 指数日线 `index_daily` | 8,696 行,1990 → 2026-08-04 | 迁移脚本补齐 2014→今 |
| 宏观表 | 61 张,共 25,887 行 | 含 20 张 `macro_fred_*`(3,010 行) |

---

## 3. 已完成功能清单(按模块)

### 3.1 提取(数据源接入)

- **FRED 官方接入**(2026-08-04):`fred_client.py` 按 `observation_start/end` **真增量**拉取,20 个序列经官方 API 逐一验证后入目录,数据截止 2026-06-01(月频,官方约 8 月中旬才发 7 月值,属正常等待)。
- **akshare / tushare**:50 + 1 个源,代码类源(股票/指数/基金)增量起点用该 code 实际 max(date);tushare 走 `fund_daily(trade_date=)` **按交易日批量**补全市场,替代逐个 code(提速数百倍)。
- **文件导入 + HTML 抓取**:表头模板精确命中→规则评分→AI 兜底→降级四级识别链路;会话级识别缓存。

### 3.2 整理存储

- **动态 schema**:列非 ORM 定义,`ALTER TABLE ADD COLUMN` 自动加列;全 TEXT 存储;中文日期归一化 + 丢弃 NaT 行。
- **统一去重写入**:API 同步 / 文件导入 / HTML 抓取三路共用 `bulk_upsert`(唯一键启发式推断);每源进程级互斥锁防并发。
- **指标归一层**:`meta_indicator` 存获信源映射,`auto_adopt_indicator` 同步后自动沿用(不覆盖手动选择)。
- **指标口径语义化(P0, 2026-08-06)**:`unit_type`/`unit_desc` 贯穿 DB→catalog→repository→sync→API→GUI;FRED=level,akshare 同比表=yoy、环比表=mom;`/indicator/{key}` 响应带 `meta:{unit_type,unit_desc,transform}`。迁移脚本 `migrate_meta_columns.py` 幂等(跑两遍 0 新增)。
- **指标派生视图**:`/indicator/{key}?transform=level|yoy|mom|pct`,按日期中位间隔推断频率;原值(ODS)永不改,只派生。

### 3.3 分发与接口

- **FastAPI 通用查询**:`/api/v1/data/{table}`(表名白名单防注入)、`/indicator/{key}`、`/macro`、`/health`、`/sources`。
- **内存 TTL 缓存**:`/indicator`/`/macro`/`/data`(无日期时);失效单点在 `SyncEngine.run()` 成功分支;**带日期时区间过滤下沉 SQL**(date+code 复合索引),绕缓存防"先 limit 再过滤"裁错日期。
- **接口规范(六, 2026-08-06)**:`/sources` 新增 `include_deprecated` 参数(默认 True 零破坏),False 时过滤 deprecated 源。

### 3.4 桌面 GUI(PySide6)

- 数据源树(分类管理对话框改 YAML)、参数面板、指标管理中心(含 P0「口径」列)、健康检查对话框、导出(CSV/Excel/PDF)、系统托盘。
- **GUI 响应性加固**(2026-08-04~05):表格卡顿根因修复(PandasModel 绘制路径 O(N)→O(1));同步期间表格刷新改后台 + 300ms 防抖;`setUniformRowHeights` 兼容修复。
- **全量同步进度(P2, 2026-08-06)**:状态栏 `QProgressBar` + 「正在同步 (n/63)」文本,连接 `sync_progress` 信号;`addPermanentWidget` 规避 QStatusBar 临时消息压控件(离屏冒烟验证)。
- **长任务心跳 / 僵死检测(P1, 2026-08-06)**:`meta_sync_jobs` 增 `running_status`/`last_heartbeat`;基金批量同步逐交易日刷新心跳;健康检查对「running 且 >10min 无心跳」标**疑似僵死**(暗红、置顶、含建议)。

---

## 4. 验证结果

| 验证项 | 方式 | 结果 |
|---|---|---|
| 全量回归测试 | `python -m pytest tests/ -q` | **223 passed**,~21s |
| P0 迁移幂等 | `migrate_meta_columns.py` 跑两遍 | 第 1 次 +4 列回填 20 行;第 2 次 0/0 |
| P0 API 冒烟 | `/indicator/us.cpi` | 返回 `meta.unit_type="level"` |
| 六 /sources 参数 | `include_deprecated=false` | 71 → 51,过滤 20 个 deprecated |
| P2 GUI 冒烟 | offscreen 实例化 MainWindow | 进度条 创建/显示推进/结束隐藏 全通过 |
| P4 回归测试 | `test_pandas_model.py` / `test_sync_lock.py` | 绘制路径 O(1) 锁定;同步锁跨实例互斥 |
| P1 心跳 | `test_fund_batch.py` / `test_freshness.py` | running/心跳/idle 全状态断言通过 |

---

## 5. 遗留问题 / 风险(供外部 AI 判断"还需改什么")

### A. 决策未定案(影响分析口径,最高优先)

1. **默认获信源口径定案**:unit 已显式标注(FRED=水平值、akshare=变化率),但**下游默认消费哪个口径**仍是业务决策,未写进任何规范文档。→ 建议:确定后写进 `docs/` 供所有下游统一引用。
2. **4 个无 FRED 源指标的停更监控闭环**:ISM 制造业/非制造业、CB 信心、NFIB 仍走 akshare 全量重拉(上游聚合站已停更风险)。`/health` 已按实际频率推断预期间隔,但**"停更后自动告警→自动切换备用"未闭环**。

### B. 已设计但尚未实施(明确可做,均小改动)

3. **指标管理中心每行显示数据截止日**(GUI 增强,CLAUDE.md 优先级 6)。
4. **AI 模型切换 qwen2.5:7b**:改 `config/default.yaml` 一行,用户下载后自行切换,未执行。
5. **`meta_cache_entries` 利用率为 0**:缓存机制存在但查询路径未广泛使用,可清理或激活。
6. **`meta_column_registry` 仅 4 行**:动态加列登记逻辑触发少,可核查是否漏登记。

### C. 工程健壮性(真正值得持续关注)

7. **后台长任务曾被系统 kill**:2005-2022 回溯期间后台任务先后 3 次被终止(1h48m / 1.5m / 续传),断点续传兜住了,**根因未排查**(超时?内存?系统策略?)。P1 心跳已让"被杀"可视化,但**根因与防杀策略未落地**。
8. **SQLite 单写者 + 306 万行**:当前规模无瓶颈;若继续扩张(更多标的/更高频),分发查询性能与写放大需重新评估(已建 date+code 复合索引,大表日期过滤下沉 SQL)。
9. **测试覆盖缺口**:223 个测试主要覆盖 importer/api/sync_engine/fred/indicator/fund_batch;GUI 其余路径(参数面板、导出、调度对话框)与 API 并发路径覆盖仍少。

### D. 已评估并明确不做

10. **P3 日期存储改 YYYYMMDD**:定长 ISO TEXT + 复合索引在 3M 行无瓶颈;改存储需迁移存量 + 重写整条日期链,负优化,拒绝。
11. **`run_fund_daily_batch` 逐日事务结构**:曾被疑"306 万行一次性提交"致被杀,已证伪(实际逐交易日提交),不改。

---

## 6. 给外部 AI 的修改建议(优先级排序)

**建议近期做(决策/补强,工作量小、收益明确):**

1. 定案默认获信源口径并写入规范文档 —— 1 小时,消除下游静默吃错数据风险。
2. 指标管理中心补数据截止日列 —— GUI 一处,直接服务于"该选哪个源"。
3. 健康检查「停更告警→备用源建议」闭环(4 个 akshare 源) —— 在现有 freshness 之上加 webhook 推送(CLI 已支持 `FRESHNESS_WEBHOOK_URL`),补 GUI 一键触发。

**建议中期做(健壮性):**

4. 排查后台长任务被杀根因(超时/内存/系统策略),评估任务续传 + 心跳告警的完整方案。
5. 补 GUI 与 API 并发路径测试,把覆盖缺口收窄。

**不建议做:**

6. P3 日期存储改造(已评估拒绝)。
7. 批量同步事务结构调整(已证伪)。

---

## 7. 关键文件索引

| 文件 | 职责 |
|---|---|
| `config/data_catalog.yaml` | 71 个源 + 40 个 indicator 节点的 unit 字段 |
| `src/core/sync_engine.py` | 同步引擎(run / run_fund_daily_batch 心跳) |
| `src/db/repository.py` | bulk_upsert / 指标 / 心跳字段 |
| `src/core/freshness.py` | 新鲜度纯函数 + 僵死检测 |
| `src/gui/main_window.py` | 主窗口(含 P2 进度条) |
| `src/gui/dialogs/health_dialog.py` | 健康检查(含疑似僵死) |
| `src/gui/dialogs/indicator_manager_dialog.py` | 指标管理中心(含口径列) |
| `src/api/routes.py` | /indicator meta + /sources include_deprecated |
| `scripts_gen/migrate_meta_columns.py` | P0 幂等迁移回填 |
| `docs/project_overview.md` | 项目全貌(8 节,含 P3 拒绝说明) |

---

*生成:2026-08-06。数字来自当日实测(git 5cea92c 后工作区干净,已 push origin/master)。*
