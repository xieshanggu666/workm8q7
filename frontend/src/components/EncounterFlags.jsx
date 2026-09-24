import React from 'react'
import { useStore } from '../store'

// 活跃奇遇 flag 侧栏：跨章抉择埋下的预兆随远征继承，下一章开头兑现
// （首场战斗修正/章节回血），部分 flag 还会在后续章奇遇节点触发续写。
export default function EncounterFlags() {
  const view = useStore((s) => s.view)
  const flags = view?.encounter_flags || []
  if (!view || flags.length === 0) return null
  return (
    <div className="panellist encounter-flags">
      <h3>🔗 奇遇印记</h3>
      {flags.map((f) => (
        <div key={f.flag} className={`enc-flag-row ${f.pending_opener ? 'pending' : ''}`}>
          <b>{f.title}</b>
          <span className="flag-desc">{f.desc}</span>
          <span className="flag-meta">
            始于第 {f.since_chapter} 章
            {f.opener_at ? ` · 预兆已在第 ${f.opener_at} 章兑现` : ' · 预兆待兑现'}
          </span>
        </div>
      ))}
    </div>
  )
}
