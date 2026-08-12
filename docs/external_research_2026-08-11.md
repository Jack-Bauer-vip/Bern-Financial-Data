# 外部项目调研评估报告（2026-08-11）

> 目的：联网调研同类开源项目，评估对本系统（Bern_Financial_Data 金融数据中台）有无可借鉴/可直接使用之处。
> 结论：本系统架构判断（SQLite 增量采集 + FastAPI 分发 + 指标归一层 + 主题看板）在当前阶段正确，无需推倒重来；有 4 项值得落地的借鉴点 + 1 项记入待办。

## 一、调研发现概览

| 类别 | 代表项目 | 对应本系统 |
|---|---|---|
| 本地金融数据底座 | free-stockdb、tdx2db、AxData、ashares_data_collect | 核心底座 |
| 存储/分析引擎 | SQLite vs DuckDB+Parquet 混合方案 | 存储层 |
| AI/Agent 接入层 | a-stock-data（SKILL.md 契约）、MCP servers | 数据分发/AI |
| 数据源与治理 | a-share-data-sources（实测基准）、AkShare vs Tushare | 多源接入/新鲜度 |

## 二、逐项评估

### ✅ 可直接借鉴（低风险高价值）

**1. 行情复权处理 — 本系统最大缺口**
- 本系统 `fund_etf_daily`/`stock_daily`/`index_daily` 存不复权原始价（ODS 原值原则）。
- 外部做法（free-stockdb、tdx2db 一致）：**独立复权因子表，原数据保真，查询时应用**——tushare `adj_factor` 接口与本系统「ODS 永不改，只派生」口径完全同构。
- 落地：新增复权因子表 + 查询端 `?adj=qfq|hfq` 派生，复用现有 `?transform=` 管线。**已列为 P0 执行项（任务 #2）**。

**2. 数据源实测基准 → 加固停更治理**
- a-share-data-sources 用可复现脚本实测覆盖/限频/复权/稳定性；失败模式分三类：源站结构变化直接报错 / HTTP 200 但空数据 / 配额错误。
- 本系统已有 28 deprecated + 7 health_check_ignore + 新鲜度纯函数，治理框架完备；缺「HTTP 200 但空数据」这类**静默停更**识别（08-07 曾踩坑：中国源 300+ 天停更未发现）。**已列为 P1 执行项（任务 #4）**。

**3. 混合存储的渐进路径（当下不迁）**
- 436MB SQLite 当前完全够用（日频/月频、增量小写入、WAL 单写多读——SQLite 是增量采集正确选择）。
- 演进路径：DuckDB 可**只读 ATTACH SQLite** 零迁移做分析；将来上分钟级/tick 再引入 Parquet 分区。**已列为 P2 文档记录（任务 #5）**。

### 💡 值得做、需设计

**4. 封装本系统为 Claude Code Skill / 对外数据服务契约** ⭐
- a-stock-data 用 SKILL.md 作 AI 接口契约；free-stockdb 提供 MCP + HTTP 双接口。
- 本系统已有 FastAPI + X-API-Key + 通用查询 + 主题看板，只差「让 AI 用自然语言查」的封装。**已列为 P0 执行项（任务 #3）**。

### 🔸 可借鉴但需评估

**5. 分钟级数据路径（tdx2db / mootdx）** — **暂不做，记入待办**。若要，tdx2db（通达信数据入库，增量幂等 + AGENTS.md）和 mootdx（直连通达信 TCP）是成熟路径，注意通达信本地数据版权/合规与停更风险。

**6. DAG 编排（ashares_data_collect）** — 当前数据量过度设计，不引入。

### ✖️ 不适用（明确排除）

| 项目 | 原因 |
|---|---|
| free-stockdb 定制 C++ 时序引擎 | 性能过剩，数据源不匹配 |
| TickDB / TerminalQ / Brieff 等商业 MCP | 付费 + 本系统已有自建数据 |
| OpenBB | 体量过重，定位不符 |
| 金智汇连ETL | 通用框架，同步管线已固化 |

## 三、结论与执行清单

| 优先级 | 事项 | 状态 |
|---|---|---|
| **P0** | 行情复权（复权因子表 + `?adj=` 派生） | 执行（任务 #2） |
| **P0** | 本系统封装 Claude Code skill + 对外 SKILL.md 契约 | 执行（任务 #3） |
| **P1** | check_freshness 识别「HTTP 200 空数据」静默停更 | 执行（任务 #4） |
| **P2** | DuckDB ATTACH 演进路径记录 | 执行（任务 #5） |
| **待办** | 分钟级数据（tdx2db/mootdx） | 记入 CLAUDE.md 待办，暂时不做 |

## 来源

- 本地数据底座：[free-stockdb](https://github.com/hello245m/free-stockdb)、[tdx2db](https://github.com/xbfighting/tdx2db)、[AxData](https://github.com/electkismet/AxData)、[ashares_data_collect](https://github.com/YangSal/ashares_data_collect)、[金智汇连ETL](https://github.com/ScottZt/jinzhi-huilian-etl)
- AI/Agent：[a-stock-data 评估文章](https://github.com/laozdao/dao-quant-research/blob/main/articles/O01-open-source-projects/O01-13-a-stock-data-fullstack-toolkit.md)、[OctagonAI/skills](https://github.com/OctagonAI/skills)
- 数据源对比：[a-share-data-sources](https://github.com/ychenfen/a-share-data-sources)、[AkShare vs Tushare](https://cloud.tencent.cn/developer/article/2685661)
- 存储选型：[SQLite vs DuckDB](https://semicolony.dev/vs/sqlite-vs-duckdb)、[量化数据库选择](https://cloud.tencent.cn/developer/article/2659354)
- SKILL.md：[Claude Code Skills Best Practice](https://mintlify.wiki/shanraisshan/claude-code-best-practice/best-practices/skills)、[Sherlock 编写指南](https://sherlock.xyz/post/how-to-write-skills-for-claude-code-and-cowork)
