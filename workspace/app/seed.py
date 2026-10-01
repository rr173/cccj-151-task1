"""初始演示数据 + 重置。

包含：受威胁网格、带容量/封闭状态的道路、两个避难点、四辆车、十户家庭。
重复调用安全（INSERT OR IGNORE）；reset 则清空重建。
"""
from __future__ import annotations

from . import db

# (id, 名称, lat, lng, 威胁等级)
CELLS = [
    ("C1", "松涛里 1 号网格", 30.010, 120.110, "burning"),
    ("C2", "松涛里 2 号网格", 30.010, 120.124, "threatened"),
    ("C3", "北坡安置区",       30.024, 120.106, "threatened"),
    ("C4", "林场家属院",       30.028, 120.128, "watch"),
    ("C5", "河谷村",          30.002, 120.132, "watch"),
]

# (id, lat, lng, kind)
NODES = [
    ("N1", 30.010, 120.110, "junction"),
    ("N2", 30.010, 120.124, "junction"),
    ("N3", 30.022, 120.106, "junction"),
    ("N4", 30.026, 120.128, "junction"),
    ("N5", 30.016, 120.102, "junction"),
    ("N6", 30.006, 120.138, "junction"),
    ("N7", 30.002, 120.132, "junction"),
    ("N8", 30.026, 120.150, "junction"),
    ("S1N", 30.040, 120.160, "shelter"),
    ("S2N", 29.985, 120.105, "shelter"),
]

# (id, a, b, 名称, 状态, 每批次车次容量, 米)
EDGES = [
    ("E01", "N1", "N2", "松涛路",       "open",   3, 1400),
    ("E02", "N1", "N5", "西麓道",       "open",   2, 900),
    ("E03", "N2", "N6", "河谷桥",       "open",   1, 1500),
    ("E04", "N2", "N4", "北环路",       "open",   2, 1100),
    ("E05", "N3", "N5", "北坡支路",     "open",   2, 900),
    ("E06", "N3", "N4", "林场连接线",   "open",   2, 1000),
    ("E07", "N4", "N8", "东进大道",     "open",   3, 2400),
    ("E08", "N6", "N7", "河谷村道",     "open",   2, 800),
    ("E09", "N6", "N8", "绕山东线",     "open",   2, 1800),
    ("E10", "N8", "S1N", "体育馆通道",  "open",   3, 1900),
    ("E11", "N5", "S2N", "南溪快速路",  "open",   2, 2200),
    ("E12", "N1", "N3", "火区近线",     "closed", 2, 1500),   # 火线逼近，已封闭
]

# (id, 名称, 节点, 床位, 无障碍, 接收中, 备注)
SHELTERS = [
    ("S1", "市体育馆避难点", "S1N", 60, 1, 1, "可接收宠物笼"),
    ("S2", "南溪学校避难点", "S2N", 40, 0, 0, "暂停接收：值班人员不足"),
]

# (id, 车牌, 座位, 无障碍, 停放节点)
VEHICLES = [
    ("V1", "浙A·1001", 15, 0, "N1"),
    ("V2", "浙A·1002", 8,  1, "N3"),
    ("V3", "浙A·1003", 12, 0, "N5"),
    ("V4", "浙A·1004", 6,  1, "N4"),
]

# (id, 户名, 人数, 行动辅助, 宠物, 联系电话, 网格, 上车节点)
HOUSEHOLDS = [
    ("H01", "张伟东户", 4, 0, "",       "13800000001", "C1", "N1"),
    ("H02", "李桂兰户", 2, 1, "",       "13800000002", "C1", "N1"),
    ("H03", "王海涛户", 5, 0, "犬1",    "13800000003", "C1", "N1"),
    ("H04", "赵敏户",   3, 0, "",       "13800000004", "C2", "N2"),
    ("H05", "陈建国户", 6, 0, "",       "13800000005", "C2", "N2"),
    ("H06", "刘芳户",   2, 1, "猫2",    "13800000006", "C3", "N3"),
    ("H07", "周德海户", 7, 0, "",       "13800000007", "C5", "N7"),
    ("H08", "吴秀珍户", 1, 1, "",       "13800000008", "C4", "N4"),
    ("H09", "郑小虎户", 4, 0, "犬1",    "13800000009", "C4", "N4"),
    ("H10", "孙立户",   3, 0, "",       "13800000010", "C3", "N3"),
    ("H11", "马丽华户", 5, 0, "",       "13800000011", "C2", "N2"),
]


def seed(wipe: bool = False) -> None:
    c = db.conn()
    with db.tx() as cx:
        if wipe:
            for t in ("plan_assignments", "orders", "plans", "households",
                      "vehicles", "shelters", "edges", "nodes", "cells", "events", "meta"):
                cx.execute(f"DELETE FROM {t}")
        cx.executemany(
            "INSERT OR IGNORE INTO cells(id,name,lat,lng,threat) VALUES(?,?,?,?,?)",
            CELLS)
        cx.executemany(
            "INSERT OR IGNORE INTO nodes(id,lat,lng,kind) VALUES(?,?,?,?)",
            NODES)
        cx.executemany(
            "INSERT OR IGNORE INTO edges(id,a,b,name,status,capacity,length_m) "
            "VALUES(?,?,?,?,?,?,?)", EDGES)
        cx.executemany(
            "INSERT OR IGNORE INTO shelters(id,name,node_id,beds,accessible,accepting,note) "
            "VALUES(?,?,?,?,?,?,?)", SHELTERS)
        cx.executemany(
            "INSERT OR IGNORE INTO vehicles(id,plate,seats,accessible,home_node,status) "
            "VALUES(?,?,?,?,?,'available')", VEHICLES)
        cx.executemany(
            "INSERT OR IGNORE INTO households(id,name,persons,needs_assist,pets,contact,"
            "cell_id,pickup_node) VALUES(?,?,?,?,?,?,?,?)", HOUSEHOLDS)
        if db.get_meta("version") is None:
            cx.execute("INSERT INTO meta(key,value) VALUES('version','1')")
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        cx.execute("INSERT INTO events(ts,kind,detail) VALUES(?,?,?)",
                   (ts, "seed" if not wipe else "reset", "导入初始场景数据"))
    if wipe:
        db.bump_version()
