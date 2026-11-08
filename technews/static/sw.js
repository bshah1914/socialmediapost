/* CloudTrio Newsroom: lets the app install, shows push alerts, and shows a page when offline.
   Signed-in pages and data are never cached, so nothing private is stored on the device. */
const CACHE = "ct-static-v1";
const OFFLINE = `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Offline</title><body style="font:16px system-ui;background:#070f20;color:#e8f0fc;display:grid;place-items:center;min-height:100vh;margin:0;text-align:center;padding:24px">
<div><h1 style="margin:0 0 8px">You are offline</h1><p style="color:#9db0cf">CloudTrio Newsroom needs a connection. Try again when you are back online.</p>
<button onclick="location.reload()" style="background:#2fd4c4;color:#04202a;border:0;border-radius:10px;padding:12px 20px;font:inherit;font-weight:700">Retry</button></div>`;

self.addEventListener("install", (e) => { self.skipWaiting(); e.waitUntil(caches.open(CACHE).then((c) => c.addAll(["/static/icon-192.png", "/static/logo-mark.png"]))); });
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const r = e.request, url = new URL(r.url);
  if (r.method !== "GET" || url.origin !== location.origin) return;
  if (r.mode === "navigate") {
    e.respondWith(fetch(r).catch(() => new Response(OFFLINE, { headers: { "Content-Type": "text/html; charset=utf-8" } })));
  } else if (url.pathname.startsWith("/static/")) {
    e.respondWith(caches.match(r).then((hit) => hit || fetch(r).then((res) => { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(r, copy)); return res; })));
  }
});

self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch { d = { body: e.data ? e.data.text() : "" }; }
  e.waitUntil(self.registration.showNotification(d.title || "CloudTrio: a post is waiting for approval", {
    body: d.body || "Open the app to review it.", icon: "/static/icon-192.png", badge: "/static/icon-192.png",
    tag: d.tag || "approval", renotify: true, data: { url: d.url || "/#approvals" },
  }));
});
self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const target = new URL((e.notification.data && e.notification.data.url) || "/#approvals", location.origin).href;
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((wins) => {
    for (const w of wins) { if (w.url.startsWith(location.origin)) { w.navigate(target); return w.focus(); } }
    return self.clients.openWindow(target);
  }));
});
