from __future__ import annotations

import random

from .cards import CARDS

# 远征委托：在旅途商店接取的限章委托。
# 战斗胜利 / 完成交易自动推进目标；在限章（deadline_chapter）前完成可领奖
# （金币或卡牌实例），战败则全部进行中的委托失败，超期未完成则失败。
# 委托随章节交接快照带入下一章，因此接取/推进/领奖/超期/失败全部由确定性的
# 动作序列派生——续局与整程回放天然一致。

MAX_ACTIVE = 3            # 同一时间最多持有的进行中委托（含可领奖未领取）
OFFERS_PER_SHOP = 2       # 远征章节每个商店最多挂出的委托数
MAX_DURATION = 2          # 委托时限跨度（章）：deadline = 当前章 + 1..MAX_DURATION
GOLD_REWARD_RANGE = (25, 60)
# 卡牌奖励候选（进阶/金色卡，排除初始基础卡）
CARD_REWARD_TIERS = ("advanced", "gold")

BATTLE = "battle"        # 目标：赢得 N 场战斗
TRADE = "trade"          # 目标：完成 N 笔商店交易（购买或移除各算一笔）

ACTIVE = "active"        # 进行中
READY = "ready"          # 已完成可领奖
CLAIMED = "claimed"      # 已领取
FAILED = "failed"        # 失败（战败 / 超期）
TERMINAL = (CLAIMED, FAILED)

KIND_LABELS = {BATTLE: "讨伐委托", TRADE: "贸易委托"}


def public_commission_kinds():
    """委托类型元数据（供前端渲染）。"""
    return [{"id": BATTLE, "name": KIND_LABELS[BATTLE]},
            {"id": TRADE, "name": KIND_LABELS[TRADE]}]


def generate_offers(offer_seed, chapter, chapters_total, commissions):
    """确定性生成商店挂出的委托（仅远征章节）。

    offer_seed 由（章节种子, 节点, 商店进入序）派生；委托内容只依赖该种子与
    当前章节号，因此同一条动作日志重放必得同一组委托。已持有的进行中委托按
    (kind,target,reward) 去重，避免同一委托重复挂出。
    """
    rng = random.Random(offer_seed)
    active_count = sum(1 for c in commissions if c["status"] in (ACTIVE, READY))
    if active_count >= MAX_ACTIVE:
        return []
    busy = {c["signature"] for c in commissions if c["status"] in (ACTIVE, READY)}

    pool = []
    for _ in range(8):  # 尝试若干次，凑满货架或撞重放弃（确定性，不依赖集合迭代序）
        kind = BATTLE if rng.random() < 0.6 else TRADE
        if kind == BATTLE:
            target = rng.randint(1, 2)
        else:
            target = rng.randint(2, 3)
        duration = rng.randint(1, MAX_DURATION)
        deadline = min(chapters_total, chapter + duration)
        # 奖励：六成金币、四成卡牌
        if rng.random() < 0.6:
            reward = {"type": "gold", "amount": rng.randint(*GOLD_REWARD_RANGE)}
        else:
            card_ids = sorted(cid for cid, c in CARDS.items() if c.get("tier") in CARD_REWARD_TIERS)
            reward = {"type": "card", "card": card_ids[rng.randrange(len(card_ids))]}
        signature = _signature(kind, target, reward)
        if signature in busy:
            continue
        busy.add(signature)
        pool.append(_offer_dict(kind, target, deadline, reward, signature, chapter))
        if len(pool) >= OFFERS_PER_SHOP or active_count + len(pool) >= MAX_ACTIVE:
            break
    return pool


def _signature(kind, target, reward):
    # 同款委托（同目标同奖励）持有期间不重复挂出
    if reward["type"] == "gold":
        r = f"gold:{reward['amount']}"
    else:
        r = f"card:{reward['card']}"
    return f"{kind}:{target}:{r}"


def _offer_dict(kind, target, deadline, reward, signature, chapter):
    return {
        "kind": kind, "target": target, "deadline_chapter": deadline,
        "reward": reward, "signature": signature, "offered_chapter": chapter,
    }


def make_commission(seq, offer):
    """挂单项 -> 已接取委托实例（进度从 0 起，状态 active）。"""
    return {
        "id": f"q{seq}",
        "kind": offer["kind"],
        "target": offer["target"],
        "progress": 0,
        "deadline_chapter": offer["deadline_chapter"],
        "reward": dict(offer["reward"]),
        "signature": offer["signature"],
        "accepted_chapter": offer["offered_chapter"],
        "status": ACTIVE,
        "fail_reason": None,
        "claimed_at": None,
    }


def apply_battle_result(commissions, won):
    """战斗结束自动推进：胜 -> battle 类委托 +1；败 -> 所有进行中委托失败。

    返回状态发生变化的委托 id 列表（供动作日志/前端提示）。
    """
    changed = []
    for c in commissions:
        if c["status"] != ACTIVE:
            continue
        if not won:
            c["status"] = FAILED
            c["fail_reason"] = "battle_lost"
            changed.append(c["id"])
        elif c["kind"] == BATTLE:
            c["progress"] += 1
            if c["progress"] >= c["target"]:
                c["status"] = READY
            changed.append(c["id"])
    return changed


def apply_trade(commissions):
    """完成一笔商店交易（购买/移除）：trade 类委托 +1。返回变化的委托 id。"""
    changed = []
    for c in commissions:
        if c["status"] == ACTIVE and c["kind"] == TRADE:
            c["progress"] += 1
            if c["progress"] >= c["target"]:
                c["status"] = READY
            changed.append(c["id"])
    return changed


def expire_active(commissions, chapter):
    """进入第 chapter 章时：限章早于本章的进行中委托超期失败。

    已完成可领奖（ready）的不超期——奖励随交接保留，玩家仍可领取。
    返回状态发生变化的委托 id 列表。
    """
    changed = []
    for c in commissions:
        if c["status"] == ACTIVE and c["deadline_chapter"] < chapter:
            c["status"] = FAILED
            c["fail_reason"] = "expired"
            changed.append(c["id"])
    return changed


def reward_text(reward):
    if reward["type"] == "gold":
        return f"{reward['amount']} 金币"
    c = CARDS.get(reward["card"])
    return f"卡牌「{c['name']}」" if c else f"卡牌 {reward['card']}"


def objective_text(c):
    if c["kind"] == BATTLE:
        return f"赢得 {c['target']} 场战斗"
    return f"完成 {c['target']} 笔商店交易"


def _reward_public(reward):
    out = dict(reward)
    out["text"] = reward_text(reward)
    if reward["type"] == "card":
        c = CARDS.get(reward["card"])
        if c:
            out["name"] = c["name"]
            out["desc"] = c["desc"]
            out["card_cost"] = c["cost"]
            out["tier"] = c["tier"]
            out["type_label"] = {"attack": "攻击", "skill": "技能", "power": "能力"}.get(c["type"])
    return out


def commission_public(c, chapter):
    """单个委托的只读视口。"""
    return {
        "id": c["id"],
        "kind": c["kind"],
        "kind_label": KIND_LABELS.get(c["kind"], c["kind"]),
        "objective": objective_text(c),
        "target": c["target"],
        "progress": c["progress"],
        "deadline_chapter": c["deadline_chapter"],
        "reward": _reward_public(c["reward"]),
        "status": c["status"],
        "fail_reason": c.get("fail_reason"),
        "accepted_chapter": c.get("accepted_chapter"),
        "chapters_left": max(0, c["deadline_chapter"] - chapter),
        "can_claim": c["status"] == READY,
    }


def offers_public(offers):
    """商店挂单项的只读视口（附带中文目标/奖励描述）。"""
    out = []
    for o in offers:
        out.append({
            "sku": f"commission:{o['signature']}",
            "kind_label": KIND_LABELS.get(o["kind"], o["kind"]),
            "objective": objective_text(o),
            "commission_kind": o["kind"],
            "target": o["target"],
            "deadline_chapter": o["deadline_chapter"],
            "reward": _reward_public(o["reward"]),
        })
    return out
