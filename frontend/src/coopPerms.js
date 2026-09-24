// 协作远征权限边界（2.9.0）的前端镜像：服务端在 act 时做权威校验（越权 403、
// 零副作用），这里仅用于把无权操作的按钮提前禁用/提示，避免误点。真正的边界
// 永远以后端为准——即使绕过前端提交，服务端也会在任何状态变更之前拒绝。

export function coopState(view) {
  return view?.coop || null
}

export function myRole(view) {
  return view?.coop?.me?.role || null
}

// 战斗动作：打牌 / 结束回合 / 战斗中用药
export function canDoBattle(view) {
  const role = myRole(view)
  return !role || role === 'leader' || role === 'combat'
}

// 资源动作：路线 / 锻造 / 商店 / 药水整理 / 伙伴 / 委托 / 奇遇
export function canDoSupply(view) {
  const role = myRole(view)
  return !role || role === 'leader' || role === 'supply'
}

export function isLeader(view) {
  return myRole(view) === 'leader'
}

export function permHint(view, kind) {
  if (!myRole(view)) return ''
  if (kind === 'battle' && !canDoBattle(view)) {
    return '只有 ⚔️ 战斗位（或队长）能进行战斗操作'
  }
  if (kind === 'supply' && !canDoSupply(view)) {
    return '只有 🎒 资源位（或队长）能进行资源/路线操作'
  }
  return ''
}
