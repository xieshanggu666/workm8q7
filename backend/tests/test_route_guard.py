"""路线推进守卫：战斗未分胜负前不允许换节点。

回归：旧实现 _choose_node 既不检查 in_battle，又对 BOSS 位置放开了
可达性检查（position == boss 时可任选节点），导致：
- 普通战斗中直接点下一节点即可跳过当前遭遇推进路线；
- 首领战中可返回任意旧节点（休整/商店/奇遇/奖励），首领遭遇被重置丢弃。
"""
from app import db, mapgen, service


def _encounter_seed_node():
    for seed in range(0, 200):
        m = mapgen.generate_map(seed)
        for n in m["routes"][m["start"]]:
            if m["nodes"][n]["type"] == mapgen.ENCOUNTER:
                return seed, n
    raise AssertionError("cannot find a row-0 encounter node")


def _create_at_encounter(client):
    seed, node = _encounter_seed_node()
    r = client.post("/api/runs", json={"seed": seed})
    assert r.status_code == 200
    rid = r.json()["run_id"]
    entered = client.post(f"/api/runs/{rid}/act",
                          json={"action": "choose_node", "node": node})
    assert entered.status_code == 200
    assert entered.json()["run"]["in_battle"] is True
    return rid, node


def test_cannot_advance_route_mid_battle(client):
    rid, node = _create_at_encounter(client)
    rec = service.load_run(rid)
    nxt = rec["map"]["routes"][node][0]
    pos_before, battle_before = rec["state"]["position"], rec["state"]["battle"]

    r = client.post(f"/api/runs/{rid}/act",
                    json={"action": "choose_node", "node": nxt})
    assert r.status_code == 400
    # 存档未被推进：仍停在原节点、原战斗
    rec = service.load_run(rid)
    assert rec["state"]["position"] == pos_before
    assert rec["state"]["in_battle"] is True
    assert rec["state"]["battle"] == battle_before
    # 战斗行动照常可用
    assert client.post(f"/api/runs/{rid}/act",
                       json={"action": "end_turn"}).status_code == 200


def test_cannot_return_to_old_node_mid_boss_battle(client):
    r = client.post("/api/runs", json={"seed": 5})
    rid = r.json()["run_id"]
    # 把存档移到首领前一格再进入首领战（测试便捷路径，同 test_expedition）
    rec = service.load_run(rid)
    row3 = next(n for n, nd in rec["map"]["nodes"].items() if nd.get("row") == 3)
    rec["state"]["position"] = row3
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    entered = client.post(f"/api/runs/{rid}/act",
                          json={"action": "choose_node", "node": "boss"})
    assert entered.status_code == 200
    assert entered.json()["run"]["in_battle"] is True

    # 旧节点（任意一个历史节点）一律不可返回
    for old in ("0-0", "1-1", "2-2"):
        r = client.post(f"/api/runs/{rid}/act",
                        json={"action": "choose_node", "node": old})
        assert r.status_code == 400
    rec = service.load_run(rid)
    assert rec["state"]["position"] == "boss"
    assert rec["state"]["in_battle"] is True
    assert rec["state"]["battle"]["enemy"] == "boss_ancient"


def test_boss_not_directly_reachable_from_other_nodes(client):
    # 删除 BOSS 位置特例后：不在末行节点上时不能直达首领
    r = client.post("/api/runs", json={"seed": 5})
    rid = r.json()["run_id"]
    r = client.post(f"/api/runs/{rid}/act",
                    json={"action": "choose_node", "node": "boss"})
    assert r.status_code == 400
    assert service.load_run(rid)["state"]["position"] == "start"


def test_cannot_leave_node_during_ambush_battle(client):
    # 奇遇伏击战同样锁节点：伏击也是 in_battle=True 的战斗，进入后不能借
    # 推进路线逃跑（伏击挂在非节点上下文，位置仍停在奇遇节点）。
    rid, node = _create_at_encounter(client)
    rec = service.load_run(rid)
    nxt = rec["map"]["routes"][node][0]
    r = client.post(f"/api/runs/{rid}/act",
                    json={"action": "choose_node", "node": nxt})
    assert r.status_code == 400


def test_route_advance_after_battle_won_still_works(client):
    rid, node = _create_at_encounter(client)
    # 压残敌人，用一张打击结束战斗
    rec = service.load_run(rid)
    rec["state"]["battle"]["entities"]["enemy"]["hp"] = 1
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    view = client.get(f"/api/runs/{rid}/resume").json()
    strike = next(h for h in view["battle"]["hand"] if h["id"] == "strike")
    won = client.post(f"/api/runs/{rid}/act",
                      json={"action": "play", "card": strike["uid"]})
    assert won.status_code == 200
    assert won.json()["run"]["in_battle"] is False
    # 领奖后推进到下一节点不受影响
    assert client.post(f"/api/runs/{rid}/act",
                       json={"action": "claim_reward", "option": 0}).status_code == 200
    nxt = rec["map"]["routes"][node][0]
    moved = client.post(f"/api/runs/{rid}/act",
                        json={"action": "choose_node", "node": nxt})
    assert moved.status_code == 200
    assert moved.json()["run"]["position"] == nxt
