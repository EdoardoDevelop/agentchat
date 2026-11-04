"""Web Push for Agent Chat, stdlib only.

Pushes carry no payload: the browser's push service only wakes the page's service
worker, which then reads the notifications waiting for it from the server
(GET /api/push/pending, with the owner's session). So there is nothing to encrypt
(RFC 8291 is not needed) and only the VAPID signature (RFC 8292, ES256 on P-256)
is computed here, with plain integer arithmetic.
"""
import base64, hashlib, json, secrets, time, urllib.error, urllib.request
from urllib.parse import urlparse

# P-256 (secp256r1)
P = 0xffffffff00000001000000000000000000000000ffffffffffffffffffffffff
A = P - 3
B = 0x5ac635d8aa3a93e7b3ebbd55769886bc651d06b0cc53b0f63bce3c3e27d2604b
N = 0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551
G = (0x6b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4a13945d898c296,
     0x4fe342e2fe1a7f9b8ee7eb4a7c0f9e162bce33576b315ececbb6406837bf51f5)

# The push services of the browsers: nothing else is ever called (the endpoint comes from
# the page, so it must not become a way to make the server call any address).
HOSTS = ("fcm.googleapis.com", ".push.services.mozilla.com", ".notify.windows.com",
         "web.push.apple.com", ".push.apple.com")


def _add(p, q):
    if p is None:
        return q
    if q is None:
        return p
    if p[0] == q[0] and (p[1] + q[1]) % P == 0:
        return None
    if p == q:
        lam = (3 * p[0] * p[0] + A) * pow(2 * p[1], -1, P) % P
    else:
        lam = (q[1] - p[1]) * pow(q[0] - p[0], -1, P) % P
    x = (lam * lam - p[0] - q[0]) % P
    return x, (lam * (p[0] - x) - p[1]) % P


def _mul(k, p=G):
    r = None
    while k:
        if k & 1:
            r = _add(r, p)
        p = _add(p, p)
        k >>= 1
    return r


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def new_keys():
    """(private key as hex, public key as base64url of the uncompressed point)."""
    d = secrets.randbelow(N - 1) + 1
    x, y = _mul(d)
    return format(d, "064x"), b64u(b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big"))


def sign(d_hex, data):
    """ES256: r || s, 64 bytes."""
    d = int(d_hex, 16)
    z = int.from_bytes(hashlib.sha256(data).digest(), "big")
    while True:
        k = secrets.randbelow(N - 1) + 1
        r = _mul(k)[0] % N
        if not r:
            continue
        s = pow(k, -1, N) * (z + r * d) % N
        if s:
            return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def allowed(endpoint):
    u = urlparse(endpoint or "")
    host = (u.hostname or "").lower()
    return u.scheme == "https" and any(host == h or (h.startswith(".") and host.endswith(h)) for h in HOSTS)


def send(endpoint, d_hex, pub, subject, ttl=3600):
    """One push without payload. Back the HTTP status (404/410: the subscription is gone)."""
    u = urlparse(endpoint)
    head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}).encode())
    claims = b64u(json.dumps({"aud": f"{u.scheme}://{u.netloc}", "exp": int(time.time()) + 12 * 3600,
                              "sub": subject}).encode())
    jwt = f"{head}.{claims}.{b64u(sign(d_hex, f'{head}.{claims}'.encode()))}"
    req = urllib.request.Request(endpoint, data=b"", method="POST", headers={
        "TTL": str(ttl), "Urgency": "high", "Content-Length": "0",
        "Authorization": f"vapid t={jwt}, k={pub}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
