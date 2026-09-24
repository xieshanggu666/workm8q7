from __future__ import annotations

# 伙伴：商店招募、可随行/休整、跨章继承的持久同伴。
# 战斗属性固定且简单，重点是把扣款、战斗伤害、负伤、治疗全部纳入同一条状态结算路径。

COMPANION_SKU = "companion:squire"
ACCOMPANY = "accompany"
REST = "rest"

SQUIRE = {
    "id": "squire",
    "name": "见习卫士 艾琳",
    "short_name": "艾琳",
    "title": "随行伙伴",
    "price": 50,
    "max_health": 20,
    "attack": 4,
    "guard": 3,
    "desc": "每轮开始攻击敌人 4 点；敌人对你造成穿透格挡的伤害时，她先抵挡 3 点。生命归零后负伤休整，休息节点可治疗。",
}

COMPANIONS = {SQUIRE["id"]: SQUIRE}


def get_companion(cid):
    return COMPANIONS[cid]


def make_companion(cid="squire"):
    c = get_companion(cid)
    return {
        "id": cid,
        "name": c["name"],
        "mode": ACCOMPANY,
        "hp": c["max_health"],
        "wounded": False,
    }


def public_companion(companion):
    if not companion:
        return None
    cid = companion.get("id", "squire")
    definition = COMPANIONS.get(cid, SQUIRE)
    return {
        "id": cid,
        "name": companion.get("name", definition["name"]),
        "mode": companion.get("mode", ACCOMPANY),
        "hp": companion.get("hp", 0),
        "max_health": definition["max_health"],
        "attack": definition["attack"],
        "guard": definition["guard"],
        "wounded": bool(companion.get("wounded") or companion.get("hp", 0) <= 0),
        "desc": definition["desc"],
    }


def offer(has_companion):
    """伙伴货架只在尚无伙伴时出现；每名玩家限定招募一名。"""
    if has_companion:
        return []
    return [{
        "sku": COMPANION_SKU,
        "kind": "companion",
        "companion": SQUIRE["id"],
        "name": SQUIRE["name"],
        "desc": SQUIRE["desc"],
        "price": SQUIRE["price"],
        "sold": False,
        "attack": SQUIRE["attack"],
        "guard": SQUIRE["guard"],
        "max_health": SQUIRE["max_health"],
    }]


def offer_view(item):
    return dict(item)


def normalize_state(companion):
    """旧档/损坏档兜底：补齐伙伴字段，并用定义校正越界生命（幂等）。"""
    if not companion:
        return companion, False
    changed = False
    cid = companion.get("id", "squire")
    if "id" not in companion:
        companion["id"] = cid
        changed = True
    definition = COMPANIONS.get(cid, SQUIRE)
    defaults = {
        "name": definition["name"],
        "mode": ACCOMPANY,
        "hp": definition["max_health"],
        "wounded": False,
    }
    for key, value in defaults.items():
        if key not in companion:
            companion[key] = value
            changed = True
    if companion["mode"] not in (ACCOMPANY, REST):
        companion["mode"] = ACCOMPANY
        changed = True
    hp = companion.get("hp", definition["max_health"])
    if not isinstance(hp, int) or isinstance(hp, bool) or hp < 0 or hp > definition["max_health"]:
        hp = max(0, min(definition["max_health"], int(hp or 0)))
        companion["hp"] = hp
        changed = True
    if hp <= 0:
        companion["wounded"] = True
        if companion["mode"] != REST:
            companion["mode"] = REST
            changed = True
    if hp > 0 and companion.get("wounded"):
        companion["wounded"] = False
        changed = True
    return companion, changed


def can_fight(companion):
    return bool(
        companion
        and companion.get("mode") == ACCOMPANY
        and companion.get("hp", 0) > 0
        and not companion.get("wounded")
    )


def heal_resting(companion):
    """休息节点治疗休整中的伙伴。返回 (是否治疗, 治疗量)。"""
    if not companion:
        return False, 0
    definition = COMPANIONS.get(companion.get("id", "squire"), SQUIRE)
    if companion.get("mode") != REST and companion.get("hp", 0) >= definition["max_health"]:
        return False, 0
    before = companion.get("hp", 0)
    healed = max(0, definition["max_health"] - before)
    companion["hp"] = definition["max_health"]
    companion["wounded"] = False
    return healed > 0, healed


def snapshot_for_battle(companion):
    """战斗中伙伴实体的独立快照；不随行/负伤时返回 None。"""
    if not can_fight(companion):
        return None
    definition = COMPANIONS.get(companion.get("id", "squire"), SQUIRE)
    return {
        "key": "companion",
        "name": companion.get("name", definition["name"]),
        "max_hp": definition["max_health"],
        "hp": min(companion.get("hp", definition["max_health"]), definition["max_health"]),
        "block": 0,
        "alive": True,
        "statuses": {},
    }
