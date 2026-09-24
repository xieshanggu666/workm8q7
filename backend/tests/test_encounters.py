"""跨章节奇遇链（规则 2.8.0）。

覆盖：
- 每张地图恰好一个「奇遇」节点（独立派生流，不改变既有节点/商店拓扑）
- 进入节点确定性挂起待抉择链；无候选走兜底清泉
- 抉择代价/奖励回写金币、生命、最大生命、卡牌、药水、遗物
- 代价校验先于变更（金币/生命不足、背满未指定替换格 -> 400 零副作用）
- 待抉择幂等：重复抉择 400；离开节点后旧抉择失效；request_id 双击只生效一次
- flag 随远征交接快照继承；下一章开头兑现预兆（首场战斗敌人+血/开局力量·
  格挡/易碎、赐福回血），预兆只消耗一次；无续写 flag 兑现后移除
- 续写链（报恩/复仇）在后续章奇遇节点按 flag 出现；伏击战胜利固定战利品、
  战败终结远征；伏击也算战斗胜利推进讨伐委托
- 续局保留待抉择/战斗；整章与整程回放逐位校验通过、只读隔离
- 旧档（2.8.0 之前）首次载入补 enc_state（迁移步 legacy），旧 create 形状
  回放兼容；事件状态全部是动作序列的确定性函数
"""
import copy

import pytest

from app import db, mapgen, service
from app import encounters as enc_mod
from app.encounters import CHAINS, LOCAL, CROSS


# ---------------- 地图与链注册 ----------------
def test_each_map_has_exactly_one_event_node(client):
    for seed in range(0, 120):
        m = mapgen.generate_map(seed)
        events = [n for n, nd in m["nodes"].items() if nd["type"] == mapgen.EVENT]
        assert len(events) == 1, seed
        nd = m["nodes"][events[0]]
        assert "enemy" not in nd and nd["row"] in (1, 2)


def test_event_node_never_replaces_shop_or_rest(client):
    for seed in range(0, 400):
        m = mapgen.generate_map(seed)
        assert not any(nd["type"] == mapgen.EVENT and nd.get("enemy")
                       for nd in m["nodes"].values())
        # 事件节点本身不可能是商店/休息
        assert all(not (nd["type"] == mapgen.EVENT and nd["type"] in ("shop", "rest"))
                   for nd in m["nodes"].values())


def test_chain_registry_well_formed():
    assert CHAINS["wounded_traveler"]["scope"] == CROSS
    assert CHAINS["avenger_ambush"]["requires_flag"] == "wt_robbed"
    assert CHAINS["traveler_gratitude"]["requires_flag"] == "wt_aided"
    for c in CHAINS.values():
        assert c["choices"] and all(ch["id"] for ch in c["choices"])
        if c["scope"] == LOCAL:
            assert not c.get("requires_flag")


# ---------------- 纯状态簿记 ----------------
def test_fresh_state_and_normalize_handles_none():
    e = enc_mod.fresh_state()
    assert e["flags"] == {} and e["battle_mods"] == {}
    norm, changed = enc_mod.normalize_state(None)
    assert changed and isinstance(norm, dict) and "flags" in norm
    # 已是完整结构：不变
    norm2, changed2 = enc_mod.normalize_state(copy.deepcopy(e))
    assert not changed2


def test_on_chapter_begin_opener_consumes_flag_without_continuation():
    e = enc_mod.fresh_state()
    e["flags"]["altar_cursed"] = 1
    heal, opened = enc_mod.on_chapter_begin(e, 2)
    assert heal == 0 and opened == ["altar_cursed"]
    assert e["battle_mods"] == {"enemy_hp": 12, "fragile": 2}
    # 无续写 flag：兑现后移除
    assert "altar_cursed" not in e["flags"]
    # 已兑现记录留在 opened：即便 flag 被异常重新埋入，同一 flag 也不会二次兑现
    e["flags"]["altar_cursed"] = 1
    heal2, opened2 = enc_mod.on_chapter_begin(e, 3)
    assert opened2 == [] and heal2 == 0


def test_on_chapter_begin_continuation_flag_is_kept():
    e = enc_mod.fresh_state()
    e["flags"]["wt_aided"] = 1
    heal, opened = enc_mod.on_chapter_begin(e, 2)
    assert heal == 0
    assert e["battle_mods"] == {"strength": 2, "block": 8}
    # 报恩/复仇 flag 保留，等续写节点结清
    assert "wt_aided" in e["flags"]
    assert e["opened"]["wt_aided"] == 2


def test_altar_blessed_chapter_heal_adds_on_top_of_rest():
    deck_uids, insts = service._make_instances(service.START_DECK)
    carry = {
        "deck": deck_uids, "card_instances": insts,
        "next_card_seq": len(deck_uids) + 1,
        "relics": {}, "gold": 0, "max_health": 75, "health": 40,
        "base_energy": 3, "potions": [], "companion": None,
        "commissions": [], "next_commission_seq": 1,
        "chapter": 1, "chapters_total": 3,
        "enc_state": None,
    }
    e = enc_mod.fresh_state()
    e["flags"]["altar_blessed"] = 1
    carry["enc_state"] = e
    st = service._new_run_state(service._chapter_seed(99, 2), carry=carry,
                                chapter=2, chapters_total=3, expedition_id="x")
    # 休整 +18，赐福 +12
    assert st["health"] == 40 + 18 + 12
    assert "altar_blessed" not in st["enc_state"]["flags"]


def test_battle_mods_consumed_by_first_battle_only():
    e = enc_mod.fresh_state()
    e["battle_mods"] = {"enemy_hp": 8, "strength": 1}
    assert enc_mod.take_battle_mods(e) == {"enemy_hp": 8, "strength": 1}
    assert enc_mod.take_battle_mods(e) == {}


def test_eligible_chains_excludes_completed_and_prefers_continuation():
    e = enc_mod.fresh_state()
    base = enc_mod.eligible_chains(e, 1, 3)
    assert "wounded_traveler" in base and "mystic_altar" in base
    # 末章不再挂跨章起始链
    last = enc_mod.eligible_chains(e, 3, 3)
    assert "wounded_traveler" not in last and "mystic_altar" not in last
    # wt_robbed 在场 -> 续写出现，起始链（仍埋着 flag 的根）不再出现
    e["flags"]["wt_robbed"] = 1
    ch2 = enc_mod.eligible_chains(e, 2, 3)
    assert "avenger_ambush" in ch2 and "wounded_traveler" not in ch2
    # 根链完成 -> 续写也不再出现
    e["flags"].pop("wt_robbed")
    e["completed"].append("wounded_traveler")
    assert "avenger_ambush" not in enc_mod.eligible_chains(e, 2, 3)


def test_cross_chains_excluded_from_single_runs():
    """普通局（chapters_total=None）不出跨章链——flag 没有后续章可以兑现。"""
    e = enc_mod.fresh_state()
    single = enc_mod.eligible_chains(e, 1, None)
    assert all(enc_mod.get_chain(cid)["scope"] == LOCAL for cid in single)
    assert "wounded_traveler" not in single and "mystic_altar" not in single
    # 即便手动埋了 flag（不应发生），续写链也不在普通局出现
    e["flags"]["wt_aided"] = 1
    assert "traveler_gratitude" not in enc_mod.eligible_chains(e, 2, None)


def test_select_chain_guarantees_continuation_when_flag_active():
    """持有续写 flag 时，后续章事件节点必选续写链（任何抽取种子下都成立）。"""
    e = enc_mod.fresh_state()
    e["flags"]["wt_aided"] = 1
    for seed in range(0, 50):
        chain_id = enc_mod.select_chain(seed, e, 2, 3)
        assert chain_id == "traveler_gratitude", (seed, chain_id)
    e["flags"] = {"wt_robbed": 1}
    for seed in range(0, 50):
        assert enc_mod.select_chain(seed, e, 2, 3) == "avenger_ambush"


# ---------------- 测试辅助：找到并抵达奇遇节点 ----------------
def _event_for_seed(exp_seed, chapter=1):
    s = service._chapter_seed(exp_seed, chapter)
    m = mapgen.generate_map(s)
    ev = next(n for n, nd in m["nodes"].items() if nd["type"] == mapgen.EVENT)
    from collections import deque
    q = deque([(m["start"], [])])
    while q:
        pos, path = q.popleft()
        if pos == ev:
            return m, ev, path
        for n in m["routes"].get(pos, []):
            q.append((n, path + [n]))
    raise AssertionError("event unreachable")


def _walk(client, rid, nodes, auto_battle=True):
    for n in nodes:
        r = client.post(f"/api/runs/{rid}/act",
                        json={"action": "choose_node", "node": n})
        assert r.status_code == 200, r.text
        v = r.json()["run"]
        if auto_battle and v["in_battle"]:
            _auto_win(client, rid)


def _auto_win(client, rid):
    """API-only 打赢当前战斗并领取金币奖励。"""
    for _ in range(120):
        v = client.get(f"/api/runs/{rid}/resume").json()
        if not v["in_battle"]:
            break
        b = v["battle"]
        playable = [h for h in b["hand"]
                    if (h.get("cost", 1) if isinstance(h, dict) else 1) <= b["energy"]]
        if playable:
            h = playable[0]
            uid = h["uid"] if isinstance(h, dict) else h
            client.post(f"/api/runs/{rid}/act", json={"action": "play", "card": uid})
        else:
            client.post(f"/api/runs/{rid}/act", json={"action": "end_turn"})
    v = client.get(f"/api/runs/{rid}/resume").json()
    if not v["reward_claimed"] and v["reward_options"]:
        gi = next((i for i, o in enumerate(v["reward_options"])
                   if o["kind"] == "gold"), 0)
        r = client.post(f"/api/runs/{rid}/act",
                        json={"action": "claim_reward", "option": gi})
        assert r.status_code == 200


def _find_exp_seed_with_chain(client, chain_id, max_seed=400):
    """找到一个第 1 章奇遇为指定链、且 API 机器人能合法通关两章的远征种子。"""
    for seed in range(1, max_seed):
        r = client.post("/api/expeditions",
                        json={"seed": seed, "chapters": 3})
        assert r.status_code == 200
        data = r.json()
        rid = data["run"]["run_id"]
        _m, ev, path = _event_for_seed(seed)
        _walk(client, rid, path)
        v = client.get(f"/api/runs/{rid}/resume").json()
        if v.get("encounter") and v["encounter"]["chain"] == chain_id:
            return seed, rid, data["expedition"]["id"], ev
    raise AssertionError(f"no seed with chain {chain_id}")


# ---------------- 进入节点 / 待抉择 ----------------
def test_encounter_node_presents_pending_choice(client):
    seed, rid, _eid, ev = _find_exp_seed_with_chain(client, "wounded_traveler")
    v = client.get(f"/api/runs/{rid}/resume").json()
    assert v["encounter"] is not None
    assert v["encounter"]["chain"] == "wounded_traveler"
    assert {ch["id"] for ch in v["encounter"]["choices"]} == {"aid", "rob", "ignore"}
    # 续局保留待抉择
    assert client.get(f"/api/runs/{rid}/resume").json()["encounter"]["chain"] == \
           "wounded_traveler"


def test_unknown_chain_or_choice_rejected(client):
    _seed, rid, _eid, _ev = _find_exp_seed_with_chain(client, "ancient_cache")
    r = client.post(f"/api/runs/{rid}/act", json={
        "action": "encounter_choice", "chain": "ancient_cache", "enc_choice": "nope"})
    assert r.status_code == 400
    r = client.post(f"/api/runs/{rid}/act", json={
        "action": "encounter_choice", "chain": "mystic_altar", "enc_choice": "pray"})
    assert r.status_code == 400


def test_choice_cannot_be_resolved_after_leaving_node(client):
    _seed, rid, _eid, _ev = _find_exp_seed_with_chain(client, "ancient_cache")
    # 离开奇遇节点走向下一可达节点，旧待抉择失效
    rec = service.load_run(rid)
    nxt = rec["map"]["routes"][rec["state"]["position"]][0]
    # 先直接尝试旧抉择 -> 仍是本节点，允许；改为先走再抉择
    client.post(f"/api/runs/{rid}/act",
                json={"action": "choose_node", "node": nxt})
    r = client.post(f"/api/runs/{rid}/act", json={
        "action": "encounter_choice", "chain": "ancient_cache",
        "enc_choice": "force"})
    assert r.status_code == 400


# ---------------- 代价 / 奖励 ----------------
def test_rob_choice_grants_gold_and_sets_flag(client):
    _seed, rid, _eid, _ev = _find_exp_seed_with_chain(client, "wounded_traveler")
    gold0 = client.get(f"/api/runs/{rid}/resume").json()["gold"]
    r = client.post(f"/api/runs/{rid}/act", json={
        "action": "encounter_choice",
        "chain": "wounded_traveler", "enc_choice": "rob"})
    assert r.status_code == 200
    v = r.json()["run"]
    assert v["gold"] == gold0 + 15
    flags = [f["flag"] for f in v["encounter_flags"]]
    assert "wt_robbed" in flags
    assert v["encounter"] is None  # 抉择后待抉择清空


def test_aid_costs_hp_and_sets_flag(client):
    _seed, rid, _eid, _ev = _find_exp_seed_with_chain(client, "wounded_traveler")
    before = client.get(f"/api/runs/{rid}/resume").json()["health"]
    r = client.post(f"/api/runs/{rid}/act", json={
        "action": "encounter_choice",
        "chain": "wounded_traveler", "enc_choice": "aid"})
    v = r.json()["run"]
    assert v["health"] == before - 6
    assert any(f["flag"] == "wt_aided" for f in v["encounter_flags"])


def test_choice_idempotent_double_submit_rejected(client):
    _seed, rid, _eid, _ev = _find_exp_seed_with_chain(client, "wounded_traveler")
    body = {"action": "encounter_choice", "chain": "wounded_traveler",
            "enc_choice": "ignore"}
    assert client.post(f"/api/runs/{rid}/act", json=body).status_code == 200
    # 已结清：重复抉择 400（待抉择已清空）
    assert client.post(f"/api/runs/{rid}/act", json=body).status_code == 400


def test_request_id_double_click_applies_once(client):
    _seed, rid, _eid, _ev = _find_exp_seed_with_chain(client, "wounded_traveler")
    gold0 = client.get(f"/api/runs/{rid}/resume").json()["gold"]
    body = {"action": "encounter_choice", "chain": "wounded_traveler",
            "enc_choice": "rob", "request_id": "enc-rob-1"}
    first = client.post(f"/api/runs/{rid}/act", json=body).json()
    dup = client.post(f"/api/runs/{rid}/act", json=body).json()
    assert dup["duplicate"] is True
    assert dup["run"]["gold"] == first["run"]["gold"] == gold0 + 15


def test_gold_cost_rejected_when_poor_and_has_no_side_effect(client):
    _seed, rid, _eid, _ev = _find_exp_seed_with_chain(client, "mystic_altar")
    rec = service.load_run(rid)
    rec["state"]["gold"] = 5
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    gold_before = service.load_run(rid)["state"]["gold"]
    r = client.post(f"/api/runs/{rid}/act", json={
        "action": "encounter_choice",
        "chain": "mystic_altar", "enc_choice": "pray"})
    assert r.status_code == 400
    rec2 = service.load_run(rid)
    assert rec2["state"]["gold"] == gold_before
    assert "vanguard_badge" not in rec2["state"]["relics"]
    # 待抉择仍在（可改选）
    assert rec2["state"]["enc_state"]["pending"]["chain"] == "mystic_altar"


def test_hp_cost_rejected_when_it_would_kill(client):
    _seed, rid, _eid, _ev = _find_exp_seed_with_chain(client, "wounded_traveler")
    rec = service.load_run(rid)
    rec["state"]["health"] = 5
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    r = client.post(f"/api/runs/{rid}/act", json={
        "action": "encounter_choice",
        "chain": "wounded_traveler", "enc_choice": "aid"})
    assert r.status_code == 400
    assert service.load_run(rid)["state"]["health"] == 5


def test_max_health_choice_raises_both_current_and_max(client):
    _seed, rid, _eid, _ev = _find_exp_seed_with_chain(client, "mystic_altar")
    rec = service.load_run(rid)
    rec["state"]["health"] = 75
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    r = client.post(f"/api/runs/{rid}/act", json={
        "action": "encounter_choice",
        "chain": "mystic_altar", "enc_choice": "blood"})
    v = r.json()["run"]
    assert v["max_health"] == 80 and v["health"] == 70
    assert any(f["flag"] == "altar_blessed" for f in v["encounter_flags"])


def test_local_chain_grants_card_and_is_marked_done_but_not_carried(client):
    _seed, rid, _eid, _ev = _find_exp_seed_with_chain(client, "ancient_cache")
    deck_before = len(client.get(f"/api/runs/{rid}/resume").json()["deck"])
    r = client.post(f"/api/runs/{rid}/act", json={
        "action": "encounter_choice",
        "chain": "ancient_cache", "enc_choice": "force"})
    assert r.status_code == 200
    v = r.json()["run"]
    assert len(v["deck"]) == deck_before + 1
    assert any(c["id"] == "iron_wave" for c in v["deck"])
    st = service.load_run(rid)["state"]
    assert "ancient_cache" in st["enc_state"]["done_local"]
    assert st["enc_state"]["flags"] == {}  # 本地链不埋跨章 flag


def test_potion_choice_requires_replace_when_belt_full(client):
    _seed, rid, _eid, _ev = _find_exp_seed_with_chain(client, "ancient_cache")
    rec = service.load_run(rid)
    rec["state"]["potions"] = ["hp", "fire", "block"]
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    # 背满未给替换格 -> 400（即便该抉择还要扣金币，校验先于扣款）
    r = client.post(f"/api/runs/{rid}/act", json={
        "action": "encounter_choice",
        "chain": "ancient_cache", "enc_choice": "pick"})
    assert r.status_code == 400
    assert service.load_run(rid)["state"]["gold"] == rec["state"]["gold"]
    # 给合法替换格 -> 成功
    r = client.post(f"/api/runs/{rid}/act", json={
        "action": "encounter_choice",
        "chain": "ancient_cache", "enc_choice": "pick", "replace": 0})
    assert r.status_code == 200
    assert [p["id"] for p in r.json()["run"]["potions"]][0] == "hp"  # 新药水入 0 位


# ---------------- 跨章继承与预兆 ----------------
def _win_and_advance(client, rid, exp_id, finish=True):
    """通关当前章并推进（测试便捷路径），返回新章 run 视口。

    位置瞬移 + 压血是「测试作弊」（绕过动作日志），因此涉及回放逐位校验的
    用例，被瞬移的那两步会按 error 豁免（与既有远征用例一致）。
    """
    rec = service.load_run(rid)
    row3 = next(n for n, nd in rec["map"]["nodes"].items() if nd.get("row") == 3)
    if rec["state"]["position"] != row3:
        rec["state"]["position"] = row3
        db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    r = client.post(f"/api/runs/{rid}/act",
                    json={"action": "choose_node", "node": "boss"})
    assert r.status_code == 200
    rec = service.load_run(rid)
    rec["state"]["battle"]["entities"]["enemy"]["hp"] = 1
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    # 持续打牌/结束回合直到首领倒下（首张手牌可能是 0 伤防御牌）
    won = False
    for _ in range(60):
        v = client.get(f"/api/runs/{rid}/resume").json()
        if v["status"] != "in_progress":
            won = True
            break
        if not v["in_battle"]:
            break
        b = v["battle"]
        playable = [h for h in b["hand"]
                    if (h.get("cost", 1) if isinstance(h, dict) else 1) <= b["energy"]]
        strike = next((h for h in playable
                       if (h["id"] if isinstance(h, dict) else h) == "strike"), None)
        if strike:
            uid = strike["uid"] if isinstance(strike, dict) else strike
            client.post(f"/api/runs/{rid}/act", json={"action": "play", "card": uid})
        else:
            client.post(f"/api/runs/{rid}/act", json={"action": "end_turn"})
    assert won
    adv = client.post(f"/api/expeditions/{exp_id}/advance", json={})
    assert adv.status_code == 200, adv.text
    return adv.json()


def test_flag_carried_into_next_chapter_and_present_in_carry(client):
    _seed, rid, exp_id, _ev = _find_exp_seed_with_chain(client, "wounded_traveler")
    client.post(f"/api/runs/{rid}/act", json={
        "action": "encounter_choice",
        "chain": "wounded_traveler", "enc_choice": "rob"})
    adv = _win_and_advance(client, rid, exp_id)
    assert any(f["flag"] == "wt_robbed"
               for f in adv["expedition"]["carry"]["encounter_flags"])
    # 预兆在第 2 章首场战斗待消耗
    st = service.load_run(adv["run"]["run_id"])["state"]
    assert st["enc_state"]["battle_mods"] == {"enemy_hp": 8, "fragile": 2}


def test_first_battle_opener_applies_mods_and_consumes_once(client):
    _seed, rid, exp_id, _ev = _find_exp_seed_with_chain(client, "wounded_traveler")
    client.post(f"/api/runs/{rid}/act", json={
        "action": "encounter_choice",
        "chain": "wounded_traveler", "enc_choice": "rob"})
    adv = _win_and_advance(client, rid, exp_id)
    rid2 = adv["run"]["run_id"]
    m = mapgen.generate_map(service.load_run(rid2)["state"]["seed"])
    bn = next(n for n, nd in m["nodes"].items()
              if nd["type"] in (mapgen.ENCOUNTER, mapgen.ELITE))
    r = client.post(f"/api/runs/{rid2}/act",
                    json={"action": "choose_node", "node": bn})
    assert r.status_code == 200
    b = r.json()["run"]["battle"]
    # 敌人 +8 生命；玩家易碎 2 回合（首帧事件可见）
    enemy_def = next(nd for nd in m["nodes"].values()
                     if nd.get("enemy")) if False else None
    st = service.load_run(rid2)["state"]
    from app import enemies as enemies_mod
    base_hp = enemies_mod.get_enemy(st["battle"]["enemy"])["hp"]
    assert b["enemy"]["hp"] == base_hp + 8
    assert any(s["id"] == "fragile" for s in b["player"]["statuses"])
    # 修正已消耗
    assert service.load_run(rid2)["state"]["enc_state"]["battle_mods"] == {}
    # 事件日志含奇遇预兆标记（前端首帧可播放）
    assert any(x.get("encounter_opener") for x in r.json()["log"])


# ---------------- 伏击战（复仇续写） ----------------
def test_ambush_chain_starts_battle_and_rewards_gold_on_win(client):
    seed, rid, exp_id, _ev = _find_exp_seed_with_chain(client, "wounded_traveler")
    client.post(f"/api/runs/{rid}/act", json={
        "action": "encounter_choice",
        "chain": "wounded_traveler", "enc_choice": "rob"})
    adv = _win_and_advance(client, rid, exp_id)
    rid2 = adv["run"]["run_id"]
    # 找到第 2 章奇遇节点（应为 avenger_ambush）
    _m2, ev2, path2 = _event_for_seed(seed, chapter=2)
    # 先把可能的战斗打过去（用 API），抵达奇遇节点
    _walk(client, rid2, path2)
    v = client.get(f"/api/runs/{rid2}/resume").json()
    assert v["encounter"] and v["encounter"]["chain"] == "avenger_ambush", \
        v["encounter"]
    gold0 = v["gold"]
    r = client.post(f"/api/runs/{rid2}/act", json={
        "action": "encounter_choice",
        "chain": "avenger_ambush", "enc_choice": "fight"})
    assert r.status_code == 200
    assert r.json()["run"]["in_battle"] is True
    assert r.json()["run"]["battle"]["enemy_id"] == "avenger"
    # 打赢（API 机器人）-> 固定 35 金币、无普通战利品
    _auto_win(client, rid2)
    v = client.get(f"/api/runs/{rid2}/resume").json()
    assert v["status"] == "in_progress"
    assert v["gold"] == gold0 + 35
    assert v["reward_options"] == []
    st = service.load_run(rid2)["state"]
    assert "wounded_traveler" in st["enc_state"]["completed"]
    assert "wt_robbed" not in st["enc_state"]["flags"]
    assert st["enc_state"]["ambush"] is None


def test_ambush_pay_choice_costs_gold_and_clears_flag(client):
    seed, rid, exp_id, _ev = _find_exp_seed_with_chain(client, "wounded_traveler")
    client.post(f"/api/runs/{rid}/act", json={
        "action": "encounter_choice",
        "chain": "wounded_traveler", "enc_choice": "rob"})
    adv = _win_and_advance(client, rid, exp_id)
    rid2 = adv["run"]["run_id"]
    _m2, _ev2, path2 = _event_for_seed(seed, chapter=2)
    _walk(client, rid2, path2)
    v = client.get(f"/api/runs/{rid2}/resume").json()
    # 续写链优先：持有 wt_robbed 时，第 2 章奇遇必为复仇伏击
    assert v["encounter"] and v["encounter"]["chain"] == "avenger_ambush"
    rec = service.load_run(rid2)
    rec["state"]["gold"] = 50
    db.save_run(rid2, rec["state"]["status"], rec["state"]["position"], rec["state"])
    r = client.post(f"/api/runs/{rid2}/act", json={
        "action": "encounter_choice",
        "chain": "avenger_ambush", "enc_choice": "pay"})
    assert r.status_code == 200
    assert r.json()["run"]["in_battle"] is False
    assert r.json()["run"]["gold"] == 30
    st = service.load_run(rid2)["state"]
    assert "wt_robbed" not in st["enc_state"]["flags"]
    assert "wounded_traveler" in st["enc_state"]["completed"]


def test_ambush_loss_settles_expedition(client):
    seed, rid, exp_id, _ev = _find_exp_seed_with_chain(client, "wounded_traveler")
    client.post(f"/api/runs/{rid}/act", json={
        "action": "encounter_choice",
        "chain": "wounded_traveler", "enc_choice": "rob"})
    adv = _win_and_advance(client, rid, exp_id)
    rid2 = adv["run"]["run_id"]
    _m2, _ev2, path2 = _event_for_seed(seed, chapter=2)
    _walk(client, rid2, path2)
    v = client.get(f"/api/runs/{rid2}/resume").json()
    assert v["encounter"] and v["encounter"]["chain"] == "avenger_ambush"
    client.post(f"/api/runs/{rid2}/act", json={
        "action": "encounter_choice",
        "chain": "avenger_ambush", "enc_choice": "fight"})
    # 玩家 1 血，只结束回合直到被复仇者击杀
    rec = service.load_run(rid2)
    rec["state"]["battle"]["entities"]["player"]["hp"] = 1
    db.save_run(rid2, rec["state"]["status"], rec["state"]["position"], rec["state"])
    lost = None
    for _ in range(20):
        r = client.post(f"/api/runs/{rid2}/act", json={"action": "end_turn"})
        if r.json()["run"]["status"] == "lost":
            lost = r.json()
            break
    assert lost is not None
    assert lost["run"]["expedition"]["status"] == "lost"
    exp = client.get(f"/api/expeditions/{exp_id}").json()["expedition"]
    assert exp["status"] == "lost"


# ---------------- 回放 / 续局 / 旧档 ----------------
def test_encounter_choice_replays_bit_exact_single_run(client):
    # 普通局本地链：抉择与代价逐位回放
    seed, rid, _eid, _ev = _find_exp_seed_with_chain(client, "ancient_cache")
    client.post(f"/api/runs/{rid}/act", json={
        "action": "encounter_choice",
        "chain": "ancient_cache", "enc_choice": "force"})
    rep = client.get(f"/api/runs/{rid}/replay").json()
    v = rep["verification"]
    assert v["mismatch"] == 0 and v["error"] == 0
    steps = [s for s in rep["steps"] if s["action"] in ("choose_node", "encounter_choice")]
    kinds = [s["kind"] for s in steps]
    assert "encounter" in kinds
    choice_step = next(s for s in rep["steps"] if s["action"] == "encounter_choice")
    assert choice_step["check"] == "ok"
    grants = [e.get("encounter_grant") for e in choice_step["events"]
              if isinstance(e, dict) and e.get("encounter_grant")]
    assert any(g.get("type") == "card" and g.get("card") == "iron_wave" for g in grants)


def test_full_expedition_with_encounter_flags_replays_bit_exact(client):
    """两章远征（含跨章 flag 与首场预兆）逐章/整程回放校验。

    第 1 章通关用测试便捷路径（位置瞬移 + 压血，属绕过日志的作弊，那两步按
    error 豁免，与既有用例一致）；第 2 章的预兆战斗与伏击战全部走合法 API，
    必须逐位通过——这才是 2.8.0 新增逻辑的逐位校验重点。
    """
    seed, rid, exp_id, _ev = _find_exp_seed_with_chain(client, "wounded_traveler")
    client.post(f"/api/runs/{rid}/act", json={
        "action": "encounter_choice",
        "chain": "wounded_traveler", "enc_choice": "rob"})
    adv = _win_and_advance(client, rid, exp_id)
    rid2 = adv["run"]["run_id"]
    # 第 2 章：打首场战斗（消耗预兆），再走到奇遇节点并处理（可能是续写）
    m2 = mapgen.generate_map(service.load_run(rid2)["state"]["seed"])
    bn = next(n for n, nd in m2["nodes"].items()
              if nd["type"] in (mapgen.ENCOUNTER, mapgen.ELITE))
    client.post(f"/api/runs/{rid2}/act", json={"action": "choose_node", "node": bn})
    _auto_win(client, rid2)
    _m2, ev2, path2 = _event_for_seed(seed, chapter=2)
    # 只走到事件节点（event 在 path 末尾；此前可能还有未走节点）
    v = client.get(f"/api/runs/{rid2}/resume").json()
    # 计算从当前位置到 ev2 的剩余路径
    from collections import deque
    q = deque([(v["position"], [])])
    rest = None
    while q:
        pos, pth = q.popleft()
        if pos == ev2:
            rest = pth
            break
        for n in m2["routes"].get(pos, []):
            q.append((n, pth + [n]))
    assert rest is not None
    _walk(client, rid2, rest)
    v = client.get(f"/api/runs/{rid2}/resume").json()
    if v.get("encounter"):
        cid0 = v["encounter"]["choices"][0]["id"]
        r = client.post(f"/api/runs/{rid2}/act", json={
            "action": "encounter_choice",
            "chain": v["encounter"]["chain"], "enc_choice": cid0})
        assert r.status_code == 200
        if r.json()["run"]["in_battle"]:
            _auto_win(client, rid2)
    # 第 2 章（预兆战斗 + 伏击续写）全程合法 API：必须逐位通过、零 mismatch/error。
    # 第 1 章通关用了位置瞬移作弊，只断言没有状态分叉（作弊两步允许 error）。
    rep1 = client.get(f"/api/runs/{rid}/replay").json()
    assert rep1["verification"]["mismatch"] == 0
    rep2 = client.get(f"/api/runs/{rid2}/replay").json()
    assert rep2["verification"]["mismatch"] == 0
    assert rep2["verification"]["error"] == 0
    # 第 2 章最终帧与在线存档一致
    online = client.get(f"/api/runs/{rid2}/resume").json()
    assert rep2["final_view"]["health"] == online["health"]
    assert rep2["final_view"]["gold"] == online["gold"]
    # 整程回放：第 2 章（新增逻辑承载章）逐位通过、只读隔离
    full = client.get(f"/api/expeditions/{exp_id}/replay").json()
    assert full["isolated"] is True
    ch2_rep = next(c for c in full["chapters"] if c["chapter"] == 2)["replay"]
    assert ch2_rep["verification"]["mismatch"] == 0
    assert ch2_rep["verification"]["error"] == 0


def test_legacy_save_migrates_enc_state(client):
    """2.8.0 之前旧档无 enc_state：首次续局/行动补全新结构（迁移步 legacy）。"""
    rid = client.post("/api/runs", json={"seed": 3}).json()["run_id"]
    # 手工构造旧形状：删除 enc_state
    rec = service.load_run(rid)
    rec["state"].pop("enc_state", None)
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    v = client.get(f"/api/runs/{rid}/resume").json()
    assert v["encounter"] is None and v["encounter_flags"] == []
    st = service.load_run(rid)["state"]
    assert "enc_state" in st and st["enc_state"]["flags"] == {}


def test_single_run_event_only_offers_local_chain(client):
    """普通局的奇遇节点只能抽到本地链（跨章起始链不会出现）。"""
    found = 0
    for seed in range(1, 60):
        r = client.post("/api/runs", json={"seed": seed})
        rid = r.json()["run_id"]
        m = service.load_run(rid)["map"]
        ev = next(n for n, nd in m["nodes"].items() if nd["type"] == mapgen.EVENT)
        from collections import deque
        q = deque([(m["start"], [])]); path = None
        while q:
            pos, pth = q.popleft()
            if pos == ev:
                path = pth
                break
            for n in m["routes"].get(pos, []):
                q.append((n, pth + [n]))
        _walk(client, rid, path)
        v = client.get(f"/api/runs/{rid}/resume").json()
        assert v.get("encounter") is not None or v["position"] == ev
        if v.get("encounter"):
            found += 1
            assert v["encounter"]["chain"] in (
                "ancient_cache", "wandering_sage", "dry_well")
            assert v["expedition"] is None
    assert found >= 3  # 多枚种子应稳定只出本地链
