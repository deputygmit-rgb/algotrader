// Agents view: live state of the Scanner / Verifier / Executor / Tracker
// agents from GET /api/agents/status, plus a header health indicator.
const AgentsView = (() => {
    const AGENTS = {
        scanner: { name: 'SCANNER', icon: '⌖', color: '#38d6ff', role: 'Refreshes the NIFTY 500 technical scan every few minutes during market hours' },
        verifier: { name: 'VERIFIER', icon: '⛨', color: '#b18cff', role: 'Audits broker session, price feed, margin and market state (advisory — does not block orders)' },
        executor: { name: 'EXECUTOR', icon: '⚡', color: '#5be3a0', role: 'Watches the live price feed that drives strategy signals and order execution' },
        tracker: { name: 'TRACKER', icon: '◎', color: '#ff9f43', role: 'Syncs open positions and orders from Zerodha while live trading is on' },
    };
    const ORDER = ['scanner', 'verifier', 'executor', 'tracker'];
    const STATE_TEXT = {
        idle: 'Idle', stopped: 'Stopped', crashed: 'Crashed',
        waiting_market_open: 'Waiting · market closed', waiting_zerodha_auth: 'Waiting · Zerodha login',
        waiting_live_trading: 'Standby · live trading off', waiting_next_cycle: 'Sleeping until next cycle',
        scanning: 'Scanning', checking: 'Auditing', monitoring: 'Monitoring feed', syncing: 'Syncing with broker',
    };
    const ACTIVE = new Set(['scanning', 'checking', 'monitoring', 'syncing']);

    let data = null, clockSkew = 0, pollTimer = null, headerTimer = null, tickTimer = null;
    let filter = 'all', detail = null, scanProg = null, started = false;
    let seenSeq = {};

    const $ = id => document.getElementById(id);
    const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    const isActiveTab = () => $('tab-agents')?.classList.contains('active') && !document.hidden;
    const now = () => Date.now() - clockSkew;
    const ts = iso => iso ? Date.parse(iso) : null;

    function ago(iso) {
        if (!iso) return '—';
        const s = Math.max(0, Math.round((now() - ts(iso)) / 1000));
        if (s < 60) return s + 's ago';
        if (s < 3600) return Math.floor(s / 60) + 'm ' + (s % 60) + 's ago';
        return Math.floor(s / 3600) + 'h ' + Math.floor((s % 3600) / 60) + 'm ago';
    }
    function dur(sec) {
        if (sec == null) return '—';
        if (sec < 10) return (Math.round(sec * 10) / 10) + 's';
        sec = Math.round(sec);
        if (sec < 60) return sec + 's';
        if (sec < 3600) return Math.floor(sec / 60) + 'm ' + (sec % 60) + 's';
        return Math.floor(sec / 3600) + 'h ' + Math.floor((sec % 3600) / 60) + 'm';
    }

    // ── health classification ─────────────────────────────────────────
    function health(key, a) {
        if (!a) return { level: 'off', text: 'No data' };
        if (a.state === 'crashed') return { level: 'bad', text: 'Crashed' };
        if (!a.running) return { level: 'off', text: 'Stopped' };
        if (key === 'executor' && a.state === 'monitoring') {
            if (a.feed_status === 'disconnected') return { level: 'bad', text: 'Feed disconnected' };
            if (a.feed_status === 'stale') return { level: 'warn', text: 'Feed stale' };
        }
        if (key === 'verifier' && a.last_report && !a.last_report.passed && a.state !== 'waiting_live_trading') {
            return { level: 'warn', text: `${(a.last_report.blocked_reasons || []).length} finding(s)` };
        }
        if (a.last_error) return { level: 'warn', text: 'Last cycle failed' };
        if (ACTIVE.has(a.state)) return { level: 'ok', text: 'Working' };
        return { level: 'idle', text: 'Waiting' };
    }

    // ── data ──────────────────────────────────────────────────────────
    async function fetchStatus() {
        try {
            const res = await fetch('/api/agents/status');
            const json = await res.json();
            if (!res.ok || json.error) throw new Error(json.error || res.statusText);
            if (json.server_time) clockSkew = Date.now() - ts(json.server_time);
            data = json;
        } catch (e) {
            data = { error: e.message };
        }
        renderHeader();
        if (isActiveTab()) render();
    }

    function poll() {
        clearTimeout(pollTimer);
        fetchStatus().finally(() => { pollTimer = setTimeout(poll, isActiveTab() ? 2000 : 10000); });
    }

    // ── header indicator (all tabs) ───────────────────────────────────
    function renderHeader() {
        const el = $('t-agents-pill');
        if (!el) return;
        if (!data || data.error) {
            el.innerHTML = '<span class="ag-pill-label">AGENTS</span><span class="ag-pill-dot off"></span>';
            el.title = 'Agents not running: ' + (data?.error || 'no data');
            return;
        }
        const parts = ORDER.map(k => {
            const h = health(k, data[k]);
            return `<span class="ag-pill-dot ${h.level}" title="${AGENTS[k].name}: ${esc(h.text)}"></span>`;
        }).join('');
        const ex = data.executor || {};
        const feed = ex.state === 'monitoring'
            ? `<span class="ag-pill-feed ${ex.feed_status === 'streaming' ? 'ok' : ex.feed_status === 'stale' ? 'warn' : 'bad'}">FEED ${esc((ex.feed_status || '').toUpperCase())}</span>`
            : '';
        el.innerHTML = `<span class="ag-pill-label">AGENTS</span>${parts}${feed}`;
        el.title = ORDER.map(k => `${AGENTS[k].name}: ${health(k, data[k]).text}`).join('\n');
    }

    // ── main view ─────────────────────────────────────────────────────
    function render() {
        if (!data) return;
        if (data.error) {
            $('ag-context').innerHTML = `<span class="ag-chip bad">AGENTS UNAVAILABLE · ${esc(data.error)}</span>`;
            return;
        }
        renderContext();
        renderTopology();
        $('ag-cards').innerHTML = ORDER.map(k => cardHtml(k, data[k])).join('');
        renderTimeline();
        if (detail) renderDetail(detail);
    }

    function renderContext() {
        const chip = (label, val, cls) => `<span class="ag-chip ${cls}"><b>${label}</b> ${val}</span>`;
        const hb = data.last_heartbeat_at ? ago(data.last_heartbeat_at) : 'pending';
        const hbStale = data.last_heartbeat_at && (now() - ts(data.last_heartbeat_at)) / 1000 > (data.heartbeat_seconds || 60) * 2.5;
        $('ag-context').innerHTML = [
            chip('ZERODHA', data.authenticated ? 'AUTHENTICATED' : 'NOT LOGGED IN', data.authenticated ? 'ok' : 'warn'),
            chip('LIVE TRADING', data.is_running ? 'ON' : 'OFF', data.is_running ? 'ok' : 'idle'),
            chip('MARKET', data.market_open ? 'OPEN' : 'CLOSED', data.market_open ? 'ok' : 'idle'),
            chip('SUPERVISOR', 'heartbeat ' + hb, hbStale ? 'bad' : 'ok'),
            chip('UPTIME', dur((now() - ts(data.created_at)) / 1000), 'idle'),
        ].join('');
    }

    function renderTopology() {
        ORDER.forEach(k => {
            const a = data[k] || {};
            const h = health(k, a);
            const link = document.querySelector(`#ag-topo [data-link="${k}"]`);
            const node = document.querySelector(`#ag-topo [data-node="${k}"]`);
            if (link) link.setAttribute('class', `ag-link ${h.level} ${ACTIVE.has(a.state) ? 'flow' : ''}`);
            if (node) {
                node.setAttribute('class', `ag-node ${h.level}`);
                const st = node.querySelector('.ag-node-state');
                if (st) st.textContent = (STATE_TEXT[a.state] || a.state || '').toUpperCase();
            }
        });
        const kite = document.querySelector('#ag-topo [data-link="kite"]');
        const anyActive = ORDER.some(k => ACTIVE.has(data[k]?.state));
        if (kite) kite.setAttribute('class', `ag-link ${data.authenticated ? 'ok' : 'warn'} ${anyActive ? 'flow' : ''}`);
        const svc = document.querySelector('#ag-topo [data-node="service"] .ag-node-state');
        if (svc) svc.textContent = data.is_running ? 'LIVE TRADING ON' : 'LIVE TRADING OFF';
    }

    // countdown to next run, or elapsed time of the running cycle
    function timerHtml(k, a) {
        const act = a.activity || {};
        if (act.cycle_started_at) {
            const el = (now() - ts(act.cycle_started_at)) / 1000;
            let bar = '', label = `RUNNING ${dur(el)}`;
            if (k === 'scanner') {
                const lim = a.timeout_seconds || 120;
                const pct = Math.min(100, el / lim * 100);
                bar = `<div class="ag-bar"><i style="width:${pct}%" class="${pct > 85 ? 'warn' : ''}"></i></div>`;
                label += ` / timeout ${lim}s`;
                if (scanProg && scanProg.total) label += ` · ${scanProg.scanned}/${scanProg.total} ${esc(scanProg.symbol || '')}`;
            }
            return `<div class="ag-timer run" data-live="1">${label}</div>${bar}`;
        }
        if (act.next_run_at && a.running) {
            const left = Math.max(0, (ts(act.next_run_at) - now()) / 1000);
            const total = a.interval_seconds || left || 1;
            const pct = Math.max(0, Math.min(100, (1 - left / total) * 100));
            return `<div class="ag-timer" data-live="1">NEXT CHECK IN ${dur(left)}</div><div class="ag-bar"><i style="width:${pct}%"></i></div>`;
        }
        return `<div class="ag-timer">—</div><div class="ag-bar"><i style="width:0"></i></div>`;
    }

    function spark(durations, color) {
        const d = (durations || []).slice(-30);
        if (!d.length) return '<div class="ag-spark empty">no cycles yet</div>';
        const max = Math.max(...d, 0.01);
        const w = 4, gap = 2, h = 28;
        const bars = d.map((v, i) => {
            const bh = Math.max(2, v / max * h);
            return `<rect x="${i * (w + gap)}" y="${h - bh}" width="${w}" height="${bh}" rx="1" fill="${color}" opacity="${0.35 + 0.65 * (i + 1) / d.length}"><title>${v}s</title></rect>`;
        }).join('');
        return `<svg class="ag-spark" viewBox="0 0 ${d.length * (w + gap)} ${h}" preserveAspectRatio="none">${bars}</svg>`;
    }

    function specificHtml(k, a) {
        const kv = (label, val, cls = '') => `<div class="ag-kv"><span>${label}</span><b class="${cls}">${val}</b></div>`;
        if (k === 'scanner') {
            return kv('Universe', esc((a.universe || '').toUpperCase())) +
                kv('Interval / timeout', `${a.interval_seconds}s / ${a.timeout_seconds}s`) +
                kv('Completed scans', a.scan_count ?? 0) +
                kv('Last scan', ago(a.last_scan_at));
        }
        if (k === 'verifier') {
            const r = a.last_report;
            const checks = r?.checks || [];
            const passed = checks.filter(c => c.passed).length;
            const failing = (r?.blocked_reasons || []);
            return kv('Checks passed', r ? `${passed} / ${checks.length}` : '—', r && !r.passed ? 'warn' : 'ok') +
                kv('Last audit', ago(a.last_checked_at)) +
                kv('Mode', 'ADVISORY') +
                (failing.length ? `<div class="ag-findings">${failing.slice(0, 3).map(f => `<div>⚠ ${esc(f)}</div>`).join('')}${failing.length > 3 ? `<div>+${failing.length - 3} more</div>` : ''}</div>` : '');
        }
        if (k === 'executor') {
            const fs = a.feed_status || 'unknown';
            const cls = fs === 'streaming' ? 'ok' : fs === 'stale' ? 'warn' : fs === 'disconnected' ? 'bad' : '';
            const outage = a.outage_started_at ? `<div class="ag-outage">⚠ Feed down for ${dur((now() - ts(a.outage_started_at)) / 1000)}</div>` : '';
            return kv('Price feed', a.state === 'monitoring' ? fs.toUpperCase() : 'NOT LIVE', cls) +
                kv('Last tick', a.tick_age_seconds == null ? '—' : dur(a.tick_age_seconds) + ' ago') +
                kv('Ticks received', Number(a.ticks_received || 0).toLocaleString('en-IN')) +
                kv('Outages (session)', (a.outages || []).length) + outage;
        }
        if (k === 'tracker') {
            const list = (a.open_positions || []).slice(0, 4).map(p =>
                `<div class="ag-pos"><span>${esc(p.symbol)}</span><span>${p.quantity}</span></div>`).join('');
            return kv('Open positions', a.open_positions_count ?? 0) +
                kv('Open orders', a.open_orders_count ?? 0) +
                kv('Last sync', ago(a.last_synced_at)) + (list ? `<div class="ag-poslist">${list}</div>` : '');
        }
        return '';
    }

    function cardHtml(k, a) {
        const m = AGENTS[k];
        if (!a) return `<div class="ag-card off"><div class="ag-card-hd"><span class="ag-icon">${m.icon}</span><b>${m.name}</b></div><div class="ag-role">No data</div></div>`;
        const h = health(k, a);
        const act = a.activity || {};
        const durs = act.durations || [];
        const avg = durs.length ? durs.reduce((s, v) => s + v, 0) / durs.length : null;
        const rate = act.cycles ? Math.round((act.cycles - act.failures) / act.cycles * 100) : null;
        const recent = (act.events || []).slice(-3).reverse().map(e =>
            `<div class="ag-ev ${e.level}"><span>${esc(e.ts.slice(11, 19))}</span>${esc(e.kind === 'state' ? '→ ' + (STATE_TEXT[e.message] || e.message) : e.message)}</div>`).join('');
        return `<div class="ag-card ${h.level}${detail === k ? ' sel' : ''}" data-agent="${k}" style="--ag:${m.color}">
            <div class="ag-card-hd">
                <span class="ag-icon">${m.icon}</span><b>${m.name}</b>
                <span class="ag-state ${h.level}"><i></i>${esc(STATE_TEXT[a.state] || a.state)}</span>
            </div>
            <div class="ag-role">${m.role}</div>
            ${timerHtml(k, a)}
            <div class="ag-stats">
                <div><b>${act.cycles ?? 0}</b><span>cycles</span></div>
                <div><b class="${act.failures ? 'warn' : ''}">${act.failures ?? 0}</b><span>failed</span></div>
                <div><b>${rate == null ? '—' : rate + '%'}</b><span>success</span></div>
                <div><b>${avg == null ? '—' : dur(avg)}</b><span>avg run</span></div>
            </div>
            ${spark(durs, m.color)}
            <div class="ag-specific">${specificHtml(k, a)}</div>
            ${a.last_error ? `<div class="ag-error">✕ ${esc(a.last_error)}</div>` : ''}
            <div class="ag-recent">${recent || '<div class="ag-ev">No activity yet</div>'}</div>
            <div class="ag-more">DETAILS ›</div>
        </div>`;
    }

    function allEvents() {
        const out = [];
        ORDER.forEach(k => (data[k]?.activity?.events || []).forEach(e => out.push({ ...e, agent: k })));
        return out.sort((a, b) => (a.ts < b.ts ? 1 : a.ts > b.ts ? -1 : b.seq - a.seq));
    }

    function renderTimeline() {
        let ev = allEvents();
        if (filter === 'issues') ev = ev.filter(e => e.level === 'warning' || e.level === 'error');
        else if (filter !== 'all') ev = ev.filter(e => e.agent === filter);
        const fresh = new Set();
        ORDER.forEach(k => {
            const last = seenSeq[k] || 0;
            (data[k]?.activity?.events || []).forEach(e => { if (last && e.seq > last) fresh.add(k + ':' + e.seq); });
            const evs = data[k]?.activity?.events || [];
            if (evs.length) seenSeq[k] = evs[evs.length - 1].seq;
        });
        $('ag-timeline').innerHTML = ev.slice(0, 120).map(e => {
            const m = AGENTS[e.agent];
            const icon = e.level === 'error' ? '✕' : e.level === 'warning' ? '⚠' : e.kind === 'state' ? '↻' : e.kind === 'cycle' ? '✓' : '•';
            const text = e.kind === 'state' ? `→ ${STATE_TEXT[e.message] || e.message}` : e.message;
            const extra = e.data?.duration_s != null ? ` <span class="ag-tl-dur">${dur(e.data.duration_s)}</span>` : '';
            return `<div class="ag-tl ${e.level}${fresh.has(e.agent + ':' + e.seq) ? ' fresh' : ''}">
                <span class="ag-tl-t">${esc(e.ts.slice(5, 10))} ${esc(e.ts.slice(11, 19))}</span>
                <span class="ag-tl-a" style="color:${m.color}">${m.name}</span>
                <span class="ag-tl-i">${icon}</span>
                <span class="ag-tl-m">${esc(text)}${extra}</span></div>`;
        }).join('') || '<div class="ag-tl idle"><span class="ag-tl-m">No events for this filter yet.</span></div>';
    }

    function renderDetail(k) {
        const a = data && data[k];
        const panel = $('ag-detail');
        if (!a) { panel.hidden = true; return; }
        const m = AGENTS[k];
        const act = a.activity || {};
        const skip = new Set(['activity', 'last_report', 'open_positions', 'outages']);
        const rows = Object.entries(a).filter(([key]) => !skip.has(key)).map(([key, v]) =>
            `<div class="ag-kv"><span>${esc(key.replace(/_/g, ' '))}</span><b>${esc(typeof v === 'object' && v !== null ? JSON.stringify(v) : v)}</b></div>`).join('');
        const checks = (a.last_report?.checks || []).map(c =>
            `<div class="ag-check ${c.passed ? 'ok' : 'bad'}"><span>${c.passed ? '✓' : '✕'} ${esc(c.name.replace(/_/g, ' '))}</span><span>${esc(c.message)}</span></div>`).join('');
        const outages = (a.outages || []).slice().reverse().map(o =>
            `<div class="ag-kv"><span>${esc(o.down_at.slice(11))} → ${esc(o.up_at.slice(11))}</span><b>${dur(o.seconds)}</b></div>`).join('');
        const events = (act.events || []).slice().reverse().map(e =>
            `<div class="ag-ev ${e.level}"><span>${esc(e.ts.slice(11, 19))}</span>${esc(e.kind === 'state' ? '→ ' + (STATE_TEXT[e.message] || e.message) : e.message)}</div>`).join('');
        panel.hidden = false;
        panel.style.setProperty('--ag', m.color);
        panel.innerHTML = `<div class="ag-detail-hd"><span class="ag-icon">${m.icon}</span><b>${m.name}</b>
                <button class="btn btn-outline btn-sm" onclick="AgentsView.closeDetail()">✕ Close</button></div>
            <div class="ag-role">${m.role}</div>
            <div class="ag-detail-grid">
                <div><div class="ag-sec">STATUS</div>${rows}</div>
                <div>${checks ? `<div class="ag-sec">PREFLIGHT CHECKS</div>${checks}` : ''}
                     ${outages ? `<div class="ag-sec">FEED OUTAGES</div>${outages}` : ''}
                     <div class="ag-sec">ACTIVITY (${(act.events || []).length})</div><div class="ag-detail-ev">${events || 'None yet'}</div></div>
            </div>`;
    }

    // live timers between polls
    function tickTimers() {
        if (!isActiveTab() || !data || data.error) return;
        ORDER.forEach(k => {
            const card = document.querySelector(`.ag-card[data-agent="${k}"]`);
            if (!card) return;
            const t = card.querySelector('.ag-timer');
            const bar = card.querySelector('.ag-bar');
            if (!t || !t.dataset.live) return;
            const tmp = document.createElement('div');
            tmp.innerHTML = timerHtml(k, data[k]);
            t.replaceWith(tmp.firstElementChild);
            const nb = tmp.querySelector('.ag-bar');
            if (bar && nb) bar.replaceWith(nb);
        });
        if (data) renderContext();
    }

    function bind() {
        $('ag-cards').addEventListener('click', e => {
            const c = e.target.closest('[data-agent]');
            if (c) { detail = c.dataset.agent; render(); $('ag-detail').scrollIntoView({ block: 'nearest', behavior: 'smooth' }); }
        });
        $('ag-topo').addEventListener('click', e => {
            const n = e.target.closest('[data-node]');
            if (n && AGENTS[n.dataset.node]) { detail = n.dataset.node; render(); }
        });
        $('ag-filter').addEventListener('click', e => {
            const b = e.target.closest('button'); if (!b) return;
            filter = b.dataset.f;
            $('ag-filter').querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b));
            renderTimeline();
        });
    }

    function start() {
        if (!started) { bind(); started = true; }
        poll();
        clearInterval(tickTimer);
        tickTimer = setInterval(tickTimers, 1000);
    }

    function init() {
        document.getElementById('t-agents-pill')?.addEventListener('click', () => document.querySelector('[data-tab="agents"]')?.click());
        poll();   // header indicator on every tab; faster while the Agents tab is open
    }

    // per-stock progress of a running scan (from the /ws/live scan_progress messages)
    function scanProgress(p) {
        scanProg = { ...p, at: Date.now() };
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
    return { start, scanProgress, closeDetail: () => { detail = null; $('ag-detail').hidden = true; render(); } };
})();
