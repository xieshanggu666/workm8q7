"""跨章状态的统一交接与回放校验策略（规则 2.10.0）。

远征委托、奇遇印记、药水背包、伙伴、协作队伍归属这类「随章节交接快照跨章继承」
的 run 状态维度，过去在 service.py 里分散手写于六处：

  1. ``_new_run_state`` 的新档默认值；
  2. ``_carry_from_run`` 的交接快照取值；
  3. ``_migrate_state`` 的旧存档结构迁移；
  4. ``state_checkpoint`` 的旧形状哈希剥字段；
  5. ``_create_ckpt_candidates`` 的旧 create 校验点形状候选；
  6. ``replay`` 里逐维的「迁移点规则先行切换 + 旧区间标记」。

每新增一个维度都要同步修改这六处（以及 16→32 种形状候选），漏一处就会导致
续局、章节结算或整程回放分叉。本模块把这些维度登记为一张**注册表**
（``DIMENSIONS``），交接（``fresh_run_defaults`` / ``carry_snapshot`` /
``apply_carry``）、旧档迁移（``migrate_run_state``）与回放校验
（``shape_matrix`` / ``LegacyWindows``）全部由注册表驱动——新维度只在一处登记。

所有钩子都是纯函数（深拷贝、不触碰数据库），在线开章与回放重建共用同一套，
保证续局、结算与整程回放逐位一致。
"""
from __future__ import annotations

import copy

from . import companions as companions_mod
from . import encounters as enc_mod


# ------------------------------------------------------------------
# 维度注册表
# ------------------------------------------------------------------
class Dimension:
    """一个跨章状态维度的统一策略描述。

    属性
    ----
    key:
        run 状态 / 交接快照中的字段名。
    version:
        该字段进入 run 状态（与交接快照）的起始规则版本；更早的存档/日志
        没有此字段，首次载入做结构迁移、回放按旧形状候选兼容比对。
    fresh:
        新 run（普通局 / 无 carry）的默认值工厂（返回新对象，避免共享引用）。
    carry_default:
        旧版交接快照缺该字段时的兜底值工厂（None 表示沿用 fresh）。
    normalize:
        ``(value) -> (归一化值, 是否发生结构变化)``；旧档/损坏档兜底，必须幂等。
    auxiliary_keys:
        随该维度一起跨章的附属字段（如委托的 uid 单调发号器）。
    """

    __slots__ = ("key", "version", "fresh", "carry_default", "normalize",
                 "auxiliary_keys", "in_shape_matrix")

    def __init__(self, key, version, fresh, normalize, carry_default=None,
                 auxiliary_keys=(), in_shape_matrix=True):
        self.key = key
        self.version = version
        self.fresh = fresh
        self.normalize = normalize
        self.carry_default = carry_default or fresh
        self.auxiliary_keys = tuple(auxiliary_keys)
        # in_shape_matrix=False：字段虽随版本引入，但任何已录制的 create 校验点
        # 里它都在场（建局默认值始终写入），因此旧 create 形状候选不需要剥这一维。
        # 委托（2.2.0）即如此：_new_run_state 从远征上线起就给了 commissions=[]，
        # 历史 16 形状候选只含 companion/potions/enc_state/coop_team 四维。
        self.in_shape_matrix = in_shape_matrix

    def fresh_value(self):
        return copy.deepcopy(self.fresh())

    def carry_value(self, carry):
        """从交接快照取本维度的值；缺字段按 carry_default 兜底（深拷贝）。"""
        if self.key in carry:
            return copy.deepcopy(carry[self.key])
        return copy.deepcopy(self.carry_default())

    def migrate(self, run):
        """旧存档首次载入：缺字段补默认；有字段则幂等规范化。

        返回是否发生结构变化（调用方据此把本步事件标记 migrated/legacy）。
        """
        if self.key not in run:
            run[self.key] = self.fresh_value()
            return True
        normalized, changed = self.normalize(run[self.key])
        if changed:
            run[self.key] = normalized
        return changed


def _idempotent_passthrough(value):
    """无结构可迁的维度（药水列表 / 协作队伍 id）：原样保留。"""
    return value, False


# 注册顺序即「规则引入顺序」：旧 -> 新。回放形状矩阵与规则窗口都依赖该顺序，
# 新增维度只需追加到末尾。
DIMENSIONS = (
    Dimension(
        "commissions", "2.2.0",
        fresh=list,
        normalize=_idempotent_passthrough,
        carry_default=list,
        auxiliary_keys=("next_commission_seq",),
        # 远征（2.1.0）上线起 _new_run_state 就写入 commissions=[]，历史 create
        # 校验点始终含此字段：不参与旧形状剥维（形状候选保持原四维）。
        in_shape_matrix=False,
    ),
    Dimension(
        "potions", "2.5.0",
        fresh=list,
        normalize=_idempotent_passthrough,
        carry_default=list,
    ),
    Dimension(
        "companion", "2.6.0",
        fresh=lambda: None,
        normalize=companions_mod.normalize_state,
        carry_default=lambda: None,
    ),
    Dimension(
        "enc_state", "2.8.0",
        fresh=enc_mod.fresh_state,
        normalize=enc_mod.normalize_state,
        carry_default=enc_mod.fresh_state,
    ),
    Dimension(
        "coop_team", "2.9.0",
        fresh=lambda: None,
        normalize=_idempotent_passthrough,
        carry_default=lambda: None,
    ),
)
DIMENSIONS_BY_KEY = {d.key: d for d in DIMENSIONS}

# 参与旧 create 校验点形状枚举的维度（始终在场的维度不剥哈希——见 Dimension
# 的 in_shape_matrix 说明）。交接/迁移仍覆盖全部 DIMENSIONS。
SHAPE_DIMENSIONS = tuple(d for d in DIMENSIONS if d.in_shape_matrix)
# 形状枚举顺序（与历史 16 候选的嵌套顺序逐位一致）：coop 最外层、companion
# 最内层——新候选是旧候选的后缀，调试名称也保持 c/p/e/o 原序。虽与规则版本
# 顺序不完全一致，但各形状哈希两两不同（缺键与默认值键的 JSON 不同），枚举
# 顺序不影响命中唯一性；保留原序仅为逐位兼容调试输出与兜底选择。
_SHAPE_ENUM_OUTER_TO_INNER = ("coop_team", "enc_state", "potions", "companion")

# 附属字段的默认值与迁移（不独立成维度：它们依附于所属维度一起出现）。
_AUX_DEFAULTS = {"next_commission_seq": 1}

# 仅规则（无存档结构）变更：动作越过该版本前后回放时序不同，但 create 形状
# 无法区分新旧——按「首个 baseline+ 动作之前为旧规则区间」处理。
# key:（瞬态标记键, 起始版本）；瞬态键只存在于回放内存 run，不写入存档、
# 也不参与校验点哈希（见 CKPT_SKIP_KEYS）。
RULE_WINDOWS = (
    ("_legacy_block", "2.7.0"),
)

# 回放越过结构迁移点后需要从内存 run 移除的「旧规则抑制标记」（这些标记在旧
# 区间内让新规则产物——药水货架/伙伴货架——保持旧形状）。
SUPPRESS_KEYS = {
    "potions": "_legacy_no_potions",
    "companion": "_legacy_no_companion",
}


def auxiliary_defaults():
    """新 run 的附属字段默认值（独立拷贝）。"""
    return dict(_AUX_DEFAULTS)


def fresh_dimension_values():
    """普通局新 run 的全部维度默认值：key -> value（独立深拷贝）。"""
    return {d.key: d.fresh_value() for d in DIMENSIONS}


def fresh_auxiliary_values():
    """新 run 的附属发号器默认值。"""
    return {k: copy.deepcopy(v) for k, v in _AUX_DEFAULTS.items()}


def carry_dimension_values(carry):
    """从交接快照取全部维度值（缺字段按各维度兜底）。"""
    return {d.key: d.carry_value(carry) for d in DIMENSIONS}


def carry_auxiliary_values(carry):
    """从交接快照取附属字段（缺字段用默认值）。"""
    out = {}
    for d in DIMENSIONS:
        for key in d.auxiliary_keys:
            out[key] = copy.deepcopy(carry.get(key, _AUX_DEFAULTS.get(key)))
    return out


# ------------------------------------------------------------------
# 交接快照
# ------------------------------------------------------------------
# 随章节交接、但不属于任何「维度」的核心字段（牌组/资源/生命与远征身份）。
# 新 run 默认值在 service._new_run_state 内给出（普通局初始牌组/75 血），
# 这里只描述「从 run 摘快照」与「从快照恢复」两条对称路径。
CARRY_CORE_KEYS = (
    "deck", "card_instances", "next_card_seq",
    "relics", "gold", "max_health", "health", "base_energy",
    # carry 里的 chapter/chapters_total 是「来源章」历史快照，仅用于
    # 边界/摘要展示；新 run 身份只认 _new_run_state 的显式入参。
    "chapter", "chapters_total",
)


def carry_snapshot(run):
    """章节通关后的交接快照（深拷贝）：核心字段 + 注册表全部维度 + 附属发号器。

    注册表是快照字段的唯一权威清单——新增跨章维度在此自动纳入，无需再改
    service 的快照字典字面量。
    """
    carry = {key: copy.deepcopy(run.get(key)) for key in CARRY_CORE_KEYS}
    for d in DIMENSIONS:
        carry[d.key] = copy.deepcopy(run.get(d.key, d.carry_default()))
        for aux in d.auxiliary_keys:
            carry[aux] = copy.deepcopy(run.get(aux, _AUX_DEFAULTS.get(aux)))
    return carry


# ------------------------------------------------------------------
# 旧存档迁移
# ------------------------------------------------------------------
def migrate_run_state(run):
    """把任意时期的 run 状态就地补齐所有跨章维度字段（幂等）。

    返回是否发生结构变化。委托的附属发号器随 commissions 维度一起补齐；
    其余核心字段（chapter/expedition_id 等）由 service 按既有逻辑处理。
    """
    changed = False
    for d in DIMENSIONS:
        if d.migrate(run):
            changed = True
        for aux in d.auxiliary_keys:
            if aux not in run:
                run[aux] = copy.deepcopy(_AUX_DEFAULTS.get(aux))
                changed = True
    return changed


# ------------------------------------------------------------------
# 新章入场钩子（维度在「进入下一章」瞬间的结算）
# ------------------------------------------------------------------
def enter_new_chapter(values, chapter):
    """开新章时对快照值跑各维度的入章结算（就地修改 values）。

    返回伴随开章产生的附加收益（目前只有奇遇印记的开章赐福治疗量）。
    在线推进与回放重建共用本函数，逐位一致。
    """
    opener_heal = 0
    enc = values.get("enc_state")
    if chapter is not None and chapter > 1 and isinstance(enc, dict):
        # 节点级痕迹清空（保留跨章 flag/已完成链），再兑现各 flag 的一次性预兆。
        enc_mod.reset_for_chapter(enc)
        opener_heal, _opened = enc_mod.on_chapter_begin(enc, chapter)
    return opener_heal


# ------------------------------------------------------------------
# 回放：旧 create 校验点形状矩阵
# ------------------------------------------------------------------
def shape_matrix():
    """枚举所有「字段形状」组合：每个结构维是否参与校验点哈希。

    旧版本 create 状态缺少后加的维度字段，录制的 ckpt 按当时的形状哈希；
    回放逐组合生成候选，命中哪种形状本 run 起点就按哪种形状对齐。

    组合顺序至关重要：优先「完整形状」（最外层维恒 True），再按规则引入
    顺序的逆序（新 -> 旧）逐层剥字段——较新版本的候选是较旧版本候选的超集。
    只枚举 SHAPE_DIMENSIONS（始终在场的维度不剥维）。
    返回 [(name, {key: included_bool}), ...]，第一项为完整形状（含全部维度）。
    """
    # 枚举顺序与历史 16 候选嵌套一致：coop 最外层 -> companion 最内层
    ordered = tuple(DIMENSIONS_BY_KEY[k]
                    for k in _SHAPE_ENUM_OUTER_TO_INNER
                    if DIMENSIONS_BY_KEY[k].in_shape_matrix)

    def _gen(idx, included):
        if idx == len(ordered):
            yield dict(included)
            return
        dim = ordered[idx]
        for present in (True, False):
            included[dim.key] = present
            yield from _gen(idx + 1, included)
            del included[dim.key]

    combos = list(_gen(0, {}))
    # 历史调试名称（沿用原 16 形状的缩写与顺序：o 最外层、c 最内层）
    labels = {"companion": "c", "potions": "p", "enc_state": "e",
              "coop_team": "o"}
    result = []
    for shape_included in combos:
        included = {d.key: True for d in DIMENSIONS}
        included.update(shape_included)
        if all(shape_included.values()):
            name = "full"
        else:
            # ordered 为 o,e,p,c（新 -> 旧），名称按历史 c,p,e,o 顺序输出
            name = "".join(f"{labels[k]}{int(shape_included[k])}"
                           for k in ("companion", "potions", "enc_state", "coop_team"))
        result.append((name, included))
    return result


# ------------------------------------------------------------------
# 回放：逐动作的旧规则窗口跟踪
# ------------------------------------------------------------------
def _ver_lt(ver, baseline):
    """语义版本比较；ver 为空/损坏时视为 True（按旧日志处理）。"""
    try:
        return tuple(int(x) for x in str(ver).split(".")[:3]) < \
               tuple(int(x) for x in baseline.split(".")[:3])
    except (ValueError, AttributeError):
        return True


class LegacyWindows:
    """回放一次 run 期间，统一跟踪所有跨章维度/纯规则的旧版本窗口。

    - 结构维度：由 create 校验点命中的形状决定起点是否处于「字段缺席」区间；
      遇到首个 version+ 动作时，在推演该动作【之前】迁移（规则先行切换）。
    - 纯规则窗口（格挡/援护时序 2.7.0）：create 形状无法区分，统一按
      「首个 version+ 动作之前」为旧区间，瞬态标记挂在回放内存 run 上。

    迁移点之前的步骤其录制哈希不含后加字段，按 legacy 呈现并跳过逐位比对；
    迁移点动作本身与在线语义对齐（在线 /resume 静默迁移或 /act 迁移都发生在
    动作推演之前），严格校验。
    """

    def __init__(self, create_included):
        # create_included: {形状维key: 该字段是否参与 create 校验点哈希}；
        # False 表示这份旧日志录制时还没有该字段。不在形状矩阵中的维度
        # （commissions：create 校验点始终含此字段）恒视为在场，永不进旧窗口。
        self._dims = {}
        for d in DIMENSIONS:
            present = True if not d.in_shape_matrix else \
                bool(create_included.get(d.key, True))
            self._dims[d.key] = {"version": d.version,
                                 "present": present, "crossed": present}
        self._rule_flags = {
            key: {"version": ver, "present_at_create": True, "crossed": False}
            for key, ver in RULE_WINDOWS
        }

    # ---- 结构维度 ----
    def is_pre(self, key, action="create"):
        """当前是否仍处于某结构维度缺席的旧区间（create 帧之后按动作计）。"""
        w = self._dims[key]
        return (not w["present"]) and (not w["crossed"]) and action != "create"

    def is_create_pre(self, key):
        """create 起点是否缺该字段。"""
        return not self._dims[key]["present"]

    def crossing(self, key, action, ver):
        """本动作是否是某维度的「迁移点动作」（需在推演前切换规则）。"""
        w = self._dims[key]
        return (action != "create" and not w["present"] and not w["crossed"]
                and bool(ver) and not _ver_lt(ver, w["version"]))

    def mark_crossed(self, key):
        self._dims[key]["crossed"] = True

    def ensure_migrated(self, key, sim):
        """越过迁移点：摘除旧规则抑制标记并补全新字段（与在线迁移对齐）。"""
        suppress = SUPPRESS_KEYS.get(key)
        if suppress:
            sim.pop(suppress, None)
        dim = DIMENSIONS_BY_KEY[key]
        if key not in sim:
            sim[key] = dim.fresh_value()
        else:
            normalized, changed = dim.normalize(sim[key])
            if changed:
                sim[key] = normalized

    def enter_old_window(self, sim):
        """回放起点：对 create 缺字段的维度布置旧区间（抑制新规则产物）。"""
        if not self._dims["companion"]["present"]:
            sim["_legacy_no_companion"] = True
            if sim.get("shop"):
                sim["shop"].pop("companions", None)
        if not self._dims["potions"]["present"]:
            sim["_legacy_no_potions"] = True
            sim["potions"] = []
            if sim.get("shop"):
                sim["shop"].pop("potions", None)
        # 纯规则窗口：建场首回合即处于旧时序（战斗全程使用同一格挡时序）
        for key in self._rule_flags:
            sim[key] = True

    def checkpoint_includes(self):
        """当前帧校验点应包含哪些结构维度：{key: bool}（旧区间内剥字段）。

        不在形状矩阵中的维度（commissions）恒包含——历史校验点一直哈希它。
        """
        return {d.key: (True if not d.in_shape_matrix else not self.is_pre(d.key))
                for d in DIMENSIONS}

    def legacy_step(self, action):
        """本步是否按 legacy 呈现（任一维度仍处旧区间）。"""
        return action != "create" and any(
            self.is_pre(d.key, action) for d in DIMENSIONS)

    # ---- 纯规则窗口 ----
    def rule_crossing(self, flag_key, action, ver):
        w = self._rule_flags[flag_key]
        return (action != "create" and not w["crossed"]
                and bool(ver) and not _ver_lt(ver, w["version"]))

    def mark_rule_crossed(self, flag_key, sim):
        self._rule_flags[flag_key]["crossed"] = True
        sim.pop(flag_key, None)

    def rule_pre_step(self, action):
        """本步是否仍处于任一纯规则旧时序区间（create 帧不豁免：create 无战斗）。"""
        return action != "create" and any(
            not w["crossed"] for w in self._rule_flags.values())

    # ---- 统一迁移点 ----
    def apply_crossings(self, sim, action, ver):
        """在推演本步动作之前统一切换所有已到迁移点的维度/规则。

        返回 (结构维度迁移集合, 纯规则切换集合)——调用方用于日志/统计。
        """
        crossed_dims, crossed_rules = [], []
        for d in DIMENSIONS:
            if self.crossing(d.key, action, ver):
                self.mark_crossed(d.key)
                self.ensure_migrated(d.key, sim)
                crossed_dims.append(d.key)
        for key in self._rule_flags:
            if self.rule_crossing(key, action, ver):
                self.mark_rule_crossed(key, sim)
                crossed_rules.append(key)
        return crossed_dims, crossed_rules

    def close_all(self, sim):
        """2.4.0 受影响章的修复点：一次性越过全部剩余窗口（规则先行切换）。"""
        for d in DIMENSIONS:
            if not self._dims[d.key]["crossed"]:
                self.mark_crossed(d.key)
                self.ensure_migrated(d.key, sim)
        for key in self._rule_flags:
            if not self._rule_flags[key]["crossed"]:
                self.mark_rule_crossed(key, sim)

    def any_crossing(self, action, ver):
        """本动作是否为任一维度/规则的迁移点。"""
        if any(self.crossing(d.key, action, ver) for d in DIMENSIONS):
            return True
        return any(self.rule_crossing(k, action, ver) for k in self._rule_flags)

    def step_pre_version(self, key, ver):
        """以【本步录制版本】判断该维度在本步动作时是否仍是旧规则。

        用于动作产生的新规则产物（商店药水/伙伴货架、战后药水战利品）：
        2.4.0 单局的 create 形状已含 potions 键，但 2.4.0 动作生成的商店
        确实没有药水货架，必须按录制版本剥除，而非按字段迁移标记。
        """
        w = self._dims[key]
        if ver:
            return _ver_lt(ver, w["version"])
        return not w["crossed"]
