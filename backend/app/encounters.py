from __future__ import annotations

import copy
import random

# 跨章节奇遇链（规则 2.8.0）。
# 玩家在路线上的「奇遇」节点面对剧情抉择并立即承担代价（生命/金币）或获得奖励
# （卡牌/药水/遗物/最大生命）。部分抉择会埋下 flag——flag 随远征交接快照继承，
# 在下一章开头触发「预兆」（首场战斗的开局修正/章节回血），并在后续章节的奇遇
# 节点触发续写事件（报恩/复仇）。所有链内容数据驱动；抉择与结算都是 run 状态
# 的纯推演（service 负责具体的牌组/背包/战斗变更），因此续局与版本化回放天然
# 逐位一致。

LOCAL = "local"   # 单节点链：当场两清（每个章节可各出现一次）
CROSS = "cross"   # 跨章链：起始抉择 -> flag -> 后续章预兆 + 续写事件

CHAINS: dict[str, dict] = {}


def _chain(cid, scope, title, text, choices, root=None,
           continue_chain=False, requires_flag=None,
           min_chapter=1, max_chapter=None):
    CHAINS[cid] = {
        "id": cid,
        "scope": scope,
        "root": root or cid,
        "continue": continue_chain,
        "requires_flag": requires_flag,
        "min_chapter": min_chapter,
        "max_chapter": max_chapter,
        "title": title,
        "text": text,
        "choices": copy.deepcopy(choices),
    }


# ================= 跨章链 1：负伤的旅人 =================
_chain(
    "wounded_traveler", CROSS,
    "负伤的旅人",
    "路边躺着一位衣衫染血的旅人。他捂住腰侧的伤口向你求助，身旁的钱袋鼓鼓囊囊。",
    [
        {"id": "aid", "label": "拿出伤药，搀扶他一程",
         "desc": "失去 6 点生命。旅人感激涕零地记下了你的脸。",
         "cost": {"hp": 6}, "effects": [], "set_flag": "wt_aided"},
        {"id": "rob", "label": "抢走他的钱袋",
         "desc": "立即获得 15 金币，但你在他怨毒的目光中离开了。",
         "cost": {}, "effects": [{"type": "gold", "value": 15}],
         "set_flag": "wt_robbed"},
        {"id": "ignore", "label": "视而不见，绕道离开",
         "desc": "各人有各人的命数。", "cost": {}, "effects": []},
    ],
)

_chain(
    "traveler_gratitude", CROSS,
    "旅人的谢礼",
    "曾被你救助的旅人带着一支商队出现在路口，他一眼就认出了你，快步迎上前来。",
    [
        {"id": "accept", "label": "收下谢礼",
         "desc": "获得遗物「旅人的护符」（每场战斗开局 +1 力量），并回复 8 点生命。",
         "cost": {},
         "effects": [{"type": "relic_set", "relic": "traveler_charm"},
                     {"type": "hp", "value": 8}]},
        {"id": "decline", "label": "婉言谢绝",
         "desc": "他坚持把一袋金币塞进你手里：获得 10 金币。",
         "cost": {}, "effects": [{"type": "gold", "value": 10}]},
    ],
    root="wounded_traveler", continue_chain=True, requires_flag="wt_aided",
    min_chapter=2,
)

_chain(
    "avenger_ambush", CROSS,
    "拦路复仇者",
    "那个被你抢走钱袋的旅人纠集了两名同伴，手持短刀拦住去路：“还钱，还是还命？”",
    [
        {"id": "fight", "label": "拔刀迎战",
         "desc": "与复仇者贴身肉搏。战斗胜利后从他身上搜出 35 金币。",
         "cost": {}, "effects": [{"type": "battle", "enemy": "avenger", "reward_gold": 35}]},
        {"id": "pay", "label": "双倍奉还，破财消灾",
         "desc": "支付 20 金币，他冷笑一声让开道路。",
         "cost": {"gold": 20}, "effects": []},
    ],
    root="wounded_traveler", continue_chain=True, requires_flag="wt_robbed",
    min_chapter=2,
)

# ================= 跨章链 2：迷雾神龛 =================
_chain(
    "mystic_altar", CROSS,
    "迷雾神龛",
    "斑驳的石龛立在路旁，龛中半截残烛无风自动，供盘里压着几枚古旧的钱币。",
    [
        {"id": "pray", "label": "虔诚祈祷（支付 20 金币）",
         "desc": "获得遗物「先驱者徽章」（每场战斗开局获得 6 点格挡）。",
         "cost": {"gold": 20},
         "effects": [{"type": "relic_set", "relic": "vanguard_badge"}]},
        {"id": "blood", "label": "献上鲜血（失去 10 点生命）",
         "desc": "最大生命 +5；神龛赐福将在进入下一章时回复 12 点生命。",
         "cost": {"hp": 10},
         "effects": [{"type": "max_health", "value": 5}],
         "set_flag": "altar_blessed"},
        {"id": "desecrate", "label": "砸碎石龛，卷走供品",
         "desc": "获得 25 金币，但低语的诅咒将在下章首场战斗降临（敌人 +12 生命，你附带易碎 2 回合）。",
         "cost": {}, "effects": [{"type": "gold", "value": 25}],
         "set_flag": "altar_cursed"},
    ],
)

# ================= 本地链（当场结清） =================
_chain(
    "ancient_cache", LOCAL,
    "古老的宝箱",
    "一只缠满铁链的旧木箱半埋在土里，锁孔泛着幽蓝的光。",
    [
        {"id": "force", "label": "强行撬开",
         "desc": "承受 5 点机关反噬，获得卡牌「铁波」。",
         "cost": {"hp": 5}, "effects": [{"type": "add_card", "card": "iron_wave"}]},
        {"id": "pick", "label": "花 15 金币打发锁簧",
         "desc": "获得一瓶治疗药水与 10 金币（需要药水背包空位）。",
         "cost": {"gold": 15},
         "effects": [{"type": "add_potion", "potion": "hp"},
                     {"type": "gold", "value": 10}]},
        {"id": "leave", "label": "贴上封条离开",
         "desc": "心安理得，回复 4 点生命。",
         "cost": {}, "effects": [{"type": "hp", "value": 4}]},
    ],
)

_chain(
    "wandering_sage", LOCAL,
    "云游智者",
    "白须智者在树下煮茶，茶香苦涩清冽。他抬眼示意你坐下聊聊。",
    [
        {"id": "spar", "label": "请教实战之道",
         "desc": "与他的木人阵切磋（失去 4 点生命），获得 20 金币酬劳。",
         "cost": {"hp": 4}, "effects": [{"type": "gold", "value": 20}]},
        {"id": "tea", "label": "喝一杯苦茶",
         "desc": "回复 10 点生命。",
         "cost": {}, "effects": [{"type": "hp", "value": 10}]},
        {"id": "leave", "label": "拱手告别",
         "desc": "相忘于江湖。", "cost": {}, "effects": []},
    ],
)

_chain(
    "dry_well", LOCAL,
    "枯井低语",
    "路旁一口枯井里传来细碎的叮当声，像有钱币在井底自己滚动。",
    [
        {"id": "wish", "label": "投入 10 金币许愿",
         "desc": "井水温热地涌上，回复 15 点生命。",
         "cost": {"gold": 10}, "effects": [{"type": "hp", "value": 15}]},
        {"id": "climb", "label": "攀下井底看个究竟",
         "desc": "失去 7 点生命，捡回一瓶炽焰药水（需要药水背包空位）。",
         "cost": {"hp": 7}, "effects": [{"type": "add_potion", "potion": "fire"}]},
        {"id": "leave", "label": "不去招惹",
         "desc": "叮当声在身后渐渐消失。", "cost": {}, "effects": []},
    ],
)

# 无链可选时的兜底奇遇
FALLBACK_HEAL = 6
FALLBACK_TITLE = "林间清泉"
FALLBACK_TEXT = "没有奇遇发生。你在一眼清泉边歇脚，喝了口水继续上路。"

# flag -> 下一章开头的「预兆」。battle 修正作用于该章首场战斗（一次性消耗），
# heal 在开章休整之外直接回复。是否保留 flag 取决于有没有续写链注册：
# 有续写（wt_*）的等续写节点结清，其余（altar_*）在预兆兑现后移除。
FLAG_OPENERS = {
    "wt_aided": {"battle": {"strength": 2, "block": 8}},
    "wt_robbed": {"battle": {"enemy_hp": 8, "fragile": 2}},
    "altar_blessed": {"heal": 12},
    "altar_cursed": {"battle": {"enemy_hp": 12, "fragile": 2}},
}
FLAG_TITLES = {
    "wt_aided": "旅人的感激",
    "wt_robbed": "旅人的怨恨",
    "altar_blessed": "神龛赐福",
    "altar_cursed": "神龛诅咒",
}
FLAG_TEXTS = {
    "wt_aided": "下一章首场战斗开局获得 +2 力量与 8 点格挡，并可能遇到旅人道谢。",
    "wt_robbed": "下一章首场战斗敌人 +8 生命、你附带易碎 2 回合，复仇者可能找上门。",
    "altar_blessed": "进入下一章时回复 12 点生命。",
    "altar_cursed": "下一章首场战斗敌人 +12 生命、你附带易碎 2 回合。",
}
AMBUSH_REWARD_KEY = "reward_gold"


# ---------- enc_state 结构 ----------
def fresh_state():
    """新 run/新远征的奇遇状态。"""
    return {
        "flags": {},          # flag -> 埋设时所在章号
        "opened": {},         # flag -> 预兆兑现章号（每 flag 至多兑现一次）
        "battle_mods": {},    # 本章首场战斗的开局修正（兑现后清空）
        "resolved_nodes": [],  # 本章已结清的奇遇节点（跨章重置）
        "done_local": [],     # 本章已出现过的本地链（跨章重置）
        "completed": [],      # 本段远征已彻底完成的跨章根链 id
        "pending": None,      # 待抉择：{chain, node}（续局保留）
        "ambush": None,       # 抉择触发的伏击战：{root, flag, reward_gold}
    }


def normalize_state(enc):
    """旧档/损坏档兜底：补齐缺失键（幂等）。返回 (规范化状态, 是否发生修复)。

    None/非 dict（旧章交接快照根本没有 enc_state）整体替换为 fresh_state。
    """
    if not isinstance(enc, dict):
        return fresh_state(), True
    changed = False
    base = fresh_state()
    for key, default in base.items():
        if key not in enc:
            enc[key] = copy.deepcopy(default)
            changed = True
    if not isinstance(enc["flags"], dict):
        enc["flags"] = {}
        changed = True
    if not isinstance(enc["battle_mods"], dict):
        enc["battle_mods"] = {}
        changed = True
    return enc, changed


def reset_for_chapter(enc):
    """交接快照 -> 新章初态：节点级痕迹重置，跨章 flag/已完成链保留。"""
    enc["resolved_nodes"] = []
    enc["done_local"] = []
    enc["pending"] = None
    enc["ambush"] = None
    enc["battle_mods"] = {}
    return enc


# ---------- 链查询 ----------
def get_chain(chain_id):
    c = CHAINS.get(chain_id)
    if c is None:
        raise KeyError(f"unknown encounter chain: {chain_id}")
    return c


def get_choice(chain, choice_id):
    ch = next((c for c in chain["choices"] if c["id"] == choice_id), None)
    if ch is None:
        raise KeyError(f"unknown encounter choice: {choice_id}")
    return ch


def _flags_set_by_chain(chain):
    return {c.get("set_flag") for c in chain["choices"] if c.get("set_flag")}


def _continuation_flags():
    """所有续写链等待的 flag 集合。"""
    return {c["requires_flag"] for c in CHAINS.values()
            if c.get("continue") and c.get("requires_flag")}


def eligible_chains(enc_state, chapter, chapters_total):
    """当前奇遇节点可选的链 id（确定性排序；调用方再用节点种子抽一条）。

    - 续写链优先（调用方应在续写候选非空时只在其中抽取）：它等待的 flag
      必须在场（章窗口/根链未完成）；
    - 跨章起始链：仅远征（chapters_total 非 None）且非末章可出现——普通局
      没有后续章兑现 flag，不挂跨章链；同一根链未完成且它埋下的 flag
      均不在场；
    - 本地链：本章尚未出现过。
    """
    flags = enc_state.get("flags", {})
    completed = set(enc_state.get("completed", []))
    done_local = set(enc_state.get("done_local", []))
    out = []
    for c in CHAINS.values():
        cid = c["id"]
        if c.get("continue"):
            # 续写链同样只存在于远征（flag 只能在远征里埋下）
            if chapters_total is None:
                continue
            if c["requires_flag"] not in flags:
                continue
            if chapter < c.get("min_chapter", 1):
                continue
            if c.get("max_chapter") and chapter > c["max_chapter"]:
                continue
            if c.get("root", cid) in completed:
                continue
        elif c["scope"] == CROSS:
            # 跨章链只在远征出现；末章埋下的 flag 永远等不到下一章
            if chapters_total is None or chapter >= chapters_total:
                continue
            if chapter < c.get("min_chapter", 1):
                continue
            if cid in completed or c.get("root", cid) in completed:
                continue
            if _flags_set_by_chain(c) & set(flags):
                continue
        else:
            if cid in done_local:
                continue
        out.append(cid)
    return sorted(out)


def select_chain(event_seed, enc_state, chapter, chapters_total):
    """确定性选出当前奇遇节点的链：续写候选非空时必在续写中抽取（保证埋有
    flag 的玩家走到后续章奇遇节点一定触发对应遭遇/报恩/复仇），否则在普通
    候选（跨章起始 + 本地）中抽取；无候选返回 None（走兜底清泉）。"""
    eligible = eligible_chains(enc_state, chapter, chapters_total)
    continuing = [cid for cid in eligible if CHAINS[cid].get("continue")]
    return pick_chain(event_seed, continuing or eligible)


def pick_chain(event_seed, chain_ids):
    """从候选链中按节点种子确定性抽一条；无候选返回 None（走兜底清泉）。"""
    if not chain_ids:
        return None
    rng = random.Random(event_seed & 0xFFFFFFFF)
    return rng.choice(list(chain_ids))


# ---------- 抉择簿记（只动 enc_state；run 状态由 service 变更） ----------
def mark_resolved(enc_state, node, chain_id=None, local=False):
    enc_state.setdefault("resolved_nodes", []).append(node)
    if local and chain_id:
        enc_state.setdefault("done_local", []).append(chain_id)


def commit_choice(enc_state, chain, choice, chapter):
    """抉择落账：埋 flag / 标记完成链。伏击战的完成由 finish_ambush 处理。

    返回是否为跨章续写链（service 据此清 flag 的时机不同——本函数只处理
    非伏击分支：续写链在抉择当场结清，起始链在埋下 flag 后保持未完成）。
    """
    flag = choice.get("set_flag")
    if flag:
        enc_state.setdefault("flags", {})[flag] = chapter
    root = chain.get("root", chain["id"])
    if chain.get("continue"):
        if _is_battle_choice(choice):
            return  # 等伏击战分出胜负再 finish_ambush
        if chain.get("requires_flag"):
            enc_state.setdefault("flags", {}).pop(chain["requires_flag"], None)
        enc_state.setdefault("completed", []).append(root)
    elif chain["scope"] == CROSS:
        if not flag:
            enc_state.setdefault("completed", []).append(root)
    else:
        enc_state.setdefault("done_local", []).append(chain["id"])


def _is_battle_choice(choice):
    return any(e.get("type") == "battle" for e in choice.get("effects", []))


def start_ambush(enc_state, chain, effect, node):
    """伏击抉择：挂起伏击上下文（战斗胜负后 finish_ambush），节点视为已进入。"""
    enc_state["ambush"] = {
        "root": chain.get("root", chain["id"]),
        "flag": chain.get("requires_flag"),
        AMBUSH_REWARD_KEY: effect.get(AMBUSH_REWARD_KEY, 0),
        "node": node,
    }


def ambush_context(enc_state):
    return (enc_state or {}).get("ambush")


def finish_ambush(enc_state, won):
    """伏击战结束：胜则根链完成、flag 清除；败则远征本就终结，同样清理。"""
    amb = (enc_state or {}).pop("ambush", None)
    if not amb:
        return None
    if won:
        if amb.get("flag"):
            enc_state.setdefault("flags", {}).pop(amb["flag"], None)
        root = amb.get("root")
        if root and root not in enc_state.setdefault("completed", []):
            enc_state["completed"].append(root)
    return amb


# ---------- 章节开头的预兆兑现 ----------
def on_chapter_begin(enc_state, chapter):
    """新章开局：兑现各 flag 一次性预兆（战斗修正/回血）。

    - 战斗修正合并进 battle_mods（该章首场战斗消耗）；
    - 回血总量返回，由 _new_run_state 在休整回血之后叠加；
    - 没有续写链等待的 flag（altar_*）兑现即移除；续写 flag（wt_*）保留
      到续写节点结清。返回 (回血总量, 已兑现 flag 列表)。
    """
    continuing = _continuation_flags()
    heal = 0
    opened = []
    for flag, set_chapter in list(enc_state.get("flags", {}).items()):
        if flag in enc_state.setdefault("opened", {}) or set_chapter >= chapter:
            continue
        spec = FLAG_OPENERS.get(flag)
        if not spec:
            continue
        if "heal" in spec:
            heal += spec["heal"]
        if "battle" in spec:
            mods = enc_state.setdefault("battle_mods", {})
            for key, value in spec["battle"].items():
                mods[key] = mods.get(key, 0) + value
        enc_state["opened"][flag] = chapter
        opened.append(flag)
        if flag not in continuing:
            enc_state["flags"].pop(flag, None)
    return heal, opened


def take_battle_mods(enc_state):
    """取出（并清空）首场战斗修正；没有修正返回空 dict。"""
    if not enc_state:
        return {}
    mods = enc_state.get("battle_mods") or {}
    enc_state["battle_mods"] = {}
    return dict(mods)


# ---------- 只读视口 ----------
def pending_public(enc_state):
    p = (enc_state or {}).get("pending")
    if not p:
        return None
    chain = get_chain(p["chain"])
    return {"chain": chain["id"], "title": chain["title"], "text": chain["text"],
            "choices": [dict(ch) for ch in chain["choices"]]}


def flags_public(enc_state, chapter):
    """侧栏/交接快照用的活跃 flag 摘要（含预兆是否已兑现）。"""
    out = []
    for flag, since in (enc_state or {}).get("flags", {}).items():
        out.append({
            "flag": flag,
            "title": FLAG_TITLES.get(flag, flag),
            "desc": FLAG_TEXTS.get(flag, ""),
            "since_chapter": since,
            "opener_at": (enc_state or {}).get("opened", {}).get(flag),
            "pending_opener": (enc_state or {}).get("opened", {}).get(flag) is None
                            and chapter is not None and since < chapter,
        })
    return out
