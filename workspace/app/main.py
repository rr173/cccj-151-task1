"""FastAPI：规划与发布 API。

并发要点
--------
* 所有改变场景的接口都在 db.tx()（BEGIN IMMEDIATE + 进程锁）内执行并
  推进单调 version；
* 候选方案记录其生成时的 base_version；发布时若 version 已变化（例如
  另一名指挥员基于旧数据发布、或发生了道路封闭/车辆故障事件），返回
  409 CONFLICT，要求重新规划；
* 发布事务中先把已有的活动命令标记 superseded，再写入新命令，
  因此“多人同时发布只有一份生效”。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db, models, planner, seed

app = FastAPI(title="山火应急撤离编排系统", version="1.0.0")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------- Pydantic ----------------

class HouseholdIn(BaseModel):
    id: str
    name: str
    persons: int = Field(gt=0)
    needs_assist: bool = False
    pets: str = ""
    contact: str = ""
    cell_id: str
    pickup_node: str


class VehicleIn(BaseModel):
    id: str
    plate: str
    seats: int = Field(gt=0)
    accessible: bool = False
    home_node: str
    status: Literal["available", "broken"] = "available"


class ShelterIn(BaseModel):
    id: str
    name: str
    node_id: str
    beds: int = Field(ge=0)
    accessible: bool = True
    accepting: bool = True
    note: str = ""


class EdgeIn(BaseModel):
    id: str
    a: str
    b: str
    name: str = ""
    status: Literal["open", "closed"] = "open"
    capacity: int = Field(default=1, ge=0)
    length_m: float = 0


class CellIn(BaseModel):
    id: str
    name: str
    lat: float
    lng: float
    threat: Literal["watch", "threatened", "burning", "safe"] = "watch"


class ImportIn(BaseModel):
    households: list[HouseholdIn] = []
    vehicles: list[VehicleIn] = []
    shelters: list[ShelterIn] = []
    edges: list[EdgeIn] = []
    cells: list[CellIn] = []


class RoadEvent(BaseModel):
    edge_id: str
    status: Literal["open", "closed"]


class VehicleEvent(BaseModel):
    vehicle_id: str
    status: Literal["available", "broken"]


class ShelterEvent(BaseModel):
    shelter_id: str
    accepting: bool


class FireEvent(BaseModel):
    cell_id: str
    threat: Literal["watch", "threatened", "burning", "safe"]


class PlanIn(BaseModel):
    profile: Literal["balanced", "urgent", "shortest"] = "balanced"
    created_by: str = "指挥员"
    time_limit_s: float = 15.0


class PublishIn(BaseModel):
    expected_version: Optional[int] = None
    published_by: str = "指挥员"


class DepartureIn(BaseModel):
    batch: Optional[int] = None          # 指定整批出发
    vehicle_id: Optional[str] = None     # 或指定单车（其当前批次）
    order_id: Optional[int] = None
    depart_ts: Optional[str] = None


# ---------------- 读取 ----------------

@app.get("/api/health")
def health():
    return {"status": "ok", "version": db.version(), "time": now()}


@app.get("/api/state")
def state():
    s = models.load_scenario()
    data = models.scenario_public_dict(s)
    data["plans"] = [models.plan_dict(r) for r in db.conn().execute(
        "SELECT * FROM plans ORDER BY id DESC")]
    data["events"] = [dict(r) for r in db.conn().execute(
        "SELECT id, ts, kind, detail FROM events ORDER BY id DESC LIMIT 50")]
    return data


# ---------------- 导入（幂等 upsert，重复导入不会重复登记） ----------------

@app.post("/api/import")
def import_data(body: ImportIn):
    counts = {"cells": 0, "edges": 0, "shelters": 0, "vehicles": 0, "households": 0}
    s = models.load_scenario()

    if body.cells:
        with db.tx() as c:
            for x in body.cells:
                c.execute(
                    "INSERT INTO cells(id,name,lat,lng,threat,threat_ts) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET name=excluded.name,lat=excluded.lat,"
                    "lng=excluded.lng,threat=excluded.threat,threat_ts=excluded.threat_ts",
                    (x.id, x.name, x.lat, x.lng, x.threat, now()))
                counts["cells"] += 1

    if body.edges:
        missing = {x.a for x in body.edges} | {x.b for x in body.edges} - set(s.nodes)
        # 允许引用已有节点；新边两端节点必须已存在
        if missing:
            raise HTTPException(400, f"道路端点节点不存在: {sorted(missing)}")
        with db.tx() as c:
            for x in body.edges:
                c.execute(
                    "INSERT INTO edges(id,a,b,name,status,capacity,length_m) "
                    "VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                    "a=excluded.a,b=excluded.b,name=excluded.name,status=excluded.status,"
                    "capacity=excluded.capacity,length_m=excluded.length_m",
                    (x.id, x.a, x.b, x.name, x.status, x.capacity, x.length_m))
                counts["edges"] += 1

    if body.shelters:
        with db.tx() as c:
            for x in body.shelters:
                if x.node_id not in s.nodes:
                    raise HTTPException(400, f"避难点节点不存在: {x.node_id}")
                c.execute(
                    "INSERT INTO shelters(id,name,node_id,beds,accessible,accepting,note) "
                    "VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,"
                    "node_id=excluded.node_id,beds=excluded.beds,accessible=excluded.accessible,"
                    "accepting=excluded.accepting,note=excluded.note",
                    (x.id, x.name, x.node_id, x.beds, int(x.accessible),
                     int(x.accepting), x.note))
                counts["shelters"] += 1

    if body.vehicles:
        with db.tx() as c:
            for x in body.vehicles:
                if x.home_node not in s.nodes:
                    raise HTTPException(400, f"车辆停放节点不存在: {x.home_node}")
                c.execute(
                    "INSERT INTO vehicles(id,plate,seats,accessible,home_node,status) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET plate=excluded.plate,"
                    "seats=excluded.seats,accessible=excluded.accessible,"
                    "home_node=excluded.home_node",
                    (x.id, x.plate, x.seats, int(x.accessible), x.home_node, x.status))
                counts["vehicles"] += 1

    skipped: list[str] = []
    if body.households:
        with db.tx() as c:
            for x in body.households:
                if x.cell_id not in s.cells:
                    raise HTTPException(400, f"家庭所在网格不存在: {x.cell_id}")
                if x.pickup_node not in s.nodes:
                    raise HTTPException(400, f"家庭上车节点不存在: {x.pickup_node}")
                # 重复导入名单：同 ID 家庭只更新联络信息，不重复登记；
                # 已出发家庭的核心安置字段不允许再改
                row = c.execute("SELECT departed FROM households WHERE id=?", (x.id,)).fetchone()
                if row and row["departed"]:
                    c.execute(
                        "UPDATE households SET name=?,pets=?,contact=? WHERE id=?",
                        (x.name, x.pets, x.contact, x.id))
                    skipped.append(x.id)
                    continue
                if row:
                    # 已存在（含“已发布待出发”）家庭：更新信息，保留其批次/目的地/命令
                    # 归属（已出发户已在上面分支处理）
                    c.execute(
                        "UPDATE households SET name=?,persons=?,needs_assist=?,pets=?,"
                        "contact=?,cell_id=?,pickup_node=? WHERE id=?",
                        (x.name, x.persons, int(x.needs_assist), x.pets, x.contact,
                         x.cell_id, x.pickup_node, x.id))
                else:
                    c.execute(
                        "INSERT INTO households(id,name,persons,needs_assist,pets,contact,"
                        "cell_id,pickup_node) VALUES(?,?,?,?,?,?,?,?)",
                        (x.id, x.name, x.persons, int(x.needs_assist), x.pets, x.contact,
                         x.cell_id, x.pickup_node))
                counts["households"] += 1

    if any(counts.values()):
        v = db.bump_version()
        db.log_event("import", f"导入数据 {counts}")
        counts["version"] = v
    counts["departed_households_skipped"] = skipped
    return counts


# ---------------- 事件：火线 / 道路 / 车辆 / 避难点 ----------------

@app.post("/api/events/fire")
def fire_event(e: FireEvent):
    s = models.load_scenario()
    if e.cell_id not in s.cells:
        raise HTTPException(404, "网格不存在")
    with db.tx() as c:
        c.execute("UPDATE cells SET threat=?, threat_ts=? WHERE id=?",
                  (e.threat, now(), e.cell_id))
    v = db.bump_version()
    db.log_event("fire", f"网格 {e.cell_id} 威胁等级 -> {e.threat}")
    return {"ok": True, "version": v,
            "note": "仅对未出发家庭重新规划；已出发家庭的安置保持不变"}


@app.post("/api/events/road")
def road_event(e: RoadEvent):
    s = models.load_scenario()
    if e.edge_id not in s.edges:
        raise HTTPException(404, "道路不存在")
    # 已关闭道路不能再承载新分配；已出发家庭不受影响（假定已驶离该路段）
    with db.tx() as c:
        c.execute("UPDATE edges SET status=? WHERE id=?", (e.status, e.edge_id))
    v = db.bump_version()
    db.log_event("road", f"道路 {e.edge_id} -> {e.status}")
    return {"ok": True, "version": v}


@app.post("/api/events/vehicle")
def vehicle_event(e: VehicleEvent):
    s = models.load_scenario()
    if e.vehicle_id not in s.vehicles:
        raise HTTPException(404, "车辆不存在")
    with db.tx() as c:
        # 已出发（离场）的车辆状态只能是 departed，事件接口不可改写
        c.execute("UPDATE vehicles SET status=? WHERE id=? AND status!='departed'",
                  (e.status, e.vehicle_id))
    v = db.bump_version()
    db.log_event("vehicle", f"车辆 {e.vehicle_id} -> {e.status}")
    return {"ok": True, "version": v}


@app.post("/api/events/shelter")
def shelter_event(e: ShelterEvent):
    s = models.load_scenario()
    if e.shelter_id not in s.shelters:
        raise HTTPException(404, "避难点不存在")
    with db.tx() as c:
        c.execute("UPDATE shelters SET accepting=? WHERE id=?",
                  (int(e.accepting), e.shelter_id))
    v = db.bump_version()
    db.log_event("shelter", f"避难点 {e.shelter_id} 接收 -> {e.accepting}")
    return {"ok": True, "version": v}


# ---------------- 规划（生成候选，可多个并存在比较页比较） ----------------

@app.post("/api/plans")
def create_plan(body: PlanIn):
    s = models.load_scenario()
    committed = models.committed_beds()
    ao = models.active_order()
    # 新规划从当前活动命令的下一个批次开始；已出发批次不参与
    if ao:
        departed_batches = [a["batch"] for a in ao["assignments"]
                            if s.households[a["household_id"]].departed]
        start = (max(departed_batches) + 1) if departed_batches else \
            min(a["batch"] for a in ao["assignments"])
    else:
        start = 1

    result = planner.solve(s, start, committed, body.profile, body.time_limit_s)

    summary = {
        "feasible": result["feasible"],
        "optimal": result["optimal"],
        "assigned_count": result["assigned_count"],
        "unassigned_count": result["unassigned_count"],
        "unassigned_ids": result["unassigned_ids"],
        "diagnostics": result["diagnostics"],
        "batches_summary": result["batches_summary"],
        "batches": result["batches"],
    }
    base_version = s.version
    with db.tx() as c:
        cur = c.execute(
            "INSERT INTO plans(created_ts,created_by,base_version,profile,status,"
            "start_batch,num_assigned,unassigned,objective,summary) "
            "VALUES(?,?,?,?, 'candidate', ?,?,?,?,?)",
            (now(), body.created_by, base_version, body.profile, start,
             result["assigned_count"], result["unassigned_count"],
             result["objective"], json.dumps(summary, ensure_ascii=False)))
        plan_id = cur.lastrowid
        c.executemany(
            "INSERT INTO plan_assignments(plan_id,household_id,vehicle_id,shelter_id,"
            "batch,path) VALUES(?,?,?,?,?,?)",
            [(plan_id, a["household_id"], a["vehicle_id"], a["shelter_id"],
              a["batch"], json.dumps(a["path_edges"])) for a in result["assignments"]])
    db.log_event("plan", f"生成候选方案 #{plan_id}（{body.profile}）："
                         f"安置 {result['assigned_count']} 户，未安置 "
                         f"{result['unassigned_count']} 户")
    return {"plan_id": plan_id, "base_version": base_version, **result}


@app.get("/api/plans/{plan_id}")
def get_plan(plan_id: int):
    row = db.conn().execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    if not row:
        raise HTTPException(404, "方案不存在")
    return models.plan_dict(row)


# ---------------- 发布（乐观版本锁，同时发布只有一份生效） ----------------

@app.post("/api/plans/{plan_id}/publish")
def publish_plan(plan_id: int, body: PublishIn):
    s = models.load_scenario()
    current_version = s.version

    with db.tx() as c:
        row = c.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        if not row:
            raise HTTPException(404, "方案不存在")
        if row["status"] == "published":
            raise HTTPException(409, "该方案已发布，请勿重复发布")
        if row["status"] == "superseded":
            raise HTTPException(409, "该候选方案已被更新发布取代，请基于最新场景重新规划")
        if body.expected_version is not None and body.expected_version != current_version:
            raise HTTPException(
                409, f"方案基于版本 v{body.expected_version}，当前已为 v{current_version}："
                     f"场景已发生变化（火线/道路/车辆/避难点/他人发布），请重新规划后再发布")
        if row["base_version"] != current_version:
            raise HTTPException(
                409, f"方案基于版本 v{row['base_version']}，当前已为 v{current_version}："
                     f"请比较最新候选后再发布")

        # 找到当前活动命令
        old = c.execute(
            "SELECT o.id, o.plan_id FROM orders o JOIN plans p ON p.id=o.plan_id "
            "ORDER BY o.id DESC LIMIT 1").fetchone()
        old_plan = old["plan_id"] if old else None

        # 旧命令中未出发家庭的预占床位/批次释放；已出发家庭保持锁定
        if old:
            c.execute(
                "UPDATE households SET shelter_id=NULL,batch=NULL,order_id=NULL "
                "WHERE order_id=? AND departed=0", (old["id"],))

        cur = c.execute(
            "INSERT INTO orders(plan_id,published_ts,published_by,base_version) "
            "VALUES(?,?,?,?)",
            (plan_id, now(), body.published_by, current_version))
        order_id = cur.lastrowid

        # 新命令的家庭进入“已分配待出发”，占用计划床位
        c.execute("UPDATE plans SET status='published' WHERE id=?", (plan_id,))
        pending = c.execute(
            "SELECT household_id, vehicle_id, shelter_id, batch FROM plan_assignments "
            "WHERE plan_id=?", (plan_id,)).fetchall()
        c.executemany(
            "UPDATE households SET order_id=?,shelter_id=?,batch=? WHERE id=? AND departed=0",
            [(order_id, r["shelter_id"], r["batch"], r["household_id"]) for r in pending])
        # 新车次必须由“可用”车辆执行；旧命令若已使某车离场，其后续任务随未出发家庭
        # 一起重新编排，候选生成时本来就排除了 departed 车辆
        if old_plan:
            c.execute("UPDATE plans SET status='superseded' WHERE id=?", (old_plan,))
        # 其余候选一并失效，避免指挥员误发布过期方案
        c.execute("UPDATE plans SET status='superseded' WHERE id!=? AND status='candidate'",
                  (plan_id,))

    v = db.bump_version()
    db.log_event("publish", f"发布撤离令 order#{order_id}（plan#{plan_id}，"
                            f"发布人 {body.published_by}）")
    return {"ok": True, "order_id": order_id, "plan_id": plan_id,
            "superseded_plan": old_plan, "version": v}


# ---------------- 车辆离场：批次与目的地占用正式承诺 ----------------

@app.post("/api/orders/depart")
def depart(body: DepartureIn):
    s = models.load_scenario()
    ao = models.active_order()
    if not ao:
        raise HTTPException(409, "当前没有已发布的撤离令")
    if body.order_id and body.order_id != ao["order_id"]:
        raise HTTPException(409, f"该命令已被 order#{ao['order_id']} 取代")

    # 按车辆出发时，只放行该车辆当前最早的待执行批次（一趟车不能一次跑两批）
    if body.vehicle_id is not None and body.batch is None:
        v_batches = sorted(a["batch"] for a in ao["assignments"]
                           if a["vehicle_id"] == body.vehicle_id
                           and not s.households[a["household_id"]].departed)
        if not v_batches:
            raise HTTPException(404, f"车辆 {body.vehicle_id} 没有待执行批次")
        body = body.model_copy(update={"batch": v_batches[0]})

    targets = []
    for a in ao["assignments"]:
        h = s.households[a["household_id"]]
        if h.departed:
            continue
        if body.batch is not None and a["batch"] != body.batch:
            continue
        if body.vehicle_id is not None and a["vehicle_id"] != body.vehicle_id:
            continue
        targets.append((a, h))

    if not targets:
        which = (f"批次 {body.batch}" if body.batch is not None
                 else f"车辆 {body.vehicle_id}" if body.vehicle_id else "活动命令")
        raise HTTPException(404, f"{which} 下没有待出发的家庭")

    vehicle_ids = sorted({a["vehicle_id"] for a, _ in targets})
    batches = sorted({a["batch"] for a, _ in targets})
    hh_ids = [h.id for _, h in targets]
    ts = body.depart_ts or now()

    with db.tx() as c:
        c.executemany(
            "UPDATE households SET departed=1,departure_ts=? WHERE id=? AND departed=0",
            [(ts, hid) for hid in hh_ids])
        # 车辆离场：故障/离场车不再参与后续重规划
        c.executemany(
            "UPDATE vehicles SET status='departed' WHERE id=? AND status='available'",
            [(vid,) for vid in vehicle_ids])

    v = db.bump_version()
    db.log_event("depart",
                 f"批次 {batches} 车辆 {vehicle_ids} 离场，{len(hh_ids)} 户承诺安置")
    return {"ok": True, "departed_households": hh_ids, "vehicles": vehicle_ids,
            "batches": batches, "committed_shelters": sorted({a["shelter_id"] for a, _ in targets}),
            "version": v,
            "note": "已出发家庭不得重新分配，其床位占用已锁定；后续规划只覆盖未出发家庭"}


# ---------------- 演示数据重置 ----------------

@app.post("/api/admin/reset")
def reset_demo():
    seed.seed(wipe=True)
    return {"ok": True, "version": db.version()}


@app.on_event("startup")
def _startup():
    # 进程重启即从 SQLite 恢复；仅在库为空时播种演示数据
    db.conn()
    if db.get_meta("version") is None:
        seed.seed(wipe=False)


# ---------------- 前端静态页 ----------------

import os  # noqa: E402

_STATIC = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=_STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(_STATIC, "index.html"))
