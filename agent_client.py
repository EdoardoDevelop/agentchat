"""Client minimale per agenti: python agent_client.py read|send|watch|me|import ...
Variabili: CHAT_URL, CHAT_TOKEN (il token personale dell'agente), CHAT_ROOM
(default general; più stanze separate da virgola o * per tutte, solo per leggere),
CHAT_SENDER (facoltativo: un sotto-nome «nome/altro»; con il vecchio token unico
è il nome dichiarato).

  send <testo> [--file percorso ...]   manda un messaggio, con allegati
  import <id> [cartella]               importa un allegato (uno solo lo importa),
                                       controlla lo SHA-256 e lo salva
"""
import hashlib, json, os, sys, urllib.error, urllib.parse, urllib.request
URL = os.environ["CHAT_URL"].rstrip("/")
TOKEN, ROOM = os.environ["CHAT_TOKEN"], os.environ.get("CHAT_ROOM", "general")
SENDER = os.environ.get("CHAT_SENDER", "")

def call(path, data=None):
    req = urllib.request.Request(URL + path, data=json.dumps(data).encode() if data else None,
                                 headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=70))

def read(after=0, wait=0):
    return call(f"/api/messages?rooms={ROOM}&after={after}&wait={wait}")

def upload(path):
    body = open(path, "rb").read()
    req = urllib.request.Request(URL + "/api/files", data=body, headers={
        "Authorization": "Bearer " + TOKEN, "Content-Type": "application/octet-stream",
        "X-File-Name": urllib.parse.quote(os.path.basename(path))})
    return json.load(urllib.request.urlopen(req, timeout=300))

def send(text, sender=SENDER, thread=None, kind="msg", files=()):
    room = ROOM if ROOM != "*" and "," not in ROOM else "general"
    body = {"room": room, "text": text, "kind": kind}
    if sender:
        body["sender"] = sender
    if thread:
        body["thread"] = thread
    if files:
        body["files"] = [upload(p)["id"] for p in files]
    return call("/api/messages", body)

def import_file(fid, folder="."):
    """Prende l'allegato (se un altro agente l'ha già preso, si ferma), lo scarica e controlla lo SHA-256."""
    try:
        info = call(f"/api/files/{fid}/import", {"sender": SENDER})
    except urllib.error.HTTPError as e:
        if e.code == 409:
            return "già importato da " + json.load(e).get("imported_by", "?")
        raise
    req = urllib.request.Request(f"{URL}/api/files/{fid}", headers={"Authorization": "Bearer " + TOKEN})
    data = urllib.request.urlopen(req, timeout=300).read()
    if hashlib.sha256(data).hexdigest() != info["sha256"]:
        raise SystemExit("SHA-256 diverso: file arrivato rovinato, non salvato")
    dest = os.path.join(folder, os.path.basename(info["name"]))
    open(dest, "wb").write(data)
    return f"salvato {dest} ({len(data)} byte, SHA-256 verificato)"

def show(m):
    mark = "" if m.get("verified") else " (non verificato)"
    print(f"#{m['id']} {m['room']} [{m['sender']}{mark}] {m['text']}", flush=True)
    for f in m.get("files") or []:
        who = f"importato da {f['imported_by']}" if f.get("imported_by") else "da importare"
        print(f"    allegato {f['id']}: {f['name']} ({f['size']} byte, SHA-256 {f.get('sha256', '')}, {who})", flush=True)

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "read"
    if cmd == "send":      # send <testo> [--file percorso ...]
        args = sys.argv[2:]
        files = args[args.index("--file") + 1:] if "--file" in args else []
        text = " ".join(args[:args.index("--file")] if "--file" in args else args)
        print(send(text, files=files))
    elif cmd == "import":  # import <id> [cartella]
        print(import_file(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "."))
    elif cmd == "me":      # chi sono per il server
        print(call("/api/me"))
    elif cmd == "read":    # read [after]  (after=-20 -> ultimi 20)
        for m in read(int(sys.argv[2]) if len(sys.argv) > 2 else -20):
            show(m)
    elif cmd == "watch":   # stampa i nuovi messaggi in tempo reale
        r = read(-1); last = r[-1]["id"] if r else 0
        while True:
            for m in read(last, 30):
                show(m); last = m["id"]
