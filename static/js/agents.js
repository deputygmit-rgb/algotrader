// Agent Operations Center: live state, workload and logs of the Scanner /
// Verifier / Executor / Tracker agents, plus a header health indicator.
//   GET /api/agents/status           - state, tasks, KPIs, recent events
//   GET /api/agents/log?cursor=...   - incremental log stream for the console
//   GET /api/agents/lifecycle        - recent signals with their life-cycle stages;
//                                      live stage events arrive on /ws/live ({type: 'lifecycle'})
const AgentsView = (() => {
    const AGENTS = {
        scanner: { name: 'SCANNER', icon: '⌖', color: '#38d6ff', role: 'Market scanner · NIFTY 500 technical analysis' },
        verifier: { name: 'VERIFIER', icon: '⛨', color: '#b18cff', role: 'Risk auditor · live preflight checklist (advisory)' },
        executor: { name: 'EXECUTOR', icon: '⚡', color: '#5be3a0', role: 'Execution supervisor · feed, strategies, signals, orders' },
        tracker: { name: 'TRACKER', icon: '◎', color: '#ff9f43', role: 'Portfolio tracker · broker positions and orders' },
    };
    const ORDER = ['scanner', 'verifier', 'executor', 'tracker'];
    const STATE_TEXT = {
        idle: 'Idle', stopped: 'Stopped', crashed: 'Crashed',
        waiting_market_open: 'Waiting · market closed', waiting_zerodha_auth: 'Waiting · trading account login',
        waiting_live_trading: 'Standby · live trading off', waiting_next_cycle: 'On duty · between cycles',
        using_shared_scan: 'Standby · reading the shared market scan',
        scanning: 'Scanning', checking: 'Auditing', monitoring: 'Monitoring', syncing: 'Syncing',
    };
    const ACTIVE = new Set(['scanning', 'checking', 'monitoring', 'syncing']);
    const NEXT_NOUN = { scanner: 'scan', verifier: 'audit', executor: 'health check', tracker: 'sync' };
    const PHASES = ['observe', 'decide', 'act'];
    const KIND_LABEL = {
        state: 'STATE', cycle: 'CYCLE', finding: 'FINDING', signal: 'SIGNAL', order: 'ORDER', change: 'CHANGE',
        error: 'ERROR', info: 'INFO', trace: 'TRACE', evaluate: 'EVAL', analyse: 'ANALYSE', scan: 'SCAN',
        audit: 'AUDIT', check: 'CHECK', sync: 'SYNC', health: 'HEALTH', bar: 'BAR', warning: 'WARN',
        lifecycle: 'LIFE',
    };
    const STAGE_LABEL = {
        SIGNAL: 'SIGNAL', VALIDATED: 'CHECKED', BLOCKED: 'BLOCKED', IGNORED: 'IGNORED', PLACED: 'ORDER',
        PARTIAL: 'PART FILL', FILLED: 'FILLED', REJECTED: 'REJECTED', CANCELLED: 'CANCELLED',
        PROTECTED: 'PROTECTED', STOP_FAILED: 'STOP FAILED', STOP_REPLACED: 'STOP RE-PLACED', TRAILED: 'TRAILED',
        SCALED: 'SCALED', EXITING: 'EXITING', CLOSED: 'CLOSED', RESTORED: 'RESTORED',
    };
    const PROBLEM = new Set(['BLOCKED', 'REJECTED', 'STOP_FAILED', 'CANCELLED']);
    const lifeSignals = new Map();   // lifecycle_id -> {..., events: []}
    let lifeFilter = 'all', lifeFresh = null, lifeLoaded = false;

    let data = null, clockSkew = 0, pollTimer = null, tickTimer = null, logTimer = null;
    let detail = null, started = false;
    // console state
    let logCursor = '', logLines = [], paused = false, pendingWhilePaused = 0, follow = true;
    let cAgent = 'all', cLevel = 'all', cStream = 'all', cQuery = '';
    const LOG_KEEP = 4000;

    const $ = id => document.getElementById(id);
    const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    const isActiveTab = () => $('tab-agents')?.classList.contains('active') && !document.hidden;
    const now = () => Date.now() - clockSkew;
    const ts = iso => iso ? Date.parse(iso) : null;
    const fmtInt = n => Number(n || 0).toLocaleString('en-IN');

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

    // ── health ────────────────────────────────────────────────────────
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
        if (a.activity?.task) return { level: 'ok', text: 'Working' };
        // on its schedule between cycles (e.g. the Verifier audits in milliseconds, then waits 45 s)
        if (a.on_duty || ACTIVE.has(a.state)) return { level: 'ok', text: 'On duty' };
        return { level: 'idle', text: 'Standby' };
    }

    // ── status polling ────────────────────────────────────────────────
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
        const parts = ORDER.map(k => `<span class="ag-pill-dot ${health(k, data[k]).level}"></span>`).join('');
        const ex = data.executor || {};
        const feed = ex.state === 'monitoring'
            ? `<span class="ag-pill-feed ${ex.feed_status === 'streaming' ? 'ok' : ex.feed_status === 'stale' ? 'warn' : 'bad'}">FEED ${esc((ex.feed_status || '').toUpperCase())}</span>`
            : '';
        el.innerHTML = `<span class="ag-pill-label">AGENTS</span>${parts}${feed}`;
        el.title = ORDER.map(k => `${AGENTS[k].name}: ${health(k, data[k]).text}${data[k]?.activity?.task ? ' — ' + data[k].activity.task : ''}`).join('\n');
    }

    // ── main view ─────────────────────────────────────────────────────
    function render() {
        if (!data) return;
        if (data.error) {
            $('ag-kpis').innerHTML = `<div class="ag-kpi bad"><span>AGENTS UNAVAILABLE</span><b>${esc(data.error)}</b></div>`;
            return;
        }
        renderKpis();
        renderTopology();
        $('ag-cards').innerHTML = ORDER.map(k => cardHtml(k, data[k])).join('');
        if (detail) renderDetail(detail);
    }

    function renderKpis() {
        const online = ORDER.filter(k => data[k]?.running).length;
        const working = ORDER.filter(k => data[k]?.activity?.task || ACTIVE.has(data[k]?.state)).length;
        const today = ORDER.reduce((s, k) => s + (data[k]?.activity?.processed_today || 0), 0);
        const perMin = ORDER.reduce((s, k) => { const t = data[k]?.activity?.throughput || []; return s + (t[t.length - 1] || 0) + (t[t.length - 2] || 0); }, 0) / 2;
        const issues = ORDER.filter(k => ['warn', 'bad'].includes(health(k, data[k]).level)).length;
        const hbStale = data.last_heartbeat_at && (now() - ts(data.last_heartbeat_at)) / 1000 > (data.heartbeat_seconds || 60) * 2.5;
        const kpi = (label, value, sub, cls = '') => `<div class="ag-kpi ${cls}"><span>${label}</span><b>${value}</b><em>${sub}</em></div>`;
        $('ag-kpis').innerHTML = [
            kpi('AGENTS ONLINE', `${online}/4`, `${working} working now`, online < 4 ? 'warn' : 'ok'),
            kpi('WORK PROCESSED TODAY', fmtInt(today), 'evaluations · checks · stocks · syncs'),
            kpi('THROUGHPUT', `${perMin.toFixed(perMin < 10 ? 1 : 0)}/min`, 'last 2 minutes'),
            kpi('OPEN ISSUES', issues, issues ? ORDER.filter(k => ['warn', 'bad'].includes(health(k, data[k]).level)).map(k => AGENTS[k].name).join(' · ') : 'all agents healthy', issues ? 'warn' : 'ok'),
            kpi('SUPERVISOR', data.last_heartbeat_at ? ago(data.last_heartbeat_at) : 'pending', `heartbeat · up ${dur((now() - ts(data.created_at)) / 1000)}`, hbStale ? 'bad' : ''),
        ].join('');
        const chip = (label, val, cls) => `<span class="ag-chip ${cls}"><b>${label}</b> ${val}</span>`;
        $('ag-context').innerHTML = [
            chip('BROKER', data.authenticated ? 'LINKED' : 'NOT LINKED', data.authenticated ? 'ok' : 'warn'),
            chip('LIVE TRADING', data.is_running ? 'ON' : 'OFF', data.is_running ? 'ok' : 'idle'),
            chip('MARKET', data.market_open ? 'OPEN' : 'CLOSED', data.market_open ? 'ok' : 'idle'),
        ].join('');
    }

    function renderTopology() {
        ORDER.forEach(k => {
            const a = data[k] || {};
            const h = health(k, a);
            const busy = ACTIVE.has(a.state) || !!a.activity?.task || !!a.on_duty;
            const link = document.querySelector(`#ag-topo [data-link="${k}"]`);
            const node = document.querySelector(`#ag-topo [data-node="${k}"]`);
            if (link) link.setAttribute('class', `ag-link ${h.level} ${busy ? 'flow' : ''}`);
            if (node) {
                node.setAttribute('class', `ag-node ${h.level}`);
                const st = node.querySelector('.ag-node-state');
                if (st) st.textContent = (a.activity?.task ? 'WORKING' : a.on_duty ? 'ON DUTY' : (STATE_TEXT[a.state] || a.state || '')).toUpperCase();
            }
        });
        const kite = document.querySelector('#ag-topo [data-link="kite"]');
        const anyBusy = ORDER.some(k => ACTIVE.has(data[k]?.state) || data[k]?.on_duty);
        if (kite) kite.setAttribute('class', `ag-link ${data.authenticated ? 'ok' : 'warn'} ${anyBusy ? 'flow' : ''}`);
        const svc = document.querySelector('#ag-topo [data-node="service"] .ag-node-state');
        if (svc) svc.textContent = data.is_running ? 'LIVE TRADING ON' : 'LIVE TRADING OFF';
    }

    // ── cards ─────────────────────────────────────────────────────────
    function avatar(k, h, busy, duty) {
        const m = AGENTS[k];
        return `<div class="ag-avatar ${h.level}${busy ? ' busy' : duty ? ' duty' : ''}">
            <svg viewBox="0 0 44 44"><circle class="ring" cx="22" cy="22" r="19"/><circle class="arc" cx="22" cy="22" r="19"/></svg>
            <span>${m.icon}</span></div>`;
    }

    function loopHtml(a, k) {
        const act = a.activity || {};
        const phase = act.task ? act.phase : 'idle';
        const steps = PHASES.map(p => `<span class="ag-step ${p === phase ? 'on' : ''}">${p.toUpperCase()}</span>`).join('<i>›</i>');
        let now_ = '';
        if (act.task) {
            now_ = `<div class="ag-now"><span class="ag-now-dot"></span>${esc(act.task)}<span class="ag-caret">▍</span></div>`;
        } else if (act.next_run_at && a.running) {
            const left = Math.max(0, (ts(act.next_run_at) - now()) / 1000);
            const total = a.interval_seconds || left || 1;
            const pct = Math.max(0, Math.min(100, (1 - left / total) * 100));
            const r = act.last_result;
            const noun = NEXT_NOUN[k] || 'cycle';
            if (a.on_duty && r) {
                const mark = r.level === 'error' ? '✕' : r.level === 'warning' ? '⚠' : '✓';
                now_ = `<div class="ag-now duty ${r.level}" data-live="next"><span class="ag-now-mark">${mark}</span>
                        <span class="ag-now-text">${esc(r.text)}<em>${ago(r.at)} · next ${noun} in ${dur(left)}</em></span></div>
                        <div class="ag-bar"><i style="width:${pct}%"></i></div>`;
            } else {
                now_ = `<div class="ag-now ${a.on_duty ? 'duty' : 'idle'}" data-live="next">${a.on_duty ? 'On duty' : 'Idle'} — next ${noun} in ${dur(left)}</div><div class="ag-bar"><i style="width:${pct}%"></i></div>`;
            }
        } else {
            now_ = `<div class="ag-now idle">${esc(STATE_TEXT[a.state] || a.state || 'Idle')}</div>`;
        }
        if (act.cycle_started_at && a.timeout_seconds) {
            const el = (now() - ts(act.cycle_started_at)) / 1000;
            const pct = Math.min(100, el / a.timeout_seconds * 100);
            const p = a.progress;
            const pr = p && p.total ? ` · ${p.scanned}/${p.total}` : '';
            now_ += `<div class="ag-bar" data-live="run"><i style="width:${pct}%" class="${pct > 85 ? 'warn' : ''}"></i></div>
                     <div class="ag-run-meta">running ${dur(el)} of ${a.timeout_seconds}s timeout${pr}</div>`;
        }
        return `<div class="ag-loop">${steps}</div>${now_}`;
    }

    function area(series, color) {
        const s = (series || []).slice(-60);
        if (!s.length || !s.some(v => v)) return `<div class="ag-area empty">no workload in the last hour</div>`;
        const max = Math.max(...s, 1), W = 240, H = 34;
        const pts = s.map((v, i) => [i / (s.length - 1) * W, H - (v / max) * (H - 3) - 1]);
        const line = pts.map((p, i) => (i ? 'L' : 'M') + p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join(' ');
        const id = 'g' + color.slice(1);
        return `<svg class="ag-area" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
            <defs><linearGradient id="${id}" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="${color}" stop-opacity=".45"/><stop offset="1" stop-color="${color}" stop-opacity="0"/></linearGradient></defs>
            <path d="${line} L${W} ${H} L0 ${H} Z" fill="url(#${id})"/><path d="${line}" fill="none" stroke="${color}" stroke-width="1.5"/></svg>`;
    }

    function specificHtml(k, a) {
        const kv = (label, val, cls = '') => `<div class="ag-kv"><span>${label}</span><b class="${cls}">${val}</b></div>`;
        if (k === 'scanner') {
            const ch = (a.last_changes || []).slice(0, 2).map(c => `<div class="ag-change">${esc(c)}</div>`).join('');
            return kv('Universe', esc((a.universe || '').toUpperCase())) +
                kv('Cadence', `every ${a.interval_seconds}s · timeout ${a.timeout_seconds}s`) +
                kv('Completed scans', a.scan_count ?? 0) + kv('Last scan', ago(a.last_scan_at)) + ch;
        }
        if (k === 'verifier') {
            const r = a.last_report, checks = r?.checks || [];
            const passed = checks.filter(c => c.passed).length;
            const failing = r?.blocked_reasons || [];
            return kv('Checks passed', r ? `${passed} / ${checks.length}` : '—', r && !r.passed ? 'warn' : 'ok') +
                kv('Audits run', a.audits ?? 0) + kv('Last audit', ago(a.last_checked_at)) + kv('Mode', 'ADVISORY') +
                (failing.length ? `<div class="ag-findings">${failing.slice(0, 2).map(f => `<div>⚠ ${esc(f)}</div>`).join('')}</div>` : '');
        }
        if (k === 'executor') {
            const fs = a.feed_status || 'unknown';
            const cls = fs === 'streaming' ? 'ok' : fs === 'stale' ? 'warn' : fs === 'disconnected' ? 'bad' : '';
            const t = a.today || {};
            const outage = a.outage_started_at ? `<div class="ag-outage">⚠ Feed down for ${dur((now() - ts(a.outage_started_at)) / 1000)}</div>` : '';
            return kv('Price feed', a.state === 'monitoring' ? fs.toUpperCase() : 'NOT LIVE', cls) +
                kv('Last tick', a.tick_age_seconds == null ? '—' : dur(a.tick_age_seconds) + ' ago') +
                kv('Today', `${fmtInt(t.evaluations)} evals · ${fmtInt(t.signals)} signals · ${fmtInt(t.orders)} orders`) +
                kv('Strategies watched', Object.keys(a.strategies || {}).length) + outage;
        }
        if (k === 'tracker') {
            const list = (a.open_positions || []).slice(0, 3).map(p =>
                `<div class="ag-pos"><span>${esc(p.symbol)}</span><span>${p.quantity}</span></div>`).join('');
            return kv('Open positions', a.open_positions_count ?? 0) + kv('Open orders', a.open_orders_count ?? 0) +
                kv('Syncs', a.syncs ?? 0) + kv('Last sync', ago(a.last_synced_at)) + (list ? `<div class="ag-poslist">${list}</div>` : '');
        }
        return '';
    }

    function cardHtml(k, a) {
        const m = AGENTS[k];
        if (!a) return `<div class="ag-card off" style="--ag:${m.color}"><div class="ag-card-hd"><b>${m.name}</b></div><div class="ag-role">No data</div></div>`;
        const h = health(k, a);
        const act = a.activity || {};
        const busy = ACTIVE.has(a.state) || !!act.task;
        const durs = act.durations || [];
        const avg = durs.length ? durs.reduce((s, v) => s + v, 0) / durs.length : null;
        const rate = act.cycles ? Math.round((act.cycles - act.failures) / act.cycles * 100) : null;
        const t = act.throughput || [];
        const lastEv = (act.events || []).filter(e => e.kind !== 'state').slice(-2).reverse().map(e =>
            `<div class="ag-ev ${e.level}"><span>${esc(e.ts.slice(11, 19))}</span>${esc(e.message)}</div>`).join('');
        return `<div class="ag-card ${h.level}${detail === k ? ' sel' : ''}${busy ? ' busy' : ''}" data-agent="${k}" style="--ag:${m.color}">
            <div class="ag-card-hd">
                ${avatar(k, h, !!act.task, a.on_duty)}
                <div class="ag-id"><b>${m.name}</b><span>${m.role}</span></div>
                <span class="ag-state ${h.level}"><i></i>${esc(h.text)}</span>
            </div>
            ${loopHtml(a, k)}
            <div class="ag-work">
                <div class="ag-work-hd"><span>WORKLOAD · 60 MIN</span><b>${fmtInt(act.processed_today)} today · ${t[t.length - 1] || 0}/min</b></div>
                ${area(t, m.color)}
            </div>
            <div class="ag-stats">
                <div><b>${act.cycles ?? 0}</b><span>cycles</span></div>
                <div><b class="${act.failures ? 'warn' : ''}">${act.failures ?? 0}</b><span>failed</span></div>
                <div><b>${rate == null ? '—' : rate + '%'}</b><span>success</span></div>
                <div><b>${avg == null ? '—' : dur(avg)}</b><span>avg cycle</span></div>
            </div>
            <div class="ag-specific">${specificHtml(k, a)}</div>
            ${a.last_error ? `<div class="ag-error">✕ ${esc(a.last_error)}</div>` : ''}
            <div class="ag-recent"><div class="ag-recent-hd">LATEST DECISIONS</div>${lastEv || '<div class="ag-ev">Nothing yet</div>'}</div>
            <div class="ag-more"><span data-console="${k}">VIEW LOG</span><span>DETAILS ›</span></div>
        </div>`;
    }

    // ── detail panel ──────────────────────────────────────────────────
    function renderDetail(k) {
        const a = data && data[k];
        const panel = $('ag-detail');
        if (!a) { panel.hidden = true; return; }
        const m = AGENTS[k];
        const act = a.activity || {};
        const skip = new Set(['activity', 'last_report', 'open_positions', 'outages', 'strategies', 'today', 'last_window', 'last_changes', 'progress']);
        const rows = Object.entries(a).filter(([key]) => !skip.has(key)).map(([key, v]) =>
            `<div class="ag-kv"><span>${esc(key.replace(/_/g, ' '))}</span><b>${esc(typeof v === 'object' && v !== null ? JSON.stringify(v) : v)}</b></div>`).join('');
        const checks = (a.last_report?.checks || []).map(c =>
            `<div class="ag-check ${c.passed ? 'ok' : 'bad'}"><span>${c.passed ? '✓' : '✕'} ${esc(c.name.replace(/_/g, ' '))}</span><span>${esc(c.message)}</span></div>`).join('');
        const strategies = Object.entries(a.strategies || {}).map(([n, s]) =>
            `<tr><td>${esc(n)}</td><td>${fmtInt(s.evaluations)}</td><td>${fmtInt(s.signals)}</td><td class="${s.errors ? 'negative' : ''}">${s.errors}</td><td>${s.avg_ms} ms</td><td>${ago(s.last_at)}</td></tr>`).join('');
        const outages = (a.outages || []).slice().reverse().map(o =>
            `<div class="ag-kv"><span>${esc(o.down_at.slice(11))} → ${esc(o.up_at.slice(11))}</span><b>${dur(o.seconds)}</b></div>`).join('');
        const changes = (a.last_changes || []).map(c => `<div class="ag-change">${esc(c)}</div>`).join('');
        const events = (act.events || []).slice().reverse().map(e =>
            `<div class="ag-ev ${e.level}"><span>${esc(e.ts.slice(11, 19))}</span>${esc(e.kind === 'state' ? '→ ' + (STATE_TEXT[e.message] || e.message) : e.message)}</div>`).join('');
        panel.hidden = false;
        panel.style.setProperty('--ag', m.color);
        panel.innerHTML = `<div class="ag-detail-hd">${avatar(k, health(k, a), false)}<div class="ag-id"><b>${m.name}</b><span>${m.role}</span></div>
                <a class="btn btn-outline btn-sm" href="/api/agents/log/download?agent=${k}">⤓ Today's log</a>
                <button class="btn btn-outline btn-sm" onclick="AgentsView.closeDetail()">✕ Close</button></div>
            <div class="ag-detail-grid">
                <div><div class="ag-sec">STATUS</div>${rows}
                     ${act.log_file ? `<div class="ag-sec">LOG FILE</div><div class="ag-logpath">${esc(act.log_file)}</div>` : ''}</div>
                <div>${strategies ? `<div class="ag-sec">STRATEGIES EVALUATED</div><table class="ag-table"><thead><tr><th>Strategy</th><th>Evals</th><th>Signals</th><th>Errors</th><th>Avg</th><th>Last</th></tr></thead><tbody>${strategies}</tbody></table>` : ''}
                     ${checks ? `<div class="ag-sec">PREFLIGHT CHECKS</div>${checks}` : ''}
                     ${changes ? `<div class="ag-sec">CHANGES SINCE PREVIOUS SCAN</div>${changes}` : ''}
                     ${outages ? `<div class="ag-sec">FEED OUTAGES</div>${outages}` : ''}
                     <div class="ag-sec">EVENTS (${(act.events || []).length})</div><div class="ag-detail-ev">${events || 'None yet'}</div></div>
            </div>`;
    }

    // ── console ───────────────────────────────────────────────────────
    async function fetchLog() {
        clearTimeout(logTimer);
        if (isActiveTab()) {
            try {
                const res = await fetch('/api/agents/log?limit=800&cursor=' + encodeURIComponent(logCursor));
                const json = await res.json();
                if (res.ok && !json.error) {
                    logCursor = json.cursor || logCursor;
                    if (json.entries?.length) {
                        logLines = logLines.concat(json.entries).slice(-LOG_KEEP);
                        if (paused) pendingWhilePaused += json.entries.length; else renderConsole(json.entries.length);
                    }
                }
            } catch (e) { /* retry next tick */ }
        }
        logTimer = setTimeout(fetchLog, 1500);
    }

    const LEVEL_RANK = { debug: 0, info: 1, warning: 2, error: 3 };
    function visible(e) {
        if (cAgent !== 'all' && e.agent !== cAgent) return false;
        if (cStream === 'events' && e.stream !== 'event') return false;
        if (cLevel === 'info' && (LEVEL_RANK[e.level] ?? 1) < 1) return false;
        if (cLevel === 'warning' && (LEVEL_RANK[e.level] ?? 1) < 2) return false;
        if (cQuery && !(e.message + ' ' + e.agent + ' ' + e.kind).toLowerCase().includes(cQuery)) return false;
        return true;
    }

    function lineHtml(e, fresh) {
        const m = AGENTS[e.agent] || { name: e.agent, color: '#999' };
        const msg = e.kind === 'state' ? '→ ' + (STATE_TEXT[e.message] || e.message) : e.message;
        const title = e.data ? ` title="${esc(JSON.stringify(e.data))}"` : '';
        return `<div class="ag-ln ${e.level} ${e.stream}${fresh ? ' fresh' : ''}"${title}>
            <span class="t">${esc(e.ts.slice(11, 19))}</span><span class="a" style="color:${m.color}">${m.name}</span>
            <span class="k k-${esc(e.kind)}">${KIND_LABEL[e.kind] || esc(e.kind).toUpperCase()}</span><span class="m">${esc(msg)}</span></div>`;
    }

    function renderConsole(newCount = 0) {
        const box = $('ag-console');
        if (!box) return;
        const lines = logLines.filter(visible).slice(-900);
        const freshFrom = lines.length - Math.min(newCount, lines.length);
        box.innerHTML = lines.map((e, i) => lineHtml(e, newCount && i >= freshFrom)).join('') ||
            '<div class="ag-ln info"><span class="m">No log entries match these filters yet.</span></div>';
        $('ag-console-count').textContent = `${fmtInt(lines.length)} shown · ${fmtInt(logLines.length)} buffered`;
        if (follow) box.scrollTop = box.scrollHeight;
        $('ag-jump').hidden = follow;
    }

    function bindConsole() {
        const box = $('ag-console');
        box.addEventListener('scroll', () => {
            follow = box.scrollHeight - box.scrollTop - box.clientHeight < 24;
            $('ag-jump').hidden = follow;
        });
        $('ag-jump').addEventListener('click', () => { follow = true; renderConsole(); });
        const seg = (id, set) => $(id).addEventListener('click', e => {
            const b = e.target.closest('button'); if (!b) return;
            $(id).querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b));
            set(b.dataset.v); renderConsole();
        });
        seg('ag-c-agent', v => { cAgent = v; });
        seg('ag-c-level', v => { cLevel = v; });
        seg('ag-c-stream', v => { cStream = v; });
        $('ag-c-search').addEventListener('input', e => { cQuery = e.target.value.trim().toLowerCase(); renderConsole(); });
        $('ag-c-pause').addEventListener('click', e => {
            paused = !paused;
            e.target.textContent = paused ? '▶ RESUME' : '❚❚ PAUSE';
            if (!paused) { renderConsole(pendingWhilePaused); pendingWhilePaused = 0; }
        });
        $('ag-c-download').addEventListener('change', e => {
            if (e.target.value) window.location.href = '/api/agents/log/download?agent=' + encodeURIComponent(e.target.value);
            e.target.value = '';
        });
    }

    function focusConsole(agent) {
        cAgent = agent;
        $('ag-c-agent').querySelectorAll('button').forEach(x => x.classList.toggle('on', x.dataset.v === agent));
        follow = true;
        renderConsole();
        $('ag-console-card').scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    // ── live timers between polls ─────────────────────────────────────
    function tickTimers() {
        if (!isActiveTab() || !data || data.error) return;
        ORDER.forEach(k => {
            const card = document.querySelector(`.ag-card[data-agent="${k}"]`);
            if (!card || !card.querySelector('[data-live]')) return;
            const holder = document.createElement('div');
            holder.innerHTML = loopHtml(data[k], k);
            const loop = card.querySelector('.ag-loop');
            let n = loop.nextElementSibling;
            while (n && !n.classList.contains('ag-work')) { const next = n.nextElementSibling; n.remove(); n = next; }
            loop.replaceWith(...holder.childNodes);
        });
    }

    // ── signal life cycle ─────────────────────────────────────────────
    function stageDetail(e) {
        const bits = [];
        if (e.order_id) bits.push('#' + e.order_id);
        if (e.reason) bits.push(e.reason);
        if (e.filled != null) bits.push(`${e.filled}/${e.quantity} @ ${e.average_price ?? '—'}`);
        if (e.trigger != null) bits.push(`trigger ${e.trigger} limit ${e.limit}`);
        if (e.price != null && e.stage !== 'SIGNAL') bits.push('@ ' + e.price);
        if (e.pnl != null) bits.push('P&L ' + e.pnl);
        return bits.join(' · ');
    }
    function lifeRowHtml(sig) {
        const last = sig.events[sig.events.length - 1] || {};
        const head = [sig.side, sig.quantity, sig.symbol].filter(x => x != null).join(' ');
        const chips = sig.events.map(e => {
            const t = (e.ts || '').slice(11, 19);
            const title = `${e.stage} · ${e.agent} · ${e.ts}\n${stageDetail(e)}`;
            return `<span class="ag-life-chip ${esc(e.level)} ${esc(e.agent)}" title="${esc(title)}">${esc(STAGE_LABEL[e.stage] || e.stage)}<em>${esc(t)}</em></span>`;
        }).join('');
        const lvl = PROBLEM.has(last.stage) ? 'error' : sig.done ? 'info' : 'warning';
        return `<div class="ag-life-row${sig.lifecycle_id === lifeFresh ? ' fresh' : ''}" data-lid="${esc(sig.lifecycle_id)}">
            <div class="ag-life-hd"><b>${esc(head || sig.lifecycle_id)}</b><span>${esc(sig.strategy || '')}${sig.timeframe ? ' · ' + esc(sig.timeframe) : ''}</span>
                <span class="id">${esc(sig.lifecycle_id)} · ${esc(ago(sig.updated_at))}</span>
                <span class="st ag-life-chip ${lvl}">${esc(STAGE_LABEL[last.stage] || last.stage || '')}</span></div>
            <div class="ag-life-strip">${chips}</div>
            <div class="ag-life-last">${esc(stageDetail(last))}</div></div>`;
    }
    function renderLife() {
        const box = $('ag-life');
        if (!box || !isActiveTab()) return;
        let rows = [...lifeSignals.values()].sort((a, b) => (b.updated_at || '').localeCompare(a.updated_at || ''));
        if (lifeFilter === 'open') rows = rows.filter(s => !s.done);
        if (lifeFilter === 'problem') rows = rows.filter(s => s.events.some(e => PROBLEM.has(e.stage)));
        $('ag-life-count').textContent = `${lifeSignals.size} signal${lifeSignals.size === 1 ? '' : 's'} · ${[...lifeSignals.values()].filter(s => !s.done).length} in progress`;
        if (!rows.length) {
            box.innerHTML = `<div class="ag-life-empty">${lifeSignals.size ? 'Nothing matches this filter.' : 'No live signals yet — each strategy or board signal appears here as it moves from signal to checks, order, fill, protection and exit.'}</div>`;
            return;
        }
        box.innerHTML = rows.slice(0, 80).map(lifeRowHtml).join('');
    }
    async function fetchLifecycle() {
        try {
            const res = await fetch('/api/agents/lifecycle?limit=100');
            const json = await res.json();
            (json.signals || []).forEach(sig => lifeSignals.set(sig.lifecycle_id, sig));
            lifeLoaded = true;
            renderLife();
        } catch (e) { /* the panel fills from the websocket */ }
    }
    // one stage event from /ws/live
    function lifecycle(ev) {
        if (!ev || !ev.lifecycle_id) return;
        let sig = lifeSignals.get(ev.lifecycle_id);
        if (!sig) {
            sig = { lifecycle_id: ev.lifecycle_id, started_at: ev.ts, events: [] };
            lifeSignals.set(ev.lifecycle_id, sig);
        }
        ['symbol', 'strategy', 'side', 'quantity', 'timeframe'].forEach(k => { if (sig[k] == null && ev[k] != null) sig[k] = ev[k]; });
        sig.events.push(ev);
        sig.stage = ev.stage;
        sig.updated_at = ev.ts;
        sig.done = ['BLOCKED', 'IGNORED', 'REJECTED', 'CANCELLED', 'CLOSED'].includes(ev.stage);
        if (lifeSignals.size > 300) lifeSignals.delete(lifeSignals.keys().next().value);
        lifeFresh = ev.lifecycle_id;
        renderLife();
    }

    function bind() {
        $('ag-life-filter')?.addEventListener('click', e => {
            const b = e.target.closest('button[data-v]');
            if (!b) return;
            lifeFilter = b.dataset.v;
            [...$('ag-life-filter').children].forEach(x => x.classList.toggle('on', x === b));
            renderLife();
        });
        $('ag-cards').addEventListener('click', e => {
            const log = e.target.closest('[data-console]');
            if (log) { focusConsole(log.dataset.console); return; }
            const c = e.target.closest('[data-agent]');
            if (c) { detail = c.dataset.agent; render(); $('ag-detail').scrollIntoView({ block: 'nearest', behavior: 'smooth' }); }
        });
        $('ag-topo').addEventListener('click', e => {
            const n = e.target.closest('[data-node]');
            if (n && AGENTS[n.dataset.node]) { detail = n.dataset.node; render(); }
        });
        bindConsole();
    }

    function start() {
        if (!started) { bind(); started = true; }
        poll();
        fetchLog();
        if (!lifeLoaded) fetchLifecycle(); else renderLife();
        clearInterval(tickTimer);
        tickTimer = setInterval(tickTimers, 1000);
    }

    function init() {
        document.getElementById('t-agents-pill')?.addEventListener('click', () => document.querySelector('[data-tab="agents"]')?.click());
        poll();
    }

    // kept for the dashboard's websocket hook; scan progress now arrives via agent status
    function scanProgress() {}

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
    return { start, scanProgress, lifecycle, focusConsole, closeDetail: () => { detail = null; $('ag-detail').hidden = true; render(); } };
})();
