from __future__ import annotations

# 卡牌成长树：在锻造节点花金币为“指定卡牌实例”解锁一个成长节点。
# 同名卡的每张副本是独立实例（uid），各自保存已解锁节点序列与每笔成本；
# 引擎在战斗中按实例的 growth 把基础卡牌定义即时换算为“生效卡牌”，
# 因此成长自动贯通战斗/续局/回放/跨章继承。
#
# 树结构（有向无环图）：
#   T1 入口（无前置）：sharpen 锋锐 / empower 强效 / refine 精炼
#   T2 分支（需 T1，同 lane 互斥）：
#     sharpen  -> keen 破甲 | bulwark 壁垒
#     empower  -> amplify 激化 | insight 洞察
#     refine   -> economize 节流 | streamline 顺发
#   T3 终阶（需对应 T2，同 lane 互斥）：
#     keen       -> rend 裂甲 | execute 处决
#     bulwark    -> fortress 堡垒 | thorns 反震
#     amplify    -> overload 超载 | attunement 共鸣
#     insight    -> enlightenment 启迪 | flow 涌动
#     economize  -> frugality 俭省 | mastery 精通
#     streamline -> gush 涌泉 | zero_form 零式
# 三条 lane 之间不互斥（可以先锋锐再强效），但同一层的分支互斥，
# 每个节点一生只能解锁一次；同一锻造节点只允许一次锻造操作。

# 旧版一次锻造的金币花费（旧 forges 日志/旧档迁移按此价格逐笔记账）
FORGE_COST = 25

# 各层节点金币成本
TIER_COST = {1: FORGE_COST, 2: 40, 3: 60}

# empower 作用的“非攻击数值”效果类型
_EMPOWER_TYPES = ("apply_status", "set_status", "draw", "heal", "gain_energy")


# ---------- 节点变换（每个节点一个纯函数，改的是深拷贝） ----------
def _has(effects, t):
    return any(e.get("type") == t for e in effects)


def _damage_effects(effects):
    return [e for e in effects if e.get("type") == "damage"]


def _t_sharpen(card, effects):
    # 所有攻击伤害 +3；没有攻击效果的牌改为格挡 +3
    if _has(effects, "damage"):
        for e in _damage_effects(effects):
            e["value"] = e.get("value", 0) + 3
    elif _has(effects, "gain_block"):
        for e in effects:
            if e.get("type") == "gain_block":
                e["value"] = e.get("value", 0) + 3


def _t_empower(card, effects):
    # 非攻击数值（状态层数/抽牌/回复/能量）+1；没有则攻击伤害 +2
    targets = [e for e in effects if e.get("type") in _EMPOWER_TYPES]
    if targets:
        for e in targets:
            e["value"] = e.get("value", 0) + 1
    else:
        for e in _damage_effects(effects):
            e["value"] = e.get("value", 0) + 2


def _t_refine(card, effects):
    # 费用 -1（最低 0）
    card["cost"] = max(0, card.get("cost", 0) - 1)


def _t_keen(card, effects):
    # 攻击伤害额外 +2，且施加 1 层易伤（无伤害的牌：格挡 +2）
    if _has(effects, "damage"):
        for e in _damage_effects(effects):
            e["value"] = e.get("value", 0) + 2
        effects.append({"type": "apply_status", "status": "vulnerable", "value": 1,
                        "stack": "add", "target": "enemy", "ticks": 2})
    elif _has(effects, "gain_block"):
        for e in effects:
            if e.get("type") == "gain_block":
                e["value"] = e.get("value", 0) + 2


def _t_bulwark(card, effects):
    # 获得 6 点额外格挡；纯攻击牌也附加 6 格挡
    if _has(effects, "gain_block"):
        for e in effects:
            if e.get("type") == "gain_block":
                e["value"] = e.get("value", 0) + 6
    else:
        effects.insert(0, {"type": "gain_block", "value": 6, "target": "player"})


def _t_amplify(card, effects):
    # 非攻击数值额外 +2；没有则攻击伤害 +3
    targets = [e for e in effects if e.get("type") in _EMPOWER_TYPES]
    if targets:
        for e in targets:
            e["value"] = e.get("value", 0) + 2
    else:
        for e in _damage_effects(effects):
            e["value"] = e.get("value", 0) + 3


def _t_insight(card, effects):
    # 抽牌数 +1；原本不抽牌的牌附加抽 1
    if _has(effects, "draw"):
        for e in effects:
            if e.get("type") == "draw":
                e["value"] = e.get("value", 0) + 1
    else:
        effects.append({"type": "draw", "value": 1})


def _t_economize(card, effects):
    # 费用再 -1（最低 0）
    card["cost"] = max(0, card.get("cost", 0) - 1)


def _t_streamline(card, effects):
    # 抽 1 张牌（攻防之外的润滑效果）
    if not _has(effects, "draw"):
        effects.append({"type": "draw", "value": 1})


def _t_rend(card, effects):
    # 易伤额外 +2 层（破甲节点已保证有施加易伤效果）
    vulns = [e for e in effects if e.get("type") == "apply_status"
             and e.get("status") == "vulnerable"]
    for e in vulns:
        e["value"] = e.get("value", 0) + 2


def _t_execute(card, effects):
    # 攻击伤害再 +4
    for e in _damage_effects(effects):
        e["value"] = e.get("value", 0) + 4


def _t_fortress(card, effects):
    # 格挡额外 +8
    for e in effects:
        if e.get("type") == "gain_block":
            e["value"] = e.get("value", 0) + 8


def _t_thorns(card, effects):
    # 获得 2 点力量（攻击牌的间接成长，对纯格挡牌同样生效）
    effects.append({"type": "apply_status", "status": "strength", "value": 2,
                    "stack": "add", "target": "player"})


def _t_overload(card, effects):
    # 非攻击数值再 +2；没有则伤害 +3
    targets = [e for e in effects if e.get("type") in _EMPOWER_TYPES]
    if targets:
        for e in targets:
            e["value"] = e.get("value", 0) + 2
    else:
        for e in _damage_effects(effects):
            e["value"] = e.get("value", 0) + 3


def _t_attunement(card, effects):
    # 获得 1 点能量
    if not _has(effects, "gain_energy"):
        effects.append({"type": "gain_energy", "value": 1})


def _t_enlightenment(card, effects):
    # 抽牌数再 +1
    if _has(effects, "draw"):
        for e in effects:
            if e.get("type") == "draw":
                e["value"] = e.get("value", 0) + 1


def _t_flow(card, effects):
    # 获得 1 点能量（涌动：流畅出牌循环）
    if not _has(effects, "gain_energy"):
        effects.append({"type": "gain_energy", "value": 1})


def _t_frugality(card, effects):
    # 费用第三次 -1（最低 0）
    card["cost"] = max(0, card.get("cost", 0) - 1)


def _t_mastery(card, effects):
    # 抽 1 张牌（精通：低成本牌的运转补偿）
    if not _has(effects, "draw"):
        effects.append({"type": "draw", "value": 1})


def _t_gush(card, effects):
    # 抽牌数再 +1
    if _has(effects, "draw"):
        for e in effects:
            if e.get("type") == "draw":
                e["value"] = e.get("value", 0) + 1


def _t_zero_form(card, effects):
    # 费用归零（零式：无条件 0 费）
    card["cost"] = 0


def _node(nid, name, tag, tier, desc, fn, requires=(), mutex_with=()):
    return {
        "id": nid, "name": name, "tag": tag, "tier": tier,
        "cost": TIER_COST[tier], "requires": tuple(requires),
        "mutex_with": tuple(mutex_with), "desc": desc, "_fn": fn,
    }


# ---------- 成长树定义 ----------
_NODES = {
    # T1：入口（旧三分支语义保留）
    "sharpen": _node("sharpen", "锋锐", "锋", 1,
                     "所有攻击伤害 +3；若该牌没有攻击效果，则格挡 +3。", _t_sharpen),
    "empower": _node("empower", "强效", "强", 1,
                     "非攻击数值（状态层数/抽牌/回复/能量）+1；没有此类效果时攻击伤害 +2。",
                     _t_empower),
    "refine": _node("refine", "精炼", "炼", 1, "费用 -1（最低 0）。", _t_refine),

    # T2：锋锐 lane（二选一）
    "keen": _node("keen", "破甲", "破", 2,
                  "需要「锋锐」。攻击伤害 +2，并施加 1 层易伤；无攻击效果时格挡 +2。",
                  _t_keen, requires=("sharpen",),
                  mutex_with=("bulwark",)),
    "bulwark": _node("bulwark", "壁垒", "壁", 2,
                     "需要「锋锐」。获得的格挡 +6；纯攻击牌也附加 6 点格挡。",
                     _t_bulwark, requires=("sharpen",),
                     mutex_with=("keen",)),

    # T2：强效 lane（二选一）
    "amplify": _node("amplify", "激化", "激", 2,
                     "需要「强效」。非攻击数值再 +2；没有此类效果时攻击伤害 +3。",
                     _t_amplify, requires=("empower",),
                     mutex_with=("insight",)),
    "insight": _node("insight", "洞察", "察", 2,
                     "需要「强效」。抽牌数 +1；原本不抽牌的牌附加抽 1。",
                     _t_insight, requires=("empower",),
                     mutex_with=("amplify",)),

    # T2：精炼 lane（二选一）
    "economize": _node("economize", "节流", "节", 2,
                       "需要「精炼」。费用再 -1（最低 0）。",
                       _t_economize, requires=("refine",),
                       mutex_with=("streamline",)),
    "streamline": _node("streamline", "顺发", "顺", 2,
                        "需要「精炼」。附加抽 1 张牌。",
                        _t_streamline, requires=("refine",),
                        mutex_with=("economize",)),

    # T3：破甲分支（二选一）
    "rend": _node("rend", "裂甲", "裂", 3,
                  "需要「破甲」。施加的易伤额外 +2 层。",
                  _t_rend, requires=("keen",),
                  mutex_with=("execute",)),
    "execute": _node("execute", "处决", "决", 3,
                     "需要「破甲」。攻击伤害再 +4。",
                     _t_execute, requires=("keen",),
                     mutex_with=("rend",)),

    # T3：壁垒分支（二选一）
    "fortress": _node("fortress", "堡垒", "垒", 3,
                      "需要「壁垒」。获得的格挡额外 +8。",
                      _t_fortress, requires=("bulwark",),
                      mutex_with=("thorns",)),
    "thorns": _node("thorns", "反震", "反", 3,
                    "需要「壁垒」。获得 2 点力量。",
                    _t_thorns, requires=("bulwark",),
                    mutex_with=("fortress",)),

    # T3：激化分支（二选一）
    "overload": _node("overload", "超载", "载", 3,
                      "需要「激化」。非攻击数值再 +2；没有时攻击伤害 +3。",
                      _t_overload, requires=("amplify",),
                      mutex_with=("attunement",)),
    "attunement": _node("attunement", "共鸣", "鸣", 3,
                        "需要「激化」。获得 1 点能量。",
                        _t_attunement, requires=("amplify",),
                        mutex_with=("overload",)),

    # T3：洞察分支（二选一）
    "enlightenment": _node("enlightenment", "启迪", "迪", 3,
                           "需要「洞察」。抽牌数再 +1。",
                           _t_enlightenment, requires=("insight",),
                           mutex_with=("flow",)),
    "flow": _node("flow", "涌动", "涌", 3,
                  "需要「洞察」。获得 1 点能量。",
                  _t_flow, requires=("insight",),
                  mutex_with=("enlightenment",)),

    # T3：节流分支（二选一）
    "frugality": _node("frugality", "俭省", "俭", 3,
                       "需要「节流」。费用第三次 -1（最低 0）。",
                       _t_frugality, requires=("economize",),
                       mutex_with=("mastery",)),
    "mastery": _node("mastery", "精通", "通", 3,
                     "需要「节流」。附加抽 1 张牌。",
                     _t_mastery, requires=("economize",),
                     mutex_with=("frugality",)),

    # T3：顺发分支（二选一）
    "gush": _node("gush", "涌泉", "泉", 3,
                  "需要「顺发」。抽牌数再 +1。",
                  _t_gush, requires=("streamline",),
                  mutex_with=("zero_form",)),
    "zero_form": _node("zero_form", "零式", "零", 3,
                       "需要「顺发」。费用直接归零。",
                       _t_zero_form, requires=("streamline",),
                       mutex_with=("gush",)),
}

NODE_IDS = set(_NODES)

# 旧版 forges 分支 id -> 成长树 T1 节点（语义等价的入口节点）
LEGACY_BRANCH_TO_NODE = {"sharpen": "sharpen", "empower": "empower", "refine": "refine"}

# 旧版重复选择同一分支时，按确定性默认链继续展开（保留付款笔数对应的成长强度）。
# 链上每个节点都是下一节点的前置；链内节点天然不互斥。
LEGACY_CHAINS = {
    "sharpen": ["sharpen", "keen", "rend"],
    "empower": ["empower", "amplify", "overload"],
    "refine": ["refine", "economize", "frugality"],
}


def node_def(nid):
    return _NODES.get(nid)


def node_name(nid):
    n = _NODES.get(nid)
    return n["name"] if n else nid


def growth_node_cost(nid):
    n = _NODES.get(nid)
    return n["cost"] if n else None


def public_tree():
    """成长树只读定义（供前端渲染与提示；不含内部变换函数）。"""
    return [{k: v[k] for k in ("id", "name", "tag", "tier", "cost",
                               "requires", "mutex_with", "desc")}
            for v in _NODES.values()]


# ---------- 实例成长状态 ----------
def acquired_nodes(inst):
    return [r["node"] for r in inst.get("growth", [])]


def growth_spent(inst):
    """该实例累计投入的锻造金币（随视口下发，便于展示总成本）。"""
    return sum(int(r.get("cost", 0)) for r in inst.get("growth", []))


def validate_unlock(inst, nid):
    """校验能否为实例解锁节点 nid。通过返回 None，否则返回中文原因。"""
    n = _NODES.get(nid)
    if n is None:
        return "unknown growth node"
    owned = set(acquired_nodes(inst))
    if nid in owned:
        return "growth node already acquired"
    missing = [r for r in n["requires"] if r not in owned]
    if missing:
        return "prerequisite not met: " + ",".join(missing)
    blocked = next((m for m in n["mutex_with"] if m in owned), None)
    if blocked:
        return f"mutex branch already taken: {blocked}"
    return None


# 稳定错误码：service 层据此区分 400（规则拒绝）与 409（幂等冲突）
ERR_UNKNOWN = "unknown growth node"
ERR_ACQUIRED = "growth node already acquired"
ERR_MUTEX_PREFIX = "mutex branch already taken"
ERR_PREREQ_PREFIX = "prerequisite not met"


def available_nodes(inst):
    """该实例当前可解锁（前置满足、未解锁、未被互斥）的节点 id 列表，按 tier/id 稳定排序。"""
    owned = set(acquired_nodes(inst))
    out = []
    for nid, n in _NODES.items():
        if nid in owned:
            continue
        if any(r not in owned for r in n["requires"]):
            continue
        if any(m in owned for m in n["mutex_with"]):
            continue
        out.append(nid)
    return sorted(out, key=lambda x: (_NODES[x]["tier"], x))


def effective_card(base_card, growth):
    """把基础卡牌定义按实例已解锁成长节点换算成生效卡牌（深拷贝，不污染注册表）。

    growth 兼容三种入参：
    - 新结构：[{"node": ..., "cost": ...}]（按解锁顺序施加，节点变换相互叠加）；
    - 节点 id 列表（如 ["sharpen", "refine"]）：引擎 _card_def 的历史调用形态；
    - 旧分支 id 列表（迁移前的直接调用）：T1 id 与节点 id 同名，等价处理。
    """
    card = dict(base_card)
    effects = [dict(e) for e in card.get("effects", [])]
    node_ids = []
    for item in growth or ():
        if isinstance(item, dict):
            node_ids.append(item.get("node"))
        else:
            node_ids.append(item)
    for nid in node_ids:
        n = _NODES.get(nid)
        if n is None:
            continue
        n["_fn"](card, effects)
    card["effects"] = effects
    return card


# ---------- 旧强化记录迁移 ----------
def migrate_forges_to_growth(forges):
    """旧版 forges（分支 id 列表，可重复）-> 新版成长记录列表。

    迁移规则：
    - 每条旧记录对应一次真实付款（25 金），按分支进入其默认展开链；
    - 链上“下一个尚未解锁的节点”成为本条记录解锁的节点，成本记 25；
    - 链已全部解锁的多余付款不再产生节点（实际不可能在同一节点连续锻造
      同一分支，但损坏/手工档可能出现；不退款、不报错）；
    - 不同分支的链互不互斥；同一 lane 内只会沿同一条默认链走，不会撞互斥。
    返回 (records, changed)。
    """
    records = []
    owned = set()
    changed = False
    for bid in forges or ():
        changed = True
        nid = LEGACY_BRANCH_TO_NODE.get(bid, bid)
        chain = LEGACY_CHAINS.get(bid)
        picked = None
        if chain:
            picked = next((c for c in chain if c not in owned), None)
        elif nid in _NODES and nid not in owned:
            picked = nid
        if picked is None:
            continue
        owned.add(picked)
        records.append({"node": picked, "cost": FORGE_COST})
    return records, changed
