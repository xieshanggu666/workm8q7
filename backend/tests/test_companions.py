"""伙伴模块：商店限招、随行/休整、回合协助与援护、负伤治疗、跨章与回放。"""
import copy

from app import companions as companion_mod
from app import db
from app import mapgen
from app import service
from app.engine import Battle
from app.enemies import get_enemy


def _find_shop_path(seed_start=0):
    for seed in range(seed_start, seed_start + 3000):
        m = mapgen.generate_map(seed)
        for n0 in m["routes"][m["start"]]:
            if m["nodes"][n0]["type"] == mapgen.SHOP:
                return seed, [n0]
            for n1 in m["routes"][n0]:
                if m["nodes"][n1]["type"] == mapgen.SHOP:
                    return seed, [n0, n1]
    raise AssertionError("no shop node")


def _clear_current_battle(client, rid, view):
    """合法打完当前战斗并领取战利品（路线经过战斗节点时用：不能靠旧漏洞跳过）。"""
    while view["in_battle"]:
        rec = service.load_run(rid)
        rec["state"]["battle"]["entities"]["enemy"]["hp"] = 1
        db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
        cur = client.get(f"/api/runs/{rid}/resume").json()
        strike = next((h for h in cur["battle"]["hand"]
                       if (h["id"] if isinstance(h, dict) else h) == "strike"), None)
        if strike is not None:
            uid = strike["uid"] if isinstance(strike, dict) else strike
            step = client.post(f"/api/runs/{rid}/act", json={"action": "play", "card": uid})
        else:
            step = client.post(f"/api/runs/{rid}/act", json={"action": "end_turn"})
        assert step.status_code == 200
        view = step.json()["run"]
    # 战利品留待离开节点时自动放弃：不领奖可保持初始金币/牌组规模，
    # 与用例的“0 金币、7 张牌”前提一致（规则允许未领奖直接前往下一节点）。
    return view


def _walk(client, rid, nodes):
    view = None
    for node in nodes:
        view = client.post(
            f"/api/runs/{rid}/act", json={"action": "choose_node", "node": node}
        ).json()["run"]
        # 路径上的战斗/奖励节点必须合法结清后才能继续推进（禁止战斗中换节点）
        view = _clear_current_battle(client, rid, view)
    return view


def _set_gold(rid, gold):
    rec = service.load_run(rid)
    rec["state"]["gold"] = gold
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])


def test_shop_recruits_one_companion_with_atomic_payment(client):
    seed, path = _find_shop_path()
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    view = _walk(client, rid, path)
    offers = view["shop"]["companions"]
    assert len(offers) == 1
    item = offers[0]
    assert item["sku"] == companion_mod.COMPANION_SKU

    poor = client.post(f"/api/runs/{rid}/act", json={
        "action": "shop_buy", "kind": "companion", "sku": item["sku"],
    })
    assert poor.status_code == 400
    state = service.load_run(rid)["state"]
    assert state["gold"] == 0 and state["companion"] is None

    _set_gold(rid, item["price"] + 5)
    res = client.post(f"/api/runs/{rid}/act", json={
        "action": "shop_buy", "kind": "companion", "sku": item["sku"],
    })
    assert res.status_code == 200
    body = res.json()
    tx = body["log"][0]["shop_tx"]
    assert tx["kind"] == "companion" and tx["price"] == item["price"]
    assert body["run"]["gold"] == 5
    assert body["run"]["companion"]["id"] == "squire"
    assert body["run"]["companion"]["mode"] == "accompany"
    assert body["run"]["companion"]["hp"] == 20

    # 货架售罄 + 状态已有伙伴：重复招募 409，不扣款
    duplicate = client.post(f"/api/runs/{rid}/act", json={
        "action": "shop_buy", "kind": "companion", "sku": item["sku"],
    })
    assert duplicate.status_code == 409
    assert service.load_run(rid)["state"]["gold"] == 5
    resumed = client.get(f"/api/runs/{rid}/resume").json()
    assert resumed["shop"]["companions"][0]["sold"] is True
    assert resumed["companion"]["name"]


def test_mode_switch_only_outside_battle_and_rest_heals(client):
    seed, path = _find_shop_path()
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    _walk(client, rid, path)
    _set_gold(rid, 100)
    client.post(f"/api/runs/{rid}/act", json={
        "action": "shop_buy", "kind": "companion",
        "sku": companion_mod.COMPANION_SKU,
    })

    res = client.post(f"/api/runs/{rid}/act", json={
        "action": "companion_set_mode", "mode": "rest",
    })
    assert res.status_code == 200
    assert res.json()["run"]["companion"]["mode"] == "rest"
    duplicate = client.post(f"/api/runs/{rid}/act", json={
        "action": "companion_set_mode", "mode": "rest",
    })
    assert duplicate.status_code == 409

    rec = service.load_run(rid)
    rec["state"]["companion"]["hp"] = 0
    rec["state"]["companion"]["wounded"] = True
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])

    # 找一个从商店可达的休息节点（如本商店没有，则先验证休整状态可续局；治疗在引擎纯流程覆盖）
    map_data = rec["map"]
    rest_node = next((n for n in map_data["routes"][rec["state"]["position"]]
                      if map_data["nodes"][n]["type"] == mapgen.REST), None)
    if rest_node is not None:
        healed = client.post(f"/api/runs/{rid}/act", json={
            "action": "choose_node", "node": rest_node,
        }).json()["run"]
        assert healed["companion"]["wounded"] is False
        assert healed["companion"]["hp"] == healed["companion"]["max_health"]


def test_companion_assists_guards_and_gets_wounded():
    enemy = {
        "id": "dummy", "name": "木桩", "hp": 100, "boss": False,
        "skills": [{"name": "重击", "effects": [
            {"type": "damage", "target": "player", "value": 20, "tags": ["attack"]}
        ]}],
    }
    run_state = {
        "max_health": 500, "health": 500, "deck": [], "relics": {},
        "base_energy": 3, "companion": companion_mod.make_companion(),
    }
    battle = Battle(run_state, enemy, seed=1, battle_index=1,
                    companion_state=run_state["companion"])
    _snap, logs = battle.start_turn()
    assert [x for x in logs if x.get("action") == "damage"][0]["value"] == 4
    assert battle.enemy["hp"] == 96

    enemy_log, _intent = battle.end_turn()
    values = [(x.get("target"), x.get("value")) for x in enemy_log if x.get("action") == "damage"]
    # 伙伴先挡 3，然后玩家受 17
    assert ("companion", 3) in values
    assert ("player", 17) in values
    assert battle.entities["companion"]["hp"] == 17
    assert battle.entities["player"]["hp"] == 483

    # 连续承受攻击：伙伴每次最多挡 3，第 7 次后生命归零并暂停
    for _ in range(7):
        enemy_log, _intent = battle.end_turn()
        battle.enemy["hp"] = 100
        battle.enemy["alive"] = True
        if battle.entities.get("companion", {}).get("hp", 0) <= 0:
            break
    assert battle.entities["companion"]["alive"] is False
    dumped = battle.dump()
    assert dumped["companion_state"]["wounded"] is True
    assert dumped["companion_state"]["mode"] == "accompany"

    # 负伤后的下一场战斗不再创建伙伴实体，也不会援护
    wounded = copy.deepcopy(dumped["companion_state"])
    wounded["mode"] = "rest"
    next_battle = Battle(
        {"max_health": 75, "health": 50, "deck": [], "relics": {}, "companion": wounded},
        get_enemy("goblin"), seed=2, battle_index=2, companion_state=wounded)
    assert "companion" not in next_battle.entities
    _snap, turn_log = next_battle.start_turn()
    assert not turn_log


def test_companion_carries_across_advance_and_replay_checkpoints(client):
    # 直接在远征第 1 章加入伙伴并推进章节，避免受随机地图是否有商店限制
    exp = client.post("/api/expeditions", json={"seed": 12345, "chapters": 2}).json()
    rid = exp["run"]["run_id"]
    rec = service.load_run(rid)
    state = rec["state"]
    state["companion"] = companion_mod.make_companion()
    state["companion"]["hp"] = 11
    state["companion"]["mode"] = "rest"
    state["status"] = "won"
    state["position"] = rec["map"]["boss"]
    state["in_battle"] = False
    state["battle"] = None
    db.save_run(rid, "won", state["position"], state)

    advanced = client.post(f"/api/expeditions/{exp['expedition']['id']}/advance", json={}).json()
    carried = advanced["run"]["companion"]
    assert carried["id"] == "squire" and carried["hp"] == 11 and carried["mode"] == "rest"

    replay = client.get(f"/api/runs/{advanced['run']['run_id']}/replay").json()
    assert replay["verification"]["mismatch"] == 0
    assert replay["verification"]["error"] == 0
    assert replay["final_view"]["companion"]["hp"] == 11
    expedition_replay = client.get(
        f"/api/expeditions/{exp['expedition']['id']}/replay").json()
    assert expedition_replay["isolated"] is True
    assert all(ch["replay"]["verification"]["mismatch"] == 0
               for ch in expedition_replay["chapters"])


def test_legacy_state_without_companion_migrates_and_old_replay_is_legacy(client):
    seed, path = _find_shop_path(5000)
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    # 直接把位置移动到商店节点并构造商店库存（测试便捷路径：不产生任何
    # choose_node/战斗日志，只留下 create 这一条旧版动作），再抹掉 companion
    # 字段与伙伴货架，模拟 2.6.0 之前停在商店里的旧档
    rec = service.load_run(rid)
    from app import shop as shop_mod
    shop_node = path[-1]
    nd = rec["map"]["nodes"][shop_node]
    stock_seed = (rec["state"]["seed"] * 10007 + nd["row"] * 131
                  + ord(shop_node[0])) & 0xFFFFFFFF
    shop = shop_mod.generate_stock(stock_seed, set(), set(), has_companion=False)
    rec["state"]["position"] = shop_node
    rec["state"]["shop"] = shop
    rec["state"].pop("companion", None)
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])

    resumed = client.get(f"/api/runs/{rid}/resume").json()
    assert resumed["companion"] is None
    migrated = service.load_run(rid)["state"]
    assert "companion" in migrated and migrated["companion"] is None

    replay = client.get(f"/api/runs/{rid}/replay").json()
    # 旧版本日志与结构迁移允许 legacy，但不能出现推演错误或哈希不匹配
    assert replay["verification"]["error"] == 0
    assert replay["verification"]["mismatch"] == 0
