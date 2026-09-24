import React, { useEffect, useRef, useState } from 'react'
import { api, handleActError } from '../api'
import { useStore } from '../store'
import { bus } from '../phaser/battleBus'
import { startPhaser, STATUS_ZH } from '../phaser/BattleScene.js'
import { growthNodesOf, growthTag } from '../growth'
import PotionBelt from './PotionBelt.jsx'
import { canDoBattle, permHint } from '../coopPerms'

export default function BattleView({ view }) {
  const mountRef = useRef(null)
  const gameRef = useRef(null)
  const viewRef = useRef(view)
  viewRef.current = view
  const [busy, setBusy] = useState(false)
  const [log, setLog] = useState([])
  const runId = useStore((s) => s.runId)
  const applyRun = useStore((s) => s.applyRun)
  const cardMeta = useStore((s) => s.cardMeta)
  const [err, setErr] = useState('')

  useEffect(() => {
    gameRef.current = startPhaser(mountRef.current)
    const off = bus.on('phaser_ready', () => {
      const v = viewRef.current
      bus.emit('snapshot', (v && v.battle) || { player: null, enemy: null })
    })
    return () => {
      bus.clear()
      off()
      gameRef.current?.destroy(true)
      gameRef.current = null
    }
  }, [])

  useEffect(() => {
    // 续局/刷新恢复：空闲时场景立即落到权威快照；播放中则由场景暂存、播完校正
    bus.emit('snapshot', view.battle || { player: null, enemy: null })
  }, [view.battle, view])

  // 把服务端结算日志交给 Phaser 按顺序播放；resolve 时表示整条连锁已播完
  function playLog(entries) {
    return new Promise((resolve) => {
      const list = (entries || []).filter(Boolean)
      if (!list.length) return resolve()
      let settled = false
      const finish = () => {
        if (settled) return
        settled = true
        off()
        clearTimeout(timer)
        resolve()
      }
      const off = bus.on('queue_done', finish)
      // 兜底：动画异常也不能让操作永久锁死
      const timer = setTimeout(finish, 15000)
      bus.emit('queue', list)
    })
  }

  async function doAct(action, extra = {}) {
    if (busy) return
    setBusy(true); setErr('')
    try {
      const res = await api.act(runId, { action, ...extra })
      const entries = res.log || []
      setLog(entries)
      // 动画期间操作锁定（busy），播完再应用权威状态：
      // 同步血量/护盾/手牌；战斗结束则衔接战后领奖或结算界面
      await playLog(entries)
      applyRun(res.run)
    } catch (e) {
      setErr(await handleActError(e, runId, applyRun))
    } finally {
      setBusy(false)
    }
  }

  async function usePotion(slot) {
    if (busy) return
    setBusy(true); setErr('')
    try {
      const res = await api.act(runId, { action: 'use_potion', slot })
      const entries = res.log || []
      setLog(entries)
      // 与打牌/结束回合同一套播放：消耗与战斗效果原子返回，按顺序播完再校正
      await playLog(entries)
      applyRun(res.run)
    } catch (e) {
      setErr(await handleActError(e, runId, applyRun))
    } finally {
      setBusy(false)
    }
  }

  const b = view.battle
  const hand = b?.hand || []
  const energy = b?.energy ?? view.energy
  const inTurn = b?.in_turn

  // 手牌项兼容旧档裸 id；新档为 {uid,id,cost,growth/growth_nodes}（费用已含成长换算）
  const handCard = (item) => {
    if (typeof item === 'string') {
      return { uid: item, id: item, cost: cardMeta(item)?.cost ?? 0, growth: [] }
    }
    return {
      uid: item.uid, id: item.id,
      cost: item.cost ?? cardMeta(item.id)?.cost ?? 0,
      growth: growthNodesOf(item),
    }
  }

  function canPlay(hc) {
    return inTurn && hc.cost <= energy && busy === false && battleAllowed
  }

  const battleAllowed = canDoBattle(view)
  const battleDeny = permHint(view, 'battle')

  return (
    <div className="battle">
      <div ref={mountRef} className="phaser" />
      {!battleAllowed && (
        <div className="battle-coop-deny">🔒 {battleDeny}（观战中，等待战斗位/队长操作）</div>
      )}
      {b?.companion && (
        <div className={`battle-companion ${b.companion.alive === false || b.companion.hp <= 0 ? 'down' : ''}`}>
          <b>🛡️ {b.companion.name}</b>
          <span>{b.companion.hp}/{b.companion.max_hp}</span>
        </div>
      )}
      <PotionBelt inBattle busy={busy} onUse={usePotion} />
      <div className="handbar">
        <div className="energy">能量 {energy} / {b?.max_energy ?? view.energy}</div>
        <div className="hand">
          {hand.length === 0 && <span className="hint">手牌为空</span>}
          {hand.map((item) => {
            const hc = handCard(item)
            const c = cardMeta(hc.id) || { id: hc.id, name: hc.id, type: 'attack', desc: '' }
            return (
              <button
                key={hc.uid}
                className={`card ${c.type} ${canPlay(hc) ? 'playable' : ''} ${hc.growth.length ? 'forged' : ''}`}
                onClick={() => canPlay(hc) && doAct('play', { card: hc.uid })}
                disabled={!canPlay(hc)}
                title={c.desc}
              >
                <span className="ccost">{hc.cost}</span>
                <span className="cname">{c.name}</span>
                {hc.growth.length > 0 && (
                  <span className="handforges">
                    {hc.growth.map((n, i) => (
                      <i key={i} className={`ftag ${n}`}>{growthTag(n)}</i>
                    ))}
                  </span>
                )}
              </button>
            )
          })}
        </div>
        {busy && <span className="hint settling">结算中…</span>}
        <button className="primary" onClick={() => doAct('end_turn')}
                disabled={!inTurn || busy || !battleAllowed}
                title={!battleAllowed ? battleDeny : undefined}>
          结束回合
        </button>
      </div>
      {log.length > 0 && (
        <div className="eventlog">
          {log.map((ev, i) => {
            const txt = fmtEvent(ev)
            if (!txt) return null
            const cls = ev && ev.result ? 'ev result' : 'ev'
            return <span key={i} className={cls}>{txt}</span>
          })}
        </div>
      )}
      {err && <div className="error">{err}</div>}
    </div>
  )
}

function fmtEvent(ev) {
  if (!ev || typeof ev !== 'object') return ''
  if (ev.potion) return `🧪 使用${ev.potion.icon || ''}「${ev.potion.name}」`
  if (ev.result) {
    if (ev.result === 'lost') return '💀 战败…'
    if (ev.result === 'run_won') return '🏆 通关！'
    return '🎉 胜利！'
  }
  if (ev.companion_turn) return `🛡️ ${ev.companion_turn.name} 随行攻击`
  if (ev.snapshot) return ''
  const tgt = ev.target === 'player' ? '你' : ev.target === 'companion' ? '伙伴' : '敌'
  switch (ev.action) {
    case 'enemy_turn':
      return `— 敌方回合${ev.extra?.name ? `：${ev.extra.name}` : ''} —`
    case 'damage':
    case 'echo_damage':
      return `${tgt} 受 ${ev.value} 伤害`
    case 'gain_block':
      return `${tgt} 获得 ${ev.value} 格挡`
    case 'heal':
      return `${tgt} 回复 ${ev.value}`
    case 'draw':
      return `抽 ${ev.value} 张牌`
    case 'gain_energy':
      return `能量 +${ev.value}`
    case 'apply_status':
    case 'set_status': {
      const s = STATUS_ZH[ev.extra?.status] || ev.extra?.status || '状态'
      return `${tgt} ${s} +${ev.value}`
    }
    case 'truncated':
      return '⚠ 连锁被强制终止（触发上限）'
    default:
      return ''
  }
}
