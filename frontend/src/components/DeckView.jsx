import React from 'react'
import { useStore, cardBadge } from '../store'
import { growthNodesOf, growthTag } from '../growth'

function GrowthTags({ nodes }) {
  if (!nodes || nodes.length === 0) return null
  return (
    <em className="ftags">
      {nodes.map((n, i) => (
        <i key={i} className={`ftag ${n}`} title={n}>{growthTag(n)}</i>
      ))}
    </em>
  )
}

export default function DeckView({ mode }) {
  const { view, cards, cardMeta } = useStore()
  const deck = view ? view.deck : null
  if (mode === 'extras') {
    // 主页展示全部卡牌
    return (
      <div className="panellist">
        <h3>卡牌图鉴（{cards.length}）</h3>
        <div className="decklist">
          {cards.map((c) => (
            <div key={c.id} className={`deckcard ${c.tier}`}>
              <span className="cname">{c.name}</span>
              <span className="cdesc">{c.desc}</span>
            </div>
          ))}
        </div>
      </div>
    )
  }
  if (!deck) return null

  // deck 项：新档为 {uid,id,growth,growth_nodes}，旧档为裸 id
  const groups = {}
  const order = []
  deck.forEach((item) => {
    const id = typeof item === 'string' ? item : item.id
    if (!groups[id]) {
      groups[id] = { id, count: 0, growths: [] }
      order.push(id)
    }
    groups[id].count += 1
    if (typeof item !== 'string') groups[id].growths.push(growthNodesOf(item))
  })

  return (
    <div className="panellist">
      <h3>牌组（{deck.length}）</h3>
      <div className="decklist">
        {order.map((id) => {
          const g = groups[id]
          const c = cardMeta(id) || { id, name: id, desc: '', tier: '' }
          // 同名卡的成长分布：各实例独立显示（如 ×4 中 锋 / 锋破裂）
          const marks = g.growths.map((ns, i) => <GrowthTags key={i} nodes={ns} />)
          return (
            <div key={id} className={`deckcard ${c.tier}`}>
              <span className="cname">
                {c.name} <em>×{g.count}</em>{' '}
                {marks.some((m) => m) && <span className="fmarks">{marks}</span>}
              </span>
              <span className="cdesc">{cardBadge(c)} · {c.desc}</span>
            </div>
          )
        })}
      </div>
    </div>
  )
}
