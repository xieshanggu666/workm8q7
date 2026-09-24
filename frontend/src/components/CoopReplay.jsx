import React, { useEffect, useState } from 'react'
import { api } from '../api'
import ReplayPlayer from './ReplayPlayer.jsx'

const EVENT_LABEL = {
  form: '🏕️ 组建队伍',
  join: '🧑‍🤝‍🧑 加入队伍',
  role: '🎭 分配角色',
  start: '🚩 协作开赛',
  chapter_clear: '✅ 章节通关',
  advance: '🚪 进入下一章',
  settle: '⚑ 远征结算',
  leave: '🚪 退出队伍',
  disband: '🛑 解散队伍',
}

const LEDGER_LABEL = {
  battle_win: '⚔️ 战斗胜利',
  resource: '🎒 后勤操作',
  chapter: '✅ 章节协作金',
  win: '🏆 终程战利',
}

// 协作远征整程回放：队伍时间线 + 个人战利 + 远征事件 + 逐章可交互回放
// （每一步带操作者头像/名字）。数据来自 GET /api/coop/teams/{id}/replay，
// 后端只读重建，不写存档、不发解锁。
export default function CoopReplay({ teamId, onClose }) {
  const [data, setData] = useState(null)
  const [err, setErr] = useState('')
  const [sel, setSel] = useState(0)

  useEffect(() => {
    let alive = true
    api.coopTeamReplay(teamId)
      .then((d) => { if (alive) setData(d) })
      .catch((e) => alive && setErr(e.message))
    return () => { alive = false }
  }, [teamId])

  if (err) {
    return (
      <div className="overlay">
        <div className="replaycard panel">
          <h2>协作远征整程回放</h2>
          <div className="error">{err}</div>
          <button className="primary" onClick={onClose}>关闭</button>
        </div>
      </div>
    )
  }
  if (!data) {
    return <div className="overlay"><div className="replaycard panel">加载协作回放数据…</div></div>
  }

  const team = data.team
  const chapters = data.expedition_replay?.chapters || []
  const cur = chapters[Math.min(sel, chapters.length - 1)]
  const memberName = (id) => data.members.find((m) => m.id === id)
  const ledgerByMember = {}
  for (const row of data.ledger || []) {
    ;(ledgerByMember[row.member_id] ||= []).push(row)
  }

  return (
    <div className="overlay replay-overlay-full">
      <div className="replay-shell">
        <header className="replay-top">
          <div className="replay-title">
            🎬 协作远征整程回放 · {team.name}
            <span className="replay-seed">入队码 {team.code}</span>
            <span className="replay-seed">🔒 只读隔离</span>
          </div>
          {chapters.length > 0 && (
            <div className="exp-tabs">
              {chapters.map((ch, i) => {
                const v = ch.replay?.verification || {}
                const bad = (v.mismatch || 0) + (v.error || 0)
                return (
                  <button key={ch.run_id}
                          className={`mini exp-tab ${i === sel ? 'on' : ''}`}
                          onClick={() => setSel(i)}
                          title={`第 ${ch.chapter} 章 · ${ch.status} · 校验 ${v.ok || 0} 通过`}>
                    第{ch.chapter}章 {ch.status === 'won' ? '🏆' : ch.status === 'lost' ? '💀' : '▶'}
                    {bad > 0 && <em className="exp-tab-bad">⚠{bad}</em>}
                  </button>
                )
              })}
            </div>
          )}
          <button className="mini" onClick={onClose}>退出回放 ✕</button>
        </header>

        <div className="exp-events coop-events">
          {(data.events || []).map((e) => {
            const who = e.payload?.member
              ? `${e.payload.member.name}`
              : e.payload?.by ? memberName(e.payload.by)?.name : ''
            return (
              <span key={e.seq} className={`exp-event ${e.kind}`} title={JSON.stringify(e.payload)}>
                {EVENT_LABEL[e.kind] || e.kind}
                {who && <em className="coop-event-who">· {who}</em>}
                {e.payload?.chapter ? ` · 第${e.payload.chapter}章` : ''}
              </span>
            )
          })}
        </div>

        <div className="coop-replay-grid">
          <aside className="coop-ledger panel">
            <h3>队伍与个人战利</h3>
            {team.members.map((m) => {
              const rows = ledgerByMember[m.id] || []
              const gold = rows.filter((r) => r.amount > 0)
                .reduce((s, r) => s + r.amount, 0)
              const wins = rows.filter((r) => r.kind === 'battle_win').length
              const ops = rows.filter((r) => r.kind === 'resource').length
              return (
                <div key={m.id} className="coop-ledger-member">
                  <b>{m.icon} {m.name}</b>
                  <span className="coop-member-role">{m.role_label}</span>
                  <span className="coop-member-stat">⚔️{wins} · 🎒{ops} · 🏅{gold}</span>
                  <ul className="coop-ledger-rows">
                    {gold > 0 && rows.filter((r) => r.amount > 0).map((r, i) => (
                      <li key={i}>
                        {LEDGER_LABEL[r.kind] || r.kind} +{r.amount}（第 {r.chapter} 章）
                      </li>
                    ))}
                  </ul>
                </div>
              )
            })}
          </aside>

          <div className="exp-player">
            {cur ? (
              <ReplayPlayer key={cur.run_id} embedded replayData={cur.replay}
                            runId={cur.run_id} onClose={onClose}
                            actorResolver={(actorId) => memberName(actorId)} />
            ) : (
              <p className="replaycard panel">队伍尚未开赛，暂无章节回放。</p>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
