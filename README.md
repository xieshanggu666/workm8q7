# 卡牌闯关（Card Run）

基于 **React + Phaser 3 + FastAPI + SQLite** 的浏览器卡牌闯关游戏。

## 功能
- 选择路线（种子地图，3 条节点路径，最终挑战首领）
- **卡牌成长树**：锻造节点花金币为指定卡牌实例点亮成长节点——三层 DAG（入门 25 金 / 进阶 40 金 / 终阶 60 金），
  进阶与终阶节点需先点亮前置，且同一层分支互斥（如「锋锐」后只能在「破甲/壁垒」二选一，
  选「破甲」后其 T3 只余「裂甲/处决」）；同名卡为独立实例、各自保存解锁节点序列与每笔成本；
  成长即时换算生效卡牌（伤害/格挡/抽牌/能量/费用），贯通战斗结算、商店移除、跨章继承、续局与回放；
  旧版三分支强化记录（`forges`）按确定性默认链（sharpen→keen→rend 等）自动迁移为节点序列
- **旅途商店**：路线上的商人节点可购买卡牌/遗物/**药水**，或付费移除指定卡牌实例；库存由种子确定性生成，
  统一事务处理扣款/售罄（409）/失败整体回退（400 零副作用），交易结果带入后续战斗，续局与回放一致
- **跨章药水背包**：药水（治疗/壁垒/炽焰/能量/力量）从商店购买或战斗战利品获得，存入限容量背包（3 格，
  背满时购买/领取需指定一格替换丢弃，也可在非战斗时主动丢弃）；可在战斗中自己的回合择机使用（不耗能量）。
  消耗与战斗效果（伤害/格挡/治疗/状态/能量，含死亡打断与胜负）在**同一个动作里原子结算**——成功才随
  事务落库，双击/超时重试走 request_id 幂等只生效一瓶；药水随交接快照跨章携带，续局、领奖、战败解锁与
  整程回放全部逐位一致（规则 2.5.0，旧档首次载入补空背包）
- **伙伴模块**：旅途商店可花 50 金币招募一名见习卫士（一局/远征限一名），非战斗时可安排随行或休整；
  随行伙伴每轮开始攻击敌人，并在敌人伤害**穿透玩家格挡后**优先援护溢出部分（2.7.0 修正：
  格挡吸收在先、溢出才由伙伴抵挡），生命归零即负伤并自动暂停参战，休整伙伴
  可在休息节点治疗。招募扣款、战斗伤害、负伤、治疗均走普通动作与统一结算队列；伙伴随章节交接快照继承，
  续局、整章/整程回放逐帧重建，2.6.0 之前旧档首次载入自动补 `companion:null` 并按 legacy 校验过渡
- 构筑牌组、挑战精英/首领，奖励选择影响后续遭遇（遗物加伤、首领血量提升等）
- **跨章节奇遇链（规则 2.8.0）**：每张地图确定性安放一个「奇遇」节点（独立派生流
  选位、只替换战斗/奖励/锻造节点，商店与休息拓扑不变）；玩家在节点上面临剧情抉择
  并立即承担代价（生命/金币）或获得奖励（卡牌/药水/遗物/最大生命）。部分抉择埋下
  **印记 flag**——随远征交接快照继承，下一章开头兑现「预兆」（首场战斗开局修正：
  敌方+血/开局力量·格挡/易碎，或开章赐福回血，一次性消耗）；后续章节的奇遇节点再按
  flag 触发续写（旅人道谢赠遗物、复仇者伏击战），续写候选在场时必定触发。印记状态
  （flags/opened/battle_mods/completed/pending/ambush）是 run 状态的一部分，抉择是
  普通动作 `encounter_choice`（request_id 幂等 + expected_rev 状态守卫 + 单事务原子
  提交：代价先校验、失败零副作用，双击只结算一次），结算回写牌组/遗物/药水/生命；
  伏击战胜负与远征结算同动作提交（败即终结远征、推进讨伐委托）。续局保留待抉择/战斗，
  逐章与整程回放逐位校验（旧 2.8.0 前存档首次载入补空 enc_state，旧 create 校验点
  形状按缺字段候选兼容比对）
- 卡牌效果统一经 **结算队列** 处理，支持连锁触发、状态叠加、死亡打断
- **多人协作远征（规则 2.9.0）**：队长在大厅生成 6 位入队码组建队伍（2~4 人），
  开赛前为队员分配 **⚔️ 战斗位**（打牌/结束回合/战斗中用药）与 **🎒 资源位**
  （路线/领奖/锻造/商店/药水整理/伙伴/委托/奇遇），队长两个领域都可操作并负责
  开章与推进；所有成员操作的是**同一个章节 run**——同一条确定性动作日志、
  同一事务/`rev` 乐观锁/`request_id` 幂等串行化，共享章节状态并同步结算。
  权限边界在**任何状态变更之前**校验（越权 403、零副作用；前端按钮同步禁用仅为
  体验优化，绕过后端照样拒绝），每步动作日志记录 `actor`（操作者）。击败章节
  首领的**协作金 +15 入共享金币池**（纯推演、进校验点、随交接快照跨章），个人
  贡献（战斗胜利/后勤操作次数）与章节/终胜名义均分（终胜 +50/人）记 `coop_ledger`；
  战败整队同事务结算 lost（只结算一次，已落袋协作金不追回），终章通关结算 won。
  队伍时间线（form/join/role/start/chapter_clear/advance/settle）+ 个人战利 +
  逐章可交互回放（每帧标注操作者）由 `GET /api/coop/teams/{id}/replay` 提供，
  全程只读隔离。run 状态新增 `coop_team`（进入交接快照；旧档首次载入补 None，
  回放按 16 种字段形状候选兼容 2.9.0 之前的校验点），单人远征行为完全不变。
- **战斗演出**：Phaser 场景按服务端结算顺序逐条播放（待机/攻击/受击/死亡动画、护盾与状态实时刷新），
  播放期间操作锁定，播完再应用权威快照同步血量/护盾/手牌，战斗结束衔接领奖
- 服务端权威校验行动，防作弊；失败解锁新卡
- **多章远征**：创建跨章节远征（默认 3 章），击败章节首领后携带牌组、锻造成长、遗物与金币
  进入下一章（交接时休整回血 25%）；战败即结算远征并更新解锁，征服终章首领则远征通关；
  章节存档/奖励交接/整程回放统一管理，推进章节走 request_id 幂等 + 状态守卫，
  重复开章（400/409）与重复结算（状态机只流转一次）都被拦截
- **远征委托**：远征章节的旅途商店挂出限章委托（讨伐 N 场战斗 / 完成 N 笔商店交易），
  免费接取（最多同时持有 3 个，同款不重复挂出）；战斗胜利与商店交易自动推进目标，
  完成后在侧栏/章节结算页领取金币或卡牌（领奖走状态机 `ready→claimed` + request_id
  幂等，重复领取 409、绝不重复发奖，失败交易不推进）；超期未完成或战败则委托失败，
  已完成可领奖的不随超期失效，章节通关后、进入下一章前仍可领取（终章结算后关闭）。
  委托随交接快照跨章保留进度，续局与整程回放完整重建（含超期/战败/领奖终态）
- 中途可续局（`POST /api/runs` → 刷新 → 回到同一节点/战斗）；种子回放一致
- **可交互整局回放**：动作日志升级为可 ▶播放 / ⏸暂停 / ◀单步 / 拖拽跳转 / 0.5–4× 倍速 的时间轴，
  逐步重建路线、战斗（Phaser 按结算顺序重演）、锻造与交易状态；记录规则版本与逐步校验点（可检出日志损坏/规则漂移），
  兼容无版本号的旧日志（裸卡牌 id）；回放全程只读隔离——不写存档、战败不发解锁
- 服务端对无限连锁触发设上限防止死循环；领奖/锻造防重复（重复领取返回 409，不重复扣款）
- **远征整程回放**：`GET /api/expeditions/{id}/replay` 返回远征事件时间线
  （create/chapter_clear/advance/settle）+ 逐章完整可交互回放；章节 run 的初始状态由
  create 事件携带的交接快照重建，校验点逐位比对；全程只读隔离——不写存档、不发解锁
- 旧存档自动兼容：裸 id 牌组在首次载入（续局/行动）时迁移为卡牌实例结构，含战斗中存档
- **并发一致性**：每个行动的「校验 → 存档 → 动作日志 → 战败解锁」在单个 SQLite 事务
  （`BEGIN IMMEDIATE` + WAL）内原子提交，任一写入失败整体回滚，杜绝写入失败/并发操作导致的
  存档与回放分叉；per-run 锁串行化同一局的读改写，乐观版本号 `rev` 检测状态冲突（409），
  请求令牌 `request_id` 做请求级幂等（双击/超时重试不重复扣款发奖）；异常/损坏日志不拖垮回放

## 目录结构
```
backend/   FastAPI + SQLite（引擎、结算队列、会话、路由、测试）
frontend/  Vite + React + Phaser + zustand
```

## 启动

### 后端（端口 8001）
```bash
cd backend
py -m pip install -r requirements.txt
py run.py
```

### 前端（端口 5173，代理 /api → 8001）
```bash
cd frontend
npm install
npm run dev
```
浏览器打开 http://localhost:5173

## 测试（后端）
```bash
cd backend
$env:PYTHONPATH="."; $env:PYTHONDONTWRITEBYTECODE="1"
py -m pytest -q tests
```
测试覆盖：结算队列（连锁/叠加/死亡打断）、无限连锁封顶、成长树（21 节点 DAG 结构与成本、
前置条件/同层互斥/跨 lane 不互斥、同名卡独立成长、失败零副作用/重复 409、
成长贯通战斗伤害与费用、续局、商店移除、跨章继承与互斥延续、合法流程回放逐位一致、
旧 forges 记录与战斗中旧档迁移、旧版/无版本号 forge 日志默认链兼容重演）、
失败解锁、种子+动作日志确定性回放、**可交互回放逐帧重建（帧视口/规则版本/校验点/损坏检出/旧日志兼容/只读隔离与解锁隔离）**、
商店库存确定性/续局一致、购买卡牌/遗物（扣款/售罄/失败回退）、移除指定实例（递增价/牌组下限）、
交易贯通后续战斗与回放、**并发与原子提交（写入失败整体回滚、并发领奖/锻造/战败只生效一次、
request_id 幂等含并发同键、expected_rev 状态冲突 409、旧 schema 自动迁移、损坏日志/序号缺口容错）**、
**多章远征（创建即开第 1 章、章节通关交接牌组/锻造/遗物/金币、推进章节防重复开章与 request_id 幂等、
战败/终章通关结算且只结算一次、整程回放逐章校验点通过且只读隔离）**、
**远征委托（仅远征商店确定性挂单/同款去重/限张、接取防重复、战斗胜利与成功交易自动推进、
失败交易不推进、领奖防重复与 request_id 幂等、未完成/失败/战斗中拒绝领奖、战败批量失败、
推进章节超期失败且 ready 不超期、章节通关后仍可领奖而终章结算后关闭、委托跨章交接、
续局保留、合法两章流程的逐章/整程回放校验点全通过且与在线状态一致）**、
**跨章药水背包（纯背包限容量/替换/消耗与确定性货架掉落、商店购买扣款售罄/背满拒绝
替换丢弃/贸易委托推进/失败零副作用、战利品药水入包与背满选格替换、五种药水战斗效果、
消耗与击杀胜负同动作原子结算、非回合/战斗外/战败终态拒绝使用且不消耗、非战斗丢弃与
战斗中禁丢、request_id 双击只生效一瓶、药水随交接快照跨章、购买/使用/替换逐位回放
校验点全通过且最终帧与在线一致、旧档空背包迁移）**、
**伙伴（商店限招与扣款售罄、非战斗随行/休整、回合协助、援护承伤、负伤暂停、
休息节点治疗、跨章交接、续局与新旧回放校验、旧档补字段迁移）**、
**格挡/援护结算顺序 2.7.0（卡牌格挡与壁垒药水在敌人行动段抵挡伤害、格挡不跨回合、
援护只吃格挡穿透溢出；战斗中存档续局一致；2.6.1 历史战斗动作按 legacy_block 旧时序
逐位重演、升级边界动作严格校验且最终帧与在线一致）**、
**跨章节奇遇链 2.8.0（每图恰好一个奇遇节点且不替换商店/休息、待抉择续局保留/
离开失效/未知链与重复抉择拒绝、代价先校验（金币/生命/背满替换）失败零副作用、
request_id 双击只结算一次、本地链发牌不埋 flag、单局不出跨章链且末章不出起始链、
flag 随交接继承与 carry 摘要、开章预兆（敌人+血/力量/格挡/易碎/赐福回血）只在首场
战斗消耗一次、无续写 flag 兑现即移除而续写 flag 保留、续写候选优先必触发、
伏击战胜利固定战利品并推进讨伐委托/破财清 flag/伏击败北终结远征、待抉择/伏击
续局、两章合法流程逐章/整程回放第 2 章零 mismatch/error 且最终帧与在线一致、
旧档补 enc_state 迁移与旧 create 8 形状候选兼容）**、
**多人协作远征 2.9.0（队长建队/6 位入队码/满员·重复名·错误码拒绝/入队幂等、
开赛前角色分配且非队长 403·队长角色不可改·开赛锁定、人数不足拒开赛且开赛幂等不重复、
战斗位越权选节点与资源位越权打牌/购买均 403 且金币·能量·手牌·库存零变化、
未带身份/他队成员/伪造 id 403、队长两域皆可、过期 expected_rev 409、同 request_id 幂等、
首领击败协作金 15 入共享池并随交接带入第 2 章、个人贡献计数与章节 15/终胜 50 名义均分
入 ledger、战败整队 lost 且 settle 只一次、非队长推进 403 而队长推进继承队伍与协作金、
金币不足锻造 400 整体回退、合法两角色行动流程逐章/整程回放校验点零 mismatch 且每步带
操作者、第 2 章从 carry（含 coop_team）重建校验一致、协作视口携带权限边界、单人远征
携带 member_id 行为不变）**。

## API 摘要
- `POST /api/runs {seed?}` 建局
- `GET  /api/runs/{id}/resume` 续局
- `POST /api/runs/{id}/act {action,...}` 行动（choose_node / play / end_turn / claim_reward / forge / shop_buy / shop_remove / use_potion / discard_potion / companion_set_mode / commission_accept / commission_claim / encounter_choice）
  - 可选并发字段：`request_id`（客户端为每个意图生成的令牌；同令牌重复/并发提交返回首次响应，
    响应里 `duplicate:true`，绝不重复执行）、`expected_rev`（所依据视口的存档版本号；
    存档已被推进则返回 409 状态冲突）。行动响应与 `/resume` 视口携带当前 `rev`。
- `GET  /api/runs/{id}/replay` 整局可交互回放：除原 `actions`（兼容）外，返回
  `rules_version` / `recorded_versions` / `legacy` / `initial.checkpoint` / `steps[]` /
  `final_view` / `verification` / `isolated`；每个 `step` 含动作、类型/标题/摘要、
  结算事件 `events`、战斗结果，以及该步完成后的完整只读 `view`（与 `/resume` 同构）
- `GET  /api/cards`、`/api/enemies`、`/api/map-preview?seed=` 元数据

多章远征：
- `POST /api/expeditions {seed?, chapters?}` 创建远征（默认 3 章）：远征记录与第 1 章 run
  在同一事务落库；返回 `{expedition, run}`，run 视口携带 `expedition` 摘要（章节进度/结算状态）。
  协作远征不直接走本接口——由队伍开赛（见下）在同一开章路径上附带 `coop_team`。
- `GET  /api/expeditions/{id}` 远征视口 + 当前章节 run 视口（续远征入口）。
- `POST /api/expeditions/{id}/advance {request_id?}` 进入下一章：仅当远征进行中且当前章
  已通关；以交接快照（牌组/锻造/遗物/金币/生命，休整回血 25%）确定性开新章
  （章节种子由远征种子派生）。重复推进 400、已结算 409、同 request_id 返回首次响应
  （`duplicate:true`），绝不重复开章。
- `GET  /api/expeditions/{id}/replay` 整程回放：远征事件时间线 + 逐章完整回放
  （复用单局可交互回放，校验点逐章比对），全程只读。
- 章节 run 就是普通 run（`/api/runs/{id}/act|resume|replay` 全部适用）；章节 run 结束时
  （won/lost）与行动同一事务同步远征状态：非终章通关记录 chapter_clear 交接快照，
  战败/终章通关结算远征（settle），已结算或非当前章节的重复触发直接跳过。

多人协作远征（`/api/coop/...`，规则 2.9.0）：
- `POST /api/coop/teams {captain_name?, name?, seed?, chapters?}` 队长建队：
  返回队伍视口，顶层 `me` 里是队长本人（含**只下发一次**的 `token` 与 `id`），
  `code` 为 6 位入队码。
- `POST /api/coop/teams/join {code, member_name, request_id?}` 凭码加入（仅 forming、
  最多 4 人、显示名不重复；同 request_id 幂等），响应 `me` 携带该队员的 id/token。
- `GET  /api/coop/teams/{id}?member_id=` 队伍大厅视口（成员/角色/个人贡献战利/时间线）。
- `POST /api/coop/teams/{id}/roles {member_id(队长), target_id, role:combat|supply, request_id?}`
  队长分配角色（仅 forming；队长身份不可改；非队长 403；重复同角色 409）。
- `POST /api/coop/teams/{id}/leave|disband {member_id}` 开赛前退队/解散（队长退队即解散）。
- `POST /api/coop/teams/{id}/start {member_id(队长), request_id?}` 开赛：至少 2 人，
  forming→started 与远征/第 1 章 run/双方日志同事务提交（双击同令牌幂等、重复开赛 409），
  返回 `{team, expedition, run}`。
- `GET  /api/coop/teams/{id}/expedition?member_id=` 协作远征续局：队伍 + 当前章节 run
  视口（`run.coop` 含成员/角色/本成员权限/奖励常量）。
- `POST /api/coop/teams/{id}/advance {member_id, request_id?}` 仅队长推进章节（非队长 403）。
- `GET  /api/coop/teams/{id}/replay` 协作整程回放：队伍时间线 + `ledger` 个人流水 +
  远征事件 + 逐章可交互回放（每步 `actor` 为操作者），全程只读。
- 协作章节 run 的行动仍走 `POST /api/runs/{id}/act`，请求体额外带 `member_id`；
  `/resume?member_id=` 用于高亮本成员。权限：战斗动作（play/end_turn/use_potion）
  限 leader/combat，资源动作（choose_node/claim_reward/forge/shop_buy/shop_remove/
  discard_potion/companion_set_mode/commission_accept/commission_claim/encounter_choice）
  限 leader/supply；越权 403 且零副作用，未带身份或他队成员同样 403。
  单人远征/普通局忽略 member_id，行为与旧版完全一致。
- 队伍奖励：章节首领击败 +`CHAPTER_CLEAR_BONUS=15` 金入共享池（run.gold，随交接跨章）；
  通关/终胜时按当时在册成员确定性均分名义金（整除余数按成员 id 升序）记入
  `coop_ledger`（chapter +15、win +`FINAL_WIN_BONUS=50`），不进共享池、不影响 run 校验点；
  战斗位每胜一场、资源位每做一个资源动作各记一次贡献。战败整队 lost，不发终胜金。

卡牌成长树（锻造节点）：`{action:"forge", card:<卡牌实例 uid>, growth_node:<节点 id>}`
（旧客户端用 `branch` 传 T1 节点 id 也兼容）。节点分三层收费（入门 25 / 进阶 40 / 终阶 60，
视口 `growth_tier_cost` 与 `growth_tree` 下发完整树定义：每个节点带 `requires` 前置与
`mutex_with` 互斥集）；同一锻造节点仅可锻造一次，重复请求 409 且不扣款，
金币不足/非法卡牌/未知节点 400，前置未达成 400，互斥分支已选 400（校验先于扣款，零副作用）。
卡牌实例按 `growth:[{node,cost}]` 保存解锁节点序列与每笔成本，`growth_spent` 为该实例累计投入。
2.3.0 之前的旧 forge 日志（无版本号或 `ver<2.3.0`，含同分支可重复选择）回放时
按确定性默认链（sharpen→keen→rend、empower→amplify→overload、refine→economize→frugality，
每笔固定 25 金）兼容重演；旧版存档的 `forges:[分支...]` 在首次载入时自动迁移。

商店行动（进入商店节点后视口携带 `shop_available:true` 与 `shop` 库存）：
- 购买：`{action:"shop_buy", kind:"card"|"relic", sku:<货架项 id，如 "card:cleave">}`，
  按货架价格扣款；重复购买/已持有遗物返回 409（售罄，不扣款），金币不足/非法货架返回 400。
- 移除：`{action:"shop_remove", card:<卡牌实例 uid>}`，永久删除该实例（同名卡其余副本不受影响），
  基础价 35 金币，同店每移除一次下一次 +15（`shop.remove.cost`/`next_cost` 随视口返回）；
  牌组仅剩 1 张、金币不足或 uid 非法返回 400。
- 两类交易都走统一事务：先校验后扣款，过程中任何异常把 run 整体回退到交易前快照；
  交易记录保存在 `shop.tx`（含类型/sku 或 uid/价格/余额），离开节点库存清空。
  购入的卡牌/遗物直接进入后续战斗（遗物在战斗初始状态换算，新卡随下次洗牌进三堆）。

药水背包（`app/potions.py`，规则 2.5.0）：
- 背包：run 状态 `potions` 为按下标排列的药水 id 列表（容量 `POTION_CAPACITY=3`），
  随章节交接快照 `carry.potions` 带入下一章；旧档缺字段时首次载入补空背包（结构迁移）。
- 获得：商店药水货架（`shop.potions[]`，确定性生成、各自独立 sold）购买；战斗胜利
  战利品约 70% 概率追加一个药水选项（独立 RNG 派生流，不改变既有选项的确定性）。
- 限容量替换：背满时 `shop_buy {kind:"potion", replace:<格位>}` 或
  `claim_reward {option, replace:<格位>}` 必须指定被替换丢弃的格位；未指定/非法
  格位返回 400 且零副作用（校验先于扣款与领奖状态翻转）。非战斗可
  `{action:"discard_potion", slot}` 主动丢弃；战斗中禁止丢弃。
- 战斗使用：`{action:"use_potion", slot}` 仅在玩家自己的回合可用（非回合/战斗外/
  非法格位 400 且不消耗）。消耗（背包移除）与效果（复用 `EffectEvent`+结算队列：
  heal/gain_block/damage/gain_energy/apply_status）及随后的死亡打断/胜负/战利品/
  战败解锁在**同一个纯推演动作**内完成，随存档/日志/解锁同一事务原子提交；因此
  炽焰药水收尾击杀与战斗胜利同帧、击败首领即 `run_won`。request_id 幂等保证
  双击/超时重试只消耗一瓶（重复请求返回首次响应，`duplicate:true`）。
- 回放：`use_potion/discard_potion` 是普通动作（kind=battle/potion），消耗与效果
  逐位重建、校验点严格比对；`log[0]` 为 `{potion:{slot,id,name,icon}}` 消耗标记，
  其后是同序结算事件，前端 Phaser 先弹药水横幅再逐条播放。

伙伴模块（`app/companions.py`，规则 2.6.0）：
- 招募：进入商店时，尚无伙伴则在 `shop.companions[]` 确定性出现见习卫士；
  `shop_buy {kind:"companion", sku:"companion:squire"}` 扣 50 金币并入队，重复招募 409。
- 随行/休整：非战斗时 `{action:"companion_set_mode", mode:"accompany|rest"}`；
  随行才会创建战斗实体并行动，休整不参战。重复设置同模式返回 409，战斗中 400。
- 战斗：每轮开始先对敌人造成 4 点伤害；敌人攻击先由玩家格挡吸收，**穿透格挡的
  溢出部分**才由伙伴先承受最多 3 点（2.7.0 修正；旧实现按全额伤害抵挡、格挡被绕过），
  剩余伤害才作用玩家。伙伴生命归零即负伤并自动转休整，暂停后续参战。
- 治疗：休整中的伙伴进入休息节点时回满生命并清除负伤；治疗与玩家节点恢复在同一个
  `choose_node` 动作内落库。随后玩家可重新安排随行。
- 持久化/回放：伙伴在 run 状态 `companion` 并进入 `carry.companion`；战斗中生命写入
  `battle.companion_state`，每个战斗动作同步。续局、整章/整程回放均逐帧重建。
  2.6.0 之前旧档缺字段时首次载入补 `companion:null`，迁移步前的旧校验点按 legacy 跳过。

远征委托（仅远征章节商店挂单，普通局不出委托）：
- 挂单：进入商店时 `shop.commissions[]` 确定性生成（随商店种子/章号/已持有委托，
  同款不重复，持有满 3 个进行中/待领取时不再刷新），每项含目标（讨伐战斗数 /
  完成交易笔数）、限章 `deadline_chapter` 与奖励（金币或卡牌）。
- 接取：`{action:"commission_accept", sku:"commission:<签名>"}`，免费；挂单随即移除，
  重复接取返回 409（不重复发委托），离开商店/非法 sku/非远征返回 400。
- 自动推进：战斗胜利推进讨伐目标、成功交易（购买/移除各算一笔）推进贸易目标；
  失败交易整体回退不推进。目标达成状态转 `ready`；战斗中不可领奖。
- 领取：`{action:"commission_claim", commission:<委托 id>}`，金币直接入账、
  卡牌以独立实例入牌组；状态机只允许 `ready→claimed` 一次，重复领取 409 不重复发奖，
  request_id 同令牌返回首次响应；未完成/已失败 400。章节通关（won）后、推进下一章前
  仍可领取；终章通关（远征 won）或战败（远征 lost）后关闭领奖。
- 失败：战败时全部进行中委托 `failed(battle_lost)`；推进章节时限章早于新章的进行中
  委托 `failed(expired)`（advance 事件携带 expired id 列表），`ready` 委托不超期。
- 持久化：委托与发号器在 run 状态（`commissions/next_commission_seq/chapter/chapters_total`）
  并进入章节交接快照；在线开章与回放重建共用 `_new_run_state`，逐位校验点一致。
  2.4.0 修复：交接快照 carry 里的 `chapter` 是「来源章」，开新章时新 run 的章号只认真实
  入参（旧实现误用 carry 章号，导致第 2 章及以后 run 的章号恒为 1，委托挂单 deadline、
  剩余期限 `chapters_left`、领奖位置 `claimed_at`、跨章超期判定全部错乱）。受影响旧档首次
  载入（续局/首行动）时以权威 `runs.chapter` 列修复 run 状态与远征表 carry：重锚委托接取
  章/期限、修正领奖记录、复活「真实接取章≥2 且修复后期限未到」却被旧逻辑误判超期的进行中
  委托（第 1 章接取的真实超期失败保持终态）；整章/整程回放通过 create 事件记录的校验点
  自动识别受影响章，沿修复后的正确路径重放（旧接取动作按修复挂单兼容重建），修复点之前的
  步骤以 legacy 呈现并跳过逐位哈希比对（`repaired` 计数），修复点之后（含在线迁移后的新
  动作）仍严格校验，最终帧与在线修复存档逐位一致，全程只读隔离。
  2.6.1 修复（存档升级 × 商店规则 × 回放重建衔接）：回放从旧形状（无 `potions`/
  `companion`）create 校验点重建时，规则切换点必须与在线迁移对齐——`/resume` 静默
  迁移（无日志事件，首个 2.5.0+ 动作之前规则已生效）与 `/act` 迁移（本步动作推演
  之前迁移）都要求回放【先切换规则、再推演迁移点动作】。旧实现在动作推演之后才切换，
  导致升级后首个「进入新商店」仍按旧规则生成库存（缺药水/伙伴货架），随后的
  `shop_buy` 无法重演（unknown shop item），实玩与回放的药水库存分叉；2.5.0 才追加
  的战后药水战利品也会让旧 `claim_reward` 下标错位（金币/卡牌张冠李戴）。修复后：
  迁移点动作按在线语义逐位重建——静默迁移的首个新版动作严格校验（不再以 legacy
  名义吞掉真实分叉），`/act` 迁移步本身按 legacy 豁免（旧结构→新结构不可比）但规则
  已先行切换，旧战斗战利品不生成药水项；迁移前生成的旧商店货架与历史交易
  （`shop.tx`）原样保留、不补造新货架，升级前动作全部以 legacy 可正常重演。
  2.7.0 修复（格挡/援护结算顺序 × 战斗中存档 × 历史动作日志兼容）：旧实现在
  `end_turn` 里、敌人行动【之前】就把玩家格挡清零，卡牌（防御等）与壁垒药水给的
  格挡完全无法抵挡敌方伤害（格挡形同虚设）；伙伴援护又按【全额伤害】判穿——应只吃
  格挡吸收后的穿透溢出——导致格挡被直接绕过（伙伴吃 guard、玩家吃全额-guard）。
  修复后顺序为：敌人攻击 → 玩家格挡吸收 → 穿透格挡的溢出部分先由伙伴援护
  （最多 guard 点）→ 仍有剩余才扣玩家生命；格挡持续整个敌方行动段，在回合末
  （下一回合 `start_turn`）清零、不跨回合保留。这是纯规则修复、无存档结构变更：
  战斗中存档只在玩家回合边界落库（格挡在玩家手中等待下一步），旧档首次续局
  即按新规则，无需迁移；历史动作日志按录制版本兼容——2.7.0 之前的战斗动作以
  `legacy_block` 旧时序（敌人行动前清格挡、按全额援护）逐位重演并按 legacy 呈现，
  首个 2.7.0+ 动作之前规则先行切换，边界动作严格校验，最终帧与在线存档逐位一致，
  续局、战斗中续局与整局/整程回放都不分叉。

跨章节奇遇链（`app/encounters.py`，规则 2.8.0）：
- 节点：地图由独立 RNG 派生流（`seed*131+17`）确定性安放恰好一个「奇遇」节点
  （`type:"event"`，第 2~3 行），只从战斗/奖励/锻造节点中挑选、且优先挑同行有
  同类备份的节点，因此商店/休息数量与唯一类型通路保持不变；既有节点类型/敌人的
  抽取序列完全不受影响（新 RNG 在主 rng 抽完之后才取）。
- 待抉择：`choose_node` 进入事件节点后 `enc_state.pending={chain,node}`，视口
  `encounter` 下发 `{chain,title,text,choices[]}`；离开节点自动作废（地图无环）。
  链由 `(章节种子,节点行,章号)` 确定性抽取——续写链（`continue:true`）候选非空时
  必在续写中抽取，保证埋了 flag 的玩家走到后续章奇遇必遇续写；普通局
  （`chapters_total=null`）只出本地链，末章不出跨章起始链；无候选时是兜底清泉。
- 抉择：`{action:"encounter_choice", chain, enc_choice, replace?}`。代价
  （cost.gold/cost.hp）先校验（金币不足/致命/药水背满未指定替换格 -> 400，零副作用），
  再同事务落账；奖励类型与奖励/商店同构并扩展 `hp/max_health/relic_set/battle`。
  重复抉择 400（pending 已清空），双击靠 request_id 返回首次响应（duplicate:true）。
- flag 与预兆：抉择的 `set_flag` 写入 `enc_state.flags{flag:埋设章}` 并进入
  `carry.enc_state`；开新章时 `on_chapter_begin(chapter)` 对「上一章埋下且未兑现」
  的 flag 各兑现一次：`FLAG_OPENERS` 的 `battle`（strength/block/fragile/enemy_hp）
  合并进 `battle_mods`、`heal` 叠加在 25% 休整回血之上；无续写链等待的 flag
  （altar_*）兑现即移除，续写 flag（wt_*）保留到续写节点。战斗修正由本章首场
  战斗一次性消费（`_build_battle` 在建场 start_turn 后施加，事件先于权威快照），
  续局从 `battle` 快照恢复、不二次施加。
- 续写：`avenger_ambush` 的「迎战」是**非节点伏击战**（敌人 avenger，26 血；
  `_build_battle(node_data=None)`），不享受首领遗物加成但照常消耗本场可用的章节
  预兆、伙伴、遗物开局；胜利固定发金币（无普通战利品/无领奖步骤）、推进讨伐委托，
  战败走普通战败路径（远征 lost、委托全失败、失败解锁）；「破财」抉择直接扣钱结清。
- 视口：`encounter`（待抉择）、`encounter_flags[]`（活跃印记：title/desc/
  since_chapter/opener_at/pending_opener）；交接快照 carry 增加
  `encounter_flags` 摘要。动作日志事件：`encounter_event`（进入/兜底）、
  `encounter_choice`（抉择）、`encounter_ambush`（伏击开场横幅）、
  `encounter_opener`（预兆施加）、`encounter_grant`（逐项奖励）、
  `encounter_ambush_result`（伏击胜负），全部进入可交互回放时间轴（kind=encounter）。
- 旧档：无 `enc_state` 字段时首次 `/resume`/`/act` 补 `fresh_state()`（结构迁移，
  本步按 legacy）；旧 create 校验点回放时在 companion/potions/encounters 三个结构维
  的 8 种形状候选中匹配（完整形状优先），缺 enc_state 的旧初态在首个 2.8.0+ 动作前
  按 legacy 呈现、规则先行切换，之后严格校验。

## 设计要点
- 所有战斗逻辑在服务端（唯一权威），客户端仅播放服务器返回的结算事件 → 续局/回放天然一致。
- 行动日志为有序序列：结算事件（`action/target/value/source/extra`）+ 控制项（`{"snapshot"}` 权威校正点、
  `{"result":"won|lost|run_won"}` 战斗结束、`enemy_turn` 敌方回合标记含技能名）；`end_turn` 的敌方结算
  事件同样按序入日志，敌人技能的全部效果都会结算（如血裔"吸取"= 伤害 + 自身回血）。
- 前端播放：Phaser 场景维护展示态（血量/护盾/状态随事件逐条推算），日志末尾的权威快照负责校正；
  播放期间到达的快照（刷新/续局）暂存至队列播完后应用，空闲时立即生效。
- 结算队列：事件按插入序稳定解析，效果 → 连锁子事件；目标死亡后定向事件被剔除（死亡打断）。
- 确定性：洗牌与敌人意图均由 `random.Random(seed)` 派生，持久化在 run 状态，回放可复现。
- 可交互回放：在线行动与回放共用纯推演函数 `_apply_action(run, action, map, grant_unlocks)`，
  “玩”和“放”永远是同一套规则。回放从建局初始状态按日志逐步推演，为每步产出一帧只读视口；
  动作事件携带 `ver`（规则版本）与 `ckpt`（权威状态 SHA-256 校验点），每步重演后比对哈希，
  不一致标记 `mismatch`（日志损坏/规则漂移）、无版本号事件标记 `legacy`（旧日志兼容、不校验）。
  回放路径 `grant_unlocks=False` 且不调用任何写库接口：runs/battle_events/profile 均不变，战败不解锁。
  前端 `ReplayPlayer` 提供播放/暂停/单步/跳转/倍速/类型过滤时间轴，战斗帧经同一 Phaser 事件队列重演。
- 卡牌实例：`run.card_instances` 为 `uid -> {id, growth:[{node,cost}]}`，牌组/牌堆/手牌只存 uid；
  引擎在打牌时用 `forging.effective_card(base, growth)` 即时换算生效卡牌（费用/数值/附加效果），
  成长节点按解锁顺序叠加；因此成长树解锁/商店购卡/商店移除自动贯通战斗结算、续局、回放与跨章继承（初始牌组 7 张，商店可增减）。
- 成长树规则（`app/forging.py`，数据驱动的 21 节点 DAG）：3 个 T1 入口（锋锐/强效/精炼），
  每个 T1 下 2 个互斥 T2，每个 T2 下 2 个互斥 T3；`requires` 表达前置、`mutex_with` 表达
  同层二选一（三条 lane 之间互不互斥）。`validate_unlock(inst,node)` 统一校验
  未知/已解锁/前置/互斥，`available_nodes(inst)` 计算当前可解锁集合；节点金币成本按 tier 固定。
  每个节点的数值变换是独立纯函数（伤害/格挡/易伤/力量/抽牌/能量/费用），依次施加、可自由组合。
- 旧档兼容：缺少 `card_instances` 的存档在首次载入时迁移；战斗中存档按三堆出现序把裸 id
  稳定映射到 uid，洗牌布局与确定性保持不变。2.3.0 起实例 `forges:[分支]` 迁移为
  `growth:[{node,cost}]`（重复分支沿确定性默认链展开、逐笔保留 25 金成本）；
  旧版 forge 动作日志在回放时走同一兼容映射，含旧版本号（`ver<2.3.0`）与无版本号日志。
  结构迁移发生在已存在的动作上，该步事件带 `migrated:true`，回放按 legacy 处理本步校验点
  （结构迁移天然不可逐位比较），后续步骤仍严格校验。
- **原子提交与并发**：`db.transaction()` 经共享单连接开启 `BEGIN IMMEDIATE`，行动事务内
  顺序写 runs（`rev=rev+1` 乐观锁）→ battle_events → profile → act_requests，异常即 rollback；
  per-run `threading.RLock` 在进程内串行同一局，WAL + busy_timeout 让读写不互斥。
  旧库启动自动补 `rev` 列与 `act_requests` 表。损坏的 `payload_json`（非法 JSON/NULL）
  在读取时降级为 `_corrupt` 行，回放标注 `error` 仍可播放其余步骤；序号缺口给出 `warning`
  与 `verification.seq_gaps`。
- SQLite：`runs`（状态，含 rev 乐观版本、商店库存/交易记录、expedition_id/chapter 远征归属）、
  `battle_events`（动作日志，含 forge/shop 行；协作远征每步 payload 带 actor/actor_role）、
  `profile`（解锁卡）、
  `act_requests`（request_id → 首次响应，请求级幂等）、
  `expeditions`（远征：状态/章节进度/交接快照 carry_json/rev/coop_team_id）、
  `expedition_events`（远征事件：create/chapter_clear/advance/settle，整程回放时间线）、
  `coop_teams`（协作队伍：入队码/队长/状态/绑定远征/rev）、`coop_members`（成员：
  入会令牌 token/角色 leader·combat·supply/入队序）、`coop_events`（队伍时间线）、
  `coop_ledger`（个人贡献与名义战利流水）、`coop_requests`（队伍管理动作幂等）。
- 远征章节 run 的初始状态由 `_new_run_state(seed, carry, chapter, chapters_total,
  expedition_id)` 构造：新 run 的章号/总章数/远征 id 只认真实入参（carry 里的同名字段
  是「来源章」历史快照，不参与新 run 身份判定）；在线开章与回放重建共用同一函数，交接
  快照随章节 run 的 create 事件落库，回放无需读取远征表即可逐位复演；
  章节种子 = f（远征种子， 章节号） 确定性派生，各章地图/洗牌独立但可复现。
  交接快照同时携带远征委托（含进度与 ready/claimed/failed 终态）与委托 uid 发号器，
  开新章时先对快照跑超期判定再建章，advance 事件记录 expired 列表，因此委托的
  接取/推进/领奖/超期/战败全部是动作序列的确定性函数，续局与回放逐位一致。
