import React, { useState } from 'react'
import { api, handleActError } from '../api'
import { useStore } from '../store'
import { canDoBattle, canDoSupply } from '../coopPerms'

// 药水背包：可跨章携带的消耗品，限容量（view.potion_capacity）。
// - 非战斗：每瓶可主动丢弃；背满时购买/领取新药水需选择被替换的格位
//   （由 ShopView/RewardView 通过 onPickReplace 回调驱动本组件高亮选择）。
// - 战斗中：玩家自己的回合可点击使用（择机）；播放/结算期间由父级锁定。
// 消耗与战斗效果在服务端同一动作原子结算，request_id 幂等防止双击重复生效。
export default function PotionBelt({ inBattle = false, busy = false, onUsed = null,
                                    onUse = null,
                                    replaceMode = false, onPickReplace = null,
                                    onCancelReplace = null }) {
  const view = useStore((s) => s.view)
  const runId = useStore((s) => s.runId)
  const applyRun = useStore((s) => s.applyRun)
  const [localBusy, setLocalBusy] = useState(false)
  const [err, setErr] = useState('')

  const potions = view?.potions || []
  const capacity = view?.potion_capacity || 3
  if (!view) return null

  const acting = busy || localBusy
  // 协作远征：战斗中用药是战斗动作，非战斗丢弃是资源动作
  const canUseInBattle = inBattle && view.battle?.in_turn && !acting
    && canDoBattle(view)
  const canManage = !inBattle && canSupply(view)

  async function send(body) {
    setLocalBusy(true); setErr('')
    try {
      const res = await api.act(runId, body)
      applyRun(res.run)
      onUsed?.(res)
    } catch (e) {
      setErr(await handleActError(e, runId, applyRun))
    } finally {
      setLocalBusy(false)
    }
  }

  function use(slot) {
    if (!canUseInBattle) return
    // 战斗中由父组件（BattleView）统一发动作并播放结算动画，避免双重演出
    if (onUse) return onUse(slot)
    return send({ action: 'use_potion', slot })
  }

  function discard(slot) {
    if (inBattle || acting || !canManage) return
    return send({ action: 'discard_potion', slot })
  }

  const emptySlots = Math.max(0, capacity - potions.length)

  return (
    <div className={`panellist potion-belt ${replaceMode ? 'replace-mode' : ''}`}>
      <h3>
        🧪 药水背包
        <span className="potion-count">{potions.length}/{capacity}</span>
      </h3>
      {replaceMode && (
        <div className="replace-hint">
          背包已满：点击一瓶将其替换丢弃
          <button className="mini" onClick={onCancelReplace} disabled={acting}>取消</button>
        </div>
      )}
      <div className="potion-slots">
        {potions.map((p) => (
          <span key={p.slot}
                className={`potion-slot ${replaceMode ? 'pickable' : ''} ${canUseInBattle ? 'usable' : ''}`}
                title={p.desc}
                onClick={() => (replaceMode ? onPickReplace(p.slot)
                                            : canUseInBattle ? use(p.slot) : null)}>
            <span className="potion-icon">{p.icon}</span>
            <span className="potion-name">{p.name}</span>
            {!replaceMode && !inBattle && canManage && (
              <button className="potion-drop" title="丢弃（不可恢复）"
                      disabled={acting}
                      onClick={(e) => { e.stopPropagation(); discard(p.slot) }}>
                ✕
              </button>
            )}
            {replaceMode && <em className="potion-replace-tag">替换</em>}
            {canUseInBattle && <em className="potion-use-tag">使用</em>}
          </span>
        ))}
        {Array.from({ length: emptySlots }).map((_, i) => (
          <span key={`empty-${i}`} className="potion-slot empty">空</span>
        ))}
      </div>
      {inBattle && (
        <p className="potion-tip">
          {view.battle?.in_turn ? '自己的回合可点击药水使用（不耗能量）。' : '等待你的回合…'}
        </p>
      )}
      {err && <div className="error">{err}</div>}
    </div>
  )
}
