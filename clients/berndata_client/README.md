# berndata-client

本地 **Bern_Financial_Data 金融数据中台**的 Python 客户端(薄封装 REST API)。给下游程序、脚本、AI 分析取数用——自动带鉴权头、统一信封解包、日期归一、惰性转 pandas DataFrame、错误抛出。

契约与 `skills/bern-financial-data/SKILL.md` 对齐(端点/参数/返回结构一致);完整接口文档也可直接看服务端 `/docs`(Swagger)或 `/openapi.json`。

## 安装

```bash
pip install -e ./clients/berndata_client       # 本仓库内可编辑安装
pip install -e "./clients/berndata_client[df]" # 需要 .df 转 DataFrame 时(默认不含 pandas)
```

前置:本地数据服务需已启动(`python src/main.py` 或 `start_bern.bat`)。

## 快速开始

```python
from berndata_client import BernDataClient

client = BernDataClient()            # 自动读 API_HOST/API_PORT/API_TOKEN(含 .env)

# 服务健康 + 数据新鲜度
print(client.health()["status"])

# 基金日线最新收盘(大表务必带 code, 否则只返回前 limit 条切片)
resp = client.data("fund_etf_daily", code="518880", limit=3)
print(resp.data)                     # [{date, open, high, low, close, volume, ...}]
df = resp.df                         # 惰性转 pandas DataFrame
```

## 鉴权

与中台服务一致:请求头 `X-API-Key`。解析优先级:

- `api_key` 显式参数 → 环境变量 `BERN_DATA_API_KEY` → `API_TOKEN`(服务 `.env` 同名) → 不鉴权。
- `base_url` 显式参数 → `BERN_DATA_BASE_URL` → `API_HOST`+`API_PORT` → 默认 `http://127.0.0.1:8765/api/v1`。
- 客户端会在当前目录尝试读 `.env`(需装 `python-dotenv`,不装则跳过)。

```python
client = BernDataClient(api_key="你的token")
client = BernDataClient(base_url="http://192.168.1.10:8765/api/v1", api_key="xxx")
```

## 常用查询

```python
# 宏观指标(推荐走指标端点): CPI 同比 —— 注意 meta.unit_type 决定要不要派生
r = client.indicator("us.cpi", transform="yoy")
print(r.meta)                        # {'unit_type': 'level', 'unit_desc': ..., 'transform': 'yoy'}
print(r.data)                        # [{date, value}, ...]

# 前复权行情 + 日期区间
df = client.data("fund_etf_daily", code="159915", adj="qfq",
                 start_date="20260101", end_date="20260812", as_df=True)

# A股日线(按代码)
r = client.stock_daily("600519", limit=10)

# 宏观表(可省 macro_ 前缀) / CPI 聚合
r = client.macro("macro_us_fred_cpi", limit=5)
r = client.macro_cpi(indicator="同比", limit=10)

# 主题看板: 每日快照 / 时序宽表
r = client.board_snapshot("macro_dashboard")
r = client.board_series("macro_dashboard", transform="yoy", start_date="20260101")

# 先发现, 再查询
print(client.tables())               # 表名清单
print(client.indicators().data)      # 指标键 + 口径
print(client.boards().data)          # 主题键
print(client.sources().data)         # 数据源(含 data_status)
```

## CSV 下载

四个 CSV 端点(`/data/{table}`、`/indicator/{key}`、`/boards/{key}`、`/boards/{key}/snapshot`)可下载原始 CSV 字节:

```python
import io
import pandas as pd

buf = client.csv("/data/fund_etf_daily", params={"code": "518880", "limit": 100})
df = pd.read_csv(io.BytesIO(buf))    # 文件带 BOM, Excel 直接打开不乱码
```

## 返回结构 & 错误

- 标准信封 `DataResponse` 解包为 `BernDataResponse`,字段:`.data`(list[dict])、`.total`、`.source`、`.data_status`(active/deprecated/local)、`.meta`(口径/分页)、`.message`;`.df` / `.to_df()` 惰性转 DataFrame。
- 分页:`indicator()` / `board_series()` 传 `page` 后 `meta.pagination = {page, page_size, has_more}`。
- 非 2xx 或服务端 `code != 200` 抛 `BernDataError(status_code, message)`,含 FastAPI 422 校验详情。
- `data_status` 语义:deprecated 表数据停在过去,**勿当最新**;`meta.unit_type` 是原始存储口径,level 要同比/环比必须 `?transform=` 派生。

## 通用逃生舱

```python
# 调任意端点(信封自动解包)
r = client.request("GET", "/indicator/us.cpi", params={"transform": "yoy"})
# 触发同步(管理操作)
client.trigger_sync("macro.us.fred.cpi")
```

## 测试

```bash
cd D:/F_Data_Sys && python -m pytest tests/test_data_client.py -q
```
