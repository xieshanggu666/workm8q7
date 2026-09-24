"""多人协作远征（规则 2.9.0）：组队 / 角色权限边界 / 共享章节状态 /
队伍奖励 / 失败回退 / 同步结算 / 整程回放。

覆盖：
- 队长建队（入队码 + 入会令牌）、队员凭码加入（满员/重复名/开赛后拒绝）
- 队长开赛前分配战斗位/资源位（非队长 403、队长角色不可改）；人数不足拒开赛
- 开赛与单人远征同路径：共享章节 run、coop 视口携带成员与权限边界
- 权限边界：战斗位只能 play/end_turn/use_potion；资源位只能资源动作；
  越权 403 且零副作用（金币/位置/战斗状态完全不变）；未带身份/他队成员 403
- 共享章节状态：两名成员交替推进同一条动作日志，rev 乐观锁/request_id 幂等
- 队伍奖励：章节首领击败的协作金入共享池（随交接跨章）；个人贡献与
  章节/终胜名义均分进 coop_ledger
- 失败回退：非法交易零副作用；战败整队同步 lost 且只结算一次
- 推进章节仅队长可操作（队员 403），协作金随交接快照带入下一章
- 整程回放：队伍时间线 + 逐章校验点全通过 + 每步带操作者，只读隔离
"""
import pytest

from app import db, service
from app.coop import (CHAPTER_CLEAR_BONUS, COMBAT, FINAL_WIN_BONUS, LEADER,
                      MAX_MEMBERS, MIN_START_MEMBERS, SUPPLY)


# ---------- 组队辅助 ----------
def _make_team(client, captain="阿队长", name=None, seed=21, chapters=2):
    r = client.post("/api/coop/teams", json={
        "captain_name": captain, "name": name, "seed": seed, "chapters": chapters})
    assert r.status_code == 200, r.text
    data = r.json()
    leader = data["me"]
    return data, leader["id"], data["code"]


def _join(client, code, name):
    r = client.post("/api/coop/teams/join", json={"code": code, "member_name": name})
    assert r.status_code == 200, r.text
    data = r.json()
    me = data["me"]
    return data, me["id"]


def _set_role(client, team_id, leader_id, target_id, role, request_id=None):
    body = {"member_id": leader_id, "target_id": target_id, "role": role}
    if request_id:
        body["request_id"] = request_id
    return client.post(f"/api/coop/teams/{team_id}/roles", json=body)


def _start(client, team_id, leader_id, request_id=None):
    body = {"member_id": leader_id}
    if request_id:
        body["request_id"] = request_id
    return client.post(f"/api/coop/teams/{team_id}/start", json=body)


def _team(client, team_id, member_id=None):
    url = f"/api/coop/teams/{team_id}"
    if member_id:
        url += f"?member_id={member_id}"
    return client.get(url)


def _squad(client, seed=21, chapters=2, supply_name="阿资", combat_name="阿战"):
    """组建一支「队长（默认战斗位）+ 资源位 + 战斗位」三人队并开赛。

    返回 dict（含 team/run/run_id/各成员 id）。队长本身两个领域都能操作。
    """
    team, leader_id, code = _make_team(client, seed=seed, chapters=chapters)
    _, supply_id = _join(client, code, supply_name)
    _, combat_id = _join(client, code, combat_name)
    assert _set_role(client, team["id"], leader_id, supply_id, SUPPLY).status_code == 200
    started = _start(client, team["id"], leader_id, request_id="squad-start")
    assert started.status_code == 200, started.text
    s = started.json()
    return {"team_id": team["id"], "leader_id": leader_id,
            "supply_id": supply_id, "combat_id": combat_id,
            "run_id": s["run"]["run_id"], "expedition_id": s["expedition"]["id"],
            "start": s}


# ---------- 大厅与组队 ----------
def test_create_team_returns_code_and_leader_token(client):
    team, leader_id, code = _make_team(client)
    assert code and len(code) == 6
    assert team["status"] == "forming"
    assert team["leader_id"] == leader_id
    assert team["members"][0]["role"] == LEADER
    me = team["me"]
    assert me["id"] == leader_id and me["role"] == LEADER
    # 队长视口携带入会令牌（列表成员不携带）
    assert me["token"] and len(me["token"]) >= 20
    assert team["members"][0]["token"] is None
    # 时间线记录 form
    kinds = [e["kind"] for e in team["events"]]
    assert kinds == ["form"]


def test_join_with_code_and_full_duplicate_rejected(client):
    team, leader_id, code = _make_team(client)
    data, mid = _join(client, code, "老二")
    assert data["status"] == "forming"
    assert any(m["id"] == mid and m["role"] == COMBAT for m in data["members"])
    assert data["me"]["token"]
    # 重复显示名拒绝（409）
    dup = client.post("/api/coop/teams/join", json={"code": code, "member_name": "老二"})
    assert dup.status_code == 409
    # 错误入队码 400
    bad = client.post("/api/coop/teams/join", json={"code": "ZZZZZZ", "member_name": "x"})
    assert bad.status_code == 400
    # 满员（MAX_MEMBERS=4：队长 + 3 队员）
    for nm in ("老三", "老四"):
        assert client.post("/api/coop/teams/join",
                           json={"code": code, "member_name": nm}).status_code == 200
    full = client.post("/api/coop/teams/join",
                       json={"code": code, "member_name": "老五"})
    assert full.status_code == 400
    t = _team(client, team["id"]).json()
    assert len(t["members"]) == MAX_MEMBERS


def test_join_idempotent_same_request_id(client):
    team, _, code = _make_team(client)
    body = {"code": code, "member_name": "网络不稳", "request_id": "join-7"}
    first = client.post("/api/coop/teams/join", json=body)
    assert first.status_code == 200
    dup = client.post("/api/coop/teams/join", json=body)
    assert dup.status_code == 200
    assert dup.json()["duplicate"] is True
    assert dup.json()["me"]["id"] == first.json()["me"]["id"]
    # 只入队一次
    t = _team(client, team["id"]).json()
    assert len(t["members"]) == 2


def test_roles_only_leader_and_started_locked(client):
    team, leader_id, code = _make_team(client)
    _, other_id = _join(client, code, "队员甲")
    # 非队长分配角色 -> 403
    r = _set_role(client, team["id"], other_id, other_id, SUPPLY)
    assert r.status_code == 403
    # 队长不能把自己改成战斗/资源位
    r = _set_role(client, team["id"], leader_id, leader_id, SUPPLY)
    assert r.status_code == 403
    # 非法角色 400
    r = _set_role(client, team["id"], leader_id, other_id, "wizard")
    assert r.status_code == 400
    # 队长合法分配
    assert _set_role(client, team["id"], leader_id, other_id, SUPPLY).status_code == 200
    # 重复同角色 -> 409
    assert _set_role(client, team["id"], leader_id, other_id, SUPPLY).status_code == 409
    # 开赛后角色锁定
    _, combat_id = _join(client, code, "队员乙")
    _start(client, team["id"], leader_id)
    assert _set_role(client, team["id"], leader_id, combat_id, SUPPLY).status_code == 400


def test_start_requires_min_members(client):
    team, leader_id, _ = _make_team(client)
    assert len(team["members"]) == 1
    r = _start(client, team["id"], leader_id)
    assert r.status_code == 400  # 不足 MIN_START_MEMBERS
    # 非队长开赛 403（拉一个队员进来）
    data, code = team["code"] if False else (team, team["code"])
    _, other = _join(client, code, "跟班")
    assert _start(client, team["id"], other).status_code == 403


def test_start_idempotent_and_creates_shared_run(client):
    squad = _squad(client)
    # 同 request_id 重复开赛返回首次响应、不重复开远征（_squad 用 squad-start 开赛）
    r1 = _start(client, squad["team_id"], squad["leader_id"], request_id="squad-start")
    assert r1.status_code == 200
    assert r1.json()["duplicate"] is True
    assert r1.json()["run"]["run_id"] == squad["run_id"]
    # 无令牌再次开赛 -> 409
    assert _start(client, squad["team_id"], squad["leader_id"]).status_code == 409
    # 队伍已绑定远征、状态 started
    t = _team(client, squad["team_id"]).json()
    assert t["status"] == "started" and t["expedition_id"] == squad["expedition_id"]
    # 开赛后加入被拒绝
    r = client.post("/api/coop/teams/join",
                    json={"code": t["code"], "member_name": "迟到"})
    assert r.status_code == 400


# ---------- 权限边界 ----------
def _enter_node(client, run_id, member_id, node):
    return client.post(f"/api/runs/{run_id}/act",
                       json={"action": "choose_node", "node": node,
                             "member_id": member_id})


def _first_encounter_node(client, run_id):
    view = client.get(f"/api/runs/{run_id}/resume").json()
    return next(n["id"] for n in view["reachable"] if n["type"] == "encounter")


def test_combat_member_cannot_move_and_supply_cannot_play(client):
    squad = _squad(client)
    rid = squad["run_id"]
    node = _first_encounter_node(client, rid)
    # 战斗位选节点（资源动作）-> 403
    r = _enter_node(client, rid, squad["combat_id"], node)
    assert r.status_code == 403
    # 未带 member_id -> 403
    r = client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": node})
    assert r.status_code == 403
    # 零副作用：位置仍在 start
    view = client.get(f"/api/runs/{rid}/resume").json()
    assert view["position"] == "start" and view["in_battle"] is False

    # 资源位选节点（合法）-> 进入战斗
    r = _enter_node(client, rid, squad["supply_id"], node)
    assert r.status_code == 200 and r.json()["run"]["in_battle"] is True

    # 资源位打牌（战斗动作）-> 403 且不消耗能量/手牌
    before = client.get(f"/api/runs/{rid}/resume").json()
    energy_before = before["battle"]["energy"]
    hand_size_before = len(before["battle"]["hand"])
    card = before["battle"]["hand"][0]
    uid = card["uid"] if isinstance(card, dict) else card
    r = client.post(f"/api/runs/{rid}/act",
                    json={"action": "play", "card": uid,
                          "member_id": squad["supply_id"]})
    assert r.status_code == 403
    after = client.get(f"/api/runs/{rid}/resume").json()
    assert after["battle"]["energy"] == energy_before
    assert len(after["battle"]["hand"]) == hand_size_before

    # 战斗位打牌合法
    r = client.post(f"/api/runs/{rid}/act",
                    json={"action": "play", "card": uid,
                          "member_id": squad["combat_id"]})
    assert r.status_code == 200


def test_leader_can_do_both_domains(client):
    squad = _squad(client)
    rid = squad["run_id"]
    node = _first_encounter_node(client, rid)
    # 队长选节点（资源）
    r = _enter_node(client, rid, squad["leader_id"], node)
    assert r.status_code == 200
    view = client.get(f"/api/runs/{rid}/resume").json()
    card = view["battle"]["hand"][0]
    uid = card["uid"] if isinstance(card, dict) else card
    # 队长打牌（战斗）
    r = client.post(f"/api/runs/{rid}/act",
                    json={"action": "play", "card": uid,
                          "member_id": squad["leader_id"]})
    assert r.status_code == 200


def test_foreign_member_and_end_state_permissions(client):
    # 两支队伍：A 队成员不能操作 B 队的章节 run
    a = _squad(client, seed=1)
    b = _squad(client, seed=2)
    node = _first_encounter_node(client, a["run_id"])
    r = _enter_node(client, a["run_id"], b["leader_id"], node)
    assert r.status_code == 403
    # 伪造成员 id 同样 403
    r = _enter_node(client, a["run_id"], "m_deadbeef00", node)
    assert r.status_code == 403


def test_supply_resource_action_403_has_zero_side_effects(client):
    """越权必须在任何状态变更之前：战斗位尝试商店购买，金币不被扣。"""
    squad = _squad(client, seed=4, chapters=2)
    rid = squad["run_id"]
    # 直接把存档挪到商店节点（测试便捷），由资源位正常进入
    rec = service.load_run(rid)
    shop_node = next(n for n, nd in rec["map"]["nodes"].items() if nd["type"] == "shop")
    row_above = next(n for n, ns in rec["map"]["nodes"].items()
                     if ns.get("row", -9) == rec["map"]["nodes"][shop_node]["row"] - 1)
    rec["state"]["position"] = row_above
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    assert _enter_node(client, rid, squad["supply_id"], shop_node).status_code == 200
    view = client.get(f"/api/runs/{rid}/resume").json()
    sku = view["shop"]["cards"][0]["sku"]
    gold_before = view["gold"]
    # 战斗位购买 -> 403
    r = client.post(f"/api/runs/{rid}/act",
                    json={"action": "shop_buy", "kind": "card", "sku": sku,
                          "member_id": squad["combat_id"]})
    assert r.status_code == 403
    after = client.get(f"/api/runs/{rid}/resume").json()
    assert after["gold"] == gold_before
    assert after["shop"]["cards"][0]["sold"] is False


# ---------- 共享章节状态 / 同步结算 ----------
def test_members_share_run_and_rev_conflicts(client):
    squad = _squad(client)
    rid = squad["run_id"]
    node = _first_encounter_node(client, rid)
    r = _enter_node(client, rid, squad["supply_id"], node)
    rev = r.json()["rev"]
    # 资源位立刻再选节点（战斗中）本就非法；这里用并发版本号验证：
    # 携带过期 expected_rev 的合法动作 -> 409
    r2 = client.post(f"/api/runs/{rid}/act", json={
        "action": "end_turn", "member_id": squad["combat_id"],
        "expected_rev": rev - 1})
    assert r2.status_code == 409


def test_request_id_idempotent_with_member_actions(client):
    squad = _squad(client)
    rid = squad["run_id"]
    node = _first_encounter_node(client, rid)
    _enter_node(client, rid, squad["supply_id"], node)
    # 资源位丢弃药水的动作不存在（初始背包为空），改用重复 end_turn 幂等：
    # 战斗位用同一 request_id 结束回合两次，第二次返回首次响应
    body = {"action": "end_turn", "member_id": squad["combat_id"],
            "request_id": "turn-1"}
    first = client.post(f"/api/runs/{rid}/act", json=body)
    assert first.status_code == 200
    dup = client.post(f"/api/runs/{rid}/act", json=body)
    assert dup.status_code == 200 and dup.json()["duplicate"] is True
    assert dup.json()["rev"] == first.json()["rev"]


# ---------- 队伍奖励：协作金 / 个人贡献 / 名义均分 ----------
def _goto_boss(client, rid, member_id):
    rec = service.load_run(rid)
    row_last = next(n for n, nd in rec["map"]["nodes"].items() if nd.get("row") == 3)
    rec["state"]["position"] = row_last
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    r = _enter_node(client, rid, member_id, "boss")
    assert r.status_code == 200 and r.json()["run"]["in_battle"] is True


def _boss_kill_by(client, rid, member_id):
    rec = service.load_run(rid)
    rec["state"]["battle"]["entities"]["enemy"]["hp"] = 1
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    view = client.get(f"/api/runs/{rid}/resume",
                      params={"member_id": member_id}).json()
    strike = next(h for h in view["battle"]["hand"]
                  if (h["id"] if isinstance(h, dict) else h) == "strike")
    uid = strike["uid"] if isinstance(strike, dict) else strike
    r = client.post(f"/api/runs/{rid}/act",
                    json={"action": "play", "card": uid, "member_id": member_id})
    assert r.status_code == 200
    return r.json()


def test_chapter_bonus_enters_shared_pool_and_logged(client):
    squad = _squad(client, seed=7, chapters=2)
    rid = squad["run_id"]
    rec = service.load_run(rid)
    rec["state"]["gold"] = 50
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    _goto_boss(client, rid, squad["leader_id"])
    won = _boss_kill_by(client, rid, squad["combat_id"])
    assert won["run"]["status"] == "won"
    # 协作金入共享池（纯推演 -> 视口金币）
    assert won["run"]["gold"] == 50 + CHAPTER_CLEAR_BONUS
    # 动作日志携带协作金事件
    assert any(x.get("coop_chapter_bonus", {}).get("amount") == CHAPTER_CLEAR_BONUS
               for x in won["log"])
    # 交接快照携带协作金
    exp = client.get(f"/api/expeditions/{squad['expedition_id']}").json()
    assert exp["expedition"]["carry"]["gold"] == 50 + CHAPTER_CLEAR_BONUS


def test_ledger_records_contributions_and_chapter_split(client):
    squad = _squad(client, seed=7, chapters=2)
    rid = squad["run_id"]
    # 资源位做一次资源动作（选节点进战斗）
    node = _first_encounter_node(client, rid)
    _enter_node(client, rid, squad["supply_id"], node)
    # 战斗位结束回合（不一定胜利，但只有胜利才记讨伐）——直接造胜
    rec = service.load_run(rid)
    rec["state"]["battle"]["entities"]["enemy"]["hp"] = 1
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    _boss_or_mob_kill = client.post(f"/api/runs/{rid}/act", json={
        "action": "play",
        "card": next(h["uid"] if isinstance(h, dict) else h
                     for h in client.get(f"/api/runs/{rid}/resume").json()["battle"]["hand"]
                     if (h["id"] if isinstance(h, dict) else h) == "strike"),
        "member_id": squad["combat_id"]})
    assert _boss_or_mob_kill.status_code == 200
    # 击败首领通关（资源位推进、战斗位击杀）
    _goto_boss(client, rid, squad["supply_id"])
    _boss_kill_by(client, rid, squad["combat_id"])

    t = _team(client, squad["team_id"]).json()
    led = {m["id"]: m["ledger"] for m in t["members"]}
    # 资源位至少 2 次后勤（选普通节点 + 选首领节点）
    assert led[squad["supply_id"]]["resource_ops"] >= 2
    # 战斗位有战斗胜利计数
    assert led[squad["combat_id"]]["battle_wins"] >= 1
    # 三名在册成员均分章节名义金 15（三人整除，每人 5）
    chapter_total = sum(s["chapter_bonus"] for s in led.values())
    assert chapter_total == CHAPTER_CLEAR_BONUS
    assert all(s["chapter_bonus"] == 5 for s in led.values())
    # 时间线有 chapter_clear
    kinds = [e["kind"] for e in t["events"]]
    assert "chapter_clear" in kinds


def test_final_win_settles_bonus_and_expedition_once(client):
    squad = _squad(client, seed=11, chapters=1)
    rid = squad["run_id"]
    _goto_boss(client, rid, squad["leader_id"])
    won = _boss_kill_by(client, rid, squad["leader_id"])
    assert won["run"]["status"] == "won"
    assert won["run"]["expedition"]["status"] == "won"
    t = _team(client, squad["team_id"]).json()
    led = {m["id"]: m["ledger"] for m in t["members"]}
    # 终章：章节协作金 + 终胜名义金都入账（总和各 15 / 50，三人分）
    assert sum(s["chapter_bonus"] for s in led.values()) == CHAPTER_CLEAR_BONUS
    assert sum(s["win_bonus"] for s in led.values()) == FINAL_WIN_BONUS
    kinds = [e["kind"] for e in t["events"]]
    assert kinds.count("settle") == 1
    # 终局共享池只含协作金（终胜名义金不进 run.gold）
    assert won["run"]["gold"] == CHAPTER_CLEAR_BONUS
    # 已结算：队长再推进 409
    r = client.post(f"/api/coop/teams/{squad['team_id']}/advance",
                    json={"member_id": squad["leader_id"]})
    assert r.status_code == 409


# ---------- 推进章节：仅队长 ----------
def test_only_leader_advances_and_bonus_carries(client):
    squad = _squad(client, seed=5, chapters=2)
    rid = squad["run_id"]
    _goto_boss(client, rid, squad["leader_id"])
    _boss_kill_by(client, rid, squad["combat_id"])
    # 非队长推进 -> 403
    r = client.post(f"/api/coop/teams/{squad['team_id']}/advance",
                    json={"member_id": squad["combat_id"]})
    assert r.status_code == 403
    # 队长推进
    r = client.post(f"/api/coop/teams/{squad['team_id']}/advance",
                    json={"member_id": squad["leader_id"]})
    assert r.status_code == 200, r.text
    run2 = r.json()["run"]
    assert run2["expedition"]["chapter"] == 2
    # 协作金随交接快照带入第 2 章
    assert run2["gold"] == CHAPTER_CLEAR_BONUS
    # 新章仍属于同一队伍、coop 视口在位
    assert run2["coop"]["team_id"] == squad["team_id"]
    assert {m["role"] for m in run2["coop"]["members"]} >= {LEADER, COMBAT, SUPPLY}
    # 新章的权限边界依旧生效（资源位可走节点）
    node = _first_encounter_node(client, run2["run_id"])
    r = _enter_node(client, run2["run_id"], squad["supply_id"], node)
    assert r.status_code == 200


# ---------- 失败回退：战败整队 lost，只结算一次 ----------
def _lose_battle(client, rid, member_id):
    rec = service.load_run(rid)
    rec["state"]["battle"]["entities"]["player"]["hp"] = 1
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    for _ in range(8):
        r = client.post(f"/api/runs/{rid}/act",
                        json={"action": "end_turn", "member_id": member_id})
        assert r.status_code == 200
        if r.json()["run"]["status"] == "lost":
            return r.json()
    raise AssertionError("did not lose")


def test_battle_loss_settles_whole_team_once(client):
    squad = _squad(client, seed=9, chapters=2)
    rid = squad["run_id"]
    node = _first_encounter_node(client, rid)
    _enter_node(client, rid, squad["supply_id"], node)
    lost = _lose_battle(client, rid, squad["combat_id"])
    assert lost["run"]["status"] == "lost"
    assert lost["run"]["expedition"]["status"] == "lost"
    t = _team(client, squad["team_id"]).json()
    assert t["status"] == "started"  # 队伍记录不删除，标记来自远征状态
    kinds = [e["kind"] for e in t["events"]]
    assert kinds.count("settle") == 1
    settle = next(e for e in t["events"] if e["kind"] == "settle")
    assert settle["payload"]["result"] == "lost"
    # 战败后任何成员再提交章节动作 -> 400
    r = client.post(f"/api/runs/{rid}/act",
                    json={"action": "end_turn", "member_id": squad["combat_id"]})
    assert r.status_code == 400
    # 推进被拒（409：远征已结算）
    r = client.post(f"/api/coop/teams/{squad['team_id']}/advance",
                    json={"member_id": squad["leader_id"]})
    assert r.status_code == 409


def test_invalid_business_action_rolls_back_entirely(client):
    """资源位金币不足时锻造/购买：纯推演抛错 -> 事务整体回滚，零副作用。"""
    squad = _squad(client, seed=1, chapters=2)
    rid = squad["run_id"]
    # 挪到锻造节点（资源位进入）
    rec = service.load_run(rid)
    forge = next(n for n, nd in rec["map"]["nodes"].items() if nd["type"] == "forge")
    above = next(n for n, ns in rec["map"]["nodes"].items()
                 if ns.get("row", -9) == rec["map"]["nodes"][forge]["row"] - 1)
    rec["state"]["position"] = above
    rec["state"]["gold"] = 0
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    r = _enter_node(client, rid, squad["supply_id"], forge)
    assert r.status_code == 200
    view = client.get(f"/api/runs/{rid}/resume").json()
    uid = view["deck"][0]["uid"]
    r = client.post(f"/api/runs/{rid}/act", json={
        "action": "forge", "card": uid, "growth_node": "sharpen",
        "member_id": squad["supply_id"]})
    assert r.status_code == 400
    after = client.get(f"/api/runs/{rid}/resume").json()
    assert after["gold"] == 0
    assert after["deck"][0]["growth_nodes"] == []
    assert after["forge_claimed"] is False


# ---------- 整程回放 ----------
_SAFE = {"rest": 0, "reward": 1, "forge": 2, "shop": 3, "encounter": 4,
         "elite": 6, "boss": 7}


def _coop_bot_play(client, rid, combat_id):
    """战斗位在一场战斗里合法打完（返回战斗结果 won/lost/ongoing）。"""
    for _ in range(80):
        view = client.get(f"/api/runs/{rid}/resume",
                          params={"member_id": combat_id}).json()
        if not view["in_battle"]:
            return view["status"]
        b = view["battle"]
        playable = [h for h in b["hand"]
                    if (h.get("cost", 1) if isinstance(h, dict) else 1) <= b["energy"]]
        strike = next((h for h in playable
                       if (h["id"] if isinstance(h, dict) else h) == "strike"), None)
        pick = strike or (playable[0] if playable else None)
        if pick:
            uid = pick["uid"] if isinstance(pick, dict) else pick
            r = client.post(f"/api/runs/{rid}/act",
                            json={"action": "play", "card": uid, "member_id": combat_id})
        else:
            r = client.post(f"/api/runs/{rid}/act",
                            json={"action": "end_turn", "member_id": combat_id})
        assert r.status_code == 200
        data = r.json()
        if data["run"]["status"] in ("won", "lost"):
            return data["run"]["status"]
        if not data["run"]["in_battle"]:
            return "ongoing"
    raise AssertionError("battle did not finish")


def _coop_clear_chapter_legal(client, squad, rid):
    """资源位走路线/领奖，战斗位打每场战斗，直到章节结束（全部合法 API 行动）。"""
    supply_id, combat_id = squad["supply_id"], squad["combat_id"]
    for _ in range(200):
        view = client.get(f"/api/runs/{rid}/resume",
                          params={"member_id": supply_id}).json()
        if view["status"] in ("won", "lost"):
            return view["status"]
        if view["in_battle"]:
            result = _coop_bot_play(client, rid, combat_id)
            if result in ("won", "lost"):
                view = client.get(f"/api/runs/{rid}/resume").json()
                if view["status"] in ("won", "lost"):
                    return view["status"]
            continue
        if not view["reward_claimed"] and view["reward_options"]:
            idx = next((i for i, o in enumerate(view["reward_options"])
                        if any(e.get("type") == "gold" for e in o.get("effects", []))), 0)
            r = client.post(f"/api/runs/{rid}/act", json={
                "action": "claim_reward", "option": idx, "member_id": supply_id})
            assert r.status_code == 200
            continue
        reach = view["reachable"]
        if not reach:
            return view["status"]
        node = sorted(reach, key=lambda n: _SAFE.get(n["type"], 9))[0]["id"]
        r = _enter_node(client, rid, supply_id, node)
        assert r.status_code == 200
        if r.json()["run"]["status"] in ("won", "lost"):
            return r.json()["run"]["status"]
    raise AssertionError("chapter did not finish")


def test_coop_replay_actors_and_checkpoints_isolated(client):
    # 全程合法行动（资源位走路线/领奖、战斗位打牌），回放校验点可逐位验证
    squad = _squad(client, seed=13, chapters=2)
    rid = squad["run_id"]
    assert _coop_clear_chapter_legal(client, squad, rid) == "won"

    r = client.get(f"/api/coop/teams/{squad['team_id']}/replay")
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["isolated"] is True
    # 队伍时间线包含 form/join/role/start/chapter_clear
    kinds = [e["kind"] for e in rep["events"]]
    assert kinds[:3] == ["form", "join", "join"]
    assert "start" in kinds and "chapter_clear" in kinds
    # 逐章回放：校验点全部通过（协作金纯推演与 actor 载荷不影响 run 哈希）
    for ch in rep["expedition_replay"]["chapters"]:
        v = ch["replay"]["verification"]
        assert v["mismatch"] == 0 and v["error"] == 0
        assert v["ok"] >= 1
    # 每步标注操作者；至少能找到资源位与战斗位的动作
    actors = set()
    for ch in rep["expedition_replay"]["chapters"]:
        for step in ch["replay"]["steps"]:
            if step.get("actor"):
                actors.add(step["actor"]["id"])
    assert squad["supply_id"] in actors
    assert squad["combat_id"] in actors
    # 个人流水在回放里可读
    ledger_members = {row["member_id"] for row in rep["ledger"]}
    assert squad["leader_id"] in ledger_members


def test_coop_run_replay_from_carry_keeps_team_marker(client):
    """第 2 章单局回放：初始状态由 carry（含 coop_team）重建，校验点一致。"""
    squad = _squad(client, seed=17, chapters=2)
    rid = squad["run_id"]
    _goto_boss(client, rid, squad["leader_id"])
    _boss_kill_by(client, rid, squad["combat_id"])
    adv = client.post(f"/api/coop/teams/{squad['team_id']}/advance",
                      json={"member_id": squad["leader_id"]}).json()
    rid2 = adv["run"]["run_id"]
    node = _first_encounter_node(client, rid2)
    _enter_node(client, rid2, squad["supply_id"], node)
    client.post(f"/api/runs/{rid2}/act",
                json={"action": "end_turn", "member_id": squad["combat_id"]})
    rep = client.get(f"/api/runs/{rid2}/replay").json()
    v = rep["verification"]
    assert v["mismatch"] == 0 and v["error"] == 0 and v["ok"] >= 2


# ---------- 视口 ----------
def test_run_view_carries_coop_badge_with_permissions(client):
    squad = _squad(client)
    rid = squad["run_id"]
    view = client.get(f"/api/runs/{rid}/resume",
                      params={"member_id": squad["combat_id"]}).json()
    coop = view["coop"]
    assert coop["team_id"] == squad["team_id"]
    assert coop["me"]["id"] == squad["combat_id"] and coop["me"]["role"] == COMBAT
    assert set(coop["permissions"]["battle_actions"]) == {"play", "end_turn", "use_potion"}
    # 续局入口返回队伍 + run
    r = client.get(f"/api/coop/teams/{squad['team_id']}/expedition",
                   params={"member_id": squad["supply_id"]})
    assert r.status_code == 200
    data = r.json()
    assert data["run"]["run_id"] == rid
    assert data["run"]["coop"]["me"]["role"] == SUPPLY


def test_solo_expedition_unaffected_by_member_field(client):
    """单人远征即使带 member_id 也不做协作鉴权（回归保障）。"""
    r = client.post("/api/expeditions", json={"seed": 1, "chapters": 1})
    rid = r.json()["run"]["run_id"]
    node = next(n["id"] for n in r.json()["run"]["reachable"] if n["type"] == "encounter")
    resp = client.post(f"/api/runs/{rid}/act",
                       json={"action": "choose_node", "node": node,
                             "member_id": "m_whatever"})
    assert resp.status_code == 200
    assert resp.json()["run"]["coop"] is None
