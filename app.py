"""Agent Chat - chat minimale per agenti IA + umani. Solo stdlib.

Chi scrive:
- l'umano (owner) dalla pagina web, dopo il login: il nome è CHAT_OWNER, verificato;
- un agente con il suo token personale (creato dall'owner in «Agenti»): il nome lo
  decide il server; un agente può firmare anche i suoi sotto-nomi «nome/altro»
  (per esempio un EasyIsland con più motori: «notebook/claude», «notebook/codex»);
- il vecchio token unico CHAT_TOKEN (facoltativo, per gli agenti non ancora
  passati al token personale): il nome è dichiarato, il messaggio è «non verificato».

Presa in carico: un agente prenota un messaggio prima di lavorarci
(POST /api/messages/<id>/claim); il primo lo prende, gli altri ricevono 409 e chi
l'ha preso. Così due PC non fanno lo stesso lavoro.
Nomi: un agente dà un nome ai suoi sotto-nomi («casa/claude» → «lia»,
POST /api/aliases); la pagina e gli agenti scrivono @lia. Unici in tutta la chat.
Chi c'è: GET /api/presence, online = in ascolto adesso o visto da pochi secondi.
Scrivere come l'owner da un'app (EasyIsland): un token owner creato dalla pagina
(«Token per scrivere come te», POST /api/owner-tokens); il messaggio è dell'owner,
verificato, con «human» = 1, anche se firmato con un nome scelto nell'app.
Stato degli agenti, senza storico: ogni agente dice cosa fa con un messaggio
(POST /api/status: in coda, ragiona, usa strumenti, scrive, fatto, nessuna risposta);
GET /api/status dà chi sta lavorando, le prese in carico recenti e chi non ha risposto.
Memoria condivisa: un agente carica la sua (PUT /api/memory), gli altri ne leggono la
copia (GET /api/memory/<agente>), la interrogano dal vivo se è acceso
(POST /api/memory/<agente>/ask, risposta entro pochi secondi o 504) e gli lasciano note
(POST /api/memory/<agente>/note), che lui applica appena è in ascolto
(GET /api/memory/requests, POST /api/memory/requests/<id>).
Incarichi (/api/tasks): un lavoro con chi lo fa, scadenza e stato; con un assegnatario il server
scrive «📋 Incarico #N» e le risposte nel suo thread lo fanno avanzare («FATTO:», «BLOCCATO:»).
Programmate (/api/schedules): il server scrive un messaggio a un'ora, nei giorni scelti.
Capacità (/api/capabilities): cosa sa fare ogni agente. Un messaggio preso da un agente che si
scollega senza rispondere torna libero dopo 3 minuti, con un messaggio di «sistema» al
coordinatore (jeeves.py, un agente con un modello via OpenRouter).
"""
import hashlib, hmac, json, os, re, secrets, sqlite3, threading, time
from urllib.parse import quote, unquote
import urllib.error, urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

PASSWORD = os.environ["CHAT_PASSWORD"]               # login web dell'umano
LEGACY = os.environ.get("CHAT_TOKEN", "")            # vecchio token unico (facoltativo)
OWNER = os.environ.get("CHAT_OWNER", "umano").strip().lower() or "umano"
SECRET = hashlib.sha256((os.environ.get("CHAT_SECRET", "") + LEGACY + PASSWORD).encode()).digest()
SESSION_DAYS = 30
DB = os.environ.get("CHAT_DB", "/data/chat.db")
KINDS = ("msg", "consult", "synthesis")
# Attachments: files next to the database, at most CHAT_MAX_FILE_MB each (20 MB),
# 10 per message. Shown in the page only when they are harmless (pictures, PDF, text).
FILES = os.path.join(os.path.dirname(os.path.abspath(DB)), "files")
MAX_FILE = int(os.environ.get("CHAT_MAX_FILE_MB", "20")) * 1024 * 1024
INLINE = {"image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf", "text/plain"}
FILE_ID = re.compile(r"^[A-Za-z0-9_-]{16,40}$")
NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")
_here = os.path.dirname(__file__)
INDEX = open(os.path.join(_here, "index.html"), "rb").read()
LOGIN = open(os.path.join(_here, "login.html"), "rb").read()
# The PWA (logo, icons, manifest, service worker): public, loaded once at start.
_TYPES = {".svg": "image/svg+xml", ".png": "image/png", ".js": "text/javascript", ".webmanifest": "application/manifest+json"}
STATIC = {}
_static = os.path.join(_here, "static")
for _f in (os.listdir(_static) if os.path.isdir(_static) else []):
    _ext = os.path.splitext(_f)[1]
    if _ext in _TYPES:
        STATIC["/static/" + _f] = (open(os.path.join(_static, _f), "rb").read(), _TYPES[_ext])
# The service worker lives at the root, so that it covers the whole site.
for _alias, _f in (("/sw.js", "/static/sw.js"), ("/manifest.webmanifest", "/static/manifest.webmanifest")):
    if _f in STATIC:
        STATIC[_alias] = STATIC[_f]


# Wrong passwords: an address that misses LOGIN_TRIES times in LOGIN_WINDOW seconds
# waits until the window passes; all addresses together at most LOGIN_ALL (spread
# attempts). The 2 s wait alone did not slow attempts sent in parallel.
LOGIN_TRIES = 5
LOGIN_ALL = 30
LOGIN_WINDOW = 15 * 60
_misses = {}          # address -> times of the wrong passwords
_misses_lock = threading.Lock()


def _recent(times, now):
    return [t for t in times if now - t < LOGIN_WINDOW]


def login_wait(addr):
    """Seconds this address must still wait before trying again (0 = may try)."""
    now = time.time()
    with _misses_lock:
        for a in list(_misses):
            _misses[a] = _recent(_misses[a], now)
            if not _misses[a]:
                del _misses[a]
        mine = _misses.get(addr, [])
        every = sorted(t for ts in _misses.values() for t in ts)
        waits = []
        if len(mine) >= LOGIN_TRIES:
            waits.append(mine[-LOGIN_TRIES] + LOGIN_WINDOW - now)
        if len(every) >= LOGIN_ALL:
            waits.append(every[-LOGIN_ALL] + LOGIN_WINDOW - now)
        return max([0] + [int(w) + 1 for w in waits])


def login_missed(addr):
    with _misses_lock:
        _misses.setdefault(addr, []).append(time.time())


def sign(exp):
    return hmac.new(SECRET, str(exp).encode(), hashlib.sha256).hexdigest()


def valid_session(cookie_header):
    for part in (cookie_header or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == "ac_s":
            exp, _, sig = v.partition(".")
            if exp.isdigit() and int(exp) > time.time() and hmac.compare_digest(sig, sign(exp)):
                return True
    return False


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


db = sqlite3.connect(DB, check_same_thread=False)
db.execute("PRAGMA journal_mode=WAL")
db.execute("""CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, room TEXT NOT NULL, sender TEXT NOT NULL,
  text TEXT NOT NULL, ts REAL NOT NULL)""")
cols = {r[1] for r in db.execute("PRAGMA table_info(messages)")}
if "verified" not in cols:
    db.execute("ALTER TABLE messages ADD COLUMN verified INTEGER NOT NULL DEFAULT 0")
if "thread" not in cols:
    db.execute("ALTER TABLE messages ADD COLUMN thread INTEGER")
if "kind" not in cols:
    db.execute("ALTER TABLE messages ADD COLUMN kind TEXT NOT NULL DEFAULT 'msg'")
if "files" not in cols:
    db.execute("ALTER TABLE messages ADD COLUMN files TEXT")
if "human" not in cols:
    db.execute("ALTER TABLE messages ADD COLUMN human INTEGER NOT NULL DEFAULT 0")
    db.execute("UPDATE messages SET human=1 WHERE sender=?", (OWNER,))
db.execute("""CREATE TABLE IF NOT EXISTS files(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL, size INTEGER NOT NULL,
  sender TEXT NOT NULL, ts REAL NOT NULL, message INTEGER)""")
fcols = {r[1] for r in db.execute("PRAGMA table_info(files)")}
for col, kind in (("sha256", "TEXT"), ("imported_by", "TEXT"), ("imported_at", "REAL")):
    if col not in fcols:
        db.execute(f"ALTER TABLE files ADD COLUMN {col} {kind}")
os.makedirs(FILES, exist_ok=True)
db.execute("CREATE INDEX IF NOT EXISTS ix_room ON messages(room, id)")
db.execute("CREATE INDEX IF NOT EXISTS ix_thread ON messages(thread, id)")
db.execute("""CREATE TABLE IF NOT EXISTS claims(
  message INTEGER PRIMARY KEY, by TEXT NOT NULL, ts REAL NOT NULL)""")
db.execute("""CREATE TABLE IF NOT EXISTS aliases(
  alias TEXT PRIMARY KEY, handle TEXT NOT NULL UNIQUE)""")
db.execute("""CREATE TABLE IF NOT EXISTS memory(
  agent TEXT PRIMARY KEY, rev INTEGER NOT NULL, ts REAL NOT NULL, data TEXT NOT NULL)""")
db.execute("""CREATE TABLE IF NOT EXISTS memreq(
  id INTEGER PRIMARY KEY AUTOINCREMENT, agent TEXT NOT NULL, sender TEXT NOT NULL, kind TEXT NOT NULL,
  body TEXT NOT NULL, ts REAL NOT NULL, answer TEXT, done INTEGER NOT NULL DEFAULT 0)""")
db.execute("""CREATE TABLE IF NOT EXISTS agents(
  name TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE, created REAL NOT NULL, last_seen REAL)""")
db.execute("""CREATE TABLE IF NOT EXISTS owner_tokens(
  label TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE, created REAL NOT NULL, last_seen REAL)""")
# What each agent can do (its integrations, engines, folders), as it says itself.
db.execute("""CREATE TABLE IF NOT EXISTS capabilities(
  agent TEXT PRIMARY KEY, ts REAL NOT NULL, data TEXT NOT NULL)""")
# Assignments: a piece of work with who does it, its state and its result. «message» is
# the chat message that hands it out: the replies in its thread are its progress.
db.execute("""CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
  assignee TEXT, state TEXT NOT NULL DEFAULT 'todo', due REAL, created_by TEXT NOT NULL,
  created REAL NOT NULL, updated REAL NOT NULL, result TEXT, message INTEGER, room TEXT NOT NULL DEFAULT 'general',
  reminded REAL)""")
# Scheduled requests: the server writes «text» at «at» (HH:MM, local time) on «days»
# (1 = Monday … 7 = Sunday). By the owner they are the owner's messages; by an agent, its own.
db.execute("""CREATE TABLE IF NOT EXISTS schedules(
  id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, at TEXT NOT NULL, days TEXT NOT NULL DEFAULT '1234567',
  room TEXT NOT NULL DEFAULT 'general', enabled INTEGER NOT NULL DEFAULT 1, created_by TEXT NOT NULL,
  created REAL NOT NULL, last_run TEXT)""")
# Claims let go because their agent went away without answering (once per message).
db.execute("CREATE TABLE IF NOT EXISTS released(message INTEGER PRIMARY KEY, by TEXT NOT NULL, ts REAL NOT NULL)")
# The server's own settings, changed from the page (the coordinator's model and key…).
db.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
db.commit()
lock = threading.Lock()
cond = threading.Condition(lock)
# Who is listening right now (long polls open), for /api/presence.
listening = {}
ONLINE_SECS = 15     # seen this recently = online, even between two polls
ASK_SECS = 8         # a live question to another agent's memory waits this much
MAX_MEMORY = 20 * 1024 * 1024
FIELDS = ("id", "room", "sender", "text", "ts", "verified", "thread", "kind", "files", "human", "claimed_by")
# What each agent is doing now (POST /api/status), in memory only: handle ->
# {message, state, detail, since, ts}. Gone after ACTIVE_SECS without news.
activity = {}
ACTIVE_SECS = 120
WORKING = ("queued", "thinking", "tools", "writing")
# Who had nothing to say or failed, per message, for a while: id -> {handle: state}.
outcomes = {}
OUTCOME_SECS = 1800
EVERYONE = {"tutti", "all", "everyone"}
# The server's own messages (claims let go). Never an agent's or the owner's name.
SYSTEM = "sistema"
# The coordinator agent (jeeves.py): told when a claim is let go.
COORDINATOR = os.environ.get("CHAT_COORDINATOR", "jeeves").strip().lower()
TASK_STATES = ("todo", "doing", "done", "blocked", "cancelled")
TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
# A claim whose agent is gone this long without a word in the thread is let go.
RELEASE_SECS = 180


def rows_to_msgs(rows):
    out = []
    for r in rows:
        m = dict(zip(FIELDS, r))
        m["verified"] = bool(m["verified"])
        m["human"] = bool(m["human"])
        try:
            m["files"] = json.loads(m["files"]) if m["files"] else []
        except ValueError:
            m["files"] = []
        out.append(m)
    # Who imported each attachment, as it is now.
    ids = [f["id"] for m in out for f in m["files"] if isinstance(f, dict) and "id" in f]
    if ids:
        got = dict(db.execute("SELECT id, imported_by FROM files WHERE id IN (%s)" % ",".join("?" * len(ids)), ids).fetchall())
        for m in out:
            for f in m["files"]:
                f["imported_by"] = got.get(f.get("id"))
    return out


def fetch(rooms, after, limit, thread=None):
    sql = ("SELECT id, room, sender, text, ts, verified, thread, kind, files, human, "
           "(SELECT by FROM claims WHERE message=messages.id) FROM messages WHERE id>?")
    args = [after]
    if thread is not None:
        sql += " AND (id=? OR thread=?)"
        args += [thread, thread]
    if rooms is not None:
        sql += " AND room IN (%s)" % ",".join("?" * len(rooms))
        args += rooms
    sql += " ORDER BY id LIMIT ?"
    args.append(limit)
    return rows_to_msgs(db.execute(sql, args).fetchall())


def last_id(rooms):
    if rooms is None:
        return db.execute("SELECT MAX(id) FROM messages").fetchone()[0] or 0
    sql = "SELECT MAX(id) FROM messages WHERE room IN (%s)" % ",".join("?" * len(rooms))
    return db.execute(sql, rooms).fetchone()[0] or 0


def parse_rooms(q):
    """rooms=a,b | rooms=* (tutte) | room=a (default general). None = tutte."""
    raw = q.get("rooms", [""])[0] or q.get("room", ["general"])[0]
    if raw.strip() == "*":
        return None
    rooms = [r.strip()[:64] for r in raw.split(",") if r.strip()]
    return rooms[:20] or ["general"]


def agent_names():
    return {r[0] for r in db.execute("SELECT name FROM agents")}


def aliases():
    return dict(db.execute("SELECT alias, handle FROM aliases ORDER BY alias").fetchall())


def presence():
    now = time.time()
    rows = db.execute("SELECT name, last_seen FROM agents ORDER BY name").fetchall()
    return {n: {"online": listening.get(n, 0) > 0 or now - (s or 0) < ONLINE_SECS, "last_seen": s} for n, s in rows}


def status_now():
    """Who works on what now, recent claims and the agents that did not answer."""
    now = time.time()
    for h in [h for h, a in activity.items() if now - a["ts"] > ACTIVE_SECS]:
        activity.pop(h, None)
    for mid in [m for m, o in outcomes.items() if now - o.get("_ts", 0) > OUTCOME_SECS]:
        outcomes.pop(mid, None)
    claims = {str(m): b for m, b in db.execute("SELECT message, by FROM claims WHERE ts>?", (now - OUTCOME_SECS,)).fetchall()}
    return {
        "active": [dict(by=h, **{k: v for k, v in a.items() if k != "ts"}) for h, a in sorted(activity.items())],
        "outcomes": {str(m): {h: s for h, s in o.items() if h != "_ts"} for m, o in outcomes.items()},
        "claims": claims,
        "online": {n: p["online"] for n, p in presence().items()},
        "now": now,
    }


TASK_FIELDS = ("id", "title", "detail", "assignee", "state", "due", "created_by", "created", "updated", "result", "message", "room")
SCHEDULE_FIELDS = ("id", "text", "at", "days", "room", "enabled", "created_by", "created", "last_run")


def tasks_list(open_only):
    sql = "SELECT %s FROM tasks" % ", ".join(TASK_FIELDS)
    if open_only:
        sql += " WHERE state IN ('todo','doing','blocked')"
    sql += " ORDER BY id DESC LIMIT 500"
    return [dict(zip(TASK_FIELDS, r)) for r in db.execute(sql).fetchall()]


def schedules_list():
    rows = db.execute("SELECT %s FROM schedules ORDER BY at, id" % ", ".join(SCHEDULE_FIELDS)).fetchall()
    out = [dict(zip(SCHEDULE_FIELDS, r)) for r in rows]
    for s in out:
        s["enabled"] = bool(s["enabled"])
    return out


def insert_message(room, sender, text, verified, thread=None, kind="msg", human=0):
    """A message written by the server itself (scheduled requests, claims let go).
    The caller holds «cond» and notifies."""
    cur = db.execute("INSERT INTO messages(room, sender, text, ts, verified, thread, kind, human) VALUES(?,?,?,?,?,?,?,?)",
                     (room, sender, text, time.time(), verified, thread, kind, human))
    return cur.lastrowid


def task_progress(thread, sender, text):
    """A reply in the thread of an assignment moves it: «FATTO…» done, «BLOCCATO…» blocked,
    anything else by whoever does it = in progress. The caller holds the lock."""
    row = db.execute("SELECT id, assignee, state FROM tasks WHERE message=?", (thread,)).fetchone()
    if not row:
        return
    tid, assignee, state = row
    head = text.lstrip().upper()
    who = sender.split("/")[0]
    mine = assignee and (assignee == sender or assignee.split("/")[0] == who)
    if head.startswith("FATTO"):
        db.execute("UPDATE tasks SET state='done', result=?, updated=? WHERE id=?", (text.strip()[:4000], time.time(), tid))
    elif head.startswith("BLOCCATO"):
        db.execute("UPDATE tasks SET state='blocked', result=?, updated=? WHERE id=?", (text.strip()[:4000], time.time(), tid))
    elif mine and state == "todo":
        db.execute("UPDATE tasks SET state='doing', updated=? WHERE id=?", (time.time(), tid))


def run_schedules(now=None):
    """The scheduled requests due this minute, each once a day."""
    t = time.localtime(now)
    hhmm, day, today = time.strftime("%H:%M", t), str(t.tm_wday + 1), time.strftime("%Y-%m-%d", t)
    fired = 0
    for sid, text, days, room, by, last in db.execute(
            "SELECT id, text, days, room, created_by, last_run FROM schedules WHERE enabled=1 AND at=?", (hhmm,)).fetchall():
        if day not in days or last == today:
            continue
        mine = by == OWNER
        insert_message(room, by, "⏰ " + text, 1, human=1 if mine else 0)
        db.execute("UPDATE schedules SET last_run=? WHERE id=?", (today, sid))
        fired += 1
    return fired


def release_claims(now=None):
    """Claims of agents gone away without a word in the thread go back, with a message
    for the coordinator (once per message, only for the last day)."""
    now = now or time.time()
    online = {n for n, s in db.execute("SELECT name, last_seen FROM agents").fetchall()
              if listening.get(n, 0) > 0 or now - (s or 0) < ONLINE_SECS}
    let_go = 0
    rows = db.execute("SELECT c.message, c.by, c.ts, m.room FROM claims c JOIN messages m ON m.id=c.message "
                      "WHERE c.ts<? AND c.ts>? AND c.message NOT IN (SELECT message FROM released)",
                      (now - RELEASE_SECS, now - 86400)).fetchall()
    for mid, by, ts, room in rows:
        base = by.split("/")[0]
        if base in online:
            continue
        seen = db.execute("SELECT last_seen FROM agents WHERE name=?", (base,)).fetchone()
        if seen and seen[0] and now - seen[0] < RELEASE_SECS:
            continue
        said = db.execute("SELECT 1 FROM messages WHERE thread=? AND (sender=? OR sender LIKE ?) AND ts>=?",
                          (mid, base, base + "/%", ts)).fetchone()
        if said:
            continue
        db.execute("DELETE FROM claims WHERE message=?", (mid,))
        db.execute("INSERT OR IGNORE INTO released(message, by, ts) VALUES(?,?,?)", (mid, by, now))
        insert_message(room, SYSTEM, f"↻ @{COORDINATOR} il messaggio #{mid} era stato preso da {by}, che si è scollegato "
                                     "senza rispondere: è di nuovo libero.", 1, thread=mid)
        let_go += 1
    return let_go


# ── The coordinator's settings ───────────────────────────────────────────────
# Chosen in the page (menu → Jeeves) by the owner and read by jeeves.py. The OpenRouter key
# is kept here, in the database (data/, readable only by root on the server): the page only
# learns whether it is set, the coordinator alone reads it.
COORD_DEFAULTS = {"on": "0", "model": "anthropic/claude-haiku-5.5", "fallback": "openai/gpt-6-luna",
                  "report_at": "19:00", "daily_tokens": "2000000", "key": ""}
OPENROUTER = os.environ.get("OPENROUTER_BASE", "https://openrouter.ai/api/v1")
# The coordinator's token: made by the server at start and left in data/ for jeeves.py
# (same volume), so nobody has to copy it anywhere.
COORD_TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(DB)), "jeeves.token")
coord_status = {}      # what jeeves.py last said about itself (POST /api/coordinator/status)
_models = {"at": 0, "list": []}


def setting(key, default=""):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def coord_config(with_key=False):
    c = {k: setting("coord_" + k, v) for k, v in COORD_DEFAULTS.items()}
    out = {"on": c["on"] == "1", "model": c["model"], "fallback": c["fallback"], "report_at": c["report_at"],
           "daily_tokens": int(c["daily_tokens"] or 0), "has_key": bool(c["key"]), "name": COORDINATOR}
    if with_key:
        out["key"] = c["key"]
    return out


def coord_token():
    """A new token for the coordinator at each start, in data/jeeves.token (0600)."""
    token = "ac_" + secrets.token_urlsafe(24)
    db.execute("INSERT INTO agents(name, token_hash, created) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET "
               "token_hash=excluded.token_hash, created=excluded.created", (COORDINATOR, token_hash(token), time.time()))
    db.commit()
    fd = os.open(COORD_TOKEN_FILE + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token)
    os.replace(COORD_TOKEN_FILE + ".tmp", COORD_TOKEN_FILE)


def openrouter(path, key=None, timeout=20):
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    req = urllib.request.Request(OPENROUTER + path, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def models_list():
    """OpenRouter's models that use tools, with their prices per million tokens (cached 1 h)."""
    if time.time() - _models["at"] < 3600 and _models["list"]:
        return _models["list"]
    out = []
    for m in openrouter("/models", timeout=30).get("data", []):
        if "tools" not in (m.get("supported_parameters") or []):
            continue
        p = m.get("pricing") or {}
        try:
            pin, pout = float(p.get("prompt") or 0) * 1e6, float(p.get("completion") or 0) * 1e6
        except ValueError:
            continue
        if pin < 0 or pout < 0:
            continue
        out.append({"id": m["id"], "name": m.get("name") or m["id"], "in": round(pin, 4), "out": round(pout, 4),
                    "context": m.get("context_length"), "created": m.get("created") or 0})
    out.sort(key=lambda x: x["id"])
    _models.update(at=time.time(), list=out)
    return out


def ticker():
    """Every 20 s: scheduled requests and claims let go."""
    while True:
        time.sleep(20)
        try:
            with cond:
                n = run_schedules() + release_claims()
                if n:
                    db.commit()
                    cond.notify_all()
        except Exception as e:  # never stop the server for this
            print("ticker:", e, flush=True)


class Listening:
    """While an agent waits in a long poll it is online."""
    def __init__(self, who):
        self.name = who["name"] if who["kind"] == "agent" else None

    def __enter__(self):
        if self.name:
            listening[self.name] = listening.get(self.name, 0) + 1

    def __exit__(self, *a):
        if self.name:
            listening[self.name] = max(listening.get(self.name, 1) - 1, 0)


class H(BaseHTTPRequestHandler):
    def _send(self, code, obj=None, body=None, ctype="application/json", cache="no-store"):
        body = body if body is not None else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("" if ctype.startswith("image/png") else "; charset=utf-8"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def _client(self):
        """The visitor's address. Behind nginx (the container listens on 127.0.0.1
        only) it is the last X-Forwarded-For entry, the one nginx added itself:
        the ones before it come from the visitor and can be made up."""
        peer = self.client_address[0]
        fwd = self.headers.get("X-Forwarded-For", "")
        if fwd and (peer.startswith("127.") or peer == "::1" or peer.startswith("172.") or peer.startswith("10.") or peer.startswith("192.168.")):
            return fwd.split(",")[-1].strip() or peer
        return peer

    def _redirect(self, to, cookie=None):
        self.send_response(303)
        self.send_header("Location", to)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _who(self):
        """Chi fa la richiesta: {"kind": owner|agent|legacy, "name"} oppure None."""
        if valid_session(self.headers.get("Cookie")):
            return {"kind": "owner", "name": OWNER}
        h = self.headers.get("Authorization", "")
        tok = h[7:].strip() if h.startswith("Bearer ") else ""
        if not tok:
            return None
        with lock:
            row = db.execute("SELECT name FROM agents WHERE token_hash=?", (token_hash(tok),)).fetchone()
            if row:
                db.execute("UPDATE agents SET last_seen=? WHERE name=?", (time.time(), row[0]))
                db.commit()
                return {"kind": "agent", "name": row[0]}
            row = db.execute("SELECT label FROM owner_tokens WHERE token_hash=?", (token_hash(tok),)).fetchone()
            if row:
                db.execute("UPDATE owner_tokens SET last_seen=? WHERE label=?", (time.time(), row[0]))
                db.commit()
                return {"kind": "owner", "name": OWNER, "via": row[0]}
        if LEGACY and hmac.compare_digest(tok, LEGACY):
            return {"kind": "legacy", "name": ""}
        return None

    def _auth(self):
        who = self._who()
        if who is None:
            self._send(401, {"error": "unauthorized"})
        return who

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n > 1_000_000:
            raise ValueError("troppo grande")
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        if u.path == "/":
            ok = valid_session(self.headers.get("Cookie"))
            return self._send(200, body=INDEX if ok else LOGIN, ctype="text/html")
        if u.path == "/logout":
            return self._redirect("/", "ac_s=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Strict")
        if u.path == "/health":
            return self._send(200, {"ok": True})
        if u.path in STATIC:
            body, ctype = STATIC[u.path]
            # The service worker and the manifest are checked again at each visit; icons for a day.
            fresh = u.path in ("/sw.js", "/manifest.webmanifest", "/static/sw.js", "/static/manifest.webmanifest")
            return self._send(200, body=body, ctype=ctype, cache="no-cache" if fresh else "public, max-age=86400")
        who = self._auth()
        if not who:
            return
        if u.path == "/api/me":
            return self._send(200, {"name": who["name"], "kind": who["kind"], "verified": who["kind"] != "legacy", "owner": OWNER})
        if u.path == "/api/messages":
            try:
                rooms = parse_rooms(q)
                after = int(q.get("after", ["0"])[0])
                limit = max(1, min(int(q.get("limit", ["200"])[0]), 1000))
                wait = max(0.0, min(float(q.get("wait", ["0"])[0]), 60))
                thread = int(q["thread"][0]) if q.get("thread") else None
            except ValueError:
                return self._send(400, {"error": "parametri non validi"})
            if after < 0:  # after=-N -> ultimi N messaggi
                with lock:
                    after = max(last_id(rooms) + after, 0)
            deadline = time.time() + wait
            with cond, Listening(who):
                msgs = fetch(rooms, after, limit, thread)
                while not msgs and time.time() < deadline:
                    cond.wait(deadline - time.time())
                    msgs = fetch(rooms, after, limit, thread)
            return self._send(200, msgs)
        if u.path == "/api/rooms":
            with lock:
                rows = db.execute("SELECT room, COUNT(*), MAX(id) FROM messages GROUP BY room").fetchall()
            return self._send(200, [{"room": r, "count": c, "last_id": m} for r, c, m in rows])
        if u.path.startswith("/api/files/"):
            return self._get_file(u.path[len("/api/files/"):])
        if u.path == "/api/aliases":
            with lock:
                return self._send(200, aliases())
        if u.path == "/api/presence":
            with lock:
                return self._send(200, presence())
        if u.path == "/api/status":
            with lock:
                return self._send(200, status_now())
        if u.path == "/api/coordinator":
            mine = who["kind"] == "agent" and who["name"] == COORDINATOR
            if not (mine or self._page_owner(who)):
                return self._send(403, {"error": "solo dalla pagina web o dal coordinatore"})
            with lock:
                out = coord_config(with_key=mine)
            out["status"] = coord_status
            return self._send(200, out)
        if u.path == "/api/coordinator/models":
            if not self._page_owner(who):
                return self._send(403, {"error": "solo dalla pagina web"})
            try:
                return self._send(200, models_list())
            except Exception as e:
                return self._send(502, {"error": f"OpenRouter non risponde: {e}"})
        if u.path == "/api/capabilities":
            with lock:
                rows = db.execute("SELECT agent, ts, data FROM capabilities ORDER BY agent").fetchall()
            return self._send(200, {a: dict(json.loads(d), ts=ts) for a, ts, d in rows})
        if u.path == "/api/tasks":
            with lock:
                return self._send(200, tasks_list(q.get("state", ["open"])[0] != "all"))
        if u.path == "/api/schedules":
            with lock:
                return self._send(200, schedules_list())
        if u.path == "/api/owner-tokens":
            if who["kind"] != "owner" or who.get("via"):
                return self._send(403, {"error": "solo dalla pagina web"})
            with lock:
                rows = db.execute("SELECT label, created, last_seen FROM owner_tokens ORDER BY label").fetchall()
            return self._send(200, [{"label": n, "created": c, "last_seen": s} for n, c, s in rows])
        if u.path == "/api/memory/requests":
            return self._memory_requests(who, q)
        if u.path.startswith("/api/memory/"):
            return self._get_memory(u.path[len("/api/memory/"):], q)
        if u.path == "/api/agents":
            if who["kind"] != "owner" or who.get("via"):
                return self._send(403, {"error": "solo dalla pagina web"})
            with lock:
                rows = db.execute("SELECT name, created, last_seen FROM agents ORDER BY name").fetchall()
            return self._send(200, [{"name": n, "created": c, "last_seen": s} for n, c, s in rows])
        self._send(404, {"error": "not found"})

    def do_DELETE(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        who = self._auth()
        if not who:
            return
        if u.path == "/api/schedules":
            return self._delete_schedule(who, q.get("id", [""])[0])
        if u.path not in ("/api/agents", "/api/owner-tokens"):
            return self._send(404, {"error": "not found"})
        if who["kind"] != "owner" or who.get("via"):
            return self._send(403, {"error": "solo dalla pagina web"})
        name = q.get("name", [""])[0]
        if u.path == "/api/agents" and name == COORDINATOR:
            return self._send(400, {"error": f"«{COORDINATOR}» è il coordinatore: si spegne da menu → Jeeves"})
        table, col = ("agents", "name") if u.path == "/api/agents" else ("owner_tokens", "label")
        with lock:
            cur = db.execute(f"DELETE FROM {table} WHERE {col}=?", (name,))
            db.commit()
        self._send(200 if cur.rowcount else 404, {"deleted": bool(cur.rowcount)})

    def do_PUT(self):
        u = urlparse(self.path)
        who = self._auth()
        if not who:
            return
        if u.path == "/api/capabilities":
            return self._put_capabilities(who)
        if u.path != "/api/memory":
            return self._send(404, {"error": "not found"})
        return self._put_memory(who)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/login":
            n = int(self.headers.get("Content-Length", 0) or 0)
            pw = parse_qs(self.rfile.read(min(n, 10_000)).decode(errors="replace")).get("password", [""])[0]
            addr = self._client()
            wait = login_wait(addr)
            if wait:
                # Not even checked: a right password does not get through either.
                return self._redirect(f"/?wait={(wait + 59) // 60}")
            if not hmac.compare_digest(pw.encode(), PASSWORD.encode()):
                login_missed(addr)
                time.sleep(2)  # rallenta i tentativi a forza bruta
                return self._redirect("/?err=1")
            exp = int(time.time()) + SESSION_DAYS * 86400
            return self._redirect("/", f"ac_s={exp}.{sign(exp)}; Path=/; Max-Age={SESSION_DAYS * 86400}; HttpOnly; Secure; SameSite=Strict")
        who = self._auth()
        if not who:
            return
        if u.path == "/api/agents":
            return self._new_agent(who)
        if u.path == "/api/owner-tokens":
            return self._new_owner_token(who)
        if u.path == "/api/status":
            return self._set_status(who)
        if u.path == "/api/aliases":
            return self._set_aliases(who)
        if u.path == "/api/coordinator":
            return self._set_coordinator(who)
        if u.path == "/api/coordinator/test":
            return self._test_coordinator(who)
        if u.path == "/api/coordinator/status":
            if who["kind"] != "agent" or who["name"] != COORDINATOR:
                return self._send(403, {"error": "solo il coordinatore"})
            try:
                d = self._body()
                coord_status.clear()
                coord_status.update({k: d[k] for k in ("state", "error", "tokens_today", "model", "last_turn") if k in d}, ts=time.time())
            except Exception:
                return self._send(400, {"error": "body JSON richiesto"})
            return self._send(200, {"ok": True})
        if u.path == "/api/tasks":
            return self._new_task(who)
        if u.path.startswith("/api/tasks/") and u.path[len("/api/tasks/"):].isdigit():
            return self._update_task(who, int(u.path[len("/api/tasks/"):]))
        if u.path == "/api/schedules":
            return self._save_schedule(who, None)
        if u.path.startswith("/api/schedules/") and u.path[len("/api/schedules/"):].isdigit():
            return self._save_schedule(who, int(u.path[len("/api/schedules/"):]))
        if u.path.startswith("/api/messages/") and u.path.endswith("/claim"):
            mid = u.path[len("/api/messages/"):-len("/claim")]
            if not mid.isdigit():
                return self._send(404, {"error": "not found"})
            return self._claim(who, int(mid))
        if u.path.startswith("/api/memory/requests/"):
            return self._memory_done(who, u.path[len("/api/memory/requests/"):])
        if u.path.startswith("/api/memory/") and u.path.endswith("/ask"):
            return self._memory_ask(who, u.path[len("/api/memory/"):-len("/ask")])
        if u.path.startswith("/api/memory/") and u.path.endswith("/note"):
            return self._memory_note(who, u.path[len("/api/memory/"):-len("/note")])
        if u.path == "/api/files":
            return self._new_file(who)
        if u.path.startswith("/api/files/") and u.path.endswith("/import"):
            fid = u.path[len("/api/files/"):-len("/import")]
            if not FILE_ID.match(fid):
                return self._send(404, {"error": "not found"})
            return self._import_file(who, fid)
        if u.path != "/api/messages":
            return self._send(404, {"error": "not found"})
        try:
            d = self._body()
            text = str(d["text"])
            room = str(d.get("room") or "general").strip()[:64]
            kind = str(d.get("kind") or "msg")
            thread = d.get("thread")
            thread = int(thread) if thread not in (None, "") else None
            file_ids = [str(f) for f in (d.get("files") or [])][:10]
            assert (text.strip() or file_ids) and room and kind in KINDS
            assert all(FILE_ID.match(f) for f in file_ids)
        except Exception:
            return self._send(400, {"error": "body JSON richiesto: {text, room?, sender?, thread?, kind?, files?}"})
        declared = str(d.get("sender") or "").strip().lower()[:64]
        human = 0
        if who["kind"] == "owner":
            sender, verified, human = OWNER, 1, 1
            # From an app with the owner token: the name chosen there, if it is free.
            if who.get("via") and declared and declared != OWNER:
                with lock:
                    taken = agent_names() | set(aliases()) | EVERYONE | {SYSTEM}
                if not NAME.match(declared) or declared.split("/")[0] in taken:
                    return self._send(403, {"error": f"il nome «{declared}» non è libero: è di un agente o non è valido"})
                sender = declared
        elif who["kind"] == "agent":
            base = who["name"]
            sender = declared or base
            sub = sender[len(base) + 1:] if sender.startswith(base + "/") else None
            if sender != base and not (sub and NAME.match(sub)):
                return self._send(403, {"error": f"puoi scrivere solo come «{base}» o «{base}/…»"})
            verified = 1
        else:
            sender, verified = declared, 0
            with lock:
                taken = agent_names() | {OWNER}
            if not sender or sender.split("/")[0] in taken:
                return self._send(403, {"error": "nome mancante o riservato: usa un token personale"})
        with cond:
            if thread is not None and not db.execute("SELECT 1 FROM messages WHERE id=?", (thread,)).fetchone():
                return self._send(400, {"error": "thread inesistente"})
            # Only one's own uploads, not yet in a message.
            files = []
            for f in file_ids:
                owner_file = OWNER if human else sender.split("/")[0]
                row = db.execute("SELECT id, name, type, size, sha256 FROM files WHERE id=? AND sender=? AND message IS NULL",
                                 (f, owner_file)).fetchone()
                if not row:
                    return self._send(400, {"error": "allegato inesistente o già usato"})
                files.append(dict(zip(("id", "name", "type", "size", "sha256"), row)))
            ts = time.time()
            cur = db.execute(
                "INSERT INTO messages(room, sender, text, ts, verified, thread, kind, files, human) VALUES(?,?,?,?,?,?,?,?,?)",
                (room, sender, text, ts, verified, thread, kind, json.dumps(files) if files else None, human))
            for f in files:
                db.execute("UPDATE files SET message=? WHERE id=?", (cur.lastrowid, f["id"]))
            if thread is not None and verified:
                task_progress(thread, sender, text)
            db.commit()
            cond.notify_all()
        self._send(201, {"id": cur.lastrowid, "room": room, "sender": sender, "text": text, "ts": ts,
                         "verified": bool(verified), "thread": thread, "kind": kind, "files": files, "human": bool(human)})

    def _new_file(self, who):
        """Un allegato: il corpo è il file, il nome in X-File-Name (codificato come URL)."""
        if who["kind"] == "legacy":
            return self._send(403, {"error": "allegati solo con un token personale"})
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n <= 0:
            return self._send(400, {"error": "file vuoto"})
        if n > MAX_FILE:
            return self._send(413, {"error": f"file troppo grande (massimo {MAX_FILE // 1024 // 1024} MB)"})
        name = os.path.basename(unquote(self.headers.get("X-File-Name", "") or "file").replace("\\", "/")).strip()[:120] or "file"
        name = "".join(c for c in name if c.isprintable()) or "file"
        ctype = (self.headers.get("Content-Type") or "application/octet-stream").split(";")[0].strip().lower()[:100]
        data = self.rfile.read(n)
        if len(data) != n:
            return self._send(400, {"error": "file arrivato a metà"})
        digest = hashlib.sha256(data).hexdigest()
        fid = secrets.token_urlsafe(18)
        with open(os.path.join(FILES, fid), "wb") as fh:
            fh.write(data)
        with lock:
            db.execute("INSERT INTO files(id, name, type, size, sender, ts, sha256) VALUES(?,?,?,?,?,?,?)",
                       (fid, name, ctype, len(data), OWNER if who["kind"] == "owner" else who["name"], time.time(), digest))
            db.commit()
        self._send(201, {"id": fid, "name": name, "type": ctype, "size": len(data), "sha256": digest})

    def _declared(self, who, body):
        """Il sotto-nome dichiarato in «sender» se è dell'agente, altrimenti il suo nome."""
        declared = str(body.get("sender") or "").strip().lower()[:64]
        base = who["name"]
        return declared if declared.startswith(base + "/") and NAME.match(declared[len(base) + 1:]) else base

    def _claim(self, who, mid):
        """La presa in carico: il primo agente che prenota il messaggio lo prende, gli altri 409."""
        if who["kind"] != "agent":
            return self._send(403, {"error": "solo un agente con il suo token prende in carico un messaggio"})
        try:
            by = self._declared(who, self._body())
        except Exception:
            by = who["name"]
        with lock:
            if not db.execute("SELECT 1 FROM messages WHERE id=?", (mid,)).fetchone():
                return self._send(404, {"error": "not found"})
            db.execute("INSERT OR IGNORE INTO claims(message, by, ts) VALUES(?,?,?)", (mid, by, time.time()))
            db.commit()
            holder = db.execute("SELECT by FROM claims WHERE message=?", (mid,)).fetchone()[0]
        if holder != by:
            return self._send(409, {"error": f"già preso da {holder}", "claimed_by": holder})
        self._send(200, {"message": mid, "claimed_by": by})

    def _set_aliases(self, who):
        """{"aliases": {"casa/claude": "lia", "casa/opencode": ""}}: i nomi dei propri sotto-nomi
        ("" toglie il nome). Unici in tutta la chat, mai il nome di un agente, dell'owner o «tutti»."""
        if who["kind"] != "agent":
            return self._send(403, {"error": "solo un agente con il suo token sceglie i nomi"})
        try:
            wanted = dict(self._body().get("aliases") or {})
        except Exception:
            return self._send(400, {"error": "body JSON richiesto: {aliases: {handle: nome}}"})
        base = who["name"]
        refused = {}
        with lock:
            reserved = agent_names() | {OWNER} | EVERYONE | {SYSTEM}
            for handle, alias in list(wanted.items())[:20]:
                handle = str(handle).strip().lower()
                alias = str(alias or "").strip().lower().lstrip("@")
                if handle != base and not (handle.startswith(base + "/") and NAME.match(handle[len(base) + 1:])):
                    refused[handle] = f"puoi dare nomi solo a «{base}» e «{base}/…»"
                    continue
                if alias and (not NAME.match(alias) or alias in reserved or "/" in alias):
                    refused[handle] = "nome non valido o riservato (lettere minuscole, cifre, . _ -)"
                    continue
                taken = db.execute("SELECT handle FROM aliases WHERE alias=?", (alias,)).fetchone()
                if alias and taken and taken[0] != handle:
                    refused[handle] = f"«{alias}» è già il nome di {taken[0]}"
                    continue
                db.execute("DELETE FROM aliases WHERE handle=?", (handle,))
                if alias:
                    db.execute("INSERT INTO aliases(alias, handle) VALUES(?,?)", (alias, handle))
            db.commit()
            now = aliases()
        self._send(200, {"aliases": now, "refused": refused})

    # Shared memory

    def _put_memory(self, who):
        """{"files": {"MEMORIA.md": "…", "pagine/Rossi.md": "…"}}: la memoria dell'agente, intera."""
        if who["kind"] != "agent":
            return self._send(403, {"error": "solo un agente con il suo token condivide la memoria"})
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n > MAX_MEMORY:
            return self._send(413, {"error": "memoria troppo grande"})
        try:
            files = json.loads(self.rfile.read(n) or b"{}").get("files")
            assert isinstance(files, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in files.items())
        except Exception:
            return self._send(400, {"error": "body JSON richiesto: {files: {percorso: testo}}"})
        with lock:
            row = db.execute("SELECT rev FROM memory WHERE agent=?", (who["name"],)).fetchone()
            rev = (row[0] if row else 0) + 1
            db.execute("INSERT INTO memory(agent, rev, ts, data) VALUES(?,?,?,?) ON CONFLICT(agent) DO UPDATE SET "
                       "rev=excluded.rev, ts=excluded.ts, data=excluded.data", (who["name"], rev, time.time(), json.dumps(files)))
            db.commit()
        self._send(200, {"rev": rev})

    def _get_memory(self, agent, q):
        """La copia della memoria di un agente; con ?rev=N e niente di nuovo, solo {rev}."""
        with lock:
            row = db.execute("SELECT rev, ts, data FROM memory WHERE agent=?", (agent,)).fetchone()
        if not row:
            return self._send(404, {"error": f"{agent} non condivide la memoria"})
        if q.get("rev", [""])[0] == str(row[0]):
            return self._send(200, {"rev": row[0], "ts": row[1]})
        self._send(200, {"rev": row[0], "ts": row[1], "files": json.loads(row[2])})

    def _memory_ask(self, who, agent):
        """{"query"}: cerca nella memoria dell'agente, se è acceso; 504 se non risponde in tempo."""
        if who["kind"] != "agent" or agent == who["name"]:
            return self._send(403, {"error": "solo un altro agente"})
        try:
            query = str(self._body().get("query") or "").strip()[:300]
            assert query
        except Exception:
            return self._send(400, {"error": "body JSON richiesto: {query}"})
        with cond:
            if agent not in agent_names():
                return self._send(404, {"error": "agente inesistente"})
            cur = db.execute("INSERT INTO memreq(agent, sender, kind, body, ts) VALUES(?,?,?,?,?)",
                             (agent, who["name"], "ask", query, time.time()))
            rid = cur.lastrowid
            db.commit()
            cond.notify_all()
            deadline = time.time() + ASK_SECS
            row = None
            while time.time() < deadline:
                row = db.execute("SELECT answer FROM memreq WHERE id=? AND done=1", (rid,)).fetchone()
                if row:
                    break
                cond.wait(deadline - time.time())
            # Late is useless: nobody answers it any more.
            db.execute("UPDATE memreq SET done=1 WHERE id=?", (rid,))
            db.commit()
        if not row:
            return self._send(504, {"error": f"{agent} non risponde"})
        self._send(200, {"answer": row[0] or ""})

    def _memory_note(self, who, agent):
        """{"text"}: una nota per la memoria dell'agente; la applica lui appena è in ascolto."""
        if who["kind"] != "agent" or agent == who["name"]:
            return self._send(403, {"error": "solo un altro agente"})
        try:
            text = str(self._body().get("text") or "").strip()[:1000]
            assert text
        except Exception:
            return self._send(400, {"error": "body JSON richiesto: {text}"})
        with cond:
            if agent not in agent_names():
                return self._send(404, {"error": "agente inesistente"})
            cur = db.execute("INSERT INTO memreq(agent, sender, kind, body, ts) VALUES(?,?,?,?,?)",
                             (agent, who["name"], "note", text, time.time()))
            db.commit()
            cond.notify_all()
            online = presence().get(agent, {}).get("online", False)
        self._send(202, {"id": cur.lastrowid, "online": online})

    def _memory_requests(self, who, q):
        """Le richieste per la propria memoria ancora da fare (domande ancora fresche, note), con ?wait=s."""
        if who["kind"] != "agent":
            return self._send(403, {"error": "solo un agente"})
        try:
            wait = max(0.0, min(float(q.get("wait", ["0"])[0]), 60))
        except ValueError:
            wait = 0.0
        deadline = time.time() + wait
        sql = ("SELECT id, sender, kind, body, ts FROM memreq WHERE agent=? AND done=0 "
               "AND (kind='note' OR ts>?) ORDER BY id LIMIT 50")
        with cond, Listening(who):
            rows = db.execute(sql, (who["name"], time.time() - ASK_SECS)).fetchall()
            while not rows and time.time() < deadline:
                cond.wait(deadline - time.time())
                rows = db.execute(sql, (who["name"], time.time() - ASK_SECS)).fetchall()
        self._send(200, [dict(zip(("id", "sender", "kind", "body", "ts"), r)) for r in rows])

    def _memory_done(self, who, rid):
        """{"answer"?}: la richiesta è fatta (la risposta a una domanda)."""
        if who["kind"] != "agent":
            return self._send(403, {"error": "solo un agente"})
        try:
            ans = str(self._body().get("answer") or "")[:20000]
            rid = int(rid)
        except Exception:
            return self._send(400, {"error": "body JSON richiesto: {answer?}"})
        with cond:
            cur = db.execute("UPDATE memreq SET answer=?, done=1 WHERE id=? AND agent=? AND done=0", (ans, rid, who["name"]))
            # Old done requests go away.
            db.execute("DELETE FROM memreq WHERE done=1 AND ts<?", (time.time() - 86400,))
            db.commit()
            cond.notify_all()
        self._send(200 if cur.rowcount else 404, {"done": bool(cur.rowcount)})

    def _import_file(self, who, fid):
        """Un solo agente importa ogni allegato: il primo che lo chiede; gli altri ricevono 409 e chi l'ha preso."""
        if who["kind"] != "agent":
            return self._send(403, {"error": "solo un agente con il suo token importa un allegato"})
        try:
            by = self._declared(who, self._body())
        except Exception:
            by = who["name"]
        with lock:
            row = db.execute("SELECT imported_by, name, size, sha256 FROM files WHERE id=?", (fid,)).fetchone()
            if not row:
                return self._send(404, {"error": "not found"})
            if row[0] and row[0] != by:
                return self._send(409, {"error": f"già importato da {row[0]}", "imported_by": row[0]})
            db.execute("UPDATE files SET imported_by=?, imported_at=? WHERE id=?", (by, time.time(), fid))
            db.commit()
        self._send(200, {"id": fid, "imported_by": by, "name": row[1], "size": row[2], "sha256": row[3]})

    def _get_file(self, fid):
        if not FILE_ID.match(fid):
            return self._send(404, {"error": "not found"})
        with lock:
            row = db.execute("SELECT name, type FROM files WHERE id=?", (fid,)).fetchone()
        path = os.path.join(FILES, fid)
        if not row or not os.path.isfile(path):
            return self._send(404, {"error": "not found"})
        name, ctype = row
        inline = ctype in INLINE
        body = open(path, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", ctype if inline else "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(name)}")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "sandbox; default-src 'none'; img-src 'self'; style-src 'unsafe-inline'")
        self.send_header("Cache-Control", "private, max-age=86400")
        self.end_headers()
        self.wfile.write(body)

    # The coordinator

    @staticmethod
    def _page_owner(who):
        """The owner in the page (not an app with an owner token)."""
        return who["kind"] == "owner" and not who.get("via")

    def _set_coordinator(self, who):
        """{on?, model?, fallback?, report_at?, daily_tokens?, key?}: "" as key removes it."""
        if not self._page_owner(who):
            return self._send(403, {"error": "solo dalla pagina web"})
        try:
            d = self._body()
            changes = {}
            if "on" in d:
                changes["on"] = "1" if d["on"] else "0"
            for k in ("model", "fallback"):
                if k in d:
                    v = str(d[k] or "").strip()[:120]
                    assert k == "fallback" or v
                    assert not v or re.match(r"^[A-Za-z0-9._:/-]+$", v)
                    changes[k] = v
            if "report_at" in d:
                v = str(d["report_at"] or "").strip()
                assert v == "" or TIME.match(v)
                changes["report_at"] = v
            if "daily_tokens" in d:
                changes["daily_tokens"] = str(max(0, min(int(d["daily_tokens"]), 100_000_000)))
            if "key" in d:
                v = str(d["key"] or "").strip()
                assert len(v) < 300 and all(c.isprintable() and not c.isspace() for c in v)
                changes["key"] = v
        except Exception:
            return self._send(400, {"error": "valori non validi"})
        with lock:
            for k, v in changes.items():
                db.execute("INSERT INTO settings(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           ("coord_" + k, v))
            db.commit()
            out = coord_config()
        out["status"] = coord_status
        self._send(200, out)

    def _test_coordinator(self, who):
        """Checks the OpenRouter key (the one given, or the saved one): its credit and limit."""
        if not self._page_owner(who):
            return self._send(403, {"error": "solo dalla pagina web"})
        try:
            key = str(self._body().get("key") or "").strip()
        except Exception:
            key = ""
        with lock:
            key = key or coord_config(with_key=True)["key"]
        if not key:
            return self._send(400, {"error": "nessuna chiave"})
        try:
            data = openrouter("/key", key).get("data") or {}
        except urllib.error.HTTPError as e:
            return self._send(200, {"ok": False, "error": "chiave rifiutata" if e.code in (401, 403) else f"OpenRouter: HTTP {e.code}"})
        except Exception as e:
            return self._send(200, {"ok": False, "error": f"OpenRouter non risponde: {e}"})
        self._send(200, {"ok": True, "label": data.get("label"), "usage": data.get("usage"), "limit": data.get("limit"),
                         "limit_remaining": data.get("limit_remaining"), "free_tier": data.get("is_free_tier")})

    # Capabilities, assignments, scheduled requests

    def _put_capabilities(self, who):
        """{"integrations": [...], "engines": [...], "folders": [...], "notes": "…"}: what the agent can do."""
        if who["kind"] != "agent":
            return self._send(403, {"error": "solo un agente con il suo token dice cosa sa fare"})
        try:
            d = self._body()
            assert isinstance(d, dict)
            data = {k: d[k] for k in ("integrations", "engines", "folders", "notes", "lead") if k in d}
            raw = json.dumps(data, ensure_ascii=False)
            assert len(raw) < 20000
        except Exception:
            return self._send(400, {"error": "body JSON richiesto: {integrations?, engines?, folders?, notes?, lead?}"})
        with lock:
            db.execute("INSERT INTO capabilities(agent, ts, data) VALUES(?,?,?) ON CONFLICT(agent) DO UPDATE SET "
                       "ts=excluded.ts, data=excluded.data", (who["name"], time.time(), raw))
            db.commit()
        self._send(200, {"ok": True})

    def _author(self, who, body):
        """Who writes: (name, human). Legacy tokens may not."""
        if who["kind"] == "owner":
            return OWNER, 1
        if who["kind"] == "agent":
            return self._declared(who, body), 0
        return None, 0

    @staticmethod
    def _due(v):
        """A deadline: seconds, «2026-11-07», «2026-11-07 18:00» or «2026-11-07T18:00» (local time)."""
        if v in (None, ""):
            return None
        if isinstance(v, (int, float)):
            return float(v)
        s = str(v).strip().replace("T", " ")[:16]
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                t = time.strptime(s, fmt)
                return time.mktime(t) + (0 if " " in s else 18 * 3600)
            except ValueError:
                pass
        raise ValueError("scadenza non valida")

    def _hand_out(self, task, author, human):
        """The assignment's message for whoever does it; its thread is the assignment's progress."""
        when = time.strftime("%d/%m/%Y %H:%M", time.localtime(task["due"])) if task["due"] else ""
        text = (f"@{task['assignee']} 📋 Incarico #{task['id']}: {task['title']}"
                + (f"\n{task['detail']}" if task["detail"] else "")
                + (f"\nScadenza: {when}" if when else "")
                + "\nRispondi in questo thread; «FATTO: …» o «BLOCCATO: …» lo chiude.")
        mid = insert_message(task["room"], author, text, 1, human=human)
        db.execute("UPDATE tasks SET message=? WHERE id=?", (mid, task["id"]))
        return mid

    def _new_task(self, who):
        try:
            d = self._body()
            title = str(d["title"]).strip()[:200]
            detail = str(d.get("detail") or "").strip()[:4000]
            assignee = str(d.get("assignee") or "").strip().lower().lstrip("@")[:64] or None
            room = str(d.get("room") or "general").strip()[:64]
            due = self._due(d.get("due"))
            assert title and (assignee is None or NAME.match(assignee.replace("/", "")[:32] or "x"))
        except Exception:
            return self._send(400, {"error": "body JSON richiesto: {title, detail?, assignee?, due?, room?}"})
        author, human = self._author(who, d)
        if not author:
            return self._send(403, {"error": "serve un token personale"})
        now = time.time()
        with cond:
            cur = db.execute("INSERT INTO tasks(title, detail, assignee, state, due, created_by, created, updated, room) "
                             "VALUES(?,?,?,?,?,?,?,?,?)", (title, detail, assignee, "todo", due, author, now, now, room))
            task = {"id": cur.lastrowid, "title": title, "detail": detail, "assignee": assignee, "due": due, "room": room}
            if assignee:
                self._hand_out(task, author, human)
            db.commit()
            cond.notify_all()
            out = dict(zip(TASK_FIELDS, db.execute("SELECT %s FROM tasks WHERE id=?" % ", ".join(TASK_FIELDS), (task["id"],)).fetchone()))
        self._send(201, out)

    def _update_task(self, who, tid):
        """{state?, assignee?, result?, due?, title?, detail?}: a new assignee gets the message."""
        try:
            d = self._body()
            changes = {}
            if "state" in d:
                assert d["state"] in TASK_STATES
                changes["state"] = d["state"]
            for k, n in (("title", 200), ("detail", 4000), ("result", 4000)):
                if k in d:
                    changes[k] = str(d[k] or "").strip()[:n]
            if "due" in d:
                changes["due"] = self._due(d["due"])
            if "assignee" in d:
                changes["assignee"] = str(d["assignee"] or "").strip().lower().lstrip("@")[:64] or None
            assert changes
        except Exception:
            return self._send(400, {"error": "body JSON richiesto: {state?, assignee?, result?, due?, title?, detail?}"})
        author, human = self._author(who, d)
        if not author:
            return self._send(403, {"error": "serve un token personale"})
        with cond:
            row = db.execute("SELECT %s FROM tasks WHERE id=?" % ", ".join(TASK_FIELDS), (tid,)).fetchone()
            if not row:
                return self._send(404, {"error": "incarico inesistente"})
            task = dict(zip(TASK_FIELDS, row))
            moved = "assignee" in changes and changes["assignee"] and changes["assignee"] != task["assignee"]
            changes["updated"] = time.time()
            db.execute("UPDATE tasks SET %s WHERE id=?" % ", ".join(f"{k}=?" for k in changes), (*changes.values(), tid))
            task.update(changes)
            if moved:
                if task["state"] in ("done", "cancelled"):
                    db.execute("UPDATE tasks SET state='todo' WHERE id=?", (tid,))
                self._hand_out(task, author, human)
            db.commit()
            cond.notify_all()
            out = dict(zip(TASK_FIELDS, db.execute("SELECT %s FROM tasks WHERE id=?" % ", ".join(TASK_FIELDS), (tid,)).fetchone()))
        self._send(200, out)

    def _save_schedule(self, who, sid):
        """{text, at: "HH:MM", days?: "12345", room?, enabled?}: new (sid None) or changed.
        The owner's are written as the owner; an agent's as itself, and it changes only its own."""
        try:
            d = self._body()
            fields = {}
            if "text" in d or sid is None:
                fields["text"] = str(d["text"]).strip()[:2000]
                assert fields["text"]
            if "at" in d or sid is None:
                fields["at"] = str(d["at"]).strip()
                assert TIME.match(fields["at"])
            if "days" in d:
                fields["days"] = "".join(sorted({c for c in str(d["days"]) if c in "1234567"})) or "1234567"
            if "room" in d:
                fields["room"] = str(d["room"] or "general").strip()[:64]
            if "enabled" in d:
                fields["enabled"] = 1 if d["enabled"] else 0
        except Exception:
            return self._send(400, {"error": "body JSON richiesto: {text, at: \"HH:MM\", days?: \"12345\", room?, enabled?}"})
        author, _ = self._author(who, d)
        if not author:
            return self._send(403, {"error": "serve un token personale"})
        with lock:
            if sid is None:
                fields.update(created_by=author, created=time.time())
                cur = db.execute("INSERT INTO schedules(%s) VALUES(%s)" % (", ".join(fields), ",".join("?" * len(fields))),
                                 tuple(fields.values()))
                sid = cur.lastrowid
            else:
                row = db.execute("SELECT created_by FROM schedules WHERE id=?", (sid,)).fetchone()
                if not row:
                    return self._send(404, {"error": "programmata inesistente"})
                if who["kind"] != "owner" and row[0].split("/")[0] != who["name"]:
                    return self._send(403, {"error": "puoi cambiare solo le tue"})
                if fields:
                    db.execute("UPDATE schedules SET %s WHERE id=?" % ", ".join(f"{k}=?" for k in fields), (*fields.values(), sid))
            db.commit()
            out = next((s for s in schedules_list() if s["id"] == sid), None)
        self._send(201 if out else 404, out or {"error": "not found"})

    def _delete_schedule(self, who, sid):
        if not str(sid).isdigit():
            return self._send(400, {"error": "id mancante"})
        with lock:
            row = db.execute("SELECT created_by FROM schedules WHERE id=?", (int(sid),)).fetchone()
            if not row:
                return self._send(404, {"error": "programmata inesistente"})
            if who["kind"] != "owner" and row[0].split("/")[0] != who.get("name"):
                return self._send(403, {"error": "puoi togliere solo le tue"})
            db.execute("DELETE FROM schedules WHERE id=?", (int(sid),))
            db.commit()
        self._send(200, {"deleted": True})

    def _set_status(self, who):
        """Cosa fa un agente con un messaggio: in memoria, mai nello storico."""
        if who["kind"] != "agent":
            return self._send(403, {"error": "solo un agente con il suo token dice cosa sta facendo"})
        try:
            d = self._body()
            mid = int(d["message"])
            state = str(d["state"])
            assert state in WORKING + ("done", "silent", "failed")
        except Exception:
            return self._send(400, {"error": "body JSON richiesto: {message, state, sender?, detail?}"})
        by = self._declared(who, d)
        detail = str(d.get("detail") or "")[:120]
        now = time.time()
        with lock:
            if state in WORKING:
                old = activity.get(by)
                since = old["since"] if old and old["message"] == mid else now
                activity[by] = {"message": mid, "state": state, "detail": detail, "since": since, "ts": now}
            else:
                old = activity.get(by)
                if old and old["message"] == mid:
                    activity.pop(by, None)
                if state in ("silent", "failed"):
                    o = outcomes.setdefault(mid, {})
                    o[by] = state
                    o["_ts"] = now
        self._send(200, {"ok": True})

    def _new_owner_token(self, who):
        """Un token per scrivere come l'owner da un'app: si vede solo qui."""
        if who["kind"] != "owner" or who.get("via"):
            return self._send(403, {"error": "solo dalla pagina web"})
        try:
            label = str(self._body().get("label") or "").strip().lower()[:32]
        except Exception:
            label = ""
        if not NAME.match(label):
            return self._send(400, {"error": "nome del token non valido (es. easyisland-casa)"})
        token = "ow_" + secrets.token_urlsafe(24)
        with lock:
            db.execute("INSERT INTO owner_tokens(label, token_hash, created) VALUES(?,?,?) "
                       "ON CONFLICT(label) DO UPDATE SET token_hash=excluded.token_hash, created=excluded.created",
                       (label, token_hash(token), time.time()))
            db.commit()
        self._send(201, {"label": label, "token": token})

    def _new_agent(self, who):
        """Crea un agente (o gli dà un token nuovo): il token si vede solo qui."""
        if who["kind"] != "owner" or who.get("via"):
            return self._send(403, {"error": "solo dalla pagina web"})
        try:
            name = str(self._body().get("name", "")).strip().lower()
        except Exception:
            name = ""
        if name == COORDINATOR:
            return self._send(400, {"error": f"«{COORDINATOR}» è il coordinatore: il suo token lo crea il server"})
        if not NAME.match(name) or name == OWNER or name in EVERYONE or name == SYSTEM:
            return self._send(400, {"error": "nome: lettere minuscole, cifre, . _ - (max 32), diverso dal tuo"})
        with lock:
            if db.execute("SELECT 1 FROM aliases WHERE alias=?", (name,)).fetchone():
                return self._send(400, {"error": f"«{name}» è già il nome di un motore"})
        token = "ac_" + secrets.token_urlsafe(24)
        with lock:
            db.execute("INSERT INTO agents(name, token_hash, created) VALUES(?,?,?) "
                       "ON CONFLICT(name) DO UPDATE SET token_hash=excluded.token_hash, created=excluded.created",
                       (name, token_hash(token), time.time()))
            db.commit()
        self._send(201, {"name": name, "token": token})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    coord_token()
    threading.Thread(target=ticker, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8000"))), H).serve_forever()
