import React, { useState } from 'react'
import { api, handleActError } from '../api'
import { useStore } from '../store'
import { canDoSupply } from '../coopPerms'
import PotionBelt from './PotionBelt.jsx'

// 跨章节奇遇抉择面板（规则 2.8.0）：进入「奇遇」节点后服务端挂起待抉择链，
// 本组件展示剧情与选项；提交走 encounter_choice 动作（request_id 幂等 +
// 状态守卫 + 统一事务），代价先校验、失败零副作用。
// 药水奖励背满时按商店同款流程指定替换格。
export default function EncounterView({ view }) {
  const enc = view.encounter
  const runId = useStore((s) => s.runId)
  const applyRun = useStore((s) => s.applyRun)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [replaceFor, setReplaceFor] = useState(null) // {chain, choice} 待选替换格

  if (!enc) return null
  // 协作远征：奇遇抉择是资源动作（伏击战触发后由战斗位接手）
  const supplyAllowed = canDoSupply(view)

  async function submit(chain, choice) {
    if (!supplyAllowed) return
    setBusy(true); setErr('')
    try {
      const res = await api.act(runId, {
        action: 'encounter_choice', chain, enc_choice: choice,
      })
      applyRun(res.run)
    } catch (e) {
      setErr(await handleActError(e, runId, applyRun))
    } finally {
      setBusy(false)
    }
  }

  async function confirmReplace(slot) {
    setBusy(true); setErr('')
    try {
      const res = await api.act(runId, {
        action: 'encounter_choice',
        chain: replaceFor.chain, enc_choice: replaceFor.choice, replace: slot,
      })
      applyRun(res.run)
      setReplaceFor(null)
    } catch (e) {
      setErr(await handleActError(e, runId, applyRun))
    } finally {
      setBusy(false)
    }
  }

  function onClick(ch) {
    // 药水奖励 + 背满：先进入替换选择模式（服务端同样兜底 400）
    const grantsPotion = (ch.desc || '').length >= 0 &&
      (ch.label.includes('药水') || /药水/.test(ch.desc))
    if (grantsPotion && (view.potions || []).length >= (view.potion_capacity || 3)) {
      setReplaceFor({ chain: enc.chain, choice: ch.id })
      return
    }
    submit(enc.chain, ch.id)
  }

  return (
    <div className="overlay">
      <div className="encountercard panel">
        <div className="enc-head">
          <span className="enc-badge">✨ 跨章奇遇</span>
          {view.expedition && (
            <span className="enc-chapter">第 {view.expedition.chapter} 章</span>
          )}
        </div>
        <h2>{enc.title}</h2>
        <p className="enc-text">{enc.text}</p>

        {(view.encounter_flags || []).length > 0 && (
          <div className="enc-active-flags">
            {(view.encounter_flags || []).map((f) => (
              <span key={f.flag} className="chip enc-flag" title={f.desc}>
                🔗 {f.title}
                {f.pending_opener && <em className="flag-soon">预兆将至</em>}
              </span>
            ))}
          </div>
        )}

        {replaceFor ? (
          <div className="enc-replace">
            <p className="shopdesc">
              背包已满：选择一瓶被替换丢弃的药水，新药水将进入该格。
            </p>
            <PotionBelt replaceMode
                        onPickReplace={confirmReplace}
                        onCancelReplace={() => setReplaceFor(null)} />
          </div>
        ) : (
          <div className="enc-choices">
            {!supplyAllowed && (
              <p className="comm-hint coop-deny">🔒 你是战斗位：奇遇抉择由 🎒 资源位/队长做出（若触发伏击战将由你接手）。</p>
            )}
            {enc.choices.map((ch) => (
              <button key={ch.id} className="enc-choice"
                      onClick={() => onClick(ch)} disabled={busy || !supplyAllowed}>
                <span className="enc-choice-label">{ch.label}</span>
                <span className="enc-choice-desc">{ch.desc}</span>
              </button>
            ))}
          </div>
        )}
        {err && <div className="error">{err}</div>}
      </div>
    </div>
  )
}
