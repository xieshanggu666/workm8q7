import React, { useState } from 'react'
import { api, handleActError } from '../api'
import { useStore } from '../store'
import { canDoSupply } from '../coopPerms'

const STATUS_LABEL = {
  active: '进行中',
  ready: '可领奖',
  claimed: '已完成',
  failed: '已失败',
}
const FAIL_LABEL = {
  expired: '超期未完成',
  battle_lost: '战败',
}

// 远征委托追踪：侧栏展示全部委托（进行中/可领奖/已完成/失败），
// 可领奖的委托在任何非战斗节点（含章节通关后、推进前）一键领取；
// 领奖走 request_id 幂等 + 状态机防重复（重复领取 409，不重复发奖）。
export default function CommissionPanel() {
  const view = useStore((s) => s.view)
  const runId = useStore((s) => s.runId)
  const applyRun = useStore((s) => s.applyRun)
  const [busyId, setBusyId] = useState(null)
  const [err, setErr] = useState('')

  const commissions = view?.commissions || []
  if (!view?.expedition || commissions.length === 0) return null
  // 领奖是资源动作：协作远征里只有资源位/队长能领
  const supplyAllowed = canDoSupply(view)

  // 终态（已领/失败）只保留最近若干条，避免侧栏越积越长
  const open = commissions.filter((q) => q.status === 'active' || q.status === 'ready')
  const closed = commissions.filter((q) => q.status === 'claimed' || q.status === 'failed').slice(-4)

  async function claim(q) {
    setBusyId(q.id); setErr('')
    try {
      const res = await api.act(runId, { action: 'commission_claim', commission: q.id })
      applyRun(res.run)
    } catch (e) {
      setErr(await handleActError(e, runId, applyRun))
    } finally {
      setBusyId(null)
    }
  }

  return (
    <div className="panellist commissions">
      <h3>📜 远征委托</h3>
      {open.map((q) => (
        <CommissionRow key={q.id} q={q} busy={busyId === q.id} onClaim={() => claim(q)}
                       claimingLocked={!!view.in_battle || !supplyAllowed}
                       lockReason={!supplyAllowed ? '只有资源位/队长能领取委托' : null} />
      ))}
      {closed.length > 0 && (
        <div className="commissions-closed">
          {closed.map((q) => (
            <span key={q.id} className={`chip comm-chip ${q.status}`} title={q.objective}>
              {q.kind === 'battle' ? '讨伐' : '贸易'}·
              {q.status === 'failed' ? FAIL_LABEL[q.fail_reason] || STATUS_LABEL[q.status] : STATUS_LABEL[q.status]}
            </span>
          ))}
        </div>
      )}
      {err && <div className="error">{err}</div>}
    </div>
  )
}

function CommissionRow({ q, busy, onClaim, claimingLocked, lockReason }) {
  const pct = Math.min(100, Math.round((q.progress / q.target) * 100))
  const lockedHint = lockReason || '战斗结束后再领取'
  return (
    <div className={`commission ${q.status}`}>
      <div className="comm-head">
        <b>{q.kind === 'battle' ? '⚔️ 讨伐委托' : '💰 贸易委托'}</b>
        <span className={`comm-status ${q.status}`}>{STATUS_LABEL[q.status]}</span>
      </div>
      <div className="comm-obj">{q.objective}</div>
      <div className="comm-bar">
        <span style={{ width: `${q.status === 'ready' ? 100 : pct}%` }} />
      </div>
      <div className="comm-meta">
        <span>进度 {q.progress}/{q.target}</span>
        <span>限第 {q.deadline_chapter} 章前
          {q.chapters_left > 0 ? `（剩 ${q.chapters_left} 章）` : '（本章截止）'}
        </span>
      </div>
      <div className="comm-reward">奖励：{q.reward.text}</div>
      {q.can_claim && (
        <button className="primary comm-claim" onClick={onClaim}
                disabled={busy || claimingLocked}
                title={claimingLocked ? lockedHint : '领取委托奖励'}>
          {busy ? '领取中…' : claimingLocked ? (lockReason ? '非资源位不可领' : '战斗中暂不可领') : '🎁 领取奖励'}
        </button>
      )}
    </div>
  )
}
