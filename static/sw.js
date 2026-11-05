// Agent Chat service worker: the app installs and opens offline on its shell.
// Messages and login always go to the network (never cached).
const CACHE = "agentchat-v4";
const SHELL = ["/static/logo.svg", "/static/icon-192.png", "/static/icon-512.png", "/static/apple-touch-icon.png", "/manifest.webmanifest"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin || url.pathname.startsWith("/api/") || url.pathname === "/logout") return;
  // The page: from the network, the last copy when offline.
  if (e.request.mode === "navigate") {
    e.respondWith(fetch(e.request).then((r) => {
      if (r.ok) { const copy = r.clone(); caches.open(CACHE).then((c) => c.put("/", copy)); }
      return r;
    }).catch(() => caches.match("/").then((r) => r || new Response("Offline", { status: 503 }))));
    return;
  }
  if (url.pathname.startsWith("/static/") || url.pathname === "/manifest.webmanifest") {
    e.respondWith(caches.match(e.request).then((r) => r || fetch(e.request)));
  }
});

// Notifications: the push carries nothing; the waiting ones are read from the server (with the
// owner's session). Nothing is shown while the chat is open in front of the user.
self.addEventListener("push", (e) => {
  e.waitUntil((async () => {
    const sub = await self.registration.pushManager.getSubscription();
    let items = [];
    try {
      const r = await fetch("/api/push/pending?endpoint=" + encodeURIComponent(sub ? sub.endpoint : ""), { credentials: "same-origin" });
      if (r.ok) items = await r.json();
    } catch {}
    const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    // The test (Prova) is always shown: it is pressed with the chat in front of the user.
    const watching = wins.some((w) => w.visibilityState === "visible" && w.focused);
    if (watching) items = items.filter((n) => n.tag === "prova");
    if (watching && !items.length) return;
    if (!items.length) items = [{ title: "Agent Chat", body: "Nuovi messaggi", tag: "agentchat", url: "/" }];
    for (const n of items.slice(-3)) {
      await self.registration.showNotification(n.title, { body: n.body, tag: n.tag, icon: "/static/icon-192.png",
        badge: "/static/icon-192.png", data: { url: n.url } });
    }
  })());
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || "/";
  e.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    const w = wins.find((x) => new URL(x.url).origin === location.origin);
    if (w) { await w.focus(); w.postMessage({ open: url }); return; }
    await self.clients.openWindow(url);
  })());
});
