import React, { useState } from 'react'
import { api, handleActError } from '../api'
import { useStore } from '../store'
import { canDoSupply } from '../coopPerms'

export default function CompanionPanel() {
  const view = useStore((s) => s.view)
  const runId = useStore((s) => s.runId)
  const applyRun = useStore((s) => s.applyRun)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const companion = view?.companion
  if (!companion) return null
  const supplyAllowed = canDoSupply(view)

  async function setMode(mode) {
    if (busy) return
    setBusy(true)
    setErr('')
    try {
      const res = await api.act(runId, { action: 'companion_set_mode', mode })
      applyRun(res.run)
    } catch (e) {
      setErr(await handleActError(e, runId, applyRun))
    } finally {
      setBusy(false)
    }
  }

  const pct = Math.max(0, Math.min(100, (companion.hp / companion.max_health) * 100))
  return (
    <div className="panellist companion-panel">
      <h3>伙伴</h3>
      <div className={`companion-card ${companion.mode} ${companion.wounded ? 'wounded' : ''}`}>
        <div className="companion-head">
          <b>🛡️ {companion.name}</b>
          <span>{companion.mode === 'accompany' ? '随行' : '休整'}</span>
        </div>
        <div className="companion-hpbar">
          <i style={{ width: `${pct}%` }} />
        </div>
        <div className="companion-stats">
          生命 {companion.hp}/{companion.max_health} · 攻击 {companion.attack} · 援护 {companion.guard}
        </div>
        <p>{companion.desc}</p>
        {companion.wounded && <div className="comm-hint">已负伤，暂停参战；保持休整并前往休息节点即可治疗。</div>}
        {!companion.wounded && companion.mode === 'rest' && (
          <div className="comm-hint">休整中不会进入战斗；可随时重新安排随行。</div>
        )}
        {!view.in_battle && view.status === 'in_progress' && (
          <div className="companion-actions">
            <button
              className="mini"
              onClick={() => setMode('accompany')}
              disabled={busy || companion.mode === 'accompany' || !supplyAllowed}
              title={!supplyAllowed ? '只有资源位/队长能安排伙伴' : undefined}
            >
              随行
            </button>
            <button
              className="mini"
              onClick={() => setMode('rest')}
              disabled={busy || companion.mode === 'rest' || !supplyAllowed}
              title={!supplyAllowed ? '只有资源位/队长能安排伙伴' : undefined}
            >
              休整
            </button>
          </div>
        )}
        {err && <div className="error">{err}</div>}
      </div>
    </div>
  )
}
