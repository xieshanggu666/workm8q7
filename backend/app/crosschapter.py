"""跨章状态（cross-chapter state）的统一交接与回放策略。

远征委托、奇遇印记（enc_state）、药水背包、伙伴、协作队伍归属等状态都需要：

1. **跨章交接**：章节通关时从 run 状态提取进交接快照（carry），开新章时由
   carry 重建新 run 的初始状态（在线推进与回放重建共用同一路径）；
2. **旧存档迁移**：旧 run 状态首次载入（续局/首行动）时补齐缺失字段，
   结构变化随本步动作原子落库（失败整体回滚）；
3. **回放校验形状**：旧 create 校验点是在「该字段尚未进入 run 状态」时
   录制的，重演时必须按当时的字段形状（该维不参与哈希）逐位比对；
4. **规则版本跨越**：首个携带 >= since 版本的动作之前，回放必须先把规则
   切换到位（补字段/剥旧货架/清瞬态标记），再推演该动作——与在线
   ``_migrate_state`` 在动作推演之前生效严格对齐。

此前这些维度散落在 ``_new_run_state`` / ``_carry_from_run`` /
``_migrate_state`` / ``state_checkpoint`` / ``replay`` 多处，每新增一类
跨章状态都要在五个地方各改一遍（2.5→2.9 连续四次重复证明了这个风险）。
本模块用一个数据驱动的注册表把它们收敛到同一处：每个维度声明
``key``（run 状态字段）、``since``（进入 run 状态的规则版本）、默认值、
迁移/规范化函数与回放穿越钩子，service 层只负责按注册表统一编排，
保证续局、结算与整程回放走同一策略、永不漏维。
"""
from __future__ import annotations

import copy

from . import companions as companions_mod
from . import encounters as enc_mod

# run 状态里「非注册结构维、但同样需要旧档 setdefault」的字段：它们自
# 远征早期就在交接快照里，没有旧版校验点形状问题，仅做缺省补齐。
LEGACY_RUN_DEFAULTS = {
    "forge_claimed": True,
    "shop": None,
    "commissions": [],
    "next_commission_seq": 1,
    "chapter": None,
    "chapters_total": None,
    "expedition_id": None,
}


class CrossChapterField:
    """一个跨章状态维度的交接/迁移/回放策略声明。

    - key：run 状态与 carry 快照中的字段名；
    - since：该字段进入 run 状态（与交接快照）的规则版本。更早的 create
      校验点按缺该维的形状录制，回放据此匹配 2^N 候选并在首个 since+ 动作
      之前完成结构穿越；
    - default_factory：旧档缺字段时补入的默认值（新章重建缺该维时同款）；
    - normalize_fn(value) -> (value, changed)：旧档/损坏档兜底规范化（幂等）；
    - shape_label：旧 create 形状候选名中的单字符标记（c/p/e/o）；
    - marker_key：回放内存 run 中「该维仍处于旧规则区间」的瞬态键
      （不写入存档、不参与校验点哈希）；
    - reset_on_cross：穿越时整体重置为默认值（True）还是 setdefault（False）；
    - shop_shelf：该维升级后商店货架新增的列表键（旧区间回放时剥除）。
    """

    __slots__ = ("key", "since", "default_factory", "normalize_fn",
                 "shape_label", "marker_key", "reset_on_cross",
                 "shop_shelf", "initial_shop_shelf", "legacy_value_factory",
                 "coerce_none")

    def __init__(self, key, since, default_factory, shape_label, marker_key,
                 normalize_fn=None, reset_on_cross=False, shop_shelf=None,
                 initial_shop_shelf=False, legacy_value_factory=None,
                 coerce_none=True):
        self.key = key
        self.since = since
        self.default_factory = default_factory
        self.normalize_fn = normalize_fn
        self.shape_label = shape_label
        self.marker_key = marker_key
        self.reset_on_cross = reset_on_cross
        self.shop_shelf = shop_shelf
        # create 形状缺该维时，回放起点的（可能存在的）商店货架是否也要剥掉
        self.initial_shop_shelf = initial_shop_shelf
        # 旧区间内该字段在回放内存 run 里的形态（药水旧规则下恒为空背包：
        # 保证战利品下标与货架按旧选项集重建）；None 表示保持新结构初值。
        self.legacy_value_factory = legacy_value_factory
        # 旧损坏交接快照里该字段可能以 NULL 出现：提取/重建时按默认值兜底
        # （历史代码用 list(carry.get(...)) 隐式把 None 兜成 []，注册表把这条
        # 规则显式化，避免旧档 carry 里的 None 穿透到公开视口/新章初态）。
        self.coerce_none = coerce_none

    def default(self):
        return copy.deepcopy(self.default_factory())

    def normalize(self, value):
        if self.normalize_fn is None:
            return value, False
        return self.normalize_fn(value)

    # ---------- 回放结构穿越 ----------
    def enter_legacy(self, sim):
        """把回放起点标记为「该维尚未进入 run 状态」的旧区间。

        与在线旧档语义对齐：旧规则下该维不参与推演（药水恒为空背包、
        商店不出药水/伙伴货架），因此字段本体在穿越前保持旧规则形态。
        """
        sim[self.marker_key] = True
        shop = sim.get("shop")
        if self.initial_shop_shelf and self.shop_shelf and isinstance(shop, dict):
            shop.pop(self.shop_shelf, None)
        if self.legacy_value_factory is not None:
            sim[self.key] = copy.deepcopy(self.legacy_value_factory())

    def cross(self, sim):
        """首个 since+ 动作【之前】完成结构切换（规则先行，与在线迁移对齐）。"""
        sim.pop(self.marker_key, None)
        if self.reset_on_cross or self.key not in sim:
            sim[self.key] = self.default()
        else:
            sim.setdefault(self.key, self.default())

    def strip_shop_shelf(self, sim):
        """旧区间动作生成的商店库存保持旧形状（剥掉该维新增加的货架）。"""
        shop = sim.get("shop")
        if self.shop_shelf and isinstance(shop, dict):
            shop.pop(self.shop_shelf, None)


def _companion_normalize(value):
    return companions_mod.normalize_state(value)


def _enc_normalize(value):
    return enc_mod.normalize_state(value)


# 顺序即「规则演进顺序」（2.5 药水 -> 2.6 伙伴 -> 2.8 印记 -> 2.9 协作），
# 也是回放形状候选名的维序。这是有语义的顺序：调整会改变旧 create ckpt
# 的候选构造与名称（回放候选按它的逆序嵌套，旧版本候选是完整候选的后缀）。
FIELDS = [
    CrossChapterField(
        key="companion", since="2.6.0",
        default_factory=lambda: None,
        shape_label="c", marker_key="_legacy_no_companion",
        normalize_fn=_companion_normalize,
        shop_shelf="companions", initial_shop_shelf=True),
    CrossChapterField(
        key="potions", since="2.5.0",
        default_factory=list,
        shape_label="p", marker_key="_legacy_no_potions",
        shop_shelf="potions", initial_shop_shelf=True,
        legacy_value_factory=list),
    CrossChapterField(
        key="enc_state", since="2.8.0",
        default_factory=enc_mod.fresh_state,
        shape_label="e", marker_key="_legacy_no_encounters",
        normalize_fn=_enc_normalize,
        reset_on_cross=True),
    CrossChapterField(
        key="coop_team", since="2.9.0",
        default_factory=lambda: None,
        shape_label="o", marker_key="_legacy_no_coop"),
]

FIELD_BY_KEY = {f.key: f for f in FIELDS}
MARKER_KEYS = {f.marker_key for f in FIELDS}

# state_checkpoint 的 include_* 关键字名（按维映射，避免 service 再硬编码）
_CHECKPOINT_KWARGS = {
    "companion": "include_companion",
    "potions": "include_potions",
    "enc_state": "include_encounters",
    "coop_team": "include_coop",
}


# ---------- 交接快照（carry） ----------
def extract_carry(run):
    """从章节通关 run 状态提取交接快照（深拷贝，与原状态解耦）。

    所有跨章状态维都从注册表取字段——新增维度只需在 FIELDS 注册，
    交接快照自动携带，不会再出现「状态进了 run 却忘记随章交接」。
    """
    instances = run.get("card_instances", {})
    carry = {
        "deck": list(run["deck"]),
        "card_instances": copy.deepcopy(instances),
        "next_card_seq": run.get("next_card_seq", len(instances) + 1),
        "relics": dict(run["relics"]),
        "gold": run["gold"],
        "max_health": run["max_health"],
        "health": run["health"],
        "base_energy": run.get("base_energy", 3),
        # 远征委托与其 uid 发号器（2.2 维，早于版本化形状机制）
        "commissions": copy.deepcopy(run.get("commissions", [])),
        "next_commission_seq": run.get("next_commission_seq", 1),
        # 来源章身份（仅作历史快照；新 run 身份只认真实入参）
        "chapter": run.get("chapter"),
        "chapters_total": run.get("chapters_total"),
    }
    for f in FIELDS:
        value = run.get(f.key)
        if value is None and f.coerce_none:
            # 已迁移损坏 run 内存里若残留 None，提取交接快照时归一为默认值
            value = f.default()
        carry[f.key] = copy.deepcopy(value)
    return carry


def value_for_new_run(carry, field, override=None):
    """开新章时从交接快照取一个跨章维的值；缺维/NULL（旧损坏快照）补默认值。

    override 非 None 时以显式入参为准（协作开章带 coop_team id）。
    """
    if override is not None:
        return copy.deepcopy(override)
    if field.key in carry:
        value = carry[field.key]
        if value is None and field.coerce_none:
            return field.default()
        return copy.deepcopy(value)
    return field.default()


# ---------- 旧存档迁移 ----------
def migrate_run_state(run, extra_defaults=None):
    """把任意时期的 run 状态就地补齐到当前结构（幂等）。

    仅处理注册表结构维 + extra_defaults；卡牌实例化（裸 id -> uid）等更早
    的迁移仍由 service._migrate_state 负责。返回是否发生结构变化，供调用方
    把本步事件标记 migrated（回放按 legacy 跳过逐位比对）。
    """
    changed = False
    for key, value in (extra_defaults or {}).items():
        if key not in run:
            run[key] = copy.deepcopy(value)
            changed = True
    for f in FIELDS:
        if f.key not in run:
            run[f.key] = f.default()
            changed = True
            continue
        new_value, norm_changed = f.normalize(run[f.key])
        if norm_changed:
            run[f.key] = new_value
            changed = True
    return changed


def fresh_carry_defaults():
    """普通局（carry=None）建局时的初始跨章状态。"""
    return {f.key: f.default() for f in FIELDS}


# ---------- 回放：形状候选 / 校验点 ----------
def shape_name(present):
    """2^N 形状候选名：全在场为 full，否则按维序拼 c0p1e1o1 式短名。"""
    if all(present.values()):
        return "full"
    return "".join(f.shape_label + str(int(bool(present[f.key]))) for f in FIELDS)


def checkpoint_kwargs(present):
    """{field_key: 是否在场} -> state_checkpoint 的 include_* 关键字。"""
    return {_CHECKPOINT_KWARGS[k]: bool(v) for k, v in present.items()}


def step_is_pre_field(ver, field, crossed_flag):
    """本步对某维是否属于旧区间：录制版本早于该维 since，或无版本号且尚未穿越。"""
    if ver:
        return _ver_lt(ver, field.since)
    return not crossed_flag


def _ver_lt(ver, baseline):
    """简单语义版本比较；ver 为空/损坏时视为更早（按旧规则处理）。"""
    try:
        return tuple(int(x) for x in str(ver).split(".")[:3]) < \
               tuple(int(x) for x in baseline.split(".")[:3])
    except (ValueError, AttributeError):
        return True
