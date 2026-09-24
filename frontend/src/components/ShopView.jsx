import React, { useMemo, useState } from 'react'
import { api, handleActError } from '../api'
import { useStore } from '../store'
import { canDoSupply } from '../coopPerms'
import PotionBelt from './PotionBelt.jsx'

const TYPE_LABEL = { attack: '攻击', skill: '技能', power: '能力' }

// 旅途商店：购买卡牌/遗物，或付费移除指定卡牌实例。
// 服务端统一处理扣款/售罄/失败回退，本组件只发动作并应用返回视口；
// “启程”仅在本地收起面板（不发动作），点击地图上的当前节点可再次打开。
export default function ShopView({ view, onClose }) {
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [selected, setSelected] = useState(null) // 待移除的卡牌实例 uid
  const [replaceTarget, setReplaceTarget] = useState(null) // 背满买药：待替换丢弃的药水 sku
  const runId = useStore((s) => s.runId)
  const applyRun = useStore((s) => s.applyRun)
  const cardMeta = useStore((s) => s.cardMeta)

  const shop = view.shop
  const removeCost = shop?.remove?.cost ?? 0
  const canRemoveAfford = view.gold >= removeCost
  // 协作远征：买卖/移除都是资源动作（战斗位进店只读）
  const supplyAllowed = canDoSupply(view)

  const deck = useMemo(
    () => (view.deck || []).map((d) => ({ ...d, meta: cardMeta(d.id) })),
    [view.deck, cardMeta],
  )

  async function transact(body) {
    setBusy(true); setErr('')
    try {
      const res = await api.act(runId, body)
      applyRun(res.run)
      setSelected(null)
    } catch (e) {
      setErr(await handleActError(e, runId, applyRun))
    } finally {
      setBusy(false)
    }
  }

  function buy(kind, sku) {
    return transact({ action: 'shop_buy', kind, sku })
  }

  const beltFull = (view.potions || []).length >= (view.potion_capacity || 3)

  async function buyPotion(sku) {
    // 背满：先进入替换选择模式，由玩家指定被替换丢弃的格位
    if (beltFull) {
      setReplaceTarget(sku)
      return
    }
    await transact({ action: 'shop_buy', kind: 'potion', sku })
  }

  async function confirmReplacePotion(slot) {
    if (replaceTarget == null) return
    await transact({ action: 'shop_buy', kind: 'potion', sku: replaceTarget, replace: slot })
    setReplaceTarget(null)
  }

  function acceptCommission(sku) {
    return transact({ action: 'commission_accept', sku })
  }

  // 交易记录里把 sku 解析成中文名称
  const itemName = (kind, sku) => {
    if (kind === 'companion') return shop.companions?.find((it) => it.sku === sku)?.name || sku
    const bucket = kind === 'card' ? shop.cards : shop.relics
    return bucket.find((it) => it.sku === sku)?.name || sku
  }

  return (
    <div className="overlay">
      <div className="shopcard panel">
        <h2>🛒 旅途商店</h2>
        <p className="shopgold">金币 <b>{view.gold}</b></p>
        {!supplyAllowed && (
          <p className="comm-hint coop-deny">🔒 你是战斗位：商店交易由 🎒 资源位/队长操作，你可以浏览库存。</p>
        )}

        <h3 className="shopsection">卡牌</h3>
        <div className="shoplist">
          {shop.cards.length === 0 && <span className="shopempty">卡牌已售罄。</span>}
          {shop.cards.map((it) => (
            <button
              key={it.sku}
              className={`shopitem ${it.tier} ${it.sold ? 'sold' : ''}`}
              onClick={() => !it.sold && buy('card', it.sku)}
              disabled={it.sold || busy || !supplyAllowed || view.gold < it.price}
              title={it.desc}
            >
              <span className="siname">
                {it.name}
                <em className="sitype">{TYPE_LABEL[it.type]} · {it.card_cost} 费</em>
              </span>
              <span className="sidesc">{it.desc}</span>
              <span className="siprice">{it.sold ? '已售出' : `${it.price} 金币`}</span>
            </button>
          ))}
        </div>

        <h3 className="shopsection">遗物</h3>
        <div className="shoplist">
          {shop.relics.length === 0 && <span className="shopempty">遗物已售罄。</span>}
          {shop.relics.map((it) => (
            <button
              key={it.sku}
              className={`shopitem relic ${it.sold ? 'sold' : ''}`}
              onClick={() => !it.sold && buy('relic', it.sku)}
              disabled={it.sold || busy || !supplyAllowed || view.gold < it.price}
              title={it.desc}
            >
              <span className="siname">📿 {it.name}</span>
              <span className="sidesc">{it.desc}</span>
              <span className="siprice">{it.sold ? '已售出' : `${it.price} 金币`}</span>
            </button>
          ))}
        </div>

        <h3 className="shopsection">伙伴</h3>
        {(shop.companions || []).length === 0 && (
          <p className="shopdesc">你已经有同行的伙伴了；一场旅途只能招募一名伙伴。</p>
        )}
        <div className="shoplist">
          {(shop.companions || []).map((it) => (
            <button
              key={it.sku}
              className={`shopitem companion ${it.sold ? 'sold' : ''}`}
              onClick={() => !it.sold && buy('companion', it.sku)}
              disabled={it.sold || busy || !supplyAllowed || view.gold < it.price}
              title={it.desc}
            >
              <span className="siname">
                🛡️ {it.name}
                <em className="sitype">攻击 {it.attack} · 援护 {it.guard} · 生命 {it.max_health}</em>
              </span>
              <span className="sidesc">{it.desc}</span>
              <span className="siprice">{it.sold ? '已招募' : `${it.price} 金币`}</span>
            </button>
          ))}
        </div>

        <h3 className="shopsection">药水（可跨章携带）</h3>
        <p className="shopdesc">
          战斗中可在自己的回合使用，不消耗能量；背包最多 {view.potion_capacity} 瓶，
          背满时购买需选择一瓶替换丢弃。
        </p>
        {replaceTarget != null && (
          <PotionBelt replaceMode
                      onPickReplace={confirmReplacePotion}
                      onCancelReplace={() => setReplaceTarget(null)} />
        )}
        <div className="shoplist">
          {(shop.potions || []).length === 0 && <span className="shopempty">药水已售罄。</span>}
          {(shop.potions || []).map((it) => (
            <button
              key={it.sku}
              className={`shopitem potion ${it.sold ? 'sold' : ''}`}
              onClick={() => !it.sold && buyPotion(it.sku)}
              disabled={it.sold || busy || !supplyAllowed || view.gold < it.price}
              title={it.desc}
            >
              <span className="siname">
                {it.icon} {it.name}
                <em className="sitype">战斗消耗品</em>
              </span>
              <span className="sidesc">{it.desc}</span>
              <span className="siprice">{it.sold ? '已售出' : `${it.price} 金币`}</span>
            </button>
          ))}
        </div>

        <h3 className="shopsection">远征委托</h3>
        {(!view.expedition || (shop.commissions || []).length === 0) && (
          <p className="shopdesc">
            {view.expedition
              ? '本商店暂无可接委托（持有委托过多时不会刷新新委托）。'
              : '委托板只在远征章节的商店开放；创建「多章远征」即可接取限章委托。'}
          </p>
        )}
        <div className="shoplist">
          {(shop.commissions || []).map((o) => (
            <span key={o.sku} className="shopitem commission" title={`${o.objective}，限第 ${o.deadline_chapter} 章前完成`}>
              <span className="siname">📜 {o.kind_label}<em className="sitype">限第 {o.deadline_chapter} 章前</em></span>
              <span className="sidesc">{o.objective}｜奖励：{o.reward.text}</span>
              <span className="sibtn">
                <button
                  className="primary"
                  onClick={() => acceptCommission(o.sku)}
                  disabled={busy || !supplyAllowed}
                >
                  {supplyAllowed ? '接取' : '🔒 资源位'}
                </button>
              </span>
            </span>
          ))}
        </div>

        <h3 className="shopsection">移除服务</h3>
        <p className="shopdesc">
          付费永久移除一张指定卡牌实例（同名卡的其余副本不受影响）。
          本次价格 <b>{removeCost}</b> 金币，每移除一次价格上涨
          {' '}{shop.remove.next_cost - removeCost} 金币。
        </p>
        <div className="forgelist">
          {deck.map((inst) => {
            const c = inst.meta || { name: inst.id, desc: '', tier: '' }
            const isSel = selected === inst.uid
            return (
              <button
                key={inst.uid}
                className={`forgeinst ${c.tier} ${isSel ? 'sel' : ''}`}
                onClick={() => supplyAllowed && setSelected(inst.uid)}
                disabled={busy || !supplyAllowed}
                title={c.desc}
              >
                <span className="cname">{c.name}</span>
                <span className="cdesc">{c.desc}</span>
              </button>
            )
          })}
        </div>
        <button
          className="primary"
          onClick={() => transact({ action: 'shop_remove', card: selected })}
          disabled={!selected || !canRemoveAfford || busy || !supplyAllowed}
        >
          移除选中卡牌（{removeCost} 金币）
        </button>
        {!canRemoveAfford && (
          <div className="error">金币不足（需要 {removeCost}，当前 {view.gold}）。</div>
        )}

        {shop.tx.length > 0 && (
          <div className="shoptx">
            <h3 className="shopsection">交易记录</h3>
            <ul>
              {shop.tx.map((t, i) => (
                <li key={i}>
                  {t.type === 'buy'
                    ? `购入${t.kind === 'card' ? '卡牌' : t.kind === 'companion' ? '伙伴' : '遗物'}「${itemName(t.kind, t.sku)}」，花费 ${t.price}`
                    : `移除卡牌实例（${cardMeta(t.card)?.name || t.card}），花费 ${t.price}，牌组 ${t.deck_size} 张`}
                  ｜余额 {t.gold_left}
                </li>
              ))}
            </ul>
          </div>
        )}

        <div className="divider" />
        <button className="primary" onClick={onClose} disabled={busy}>启程 →</button>
        {err && <div className="error">{err}</div>}
      </div>
    </div>
  )
}
