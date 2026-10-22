// Swing tab: 1-hour setups with daily context on the scanner's picks (plus
// symbols you track) - a signal feed with outcomes, the selected setup on a
// 1-hour chart with the trend behind it on a daily chart, and a board of cards.
// Signals come from src/analysis/swing_setups.py and trigger on closed bars only.
const Swing = (() => {
    const POLL_MS = 20000;
    const STATUS_CLASS = {
        'PENDING': 'pending', 'ACTIVE': 'open', 'T1 HIT': 'win', 'TARGET 2': 'win', 'BREAKEVEN': 'flat',
        'STOPPED': 'loss', 'TIME EXIT': 'flat',
    };
    const OPEN = new Set(['PENDING', 'ACTIVE', 'T1 HIT']);
    // the public demo shows signals and plans, not the rules behind them (src/api/demo_scope.py)
    const PUBLIC_DEMO = document.body?.dataset.demoScope === 'public';
    // setup rules come from the server for signed-in accounts (src/api/setup_rules.py)
    let RULE = {};
    async function loadRules() {
        if (PUBLIC_DEMO || Object.keys(RULE).length) return;
        try {
            const res = await fetch('/api/market/setup-rules');
            if (!res.ok) return;
            RULE = (await res.json()).swing || {};
            if (data) render();
        } catch (e) { /* the detail shows without the rule text */ }
    }

    let started = false, timer = null, inFlight = false, data = null, filter = 'all', seen = null, focus = null;
    const $ = id => document.getElementById(id);
    const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    const num = (n, d = 2) => n === null || n === undefined ? '—' : Number(n).toFixed(d);
    const sgn = (n, d = 2) => n === null || n === undefined ? '—' : `${n > 0 ? '+' : n < 0 ? '−' : ''}${Math.abs(n).toFixed(d)}`;
    const demo = () => Boolean($('mf-demo')?.checked);
    const qs = () => demo() ? '?demo=1' : '';
    const isActive = () => $('tab-swing')?.classList.contains('active') && !document.hidden;
    const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    const when = s => s ? `${String(s).slice(8, 10)} ${MONTHS[Number(String(s).slice(5, 7)) - 1] || ''} ${String(s).slice(11, 16)}` : '—';
    const dayOf = t => String(t).slice(0, 10);
    const hhmm = t => String(t).slice(11, 16);

    // ── data ────────────────────────────────────────────────────────
    async function poll() {
        clearTimeout(timer);
        if (isActive() && !inFlight) {
            inFlight = true;
            try {
                const res = await fetch('/api/market/swing' + qs());
                const json = await res.json();
                if (!res.ok || json.error) throw new Error(json.error || res.statusText);
                alertChanges(json);
                data = json;
                render();
            } catch (e) {
                $('sw-meta').textContent = `Unavailable: ${e.message}`;
            } finally {
                inFlight = false;
            }
        }
        // bars load in the background on the first visit: check back sooner until they have
        timer = setTimeout(poll, data && !data.pending ? POLL_MS : 5000);
    }

    function alertChanges(next) {
        const key = s => `${s.status}|${s.live_flag || ''}`;
        const current = new Map(next.feed.map(s => [s.id, key(s)]));
        if (seen) {
            for (const s of next.feed) {
                if (s.backfilled || s.skipped) continue;
                const before = seen.get(s.id);
                if (before === undefined && OPEN.has(s.status)) {
                    notify(`SWING ${s.symbol} ${s.side} · ${s.label} at ${when(s.time)} — enter next hourly open (~${num(s.entry)}, SL ${num(s.stop)})`, s.side === 'LONG' ? 'success' : 'error');
                } else if (before && before !== key(s)) {
                    notify(`SWING ${s.symbol} ${s.side} ${s.setup}: ${s.status}${s.result_r !== null && s.result_r !== undefined ? ` ${sgn(s.result_r)}R net` : ''}`, 'info');
                }
            }
        }
        seen = current;
    }

    function notify(msg, type) {
        if (typeof showToast === 'function') showToast(msg, type);
        if (!$('sw-alerts')?.checked) return;
        try {
            const ctx = new (window.AudioContext || window.webkitAudioContext)();
            const osc = ctx.createOscillator(), gain = ctx.createGain();
            osc.frequency.value = type === 'error' ? 392 : 784;
            gain.gain.setValueAtTime(0.08, ctx.currentTime);
            gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.3);
            osc.connect(gain).connect(ctx.destination);
            osc.start(); osc.stop(ctx.currentTime + 0.3);
        } catch { /* audio is optional */ }
    }

    // ── feed and board ──────────────────────────────────────────────
    const rValue = s => s.result_r !== null && s.result_r !== undefined ? s.result_r : (s.current_r ?? null);

    function feedRow(s) {
        const r = rValue(s);
        const rCls = r === null ? '' : r > 0 ? 'mf-gt' : r < 0 ? 'mf-rt' : '';
        return `<tr data-sym="${esc(s.symbol)}" data-id="${esc(s.id)}" class="${OPEN.has(s.status) ? 'is-open' : ''}${s.backfilled || s.skipped ? ' bf' : ''}${focus?.id === s.id ? ' sel' : ''}"
            title="${esc(`${s.trigger ?? s.label} · signal at the ${when(s.time)} bar close · ${s.entry_time ? `filled ${when(s.entry_time)}` : 'fills at the next hourly open'} · T2 ${num(s.target2)}`)}">
            <td>${esc(when(s.time))}${s.backfilled ? '<i title="Fired before the stock joined the board; replayed from history and left out of the stats">bf</i>' : ''}${s.skipped ? `<i title="Not taken: ${esc(s.skipped)}">skip</i>` : ''}</td>
            <th>${esc(s.symbol)}</th>
            <td><span class="dt-side ${s.side === 'LONG' ? 'long' : 'short'}">${s.side}</span></td>
            <td class="dt-setup">${esc(s.label)}</td>
            <td class="num">${s.quality}</td>
            <td class="num">${num(s.entry)}</td>
            <td class="num">${num(s.stop)}</td>
            <td class="num">${num(s.target1)}</td>
            <td><span class="dt-status ${STATUS_CLASS[s.status] || ''}">${s.status}${s.exit_time ? ` ${esc(when(s.exit_time).slice(0, 6))}` : ''}</span>${s.live_flag && s.status !== 'PENDING' ? `<span class="dt-flag">${esc(s.live_flag)}</span>` : ''}</td>
            <td class="num ${rCls}">${r === null ? '—' : `${sgn(r)}R`}${s.status === 'PENDING' ? '<i>est</i>' : ''}</td>
        </tr>`;
    }

    function emptyReason() {
        const demoNote = data.demo ? ' (DEMO: simulated market.)' : '';
        if (data.feed.length) return 'No signals match this filter — choose ALL to see every signal.';
        if (!data.tracked.length) return 'No stocks picked yet — the board takes the scanner’s leaders (or money flow before the first scan).' + demoNote;
        if (data.pending) return `Loading 1-hour and daily bars for ${data.pending} of ${data.tracked.length} stocks…` + demoNote;
        const watching = data.tracked.reduce((n, t) => n + (t.watch || []).length, 0);
        return `No swing setup in the last 20 sessions on the ${data.tracked.length} tracked stocks; ${watching} trigger levels on watch — see each card.` + demoNote;
    }

    function stateLine(t) {
        if (t.loading) return '<span class="mf-dim">loading 1-hour and daily bars…</span>';
        const a = t.active;
        if (a && a.status === 'PENDING') return `<span class="dt-side ${a.side === 'LONG' ? 'long' : 'short'}">${a.side} SIGNAL</span> ${esc(a.setup)} at ${esc(when(a.time))} · next open ~${num(a.entry)} · SL ${num(a.stop)}`;
        if (a) {
            const r = a.current_r ?? null;
            return `<span class="dt-side ${a.side === 'LONG' ? 'long' : 'short'}">IN ${a.side}</span> ${esc(a.setup)} @ ${num(a.entry)} · SL ${num(a.stop)}${r === null ? '' : ` · <b class="${r >= 0 ? 'mf-gt' : 'mf-rt'}">${sgn(r)}R</b>`}${a.status === 'T1 HIT' ? ' · <b class="mf-gt">T1 banked</b>' : ''}`;
        }
        const next = [...(t.watch || [])].sort((x, y) => Math.abs(x.distance_pct) - Math.abs(y.distance_pct))[0];
        return next ? `<span class="mf-dim">ARMED</span> ${esc(next.text)} <span class="mf-dim">(${sgn(next.distance_pct)}%)</span>` : '<span class="mf-dim">no open trade · waiting for a setup</span>';
    }

    function boardCard(t) {
        return `<div class="dt-card${t.active ? ' live' : ''}${focus?.symbol === t.symbol ? ' sel' : ''}" data-sym="${esc(t.symbol)}">
            <div class="dt-card-hd">
                <b>${esc(t.symbol)}</b>
                <span>${num(t.ltp)}</span>
                <span class="${(t.change_pct || 0) >= 0 ? 'mf-gt' : 'mf-rt'}">${sgn(t.change_pct)}%</span>
                ${t.trade_direction ? `<span class="dt-side ${t.trade_direction === 'LONG' ? 'long' : 'short'}" title="${esc(t.why || '')}">${t.trade_direction} ONLY</span>` : ''}
                <span class="dt-src" title="${esc(t.why || '')}">${t.source === 'manual' ? 'MANUAL' : esc((t.why || 'AUTO').toUpperCase())} · ${esc(when(t.since).slice(0, 6))}</span>
                ${t.source === 'manual' ? `<button class="dt-x" data-remove="${esc(t.symbol)}" title="Stop tracking">×</button>` : ''}
            </div>
            <canvas class="dt-chart" data-chart="${esc(t.symbol)}" role="img" aria-label="${esc(t.symbol)} 1-hour chart"></canvas>
            <div class="dt-state">${stateLine(t)}</div>
        </div>`;
    }

    function cardOrder(a, b) {
        if (Boolean(b.active) !== Boolean(a.active)) return b.active ? 1 : -1;
        if (b.signal_count !== a.signal_count) return b.signal_count - a.signal_count;
        return Math.abs(b.flow_score || 0) - Math.abs(a.flow_score || 0);
    }

    const signalsOf = symbol => (data?.feed || []).filter(s => s.symbol === symbol).sort((a, b) => String(a.bar_time).localeCompare(String(b.bar_time)));

    function defaultFocus() {
        const s = (data?.feed || []).find(x => !x.backfilled && !x.skipped) || data?.feed?.[0];
        if (s) return { symbol: s.symbol, id: s.id, auto: true };
        const t = [...(data?.tracked || [])].sort(cardOrder)[0];
        return t ? { symbol: t.symbol, id: null, auto: true } : null;
    }

    function render() {
        if (!data) return;
        // an automatic choice follows the data (a signal once one exists); a chosen one stays
        if (!focus || !data.tracked.some(t => t.symbol === focus.symbol) || (focus.auto && !focus.id && data.feed.length)) focus = defaultFocus();
        const replayMode = !(data.stats?.signals) && data.replay_stats?.signals;
        const st = (replayMode ? data.replay_stats : data.stats) || {};
        $('sw-meta').textContent = `${data.tracked.length} tracked · ${data.market_open ? 'LIVE' : 'MARKET CLOSED'} · last 20 sessions${data.pending ? ` · loading ${data.pending}` : ''}${data.demo ? ' · DEMO: simulated market' : ''}`;
        $('sw-stats').innerHTML = `
            ${replayMode ? '<span class="dt-flag" title="Every signal fired before its stock joined the board; these figures assume each was taken">REPLAY · HYPOTHETICAL</span>' : ''}
            <span>SIGNALS <b>${st.signals ?? 0}</b></span>
            <span>OPEN <b class="mf-ct">${st.open ?? 0}</b></span>
            <span>WIN <b>${st.win_rate == null ? '—' : st.win_rate + '%'}</b></span>
            <span title="Average per closed trade after slippage and delivery charges">AVG NET <b class="${(st.avg_r || 0) >= 0 ? 'mf-gt' : 'mf-rt'}">${st.avg_r == null ? '—' : sgn(st.avg_r) + 'R'}</b></span>
            <span>TOTAL NET <b class="${(st.total_r || 0) >= 0 ? 'mf-gt' : 'mf-rt'}">${sgn(st.total_r || 0)}R</b></span>
            <span title="Slippage and charges already deducted">COSTS <b>${num(st.cost_r || 0)}R</b></span>
            ${st.skipped ? `<span title="Not taken: 5 swing trades open, or the setup's recent record suspended / weakened it">SKIPPED <b>${st.skipped}</b></span>` : ''}`;
        $('sw-eff').innerHTML = effChips(data.efficacy, data.efficacy_as_of, {});
        const feed = data.feed.filter(s => filter === 'open' ? OPEN.has(s.status) : filter === 'LONG' || filter === 'SHORT' ? s.side === filter : true);
        $('sw-feed').innerHTML = feed.length
            ? `<table class="dt-table"><thead><tr><th title="Hourly bar close when the signal fired">SIGNAL</th><th>SYMBOL</th><th>SIDE</th><th>SETUP</th><th>Q</th><th>ENTRY</th><th>STOP</th><th>T1</th><th>STATUS</th><th>R</th></tr></thead>
                <tbody>${feed.map(feedRow).join('')}</tbody></table>`
            : `<div class="mf-dim" style="padding:10px">${esc(emptyReason())}</div>`;
        const cards = [...data.tracked].sort(cardOrder);
        $('sw-board').innerHTML = cards.map(boardCard).join('') || '<div class="mf-dim" style="padding:10px">Nothing tracked yet.</div>';
        const byS = new Map(data.tracked.map(t => [t.symbol, t]));
        document.querySelectorAll('#sw-board canvas[data-chart]').forEach(c => {
            const t = byS.get(c.dataset.chart);
            if (t) drawHourly(c, t, null, signalsOf(t.symbol), true);
        });
        renderFocus();
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
        if (!e.n) return '<dt>Track record</dt><dd class="mf-dim">none yet for this setup</dd>';
        return `<dt>Track record</dt><dd class="${e.status === 'suspended' ? 'mf-rt' : e.avg_r >= 0 ? 'mf-gt' : ''}">${e.avg_r >= 0 ? '+' : ''}${Number(e.avg_r).toFixed(2)}R · last ${e.n}${e.status === 'suspended' ? ' · SUSPENDED' : ''}</dd>
                <dt class="dt-fi-sub">quality adjustment</dt><dd>${e.adj > 0 ? '+' : ''}${e.adj}${s.base_quality_board != null ? ` <span class="mf-dim">(${s.base_quality_board} → ${s.quality})</span>` : ''}</dd>`;
    }

    // ── charts ──────────────────────────────────────────────────────
    function setupCanvas(canvas) {
        const rect = canvas.getBoundingClientRect();
        if (!rect.width) return null;
        const dpr = window.devicePixelRatio || 1;
        canvas.width = rect.width * dpr; canvas.height = rect.height * dpr;
        const ctx = canvas.getContext('2d');
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.clearRect(0, 0, rect.width, rect.height);
        return { ctx, W: rect.width, H: rect.height };
    }
    const mono = px => `${px}px JetBrains Mono, monospace`;

    // price scale + right-axis labels that step aside instead of overlapping
    function scale(ctx, top, bottom, lo, hi, right) {
        const pad = (hi - lo) * 0.05 || 1;
        const y = v => top + (hi + pad - v) / (hi - lo + 2 * pad) * (bottom - top);
        const placed = [];
        const tag = (v, color, text) => {
            if (v == null) return;
            let yy = Math.max(top + 8, Math.min(bottom - 2, y(v)));
            for (let k = 0; k < 6 && placed.some(p => Math.abs(p - yy) < 10); k++) yy += yy > (top + bottom) / 2 ? -10 : 10;
            placed.push(yy);
            ctx.fillStyle = color; ctx.font = mono(9); ctx.textAlign = 'left';
            ctx.fillText(`${text} ${Number(v).toFixed(2)}`, right + 4, yy + 3);
        };
        const hseg = (v, x0, x1, color, dash, w = 1) => {
            if (v == null) return;
            ctx.strokeStyle = color; ctx.setLineDash(dash); ctx.lineWidth = w;
            ctx.beginPath(); ctx.moveTo(x0, y(v)); ctx.lineTo(x1, y(v)); ctx.stroke(); ctx.setLineDash([]);
        };
        return { y, tag, hseg };
    }

    function candles(ctx, bars, x, y, cw) {
        bars.forEach((b, i) => {
            ctx.strokeStyle = ctx.fillStyle = b.c >= b.o ? '#25c996' : '#f05d71';
            ctx.lineWidth = 1;
            ctx.beginPath(); ctx.moveTo(x(i), y(b.h)); ctx.lineTo(x(i), y(b.l)); ctx.stroke();
            ctx.fillRect(x(i) - cw / 2, Math.min(y(b.o), y(b.c)), cw, Math.max(1, Math.abs(y(b.o) - y(b.c))));
        });
    }
    function line(ctx, vals, x, y, color, width) {
        if (!vals?.length) return;
        ctx.strokeStyle = color; ctx.lineWidth = width; ctx.beginPath();
        vals.forEach((v, i) => (i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v))));
        ctx.stroke();
    }

    // 1-hour chart: sessions, EMA 20 / 50, the setup's geometry, volume and the plan
    function drawHourly(canvas, t, s, signals, mini = false) {
        const c = setupCanvas(canvas);
        if (!c) return;
        const { ctx, W, H } = c;
        const bars = t.bars || [];
        if (!bars.length) {
            ctx.fillStyle = '#6b7f8c'; ctx.font = mono(11);
            ctx.fillText(t.loading ? 'loading 1-hour bars…' : 'no bars yet', 12, 22);
            return;
        }
        const right = W - (mini ? 46 : 84), axisH = mini ? 0 : 14, padL = 4;
        const priceH = mini ? H - 4 : Math.round((H - axisH) * 0.74), volTop = priceH + 8, volH = H - axisH - volTop;
        const ind = t.indicators || {};
        const idx = new Map(bars.map((b, i) => [b.t, i]));
        const i = s ? idx.get(s.bar_time) : undefined;
        const g = s?.geometry || {};
        const lv = [];
        if (s) lv.push(s.entry, s.stop, s.target1, s.target2, g.level, ...(g.range || []));
        if (g.box) lv.push(g.box[2], g.box[3]);
        if (!s && !mini) lv.push(...(t.watch || []).map(w => w.level));
        const vals = lv.filter(v => v != null && v > 0);
        const lo = Math.min(...bars.map(b => b.l), ...vals), hi = Math.max(...bars.map(b => b.h), ...vals);
        const { y, tag, hseg } = scale(ctx, 4, priceH, lo, hi, right);
        const slot = (right - padL) / Math.max(bars.length + 1, 40);
        const x = k => padL + slot * (k + 0.5);
        const entryI = s?.entry_time ? bars.findIndex(b => `${dayOf(b.t)} ${hhmm(b.t)}` === s.entry_time) : -1;
        const exitI = s?.exit_time ? bars.findIndex(b => endOf(b) === s.exit_time) : -1;
        const t1I = s?.t1_time ? bars.findIndex(b => endOf(b) === s.t1_time) : -1;

        // sessions
        let prevDay = null, lastLabel = -1e9;
        bars.forEach((b, k) => {
            const d = dayOf(b.t);
            if (d !== prevDay) {
                if (prevDay !== null) {
                    ctx.strokeStyle = 'rgba(26,42,51,.85)'; ctx.lineWidth = 1;
                    ctx.beginPath(); ctx.moveTo(x(k) - slot / 2, 0); ctx.lineTo(x(k) - slot / 2, H - axisH); ctx.stroke();
                }
                if (!mini && x(k) - lastLabel >= 46) {     // skip a label that would overlap the previous one
                    ctx.fillStyle = '#6b7f8c'; ctx.font = mono(9); ctx.textAlign = 'left';
                    ctx.fillText(`${d.slice(8, 10)} ${MONTHS[Number(d.slice(5, 7)) - 1]}`, x(k) - slot / 2 + 2, H - 3);
                    lastLabel = x(k);
                }
                prevDay = d;
            }
        });

        if (s && i !== undefined) {
            ctx.fillStyle = 'rgba(255,209,102,.12)'; ctx.fillRect(x(i) - slot / 2, 0, slot, H - axisH);
            if (g.range) {                                     // inside day / NR7: yesterday's range
                ctx.fillStyle = 'rgba(255,209,102,.07)';
                const dayStart = bars.findIndex(b => dayOf(b.t) === dayOf(s.bar_time));
                const prevStart = bars.findIndex(b => dayOf(b.t) === dayOf(bars[Math.max(0, dayStart - 1)].t));
                ctx.fillRect(x(Math.max(0, prevStart)) - slot / 2, y(g.range[1]), x(i) - x(Math.max(0, prevStart)) + slot, y(g.range[0]) - y(g.range[1]));
            }
            if (g.box && g.box[0] >= 0) {
                const bx = x(g.box[0]) - slot / 2, bw = x(g.box[1]) - x(g.box[0]) + slot;
                ctx.fillStyle = 'rgba(56,214,255,.08)'; ctx.fillRect(bx, y(g.box[3]), bw, y(g.box[2]) - y(g.box[3]));
                ctx.strokeStyle = 'rgba(56,214,255,.8)'; ctx.setLineDash([3, 2]); ctx.strokeRect(bx, y(g.box[3]), bw, y(g.box[2]) - y(g.box[3])); ctx.setLineDash([]);
                if (!mini) { ctx.fillStyle = '#38d6ff'; ctx.font = mono(9); ctx.textAlign = 'left'; ctx.fillText('FLAG', bx, Math.min(priceH - 4, y(g.box[2]) + 11)); }
            }
            if (g.level != null) {
                hseg(g.level, padL, x(i) + slot / 2, 'rgba(190,205,215,.85)', [6, 3], 1.3);
                if (!mini) tag(g.level, '#bfcdd7', { BREAKOUT_20D: s.side === 'LONG' ? '20D HIGH' : '20D LOW', TREND_PULLBACK: 'D EMA20', INSIDE_DAY: s.side === 'LONG' ? 'PREV HIGH' : 'PREV LOW' }[s.setup] || 'LEVEL');
            }
            const from = entryI >= 0 ? x(entryI) - slot / 2 : x(i) + slot / 2;
            const to = exitI >= 0 ? x(exitI) + slot / 2 : s.status === 'PENDING' ? x(i) + slot * 4 : x(bars.length - 1) + slot / 2;
            ctx.fillStyle = 'rgba(255,90,110,.10)'; ctx.fillRect(from, Math.min(y(s.entry), y(s.stop)), to - from, Math.abs(y(s.stop) - y(s.entry)));
            ctx.fillStyle = 'rgba(91,227,160,.08)'; ctx.fillRect(from, Math.min(y(s.entry), y(s.target2)), to - from, Math.abs(y(s.target2) - y(s.entry)));
            s._from = from; s._to = to;
        }

        const cw = Math.max(1, slot * 0.6);
        candles(ctx, bars, x, y, cw);
        line(ctx, ind.ema20, x, y, '#ffad60', 1.1);
        line(ctx, ind.ema50, x, y, '#ef7697', 1.1);

        if (s && i !== undefined) {
            const { _from: from, _to: to } = s;
            const beAt = t1I >= 0 ? x(t1I) + slot / 2 : to;
            hseg(s.entry, from, to, 'rgba(56,214,255,.95)', s.status === 'PENDING' ? [5, 3] : [], 1.3);
            hseg(s.stop, from, beAt, 'rgba(255,90,110,.95)', [4, 3], 1.3);
            if (t1I >= 0) hseg(s.entry, beAt, to, 'rgba(255,90,110,.75)', [2, 3], 1.3);
            hseg(s.target1, from, to, 'rgba(91,227,160,.9)', [4, 3]);
            hseg(s.target2, from, to, 'rgba(91,227,160,.9)', [4, 3]);
            if (!mini) { tag(s.entry, '#38d6ff', s.status === 'PENDING' ? 'EST' : 'IN'); tag(s.stop, '#ff5a6e', 'SL'); tag(s.target1, '#5be3a0', 'T1'); tag(s.target2, '#5be3a0', 'T2'); }
            const long = s.side === 'LONG', py = long ? y(bars[i].l) + 9 : y(bars[i].h) - 9;
            ctx.fillStyle = long ? '#5be3a0' : '#ff5a6e';
            ctx.beginPath(); ctx.moveTo(x(i), long ? py - 6 : py + 6); ctx.lineTo(x(i) - 5, long ? py + 3 : py - 3); ctx.lineTo(x(i) + 5, long ? py + 3 : py - 3); ctx.fill();
            if (!mini) { ctx.fillStyle = '#ffd166'; ctx.font = mono(9); ctx.textAlign = 'center'; ctx.fillText(`SIGNAL ${when(s.time)}`, Math.min(right - 50, Math.max(50, x(i))), long ? Math.min(priceH - 2, py + 14) : Math.max(9, py - 9)); }
            if (t1I >= 0) { ctx.fillStyle = '#5be3a0'; ctx.beginPath(); ctx.arc(x(t1I), y(s.target1), 3.2, 0, Math.PI * 2); ctx.fill(); }
            if (exitI >= 0 && s.exit_price != null) {
                const win = (s.result_r ?? 0) >= 0;
                ctx.strokeStyle = win ? '#5be3a0' : '#ff5a6e'; ctx.lineWidth = 2;
                ctx.beginPath(); ctx.arc(x(exitI), y(s.exit_price), 4.5, 0, Math.PI * 2); ctx.stroke();
                if (!mini) { ctx.fillStyle = win ? '#5be3a0' : '#ff5a6e'; ctx.font = mono(9); ctx.textAlign = 'center'; ctx.fillText(`${s.status} ${sgn(s.result_r)}R`, Math.min(right - 40, Math.max(40, x(exitI))), Math.max(9, y(s.exit_price) - 8)); }
            }
        } else if (mini) {
            for (const o of signals) {
                const k = idx.get(o.bar_time); if (k === undefined) continue;
                const l = o.side === 'LONG', oy = l ? y(bars[k].l) + 7 : y(bars[k].h) - 7;
                ctx.fillStyle = l ? '#5be3a0' : '#ff5a6e';
                ctx.beginPath(); ctx.moveTo(x(k), l ? oy - 5 : oy + 5); ctx.lineTo(x(k) - 4, l ? oy + 2 : oy - 2); ctx.lineTo(x(k) + 4, l ? oy + 2 : oy - 2); ctx.fill();
            }
        } else {
            for (const w of t.watch || []) { hseg(w.level, padL, right, 'rgba(255,159,67,.8)', [5, 3]); tag(w.level, '#ff9f43', w.side === 'LONG' ? 'ARM ▲' : 'ARM ▼'); }
        }
        if (!mini && t.ltp != null) { ctx.fillStyle = '#d7e3ea'; ctx.fillRect(right, y(t.ltp) - 6, 3, 12); tag(t.ltp, '#d7e3ea', 'LTP'); }

        if (mini) return;
        const vmax = Math.max(...bars.map(b => b.v), 1);
        const vy = v => volTop + volH - v / vmax * (volH - 2);
        bars.forEach((b, k) => {
            ctx.fillStyle = k === i ? '#ffd166' : b.c >= b.o ? 'rgba(37,201,150,.45)' : 'rgba(240,93,113,.45)';
            ctx.fillRect(x(k) - cw / 2, vy(b.v), cw, volTop + volH - vy(b.v));
        });
        ctx.fillStyle = '#6b7f8c'; ctx.font = mono(9); ctx.textAlign = 'left'; ctx.fillText('VOL', padL, volTop + 9);
        if (s && i !== undefined) {
            ctx.fillStyle = '#ffd166';
            ctx.fillText(`${num(s.vol_x, 1)}× prev session`, right + 4, Math.max(volTop + 9, vy(bars[i].v) + 3));
            ctx.fillText(s.tod_x == null ? 'no hour normal' : `${num(s.tod_x, 1)}× normal ${hhmm(s.bar_time)}`, right + 4, Math.max(volTop + 20, vy(bars[i].v) + 14));
        }
    }
    function endOf(b) {               // 'YYYY-MM-DD HH:MM' when an hourly bar closes
        const [h, m] = hhmm(b.t).split(':').map(Number);
        const end = Math.min(h * 60 + m + 60, 15 * 60 + 30);
        return `${dayOf(b.t)} ${String(Math.floor(end / 60)).padStart(2, '0')}:${String(end % 60).padStart(2, '0')}`;
    }

    // 1-day chart: the trend behind the setup
    function drawDaily(canvas, t, s) {
        const c = setupCanvas(canvas);
        if (!c) return;
        const { ctx, W, H } = c;
        const bars = t.daily || [];
        if (!bars.length) { ctx.fillStyle = '#6b7f8c'; ctx.font = mono(11); ctx.fillText(t.loading ? 'loading daily bars…' : 'no daily bars', 12, 22); return; }
        const right = W - 84, axisH = 14, padL = 4;
        const lv = t.levels || {};
        const vals = [lv.high20, lv.low20, s?.entry, s?.stop, s?.target1, s?.target2].filter(v => v != null && v > 0);
        const lo = Math.min(...bars.map(b => b.l), ...vals), hi = Math.max(...bars.map(b => b.h), ...vals);
        const { y, tag, hseg } = scale(ctx, 4, H - axisH, lo, hi, right);
        const slot = (right - padL) / Math.max(bars.length + 1, 40);
        const x = k => padL + slot * (k + 0.5);
        ctx.fillStyle = '#6b7f8c'; ctx.font = mono(9); ctx.textAlign = 'center';
        bars.forEach((b, k) => { if (k % 15 === 0) ctx.fillText(`${dayOf(b.t).slice(8, 10)} ${MONTHS[Number(dayOf(b.t).slice(5, 7)) - 1]}`, x(k), H - 3); });
        const dayIdx = d => bars.findIndex(b => dayOf(b.t) === d);
        let si = -1;
        if (s) {
            si = dayIdx(dayOf(s.bar_time));
            const col = si >= 0 ? si : bars.length;          // the signal day may still be forming (not a completed daily bar)
            ctx.fillStyle = 'rgba(255,209,102,.13)'; ctx.fillRect(x(col) - slot / 2, 0, slot, H - axisH);
        }
        hseg(lv.high20, padL, right, 'rgba(190,205,215,.6)', [6, 3]); tag(lv.high20, '#bfcdd7', '20D H');
        hseg(lv.low20, padL, right, 'rgba(190,205,215,.6)', [6, 3]); tag(lv.low20, '#bfcdd7', '20D L');
        candles(ctx, bars, x, y, Math.max(1, slot * 0.62));
        const di = t.daily_indicators || {};
        line(ctx, di.ema20, x, y, '#ffad60', 1.2);
        line(ctx, di.ema50, x, y, '#ef7697', 1.2);
        if (s) {
            const from = x(si >= 0 ? si : bars.length) - slot / 2;
            const exitDay = s.exit_time ? dayIdx(s.exit_time.slice(0, 10)) : -1;
            const to = exitDay >= 0 ? x(exitDay) + slot / 2 : right;
            hseg(s.entry, from, to, 'rgba(56,214,255,.9)', [], 1.2);
            hseg(s.stop, from, to, 'rgba(255,90,110,.9)', [4, 3], 1.2);
            hseg(s.target1, from, to, 'rgba(91,227,160,.85)', [4, 3]);
            hseg(s.target2, from, to, 'rgba(91,227,160,.85)', [4, 3]);
            tag(s.entry, '#38d6ff', 'IN'); tag(s.stop, '#ff5a6e', 'SL'); tag(s.target2, '#5be3a0', 'T2');
        }
        if (t.ltp != null) tag(t.ltp, '#d7e3ea', 'LTP');
    }

    function info(t, s, signals) {
        const chips = signals.length ? `<div class="dt-fi-chips">${signals.map(o => `<button class="dt-chip${s && o.id === s.id ? ' on' : ''}" data-focus="${esc(o.id)}">${esc(when(o.time))} ${esc(o.setup)} ${o.side === 'LONG' ? '▲' : '▼'}</button>`).join('')}</div>` : '';
        const lv = t.levels || {};
        const trend = lv.ema20 && lv.ema50 ? (lv.prev_close > lv.ema20 && lv.ema20 > lv.ema50 ? 'up (close > EMA 20 > EMA 50)' : lv.prev_close < lv.ema20 && lv.ema20 < lv.ema50 ? 'down (close < EMA 20 < EMA 50)' : 'mixed') : '—';
        const context = `<div class="dt-fi"><h4>DAILY CONTEXT NOW</h4><dl>
            <dt>Trend</dt><dd>${esc(trend)}</dd>
            <dt>20-day high / low</dt><dd>${num(lv.high20)} / ${num(lv.low20)}</dd>
            <dt>Daily ATR</dt><dd>${num(lv.atr)}</dd>
            <dt>Picked</dt><dd>${esc(t.why || '—')}${t.trade_direction ? ` · ${t.trade_direction} only` : ''}</dd></dl></div>`;
        if (!s) {
            const watch = (t.watch || []).map(w => `<li>${esc(w.text)} <span class="mf-dim">(${sgn(w.distance_pct)}% away)</span></li>`).join('');
            return `${chips}<div class="dt-fi"><h4>NO SWING SETUP IN 20 SESSIONS</h4>${watch ? `<h4>ARMED LEVELS</h4><ul>${watch}</ul>` : '<p class="mf-dim">No level on watch: pullbacks and flags have no fixed trigger until they form.</p>'}</div>${context}`;
        }
        const rule = RULE[s.setup] || {};
        const long = s.side === 'LONG';
        const outcome = s.result_r != null
            ? `<b class="${s.result_r >= 0 ? 'mf-gt' : 'mf-rt'}">${s.status} ${esc(when(s.exit_time))} · ${sgn(s.result_r)}R</b> <span class="mf-dim">(gross ${sgn(s.gross_r)} − costs ${num(s.cost_r)})</span>`
            : OPEN.has(s.status) ? `<b class="mf-ct">${s.status}</b>${s.current_r != null ? ` · live ${sgn(s.current_r)}R` : ''}${s.live_flag ? ` · <b class="dt-flag">${esc(s.live_flag)}</b>` : ''}` : esc(s.status);
        const notes = [
            s.backfilled ? 'Replayed: it fired before the stock joined the board, so it is left out of the stats.' : '',
            s.skipped ? `Not taken: ${esc(s.skipped)}.` : '',
        ].filter(Boolean).map(n => `<p class="dt-fi-note">${n}</p>`).join('');
        const method = PUBLIC_DEMO
            ? `<div class="dt-fi"><h4>${esc(s.label)} · <span class="dt-side ${long ? 'long' : 'short'}">${s.side}</span></h4>
                <p>Quality <b>${s.quality}</b>. The trigger rule and quality breakdown are shown in your own account.</p></div>`
            : `<div class="dt-fi"><h4>${esc(s.label)} · <span class="dt-side ${long ? 'long' : 'short'}">${s.side}</span></h4>
                ${rule.long ? `<p>${esc(long ? rule.long : rule.short)}, on ≥ ${rule.vol}× volume against both the previous session and the normal for that hour. Stop: ${esc(rule.stop)}, kept 0.6–1.5 daily ATR from the fill.</p>` : ''}
                <p class="dt-fi-trig">${esc(s.trigger)}</p></div>
            <div class="dt-fi"><h4>WHY IT QUALIFIED</h4><dl>
                <dt>Vs previous session</dt><dd>${num(s.vol_x)}×${rule.vol ? ` / needs ${rule.vol}×` : ''}</dd>
                <dt>Vs normal at ${esc(hhmm(s.bar_time))}</dt><dd>${s.tod_x == null ? '<span class="mf-dim">no 20-session normal yet</span>' : `${num(s.tod_x)}×${rule.vol ? ` / needs ${rule.vol}×` : ''}`}</dd>
                <dt>Quality</dt><dd><b>${s.quality}</b> <span class="mf-dim">(min 60)</span></dd>
                <dt class="dt-fi-sub">volume · daily trend · hourly trend</dt><dd class="mf-dim">35 · 35 · 30</dd>
                ${effRows(s)}
                <dt>Daily ATR then</dt><dd>${num(s.atr)}</dd>
            </dl></div>`;
        return `${chips}
            ${method}
            <div class="dt-fi"><h4>TRADE PLAN</h4><dl>
                <dt>Signal</dt><dd>${esc(when(s.time))} close</dd>
                <dt>Entry</dt><dd>${num(s.entry)} · ${s.entry_time ? `filled ${esc(when(s.entry_time))}` : 'est. · next open'}</dd>
                <dt>Stop</dt><dd class="mf-rt">${num(s.stop)}</dd>
                ${PUBLIC_DEMO ? '' : `<dt class="dt-fi-sub">setup level</dt><dd class="mf-dim">${num(s.natural_stop)}</dd>`}
                <dt title="Half is booked at T1 and the stop on the rest moves to breakeven">T1 · 1.5R · ½ off</dt><dd class="mf-gt">${num(s.target1)}</dd>
                <dt>T2 · 3R</dt><dd class="mf-gt">${num(s.target2)}</dd>
                <dt>Risk</dt><dd>${num(s.risk_pct)}% of price</dd>
                <dt>Outcome</dt><dd class="dt-fi-wrap">${outcome}</dd>
                ${s.entry_time ? `<dt>Best / worst</dt><dd>${sgn(s.mfe_r)} / ${sgn(s.mae_r)}R</dd><dt>Held</dt><dd>${s.bars_held ?? 0} hourly bars</dd>` : ''}
            </dl>${notes}</div>${context}`;
    }

    function renderFocus() {
        if (!data || !focus) { $('sw-focus-title').textContent = 'nothing tracked yet'; $('sw-focus-open').hidden = true; return; }
        const t = data.tracked.find(x => x.symbol === focus.symbol);
        if (!t) return;
        const signals = signalsOf(focus.symbol);
        let s = focus.id ? signals.find(x => x.id === focus.id) : null;
        if (focus.id && !s) focus.id = null;
        $('sw-focus-title').textContent = s ? `${t.symbol} · ${s.side} ${s.label} at ${when(s.time)}` : `${t.symbol} · 1-hour and daily`;
        $('sw-focus-open').hidden = false;
        drawHourly($('sw-chart-h'), t, s, signals);
        drawDaily($('sw-chart-d'), t, s);
        $('sw-info').innerHTML = info(t, s, signals);
        document.querySelectorAll('#sw-feed tr[data-id]').forEach(r => r.classList.toggle('sel', r.dataset.id === focus.id));
        document.querySelectorAll('#sw-board .dt-card').forEach(c => c.classList.toggle('sel', c.dataset.sym === focus.symbol));
    }

    function select(symbol, id) {
        focus = { symbol, id: id || null };
        renderFocus();
    }

    async function track(symbol, action) {
        try {
            const res = await API.post('/api/market/swing/track' + qs(), { symbol, action });
            if (res.error) throw new Error(res.error);
            if (action === 'add' && typeof showToast === 'function') showToast(`Tracking ${symbol} on swing setups`, 'success');
            poll();
        } catch (e) {
            if (typeof showToast === 'function') showToast(`Track ${symbol}: ${e.message}`, 'error');
        }
    }

    function bind() {
        $('sw-add-form').addEventListener('submit', e => {
            e.preventDefault();
            const sym = $('sw-add').value.trim().toUpperCase();
            if (!/^[A-Z0-9&.-]{1,32}$/.test(sym)) return;
            $('sw-add').value = '';
            track(sym, 'add');
        });
        $('sw-filter').addEventListener('click', e => {
            const b = e.target.closest('button'); if (!b) return;
            $('sw-filter').querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b));
            filter = b.dataset.v;
            render();
        });
        $('sw-feed').addEventListener('click', e => {
            const row = e.target.closest('tr[data-id]');
            if (row) select(row.dataset.sym, row.dataset.id);
        });
        $('sw-board').addEventListener('click', e => {
            const rm = e.target.closest('[data-remove]');
            if (rm) { e.stopPropagation(); track(rm.dataset.remove, 'remove'); return; }
            const card = e.target.closest('[data-sym]');
            if (card) select(card.dataset.sym, signalsOf(card.dataset.sym).at(-1)?.id);
        });
        $('sw-info').addEventListener('click', e => {
            const chip = e.target.closest('[data-focus]');
            if (chip && focus) select(focus.symbol, chip.dataset.focus);
        });
        $('sw-focus-open').addEventListener('click', () => { if (focus && typeof openStock === 'function') openStock(focus.symbol); });
        const mfDemo = $('mf-demo'), swDemo = $('sw-demo');
        if (mfDemo && swDemo) {
            swDemo.checked = mfDemo.checked;
            swDemo.addEventListener('change', () => { mfDemo.checked = swDemo.checked; mfDemo.dispatchEvent(new Event('change')); });
            mfDemo.addEventListener('change', () => { swDemo.checked = mfDemo.checked; data = null; seen = null; focus = null; if (isActive()) poll(); });
        }
        window.addEventListener('resize', () => { if (data && isActive()) render(); });
        document.addEventListener('visibilitychange', () => { if (isActive()) poll(); });
    }

    function start() {
        if (!started) { bind(); started = true; loadRules(); }
        poll();
    }

    return { start };
})();
