import React, { useEffect, useState } from 'react'
import { api } from '../api'
import ReplayPlayer from './ReplayPlayer.jsx'

const EXP_STATUS = {
  in_progress: '远征进行中',
  won: '🏆 远征通关',
  lost: '💀 远征终结',
}
const RUN_STATUS = { in_progress: '进行中', won: '已通关', lost: '已战败' }
const EVENT_LABEL = {
  create: '🏕️ 创建远征',
  chapter_clear: '✅ 章节通关',
  advance: '🚪 进入下一章',
  settle: '⚑ 远征结算',
}

// 远征整程回放：章节切换 + 远征事件时间线 + 逐章完整可交互回放（复用 ReplayPlayer）。
// 数据来自 GET /api/expeditions/{id}/replay（后端只读重建，不写存档、不发解锁）。
export default function ExpeditionReplay({ expeditionId, onClose }) {
  const [data, setData] = useState(null)
  const [err, setErr] = useState('')
  const [sel, setSel] = useState(0) // 当前章节下标

  useEffect(() => {
    let alive = true
    api.expeditionReplay(expeditionId)
      .then((d) => { if (alive) setData(d) })
      .catch((e) => alive && setErr(e.message))
    return () => { alive = false }
  }, [expeditionId])

  if (err) {
    return (
      <div className="overlay">
        <div className="replaycard panel">
          <h2>远征整程回放</h2>
          <div className="error">{err}</div>
          <button className="primary" onClick={onClose}>关闭</button>
        </div>
      </div>
    )
  }
  if (!data) {
    return <div className="overlay"><div className="replaycard panel">加载远征回放数据…</div></div>
  }

  const exp = data.expedition
  const chapters = data.chapters || []
  const cur = chapters[Math.min(sel, chapters.length - 1)]

  return (
    <div className="overlay replay-overlay-full">
      <div className="replay-shell">
        <header className="replay-top">
          <div className="replay-title">
            🎬 远征整程回放
            <span className="replay-seed">种子 #{exp.seed}</span>
            <span className={`exp-status ${exp.status}`}>{EXP_STATUS[exp.status] || exp.status}</span>
            <span className="replay-seed">
              第 {exp.chapter}/{exp.chapters_total} 章 · 🔒 只读隔离
            </span>
          </div>
          <div className="exp-tabs">
            {chapters.map((ch, i) => {
              const v = ch.replay?.verification || {}
              const bad = (v.mismatch || 0) + (v.error || 0)
              return (
                <button
                  key={ch.run_id}
                  className={`mini exp-tab ${i === sel ? 'on' : ''}`}
                  onClick={() => setSel(i)}
                  title={`第 ${ch.chapter} 章 · ${RUN_STATUS[ch.status] || ch.status} · 校验 ${v.ok || 0} 通过${bad ? ` / ${bad} 异常` : ''}`}
                >
                  第{ch.chapter}章 {ch.status === 'won' ? '🏆' : ch.status === 'lost' ? '💀' : '▶'}
                  {bad > 0 && <em className="exp-tab-bad">⚠{bad}</em>}
                </button>
              )
            })}
          </div>
          <button className="mini" onClick={onClose}>退出回放 ✕</button>
        </header>

        <div className="exp-events">
          {(data.events || []).map((e) => (
            <span key={e.seq} className={`exp-event ${e.kind}`} title={JSON.stringify(e.payload)}>
              {EVENT_LABEL[e.kind] || e.kind}
              {e.payload?.chapter ? ` · 第${e.payload.chapter}章` : ''}
            </span>
          ))}
        </div>

        {cur && (
          <div className="exp-player">
            <ReplayPlayer
              key={cur.run_id}
              embedded
              replayData={cur.replay}
              runId={cur.run_id}
              onClose={onClose}
            />
          </div>
        )}
      </div>
    </div>
  )
}
