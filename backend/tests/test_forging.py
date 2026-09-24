"""卡牌成长树：前置条件/互斥分支、按实例保存选择与成本、贯通战斗/商店/续局/
回放/跨章继承、旧 forges 强化记录迁移、旧日志兼容重演。"""
import pytest

from app import service, mapgen, db, forging
from app.cards import get_card
from app.forging import FORGE_COST, effective_card, validate_unlock, available_nodes


# ---------- 纯函数：成长树结构 ----------
def test_tree_shape_and_costs():
    tree = {n["id"]: n for n in forging.public_tree()}
    assert len(tree) == 21  # 3 T1 + 6 T2 + 12 T3
    for nid, n in tree.items():
        tiers = [tree[r]["tier"] for r in n["requires"]]
        assert all(t == n["tier"] - 1 for t in tiers)
        assert n["cost"] == forging.TIER_COST[n["tier"]]
    # 互斥对称：A 锁 B 则 B 也锁 A
    for nid, n in tree.items():
        for m in n["mutex_with"]:
            assert nid in tree[m]["mutex_with"]


def test_effective_card_growth_chain():
    # 锋锐 -> 破甲 -> 处决：打击 6+3+2+4=15 伤，破甲附加 1 层易伤
    growth = [{"node": n, "cost": 1} for n in ("sharpen", "keen", "execute")]
    eff = effective_card(get_card("strike"), growth)
    dmg = [e for e in eff["effects"] if e["type"] == "damage"]
    vuln = [e for e in eff["effects"] if e.get("status") == "vulnerable"]
    assert dmg[0]["value"] == 15 and vuln and vuln[0]["value"] == 1
    # 不污染注册表
    assert get_card("strike")["effects"][0]["value"] == 6


def test_effective_card_id_list_still_accepted():
    # 直接传节点 id 列表（引擎历史调用形态）也能换算
    eff = effective_card(get_card("strike"), ["sharpen", "refine"])
    assert eff["effects"][0]["value"] == 9 and eff["cost"] == 0


def test_validate_unlock_prereq_and_mutex():
    empty = {"growth": []}
    assert validate_unlock(empty, "sharpen") is None
    assert "prerequisite" in validate_unlock(empty, "keen")
    once = {"growth": [{"node": "sharpen", "cost": 25}]}
    assert validate_unlock(once, "keen") is None
    assert validate_unlock(once, "sharpen") == "growth node already acquired"
    keen = {"growth": [{"node": "sharpen", "cost": 25}, {"node": "keen", "cost": 40}]}
    assert "mutex" in validate_unlock(keen, "bulwark")
    assert validate_unlock(keen, "rend") is None
    assert validate_unlock(keen, "empower") is None  # 不同 lane 不互斥
    rend = {"growth": keen["growth"] + [{"node": "rend", "cost": 60}]}
    assert "mutex" in validate_unlock(rend, "execute")
    # 精炼链：费用三次 -1
    chain = [{"node": n, "cost": c} for n, c in
             (("refine", 25), ("economize", 40), ("frugality", 60))]
    assert effective_card(get_card("heavy_blow"), chain)["cost"] == 0


def test_available_nodes_follow_tree_state():
    assert available_nodes({"growth": []}) == ["empower", "refine", "sharpen"]
    inst = {"growth": [{"node": "sharpen", "cost": 25}, {"node": "keen", "cost": 40}]}
    avail = available_nodes(inst)
    assert "rend" in avail and "execute" in avail
    assert "bulwark" not in avail and "fortress" not in avail  # 互斥整支锁定
    assert "empower" in avail


# ---------- 路径/流程辅助 ----------
def _walk(client, rid, nodes):
    run = None
    for n in nodes:
        run = client.post(f"/api/runs/{rid}/act",
                          json={"action": "choose_node", "node": n}).json()["run"]
    return run


def _find_path(pred, depth=4, seed_limit=4000, seed_start=0):
    """枚举 start 出发 depth 层路径，找到满足 pred(节点类型序列) 的 (seed, 路径)。"""
    for seed in range(seed_start, seed_start + seed_limit):
        m = mapgen.generate_map(seed)

        def search(pos, path, types):
            if pred(types):
                return seed, list(path)
            if len(path) >= depth:
                return None
            for nxt in m["routes"].get(pos, []):
                found = search(nxt, path + [nxt], types + [m["nodes"][nxt]["type"]])
                if found:
                    return found
            return None

        found = search(m["start"], [], [])
        if found:
            return found
    raise AssertionError("no path found")


def _win_current_battle(client, rid, choose_gold=True):
    """压残敌人并把手牌/能量调到必胜，打完战斗并领取金币奖励。返回领赏后的视口。

    注意：这是测试夹具直改存档，不参与回放逐位校验（只用于不断言回放的用例）。
    """
    rec = service.load_run(rid)
    b = rec["state"]["battle"]
    b["entities"]["enemy"]["hp"] = 1
    strike_uid = next(
        u for u in (b["hand"] + b["draw_pile"] + b["discard"])
        if rec["state"]["card_instances"][u]["id"] == "strike")
    b["hand"] = [strike_uid]
    b["draw_pile"] = [u for u in b["draw_pile"] if u != strike_uid]
    b["discard"] = [u for u in b["discard"] if u != strike_uid]
    b["energy"], b["max_energy"] = 3, 3
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    res = client.post(f"/api/runs/{rid}/act", json={"action": "play", "card": strike_uid})
    assert res.status_code == 200
    run = res.json()["run"]
    if choose_gold:
        idx = next(i for i, o in enumerate(run["reward_options"]) if o["kind"] == "gold")
        run = client.post(f"/api/runs/{rid}/act",
                          json={"action": "claim_reward", "option": idx}).json()["run"]
    return run


def _legit_win_battle(client, rid, max_turns=30):
    """纯 API 合法打完当前战斗：每回合出完能出的牌，再结束回合；胜利后领金币。

    所有状态变化都来自动作日志，因此回放可以逐位复现。打不赢/超时返回 None。
    """
    for _ in range(max_turns):
        view = client.get(f"/api/runs/{rid}").json()
        if not view["in_battle"]:
            break
        # 贪心：反复出第一张费用足够的手牌，直到本回合再也出不起
        while True:
            view = client.get(f"/api/runs/{rid}").json()
            if not view["in_battle"]:
                break
            playable = next((h for h in view["battle"]["hand"]
                             if h["cost"] <= view["battle"]["energy"]), None)
            if playable is None:
                break
            r = client.post(f"/api/runs/{rid}/act",
                            json={"action": "play", "card": playable["uid"]})
            if r.status_code != 200:
                break
        view = client.get(f"/api/runs/{rid}").json()
        if not view["in_battle"]:
            break
        r = client.post(f"/api/runs/{rid}/act", json={"action": "end_turn"})
        if r.status_code != 200:
            break

    view = client.get(f"/api/runs/{rid}").json()
    # 战败 / 仍在战斗（超时未分胜负）：放弃此种子
    if view["status"] != "in_progress" or view["in_battle"]:
        return None
    # 章节首领胜利会直接结束 run（无奖励选项）；普通战斗胜利后领金币
    if not view["reward_options"] or view["reward_claimed"]:
        return view
    idx = next((i for i, o in enumerate(view["reward_options"]) if o["kind"] == "gold"), None)
    if idx is None:
        return view
    claimed = client.post(f"/api/runs/{rid}/act",
                          json={"action": "claim_reward", "option": idx})
    if claimed.status_code != 200:
        return None
    return claimed.json()["run"]


def _forge_ready_pred(need_battles):
    def pred(types):
        if not types or types[-1] != mapgen.FORGE:
            return False
        prefix = types[:-1]
        battles = prefix.count("encounter") + prefix.count("elite")
        # 锻造必须排在所有战斗之后（前缀不允许再出现锻造/首领）
        return (battles >= need_battles and len(prefix) <= need_battles + 1
                and all(t not in (mapgen.FORGE, mapgen.BOSS, mapgen.SHOP) for t in prefix))
    return pred


def _find_forge_ready(need_battles=1):
    """找到一条「先打 N 场再到锻造节点」的路径；返回种子与节点序列。"""
    return _find_path(_forge_ready_pred(need_battles), depth=need_battles + 2)


def _start_run_and_reach_forge(client, need_battles=1, *, legit=False, min_gold=0,
                               tries=200):
    """建局 → 沿路径打完 need_battles 场（领金币）→ 停在待锻造节点。

    legit=True 时只用纯 API 战斗（状态全部由动作派生，回放逐位一致），并自动
    搜索能打赢且金币达 min_gold 的种子；否则用测试夹具直改存档必胜（不用于
    断言回放逐位校验的用例）。
    """
    seed_start = 0
    for _ in range(tries):
        try:
            seed, full_path = _find_path(
                _forge_ready_pred(need_battles),
                depth=need_battles + 2, seed_start=seed_start)
        except AssertionError:
            break
        seed_start = seed + 1
        rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
        gold, ok = 0, True
        for n in full_path:
            node_type = mapgen.generate_map(seed)["nodes"][n]["type"]
            _walk(client, rid, [n])
            if node_type in ("encounter", "elite"):
                r = _legit_win_battle(client, rid) if legit else _win_current_battle(client, rid)
                if r is None:
                    ok = False
                    break
                gold = r["gold"]
            if node_type == mapgen.FORGE:
                break
        if ok and (not legit or gold >= min_gold):
            return rid, gold
    raise AssertionError("no winnable forge path found")


def _forge(client, rid, uid, node, *, branch_field=False):
    body = {"action": "forge", "card": uid}
    body["branch" if branch_field else "growth_node"] = node
    return client.post(f"/api/runs/{rid}/act", json=body)


def _reopen_forge(rid):
    """直接在存档上重新开启一次锻造机会（仅限不走回放校验的用例）。"""
    st = service.load_run(rid)["state"]
    st["forge_claimed"] = False
    db.save_run(rid, st["status"], st["position"], st)


# ---------- 锻造节点：成本/防重复/视口 ----------
def test_forge_unlock_t1_charges_tier_cost_once(client):
    rid, _gold = _start_run_and_reach_forge(client, need_battles=1)
    # 非回放用例：直接设定金币，专注校验扣费/幂等
    rec = service.load_run(rid); rec["state"]["gold"] = 60
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    run = client.get(f"/api/runs/{rid}").json()
    assert run["forge_available"] is True
    assert {c["tier"]: c["cost"] for c in run["growth_tree"] if c["tier"] in (1, 2, 3)} == {1: 25, 2: 40, 3: 60}
    uid = run["deck"][0]["uid"]
    assert run["deck"][0]["growth_available"] == ["empower", "refine", "sharpen"]

    # 金币不足 -> 400
    rec = service.load_run(rid); rec["state"]["gold"] = 0
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    assert _forge(client, rid, uid, "sharpen").status_code == 400
    rec = service.load_run(rid); rec["state"]["gold"] = 60
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])

    ok = _forge(client, rid, uid, "sharpen")
    assert ok.status_code == 200
    body = ok.json()
    assert body["run"]["gold"] == 60 - FORGE_COST
    f = body["log"][0]["forged"]
    assert f["node"] == "sharpen" and f["name"] == "锋锐" and f["cost"] == FORGE_COST

    dup = _forge(client, rid, uid, "empower")
    assert dup.status_code == 409
    assert client.get(f"/api/runs/{rid}").json()["gold"] == 60 - FORGE_COST
    deck = {c["uid"]: c for c in client.get(f"/api/runs/{rid}").json()["deck"]}
    assert deck[uid]["growth_nodes"] == ["sharpen"]
    assert deck[uid]["growth_spent"] == FORGE_COST


def test_forge_rejects_bad_card_and_unknown_node(client):
    rid, _ = _start_run_and_reach_forge(client)
    rec = service.load_run(rid); rec["state"]["gold"] = 200
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    uid = service.load_run(rid)["state"]["deck"][0]
    assert _forge(client, rid, "nope", "sharpen").status_code == 400
    assert _forge(client, rid, uid, "overload_xyz").status_code == 400  # 未知节点
    # 不带节点 id -> 400
    assert client.post(f"/api/runs/{rid}/act",
                       json={"action": "forge", "card": uid}).status_code == 400


def test_branch_field_alias_still_accepted(client):
    # 旧客户端用 branch 字段传 T1 节点 id 也接受（growth_node 的别名）
    rid, gold = _start_run_and_reach_forge(client)
    uid = client.get(f"/api/runs/{rid}").json()["deck"][0]["uid"]
    r = _forge(client, rid, uid, "sharpen", branch_field=True)
    assert r.status_code == 200
    st = service.load_run(rid)["state"]
    assert [x["node"] for x in st["card_instances"][uid]["growth"]] == ["sharpen"]


# ---------- 前置条件与互斥分支（不开回放校验的用例：直接重开锻造机会） ----------
def test_forge_enforces_prerequisites(client):
    rid, _ = _start_run_and_reach_forge(client)
    rec = service.load_run(rid); rec["state"]["gold"] = 300
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    uid = service.load_run(rid)["state"]["deck"][0]
    # 直接解锁 T2/T3 -> 400 且零副作用
    assert _forge(client, rid, uid, "keen").status_code == 400
    assert _forge(client, rid, uid, "rend").status_code == 400
    st = service.load_run(rid)["state"]
    assert st["gold"] == 300 and st["card_instances"][uid]["growth"] == []
    # 前置满足后 T2 可解锁，按 T2 价格收费
    _reopen_forge(rid)
    assert _forge(client, rid, uid, "sharpen").status_code == 200
    _reopen_forge(rid)
    r = _forge(client, rid, uid, "keen")
    assert r.status_code == 200 and r.json()["run"]["gold"] == 300 - 25 - 40


def test_forge_enforces_mutex_branches(client):
    rid, _ = _start_run_and_reach_forge(client)
    rec = service.load_run(rid); rec["state"]["gold"] = 500
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    uid = service.load_run(rid)["state"]["deck"][0]
    for node in ("sharpen", "keen"):
        _reopen_forge(rid)
        assert _forge(client, rid, uid, node).status_code == 200
    # 互斥校验先于“锻造机会已消耗”：重开一次机会后 bulwark 仍必须 400
    _reopen_forge(rid)
    blocked = _forge(client, rid, uid, "bulwark")
    assert blocked.status_code == 400
    # 零副作用：没扣款、没加节点、锻造机会仍在
    st = service.load_run(rid)["state"]
    assert st["gold"] == 500 - 25 - 40
    assert [r["node"] for r in st["card_instances"][uid]["growth"]] == ["sharpen", "keen"]
    assert st["forge_claimed"] is False
    # 同节点重复解锁 -> 409
    assert _forge(client, rid, uid, "keen").status_code == 409
    # 正确的 T3 分支（rend）在前置 keen 下可解锁
    _reopen_forge(rid)
    ok = _forge(client, rid, uid, "rend")
    assert ok.status_code == 200
    assert ok.json()["run"]["gold"] == 500 - 25 - 40 - 60


# ---------- 同名卡独立成长 ----------
def test_same_name_cards_grow_independently(client):
    rid, _ = _start_run_and_reach_forge(client)
    created_run = client.get(f"/api/runs/{rid}").json()
    strikes = [c for c in created_run["deck"] if c["id"] == "strike"]
    assert len(strikes) >= 2
    rec = service.load_run(rid); rec["state"]["gold"] = 200
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    target, other = strikes[0]["uid"], strikes[1]["uid"]
    assert _forge(client, rid, target, "sharpen").status_code == 200
    deck = {c["uid"]: c for c in client.get(f"/api/runs/{rid}").json()["deck"]}
    assert deck[target]["growth_nodes"] == ["sharpen"]
    assert deck[other]["growth_nodes"] == []
    _reopen_forge(rid)
    assert _forge(client, rid, other, "refine").status_code == 200
    st = service.load_run(rid)["state"]
    assert [x["node"] for x in st["card_instances"][target]["growth"]] == ["sharpen"]
    assert [x["node"] for x in st["card_instances"][other]["growth"]] == ["refine"]


# ---------- 贯通战斗结算 ----------
def test_growth_takes_effect_in_battle(client):
    seed = 7
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    m = mapgen.generate_map(seed)
    enemy_node = next(n for n in m["routes"]["start"]
                      if m["nodes"][n]["type"] in ("encounter", "elite"))
    # 精炼 + 节流：打击费用 1-1-1=0
    rec = service.load_run(rid)
    rec["state"]["card_instances"]["c1"]["growth"] = [
        {"node": "refine", "cost": 25}, {"node": "economize", "cost": 40}]
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    _walk(client, rid, [enemy_node])
    rec = service.load_run(rid)
    b = rec["state"]["battle"]
    b["hand"] = ["c1"]
    b["draw_pile"] = [u for u in b["draw_pile"] if u != "c1"]
    b["discard"] = [u for u in b["discard"] if u != "c1"]
    b["energy"], b["max_energy"] = 3, 3
    b["entities"]["enemy"]["hp"] = 30
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    view = client.get(f"/api/runs/{rid}").json()
    hand = view["battle"]["hand"]
    assert hand[0]["uid"] == "c1" and hand[0]["cost"] == 0
    assert hand[0]["growth_nodes"] == ["refine", "economize"]
    res = client.post(f"/api/runs/{rid}/act", json={"action": "play", "card": "c1"})
    dmg = [e["value"] for e in res.json()["log"]
           if isinstance(e, dict) and e.get("action") == "damage"]
    assert dmg == [6]  # 精炼 lane 只降费，不加伤害


def test_growth_keen_adds_damage_and_vulnerable_in_battle(client):
    seed = 7
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    m = mapgen.generate_map(seed)
    enemy_node = next(n for n in m["routes"]["start"]
                      if m["nodes"][n]["type"] in ("encounter", "elite"))
    # 锋锐 + 破甲：打击 6+3+2=11 伤，附加易伤
    rec = service.load_run(rid)
    rec["state"]["card_instances"]["c1"]["growth"] = [
        {"node": "sharpen", "cost": 25}, {"node": "keen", "cost": 40}]
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    _walk(client, rid, [enemy_node])
    rec = service.load_run(rid)
    b = rec["state"]["battle"]
    b["hand"] = ["c1"]
    b["draw_pile"] = [u for u in b["draw_pile"] if u != "c1"]
    b["discard"] = [u for u in b["discard"] if u != "c1"]
    b["energy"], b["max_energy"] = 3, 3
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    log = client.post(f"/api/runs/{rid}/act",
                      json={"action": "play", "card": "c1"}).json()["log"]
    assert [e["value"] for e in log if isinstance(e, dict) and e.get("action") == "damage"] == [11]
    assert any(isinstance(e, dict) and e.get("action") == "apply_status"
               and e.get("extra", {}).get("status") == "vulnerable" for e in log)


# ---------- 贯通续局 ----------
def test_growth_persists_across_resume(client):
    rid, _ = _start_run_and_reach_forge(client)
    uid = client.get(f"/api/runs/{rid}").json()["deck"][0]["uid"]
    _forge(client, rid, uid, "empower")
    resumed = client.get(f"/api/runs/{rid}/resume").json()
    by_uid = {c["uid"]: c for c in resumed["deck"]}
    assert by_uid[uid]["growth_nodes"] == ["empower"]
    assert by_uid[uid]["growth_spent"] == FORGE_COST
    assert resumed["forge_available"] is False


# ---------- 贯通回放（用真实战斗奖励做金币来源，校验点逐位一致） ----------
def test_growth_recorded_and_replays_bit_exact(client):
    rid, gold = _start_run_and_reach_forge(client, legit=True, min_gold=FORGE_COST)
    uid = client.get(f"/api/runs/{rid}").json()["deck"][0]["uid"]
    r = _forge(client, rid, uid, "refine")
    assert r.status_code == 200
    replay = client.get(f"/api/runs/{rid}/replay").json()
    forged = [a for a in replay["actions"] if a["action"] == "forge"]
    assert len(forged) == 1
    assert forged[0]["payload"]["growth_node"] == "refine"
    assert replay["recorded_versions"] == [service.RULES_VERSION]
    assert replay["verification"]["mismatch"] == 0
    assert replay["verification"]["error"] == 0
    assert replay["verification"]["final_match"] is True
    step = next(s for s in replay["steps"] if s["action"] == "forge")
    card = next(c for c in step["view"]["deck"] if c["uid"] == uid)
    assert card["growth_nodes"] == ["refine"]
    assert "精炼" in step["title"]


# ---------- 商店移除连带成长记录 ----------
def test_shop_remove_deletes_grown_instance_flow(client):
    """两战 → 锻造 → 移除：全程纯 API（状态全由动作派生），回放逐位一致。"""
    def pred(types):
        if len(types) != 4:
            return False
        a, b, cc, d = types
        return (a in ("encounter", "elite") and b in ("encounter", "elite")
                and cc == mapgen.FORGE and d == mapgen.SHOP)

    for seed_start in range(0, 800):
        try:
            seed, path = _find_path(pred, depth=4, seed_start=seed_start)
        except AssertionError:
            pytest.skip("no battle x2 -> forge -> shop path")
        rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
        ok = True
        for idx, n in enumerate(path):
            nt = mapgen.generate_map(seed)["nodes"][n]["type"]
            _walk(client, rid, [n])
            if nt in ("encounter", "elite"):
                if _legit_win_battle(client, rid) is None:
                    ok = False
                    break
            elif nt == mapgen.FORGE:
                view = client.get(f"/api/runs/{rid}").json()
                if view["gold"] < FORGE_COST:
                    ok = False
                    break
                uid = view["deck"][0]["uid"]
                if _forge(client, rid, uid, "sharpen").status_code != 200:
                    ok = False
                    break
        if not ok:
            continue
        run = client.get(f"/api/runs/{rid}").json()
        if run.get("shop") is None or run["gold"] < 35:
            continue
        uid = run["deck"][0]["uid"]  # 与锻造时选中的第一张一致（顺序未变）
        grown = next(c for c in run["deck"] if c["uid"] == uid)
        assert grown["growth_nodes"] == ["sharpen"]
        r = client.post(f"/api/runs/{rid}/act",
                        json={"action": "shop_remove", "card": uid})
        assert r.status_code == 200
        st = service.load_run(rid)["state"]
        assert uid not in st["card_instances"] and uid not in st["deck"]
        replay = client.get(f"/api/runs/{rid}/replay").json()
        assert replay["verification"]["mismatch"] == 0
        assert replay["verification"]["error"] == 0
        assert uid not in [c["uid"] for c in replay["final_view"]["deck"]
                           if isinstance(c, dict)]
        return
    pytest.skip("no winnable battle x2 -> forge -> shop path with enough gold")


# ---------- 跨章继承 ----------
def test_growth_carried_into_next_chapter(client):
    from tests.test_expedition import _create, _win_chapter
    data = _create(client, seed=5, chapters=3)
    exp_id = data["expedition"]["id"]
    rid1 = data["run"]["run_id"]
    uid = data["run"]["deck"][0]["uid"]
    rec = service.load_run(rid1)
    rec["state"]["card_instances"][uid]["growth"] = [
        {"node": "sharpen", "cost": 25}, {"node": "keen", "cost": 40},
        {"node": "rend", "cost": 60}]
    db.save_run(rid1, rec["state"]["status"], rec["state"]["position"], rec["state"])
    _win_chapter(client, rid1)
    adv = client.post(f"/api/expeditions/{exp_id}/advance", json={}).json()
    carried = next(c for c in adv["run"]["deck"] if c["uid"] == uid)
    assert carried["growth_nodes"] == ["sharpen", "keen", "rend"]
    assert carried["growth_spent"] == 125
    inst = service.load_run(adv["run"]["run_id"])["state"]["card_instances"][uid]
    eff = effective_card(get_card(inst["id"]), inst["growth"])
    assert eff["effects"][0]["value"] == 11  # 6+3 锋锐 +2 破甲
    assert "mutex" in validate_unlock(inst, "bulwark")  # 互斥随实例带到新章
    # 整程回放：成长在章节 create 携带的 carry 中逐位重建
    rep = client.get(f"/api/expeditions/{exp_id}/replay").json()
    ch1_carry = rep["events"][1]["payload"]["carry"]
    assert [r["node"] for r in ch1_carry["card_instances"][uid]["growth"]] == \
        ["sharpen", "keen", "rend"]
    assert rep["chapters"][0]["replay"]["verification"]["mismatch"] == 0


# ---------- 旧 forges 强化记录迁移 ----------
def test_legacy_forges_records_migrate_to_growth(client):
    rid = client.post("/api/runs", json={"seed": 3}).json()["run_id"]
    rec = db.load_run(rid)
    st = rec["state"]
    st["card_instances"]["c1"]["forges"] = ["sharpen", "sharpen"]
    del st["card_instances"]["c1"]["growth"]
    st["card_instances"]["c2"]["forges"] = ["refine"]
    del st["card_instances"]["c2"]["growth"]
    db.save_run(rid, st["status"], st["position"], st)

    resumed = client.get(f"/api/runs/{rid}/resume").json()
    by_uid = {c["uid"]: c for c in resumed["deck"]}
    assert by_uid["c1"]["growth_nodes"] == ["sharpen", "keen"]  # 默认链展开
    assert by_uid["c1"]["growth_spent"] == 50                   # 两笔各 25
    assert by_uid["c2"]["growth_nodes"] == ["refine"]
    st2 = service.load_run(rid)["state"]
    assert "forges" not in st2["card_instances"]["c1"]
    # 迁移后的成长在战斗中生效（11 伤）
    eff = effective_card(get_card("strike"), st2["card_instances"]["c1"]["growth"])
    assert eff["effects"][0]["value"] == 11


def test_legacy_mid_battle_save_migrates_and_plays(client):
    seed = 7
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    m = mapgen.generate_map(seed)
    enemy_node = next(n for n in m["routes"]["start"]
                      if m["nodes"][n]["type"] in ("encounter", "elite"))
    _walk(client, rid, [enemy_node])
    rec = db.load_run(rid)
    st = rec["state"]
    # 构造 2.2.x 旧档（实例表已存在、三堆为 uid），但成长字段回退为旧 forges：
    # run 级与战斗内快照实例表都写 forges 并删除 growth。
    st["rules_version"] = "2.2.0"
    for table in (st["card_instances"], st["battle"]["card_instances"]):
        for inst in table.values():
            inst.pop("growth", None)
        table["c1"]["forges"] = ["sharpen"]
    db.save_run(rid, st["status"], st["position"], st)

    resumed = client.get(f"/api/runs/{rid}/resume").json()
    st2 = service.load_run(rid)["state"]
    piles = st2["battle"]["draw_pile"] + st2["battle"]["hand"] + st2["battle"]["discard"]
    assert sorted(piles) == sorted(st2["deck"])
    # run 级与战斗内实例表都把 c1 的旧 forges 迁移成 growth
    assert [r["node"] for r in st2["card_instances"]["c1"]["growth"]] == ["sharpen"]
    assert [r["node"]
            for r in st2["battle"]["card_instances"]["c1"]["growth"]] == ["sharpen"]
    # 迁移后战斗可正常打出
    uid = resumed["battle"]["hand"][0]["uid"]
    play = client.post(f"/api/runs/{rid}/act", json={"action": "play", "card": uid})
    assert play.status_code == 200


def test_legacy_bare_id_mid_battle_save_migrates(client):
    """更老的裸 id 战斗旧档（无实例表）迁移为实例结构，三堆映射保持不变。"""
    seed = 7
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    m = mapgen.generate_map(seed)
    enemy_node = next(n for n in m["routes"]["start"]
                      if m["nodes"][n]["type"] in ("encounter", "elite"))
    _walk(client, rid, [enemy_node])
    rec = db.load_run(rid)
    st = rec["state"]
    for pile in ("draw_pile", "hand", "discard"):
        st["battle"][pile] = [st["card_instances"][u]["id"] for u in st["battle"][pile]]
    st["battle"].pop("card_instances", None)
    st["deck"] = [inst["id"] for inst in st["card_instances"].values()]
    del st["card_instances"]
    st.pop("next_card_seq", None)
    st.pop("forge_claimed", None)
    db.save_run(rid, st["status"], st["position"], st)

    resumed = client.get(f"/api/runs/{rid}/resume").json()
    assert all(isinstance(c, dict) and c["uid"] for c in resumed["deck"])
    assert all(isinstance(h, dict) and h["uid"] for h in resumed["battle"]["hand"])
    st2 = service.load_run(rid)["state"]
    piles = st2["battle"]["draw_pile"] + st2["battle"]["hand"] + st2["battle"]["discard"]
    assert sorted(piles) == sorted(st2["deck"])
    assert all(u in st2["card_instances"] for u in piles)
    uid = resumed["battle"]["hand"][0]["uid"]
    play = client.post(f"/api/runs/{rid}/act", json={"action": "play", "card": uid})
    assert play.status_code == 200


def test_legacy_save_outside_battle_migrates(client):
    rid = client.post("/api/runs", json={"seed": 1}).json()["run_id"]
    rec = db.load_run(rid)
    st = rec["state"]
    st["deck"] = [inst["id"] for inst in st["card_instances"].values()]
    del st["card_instances"]
    st.pop("next_card_seq", None)
    db.save_run(rid, st["status"], st["position"], st)
    resumed = client.get(f"/api/runs/{rid}/resume").json()
    assert len(resumed["deck"]) == 7
    assert len({c["uid"] for c in resumed["deck"]}) == 7
    assert all(c["growth_nodes"] == [] and c["growth_spent"] == 0 for c in resumed["deck"])
    assert resumed["forge_claimed"] is True


def test_legacy_carry_with_forges_normalizes_into_next_chapter():
    """2.2.x 章节交接快照里实例仍是 forges 结构时，开新章自动归一化为 growth。"""
    inst = {"id": "strike", "forges": ["empower", "empower"]}
    carry = {
        "deck": ["c1"], "card_instances": {"c1": inst}, "next_card_seq": 2,
        "relics": {}, "gold": 10, "max_health": 75, "health": 75,
        "base_energy": 3, "commissions": [], "next_commission_seq": 1,
        "chapter": 1, "chapters_total": 2,
    }
    state = service._new_run_state(123, carry=carry, chapter=2, chapters_total=2,
                                   expedition_id="exp")
    c1 = state["card_instances"]["c1"]
    assert "forges" not in c1
    assert [r["node"] for r in c1["growth"]] == ["empower", "amplify"]
    assert [r["cost"] for r in c1["growth"]] == [FORGE_COST, FORGE_COST]
    # 归一化不污染传入的 carry
    assert "forges" in inst


# ---------- 旧版 forge 日志兼容重演 ----------
def test_legacy_forge_log_replays_via_default_chain(client):
    """旧形态 forge 事件（无 ver、branch=sharpen）在回放中按默认链兼容重演。

    通过直接追加一条旧版 battle_events 行 + 把在线存档同步为“该动作后的状态”，
    验证回放独立从初始状态推演时能正确展开节点、金币轨迹一致且无 error。
    """
    rid, gold_before = _start_run_and_reach_forge(client, legit=True, min_gold=FORGE_COST)
    uid = client.get(f"/api/runs/{rid}").json()["deck"][0]["uid"]
    conn = db.get_conn()
    try:
        seq = db.next_seq_conn(conn, rid)
        conn.execute(
            "UPDATE runs SET state_json=json_set(state_json, '$.gold', ?, "
            "'$.forge_claimed', 1) WHERE id=?",
            (gold_before - FORGE_COST, rid))
        conn.execute(
            "INSERT INTO battle_events (run_id, seq, action, payload_json) VALUES (?,?,?,?)",
            (rid, seq, "forge",
             '{"card": "%s", "branch": "sharpen"}' % uid))
        conn.commit()
    finally:
        conn.close()
    # 在线存档实例同步为“旧动作后”：按迁移形态（sharpen）
    st = service.load_run(rid)["state"]
    st["card_instances"][uid]["growth"] = [{"node": "sharpen", "cost": FORGE_COST}]
    db.save_run(rid, st["status"], st["position"], st)

    rep = client.get(f"/api/runs/{rid}/replay").json()
    step = next(s for s in rep["steps"] if s["action"] == "forge")
    assert step["legacy"] is True and step["check"] == "legacy"
    card = next(c for c in step["view"]["deck"] if c["uid"] == uid)
    assert card["growth_nodes"] == ["sharpen"]
    assert step["view"]["gold"] == gold_before - FORGE_COST
    assert rep["verification"]["error"] == 0


def test_legacy_version_forge_event_uses_default_chain(client):
    """带旧版本号 2.2.0（非无 ver）的 forge 事件也必须按旧规则兼容重演。"""
    rid, gold_before = _start_run_and_reach_forge(client, legit=True, min_gold=FORGE_COST)
    uid = client.get(f"/api/runs/{rid}").json()["deck"][0]["uid"]
    conn = db.get_conn()
    try:
        seq = db.next_seq_conn(conn, rid)
        conn.execute(
            "UPDATE runs SET state_json=json_set(state_json, '$.gold', ?, "
            "'$.forge_claimed', 1) WHERE id=?",
            (gold_before - FORGE_COST, rid))
        conn.execute(
            "INSERT INTO battle_events (run_id, seq, action, payload_json) VALUES (?,?,?,?)",
            (rid, seq, "forge",
             '{"card": "%s", "branch": "refine", "ver": "2.2.0"}' % uid))
        conn.commit()
    finally:
        conn.close()
    st = service.load_run(rid)["state"]
    st["card_instances"][uid]["growth"] = [{"node": "refine", "cost": FORGE_COST}]
    db.save_run(rid, st["status"], st["position"], st)

    rep = client.get(f"/api/runs/{rid}/replay").json()
    # 带 2.2.0 版本号：不算 legacy 帧，但仍走兼容重演（refine 是合法 T1 节点），无 error
    assert rep["verification"]["error"] == 0
    step = next(s for s in rep["steps"] if s["action"] == "forge")
    card = next(c for c in step["view"]["deck"] if c["uid"] == uid)
    assert card["growth_nodes"] == ["refine"]


def test_legacy_forge_action_unit():
    """直接用旧规则推演函数验证「同分支重复」在兼容路径下沿默认链展开（含扣款）。"""
    rid_state = None
    run = {
        "forge_claimed": False, "gold": 100, "position": "1-1",
        "card_instances": {"c1": {"id": "strike", "growth": []}}, "events_log": [],
    }
    # 第一次 sharpen -> sharpen
    service._forge(run, "c1", "sharpen", legacy=True)
    assert [r["node"] for r in run["card_instances"]["c1"]["growth"]] == ["sharpen"]
    assert run["gold"] == 75 and run["forge_claimed"] is True
    # 再来一次（模拟第二个锻造节点的旧日志）：sharpen -> keen
    run["forge_claimed"] = False
    service._forge(run, "c1", "sharpen", legacy=True)
    assert [r["node"] for r in run["card_instances"]["c1"]["growth"]] == ["sharpen", "keen"]
    assert run["gold"] == 50
    # 第三次 -> rend
    run["forge_claimed"] = False
    service._forge(run, "c1", "sharpen", legacy=True)
    assert [r["node"] for r in run["card_instances"]["c1"]["growth"]] == ["sharpen", "keen", "rend"]
    # 链满后的第四次付款：仍扣款但不再产生节点（忠实复刻损坏/极端旧档的金币轨迹）
    run["forge_claimed"] = False
    run["gold"] = 25
    log = service._forge(run, "c1", "sharpen", legacy=True)
    assert [r["node"] for r in run["card_instances"]["c1"]["growth"]] == ["sharpen", "keen", "rend"]
    assert run["gold"] == 0
    assert log[0]["forged"]["node"] is None
    # 未知旧分支 -> InvalidAction
    run["forge_claimed"] = False
    with pytest.raises(service.InvalidAction):
        service._forge(run, "c1", "no_such_branch", legacy=True)


def test_legacy_repeated_branch_unrolls_chain():
    """旧版同一分支重复锻造（三次 sharpen）重演时沿 sharpen->keen->rend 展开。"""
    records, changed = forging.migrate_forges_to_growth(["sharpen", "sharpen", "sharpen"])
    assert [r["node"] for r in records] == ["sharpen", "keen", "rend"]
    assert all(r["cost"] == FORGE_COST for r in records) and changed
    eff = effective_card(get_card("strike"), records)
    # 6 +3 锋锐 +2 破甲（裂甲只加易伤层数）= 11
    assert eff["effects"][0]["value"] == 11
    vuln = [e for e in eff["effects"] if e.get("status") == "vulnerable"]
    assert vuln and vuln[0]["value"] == 3  # 破甲 1 + 裂甲 2


# ---------- 奖励入牌：新实例独立成长 ----------
def test_battle_reward_add_card_creates_independent_instance(client):
    seed = 7
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    m = mapgen.generate_map(seed)
    enemy_node = next(n for n in m["routes"]["start"]
                      if m["nodes"][n]["type"] in ("encounter", "elite"))
    _walk(client, rid, [enemy_node])
    rec = service.load_run(rid)
    enemy_id = rec["state"]["battle"]["enemy"]
    b = rec["state"]["battle"]
    b["entities"]["enemy"]["hp"] = 1
    b["hand"] = ["c1"]
    b["energy"] = 3
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    res = client.post(f"/api/runs/{rid}/act", json={"action": "play", "card": "c1"})
    assert res.json()["run"]["status"] == "in_progress"
    opts = res.json()["run"]["reward_options"]
    card_idx = next(i for i, o in enumerate(opts) if o["kind"] == "card")
    expected_cid = next(e["card"] for e in opts[card_idx]["effects"] if e["type"] == "add_card")
    claimed = client.post(f"/api/runs/{rid}/act",
                          json={"action": "claim_reward", "option": card_idx})
    assert claimed.status_code == 200
    state = service.load_run(rid)["state"]
    new_uid = state["deck"][-1]
    assert state["card_instances"][new_uid] == {"id": expected_cid, "growth": []}
    from app.enemies import get_enemy
    assert expected_cid in get_enemy(enemy_id)["reward_cards"]
    assert state["next_card_seq"] == len(state["card_instances"]) + 1
    same_name = [u for u, i in state["card_instances"].items() if i["id"] == expected_cid]
    assert new_uid in same_name and all(state["card_instances"][u]["growth"] == [] for u in same_name)
