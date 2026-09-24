"""存档升级衔接：旧规则档（无药水/无伙伴字段）迁移后，商店规则切换与回放重建一致。

背景（2.5.0 药水背包、2.6.0 伙伴引入）：旧服务器录制的日志与旧形状校验点里没有
药水/伙伴字段；升级后首次载入（/resume 静默迁移或首个 /act 迁移）切换到新规则。
回放从 create 日志重建时必须在【迁移点的那个动作之前】切换规则，否则：

- 迁移后首个动作若是「进入新商店」，回放仍按旧规则生成库存（缺药水/伙伴货架），
  升级后的购买动作无法重演（unknown shop item），实玩与回放库存分叉；
- 战后战利品在 2.5.0 才追加药水选项，旧战斗若按新选项集重建会让旧
  claim_reward 下标错位（金币/卡牌张冠李戴）；
- 迁移点步骤被一律按 legacy 豁免，真实的校验点分叉被静默吞掉（误报通过）。

本文件用「旧版服务器提交」夹具忠实构造旧形状存档（状态、日志、校验点全部按
旧版本录制），再走升级后的在线动作与整局回放，逐位核对。
"""
import uuid

from app import db
from app import enemies as enemies_mod
from app import mapgen
from app import potions as potions_mod
from app import rewards as rewards_mod
from app import service


# ---------- 旧版服务器夹具 ----------
def _make_legacy_run(seed, old_ver, *, drop_potions, drop_companion):
    """构造一局旧版本形状的 run：create 事件记录旧形状校验点。"""
    run_id = uuid.uuid4().hex[:12]
    state = service._new_run_state(seed)
    map_data = mapgen.generate_map(seed)
    state["rules_version"] = old_ver
    if drop_potions:
        state.pop("potions", None)
    if drop_companion:
        state.pop("companion", None)
    with db.transaction() as conn:
        db.insert_run(conn, run_id, seed, state["status"], state["position"],
                      map_data, state)
        db.append_event_conn(conn, run_id, 1, "create", {
            "seed": seed, "ver": old_ver,
            "ckpt": service.state_checkpoint(
                state,
                include_potions=not drop_potions,
                include_companion=not drop_companion),
        })
    return run_id


def _legacy_commit(run_id, action, old_ver, *, no_potions, no_companion):
    """用旧版语义提交一个动作：旧规则推演 + 旧形状状态/校验点落库。"""
    rec = service.load_run(run_id)
    run, m = rec["state"], rec["map"]
    # 2.7.0 修复了格挡/援护结算顺序：模拟更旧版本服务器时，战斗必须按旧时序
    # 推演（敌人行动前清格挡、援护按全额抵挡），否则录出的战斗血量是新规则
    # 结果，回放沿 legacy_block 旧路径重建时会产生真实分叉。
    legacy_block = service._ver_lt(old_ver, service.BLOCK_RULES_VERSION)
    if no_potions:
        run.pop("potions", None)
        run[service._LEGACY_NO_POTIONS_KEY] = True
    if no_companion:
        run.pop("companion", None)
        run[service._LEGACY_NO_COMPANION_KEY] = True
    if legacy_block:
        run[service._LEGACY_BLOCK_KEY] = True
    try:
        service._apply_action(run, action["action"], action, m,
                              grant_unlocks=True)
    finally:
        run.pop(service._LEGACY_NO_POTIONS_KEY, None)
        run.pop(service._LEGACY_NO_COMPANION_KEY, None)
        run.pop(service._LEGACY_BLOCK_KEY, None)
    if no_potions:
        run.pop("potions", None)
    if no_companion:
        run.pop("companion", None)
    if run.get("shop"):
        if no_potions:
            run["shop"].pop("potions", None)
        if no_companion:
            run["shop"].pop("companions", None)
    payload = {
        "node": action.get("node"), "card": action.get("card"),
        "option": action.get("option"), "kind": action.get("kind"),
        "sku": action.get("sku"), "slot": action.get("slot"),
        "ver": old_ver,
        "ckpt": service.state_checkpoint(
            run, include_potions=not no_potions,
            include_companion=not no_companion),
    }
    with db.transaction() as conn:
        db.save_run_run(conn, run_id, run["status"], run["position"], run)
        db.append_event_conn(conn, run_id, db.next_seq_conn(conn, run_id),
                             action["action"], payload)
    return run


def _legacy_win_and_claim_gold(run_id, old_ver, *, no_potions, no_companion,
                               turns=160):
    """旧版合法打赢当前战斗并领金币（全部动作进日志）。返回领到的金币数。"""
    for _ in range(turns):
        st = service.load_run(run_id)["state"]
        if not st.get("in_battle"):
            break
        b = service._load_battle(st)
        playable = sorted((u for u in b.hand if b._card_def(u)["cost"] <= b.energy),
                          key=lambda u: b._card_def(u)["cost"])
        act = ({"action": "play", "card": playable[0]} if playable
               else {"action": "end_turn"})
        _legacy_commit(run_id, act, old_ver,
                       no_potions=no_potions, no_companion=no_companion)
    assert not service.load_run(run_id)["state"].get("in_battle")
    st = service.load_run(run_id)["state"]
    gold_idx = next((i for i, o in enumerate(st.get("reward_options", []))
                     if o["kind"] == "gold"), None)
    if gold_idx is None:
        return 0
    gain = next(eff["value"] for eff in st["reward_options"][gold_idx]["effects"]
                if eff["type"] == "gold")
    # 2.5.0 之前的旧战斗战利品里没有药水选项
    if no_potions:
        assert not any(any(e.get("type") == "add_potion" for e in o.get("effects", []))
                       for o in st["reward_options"])
    _legacy_commit(run_id, {"action": "claim_reward", "option": gold_idx}, old_ver,
                   no_potions=no_potions, no_companion=no_companion)
    return gain


def _find_battle_shop_path(seed_start=0, min_gold=None, battle_count=1):
    """找 start->遭遇(1~2 场)->商店 路径；min_gold 为旧规则下各战金币之和下限。"""
    dummy = {"card_instances": {}}

    def battle_gold(seed, m, node, index):
        enemy = enemies_mod.get_enemy(m["nodes"][node]["enemy"])
        opts = rewards_mod.battle_reward_options(
            seed + index, enemy, dummy, include_potions=False)
        gold_opt = next((o for o in opts if o["kind"] == "gold"), None)
        return next((eff["value"] for eff in gold_opt["effects"]
                     if eff["type"] == "gold"), 0) if gold_opt else 0

    for seed in range(seed_start, seed_start + 80000):
        m = mapgen.generate_map(seed)
        chains = []
        for e1 in m["routes"][m["start"]]:
            if battle_count == 1:
                chains.append((e1,))
            else:
                chains.extend((e1, e2) for e2 in m["routes"][e1])
        for chain in chains:
            if any(m["nodes"][e]["type"] != mapgen.ENCOUNTER for e in chain):
                continue
            last = chain[-1]
            shop = next((n for n in m["routes"][last]
                         if m["nodes"][n]["type"] == mapgen.SHOP), None)
            if shop is None:
                continue
            if min_gold is not None:
                total = sum(battle_gold(seed, m, e, i)
                            for i, e in enumerate(chain, start=1))
                if total < min_gold:
                    continue
            return seed, list(chain), shop
    raise AssertionError("no qualifying battle->shop path")


# ---------- 旧战斗战利品：药水选项的加入不改变旧 claim 下标 ----------
def test_legacy_battle_reward_options_exclude_potions():
    enemy = {"reward_cards": [], "reward_gold": 30}
    old_opts = rewards_mod.battle_reward_options(
        123, enemy, {"card_instances": {}}, include_potions=False)
    new_opts = rewards_mod.battle_reward_options(
        123, enemy, {"card_instances": {}}, include_potions=True)
    assert all(o["kind"] != "potion" for o in old_opts)
    # 旧选项是新选项去掉药水项后的同一前缀：下标含义保持不变
    assert new_opts[:len(old_opts)] == old_opts


# ---------- 2.4.0 旧档：resume 静默迁移后进新商店买药水 ----------
def test_pre_potions_save_resume_migration_new_shop_replays_bit_exact():
    cheapest = min(potions_mod.POTION_PRICE.values())
    seed, [battle_node], shop_node = _find_battle_shop_path(min_gold=cheapest)
    rid = _make_legacy_run(seed, "2.4.0", drop_potions=True, drop_companion=False)

    _legacy_commit(rid, {"action": "choose_node", "node": battle_node},
                   "2.4.0", no_potions=True, no_companion=False)
    gold = _legacy_win_and_claim_gold(
        rid, "2.4.0", no_potions=True, no_companion=False)
    assert gold > 0

    # 升级：/resume 静默补空背包（无迁移日志事件）
    assert service.resume(rid)["potions"] == []

    # 升级后的动作：进入新商店——货架必须按新规则带药水
    entered = service.act(rid, {"action": "choose_node", "node": shop_node})
    shelf = entered["run"]["shop"]["potions"]
    assert len(shelf) >= 1
    offer = min(shelf, key=lambda o: o["price"])
    assert gold >= offer["price"]
    bought = service.act(rid, {"action": "shop_buy", "kind": "potion",
                              "sku": offer["sku"]})
    online_belt = [p["id"] for p in bought["run"]["potions"]]
    assert online_belt == [offer["potion"]]

    rep = service.replay(rid)
    v = rep["verification"]
    assert v["mismatch"] == 0 and v["error"] == 0
    assert v["final_match"] is True

    boundary = next(s for s in rep["steps"] if s["action"] == "choose_node"
                    and s["view"]["position"] == shop_node)
    buy = next(s for s in rep["steps"] if s["action"] == "shop_buy")
    # 关键：静默迁移不产生日志，迁移点（首个新版动作 = 进新商店）必须按
    # 新规则生成库存并逐位可比——不能再以 legacy 名义豁免掉真实分叉。
    assert boundary["check"] == "ok"
    assert [p["sku"] for p in boundary["view"]["shop"]["potions"]] == \
           [p["sku"] for p in shelf]
    assert buy["check"] == "ok"
    assert [p["id"] for p in buy["view"]["potions"]] == [offer["potion"]]
    # 最终帧背包与在线一致；升级前的历史动作仍按 legacy 呈现但可正常重演
    assert [p["id"] for p in rep["final_view"]["potions"]] == online_belt
    assert all(c["status"] in ("ok", "legacy") for c in v["checks"])


# ---------- 2.4.0 旧档：首个 /act 才迁移，第一个动作就是进新商店 ----------
def test_pre_potions_save_act_migration_boundary_shop_is_consistent():
    cheapest = min(potions_mod.POTION_PRICE.values())
    seed, [battle_node], shop_node = _find_battle_shop_path(
        2000, min_gold=cheapest)
    rid = _make_legacy_run(seed, "2.4.0", drop_potions=True, drop_companion=False)
    _legacy_commit(rid, {"action": "choose_node", "node": battle_node},
                   "2.4.0", no_potions=True, no_companion=False)
    gold = _legacy_win_and_claim_gold(
        rid, "2.4.0", no_potions=True, no_companion=False)

    # 不先 /resume：首个新动作直接进商店，在线路径在本步事务内迁移
    entered = service.act(rid, {"action": "choose_node", "node": shop_node})
    assert entered["rev"] >= 2
    shelf = entered["run"]["shop"]["potions"]
    assert len(shelf) >= 1
    offer = min(shelf, key=lambda o: o["price"])
    assert gold >= offer["price"]

    bought = service.act(rid, {"action": "shop_buy", "kind": "potion",
                              "sku": offer["sku"]})
    online_belt = [p["id"] for p in bought["run"]["potions"]]

    rep = service.replay(rid)
    v = rep["verification"]
    assert v["mismatch"] == 0 and v["error"] == 0
    # 迁移步本身（带 migrated 标记）按 legacy 豁免；其后的购买必须严格通过
    boundary = next(s for s in rep["steps"] if s["action"] == "choose_node"
                    and s["view"]["position"] == shop_node)
    buy = next(s for s in rep["steps"] if s["action"] == "shop_buy")
    assert boundary["migrated"] is True and boundary["check"] == "legacy"
    # 规则在动作前已切换：迁移步内生成的新商店已带药水货架
    assert [p["sku"] for p in boundary["view"]["shop"]["potions"]] == \
           [p["sku"] for p in shelf]
    assert buy["check"] == "ok" and buy["error"] is None
    assert [p["id"] for p in rep["final_view"]["potions"]] == online_belt


# ---------- 旧商店（迁移前生成、缺药水货架）迁移后不补造，历史交易保留 ----------
def test_old_shop_shelf_and_tx_preserved_through_migration():
    cheapest_card = 45  # 进阶卡售价区间下限
    seed, battles, shop_node = _find_battle_shop_path(
        4000, min_gold=cheapest_card, battle_count=2)
    rid = _make_legacy_run(seed, "2.4.0", drop_potions=True, drop_companion=False)
    total_gold = 0
    for b in battles:
        _legacy_commit(rid, {"action": "choose_node", "node": b},
                       "2.4.0", no_potions=True, no_companion=False)
        total_gold += _legacy_win_and_claim_gold(
            rid, "2.4.0", no_potions=True, no_companion=False)

    # 旧版进入商店（旧库存：没有药水货架）
    _legacy_commit(rid, {"action": "choose_node", "node": shop_node},
                   "2.4.0", no_potions=True, no_companion=False)
    old_shop = service.load_run(rid)["state"]["shop"]
    assert "potions" not in old_shop  # 旧库存结构里根本没有该货架
    card_offer = next(it for it in old_shop["cards"] if not it["sold"])
    assert total_gold >= card_offer["price"]

    # 旧版完成一笔卡牌购买（历史交易，旧形状校验点）
    _legacy_commit(rid, {"action": "shop_buy", "kind": "card",
                         "sku": card_offer["sku"]},
                   "2.4.0", no_potions=True, no_companion=False)
    # 升级 + 续局：既有货架与交易记录原样保留，不凭空补药水货架
    service.resume(rid)
    migrated_shop = service.load_run(rid)["state"]["shop"]
    assert len(migrated_shop["tx"]) == 1
    assert migrated_shop["tx"][0]["sku"] == card_offer["sku"]
    assert migrated_shop["tx"][0]["type"] == "buy"

    rep = service.replay(rid)
    v = rep["verification"]
    assert v["mismatch"] == 0 and v["error"] == 0
    old_buy = next(s for s in rep["steps"] if s["action"] == "shop_buy")
    # 旧商店帧同样没有药水货架，历史交易逐帧可见
    assert old_buy["view"]["shop"]["potions"] == []
    assert [t["sku"] for t in old_buy["view"]["shop"]["tx"]] == [card_offer["sku"]]


# ---------- 2.5.0 旧档（有药水、无伙伴）：升级后新商店伙伴货架边界一致 ----------
def test_pre_companion_save_new_shop_recruit_replays_bit_exact():
    seed, battles, shop_node = _find_battle_shop_path(
        0, min_gold=50, battle_count=2)
    rid = _make_legacy_run(seed, "2.5.0", drop_potions=False, drop_companion=True)
    total_gold = 0
    for b in battles:
        _legacy_commit(rid, {"action": "choose_node", "node": b},
                       "2.5.0", no_potions=False, no_companion=True)
        total_gold += _legacy_win_and_claim_gold(
            rid, "2.5.0", no_potions=False, no_companion=True)
    assert total_gold >= 50

    service.resume(rid)
    entered = service.act(rid, {"action": "choose_node", "node": shop_node})
    offers = entered["run"]["shop"]["companions"]
    assert len(offers) == 1 and offers[0]["companion"] == "squire"
    recruited = service.act(rid, {"action": "shop_buy", "kind": "companion",
                                 "sku": offers[0]["sku"]})
    assert recruited["run"]["companion"]["id"] == "squire"

    rep = service.replay(rid)
    v = rep["verification"]
    assert v["mismatch"] == 0 and v["error"] == 0 and v["final_match"] is True
    boundary = next(s for s in rep["steps"] if s["action"] == "choose_node"
                    and s["view"]["position"] == shop_node)
    buy = next(s for s in rep["steps"] if s["action"] == "shop_buy")
    assert boundary["check"] == "ok"  # 迁移点动作逐位可比，不被 legacy 吞掉
    assert [o["sku"] for o in boundary["view"]["shop"]["companions"]] == \
           [o["sku"] for o in offers]
    assert buy["check"] == "ok"
    assert rep["final_view"]["companion"]["id"] == "squire"


# ---------- 2.3.0 形状（药水/伙伴都缺）：双重迁移边界仍严格一致 ----------
def test_legacy_shape_both_fields_new_shop_potion_replays_consistent():
    cheapest = min(potions_mod.POTION_PRICE.values())
    seed, [battle_node], shop_node = _find_battle_shop_path(
        8000, min_gold=cheapest)
    rid = _make_legacy_run(seed, "2.3.0", drop_potions=True, drop_companion=True)
    _legacy_commit(rid, {"action": "choose_node", "node": battle_node},
                   "2.3.0", no_potions=True, no_companion=True)
    gold = _legacy_win_and_claim_gold(
        rid, "2.3.0", no_potions=True, no_companion=True)

    service.resume(rid)
    entered = service.act(rid, {"action": "choose_node", "node": shop_node})
    shelf = entered["run"]["shop"]["potions"]
    assert len(shelf) >= 1
    offer = min(shelf, key=lambda o: o["price"])
    assert gold >= offer["price"]
    service.act(rid, {"action": "shop_buy", "kind": "potion", "sku": offer["sku"]})

    rep = service.replay(rid)
    v = rep["verification"]
    assert v["mismatch"] == 0 and v["error"] == 0
    buy = next(s for s in rep["steps"] if s["action"] == "shop_buy")
    assert buy["check"] == "ok" and buy["error"] is None
    assert [p["id"] for p in rep["final_view"]["potions"]] == [offer["potion"]]
    assert rep["final_view"]["companion"] is None
