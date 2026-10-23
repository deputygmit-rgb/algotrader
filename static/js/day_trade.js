// Day Trade tab: 5-minute setups on the tracked list (money-flow picks + manual
// symbols), a live signal feed with outcomes, a setup chart for the selected
// signal (its trigger level, signal bar, volume test and trade plan from fill to
// exit) and a setup board with a mini 5-minute chart per stock. Signals come
// from src/analysis/intraday_setups.py and only trigger on closed bars.
const DayTrade = (() => {
    const POLL_MS = 10000;
    const STATUS_CLASS = {
        'PENDING': 'pending', 'ACTIVE': 'open', 'T1 HIT': 'win', 'TARGET 2': 'win', 'BREAKEVEN': 'flat',
        'STOPPED': 'loss', 'TIME EXIT': 'flat',
    };
    const OPEN = new Set(['PENDING', 'ACTIVE', 'T1 HIT']);

    let started = false, timer = null, inFlight = false, data = null, filter = 'all';
    let seen = null;              // signal id -> status, for alerts
    let focus = null;             // {symbol, id}: what the setup chart shows (id null = the stock only)
    const $ = id => document.getElementById(id);
    const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    const num = (n, d = 2) => n === null || n === undefined ? '—' : Number(n).toFixed(d);
    const sgn = (n, d = 2) => n === null || n === undefined ? '—' : `${n > 0 ? '+' : n < 0 ? '−' : ''}${Math.abs(n).toFixed(d)}`;
    const demo = () => Boolean($('mf-demo')?.checked);
    const qs = () => demo() ? '?demo=1' : '';
    const isActive = () => $('tab-day-trade')?.classList.contains('active') && !document.hidden;

    // ── data ────────────────────────────────────────────────────────
    async function poll() {
        clearTimeout(timer);
        if (isActive() && !inFlight) {
            inFlight = true;
            try {
                const res = await fetch('/api/market/day-trade' + qs());
                const json = await res.json();
                if (!res.ok || json.error) throw new Error(json.error || res.statusText);
                // a demo session's board only picks stocks during market hours: when it is
                // empty (evenings, weekends) show the simulated sessions (the DEMO switch)
                if (json.demo && !demo() && !json.tracked?.length && !json.market_open && $('dt-demo')) {
                    $('dt-demo').checked = true;
                    $('dt-demo').dispatchEvent(new Event('change'));
                    return;
                }
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
        return `<tr data-sym="${esc(s.symbol)}" data-id="${esc(s.id)}" class="${OPEN.has(s.status) ? 'is-open' : ''}${s.backfilled || s.skipped ? ' bf' : ''}${focus?.id === s.id ? ' sel' : ''}"
            title="${esc(`${s.trigger ?? s.label} · signal at ${s.time} bar close · ${s.entry_time ? `filled ${s.entry_time} open` : 'fills at next bar open'} · T2 ${num(s.target2)} · risk ${num(s.risk_pct)}% · best ${sgn(s.mfe_r)}R / worst ${sgn(s.mae_r)}R`)}">
            <td>${esc(s.time)}${s.backfilled ? '<i title="Signal closed before the stock was tracked; replayed from history and left out of the stats">bf</i>' : ''}${s.skipped ? `<i title="Not taken: ${esc(s.skipped)}; left out of the stats">skip</i>` : ''}</td>
            <th>${esc(s.symbol)}</th>
            <td><span class="dt-side ${s.side === 'LONG' ? 'long' : 'short'}">${s.side}</span></td>
            <td class="dt-setup">${esc(s.label)}</td>
            <td class="num" title="${esc(qualityTip(s))}">${s.quality}${s.market_adj ? `<small style="color:var(${s.market_adj > 0 ? '--t-g' : '--t-r'})"> ${s.market_adj > 0 ? '+' : ''}${s.market_adj}</small>` : ''}</td>
            <td class="num">${num(s.entry)}</td>
            <td class="num">${num(s.stop)}</td>
            <td class="num">${num(s.target1)}</td>
            <td><span class="dt-status ${STATUS_CLASS[s.status] || ''}">${s.status}${s.exit_time ? ` ${esc(s.exit_time)}` : ''}</span>${s.live_flag && s.status !== 'PENDING' ? `<span class="dt-flag" title="Live price says so; confirmed when the bar closes">${esc(s.live_flag)}</span>` : ''}</td>
            <td class="num ${rCls}" title="${esc(rTitle(s))}">${r === null ? '—' : `${sgn(r)}R`}${s.status === 'PENDING' ? '<i>est</i>' : ''}</td>
            <td>${orderCell(s.order)}</td>
        </tr>`;
    }

    // quality = the setup's own score (bars) + the index OI bias at the signal bar's close
    function qualityTip(s) {
        if (s.base_quality == null) return '';
        const mkt = s.market_bias ? `NIFTY OI ${s.market_bias}: ${s.market_adj > 0 ? '+' : ''}${s.market_adj}` : 'no NIFTY OI reading at the signal: no adjustment';
        return `Setup ${s.base_quality} · ${mkt}`;
    }

    // Auto-trade decision for a signal (only signals that were candidates get one)
    function orderCell(o) {
        if (!o) return '<span class="mf-dim">—</span>';
        const tip = esc(`${o.decision} at ${(o.at || '').slice(11, 19)}${o.quantity ? ` · qty ${o.quantity}` : ''}${o.reason ? ` · ${o.reason}` : ''}`);
        if (o.decision === 'sent') return `<span class="dt-ord sent" title="${tip}">SENT #${esc(o.order_id)}</span>`;
        if (o.decision === 'skipped') return `<span class="dt-ord skip" title="${tip}">not sent</span>`;
        return `<span class="dt-ord ${o.decision === 'failed' ? 'fail' : 'block'}" title="${tip}">${esc(o.decision.toUpperCase())}</span>`;
    }


    // each setup's recent track record (src/analysis/efficacy.py)
    function effChips(rows, asOf, labels) {
        if (!rows || !rows.length) return '';
        const chip = r => {
            const cls = r.status === 'suspended' ? 'off' : r.adj > 0 ? 'good' : r.adj < 0 ? 'weak' : '';
            const tip = `${labels[r.setup] || r.setup} ${r.side}: ${r.avg_r >= 0 ? '+' : ''}${r.avg_r.toFixed(2)}R a trade over the last ${r.n} (${r.win_rate}% won), quality ${r.adj > 0 ? '+' : ''}${r.adj}${r.status === 'suspended' ? ' · SUSPENDED: signals are shown but not taken' : ''}`;
            return `<span class="dt-eff-chip ${cls}" title="${esc(tip)}">${esc(r.setup)} ${r.side === 'LONG' ? '▲' : '▼'} <b>${r.avg_r >= 0 ? '+' : ''}${r.avg_r.toFixed(2)}R</b> ·${r.n}${r.status === 'suspended' ? ' OFF' : ''}</span>`;
        };
        return `<span class="mf-dim" style="font-size:9.5px">TRACK RECORD${asOf ? ` ${esc(asOf.slice(5))}` : ''}</span>` + rows.map(chip).join('');
    }

    function effRows(s) {
        const e = s.efficacy;
        if (!e) return '';
        const win = e.window_n ? `<dt class="dt-fi-sub">entry ${e.window === 'early' ? 'to 10:30' : 'after 10:30'}</dt><dd class="${e.window_avg_r >= 0 ? 'mf-gt' : ''}">${e.window_avg_r >= 0 ? '+' : ''}${Number(e.window_avg_r).toFixed(2)}R · ${e.window_n} live · quality ${e.window_adj > 0 ? '+' : ''}${e.window_adj}</dd>` : '';
        if (!e.n) return '<dt>Track record</dt><dd class="mf-dim">none yet for this setup</dd>' + win;
        return win + `<dt>Track record</dt><dd class="${e.status === 'suspended' ? 'mf-rt' : e.avg_r >= 0 ? 'mf-gt' : ''}">${e.avg_r >= 0 ? '+' : ''}${Number(e.avg_r).toFixed(2)}R · last ${e.n}${e.status === 'suspended' ? ' · SUSPENDED' : ''}</dd>
                <dt class="dt-fi-sub">quality adjustment</dt><dd>${e.adj > 0 ? '+' : ''}${e.adj}${s.base_quality_board != null ? ` <span class="mf-dim">(${s.base_quality_board} → ${s.quality})</span>` : ''}</dd>`;
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

    // why the feed is empty, in words (it used to just say "no signals match")
    function emptyFeedReason() {
        const demoNote = data.demo ? ' (DEMO data: simulated market.)' : '';
        if (data.feed.length) return 'No signals match this filter — choose ALL to see every signal.';
        if (!data.tracked.length) {
            return (data.market_open
                ? 'No stocks tracked yet — money-flow picks are added from 09:20, once the first 5-minute bar has closed.'
                : 'No stocks tracked — the board picks stocks from live money flow during market hours.') + demoNote;
        }
        if (data.pending) return `Loading today's 5-minute bars for ${data.pending} of ${data.tracked.length} tracked stocks…` + demoNote;
        if (!data.market_open) return 'Market closed — no signals in the last session for the tracked stocks.' + demoNote;
        if (!data.last_bar || data.last_bar < '09:30') {
            return `Waiting for the opening range (09:15–09:30) to complete — the first setups usually fire from about 09:35. ${data.tracked.length} stocks tracked.` + demoNote;
        }
        const watching = data.tracked.reduce((n, t) => n + ((t.watch || []).length), 0);
        return `No setup has fired yet on the ${data.tracked.length} tracked stocks (last bar ${data.last_bar}); ${watching} trigger levels on watch — see each card.` + demoNote;
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
        return `<div class="dt-card${t.active ? ' live' : ''}${focus?.symbol === t.symbol ? ' sel' : ''}" data-sym="${esc(t.symbol)}">
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
        // settle the selection first, so the feed and the board highlight it
        // an automatic choice follows the data (a signal once one exists); a chosen one stays
        if (!focus || !data.tracked.some(t => t.symbol === focus.symbol) || (focus.auto && !focus.id && data.feed.length)) focus = defaultFocus();
        if (data.autotrade) renderAuto(data.autotrade);
        // nothing tradeable yet (e.g. after hours): show the whole session, labelled hypothetical
        const replayMode = !(data.stats?.signals) && data.replay_stats?.signals;
        const st = (replayMode ? data.replay_stats : data.stats) || {};
        $('dt-meta').textContent = `${data.tracked.length} tracked · last bar ${data.last_bar || '—'}${data.session ? ` (${data.session})` : ''} · ${data.market_open ? 'LIVE' : 'MARKET CLOSED — session replay'}${data.pending ? ` · loading ${data.pending}` : ''}${data.demo ? ' · DEMO: simulated broker, trading account not linked' : ''}`;
        $('dt-stats').innerHTML = `
            ${replayMode ? '<span class="dt-flag" title="All signals were replayed from bars closed before tracking; these figures assume every signal was taken">SESSION REPLAY · HYPOTHETICAL</span>' : ''}
            <span>SIGNALS <b>${st.signals ?? 0}</b></span>
            <span>OPEN <b class="mf-ct">${st.open ?? 0}</b></span>
            <span>WIN <b>${st.win_rate === null || st.win_rate === undefined ? '—' : st.win_rate + '%'}</b></span>
            <span title="Average result per closed trade after slippage and charges">AVG NET <b class="${(st.avg_r || 0) >= 0 ? 'mf-gt' : 'mf-rt'}">${st.avg_r === null || st.avg_r === undefined ? '—' : sgn(st.avg_r) + 'R'}</b></span>
            <span>TOTAL NET <b class="${(st.total_r || 0) >= 0 ? 'mf-gt' : 'mf-rt'}">${sgn(st.total_r || 0)}R</b></span>
            <span title="Slippage and charges already deducted above">COSTS <b>${num(st.cost_r || 0)}R</b></span>
            ${st.skipped ? `<span title="Not taken: portfolio full, or the setup's recent record suspended / weakened it — not in the stats">SKIPPED <b>${st.skipped}</b></span>` : ''}
            ${!replayMode && st.replayed ? `<span title="Signals from before a stock was tracked — not tradeable, not in the stats">REPLAYED <b>${st.replayed}</b> excl.</span>` : ''}`;

        $('dt-eff').innerHTML = effChips(data.efficacy, data.efficacy_as_of, {});
        const feed = data.feed.filter(feedFilter);
        $('dt-feed').innerHTML = feed.length
            ? `<table class="dt-table"><thead><tr><th title="Bar close time when the signal fired">SIGNAL</th><th>SYMBOL</th><th>SIDE</th><th>SETUP</th><th>Q</th><th>ENTRY</th><th>STOP</th><th>T1</th><th>STATUS</th><th>R</th><th title="Board auto-trade decision for this signal">ORDER</th></tr></thead>
                <tbody>${feed.map(feedRow).join('')}</tbody></table>`
            : `<div class="mf-dim" style="padding:10px">${esc(emptyFeedReason())}</div>`;

        const cards = [...data.tracked].sort(cardOrder);
        $('dt-board').innerHTML = cards.map(boardCard).join('') || '<div class="mf-dim" style="padding:10px">Nothing tracked yet — money-flow picks are added automatically.</div>';
        const byS = new Map(data.tracked.map(t => [t.symbol, t]));
        const signalsBy = new Map();
        for (const s of data.feed) (signalsBy.get(s.symbol) || signalsBy.set(s.symbol, []).get(s.symbol)).push(s);
        document.querySelectorAll('#dt-board canvas[data-chart]').forEach(c => {
            const t = byS.get(c.dataset.chart);
            if (t) drawMini(c, t, signalsBy.get(t.symbol) || []);
        });
        renderFocus();
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

    // ── setup chart (selected signal) ───────────────────────────────
    // The setup's own geometry is recomputed here with the engine's rules
    // (src/analysis/intraday_setups.py), so the chart shows exactly what fired.
    // setup rules come from the server for signed-in accounts (src/api/setup_rules.py)
    let SETUP_RULE = {};
    let TOD_NOTE = '';
    async function loadRules() {
        if (Object.keys(SETUP_RULE).length) return;
        try {
            const res = await fetch('/api/market/setup-rules');
            if (!res.ok) return;
            const r = await res.json();
            SETUP_RULE = r.day || {};
            TOD_NOTE = r.day_tod_note || '';
            if (data) render();
        } catch (e) { /* the detail shows without the rule text */ }
    }
    const VOL_LOOKBACK = 10, SQUEEZE_BARS = 6, ORB_BARS = 3;
    const hhmm = b => String(b.t).slice(11, 16);
    const closeOf = b => {
        const [h, m] = hhmm(b).split(':').map(Number);
        const t = h * 60 + m + 5;
        return `${String(Math.floor(t / 60)).padStart(2, '0')}:${String(t % 60).padStart(2, '0')}`;
    };

    function focusSignals(symbol) {
        return (data?.feed || []).filter(s => s.symbol === symbol).sort((a, b) => String(a.bar_time).localeCompare(String(b.bar_time)));
    }

    // first load: the newest signal that was actually tradeable
    function defaultFocus() {
        const s = (data?.feed || []).find(x => !x.backfilled && !x.skipped) || data?.feed?.[0];
        if (s) return { symbol: s.symbol, id: s.id, auto: true };
        const t = [...(data?.tracked || [])].sort(cardOrder)[0];
        return t ? { symbol: t.symbol, id: null, auto: true } : null;
    }

    function select(symbol, id) {
        focus = { symbol, id: id || null };
        document.querySelectorAll('#dt-feed tr[data-id]').forEach(r => r.classList.toggle('sel', r.dataset.id === focus.id));
        document.querySelectorAll('#dt-board .dt-card').forEach(c => c.classList.toggle('sel', c.dataset.sym === symbol));
        renderFocus();
    }

    // Everything the chart and the panel need about one signal, from the bars.
    function setupGeometry(t, s) {
        const bars = t.bars || [];
        const ind = t.indicators || {};
        const i = bars.findIndex(b => b.t === s.bar_time);
        if (i < 0) return null;
        const side = s.side === 'LONG' ? 1 : -1;
        const prior = bars.slice(Math.max(0, i - VOL_LOOKBACK), i).map(b => b.v);
        const avgVol = prior.length ? prior.reduce((a, v) => a + v, 0) / prior.length : 0;
        const volX = avgVol ? bars[i].v / avgVol : null;
        const g = { i, side, avgVol, volX, box: null, level: null, levelName: '' };
        if (s.setup === 'SQUEEZE' && i >= SQUEEZE_BARS) {
            const box = bars.slice(i - SQUEEZE_BARS, i);
            g.box = { from: i - SQUEEZE_BARS, to: i - 1, top: Math.max(...box.map(b => b.h)), bottom: Math.min(...box.map(b => b.l)) };
        }
        if (s.setup === 'ORB') { g.level = side > 0 ? t.orh : t.orl; g.levelName = side > 0 ? 'OR HIGH' : 'OR LOW'; }
        if (s.setup === 'PD_BREAK') { g.level = side > 0 ? t.prev_high : t.prev_low; g.levelName = side > 0 ? 'PREV-DAY HIGH' : 'PREV-DAY LOW'; }
        // quality from the bars alone: volume 35 x min(vol/2, 1) + EMA trend 35 + VWAP side 30
        const e9 = ind.ema9?.[i], e20 = ind.ema20?.[i], vw = ind.vwap?.[i];
        const volScore = s.tod_x != null ? Math.min(s.vol_x ?? volX ?? 1, s.tod_x) : (s.vol_x ?? volX ?? 1);
        g.q = {
            vol: Math.round(35 * Math.min(volScore / 2, 1)),
            trend: e9 != null && e20 != null && (e9 - e20) * side > 0 ? 35 : 0,
            vwap: vw != null && (bars[i].c - vw) * side > 0 ? 30 : 0,
        };
        // the trade from the fill bar to the exit, by the engine's rules
        g.entryI = s.entry_time ? bars.findIndex((b, k) => k > i && hhmm(b) === s.entry_time) : -1;
        g.exitI = s.exit_time ? bars.findIndex((b, k) => k >= Math.max(g.entryI, i + 1) && closeOf(b) === s.exit_time) : -1;
        g.t1I = -1;
        if (g.entryI >= 0 && s.entry != null) {
            let stop = s.stop;
            const last = g.exitI >= 0 ? g.exitI : bars.length - 1;
            for (let k = g.entryI; k <= last; k++) {
                const best = side > 0 ? bars[k].h : bars[k].l, worst = side > 0 ? bars[k].l : bars[k].h;
                if (side * (worst - stop) <= 0 || side * (best - s.target2) >= 0) break;
                if (side * (best - s.target1) >= 0) { g.t1I = k; break; }
            }
        }
        return g;
    }

    function exitPrice(s, t, g) {
        switch (s.status) {
            case 'STOPPED': return s.stop;
            case 'BREAKEVEN': return s.entry;
            case 'TARGET 2': return s.target2;
            case 'TIME EXIT': return g.exitI >= 0 ? t.bars[g.exitI].c : null;
            default: return null;
        }
    }

    function drawSetup(canvas, t, s, signals) {
        const rect = canvas.getBoundingClientRect();
        if (!rect.width) return;
        const dpr = window.devicePixelRatio || 1;
        canvas.width = rect.width * dpr; canvas.height = rect.height * dpr;
        const ctx = canvas.getContext('2d');
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        const W = rect.width, H = rect.height, padL = 6, right = W - 84, axisH = 14;
        const priceH = Math.round((H - axisH) * 0.74), volTop = priceH + 8, volH = H - axisH - volTop;
        ctx.clearRect(0, 0, W, H);
        const bars = t.bars || [];
        if (!bars.length) {
            ctx.fillStyle = '#6b7f8c'; ctx.font = '11px JetBrains Mono, monospace';
            ctx.fillText(t.loading ? 'loading 5-minute bars…' : 'no bars yet', 12, 22);
            return;
        }
        const ind = t.indicators || {};
        const g = s ? setupGeometry(t, s) : null;
        const levels = [];
        if (s) levels.push(s.entry, s.stop, s.target1, s.target2);
        if (g?.level != null) levels.push(g.level);
        if (g?.box) levels.push(g.box.top, g.box.bottom);
        if (!s) levels.push(t.orh, t.orl, ...(t.watch || []).map(w => w.level));
        const lv = levels.filter(v => v != null && v > 0);
        const lo = Math.min(...bars.map(b => b.l), ...lv), hi = Math.max(...bars.map(b => b.h), ...lv);
        const pad = (hi - lo) * 0.04 || 1;
        const top = hi + pad, bot = lo - pad;
        const y = v => 6 + (top - v) / (top - bot) * (priceH - 12);
        const slot = (right - padL) / Math.max(bars.length + (s && s.status === 'PENDING' ? 6 : 1), 40);
        const x = i => padL + slot * (i + 0.5);
        const mono = px => `${px}px JetBrains Mono, monospace`;
        const placed = [];                                     // right-axis labels step aside instead of overlapping
        const tag = (v, color, text) => {
            if (v == null) return;
            let yy = Math.max(8, Math.min(priceH - 4, y(v)));
            for (let k = 0; k < 6 && placed.some(p => Math.abs(p - yy) < 10); k++) yy += yy > priceH / 2 ? -10 : 10;
            placed.push(yy);
            ctx.fillStyle = color; ctx.font = mono(9); ctx.textAlign = 'left';
            ctx.fillText(`${text} ${Number(v).toFixed(2)}`, right + 4, yy + 3);
        };
        const hseg = (v, x0, x1, color, dash, width = 1) => {
            if (v == null) return;
            ctx.strokeStyle = color; ctx.setLineDash(dash); ctx.lineWidth = width;
            ctx.beginPath(); ctx.moveTo(x0, y(v)); ctx.lineTo(x1, y(v)); ctx.stroke(); ctx.setLineDash([]);
        };

        // time axis and grid
        ctx.font = mono(9); ctx.textAlign = 'center'; ctx.fillStyle = '#6b7f8c';
        const every = slot * 6 >= 40 ? 30 : 60;          // minutes between labels: hourly when narrow
        bars.forEach((b, i) => {
            const [hh, mm] = hhmm(b).split(':').map(Number);
            if ((hh * 60 + mm - 555) % every === 0) {
                ctx.fillText(hhmm(b), x(i), H - 3);
                ctx.strokeStyle = 'rgba(26,42,51,.6)'; ctx.beginPath(); ctx.moveTo(x(i), 0); ctx.lineTo(x(i), H - axisH); ctx.stroke();
            }
        });

        // the setup's own geometry, under the candles
        if (s && g) {
            // signal bar column
            ctx.fillStyle = 'rgba(255,209,102,.10)';
            ctx.fillRect(x(g.i) - slot / 2, 0, slot, H - axisH);
            if (s.setup === 'ORB' && t.orh != null) {
                ctx.fillStyle = 'rgba(255,209,102,.08)';
                ctx.fillRect(x(0) - slot / 2, y(t.orh), x(ORB_BARS - 1) - x(0) + slot, y(t.orl) - y(t.orh));
                hseg(t.orh, x(0) - slot / 2, x(g.i) + slot / 2, 'rgba(255,209,102,.75)', [4, 3]);
                hseg(t.orl, x(0) - slot / 2, x(g.i) + slot / 2, 'rgba(255,209,102,.75)', [4, 3]);
                ctx.fillStyle = 'rgba(255,209,102,.9)'; ctx.font = mono(9); ctx.textAlign = 'left';
                ctx.fillText('OPENING RANGE', x(0) - slot / 2, Math.max(10, y(t.orh) - 4));
                tag(t.orh, 'rgba(255,209,102,.9)', 'ORH'); tag(t.orl, 'rgba(255,209,102,.9)', 'ORL');
            }
            if (s.setup === 'PD_BREAK' && g.level != null) {
                hseg(g.level, padL, right, 'rgba(160,180,195,.85)', [6, 3], 1.4);
                tag(g.level, 'rgba(190,205,215,.95)', g.side > 0 ? 'PDH' : 'PDL');
            }
            if (g.box) {
                const bx = x(g.box.from) - slot / 2, bw = x(g.box.to) - x(g.box.from) + slot;
                ctx.fillStyle = 'rgba(56,214,255,.08)'; ctx.fillRect(bx, y(g.box.top), bw, y(g.box.bottom) - y(g.box.top));
                ctx.strokeStyle = 'rgba(56,214,255,.75)'; ctx.setLineDash([3, 2]); ctx.strokeRect(bx, y(g.box.top), bw, y(g.box.bottom) - y(g.box.top)); ctx.setLineDash([]);
                ctx.fillStyle = 'rgba(56,214,255,.95)'; ctx.font = mono(9); ctx.textAlign = 'left';
                ctx.fillText(`${SQUEEZE_BARS}-BAR BOX`, bx, Math.min(priceH - 4, y(g.box.bottom) + 11));
            }
            if (s.setup === 'VWAP_RECLAIM' && g.i >= 2) {
                for (const k of [g.i - 2, g.i - 1]) {      // the two closes on the other side of VWAP
                    ctx.fillStyle = 'rgba(56,214,255,.9)';
                    ctx.beginPath(); ctx.arc(x(k), y(bars[k].c), 2.6, 0, Math.PI * 2); ctx.fill();
                }
            }
            // trade plan: risk (entry→stop) and reward (entry→T2) zones from the fill to the exit
            const from = g.entryI >= 0 ? x(g.entryI) - slot / 2 : x(g.i) + slot / 2;
            const to = g.exitI >= 0 ? x(g.exitI) + slot / 2
                : s.status === 'PENDING' ? x(g.i) + slot * 6.5 : x(bars.length - 1) + slot / 2;
            if (s.entry != null) {
                ctx.fillStyle = 'rgba(255,90,110,.10)'; ctx.fillRect(from, Math.min(y(s.entry), y(s.stop)), to - from, Math.abs(y(s.stop) - y(s.entry)));
                ctx.fillStyle = 'rgba(91,227,160,.08)'; ctx.fillRect(from, Math.min(y(s.entry), y(s.target2)), to - from, Math.abs(y(s.target2) - y(s.entry)));
            }
        }

        // candles
        const cw = Math.max(1, slot * 0.62);
        bars.forEach((b, i) => {
            const up = b.c >= b.o;
            ctx.strokeStyle = ctx.fillStyle = up ? '#25c996' : '#f05d71';
            ctx.lineWidth = 1;
            ctx.beginPath(); ctx.moveTo(x(i), y(b.h)); ctx.lineTo(x(i), y(b.l)); ctx.stroke();
            ctx.fillRect(x(i) - cw / 2, Math.min(y(b.o), y(b.c)), cw, Math.max(1, Math.abs(y(b.o) - y(b.c))));
        });
        const line = (vals, color, width) => {
            if (!vals?.length) return;
            ctx.strokeStyle = color; ctx.lineWidth = width; ctx.beginPath();
            vals.forEach((v, i) => (i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v))));
            ctx.stroke();
        };
        const emph = s && (s.setup === 'VWAP_RECLAIM' || s.setup === 'PULLBACK');
        line(ind.vwap, '#38d6ff', emph ? 2 : 1.1);
        line(ind.ema9, '#ffad60', 1.1);
        line(ind.ema20, '#ef7697', s?.setup === 'PULLBACK' ? 2 : 1.1);

        // plan lines and markers on top
        if (s && g) {
            const from = g.entryI >= 0 ? x(g.entryI) - slot / 2 : x(g.i) + slot / 2;
            const to = g.exitI >= 0 ? x(g.exitI) + slot / 2
                : s.status === 'PENDING' ? x(g.i) + slot * 6.5 : x(bars.length - 1) + slot / 2;
            const pendingDash = s.status === 'PENDING' ? [5, 3] : [];
            hseg(s.entry, from, to, 'rgba(56,214,255,.95)', pendingDash, 1.3);
            const beAt = g.t1I >= 0 ? x(g.t1I) + slot / 2 : to;
            hseg(s.stop, from, beAt, 'rgba(255,90,110,.95)', [4, 3], 1.3);
            if (g.t1I >= 0) hseg(s.entry, beAt, to, 'rgba(255,90,110,.75)', [2, 3], 1.3);   // stop moved to breakeven after T1
            hseg(s.target1, from, to, 'rgba(91,227,160,.9)', [4, 3], 1.1);
            hseg(s.target2, from, to, 'rgba(91,227,160,.9)', [4, 3], 1.1);
            tag(s.entry, '#38d6ff', s.status === 'PENDING' ? 'EST' : 'IN');
            tag(s.stop, '#ff5a6e', 'SL'); tag(s.target1, '#5be3a0', 'T1'); tag(s.target2, '#5be3a0', 'T2');
            // signal marker
            const long = g.side > 0;
            const px = x(g.i), py = long ? y(bars[g.i].l) + 9 : y(bars[g.i].h) - 9;
            ctx.fillStyle = long ? '#5be3a0' : '#ff5a6e';
            ctx.beginPath(); ctx.moveTo(px, long ? py - 6 : py + 6); ctx.lineTo(px - 5, long ? py + 3 : py - 3); ctx.lineTo(px + 5, long ? py + 3 : py - 3); ctx.fill();
            ctx.fillStyle = '#ffd166'; ctx.font = mono(9); ctx.textAlign = 'center';
            ctx.fillText(`SIGNAL ${s.time}`, Math.min(right - 30, Math.max(34, px)), long ? Math.min(priceH - 2, py + 14) : Math.max(9, py - 9));
            // T1 and exit markers
            if (g.t1I >= 0) {
                ctx.fillStyle = '#5be3a0'; ctx.beginPath(); ctx.arc(x(g.t1I), y(s.target1), 3.2, 0, Math.PI * 2); ctx.fill();
            }
            const xp = exitPrice(s, t, g);
            if (g.exitI >= 0 && xp != null) {
                const win = (s.result_r ?? 0) >= 0;
                ctx.strokeStyle = win ? '#5be3a0' : '#ff5a6e'; ctx.lineWidth = 2;
                ctx.beginPath(); ctx.arc(x(g.exitI), y(xp), 4.5, 0, Math.PI * 2); ctx.stroke();
                ctx.fillStyle = win ? '#5be3a0' : '#ff5a6e'; ctx.font = mono(9); ctx.textAlign = 'center';
                ctx.fillText(`${s.status}${s.result_r != null ? ` ${sgn(s.result_r)}R` : ''}`, Math.min(right - 40, Math.max(40, x(g.exitI))), Math.max(9, y(xp) - 8));
            }
            // other signals on this stock today
            const index = new Map(bars.map((b, k) => [b.t, k]));
            for (const o of signals) {
                if (o.id === s.id) continue;
                const k = index.get(o.bar_time); if (k === undefined) continue;
                const l = o.side === 'LONG', oy = l ? y(bars[k].l) + 8 : y(bars[k].h) - 8;
                ctx.fillStyle = 'rgba(200,210,220,.55)';
                ctx.beginPath(); ctx.moveTo(x(k), l ? oy - 5 : oy + 5); ctx.lineTo(x(k) - 4, l ? oy + 2 : oy - 2); ctx.lineTo(x(k) + 4, l ? oy + 2 : oy - 2); ctx.fill();
            }
        } else {
            hseg(t.orh, padL, right, 'rgba(255,209,102,.55)', [3, 3]); tag(t.orh, 'rgba(255,209,102,.9)', 'ORH');
            hseg(t.orl, padL, right, 'rgba(255,209,102,.55)', [3, 3]); tag(t.orl, 'rgba(255,209,102,.9)', 'ORL');
            for (const w of t.watch || []) {
                if (w.setup === 'ORB') continue;
                hseg(w.level, padL, right, 'rgba(255,159,67,.8)', [5, 3]);
                tag(w.level, '#ff9f43', w.side === 'LONG' ? 'ARM ▲' : 'ARM ▼');
            }
        }
        if (t.ltp != null && t.ltp >= bot && t.ltp <= top) {
            ctx.fillStyle = '#d7e3ea'; ctx.fillRect(right, y(t.ltp) - 6, 3, 12);
            tag(t.ltp, '#d7e3ea', 'LTP');
        }

        // volume pane: the signal bar against its 10-bar average and the setup's threshold
        const vmax = Math.max(...bars.map(b => b.v), 1);
        const vy = v => volTop + volH - v / vmax * (volH - 2);
        bars.forEach((b, i) => {
            ctx.fillStyle = g && i === g.i ? '#ffd166' : b.c >= b.o ? 'rgba(37,201,150,.45)' : 'rgba(240,93,113,.45)';
            ctx.fillRect(x(i) - cw / 2, vy(b.v), cw, volTop + volH - vy(b.v));
        });
        ctx.fillStyle = '#6b7f8c'; ctx.font = mono(9); ctx.textAlign = 'left';
        ctx.fillText('VOL', padL, volTop + 9);
        if (g && g.avgVol) {
            const x0 = x(Math.max(0, g.i - VOL_LOOKBACK)) - slot / 2, x1 = x(g.i) + slot / 2;
            ctx.strokeStyle = 'rgba(215,227,234,.7)'; ctx.setLineDash([3, 2]); ctx.lineWidth = 1;
            ctx.beginPath(); ctx.moveTo(x0, vy(g.avgVol)); ctx.lineTo(x1, vy(g.avgVol)); ctx.stroke();
            const need = SETUP_RULE[s.setup]?.vol;
            if (need) {
                ctx.strokeStyle = 'rgba(255,209,102,.7)';
                ctx.beginPath(); ctx.moveTo(x0, vy(g.avgVol * need)); ctx.lineTo(x1, vy(g.avgVol * need)); ctx.stroke();
            }
            ctx.setLineDash([]);
            ctx.fillStyle = '#ffd166'; ctx.textAlign = 'left';
            ctx.fillText(`${g.volX == null ? '—' : g.volX.toFixed(1)}× avg`, right + 4, Math.max(volTop + 9, vy(bars[g.i].v) + 3));
            if (need) ctx.fillText(`needs ≥${need}×`, right + 4, Math.max(volTop + 20, vy(bars[g.i].v) + 14));
        }
    }

    function focusInfo(t, s, signals) {
        const chips = signals.length > 1 || (s && signals.length)
            ? `<div class="dt-fi-chips">${signals.map(o => `<button class="dt-chip${s && o.id === s.id ? ' on' : ''}" data-focus="${esc(o.id)}">${esc(o.time)} ${esc(o.setup)} ${o.side === 'LONG' ? '▲' : '▼'}</button>`).join('')}</div>` : '';
        if (!s) {
            const watch = (t.watch || []).map(w => `<li>${esc(w.text)} <span class="mf-dim">(${sgn(w.distance_pct)}% away)</span></li>`).join('');
            return `${chips}<div class="dt-fi"><h4>NO SETUP YET</h4>
                <p>${t.trade_direction ? `Tracked as <b>${t.trade_direction} only</b> (money-flow bias when it joined the board at ${esc(t.since)}).` : `Tracked since ${esc(t.since)}.`}
                ${t.loading ? ' Loading 5-minute bars…' : t.insufficient_history ? ' Needs 15 earlier sessions of 5-minute history before it can be traded.' : ''}</p>
                ${watch ? `<h4>ARMED LEVELS</h4><ul>${watch}</ul>` : '<p class="mf-dim">No trigger level on watch (VWAP, squeeze and pullback setups have no fixed level until they form).</p>'}</div>`;
        }
        const g = setupGeometry(t, s) || {};
        const rule = SETUP_RULE[s.setup] || {};
        const long = s.side === 'LONG';
        const q = g.q || {};
        const filled = s.entry_time ? `filled ${esc(s.entry_time)}` : 'est. · next open';
        const outcome = s.result_r != null
            ? `<b class="${s.result_r >= 0 ? 'mf-gt' : 'mf-rt'}">${s.status} at ${esc(s.exit_time)} · ${sgn(s.result_r)}R net</b> <span class="mf-dim">(gross ${sgn(s.gross_r)}R − costs ${num(s.cost_r)}R)</span>`
            : OPEN.has(s.status) ? `<b class="mf-ct">${s.status}</b>${s.current_r != null ? ` · live ${sgn(s.current_r)}R` : ''}${s.live_flag ? ` · <b class="dt-flag">${esc(s.live_flag)}</b>` : ''}` : esc(s.status);
        const notes = [
            s.backfilled ? 'Replayed: it fired before the stock joined the board, so it was never tradeable and is left out of the stats.' : '',
            s.skipped ? `Not taken: ${esc(s.skipped)}.` : '',
            s.order ? `Auto-trade: ${esc(s.order.decision)}${s.order.reason ? ` — ${esc(s.order.reason)}` : ''}${s.order.order_id ? ` · order #${esc(s.order.order_id)}` : ''}.` : '',
        ].filter(Boolean).map(n => `<p class="dt-fi-note">${n}</p>`).join('');
        const method = `<div class="dt-fi"><h4>${esc(s.label)} · <span class="dt-side ${long ? 'long' : 'short'}">${s.side}</span></h4>
                ${rule.long ? `<p>${esc(long ? rule.long : rule.short)}${rule.vol ? ` on ≥ ${rule.vol}× the 10-bar average volume` : ''}.${TOD_NOTE} Stop: ${esc(rule.stop)}, kept 0.5–1.2 daily ATR from the fill.</p>` : ''}
                <p class="dt-fi-trig">${esc(s.trigger)}</p></div>
            <div class="dt-fi"><h4>WHY IT QUALIFIED</h4><dl>
                <dt>Volume vs 10 bars</dt><dd>${(s.vol_x ?? g.volX) == null ? '—' : `${Number(s.vol_x ?? g.volX).toFixed(2)}×`}${rule.vol ? ` / needs ${rule.vol}×` : ''}</dd>
                <dt title="The signal bar against the median volume of the same 5-minute slot over the previous 20 sessions">Vs normal at ${esc(String(s.bar_time).slice(11, 16))}</dt><dd>${s.tod_x == null ? '<span class="mf-dim">no 20-session normal yet</span>' : `${Number(s.tod_x).toFixed(2)}× / needs 1.2×`}</dd>
                <dt>Quality</dt><dd><b>${s.quality}</b> <span class="mf-dim">(min 60)</span></dd>
                <dt class="dt-fi-sub">volume</dt><dd>${q.vol ?? '—'} / 35</dd>
                <dt class="dt-fi-sub">EMA 9 vs 20</dt><dd>${q.trend ?? '—'} / 35</dd>
                <dt class="dt-fi-sub">VWAP side</dt><dd>${q.vwap ?? '—'} / 30</dd>
                <dt class="dt-fi-sub">NIFTY OI bias</dt><dd>${s.market_bias ? `${esc(s.market_bias)} ${s.market_adj > 0 ? '+' : ''}${s.market_adj ?? 0}` : 'no reading · 0'}</dd>
                ${effRows(s)}
                ${s.flow_bias ? `<dt>Money flow</dt><dd>${esc(s.flow_bias)}${s.flow_score != null ? ` · ${sgn(s.flow_score, 0)}` : ''}</dd>` : ''}
            </dl></div>`;
        return `${chips}
            ${method}
            <div class="dt-fi"><h4>TRADE PLAN</h4><dl>
                <dt>Signal</dt><dd>${esc(s.time)} bar close</dd>
                <dt>Entry</dt><dd>${num(s.entry)} · ${filled}</dd>
                <dt>Stop</dt><dd class="mf-rt">${num(s.stop)}</dd>
                <dt class="dt-fi-sub">setup level</dt><dd class="mf-dim">${num(s.natural_stop)}</dd>
                <dt title="Half is booked at T1 and the stop on the rest moves to breakeven">T1 · 1.5R · ½ off</dt><dd class="mf-gt">${num(s.target1)}</dd>
                <dt>T2 · 3R</dt><dd class="mf-gt">${num(s.target2)}</dd>
                <dt>Risk</dt><dd>${num(s.risk_pct)}% of price</dd>
                <dt>Outcome</dt><dd class="dt-fi-wrap">${outcome}</dd>
                ${s.mfe_r != null && s.entry_time ? `<dt>Best / worst</dt><dd>${sgn(s.mfe_r)} / ${sgn(s.mae_r)}R</dd><dt>Held</dt><dd>${s.bars_held ?? 0} bars</dd>` : ''}
            </dl>${notes}</div>`;
    }

    function renderFocus() {
        if (!data) return;
        const title = $('dt-focus-title'), open = $('dt-focus-open');
        if (!focus) {
            title.textContent = 'nothing tracked yet';
            open.hidden = true;
            return;
        }
        const t = data.tracked.find(x => x.symbol === focus.symbol);
        const signals = focusSignals(focus.symbol);
        let s = focus.id ? signals.find(x => x.id === focus.id) : null;
        if (focus.id && !s) { focus.id = null; }
        title.textContent = s ? `${t.symbol} · ${s.side} ${s.label} at ${s.time}` : `${t.symbol} · 5-minute session`;
        open.hidden = !t.symbol;
        drawSetup($('dt-focus-chart'), t, s, signals);
        $('dt-focus-info').innerHTML = focusInfo(t, s, signals);
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
        $('dt-replay-form')?.addEventListener('submit', e => {
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
        // a feed row charts that signal's setup; a card charts the stock (its latest signal, if any)
        $('dt-feed').addEventListener('click', e => {
            const row = e.target.closest('tr[data-id]');
            if (row) select(row.dataset.sym, row.dataset.id);
        });
        $('dt-board').addEventListener('click', e => {
            const rm = e.target.closest('[data-remove]');
            if (rm) { e.stopPropagation(); track(rm.dataset.remove, 'remove'); return; }
            const card = e.target.closest('[data-sym]');
            if (!card) return;
            const latest = focusSignals(card.dataset.sym).at(-1);
            select(card.dataset.sym, latest?.id);
        });
        $('dt-focus-info').addEventListener('click', e => {
            const chip = e.target.closest('[data-focus]');
            if (chip && focus) select(focus.symbol, chip.dataset.focus);
        });
        $('dt-focus-open').addEventListener('click', () => {
            if (focus && typeof openStock === 'function') openStock(focus.symbol);
        });
        // DEMO here is the same switch as Market Flow's
        const mfDemo = $('mf-demo'), dtDemo = $('dt-demo');
        if (mfDemo && dtDemo) {
            dtDemo.checked = mfDemo.checked;
            dtDemo.addEventListener('change', () => {
                mfDemo.checked = dtDemo.checked;
                mfDemo.dispatchEvent(new Event('change'));
            });
            mfDemo.addEventListener('change', () => { dtDemo.checked = mfDemo.checked; });
        }
        $('dt-auto-btn')?.addEventListener('click', () => { $('dt-auto').hidden = !$('dt-auto').hidden; if (!$('dt-auto').hidden) loadAuto(); });
        $('dt-auto-save')?.addEventListener('click', () => saveAuto(autoForm()));
        $('dt-auto-toggle')?.addEventListener('click', async () => {
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
        $('mf-demo')?.addEventListener('change', () => { data = null; seen = null; focus = null; poll(); });
        window.addEventListener('resize', () => { if (data && isActive()) render(); });
        document.addEventListener('visibilitychange', () => { if (isActive()) poll(); });
    }

    function start() {
        if (!started) { bind(); started = true; loadRules(); }
        poll();
    }

    return { start };
})();
