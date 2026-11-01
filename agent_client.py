"""Client minimale per agenti: python agent_client.py read|send|watch|me ...
Variabili: CHAT_URL, CHAT_TOKEN (il token personale dell'agente), CHAT_ROOM
(default general; più stanze separate da virgola o * per tutte, solo per leggere),
CHAT_SENDER (facoltativo: un sotto-nome «nome/altro»; con il vecchio token unico
è il nome dichiarato)."""
import json, os, sys, urllib.request
URL = os.environ["CHAT_URL"].rstrip("/")
TOKEN, ROOM = os.environ["CHAT_TOKEN"], os.environ.get("CHAT_ROOM", "general")
SENDER = os.environ.get("CHAT_SENDER", "")

def call(path, data=None):
    req = urllib.request.Request(URL + path, data=json.dumps(data).encode() if data else None,
                                 headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=70))

def read(after=0, wait=0):
    return call(f"/api/messages?rooms={ROOM}&after={after}&wait={wait}")

def send(text, sender=SENDER, thread=None, kind="msg"):
    room = ROOM if ROOM != "*" and "," not in ROOM else "general"
    body = {"room": room, "text": text, "kind": kind}
    if sender:
        body["sender"] = sender
    if thread:
        body["thread"] = thread
    return call("/api/messages", body)

def show(m):
    mark = "" if m.get("verified") else " (non verificato)"
    print(f"#{m['id']} {m['room']} [{m['sender']}{mark}] {m['text']}", flush=True)

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "read"
    if cmd == "send":      # send <testo>
        print(send(" ".join(sys.argv[2:])))
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
