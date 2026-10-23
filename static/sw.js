/* VerquoAI service worker: fast start, an offline screen, and push alerts.
 *
 * What it caches: static files only (scripts, styles, icons, fonts) - served at
 * once from the cache and refreshed in the background. Pages, the API, the live
 * socket, sign-in and every non-GET request always go to the server: prices,
 * orders and account pages are never stored on the device or shown stale.
 * Served from /sw.js (scope /) by the app; CACHE_VERSION is set by the server.
 */
const CACHE_VERSION = '__VERSION__';
const STATIC_CACHE = `vq-static-${CACHE_VERSION}`;
const OFFLINE_URL = '/offline';
const PRECACHE = [OFFLINE_URL, '/static/favicon.svg', '/static/img/icon-192.png', '/static/img/icon-512.png',
    '/static/img/badge-96.png'];
const NEVER = [/^\/api\//, /^\/ws\//, /^\/auth\//, /^\/demo\//, /^\/admin/, /^\/metrics\//, /^\/contact/,
    /^\/sw\.js$/, /^\/manifest\.webmanifest$/];

self.addEventListener('install', event => {
    event.waitUntil(caches.open(STATIC_CACHE).then(c => c.addAll(PRECACHE)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', event => {
    event.waitUntil((async () => {
        const names = await caches.keys();
        await Promise.all(names.filter(n => n.startsWith('vq-') && n !== STATIC_CACHE).map(n => caches.delete(n)));
        if (self.registration.navigationPreload) await self.registration.navigationPreload.enable();
        await self.clients.claim();
    })());
});

self.addEventListener('fetch', event => {
    const req = event.request;
    if (req.method !== 'GET') return;
    const url = new URL(req.url);
    // other sites (Google Fonts): left to the browser and its own cache - the worker is bound by
    // the site's security policy (connections to this site only), so it must not fetch them itself
    if (url.origin !== self.location.origin) return;
    if (NEVER.some(rx => rx.test(url.pathname))) return;
    if (req.mode === 'navigate') {
        event.respondWith(page(event));
        return;
    }
    if (url.pathname.startsWith('/static/')) event.respondWith(staleWhileRevalidate(req));
});

// pages: always from the network (fresh session, CSRF token, data); offline -> the offline screen
async function page(event) {
    try {
        const preloaded = await event.preloadResponse;
        if (preloaded) return preloaded;
        return await fetch(event.request);
    } catch (e) {
        const cached = await caches.match(OFFLINE_URL);
        return cached || new Response('Offline', { status: 503, headers: { 'Content-Type': 'text/plain' } });
    }
}

async function staleWhileRevalidate(req) {
    const cache = await caches.open(STATIC_CACHE);
    const cached = await cache.match(req);
    const network = fetch(req).then(res => {
        if (res && res.ok && (res.type === 'basic' || res.type === 'cors')) cache.put(req, res.clone());
        return res;
    }).catch(() => cached || Response.error());       // offline and never cached: a normal network error
    return cached || network;
}

self.addEventListener('message', event => {
    if (event.data === 'skip-waiting') self.skipWaiting();
    if (event.data === 'clear') event.waitUntil(caches.keys().then(n => Promise.all(n.map(k => caches.delete(k)))));
});

// ── push alerts ──────────────────────────────────────────────────────────
self.addEventListener('push', event => {
    let data = {};
    try { data = event.data ? event.data.json() : {}; } catch (e) { data = { title: 'VerquoAI', body: event.data && event.data.text() }; }
    const risk = data.category === 'risk';
    event.waitUntil(self.registration.showNotification(data.title || 'VerquoAI', {
        body: data.body || '',
        tag: data.tag || undefined,
        renotify: risk,
        requireInteraction: risk,          // a failed exit stays on screen until seen
        icon: '/static/img/icon-192.png',
        badge: '/static/img/badge-96.png',
        timestamp: (data.ts || Date.now() / 1000) * 1000,
        data: { url: data.url || '/app' },
        vibrate: risk ? [200, 100, 200, 100, 400] : [120],
    }));
});

self.addEventListener('notificationclick', event => {
    event.notification.close();
    const target = new URL(event.notification.data && event.notification.data.url || '/app', self.location.origin);
    if (target.origin !== self.location.origin) return;
    event.waitUntil((async () => {
        const wins = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
        const open = wins.find(w => new URL(w.url).pathname.startsWith('/app'));
        if (open) {
            await open.focus();
            open.postMessage({ type: 'open', url: target.pathname + target.search + target.hash });
            return;
        }
        await self.clients.openWindow(target.href);
    })());
});

self.addEventListener('pushsubscriptionchange', event => {
    // the browser rotated the subscription: re-register it under the same session
    event.waitUntil((async () => {
        try {
            const keyRes = await fetch('/api/push/key', { credentials: 'same-origin' });
            if (!keyRes.ok) return;
            const { key } = await keyRes.json();
            const sub = await self.registration.pushManager.subscribe({
                userVisibleOnly: true, applicationServerKey: urlB64ToBytes(key) });
            const { csrf_token: csrf } = await (await fetch('/auth/csrf', { credentials: 'same-origin' })).json();
            await fetch('/api/push/resubscribe', {
                method: 'POST', credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
                body: JSON.stringify({ subscription: sub.toJSON(),
                    old_endpoint: event.oldSubscription && event.oldSubscription.endpoint }),
            });
        } catch (e) { /* the app re-subscribes on its next start */ }
    })());
});

function urlB64ToBytes(b64) {
    const pad = '='.repeat((4 - b64.length % 4) % 4);
    const raw = atob((b64 + pad).replace(/-/g, '+').replace(/_/g, '/'));
    return Uint8Array.from(raw, c => c.charCodeAt(0));
}
