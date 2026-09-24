import React, { useMemo, useState } from 'react'
import { api, handleActError } from '../api'
import { useStore } from '../store'
import { growthNodesOf, growthTag, growthName } from '../growth'
import { canDoSupply } from '../coopPerms'

// 成长树锻造：选一张卡牌实例，再选一个满足前置、未被互斥的成长节点解锁。
// 同名卡各自独立成长；每个锻造节点仅可锻造一次，成本按节点层（25/40/60）。
const TIER_LABEL = { 1: '入门', 2: '进阶', 3: '终阶' }

export default function ForgeView({ view }) {
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [selected, setSelected] = useState(null) // 卡牌实例 uid
  const runId = useStore((s) => s.runId)
  const applyRun = useStore((s) => s.applyRun)
  const cardMeta = useStore((s) => s.cardMeta)

  const tree = view.growth_tree || []
  const tierCost = view.growth_tier_cost || { 1: 25, 2: 40, 3: 60 }
  const treeById = useMemo(() => Object.fromEntries(tree.map((n) => [n.id, n])), [tree])
  // 协作远征：锻造是资源动作
  const supplyAllowed = canDoSupply(view)

  // 同名卡按实例列出，各自携带成长节点
  const instances = useMemo(
    () => (view.deck || []).map((d) => ({ ...d, meta: cardMeta(d.id), nodes: growthNodesOf(d) })),
    [view.deck, cardMeta],
  )
  const selectedInst = instances.find((i) => i.uid === selected) || null

  // 每个树节点对当前选中实例的状态
  const nodeState = (n) => {
    if (!selectedInst) return { kind: 'nocard', enabled: false }
    const owned = new Set(selectedInst.nodes)
    if (owned.has(n.id)) return { kind: 'owned', enabled: false }
    if (n.requires.some((r) => !owned.has(r))) return { kind: 'locked', enabled: false }
    if (n.mutex_with.some((m) => owned.has(m))) return { kind: 'mutex', enabled: false }
    return { kind: 'available', enabled: view.gold >= n.cost }
  }

  async function forge(nodeId) {
    if (!selected || busy || !supplyAllowed) return
    setBusy(true); setErr('')
    try {
      const res = await api.act(runId, { action: 'forge', card: selected, growth_node: nodeId })
      applyRun(res.run)
      // 该锻造节点已消耗：服务端会关闭面板，本地保留选中即可
    } catch (e) {
      setErr(await handleActError(e, runId, applyRun))
    } finally {
      setBusy(false)
    }
  }

  const byTier = (tier) => tree.filter((n) => n.tier === tier)

  return (
    <div className="overlay">
      <div className="forgecard panel">
        <h2>🔨 锻造台 · 成长树</h2>
        <p className="forgedesc">
          选择一张卡牌，再点亮一个成长节点。节点需要前置、同层分支互斥；
          同名卡各自独立成长。每处锻造台只能使用一次。
        </p>
        {!supplyAllowed && (
          <p className="comm-hint coop-deny">🔒 你是战斗位：锻造由 🎒 资源位/队长操作，你可以浏览成长树。</p>
        )}

        <div className="forgelist">
          {instances.map((inst) => {
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
                <span className="cname">
                  {c.name}
                  {inst.nodes.length > 0 && (
                    <em className="ftags">
                      {inst.nodes.map((n, i) => (
                        <i key={i} className={`ftag node-t${treeById[n]?.tier || 1}`}>{growthTag(n)}</i>
                      ))}
                    </em>
                  )}
                  {inst.growth_spent > 0 && <em className="gspent">已投入 {inst.growth_spent}</em>}
                </span>
                <span className="cdesc">{c.desc}</span>
                {Array.isArray(inst.growth_available) && inst.growth_available.length > 0 && (
                  <span className="gavail">可解锁 {inst.growth_available.length} 项</span>
                )}
              </button>
            )
          })}
        </div>

        {selectedInst && (
          <div className="growthtree">
            {[1, 2, 3].map((tier) => (
              <div key={tier} className="growthtier">
                <h4>{TIER_LABEL[tier]} <em>{tierCost[tier]} 金</em></h4>
                <div className="growthnodes">
                  {byTier(tier).map((n) => {
                    const st = nodeState(n)
                    return (
                      <button
                        key={n.id}
                        className={`gnode ${st.kind} node-t${tier}`}
                        onClick={() => st.kind === 'available' && forge(n.id)}
                        disabled={!st.enabled || busy || !supplyAllowed}
                        title={n.desc}
                      >
                        <span className="ftag big">{growthTag(n.id)}</span>
                        <span className="bname">{n.name}</span>
                        <span className="gcost">{n.cost} 金</span>
                        <span className="gstate">
                          {st.kind === 'owned' && '已习得'}
                          {st.kind === 'locked' && '前置未达成'}
                          {st.kind === 'mutex' && '分支互斥'}
                          {st.kind === 'available' && (view.gold >= n.cost ? '可解锁' : '金币不足')}
                        </span>
                      </button>
                    )
                  })}
                </div>
              </div>
            ))}
          </div>
        )}
        {!selectedInst && <div className="hint">先选择一张要锻造的卡牌。</div>}
        {err && <div className="error">{err}</div>}
      </div>
    </div>
  )
}
