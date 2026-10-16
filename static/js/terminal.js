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
                return `<span class="t-tape-item"><b>${esc(r.symbol)}</b><span class="px">${Number(r.last_price).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>` +
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
