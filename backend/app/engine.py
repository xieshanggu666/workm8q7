from __future__ import annotations

import copy
import math

from .settlement import EffectEvent, SettlementQueue
from . import companions as companions_mod

# 状态 id -> 中文名/说明（供前端展示）
STATUS_INFO = {
    "strength": "力量",
    "vulnerable": "易伤",
    "fragile": "易碎",
    "echo": "回响",
    "strength_per_turn": "力量成长",
    "power_up": "增伤",
}


def make_entity(key, name, max_hp, hp=None):
    return {
        "key": key, "name": name, "max_hp": max_hp, "hp": hp if hp is not None else max_hp,
        "block": 0, "statuses": {}, "alive": True,
    }


class Battle:
    """单场战斗：玩家 vs 一个敌人/首领。resolve 消费结算队列，产出事件日志。"""

    def __init__(self, run_state, enemy_def, seed, battle_index, relic_status="", relic_power=0, boss_hp_bonus=0,
                 card_instances=None, companion_state=None, enemy_hp_bonus=0):
        max_hp = run_state["max_health"]
        self.entities = {
            "player": make_entity("player", run_state.get("player_name", "勇者"), max_hp, run_state["health"]),
        }
        # 敌方生命加成：遗物首领加成（仅首领节点）与奇遇链预兆（enemy_hp_bonus，
        # 章节首场战斗一次性消耗）叠加。
        total_hp_bonus = boss_hp_bonus + enemy_hp_bonus
        if total_hp_bonus:
            enemy_hp = enemy_def["hp"] + total_hp_bonus
        else:
            enemy_hp = enemy_def["hp"]
        enemy_key = "enemy"
        self.enemy_def = enemy_def
        self.enemy = make_entity(enemy_key, enemy_def["name"], enemy_hp, enemy_hp)
        self.entities[enemy_key] = self.enemy
        self.companion_state = copy.deepcopy(companion_state) if companion_state else None
        self.companion_def = (
            companions_mod.COMPANIONS.get(self.companion_state.get("id"), companions_mod.SQUIRE)
            if self.companion_state else None
        )
        companion_ent = companions_mod.snapshot_for_battle(self.companion_state)
        if companion_ent is not None:
            self.entities["companion"] = companion_ent

        # 由奖励选择带入的常驻效果（relic）幻化成战斗初始状态
        st = self.entities["player"]["statuses"]
        if relic_status:
            st[relic_status] = {"mode": "add", "value": relic_power, "ticks": None}
        if "power_up" in run_state.get("relics", {}):
            st.setdefault("strength", {"mode": "add", "value": 0, "ticks": None})["value"] += 2

        # 敌人阶段
        self.phase = 0
        self.phases_active = False

        # 牌堆（确定性：用 run_seed + battle_index 洗牌）
        # run_state["deck"] 为引用列表：新档是实例 uid，旧档是卡牌 id（兼容）
        self.seed = seed
        self.battle_index = battle_index
        deck = list(run_state["deck"])
        rng = _seeded_rng(seed, battle_index)
        rng.shuffle(deck)
        self.draw_pile = deck
        self.hand = []
        self.discard = []
        # uid -> 卡牌实例 {"id","growth":[{node,cost}]}；旧档（裸 id 牌堆）为空 dict
        self.card_instances = dict(card_instances or {})
        self.energy = run_state.get("base_energy", 3)
        self.max_energy = run_state.get("base_energy", 3)
        self.in_turn = False
        self.turn = 0
        self.truncated = False
        # 2.7.0 之前历史日志回放标志：旧格挡时序（敌人行动前清格挡）与旧援护
        # 顺序（按全额伤害抵挡）仅在重放旧动作时开启；在线新档恒为 False。
        self.legacy_block = False
        # 2.8.0 奇遇链预兆：首场战斗开局修正（start_turn 之后由 service 施加，
        # 随首帧快照落库，战斗中续局/回放不需要再次施加）。
        self.opener = None
        self.queue = SettlementQueue(self)

    def apply_opener(self, mods):
        """施加奇遇链预兆的开局修正（建场首回合抽牌之后调用，一次性）。

        mods: {"strength": N, "block": N, "fragile": N, "vulnerable": N}
        - strength：永久力量（本场战斗，ticks=None）；
        - block：开局格挡，持续整个敌方行动段，下回合开始清零；
        - fragile/vulnerable：施加给玩家、持续 N 回合的负面状态。
        返回结算事件日志（供前端首帧动画逐条播放）。
        """
        if not mods:
            return []
        self.opener = dict(mods)
        q = SettlementQueue(self)
        if mods.get("strength"):
            q.push(EffectEvent("apply_status", target="player",
                               value=mods["strength"], source="encounter",
                               tags=["encounter", "opener"],
                               extra={"status": "strength", "stack": "add"}))
        if mods.get("fragile"):
            q.push(EffectEvent("apply_status", target="player",
                               value=mods["fragile"], source="encounter",
                               tags=["encounter", "opener"],
                               extra={"status": "fragile", "stack": "add",
                                      "ticks": mods.get("fragile")}))
        if mods.get("vulnerable"):
            q.push(EffectEvent("apply_status", target="player",
                               value=mods["vulnerable"], source="encounter",
                               tags=["encounter", "opener"],
                               extra={"status": "vulnerable", "stack": "add",
                                      "ticks": mods.get("vulnerable")}))
        if mods.get("block"):
            q.push(EffectEvent("gain_block", target="player",
                               value=mods["block"], source="encounter",
                               tags=["encounter", "opener"]))
        log = q.run()
        self.truncated = self.truncated or q.truncated
        return [{"encounter_opener": dict(mods)}, *log]

    # ---------- 卡牌实例 ----------
    def _card_def(self, ref):
        """手牌引用（uid 或旧档裸 id）-> 生效卡牌定义（含成长树换算）。"""
        from .cards import get_card
        from .forging import effective_card
        inst = self.card_instances.get(ref)
        if inst is None:
            return get_card(ref)
        return effective_card(get_card(inst["id"]), inst.get("growth", []))

    # ---------- 实体查询 ----------
    def entity(self, key):
        return self.entities.get(key)

    def is_dead(self, key):
        ent = self.entities.get(key)
        return ent is None or not ent["alive"]

    def hpof(self, key):
        ent = self.entity(key)
        return ent["hp"] if ent else 0

    # ---------- 手感用 ----------
    def player_attack_base(self):
        p = self.entities["player"]
        return 0

    # ---------- 伙伴 ----------
    def companion_assists(self):
        """随行且未负伤的伙伴在回合开始时攻击敌人。"""
        companion = self.entities.get("companion")
        if companion is None or not companion["alive"] or self.is_dead("enemy"):
            return []
        q = SettlementQueue(self)
        q.push(EffectEvent("damage", target="enemy",
                           value=self.companion_def["attack"],
                           source="companion", tags=["attack", "companion"]))
        log = q.run()
        self.truncated = self.truncated or q.truncated
        return log

    def before_resolve(self, ev):
        """伙伴援护在玩家受击前先入队；标记后当前伤害再按减免值结算。

        结算顺序（2.7.0 修正）：敌人攻击 -> 玩家格挡吸收 -> 穿透溢出先由伙伴
        援护（最多 guard 点）-> 仍有剩余才扣玩家生命。旧实现按全额伤害判穿，
        卡牌/药水格挡被直接穿透（伙伴吃 guard、玩家吃全额-guard）。
        """
        if ev.action not in ("damage", "echo_damage") or ev.extra.get("guard_prepared"):
            return []
        target = self.entities.get(ev.target)
        if (ev.source != "enemy" or target is not self.entities.get("player")
                or "attack" not in ev.tags):
            ev.extra["guard_prepared"] = True
            return []
        companion = self.entities.get("companion")
        player = self.entities.get("player")
        if companion is None or not companion["alive"]:
            return []
        dmg = _final_damage(self, ev)
        ev.extra["guard_prepared"] = True
        if getattr(self, "legacy_block", False):
            # 旧规则（2.7.0 前）：格挡在 end_turn 敌人行动前已清零，援护按全额抵挡
            if dmg <= player["block"]:
                return []
            absorbed_legacy = min(self.companion_def["guard"], companion["hp"], dmg)
            if absorbed_legacy <= 0:
                return []
            ev.value = max(0, dmg - absorbed_legacy)
            ev.extra["final_damage"] = ev.value
            return [EffectEvent(
                "damage", target="companion", value=absorbed_legacy,
                source="enemy", tags=["attack", "companion_guard"],
            )]
        # 新规则（2.7.0）：格挡先吸收，只有穿透格挡的溢出才触发援护；格挡
        # 本身仍由本事件经 _hurt 正常消耗（伙伴承伤事件先入队、先结算）。
        overflow = max(0, dmg - player["block"])
        if overflow <= 0:
            return []
        absorbed = min(self.companion_def["guard"], companion["hp"], overflow)
        if absorbed <= 0:
            return []
        # 玩家剩余承伤 = 全额 - 援护（格挡部分由 _hurt 吸收）
        ev.value = max(0, dmg - absorbed)
        ev.extra["final_damage"] = ev.value
        return [EffectEvent(
            "damage", target="companion", value=absorbed,
            source="enemy", tags=["attack", "companion_guard"],
        )]

    # ---------- 效果解析（返回子事件 = 连锁） ----------
    def resolve(self, ev: EffectEvent) -> list:
        children = []
        a = ev.action
        target = self.entities.get(ev.target)

        if a == "damage" or a == "echo_damage":
            if target is None or not target["alive"]:
                return children
            dmg = _final_damage(self, ev)
            if "final_damage" in ev.extra:
                dmg = ev.extra["final_damage"]
            _hurt(target, dmg)
            ev.value = dmg  # 日志记录实际结算伤害
            # 连锁：攻击者的回响 -> 再攻击
            if "attack" in ev.tags:
                src = self.entities.get(ev.source)
                if src and src["alive"] and src["statuses"].get("echo"):
                    ech = src["statuses"]["echo"]["value"]
                    if ech > 0:
                        src["statuses"]["echo"]["value"] = ech - 1
                        children.append(EffectEvent(
                            "damage", target=ev.target, value=max(1, int(ev.value / 2)),
                            source=ev.source, tags=["attack", "echo"]))
        elif a == "gain_block":
            if target and target["alive"]:
                target["block"] += ev.value
        elif a == "draw":
            # 抽出 draw_pile 前 value 张进手牌
            for _ in range(ev.value):
                if not self.draw_pile:
                    self.draw_pile = list(self.discard); self.discard = []
                    rng = _seeded_rng(self.seed, self.battle_index + self.turn + 99)
                    rng.shuffle(self.draw_pile)
                    if not self.draw_pile:
                        break
                self.hand.append(self.draw_pile.pop(0))
        elif a == "apply_status":
            if target and target["alive"]:
                _apply_status(target, ev.extra.get("status"), ev.value,
                              ev.extra.get("stack", "add"), ev.extra.get("ticks"))
        elif a == "set_status":
            if target and target["alive"]:
                target["statuses"][ev.extra["status"]] = {
                    "mode": ev.extra.get("stack", "replace"),
                    "value": ev.value, "ticks": ev.extra.get("ticks"),
                }
        elif a == "heal":
            if target and target["alive"]:
                target["hp"] = min(target["max_hp"], target["hp"] + ev.value)
        elif a == "gain_energy":
            self.energy += ev.value
        return children

    # ---------- 死亡结算与连锁中断 ----------
    def collect_deaths(self, queue: SettlementQueue):
        changed = True
        while changed:
            changed = False
            for key, ent in self.entities.items():
                if ent["alive"] and ent["hp"] <= 0:
                    ent["alive"] = False
                    ent["hp"] = 0
                    changed = True
                    # 死亡打断：取消所有仍指向它的待处理事件（阻止后续连锁作用死目标）
                    queue.pending = [e for e in queue.pending if e.target != key]
                    continue

    # ---------- 回合流程 ----------
    def start_turn(self):
        self.turn += 1
        self.in_turn = True
        self.energy = self.max_energy
        logs = []
        companion_log = self.companion_assists()
        if companion_log:
            logs.append({"companion_turn": {
                "name": self.entities["companion"]["name"],
                "attack": self.companion_def["attack"],
            }})
            logs.extend(companion_log)
        p = self.entities["player"]
        # 格挡只持续一个敌方行动段：新回合开始时清空上一回合残余（2.7.0 起
        # 格挡不再在 end_turn 敌人行动前清零，否则格挡无法抵挡敌方攻击）。
        p["block"] = 0
        # 回合开始触发：力量成长
        spt = p["statuses"].get("strength_per_turn")
        if spt:
            _apply_status(p, "strength", spt["value"], "add")
        # 抽牌 & 韧性递减
        have_draw = 0
        for _ in range(5):
            if not self.draw_pile:
                self.draw_pile = list(self.discard); self.discard = []
                rng = _seeded_rng(self.seed, self.battle_index * 1000 + self.turn)
                rng.shuffle(self.draw_pile)
            if not self.draw_pile:
                break
            self.hand.append(self.draw_pile.pop(0)); have_draw += 1
        for ent in self.entities.values():
            _tick_statuses(ent)
        return self.to_snapshot(), logs

    def end_turn(self):
        """敌方行动并推进回合。返回 (敌方结算日志, 意图)，日志按结算顺序供前端播放。

        2.7.0 修正：玩家格挡必须保留到敌人行动结算（卡牌/药水的格挡用于抵挡
        即将到来的攻击），在回合末才清空——由随后的 start_turn 统一清零。
        旧实现在敌人行动之前就 p["block"]=0，导致本回合打出的格挡完全无法
        抵挡敌方伤害（卡牌格挡与壁垒药水形同虚设）。legacy_block=True 回放
        2.7.0 之前日志时，在敌人行动前清零以逐位复刻旧时序。
        """
        p = self.entities["player"]
        if getattr(self, "legacy_block", False):
            p["block"] = 0
        logs, intent = [], None
        # 敌人行动
        if self.enemy["alive"]:
            intent = self._enemy_intent()
            q = SettlementQueue(self)
            for eff in intent["effects"]:
                t = eff["type"]
                # 与 play_card 对称的默认目标：伤害指向玩家，其余默认作用于敌方自身
                target = eff.get("target") or ("player" if t in ("damage", "echo_damage") else "enemy")
                q.push(EffectEvent(t, target=target, value=eff.get("value", 0),
                                   source="enemy", tags=eff.get("tags", []),
                                   extra={"status": eff.get("status"), "ticks": eff.get("ticks"),
                                          "stack": eff.get("stack")}))
            logs = self._run_sub(q, intent)
        self.collect_deaths(self.queue)
        # 战斗未结束则进入玩家下一回合（重新获得能量并抽牌）
        if self.battle_result() == "ongoing":
            _snapshot, turn_log = self.start_turn()
            logs.extend(turn_log)
        return logs, intent

    def _enemy_intent(self):
        """确定性选择敌人意图（返回完整技能：名称 + 全部效果）。"""
        en = self.enemy_def
        if en.get("phases"):
            # 按阶段选技能列表
            phase = self.phase
            if en["boss"] and self.turn % 4 == 0:
                phase = min(phase + 1, len(en["phases"]) - 1)
                self.phase = phase
            skills = en["phases"][phase]["skills"]
        else:
            skills = en["skills"]
        rng = _seeded_rng(self.seed, self.battle_index * 100000 + self.turn)
        skill = skills[rng.randrange(len(skills))]
        return {"name": skill.get("name", ""), "effects": skill["effects"]}

    def _run_sub(self, q, intent=None):
        log = q.run()
        self.truncated = self.truncated or q.truncated
        return log

    def play_card(self, card_ref_or_def):
        """玩家打出一张手牌，走结算队列。返回过程日志。

        card_ref_or_def 兼容两种形态：
        - 新档：手牌引用（uid 字符串），通过 _card_def 解析生效卡牌；
        - 旧档/单测：直接传入卡牌定义 dict（含 id 与 effects）。
        """
        if not self.in_turn:
            raise ValueError("当前不是玩家回合")
        if isinstance(card_ref_or_def, dict):
            card_ref = card_ref_or_def["id"]
            c = card_ref_or_def
        else:
            card_ref = card_ref_or_def
            c = self._card_def(card_ref)
        # 打出手牌：从 hand 移除（能量校验由 service 层完成）
        if card_ref not in self.hand:
            raise ValueError("手牌中不存在该卡牌")
        self.hand.remove(card_ref)
        self.discard.append(card_ref)
        q = SettlementQueue(self)
        for eff in c["effects"]:
            t = eff["type"]
            if t in ("apply_status", "set_status", "gain_block", "heal", "draw", "gain_energy"):
                target = eff.get("target", "player")
            else:
                target = eff.get("target", "enemy")
            q.push(EffectEvent(
                eff["type"], target=target, value=eff.get("value", 0),
                source="player", tags=eff.get("tags", []),
                extra={"status": eff.get("status"), "ticks": eff.get("ticks"),
                       "stack": eff.get("stack")}))
        log = q.run()
        self.truncated = self.truncated or q.truncated
        return log

    # ---------- 序列化（续局持久化） ----------
    def dump(self):
        ents = {}
        for k, ent in self.entities.items():
            ents[k] = {
                "name": ent["name"], "hp": ent["hp"], "max_hp": ent["max_hp"],
                "block": ent["block"], "alive": ent["alive"], "statuses": ent["statuses"],
            }
        companion_state = copy.deepcopy(self.companion_state)
        if companion_state is not None:
            ent = self.entities.get("companion")
            if ent is not None:
                companion_state["hp"] = ent["hp"]
                companion_state["wounded"] = not ent["alive"] or ent["hp"] <= 0
            else:
                companion_state["hp"] = 0
                companion_state["wounded"] = True
        return {
            "index": self.battle_index,
            "enemy": self.enemy_def["id"],
            "entities": ents,
            "draw_pile": list(self.draw_pile), "hand": list(self.hand),
            "discard": list(self.discard),
            "card_instances": dict(self.card_instances),
            "energy": self.energy, "max_energy": self.max_energy,
            "turn": self.turn, "in_turn": self.in_turn, "phase": self.phase,
            "truncated": self.truncated,
            "companion_state": companion_state,
        }

    @classmethod
    def from_state(cls, bstate, enemy_def, seed, health=75, max_health=75, relic_status="", relic_power=0):
        b = cls.__new__(cls)
        b.enemy_def = enemy_def
        b.seed = seed
        b.battle_index = bstate.get("index", 0)
        b.entities = {}
        for k, ed in bstate.get("entities", {}).items():
            b.entities[k] = {
                "key": k, "name": ed["name"], "max_hp": ed["max_hp"],
                "hp": ed["hp"], "block": ed["block"], "alive": ed["alive"],
                "statuses": ed.get("statuses", {}),
            }
        b.enemy = b.entities.get("enemy") or next((v for v in b.entities.values() if v["key"] == "enemy"), None)
        b.draw_pile = list(bstate.get("draw_pile", []))
        b.hand = list(bstate.get("hand", []))
        b.discard = list(bstate.get("discard", []))
        b.card_instances = dict(bstate.get("card_instances", {}))
        b.energy = bstate.get("energy", 3)
        b.max_energy = bstate.get("max_energy", 3)
        b.turn = bstate.get("turn", 0)
        b.in_turn = bstate.get("in_turn", False)
        b.phase = bstate.get("phase", 0)
        b.truncated = bstate.get("truncated", False)
        b.legacy_block = False  # 默认新规则；由 service 层按回放区间设置
        b.companion_state = copy.deepcopy(bstate.get("companion_state"))
        b.companion_def = (
            companions_mod.COMPANIONS.get(b.companion_state.get("id"), companions_mod.SQUIRE)
            if b.companion_state else None
        )
        if b.companion_state is not None and "companion" not in b.entities:
            ent = companions_mod.snapshot_for_battle(b.companion_state)
            if ent is not None:
                b.entities["companion"] = ent
        b.queue = SettlementQueue(b)
        return b

    # ---------- 快照 ----------
    def to_snapshot(self):
        ents = {}
        for k, ent in self.entities.items():
            ents[k] = {
                "name": ent["name"], "hp": ent["hp"], "max_hp": ent["max_hp"],
                "block": ent["block"], "alive": ent["alive"],
                "statuses": _statuses_public(ent["statuses"]),
            }
        return {
            "player": ents.get("player"), "enemy": ents.get("enemy"),
            "companion": _public_entity(ents.get("companion")),
            "energy": self.energy, "max_energy": self.max_energy,
            "turn": self.turn, "in_turn": self.in_turn, "truncated": self.truncated,
            "hand": list(self.hand),
        }

    def battle_result(self):
        p = self.entities["player"]
        e = self.enemy
        if p["alive"] and e["alive"]:
            return "ongoing"
        if not e["alive"]:
            return "won"
        if not p["alive"]:
            return "lost"
        return "ongoing"


# ---------- 辅助 ----------
def _seeded_rng(seed, salt):
    import random
    return random.Random((seed * 1000003 + salt) & 0x7fffffff)


def _final_damage(battle, ev):
    dmg = float(ev.value)
    target = battle.entities.get(ev.target)
    src = battle.entities.get(ev.source)
    if src and src.get("statuses", {}).get("strength") and "attack" in (ev.tags or ()):
        dmg += src["statuses"]["strength"]["value"]
    if target and target.get("statuses", {}).get("vulnerable"):
        dmg *= 1.5
    if target and target.get("statuses", {}).get("fragile"):
        dmg *= 1.5
    return max(0, int(round(dmg)))


def _hurt(ent, dmg):
    rem = dmg
    if ent["block"] > 0:
        absorbed = min(ent["block"], rem)
        ent["block"] -= absorbed
        rem -= absorbed
    if rem > 0:
        ent["hp"] = max(0, ent["hp"] - rem)


def _apply_status(ent, sid, value, mode="add", ticks=None):
    st = ent["statuses"]
    cur = st.get(sid)
    if mode == "add":
        if cur:
            cur["value"] += value
        else:
            st[sid] = {"mode": "add", "value": value, "ticks": ticks}
    elif mode == "replace":
        st[sid] = {"mode": "replace", "value": value, "ticks": ticks}
    else:
        st[sid] = {"mode": mode, "value": value, "ticks": ticks}


def _tick_statuses(ent):
    # 韧性随回合衰减；永久状态（ticks=None）不衰减
    keep = {}
    for sid, s in ent["statuses"].items():
        if s.get("ticks") is not None:
            if s["ticks"] > 1:
                keep[sid] = {**s, "ticks": s["ticks"] - 1}
            else:
                continue
        else:
            keep[sid] = s
    ent["statuses"] = keep


def _public_entity(ent):
    if ent is None:
        return None
    statuses = ent.get("statuses", {})
    return {
        "name": ent["name"], "hp": ent["hp"], "max_hp": ent["max_hp"],
        "block": ent["block"], "alive": ent["alive"],
        "statuses": statuses if isinstance(statuses, list) else _statuses_public(statuses),
    }


def _statuses_public(statuses):
    out = []
    for sid, s in statuses.items():
        out.append({"id": sid, "name": STATUS_INFO.get(sid, sid),
                    "value": s["value"], "mode": s["mode"]})
    return out