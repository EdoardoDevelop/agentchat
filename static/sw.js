// Agent Chat service worker: the app installs and opens offline on its shell.
// Messages and login always go to the network (never cached).
const CACHE = "agentchat-v1";
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
