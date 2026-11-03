const CACHE_NAME = 'msg-push-v8';
const ASSETS = ['./', 'index.html', 'message-protocol.js?v=20261103-direct-1', 'manifest.json'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE_NAME).then(c => c.addAll(ASSETS.map(url => new Request(url, { cache: 'reload' })))));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET' || new URL(e.request.url).origin !== self.location.origin) return;
  // Network first, fall back to the app shell cache
  e.respondWith(
    fetch(e.request, { cache: 'no-cache' }).catch(() =>
      caches.open(CACHE_NAME).then(cache => cache.match(e.request))
    )
  );
});
