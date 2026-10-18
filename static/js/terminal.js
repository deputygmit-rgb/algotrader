// Terminal chrome: IST clock + market session, ticker tape, command palette.
const Terminal = (() => {
    const TAPE_SYMBOLS = [
        'NIFTY 50', 'NIFTY BANK', 'NIFTY FIN SERVICE', 'INDIA VIX', 'NIFTY IT',
        'RELIANCE', 'HDFCBANK', 'ICICIBANK', 'INFY', 'TCS', 'BHARTIARTL', 'SBIN', 'ITC', 'LT', 'AXISBANK',
    ];
    const IST = new Intl.DateTimeFormat('en-GB', {
        timeZone: 'Asia/Kolkata', hour12: false, weekday: 'short',
        hour: '2-digit', minute: '2-digit', second: '2-digit',
    });
    const esc = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

    // ── clock & session ────────────────────────────────────────────────
    function istNow() {
        const parts = Object.fromEntries(IST.formatToParts(new Date()).map(p => [p.type, p.value]));
        return { day: parts.weekday, hh: +parts.hour, mm: +parts.minute, text: `${parts.hour}:${parts.minute}:${parts.second}` };
    }

    // Exchange holidays aren't known client-side, so a weekday holiday shows OPEN.
    function session(t) {
        if (t.day === 'Sat' || t.day === 'Sun') return ['closed', 'MARKET CLOSED · WEEKEND'];
        const m = t.hh * 60 + t.mm;
        if (m >= 540 && m < 555) return ['pre', 'PRE-OPEN'];
        if (m >= 555 && m < 930) return ['open', 'MARKET OPEN'];
        if (m >= 930 && m < 960) return ['pre', 'POST-CLOSE'];
        return ['closed', 'MARKET CLOSED'];
    }

    function tick() {
        const t = istNow();
        const [cls, label] = session(t);
        document.querySelectorAll('[data-t-clock]').forEach(el => { el.textContent = `${t.text} IST`; });
        document.querySelectorAll('[data-t-session]').forEach(el => {
            el.className = 't-session ' + cls;
            el.innerHTML = `<i></i>${label}`;
        });
    }

    // ── ticker tape ────────────────────────────────────────────────────
    async function loadTape() {
        const tape = document.getElementById('t-tape');
        if (!tape || document.hidden) return;
        try {
            const res = await fetch('/api/market/overview?symbols=' + encodeURIComponent(TAPE_SYMBOLS.join(',')));
            const data = await res.json();
            const rows = data.market_overview || [];
            if (!res.ok || data.error || !rows.length) { tape.hidden = true; return; }
            rows.sort((a, b) => TAPE_SYMBOLS.indexOf(a.symbol) - TAPE_SYMBOLS.indexOf(b.symbol));
            const item = r => {
                const up = r.change_pct >= 0;
                const col = up ? 'var(--t-g)' : 'var(--t-r)';
                return `<span class="t-tape-item" data-stock="${esc(r.symbol)}" title="Open analysis"><b>${esc(r.symbol)}</b><span class="px">${Number(r.last_price).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>` +
                    `<span style="color:${col}">${up ? '▲' : '▼'} ${up ? '+' : ''}${r.change_pct.toFixed(2)}%</span></span>`;
            };
            const html = rows.map(item).join('');
            document.getElementById('t-tape-track').innerHTML = html + html;   // doubled for a seamless loop
            tape.hidden = false;
        } catch (e) {
            tape.hidden = true;
        }
    }

    // ── command palette (Ctrl+K or /) ──────────────────────────────────
    let palIndex = 0;
    function palItems(q) {
        return [...document.querySelectorAll('.tabs .tab')]
            .map((b, i) => ({ b, label: b.textContent.trim(), key: i < 9 ? `ALT+${i + 1}` : '' }))
            .filter(x => x.label.toLowerCase().includes(q.toLowerCase()));
    }
    function palRender() {
        const q = document.getElementById('t-pal-input').value;
        const items = palItems(q);
        palIndex = Math.min(palIndex, Math.max(0, items.length - 1));
        document.getElementById('t-pal-list').innerHTML = items
            .map((x, i) => `<li class="${i === palIndex ? 'on' : ''}" data-i="${i}"><span>${esc(x.label)}</span><span>${x.key}</span></li>`)
            .join('') || '<li><span>No match</span><span></span></li>';
        return items;
    }
    function palOpen() {
        const ov = document.getElementById('t-pal');
        ov.classList.add('show');
        const input = document.getElementById('t-pal-input');
        input.value = ''; palIndex = 0; palRender(); input.focus();
    }
    function palClose() { document.getElementById('t-pal').classList.remove('show'); }
    function palPick(i) {
        const items = palItems(document.getElementById('t-pal-input').value);
        if (items[i]) { palClose(); items[i].b.click(); items[i].b.scrollIntoView({ inline: 'center', block: 'nearest' }); }
    }

    function bindPalette() {
        const ov = document.getElementById('t-pal');
        const input = document.getElementById('t-pal-input');
        ov.addEventListener('click', e => {
            const li = e.target.closest('li[data-i]');
            if (li) palPick(+li.dataset.i); else if (e.target === ov) palClose();
        });
        input.addEventListener('input', () => { palIndex = 0; palRender(); });
        input.addEventListener('keydown', e => {
            const n = palItems(input.value).length;
            if (e.key === 'ArrowDown') { palIndex = (palIndex + 1) % Math.max(n, 1); palRender(); e.preventDefault(); }
            else if (e.key === 'ArrowUp') { palIndex = (palIndex - 1 + n) % Math.max(n, 1); palRender(); e.preventDefault(); }
            else if (e.key === 'Enter') { palPick(palIndex); e.preventDefault(); }
            else if (e.key === 'Escape') { palClose(); }
        });
        document.addEventListener('keydown', e => {
            const typing = /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement?.tagName) || document.activeElement?.isContentEditable;
            if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); palOpen(); }
            else if (e.key === '/' && !typing) { e.preventDefault(); palOpen(); }
            else if (e.altKey && /^[1-9]$/.test(e.key)) {
                const tab = document.querySelectorAll('.tabs .tab')[+e.key - 1];
                if (tab) { e.preventDefault(); tab.click(); }
            }
        });
    }

    function init() {
        tick();
        setInterval(tick, 1000);
        bindPalette();
        loadTape();
        setInterval(loadTape, 15000);
        document.addEventListener('visibilitychange', () => { if (!document.hidden) loadTape(); });
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
    return { open: palOpen };
})();

// Privacy mask for account details, P&L, and report data are blurred until
// clicked. A click reveals that tile (or that table's masked columns) for
// REVEAL_MS; the header button / Alt+P turns masking off altogether. This is
// a visual mask for screen-sharing — the values are still in the page.
const Privacy = (() => {
    const REVEAL_MS = 30000;
    const STORE_KEY = 't-privacy';
    // zone = element that survives re-renders; cells = masked parts inside it
    const ZONES = [
        { sel: '#t-zid' },
        { sel: '#account-name' }, { sel: '#account-id' },
        { sel: '#available-margin' }, { sel: '#used-margin' },
        { sel: '#holdings-value' }, { sel: '#positions-pnl' },
        { sel: '#daily-pnl' }, { sel: '#daily-pnl-label' }, { sel: '#daily-pnl-bot' },
        { sel: '#total-pnl' }, { sel: '#total-pnl-label' },
        { sel: '#exposure' }, { sel: '#exposure-label' },
        // Qty (2) and P&L (5) columns
        { sel: '#overview-positions-body', cells: ['td:nth-child(2)', 'td:nth-child(5)'] },
        { sel: '#overview-holdings-body', cells: ['td:nth-child(2)', 'td:nth-child(5)'] },
        { sel: '#pnl-summary-cards', cells: ['.stat-value'] },
        { sel: '#holdings-summary-cards', cells: ['.stat-value'] },
        { sel: '#positions-summary-cards', cells: ['.stat-value'] },
        { sel: '#trades-summary-cards', cells: ['.stat-value'] },
        { sel: '#orders-summary-cards', cells: ['.stat-value'] },
        { sel: '#margins-cards', cells: ['.stat-value'] },
        { sel: '#pnl-details', cells: ['.privacy-sensitive'] },
        { sel: '#report-holdings-body', cells: ['td:nth-child(1)', 'td:nth-child(2)', 'td:nth-child(3)', 'td:nth-child(4)', 'td:nth-child(5)', 'td:nth-child(6)', 'td:nth-child(7)', 'td:nth-child(8)'] },
        { sel: '#report-positions-body', cells: ['td:nth-child(1)', 'td:nth-child(2)', 'td:nth-child(3)', 'td:nth-child(4)', 'td:nth-child(5)', 'td:nth-child(6)', 'td:nth-child(7)'] },
        { sel: '#report-trades-body', cells: ['td:nth-child(1)', 'td:nth-child(2)', 'td:nth-child(3)', 'td:nth-child(4)', 'td:nth-child(5)', 'td:nth-child(6)', 'td:nth-child(7)'] },
        { sel: '#report-orders-body', cells: ['td:nth-child(1)', 'td:nth-child(2)', 'td:nth-child(3)', 'td:nth-child(4)', 'td:nth-child(5)', 'td:nth-child(6)', 'td:nth-child(7)'] },
    ];
    const timers = new WeakMap();

    function readSetting() {
        try { return localStorage.getItem(STORE_KEY) !== 'off'; } catch (e) { return true; }
    }
    function writeSetting(on) {
        try { localStorage.setItem(STORE_KEY, on ? 'on' : 'off'); } catch (e) { /* not persisted */ }
    }

    function injectCss() {
        const masked = ZONES.flatMap(z => z.cells
            ? z.cells.map(c => `body.t-private ${z.sel}:not(.t-revealed) ${c}`)
            : [`body.t-private ${z.sel}:not(.t-revealed)`]);
        const style = document.createElement('style');
        style.textContent = `${masked.join(',\n')} {
    filter: blur(7px); user-select: none; cursor: pointer; transition: filter .2s;
}`;
        document.head.appendChild(style);
    }

    function apply(on) {
        document.body.classList.toggle('t-private', on);
        const btn = document.getElementById('t-privacy-btn');
        if (btn) {
            btn.textContent = on ? 'MASK: ON' : 'MASK: OFF';
            btn.classList.toggle('btn-primary', on);
            btn.classList.toggle('btn-outline', !on);
        }
    }

    function toggle() {
        const on = !document.body.classList.contains('t-private');
        writeSetting(on);
        apply(on);
    }

    // Masked part the click landed on, if any → its zone element
    function maskedZoneAt(target) {
        for (const z of ZONES) {
            const zone = target.closest(z.sel);
            if (!zone || zone.classList.contains('t-revealed')) continue;
            if (!z.cells || z.cells.some(c => target.closest(c) && zone.contains(target.closest(c)))) return zone;
        }
        return null;
    }

    function reveal(zone) {
        zone.classList.add('t-revealed');
        clearTimeout(timers.get(zone));
        timers.set(zone, setTimeout(() => zone.classList.remove('t-revealed'), REVEAL_MS));
    }

    function init() {
        injectCss();
        apply(readSetting());
        document.getElementById('t-privacy-btn')?.addEventListener('click', toggle);
        // capture phase, so revealing a masked cell does not also open the row's analysis
        document.addEventListener('click', (e) => {
            if (!document.body.classList.contains('t-private')) return;
            const zone = maskedZoneAt(e.target);
            if (!zone) return;
            e.preventDefault();
            e.stopPropagation();
            reveal(zone);
        }, true);
        document.addEventListener('keydown', (e) => {
            if (e.altKey && e.key.toLowerCase() === 'p') { e.preventDefault(); toggle(); }
        });
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
    return { toggle };
})();
