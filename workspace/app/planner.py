"""撤离规划器。

家庭作为不可拆分单元，被分配到 (避难点, 车辆车次, 批次)。
约束：
  * 每个未出发家庭至多被一个车次接走（不允许自行撤离经过已关闭路段）；
  * 车辆每批次至多执行一个车次；故障/已离场车辆不可用；
  * 车次数座位 >= 车上家庭总人数；行动辅助家庭只能上无障碍车辆；
  * 避难点剩余床位 >= 分配总人数；行动辅助家庭只能去无障碍避难点；
  * 暂停接收的避难点不可分配；
  * 车辆只沿“开放”道路行驶（Dijkstra 预计算可达路径），
    且每条道路每批次通过的车次不超过其容量。
"""
from __future__ import annotations

import heapq
import math
from collections import defaultdict

from .models import Scenario

MAX_BATCH = 7

# 方案画像：权重为“每米成本”，数值越小越优先该项
PROFILES = {
    "balanced":  {"urgency_per_batch": 250, "per_meter": 1.0, "minimax_w": 120},
    "urgent":    {"urgency_per_batch": 900, "per_meter": 0.3, "minimax_w": 60},
    "shortest":  {"urgency_per_batch": 40,  "per_meter": 2.0, "minimax_w": 300},
}
URGENCY_SCORE = {"burning": 3, "threatened": 2, "watch": 1, "safe": 0}


# ---------- 地理 / 路网 ----------

def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def dijkstra_all(adj: dict[str, list[tuple[str, object]]], src: str):
    """单源最短路，返回 {目标节点: (长度米, [边id...])}。"""
    dist = {src: 0.0}
    prev: dict[str, tuple[str, str]] = {}
    pq = [(0.0, src)]
    while pq:
        d, u = heapq.heappop(pq)
        if d > dist.get(u, math.inf):
            continue
        for v, e in adj.get(u, []):
            nd = d + e.length_m
            if nd < dist.get(v, math.inf):
                dist[v] = nd
                prev[v] = (u, e.id)
                heapq.heappush(pq, (nd, v))
    out = {}
    for dst, dlen in dist.items():
        edges, cur = [], dst
        while cur != src:
            u, eid = prev[cur]
            edges.append(eid)
            cur = u
        edges.reverse()
        out[dst] = (dlen, edges)
    return out


# ---------- 候选结构 ----------

class PlanProblem:
    def __init__(self, sc: Scenario, start_batch: int, committed_beds: dict[str, int]):
        self.sc = sc
        self.start_batch = start_batch
        self.batches = list(range(start_batch, start_batch + MAX_BATCH))
        self.committed = committed_beds

        # 只编排未出发家庭、可用车辆、开放网络
        self.households = {hid: h for hid, h in sc.households.items() if not h.departed}
        self.vehicles = {vid: v for vid, v in sc.vehicles.items() if v.status == "available"}
        self.adj = sc.open_adjacency()

        # 可接收的避难点 + 剩余床位
        self.shelters = {}
        self.remaining = {}
        for sid, sh in sc.shelters.items():
            rem = sh.beds - committed_beds.get(sid, 0)
            if sh.accepting and rem > 0:
                self.shelters[sid] = sh
                self.remaining[sid] = rem

        # 节点级最短路表：sp[源][宿] = (距离, [边...])，仅沿开放道路
        sp = {}
        for o in sc.nodes:
            sp[o] = dijkstra_all(self.adj, o)

        # route[(origin, sid)] = (dist, [edge ids])
        self.route: dict[tuple[str, str], tuple[float, list[str]]] = {}
        origins = {h.pickup_node for h in self.households.values()} | \
                  {v.home_node for v in self.vehicles.values()}
        for o in origins:
            if o not in sc.nodes:
                continue
            for sid, sh in self.shelters.items():
                if sh.node_id in sp.get(o, {}):
                    self.route[(o, sid)] = sp[o][sh.node_id]

        # 每个家庭可选 (避难点, 批次)：可达且避难点无障碍限制满足
        self.h_options: dict[str, list[tuple[str, int]]] = {}
        for hid, h in self.households.items():
            opts = []
            for sid, sh in self.shelters.items():
                if h.needs_assist and not sh.accessible:
                    continue
                if (h.pickup_node, sid) in self.route:
                    for b in self.batches:
                        opts.append((sid, b))
            self.h_options[hid] = opts

        # 车次候选项：vehicle, sid, batch, pickup origin, 途经边
        self.trips = []
        seen = set()
        for vid, v in self.vehicles.items():
            for sid in self.shelters:
                if (v.home_node, sid) not in self.route:
                    continue
                for b in self.batches:
                    origins = {h.pickup_node for h in self.households.values()
                               if (h.pickup_node, sid) in self.route
                               and (not h.needs_assist or v.accessible)}
                    for origin in origins:
                        key = (vid, sid, b, origin)
                        if key in seen:
                            continue
                        # 车辆: 停放点 -> 家庭上车点（空驶接人），再 -> 避难点；
                        # 两段都必须沿开放道路可达
                        leg1 = sp.get(v.home_node, {}).get(origin)
                        leg2 = self.route.get((origin, sid))
                        if not leg2 or (v.home_node != origin and not leg1):
                            continue
                        seen.add(key)
                        path_edges = (leg1[1] if v.home_node != origin else []) + leg2[1]
                        dist = (leg1[0] if v.home_node != origin else 0.0) + leg2[0]
                        self.trips.append({
                            "key": key, "vehicle": vid, "shelter": sid,
                            "batch": b, "origin": origin,
                            "edges": path_edges, "dist": dist,
                        })

        # 可服务家庭 hid 的车次下标
        self.trips_for_h: dict[str, list[int]] = defaultdict(list)
        for ti, t in enumerate(self.trips):
            for hid, h in self.households.items():
                if h.pickup_node != t["origin"]:
                    continue
                sh = self.shelters[t["shelter"]]
                if h.needs_assist and not (self.vehicles[t["vehicle"]].accessible and sh.accessible):
                    continue
                self.trips_for_h[hid].append(ti)


# ---------- 诊断（无可行安置的具体约束说明） ----------

def diagnose(p: PlanProblem) -> dict:
    """针对未安排家庭逐个给出阻塞原因，并汇总全局瓶颈。"""
    per_household = []
    reasons_count: dict[str, int] = defaultdict(int)

    for hid, h in p.households.items():
        reasons = []
        sh = p.sc.shelters

        reachable_open = [sid for sid in sh if (h.pickup_node, sid) in p.route]
        if not reachable_open:
            reasons.append("NO_ROUTE: 沿当前开放道路无法到达任何避难点（存在封闭路段阻隔）")
        else:
            acc_ok = [sid for sid in reachable_open
                      if not h.needs_assist or sh[sid].accessible]
            if h.needs_assist and not acc_ok:
                reasons.append("NO_ACCESSIBLE_SHELTER: 可达的避难点均不具备无障碍条件")
            accepting = [sid for sid in acc_ok if sh[sid].accepting]
            paused = [sid for sid in acc_ok if not sh[sid].accepting]
            if not accepting:
                names = "、".join(sh[s].name for s in paused) or "—"
                reasons.append(f"SHELTER_PAUSED: 可达避难点已暂停接收（{names}）")
            else:
                free = [sid for sid in accepting if p.remaining[sid] >= h.persons]
                if not free:
                    names = "、".join(f"{sh[s].name}(剩{p.remaining[s]})" for s in accepting)
                    reasons.append(f"NO_BEDS: 可达避难点床位不足（{names}）")

        vehs = p.vehicles
        seat_ok = [vid for vid, v in vehs.items() if v.seats >= h.persons]
        if not seat_ok:
            reasons.append(f"NO_VEHICLE_CAPACITY: 没有可用车辆能容纳该户 {h.persons} 人")
        if h.needs_assist:
            acc_v = [vid for vid in seat_ok if vehs[vid].accessible]
            if not acc_v:
                reasons.append("NO_ACCESSIBLE_VEHICLE: 需要行动辅助但没有可用的无障碍车辆")

        # 有车有床位，但无法在同一趟匹配（如车到不了上车点/避难点方向不通）
        feasible_trips = p.trips_for_h.get(hid, [])
        veh_cap = bool(seat_ok) and (not h.needs_assist or
                                     any(vehs[vid].accessible for vid in seat_ok))
        if feasible_trips and veh_cap and any(
            sh[t["shelter"]].accepting and p.remaining[t["shelter"]] >= h.persons
            and (not h.needs_assist or
                 (vehs[t["vehicle"]].accessible and sh[t["shelter"]].accessible))
            for t in (p.trips[i] for i in feasible_trips)
        ):
            pass  # 结构性可行；若最终未分配则多半是道路容量/总运力等组合约束
        elif reasons:
            pass
        else:
            reasons.append("ROAD_CAPACITY: 通往可用避难点的道路批次容量不足，车辆无法在任何批次完成接驳")

        if not reasons:
            reasons.append("CAPACITY_TIGHT: 单车/床位/道路容量组合约束下本轮无法安排，建议增加车辆或等待资源恢复")

        cell = p.sc.cells.get(h.cell_id)
        per_household.append({
            "household_id": hid,
            "name": h.name,
            "persons": h.persons,
            "needs_assist": h.needs_assist,
            "threat": cell.threat if cell else "unknown",
            "reasons": reasons,
        })
        for r in reasons:
            reasons_count[r.split(":")[0]] += 1

    # 全局瓶颈
    bottlenecks = []
    total_people = sum(h.persons for h in p.households.values())
    total_seats = sum(v.seats for v in p.vehicles.values()) * len(p.batches)
    if total_people > total_seats:
        bottlenecks.append(
            f"待撤离 {total_people} 人，但 {len(p.vehicles)} 辆可用车在 "
            f"{len(p.batches)} 个批次内最多提供 {total_seats} 座位")
    total_beds = sum(p.remaining.values())
    if total_people > total_beds:
        bottlenecks.append(f"待撤离 {total_people} 人，可接收避难点合计仅剩 {total_beds} 床")
    acc_h = [h for h in p.households.values() if h.needs_assist]
    acc_v = [v for v in p.vehicles.values() if v.accessible]
    if acc_h and not acc_v:
        bottlenecks.append(f"{len(acc_h)} 户需要行动辅助，但没有可用无障碍车辆")
    closed = [e for e in p.sc.edges.values() if e.status == "closed"]
    if closed:
        bottlenecks.append("封闭道路：" + "、".join(f"{e.name or e.id}" for e in closed))

    return {
        "unassigned_households": per_household,
        "reason_counts": dict(reasons_count),
        "bottlenecks": bottlenecks,
    }


# ---------- CP-SAT 求解 ----------

def solve(sc: Scenario, start_batch: int, committed_beds: dict[str, int],
          profile: str = "balanced", time_limit_s: float = 15.0) -> dict:
    from ortools.sat.python import cp_model

    p = PlanProblem(sc, start_batch, committed_beds)
    weights = PROFILES.get(profile, PROFILES["balanced"])

    model = cp_model.CpModel()

    # y[t]：车次是否执行
    y = [model.NewBoolVar(f"trip_{i}") for i in range(len(p.trips))]
    # x[(hid, ti)]：家庭 hid 由车次 ti 接走
    x: dict[tuple[str, int], object] = {}
    for hid, h in p.households.items():
        for ti in p.trips_for_h.get(hid, []):
            x[(hid, ti)] = model.NewBoolVar(f"x_{hid}_{ti}")

    assigned = {}
    for hid, h in p.households.items():
        xs = [x[(hid, ti)] for ti in p.trips_for_h.get(hid, [])]
        if xs:
            a = model.NewBoolVar(f"assigned_{hid}")
            model.Add(sum(xs) == a)
        else:
            a = model.NewConstant(0)
        assigned[hid] = a

    # 家庭 -> 车次：同车总人数不超过座位
    for ti, t in enumerate(p.trips):
        expr = sum(p.households[hid].persons * x[(hid, ti)]
                   for hid in p.households
                   if (hid, ti) in x)
        model.Add(expr <= p.vehicles[t["vehicle"]].seats).OnlyEnforceIf(y[ti])
        # 车次上有人则必须执行
        for hid in p.households:
            if (hid, ti) in x:
                model.AddImplication(x[(hid, ti)], y[ti])

    # 车辆每批次最多一个车次
    by_vehicle_batch: dict[tuple[str, int], list[int]] = defaultdict(list)
    for ti, t in enumerate(p.trips):
        by_vehicle_batch[(t["vehicle"], t["batch"])].append(ti)
    for trips in by_vehicle_batch.values():
        model.Add(sum(y[i] for i in trips) <= 1)

    # 避难点床位（已扣除出发家庭锁定床位）
    by_shelter: dict[str, object] = defaultdict(int)
    for (hid, ti), var in x.items():
        by_shelter[p.trips[ti]["shelter"]] += p.households[hid].persons * var
    for sid, expr in by_shelter.items():
        model.Add(expr <= p.remaining[sid])

    # 道路每批次容量：按 (edge, batch) 汇总通过车次
    flow: dict[tuple[str, int], object] = defaultdict(int)
    for ti, t in enumerate(p.trips):
        for eid in set(t["edges"]):
            flow[(eid, t["batch"])] += y[ti]
    for (eid, b), expr in flow.items():
        model.Add(expr <= sc.edges[eid].capacity)

    # 目标：最大化 撤离人数*1_000_000 − 批次紧迫惩罚 − 行驶距离 − 最远车次惩罚
    reward_terms = []
    max_dist_terms = []
    for hid, h in p.households.items():
        reward_terms.append(1_000_000 * assigned[hid])
        cell = sc.cells.get(h.cell_id)
        urg = URGENCY_SCORE.get(cell.threat if cell else "watch", 1)
        for ti in p.trips_for_h.get(hid, []):
            t = p.trips[ti]
            delay = t["batch"] - start_batch
            cost = weights["urgency_per_batch"] * urg * delay + round(weights["per_meter"] * t["dist"])
            if cost > 0:
                reward_terms.append(-cost * x[(hid, ti)])
    for ti, t in enumerate(p.trips):
        max_dist_terms.append((round(weights["per_meter"] * t["dist"]) * y[ti], ti))
    model.Maximize(sum(reward_terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s
    solver.parameters.num_search_workers = 4
    solver.parameters.log_search_progress = False
    status = solver.Solve(model)

    feasible = status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    assignments = []
    assigned_hids: set[str] = set()
    used_trips: dict[int, dict] = {}

    if feasible:
        for (hid, ti), var in x.items():
            if solver.Value(var):
                t = p.trips[ti]
                used_trips[ti] = t
                assignments.append({
                    "household_id": hid,
                    "vehicle_id": t["vehicle"],
                    "shelter_id": t["shelter"],
                    "batch": t["batch"],
                    "path_edges": t["edges"],
                    "dist_m": round(t["dist"]),
                })
                assigned_hids.add(hid)

    unassigned = [hid for hid in p.households if hid not in assigned_hids]
    diag = diagnose(p)
    diag["unassigned_households"] = [
        d for d in diag["unassigned_households"] if d["household_id"] in set(unassigned)
    ]

    # 汇总按批次/车辆/避难点
    by_batch = defaultdict(lambda: {"households": 0, "persons": 0, "vehicles": set()})
    for a in assignments:
        bb = by_batch[a["batch"]]
        bb["households"] += 1
        bb["persons"] += p.households[a["household_id"]].persons
        bb["vehicles"].add(a["vehicle_id"])
    batches_summary = [
        {"batch": b, "households": d["households"], "persons": d["persons"],
         "vehicles": sorted(d["vehicles"])}
        for b, d in sorted(by_batch.items())
    ]

    return {
        "feasible": feasible,
        "optimal": status == cp_model.OPTIMAL,
        "profile": profile,
        "start_batch": start_batch,
        "batches": p.batches,
        "assignments": assignments,
        "assigned_count": len(assigned_hids),
        "unassigned_count": len(unassigned),
        "unassigned_ids": unassigned,
        "diagnostics": diag,
        "batches_summary": batches_summary,
        "objective": int(solver.ObjectiveValue()) if feasible else 0,
    }
