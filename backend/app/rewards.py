from __future__ import annotations

import random

from .cards import get_card
from . import potions as potions_mod

# 战斗胜利奖励：给 2-3 个“卡牌/回血/金币”选项，选一个。
# 每个选项带 effects：改变 run 状态（加牌/回血/金、或引入后续遭遇的隐性修正 relic）。

ALL_RELIC_OPTIONS = [
    {"id": "anvil", "name": "铁砧", "desc": "获得永久力量 +2（影响后续所有战斗）",
     "effects": [{"type": "relic_set", "relic": "power_up", "value": 1}]},
    {"id": "curse_core", "name": "诅咒之核", "desc": "本局增伤 +25%，但首领生命 +20%（后续遭遇更强）",
     "effects": [{"type": "relic_set", "relic": "boss_hp_bonus", "value": 12},
                 {"type": "relic_set", "relic": "power_up", "value": 1}]},
    {"id": "heal_15", "name": "治疗药膏", "desc": "回复 15 点生命", "effects": [{"type": "heal_run", "value": 15}]},
    {"id": "card_flurry", "name": "『连击』", "desc": "把「连击」加入牌组", "effects": [{"type": "add_card", "card": "flurry"}]},
    {"id": "gold_30", "name": "金币袋", "desc": "获得 30 金币", "effects": [{"type": "gold", "value": 30}]},
]


def battle_reward_options(rng_seed, enemy_def, run_state, include_potions=True):
    """根据敌人掉落卡与金币生成 2-3 个可直接领取的奖励选项（确定性）。

    include_potions=False 仅用于回放 2.5.0 之前的旧战斗：药水选项是 2.5.0 才
    追加的，旧日志里的 claim_reward 下标按不含药水项的旧选项集录制；若在旧
    战斗里生成药水项，下标整体错位会领到错误奖励（含金币/卡牌张冠李戴）。
    """
    rng = random.Random((rng_seed * 31 + 7) & 0xFFFFFFFF)
    reward_cards = enemy_def.get("reward_cards") or []
    options = []
    seen = set()

    def add(kind, name, desc, effects, key=None):
        key = key or kind
        if key in seen:
            return
        seen.add(key)
        options.append({"kind": kind, "name": name, "desc": desc, "effects": effects})

    # 掉落卡：确定抽一张加入牌组（同名卡已持有则不再提供该选项）
    if reward_cards:
        cid = reward_cards[rng.randrange(len(reward_cards))]
        c = get_card(cid)
        instances = run_state.get("card_instances")
        if instances:
            owned = {inst["id"] for inst in instances.values()}
        else:
            owned = set(run_state.get("deck", []))  # 旧档：裸 id 牌组
        if cid not in owned:
            add("card", c["name"], f"把「{c['name']}」加入牌组",
                [{"type": "add_card", "card": cid}], key="card")
    # 金币
    add("gold", "金币袋", f"获得 {enemy_def.get('reward_gold', 30)} 金币",
        [{"type": "gold", "value": enemy_def.get("reward_gold", 30)}], key="gold")
    # 治疗
    add("heal", "药膏", "回复 12 点生命", [{"type": "heal_run", "value": 12}], key="heal")
    # 遗物（直接给一个真实遗物，起训练用）
    relic = rng.choice([o for o in ALL_RELIC_OPTIONS])
    add("relic", relic["name"], relic["desc"], relic["effects"], key="relic")
    # 药水（可跨章携带的消耗品）：使用独立的 RNG 派生流，不改变上述既有选项的
    # 确定性抽法，追加为最后一项——旧日志记录的选项下标含义保持不变（旧档回放
    # 逐位一致），仅在 2.5.0+ 的战利品里多出第 4/5 个选项。
    if include_potions:
        prng = random.Random((rng_seed * 41 + 13) & 0xFFFFFFFF)
        if prng.random() < 0.7:
            pid = potions_mod.loot_potion_id(rng_seed)
            p = potions_mod.get_potion(pid)
            add("potion", f"{p['icon']} {p['name']}", f"{p['desc']}（放入药水背包）",
                [{"type": "add_potion", "potion": pid}], key="potion")
    return options


def relic_choice_options(rng_seed):
    rng = random.Random((rng_seed * 53 + 3) & 0xFFFFFFFF)
    pool = [o for o in ALL_RELIC_OPTIONS]
    return rng.sample(pool, k=min(2, len(pool)))