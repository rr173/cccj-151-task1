# 山火应急撤离编排系统

面向指挥员的山火撤离编排：导入受威胁网格、道路通行状态、避难点床位与可用车辆，
登记每户人数 / 行动辅助需求 / 宠物 / 联络方式，由 CP-SAT 约束求解器把
**家庭作为不可拆分单元**分配到避难点与撤离批次；指挥员可比较多个候选方案后
发布分区撤离令，车辆离场后批次与床位占用即**承诺锁定**。

## 启动

### 方式一：容器（推荐）

```bash
docker compose up --build
# 浏览器打开 http://localhost:8000
```

SQLite 数据保存在命名卷 `evac-data`（容器内 `/data/evac.db`），容器/进程重启后
已发布命令、出发记录、剩余容量自动恢复。

### 方式二：本地 Python

```bash
pip install -r requirements.txt
./run.sh            # 或: uvicorn app.main:app --port 8000
```

首次启动自动播种一套演示数据（11 户、4 辆车、2 个避难点，其中南溪学校暂停接收、
火区近线 E12 已封闭，河谷桥 E03 每批次仅 1 车次）。可在“导入/事件”页一键重置。

## 约束模型（`app/planner.py`）

* 家庭不可拆分；不安排自行撤离，所有接驳只沿 **open** 道路（Dijkstra 预算可达路径）；
* 每个未出发家庭至多上一个车次；车辆每批次至多执行一个车次；
* 车次数座位 ≥ 车上家庭总人数；行动辅助家庭只能乘 **无障碍车辆**、进入 **无障碍避难点**；
* 避难点床位约束扣除**已出发家庭锁定占用**的床位；暂停接收的避难点不参与分配；
* 每条道路**每批次**通过车次 ≤ 道路容量；
* 目标按方案画像调整：`urgent`（大火网格尽早批次）、`shortest`（里程最短）、`balanced`。

无解时给出逐户的具体阻塞原因（`NO_ROUTE / NO_ACCESSIBLE_VEHICLE / NO_ACCESSIBLE_SHELTER /
SHELTER_PAUSED / NO_BEDS / NO_VEHICLE_CAPACITY / ROAD_CAPACITY`）及全局瓶颈。

## 关键业务规则

| 场景 | 行为 |
|---|---|
| 火线扩大 / 道路封闭 / 车辆故障 / 避难点暂停 | 版本号 +1，重算**只覆盖未出发家庭**；已出发家庭与已锁定床位不动 |
| 车辆离场（批次或单车） | 家庭 `departed=1`、车辆 `departed`、床位占用锁定；之后无法重新分配或释放 |
| 资源恢复 | 对应事件切回 open/available/接收中，即可再次生成候选继续编排 |
| 重复导入名单 | 以家庭 ID upsert：更新信息，**不会重复登记**；已出发户只允许改联络信息 |
| 多名指挥员同时发布 | 候选带 `base_version` 乐观锁，发布在 `BEGIN IMMEDIATE` 串行事务中进行；场景已变 → 409；同一时刻只有一份活动命令，旧命令未出发家庭的预占被释放，已出发承诺保留 |
| 进程重启 | 全部状态在 SQLite（WAL），启动即恢复，无需快照 |

## API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| GET  | `/api/state` | 全量态势：网格/路网/避难点/车辆/家庭/活动命令/候选/日志 |
| POST | `/api/import` | 批量 upsert：households/vehicles/shelters/edges/cells |
| POST | `/api/events/fire` | 网格威胁等级变更 |
| POST | `/api/events/road` | 道路 closed/open |
| POST | `/api/events/vehicle` | 车辆 broken/available |
| POST | `/api/events/shelter` | 避难点 暂停/恢复接收 |
| POST | `/api/plans` | 生成候选（body: `profile`, `created_by`） |
| GET  | `/api/plans/{id}` | 候选明细（含路径边序列、诊断） |
| POST | `/api/plans/{id}/publish` | 发布（可带 `expected_version` 乐观校验） |
| POST | `/api/orders/depart` | 车辆离场：`{batch}` 或 `{vehicle_id}` |
| POST | `/api/admin/reset` | 重置演示数据 |
| GET  | `/docs` | Swagger 交互文档 |

### 命令行演练

```bash
# 生成均衡方案并发布
curl -s localhost:8000/api/plans -H 'Content-Type: application/json' \
  -d '{"profile":"balanced"}' | tee /tmp/plan.json
PID=$(python3 -c "import json;print(json.load(open('/tmp/plan.json'))['plan_id'])")
curl -s -XPOST localhost:8000/api/plans/$PID/publish \
  -H 'Content-Type: application/json' -d '{"published_by":"张指挥"}'

# 批次 1 车辆离场（承诺）
curl -s -XPOST localhost:8000/api/orders/depart \
  -H 'Content-Type: application/json' -d '{"batch":1}'

# 火区扩大 + 道路封闭后，旧候选发布会被 409 拒绝
curl -s -XPOST localhost:8000/api/events/road -H 'Content-Type: application/json' \
  -d '{"edge_id":"E04","status":"closed"}'
curl -s -XPOST localhost:8000/api/plans/$PID/publish -H 'Content-Type: application/json' -d '{}'
```

## 页面

* **态势地图**（Leaflet）：威胁网格、道路（封闭红虚线）、避难点占用条、车辆/家庭状态；
* **批次与命令**：按批次/车辆展示车厢编组，支持整批或单车“发车离场”，查看承诺记录；
* **方案比较**：生成三种画像候选，比较安置数、批次分布、无可行安置原因，一键发布；
* **资源与名单**：家庭名册与道路状态；
* **导入 / 事件**：JSON 批量导入 + 火线/道路/车辆/避难点快速事件 + 事件日志。

## 目录

```
app/
  db.py         SQLite 模式、串行写事务、版本号
  models.py     领域模型与场景装载（含已承诺床位）
  planner.py    Dijkstra 可达路径 + CP-SAT 模型 + 无可行安置诊断
  seed.py       演示数据
  main.py       FastAPI 路由（导入/事件/规划/发布/出发）
  static/       地图与批次页面（index.html, app.js）
Dockerfile, docker-compose.yml, run.sh
```
