"""跨章药水背包：商店购买 / 战利品获得 / 限容量替换丢弃 / 战斗中择机使用。

覆盖：
- 背包纯逻辑（限容量 POTION_CAPACITY、背满替换、空位不可带 replace、下标校验）
- 货架与战利品确定性生成
- 商店购买药水：扣款/售罄/背满拒绝(400 零副作用)/替换丢弃；贸易委托推进
- 战斗战利品药水选项：入包；背满不选格 400 且奖励仍可领；选格替换
- 战斗中使用：五种效果逐个验证；消耗与击杀/胜负/奖励/战败解锁在同一动作原子
  结算；非自己回合/战斗外/非法格位拒绝且不消耗
- 非战斗主动丢弃；战斗中不可丢弃
- request_id 幂等：双击/重试同令牌只消耗一瓶（duplicate:true）
- 跨章携带：药水随交接快照进入下一章；战败/续局一致
- 回放：购买/替换/战斗使用全部逐位校验通过；整程远征回放携带药水且只读
- 旧档迁移：无 potions 字段的存档首次载入补空背包
"""
import copy

import pytest

from app import service, mapgen, db, potions as potions_mod
from app import rewards as rewards_mod
from app import enemies as enemies_mod


# ---------- 纯函数：背包逻辑 ----------
def test_belt_capacity_replace_and_consume():
    belt = []
    s0, d = potions_mod.add(belt, "hp")
    assert (s0, d) == (0, None)
    potions_mod.add(belt, "fire")
    potions_mod.add(belt, "block")
    assert potions_mod.is_full(belt) and len(belt) == potions_mod.POTION_CAPACITY

    # 背满不带替换格 -> 拒绝
    with pytest.raises(ValueError):
        potions_mod.add(belt, "energy")
    # 非法替换下标
    with pytest.raises(ValueError):
        potions_mod.add(belt, "energy", replace=9)
    # 替换第 1 格：新药水入位，旧药水被丢弃
    slot, discarded = potions_mod.add(belt, "energy", replace=1)
    assert slot == 1 and discarded == "fire"
    assert belt == ["hp", "energy", "block"]

    # 有空位时不允许带 replace（防误丢）
    belt2 = ["hp"]
    with pytest.raises(ValueError):
        potions_mod.add(belt2, "fire", replace=0)

    # 未知药水
    with pytest.raises(KeyError):
        potions_mod.add([], "moon_milk")

    assert potions_mod.consume(belt, 0) == "hp"
    assert belt == ["energy", "block"]


def test_shop_offers_and_loot_are_deterministic():
    a = potions_mod.generate_shop_offers(123)
    b = potions_mod.generate_shop_offers(123)
    assert a == b
    assert 1 <= len(a) <= potions_mod.POTION_OFFER_COUNT
    for it in a:
        assert it["sku"] == f"potion:{it['potion']}" and it["price"] > 0 and it["sold"] is False
    # 战利品：同种子同药水
    assert potions_mod.loot_potion_id(7) == potions_mod.loot_potion_id(7)
    assert potions_mod.loot_potion_id(7) in potions_mod.POTIONS


# ---------- 地图辅助 ----------
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


def _clear_current_battle(client, rid, run):
    """合法打完当前战斗并领取战利品（路线经过战斗节点时用：不能靠旧漏洞跳过）。"""
    while run["in_battle"]:
        rec = service.load_run(rid)
        rec["state"]["battle"]["entities"]["enemy"]["hp"] = 1
        db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
        view = client.get(f"/api/runs/{rid}/resume").json()
        strike = next((h for h in view["battle"]["hand"]
                       if (h["id"] if isinstance(h, dict) else h) == "strike"), None)
        if strike is not None:
            uid = strike["uid"] if isinstance(strike, dict) else strike
            step = client.post(f"/api/runs/{rid}/act", json={"action": "play", "card": uid})
        else:
            step = client.post(f"/api/runs/{rid}/act", json={"action": "end_turn"})
        assert step.status_code == 200
        run = step.json()["run"]
    # 战利品留待离开节点时自动放弃：不领奖可保持初始金币/药水背包，
    # 避免路径上的金币/药水奖励污染商店用例前提（规则允许未领奖直接离开）。
    return run


def _walk(client, rid, nodes):
    run = None
    for n in nodes:
        run = client.post(f"/api/runs/{rid}/act",
                          json={"action": "choose_node", "node": n}).json()["run"]
        # 路径上的战斗/奖励节点必须合法结清后才能继续推进（禁止战斗中换节点）
        run = _clear_current_battle(client, rid, run)
    return run


def _set_gold(rid, gold):
    rec = service.load_run(rid)
    rec["state"]["gold"] = gold
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])


def _set_potions(rid, pids):
    rec = service.load_run(rid)
    rec["state"]["potions"] = list(pids)
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])


def _find_encounter(client, rid):
    rec = service.load_run(rid)
    return next(n for n in rec["map"]["routes"]["start"]
                if rec["map"]["nodes"][n]["type"] == mapgen.ENCOUNTER)


# ---------- 商店：货架与购买 ----------
def test_shop_lists_potions(client):
    seed, path = _find_shop_path()
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    run = _walk(client, rid, path)
    assert len(run["shop"]["potions"]) >= 1
    item = run["shop"]["potions"][0]
    assert item["kind"] == "potion" and item["name"] and item["desc"]
    assert run["potion_capacity"] == potions_mod.POTION_CAPACITY
    assert run["potions"] == []


def test_buy_potion_deducts_marks_sold_and_enters_belt(client):
    seed, path = _find_shop_path(100)
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    run = _walk(client, rid, path)
    item = run["shop"]["potions"][0]
    _set_gold(rid, 200)
    res = client.post(f"/api/runs/{rid}/act",
                      json={"action": "shop_buy", "kind": "potion", "sku": item["sku"]})
    assert res.status_code == 200
    tx = res.json()["log"][0]["shop_tx"]
    assert tx["type"] == "buy" and tx["kind"] == "potion"
    assert tx["potion"] == item["potion"] and tx["slot"] == 0 and tx["discarded"] is None
    assert tx["gold_left"] == 200 - item["price"]
    belt = [p["id"] for p in res.json()["run"]["potions"]]
    assert belt == [item["potion"]]

    # 同货架项重复购买 -> 409 售罄，不扣款
    dup = client.post(f"/api/runs/{rid}/act",
                      json={"action": "shop_buy", "kind": "potion", "sku": item["sku"]})
    assert dup.status_code == 409
    assert client.get(f"/api/runs/{rid}").json()["gold"] == 200 - item["price"]
    assert len(client.get(f"/api/runs/{rid}").json()["potions"]) == 1


def test_buy_potion_poor_rejects_without_side_effects(client):
    seed, path = _find_shop_path(200)
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    run = _walk(client, rid, path)
    sku = run["shop"]["potions"][0]["sku"]
    poor = client.post(f"/api/runs/{rid}/act",
                       json={"action": "shop_buy", "kind": "potion", "sku": sku})
    assert poor.status_code == 400
    st = service.load_run(rid)["state"]
    assert st["gold"] == 0 and st["potions"] == []
    assert next(p for p in st["shop"]["potions"] if p["sku"] == sku)["sold"] is False


def test_buy_potion_with_full_belt_requires_replace(client):
    seed, path = _find_shop_path(300)
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    run = _walk(client, rid, path)
    sku = run["shop"]["potions"][0]["sku"]
    _set_gold(rid, 300)
    _set_potions(rid, ["hp", "block", "strength"])
    before = service.load_run(rid)["state"]
    snapshot = copy.deepcopy(before)

    # 背满不带 replace -> 400，整体零副作用（不扣款、不替换、不售罄）
    no_slot = client.post(f"/api/runs/{rid}/act",
                          json={"action": "shop_buy", "kind": "potion", "sku": sku})
    assert no_slot.status_code == 400
    mid = service.load_run(rid)["state"]
    assert mid["gold"] == 300 and [p for p in mid["potions"]] == ["hp", "block", "strength"]
    assert next(p for p in mid["shop"]["potions"] if p["sku"] == sku)["sold"] is False

    # 非法格位 -> 400
    bad_slot = client.post(f"/api/runs/{rid}/act",
                           json={"action": "shop_buy", "kind": "potion",
                                 "sku": sku, "replace": 7})
    assert bad_slot.status_code == 400
    assert service.load_run(rid)["state"]["potions"] == snapshot["potions"]

    # 指定替换第 2 格 -> 扣款 + 入位 + 旧药水丢弃 + 售罄
    ok = client.post(f"/api/runs/{rid}/act",
                     json={"action": "shop_buy", "kind": "potion",
                           "sku": sku, "replace": 2})
    assert ok.status_code == 200
    tx = ok.json()["log"][0]["shop_tx"]
    assert tx["slot"] == 2 and tx["discarded"] == "strength"
    st = service.load_run(rid)["state"]
    assert st["potions"] == ["hp", "block", tx["potion"]]
    assert next(p for p in st["shop"]["potions"] if p["sku"] == sku)["sold"] is True


def test_buy_potion_outside_shop_rejected(client):
    rid = client.post("/api/runs", json={"seed": 1}).json()["run_id"]
    r = client.post(f"/api/runs/{rid}/act",
                    json={"action": "shop_buy", "kind": "potion", "sku": "potion:hp"})
    assert r.status_code == 400


# ---------- 战利品 ----------
def _find_seed_with_potion_loot(seed_start=0):
    """找到一个「战斗胜利奖励含药水选项」的种子（约 70% 的战斗会出）。

    战斗发生在第 1 行节点（battle_index=1，与 _choose_node 的自增一致），
    奖励 RNG 种子为 run_seed + battle_index，与 service._after_battle_step 同源。
    """
    dummy_run = {"seed": 0, "battle_index": 1, "card_instances": {}}
    for seed in range(seed_start, seed_start + 400):
        m = mapgen.generate_map(seed)
        dummy_run["seed"] = seed
        for n in m["routes"][m["start"]]:
            if m["nodes"][n]["type"] != mapgen.ENCOUNTER:
                continue
            enemy = enemies_mod.get_enemy(m["nodes"][n]["enemy"])
            options = rewards_mod.battle_reward_options(seed + 1, enemy, dummy_run)
            if any(eff.get("type") == "add_potion"
                   for o in options for eff in o.get("effects", [])):
                return seed, n
    raise AssertionError("no seed with potion loot")


def _battle_rng_seed(seed, battle_index):
    return seed + battle_index


def test_battle_loot_potion_claim_adds_to_belt(client):
    seed, node = _find_seed_with_potion_loot()
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    # 直接走到遭遇并打赢
    client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": node})
    rec = service.load_run(rid)
    rec["state"]["battle"]["entities"]["enemy"]["hp"] = 1
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    view = client.get(f"/api/runs/{rid}/resume").json()
    strike = next(h for h in view["battle"]["hand"]
                  if (h["id"] if isinstance(h, dict) else h) == "strike")
    client.post(f"/api/runs/{rid}/act",
                json={"action": "play", "card": strike["uid"] if isinstance(strike, dict) else strike})
    post = client.get(f"/api/runs/{rid}/resume").json()
    idx = next(i for i, o in enumerate(post["reward_options"])
               if any(e.get("type") == "add_potion" for e in o.get("effects", [])))
    res = client.post(f"/api/runs/{rid}/act",
                      json={"action": "claim_reward", "option": idx})
    assert res.status_code == 200
    assert len(res.json()["run"]["potions"]) == 1
    # 重复领取 -> 409
    assert client.post(f"/api/runs/{rid}/act",
                       json={"action": "claim_reward", "option": idx}).status_code == 409


def test_loot_potion_full_belt_needs_replace_then_swaps(client):
    seed, node = _find_seed_with_potion_loot(400)
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": node})
    # 战前先把背包装满
    _set_potions(rid, ["hp", "fire", "block"])
    rec = service.load_run(rid)
    rec["state"]["battle"]["entities"]["enemy"]["hp"] = 1
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    view = client.get(f"/api/runs/{rid}/resume").json()
    strike = next(h for h in view["battle"]["hand"]
                  if (h["id"] if isinstance(h, dict) else h) == "strike")
    client.post(f"/api/runs/{rid}/act",
                json={"action": "play", "card": strike["uid"] if isinstance(strike, dict) else strike})
    post = client.get(f"/api/runs/{rid}/resume").json()
    idx = next(i for i, o in enumerate(post["reward_options"])
               if any(e.get("type") == "add_potion" for e in o.get("effects", [])))
    loot_pid = next(eff["potion"] for eff in post["reward_options"][idx]["effects"]
                    if eff.get("type") == "add_potion")

    # 不选格 -> 400，奖励仍可领，背包不变
    r = client.post(f"/api/runs/{rid}/act",
                    json={"action": "claim_reward", "option": idx})
    assert r.status_code == 400
    again = client.get(f"/api/runs/{rid}/resume").json()
    assert again["reward_claimed"] is False and len(again["reward_options"]) >= idx + 1
    assert [p["id"] for p in again["potions"]] == ["hp", "fire", "block"]

    # 选格替换
    ok = client.post(f"/api/runs/{rid}/act",
                     json={"action": "claim_reward", "option": idx, "replace": 0})
    assert ok.status_code == 200
    assert [p["id"] for p in ok.json()["run"]["potions"]] == [loot_pid, "fire", "block"]


# ---------- 战斗中使用 ----------
def _enter_battle_with_potions(client, rid, pids):
    _set_potions(rid, pids)
    node = _find_encounter(client, rid)
    entered = client.post(f"/api/runs/{rid}/act",
                          json={"action": "choose_node", "node": node}).json()["run"]
    assert entered["in_battle"] is True
    return entered


def test_use_hp_potion_heals_and_is_consumed(client):
    rid = client.post("/api/runs", json={"seed": 2}).json()["run_id"]
    _enter_battle_with_potions(client, rid, ["hp"])
    rec = service.load_run(rid)
    rec["state"]["health"] = 40
    rec["state"]["battle"]["entities"]["player"]["hp"] = 40
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    hp_before = client.get(f"/api/runs/{rid}/resume").json()["health"]
    assert hp_before == 40
    res = client.post(f"/api/runs/{rid}/act", json={"action": "use_potion", "slot": 0})
    assert res.status_code == 200
    run = res.json()["run"]
    assert run["health"] == min(run["max_health"], hp_before + 12)
    assert run["potions"] == []
    assert run["in_battle"] is True  # 战斗继续
    kinds = [e.get("action") for e in res.json()["log"]]
    assert "heal" in kinds and any(e.get("potion") for e in res.json()["log"])


def test_use_fire_potion_deals_damage(client):
    rid = client.post("/api/runs", json={"seed": 3}).json()["run_id"]
    _enter_battle_with_potions(client, rid, ["fire"])
    enemy_hp = client.get(f"/api/runs/{rid}/resume").json()["battle"]["enemy"]["hp"]
    res = client.post(f"/api/runs/{rid}/act", json={"action": "use_potion", "slot": 0})
    assert res.status_code == 200
    assert res.json()["run"]["battle"]["enemy"]["hp"] == enemy_hp - 10
    assert res.json()["run"]["potions"] == []


def test_use_block_energy_strength_potions(client):
    rid = client.post("/api/runs", json={"seed": 4}).json()["run_id"]
    _enter_battle_with_potions(client, rid, ["block", "energy", "strength"])
    r1 = client.post(f"/api/runs/{rid}/act", json={"action": "use_potion", "slot": 2})
    assert r1.status_code == 200
    statuses = {s["id"]: s["value"] for s in r1.json()["run"]["battle"]["player"]["statuses"]}
    assert statuses.get("strength") == 2
    # 下标随移除变化：能量药水现在 slot=1
    energy_before = r1.json()["run"]["battle"]["energy"]
    r2 = client.post(f"/api/runs/{rid}/act", json={"action": "use_potion", "slot": 1})
    assert r2.json()["run"]["battle"]["energy"] == energy_before + 2
    r3 = client.post(f"/api/runs/{rid}/act", json={"action": "use_potion", "slot": 0})
    assert r3.json()["run"]["battle"]["player"]["block"] == 12
    assert client.get(f"/api/runs/{rid}/resume").json()["potions"] == []


def test_fire_potion_kill_is_atomic_and_triggers_victory_loot(client):
    """炽焰药水收尾：消耗 + 伤害 + 死亡 + 胜利 + 战利品在同一动作里一次结算。"""
    rid = client.post("/api/runs", json={"seed": 5}).json()["run_id"]
    _enter_battle_with_potions(client, rid, ["fire", "hp"])
    rec = service.load_run(rid)
    rec["state"]["battle"]["entities"]["enemy"]["hp"] = 10
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    res = client.post(f"/api/runs/{rid}/act", json={"action": "use_potion", "slot": 0})
    assert res.status_code == 200
    body = res.json()
    results = [e.get("result") for e in body["log"] if isinstance(e, dict) and e.get("result")]
    assert "won" in results
    run = body["run"]
    assert run["in_battle"] is False and run["reward_claimed"] is False
    assert [p["id"] for p in run["potions"]] == ["hp"]  # 只用掉炽焰，另一瓶保留到战后


def test_fire_potion_kills_boss_wins_run(client):
    rid = client.post("/api/runs", json={"seed": 6}).json()["run_id"]
    rec0 = service.load_run(rid)
    row3 = next(n for n, nd in rec0["map"]["nodes"].items() if nd.get("row") == 3)
    rec0["state"]["position"] = row3
    db.save_run(rid, rec0["state"]["status"], rec0["state"]["position"], rec0["state"])
    client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": "boss"})
    _set_potions(rid, ["fire"])
    rec = service.load_run(rid)
    rec["state"]["battle"]["entities"]["enemy"]["hp"] = 10
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    res = client.post(f"/api/runs/{rid}/act", json={"action": "use_potion", "slot": 0})
    assert res.status_code == 200
    results = [e.get("result") for e in res.json()["log"]
               if isinstance(e, dict) and e.get("result")]
    assert "run_won" in results
    assert res.json()["run"]["status"] == "won"


def test_use_potion_rejected_invalid_slot_out_of_battle_and_not_consumed(client):
    rid = client.post("/api/runs", json={"seed": 7}).json()["run_id"]
    _enter_battle_with_potions(client, rid, ["hp"])
    # 非法格位
    r = client.post(f"/api/runs/{rid}/act", json={"action": "use_potion", "slot": 5})
    assert r.status_code == 400
    r = client.post(f"/api/runs/{rid}/act", json={"action": "use_potion"})
    assert r.status_code == 400
    st = service.load_run(rid)["state"]
    assert st["potions"] == ["hp"]  # 拒绝不消耗

    # 战斗外不可使用
    rec = service.load_run(rid)
    rec["state"]["in_battle"] = False
    rec["state"]["battle"] = None
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    out = client.post(f"/api/runs/{rid}/act", json={"action": "use_potion", "slot": 0})
    assert out.status_code == 400
    assert service.load_run(rid)["state"]["potions"] == ["hp"]


def test_potion_use_after_battle_already_lost_is_rejected():
    """战败终态后药水动作被 run 终态守卫拦截，不会在已结算局上再扣药水。"""
    rid = service.create_run(seed=14)["run_id"]
    rec = service.load_run(rid)
    node = next(n for n in rec["map"]["routes"]["start"]
                if rec["map"]["nodes"][n]["type"] == mapgen.ENCOUNTER)
    rec["state"]["potions"] = ["fire"]
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    service.act(rid, {"action": "choose_node", "node": node})
    # 置为战败终态：use_potion 被终态守卫拦截（与打牌/领奖同款），药水不消耗
    rec = service.load_run(rid)
    rec["state"]["status"] = "lost"
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    with pytest.raises(service.InvalidAction):
        service.act(rid, {"action": "use_potion", "slot": 0})
    with pytest.raises(service.InvalidAction):
        service.act(rid, {"action": "discard_potion", "slot": 0})
    assert service.load_run(rid)["state"]["potions"] == ["fire"]


def test_use_potion_rejected_when_not_player_turn_without_consuming():
    """非自己回合使用：直接拒绝且不消耗（纯推演层校验先于移除）。"""
    rid = service.create_run(seed=8)["run_id"]
    node = next(n for n in mapgen.generate_map(8)["routes"]["start"])
    service.act(rid, {"action": "choose_node", "node": node})
    rec = service.load_run(rid)
    rec["state"]["potions"] = ["hp"]
    rec["state"]["battle"]["in_turn"] = False
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    with pytest.raises(service.InvalidAction):
        service.act(rid, {"action": "use_potion", "slot": 0})
    assert service.load_run(rid)["state"]["potions"] == ["hp"]


# ---------- 主动丢弃 ----------
def test_discard_potion_outside_battle(client):
    rid = client.post("/api/runs", json={"seed": 9}).json()["run_id"]
    _set_potions(rid, ["hp", "fire"])
    res = client.post(f"/api/runs/{rid}/act",
                      json={"action": "discard_potion", "slot": 0})
    assert res.status_code == 200
    assert res.json()["log"][0]["potion_discarded"]["id"] == "hp"
    assert [p["id"] for p in res.json()["run"]["potions"]] == ["fire"]


def test_discard_potion_rejected_in_battle_and_bad_slot(client):
    rid = client.post("/api/runs", json={"seed": 10}).json()["run_id"]
    _enter_battle_with_potions(client, rid, ["hp"])
    assert client.post(f"/api/runs/{rid}/act",
                       json={"action": "discard_potion", "slot": 0}).status_code == 400
    # 离开战斗后非法格位
    rec = service.load_run(rid)
    rec["state"]["in_battle"] = False
    rec["state"]["battle"] = None
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    assert client.post(f"/api/runs/{rid}/act",
                       json={"action": "discard_potion", "slot": 3}).status_code == 400
    assert service.load_run(rid)["state"]["potions"] == ["hp"]


# ---------- 幂等：重试不重复生效 ----------
def test_use_potion_request_id_idempotent(client):
    rid = client.post("/api/runs", json={"seed": 11}).json()["run_id"]
    _enter_battle_with_potions(client, rid, ["hp", "fire"])
    body = {"action": "use_potion", "slot": 0, "request_id": "potion-once"}
    first = client.post(f"/api/runs/{rid}/act", json=body)
    assert first.status_code == 200 and first.json().get("duplicate") is False
    dup = client.post(f"/api/runs/{rid}/act", json=body)
    assert dup.status_code == 200 and dup.json().get("duplicate") is True
    # 只消耗一瓶：剩余 fire（同令牌返回首次响应，绝不二次结算）
    assert [p["id"] for p in client.get(f"/api/runs/{rid}").json()["potions"]] == ["fire"]
    # 首次响应内容被原样返回
    assert dup.json()["seq"] == first.json()["seq"]


def test_buy_potion_request_id_idempotent(client):
    seed, path = _find_shop_path(900)
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    run = _walk(client, rid, path)
    sku = run["shop"]["potions"][0]["sku"]
    price = run["shop"]["potions"][0]["price"]
    _set_gold(rid, 200)
    body = {"action": "shop_buy", "kind": "potion", "sku": sku, "request_id": "buy-once"}
    first = client.post(f"/api/runs/{rid}/act", json=body)
    dup = client.post(f"/api/runs/{rid}/act", json=body)
    assert first.status_code == dup.status_code == 200
    assert dup.json().get("duplicate") is True
    st = service.load_run(rid)["state"]
    assert st["gold"] == 200 - price and len(st["potions"]) == 1


# ---------- 购买药水也推进远征贸易委托 ----------
def test_buy_potion_progresses_trade_commission(client):
    """药水购买走统一商店事务，成功交易自动推进 trade 类远征委托（失败不推进）。"""
    data = client.post("/api/expeditions", json={"seed": 5, "chapters": 3}).json()
    rid = data["run"]["run_id"]
    rec = service.load_run(rid)
    # 直接挂一个贸易委托（目标 2 笔）并把角色放到带药水货架的商店
    from app import commissions as qmod
    offer = {"kind": qmod.TRADE, "target": 2, "deadline_chapter": 3,
             "reward": {"type": "gold", "amount": 10},
             "signature": "trade:2:gold:10", "offered_chapter": 1}
    rec["state"]["commissions"] = [qmod.make_commission(1, offer)]
    node = next(n for n, nd in rec["map"]["nodes"].items() if nd["type"] == mapgen.SHOP)
    rec["state"]["position"] = node
    rec["state"]["shop"] = __import__("app.shop", fromlist=["generate_stock"]).generate_stock(
        (rec["state"]["seed"] * 10007 + rec["map"]["nodes"][node]["row"] * 131
         + ord(node[0])) & 0xFFFFFFFF, set(), set(),
        expedition_ctx={"chapter": 1, "chapters_total": 3,
                        "commissions": rec["state"]["commissions"]})
    rec["state"]["gold"] = 200
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    sku = rec["state"]["shop"]["potions"][0]["sku"]
    r = client.post(f"/api/runs/{rid}/act",
                    json={"action": "shop_buy", "kind": "potion", "sku": sku})
    assert r.status_code == 200
    comm = next(c for c in r.json()["run"]["commissions"] if c["id"] == "q1")
    assert comm["progress"] == 1 and comm["status"] == "active"


# ---------- 跨章携带 ----------
def test_potions_carry_across_chapters(client):
    data = client.post("/api/expeditions", json={"seed": 42, "chapters": 2}).json()
    exp_id = data["expedition"]["id"]
    rid = data["run"]["run_id"]
    _set_potions(rid, ["hp", "fire", "energy"])

    # 走到首领并击败
    rec = service.load_run(rid)
    row3 = next(n for n, nd in rec["map"]["nodes"].items() if nd.get("row") == 3)
    rec["state"]["position"] = row3
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": "boss"})
    rec = service.load_run(rid)
    rec["state"]["battle"]["entities"]["enemy"]["hp"] = 1
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    view = client.get(f"/api/runs/{rid}/resume").json()
    strike = next(h for h in view["battle"]["hand"]
                  if (h["id"] if isinstance(h, dict) else h) == "strike")
    won = client.post(f"/api/runs/{rid}/act",
                      json={"action": "play", "card": strike["uid"] if isinstance(strike, dict) else strike})
    assert won.json()["run"]["status"] == "won"

    adv = client.post(f"/api/expeditions/{exp_id}/advance", json={}).json()
    ch2 = adv["run"]
    assert [p["id"] for p in ch2["potions"]] == ["hp", "fire", "energy"]
    assert ch2["expedition"]["chapter"] == 2
    # 交接快照视口也携带药水
    exp = client.get(f"/api/expeditions/{exp_id}").json()["expedition"]
    assert [p["id"] for p in exp["carry"]["potions"]] == ["hp", "fire", "energy"]


# ---------- 回放 ----------
def _auto_win_battle(client, rid, max_turns=120):
    """合法打赢当前战斗：能出牌就出费用最低的，否则结束回合（不绕过日志）。"""
    for _ in range(max_turns):
        v = client.get(f"/api/runs/{rid}/resume").json()
        if not v["in_battle"]:
            return
        hand = v["battle"]["hand"]
        energy = v["battle"]["energy"]
        playable = sorted((h for h in hand if h["cost"] <= energy), key=lambda h: h["cost"])
        moved = False
        if playable:
            r = client.post(f"/api/runs/{rid}/act",
                            json={"action": "play", "card": playable[0]["uid"]})
            moved = r.status_code == 200
        if not moved:
            client.post(f"/api/runs/{rid}/act", json={"action": "end_turn"})
    raise AssertionError("battle did not finish")


def _find_battle_battle_shop_battle_path(seed_start=0, shop_min_offers=1):
    """合法攒金路径：遭遇(row0) -> 遭遇(row1) -> 商店(row2) -> 遭遇(row3)。

    前两战的金币奖励之和需买得起商店最便宜的药水；商店药水货架至少
    shop_min_offers 种；战后的 row3 必有可达遭遇（供战斗中使用药水）。
    返回 (seed, [e1, e2, shop, e3], 前两战可领金币总额)。
    """
    battle_types = (mapgen.ENCOUNTER,)
    for seed in range(seed_start, seed_start + 80000):
        m = mapgen.generate_map(seed)
        dummy = {"seed": seed, "battle_index": 0, "card_instances": {}}
        for e1 in m["routes"][m["start"]]:
            if m["nodes"][e1]["type"] not in battle_types:
                continue
            for e2 in m["routes"][e1]:
                if m["nodes"][e2]["type"] not in battle_types:
                    continue
                shop = next((n for n in m["routes"][e2]
                             if m["nodes"][n]["type"] == mapgen.SHOP
                             and m["nodes"][n]["row"] == 2), None)
                if shop is None:
                    continue
                e3 = next((n for n in m["routes"][shop]
                           if m["nodes"][n]["type"] in battle_types), None)
                if e3 is None:
                    continue
                nd = m["nodes"][shop]
                stock_seed = (seed * 10007 + nd["row"] * 131 + ord(shop[0])) & 0xFFFFFFFF
                offers = potions_mod.generate_shop_offers(stock_seed)
                if len(offers) < shop_min_offers:
                    continue
                gold_total = 0
                ok = True
                for bi, en in enumerate((e1, e2), start=1):
                    dummy["battle_index"] = bi
                    enemy = enemies_mod.get_enemy(m["nodes"][en]["enemy"])
                    opts = rewards_mod.battle_reward_options(seed + bi, enemy, dummy)
                    g = next((o for o in opts if o["kind"] == "gold"), None)
                    if g is None:
                        ok = False
                        break
                    gold_total += next(e["value"] for e in g["effects"] if e["type"] == "gold")
                if not ok or gold_total < min(o["price"] for o in offers):
                    continue
                return seed, [e1, e2, shop, e3], gold_total
    raise AssertionError("no battle-battle-shop-battle path")


def _win_and_claim(client, rid, kind):
    """合法打赢当前战斗并领取指定 kind 的奖励选项；返回领取响应。"""
    _auto_win_battle(client, rid)
    v = client.get(f"/api/runs/{rid}/resume").json()
    assert v["in_battle"] is False
    idx = next(i for i, o in enumerate(v["reward_options"]) if o["kind"] == kind)
    r = client.post(f"/api/runs/{rid}/act", json={"action": "claim_reward", "option": idx})
    assert r.status_code == 200
    return r


def _find_four_potion_loot_battles(seed_start=0):
    """连续四场可达遭遇，战利品都含药水选项：前三战装满背包，第四战触发替换。"""
    for seed in range(seed_start, seed_start + 80000):
        m = mapgen.generate_map(seed)
        dummy = {"seed": seed, "battle_index": 0, "card_instances": {}}

        def has_potion(bi, en):
            dummy["battle_index"] = bi
            enemy = enemies_mod.get_enemy(m["nodes"][en]["enemy"])
            opts = rewards_mod.battle_reward_options(seed + bi, enemy, dummy)
            return any(e.get("type") == "add_potion"
                       for o in opts for e in o.get("effects", []))

        for c0 in m["routes"][m["start"]]:
            if m["nodes"][c0]["type"] != mapgen.ENCOUNTER or not has_potion(1, c0):
                continue
            for c1 in m["routes"][c0]:
                if m["nodes"][c1]["type"] != mapgen.ENCOUNTER or not has_potion(2, c1):
                    continue
                for c2 in m["routes"][c1]:
                    if m["nodes"][c2]["type"] != mapgen.ENCOUNTER or not has_potion(3, c2):
                        continue
                    c3 = next((n for n in m["routes"][c2]
                               if m["nodes"][n]["type"] == mapgen.ENCOUNTER
                               and has_potion(4, n)), None)
                    if c3:
                        return seed, [c0, c1, c2, c3]
    raise AssertionError("no four consecutive potion-loot battles")


def test_potion_actions_replay_bit_exact(client):
    # 全部药水变动都走真实动作（直接改库不进日志会破坏回放逐位一致）：
    # 合法打赢前两战领金币 -> 商店买药水（售罄项重复购买 409）->
    # 下一场战斗中使用药水 -> 战斗中丢弃被拒。每步校验点必须逐位一致。
    seed, path, gold_total = _find_battle_battle_shop_battle_path(1200)
    e1, e2, shop_node, e3 = path
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]

    client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": e1})
    _win_and_claim(client, rid, "gold")
    client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": e2})
    _win_and_claim(client, rid, "gold")
    client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": shop_node})
    shop_view = client.get(f"/api/runs/{rid}").json()["shop"]
    assert client.get(f"/api/runs/{rid}").json()["gold"] == gold_total
    offer = min(shop_view["potions"], key=lambda o: o["price"])
    bought = client.post(f"/api/runs/{rid}/act",
                         json={"action": "shop_buy", "kind": "potion", "sku": offer["sku"]})
    assert bought.status_code == 200
    bought_pid = bought.json()["log"][0]["shop_tx"]["potion"]

    # 已售罄货架项重复购买 -> 409，背包/金币不变
    belt_before = [p["id"] for p in client.get(f"/api/runs/{rid}").json()["potions"]]
    blocked = client.post(f"/api/runs/{rid}/act",
                          json={"action": "shop_buy", "kind": "potion", "sku": offer["sku"]})
    assert blocked.status_code == 409
    assert [p["id"] for p in client.get(f"/api/runs/{rid}").json()["potions"]] == belt_before

    # 战斗中使用第 0 格药水
    client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": e3})
    used = client.post(f"/api/runs/{rid}/act", json={"action": "use_potion", "slot": 0})
    assert used.status_code == 200
    # 战斗中丢弃 -> 400 且不消耗
    assert client.post(f"/api/runs/{rid}/act",
                       json={"action": "discard_potion", "slot": 0}).status_code == 400

    replay = client.get(f"/api/runs/{rid}/replay").json()
    ver = replay["verification"]
    assert ver["mismatch"] == 0 and ver["error"] == 0
    checks_by_action = {}
    for c in ver["checks"]:
        checks_by_action.setdefault(c["action"], []).append(c)
    assert all(c["status"] == "ok" for c in checks_by_action["use_potion"])
    assert all(c["status"] == "ok" for c in checks_by_action["shop_buy"])
    online = [p["id"] for p in client.get(f"/api/runs/{rid}").json()["potions"]]
    replayed = [p["id"] for p in replay["final_view"]["potions"]]
    assert online == replayed == []
    assert bought_pid in potions_mod.POTIONS


def test_loot_replace_path_replay_bit_exact(client):
    """背满替换的逐位回放：合法连打四场掉药水的战斗——前三战装满背包，
    第四战的药水奖励不选格被 400 拦截、选格替换丢弃，全部逐位校验通过。"""
    seed, nodes = _find_four_potion_loot_battles(4000)
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    for i, n in enumerate(nodes):
        client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": n})
        _auto_win_battle(client, rid)
        post = client.get(f"/api/runs/{rid}/resume").json()
        assert post["in_battle"] is False
        idx = next(i2 for i2, o in enumerate(post["reward_options"])
                   if any(e.get("type") == "add_potion" for e in o.get("effects", [])))
        if i < potions_mod.POTION_CAPACITY:
            r = client.post(f"/api/runs/{rid}/act",
                            json={"action": "claim_reward", "option": idx})
            assert r.status_code == 200
        else:
            # 背满：不选格 -> 400（零副作用，奖励仍可领）；选格 -> 替换成功
            assert client.post(f"/api/runs/{rid}/act",
                               json={"action": "claim_reward", "option": idx}).status_code == 400
            ok = client.post(f"/api/runs/{rid}/act",
                             json={"action": "claim_reward", "option": idx, "replace": 2})
            assert ok.status_code == 200
    final = client.get(f"/api/runs/{rid}").json()
    assert len(final["potions"]) == potions_mod.POTION_CAPACITY
    replay = client.get(f"/api/runs/{rid}/replay").json()
    assert replay["verification"]["mismatch"] == 0 and replay["verification"]["error"] == 0
    claim_steps = [s for s in replay["steps"] if s["action"] == "claim_reward"]
    assert len(claim_steps) == potions_mod.POTION_CAPACITY + 1
    assert all(s["check"] == "ok" for s in claim_steps)


def test_potion_replay_isolated_and_recorded_versions(client):
    rid = client.post("/api/runs", json={"seed": 12}).json()["run_id"]
    _enter_battle_with_potions(client, rid, ["fire"])
    client.post(f"/api/runs/{rid}/act", json={"action": "use_potion", "slot": 0})
    replay = client.get(f"/api/runs/{rid}/replay").json()
    assert replay["isolated"] is True
    assert service.RULES_VERSION in replay["recorded_versions"]
    step = next(s for s in replay["steps"] if s["action"] == "use_potion")
    assert step["kind"] == "battle" and "药水" in step["title"]


# ---------- 旧档迁移 ----------
def test_legacy_state_without_potions_migrates_on_load_and_works(client):
    seed, path, _ = _find_battle_battle_shop_battle_path(2000)
    e1, e2, shop_node, e3 = path
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    # 模拟 2.5.0 之前的旧档：run 状态里没有 potions 字段
    rec = service.load_run(rid)
    del rec["state"]["potions"]
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    # 续局即迁移：补空背包（结构升级在 /resume 事务内原子落库）
    view = client.get(f"/api/runs/{rid}/resume").json()
    assert view["potions"] == [] and view["potion_capacity"] == potions_mod.POTION_CAPACITY

    # 迁移后走合法动作：两战攒金 -> 商店买药水 -> 下一战战斗中使用
    client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": e1})
    _win_and_claim(client, rid, "gold")
    client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": e2})
    _win_and_claim(client, rid, "gold")
    client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": shop_node})
    shop_view = client.get(f"/api/runs/{rid}").json()["shop"]
    offer = min(shop_view["potions"], key=lambda o: o["price"])
    bought = client.post(f"/api/runs/{rid}/act",
                         json={"action": "shop_buy", "kind": "potion", "sku": offer["sku"]})
    assert bought.status_code == 200
    client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": e3})
    res = client.post(f"/api/runs/{rid}/act", json={"action": "use_potion", "slot": 0})
    assert res.status_code == 200

    # 回放：create 事件记录的是旧（无 potions）初态哈希，首步 choose_node 的
    # 在线存档在该步前被迁移，属于既有迁移机制覆盖的场景（迁移步按 legacy 呈现）；
    # 迁移完成后的购买/使用等新动作必须严格一致、全程无推演错误。
    replay = client.get(f"/api/runs/{rid}/replay").json()
    assert replay["verification"]["error"] == 0
    buy_step = next(s for s in replay["steps"] if s["action"] == "shop_buy")
    use_step = next(s for s in replay["steps"] if s["action"] == "use_potion")
    assert buy_step["check"] == "ok" and use_step["check"] == "ok"
    assert use_step["view"]["potions"] == []
    # 最终帧背包与在线状态一致
    online = [p["id"] for p in client.get(f"/api/runs/{rid}").json()["potions"]]
    assert [p["id"] for p in replay["final_view"]["potions"]] == online == []
