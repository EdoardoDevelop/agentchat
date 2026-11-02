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
"""
import hashlib, hmac, json, os, re, secrets, sqlite3, threading, time
from urllib.parse import quote, unquote
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
db.execute("""CREATE TABLE IF NOT EXISTS agents(
  name TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE, created REAL NOT NULL, last_seen REAL)""")
db.commit()
lock = threading.Lock()
cond = threading.Condition(lock)
FIELDS = ("id", "room", "sender", "text", "ts", "verified", "thread", "kind", "files", "claimed_by")
EVERYONE = {"tutti", "all", "everyone"}


def rows_to_msgs(rows):
    out = []
    for r in rows:
        m = dict(zip(FIELDS, r))
        m["verified"] = bool(m["verified"])
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
    sql = ("SELECT id, room, sender, text, ts, verified, thread, kind, files, "
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


class H(BaseHTTPRequestHandler):
    def _send(self, code, obj=None, body=None, ctype="application/json", cache="no-store"):
        body = body if body is not None else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("" if ctype.startswith("image/png") else "; charset=utf-8"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

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
            with cond:
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
        if u.path == "/api/agents":
            if who["kind"] != "owner":
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
        if u.path != "/api/agents":
            return self._send(404, {"error": "not found"})
        if who["kind"] != "owner":
            return self._send(403, {"error": "solo dalla pagina web"})
        name = q.get("name", [""])[0]
        with lock:
            cur = db.execute("DELETE FROM agents WHERE name=?", (name,))
            db.commit()
        self._send(200 if cur.rowcount else 404, {"deleted": bool(cur.rowcount)})

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/login":
            n = int(self.headers.get("Content-Length", 0) or 0)
            pw = parse_qs(self.rfile.read(min(n, 10_000)).decode(errors="replace")).get("password", [""])[0]
            if not hmac.compare_digest(pw.encode(), PASSWORD.encode()):
                time.sleep(2)  # rallenta i tentativi a forza bruta
                return self._redirect("/?err=1")
            exp = int(time.time()) + SESSION_DAYS * 86400
            return self._redirect("/", f"ac_s={exp}.{sign(exp)}; Path=/; Max-Age={SESSION_DAYS * 86400}; HttpOnly; Secure; SameSite=Strict")
        who = self._auth()
        if not who:
            return
        if u.path == "/api/agents":
            return self._new_agent(who)
        if u.path == "/api/aliases":
            return self._set_aliases(who)
        if u.path.startswith("/api/messages/") and u.path.endswith("/claim"):
            mid = u.path[len("/api/messages/"):-len("/claim")]
            if not mid.isdigit():
                return self._send(404, {"error": "not found"})
            return self._claim(who, int(mid))
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
        if who["kind"] == "owner":
            sender, verified = OWNER, 1
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
                row = db.execute("SELECT id, name, type, size, sha256 FROM files WHERE id=? AND sender=? AND message IS NULL",
                                 (f, sender.split("/")[0])).fetchone()
                if not row:
                    return self._send(400, {"error": "allegato inesistente o già usato"})
                files.append(dict(zip(("id", "name", "type", "size", "sha256"), row)))
            ts = time.time()
            cur = db.execute(
                "INSERT INTO messages(room, sender, text, ts, verified, thread, kind, files) VALUES(?,?,?,?,?,?,?,?)",
                (room, sender, text, ts, verified, thread, kind, json.dumps(files) if files else None))
            for f in files:
                db.execute("UPDATE files SET message=? WHERE id=?", (cur.lastrowid, f["id"]))
            db.commit()
            cond.notify_all()
        self._send(201, {"id": cur.lastrowid, "room": room, "sender": sender, "text": text, "ts": ts,
                         "verified": bool(verified), "thread": thread, "kind": kind, "files": files})

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
            reserved = agent_names() | {OWNER} | EVERYONE
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

    def _new_agent(self, who):
        """Crea un agente (o gli dà un token nuovo): il token si vede solo qui."""
        if who["kind"] != "owner":
            return self._send(403, {"error": "solo dalla pagina web"})
        try:
            name = str(self._body().get("name", "")).strip().lower()
        except Exception:
            name = ""
        if not NAME.match(name) or name == OWNER or name in EVERYONE:
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
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8000"))), H).serve_forever()
