---
name: bern-financial-data
description: 当任务需要查询本机 Bern_Financial_Data 金融数据中台的本地数据时使用——行情 K线(基金/股票/指数日线)、宏观指标(CPI/失业率/非农/PPI/FRED 美国序列等)、主题看板每日快照, 统一走本机 FastAPI (http://127.0.0.1:8765/api/v1, X-API-Key 鉴权)。支持复权(adj=qfq|hfq)、派生(transform=yoy|mom|pct)、代码搜索(/api/v1/search, 代码/中文名/拼音)、CSV 下载、分页; 通过 /sources、/data/tables、/indicator、/boards 先发现再查询。仅在需要实际取数时使用: 投资观点讨论、策略问答、纯分析话题等无需查询本地数据库的内容不要加载本 skill。
origin: custom
version: 1.1.0
---

# Bern_Financial_Data — 本地金融数据中台查询契约 V1.1.0

> 本机桌面金融数据中台(SQLite 存储 + FastAPI 分发)。数据**已入库**并有专人维护同步,本 skill 只负责「**怎么查**」——不内嵌取数代码,而是给出**完整可复制的查询契约**。数据清单通过 API 自己的活端点实时发现,不会因目录新增源而过时。
>
> 安装:将本文件放入 `~/.claude/skills/bern-financial-data/SKILL.md`(或运行项目内 `scripts_gen/install_skill.py`),Claude Code 会自动识别并在「需要查本地金融数据」的对话中激活。

## 0. 快速开始(30 秒验证)

```bash
curl http://127.0.0.1:8765/api/v1/health
# → {"status":"ok","version":"0.2.0", ... ,"scheduler_running":true,"stale_sources":[...],"stale_count":N}
```

- **连接被拒 / 超时** → 数据服务未运行。不要编造数据,告知用户启动服务(Windows 下运行项目 `start_bern.bat`,或 `python src/main.py`),然后重试。
- `health.status=="ok"` 即服务就绪;`stale_sources`/`stale_count` 是滞后/停更源清单,可作数据新鲜度参考。

## 1. 鉴权契约

| 项 | 值 |
|---|---|
| 鉴权头 | `X-API-Key: <token>`(仅请求头,不接受 URL 参数) |
| token 来源 | 服务端 `.env` 的 `API_TOKEN` |
| 留空 | `API_TOKEN` 未配置 → **不鉴权**,请求头可省略(本地开发默认) |
| 公开豁免 | `/api/v1/health` `/docs` `/openapi.json`(免鉴权) |
| 配置后未带 | 401 `{"code":401,"message":"无效或缺失的 API token"}` |

带鉴权的两种写法:

```bash
curl -H "X-API-Key: 你的token" "http://127.0.0.1:8765/api/v1/data/fund_etf_daily?code=159915&limit=5"
```

```python
import requests
H = {"X-API-Key": "你的token"}           # token 从服务端 .env 的 API_TOKEN 读取; 留空则删掉这行
r = requests.get("http://127.0.0.1:8765/api/v1/data/fund_etf_daily",
                 params={"code": "159915", "limit": 5}, headers=H)
print(r.json())
```

> ⚠️ 不要把密钥硬编码进最终交付代码;示例中的「你的token」仅演示占位。

## 2. 发现有什么数据(先发现,再查询)

**正确链路:先调用发现端点拿到当前真实清单,再定位目标表/指标/主题。** 不要凭记忆猜表名或指标键。

| 端点 | 返回什么 | 用途 |
|---|---|---|
| `GET /api/v1/sources` | 全部数据源: `[{source_key, name, api_function, table_name, has_incremental, data_status}]`,可加 `?include_deprecated=false` 过滤已停更源 | 看有哪些源、归属哪张表、是否已停更 |
| `GET /api/v1/sources/{source_key}/status` | 单源最近同步状态(sync_job 记录) | 查某源上次同步时间/结果 |
| `GET /api/v1/data/tables` | 当前可查询的全部表名 `[{table_name}]`(含导入表,排除 meta_ 元数据表) | `/data/{table}` 的合法表名白名单 |
| `GET /api/v1/indicator` | 归一化指标清单: `[{indicator_key, preferred_table, source_api, date_column, value_column, unit_type, unit_desc}]` | 找宏观指标键(如 `us.cpi`),推荐优先走指标端点 |
| `GET /api/v1/boards` | 主题看板清单: `[{key, name, description, item_count, date_start, date_end}]` | 找主题键,看预定义看板 |
| `GET /api/v1/search` | 表内代码搜索: `[{code, name, table}]` + `meta.code_column` | 代码/中文名/拼音模糊匹配,拿行情表的 code(供 ?code= 过滤) |
| `GET /api/v1/health` | `stale_sources` 滞后源 | 了解数据新鲜度,判断「最新」到什么日期 |

**数据类型总览**(当前目录):

```
全球宏观数据   → 表 macro_fred_*(FRED 美国, level)、macro_usa_*(多已 deprecated)、
                 macro_china_*、macro_euro_*(已 deprecated)、macro_japan_*、macro_uk_*、macro_australia_*
A股日线行情    → 表 stock_daily   (支持 ?adj=)
指数日线      → 表 index_daily   (价格指数, 不支持复权)
ETF基金日线   → 表 fund_etf_daily (支持 ?adj=, 2700+ 只)
复权因子       → 表 asset_adj_factor (基础设施, 供 ?adj= 派生用, 勿直接消费)
```

## 3. 端点契约速查表

> 日期参数全 API 统一 **`YYYYMMDD` 8 位无分隔符**(如 `20260810`);响应统一包装 `{code, message, data:[...], total, source, data_status, meta}`。

### 系统

| 端点 | 方法 | 用途 | 关键参数 |
|---|---|---|---|
| `/api/v1/health` | GET | 服务状态 + 新鲜度 | — |
| `/api/v1/connections` | GET | 连接监控(客户端 IP/UA/请求统计) | — |

### 数据分发(核心)

| 端点 | 方法 | 用途 | 关键参数 |
|---|---|---|---|
| `/api/v1/data/{table_name}` | GET | **通用数据表查询**(数据分发) | `start_date` `end_date`(YYYYMMDD) `limit`(≤100000,默认200) `fields`(逗号分隔列) `format`(json\|csv) `adj`(qfq\|hfq) `code`(按代码列 **code/symbol/ts_code** 任一精确过滤) |
| `/api/v1/search` | GET | **表内代码搜索**(代码/中文名/拼音) | `q`(空=前 limit 个) `table`(**必填**) `limit`(≤50,默认20);只返回表内实际存在的 code |
| `/api/v1/stock/daily` | GET | A股日线(按代码) | `symbol`(**必填**) `start_date` `end_date` `limit`(≤50000) |
| `/api/v1/macro/{table_name}` | GET | 宏观表查询(白名单) | `start_date` `end_date` `limit`(≤10000);表名可省 `macro_` 前缀 |
| `/api/v1/macro/cpi` | GET | CPI 聚合(多张 CPI 表归并) | `indicator`(名称模糊) `start_date` `end_date` `limit` |

### 指标(归一化,推荐宏观消费优先走这里)

| 端点 | 方法 | 用途 | 关键参数 |
|---|---|---|---|
| `/api/v1/indicator` | GET | 指标键清单 | — |
| `/api/v1/indicator/{indicator_key}` | GET | **统一指标序列** `{date,value}` | `transform`(level\|yoy\|mom\|pct) `start_date` `end_date` `limit`(≤100000,默认5000) `format`(json\|csv) `page` |
| `/api/v1/indicator/{indicator_key}` | PUT | 手动设获信源(管理) | body `{"table":"..."}` |

### 主题看板

| 端点 | 方法 | 用途 | 关键参数 |
|---|---|---|---|
| `/api/v1/boards` | GET | 主题键清单 | — |
| `/api/v1/boards/{board_key}/snapshot` | GET | **每日快照**(每指标一行:最新值/环比/同比/近3期) | `transform` `format` |
| `/api/v1/boards/{board_key}` | GET | 时间序列宽表(date 为行,每指标一列) | `start_date` `end_date` `transform` `limit` `format` `page` |

### 同步控制(管理操作,谨慎)

| 端点 | 方法 | 用途 | 关键参数 |
|---|---|---|---|
| `/api/v1/sync/{source_key}` | POST | 触发异步同步(202 accepted) | `source_key`;并发上限 4 |

## 4. 数据语义防误读(必读)

1. **`meta.unit_type` 描述的是原始存储值口径**,决定你该不该再派生:
   - `level` — 存储值即绝对值(FRED 全量:CPI 指数、失业率%、非农人数千人)。要同比/环比 → **必须** `?transform=yoy|mom` 由服务端派生。
   - `yoy` — 存储值**本身已是同比%**(中国官方 CPI/PPI 等)。**不要再** `?transform=yoy` 二次派生,直接用。
   - `mom` — 存储值本身已是环比%。
   - 响应 `meta` 带 `{unit_type, unit_desc, transform}`,先读再消费。
2. **`data_status`**:`active`(在同步)/`deprecated`(已停更,保留历史)/`local`(导入/抓取表)。`deprecated` 表数据停在过去,**勿当最新**。`/sources` 可 `include_deprecated=false` 只看活源。
3. **复权 `?adj=`**:仅 `stock_daily`/`fund_etf_daily` 支持。`qfq`=前复权(价格回落到最新,当前价不变)、`hfq`=后复权(历史价复利放大)。行情表存不复权原始价,复权是查询时派生。指数/宏观/无因子源表传 `adj` → **422**。
4. **`fund_etf_daily` 2005-2017 数据含封闭式基金/LOF**(非纯 ETF)。按「ETF」口径做回测/统计需按标的类型过滤,否则污染结果。
5. **日期**:参数一律 `YYYYMMDD`;返回 `date` 字段为 ISO `YYYY-MM-DD`。
6. **`format=csv`**:返回 `text/csv` 文件下载(带 BOM,Excel 直接打开不乱码)。文件名形如 `{table}.csv`/`{key}_snapshot.csv`。
7. **分页**:`/indicator/{key}` 与 `/boards/{board_key}` 支持 `page`(≥1)。传 `page` 后按 `limit` 做页大小,`meta.pagination = {page, page_size, has_more}`。
8. **大表查询**:`/data/{table}` 未带日期区间时只返回 `limit` 条并提示。查 300 万行的 `fund_etf_daily` **务必带 `code` 和/或日期区间**,否则截断到任意切片,结论会错。
9. **`?code=` 与 `?adj=` 的代码列**:`?code=` 按表内代码列过滤——`fund_etf_daily` 是 `code` 列、`index_daily`/`stock_daily` 是 `symbol` 列,均支持。**先用 `/search` 拿 code**(表内实际存在,搜到的必有数据),再带 `?code=` 查询;无代码列的表(宏观表)传 `?code=` → 422。
10. **新鲜度**:月频指标(CPI/失业率等)FRED 官方约每月中旬才发布上月值,月初显示滞后属正常,勿误判停更。

## 5. 常见查询模式(完整可复制)

### ① 单只基金日线 + 前复权(最常用)

```bash
curl "http://127.0.0.1:8765/api/v1/data/fund_etf_daily?code=159915&adj=qfq&start_date=20260101&end_date=20260810&fields=date,open,high,low,close,volume&limit=200"
```

```python
import requests, pandas as pd
r = requests.get("http://127.0.0.1:8765/api/v1/data/fund_etf_daily",
                 params={"code": "159915", "adj": "qfq",
                         "start_date": "20260101", "end_date": "20260810",
                         "limit": 200})
df = pd.DataFrame(r.json()["data"])
```

### ② 宏观指标序列 + 派生(CPI 同比)

```bash
curl "http://127.0.0.1:8765/api/v1/indicator/us.cpi?transform=yoy&start_date=20250101&end_date=20260810"
```

```python
import requests, pandas as pd
r = requests.get("http://127.0.0.1:8765/api/v1/indicator/us.cpi",
                 params={"transform": "yoy", "start_date": "20250101"})
body = r.json()
print(body["meta"])          # {'unit_type': 'level', 'unit_desc': '...', 'transform': 'yoy'}
df = pd.DataFrame(body["data"])   # columns: [date, value]
```

> 指标键不记得?先 `GET /indicator` 拿全量清单(含 `unit_type`,顺带确认口径)。

### ③ 主题看板快照 + 存 CSV

```bash
curl -o snapshot.csv "http://127.0.0.1:8765/api/v1/boards/{board_key}/snapshot?format=csv"
```

```python
import requests
r = requests.get(f"http://127.0.0.1:8765/api/v1/boards/{board_key}/snapshot",
                 params={"format": "csv"})
open("snapshot.csv", "wb").write(r.content)
```

### ④ 动态发现三步(首次接触时先跑)

```python
import requests, json
base = "http://127.0.0.1:8765/api/v1"
for ep in ("/sources", "/data/tables", "/indicator", "/boards"):
    r = requests.get(base + ep)
    print(ep, "→", len(r.json()["data"]), "条")
    print(json.dumps(r.json()["data"][:3], ensure_ascii=False, indent=2))
```

### ⑤ 代码搜索(行情表找 code,再 ?code= 精确过滤)

```bash
curl "http://127.0.0.1:8765/api/v1/search?q=创业板&table=fund_etf_daily&limit=20"
# → {"code":200,"data":[{"code":"159915","name":"创业板ETF易方达","table":"fund_etf_daily"},...],
#    "meta":{"code_column":"code","matched_by":"name"}}
```

```python
import requests
r = requests.get("http://127.0.0.1:8765/api/v1/search",
                 params={"q": "5103", "table": "fund_etf_daily", "limit": 20})
codes = [it["code"] for it in r.json()["data"]]   # ['510300', ...] 只含表内实际 code
# 拿到 code 后查询: ?code=159915&start_date=...
```

> 支持代码精确/前缀、中文名、拼音(依赖 pypinyin,未装则跳过)模糊匹配;`q` 留空返回前 `limit` 个 code 供下拉初始化。

## 6. 边界 / 不做什么

- **只读消费为主**:本 skill 定位是查询数据。`PUT /indicator/{key}`(改获信源)、`POST /sync/{source_key}`(触发同步)属管理操作,除非用户明确要求,否则**不主动调用**。
- **不写库、不改表**:数据由中台同步/导入维护;如需补数或新增数据源,让用户用桌面端 GUI 操作。
- **不凭记忆猜表名/指标键**:一律先走 §2 发现端点确认存在,再查询。
- **服务未运行就直说**:不编造数据、不用旧缓存假装实时,引导用户启动服务。
