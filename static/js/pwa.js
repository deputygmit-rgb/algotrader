// Installed-app support on every page: the service worker (/sw.js), the
// "Install app" button, push alerts and their settings, the Android back button
// for pop-ups, an offline banner, and an instant reconnect when the app returns
// to the foreground (low latency after the phone wakes).
(function () {
    'use strict';
    const $ = id => document.getElementById(id);
    const standalone = () => window.matchMedia('(display-mode: standalone)').matches
        || window.matchMedia('(display-mode: window-controls-overlay)').matches
        || window.navigator.standalone === true || document.referrer.startsWith('android-app://');
    if (standalone()) document.documentElement.classList.add('is-app');

    // ── service worker ─────────────────────────────────────────────────
    let registration = null;
    if ('serviceWorker' in navigator && window.isSecureContext) {
        window.addEventListener('load', () => {
            navigator.serviceWorker.register('/sw.js', { scope: '/' }).then(reg => {
                registration = reg;
                syncPushState();
                // a new version: take over at once (pages are never cached, so this is safe)
                reg.addEventListener('updatefound', () => {
                    const next = reg.installing;
                    next && next.addEventListener('statechange', () => {
                        if (next.state === 'installed' && navigator.serviceWorker.controller) next.postMessage('skip-waiting');
                    });
                });
            }).catch(e => console.warn('service worker not registered', e));
        });
        navigator.serviceWorker.addEventListener('message', e => {
            if (e.data && e.data.type === 'open' && typeof e.data.url === 'string' && e.data.url.startsWith('/')) {
                const target = new URL(e.data.url, location.origin);
                if (target.pathname === location.pathname && target.hash) {
                    location.hash = target.hash;
                    window.dispatchEvent(new HashChangeEvent('hashchange'));
                    const tab = new URLSearchParams(target.hash.slice(1)).get('tab');
                    if (tab) document.querySelector(`.tabs .tab[data-tab="${CSS.escape(tab)}"]`)?.click();
                } else {
                    location.href = target.href;
                }
            }
        });
    }

    // ── install button ─────────────────────────────────────────────────
    let deferredPrompt = null;
    window.addEventListener('beforeinstallprompt', e => {
        e.preventDefault();
        deferredPrompt = e;
        document.querySelectorAll('[data-pwa-install]').forEach(b => { b.hidden = false; });
    });
    window.addEventListener('appinstalled', () => {
        deferredPrompt = null;
        document.querySelectorAll('[data-pwa-install]').forEach(b => { b.hidden = true; });
    });
    document.addEventListener('click', async e => {
        const btn = e.target.closest('[data-pwa-install]');
        if (!btn || !deferredPrompt) return;
        e.preventDefault();
        deferredPrompt.prompt();
        await deferredPrompt.userChoice.catch(() => null);
        deferredPrompt = null;
        btn.hidden = true;
    });

    // ── back button: closes the open pop-up; in the app also steps back through tabs ──
    const openModals = () => [...document.querySelectorAll('.modal-overlay.show')];
    let skipPop = false, restoringTab = false;
    if (typeof window.showModal === 'function' && typeof window.hideModal === 'function') {
        const show = window.showModal, hide = window.hideModal;
        window.showModal = function (id) {
            show.apply(this, arguments);
            history.pushState({ ...(history.state || {}), vqModal: id }, '', location.href);
        };
        window.hideModal = function (id) {
            hide.apply(this, arguments);
            if (history.state && history.state.vqModal === id) {       // closed with its own button: drop its entry
                skipPop = true;
                history.back();
            }
        };
    }
    if (standalone() && typeof window.switchTab === 'function') {
        const switchTab = window.switchTab;
        window.switchTab = function (name) {
            const before = (history.state && history.state.vqTab) || null;
            switchTab.apply(this, arguments);
            if (!restoringTab && name !== before) history.pushState({ vqTab: name }, '', location.href);
        };
        const first = document.querySelector('.tabs .tab.active')?.dataset.tab;
        if (first) history.replaceState({ ...(history.state || {}), vqTab: first }, '', location.href);
    }
    window.addEventListener('popstate', e => {
        if (skipPop) { skipPop = false; return; }
        const top = openModals().pop();
        if (top) {
            top.classList.remove('show');                                  // closed by Back: its entry is gone already
            return;
        }
        const tab = e.state && e.state.vqTab;
        if (tab && typeof window.switchTab === 'function' && document.querySelector(`.tabs .tab[data-tab="${CSS.escape(tab)}"]`)) {
            restoringTab = true;
            try { document.querySelector(`.tabs .tab[data-tab="${CSS.escape(tab)}"]`).click(); } finally { restoringTab = false; }
        }
    });

    // ── connectivity and resume ────────────────────────────────────────
    function offlineBanner(on) {
        let bar = $('pwa-offline');
        if (!bar) {
            bar = document.createElement('div');
            bar.id = 'pwa-offline';
            bar.className = 'pwa-offline';
            bar.setAttribute('role', 'status');
            bar.textContent = 'Offline — prices and orders resume when the connection is back.';
            document.body.appendChild(bar);
        }
        bar.hidden = !on;
    }
    function resume() {
        // after sleep or a network change the socket is usually dead: reconnect now, not on its backoff timer
        if (typeof window.connectWS === 'function') window.connectWS();
        if (typeof window.loadStatus === 'function') window.loadStatus();
    }
    window.addEventListener('offline', () => offlineBanner(true));
    window.addEventListener('online', () => { offlineBanner(false); resume(); });
    document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') resume(); });
    window.addEventListener('pageshow', e => { if (e.persisted) resume(); });

    // ── push alerts ────────────────────────────────────────────────────
    const CATS = [
        ['trades', 'Trades', 'Entries filled and positions closed'],
        ['risk', 'Risk', 'Failed exits, positions without a stop, kill switch, rejected orders'],
        ['signals', 'Day Trade signals', 'New setups on the Day Trade board'],
    ];
    const pushSupported = () => 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
    const csrf = () => document.body.dataset.csrf || '';

    async function api(method, path, body) {
        const res = await fetch(path, {
            method, credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf() },
            body: body ? JSON.stringify(body) : undefined,
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.error || data.detail || `HTTP ${res.status}`);
        return data;
    }
    function keyBytes(b64) {
        const pad = '='.repeat((4 - b64.length % 4) % 4);
        return Uint8Array.from(atob((b64 + pad).replace(/-/g, '+').replace(/_/g, '/')), c => c.charCodeAt(0));
    }
    async function currentSub() {
        const reg = registration || await navigator.serviceWorker.ready;
        return reg.pushManager.getSubscription();
    }
    async function enablePush(prefs) {
        if (Notification.permission !== 'granted' && await Notification.requestPermission() !== 'granted') {
            throw new Error('Notifications are blocked for this app - allow them in the phone or browser settings.');
        }
        const { key } = await api('GET', '/api/push/key');
        const reg = registration || await navigator.serviceWorker.ready;
        let sub = await reg.pushManager.getSubscription();
        if (!sub) sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(key) });
        return api('POST', '/api/push/subscribe', { subscription: sub.toJSON(), prefs });
    }
    async function disablePush() {
        const sub = await currentSub();
        if (!sub) return;
        await api('POST', '/api/push/unsubscribe', { endpoint: sub.endpoint }).catch(() => null);
        await sub.unsubscribe().catch(() => null);
    }
    async function syncPushState() {
        const btn = $('pwa-alerts-btn');
        if (!btn || !pushSupported() || !document.body.dataset.csrf) return;
        btn.hidden = false;
        try {
            let sub = await currentSub();
            if (sub && sub.options && sub.options.applicationServerKey) {
                // the server's key changed (restored server, new key): this subscription can no longer be used
                const { key } = await api('GET', '/api/push/key');
                const mine = btoa(String.fromCharCode(...new Uint8Array(sub.options.applicationServerKey)))
                    .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
                if (mine !== key) {
                    const prefs = (await api('GET', `/api/push/devices?endpoint=${encodeURIComponent(sub.endpoint)}`)).prefs;
                    await sub.unsubscribe();
                    sub = null;
                    if (Notification.permission === 'granted') await enablePush(prefs || undefined).catch(() => null);
                    sub = await currentSub();
                }
            }
            btn.classList.toggle('on', !!sub && Notification.permission === 'granted');
            if (sub) await api('POST', '/api/push/subscribe', { subscription: sub.toJSON() }).catch(() => null);
        } catch (e) { /* not available in this browser */ }
    }
    // signing out removes this device's alerts (a shared phone must not keep getting them)
    window.vqBeforeLogout = async () => { if (pushSupported()) await disablePush().catch(() => null); };

    function settingsModal() {
        let el = $('modal-alerts');
        if (el) return el;
        el = document.createElement('div');
        el.id = 'modal-alerts';
        el.className = 'modal-overlay';
        el.addEventListener('click', e => { if (e.target === el) window.hideModal('modal-alerts'); });
        el.innerHTML = `<div class="modal" role="dialog" aria-modal="true" aria-labelledby="alerts-title">
            <div class="pwa-modal-head"><h3 class="modal-title" id="alerts-title">Alerts on this device</h3>
                <button class="pwa-close" type="button" aria-label="Close" data-alerts-close>&times;</button></div>
            <p class="pwa-note">Sent even when the app is closed. Lock-screen alerts name the symbol and what happened, never amounts.</p>
            <div class="pwa-cats">${CATS.map(([k, label, hint]) => `
                <label class="pwa-cat"><input type="checkbox" data-cat="${k}"><span><b>${label}</b><small>${hint}</small></span></label>`).join('')}
            </div>
            <div class="pwa-status" id="alerts-status" role="status"></div>
            <div class="pwa-actions">
                <button class="btn btn-outline btn-sm" type="button" data-alerts-off>Turn off</button>
                <button class="btn btn-outline btn-sm" type="button" data-alerts-test>Send a test</button>
                <button class="btn btn-primary btn-sm" type="button" data-alerts-save>Save</button>
            </div></div>`;
        document.body.appendChild(el);
        const status = (msg, bad) => { const s = $('alerts-status'); s.textContent = msg; s.classList.toggle('bad', !!bad); };
        const prefs = () => Object.fromEntries([...el.querySelectorAll('[data-cat]')].map(c => [c.dataset.cat, c.checked]));
        el.querySelector('[data-alerts-close]').addEventListener('click', () => window.hideModal('modal-alerts'));
        el.querySelector('[data-alerts-save]').addEventListener('click', async () => {
            try { await enablePush(prefs()); status('Alerts are on for this device.'); syncPushState(); }
            catch (e) { status(e.message, true); }
        });
        el.querySelector('[data-alerts-off]').addEventListener('click', async () => {
            await disablePush(); status('Alerts are off for this device.'); $('pwa-alerts-btn')?.classList.remove('on');
        });
        el.querySelector('[data-alerts-test]').addEventListener('click', async () => {
            try { const r = await api('POST', '/api/push/test'); status(r.delivered ? 'Test sent - it should arrive in a few seconds.' : 'No device received it: save first.', !r.delivered); }
            catch (e) { status(e.message, true); }
        });
        return el;
    }
    document.addEventListener('click', async e => {
        if (!e.target.closest('#pwa-alerts-btn')) return;
        const el = settingsModal();
        const defaults = { trades: true, risk: true, signals: false };
        let saved = null;
        try {
            const sub = await currentSub();
            if (sub) saved = (await api('GET', `/api/push/devices?endpoint=${encodeURIComponent(sub.endpoint)}`)).prefs;
        } catch (err) { saved = null; }
        el.querySelectorAll('[data-cat]').forEach(c => { c.checked = (saved || defaults)[c.dataset.cat]; });
        $('alerts-status').textContent = Notification.permission === 'denied'
            ? 'Notifications are blocked for this app - allow them in settings first.' : '';
        window.showModal('modal-alerts');
    });
})();
