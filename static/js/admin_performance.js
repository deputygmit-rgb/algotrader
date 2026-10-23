// Admin panel: strategy performance of the Day Trade and Swing setups
// (/api/admin/performance, built by src/analysis/performance_report.py).
// Every value is written as text, never parsed as HTML.
(function () {
    'use strict';
    const $ = id => document.getElementById(id);
    const root = $('adm-perf');
    if (!root) return;
    let report = null, board = 'day', sort = { key: 'expectancy_r', dir: -1 };

    function el(tag, attrs, ...children) {
        const node = document.createElement(tag);
        Object.entries(attrs || {}).forEach(([k, v]) => {
            if (k === 'class') node.className = v;
            else if (k === 'style') node.setAttribute('style', v);
            else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
            else if (v !== false && v !== null && v !== undefined) node.setAttribute(k, v === true ? '' : v);
        });
        children.flat().forEach(c => node.append(c instanceof Node ? c : document.createTextNode(String(c ?? ''))));
        return node;
    }
    const fmt = (v, d = 2, sign = true) => v === null || v === undefined ? '–'
        : `${sign && v > 0 ? '+' : ''}${Number(v).toFixed(d)}`;
    const tone = v => v === null || v === undefined ? '' : v > 0 ? 'perf-up' : v < 0 ? 'perf-down' : '';
    const VERDICT = { 'proven edge': 'good', 'positive, unproven': 'ok', 'positive, fading': 'warn', 'no edge': 'warn',
                      'losing': 'bad', 'suspended': 'bad', 'too few trades': 'muted' };

    async function call(method, url, body) {
        const res = await fetch(url, { method, headers: body ? { 'Content-Type': 'application/json' } : {},
                                       body: body ? JSON.stringify(body) : undefined });
        const json = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(json.detail || json.message || `Request failed (${res.status})`);
        return json;
    }
    function flash(text, kind) {
        const box = $('adm-flash');
        if (!box) return;
        box.textContent = text;
        box.className = `adm-flash ${kind || 'info'}`;
        box.hidden = false;
        setTimeout(() => { box.hidden = true; }, 6000);
    }

    // ── pieces ─────────────────────────────────────────────────────────
    function kpis(o) {
        if (!o || !o.n) return el('p', { class: 'adm-muted' }, 'No closed trades in this window yet.');
        const tile = (label, value, cls, hint) => el('div', { class: 'perf-kpi', title: hint || '' },
            el('span', {}, label), el('b', { class: cls || '' }, value));
        return el('div', { class: 'perf-kpis' },
            tile('Trades', String(o.n), '', `${o.days} sessions`),
            tile('Win rate', `${o.win_rate}%`),
            tile('Expectancy', `${fmt(o.expectancy_r, 3)}R`, tone(o.expectancy_r), `95% CI ${fmt(o.ci95[0])} … ${fmt(o.ci95[1])}R`),
            tile('Profit factor', fmt(o.profit_factor, 2, false), o.profit_factor >= 1 ? 'perf-up' : 'perf-down'),
            tile('Total', `${fmt(o.total_r, 1)}R`, tone(o.total_r)),
            tile('Max drawdown', `${fmt(o.max_dd_r, 1, false)}R`, 'perf-down'),
            tile('Sharpe (daily)', fmt(o.sharpe_daily, 2, false), tone(o.sharpe_daily)),
            tile('SQN', fmt(o.sqn, 2, false), tone(o.sqn)),
            tile('Cost drag', o.cost_drag === null ? '–' : `${o.cost_drag}%`, '', 'slippage and charges as a share of the gross move'),
            tile('Last 20', `${fmt(o.recent_r, 2)}R`, tone(o.recent_r), 'mean R of the 20 most recent trades'));
    }

    function curve(daily) {
        if (!daily.length) return el('div');
        const W = 900, H = 160, P = 8;
        const cum = daily.map(d => d.cum_r), net = daily.map(d => d.net_r);
        const lo = Math.min(0, ...cum), hi = Math.max(0, ...cum);
        const span = hi - lo || 1, step = (W - 2 * P) / Math.max(daily.length - 1, 1);
        const y = v => P + (hi - v) / span * (H - 2 * P);
        const maxBar = Math.max(...net.map(Math.abs), 0.01);
        const ns = 'http://www.w3.org/2000/svg';
        const svg = document.createElementNS(ns, 'svg');
        svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
        svg.setAttribute('class', 'perf-curve');
        svg.setAttribute('preserveAspectRatio', 'none');        // full width; the line keeps its stroke (non-scaling)
        svg.setAttribute('role', 'img');
        svg.setAttribute('aria-label', `Cumulative R over ${daily.length} sessions, ending at ${fmt(cum[cum.length - 1], 1)}R`);
        const add = (tag, attrs) => { const n = document.createElementNS(ns, tag); Object.entries(attrs).forEach(([k, v]) => n.setAttribute(k, v)); svg.append(n); return n; };
        add('line', { x1: P, x2: W - P, y1: y(0), y2: y(0), class: 'perf-zero' });
        daily.forEach((d, i) => {
            const h = Math.abs(d.net_r) / maxBar * (H / 3);
            const bar = add('rect', { x: P + i * step - Math.max(step * .3, 1), width: Math.max(step * .6, 2),
                                      y: d.net_r >= 0 ? y(0) - h : y(0), height: Math.max(h, 0.5),
                                      class: d.net_r >= 0 ? 'perf-bar-up' : 'perf-bar-down' });
            const t = document.createElementNS(ns, 'title');
            t.textContent = `${d.date}: ${d.trades} trades, net ${fmt(d.net_r)}R, cumulative ${fmt(d.cum_r, 1)}R`;
            bar.append(t);
        });
        add('polyline', { points: cum.map((v, i) => `${P + i * step},${y(v)}`).join(' '), class: 'perf-line' });
        return el('div', { class: 'perf-curve-wrap' }, svg,
            el('div', { class: 'perf-legend' }, 'Cumulative R (line) and each session\'s net R (bars) · ',
               `${daily[0].date} → ${daily[daily.length - 1].date}`));
    }

    const COLS = [
        ['setup', 'Setup'], ['n', 'N'], ['win_rate', 'Win %'], ['expectancy_r', 'Exp R'], ['ci95', '95% CI'],
        ['profit_factor', 'PF'], ['payoff', 'Payoff'], ['sqn', 'SQN'], ['total_r', 'Total R'], ['max_dd_r', 'Max DD'],
        ['losing_streak', 'Loss run'], ['avg_mfe_r', 'MFE'], ['avg_mae_r', 'MAE'], ['capture', 'Capture'],
        ['avg_cost_r', 'Cost R'], ['avg_hold_bars', 'Hold'], ['recent_r', 'Last 20'], ['adj', 'Q adj'], ['verdict', 'Verdict'],
    ];
    function setupTable(setups) {
        const rows = setups.filter(s => s.n).slice().sort((a, b) => {
            const va = sort.key === 'adj' ? (a.efficacy || {}).adj : sort.key === 'setup' ? `${a.setup} ${a.side}` : a[sort.key];
            const vb = sort.key === 'adj' ? (b.efficacy || {}).adj : sort.key === 'setup' ? `${b.setup} ${b.side}` : b[sort.key];
            if (va === vb) return 0;
            if (va === null || va === undefined) return 1;
            if (vb === null || vb === undefined) return -1;
            return (va > vb ? 1 : -1) * sort.dir;
        });
        if (!rows.length) return el('p', { class: 'adm-muted' }, 'No closed trades in this window.');
        const head = el('tr', {}, COLS.map(([key, label]) => el('th', {
            class: `perf-sort${sort.key === key ? (sort.dir > 0 ? ' asc' : ' desc') : ''}`, tabindex: 0, scope: 'col',
            'aria-sort': sort.key === key ? (sort.dir > 0 ? 'ascending' : 'descending') : 'none',
            onclick: () => { sort = { key, dir: sort.key === key ? -sort.dir : -1 }; render(); },
            onkeydown: e => { if (e.key === 'Enter') e.target.click(); },
        }, label)));
        const body = rows.map(s => {
            const exits = Object.entries(s.exits || {}).sort((a, b) => b[1] - a[1]).map(([k, v]) => `${k} ${v}`).join(' · ');
            return el('tr', { title: exits ? `Exits: ${exits}` : '' },
                el('td', { class: 'perf-name' }, `${s.setup} `, el('small', {}, s.side)),
                el('td', {}, String(s.n)), el('td', {}, `${s.win_rate}%`),
                el('td', { class: tone(s.expectancy_r) }, fmt(s.expectancy_r, 3)),
                el('td', { class: 'adm-muted' }, `${fmt(s.ci95[0])} … ${fmt(s.ci95[1])}`),
                el('td', {}, fmt(s.profit_factor, 2, false)), el('td', {}, fmt(s.payoff, 2, false)),
                el('td', { class: tone(s.sqn) }, fmt(s.sqn, 2, false)), el('td', { class: tone(s.total_r) }, fmt(s.total_r, 1)),
                el('td', { class: 'perf-down' }, fmt(s.max_dd_r, 1, false)), el('td', {}, String(s.losing_streak)),
                el('td', {}, fmt(s.avg_mfe_r, 2, false)), el('td', {}, fmt(s.avg_mae_r, 2, false)),
                el('td', { class: tone(s.capture) }, s.capture === null ? '–' : `${s.capture}%`),
                el('td', {}, fmt(s.avg_cost_r, 3, false)), el('td', {}, fmt(s.avg_hold_bars, 1, false)),
                el('td', { class: tone(s.recent_r) }, fmt(s.recent_r, 2)),
                el('td', {}, s.efficacy ? fmt(s.efficacy.adj, 0) : '–'),
                el('td', {}, el('span', { class: `perf-pill ${VERDICT[s.verdict] || ''}` }, s.verdict)));
        });
        return el('div', { class: 'adm-table-wrap' }, el('table', { class: 'adm-table perf-table' }, el('thead', {}, head), el('tbody', {}, body)));
    }

    function heatmap(daily, setups) {
        const days = daily.slice(-15);
        const names = setups.filter(s => s.n).map(s => `${s.setup} ${s.side}`);
        if (!days.length || !names.length) return el('div');
        const max = Math.max(0.5, ...days.flatMap(d => Object.values(d.setups).map(Math.abs)));
        const cell = v => {
            if (v === undefined) return el('td', { class: 'perf-heat empty' }, '');
            const a = Math.min(Math.abs(v) / max, 1) * .55 + .08;
            const color = v >= 0 ? `rgba(91,227,160,${a})` : `rgba(255,90,110,${a})`;
            return el('td', { class: 'perf-heat', style: `background:${color}`, title: `${fmt(v)}R` }, fmt(v, 1));
        };
        return el('div', { class: 'adm-table-wrap' }, el('table', { class: 'adm-table perf-heatmap' },
            el('thead', {}, el('tr', {}, el('th', { scope: 'col' }, 'Setup'), days.map(d => el('th', { scope: 'col' }, d.date.slice(5))))),
            el('tbody', {}, names.map(n => el('tr', {}, el('th', { scope: 'row', class: 'perf-name' }, n),
                days.map(d => cell(d.setups[n])))))));
    }

    function dayTable(daily) {
        if (!daily.length) return el('div');
        return el('div', { class: 'adm-table-wrap perf-scroll' }, el('table', { class: 'adm-table' },
            el('thead', {}, el('tr', {}, ['Date', 'Trades', 'Win', 'Net R', 'Cum R', 'Best setup', 'Worst setup'].map(h => el('th', { scope: 'col' }, h)))),
            el('tbody', {}, daily.slice().reverse().map(d => el('tr', {},
                el('td', {}, d.date), el('td', {}, String(d.trades)), el('td', {}, `${Math.round(d.wins / d.trades * 100)}%`),
                el('td', { class: tone(d.net_r) }, fmt(d.net_r)), el('td', { class: tone(d.cum_r) }, fmt(d.cum_r, 1)),
                el('td', {}, `${d.best[0]} ${fmt(d.best[1])}`), el('td', {}, `${d.worst[0]} ${fmt(d.worst[1])}`))))));
    }

    function liveTable(live) {
        if (!live.length) return el('p', { class: 'adm-muted' }, 'No live board signals recorded in this window.');
        return el('div', { class: 'adm-table-wrap perf-scroll' }, el('table', { class: 'adm-table' },
            el('thead', {}, el('tr', {}, ['Date', 'Signals', 'Taken', 'Skipped', 'Closed', 'Win', 'Net R', 'Orders', 'Accounts'].map(h => el('th', { scope: 'col' }, h)))),
            el('tbody', {}, live.slice().reverse().map(d => el('tr', {},
                el('td', {}, d.date), el('td', {}, String(d.signals)), el('td', {}, String(d.taken)), el('td', {}, String(d.skipped)),
                el('td', {}, String(d.closed)), el('td', {}, d.closed ? `${Math.round(d.wins / d.closed * 100)}%` : '–'),
                el('td', { class: tone(d.net_r) }, fmt(d.net_r)), el('td', {}, String(d.orders)), el('td', {}, String(d.accounts)))))));
    }

    function paramTable(params) {
        if (!params.length) return el('div');
        const shared = Object.entries(params[0]).filter(([k]) => !['setup', 'label', 'enabled', 'volume_min', 'long', 'short', 'stop'].includes(k));
        return el('details', { class: 'perf-params' }, el('summary', {}, 'Parameters, as the engine runs them today'),
            el('ul', {}, shared.map(([k, v]) => el('li', {}, el('b', {}, `${k.replace(/_/g, ' ')}: `), String(v)))),
            el('div', { class: 'adm-table-wrap' }, el('table', { class: 'adm-table' },
                el('thead', {}, el('tr', {}, ['Setup', 'On', 'Volume', 'Long', 'Short', 'Stop'].map(h => el('th', { scope: 'col' }, h)))),
                el('tbody', {}, params.map(p => el('tr', {},
                    el('td', { class: 'perf-name' }, `${p.setup} `, el('small', {}, p.label)), el('td', {}, p.enabled ? 'yes' : 'no'),
                    el('td', {}, p.volume_min), el('td', {}, p.long), el('td', {}, p.short), el('td', {}, p.stop)))))));
    }

    function render() {
        const b = report && report.boards[board];
        if (!b) return;
        root.querySelectorAll('[data-board]').forEach(t => t.setAttribute('aria-selected', String(t.dataset.board === board)));
        const findings = (report.findings || []).filter(f => f.startsWith(board === 'day' ? 'Day Trade' : 'Swing'));
        $('perf-body').replaceChildren(
            findings.length ? el('ul', { class: 'perf-findings' }, findings.map(f => el('li', {}, f))) : '',
            kpis(b.overall), curve(b.daily),
            el('h3', { class: 'perf-h' }, 'Per setup and side'), setupTable(b.setups),
            el('h3', { class: 'perf-h' }, 'Setup × day (net R, last 15 sessions)'), heatmap(b.daily, b.setups),
            el('h3', { class: 'perf-h' }, 'Day by day'), dayTable(b.daily),
            el('h3', { class: 'perf-h' }, 'Live board (signals shown to users)'), liveTable(b.live),
            paramTable(b.parameters));
    }

    async function load() {
        const days = Number($('perf-days').value || 60);
        $('perf-md').href = `/api/admin/performance.md?days=${days}`;
        try {
            report = await call('GET', `/api/admin/performance?days=${days}`);
            $('perf-asof').textContent = report.as_of ? `replay of ${report.as_of} · ${report.since} → ${report.today}`
                : 'no replay saved yet: it is built nightly after the close';
            render();
        } catch (e) {
            $('perf-body').replaceChildren(el('p', { class: 'adm-muted' }, e.message));
        }
    }

    async function archive() {
        try {
            const { reports } = await call('GET', '/api/admin/performance/archive');
            $('perf-archive').replaceChildren(el('option', { value: '' }, reports.length ? 'saved reports…' : 'none yet'),
                reports.map(r => el('option', { value: r.name }, r.date)));
        } catch (e) { /* the archive is optional */ }
    }

    [30, 60, 90, 120].forEach(d => $('perf-days').append(el('option', { value: d, selected: d === 60 }, `${d} days`)));
    $('perf-days').addEventListener('change', load);
    root.querySelectorAll('[data-board]').forEach(t => t.addEventListener('click', () => { board = t.dataset.board; render(); }));
    // downloads go through fetch (secure_fetch.js asks for a confirmation code if it has expired)
    async function download(url, fallbackName) {
        try {
            const res = await fetch(url);
            if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `Download failed (${res.status})`);
            const name = (res.headers.get('Content-Disposition') || '').match(/filename="?([^";]+)"?/)?.[1] || fallbackName;
            const href = URL.createObjectURL(await res.blob());
            const a = el('a', { href, download: name });
            document.body.append(a);
            a.click();
            a.remove();
            setTimeout(() => URL.revokeObjectURL(href), 1000);
        } catch (e) { flash(e.message, 'error'); }
    }
    $('perf-md').addEventListener('click', e => {
        e.preventDefault();
        const days = Number($('perf-days').value || 60);
        download(`/api/admin/performance.md?days=${days}`, `verquoai-performance-${days}d.md`);
    });
    $('perf-archive').addEventListener('change', e => {
        if (e.target.value) download(`/api/admin/performance/archive/${encodeURIComponent(e.target.value)}`, e.target.value);
        e.target.value = '';
    });
    $('perf-email').addEventListener('click', async () => {
        const btn = $('perf-email');
        btn.disabled = true;
        try {
            const r = await call('POST', '/api/admin/performance/email', { days: Number($('perf-days').value || 60) });
            flash(`Report emailed to ${r.to}.`, 'success');
        } catch (e) { flash(e.message, 'error'); }
        btn.disabled = false;
    });
    load();
    archive();
})();
