import React, { useState } from 'react'
import { api, handleActError } from '../api'
import { useStore } from '../store'
import { canDoSupply } from '../coopPerms'
import PotionBelt from './PotionBelt.jsx'

function isPotionOption(o) {
  return (o.effects || []).some((e) => e.type === 'add_potion')
}

// 战后奖励：药水选项在背满时必须先选一瓶替换丢弃（选格领取），
// 其余奖励直接领取。服务端校验背满未选格 -> 400 且奖励仍可领（零副作用）。
export default function RewardView({ view }) {
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [replaceIdx, setReplaceIdx] = useState(null) // 正在选择替换格的药水选项
  const runId = useStore((s) => s.runId)
  const applyRun = useStore((s) => s.applyRun)

  const beltFull = (view.potions || []).length >= (view.potion_capacity || 3)
  // 协作远征：领取战后奖励是资源动作
  const supplyAllowed = canDoSupply(view)

  async function claim(idx, replace) {
    if (!supplyAllowed) return
    setBusy(true); setErr('')
    try {
      const body = { action: 'claim_reward', option: idx }
      if (replace != null) body.replace = replace
      const res = await api.act(runId, body)
      applyRun(res.run)
      setReplaceIdx(null)
    } catch (e) {
      setErr(await handleActError(e, runId, applyRun))
    } finally {
      setBusy(false)
    }
  }

  function pick(idx, o) {
    if (isPotionOption(o) && beltFull) {
      setReplaceIdx(idx)
      return
    }
    return claim(idx)
  }

  if (replaceIdx != null) {
    const o = view.reward_options[replaceIdx]
    return (
      <div className="overlay">
        <div className="rewardcard panel">
          <h2>背包已满</h2>
          <p className="shopdesc">
            领取「{o.name}」需要替换丢弃一瓶现有药水，请点击要替换的格位：
          </p>
          <PotionBelt replaceMode
                      onPickReplace={(slot) => claim(replaceIdx, slot)}
                      onCancelReplace={() => setReplaceIdx(null)} />
          {err && <div className="error">{err}</div>}
        </div>
      </div>
    )
  }

  return (
    <div className="overlay">
      <div className="rewardcard panel">
        <h2>选择奖励</h2>
        {!supplyAllowed && (
          <p className="comm-hint coop-deny">🔒 你是战斗位：战利品由 🎒 资源位/队长领取。</p>
        )}
        <div className="rewardopts">
          {view.reward_options.map((o, i) => (
            <button key={i} className="roption" onClick={() => pick(i, o)}
                    disabled={busy || !supplyAllowed} title={o.desc}>
              <span className="rkind">{o.name}</span>
              <span className="rdesc">
                {o.desc}
                {isPotionOption(o) && beltFull && <em className="replace-warn">（背包已满，领取需替换）</em>}
              </span>
              <span className="rgo">{isPotionOption(o) && beltFull ? '替换领取 →' : '领取 →'}</span>
            </button>
          ))}
        </div>
        {err && <div className="error">{err}</div>}
      </div>
    </div>
  )
}
