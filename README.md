# 共线生产过敏原溯源服务

为共线（共享设备）生产的食品批次提供过敏原溯源。基于 **Python 3.12 / FastAPI / SQLite**，无外部依赖，一条命令启动。

## 业务规则

### 登记

- **原料批次**：登记过敏原集合（大写归一、自动去重），或登记为 **待确认（pending）**。待确认与明确过敏原集合互斥。原料声明可事后**更正**（覆盖当前有效声明）。
- **加工批次**：登记加工记录 `设备 + UTC 起止 + 投入批次 + 产出批次`，登记时创建产出批次。
  - 投入批次必须在加工**开始前可用**：原料批次登记即可用；加工批次须在其生产加工结束后才可用。
  - 批次来源图**不得成环**（产出若是任一投入批次的上游则拒绝）。
  - **同一设备**的加工时间区间**不得重叠**（半开区间，端点相接允许）。
  - 加工结束必须晚于开始；产出批次必须是新批次；同一批次不得同时作投入与产出。
- **清洗**：登记设备、清洗时刻（UTC）、是否已验证。清洗时刻落在同设备加工进行期间会被拒绝。清洗可**撤销**（幂等）。

### 过敏原与待确认状态的派生

加工批次的过敏原 = **全部投入批次的过敏原 ∪ 设备残留过敏原**；待确认状态同理取或。

- **设备残留**：同设备在本次加工开始前最近一次已结束加工的产出内容；
- 只有前次加工结束与本次加工开始之间存在**已验证且未撤销**的清洗，才清除残留；
- 未验证、已撤销的清洗都不能清除残留。

当前结论（未签发）**不落地**，每次查询沿来源图实时递归计算，因此：

- 原料声明更正后，所有下游批次的当前结论立即按新声明重算；
- 清洗撤销后，残留立即恢复。

### 查询：每项过敏原 / 待确认状态的来源路径

`GET /batches/{id}` 返回 `current.allergens`、`current.pending`，以及：

- `current.sources.allergens["X"]`：过敏原 X 的**全部来源路径**；
- `current.sources.pending`：待确认状态的来源路径。

路径为有序节点链，例如：

```
raw_declaration(原料声明) → batch(原料批次) → processing(投入/残留) → … → batch(被查批次)
```

加工节点带 `role`：`input`（投入，附 input_batch_id）或 `residue`（设备残留，附前次加工、窗口内清洗与有效清洗判定），可完整解释"为什么含有该过敏原"。

### 标签签发（冻结）

`POST /labels` 按**当前有效记录**计算结论，并把结论、过敏原、每条来源路径、依据证据（原料声明、加工边、清洗窗口与残留判定）整体**快照冻结**入库：

- 之后原料更正、清洗撤销都**不改变旧标签**（标签始终带 `frozen: true`）；
- 结论为 `finalized`（过敏原已明确）或 `pending`（含待确认来源）；
- 可用 `supersedes_id` 签发**关联旧版的新标签**，形成版本链；标签按批次可列、可查。

### 冲突处理

无效时序、来源环、投入不可用、设备重叠、重复 ID 等一律拒绝（4xx），所有写操作在单个 `BEGIN IMMEDIATE` 事务内**先校验后写入**，失败整体回滚，**保持原状态不变**。

## 一键启动（Docker Compose）

```bash
docker compose up --build
```

启动即自动：安装依赖 → 建表（幂等）→ 因 `SEED_DEMO=1` 在空库写入演示数据。

- 服务地址：<http://localhost:8000>
- 交互式 API 文档（Swagger UI）：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/health>

停止：`docker compose down`（加 `-v` 同时删除数据卷）。

> 容器内数据库固定在 `/data/allergens.db`，由命名卷 `allergen-data` 持久化。

## 本地直接运行（不用 Docker）

需要 Python 3.11+：

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

# 可选：开启演示数据 / 指定数据库
export SEED_DEMO=1
export DB_PATH=./data/allergens.db
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

运行测试：

```bash
pip install -r requirements-dev.txt
pytest -q
```

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DB_PATH` | `./data/allergens.db`（容器内 `/data/allergens.db`） | SQLite 文件路径，目录自动创建 |
| `HOST` | `0.0.0.0` | 监听地址 |
| `PORT` | `8000` | 监听端口 |
| `SEED_DEMO` | `false`（Compose 中为 `1`） | 启动且库为空时写入演示数据 |
| `ALLOWED_ALLERGENS` | 空（不限制） | 逗号分隔过敏原白名单，如 `PEANUT,MILK,EGG,SOY,WHEAT`；声明时大小写不敏感并统一大写 |

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/raw-batches` | 登记原料批次（过敏原集合或待确认） |
| GET | `/raw-batches` | 原料批次列表 |
| PATCH | `/raw-batches/{id}` | 更正原料当前有效声明 |
| POST | `/processings` | 登记加工（设备/UTC 起止/投入/产出） |
| GET | `/processings` | 加工列表 |
| GET | `/processings/{id}` | 加工详情（含当前结论、来源、残留判定） |
| POST | `/cleanings` | 登记清洗 |
| POST | `/cleanings/{id}/revoke` | 撤销清洗（幂等） |
| GET | `/cleanings` | 清洗列表 |
| GET | `/batches` | 全部批次 |
| GET | `/batches/{id}` | 批次详情 + 当前结论与每项过敏原/待确认来源路径 |
| POST | `/labels` | 签发标签（冻结依据与结论，可 `supersedes_id` 关联旧版） |
| GET | `/labels?batch_id=` | 标签列表（可按批次过滤） |
| GET | `/labels/{id}` | 标签详情（含冻结快照） |
| GET | `/health` | 健康检查 |

时间字段一律 ISO-8601 且**必须带时区**，服务端换算 UTC，例如 `2026-09-30T10:00:00Z` 或 `2026-09-30T18:00:00+08:00`。

## 调用示例（curl）

演示数据：`RM-PEANUT`（花生）、`RM-MILK`（奶）、`RM-UNKNOWN`（待确认）；`PR-1` 在 `LINE-A` 上 10:00–11:00 用花生生产 `P1`，`PR-2` 在 12:00–13:00 用奶生产 `P2`（因未清洗，`P2` 同时带花生残留）。

### 1. 登记原料批次

```bash
curl -s -X POST http://localhost:8000/raw-batches \
  -H 'Content-Type: application/json' \
  -d '{"id":"RM-001","allergens":["peanut"],"pending":false}'

# 待确认（过敏原未知）：
curl -s -X POST http://localhost:8000/raw-batches \
  -H 'Content-Type: application/json' \
  -d '{"id":"RM-002","allergens":[],"pending":true}'
```

### 2. 登记加工（投入、设备、UTC 起止、产出）

```bash
curl -s -X POST http://localhost:8000/processings \
  -H 'Content-Type: application/json' \
  -d '{
    "id":"PR-003","equipment":"LINE-A",
    "start_utc":"2026-09-30T14:00:00Z","end_utc":"2026-09-30T15:00:00Z",
    "inputs":["P2"],"outputs":["FG-003"]
  }'
```

### 3. 查询批次：结论 + 每项过敏原来源路径

```bash
curl -s http://localhost:8000/batches/P2 | python -m json.tool
# current.allergens = ["MILK","PEANUT"]
# sources.allergens.MILK[0]  ：RM-MILK → PR-2(role=input) → P2
# sources.allergens.PEANUT[0]：RM-PEANUT → PR-1(role=input) → PR-2(role=residue) → P2
```

### 4. 登记已验证清洗 → 残留被清除；撤销 → 残留恢复

```bash
curl -s -X POST http://localhost:8000/cleanings \
  -H 'Content-Type: application/json' \
  -d '{"id":"CL-001","equipment":"LINE-A","cleaned_utc":"2026-09-30T11:30:00Z","verified":true}'
# 此后 P2 的当前结论只剩 ["MILK"]

curl -s -X POST http://localhost:8000/cleanings/CL-001/revoke
# 撤销后 P2 的当前结论恢复为 ["MILK","PEANUT"]
```

### 5. 原料声明更正：未签发结论重算

```bash
curl -s -X PATCH http://localhost:8000/raw-batches/RM-MILK \
  -H 'Content-Type: application/json' \
  -d '{"allergens":["SOY"],"pending":false}'
# P2 当前结论立即变为 ["PEANUT","SOY"]
```

### 6. 签发标签、查看冻结依据、签发关联旧版的新标签

```bash
# 签发（结论按当前有效记录；pending 来源存在时 conclusion=pending）
curl -s -X POST http://localhost:8000/labels \
  -H 'Content-Type: application/json' -d '{"batch_id":"P2"}'

# 查看旧标签（即使之后原料更正/清洗撤销，allergens 与 snapshot 不变）
curl -s http://localhost:8000/labels/1 | python -m json.tool

# 原料更正后签发新版并关联旧版
curl -s -X POST http://localhost:8000/labels \
  -H 'Content-Type: application/json' \
  -d '{"batch_id":"P2","supersedes_id":1}'

# 列出某批次全部标签版本
curl -s "http://localhost:8000/labels?batch_id=P2"
```

### 7. 冲突示例（均被拒绝且不改变状态）

```bash
# 同设备时间重叠 → 409
# 投入批次尚未生产结束     → 409
# 来源成环 / 重复 ID       → 400/409
# 结束早于开始、无时区      → 422
```

## 目录结构

```
app/
  config.py     # 环境变量配置
  db.py         # SQLite 连接、建表、写事务
  models.py     # Pydantic 请求模型（UTC 时间校验）
  services.py   # 核心领域：校验/登记/实时派生/来源路径/标签冻结
  seed.py       # 演示数据
  main.py       # FastAPI 路由
tests/test_api.py
Dockerfile
docker-compose.yml
requirements.txt / requirements-dev.txt
```
