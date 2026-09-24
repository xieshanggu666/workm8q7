import React, { useEffect, useRef } from 'react'
import { useStore } from '../store'
import { bus } from '../phaser/battleBus'
import { startPhaser } from '../phaser/BattleScene.js'
import ReplayMap from './ReplayMap.jsx'
import { growthNodesOf, growthTag } from '../growth'
import PotionBelt from './PotionBelt.jsx'

const TYPE_LABEL = {
  encounter: '遭遇', elite: '精英', rest: '休息', reward: '奖励',
  forge: '锻造', shop: '商店', event: '奇遇', boss: '首领', start: '营地',
}

// 整局回放的单帧舞台：严格只读，不调用 api.act。
// - 地图帧：高亮当前位置与已走路径
// - 战斗帧：Phaser 落到该帧权威快照；播放时逐行动画由父组件经 bus 推送
// - 锻造/奖励/商店帧：渲染该帧重建出的面板状态（金币、分支、货架/交易记录）
export default function ReplayStage({ view, showShop }) {
  const mountRef = useRef(null)
  const gameRef = useRef(null)
  const viewRef = useRef(view)
  viewRef.current = view
  const cardMeta = useStore((s) => s.cardMeta)

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
    // 跳转/单步：立即落到权威快照（不放动画）
    bus.emit('snapshot', view.battle || { player: null, enemy: null })
  }, [view])

  if (view.in_battle && view.battle) {
    const b = view.battle
    const hand = b.hand || []
    return (
      <div className="battle replay-battle">
        <div ref={mountRef} className="phaser" />
        {b.companion && (
          <div className={`battle-companion ${b.companion.alive === false || b.companion.hp <= 0 ? 'down' : ''}`}>
            <b>🛡️ {b.companion.name}</b>
            <span>{b.companion.hp}/{b.companion.max_hp}</span>
          </div>
        )}
        <div className="handbar replay-handbar">
          <div className="energy">能量 {b.energy} / {b.max_energy}</div>
          <div className="hand">
            {hand.length === 0 && <span className="hint">手牌为空</span>}
            {hand.map((item) => {
              const hc = typeof item === 'string'
                ? { uid: item, id: item, cost: cardMeta(item)?.cost ?? 0, growth: [] }
                : { uid: item.uid, id: item.id, cost: item.cost ?? cardMeta(item.id)?.cost ?? 0, growth: growthNodesOf(item) }
              const c = cardMeta(hc.id) || { name: hc.id, type: 'attack', desc: '' }
              return (
                <span key={hc.uid} className={`card ${c.type} replayed ${hc.growth.length ? 'forged' : ''}`} title={c.desc}>
                  <span className="ccost">{hc.cost}</span>
                  <span className="cname">{c.name}</span>
                  {hc.growth.length > 0 && (
                    <span className="handforges">
                      {hc.growth.map((n, i) => (
                        <i key={i} className={`ftag ${n}`}>{growthTag(n)}</i>
                      ))}
                    </span>
                  )}
                </span>
              )
            })}
          </div>
          <span className="replay-badge">回放 · 回合 {b.turn}{b.in_turn ? ' · 玩家回合' : ''}</span>
        </div>
      </div>
    )
  }

  const node = view.map?.nodes?.[view.position]
  return (
    <div className="replay-stage">
      <ReplayMap view={view} />
      <PotionBeltReadOnly view={view} />
      {view.expedition && (view.commissions || []).length > 0 && (
        <CommissionsSnapshot commissions={view.commissions} />
      )}
      {node?.type === 'forge' && <ForgeSnapshot view={view} />}
      {node?.type === 'reward' && <RewardSnapshot view={view} />}
      {view.encounter && <EncounterSnapshot view={view} />}
      {(view.encounter_flags || []).length > 0 && <EncounterFlagsSnapshot view={view} />}
      {showShop && view.shop && <ShopSnapshot view={view} />}
      {view.status === 'won' && <div className="replay-end won">🏆 本帧：通关</div>}
      {view.status === 'lost' && <div className="replay-end lost">💀 本帧：战败（回放不发放解锁）</div>}
      <div className="replay-route-meta">
        <span>生命 {view.health}/{view.max_health}</span>
        <span>金币 {view.gold}</span>
        <span>牌组 {view.deck.length}</span>
        <span>{node ? TYPE_LABEL[node.type] : view.position}</span>
      </div>
    </div>
  )
}

function ForgeSnapshot({ view }) {
  const cardMeta = useStore((s) => s.cardMeta)
  const grown = view.deck.filter((d) => growthNodesOf(d).length)
  return (
    <div className="overlay replay-overlay">
      <div className="forgecard panel replay-panel">
        <h2>🔨 锻造台 · 成长树（回放）</h2>
        <p className="forgedesc">
          锻造节点状态：{view.forge_claimed ? '已完成锻造（或离开）' : '尚未锻造'}。
          下列卡牌实例携带各自在本局累计解锁的成长节点与成本（只读）。
        </p>
        <div className="forgelist">
          {grown.length === 0 && <span className="shopempty">本帧尚无卡牌获得成长。</span>}
          {view.deck.map((inst) => {
            const nodes = growthNodesOf(inst)
            const c = cardMeta(inst.id) || { name: inst.id, desc: '', tier: '' }
            return (
              <span key={inst.uid} className={`forgeinst ${c.tier} ${nodes.length ? 'sel' : ''} readonly`}>
                <span className="cname">
                  {c.name}
                  {nodes.length > 0 && (
                    <em className="ftags">
                      {nodes.map((n, i) => (
                        <i key={i} className={`ftag ${n}`}>{growthTag(n)}</i>
                      ))}
                    </em>
                  )}
                  {inst.growth_spent > 0 && <em className="gspent">已投入 {inst.growth_spent}</em>}
                </span>
                <span className="cdesc">{c.desc}</span>
              </span>
            )
          })}
        </div>
      </div>
    </div>
  )
}

function RewardSnapshot({ view }) {
  return (
    <div className="overlay replay-overlay">
      <div className="rewardcard panel replay-panel">
        <h2>奖励（回放）</h2>
        {view.reward_claimed ? (
          <p className="shopdesc">奖励已在之前/本帧领取。</p>
        ) : (
          <div className="rewardopts">
            {(view.reward_options || []).map((o, i) => (
              <span key={i} className="roption readonly">
                <span className="rkind">{o.name}</span>
                <span className="rdesc">{o.desc}</span>
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

function CommissionsSnapshot({ commissions }) {
  const STATUS = { active: '进行中', ready: '可领奖', claimed: '已完成', failed: '已失败' }
  const FAIL = { expired: '超期', battle_lost: '战败' }
  return (
    <div className="panellist commissions replay-commissions">
      <h3>📜 远征委托（回放）</h3>
      {commissions.map((q) => (
        <div key={q.id} className={`commission ${q.status}`}>
          <div className="comm-head">
            <b>{q.kind === 'battle' ? '⚔️ 讨伐委托' : '💰 贸易委托'}</b>
            <span className={`comm-status ${q.status}`}>
              {q.status === 'failed' ? FAIL[q.fail_reason] || '已失败' : STATUS[q.status]}
            </span>
          </div>
          <div className="comm-obj">{q.objective}</div>
          <div className="comm-meta">
            <span>进度 {q.progress}/{q.target}</span>
            <span>限第 {q.deadline_chapter} 章前</span>
          </div>
          <div className="comm-reward">奖励：{q.reward.text}</div>
        </div>
      ))}
    </div>
  )
}

function PotionBeltReadOnly({ view }) {
  const potions = view.potions || []
  if (potions.length === 0) return null
  return (
    <div className="panellist potion-belt replay-potions">
      <h3>🧪 药水背包（回放）<span className="potion-count">
        {potions.length}/{view.potion_capacity || 3}
      </span></h3>
      <div className="potion-slots">
        {potions.map((p) => (
          <span key={p.slot} className="potion-slot" title={p.desc}>
            <span className="potion-icon">{p.icon}</span>
            <span className="potion-name">{p.name}</span>
          </span>
        ))}
      </div>
    </div>
  )
}

function EncounterSnapshot({ view }) {
  const enc = view.encounter
  return (
    <div className="overlay replay-overlay">
      <div className="encountercard panel replay-panel">
        <div className="enc-head">
          <span className="enc-badge">✨ 跨章奇遇（回放）</span>
          {view.expedition && <span className="enc-chapter">第 {view.expedition.chapter} 章</span>}
        </div>
        <h2>{enc.title}</h2>
        <p className="enc-text">{enc.text}</p>
        <div className="enc-choices">
          {enc.choices.map((ch) => (
            <span key={ch.id} className="enc-choice readonly">
              <span className="enc-choice-label">{ch.label}</span>
              <span className="enc-choice-desc">{ch.desc}</span>
            </span>
          ))}
        </div>
      </div>
    </div>
  )
}

function EncounterFlagsSnapshot({ view }) {
  return (
    <div className="panellist encounter-flags replay-enc-flags">
      <h3>🔗 奇遇印记（回放）</h3>
      {(view.encounter_flags || []).map((f) => (
        <div key={f.flag} className={`enc-flag-row ${f.pending_opener ? 'pending' : ''}`}>
          <b>{f.title}</b>
          <span className="flag-desc">{f.desc}</span>
          <span className="flag-meta">
            始于第 {f.since_chapter} 章
            {f.opener_at ? ` · 预兆已于第 ${f.opener_at} 章兑现` : ' · 预兆待兑现'}
          </span>
        </div>
      ))}
    </div>
  )
}

function ShopSnapshot({ view }) {
  const shop = view.shop
  const cardMeta = useStore((s) => s.cardMeta)
  const nameOf = (kind, sku) => {
    if (kind === 'potion') return shop.potions.find((it) => it.sku === sku)?.name || sku
    return (kind === 'card' ? shop.cards : shop.relics).find((it) => it.sku === sku)?.name || sku
  }
  return (
    <div className="overlay replay-overlay">
      <div className="shopcard panel replay-panel">
        <h2>🛒 旅途商店（回放）</h2>
        <p className="shopgold">本帧金币 <b>{view.gold}</b>（只读）</p>

        <h3 className="shopsection">卡牌货架</h3>
        <div className="shoplist">
          {shop.cards.length === 0 && <span className="shopempty">无货架</span>}
          {shop.cards.map((it) => (
            <span key={it.sku} className={`shopitem ${it.tier} readonly ${it.sold ? 'sold' : ''}`}>
              <span className="siname">{it.name}<em className="sitype">{it.type} · {it.card_cost} 费</em></span>
              <span className="sidesc">{it.desc}</span>
              <span className="siprice">{it.sold ? '已售出' : `${it.price} 金币`}</span>
            </span>
          ))}
        </div>

        <h3 className="shopsection">药水货架</h3>
        <div className="shoplist">
          {(shop.potions || []).length === 0 && <span className="shopempty">无货架</span>}
          {(shop.potions || []).map((it) => (
            <span key={it.sku} className={`shopitem potion readonly ${it.sold ? 'sold' : ''}`}>
              <span className="siname">{it.icon} {it.name}<em className="sitype">战斗消耗品</em></span>
              <span className="sidesc">{it.desc}</span>
              <span className="siprice">{it.sold ? '已售出' : `${it.price} 金币`}</span>
            </span>
          ))}
        </div>

        <h3 className="shopsection">远征委托</h3>
        <div className="shoplist">
          {(shop.commissions || []).length === 0 && (
            <span className="shopempty">本帧无委托挂单</span>
          )}
          {shop.commissions?.map((o) => (
            <span key={o.sku} className="shopitem commission readonly">
              <span className="siname">📜 {o.kind_label}<em className="sitype">限第 {o.deadline_chapter} 章前</em></span>
              <span className="sidesc">{o.objective}｜奖励：{o.reward.text}</span>
            </span>
          ))}
        </div>

        <h3 className="shopsection">遗物货架</h3>
        <div className="shoplist">
          {shop.relics.map((it) => (
            <span key={it.sku} className={`shopitem relic readonly ${it.sold ? 'sold' : ''}`}>
              <span className="siname">📿 {it.name}</span>
              <span className="sidesc">{it.desc}</span>
              <span className="siprice">{it.sold ? '已售出' : `${it.price} 金币`}</span>
            </span>
          ))}
        </div>

        {shop.tx.length > 0 && (
          <div className="shoptx">
            <h3 className="shopsection">已重建的交易记录（{shop.tx.length}）</h3>
            <ul>
              {shop.tx.map((t, i) => (
                <li key={i}>
                  {t.type === 'buy'
                    ? `购入${t.kind === 'card' ? '卡牌' : t.kind === 'potion' ? '药水' : '遗物'}「${nameOf(t.kind, t.sku)}」，花费 ${t.price}${t.discarded ? `（替换丢弃）` : ''}`
                    : `移除卡牌实例（${cardMeta(t.card)?.name || t.card}），花费 ${t.price}，牌组 ${t.deck_size} 张`}
                  ｜余额 {t.gold_left}
                </li>
              ))}
            </ul>
          </div>
        )}
        <p className="shopdesc">移除服务：本次价格 {shop.remove.cost}，已用 {shop.remove.used} 次。</p>
      </div>
    </div>
  )
}
