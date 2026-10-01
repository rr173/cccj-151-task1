"""领域模型与从 SQLite 装载当前场景状态的读取器。"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Optional

from . import db


@dataclass
class Cell:
    id: str
    name: str
    lat: float
    lng: float
    threat: str = "watch"


@dataclass
class Node:
    id: str
    lat: float
    lng: float
    kind: str = "junction"


@dataclass
class Edge:
    id: str
    a: str
    b: str
    name: str
    status: str          # open / closed
    capacity: int
    length_m: float


@dataclass
class Shelter:
    id: str
    name: str
    node_id: str
    beds: int
    accessible: bool
    accepting: bool
    note: str = ""


@dataclass
class Vehicle:
    id: str
    plate: str
    seats: int
    accessible: bool
    home_node: str
    status: str = "available"   # available / broken / departed


@dataclass
class Household:
    id: str
    name: str
    persons: int
    needs_assist: bool
    pets: str
    contact: str
    cell_id: str
    pickup_node: str
    departed: bool = False
    departure_ts: Optional[str] = None
    order_id: Optional[int] = None
    shelter_id: Optional[str] = None
    batch: Optional[int] = None


@dataclass
class Scenario:
    version: int
    cells: dict[str, Cell] = field(default_factory=dict)
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: dict[str, Edge] = field(default_factory=dict)
    shelters: dict[str, Shelter] = field(default_factory=dict)
    vehicles: dict[str, Vehicle] = field(default_factory=dict)
    households: dict[str, Household] = field(default_factory=dict)

    def open_adjacency(self) -> dict[str, list[tuple[str, Edge]]]:
        adj: dict[str, list[tuple[str, Edge]]] = {n: [] for n in self.nodes}
        for e in self.edges.values():
            if e.status == "open":
                adj[e.a].append((e.b, e))
                adj[e.b].append((e.a, e))
        return adj


def load_scenario() -> Scenario:
    c = db.conn()
    s = Scenario(version=db.version())
    for r in c.execute("SELECT * FROM cells"):
        s.cells[r["id"]] = Cell(r["id"], r["name"], r["lat"], r["lng"], r["threat"])
    for r in c.execute("SELECT * FROM nodes"):
        s.nodes[r["id"]] = Node(r["id"], r["lat"], r["lng"], r["kind"])
    for r in c.execute("SELECT * FROM edges"):
        s.edges[r["id"]] = Edge(
            r["id"], r["a"], r["b"], r["name"], r["status"], r["capacity"], r["length_m"]
        )
    for r in c.execute("SELECT * FROM shelters"):
        s.shelters[r["id"]] = Shelter(
            r["id"], r["name"], r["node_id"], r["beds"],
            bool(r["accessible"]), bool(r["accepting"]), r["note"],
        )
    for r in c.execute("SELECT * FROM vehicles"):
        s.vehicles[r["id"]] = Vehicle(
            r["id"], r["plate"], r["seats"], bool(r["accessible"]),
            r["home_node"], r["status"],
        )
    for r in c.execute("SELECT * FROM households"):
        s.households[r["id"]] = Household(
            r["id"], r["name"], r["persons"], bool(r["needs_assist"]),
            r["pets"], r["contact"], r["cell_id"], r["pickup_node"],
            bool(r["departed"]), r["departure_ts"], r["order_id"],
            r["shelter_id"], r["batch"],
        )
    return s


# ---------- 已承诺（committed）状态 ----------

def committed_beds() -> dict[str, int]:
    """每个避难点已出发家庭锁定占用的床位数。"""
    out: dict[str, int] = {}
    for r in db.conn().execute(
        "SELECT shelter_id, persons FROM households h WHERE departed=1 AND shelter_id IS NOT NULL"
    ):
        out[r["shelter_id"]] = out.get(r["shelter_id"], 0) + r["persons"]
    return out


def active_order() -> Optional[dict]:
    row = db.conn().execute(
        "SELECT o.id AS order_id, o.plan_id, o.published_ts, o.published_by, "
        "p.profile, p.base_version "
        "FROM orders o JOIN plans p ON p.id=o.plan_id "
        "ORDER BY o.id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["assignments"] = [dict(r) for r in db.conn().execute(
        "SELECT household_id, vehicle_id, shelter_id, batch FROM plan_assignments "
        "WHERE plan_id=? ORDER BY batch, shelter_id, vehicle_id", (d["plan_id"],)
    )]
    return d


def plan_dict(plan_row) -> dict:
    d = dict(plan_row)
    d["summary"] = json.loads(d.get("summary") or "{}")
    d["assignments"] = [dict(r) for r in db.conn().execute(
        "SELECT household_id, vehicle_id, shelter_id, batch, path "
        "FROM plan_assignments WHERE plan_id=? ORDER BY batch, shelter_id, vehicle_id",
        (d["id"],),
    )]
    for a in d["assignments"]:
        a["path"] = json.loads(a["path"] or "[]")
    return d


def scenario_public_dict(s: Scenario) -> dict:
    beds = committed_beds()
    return {
        "version": s.version,
        "cells": [asdict(x) for x in s.cells.values()],
        "nodes": [asdict(x) for x in s.nodes.values()],
        "edges": [asdict(x) for x in s.edges.values()],
        "shelters": [
            {**asdict(x), "committed_beds": beds.get(x.id, 0),
             "remaining_beds": x.beds - beds.get(x.id, 0)}
            for x in s.shelters.values()
        ],
        "vehicles": [asdict(x) for x in s.vehicles.values()],
        "households": [asdict(x) for x in s.households.values()],
        "active_order": active_order(),
    }
