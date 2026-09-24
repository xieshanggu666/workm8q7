import React, { useCallback, useEffect, useRef, useState } from 'react'
import { api, ForbiddenError, setCurrentMemberId } from '../api'

// 多人协作远征大厅（2.9.0）：
// - 队长建队（拿到 6 位入队码 + 自己的成员 id），为队员分配战斗位/资源位，开赛；
// - 队员凭入队码加入（拿到自己的成员 id，存 localStorage 便于刷新后续局）；
// - 大厅轮询队伍状态（角色调整/成员进出），开赛后切换到共享章节 run 视口。
// 成员身份（memberId）是本服务的“会话令牌”——没有账号体系，客户端自行保存。
const STORAGE_KEY = 'cardrun_coop_identity_v1'
const POLL_MS = 2500

function loadIdentity() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    return raw ? JSON.parse(raw) : null
  } catch {
    return null
  }
}

function saveIdentity(teamId, memberId, token) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ teamId, memberId, token }))
  } catch { /* 隐私模式等场景忽略存储失败 */ }
}

function clearIdentity() {
  try {
    localStorage.removeItem(STORAGE_KEY)
  } catch { /* ignore */ }
}

const ROLE_BTN = {
  combat: { label: '⚔️ 战斗位', title: '打牌 / 结束回合 / 战斗中用药' },
  supply: { label: '🎒 资源位', title: '路线 / 锻造 / 商店 / 药水 / 伙伴 / 委托 / 奇遇' },
}

export default function CoopLobby({ onEnterRun }) {
  const [mode, setMode] = useState('home') // home / captain / member / lobby
  const [team, setTeam] = useState(null)
  const [memberId, setMemberId] = useState(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const pollRef = useRef(null)

  // 建队表单
  const [captainName, setCaptainName] = useState('')
  const [teamName, setTeamName] = useState('')
  const [chapters, setChapters] = useState('3')
  // 入队表单
  const [joinCode, setJoinCode] = useState('')
  const [joinName, setJoinName] = useState('')
  // 续队（粘贴 team_id）
  const [resumeTeam, setResumeTeam] = useState('')

  const applyTeam = useCallback((data, mid) => {
    setTeam(data)
    const me = mid || data.me?.id
    if (me) {
      setMemberId(me)
      setCurrentMemberId(me)
    }
    if (data.status === 'started' && data.expedition_id && onEnterRun) {
      onEnterRun(data)
    }
  }, [onEnterRun])

  const refreshTeam = useCallback(async (teamId, mid) => {
    const data = await api.getCoopTeam(teamId, mid)
    applyTeam(data, mid)
    return data
  }, [applyTeam])

  // 大厅轮询：forming 阶段持续刷新成员/角色，开赛后立即进入远征
  useEffect(() => {
    if (mode !== 'lobby' || !team || team.status !== 'forming') return () => {}
    let alive = true
    pollRef.current = setInterval(async () => {
      try {
        const data = await api.getCoopTeam(team.id, memberId)
        if (!alive) return
        applyTeam(data, memberId)
      } catch (e) {
        if (!alive) return
        setErr(e.message)
      }
    }, POLL_MS)
    return () => { alive = false; clearInterval(pollRef.current) }
  }, [mode, team?.id, team?.status, memberId, applyTeam])

  async function createTeam() {
    setBusy(true); setErr('')
    try {
      const data = await api.createCoopTeam({
        name: teamName,
        captainName: captainName || '队长',
        chapters: chapters ? Number(chapters) : 3,
      })
      const mid = data.me.id
      saveIdentity(data.id, mid, data.me.token)
      setMode('lobby')
      applyTeam(data, mid)
    } catch (e) {
      setErr(e.message)
    } finally {
      setBusy(false)
    }
  }

  async function joinTeam() {
    setBusy(true); setErr('')
    try {
      const data = await api.joinCoopTeam(joinCode.trim().toUpperCase(), joinName || '队员')
      if (data.duplicate) setErr('该入队请求已提交过（幂等返回）')
      const mid = data.me.id
      saveIdentity(data.id, mid, data.me.token)
      setMode('lobby')
      applyTeam(data, mid)
    } catch (e) {
      setErr(e.message)
    } finally {
      setBusy(false)
    }
  }

  async function openExistingTeam() {
    const id = (resumeTeam || '').trim()
    if (!id) return
    setBusy(true); setErr('')
    try {
      const ident = loadIdentity()
      const mid = ident && ident.teamId === id ? ident.memberId : null
      if (mid) {
        setMemberId(mid)
        setCurrentMemberId(mid)
      }
      const data = await api.getCoopTeam(id, mid)
      setMode('lobby')
      applyTeam(data, mid)
    } catch (e) {
      setErr(e.message)
    } finally {
      setBusy(false)
    }
  }

  async function restoreIdentity() {
    const ident = loadIdentity()
    if (!ident) return false
    setBusy(true); setErr('')
    try {
      setMemberId(ident.memberId)
      setCurrentMemberId(ident.memberId)
      const data = await api.getCoopTeam(ident.teamId, ident.memberId)
      setMode('lobby')
      applyTeam(data, ident.memberId)
      return true
    } catch (e) {
      setErr(e.message)
      clearIdentity()
      return false
    } finally {
      setBusy(false)
    }
  }

  async function assign(targetId, role) {
    setBusy(true); setErr('')
    try {
      const data = await api.assignRole(team.id, targetId, role)
      applyTeam(data, memberId)
    } catch (e) {
      setErr(e instanceof ForbiddenError ? `权限不足：${e.message}` : e.message)
    } finally {
      setBusy(false)
    }
  }

  async function startRun() {
    setBusy(true); setErr('')
    try {
      const data = await api.startCoopExpedition(team.id)
      if (data.duplicate) {
        // 双击/超时：当作成功，用首次响应进入
      }
      setCurrentMemberId(memberId)
      applyTeam(data.team, memberId)
      if (onEnterRun) {
        onEnterRun({
          id: data.team.id,
          status: 'started',
          expedition_id: data.expedition.id,
        }, data.run)
      }
    } catch (e) {
      setErr(e.message)
    } finally {
      setBusy(false)
    }
  }

  async function leave() {
    setBusy(true); setErr('')
    try {
      await api.leaveCoopTeam(team.id)
    } catch (e) {
      setErr(e.message)
    } finally {
      setBusy(false)
      clearIdentity(); setCurrentMemberId(null)
      setTeam(null); setMemberId(null); setMode('home')
    }
  }

  async function disband() {
    if (!window.confirm('确认解散队伍？所有未开赛的成员将被移出。')) return
    setBusy(true); setErr('')
    try {
      await api.disbandCoopTeam(team.id)
    } catch (e) {
      setErr(e.message)
    } finally {
      setBusy(false)
      clearIdentity(); setCurrentMemberId(null)
      setTeam(null); setMemberId(null); setMode('home')
    }
  }

  function continueExpedition() {
    if (team?.status === 'started' && onEnterRun) {
      onEnterRun(team)
    }
  }

  if (mode === 'home') {
    return (
      <div className="coop-lobby panel">
        <h2>👥 多人协作远征</h2>
        <p className="coop-intro">
          队长组建队伍，成员分别承担 <b>⚔️ 战斗位</b>（打牌/回合/战斗药水）与
          <b> 🎒 资源位</b>（路线/锻造/商店/药水整理/伙伴/委托/奇遇），
          共享章节状态、同步结算；通关协作金全队共享，整程可回放每一步的操作者。
        </p>
        <div className="coop-home-actions">
          <button className="primary" onClick={() => setMode('captain')}>👑 我是队长 · 组建队伍</button>
          <button className="primary" onClick={() => setMode('member')}>🎒 我有入队码 · 加入队伍</button>
          <button onClick={restoreIdentity} disabled={busy}>🔑 恢复我的队员身份</button>
        </div>
        <div className="fieldrow coop-resume">
          <span>队伍 ID</span>
          <input value={resumeTeam} onChange={(e) => setResumeTeam(e.target.value)}
                 placeholder="粘贴 team_id 查看/续队" />
          <button onClick={openExistingTeam} disabled={busy || !resumeTeam}>进入队伍</button>
        </div>
        {err && <div className="error">{err}</div>}
      </div>
    )
  }

  if (mode === 'captain') {
    return (
      <div className="coop-lobby panel">
        <h2>👑 组建远征队</h2>
        <div className="fieldrow"><span>你的称呼</span>
          <input value={captainName} onChange={(e) => setCaptainName(e.target.value)}
                 placeholder="队长" maxLength={12} />
        </div>
        <div className="fieldrow"><span>队伍名</span>
          <input value={teamName} onChange={(e) => setTeamName(e.target.value)}
                 placeholder="缺省用你的称呼" maxLength={24} />
        </div>
        <div className="fieldrow"><span>章节数</span>
          <input value={chapters} onChange={(e) => setChapters(e.target.value)} placeholder="3" />
        </div>
        <div className="coop-actions">
          <button className="primary" onClick={createTeam} disabled={busy}>
            {busy ? '创建中…' : '生成入队码并建队'}
          </button>
          <button onClick={() => { setMode('home'); setErr('') }}>返回</button>
        </div>
        {err && <div className="error">{err}</div>}
      </div>
    )
  }

  if (mode === 'member') {
    return (
      <div className="coop-lobby panel">
        <h2>🎒 加入远征队</h2>
        <div className="fieldrow"><span>入队码</span>
          <input value={joinCode} onChange={(e) => setJoinCode(e.target.value.toUpperCase())}
                 placeholder="6 位码" maxLength={6} className="coop-code-input" />
        </div>
        <div className="fieldrow"><span>你的称呼</span>
          <input value={joinName} onChange={(e) => setJoinName(e.target.value)}
                 placeholder="队员" maxLength={12} />
        </div>
        <div className="coop-actions">
          <button className="primary" onClick={joinTeam} disabled={busy || joinCode.length < 4}>
            {busy ? '加入中…' : '加入队伍'}
          </button>
          <button onClick={() => { setMode('home'); setErr('') }}>返回</button>
        </div>
        {err && <div className="error">{err}</div>}
      </div>
    )
  }

  // lobby
  if (!team) return null
  const me = team.members.find((m) => m.id === memberId) || team.me
  const isLeader = team.leader_id === memberId
  const canStart = team.members.length >= team.min_start

  return (
    <div className="coop-lobby panel">
      <h2>👥 {team.name || '远征队'}</h2>
      <div className="coop-statusline">
        <span className={`chip coop-status ${team.status}`}>
          {team.status === 'forming' ? '组队中' : team.status === 'started' ? '远征进行中' : '已解散'}
        </span>
        <span>{team.members.length}/{team.max_members} 人</span>
        {team.chapters && <span>🚩 {team.chapters} 章</span>}
      </div>

      <div className="coop-code-box">
        <span className="coop-code-label">入队码（发给队友）</span>
        <span className="coop-code">{team.code}</span>
        <button className="mini" onClick={() => navigator.clipboard?.writeText(team.code)}>复制</button>
        <span className="sub">队伍 ID：{team.id}</span>
      </div>

      <ul className="coop-members">
        {team.members.map((m) => (
          <li key={m.id} className={`coop-member ${m.role} ${m.id === memberId ? 'me' : ''}`}>
            <span className="coop-member-icon">{m.icon}</span>
            <span className="coop-member-name">
              {m.name}{m.id === memberId && <em className="coop-me-tag">（我）</em>}
              {m.is_leader && <em className="coop-leader-tag">队长</em>}
            </span>
            <span className="coop-member-role">{m.role_label}</span>
            {m.ledger && (m.ledger.battle_wins > 0 || m.ledger.resource_ops > 0) && (
              <span className="coop-member-stat">
                ⚔️{m.ledger.battle_wins} · 🎒{m.ledger.resource_ops}
                {m.ledger.gold > 0 && ` · 🏅${m.ledger.gold}`}
              </span>
            )}
            {isLeader && team.status === 'forming' && !m.is_leader && (
              <span className="coop-role-btns">
                {Object.entries(ROLE_BTN).map(([role, meta]) => (
                  <button key={role} className={`mini ${m.role === role ? 'on' : ''}`}
                          title={meta.title} disabled={busy || m.role === role}
                          onClick={() => assign(m.id, role)}>
                    {meta.label}
                  </button>
                ))}
              </span>
            )}
          </li>
        ))}
      </ul>

      {isLeader && (
        <p className="coop-hint">
          队长两个领域都能操作；开赛前请为队员分配角色，战斗位与资源位至少各一人更顺手。
        </p>
      )}

      {team.status === 'forming' ? (
        <div className="coop-actions">
          {isLeader && (
            <button className="primary" onClick={startRun} disabled={busy || !canStart}>
              {canStart ? '🚩 开赛（创建协作远征）' : `至少 ${team.min_start} 人才能开赛`}
            </button>
          )}
          {isLeader
            ? <button onClick={disband} disabled={busy}>解散队伍</button>
            : <button onClick={leave} disabled={busy}>退出队伍</button>}
          <button onClick={() => { setMode('home'); setErr('') }}>返回</button>
        </div>
      ) : (
        <div className="coop-actions">
          <button className="primary" onClick={continueExpedition}>▶ 进入协作远征</button>
        </div>
      )}
      {err && <div className="error">{err}</div>}
    </div>
  )
}
