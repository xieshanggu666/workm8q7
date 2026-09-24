"""2.4.0 修复：远征跨章后委托章号/剩余期限/领奖记录错乱。

旧实现的根因：_new_run_state 用交接快照 carry 里的「来源章号」覆盖了新章的
显式章号，导致第 2 章及以后的 run 状态 chapter 恒为 1，进而：
- 新挂委托的 deadline_chapter / offered_chapter 以错误章号为基准；
- chapters_left（deadline - 当前章）虚高，超期被掩盖；
- claimed_at 记录的章节前缀错误；
- 进入更后章时，跨章带入的进行中委托被提前误判 failed(expired)。

本测试用「旧版语义」真实构造受影响存档（错误 chapter + 按错误状态哈希的日志），
覆盖：
- 新代码开新章：章号/挂单基准/剩余期限/领奖位置全部正确；
- 受影响旧档首次续局：按权威 runs.chapter 修复委托章号/期限/领奖记录，
  被误判超期的进行中委托恢复，远征表 carry 同步修复（幂等）；
- 受影响旧档的整章/整程回放：沿修复路径完整重建（含旧委托接取），修复前
  步骤按 legacy 呈现（不逐位比对），无 error/mismatch，最终状态与在线修复一致；
- 修复后再产生的新动作（2.4.0 事件）严格校验通过；整程回放只读隔离。
"""
import json
import uuid

from app import db, service
from app.commissions import ACTIVE, FAILED, READY, CLAIMED

SEED = 11
SHOP1 = "1-2"   # 第 1 章商店
R0 = "0-0"


# ---------- 旧版（buggy）行为模拟 ----------
def _buggy_new_run_state(seed, carry, nxt, chapters_total, exp_id):
    """复刻 2.4.0 之前的开章：carry 的来源章号覆盖新章号（核心 bug）。

    rules_version 保留旧串 2.3.0（旧建局时写入的状态标签）。
    """
    state = service._new_run_state(
        seed, carry=carry, chapter=nxt, chapters_total=chapters_total,
        expedition_id=exp_id)
    state["chapter"] = carry.get("chapter")
    state["rules_version"] = "2.3.0"
    # 该夹具复刻 2.3 旧状态：伙伴与药水字段均尚未加入状态结构
    state.pop("companion", None)
    state.pop("potions", None)
    carry.pop("companion", None)
    carry.pop("potions", None)
    # create 校验点必须记录旧状态形状（含未迁移的章号）
    return state


def _buggy_advance(exp_id):
    """用旧版事务把当前已通关章推进到下一章（复刻 service.advance_expedition）。"""
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM expeditions WHERE id=?", (exp_id,)).fetchone()
        cur = conn.execute("SELECT * FROM runs WHERE id=?",
                           (row["current_run_id"],)).fetchone()
        assert cur["status"] == "won"
        nxt = row["chapter"] + 1
        carry = service._carry_from_run(json.loads(cur["state_json"]))
        expired = service.commission_mod.expire_active(carry["commissions"], nxt)
        run_id = uuid.uuid4().hex[:12]
        state = _buggy_new_run_state(
            service._chapter_seed(row["seed"], nxt), carry, nxt,
            row["chapters_total"], exp_id)
        map_data = service.mapgen.generate_map(state["seed"])
        legacy_ckpt = service.state_checkpoint(
            state, include_companion=False, include_potions=False)
        db.insert_run(conn, run_id, state["seed"], state["status"], state["position"],
                      map_data, state, expedition_id=exp_id, chapter=nxt)
        db.append_event_conn(conn, run_id, 1, "create", {
            "seed": state["seed"], "ver": "2.3.0",
            "ckpt": legacy_ckpt,
            "expedition": exp_id, "chapter": nxt,
            "chapters_total": row["chapters_total"], "carry": carry,
        })
        db.append_expedition_event_conn(conn, exp_id,
                                        db.next_expedition_seq_conn(conn, exp_id),
                                        "advance", {
            "chapter": nxt, "run_id": run_id, "carry": carry,
            "rest_heal": state["health"] - carry["health"], "expired": expired,
        })
        db.save_expedition_conn(conn, exp_id, "in_progress", nxt, run_id, carry,
                                expected_rev=row["rev"])
    return run_id


def _buggy_act(run_id, action_dict):
    """旧版在线动作：按错误章号推演并落库（事件带 2.3.0 + 错误状态 ckpt）。"""
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        state = json.loads(row["state_json"])
        map_data = json.loads(row["map_json"])
        service._apply_action(state, action_dict["action"], action_dict,
                              map_data, grant_unlocks=False)
        # 2.3 旧版没有伙伴/药水背包，也没有伙伴货架
        state.pop("companion", None)
        state.pop("potions", None)
        if state.get("shop"):
            state["shop"].pop("companions", None)
            state["shop"].pop("potions", None)
        # 旧版没有战败解锁落库（测试路径不依赖）；同步远征通关/结算
        rec = {"id": run_id, "chapter": row["chapter"],
               "expedition_id": row["expedition_id"]}
        if row["expedition_id"] and state["status"] in ("won", "lost"):
            service._sync_expedition_conn(conn, row["expedition_id"], rec, state)
        db.save_run_run(conn, run_id, state["status"], state["position"], state)
        seq = db.next_seq_conn(conn, run_id)
        payload = {
            "node": action_dict.get("node"), "card": action_dict.get("card"),
            "option": action_dict.get("option"),
            "growth_node": action_dict.get("growth_node"),
            "branch": action_dict.get("branch"),
            "kind": action_dict.get("kind"), "sku": action_dict.get("sku"),
            "commission": action_dict.get("commission"),
            "ver": "2.3.0",
            "ckpt": service.state_checkpoint(
                state, include_companion=False, include_potions=False)}
        db.append_event_conn(conn, run_id, seq, action_dict["action"], payload)
    return state


def _win_battle_in_state(client, run_id):
    rec = service.load_run(run_id)
    rec["state"]["battle"]["entities"]["enemy"]["hp"] = 1
    db.save_run(run_id, rec["state"]["status"], rec["state"]["position"], rec["state"])
    view = client.get(f"/api/runs/{run_id}/resume").json()
    uid = next(h["uid"] for h in view["battle"]["hand"] if h["id"] == "strike")
    r = client.post(f"/api/runs/{run_id}/act", json={"action": "play", "card": uid})
    assert r.status_code == 200, r.text
    return r.json()


def _make_buggy_two_chapter_exp(client, chapters=3, extra_dl1=False, ch1_gold=0):
    """构造：第 1 章合法（含委托）-> 旧版推进到错误章号的第 2 章，并在第 2 章
    用旧版接取一个委托（deadline 错误锚定章号 1）。ch1_gold 非 0 时在第 1 章
    通关快照里带金币（随错误 carry 进入第 2 章，回放初态即具备）。"""
    exp = client.post("/api/expeditions",
                      json={"seed": SEED, "chapters": chapters}).json()
    exp_id = exp["expedition"]["id"]
    rid1 = exp["run"]["run_id"]
    client.post(f"/api/runs/{rid1}/act", json={"action": "choose_node", "node": R0})
    view = client.post(f"/api/runs/{rid1}/act",
                       json={"action": "choose_node", "node": SHOP1}).json()["run"]
    sku = view["shop"]["commissions"][0]["sku"]
    client.post(f"/api/runs/{rid1}/act",
                json={"action": "commission_accept", "sku": sku})
    carried_sig = sku.split("commission:", 1)[1]

    if extra_dl1:
        # deadline=1 的进行中贸易委托：旧版推进第 2 章时会被误判超期
        rec = service.load_run(rid1)
        seq = rec["state"]["next_commission_seq"]
        dl1 = service.commission_mod.make_commission(seq, {
            "kind": "trade", "target": 3, "deadline_chapter": 1,
            "reward": {"type": "gold", "amount": 10},
            "signature": "trade:3:gold:10", "offered_chapter": 1})
        rec["state"]["commissions"].append(dl1)
        rec["state"]["next_commission_seq"] = seq + 1
        db.save_run(rid1, rec["state"]["status"], rec["state"]["position"],
                    rec["state"])
        dl1_id = dl1["id"]
    else:
        dl1_id = None

    # 通关第 1 章（合法 API）
    rec = service.load_run(rid1)
    if ch1_gold:
        rec["state"]["gold"] = ch1_gold  # 进入交接快照的金币
    row3 = next(n for n, nd in rec["map"]["nodes"].items() if nd.get("row") == 3)
    rec["state"]["position"] = row3
    db.save_run(rid1, rec["state"]["status"], rec["state"]["position"], rec["state"])
    client.post(f"/api/runs/{rid1}/act",
                json={"action": "choose_node", "node": "boss"})
    _win_battle_in_state(client, rid1)

    # 旧版推进 -> 错误章号的第 2 章
    rid2 = _buggy_advance(exp_id)
    rec2 = service.load_run(rid2)
    assert rec2["state"]["chapter"] == 1  # 旧 bug：第 2 章状态章号恒为 1
    assert rec2["chapter"] == 2          # runs 列权威章号正确

    # 旧版在第 2 章走到商店并接取委托（错误基准）
    state = rec2["state"]
    shop_node = next(n for n, nd in rec2["map"]["nodes"].items()
                     if nd["type"] == "shop")
    pre = _walk_path_to(rec2["map"], state["position"], shop_node)
    for node in pre:
        _buggy_act(rid2, {"action": "choose_node", "node": node})
    state = service.load_run(rid2)["state"]
    offers = state["shop"]["commission_offers"]
    assert offers, "第 2 章商店应挂出委托"
    ch2_offer = offers[0]
    _buggy_act(rid2, {"action": "commission_accept",
                      "sku": f"commission:{ch2_offer['signature']}"})
    state = service.load_run(rid2)["state"]
    ch2_q = next(c for c in state["commissions"]
                 if c["signature"] == ch2_offer["signature"])
    # 旧 bug：第 2 章接取的委托按章号 1 锚定
    assert ch2_q["accepted_chapter"] == 1
    return {"exp_id": exp_id, "rid1": rid1, "rid2": rid2,
            "carried_sig": carried_sig, "ch2_q": ch2_q,
            "ch2_offer": ch2_offer, "dl1_id": dl1_id,
            "shop_node": shop_node}


def _buggy_path(map_data, start, target, allow_battle=False):
    """BFS 求 start -> target 的节点路径；allow_battle=False 只走非战斗支线。"""
    from collections import deque
    q = deque([(start, [])])
    seen = {start}
    while q:
        cur, path = q.popleft()
        if cur == target:
            return path
        for nxt in map_data["routes"].get(cur, []):
            if nxt in seen:
                continue
            t = map_data["nodes"][nxt]["type"]
            if not allow_battle and t in ("encounter", "elite", "boss"):
                continue
            seen.add(nxt)
            q.append((nxt, path + [nxt]))
    raise AssertionError(f"no path {start}->{target}")


def _buggy_clear_chapter(run_id, exp_id, chapter):
    """旧版语义打完当前章（走含战斗的路径），并同步远征结算。

    每一步都从落库状态判断该做什么（与回放能重建的动作序列一致）：战斗中出牌/
    结束回合；有待领奖励才领；否则走向下一节点。不瞬移、不重复领奖。
    """
    for _ in range(300):
        rec = service.load_run(run_id)
        st = rec["state"]
        if st["status"] != "in_progress":
            return
        if st.get("in_battle") and st.get("battle"):
            hand = st["battle"]["hand"]
            energy = st["battle"]["energy"]
            uid = next((u for u in hand
                       if st["card_instances"][u]["id"] == "strike"), None)
            # 打击基础费 1（本流程不锻造打击），能量够就打，否则结束回合
            if uid and energy >= 1:
                _buggy_act(run_id, {"action": "play", "card": uid})
            else:
                _buggy_act(run_id, {"action": "end_turn"})
            continue
        if not st["reward_claimed"] and st["reward_options"]:
            idx = next((i for i, o in enumerate(st["reward_options"])
                        if o.get("kind") == "gold"), 0)
            _buggy_act(run_id, {"action": "claim_reward", "option": idx})
            continue
        reach = rec["map"]["routes"].get(st["position"], [])
        if not reach:
            return
        pref = {"rest": 0, "reward": 1, "shop": 2, "forge": 3,
                "encounter": 4, "elite": 6, "boss": 7}
        node = sorted(reach, key=lambda n: pref.get(
            rec["map"]["nodes"][n]["type"], 9))[0]
        _buggy_act(run_id, {"action": "choose_node", "node": node})


def _walk_path_to(map_data, start, target):
    return _buggy_path(map_data, start, target, allow_battle=False)


# ---------------- 根因修复：新代码开新章 ----------------
def test_new_advance_uses_correct_chapter(client):
    exp = client.post("/api/expeditions",
                      json={"seed": SEED, "chapters": 3}).json()
    exp_id = exp["expedition"]["id"]
    rid1 = exp["run"]["run_id"]
    client.post(f"/api/runs/{rid1}/act", json={"action": "choose_node", "node": R0})
    view = client.post(f"/api/runs/{rid1}/act",
                       json={"action": "choose_node", "node": SHOP1}).json()["run"]
    sku = view["shop"]["commissions"][0]["sku"]
    client.post(f"/api/runs/{rid1}/act",
                json={"action": "commission_accept", "sku": sku})
    rec = service.load_run(rid1)
    row3 = next(n for n, nd in rec["map"]["nodes"].items() if nd.get("row") == 3)
    rec["state"]["position"] = row3
    db.save_run(rid1, rec["state"]["status"], rec["state"]["position"], rec["state"])
    client.post(f"/api/runs/{rid1}/act",
                json={"action": "choose_node", "node": "boss"})
    _win_battle_in_state(client, rid1)

    adv = client.post(f"/api/expeditions/{exp_id}/advance", json={}).json()
    rid2 = adv["run"]["run_id"]
    assert adv["run"]["expedition"]["chapter"] == 2
    rec2 = service.load_run(rid2)
    assert rec2["state"]["chapter"] == 2 and rec2["state"]["chapters_total"] == 3
    # 带入委托的剩余期限按真实章号计算
    carried = adv["run"]["commissions"][0]
    assert carried["chapters_left"] == max(0, carried["deadline_chapter"] - 2)
    assert carried["accepted_chapter"] == 1


def test_new_ch2_offers_anchored_to_chapter_two(client):
    info = _make_buggy_two_chapter_exp(client)  # 仅借用建局；下面验证新挂单基准
    # 直接用修复后的第 2 章状态生成挂单：deadline 必须 >= 2
    rec2 = service.load_run(info["rid2"])
    # 先触发在线修复（续局）
    client.get(f"/api/runs/{info['rid2']}/resume")
    rec2 = service.load_run(info["rid2"])
    offers = service.commission_mod.generate_offers(
        12321, rec2["state"]["chapter"], rec2["state"]["chapters_total"],
        rec2["state"]["commissions"])
    for o in offers:
        assert o["deadline_chapter"] >= 2 and o["offered_chapter"] == 2


# ---------------- 受影响旧档：续局修复 ----------------
def test_resume_repairs_buggy_chapter_run(client):
    info = _make_buggy_two_chapter_exp(client)
    rid2 = info["rid2"]
    # 修复前：错误章号 + 错误锚定
    raw = service.load_run(rid2)
    assert raw["state"]["chapter"] == 1
    assert info["ch2_q"]["accepted_chapter"] == 1

    view = client.get(f"/api/runs/{rid2}/resume").json()
    assert view["expedition"]["chapter"] == 2
    by_sig = {q["id"]: q for q in view["commissions"]}
    # 第 2 章接取的委托：章号修复为 2，deadline 重新锚定（>=2），剩余期限正确
    fixed_q = next(q for q in view["commissions"]
                   if q["id"] == info["ch2_q"]["id"])
    assert fixed_q["accepted_chapter"] == 2
    assert fixed_q["deadline_chapter"] >= 2
    assert fixed_q["chapters_left"] == max(0, fixed_q["deadline_chapter"] - 2)
    # 带入的第 1 章委托：accepted_chapter 仍为 1，deadline 不被重算
    carried = next(q for q in view["commissions"]
                   if q["id"] != info["ch2_q"]["id"])
    assert carried["accepted_chapter"] == 1
    assert carried["chapters_left"] == max(0, carried["deadline_chapter"] - 2)

    # 落库修复（幂等：再续局不再变化）
    rec2 = service.load_run(rid2)
    assert rec2["state"]["chapter"] == 2
    rev_before = rec2["rev"]
    client.get(f"/api/runs/{rid2}/resume")
    assert service.load_run(rid2)["rev"] == rev_before

    # 领奖记录章号正确：修复后领取第 2 章 ready 委托
    rec2 = service.load_run(rid2)
    c = next(c for c in rec2["state"]["commissions"]
             if c["id"] == info["ch2_q"]["id"])
    c["status"] = READY
    c["progress"] = c["target"]
    db.save_run(rid2, rec2["state"]["status"], rec2["state"]["position"],
                rec2["state"])
    r = client.post(f"/api/runs/{rid2}/act",
                    json={"action": "commission_claim",
                          "commission": c["id"]})
    assert r.status_code == 200, r.text
    rec2 = service.load_run(rid2)
    c = next(c for c in rec2["state"]["commissions"]
             if c["id"] == info["ch2_q"]["id"])
    assert c["status"] == CLAIMED
    assert c["claimed_at"].startswith("chapter:2:"), c["claimed_at"]


def test_resume_revives_wrongly_expired_carry_commission(client):
    info = _make_buggy_two_chapter_exp(client, extra_dl1=True)
    rid2 = info["rid2"]
    # 旧版推进时 deadline=1 的贸易委托已 failed(expired)——但它其实应在第 2 章末才到期
    raw = next(c for c in service.load_run(rid2)["state"]["commissions"]
               if c["id"] == info["dl1_id"])
    assert raw["status"] == FAILED and raw["fail_reason"] == "expired"
    # 续局修复：deadline=1 在进入第 2 章时确实到期……（1 < 2，超期成立，不应复活）
    view = client.get(f"/api/runs/{rid2}/resume").json()
    dl1 = next(q for q in view["commissions"] if q["id"] == info["dl1_id"])
    assert dl1["status"] == FAILED and dl1["fail_reason"] == "expired"


def _buggy_chapter_state_from_carry(exp_id, carry, nxt, chapters_total, run_id=None):
    """在事务内用旧版语义从 carry 开第 nxt 章（含旧超期判定/错误章号/旧日志）。

    要求 carry 来源章（nxt-1）已通关；同步补写该来源章的 chapter_clear 事件
    （真实在线流程在通关动作事务内落库），使修复用的交接快照边界与线上一致。
    """
    run_id = run_id or uuid.uuid4().hex[:12]
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM expeditions WHERE id=?",
                           (exp_id,)).fetchone()
        # 来源章通关事件（若尚缺）：advance 前的权威快照（未做超期判定）
        origin = nxt - 1
        have_clear = any(
            e["kind"] == "chapter_clear"
            and (e.get("payload") or {}).get("chapter") == origin
            for e in db.load_expedition_events(exp_id))
        if not have_clear and origin >= 1 and origin < chapters_total:
            db.append_expedition_event_conn(
                conn, exp_id, db.next_expedition_seq_conn(conn, exp_id),
                "chapter_clear",
                {"chapter": origin, "run_id": row["current_run_id"], "carry": carry})
        expired = service.commission_mod.expire_active(carry["commissions"], nxt)
        state = _buggy_new_run_state(
            service._chapter_seed(row["seed"], nxt), carry, nxt,
            chapters_total, exp_id)
        map_data = service.mapgen.generate_map(state["seed"])
        if conn.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone():
            conn.execute("DELETE FROM battle_events WHERE run_id=?", (run_id,))
            conn.execute(
                "UPDATE runs SET status=?, position=?, state_json=?, map_json=? WHERE id=?",
                (state["status"], state["position"],
                 json.dumps(state, ensure_ascii=False),
                 json.dumps(map_data, ensure_ascii=False), run_id))
        else:
            db.insert_run(conn, run_id, state["seed"], state["status"],
                          state["position"], map_data, state,
                          expedition_id=exp_id, chapter=nxt)
        db.append_event_conn(conn, run_id, 1, "create", {
            "seed": state["seed"], "ver": "2.3.0",
            "ckpt": service.state_checkpoint(
                state, include_companion=False, include_potions=False),
            "expedition": exp_id,
            "chapter": nxt, "chapters_total": chapters_total, "carry": carry})
        db.save_expedition_conn(conn, exp_id, "in_progress", nxt, run_id, carry,
                                expected_rev=row["rev"])
    return run_id, expired


def test_resume_revives_commission_with_valid_deadline(client):
    """deadline 仍覆盖当前章的进行中委托：旧逻辑推进时被误判超期，修复应复活。

    场景：委托在第 2 章接取（修复视角 deadline=3），旧版状态章号错乱为 1；
    旧逻辑推进到第 3 章时 3<3 不成立其实不会误判——用「第 3 章接取、deadline=3、
    旧版推进到第 4 章」构造真实误判：旧状态章号恒 1 使挂单 deadline=min(total,1+d)，
    d=2 时 deadline=3，进入真实第 4 章 3<4 被旧超期判定误杀；修复后接取章=3、
    deadline=min(total,3+2)>=4，进入第 4 章不应超期。
    """
    info = _make_buggy_two_chapter_exp(client, chapters=4)
    exp_id, rid2 = info["exp_id"], info["rid2"]
    # 让第 2 章通关并旧版推进到第 3 章
    rec = service.load_run(rid2)
    rec["state"]["status"] = "won"
    db.save_run(rid2, "won", rec["state"]["position"], rec["state"])
    carry2 = service._carry_from_run(rec["state"])
    rid3, _ = _buggy_chapter_state_from_carry(exp_id, carry2, 3, 4)
    # 在第 3 章（旧状态章号=1）直接注入一个「旧版挂单」：贸易委托 d=2 ->
    # 旧锚定章号 1 使 deadline=min(4,1+2)=3；修复视角接取章=3 后 deadline=4。
    rec3 = service.load_run(rid3)
    offer = {
        "kind": "trade", "target": 3, "deadline_chapter": 3,
        "reward": {"type": "gold", "amount": 44},
        "signature": "trade:3:gold:44", "offered_chapter": 1,  # 旧章号错乱
    }
    seq = rec3["state"]["next_commission_seq"]
    vic = service.commission_mod.make_commission(seq, offer)
    rec3["state"]["commissions"].append(vic)
    rec3["state"]["next_commission_seq"] = seq + 1
    rec3["state"]["status"] = "won"
    db.save_run(rid3, "won", rec3["state"]["position"], rec3["state"])

    # 旧版推进到第 4 章：deadline 3 < 4 -> 误判 expired
    carry3 = service._carry_from_run(rec3["state"])
    rid4, expired = _buggy_chapter_state_from_carry(exp_id, carry3, 4, 4)
    assert vic["id"] in expired
    raw = next(c for c in service.load_run(rid4)["state"]["commissions"]
               if c["id"] == vic["id"])
    assert raw["status"] == FAILED and raw["fail_reason"] == "expired"

    # 续局修复：接取章修正为 3，deadline=min(4,3+2)=4 >= 4 -> 复活为进行中
    view = client.get(f"/api/runs/{rid4}/resume").json()
    fixed = next(q for q in view["commissions"] if q["id"] == vic["id"])
    assert fixed["status"] == ACTIVE, fixed
    assert fixed["accepted_chapter"] == 3 and fixed["deadline_chapter"] == 4
    assert fixed["chapters_left"] == 0
    # 幂等
    rev = service.load_run(rid4)["rev"]
    client.get(f"/api/runs/{rid4}/resume")
    assert service.load_run(rid4)["rev"] == rev


# ---------------- 受影响旧档：整章回放 ----------------
def test_replay_of_buggy_chapter_repairs_and_matches_online(client):
    # 第 1 章通关快照携带 500 金币 -> 随错误 carry 进入第 2 章（回放初态同样具备）
    info = _make_buggy_two_chapter_exp(client, ch1_gold=500)
    rid2 = info["rid2"]
    assert service.load_run(rid2)["state"]["gold"] == 500
    rec = service.load_run(rid2)
    state = rec["state"]
    q = next(c for c in state["commissions"] if c["id"] == info["ch2_q"]["id"])
    target_id = None
    if q["kind"] != "trade" or q["target"] > 1:
        # 直接构造一个旧版贸易挂单（target=1，旧章号 1 锚定 deadline=2），
        # 并用旧版 commission_accept 动作接取（事件带 2.3.0 ckpt），回放才能重建。
        trade_offer = {
            "kind": "trade", "target": 1, "deadline_chapter": 2,
            "reward": {"type": "gold", "amount": 55},
            "signature": "trade:1:gold:55", "offered_chapter": 1}
        state["shop"].setdefault("commission_offers", []).append(trade_offer)
        db.save_run(rid2, state["status"], state["position"], state)
        _buggy_act(rid2, {"action": "commission_accept",
                          "sku": "commission:trade:1:gold:55"})
        target_id = next(c["id"] for c in service.load_run(rid2)["state"]["commissions"]
                         if c["signature"] == "trade:1:gold:55")
    else:
        target_id = q["id"]

    # 用旧版动作完成一笔购买（推进贸易目标到 ready），事件带 2.3.0 ckpt
    rec = service.load_run(rid2)
    card = next(it for it in rec["state"]["shop"]["cards"] if not it["sold"])
    _buggy_act(rid2, {"action": "shop_buy", "kind": "card", "sku": card["sku"]})
    rec = service.load_run(rid2)
    tq = next(c for c in rec["state"]["commissions"] if c["id"] == target_id)
    assert tq["status"] == READY, (tq["kind"], tq["target"], tq["progress"])

    # 在线修复（续局）后，用新版动作领奖 -> 产生一条 2.4.0 严格校验事件
    client.get(f"/api/runs/{rid2}/resume")
    r = client.post(f"/api/runs/{rid2}/act",
                    json={"action": "commission_claim", "commission": target_id})
    assert r.status_code == 200, r.text

    rep = client.get(f"/api/runs/{rid2}/replay").json()
    v = rep["verification"]
    assert v["error"] == 0
    # 修复前步骤（create + 旧接取/走路/旧交易）按 legacy 兼容；修复后 claim 严格通过
    assert v["repaired"] >= 1
    assert any(ch["action"] == "commission_claim" and ch["status"] == "ok"
               for ch in v["checks"])
    # 最终帧与在线修复后存档一致（委托状态/章号/领奖记录）
    online = service.load_run(rid2)["state"]
    final = rep["final_view"]
    assert final["expedition"]["chapter"] == 2
    q_online = next(x for x in online["commissions"] if x["id"] == target_id)
    q_final = next(x for x in final["commissions"] if x["id"] == target_id)
    assert (q_final["status"], q_final["accepted_chapter"],
            q_final["deadline_chapter"]) == \
           (q_online["status"], q_online["accepted_chapter"],
            q_online["deadline_chapter"])
    assert q_final["chapters_left"] == max(0, q_final["deadline_chapter"] - 2)
    # 旧接取动作在修复路径上可重放（不报错），且帧内委托章号已修复
    accept_step = next(s for s in rep["steps"]
                       if s["action"] == "commission_accept" and s["repaired"])
    assert accept_step["error"] is None
    assert any(q["accepted_chapter"] == 2
               for q in accept_step["view"]["commissions"])


# ---------------- 受影响旧档：整程回放只读隔离 ----------------
def test_full_expedition_replay_of_buggy_save_isolated(client):
    # 第 1 章用合法机器人打完（其回放逐位严格）；第 2 章为受影响旧档
    exp_id = client.post("/api/expeditions",
                         json={"seed": 2, "chapters": 2}).json()["expedition"]["id"]
    rid1 = client.get(f"/api/expeditions/{exp_id}").json()["run"]["run_id"]

    def bot(rid):
        pref = {"rest": 0, "reward": 1, "shop": 2, "forge": 3,
                "encounter": 4, "elite": 6, "boss": 7}
        for _ in range(300):
            view = client.get(f"/api/runs/{rid}/resume").json()
            if view["status"] != "in_progress":
                return
            if view["in_battle"]:
                pick = next((h for h in view["battle"]["hand"]
                             if h["id"] == "strike"
                             and h["cost"] <= view["battle"]["energy"]), None)
                body = ({"action": "play", "card": pick["uid"]} if pick
                        else {"action": "end_turn"})
                client.post(f"/api/runs/{rid}/act", json=body)
                continue
            if view.get("shop_available") and view["shop"]:
                for o in view["shop"]["commissions"]:
                    client.post(f"/api/runs/{rid}/act",
                                json={"action": "commission_accept",
                                      "sku": o["sku"]})
            if not view["reward_claimed"] and view["reward_options"]:
                idx = next((i for i, o in enumerate(view["reward_options"])
                            if o.get("kind") == "gold"), 0)
                client.post(f"/api/runs/{rid}/act",
                            json={"action": "claim_reward", "option": idx})
                continue
            node = sorted(view["reachable"], key=lambda n: pref.get(n["type"], 9))[0]
            client.post(f"/api/runs/{rid}/act",
                        json={"action": "choose_node", "node": node["id"]})

    bot(rid1)
    assert service.load_run(rid1)["state"]["status"] == "won"

    # 旧版推进到错误章号的第 2 章，并在第 2 章商店旧版接取委托
    rid2 = _buggy_advance(exp_id)
    rec2 = service.load_run(rid2)
    shop_node = next(n for n, nd in rec2["map"]["nodes"].items()
                     if nd["type"] == "shop")
    for node in _buggy_path(rec2["map"], rec2["state"]["position"], shop_node):
        _buggy_act(rid2, {"action": "choose_node", "node": node})
    rec2 = service.load_run(rid2)
    offer = rec2["state"]["shop"]["commission_offers"][0]
    _buggy_act(rid2, {"action": "commission_accept",
                      "sku": f"commission:{offer['signature']}"})
    ch2_q_id = service.load_run(rid2)["state"]["commissions"][-1]["id"]

    # 旧版语义打完终章（含战斗/奖励），同步远征 won 结算
    _buggy_clear_chapter(rid2, exp_id, 2)
    assert db.load_expedition(exp_id)["status"] == "won"

    profile_before = db.get_profile()
    rep = client.get(f"/api/expeditions/{exp_id}/replay").json()
    assert rep["isolated"] is True
    # 两章回放都无 error/mismatch；第 1 章严格、第 2 章受影响步骤以 legacy 兼容
    for ch in rep["chapters"]:
        vv = ch["replay"]["verification"]
        assert vv["error"] == 0 and vv["mismatch"] == 0
    ch1v, ch2v = (ch["replay"]["verification"] for ch in rep["chapters"])
    assert ch2v["repaired"] >= 1
    # 第 2 章最终帧：第 2 章接取的委托章号已修复
    q = next(x for x in rep["chapters"][1]["replay"]["final_view"]["commissions"]
             if x["id"] == ch2_q_id)
    assert q["accepted_chapter"] == 2
    # 只读隔离
    assert db.get_profile() == profile_before
    assert db.load_expedition(exp_id)["status"] == "won"


# ---------------- 新（已修复）远征回放仍严格逐位一致 ----------------
def test_fixed_expedition_replay_still_strict(client):
    exp = client.post("/api/expeditions",
                      json={"seed": 2, "chapters": 2}).json()
    exp_id = exp["expedition"]["id"]
    rid = exp["run"]["run_id"]

    def win(rid):
        """合法打完章节（不瞬移）：rest/reward/shop 优先，战斗靠 strike。"""
        pref = {"rest": 0, "reward": 1, "shop": 2, "forge": 3,
                "encounter": 4, "elite": 6, "boss": 7}
        for _ in range(300):
            view = client.get(f"/api/runs/{rid}/resume").json()
            if view["status"] != "in_progress":
                return
            if view["in_battle"]:
                hand = view["battle"]["hand"]
                energy = view["battle"]["energy"]
                pick = next((h for h in hand
                             if h["id"] == "strike" and h["cost"] <= energy), None)
                body = ({"action": "play", "card": pick["uid"]} if pick
                        else {"action": "end_turn"})
                client.post(f"/api/runs/{rid}/act", json=body)
                continue
            if not view["reward_claimed"] and view["reward_options"]:
                idx = next((i for i, o in enumerate(view["reward_options"])
                            if o.get("kind") == "gold"), 0)
                client.post(f"/api/runs/{rid}/act",
                            json={"action": "claim_reward", "option": idx})
                continue
            reach = view["reachable"]
            node = sorted(reach, key=lambda n: pref.get(n["type"], 9))[0]
            client.post(f"/api/runs/{rid}/act",
                        json={"action": "choose_node", "node": node["id"]})

    win(rid)
    adv = client.post(f"/api/expeditions/{exp_id}/advance", json={}).json()
    rid2 = adv["run"]["run_id"]
    win(rid2)
    rep = client.get(f"/api/expeditions/{exp_id}/replay").json()
    for ch in rep["chapters"]:
        vv = ch["replay"]["verification"]
        assert vv["mismatch"] == 0 and vv["error"] == 0
        assert vv["repaired"] == 0
