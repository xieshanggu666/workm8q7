"""SQLite 初始化与访问：managed 文件位于 backend/data/game.db。

并发一致性设计：
- 所有写操作经单个连接在一个事务里提交（BEGIN IMMEDIATE），行动的
  「存档 + 动作日志 + 解锁奖励」要么全部生效、要么全部回滚，杜绝写入失败
  造成的存档/回放分叉；
- WAL 日志 + busy_timeout，读写并发不再因锁竞争互相报错；
- per-run 进程内互斥锁串行化同一局的读改写；幂等表 act_requests 记录
  客户端请求令牌，重复请求（双击/超时重试）返回首次结果而非再执行一次；
- 旧库启动时自动补齐后增列与幂等表（向后兼容已有 game.db）。
"""
import json
import os
import sqlite3
import threading

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # backend/
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.environ.get("GAME_DB_PATH", os.path.join(DATA_DIR, "game.db"))

_lock = threading.RLock()
_conn = None  # 进程内单连接（check_same_thread=False + 写锁串行化）

# 每个 run 一把互斥锁：同一局的行动/迁移在进程内串行，不同局互不阻塞
_run_locks = {}
_run_locks_guard = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    seed INTEGER NOT NULL,
    status TEXT NOT NULL,
    position TEXT NOT NULL,          -- 当前地图节点 id
    map_json TEXT NOT NULL,
    state_json TEXT NOT NULL,
    rev INTEGER NOT NULL DEFAULT 1,  -- 乐观版本号：每次原子提交 +1
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS battle_events (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    action TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);

CREATE TABLE IF NOT EXISTS profile (
    id TEXT PRIMARY KEY,             -- 'single'
    unlocked_cards TEXT NOT NULL
);

-- 行动请求幂等：同一 run + request_id 只提交一次，重复请求返回首次响应
CREATE TABLE IF NOT EXISTS act_requests (
    run_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, request_id)
);

-- 多章远征：一条远征串起若干章节 run；carry_json 为最近一次章节交接快照
CREATE TABLE IF NOT EXISTS expeditions (
    id TEXT PRIMARY KEY,
    seed INTEGER NOT NULL,
    status TEXT NOT NULL,            -- in_progress / won / lost
    chapter INTEGER NOT NULL,        -- 当前章节（1 起）
    chapters_total INTEGER NOT NULL,
    current_run_id TEXT NOT NULL,    -- 当前章节对应的 run
    carry_json TEXT,                 -- 章节交接快照（牌组/锻造/遗物/金币/生命）
    rev INTEGER NOT NULL DEFAULT 1,  -- 乐观版本号：推进章节/结算时 +1
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 远征事件日志：create / chapter_clear / advance / settle，整程回放按序呈现
CREATE TABLE IF NOT EXISTS expedition_events (
    exp_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (exp_id, seq)
);

-- 多人协作远征（2.9.0）：队伍大厅。一支队伍绑定一支远征（start 后写 expedition_id）；
-- join_code 为 6 位入队码（队长分发给队员），status: forming/started/disbanded
CREATE TABLE IF NOT EXISTS coop_teams (
    id TEXT PRIMARY KEY,
    join_code TEXT NOT NULL UNIQUE,
    name TEXT,
    status TEXT NOT NULL,             -- forming / started / disbanded
    leader_id TEXT NOT NULL,
    seed INTEGER,
    chapters_total INTEGER,
    expedition_id TEXT,               -- 开赛后绑定的远征（forming 时为 NULL）
    rev INTEGER NOT NULL DEFAULT 1,   -- 队伍乐观版本：角色调整/开赛 +1
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 队伍成员：token 为该成员的入会令牌（客户端随管理/行动请求携带，做权限边界校验）
CREATE TABLE IF NOT EXISTS coop_members (
    id TEXT NOT NULL,
    team_id TEXT NOT NULL,
    token TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    role TEXT NOT NULL,               -- leader / combat / supply
    joined_seq INTEGER NOT NULL,      -- 入队序（确定性排序与均分余数）
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, team_id)
);

-- 队伍时间线事件（组队/入队/角色/开赛/推进/结算 + 个人战利 ledger），整程回放按序呈现
CREATE TABLE IF NOT EXISTS coop_events (
    team_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (team_id, seq)
);

-- 个人奖励/贡献流水：member_id 维度（战斗胜利/后勤操作/章节均分/终胜均分）
CREATE TABLE IF NOT EXISTS coop_ledger (
    team_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    member_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    amount INTEGER NOT NULL DEFAULT 0,
    chapter INTEGER,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (team_id, seq)
);

-- 协作队伍管理动作的请求级幂等（入队/改角色/开赛/推进/退队/解散），
-- 键空间与章节 run 的 act_requests 分开：run_id 列存 'coop:<team_id>'
CREATE TABLE IF NOT EXISTS coop_requests (
    team_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (team_id, request_id)
);
"""


def _connect():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    # WAL：读者不阻塞写者；busy_timeout 让锁竞争等待而不是立即报错
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_conn():
    """供测试/运维使用的独立连接（只读探查或测试夹具清表）。

    业务写入不要自行开连接提交——那会绕过单连接事务边界。需要事务时用
    ``transaction()``；需要在行动事务里完成多表写入时用 ``act_transaction()``。
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db():
    global _conn
    with _lock:
        os.makedirs(DATA_DIR, exist_ok=True)
        conn = _connect()
        try:
            conn.executescript(_SCHEMA)
            # 旧库兼容：补齐后增列（ALTER 失败说明列已存在，忽略）
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
            if "rev" not in cols:
                conn.execute("ALTER TABLE runs ADD COLUMN rev INTEGER NOT NULL DEFAULT 1")
            if "expedition_id" not in cols:
                # 多章远征：章节 run 归属的远征与章节序号（普通局为 NULL）
                conn.execute("ALTER TABLE runs ADD COLUMN expedition_id TEXT")
                conn.execute("ALTER TABLE runs ADD COLUMN chapter INTEGER")
            # 2.9.0：协作队伍归属（远征记录指向其队伍；单人远征为 NULL）
            ecols = {r["name"] for r in conn.execute("PRAGMA table_info(expeditions)").fetchall()}
            if "coop_team_id" not in ecols:
                conn.execute("ALTER TABLE expeditions ADD COLUMN coop_team_id TEXT")
            conn.commit()
        finally:
            conn.close()
        _conn = _connect()


def run_lock(run_id):
    """返回该局专属的进程内互斥锁（串行化同一 run 的读-改-写）。"""
    with _run_locks_guard:
        lk = _run_locks.get(run_id)
        if lk is None:
            lk = threading.RLock()
            _run_locks[run_id] = lk
        return lk


# ---------- 低层事务 ----------
def transaction():
    """返回一个立即写事务上下文：块内所有语句一起提交，异常整体回滚。"""
    return _Tx(_conn)


class _Tx:
    """对共享单连接加全局锁后开启立即写事务：读写互不交错。

    进程内所有 SQL 都经同一连接且被 _lock 串行；per-run 锁在更外层保证
    “读-改-写”的逻辑原子性。
    """

    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        _lock.acquire()
        if self._conn is None:
            init_db()
            self._conn = _conn
        # IMMEDIATE：进入即拿写锁，消除两个写事务交错提交（丢失更新）的窗口
        self._conn.execute("BEGIN IMMEDIATE")
        return self._conn

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            _lock.release()
        return False


# ---------- runs ----------
def insert_run(conn, run_id, seed, status, position, map_data, state,
               expedition_id=None, chapter=None):
    conn.execute(
        "INSERT INTO runs(id,seed,status,position,map_json,state_json,rev,"
        "expedition_id,chapter,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,1,?,?,datetime('now'),datetime('now'))",
        (run_id, seed, status, position,
         json.dumps(map_data, ensure_ascii=False),
         json.dumps(state, ensure_ascii=False),
         expedition_id, chapter),
    )


def _row_to_run(row):
    keys = row.keys()
    return {
        "id": row["id"], "seed": row["seed"], "status": row["status"],
        "position": row["position"], "map": json.loads(row["map_json"]),
        "state": json.loads(row["state_json"]), "rev": row["rev"],
        # 旧库在 init_db 迁移后才有这两列；普通局为 None
        "expedition_id": row["expedition_id"] if "expedition_id" in keys else None,
        "chapter": row["chapter"] if "chapter" in keys else None,
    }


def load_run(run_id):
    """读取单局（含乐观版本号 rev）。使用共享连接，读不到未提交数据。"""
    with _lock:
        conn = _conn
        if conn is None:
            init_db()
            conn = _conn
        row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    return None if row is None else _row_to_run(row)


def save_run_run(conn, run_id, status, position, state, expected_rev=None):
    """在事务内更新存档并推进 rev。

    expected_rev 非 None 时做乐观并发检查：存档已被其它请求推进则抛出
    ConcurrentModification，由上层转 409（状态冲突）。
    """
    if expected_rev is None:
        conn.execute(
            "UPDATE runs SET status=?, position=?, state_json=?, rev=rev+1, "
            "updated_at=datetime('now') WHERE id=?",
            (status, position, json.dumps(state, ensure_ascii=False), run_id),
        )
        return
    cur = conn.execute(
        "UPDATE runs SET status=?, position=?, state_json=?, rev=rev+1, "
        "updated_at=datetime('now') WHERE id=? AND rev=?",
        (status, position, json.dumps(state, ensure_ascii=False), run_id, expected_rev),
    )
    if cur.rowcount == 0:
        raise ConcurrentModification(f"run {run_id} changed concurrently (rev {expected_rev})")


# ---------- battle_events ----------
def next_seq_conn(conn, run_id):
    row = conn.execute(
        "SELECT COALESCE(MAX(seq),0) AS m FROM battle_events WHERE run_id=?", (run_id,)
    ).fetchone()
    return row["m"] + 1


def append_event_conn(conn, run_id, seq, action, payload):
    conn.execute(
        "INSERT INTO battle_events(run_id,seq,action,payload_json) VALUES(?,?,?,?)",
        (run_id, seq, action, json.dumps(payload, ensure_ascii=False)),
    )


def load_events(run_id):
    """读取动作日志（只读）。

    异常日志兼容：payload_json 损坏（NULL/截断/非法 JSON）的行不抹掉整段回放，
    返回空 payload 并由回放推演标注为 error（前端仍可跳转其余步骤）。
    """
    with _lock:
        conn = _conn
        if conn is None:
            init_db()
            conn = _conn
        rows = conn.execute(
            "SELECT seq, action, payload_json FROM battle_events WHERE run_id=? ORDER BY seq",
            (run_id,),
        ).fetchall()
    out = []
    for r in rows:
        raw = r["payload_json"]
        try:
            payload = json.loads(raw) if raw is not None else {}
        except (ValueError, TypeError):
            payload = {"_corrupt": True, "_raw": raw[:200] if isinstance(raw, str) else None}
        if not isinstance(payload, dict):
            payload = {"_corrupt": True}
        out.append({"seq": r["seq"], "action": r["action"], "payload": payload})
    return out


# ---------- 行动请求幂等 ----------
def get_idempotent(conn, run_id, request_id):
    """查重复请求的首次响应；request_id 为空或查不到返回 None。"""
    if not request_id:
        return None
    row = conn.execute(
        "SELECT seq, response_json FROM act_requests WHERE run_id=? AND request_id=?",
        (run_id, request_id),
    ).fetchone()
    if row is None:
        return None
    return {"seq": row["seq"], "response": json.loads(row["response_json"])}


def put_idempotent(conn, run_id, request_id, seq, response):
    if not request_id:
        return
    conn.execute(
        "INSERT INTO act_requests(run_id,request_id,seq,response_json,created_at) "
        "VALUES(?,?,?,?,datetime('now'))",
        (run_id, request_id, seq, json.dumps(response, ensure_ascii=False)),
    )


# ---------- profile ----------
def get_profile_conn(conn):
    row = conn.execute("SELECT unlocked_cards FROM profile WHERE id='single'").fetchone()
    return None if row is None else json.loads(row["unlocked_cards"])


def get_profile():
    with _lock:
        conn = _conn
        if conn is None:
            init_db()
            conn = _conn
        row = conn.execute("SELECT unlocked_cards FROM profile WHERE id='single'").fetchone()
    return None if row is None else json.loads(row["unlocked_cards"])


def upsert_profile_conn(conn, unlocked_cards):
    conn.execute(
        "INSERT INTO profile(id,unlocked_cards) VALUES('single',?) "
        "ON CONFLICT(id) DO UPDATE SET unlocked_cards=excluded.unlocked_cards",
        (json.dumps(unlocked_cards, ensure_ascii=False),),
    )


# ---------- 多章远征 ----------
def insert_expedition(conn, exp_id, seed, chapters_total, current_run_id, carry=None,
                      coop_team_id=None):
    conn.execute(
        "INSERT INTO expeditions(id,seed,status,chapter,chapters_total,current_run_id,"
        "carry_json,coop_team_id,rev,created_at,updated_at) "
        "VALUES(?,?,'in_progress',1,?,?,?,?,1,datetime('now'),datetime('now'))",
        (exp_id, seed, chapters_total, current_run_id,
         json.dumps(carry, ensure_ascii=False) if carry is not None else None,
         coop_team_id),
    )


def _row_to_expedition(row):
    keys = row.keys()
    return {
        "id": row["id"], "seed": row["seed"], "status": row["status"],
        "chapter": row["chapter"], "chapters_total": row["chapters_total"],
        "current_run_id": row["current_run_id"],
        "carry": json.loads(row["carry_json"]) if row["carry_json"] else None,
        # 旧库迁移后才有该列；单人远征为 None
        "coop_team_id": row["coop_team_id"] if "coop_team_id" in keys else None,
        "rev": row["rev"],
    }


def load_expedition(exp_id):
    """读取远征（含交接快照）。使用共享连接，读不到未提交数据。"""
    with _lock:
        conn = _conn
        if conn is None:
            init_db()
            conn = _conn
        row = conn.execute("SELECT * FROM expeditions WHERE id=?", (exp_id,)).fetchone()
    return None if row is None else _row_to_expedition(row)


def save_expedition_conn(conn, exp_id, status, chapter, current_run_id, carry, expected_rev=None):
    """在事务内推进远征状态并 rev+1；expected_rev 非 None 时做乐观并发检查。"""
    cur = conn.execute(
        "UPDATE expeditions SET status=?, chapter=?, current_run_id=?, carry_json=?, "
        "rev=rev+1, updated_at=datetime('now') WHERE id=? AND (? IS NULL OR rev=?)",
        (status, chapter, current_run_id,
         json.dumps(carry, ensure_ascii=False) if carry is not None else None,
         exp_id, expected_rev, expected_rev),
    )
    if cur.rowcount == 0:
        raise ConcurrentModification(f"expedition {exp_id} changed concurrently (rev {expected_rev})")


def next_expedition_seq_conn(conn, exp_id):
    row = conn.execute(
        "SELECT COALESCE(MAX(seq),0) AS m FROM expedition_events WHERE exp_id=?", (exp_id,)
    ).fetchone()
    return row["m"] + 1


def append_expedition_event_conn(conn, exp_id, seq, kind, payload):
    conn.execute(
        "INSERT INTO expedition_events(exp_id,seq,kind,payload_json) VALUES(?,?,?,?)",
        (exp_id, seq, kind, json.dumps(payload, ensure_ascii=False)),
    )


def load_expedition_events(exp_id):
    """读取远征事件日志（只读）；损坏行降级为 _corrupt，不拖垮整程回放。"""
    with _lock:
        conn = _conn
        if conn is None:
            init_db()
            conn = _conn
        rows = conn.execute(
            "SELECT seq, kind, payload_json FROM expedition_events WHERE exp_id=? ORDER BY seq",
            (exp_id,),
        ).fetchall()
    out = []
    for r in rows:
        raw = r["payload_json"]
        try:
            payload = json.loads(raw) if raw is not None else {}
        except (ValueError, TypeError):
            payload = {"_corrupt": True, "_raw": raw[:200] if isinstance(raw, str) else None}
        if not isinstance(payload, dict):
            payload = {"_corrupt": True}
        out.append({"seq": r["seq"], "kind": r["kind"], "payload": payload})
    return out


def list_expedition_runs(exp_id):
    """远征的章节 run 列表（按章节序），用于统一管理章节存档与整程回放。"""
    with _lock:
        conn = _conn
        if conn is None:
            init_db()
            conn = _conn
        rows = conn.execute(
            "SELECT id, chapter, status FROM runs WHERE expedition_id=? ORDER BY chapter",
            (exp_id,),
        ).fetchall()
    return [{"run_id": r["id"], "chapter": r["chapter"], "status": r["status"]} for r in rows]


# ---------- 独立包装：迁移/测试/运维用（单表原子即可的场景） ----------
def save_run(run_id, status, position, state):
    with transaction() as conn:
        save_run_run(conn, run_id, status, position, state)


def next_seq(run_id):
    with transaction() as conn:
        return next_seq_conn(conn, run_id)


def append_event(run_id, seq, action, payload):
    with transaction() as conn:
        append_event_conn(conn, run_id, seq, action, payload)


def upsert_profile(unlocked_cards):
    with transaction() as conn:
        upsert_profile_conn(conn, unlocked_cards)


class ConcurrentModification(Exception):
    """乐观版本号不匹配：存档在本请求处理期间被其它提交推进。"""
    pass


# ---------- 多人协作远征（2.9.0） ----------
def insert_coop_team_conn(conn, team_id, join_code, leader_id, name=None,
                          seed=None, chapters_total=None):
    conn.execute(
        "INSERT INTO coop_teams(id,join_code,name,status,leader_id,seed,chapters_total,"
        "expedition_id,rev,created_at,updated_at) "
        "VALUES(?,?,?,'forming',?,?,?,NULL,1,datetime('now'),datetime('now'))",
        (team_id, join_code, name, leader_id, seed, chapters_total),
    )


def insert_coop_member_conn(conn, member_id, team_id, token, name, role, joined_seq):
    conn.execute(
        "INSERT INTO coop_members(id,team_id,token,name,role,joined_seq,created_at) "
        "VALUES(?,?,?,?,?,?,datetime('now'))",
        (member_id, team_id, token, name, role, joined_seq),
    )


def _row_to_team(row):
    return {
        "id": row["id"], "join_code": row["join_code"], "name": row["name"],
        "status": row["status"], "leader_id": row["leader_id"],
        "seed": row["seed"], "chapters_total": row["chapters_total"],
        "expedition_id": row["expedition_id"], "rev": row["rev"],
    }


def load_coop_team(team_id):
    with _lock:
        conn = _conn
        if conn is None:
            init_db()
            conn = _conn
        row = conn.execute("SELECT * FROM coop_teams WHERE id=?", (team_id,)).fetchone()
    return None if row is None else _row_to_team(row)


def load_coop_team_by_code_conn(conn, code):
    row = conn.execute("SELECT * FROM coop_teams WHERE join_code=?", (code,)).fetchone()
    return None if row is None else _row_to_team(row)


def load_coop_team_conn(conn, team_id):
    row = conn.execute("SELECT * FROM coop_teams WHERE id=?", (team_id,)).fetchone()
    return None if row is None else _row_to_team(row)


def list_coop_members_conn(conn, team_id):
    rows = conn.execute(
        "SELECT * FROM coop_members WHERE team_id=? ORDER BY joined_seq", (team_id,)
    ).fetchall()
    return [{"id": r["id"], "team_id": r["team_id"], "token": r["token"],
             "name": r["name"], "role": r["role"], "joined_seq": r["joined_seq"]}
            for r in rows]


def get_coop_member_conn(conn, team_id, member_id):
    row = conn.execute(
        "SELECT * FROM coop_members WHERE team_id=? AND id=?", (team_id, member_id)
    ).fetchone()
    if row is None:
        return None
    return {"id": row["id"], "team_id": row["team_id"], "token": row["token"],
            "name": row["name"], "role": row["role"], "joined_seq": row["joined_seq"]}


def get_coop_member_by_token_conn(conn, token):
    """按入会令牌定位成员（跨队唯一）；查不到返回 None。"""
    if not token:
        return None
    row = conn.execute("SELECT * FROM coop_members WHERE token=?", (token,)).fetchone()
    if row is None:
        return None
    return {"id": row["id"], "team_id": row["team_id"], "token": row["token"],
            "name": row["name"], "role": row["role"], "joined_seq": row["joined_seq"]}


def update_coop_member_role_conn(conn, team_id, member_id, role):
    cur = conn.execute(
        "UPDATE coop_members SET role=? WHERE team_id=? AND id=?",
        (role, team_id, member_id))
    return cur.rowcount > 0


def delete_coop_member_conn(conn, team_id, member_id):
    cur = conn.execute(
        "DELETE FROM coop_members WHERE team_id=? AND id=?", (team_id, member_id))
    return cur.rowcount > 0


def save_coop_team_conn(conn, team_id, status=None, leader_id=None, seed=None,
                        chapters_total=None, expedition_id=None, name=None,
                        expected_rev=None, bump_rev=True):
    """更新队伍（只覆盖非 None 字段；开赛绑定远征时一次性写入）。

    expected_rev 非 None 时做乐观检查；bump_rev=False 仅改非版本字段
    （当前没有该场景，保留与远征更新同构的语义）。
    """
    cur = conn.execute("SELECT * FROM coop_teams WHERE id=?", (team_id,)).fetchone()
    if cur is None:
        raise ConcurrentModification(f"coop team {team_id} not found")
    new_status = cur["status"] if status is None else status
    new_leader = cur["leader_id"] if leader_id is None else leader_id
    new_seed = cur["seed"] if seed is None else seed
    new_chapters = cur["chapters_total"] if chapters_total is None else chapters_total
    new_exp = cur["expedition_id"] if expedition_id is None else expedition_id
    new_name = cur["name"] if name is None else name
    rev_clause = "AND rev=?" if expected_rev is not None else ""
    params = [new_status, new_leader, new_seed, new_chapters, new_exp, new_name, team_id]
    if bump_rev:
        sql = ("UPDATE coop_teams SET status=?, leader_id=?, seed=?, chapters_total=?, "
               "expedition_id=?, name=?, rev=rev+1, updated_at=datetime('now') "
               "WHERE id=? " + rev_clause)
    else:
        sql = ("UPDATE coop_teams SET status=?, leader_id=?, seed=?, chapters_total=?, "
               "expedition_id=?, name=?, updated_at=datetime('now') "
               "WHERE id=? " + rev_clause)
    if expected_rev is not None:
        params.append(expected_rev)
    res = conn.execute(sql, params)
    if res.rowcount == 0:
        raise ConcurrentModification(
            f"coop team {team_id} changed concurrently (rev {expected_rev})")


def bind_expedition_to_team_conn(conn, team_id, expedition_id, seed, chapters_total,
                                 expected_rev):
    """开赛：forming -> started 并绑定远征（乐观版本守卫，原子）。"""
    cur = conn.execute(
        "UPDATE coop_teams SET status='started', expedition_id=?, seed=?, "
        "chapters_total=?, rev=rev+1, updated_at=datetime('now') "
        "WHERE id=? AND status='forming' AND rev=?",
        (expedition_id, seed, chapters_total, team_id, expected_rev))
    if cur.rowcount == 0:
        raise ConcurrentModification(f"coop team {team_id} cannot start (rev/state)")


def next_coop_seq_conn(conn, team_id):
    row = conn.execute(
        "SELECT COALESCE(MAX(seq),0) AS m FROM coop_events WHERE team_id=?", (team_id,)
    ).fetchone()
    return row["m"] + 1


def append_coop_event_conn(conn, team_id, seq, kind, payload):
    conn.execute(
        "INSERT INTO coop_events(team_id,seq,kind,payload_json) VALUES(?,?,?,?)",
        (team_id, seq, kind, json.dumps(payload, ensure_ascii=False)),
    )


def load_coop_events(team_id):
    with _lock:
        conn = _conn
        if conn is None:
            init_db()
            conn = _conn
        rows = conn.execute(
            "SELECT seq, kind, payload_json FROM coop_events WHERE team_id=? ORDER BY seq",
            (team_id,)).fetchall()
    out = []
    for r in rows:
        raw = r["payload_json"]
        try:
            payload = json.loads(raw) if raw is not None else {}
        except (ValueError, TypeError):
            payload = {"_corrupt": True}
        if not isinstance(payload, dict):
            payload = {"_corrupt": True}
        out.append({"seq": r["seq"], "kind": r["kind"], "payload": payload})
    return out


def next_ledger_seq_conn(conn, team_id):
    row = conn.execute(
        "SELECT COALESCE(MAX(seq),0) AS m FROM coop_ledger WHERE team_id=?", (team_id,)
    ).fetchone()
    return row["m"] + 1


def append_ledger_conn(conn, team_id, seq, member_id, kind, amount, chapter, payload):
    conn.execute(
        "INSERT INTO coop_ledger(team_id,seq,member_id,kind,amount,chapter,payload_json,"
        "created_at) VALUES(?,?,?,?,?,?,?,datetime('now'))",
        (team_id, seq, member_id, kind, amount, chapter,
         json.dumps(payload, ensure_ascii=False)),
    )


def list_coop_ledger_conn(conn, team_id):
    rows = conn.execute(
        "SELECT seq, member_id, kind, amount, chapter, payload_json FROM coop_ledger "
        "WHERE team_id=? ORDER BY seq", (team_id,)).fetchall()
    out = []
    for r in rows:
        try:
            payload = json.loads(r["payload_json"])
        except (ValueError, TypeError):
            payload = {"_corrupt": True}
        out.append({"seq": r["seq"], "member_id": r["member_id"], "kind": r["kind"],
                    "amount": r["amount"], "chapter": r["chapter"], "payload": payload})
    return out


def list_all_join_codes_conn(conn):
    """现存全部入队码（生成新码时去重；forming 与 started 的码都占命名空间）。"""
    return {r["join_code"] for r in conn.execute("SELECT join_code FROM coop_teams").fetchall()}


def get_coop_idempotent_conn(conn, team_id, request_id):
    if not request_id:
        return None
    row = conn.execute(
        "SELECT seq, response_json FROM coop_requests WHERE team_id=? AND request_id=?",
        (team_id, request_id)).fetchone()
    if row is None:
        return None
    return {"seq": row["seq"], "response": json.loads(row["response_json"])}


def put_coop_idempotent_conn(conn, team_id, request_id, seq, response):
    if not request_id:
        return
    conn.execute(
        "INSERT INTO coop_requests(team_id,request_id,seq,response_json,created_at) "
        "VALUES(?,?,?,?,datetime('now'))",
        (team_id, request_id, seq, json.dumps(response, ensure_ascii=False)))
