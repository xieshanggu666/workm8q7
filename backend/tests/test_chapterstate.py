"""跨章状态统一交接与回放校验策略（规则 2.10.0）。

远征委托 / 奇遇印记 / 药水背包 / 伙伴 / 协作队伍归属五类随交接快照跨章继承
的 run 状态维度，统一由 app/chapterstate.py 注册表驱动：

- 注册表单点登记：维度（key/起始版本/fresh 默认/旧档 normalize/附属字段）
- 交接：fresh_dimension_values / carry_dimension_values / carry_snapshot 对称
- 旧存档迁移：migrate_run_state 幂等补齐所有维度
- 回放：shape_matrix 枚举全部旧 create 形状候选；LegacyWindows 统一跟踪
  「迁移点规则先行切换 + 旧区间 legacy 标记」（结构维 + 纯规则窗口）
- 整程：合法两章流程中委托/药水/伙伴/印记经同一交接快照带入下一章，
  续局、章节结算、整章/整程回放逐位一致；协作远征（多人操作）同路径。
"""
import copy

from app import db, service, chapterstate as cs
from app import companions as companions_mod
from app import encounters as enc_mod

# 与 test_commissions 同款合法机器人（战斗优先打击/商店接委托/奖励优先金币）
_BOT_PREF = {"shop": 0, "rest": 1, "reward": 2, "forge": 3,
             "encounter": 4, "event": 4, "elite": 6, "boss": 7}


def _accept(client, rid, sku, request_id=None):
    body = {"action": "commission_accept", "sku": sku}
    if request_id is not None:
        body["request_id"] = request_id
    return client.post(f"/api/runs/{rid}/act", json=body)


def _bot_clear_chapter(client, rid, cap=300, accept_commissions=True, member_id=None):
    def _act(body):
        if member_id is not None:
            body["member_id"] = member_id
        return client.post(f"/api/runs/{rid}/act", json=body)

    def _accept_now(sku):
        return _accept(client, rid, sku) if member_id is None else \
            client.post(f"/api/runs/{rid}/act",
                        json={"action": "commission_accept", "sku": sku,
                              "member_id": member_id})

    for _ in range(cap):
        suffix = f"?member_id={member_id}" if member_id is not None else ""
        view = client.get(f"/api/runs/{rid}/resume{suffix}").json()
        if view["status"] != "in_progress":
            return view["status"]
        if view["in_battle"]:
            hand = view["battle"]["hand"]
            energy = view["battle"]["energy"]

            def cost(h):
                return h.get("cost", 1) if isinstance(h, dict) else 1

            def cid(h):
                return h["id"] if isinstance(h, dict) else h

            playable = [h for h in hand if cost(h) <= energy]
            pick = next((h for h in playable if cid(h) == "strike"),
                        playable[0] if playable else None)
            if pick:
                body = {"action": "play",
                        "card": pick["uid"] if isinstance(pick, dict) else pick}
            else:
                body = {"action": "end_turn"}
            r = _act(body)
            assert r.status_code == 200, r.text
            continue
        if accept_commissions and view.get("shop_available") and view["shop"]:
            for o in view["shop"]["commissions"]:
                assert _accept_now(o["sku"]).status_code == 200
        if not view["reward_claimed"] and view["reward_options"]:
            idx = next((i for i, o in enumerate(view["reward_options"])
                        if o.get("kind") == "gold"), 0)
            r = _act({"action": "claim_reward", "option": idx})
            assert r.status_code == 200, r.text
            continue
        reach = view["reachable"]
        if not reach:
            return view["status"]
        node = sorted(reach, key=lambda n: _BOT_PREF.get(n["type"], 9))[0]
        r = _act({"action": "choose_node", "node": node["id"]})
        assert r.status_code == 200, r.text
    raise AssertionError("chapter did not finish in time")


# ------------------------------------------------------------------
# 注册表：单点登记
# ------------------------------------------------------------------
def test_registry_lists_all_cross_chapter_dimensions():
    keys = [d.key for d in cs.DIMENSIONS]
    assert keys == ["commissions", "potions", "companion", "enc_state", "coop_team"]
    # 起始版本单调不减（注册顺序即规则引入顺序）
    versions = [d.version for d in cs.DIMENSIONS]
    assert versions == sorted(versions)
    # 每个维度都能独立给出 fresh 默认值（可变值两次调用不共享引用；None 除外）
    for d in cs.DIMENSIONS:
        a, b = d.fresh_value(), d.fresh_value()
        assert a == b
        if a is not None:
            assert a is not b
    # 委托维度带 uid 发号器附属字段
    commission_dim = cs.DIMENSIONS_BY_KEY["commissions"]
    assert commission_dim.auxiliary_keys == ("next_commission_seq",)


def test_fresh_and_carry_values_are_symmetric_and_independent():
    fresh = cs.fresh_dimension_values()
    assert set(fresh) == {d.key for d in cs.DIMENSIONS}
    assert fresh["potions"] == [] and fresh["companion"] is None
    assert fresh["coop_team"] is None and fresh["commissions"] == []
    assert fresh["enc_state"] == enc_mod.fresh_state()
    assert cs.fresh_auxiliary_values() == {"next_commission_seq": 1}

    # 交接快照 -> 取值必须是深拷贝（改快照不影响取出的值，反之亦然）
    state = service._new_run_state(123)
    state["potions"].append("hp")
    state["commissions"].append({"id": "q1"})
    carry = cs.carry_snapshot(state)
    values = cs.carry_dimension_values(carry)
    values["potions"].append("fire")
    assert carry["potions"] == ["hp"]
    assert cs.carry_auxiliary_values(carry) == {"next_commission_seq": 1}
    # 缺字段的旧版快照按各维度兜底
    old_carry = {k: v for k, v in carry.items()
                 if k not in ("potions", "companion", "enc_state", "coop_team")}
    old_values = cs.carry_dimension_values(old_carry)
    assert old_values["potions"] == [] and old_values["companion"] is None
    assert old_values["coop_team"] is None
    assert old_values["enc_state"] == enc_mod.fresh_state()


def test_carry_snapshot_is_single_source_of_handover_fields():
    state = service._new_run_state(9)
    state["potions"] = ["hp", "fire"]
    state["companion"] = companions_mod.make_companion()
    state["coop_team"] = "t_abc"
    state["commissions"] = [{"id": "q1", "status": "active"}]
    state["next_commission_seq"] = 4
    carry = cs.carry_snapshot(state)
    # 核心字段 + 每个注册表维度 + 附属发号器都在快照里
    for key in cs.CARRY_CORE_KEYS:
        assert key in carry
    for d in cs.DIMENSIONS:
        assert d.key in carry
    assert carry["next_commission_seq"] == 4
    # _carry_from_run 与注册表同一张清单
    assert set(service._carry_from_run(state)) == set(carry)
    # 深拷贝隔离：改快照不改活状态
    carry["potions"].append("energy")
    assert state["potions"] == ["hp", "fire"]


# ------------------------------------------------------------------
# 旧存档迁移
# ------------------------------------------------------------------
def test_migrate_run_state_backfills_all_dimensions_idempotently():
    # 模拟最旧的存档：五个维度字段全缺
    run = {"deck": ["strike"], "card_instances": {}, "relics": {}, "gold": 0}
    assert cs.migrate_run_state(run) is True
    assert run["potions"] == []
    assert run["companion"] is None
    assert run["coop_team"] is None
    assert run["commissions"] == []
    assert run["next_commission_seq"] == 1
    assert run["enc_state"] == enc_mod.fresh_state()
    # 再迁一次：幂等，无变化
    assert cs.migrate_run_state(run) is False


def test_migrate_normalizes_damaged_companion_and_enc_state():
    run = {"deck": [], "card_instances": {}, "relics": {}, "gold": 0,
           # 损坏的伙伴（缺字段 + 越界生命）与损坏的 enc_state
           "companion": {"id": "squire", "hp": 999},
           "enc_state": "corrupt"}
    changed = cs.migrate_run_state(run)
    assert changed is True
    comp = run["companion"]
    assert comp["hp"] == companions_mod.SQUIRE["max_health"]
    assert comp["mode"] == companions_mod.ACCOMPANY
    assert run["enc_state"] == enc_mod.fresh_state()
    # 在线迁移路径：旧档首次续局后补齐且可继续行动
    rid = service.create_run(seed=5)["run_id"]
    rec = service.load_run(rid)
    for key in ("commissions", "potions", "companion", "enc_state", "coop_team",
                "next_commission_seq"):
        rec["state"].pop(key, None)
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    view = service.resume(rid)
    assert view["potions"] == [] and view["companion"] is None
    assert view["commissions"] == [] and view["encounter_flags"] == []


# ------------------------------------------------------------------
# 回放形状矩阵
# ------------------------------------------------------------------
def test_shape_matrix_enumerates_all_dimension_combinations_full_first():
    matrix = cs.shape_matrix()
    # 仅 4 个形状维枚举（companion/potions/enc_state/coop_team），
    # commissions 从远征上线起就在 create 哈希里，不剥维（保持历史 16 形状）。
    shape_dims = cs.SHAPE_DIMENSIONS
    assert [d.key for d in shape_dims] == \
        ["potions", "companion", "enc_state", "coop_team"]
    assert len(matrix) == 2 ** len(shape_dims) == 16
    name0, included0 = matrix[0]
    assert name0 == "full" and all(included0.values())
    # 每一行都包含全部维度键，且 commissions 恒参与
    for _name, included in matrix:
        assert set(included) == {d.key for d in cs.DIMENSIONS}
        assert included["commissions"] is True
    names = [name for name, _ in matrix]
    assert len(set(names)) == 16
    # 末行：四个形状维全剥，commissions 仍在
    assert matrix[-1][1]["commissions"] is True
    assert {k for k, v in matrix[-1][1].items() if not v} == \
        {"potions", "companion", "enc_state", "coop_team"}
    # 校验点：剥掉 potions 的形状等于历史 include_potions=False 兼容哈希
    state = service._new_run_state(321)
    for _name, included in matrix:
        assert len(service.state_checkpoint(
            state, include_dimensions=included)) == 16
    no_potions = next(inc for _, inc in matrix if not inc["potions"]
                      and all(inc[k] for k in inc if k != "potions"))
    assert service.state_checkpoint(state, include_dimensions=no_potions) == \
        service.state_checkpoint(state, include_potions=False)


def test_checkpoint_legacy_keyword_compat():
    state = service._new_run_state(77)
    # 旧式四个关键字仍可用（其余维度按完整形状参与）
    h = service.state_checkpoint(state, include_coop=False, include_encounters=False)
    matrix_h = service.state_checkpoint(state, include_dimensions={
        d.key: d.key not in ("coop_team", "enc_state") for d in cs.DIMENSIONS})
    assert h == matrix_h


# ------------------------------------------------------------------
# LegacyWindows：结构维 + 纯规则窗口统一切换
# ------------------------------------------------------------------
def test_legacy_windows_structure_dimensions_cross_before_action():
    # create 形状缺 potions/companion（2.6.0 之前）：窗口内按 legacy
    included = {d.key: True for d in cs.DIMENSIONS}
    included["potions"] = included["companion"] = False
    w = cs.LegacyWindows(included)
    sim = {"potions": [], "companion": None}
    w.enter_old_window(sim)
    assert sim["_legacy_no_potions"] is True and sim["_legacy_no_companion"] is True
    assert sim["_legacy_block"] is True  # 纯规则窗口默认从旧时序开始
    assert w.is_pre("potions", "choose_node") and w.legacy_step("choose_node")
    # 2.5.0 动作先越过药水维（规则先行切换：抑制标记摘除、字段补齐）
    dims, rules = w.apply_crossings(sim, "shop_buy", "2.5.0")
    assert dims == ["potions"] and rules == []
    assert "_legacy_no_potions" not in sim and sim["potions"] == []
    assert w.is_pre("companion", "shop_buy") and not w.is_pre("potions", "shop_buy")
    # 2.7.0 动作同时越过伙伴维与格挡纯规则窗口
    dims, rules = w.apply_crossings(sim, "end_turn", "2.7.0")
    assert set(dims) == {"companion"} and rules == ["_legacy_block"]
    assert not w.legacy_step("end_turn") and not w.rule_pre_step("end_turn")
    # 越过之后不再重复切换
    assert w.apply_crossings(sim, "play", "2.10.0") == ([], [])


def test_legacy_windows_close_all_clears_every_remaining_window():
    # 最旧 create：四个形状维全缺（commissions 不在形状矩阵中，恒在场）
    included = {d.key: (not d.in_shape_matrix) for d in cs.DIMENSIONS}
    w = cs.LegacyWindows(included)
    sim = {}
    w.enter_old_window(sim)
    assert w.legacy_step("play") and w.rule_pre_step("play")
    w.close_all(sim)
    assert not w.legacy_step("play") and not w.rule_pre_step("play")
    # 全部形状维字段都已补齐为 fresh
    assert sim["potions"] == [] and sim["companion"] is None
    assert sim["coop_team"] is None and sim["enc_state"] == enc_mod.fresh_state()


def test_legacy_windows_step_pre_version_uses_recorded_version():
    # create 含字段（present=True），但本步录制版本仍早于维度版本时按旧规则
    included = {d.key: True for d in cs.DIMENSIONS}
    w = cs.LegacyWindows(included)
    sim = {}
    w.enter_old_window(sim)
    # 2.4.0 动作：结构已在场、但药水货架规则尚未上线 -> step_pre_version True
    assert w.step_pre_version("potions", "2.4.0") is True
    assert w.step_pre_version("companion", "2.4.0") is True
    assert w.step_pre_version("potions", "2.5.0") is False
    # create 已含该字段（present=True -> 窗口起点即 crossed）：无版本号日志
    # 也按新结构处理（只有 create 缺字段的旧档才在越过窗口前按旧规则）
    assert w.step_pre_version("potions", None) is False


# ------------------------------------------------------------------
# 整程：交接 / 续局 / 结算 / 回放一致（含多人操作）
# ------------------------------------------------------------------
def test_two_chapter_handover_replay_matches_online(client):
    """合法两章全胜：五类跨章状态经同一快照交接，整章/整程回放零 mismatch。"""
    exp_id = client.post("/api/expeditions",
                         json={"seed": 2, "chapters": 2}).json()["expedition"]["id"]
    rid = client.get(f"/api/expeditions/{exp_id}").json()["run"]["run_id"]
    assert _bot_clear_chapter(client, rid) == "won"

    adv = client.post(f"/api/expeditions/{exp_id}/advance", json={}).json()
    rid2 = adv["run"]["run_id"]
    # 交接快照是 carry_snapshot 的产物：五类维度键齐全
    carry_event = next(e for e in db.load_expedition_events(exp_id)
                       if e["kind"] == "advance")["payload"]["carry"]
    for d in cs.DIMENSIONS:
        assert d.key in carry_event

    online_ch2 = client.get(f"/api/runs/{rid2}/resume").json()
    assert _bot_clear_chapter(client, rid2) == "won"

    rep = client.get(f"/api/expeditions/{exp_id}/replay").json()
    assert rep["isolated"] is True
    assert [e["kind"] for e in rep["events"]] == \
        ["create", "chapter_clear", "advance", "settle"]
    for ch in rep["chapters"]:
        vv = ch["replay"]["verification"]
        assert (vv["mismatch"], vv["error"]) == (0, 0)
        assert vv["ok"] >= 10
    # 第 2 章回放首帧（从交接快照重建）与在线开章视口逐位一致
    first = rep["chapters"][1]["replay"]["steps"][0]["view"]
    for field in ("potions", "companion", "commissions", "encounter_flags",
                  "gold", "relics", "health"):
        assert first[field] == online_ch2[field], field


def test_handover_snapshot_roundtrip_through_new_run_state():
    """carry_snapshot -> _new_run_state(carry=...) 往返：各维度值保持/入章结算正确。"""
    state = service._new_run_state(service._chapter_seed(55, 1), chapter=1,
                                   chapters_total=3, expedition_id="e1")
    state["potions"] = ["block", "energy"]
    state["companion"] = companions_mod.make_companion()
    state["commissions"] = [{"id": "q1", "status": "ready"}]
    state["enc_state"]["flags"] = {"altar_blessed": 1, "wt_aided": 1}
    carry = cs.carry_snapshot(state)
    nxt = service._new_run_state(service._chapter_seed(55, 2), carry=carry,
                                 chapter=2, chapters_total=3, expedition_id="e1")
    # 道具/伙伴/委托原样带入
    assert nxt["potions"] == ["block", "energy"]
    assert nxt["companion"]["id"] == "squire"
    assert [c["id"] for c in nxt["commissions"]] == ["q1"]
    # 印记开章结算：无续写链等待的 altar_blessed 兑现赐福后移除（记入 opened），
    # 续写 flag（wt_aided）保留到后续章的续写节点
    assert "altar_blessed" not in nxt["enc_state"]["flags"]
    assert nxt["enc_state"]["opened"].get("altar_blessed") == 2
    assert nxt["enc_state"]["flags"].get("wt_aided") == 1
    assert nxt["enc_state"]["resolved_nodes"] == []
    # 新 run 身份只认真实入参，不被 carry 的来源章号污染（2.4.0 回归）
    assert nxt["chapter"] == 2 and nxt["expedition_id"] == "e1"


def _bot_clear_chapter_multi(client, rid, combat_id, supply_id, cap=300):
    """协作远征机器人：战斗动作由战斗位提交、资源动作由资源位提交（多人操作同一条日志）。"""
    for _ in range(cap):
        view = client.get(
            f"/api/runs/{rid}/resume?member_id={combat_id}").json()
        if view["status"] != "in_progress":
            return view["status"]
        if view["in_battle"]:
            actor = combat_id
            hand = view["battle"]["hand"]
            energy = view["battle"]["energy"]

            def cost(h):
                return h.get("cost", 1) if isinstance(h, dict) else 1

            def cid(h):
                return h["id"] if isinstance(h, dict) else h

            playable = [h for h in hand if cost(h) <= energy]
            pick = next((h for h in playable if cid(h) == "strike"),
                        playable[0] if playable else None)
            body = {"action": "play",
                    "card": pick["uid"] if isinstance(pick, dict) else pick} \
                if pick else {"action": "end_turn"}
        else:
            actor = supply_id
            if view.get("shop_available") and view["shop"]:
                for o in view["shop"]["commissions"]:
                    r = client.post(f"/api/runs/{rid}/act", json={
                        "action": "commission_accept", "sku": o["sku"],
                        "member_id": supply_id})
                    assert r.status_code == 200, r.text
            if not view["reward_claimed"] and view["reward_options"]:
                idx = next((i for i, o in enumerate(view["reward_options"])
                            if o.get("kind") == "gold"), 0)
                body = {"action": "claim_reward", "option": idx}
            else:
                reach = view["reachable"]
                if not reach:
                    return view["status"]
                node = sorted(reach, key=lambda n: _BOT_PREF.get(n["type"], 9))[0]
                body = {"action": "choose_node", "node": node["id"]}
        body["member_id"] = actor
        r = client.post(f"/api/runs/{rid}/act", json=body)
        assert r.status_code == 200, r.text
    raise AssertionError("chapter did not finish in time")


def test_coop_handover_uses_same_registry_path_with_actor(client):
    """多人协作远征：战斗位/资源位分别操作同一条日志，协作金/各维度随统一快照跨章，
    整程回放零 mismatch 且每步带操作者；越权 403 零副作用。"""
    # 队长 + 资源位 + 战斗位
    team = client.post("/api/coop/teams",
                       json={"captain_name": "队长", "seed": 2, "chapters": 2}).json()
    leader_id = team["me"]["id"]; code = team["code"]; team_id = team["id"]
    supply_id = client.post("/api/coop/teams/join",
                            json={"code": code, "member_name": "阿资"}).json()["me"]["id"]
    combat_id = client.post("/api/coop/teams/join",
                            json={"code": code, "member_name": "阿战"}).json()["me"]["id"]
    assert client.post(f"/api/coop/teams/{team_id}/roles",
                       json={"member_id": leader_id, "target_id": supply_id,
                             "role": "supply"}).status_code == 200
    started = client.post(f"/api/coop/teams/{team_id}/start",
                          json={"member_id": leader_id}).json()
    rid = started["run"]["run_id"]; exp_id = started["expedition"]["id"]

    # 权限边界：战斗位提交资源动作 403、资源位提交战斗动作 403（发生在状态变更前）
    def _reach_enemy():
        v = client.get(f"/api/runs/{rid}/resume?member_id={supply_id}").json()
        return next((n["id"] for n in v["reachable"]
                     if n["type"] in ("encounter", "elite")), None)
    enemy_node = _reach_enemy()
    if enemy_node is not None:
        gold_before = client.get(f"/api/runs/{rid}/resume").json()["gold"]
        denied = client.post(f"/api/runs/{rid}/act", json={
            "action": "choose_node", "node": enemy_node, "member_id": combat_id})
        assert denied.status_code == 403
        # 资源位选路合法
        ok = client.post(f"/api/runs/{rid}/act", json={
            "action": "choose_node", "node": enemy_node, "member_id": supply_id})
        assert ok.status_code == 200
        # 资源位打牌 403（战斗已开始）
        v = client.get(f"/api/runs/{rid}/resume?member_id={supply_id}").json()
        if v["in_battle"] and v["battle"]["hand"]:
            denied2 = client.post(f"/api/runs/{rid}/act", json={
                "action": "play", "card": v["battle"]["hand"][0]["uid"],
                "member_id": supply_id})
            assert denied2.status_code == 403
        assert client.get(f"/api/runs/{rid}/resume").json()["gold"] == gold_before

    # 两章由战斗位/资源位分别操作推进（多人操作同一条确定性日志）
    assert _bot_clear_chapter_multi(
        client, rid, combat_id, supply_id) == "won"
    adv = client.post(f"/api/coop/teams/{team_id}/advance",
                      json={"member_id": leader_id}).json()
    rid2 = adv["run"]["run_id"]
    carry = next(e for e in db.load_expedition_events(exp_id)
                 if e["kind"] == "advance")["payload"]["carry"]
    assert carry["coop_team"] == team_id
    assert _bot_clear_chapter_multi(
        client, rid2, combat_id, supply_id) == "won"

    rep = client.get(f"/api/coop/teams/{team_id}/replay").json()
    assert rep["isolated"] is True
    for ch in rep["expedition_replay"]["chapters"]:
        vv = ch["replay"]["verification"]
        assert (vv["mismatch"], vv["error"]) == (0, 0)
    # 每步都标注操作者；战斗步骤操作者是战斗位、资源步骤是资源位
    acted = [(s["action"], (s.get("actor") or {}).get("id"))
             for ch in rep["expedition_replay"]["chapters"]
             for s in ch["replay"]["steps"]
             if s["action"] not in ("create",)]
    battle_actors = {a for act, a in acted if act in ("play", "end_turn", "use_potion")}
    resource_acts = {act for act, a in acted
                     if act in ("choose_node", "claim_reward", "commission_accept")}
    assert battle_actors == {combat_id}
    assert resource_acts  # 确有资源动作
    # 协作金入共享池并随交接跨章（第 1 章通关 +15）
    ledger = {(e["kind"], e.get("member_id")) for e in rep["ledger"]}
    assert ("chapter", combat_id) in ledger or ("chapter", supply_id) in ledger
