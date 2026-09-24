"""远征委托（0-1 新增）：商店接取限章委托，战斗/交易自动推进，领奖防重复，
超期/战败失败，随章节交接，续局与整程回放保留完整状态。

覆盖：
- 仅远征章节商店确定性挂单（普通局无委托）；同种子重放挂单一致
- 接取：挂单移除、委托进入进行中；重复接取 409（不重复发委托）；非商店 400；限张
- 战斗胜利自动推进讨伐目标（贸易目标不随战斗推进）；可领奖后领取金币/卡牌
- 商店交易自动推进贸易目标；失败交易不推进
- 领奖幂等：重复领取 409 不重复发奖；未完成/失败/战斗中领取 400
- 战败：进行中委托全部失败（battle_lost），不可再领；远征同步结算
- 超期：推进章节时限章早于新章的进行中委托失败（expired），可领奖的不超期
- 章节通关后、进入下一章前仍可领奖；终章通关后不可再领
- 委托随交接快照进入下一章；续局保留；逐章/整程回放校验点全通过、状态一致、只读隔离
- request_id 幂等：接取/领奖双击返回首次响应，不重复发奖
"""
import threading

from app import db, service
from app.commissions import (ACTIVE, READY, CLAIMED, FAILED, BATTLE, TRADE,
                             generate_offers, make_commission, expire_active,
                             apply_battle_result, apply_trade)

SEED = 11
R0 = "0-0"          # 第 0 行奖励节点（合法路径，非战斗）
SHOP1 = "1-2"       # 第 1 章商店：trade 委托（target2/3）
SHOP_BATTLE = "3-0"  # 第 1 章另一商店：battle 委托（boss 前）
CH2_SHOP = "3-1"    # 第 2 章商店（在 2-2 之后）


def _create(client, chapters=3):
    r = client.post("/api/expeditions", json={"seed": SEED, "chapters": chapters})
    assert r.status_code == 200
    return r.json()


def _go(client, rid, node):
    r = client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": node})
    assert r.status_code == 200, r.text
    return r.json()


def _reach_ch1_shop(client, rid, shop=SHOP1):
    """合法走到第 1 章商店：start -> 0-0（奖励）-> 商店。"""
    _go(client, rid, R0)
    return _go(client, rid, shop)["run"]


def _shop_offers(view):
    return view["shop"]["commissions"]


def _accept(client, rid, sku, request_id=None):
    body = {"action": "commission_accept", "sku": sku}
    if request_id is not None:
        body["request_id"] = request_id
    return client.post(f"/api/runs/{rid}/act", json=body)


def _claim(client, rid, cid, request_id=None):
    body = {"action": "commission_claim", "commission": cid}
    if request_id is not None:
        body["request_id"] = request_id
    return client.post(f"/api/runs/{rid}/act", json=body)


def _enter_encounter_after(client, rid, node):
    rec = service.load_run(rid)
    nxt = next(n for n in rec["map"]["routes"][node]
               if rec["map"]["nodes"][n]["type"] == "encounter")
    return _go(client, rid, nxt)["run"]


def _win_current_battle(client, rid):
    """敌方血量压到 1，打出一张打击结束战斗（测试作弊路径）。"""
    rec = service.load_run(rid)
    rec["state"]["battle"]["entities"]["enemy"]["hp"] = 1
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    view = client.get(f"/api/runs/{rid}/resume").json()
    strike = next(h for h in view["battle"]["hand"]
                  if (h["id"] if isinstance(h, dict) else h) == "strike")
    uid = strike["uid"] if isinstance(strike, dict) else strike
    r = client.post(f"/api/runs/{rid}/act", json={"action": "play", "card": uid})
    assert r.status_code == 200
    return r.json()


def _lose_current_battle(client, rid, turns=8):
    rec = service.load_run(rid)
    rec["state"]["battle"]["entities"]["player"]["hp"] = 1
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    for _ in range(turns):
        r = client.post(f"/api/runs/{rid}/act", json={"action": "end_turn"})
        assert r.status_code == 200
        if r.json()["run"]["status"] == "lost":
            return r.json()
    raise AssertionError("player did not die")


def _win_chapter(client, rid):
    """从任意位置通关当前章节：跳到 row3 再选 boss 并击杀（测试便捷路径）。"""
    rec = service.load_run(rid)
    row3 = next(n for n, nd in rec["map"]["nodes"].items() if nd.get("row") == 3)
    if rec["state"]["position"] != row3:
        rec["state"]["position"] = row3
        db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    _go(client, rid, "boss")
    return _win_current_battle(client, rid)


# ---------------- 挂单 ----------------
def test_commissions_offered_only_in_expedition_shop(client):
    data = _create(client)
    rid = data["run"]["run_id"]
    view = _reach_ch1_shop(client, rid)
    offers = _shop_offers(view)
    assert len(offers) >= 1
    for o in offers:
        assert o["sku"].startswith("commission:")
        assert o["deadline_chapter"] >= 1
        assert o["reward"]["text"]
    # 普通局商店：不出委托，视口无委托
    r = client.post("/api/runs", json={"seed": 777}).json()
    # 找一个商店节点并合法抵达（行 1+），直接用确定性检查：普通局库存恒无挂单
    from app import shop as shop_mod
    stock = shop_mod.generate_stock(1, set(), set())
    assert stock["commission_offers"] == []
    assert r["commissions"] == []


def test_offers_deterministic_same_seed(client):
    a = generate_offers(4242, 1, 3, [])
    b = generate_offers(4242, 1, 3, [])
    assert a == b
    # 章节/总章数影响时限
    later = generate_offers(4242, 2, 3, [])
    assert all(o["deadline_chapter"] >= 2 for o in later)


def test_accepted_offer_not_duplicated(client):
    data = _create(client)
    rid = data["run"]["run_id"]
    view = _reach_ch1_shop(client, rid)
    sku = _shop_offers(view)[0]["sku"]
    r = _accept(client, rid, sku)
    assert r.status_code == 200
    # 挂单已从商店移除
    assert all(o["sku"] != sku for o in r.json()["run"]["shop"]["commissions"])
    # 再次接取同一委托：409 售罄，不重复发委托
    dup = _accept(client, rid, sku)
    assert dup.status_code == 409
    assert len([q for q in service.load_run(rid)["state"]["commissions"]]) == 1


def test_accept_requires_shop(client):
    data = _create(client)
    rid = data["run"]["run_id"]
    _reach_ch1_shop(client, rid)
    sku = service.load_run(rid)["state"]["shop"]["commission_offers"][0]
    full_sku = f"commission:{sku['signature']}"
    # 离开商店后接取 -> 400
    rec = service.load_run(rid)
    nxt = rec["map"]["routes"][SHOP1][0]
    _go(client, rid, nxt)
    assert _accept(client, rid, full_sku).status_code == 400


def test_accept_unknown_sku_400(client):
    data = _create(client)
    rid = data["run"]["run_id"]
    _reach_ch1_shop(client, rid)
    assert _accept(client, rid, "commission:nope").status_code == 400


def test_active_commission_cap(client):
    # MAX_ACTIVE=3：挂单最多 2 个，直接构造 3 个进行中后商店不再生成挂单
    data = _create(client)
    rid = data["run"]["run_id"]
    view = _reach_ch1_shop(client, rid)
    skus = [o["sku"] for o in _shop_offers(view)]
    assert len(skus) == 2
    for sku in skus:
        assert _accept(client, rid, sku).status_code == 200
    # 注入第 3 个进行中委托（同一种子商店若重开应无挂单）
    rec = service.load_run(rid)
    offers = generate_offers(999, 1, 3, rec["state"]["commissions"])
    if offers:
        seq = rec["state"]["next_commission_seq"]
        rec["state"]["commissions"].append(make_commission(seq, offers[0]))
        rec["state"]["next_commission_seq"] = seq + 1
        db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
        assert generate_offers(999, 1, 3,
                               service.load_run(rid)["state"]["commissions"]) == []


# ---------------- 战斗推进 ----------------
def test_battle_win_progresses_battle_commission(client):
    data = _create(client)
    rid = data["run"]["run_id"]
    # 3-0 商店挂 battle 委托：start->0-0->1-2(shop)->2-? 合法走到 row3
    _reach_ch1_shop(client, rid, SHOP1)
    # 经 SHOP1 -> 2-0(forge) -> 3-0(shop)
    _go(client, rid, "2-0")
    view = _go(client, rid, SHOP_BATTLE)["run"]
    offer = next(o for o in _shop_offers(view) if o["commission_kind"] == BATTLE)
    assert _accept(client, rid, offer["sku"]).status_code == 200
    # 首领是最后一场战斗；为可领奖验证改打精英：从 3-0 无 encounter 后继，
    # 直接打 boss（battle target=2 时一场只到 active 1/2）
    q = service.load_run(rid)["state"]["commissions"][0]
    assert q["target"] == 2
    _go(client, rid, "boss")
    won = _win_current_battle(client, rid)
    c = next(x for x in won["run"]["commissions"] if x["kind"] == "battle")
    # 首胜进度 +1（目标 2，尚未可领奖）
    assert c["progress"] == 1 and c["status"] == ACTIVE
    # 章节已通关：进行中委托随交接保留（推进后可在第 2 章继续）
    assert won["run"]["status"] == "won"
    adv = client.post(f"/api/expeditions/{data['expedition']['id']}/advance", json={})
    assert adv.status_code == 200
    rid2 = adv.json()["run"]["run_id"]
    carried = next(x for x in adv.json()["run"]["commissions"] if x["kind"] == "battle")
    assert carried["progress"] == 1 and carried["status"] == ACTIVE
    # 第 2 章赢一场 -> ready
    _go(client, rid2, "0-1")  # encounter maggot
    won2 = _win_current_battle(client, rid2)
    c2 = next(x for x in won2["run"]["commissions"] if x["kind"] == "battle")
    assert c2["status"] == READY and c2["can_claim"] is True
    return data, rid2, c2["id"]


def test_battle_does_not_progress_trade_commission(client):
    data = _create(client)
    rid = data["run"]["run_id"]
    view = _reach_ch1_shop(client, rid)
    offer = next(o for o in _shop_offers(view) if o["commission_kind"] == TRADE)
    _accept(client, rid, offer["sku"])
    run = _enter_encounter_after(client, rid, SHOP1)
    assert run["in_battle"] is True
    won = _win_current_battle(client, rid)
    q = won["run"]["commissions"][0]
    assert q["kind"] == TRADE and q["progress"] == 0 and q["status"] == ACTIVE


def test_loss_fails_all_active_commissions(client):
    data = _create(client)
    exp_id = data["expedition"]["id"]
    rid = data["run"]["run_id"]
    view = _reach_ch1_shop(client, rid)
    for o in _shop_offers(view):
        _accept(client, rid, o["sku"])
    _enter_encounter_after(client, rid, SHOP1)
    lost = _lose_current_battle(client, rid)
    assert lost["run"]["status"] == "lost"
    assert lost["run"]["expedition"]["status"] == "lost"
    for q in lost["run"]["commissions"]:
        assert q["status"] == FAILED and q["fail_reason"] == "battle_lost"
    # 失败委托不可领取
    for q in lost["run"]["commissions"]:
        assert _claim(client, rid, q["id"]).status_code == 400
    # 远征已结算：不重复结算事件
    settles = [e for e in db.load_expedition_events(exp_id) if e["kind"] == "settle"]
    assert len(settles) == 1 and settles[0]["payload"]["result"] == "lost"


# ---------------- 领奖 ----------------
def test_claim_gold_and_card_rewards(client):
    # 金币奖励：贸易 target2（seed11 ch1 1-2 第二个挂单）
    data = _create(client)
    rid = data["run"]["run_id"]
    view = _reach_ch1_shop(client, rid)
    gold_offer = next(o for o in _shop_offers(view)
                      if o["commission_kind"] == TRADE and o["reward"]["type"] == "gold")
    _accept(client, rid, gold_offer["sku"])
    rec = service.load_run(rid)
    rec["state"]["gold"] = 300
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    view = client.get(f"/api/runs/{rid}/resume").json()
    card_item = next(it for it in view["shop"]["cards"] if not it["sold"])
    r = client.post(f"/api/runs/{rid}/act",
                    json={"action": "shop_buy", "kind": "card", "sku": card_item["sku"]})
    assert r.status_code == 200
    # 失败交易（金币不足）不推进：先记录进度 1/2，再尝试买遗物（余额可能不足）
    cid = r.json()["run"]["deck"][0]["uid"]
    r = client.post(f"/api/runs/{rid}/act", json={"action": "shop_remove", "card": cid})
    assert r.status_code == 200
    q = r.json()["run"]["commissions"][0]
    assert q["status"] == READY
    before = r.json()["run"]["gold"]
    r = _claim(client, rid, q["id"])
    assert r.status_code == 200
    granted = next(x["commission_claimed"] for x in r.json()["log"]
                   if "commission_claimed" in x)
    assert granted["granted"]["type"] == "gold"
    assert r.json()["run"]["gold"] == before + gold_offer["reward"]["amount"]
    assert r.json()["run"]["commissions"][0]["status"] == CLAIMED

    # 卡牌奖励：battle target2 的 3-0 卡挂单 -> 跨章第二胜后领取
    data2 = _create(client)
    rid = data2["run"]["run_id"]
    _reach_ch1_shop(client, rid)
    _go(client, rid, "2-0")
    view = _go(client, rid, SHOP_BATTLE)["run"]
    card_offer = next(o for o in _shop_offers(view)
                      if o["commission_kind"] == BATTLE and o["reward"]["type"] == "card")
    _accept(client, rid, card_offer["sku"])
    _go(client, rid, "boss")
    _win_current_battle(client, rid)
    adv = client.post(f"/api/expeditions/{data2['expedition']['id']}/advance", json={}).json()
    rid2 = adv["run"]["run_id"]
    _go(client, rid2, "0-1")
    won = _win_current_battle(client, rid2)
    q = next(x for x in won["run"]["commissions"] if x["can_claim"])
    deck_before = {d["uid"] for d in won["run"]["deck"]}
    r = _claim(client, rid2, q["id"])
    assert r.status_code == 200
    granted = next(x["commission_claimed"] for x in r.json()["log"]
                   if "commission_claimed" in x)
    assert granted["granted"]["type"] == "card"
    assert granted["granted"]["uid"] not in deck_before
    assert any(d["id"] == card_offer["reward"]["card"] for d in r.json()["run"]["deck"])


def test_claim_duplicate_409_and_idempotent(client):
    data = _create(client)
    rid = data["run"]["run_id"]
    view = _reach_ch1_shop(client, rid)
    offer = next(o for o in _shop_offers(view) if o["target"] == 2
                 and o["reward"]["type"] == "gold")
    _accept(client, rid, offer["sku"])
    rec = service.load_run(rid)
    rec["state"]["gold"] = 300
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    view = client.get(f"/api/runs/{rid}/resume").json()
    for it in [x for x in view["shop"]["cards"] if not x["sold"]][:2]:
        r = client.post(f"/api/runs/{rid}/act",
                        json={"action": "shop_buy", "kind": "card", "sku": it["sku"]})
        assert r.status_code == 200
    q = r.json()["run"]["commissions"][0]
    assert q["status"] == READY
    first = _claim(client, rid, q["id"], request_id="claim-1")
    assert first.status_code == 200
    gold_after = first.json()["run"]["gold"]
    # 同 request_id 重放：返回首次响应，duplicate:true，不重复发奖
    dup = _claim(client, rid, q["id"], request_id="claim-1")
    assert dup.status_code == 200 and dup.json()["duplicate"] is True
    assert dup.json()["run"]["gold"] == gold_after
    # 无令牌重复领取：状态机已 claimed -> 409
    again = _claim(client, rid, q["id"])
    assert again.status_code == 409


def test_concurrent_commission_claim_grants_once(client):
    # 并发（无 request_id）领取同一个 ready 委托：per-run 锁 + 状态机只允许一次
    data = _create(client)
    rid = data["run"]["run_id"]
    view = _reach_ch1_shop(client, rid)
    offer = next(o for o in _shop_offers(view) if o["target"] == 2
                 and o["reward"]["type"] == "gold")
    _accept(client, rid, offer["sku"])
    rec = service.load_run(rid)
    rec["state"]["gold"] = 300
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    view = client.get(f"/api/runs/{rid}/resume").json()
    for it in [x for x in view["shop"]["cards"] if not x["sold"]][:2]:
        client.post(f"/api/runs/{rid}/act",
                    json={"action": "shop_buy", "kind": "card", "sku": it["sku"]})
    qid = client.get(f"/api/runs/{rid}/resume").json()["commissions"][0]["id"]
    amount = offer["reward"]["amount"]

    codes = []
    barrier = threading.Barrier(6)

    def worker():
        barrier.wait()
        codes.append(_claim(client, rid, qid).status_code)

    ts = [threading.Thread(target=worker) for _ in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert codes.count(200) == 1 and codes.count(409) == 5
    final = client.get(f"/api/runs/{rid}/resume").json()
    spent = sum(t["price"] for t in final["shop"]["tx"] if t["type"] == "buy")
    # 初始 300 - 两笔购卡花费 + 委托奖励仅一次
    assert final["gold"] == 300 - spent + amount
    assert final["commissions"][0]["status"] == CLAIMED
    assert [e["action"] for e in db.load_events(rid)].count("commission_claim") == 1


def test_claim_not_ready_or_unknown_400(client):
    data = _create(client)
    rid = data["run"]["run_id"]
    view = _reach_ch1_shop(client, rid)
    offer = _shop_offers(view)[0]
    _accept(client, rid, offer["sku"])
    qid = service.load_run(rid)["state"]["commissions"][0]["id"]
    # 未完成
    assert _claim(client, rid, qid).status_code == 400
    # 非法 id
    assert _claim(client, rid, "q999").status_code == 400
    assert _claim(client, rid, None).status_code in (400, 422)


def test_cannot_claim_during_battle(client):
    # 构造 ready 后再进战斗（通过存档作弊设为 ready，模拟跨节点持有），战斗中领取应 400
    data = _create(client)
    rid = data["run"]["run_id"]
    view = _reach_ch1_shop(client, rid)
    offer = _shop_offers(view)[0]
    _accept(client, rid, offer["sku"])
    _enter_encounter_after(client, rid, SHOP1)
    rec = service.load_run(rid)
    rec["state"]["commissions"][0]["status"] = READY
    rec["state"]["commissions"][0]["progress"] = rec["state"]["commissions"][0]["target"]
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    r = _claim(client, rid, rec["state"]["commissions"][0]["id"])
    assert r.status_code == 400


def test_failed_trade_does_not_progress(client):
    data = _create(client)
    rid = data["run"]["run_id"]
    view = _reach_ch1_shop(client, rid)
    offer = next(o for o in _shop_offers(view) if o["commission_kind"] == TRADE)
    _accept(client, rid, offer["sku"])
    # 金币为 0：购买必失败（400，整体回退），委托不推进
    it = view["shop"]["cards"][0]
    r = client.post(f"/api/runs/{rid}/act",
                    json={"action": "shop_buy", "kind": "card", "sku": it["sku"]})
    assert r.status_code == 400
    q = client.get(f"/api/runs/{rid}/resume").json()["commissions"][0]
    assert q["progress"] == 0 and q["status"] == ACTIVE


# ---------------- 超期与交接 ----------------
def test_expire_on_chapter_advance(client):
    data = _create(client)
    exp_id = data["expedition"]["id"]
    rid = data["run"]["run_id"]
    view = _reach_ch1_shop(client, rid)
    for o in _shop_offers(view):
        _accept(client, rid, o["sku"])
    rec = service.load_run(rid)
    # 两个挂单 deadline 分别为 3 / 2；额外注入一个 deadline=1 的进行中贸易委托
    # （贸易目标不随战斗推进，击败首领不会自动完成它，从而真实触发超期）
    dl1_offer = {"kind": TRADE, "target": 3, "deadline_chapter": 1,
                 "reward": {"type": "gold", "amount": 10},
                 "signature": "trade:3:gold:10", "offered_chapter": 1}
    seq = rec["state"]["next_commission_seq"]
    dl1 = make_commission(seq, dl1_offer)
    rec["state"]["commissions"].append(dl1)
    rec["state"]["next_commission_seq"] = seq + 1
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])

    won = _win_chapter(client, rid)
    assert won["run"]["status"] == "won"
    adv = client.post(f"/api/expeditions/{exp_id}/advance", json={}).json()
    advance_ev = next(e for e in db.load_expedition_events(exp_id) if e["kind"] == "advance")
    assert dl1["id"] in advance_ev["payload"]["expired"]
    by_id = {q["id"]: q for q in adv["run"]["commissions"]}
    assert by_id[dl1["id"]]["status"] == FAILED
    assert by_id[dl1["id"]]["fail_reason"] == "expired"
    # deadline>=2 的进行中委托不受影响
    for q in adv["run"]["commissions"]:
        if q["id"] != dl1["id"]:
            assert q["status"] == ACTIVE
            assert q["deadline_chapter"] >= 2


def test_ready_commission_survives_expiry_and_can_claim_next_chapter(client):
    data = _create(client)
    exp_id = data["expedition"]["id"]
    rid = data["run"]["run_id"]
    view = _reach_ch1_shop(client, rid)
    offer = next(o for o in _shop_offers(view) if o["target"] == 2
                 and o["reward"]["type"] == "gold")
    _accept(client, rid, offer["sku"])
    # 两笔交易完成 -> ready（注入金币后合法交易）
    rec = service.load_run(rid)
    rec["state"]["gold"] = 300
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    view = client.get(f"/api/runs/{rid}/resume").json()
    for it in [x for x in view["shop"]["cards"] if not x["sold"]][:2]:
        client.post(f"/api/runs/{rid}/act",
                    json={"action": "shop_buy", "kind": "card", "sku": it["sku"]})
    qid = client.get(f"/api/runs/{rid}/resume").json()["commissions"][0]["id"]
    assert client.get(f"/api/runs/{rid}/resume").json()["commissions"][0]["status"] == READY
    # ready 状态下打通章节并推进：不超期、不失败
    won = _win_chapter(client, rid)
    assert next(q for q in won["run"]["commissions"] if q["id"] == qid)["status"] == READY
    adv = client.post(f"/api/expeditions/{exp_id}/advance", json={}).json()
    rid2 = adv["run"]["run_id"]
    q = next(x for x in adv["run"]["commissions"] if x["id"] == qid)
    assert q["status"] == READY and q["can_claim"] is True
    # 第 2 章领奖成功
    r = _claim(client, rid2, qid)
    assert r.status_code == 200
    assert r.json()["run"]["commissions"][0]["status"] == CLAIMED


def test_claim_allowed_after_chapter_win_before_advance(client):
    data = _create(client)
    exp_id = data["expedition"]["id"]
    rid = data["run"]["run_id"]
    _reach_ch1_shop(client, rid, SHOP1)
    _go(client, rid, "2-0")
    view = _go(client, rid, SHOP_BATTLE)["run"]
    offer = next(o for o in _shop_offers(view) if o["commission_kind"] == BATTLE)
    _accept(client, rid, offer["sku"])
    # target=2：首胜发生在 boss（第1章唯一一战），进度只到 1——为验证通关后领奖，
    # 注入 ready（模拟更早在本章节完成目标），再打 boss
    rec = service.load_run(rid)
    rec["state"]["commissions"][0]["status"] = READY
    rec["state"]["commissions"][0]["progress"] = rec["state"]["commissions"][0]["target"]
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    won = _win_chapter(client, rid)
    assert won["run"]["status"] == "won"
    assert won["run"]["expedition"]["status"] == "in_progress"
    qid = won["run"]["commissions"][0]["id"]
    r = _claim(client, rid, qid)
    assert r.status_code == 200
    assert r.json()["run"]["commissions"][0]["status"] == CLAIMED
    # 领奖没有重复结算远征（仍只 chapter_clear，无 settle）
    kinds = [e["kind"] for e in db.load_expedition_events(exp_id)]
    assert kinds.count("chapter_clear") == 1 and "settle" not in kinds
    # 仍可正常推进
    adv = client.post(f"/api/expeditions/{exp_id}/advance", json={})
    assert adv.status_code == 200


def test_no_claim_after_final_chapter_win(client):
    data = _create(client, chapters=1)
    rid = data["run"]["run_id"]
    view = _reach_ch1_shop(client, rid)
    for o in _shop_offers(view):
        _accept(client, rid, o["sku"])
    # 注入 ready（终章击败首领前完成目标的情形），再通关
    rec = service.load_run(rid)
    for q in rec["state"]["commissions"]:
        q["status"] = READY
        q["progress"] = q["target"]
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    won = _win_chapter(client, rid)
    assert won["run"]["expedition"]["status"] == "won"
    for q in won["run"]["commissions"]:
        assert _claim(client, rid, q["id"]).status_code == 400
    # 战败章节同理不可领
    data2 = _create(client, chapters=1)
    rid2 = data2["run"]["run_id"]
    view = _reach_ch1_shop(client, rid2)
    for o in _shop_offers(view):
        _accept(client, rid2, o["sku"])
    _enter_encounter_after(client, rid2, SHOP1)
    lost = _lose_current_battle(client, rid2)
    for q in lost["run"]["commissions"]:
        assert _claim(client, rid2, q["id"]).status_code == 400


# ---------------- 续局与回放 ----------------
# 合法行动机器人（商店优先，全程 API 行动；用于回放校验点逐位验证）
_BOT_PREF = {"shop": 0, "rest": 1, "reward": 2, "forge": 3,
             "encounter": 4, "elite": 6, "boss": 7}


def _bot_clear_chapter(client, rid, cap=300, accept_commissions=True):
    """合法打完当前章节：战斗优先打出打击，商店自动接委托，奖励优先金币。"""
    for _ in range(cap):
        view = client.get(f"/api/runs/{rid}/resume").json()
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
                body = {"action": "play", "card": pick["uid"] if isinstance(pick, dict) else pick}
            else:
                body = {"action": "end_turn"}
            r = client.post(f"/api/runs/{rid}/act", json=body)
            assert r.status_code == 200
            continue
        if accept_commissions and view.get("shop_available") and view["shop"]:
            for o in view["shop"]["commissions"]:
                r = _accept(client, rid, o["sku"])
                assert r.status_code == 200
        if not view["reward_claimed"] and view["reward_options"]:
            idx = next((i for i, o in enumerate(view["reward_options"])
                        if o.get("kind") == "gold"), 0)
            r = client.post(f"/api/runs/{rid}/act",
                            json={"action": "claim_reward", "option": idx})
            assert r.status_code == 200
            continue
        reach = view["reachable"]
        if not reach:
            return view["status"]
        node = sorted(reach, key=lambda n: _BOT_PREF.get(n["type"], 9))[0]
        r = client.post(f"/api/runs/{rid}/act",
                        json={"action": "choose_node", "node": node["id"]})
        assert r.status_code == 200
    raise AssertionError("chapter did not finish in time")


def test_resume_preserves_commissions(client):
    data = _create(client)
    rid = data["run"]["run_id"]
    view = _reach_ch1_shop(client, rid)
    offer = _shop_offers(view)[0]
    _accept(client, rid, offer["sku"])
    resumed = client.get(f"/api/runs/{rid}/resume").json()
    assert len(resumed["commissions"]) == 1
    assert resumed["commissions"][0]["objective"]
    assert resumed["commission_kinds"]


def test_chapter_replay_checkpoints_match(client):
    data = _create(client)
    rid = data["run"]["run_id"]
    view = _reach_ch1_shop(client, rid)
    offer = next(o for o in _shop_offers(view) if o["commission_kind"] == TRADE)
    _accept(client, rid, offer["sku"])
    rep = client.get(f"/api/runs/{rid}/replay").json()
    v = rep["verification"]
    assert v["mismatch"] == 0 and v["error"] == 0 and v["ok"] >= 3
    # 接取帧的视口含委托
    accept_step = next(s for s in rep["steps"] if s["action"] == "commission_accept")
    assert accept_step["kind"] == "commission"
    assert accept_step["view"]["commissions"][0]["status"] == ACTIVE


def test_full_expedition_replay_with_commissions_isolated(client):
    # 全程合法行动（商店优先机器人，seed 2 可两章全胜）：
    # 第 1 章在商店接委托并打完 -> ready 随交接进入第 2 章 -> 通关结算
    exp_id = client.post("/api/expeditions",
                         json={"seed": 2, "chapters": 2}).json()["expedition"]["id"]
    rid = client.get(f"/api/expeditions/{exp_id}").json()["run"]["run_id"]
    assert _bot_clear_chapter(client, rid) == "won"
    view = client.get(f"/api/runs/{rid}/resume").json()
    assert view["commissions"]  # 第 1 章持有委托
    adv = client.post(f"/api/expeditions/{exp_id}/advance", json={}).json()
    rid2 = adv["run"]["run_id"]
    # 第 2 章开章瞬间的委托状态（与回放首帧对应）
    online = {q["id"]: (q["status"], q["progress"], q["fail_reason"])
              for q in adv["run"]["commissions"]}
    assert _bot_clear_chapter(client, rid2) == "won"

    profile_before = db.get_profile()
    rep = client.get(f"/api/expeditions/{exp_id}/replay").json()
    assert rep["isolated"] is True
    assert [e["kind"] for e in rep["events"]] == \
        ["create", "chapter_clear", "advance", "settle"]
    for ch in rep["chapters"]:
        vv = ch["replay"]["verification"]
        assert vv["mismatch"] == 0 and vv["error"] == 0
        assert vv["ok"] >= 10
    # 第 2 章回放首帧：从交接快照重建的委托状态与在线完全一致
    first = rep["chapters"][1]["replay"]["steps"][0]["view"]
    replayed = {q["id"]: (q["status"], q["progress"], q["fail_reason"])
                for q in first["commissions"]}
    assert replayed == online
    assert online  # 非空：委托确实跨章交接
    # 委托动作出现在时间轴且类型正确
    kinds = {s["kind"] for ch in rep["chapters"] for s in ch["replay"]["steps"]}
    assert "commission" in kinds
    # 只读隔离：不写 profile
    assert db.get_profile() == profile_before


def test_legal_carry_state_matches_replay(client):
    """合法两章全胜：委托（ready/claimed）随交接带入第 2 章，回放首帧逐位一致。

    超期分支由 test_expire_on_chapter_advance（注入 deadline=1 贸易委托）覆盖；
    合法流程中第 1 章挂单时限至少为第 2 章，因此这里不强求出现 expired，
    只验证在线与回放重建的委托状态完全一致，以及 advance 事件的 expired 自洽。
    """
    exp_id = client.post("/api/expeditions",
                         json={"seed": 2, "chapters": 2}).json()["expedition"]["id"]
    rid = client.get(f"/api/expeditions/{exp_id}").json()["run"]["run_id"]
    assert _bot_clear_chapter(client, rid) == "won"
    adv = client.post(f"/api/expeditions/{exp_id}/advance", json={}).json()
    advance_ev = next(e for e in db.load_expedition_events(exp_id) if e["kind"] == "advance")
    expired_ids = set(advance_ev["payload"].get("expired", []))
    rid2 = adv["run"]["run_id"]
    online = {q["id"]: (q["status"], q["progress"], q["fail_reason"])
              for q in adv["run"]["commissions"]}
    for cid in expired_ids:
        assert online[cid][0] == FAILED and online[cid][2] == "expired"
    assert online  # 第 1 章商店接取的委托跨章保留
    assert _bot_clear_chapter(client, rid2) == "won"
    rep = client.get(f"/api/expeditions/{exp_id}/replay").json()
    for ch in rep["chapters"]:
        vv = ch["replay"]["verification"]
        assert vv["mismatch"] == 0 and vv["error"] == 0
    first = rep["chapters"][1]["replay"]["steps"][0]["view"]
    replayed = {q["id"]: (q["status"], q["progress"], q["fail_reason"])
                for q in first["commissions"]}
    assert replayed == online


# ---------------- 纯规则 ----------------
def test_pure_expire_and_progress_rules():
    offers = generate_offers(77, 1, 3, [])
    cs = [make_commission(i + 1, o) for i, o in enumerate(offers)]
    # 战败：全部进行中失败
    changed = apply_battle_result(cs, False)
    assert set(changed) == {c["id"] for c in cs}
    assert all(c["status"] == FAILED and c["fail_reason"] == "battle_lost" for c in cs)

    cs = [make_commission(i + 1, o) for i, o in enumerate(offers)]
    apply_trade(cs)
    for c in cs:
        assert c["progress"] == (1 if c["kind"] == TRADE else 0)
    # ready 不被超期
    for c in cs:
        if c["kind"] == TRADE and c["target"] == 1:
            c["status"] = READY
    expire_active(cs, 9)
    for c in cs:
        if c["status"] == READY:
            assert c["fail_reason"] is None
