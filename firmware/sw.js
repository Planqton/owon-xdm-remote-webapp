// Minimal service worker — its only hard job is to exist with a fetch handler
// so the page is installable. Strategy: network-first (so OTA-updated UI is never
// stale), cache the shell as an offline fallback, and NEVER touch /api (live data).
var C = 'owon-v1';
var SHELL = ['/', '/icon-192.png', '/icon-512.png', '/manifest.json'];

self.addEventListener('install', function (e) {
  self.skipWaiting();
  e.waitUntil(caches.open(C).then(function (c) { return c.addAll(SHELL).catch(function () {}); }));
});

self.addEventListener('activate', function (e) {
  self.clients.claim();
  e.waitUntil(caches.keys().then(function (ks) {
    return Promise.all(ks.map(function (k) { return k === C ? null : caches.delete(k); }));
  }));
});

self.addEventListener('fetch', function (e) {
  var req = e.request;
  if (req.method !== 'GET') return;
  var u = new URL(req.url);
  if (u.pathname.indexOf('/api') === 0 || u.pathname === '/cmd') return; // live data: network only
  e.respondWith(
    fetch(req).then(function (r) {
      if (r && r.status === 200 && u.origin === self.location.origin) {
        var cp = r.clone();
        caches.open(C).then(function (c) { c.put(req, cp); });
      }
      return r;
    }).catch(function () {
      return caches.match(req).then(function (m) { return m || caches.match('/'); });
    })
  );
});
