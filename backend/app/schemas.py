from pydantic import BaseModel
from typing import Optional


class CreateRunRequest(BaseModel):
    seed: Optional[int] = None


class CreateExpeditionRequest(BaseModel):
    seed: Optional[int] = None
    chapters: Optional[int] = None  # 章节数（默认 3，上限见 service.MAX_CHAPTERS）


class AdvanceRequest(BaseModel):
    # 请求级幂等：同一令牌重复/并发提交返回首次响应，不会重复开章
    request_id: Optional[str] = None
    # 2.9.0：协作远征推进章节时，只有队长可操作（单人远征忽略该字段）
    member_id: Optional[str] = None


class ActRequest(BaseModel):
    action: str
    node: Optional[str] = None
    card: Optional[str] = None
    target: Optional[str] = "enemy"
    option: Optional[int] = None
    growth_node: Optional[str] = None  # forge 动作用：成长树节点 id（2.3.0+）
    branch: Optional[str] = None  # forge 旧字段（兼容旧客户端/日志：等同 growth_node）
    kind: Optional[str] = None    # shop_buy 动作用：货架类别 card/relic/potion
    sku: Optional[str] = None     # shop_buy/commission_accept 动作用：货架项/委托挂单项 id
    commission: Optional[str] = None  # commission_claim 动作用：委托实例 id
    slot: Optional[int] = None    # use_potion/discard_potion 动作用：药水背包格位（0 基）
    replace: Optional[int] = None  # 背满购买/领取药水时：被替换丢弃的格位（0 基）
    mode: Optional[str] = None    # companion_set_mode：accompany/rest
    # 跨章节奇遇链（2.8.0）：encounter_choice 在奇遇节点提交抉择
    chain: Optional[str] = None       # 奇遇链 id（如 wounded_traveler）
    enc_choice: Optional[str] = None  # 链内抉择 id（如 aid/rob/ignore）
    # 多人协作远征（2.9.0）：提交该动作的队伍成员 id（协作章节 run 必填）
    member_id: Optional[str] = None
    # 并发控制（可选，老客户端不带也完全兼容）：
    request_id: Optional[str] = None   # 客户端生成的请求令牌：同令牌重复提交返回首次结果
    expected_rev: Optional[int] = None  # 所依据视口的存档版本；过期提交 -> 409


# ---------- 多人协作远征（2.9.0） ----------
class CreateCoopTeamRequest(BaseModel):
    name: Optional[str] = None        # 队伍名（缺省用队长名生成）
    captain_name: Optional[str] = None  # 队长显示名
    seed: Optional[int] = None
    chapters: Optional[int] = None


class JoinCoopTeamRequest(BaseModel):
    code: str
    member_name: Optional[str] = None
    request_id: Optional[str] = None


class CoopMemberRequest(BaseModel):
    """队伍管理动作的共同字段：member_id 为入会时拿到的成员 id。"""
    member_id: str
    request_id: Optional[str] = None


class AssignRoleRequest(CoopMemberRequest):
    target_id: str
    role: str          # combat / supply


class CoopAdvanceRequest(CoopMemberRequest):
    pass
