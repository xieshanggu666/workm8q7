import React, { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { useStore } from '../store'

// 协作远征侧栏：队伍成员、各自角色、个人贡献/战利，以及“我”的权限提示。
// 大厅轮询同款轻量刷新（只在协作远征中挂载），让各成员看到彼此的最新状态。
const ROLE_ICON = { leader: '👑', combat: '⚔️', supply: '🎒' }
const ROLE_NAME = { leader: '队长', combat: '战斗位', supply: '资源位' }

export default function CoopPanel() {
  const view = useStore((s) => s.view)
  const runId = useStore((s) => s.runId)
  const [team, setTeam] = useState(null)
  const [err, setErr] = useState('')
  const timer = useRef(null)

  const coop = view?.coop
  const teamId = coop?.team_id
  const meId = coop?.me?.id

  useEffect(() => {
    if (!teamId) {
      setTeam(null)
      return () => {}
    }
    let alive = true
    async function load() {
      try {
        const data = await api.getCoopTeam(teamId, meId)
        if (alive) setTeam(data)
      } catch (e) {
        if (alive) setErr(e.message)
      }
    }
    load()
    // 协作中低频同步成员列表/贡献（权威章节状态仍由动作响应与手动刷新驱动）
    timer.current = setInterval(load, 5000)
    return () => { alive = false; clearInterval(timer.current) }
  }, [teamId, meId, runId])

  if (!coop) return null
  const members = team?.members || coop.members
  const me = members.find((m) => m.id === meId) || coop.me
  const canBattle = me && (me.role === 'leader' || me.role === 'combat')
  const canSupply = me && (me.role === 'leader' || me.role === 'supply')

  return (
    <div className="panellist coop-panel">
      <h3>
        👥 协作远征
        <span className={`chip coop-status ${coop.expedition_status}`}>
          {coop.expedition_status === 'in_progress'
            ? `第 ${coop.chapter}/${coop.chapters_total} 章`
            : coop.expedition_status === 'won' ? '已通关' : '已终结'}
        </span>
      </h3>
      <div className="coop-me-perm">
        我的角色：<b>{ROLE_ICON[me?.role] || '•'} {ROLE_NAME[me?.role] || me?.role}</b>
        <span className="coop-perm-tags">
          {canBattle && <em className="perm battle">可战斗操作</em>}
          {canSupply && <em className="perm supply">可资源操作</em>}
        </span>
      </div>
      <ul className="coop-roster">
        {members.map((m) => (
          <li key={m.id} className={`coop-roster-row ${m.role} ${m.id === meId ? 'me' : ''}`}>
            <span className="coop-member-icon">{m.icon || ROLE_ICON[m.role]}</span>
            <span className="coop-member-name">
              {m.name}{m.id === meId && <em className="coop-me-tag">（我）</em>}
            </span>
            <span className="coop-member-role">{m.role_label || ROLE_NAME[m.role]}</span>
            {m.ledger && (
              <span className="coop-member-stat" title="战斗胜利 / 后勤操作 / 个人战利金">
                ⚔️{m.ledger.battle_wins ?? 0} · 🎒{m.ledger.resource_ops ?? 0}
                {(m.ledger.gold ?? 0) > 0 && ` · 🏅${m.ledger.gold}`}
              </span>
            )}
          </li>
        ))}
      </ul>
      <p className="coop-hint">
        入队码 <b className="coop-code-inline">{coop.code}</b> ·
        通关协作金 {coop.rewards.chapter_clear_bonus} 金入共享池随章节继承
      </p>
      {err && <div className="error">{err}</div>}
    </div>
  )
}
