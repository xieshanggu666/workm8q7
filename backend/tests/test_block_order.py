"""格挡/援护结算顺序修复（2.7.0）的规则升级兼容。

背景：2.7.0 之前存在两个战斗结算缺陷——
1. 玩家格挡在 end_turn 里、敌人行动【之前】就被清零，卡牌（防御等）与壁垒
   药水给的格挡完全无法抵挡敌方伤害；
2. 伙伴援护按【全额伤害】判穿（应只吃格挡吸收后的穿透溢出），格挡被直接绕过。

修复后顺序：敌人攻击 -> 格挡吸收 -> 穿透溢出先由伙伴援护 -> 剩余扣血；格挡
持续整个敌方行动段、在回合末（下一回合 start_turn）清零。

本文件验证：
- 新规则本身：卡牌格挡/壁垒药水抵挡伤害、援护只吃穿透溢出、格挡不跨回合保留；
- 旧规则（2.6.1）下录制的战斗动作按 legacy_block 旧时序逐位重演（敌人行动前
  清格挡 + 按全额援护），历史日志全部可回放、不标 mismatch/error；
- 升级边界：旧动作序列之后首个由 2.7.0 服务器提交的动作严格校验（ok），
  最终帧与在线存档逐位一致——续局与回放不分叉。
"""
import uuid

from app import companions as companions_mod
from app import db
from app import mapgen
from app import service
from app.engine import Battle
from app.enemies import get_enemy

OLD_VER = "2.6.1"


# ---------- 旧版服务器夹具：旧格挡时序下录制动作 ----------
def _make_legacy_run(seed):
    """构造一局旧版本（2.6.1）规则录制的 run（create 时伙伴恒为 None：
    伙伴只能经商店招募，建局直接带伙伴不是合法状态）。"""
    run_id = uuid.uuid4().hex[:12]
    state = service._new_run_state(seed)
    state["rules_version"] = OLD_VER
    map_data = mapgen.generate_map(seed)
    with db.transaction() as conn:
        db.insert_run(conn, run_id, seed, state["status"], state["position"],
                      map_data, state)
        db.append_event_conn(conn, run_id, 1, "create", {
            "seed": seed, "ver": OLD_VER,
            "ckpt": service.state_checkpoint(state),
        })
    return run_id


def _legacy_commit(run_id, action):
    """用旧版（2.6.1）格挡时序提交一个战斗动作并落库旧校验点。"""
    rec = service.load_run(run_id)
    run, m = rec["state"], rec["map"]
    # 纯规则差异：让本次推演加载的 Battle 走旧时序
    run[service._LEGACY_BLOCK_KEY] = True
    try:
        service._apply_action(run, action["action"], action, m,
                              grant_unlocks=False)
    finally:
        run.pop(service._LEGACY_BLOCK_KEY, None)
    payload = {
        "node": action.get("node"), "card": action.get("card"),
        "slot": action.get("slot"),
        "ver": OLD_VER, "ckpt": service.state_checkpoint(run),
    }
    with db.transaction() as conn:
        db.save_run_run(conn, run_id, run["status"], run["position"], run)
        db.append_event_conn(conn, run_id, db.next_seq_conn(conn, run_id),
                             action["action"], payload)
    return run


def _find_battle(seed):
    m = mapgen.generate_map(seed)
    return next(n for n in m["routes"][m["start"]]
                if m["nodes"][n]["type"] in (mapgen.ENCOUNTER, mapgen.ELITE))


def _auto_action(st, prefer=("strike", "guard")):
    """从当前战斗视口选一个可出的手牌引用；无牌可出返回 None（-> end_turn）。"""
    b = service._load_battle(st)
    for want in prefer:
        ref = next((u for u in b.hand
                    if b._card_def(u)["id"] == want
                    and b._card_def(u)["cost"] <= b.energy), None)
        if ref is not None:
            return ref
    return None


def _play_legacy_turns(rid, turns):
    """旧规则下打若干轮：能出攻击牌就出，否则结束回合（触发旧格挡时序）。"""
    for _ in range(turns):
        st = service.load_run(rid)["state"]
        if not st.get("in_battle"):
            break
        ref = _auto_action(st)
        if ref is not None:
            _legacy_commit(rid, {"action": "play", "card": ref})
        else:
            _legacy_commit(rid, {"action": "end_turn"})


# ---------- 新规则：格挡真正抵挡敌方伤害 ----------
def test_card_block_now_absorbs_enemy_damage():
    rid = service.create_run(seed=1)["run_id"]
    node = _find_battle(1)
    service.act(rid, {"action": "choose_node", "node": node})
    hp0 = service.load_run(rid)["state"]["battle"]["entities"]["player"]["hp"]

    st = service.load_run(rid)["state"]
    guard_uid = next(h for h in st["battle"]["hand"]
                     if st["card_instances"][h]["id"] == "guard")
    service.act(rid, {"action": "play", "card": guard_uid})
    r = service.act(rid, {"action": "end_turn"})
    dmg_events = [e for e in r["log"] if isinstance(e, dict)
                  and e.get("action") == "damage" and e.get("target") == "player"]
    assert dmg_events, "敌人本次意图必须为直接攻击（seed=1 哥布林抓挠 6 点）"
    assert dmg_events[0]["value"] == 6
    hp1 = r["run"]["battle"]["player"]["hp"]
    # 6 点攻击被 5 格挡吸收，只掉 1 血（旧规则掉 6 血）
    assert hp0 - hp1 == 1


def test_block_potion_now_absorbs_enemy_damage():
    rid = service.create_run(seed=1)["run_id"]
    node = _find_battle(1)
    rec = service.load_run(rid)
    rec["state"]["potions"] = ["block"]
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    service.act(rid, {"action": "choose_node", "node": node})
    hp0 = service.load_run(rid)["state"]["battle"]["entities"]["player"]["hp"]
    service.act(rid, {"action": "use_potion", "slot": 0})
    r = service.act(rid, {"action": "end_turn"})
    hp1 = r["run"]["battle"]["player"]["hp"]
    # 壁垒药水 12 格挡完全挡下 6 点攻击，不掉血
    assert hp0 == hp1


def test_block_does_not_carry_across_turns():
    """格挡只持续敌方行动段：敌人行动后进入下一回合时清零（不滚存）。"""
    rid = service.create_run(seed=1)["run_id"]
    node = _find_battle(1)
    service.act(rid, {"action": "choose_node", "node": node})
    st = service.load_run(rid)["state"]
    guard_uid = next(h for h in st["battle"]["hand"]
                     if st["card_instances"][h]["id"] == "guard")
    service.act(rid, {"action": "play", "card": guard_uid})
    r = service.act(rid, {"action": "end_turn"})
    assert r["run"]["in_battle"] is True
    assert r["run"]["battle"]["player"]["block"] == 0


# ---------- 援护新顺序：只吃格挡穿透后的溢出（引擎直构，伙伴在场） ----------
def _one_enemy_hit(enemy_id, block, raw_damage):
    """手工推一次带 attack 标签的敌人攻击（不经过 end_turn，避免意图随机性）。"""
    from app.settlement import EffectEvent, SettlementQueue
    run_state = {
        "max_health": 75, "health": 75, "deck": ["strike"] * 7,
        "relics": {}, "base_energy": 3, "card_instances": {},
        "companion": companions_mod.make_companion(),
    }
    b = Battle(run_state, get_enemy(enemy_id), seed=1, battle_index=1,
               companion_state=run_state["companion"])
    b.start_turn()
    p, c = b.entities["player"], b.entities["companion"]
    p["block"] = block
    chp0 = c["hp"]
    q = SettlementQueue(b)
    q.push(EffectEvent("damage", target="player", value=raw_damage,
                       source="enemy", tags=["attack"]))
    q.run()
    return p["hp"], p["block"], chp0 - c["hp"]


def test_companion_guard_eats_only_block_overflow():
    # 5 格挡 + 6 伤害：格挡吃 5，溢出 1 由援护吃掉，玩家不掉血
    assert _one_enemy_hit("echo_knight", 5, 6) == (75, 0, 1)
    # 12 格挡 + 6 伤害：完全格挡，援护不承伤
    assert _one_enemy_hit("echo_knight", 12, 6) == (75, 6, 0)
    # 0 格挡 + 6 伤害：援护吃 3，玩家吃 3
    assert _one_enemy_hit("echo_knight", 0, 6) == (72, 0, 3)
    # 2 格挡 + 10 伤害：格挡吃 2，援护吃 3，玩家吃 5
    assert _one_enemy_hit("echo_knight", 2, 10) == (70, 0, 3)


def _end_turn_with_block(block, legacy):
    """走完整 end_turn（回响剑士连环斩两次 4 点，均带 attack 标签）。

    返回 (玩家hp, 玩家block, 伙伴承伤)。旧时序格挡在敌人行动前清零：两次攻击
    各被援护吃 3 -> 玩家各吃 1（共 2）、伙伴承伤 6；新时序 5 格挡先吸收第一击
    的 4，第二击格挡再吃 1、穿透溢出 3 由援护吃掉，玩家全程不掉血。
    """
    run_state = {
        "max_health": 75, "health": 75, "deck": ["strike"] * 7,
        "relics": {}, "base_energy": 3, "card_instances": {},
        "companion": companions_mod.make_companion(),
    }
    b = Battle(run_state, get_enemy("echo_knight"), seed=1, battle_index=1,
               companion_state=run_state["companion"])
    b.start_turn()
    p, c = b.entities["player"], b.entities["companion"]
    p["block"] = block
    chp0 = c["hp"]
    b.legacy_block = legacy
    b.end_turn()
    return p["hp"], p["block"], chp0 - c["hp"]


def test_legacy_timing_reproduces_old_block_pierce():
    # 旧：格挡先清零，两次 4 点全额作用 -> 援护各吃 3，玩家各吃 1（共掉 2）
    assert _end_turn_with_block(5, legacy=True) == (73, 0, 6)
    # 新：5 格挡挡下第一击 4；第二击格挡再吃 1，穿透溢出 3 全由援护吃掉，
    # 玩家不掉血
    assert _end_turn_with_block(5, legacy=False) == (75, 0, 3)


# ---------- 升级兼容：2.6.1 录制的战斗动作可逐位重演 ----------
def test_legacy_battle_actions_replay_without_mismatch():
    rid = _make_legacy_run(1)
    node = _find_battle(1)
    _legacy_commit(rid, {"action": "choose_node", "node": node})
    # 旧规则下打出防御（5 格挡），随后 end_turn 时格挡在敌人行动前被清零
    st = service.load_run(rid)["state"]
    guard_uid = next(h for h in st["battle"]["hand"]
                     if st["card_instances"][h]["id"] == "guard")
    _legacy_commit(rid, {"action": "play", "card": guard_uid})
    hp_before = service.load_run(rid)["state"]["battle"]["entities"]["player"]["hp"]
    _legacy_commit(rid, {"action": "end_turn"})
    hp_after = service.load_run(rid)["state"]["battle"]["entities"]["player"]["hp"]
    # 旧录制品：格挡被清零，6 点攻击全额命中
    assert hp_before - hp_after == 6

    rep = service.replay(rid)
    checks = rep["verification"]["checks"]
    assert not [c for c in checks if c["status"] in ("mismatch", "error")]
    # 旧动作以 legacy 呈现（旧格挡时序与新推演不可逐位比）；create 不含战斗、
    # 纯规则修复不改初态结构，create 严格 ok
    by_action = {c["action"]: c["status"] for c in checks}
    assert by_action["create"] == "ok"
    assert by_action["choose_node"] == "legacy"
    assert by_action["play"] == "legacy"
    assert by_action["end_turn"] == "legacy"


def test_upgrade_boundary_first_new_action_is_strict_and_final_matches():
    """旧动作 -> 升级后首个 2.7.0 动作：边界动作严格校验，续局与回放终态一致。"""
    rid = _make_legacy_run(1)
    node = _find_battle(1)
    _legacy_commit(rid, {"action": "choose_node", "node": node})
    _play_legacy_turns(rid, 2)  # 旧规则下打两轮（含 end_turn 的敌方攻击）

    # 在线续局：纯规则修复无结构迁移，直接返回战斗中视口
    resumed = service.resume(rid)
    assert resumed["in_battle"] is True

    # 升级后的首个动作（新规则）正常在线提交（响应不标记重复）
    st = service.load_run(rid)["state"]
    assert st.get("in_battle")
    ref = _auto_action(st)
    action = {"action": "play", "card": ref} if ref is not None \
        else {"action": "end_turn"}
    r = service.act(rid, action)
    assert r.get("duplicate") is False

    rep = service.replay(rid)
    checks = rep["verification"]["checks"]
    # 不存在 mismatch/error；旧动作 legacy，边界动作（末步）严格 ok
    assert all(c["status"] in ("ok", "legacy") for c in checks)
    assert checks[-1]["status"] == "ok"
    assert rep["verification"]["final_match"] is True


def test_new_run_checkpoints_all_strict_under_fixed_rules():
    """全新局整场战斗：play/end_turn 每一步都严格校验（修复在新档全量生效）。"""
    rid = service.create_run(seed=2)["run_id"]
    node = _find_battle(2)
    service.act(rid, {"action": "choose_node", "node": node})
    for _ in range(8):
        st = service.load_run(rid)["state"]
        if not st.get("in_battle"):
            break
        ref = _auto_action(st)
        if ref is not None:
            service.act(rid, {"action": "play", "card": ref})
        else:
            service.act(rid, {"action": "end_turn"})
    rep = service.replay(rid)
    battle_checks = [c for c in rep["verification"]["checks"]
                     if c["action"] in ("play", "end_turn")]
    assert battle_checks and all(c["status"] == "ok" for c in battle_checks)
