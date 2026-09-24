"""多章远征：跨章节开章/交接/结算/整程回放。

覆盖：
- 创建远征（远征记录与第 1 章 run 同事务落库，视口携带远征摘要）
- 击败章节首领 -> 交接快照（牌组/锻造成长/遗物/金币）-> 进入下一章
- 防重复开章：重复 advance 400/409，request_id 幂等返回首次响应
- 战败结算远征并更新解锁；终章通关结算为 won；已结算后不再变动（防重复结算）
- 整程回放：逐章重建、校验点通过、只读隔离（不写存档/不发解锁）
"""
import pytest

from app import db, mapgen, service


def _create(client, seed=1, chapters=3):
    r = client.post("/api/expeditions", json={"seed": seed, "chapters": chapters})
    assert r.status_code == 200
    return r.json()


def _goto_boss(client, rid):
    """把存档移到首领前一格再选首领节点（测试便捷路径）。"""
    rec = service.load_run(rid)
    row3 = next(n for n, nd in rec["map"]["nodes"].items() if nd.get("row") == 3)
    rec["state"]["position"] = row3
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    r = client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": "boss"})
    assert r.status_code == 200
    assert r.json()["run"]["in_battle"] is True
    return r.json()


def _win_battle(client, rid):
    """敌方血量压到 1，打出一张打击结束战斗。"""
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


def _win_chapter(client, rid):
    _goto_boss(client, rid)
    return _win_battle(client, rid)


def _lose_current_battle(client, rid):
    """玩家血量压到 1，连续结束回合直到被敌方击杀（敌人可能先上 buff）。"""
    rec = service.load_run(rid)
    rec["state"]["battle"]["entities"]["player"]["hp"] = 1
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    for _ in range(6):
        r = client.post(f"/api/runs/{rid}/act", json={"action": "end_turn"})
        assert r.status_code == 200
        if r.json()["run"]["status"] == "lost":
            return r.json()
    raise AssertionError("player did not die within 6 turns")


def _enter_first_encounter(client, rid):
    rec = service.load_run(rid)
    node = next(n for n in rec["map"]["routes"]["start"]
                if rec["map"]["nodes"][n]["type"] == mapgen.ENCOUNTER)
    r = client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": node})
    assert r.status_code == 200
    assert r.json()["run"]["in_battle"] is True
    return r.json()


def _get_exp(client, exp_id):
    r = client.get(f"/api/expeditions/{exp_id}")
    assert r.status_code == 200
    return r.json()


# ---------------- 合法行动机器人（不直接改存档，回放校验点可逐位验证） ----------------
_SAFE = {"rest": 0, "reward": 1, "forge": 2, "shop": 3, "encounter": 4, "elite": 6, "boss": 7}


def _bot_step(client, rid):
    """推进一步：战斗中打牌/结束回合，领奖优先金币，否则走向最安全节点。"""
    view = client.get(f"/api/runs/{rid}/resume").json()
    if view["status"] != "in_progress":
        return view
    if view["in_battle"]:
        hand = view["battle"]["hand"]
        energy = view["battle"]["energy"]

        def cost(h):
            return h.get("cost", 1) if isinstance(h, dict) else 1

        def cid(h):
            return h["id"] if isinstance(h, dict) else h

        playable = [h for h in hand if cost(h) <= energy]
        pick = next((h for h in playable if cid(h) == "strike"), None) \
            or (playable[0] if playable else None)
        if pick:
            uid = pick["uid"] if isinstance(pick, dict) else pick
            r = client.post(f"/api/runs/{rid}/act", json={"action": "play", "card": uid})
        else:
            r = client.post(f"/api/runs/{rid}/act", json={"action": "end_turn"})
        assert r.status_code == 200
        return None
    if not view["reward_claimed"] and view["reward_options"]:
        idx = next((i for i, o in enumerate(view["reward_options"]) if o["kind"] == "gold"), 0)
        r = client.post(f"/api/runs/{rid}/act", json={"action": "claim_reward", "option": idx})
        assert r.status_code == 200
        return None
    reach = view["reachable"]
    if not reach:
        return view
    node = sorted(reach, key=lambda n: _SAFE.get(n["type"], 9))[0]
    r = client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": node["id"]})
    assert r.status_code == 200
    return None


def _bot_run_chapter(client, rid):
    """合法打完当前章节（只走 API 行动），返回 run 终态 status。"""
    for _ in range(300):
        view = _bot_step(client, rid)
        if view is None:
            view = client.get(f"/api/runs/{rid}/resume").json()
        if view["status"] != "in_progress":
            return view["status"]
    raise AssertionError("chapter did not finish within 300 actions")


def _bot_lose_chapter(client, rid):
    """进入首场战斗后只结束回合，直到被敌方击杀（全部合法行动）。"""
    _enter_first_encounter(client, rid)
    for _ in range(60):
        r = client.post(f"/api/runs/{rid}/act", json={"action": "end_turn"})
        assert r.status_code == 200
        if r.json()["run"]["status"] == "lost":
            return r.json()
    raise AssertionError("player did not die within 60 turns")


# ---------------- 创建与视口 ----------------
def test_create_expedition_starts_chapter_one(client):
    data = _create(client, seed=42, chapters=3)
    exp = data["expedition"]
    run = data["run"]
    assert exp["status"] == "in_progress"
    assert exp["chapter"] == 1 and exp["chapters_total"] == 3
    assert exp["current_run_id"] == run["run_id"]
    assert exp["chapters"] == [{"run_id": run["run_id"], "chapter": 1, "status": "in_progress"}]
    # run 视口携带远征摘要；初始牌组为 7 张实例
    assert run["expedition"]["id"] == exp["id"]
    assert run["expedition"]["chapter"] == 1
    assert len(run["deck"]) == 7
    # 远征事件日志：create
    events = db.load_expedition_events(exp["id"])
    assert [e["kind"] for e in events] == ["create"]


def test_create_expedition_validates_chapters(client):
    assert client.post("/api/expeditions", json={"chapters": 0}).status_code == 400
    assert client.post("/api/expeditions", json={"chapters": 99}).status_code == 400


def test_expedition_seed_deterministic_chapter_maps(client):
    a = _create(client, seed=7, chapters=3)["expedition"]
    b = _create(client, seed=7, chapters=3)["expedition"]
    ra = service.load_run(a["current_run_id"])
    rb = service.load_run(b["current_run_id"])
    assert ra["map"] == rb["map"]  # 同种子同图
    assert ra["state"]["seed"] == service._chapter_seed(7, 1)


# ---------------- 章节通关与交接 ----------------
def test_chapter_clear_records_carry_and_advance(client):
    data = _create(client, seed=5, chapters=3)
    exp_id = data["expedition"]["id"]
    rid1 = data["run"]["run_id"]
    # 人为制造成长：金币/遗物/锻造，验证交接完整性
    rec = service.load_run(rid1)
    rec["state"]["gold"] = 66
    rec["state"]["relics"]["power_up"] = 1
    uid = rec["state"]["deck"][0]
    rec["state"]["card_instances"][uid]["growth"].append(
        {"node": "sharpen", "cost": service.FORGE_COST})
    rec["state"]["health"] = 40
    db.save_run(rid1, rec["state"]["status"], rec["state"]["position"], rec["state"])

    won = _win_chapter(client, rid1)
    assert won["run"]["status"] == "won"
    # 非终章：远征不结算，等待推进；视口远征摘要同步
    assert won["run"]["expedition"]["status"] == "in_progress"
    exp = _get_exp(client, exp_id)["expedition"]
    assert exp["status"] == "in_progress" and exp["chapter"] == 1
    assert exp["carry"]["gold"] == 66 and exp["carry"]["relics"] == {"power_up": 1}
    forged = next(c for c in exp["carry"]["deck"] if c["uid"] == uid)
    assert forged["growth_nodes"] == ["sharpen"]
    assert [e["kind"] for e in db.load_expedition_events(exp_id)] == ["create", "chapter_clear"]

    # 进入下一章：牌组/锻造/遗物/金币交接，休整回血（40 -> 40+18=58，上限 75）
    adv = client.post(f"/api/expeditions/{exp_id}/advance", json={})
    assert adv.status_code == 200
    adv = adv.json()
    run2 = adv["run"]
    assert adv["duplicate"] is False
    assert adv["expedition"]["chapter"] == 2
    assert run2["expedition"]["chapter"] == 2
    assert run2["gold"] == 66
    assert run2["relics"] == {"power_up": 1}
    assert run2["health"] == 58 and run2["max_health"] == 75
    carried = next(c for c in run2["deck"] if c["uid"] == uid)
    assert carried["growth_nodes"] == ["sharpen"]
    # 新章为新图（章节种子派生）
    rec2 = service.load_run(run2["run_id"])
    assert rec2["state"]["seed"] == service._chapter_seed(5, 2)
    assert rec2["chapter"] == 2 and rec2["expedition_id"] == exp_id
    kinds = [e["kind"] for e in db.load_expedition_events(exp_id)]
    assert kinds == ["create", "chapter_clear", "advance"]


def test_advance_rejected_before_chapter_cleared(client):
    data = _create(client, seed=5, chapters=3)
    exp_id = data["expedition"]["id"]
    r = client.post(f"/api/expeditions/{exp_id}/advance", json={})
    assert r.status_code == 400  # 当前章未通关
    # 远征状态未被破坏
    assert _get_exp(client, exp_id)["expedition"]["chapter"] == 1


def test_advance_not_duplicated_and_idempotent(client):
    data = _create(client, seed=5, chapters=3)
    exp_id = data["expedition"]["id"]
    _win_chapter(client, data["run"]["run_id"])

    first = client.post(f"/api/expeditions/{exp_id}/advance",
                        json={"request_id": "adv-1"})
    assert first.status_code == 200
    run2 = first.json()["run"]["run_id"]
    # 同令牌重复提交：返回首次响应，不重复开章
    dup = client.post(f"/api/expeditions/{exp_id}/advance", json={"request_id": "adv-1"})
    assert dup.status_code == 200
    assert dup.json()["duplicate"] is True
    assert dup.json()["run"]["run_id"] == run2
    # 无令牌重复推进：当前章已是进行中 -> 400，不会开出第 3 个 run
    again = client.post(f"/api/expeditions/{exp_id}/advance", json={})
    assert again.status_code == 400
    runs = db.list_expedition_runs(exp_id)
    assert len(runs) == 2
    assert [r["chapter"] for r in runs] == [1, 2]


# ---------------- 结算 ----------------
def test_loss_settles_expedition_and_grants_unlock(client):
    data = _create(client, seed=9, chapters=3)
    exp_id = data["expedition"]["id"]
    rid = data["run"]["run_id"]
    before = len(data["run"]["unlocked_cards"]["unlocked"])

    _enter_first_encounter(client, rid)
    lost = _lose_current_battle(client, rid)
    assert lost["run"]["status"] == "lost"
    assert lost["run"]["expedition"]["status"] == "lost"

    exp = _get_exp(client, exp_id)["expedition"]
    assert exp["status"] == "lost"
    # 战败解锁已更新
    prof = client.get(f"/api/runs/{rid}").json()["unlocked_cards"]
    assert len(prof["unlocked"]) == before + 1
    # 结算事件落库且唯一
    settles = [e for e in db.load_expedition_events(exp_id) if e["kind"] == "settle"]
    assert len(settles) == 1 and settles[0]["payload"]["result"] == "lost"
    # 已结算：不再开章（409），run 不再接受行动（400），远征状态不再变化
    assert client.post(f"/api/expeditions/{exp_id}/advance", json={}).status_code == 409
    assert client.post(f"/api/runs/{rid}/act",
                       json={"action": "end_turn"}).status_code == 400
    assert _get_exp(client, exp_id)["expedition"]["status"] == "lost"
    assert len([e for e in db.load_expedition_events(exp_id) if e["kind"] == "settle"]) == 1


def test_final_chapter_win_settles_expedition_won(client):
    data = _create(client, seed=11, chapters=1)
    exp_id = data["expedition"]["id"]
    won = _win_chapter(client, data["run"]["run_id"])
    assert won["run"]["status"] == "won"
    assert won["run"]["expedition"]["status"] == "won"
    exp = _get_exp(client, exp_id)["expedition"]
    assert exp["status"] == "won"
    settles = [e for e in db.load_expedition_events(exp_id) if e["kind"] == "settle"]
    assert len(settles) == 1 and settles[0]["payload"]["result"] == "won"
    # 已通关：不再开章
    assert client.post(f"/api/expeditions/{exp_id}/advance", json={}).status_code == 409


def test_full_expedition_three_chapters(client):
    data = _create(client, seed=13, chapters=3)
    exp_id = data["expedition"]["id"]
    rid = data["run"]["run_id"]
    for chapter in (1, 2, 3):
        won = _win_chapter(client, rid)
        if chapter < 3:
            assert won["run"]["expedition"]["status"] == "in_progress"
            adv = client.post(f"/api/expeditions/{exp_id}/advance", json={})
            assert adv.status_code == 200
            rid = adv.json()["run"]["run_id"]
        else:
            assert won["run"]["expedition"]["status"] == "won"
    exp = _get_exp(client, exp_id)["expedition"]
    assert exp["status"] == "won" and exp["chapter"] == 3
    assert [c["status"] for c in exp["chapters"]] == ["won", "won", "won"]


# ---------------- 回放 ----------------
def test_expedition_replay_stitches_chapters_and_stays_isolated(client):
    # 全程合法行动（机器人），回放校验点可逐位验证
    data = _create(client, seed=1, chapters=2)
    exp_id = data["expedition"]["id"]
    rid1 = data["run"]["run_id"]
    before_profile = db.get_profile()

    assert _bot_run_chapter(client, rid1) == "won"
    adv = client.post(f"/api/expeditions/{exp_id}/advance", json={}).json()
    rid2 = adv["run"]["run_id"]
    _bot_lose_chapter(client, rid2)
    profile_after_run = db.get_profile()
    assert profile_after_run != before_profile  # 战败已解锁（在线路径）

    r = client.get(f"/api/expeditions/{exp_id}/replay")
    assert r.status_code == 200
    rep = r.json()
    assert rep["isolated"] is True
    assert rep["expedition"]["status"] == "lost"
    assert [c["chapter"] for c in rep["chapters"]] == [1, 2]
    assert [e["kind"] for e in rep["events"]] == ["create", "chapter_clear", "advance", "settle"]
    # 每章回放逐步重建且校验点全部通过（含第 2 章从交接快照重建初始状态）
    for ch in rep["chapters"]:
        v = ch["replay"]["verification"]
        assert v["mismatch"] == 0 and v["error"] == 0
        assert v["ok"] >= 1
        assert ch["replay"]["steps"]
    # 只读隔离：回放不产生任何存档/解锁副作用
    assert db.get_profile() == profile_after_run
    assert _get_exp(client, exp_id)["expedition"]["status"] == "lost"


def test_chapter_run_replay_verifies_from_carry(client):
    """第 2 章 run 的单局回放：初始状态由 create 事件里的交接快照重建，校验点一致。"""
    data = _create(client, seed=1, chapters=3)
    exp_id = data["expedition"]["id"]
    assert _bot_run_chapter(client, data["run"]["run_id"]) == "won"
    adv = client.post(f"/api/expeditions/{exp_id}/advance", json={}).json()
    rid2 = adv["run"]["run_id"]
    carry_gold = adv["expedition"]["carry"]["gold"]
    assert adv["run"]["gold"] == carry_gold
    # 第 2 章进行若干合法行动
    _enter_first_encounter(client, rid2)
    client.post(f"/api/runs/{rid2}/act", json={"action": "end_turn"})

    rep = client.get(f"/api/runs/{rid2}/replay").json()
    assert rep["verification"]["mismatch"] == 0
    assert rep["verification"]["error"] == 0
    assert rep["verification"]["ok"] >= 2
    # 初始帧即携带交接后的金币与远征摘要
    assert rep["steps"][0]["view"]["gold"] == carry_gold
    assert rep["steps"][0]["view"]["expedition"]["chapter"] == 2


def test_expedition_replay_unknown_id_400(client):
    assert client.get("/api/expeditions/nope/replay").status_code == 400
    assert client.get("/api/expeditions/nope").status_code == 400
