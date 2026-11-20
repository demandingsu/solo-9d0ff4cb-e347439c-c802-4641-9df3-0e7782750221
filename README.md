# 拆股复权日行情 API

基于 **FastAPI + SQLite** 的版本化行情服务：导入带修订号的日开高低收/成交量与
拆股（并股）公告，按**入库截止批次**返回拆股前后可比较的复权日行情。
后来补报或更正的公告**不会改写此前已交付批次的序列**。

另提供**交割单入账**：按账户+证券+结账日+公告截止批次补录成交并重算
卖出的买入来源与结转成本，输出完整底稿；入账失败整次作废，上次底稿仍可查。

## 业务规则

### 版本化导入（只追加，从不改写历史行）

| 规则 | 行为 |
| --- | --- |
| 同业务键（行情=证券+交易日，公告=证券+事件号）仅最高修订号生效 | 查询只取最高修订 |
| 同修订号但值不同 | **整批拒收（422）**，事务回滚，不产生批次 |
| 收到修订号低于库中已生效修订 | **整批拒收（422，"过期修订"）** |
| 同修订同值重复提交 | 幂等跳过，不写新行 |
| 每次成功导入 | 同一事务内生成一个**入库批次**（自增批次号） |

### 截止批次复权查询 `GET /quotes/adjusted`

- 以请求的**区间末日 `end_date` 为基准日**；
- 只使用**截至 `cutoff_batch` 已入库**（行所在批次 ≤ 截止批次）的公告与行情；
- 对行情日 `d`，生效公告满足 `d < 除权日 ≤ end_date`（除权日当天不折算当日行情）；
- `ratio` 表示老 1 股 → 新 `ratio` 股：**1 拆 2 填 `2.0`（价格减半、成交量加倍）**，
  2 并 1 填 `0.5`（价格翻倍、成交量减半）；多笔公告比例连乘；
- 返回原值、折算值（Decimal 计算后四舍五入保留 5 位小数）、折算系数与**事件依据**
  （事件号、除权日、比例、类型、修订号、所在批次）。

### 并发与一致性

- SQLite WAL 模式 + `BEGIN IMMEDIATE` 写事务 + 30s `busy_timeout`：写事务串行，
  读不阻塞；并发导入同键时唯一索引兜底，恰好一个成功，其余整批回滚；
- 每次查询在单个只读快照事务内完成，**价量与事件依据来自同一截止批次下的已提交状态**，
  读不到查询期间新提交的批次；
- 失败导入整体回滚：不留行、不留批次。

### 交割单入账（`POST /postings`）

指定**账户、证券、结账日、公告截止批次**；`trades` 非空时先**补录成交**，
为空时只用库中成交**换公告批次重算**。每次入账在单个写事务内完成
「补录 + 全量重算 + 写底稿」，生成一个入账批次。

| 规则 | 行为 |
| --- | --- |
| 成交字段 | 成交号、交易日、方向（buy/sell）、股数；买入另填总额（按分四舍五入） |
| 同（账户+证券+成交号）同值 | 幂等只记一次（`trades_new=0`） |
| 同号异值 | **整次拒收（422）**，事务回滚 |
| 成交处理顺序 | 按（交易日, 成交号）排序（成交号为字符串字典序） |
| 卖出扣股 | 按买入先后（FIFO）逐批扣减 |
| 成本结转 | 扣减股数 ÷ 该批剩余股数 × 该批剩余成本，**四舍五入到分**；**扣完一批时结转全部剩余成本** |
| 拆并股公告 | 只采用**截至 cutoff_batch 已入库**、除权日 ≤ 结账日的有效公告（每事件号最高修订） |
| 除权日处理 | **先调整各笔剩余股数再处理当日成交**；保留碎股，剩余成本不变；除权日当天买入不调整 |
| 重算 | 补录成交或换截止批次后，该账户+证券的全部卖出与持仓按最新口径全量重算 |
| 整次作废 | 结账日早于任一成交日 / 重算股数不足 / 写入失败 => **整体回滚**，不留成交与批次；`GET /postings/latest` 仍返回上次采用的公告批次及完整底稿 |

底稿内容（`GET /postings/latest` 或 `GET /postings/{posting_id}`）：

- `allocations`：每笔卖出的买入来源（成交号、交易日）、扣减股数、结转成本；
- `positions`：结账日各买入批次的剩余股数与剩余成本（含已扣完的批次，剩余为 0）；
- 成本守恒：Σ结转成本 + Σ剩余成本 = Σ买入总额。

## 启动（Docker Compose）

```bash
docker compose up --build
# 打开 http://localhost:8000/docs ，在 Swagger UI 直接导入、查询
```

SQLite 数据文件位于命名卷 `market-data` 的 `/data/market.db`。

## 本地开发（无 Docker）

```bash
python3 -m venv --without-pip .venv
python3 -I get-pip.py                 # 如系统没有 pip
.venv/bin/pip install -r requirements.txt
.venv/bin/python -I -m pytest -q      # 26 项自测
MARKET_DB_PATH=./data/market.db .venv/bin/python -I -m uvicorn app.main:app --port 8000
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/imports/quotes` | 导入日行情，成功返回 `batch_id` |
| POST | `/imports/corporate-actions` | 导入拆股/并股公告，成功返回 `batch_id` |
| GET | `/quotes/adjusted` | 复权行情（原值+折算值+事件依据），参数 `security_code/start_date/end_date/cutoff_batch` |
| GET | `/quotes/raw` | 原值日行情（截止批次下生效修订） |
| GET | `/corporate-actions` | 公告查询（每事件号生效修订） |
| GET | `/batches` | 入库批次清单 |
| POST | `/postings` | 入账：补录成交并按公告截止批次重算成本结转与持仓 |
| GET | `/postings` | 入账批次清单（可按 `account/security_code` 过滤） |
| GET | `/postings/latest` | 该账户+证券最近一次成功入账的完整底稿 |
| GET | `/postings/{posting_id}` | 按入账批次号查底稿（历史批次不可变） |
| GET | `/trades` | 已入库成交清单（按交易日、成交号排序） |
| GET | `/health` | 健康检查 |

### 请求示例

```bash
# 1. 导入行情（批次 1）
curl -X POST localhost:8000/imports/quotes -H 'Content-Type: application/json' -d '{
  "quotes": [{"security_code": "600001", "trade_date": "2026-01-02",
              "revision": 0, "open": 100, "high": 102, "low": 98,
              "close": 100, "volume": 1000}]}'

# 2. 补报 1 拆 2 公告（批次 2，除权日 2026-01-05）
curl -X POST localhost:8000/imports/corporate-actions -H 'Content-Type: application/json' -d '{
  "actions": [{"security_code": "600001", "event_no": "SP-001",
               "ex_date": "2026-01-05", "ratio": 2.0, "revision": 0,
               "action_type": "split"}]}'

# 3. 按批次 1 查询 -> 补报不可见，折算值=原值；不传 cutoff_batch 则取最新批次
curl 'localhost:8000/quotes/adjusted?security_code=600001&start_date=2026-01-01&end_date=2026-01-10&cutoff_batch=1'

# 4. 入账：买 100 股(总额 10000) -> 1拆2 -> 卖 150 股，结转成本 7500
curl -X POST localhost:8000/postings -H 'Content-Type: application/json' -d '{
  "account": "ACC001", "security_code": "600001", "closing_date": "2026-01-10",
  "cutoff_batch": 2,
  "trades": [
    {"trade_no": "B1", "trade_date": "2026-01-02", "direction": "buy",
     "shares": 100, "amount": 10000.00},
    {"trade_no": "S1", "trade_date": "2026-01-06", "direction": "sell",
     "shares": 150}]}'

# 5. 查底稿：卖出明细 + 结账日持仓（B1 余 50 股 / 2500 元）
curl 'localhost:8000/postings/latest?account=ACC001&security_code=600001'
```

## 目录结构

```
app/            FastAPI 应用（db 连接/建表、models 校验、services 业务逻辑、main 路由）
tests/          26 项 pytest 自测（复权规则、补报不可变、修订、拒收回滚、并发、
                入账 FIFO 结转、跨拆股补录重算、整次作废）
devtools/       端到端与并发压力脚本（对真实 uvicorn 进程发 HTTP 请求）
Dockerfile / docker-compose.yml / requirements.txt
```
