// Day-trade panel in the Market Flow tab: 5-minute setups on the tracked list
// (money-flow picks + manual symbols), a live signal feed with outcomes, and a
// setup board with a mini 5-minute chart per stock. Signals come from
// src/analysis/intraday_setups.py and only trigger on closed bars.
const DayTrade = (() => {
    const POLL_MS = 10000;
    const STATUS_CLASS = {
        'PENDING': 'pending', 'ACTIVE': 'open', 'T1 HIT': 'win', 'TARGET 2': 'win', 'BREAKEVEN': 'flat',
        'STOPPED': 'loss', 'TIME EXIT': 'flat',
    };
    const OPEN = new Set(['PENDING', 'ACTIVE', 'T1 HIT']);

    let started = false, timer = null, inFlight = false, data = null, filter = 'all';
    let seen = null;              // signal id -> status, for alerts
    const $ = id => document.getElementById(id);
    const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    const num = (n, d = 2) => n === null || n === undefined ? '—' : Number(n).toFixed(d);
    const sgn = (n, d = 2) => n === null || n === undefined ? '—' : `${n > 0 ? '+' : n < 0 ? '−' : ''}${Math.abs(n).toFixed(d)}`;
    const demo = () => Boolean($('mf-demo')?.checked);
    const qs = () => demo() ? '?demo=1' : '';
    const isActive = () => $('tab-market-flow')?.classList.contains('active') && !document.hidden;

    // ── data ────────────────────────────────────────────────────────
    async function poll() {
        clearTimeout(timer);
        if (isActive() && !inFlight) {
            inFlight = true;
            try {
                const res = await fetch('/api/market/day-trade' + qs());
                const json = await res.json();
                if (!res.ok || json.error) throw new Error(json.error || res.statusText);
                alertChanges(json);
                data = json;
                render();
            } catch (e) {
                $('dt-meta').textContent = `Unavailable: ${e.message}`;
            } finally {
                inFlight = false;
            }
        }
        timer = setTimeout(poll, POLL_MS);
    }

    // New signals and outcome changes since the last poll become toasts (and a
    // short tone when ALERTS is on). The first load only records what exists.
    function alertChanges(next) {
        const key = s => `${s.status}|${s.live_flag || ''}`;
        const current = new Map(next.feed.map(s => [s.id, key(s)]));
        if (seen) {
            for (const s of next.feed) {
                if (s.backfilled || s.skipped) continue;
                const before = seen.get(s.id);
                if (before === undefined && OPEN.has(s.status)) {
                    notify(`${s.symbol} ${s.side} · ${s.label} at ${s.time} close — enter at next bar open (~${num(s.entry)}, SL ${num(s.stop)})`, s.side === 'LONG' ? 'success' : 'error');
                } else if (before && before !== key(s)) {
                    const what = s.status === 'ACTIVE' && before.startsWith('PENDING') ? `filled @ ${num(s.entry)} (${s.entry_time})`
                        : s.live_flag && s.status !== 'PENDING' ? `${s.live_flag} (bar not closed yet)`
                        : `${s.status}${s.result_r !== null ? ` ${sgn(s.result_r)}R net` : ''}`;
                    notify(`${s.symbol} ${s.side} ${s.setup}: ${what}`, 'info');
                }
            }
        }
        seen = current;
    }

    function notify(msg, type) {
        if (typeof showToast === 'function') showToast(msg, type);
        if (!$('dt-alerts')?.checked) return;
        try {
            const ctx = new (window.AudioContext || window.webkitAudioContext)();
            const osc = ctx.createOscillator(), gain = ctx.createGain();
            osc.frequency.value = type === 'error' ? 440 : 880;
            gain.gain.setValueAtTime(0.08, ctx.currentTime);
            gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.25);
            osc.connect(gain).connect(ctx.destination);
            osc.start(); osc.stop(ctx.currentTime + 0.25);
        } catch { /* audio is optional */ }
    }

    // ── render ──────────────────────────────────────────────────────
    function rValue(s) {
        if (s.result_r !== null && s.result_r !== undefined) return s.result_r;
        return s.current_r ?? null;
    }

    function rTitle(s) {
        if (s.result_r !== null && s.result_r !== undefined) {
            return `net ${sgn(s.result_r)}R = gross ${sgn(s.gross_r)}R − costs ${num(s.cost_r)}R (slippage + charges)`;
        }
        return s.status === 'PENDING' ? 'estimate: fills at the next bar open' : 'live, before exit costs';
    }

    function feedRow(s) {
        const r = rValue(s);
        const rCls = r === null ? '' : r > 0 ? 'mf-gt' : r < 0 ? 'mf-rt' : '';
        return `<tr data-sym="${esc(s.symbol)}" class="${OPEN.has(s.status) ? 'is-open' : ''}${s.backfilled || s.skipped ? ' bf' : ''}"
            title="${esc(`${s.trigger} · signal at ${s.time} bar close · ${s.entry_time ? `filled ${s.entry_time} open` : 'fills at next bar open'} · T2 ${num(s.target2)} · risk ${num(s.risk_pct)}% · best ${sgn(s.mfe_r)}R / worst ${sgn(s.mae_r)}R`)}">
            <td>${esc(s.time)}${s.backfilled ? '<i title="Signal closed before the stock was tracked; replayed from history and left out of the stats">bf</i>' : ''}${s.skipped ? `<i title="Not taken: ${esc(s.skipped)} (max open trades reached); left out of the stats">skip</i>` : ''}</td>
            <th>${esc(s.symbol)}</th>
            <td><span class="dt-side ${s.side === 'LONG' ? 'long' : 'short'}">${s.side}</span></td>
            <td class="dt-setup">${esc(s.label)}</td>
            <td class="num">${s.quality}</td>
            <td class="num">${num(s.entry)}</td>
            <td class="num">${num(s.stop)}</td>
            <td class="num">${num(s.target1)}</td>
            <td><span class="dt-status ${STATUS_CLASS[s.status] || ''}">${s.status}${s.exit_time ? ` ${esc(s.exit_time)}` : ''}</span>${s.live_flag && s.status !== 'PENDING' ? `<span class="dt-flag" title="Live price says so; confirmed when the bar closes">${esc(s.live_flag)}</span>` : ''}</td>
            <td class="num ${rCls}" title="${esc(rTitle(s))}">${r === null ? '—' : `${sgn(r)}R`}${s.status === 'PENDING' ? '<i>est</i>' : ''}</td>
            <td>${orderCell(s.order)}</td>
        </tr>`;
    }

    // Auto-trade decision for a signal (only signals that were candidates get one)
    function orderCell(o) {
        if (!o) return '<span class="mf-dim">—</span>';
        const tip = esc(`${o.decision} at ${(o.at || '').slice(11, 19)}${o.quantity ? ` · qty ${o.quantity}` : ''}${o.reason ? ` · ${o.reason}` : ''}`);
        if (o.decision === 'sent') return `<span class="dt-ord sent" title="${tip}">SENT #${esc(o.order_id)}</span>`;
        if (o.decision === 'skipped') return `<span class="dt-ord skip" title="${tip}">not sent</span>`;
        return `<span class="dt-ord ${o.decision === 'failed' ? 'fail' : 'block'}" title="${tip}">${esc(o.decision.toUpperCase())}</span>`;
    }

    // ── auto-trade control ──────────────────────────────────────────
    let auto = null;
    function renderAuto(a) {
        if (!a) return;
        auto = a;
        const btn = $('dt-auto-btn');
        btn.textContent = `AUTO-TRADE: ${a.enabled ? 'ON' : 'OFF'}`;
        btn.classList.toggle('on', !!a.enabled);
        const panel = $('dt-auto');
        if (panel.hidden && !a.enabled) return;
        if (document.activeElement?.closest('#dt-auto')) return;   // don't overwrite while editing
        $('dt-auto-risk').value = a.risk_per_trade;
        $('dt-auto-cap').value = a.max_order_value;
        $('dt-auto-max').value = a.max_orders_per_day;
        $('dt-auto-long').checked = (a.sides || []).includes('LONG');
        $('dt-auto-short').checked = (a.sides || []).includes('SHORT');
        $('dt-auto-preflight').checked = a.require_preflight !== false;
        $('dt-auto-toggle').textContent = a.enabled ? 'DISABLE' : 'ENABLE';
        // switching auto-trade on needs agentic access; switching it off never does
        if (a.enabled) delete $('dt-auto-toggle').dataset.requires;
        else $('dt-auto-toggle').dataset.requires = 'agentic';
        $('dt-auto-toggle').classList.toggle('danger', !a.enabled);
        const live = a.live_trading === undefined ? null : a.live_trading;
        $('dt-auto-status').innerHTML = a.enabled
            ? `<b class="mf-gt">ON</b> since ${esc((a.enabled_at || '').slice(11, 16))} · ${a.orders_today ?? 0}/${a.max_orders_per_day} orders today`
              + (live === false ? ' · <b class="mf-rt">live trading is OFF — nothing will be sent until you Start Live</b>' : '')
            : '<b>OFF</b> — board signals are tracked and logged only; no orders are placed';
    }

    async function saveAuto(patch) {
        try {
            const res = await fetch('/api/market/day-trade/autotrade', {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch),
            });
            const json = await res.json();
            if (!res.ok || json.error) throw new Error(json.error || json.detail || res.statusText);
            renderAuto(json);
            return true;
        } catch (e) {
            if (typeof showToast === 'function') showToast('Auto-trade update failed: ' + e.message, 'error');
            return false;
        }
    }

    function autoForm() {
        return {
            risk_per_trade: Number($('dt-auto-risk').value), max_order_value: Number($('dt-auto-cap').value),
            max_orders_per_day: Number($('dt-auto-max').value),
            sides: [$('dt-auto-long').checked && 'LONG', $('dt-auto-short').checked && 'SHORT'].filter(Boolean),
            require_preflight: $('dt-auto-preflight').checked,
        };
    }

    async function loadAuto() {
        try {
            const res = await fetch('/api/market/day-trade/autotrade');
            if (res.ok) renderAuto(await res.json());
        } catch (e) { /* shown on the next poll */ }
    }

    function feedFilter(s) {
        if (filter === 'open') return OPEN.has(s.status);
        if (filter === 'LONG' || filter === 'SHORT') return s.side === filter;
        return true;
    }

    function stateLine(t) {
        if (t.loading) return '<span class="mf-dim">loading 5-minute bars…</span>';
        if (t.insufficient_history) return '<span class="mf-dim">needs 15 earlier sessions of 5-minute history before it can be traded</span>';
        const a = t.active;
        if (a && a.status === 'PENDING') {
            return `<span class="dt-side ${a.side === 'LONG' ? 'long' : 'short'}">${a.side} SIGNAL</span> ${esc(a.setup)} at ${esc(a.time)} close · enter next bar open ~${num(a.entry)} · SL ${num(a.stop)}`;
        }
        if (a) {
            const r = a.current_r ?? null;
            return `<span class="dt-side ${a.side === 'LONG' ? 'long' : 'short'}">IN ${a.side}</span> ${esc(a.setup)} @ ${num(a.entry)} · SL ${num(a.stop)} · T1 ${num(a.target1)}
                ${r === null ? '' : `· <b class="${r >= 0 ? 'mf-gt' : 'mf-rt'}">${sgn(r)}R</b>`}${a.status === 'T1 HIT' ? ' · <b class="mf-gt">T1 banked</b>' : ''}${a.live_flag ? ` · <b class="dt-flag">${esc(a.live_flag)}</b>` : ''}`;
        }
        const next = [...(t.watch || [])].sort((x, y) => Math.abs(x.distance_pct) - Math.abs(y.distance_pct))[0];
        return next
            ? `<span class="mf-dim">ARMED</span> ${esc(next.text)} <span class="mf-dim">(${sgn(next.distance_pct)}%)</span>`
            : '<span class="mf-dim">no open trade · waiting for a setup</span>';
    }

    function boardCard(t) {
        const flowCls = (t.flow_score || 0) >= 0 ? 'mf-gt' : 'mf-rt';
        return `<div class="dt-card${t.active ? ' live' : ''}" data-sym="${esc(t.symbol)}">
            <div class="dt-card-hd">
                <b>${esc(t.symbol)}</b>
                <span>${num(t.ltp)}</span>
                <span class="${(t.change_pct || 0) >= 0 ? 'mf-gt' : 'mf-rt'}">${sgn(t.change_pct)}%</span>
                <span class="${flowCls}" title="money-flow score">FLOW ${sgn(t.flow_score, 0)}</span>
                ${t.trade_direction ? `<span class="dt-side ${t.trade_direction === 'LONG' ? 'long' : 'short'}" title="Direction fixed for the session from the money-flow bias when tracking started">${t.trade_direction} ONLY</span>` : ''}
                <span class="dt-src">${t.source === 'manual' ? 'MANUAL' : 'AUTO'} · ${esc(t.since)}</span>
                ${t.source === 'manual' ? `<button class="dt-x" data-remove="${esc(t.symbol)}" title="Stop tracking">×</button>` : ''}
            </div>
            <canvas class="dt-chart" data-chart="${esc(t.symbol)}" role="img" aria-label="${esc(t.symbol)} 5-minute chart"></canvas>
            <div class="dt-state">${stateLine(t)}</div>
        </div>`;
    }

    function cardOrder(a, b) {
        if (Boolean(b.active) !== Boolean(a.active)) return b.active ? 1 : -1;
        if (b.signal_count !== a.signal_count) return b.signal_count - a.signal_count;
        return Math.abs(b.flow_score || 0) - Math.abs(a.flow_score || 0);
    }

    function render() {
        if (!data) return;
        if (data.autotrade) renderAuto(data.autotrade);
        // nothing tradeable yet (e.g. after hours): show the whole session, labelled hypothetical
        const replayMode = !(data.stats?.signals) && data.replay_stats?.signals;
        const st = (replayMode ? data.replay_stats : data.stats) || {};
        $('dt-meta').textContent = `${data.tracked.length} tracked · last bar ${data.last_bar || '—'}${data.session ? ` (${data.session})` : ''} · ${data.market_open ? 'LIVE' : 'MARKET CLOSED — session replay'}${data.pending ? ` · loading ${data.pending}` : ''}${data.demo ? ' · DEMO' : ''}`;
        $('dt-stats').innerHTML = `
            ${replayMode ? '<span class="dt-flag" title="All signals were replayed from bars closed before tracking; these figures assume every signal was taken">SESSION REPLAY · HYPOTHETICAL</span>' : ''}
            <span>SIGNALS <b>${st.signals ?? 0}</b></span>
            <span>OPEN <b class="mf-ct">${st.open ?? 0}</b></span>
            <span>WIN <b>${st.win_rate === null || st.win_rate === undefined ? '—' : st.win_rate + '%'}</b></span>
            <span title="Average result per closed trade after slippage and charges">AVG NET <b class="${(st.avg_r || 0) >= 0 ? 'mf-gt' : 'mf-rt'}">${st.avg_r === null || st.avg_r === undefined ? '—' : sgn(st.avg_r) + 'R'}</b></span>
            <span>TOTAL NET <b class="${(st.total_r || 0) >= 0 ? 'mf-gt' : 'mf-rt'}">${sgn(st.total_r || 0)}R</b></span>
            <span title="Slippage and charges already deducted above">COSTS <b>${num(st.cost_r || 0)}R</b></span>
            ${st.skipped ? `<span title="Not taken because 5 trades were already open — not in the stats">SKIPPED <b>${st.skipped}</b></span>` : ''}
            ${!replayMode && st.replayed ? `<span title="Signals from before a stock was tracked — not tradeable, not in the stats">REPLAYED <b>${st.replayed}</b> excl.</span>` : ''}`;

        const feed = data.feed.filter(feedFilter);
        $('dt-feed').innerHTML = feed.length
            ? `<table class="dt-table"><thead><tr><th title="Bar close time when the signal fired">SIGNAL</th><th>SYMBOL</th><th>SIDE</th><th>SETUP</th><th>Q</th><th>ENTRY</th><th>STOP</th><th>T1</th><th>STATUS</th><th>R</th><th title="Board auto-trade decision for this signal">ORDER</th></tr></thead>
                <tbody>${feed.map(feedRow).join('')}</tbody></table>`
            : '<div class="mf-dim" style="padding:10px">No signals match this filter yet.</div>';

        const cards = [...data.tracked].sort(cardOrder);
        $('dt-board').innerHTML = cards.map(boardCard).join('') || '<div class="mf-dim" style="padding:10px">Nothing tracked yet — money-flow picks are added automatically.</div>';
        const byS = new Map(data.tracked.map(t => [t.symbol, t]));
        const signalsBy = new Map();
        for (const s of data.feed) (signalsBy.get(s.symbol) || signalsBy.set(s.symbol, []).get(s.symbol)).push(s);
        document.querySelectorAll('#dt-board canvas[data-chart]').forEach(c => {
            const t = byS.get(c.dataset.chart);
            if (t) drawMini(c, t, signalsBy.get(t.symbol) || []);
        });
    }

    // Mini 5-minute chart: candles, VWAP, EMA 9 / 20, opening range,
    // previous-day high/low, signal markers and the open trade's levels.
    function drawMini(canvas, t, signals) {
        const rect = canvas.getBoundingClientRect();
        if (!rect.width) return;
        const dpr = window.devicePixelRatio || 1;
        canvas.width = rect.width * dpr; canvas.height = rect.height * dpr;
        const ctx = canvas.getContext('2d');
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        const W = rect.width, H = rect.height, pad = 4, right = W - 46;
        ctx.clearRect(0, 0, W, H);
        const bars = t.bars || [];
        if (!bars.length) return;
        const ind = t.indicators || {};
        const a = t.active;
        const levels = [t.orh, t.orl, a?.stop, a?.target1].filter(v => v !== null && v !== undefined);
        const lo = Math.min(...bars.map(b => b.l), ...levels.filter(v => v > 0));
        const hi = Math.max(...bars.map(b => b.h), ...levels);
        const span = hi - lo || 1;
        const y = v => pad + (hi - v) / span * (H - pad * 2);
        const slot = (right - pad) / Math.max(bars.length, 40);
        const x = i => pad + slot * (i + 0.5);

        const hline = (v, color, dash, label) => {
            if (v === null || v === undefined || v < lo || v > hi) return;
            ctx.strokeStyle = color; ctx.setLineDash(dash); ctx.lineWidth = 1;
            ctx.beginPath(); ctx.moveTo(pad, y(v)); ctx.lineTo(right, y(v)); ctx.stroke();
            ctx.setLineDash([]);
            if (label) { ctx.fillStyle = color; ctx.font = '8.5px JetBrains Mono, monospace'; ctx.textAlign = 'left'; ctx.fillText(`${label} ${v.toFixed(1)}`, right + 3, y(v) + 3); }
        };
        hline(t.orh, 'rgba(255,209,102,.55)', [3, 3], 'ORH');
        hline(t.orl, 'rgba(255,209,102,.55)', [3, 3], 'ORL');
        hline(t.prev_high, 'rgba(160,180,195,.45)', [1, 3], 'PDH');
        hline(t.prev_low, 'rgba(160,180,195,.45)', [1, 3], 'PDL');
        if (a) {
            hline(a.entry, 'rgba(56,214,255,.8)', [], 'IN');
            hline(a.stop, 'rgba(255,90,110,.8)', [4, 3], 'SL');
            hline(a.target1, 'rgba(91,227,160,.8)', [4, 3], 'T1');
        }

        const cw = Math.max(1, slot * 0.6);
        bars.forEach((b, i) => {
            const up = b.c >= b.o;
            ctx.strokeStyle = ctx.fillStyle = up ? '#25c996' : '#f05d71';
            ctx.beginPath(); ctx.moveTo(x(i), y(b.h)); ctx.lineTo(x(i), y(b.l)); ctx.stroke();
            ctx.fillRect(x(i) - cw / 2, Math.min(y(b.o), y(b.c)), cw, Math.max(1, Math.abs(y(b.o) - y(b.c))));
        });
        const line = (vals, color) => {
            if (!vals?.length) return;
            ctx.strokeStyle = color; ctx.lineWidth = 1.1; ctx.beginPath();
            vals.forEach((v, i) => (i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v))));
            ctx.stroke();
        };
        line(ind.vwap, '#38d6ff');
        line(ind.ema9, '#ffad60');
        line(ind.ema20, '#ef7697');

        // entry markers at the signal bar
        const index = new Map(bars.map((b, i) => [b.t, i]));
        for (const s of signals) {
            const i = index.get(s.bar_time);
            if (i === undefined) continue;
            const long = s.side === 'LONG';
            const px = x(i), py = long ? y(bars[i].l) + 7 : y(bars[i].h) - 7;
            ctx.fillStyle = long ? '#5be3a0' : '#ff5a6e';
            ctx.beginPath();
            ctx.moveTo(px, long ? py - 5 : py + 5); ctx.lineTo(px - 4, long ? py + 2 : py - 2); ctx.lineTo(px + 4, long ? py + 2 : py - 2);
            ctx.fill();
        }
    }

    // ── multi-day replay ────────────────────────────────────────────
    function statCells(st) {
        const avg = st.avg_r;
        return `<td class="num">${st.signals}</td><td class="num">${st.closed}</td>
            <td class="num">${st.win_rate === null || st.win_rate === undefined ? '—' : st.win_rate + '%'}</td>
            <td class="num ${(avg || 0) >= 0 ? 'mf-gt' : 'mf-rt'}">${avg === null || avg === undefined ? '—' : sgn(avg) + 'R'}</td>
            <td class="num ${(st.total_r || 0) >= 0 ? 'mf-gt' : 'mf-rt'}">${sgn(st.total_r || 0)}R</td>
            <td class="num">${num(st.cost_r || 0)}R</td>`;
    }

    function statTable(title, rows) {
        if (!rows.length) return '';
        return `<table class="dt-table"><thead><tr><th>${title}</th><th>SIGNALS</th><th>CLOSED</th><th>WIN</th><th>AVG NET</th><th>TOTAL NET</th><th>COSTS</th></tr></thead>
            <tbody>${rows.map(([name, st]) => `<tr><th>${esc(name)}</th>${statCells(st)}</tr>`).join('')}</tbody></table>`;
    }

    async function runReplay(symbols, days) {
        const el = $('dt-replay');
        el.hidden = false;
        el.innerHTML = `<div class="dt-sub">REPLAY · ${esc(symbols.join(', '))} · ${days} sessions <span class="mf-dim">loading 5-minute history…</span></div>`;
        try {
            const res = await fetch(`/api/market/day-trade/replay?symbols=${encodeURIComponent(symbols.join(','))}&days=${days}${demo() ? '&demo=1' : ''}`);
            const r = await res.json();
            if (!res.ok || r.error) throw new Error(r.error || r.detail || res.statusText);
            const labels = { ORB: 'Opening-range breakout', PD_BREAK: 'Previous-day high/low break', VWAP_RECLAIM: 'VWAP reclaim / rejection', PULLBACK: 'Trend pullback', SQUEEZE: 'Squeeze breakout' };
            const sym = Object.entries(r.symbols).map(([name, v]) => [name, v.error ? null : v.stats, v.error]);
            el.innerHTML = `
                <div class="dt-sub">REPLAY · ${days} sessions · net of slippage and charges
                    <span class="mf-dim">same rules as the live panel · each session only sees earlier sessions</span>
                    <button class="dt-x" id="dt-replay-close" title="Close replay">×</button></div>
                <div class="dt-replay-grid">
                    <div>${statTable('ALL SETUPS', [['Combined', r.stats]])}
                         ${statTable('SETUP', Object.entries(r.by_setup).map(([k, v]) => [labels[k] || k, v]))}
                         ${statTable('SIDE', Object.entries(r.by_side))}</div>
                    <div>${statTable('SYMBOL', sym.filter(x => x[1]).map(x => [x[0], x[1]]))}
                         ${sym.filter(x => x[2]).map(x => `<div class="mf-dim">${esc(x[0])}: ${esc(x[2])}</div>`).join('')}</div>
                </div>`;
            $('dt-replay-close').addEventListener('click', () => { el.hidden = true; });
        } catch (e) {
            el.innerHTML = `<div class="dt-sub">REPLAY <span class="mf-rt">${esc(e.message)}</span></div>`;
        }
    }

    // ── interaction ─────────────────────────────────────────────────
    async function track(symbol, action) {
        try {
            const res = await API.post('/api/market/day-trade/track' + qs(), { symbol, action });
            if (res.error) throw new Error(res.error);
            if (action === 'add' && typeof showToast === 'function') showToast(`Tracking ${symbol} on 5-minute setups`, 'success');
            poll();
        } catch (e) {
            if (typeof showToast === 'function') showToast(`Track ${symbol}: ${e.message}`, 'error');
        }
    }

    function bind() {
        $('dt-add-form').addEventListener('submit', e => {
            e.preventDefault();
            const sym = $('dt-add').value.trim().toUpperCase();
            if (!/^[A-Z0-9&.-]{1,32}$/.test(sym)) return;
            $('dt-add').value = '';
            track(sym, 'add');
        });
        $('dt-replay-form').addEventListener('submit', e => {
            e.preventDefault();
            let symbols = $('dt-replay-symbols').value.split(',').map(x => x.trim().toUpperCase()).filter(x => /^[A-Z0-9&.-]{1,32}$/.test(x));
            if (!symbols.length && data) symbols = [...data.tracked].sort(cardOrder).slice(0, 5).map(t => t.symbol);
            if (!symbols.length) return;
            runReplay(symbols.slice(0, 10), Number($('dt-replay-days').value));
        });
        $('dt-filter').addEventListener('click', e => {
            const b = e.target.closest('button'); if (!b) return;
            $('dt-filter').querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b));
            filter = b.dataset.v;
            render();
        });
        for (const id of ['dt-feed', 'dt-board']) {
            $(id).addEventListener('click', e => {
                const rm = e.target.closest('[data-remove]');
                if (rm) { e.stopPropagation(); track(rm.dataset.remove, 'remove'); return; }
                const row = e.target.closest('[data-sym]');
                if (row && typeof openStock === 'function') openStock(row.dataset.sym);
            });
        }
        $('dt-auto-btn').addEventListener('click', () => { $('dt-auto').hidden = !$('dt-auto').hidden; if (!$('dt-auto').hidden) loadAuto(); });
        $('dt-auto-save').addEventListener('click', () => saveAuto(autoForm()));
        $('dt-auto-toggle').addEventListener('click', async () => {
            const enabling = !(auto && auto.enabled);
            if (enabling) {
                const f = autoForm();
                const ok = window.confirm(
                    'Turn ON board auto-trade?\n\nNew Day Trade board signals will be sent as REAL orders to your broker ' +
                    `(risk ₹${f.risk_per_trade} per trade, max ₹${f.max_order_value} per order, up to ${f.max_orders_per_day} orders a day, ` +
                    `${f.sides.join(' + ') || 'no'} side(s)${f.require_preflight ? ', preflight checklist on every order' : ', WITHOUT the preflight checklist'}).\n\n` +
                    'Orders go through the live-trading path and are only sent while live trading is running.');
                if (!ok) return;
                await saveAuto({ ...f, enabled: true });
            } else {
                await saveAuto({ enabled: false });
            }
        });
        $('mf-demo')?.addEventListener('change', () => { data = null; seen = null; poll(); });
        window.addEventListener('resize', () => { if (data && isActive()) render(); });
        document.addEventListener('visibilitychange', () => { if (isActive()) poll(); });
    }

    function start() {
        if (!started) { bind(); started = true; }
        poll();
    }

    return { start };
})();
