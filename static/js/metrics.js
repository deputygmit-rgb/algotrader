// Usage metrics: page views, clicks and tab switches, batched to /metrics/events.
// Records what was clicked (button label, tab, stock symbol) - never what was typed.
// Visitors with Do Not Track / Global Privacy Control enabled are not tracked.
(function () {
    'use strict';
    if (navigator.doNotTrack === '1' || window.doNotTrack === '1' || navigator.globalPrivacyControl) {
        window.vqTrack = () => {};
        return;
    }
    const ENDPOINT = '/metrics/events';
    const page = location.pathname.replace(/^\/app\/stock\/[^/]+/, '/app/stock/:symbol');
    const queue = [];

    function flush(leaving) {
        if (!queue.length) return;
        const body = JSON.stringify({ events: queue.splice(0, 50) });
        if (leaving && navigator.sendBeacon) {
            navigator.sendBeacon(ENDPOINT, new Blob([body], { type: 'application/json' }));
            return;
        }
        fetch(ENDPOINT, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body, keepalive: true })
            .catch(() => {});
    }

    function track(event, target, detail) {
        queue.push({ event, page, target: String(target || '').slice(0, 80), detail: detail || {} });
        if (queue.length >= 20) flush(false);
    }

    function label(el) {
        if (el.dataset.track) return el.dataset.track;
        if (el.dataset.tab) return `tab:${el.dataset.tab}`;
        if (el.dataset.stock) return `stock:${el.dataset.stock}`;
        if (el.dataset.sym) return `stock:${el.dataset.sym}`;
        if (el.hasAttribute('data-start-demo')) return `start-demo:${el.dataset.startDemo || 'home'}`;
        const text = (el.getAttribute('aria-label') || el.textContent || '').replace(/\s+/g, ' ').trim();
        return text.slice(0, 60) || el.id || el.tagName.toLowerCase();
    }

    document.addEventListener('click', (e) => {
        const el = e.target.closest('button, a, [role="button"], [data-track], [data-stock], [data-sym]');
        if (!el) return;
        const detail = {};
        if (el.tagName === 'A' && el.getAttribute('href')) {
            const url = new URL(el.href, location.href);
            detail.to = url.origin === location.origin ? url.pathname + url.hash : url.hostname;
        }
        track(el.dataset.tab ? 'tab_view' : 'click', label(el), detail);
    }, true);

    const params = new URLSearchParams(location.search);
    const detail = { w: window.innerWidth };
    if (document.referrer) {
        try {
            const ref = new URL(document.referrer);
            if (ref.origin !== location.origin) detail.ref = ref.hostname;
        } catch (e) { /* ignore malformed referrer */ }
    }
    ['utm_source', 'utm_medium', 'utm_campaign'].forEach(k => { if (params.get(k)) detail[k] = params.get(k).slice(0, 60); });
    track('page_view', document.title.slice(0, 80), detail);

    const opened = Date.now();
    setInterval(() => flush(false), 15000);
    window.addEventListener('pagehide', () => {
        track('page_leave', '', { seconds: Math.round((Date.now() - opened) / 1000) });
        flush(true);
    });
    window.vqTrack = track;
})();
