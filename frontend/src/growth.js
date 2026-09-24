// 卡牌成长树：成长节点标签/名称与实例成长状态读取。
// 后端 2.3.0 起手牌/牌组项为 {uid,id,cost,growth:[{node,cost}],growth_nodes?}；
// 旧版字段 forges（分支 id 列表）与裸 id 仍做兼容显示。

export const NODE_TAG = {
  sharpen: '锋', empower: '强', refine: '炼',
  keen: '破', bulwark: '壁',
  amplify: '激', insight: '察',
  economize: '节', streamline: '顺',
  rend: '裂', execute: '决',
  fortress: '垒', thorns: '反',
  overload: '载', attunement: '鸣',
  enlightenment: '迪', flow: '涌',
  frugality: '俭', mastery: '通',
  gush: '泉', zero_form: '零',
}

// 节点 id -> 中文名（树定义随视口 growth_tree 下发；这是标签级兜底）
export const NODE_NAME = {
  sharpen: '锋锐', empower: '强效', refine: '精炼',
  keen: '破甲', bulwark: '壁垒',
  amplify: '激化', insight: '洞察',
  economize: '节流', streamline: '顺发',
  rend: '裂甲', execute: '处决',
  fortress: '堡垒', thorns: '反震',
  overload: '超载', attunement: '共鸣',
  enlightenment: '启迪', flow: '涌动',
  frugality: '俭省', mastery: '精通',
  gush: '涌泉', zero_form: '零式',
}

// 从手牌/牌组项读取已解锁成长节点 id 列表（兼容旧 forges / 缺失字段）
export function growthNodesOf(item) {
  if (!item || typeof item === 'string') return []
  if (Array.isArray(item.growth_nodes) && item.growth_nodes.length) return item.growth_nodes
  if (Array.isArray(item.growth)) return item.growth.map((r) => (typeof r === 'string' ? r : r?.node)).filter(Boolean)
  return item.forges || []
}

export function growthTag(nid) {
  return NODE_TAG[nid] || NODE_NAME[nid] || nid
}

export function growthName(nid, tree) {
  const hit = (tree || []).find((n) => n.id === nid)
  return hit?.name || NODE_NAME[nid] || nid
}

// 节点在树上的层（1/2/3）；未知时返回 0
export function nodeTier(nid, tree) {
  return (tree || []).find((n) => n.id === nid)?.tier || 0
}
