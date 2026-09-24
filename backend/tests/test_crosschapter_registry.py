"""跨章状态统一交接/回放策略（crosschapter 注册表）。

把远征委托、奇遇印记、药水背包、伙伴、协作队伍归属等跨章状态纳入同一策略后，
本文件锁定注册表本身的不变量，防止以后新增维度时只改一处导致交接/迁移/回放
分叉：

- FIELDS 注册的每个维度都必须出现在交接快照（extract_carry）与新章重建
  （_new_run_state + value_for_new_run）里，且在线开章与回放重建逐位相同；
- 每个维度都参与旧档迁移（migrate_run_state：缺字段补默认值，幂等）；
- 每个维度都有旧版 create 校验点形状（state_checkpoint 的 include_* 开关 +
  2^N 候选匹配），缺维旧档回放逐位可重建；
- 损坏旧快照里的 NULL 值在交接/公开视口边界被兜底，不穿透；
- 多人协作（coop_team）作为注册维随交接快照跨章，推进后队伍归属不丢。
"""
import uuid

from app import db
from app import mapgen
from app import service
from app import crosschapter as xc


# ---------- 注册表内部不变量 ----------
def test_every_registered_field_has_unique_key_version_and_marker():
    keys = [f.key for f in xc.FIELDS]
    assert len(keys) == len(set(keys))
    assert set(keys) == {"companion", "potions", "enc_state", "coop_team"}
    assert len({f.since for f in xc.FIELDS}) == len(xc.FIELDS)
    assert len({f.marker_key for f in xc.FIELDS}) == len(xc.FIELDS)
    # 维序即规则演进顺序（旧版本候选必须是完整候选的后缀）
    by = {f.key: f for f in xc.FIELDS}
    assert by["potions"].since == "2.5.0"
    assert by["companion"].since == "2.6.0"
    assert by["enc_state"].since == "2.8.0"
    assert by["coop_team"].since == "2.9.0"
    # 回放瞬态标记一律不参与校验点哈希
    for f in xc.FIELDS:
        assert f.marker_key in service._CKPT_SKIP_KEYS


def test_extract_carry_covers_every_registered_field_and_identity():
    state = service._new_run_state(123)
    state["chapter"] = 1
    state["chapters_total"] = 3
    state["expedition_id"] = "exp1"
    carry = xc.extract_carry(state)
    for f in xc.FIELDS:
        assert f.key in carry, f"跨章维 {f.key} 必须进入交接快照"
    # 委托与远征身份同样在交接快照里
    for key in ("deck", "card_instances", "next_card_seq", "relics", "gold",
                "max_health", "health", "base_energy", "commissions",
                "next_commission_seq", "chapter", "chapters_total"):
        assert key in carry
    # 深拷贝解耦：改快照不回写 run
    carry["potions"].append("hp")
    assert state["potions"] == []


def test_new_run_state_roundtrips_every_field_through_carry():
    state = service._new_run_state(999)
    state["chapter"] = 1
    state["chapters_total"] = 2
    state["potions"] = ["hp", "fire"]
    state["companion"] = {"id": "squire", "name": "见习卫士 艾琳",
                          "mode": "accompany", "hp": 12, "wounded": False}
    state["enc_state"]["flags"]["wt_aided"] = 1
    state["coop_team"] = "t_abc"
    state["commissions"] = []
    carry = xc.extract_carry(state)
    rebuilt = service._new_run_state(
        service._chapter_seed(999, 2), carry=carry, chapter=2,
        chapters_total=2, expedition_id="expX", coop_team="t_abc")
    # 各注册维随章带入；印记 flag 保留并在开章兑现预兆
    assert rebuilt["potions"] == ["hp", "fire"]
    assert rebuilt["companion"] and rebuilt["companion"]["hp"] == 12
    assert rebuilt["enc_state"]["flags"].get("wt_aided") == 1
    assert rebuilt["coop_team"] == "t_abc"
    # 新 run 身份只认真实入参，不被 carry 来源章号污染（2.4.0 修复点）
    assert rebuilt["chapter"] == 2
    assert rebuilt["expedition_id"] == "expX"


def test_carried_state_is_bit_identical_between_live_advance_and_replay():
    """在线推进与回放重建共用 _new_run_state：同 carry+入参必得同一校验点。"""
    state = service._new_run_state(7)
    state["chapter"] = 1
    state["chapters_total"] = 2
    state["potions"] = ["block"]
    carry = xc.extract_carry(state)
    a = service._new_run_state(service._chapter_seed(7, 2), carry=carry,
                               chapter=2, chapters_total=2, expedition_id="e")
    b = service._new_run_state(service._chapter_seed(7, 2), carry=xc.extract_carry(state),
                               chapter=2, chapters_total=2, expedition_id="e")
    assert service.state_checkpoint(a) == service.state_checkpoint(b)


# ---------- 旧档迁移 ----------
def test_migrate_run_state_fills_defaults_idempotently():
    run = {"rules_version": "2.4.0"}
    changed = xc.migrate_run_state(run)
    assert changed is True
    assert run["potions"] == []
    assert run["companion"] is None
    assert run["enc_state"]["flags"] == {}
    assert run["coop_team"] is None
    # 再跑一次幂等：无变化
    assert xc.migrate_run_state(run) is False


def test_migrate_normalizes_corrupt_companion_and_enc_state():
    run = {
        "potions": ["hp"],
        "companion": {"id": "squire", "hp": 99},   # 越界生命
        "enc_state": {"flags": "not-a-dict"},      # 损坏结构
        "coop_team": None,
    }
    changed = xc.migrate_run_state(run)
    assert changed is True
    assert run["companion"]["hp"] == 20
    assert isinstance(run["enc_state"]["flags"], dict)
    assert run["enc_state"]["pending"] is None


def test_migration_on_load_is_atomic_and_replay_is_clean():
    """缺所有注册维的旧 run 首次续局迁移后，回放零 mismatch/error。"""
    run_id = uuid.uuid4().hex[:12]
    state = service._new_run_state(55)
    for f in xc.FIELDS:
        state.pop(f.key, None)
    state["rules_version"] = "2.4.0"
    map_data = mapgen.generate_map(55)
    with db.transaction() as conn:
        db.insert_run(conn, run_id, 55, state["status"], state["position"],
                      map_data, state)
        db.append_event_conn(conn, run_id, 1, "create", {"seed": 55, "ver": "2.4.0"})
    view = service.resume(run_id)
    assert view["potions"] == [] and view["companion"] is None
    rep = service.replay(run_id)
    v = rep["verification"]
    assert v["mismatch"] == 0 and v["error"] == 0


# ---------- 旧版 create 校验点形状 ----------
def test_state_checkpoint_shape_kwargs_strip_registered_fields():
    state = service._new_run_state(321)
    full = service.state_checkpoint(state)
    assert service.state_checkpoint(state, include_potions=False) != full
    assert service.state_checkpoint(state, include_companion=False) != full
    assert service.state_checkpoint(state, include_encounters=False) != full
    assert service.state_checkpoint(state, include_coop=False) != full
    # 剥维后改该维不再影响哈希（旧版服务器录制语义）
    state["potions"].append("hp")
    assert service.state_checkpoint(state, include_potions=False) == \
        service.state_checkpoint(service._new_run_state(321), include_potions=False)


def test_create_candidates_match_legacy_shapes_for_all_dimensions():
    """模拟 2^N 种旧 create 形状录制：

    候选匹配的核心契约是「录制哈希必须在候选集合里、且返回的比对值与录制
    逐位相等」。注意：某维默认值为 None 时（如未开协作的 coop_team），
    剥不剥该维哈希天然相同，此时形状匹配会落到完整形状（安全方向）——
    这不影响逐位校验，因为两者本就等价。
    """
    import itertools
    seed = 888
    for drop_bits in itertools.product((False, True), repeat=len(xc.FIELDS)):
        state = service._new_run_state(seed)
        present = {}
        dropped_sinces = []
        for f, drop in zip(xc.FIELDS, drop_bits):
            present[f.key] = not drop
            if drop:
                dropped_sinces.append(f.since)
                state.pop(f.key, None)
        ver = "2.0.0" if dropped_sinces else service.RULES_VERSION
        state["rules_version"] = ver  # 旧录制的状态标签与事件 ver 一致
        ckpt = service.state_checkpoint(state, **xc.checkpoint_kwargs(present))
        # 该形状哈希是否恰好等价于完整形状哈希（None 维剥除时可能成立）
        full_state = service._new_run_state(seed)
        full_state["rules_version"] = ver
        full_ckpt = service.state_checkpoint(full_state)
        payload = {"seed": seed, "ver": ver, "ckpt": ckpt}
        _sim, fixed, buggy, matched = service._create_ckpt_candidates(seed, payload)
        assert buggy is None  # 普通局（无 carry）不存在 2.4 章号错位候选
        assert fixed == ckpt  # 比对值逐位等于录制值（create 帧可标 ok）
        if ckpt != full_ckpt:
            # 只有哈希真正不同（剥除的是非 None 维）时，匹配形状才必须精确
            assert matched == present, (drop_bits, matched)
        else:
            # 等价形状：匹配落到完整形状（pre_* 全 False，后续严格校验，安全）
            assert all(matched.values())


# ---------- 损坏快照 NULL 兜底 ----------
def test_extract_and_public_carry_tolerate_null_field_values():
    state = service._new_run_state(44)
    state["potions"] = None
    carry = xc.extract_carry(state)
    assert carry["potions"] == []
    # 公开视口直接面对数据库里的损坏 carry（不经过 extract）也要兜住：
    # 注册维的列表/结构为 NULL 时不穿透到序列化/视口。
    public = service._carry_public({
        "deck": [], "card_instances": {}, "potions": None,
        "enc_state": None, "commissions": None, "chapter": 1})
    assert public["potions"] == []
    assert public["commissions"] == []
    assert public["encounter_flags"] == []


# ---------- 多人协作：coop_team 是注册维，随交接跨章 ----------
def test_coop_team_carries_through_advance_and_replay(client):
    team = service.create_coop_team(captain_name="队长", seed=31, chapters=2)
    tid = team["id"]
    cap = next(m["id"] for m in team["members"] if m["name"] == "队长")
    joined = service.join_coop_team(team["code"], "队员甲")
    fighter = next(m["id"] for m in joined["members"] if m["id"] != cap)
    started = service.start_coop_expedition(tid, cap, request_id="s1")
    eid = started["expedition"]["id"]
    rid = started["run"]["run_id"]
    # 直接把当前章 run 置为 won（队长共享池协作金也在状态里）
    rec = service.load_run(rid)
    rec["state"]["status"] = "won"
    db.save_run(rid, "won", rec["state"]["position"], rec["state"])
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM expeditions WHERE id=?", (eid,)).fetchone()
        service._sync_expedition_conn(conn, eid,
                                      {"id": rid, "chapter": 1,
                                       "expedition_id": eid},
                                      rec["state"])
    adv = service.advance_coop_expedition(tid, cap, request_id="a1")
    rid2 = adv["run"]["run_id"]
    # coop_team 作为注册维随交接快照带入第 2 章（在线状态与 create 事件一致）
    assert service.load_run(rid2)["state"]["coop_team"] == tid
    create_ev = next(e for e in db.load_events(rid2) if e["action"] == "create")
    assert create_ev["payload"].get("carry", {}).get("coop_team") == tid
    # 第 2 章回放：create 完整形状（coop 维在场）校验通过
    rep = service.replay(rid2)
    assert rep["verification"]["mismatch"] == 0
    assert rep["verification"]["error"] == 0
    # 权限边界仍然生效：战斗位越权资源动作 403，零副作用
    view = service.resume(rid2, member_id=fighter)
    node = view["reachable"][0]["id"]
    try:
        service.act(rid2, {"action": "choose_node", "node": node},
                    member_id=fighter)
        assert False, "战斗位越权选节点应 403"
    except service.PermissionDenied:
        pass
    # 越权后状态不变（失败回滚）：位置仍在 start，回放仍干净
    assert service.resume(rid2)["position"] == "start"
    assert service.replay(rid2)["verification"]["mismatch"] == 0
