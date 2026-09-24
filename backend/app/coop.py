"""多人协作远征（规则 2.9.0）——纯领域逻辑：角色、权限边界、队伍奖励常量。

协作远征在既有远征链路之上叠加一层「队伍」：
- 队长（leader）组建队伍、分配角色，开赛前后均承担管理职责（开章/推进/解散）；
- 战斗位（combat）成员只能提交战斗动作（打牌/结束回合/战斗中用药）；
- 资源位（supply）成员承担所有非战斗操作（路线/锻造/商店/药水整理/伙伴/委托/奇遇）；
- 队长两个领域都可操作。共享章节状态就是同一个章节 run（同一条确定性动作日志），
  所有成员的操作经同一事务串行化、request_id 幂等、expected_rev 状态守卫，
  同步结算与单人远征完全同路径。

队伍奖励（全部确定性，纯动作序列的函数 -> 续局/整程回放逐位一致）：
- 章节通关协作金 CHAPTER_CLEAR_BONUS：击败非终章/终章首领即入【共享金币池】
  （run.gold，随交接快照带入下一章），纯推演内完成，进 run 校验点；
- 战斗胜利记一次「讨伐」贡献给实际操作者；
- 资源操作（商店交易/锻造/接取·领取委托/奇遇抉择/招募伙伴）记一次「后勤」贡献；
- 章节通关与远征终胜时，按当时在册成员均分一份名义奖励（个人战利记录，
  不进共享池、不影响 run 状态），落在 coop_ledger，供整程回放与结算页呈现。

失败回退：
- 越权动作在【任何状态变更之前】拒绝（403，零副作用）；
- 业务校验失败（金币不足/格位非法等）沿用既有事务整体回滚；
- 章节战败 -> 远征与队伍同事务结算为 lost，已入账的章节通关协作金不追回
  （它们已在通过的章节里落袋），终胜名义奖励不发放。
"""
from __future__ import annotations

import random
import string

LEADER = "leader"          # 队长：战斗 + 资源 + 队伍管理
COMBAT = "combat"          # 战斗位：只承担战斗操作
SUPPLY = "supply"          # 资源位：只承担非战斗操作
ROLES = (LEADER, COMBAT, SUPPLY)
ASSIGNABLE_ROLES = (COMBAT, SUPPLY)  # 队长为队员分配角色时的可选值（队长身份不可转）

ROLE_LABELS = {
    LEADER: "队长",
    COMBAT: "战斗位",
    SUPPLY: "资源位",
}
ROLE_ICONS = {LEADER: "👑", COMBAT: "⚔️", SUPPLY: "🎒"}

MAX_MEMBERS = 4            # 队伍人数上限（含队长）
MIN_START_MEMBERS = 2      # 开赛最低人数（1 人请走单人远征）
TEAM_NAME_MAX = 24
MEMBER_NAME_MAX = 12

# ---------- 队伍奖励 ----------
CHAPTER_CLEAR_BONUS = 15   # 每章首领击败：协作金入共享金币池（随交接跨章）
FINAL_WIN_BONUS = 50       # 征服终章首领：每位在册成员的名义战利（不入共享池）

# 个人贡献/战利类型
C_BATTLE = "battle_win"    # 战斗胜利（实际操作者 +1）
C_RESOURCE = "resource"    # 后勤操作（实际操作者 +1）
C_CHAPTER = "chapter"      # 章节通关均分名义金
C_WIN = "win"              # 终章通关均分名义金

# ---------- 动作权限分类 ----------
BATTLE_ACTIONS = frozenset({
    "play", "end_turn", "use_potion",
})
RESOURCE_ACTIONS = frozenset({
    "choose_node", "claim_reward", "forge",
    "shop_buy", "shop_remove",
    "discard_potion", "companion_set_mode",
    "commission_accept", "commission_claim",
    "encounter_choice",
})
# create 等仅由系统产生，不出现在权限表中
ACTION_KIND = {a: "battle" for a in BATTLE_ACTIONS}
ACTION_KIND.update({a: "resource" for a in RESOURCE_ACTIONS})


def action_kind(action):
    """动作 -> 领域（battle/resource）；未登记动作返回 None（管理/系统动作）。"""
    return ACTION_KIND.get(action)


def can_perform(role, action):
    """角色是否有权提交该动作。未登记的动作（系统内部）默认拒绝显式提交。"""
    if role == LEADER:
        return action in BATTLE_ACTIONS or action in RESOURCE_ACTIONS
    if role == COMBAT:
        return action in BATTLE_ACTIONS
    if role == SUPPLY:
        return action in RESOURCE_ACTIONS
    return False


def role_label(role):
    return ROLE_LABELS.get(role, role)


# ---------- 入队码 ----------
# 去掉易混字符（0/O、1/I/L），6 位大写字母数字
_CODE_ALPHABET = "".join(c for c in (string.ascii_uppercase + string.digits)
                         if c not in "0O1IL")
CODE_LEN = 6


def gen_join_code(rng=None):
    rng = rng or random
    return "".join(rng.choice(_CODE_ALPHABET) for _ in range(CODE_LEN))


def unique_join_code(existing, rng=None):
    """生成不与现存入队码冲突的 6 位码（existing 为已占用码集合）。"""
    rng = rng or random
    for _ in range(100):
        code = gen_join_code(rng)
        if code not in existing:
            return code
    # 极端情况兜底（随机空间 30^6，正常走不到）
    while True:
        code = gen_join_code(rng)
        if code not in existing:
            return code


# ---------- 个人战利记录 ----------
def ledger_entry(kind, amount, chapter, detail=None, member_id=None, member_name=None):
    """构造一条个人奖励/贡献流水（coop_ledger 行 payload）。"""
    return {
        "kind": kind,
        "amount": amount,
        "chapter": chapter,
        "detail": detail or {},
        "member_id": member_id,
        "member_name": member_name,
    }


def split_bonus(total, members):
    """把总额确定性均分给成员：整除余数按成员 id 升序前若干位各 +1。"""
    n = len(members)
    base = total // n
    rem = total - base * n
    order = sorted(members, key=lambda m: m["id"])
    extra = [m["id"] for m in order[:rem]]
    return {m["id"]: base + (1 if m["id"] in extra else 0) for m in members}


# ---------- 只读视口 ----------
def member_public(m, me=False):
    return {
        "id": m["id"],
        "name": m["name"],
        "role": m["role"],
        "role_label": ROLE_LABELS.get(m["role"], m["role"]),
        "icon": ROLE_ICONS.get(m["role"], "•"),
        "is_leader": m["role"] == LEADER,
        "joined_seq": m.get("joined_seq", 0),
        "is_me": me,
        # 令牌仅在「自己」的入会响应里返回，列表视口绝不携带
        "token": m.get("token") if me else None,
    }


def team_public(team, members, me_id=None):
    """队伍大厅/状态视口。members 按 joined_seq 排序。

    成员列表只标 is_me、绝不携带令牌；调用方自己的令牌只在顶层 me 里下发一次。
    """
    members = sorted(members, key=lambda m: m.get("joined_seq", 0))
    name = team.get("name")
    return {
        "id": team["id"],
        "code": team["join_code"],
        "name": name or (f"{members[0]['name']} 的远征队" if members else None),
        "status": team["status"],                 # forming / started / disbanded
        "leader_id": team["leader_id"],
        "seed": team.get("seed"),
        "chapters": team.get("chapters_total"),
        "expedition_id": team.get("expedition_id"),
        "max_members": MAX_MEMBERS,
        "min_start": MIN_START_MEMBERS,
        "members": [
            {**member_public(m, me=False), "is_me": (m["id"] == me_id)}
            for m in members
        ],
        "me": next((member_public(m, me=True) for m in members if m["id"] == me_id), None),
        "assignable_roles": [
            {"id": r, "name": ROLE_LABELS[r], "icon": ROLE_ICONS[r]} for r in ASSIGNABLE_ROLES
        ],
    }


def coop_badge(team, members, chapter, chapters_total, exp_status, me_id=None):
    """随章节 run 视口下发的协作远征摘要（权限边界在客户端的依据）。"""
    members = sorted(members, key=lambda m: m.get("joined_seq", 0))
    return {
        "team_id": team["id"],
        "code": team["join_code"],
        "name": team.get("name"),
        "status": team["status"],
        "leader_id": team["leader_id"],
        "chapter": chapter,
        "chapters_total": chapters_total,
        "expedition_status": exp_status,
        "members": [member_public(m, me=(m["id"] == me_id)) for m in members],
        "me": next((member_public(m, me=True) for m in members if m["id"] == me_id), None),
        "roles": {"leader": LEADER, "combat": COMBAT, "supply": SUPPLY},
        "permissions": {
            "battle_actions": sorted(BATTLE_ACTIONS),
            "resource_actions": sorted(RESOURCE_ACTIONS),
        },
        "rewards": {
            "chapter_clear_bonus": CHAPTER_CLEAR_BONUS,
            "final_win_bonus": FINAL_WIN_BONUS,
        },
    }
