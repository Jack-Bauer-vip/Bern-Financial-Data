# Bern_Financial_Data 金融数据中台

本地化、增量更新的桌面金融数据中台。用于**汇总数据**和**分发数据**——数据通过 API（akshare/tushare）、上传文件、数据抓取等方式获取，汇总入库后可供 AI 智能分析，也可组织成临时数据接口供其他模块调用。

## 功能

- **多数据源汇总**：宏观 / 股票 / 指数 / 基金，经 akshare / tushare 获取，增量更新入库（SQLite）
- **定时调度**：APScheduler 按 cron 自动同步各板块数据，可暂停/启用
- **文件导入（智能识别）**：批量上传 CSV / Excel，自动识别目标表、列名映射、新字段建议（规则优先 + 本地 AI 兜底），按唯一键更新+新增
- **智能分析（AI）**：本地 ollama（deepseek-r1）对汇总数据生成中文摘要/趋势整理
- **数据分发**：内置 FastAPI 本地服务，提供统一查询接口；可按需生成临时数据接口供其他模块调用
- **导出**：CSV / Excel / PDF
- **健康检查**：检测各数据源停更状态、给出备用数据源建议

## 技术栈

- Python 3.12+，PySide6（桌面 GUI）、FastAPI + uvicorn（本地数据服务）
- SQLAlchemy 2 + SQLite（WAL 模式，动态建表、动态列）
- akshare / tushare（金融数据源）
- APScheduler（定时任务）
- ollama（本地 AI，deepseek-r1 可选）

## 快速开始

```bash
# 1. 安装依赖
pip install -e .[dev]

# 2. 配置（复制模板并填写自己的密钥）
copy .env.example .env
#   编辑 .env，填写 TUSHARE_TOKEN 等（可选）

# 3. 启动桌面端
start_bern.bat   # 或 python src/main.py
```

首次启动会弹出初始化向导，选择需要的数据模块与历史年数。数据同步后即可查询、导出、分析。

> **AI 智能识别/分析**：默认调用本地 ollama（`http://localhost:11434`，模型 `deepseek-r1:14b`）。如未安装 ollama，会自动降级为纯规则识别，不影响导入功能。

## 本地数据服务（API）

启动桌面端后，API 服务自动运行在 `http://127.0.0.1:8765`：

| 端点 | 说明 |
|---|---|
| `GET /api/v1/health` | 健康状态（含调度器状态） |
| `GET /api/v1/sources` | 列出所有数据源 |
| `GET /api/v1/macro/cpi` | CPI 多表查询（指标+日期过滤） |
| `GET /api/v1/macro/{table_name}` | 通用宏观表查询 |
| `GET /api/v1/stock/daily` | A 股日线查询 |
| `POST /api/v1/sync/{source_key}` | 触发异步同步 |
| `GET /docs` | Swagger 交互文档 |

**鉴权**：若在 `.env` 配置了 `API_TOKEN`，除 `/health`、`/docs` 外的接口需带 `X-API-Key: <token>` 头（或 `?token=<token>` 参数）。留空则不鉴权。

**数据分发**：`GET /api/v1/data/{table_name}` 通用按表查询，供其他模块调用（`/api/v1/data/tables` 列出可用表）。

## 测试

```bash
python -m pytest tests/ -v
```

## 目录结构

```
config/         # 数据源目录(data_catalog.yaml)、默认配置(default.yaml)
src/
  api/          # FastAPI 本地数据服务
  core/         # 同步引擎、调度器、数据获取、唯一键
  db/           # SQLAlchemy 引擎、ORM 模型、通用数据访问层
  export/       # CSV/Excel/PDF 导出
  gui/          # PySide6 桌面界面（主窗口、对话框）
  importer/     # 文件导入、智能识别、AI 客户端、列名映射
  utils/        # 配置、日志
tests/          # pytest 测试
```

## 版本管理

- `.env`（密钥）不入库，使用 `.env.example` 模板
- 数据文件（`data/*.db`）不入库
- 采用 Git 本地 + GitHub 远程双份管理

## License

私有项目，未指定开源协议。
