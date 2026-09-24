from __future__ import annotations

import random

from . import enemies as enemies_mod

# 节点类型
ENCOUNTER = "encounter"
ELITE = "elite"
REST = "rest"
REWARD = "reward"
FORGE = "forge"
SHOP = "shop"
EVENT = "event"   # 2.8.0：跨章奇遇节点（抉择与代价，埋下的 flag 随远征继承）
BOSS = "boss"

# 行数（从起点到首领的节点段数）
ROWS = 4
# 每条路线的列偏移；生成 ROWS 行，最后一行全指向 BOSS
NORMAL_POOL = ["goblin", "wolf", "brute", "maggot", "vampire", "echo_knight"]
ELITE_POOL = ["elite_warlord"]


def generate_map(seed):
    """确定性生成地图。返回 map 结构与节点池。

    地图结构：{nodes: {nid: {...}}, routes: {nid: [next_ids]}, start, boss}
    三条路线在每一行给出一个三选一推进选择。
    """
    rng = random.Random((seed * 97 + 11) & 0xFFFFFFFF)
    nodes = {}
    routes = {}

    def nid(row, col):
        return f"{row}-{col}"

    start = "start"
    nodes[start] = {"id": start, "type": "start", "label": "营地", "row": -1}
    routes[start] = [nid(0, c) for c in range(3)]

    for row in range(ROWS):
        for col in range(3):
            _id = nid(row, col)
            r = row
            # 行0 普通遭遇为主、不出商店（开局尚无金币）；越靠后越容易出精英/奖励/锻造/商店
            if r == 0:
                t = rng.choices([ENCOUNTER, REWARD, FORGE], [0.7, 0.2, 0.1])[0]
            elif r == 1:
                t = rng.choices([ENCOUNTER, REST, REWARD, FORGE, SHOP], [0.4, 0.2, 0.15, 0.1, 0.15])[0]
            elif r == 2:
                t = rng.choices([ENCOUNTER, ELITE, REWARD, FORGE, SHOP], [0.3, 0.25, 0.15, 0.15, 0.15])[0]
            else:
                t = rng.choices([ENCOUNTER, ELITE, REST, FORGE, SHOP], [0.3, 0.35, 0.15, 0.1, 0.1])[0]

            node = {"id": _id, "type": t, "row": r}
            if t in (ENCOUNTER, ELITE):
                pool = ELITE_POOL if t == ELITE else NORMAL_POOL
                node["enemy"] = rng.choice(pool)
            if t == ENCOUNTER and node["enemy"] == "echo_knight" and rng.random() < 0.5:
                node["enemy"] = "goblin"
            nodes[_id] = node

            if row < ROWS - 1:
                routes[_id] = [nid(row + 1, c) for c in range(3)]
            else:
                routes[_id] = ["boss"]

    boss = "boss"
    # 首领属性由 map 层携带；seed 亦用于重放
    nodes[boss] = {"id": boss, "type": BOSS, "row": ROWS, "enemy": "boss_ancient", "label": "远古守卫"}
    routes["boss"] = []

    # 2.8.0：每张图确定性安放恰好一个「奇遇」节点。使用独立派生流选位（在主
    # rng 全部抽完之后再抽），因此既有节点类型/敌人序列的确定性完全不变——
    # 老种子重生成时原有节点逐位一致，只是多出一个被改写为奇遇的节点。
    # 放在第 2~3 行（index 1..2）：既避开开局行，也保证无论玩家此前选哪条
    # 路线都能抵达（行间路由是全连接的）。只替换战斗/奖励/锻造节点，绝不
    # 替换商店/休息——商店数量与恢复点关系到既有地图拓扑与固定种子测试。
    # 选位偏好「同行有同类型备份」的节点：去掉它后，从上一行任一节点仍能
    # 抵达一个该类型节点（避免旧种子上唯一通路被改写）。
    ev_rng = random.Random((seed * 131 + 17) & 0xFFFFFFFF)
    grid = {(r, c): nodes[nid(r, c)] for r in (1, 2) for c in range(3)}
    replaceable = {pos for pos, nd in grid.items()
                   if nd["type"] in (ENCOUNTER, ELITE, REWARD, FORGE)}
    redundant = []
    for (r, c) in sorted(replaceable):
        siblings = [grid[(r, k)]["type"] for k in range(3)
                    if k != c and (r, k) in replaceable]
        # 放宽：同一「类别」（战斗类/锻造/奖励）有备份即可
        def kind_of(t):
            return "battle" if t in (ENCOUNTER, ELITE) else t
        if any(kind_of(t) == kind_of(grid[(r, c)]["type"]) for t in siblings):
            redundant.append(nid(r, c))
    ev_pool = redundant or [nid(r, c) for (r, c) in sorted(replaceable)]
    ev_id = ev_rng.choice(ev_pool)
    ev_node = nodes[ev_id]
    ev_node["type"] = EVENT
    ev_node.pop("enemy", None)

    return {
        "seed": seed, "start": start, "boss": boss, "rows": ROWS,
        "nodes": nodes, "routes": routes,
    }


def route_candidates(map_data, position):
    return list(map_data["routes"].get(position, []))


def next_from(map_data, position):
    """玩家在 start / 中间节点选定下一节点。合并三选一实际只有一条继续。"""
    cands = route_candidates(map_data, position)
    return cands[0] if cands else None