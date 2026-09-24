from __future__ import annotations

import random

# 药水：可跨章携带的消耗品。从旅途商店购买或战斗战利品获得，存入限容量背包
# （POTION_CAPACITY 格，背满时新药水必须指定替换某一格，被替换的药水当场丢弃）。
# 战斗中可在自己的回合择机使用：消耗（从背包移除）与战斗效果在同一个动作里
# 原子结算——动作在共享的纯推演 _apply_action 上执行，成功才随事务落库，
# 失败/重试不产生任何效果；request_id 幂等保证双击/超时重试绝不重复生效。
# 药水列表存于 run 状态（run["potions"]，按下标定位），随章节交接快照带入
# 下一章，因此购买/掉落/替换/丢弃/使用全部是动作序列的确定性函数——
# 续局与整程回放天然逐位一致。

POTION_CAPACITY = 3  # 背包格数上限

# 药水 id -> 定义。effects 与卡牌效果同构（type/value/target/status/stack/ticks），
# 由引擎结算队列统一解释，因此伤害/格挡/治疗/状态都走同一条死亡打断/连锁路径。
POTIONS: dict[str, dict] = {}


def _potion(pid, name, desc, price, effects, icon="🧪"):
    POTIONS[pid] = {
        "id": pid, "name": name, "desc": desc, "price": price,
        "effects": effects, "icon": icon,
    }


_potion("hp", "治疗药水", "战斗中回复 12 点生命。", 30,
        [{"type": "heal", "value": 12, "target": "player"}], icon="❤️")
_potion("block", "壁垒药水", "战斗中获得 12 点格挡。", 30,
        [{"type": "gain_block", "value": 12, "target": "player"}], icon="🛡️")
_potion("fire", "炽焰药水", "战斗中对敌人造成 10 点伤害（无视力量，不经攻击标签）。", 45,
        [{"type": "damage", "value": 10, "target": "enemy"}], icon="🔥")
_potion("energy", "能量药水", "战斗中立即获得 2 点能量（可超过上限）。", 40,
        [{"type": "gain_energy", "value": 2, "target": "player"}], icon="⚡")
_potion("strength", "力量药水", "战斗中获得 2 点力量（本场战斗内攻击加伤）。", 55,
        [{"type": "apply_status", "status": "strength", "value": 2,
          "stack": "add", "target": "player"}], icon="💪")

# 商店货架上药水固定价格（货架生成时确定性取 1..POTION_OFFER_COUNT 种）
POTION_PRICE = {pid: p["price"] for pid, p in POTIONS.items()}
POTION_OFFER_COUNT = 3


def get_potion(pid):
    p = POTIONS.get(pid)
    if p is None:
        raise KeyError(f"unknown potion: {pid}")
    return dict(p)


def all_potions():
    return [dict(v) for v in POTIONS.values()]


def public_potion(pid):
    """背包格 / 货架 / 战利品共用的只读药水元数据。"""
    p = POTIONS[pid]
    return {"id": pid, "name": p["name"], "desc": p["desc"],
            "price": p["price"], "icon": p["icon"]}


def generate_shop_offers(stock_seed):
    """确定性生成商店药水货架：从全部药水中抽取（药水可持有多瓶；货架项独立、
    各自有 sold 标记，与卡牌货架一致，背满时购买需指定替换格，见 service._shop_buy）。

    返回 [{sku, kind:"potion", potion, price, sold}]，只依赖 (stock_seed,)。
    """
    rng = random.Random((stock_seed * 13 + 17) & 0xFFFFFFFF)
    ids = sorted(POTIONS)
    rng.shuffle(ids)
    return [{
        "sku": f"potion:{pid}", "kind": "potion", "potion": pid,
        "price": POTION_PRICE[pid], "sold": False,
    } for pid in ids[:POTION_OFFER_COUNT]]


def loot_potion_id(loot_seed):
    """战斗胜利战利品：按种子确定性选一瓶药水（背满时玩家可选择替换或放弃）。"""
    rng = random.Random((loot_seed * 37 + 3) & 0xFFFFFFFF)
    ids = sorted(POTIONS)
    return ids[rng.randrange(len(ids))]


# ---------- 背包纯操作（供 service 统一调用；非法情况由调用方先校验） ----------
def is_full(potions):
    return len(potions) >= POTION_CAPACITY


def add(potions, pid, replace=None):
    """把药水加入背包。背满时必须给 replace（0 基下标）指定被替换丢弃的格；
    背包有空位时 replace 必须为 None（避免误丢）。返回 (插入位置, 被丢弃的药水或 None)。
    """
    if pid not in POTIONS:
        raise KeyError(f"unknown potion: {pid}")
    if is_full(potions):
        if replace is None:
            raise ValueError("potion belt is full; choose a slot to replace")
        if not isinstance(replace, int) or not (0 <= replace < len(potions)):
            raise ValueError("invalid replace slot")
        discarded = potions[replace]
        potions[replace] = pid
        return replace, discarded
    if replace is not None:
        raise ValueError("replace only allowed when the belt is full")
    potions.append(pid)
    return len(potions) - 1, None


def consume(potions, index):
    """使用/丢弃：移除并返回指定格的药水 id（下标非法抛 IndexError，由调用方拦截）。"""
    return potions.pop(index)


def belt_public(potions):
    """背包只读视口：按下标返回格位（战斗使用/替换/丢弃都按格位定位）。"""
    return [{"slot": i, **public_potion(pid)} for i, pid in enumerate(potions)]
