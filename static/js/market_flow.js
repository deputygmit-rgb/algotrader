// Market flow HUD (NIFTY 50 / F&O / NIFTY 500).
//
// Every stock is one strand. It enters on the left at the height of its value
// LOOKBACK_MIN minutes ago (above centre = bullish, below = bearish), passes
// through the gate at the time shown on the rail, and fans out on the right
// to where it is now: the strongest movers end at labelled tags, the rest
// fade out at the edge. Lens MONEY FLOW uses the -100..+100 flow score
// (src/analysis/money_flow.py); CASH PRICE uses % change; FUTURE OI uses
// futures OI change coloured by build-up type. Dragging the rail replays history.
const MarketFlow = (() => {
    const COL = {
        bull: [91, 227, 160], bear: [255, 90, 110], flat: [107, 127, 140],
        cyan: [56, 214, 255], amber: [255, 209, 102], orange: [255, 159, 67], blue: [80, 150, 255],
    };
    const BUILDUP_COL = {
        'LONG BUILD-UP': COL.bull, 'SHORT COVERING': COL.cyan,
        'SHORT BUILD-UP': COL.bear, 'LONG UNWINDING': COL.orange,
    };
    const BULLISH_BU = new Set(['LONG BUILD-UP', 'SHORT COVERING']);
    const BEARISH_BU = new Set(['SHORT BUILD-UP', 'LONG UNWINDING']);
    const TAGS_PER_SIDE = 10, LOOKBACK_MIN = 15, RAIL_MIN = 60, TAG_W = 150;
    const FONT = "'JetBrains Mono', ui-monospace, Consolas, monospace";

    let canvas, ctx, stage, tip;
    let W = 0, H = 0, dpr = 1;
    let started = false, paused = false, demo = false;
    let lens = 'flow', universe = 'nifty50', filter = 'all';
    const BIAS_THRESHOLD = 15;   // mirrors money_flow.BIAS_THRESHOLD
    const UNIVERSE_LABEL = { nifty50: 'NIFTY 50', fno: 'F&O', nifty500: 'NIFTY 500' };
    let snap = null, history = [], scrubMin = 0;
    let display = [], scale = 1, counts = { bull: 0, bear: 0, flat: 0 };
    let hoverSym = null, pinSym = null, dragging = false;
    let pollTimer = null, histTimer = null, rafId = null, lastFrame = 0, inFlight = false;
    let stars = [];
    const strands = new Map();

    const $ = id => document.getElementById(id);
    const esc = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    const sgn = (n, d = 2) => n === null || n === undefined ? '—' : (n > 0 ? '+' : n < 0 ? '-' : '') + Math.abs(n).toFixed(d);
    const lvl = n => n === null || n === undefined ? '—' : Number(n).toFixed(1);
    const cr = n => n === null || n === undefined ? '—' : `${n < 0 ? '-' : ''}₹${Math.abs(n) >= 100 ? Math.abs(n).toFixed(0) : Math.abs(n).toFixed(1)}cr`;
    const rgba = (c, a) => `rgba(${c[0]},${c[1]},${c[2]},${a})`;
    const lerp = (a, b, k) => a + (b - a) * k;
    const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
    const hash = s => { let h = 0; for (const ch of s) h = (h * 31 + ch.charCodeAt(0)) | 0; return (h >>> 0) / 4294967295; };
    const isActive = () => $('tab-market-flow')?.classList.contains('active') && !document.hidden;
    const qs = () => demo ? '?demo=1' : '';

    // ── classification (mirrors src/analysis/fno_flow.py) ──────────────
    function sideFromChg(chg) { return chg >= 0.05 ? 'BULL' : chg <= -0.05 ? 'BEAR' : 'FLAT'; }
    function biasOf(flow) { return flow >= BIAS_THRESHOLD ? 'LONG' : flow <= -BIAS_THRESHOLD ? 'SHORT' : 'NEUTRAL'; }
    function buildupOf(chg, oi) {
        if (oi === null || oi === undefined) return 'OI —';
        if (Math.abs(oi) < 0.1 || Math.abs(chg) < 0.05) return 'OI NEUTRAL';
        if (oi > 0) return chg > 0 ? 'LONG BUILD-UP' : 'SHORT BUILD-UP';
        return chg > 0 ? 'SHORT COVERING' : 'LONG UNWINDING';
    }
    function lensSide(r) {
        if (lens === 'oi') return BULLISH_BU.has(r.buildup) ? 'BULL' : BEARISH_BU.has(r.buildup) ? 'BEAR' : 'FLAT';
        if (lens === 'flow') return r.bias === 'LONG' ? 'BULL' : r.bias === 'SHORT' ? 'BEAR' : 'FLAT';
        return r.side;
    }
    function lensValue(r) {
        if (lens === 'flow') return r.flow_score || 0;
        if (universe === 'nifty50' && lens === 'price') return r.nifty_points || 0;
        if (lens === 'oi') {
            const mag = Math.abs(r.oi_change_pct || 0);
            return lensSide(r) === 'BULL' ? mag : lensSide(r) === 'BEAR' ? -mag : 0;
        }
        return r.change_pct;
    }
    function colorOf(r) {
        if (lens === 'oi') return BUILDUP_COL[r.buildup] || COL.flat;
        const s = lensSide(r);
        return s === 'BULL' ? COL.bull : s === 'BEAR' ? COL.bear : COL.flat;
    }
    const sideClass = s => s === 'BULL' ? 'bull' : s === 'BEAR' ? 'bear' : 'flat';
    const buClass = bu => BULLISH_BU.has(bu) ? (bu === 'SHORT COVERING' ? 'mf-ct' : 'mf-gt') : BEARISH_BU.has(bu) ? (bu === 'LONG UNWINDING' ? 'mf-ot' : 'mf-rt') : '';

    // ── data & time travel ──────────────────────────────────────────────
    function nearestPoint(ts) {
        if (!history.length) return null;
        let best = history[0];
        for (const p of history) if (Math.abs(p.ts - ts) < Math.abs(best.ts - ts)) best = p;
        return Math.abs(best.ts - ts) < 5 * 60 ? best : null;
    }

    function withPoint(r, p) {
        const v = p && p.rows[r.symbol];
        if (!v) return r;
        const [chg, oi, score, flow] = v;
        // index points scale linearly with % change for a fixed weight
        const pts = r.nifty_points !== null && r.nifty_points !== undefined && r.change_pct
            ? r.nifty_points * chg / r.change_pct : r.nifty_points;
        const flowScore = flow === undefined || flow === null ? r.flow_score : flow;
        return {
            ...r, nifty_points: pts, change_pct: chg, oi_change_pct: oi, score, flow_score: flowScore,
            bias: biasOf(flowScore), side: sideFromChg(chg),
            buildup: r.buildup === 'CASH' ? 'CASH' : buildupOf(chg, oi), rank: null, _replay: true,
        };
    }

    function rebuild() {
        if (!snap) { display = []; return; }
        let rows = snap.rows.filter(r => (r.universes || []).includes(universe));
        if (lens === 'oi') rows = rows.filter(r => r.buildup !== 'CASH');
        const nowTs = Date.now() / 1000;
        const atTs = nowTs - scrubMin * 60;
        const atPoint = scrubMin > 0 ? nearestPoint(atTs) : null;
        const pastPoint = nearestPoint(atTs - LOOKBACK_MIN * 60) || (history.length ? history[0] : null);
        display = rows.map(r => {
            const cur = atPoint ? withPoint(r, atPoint) : r;
            const past = pastPoint && pastPoint !== atPoint ? withPoint(r, pastPoint) : cur;
            return { row: cur, pastValue: lensValue(past) };
        });
        const mags = display.map(d => Math.abs(lensValue(d.row))).sort((a, b) => a - b);
        const floor = lens === 'flow' ? 30 : universe === 'nifty50' && lens === 'price' ? 0.5 : 0.3;
        scale = Math.max(floor, mags[Math.floor(mags.length * 0.9)] || 1);
        counts = { bull: 0, bear: 0, flat: 0 };
        for (const d of display) counts[sideClass(lensSide(d.row))]++;
        layout();
    }

    function layout() {
        if (!W) return;
        const g = geom();
        const seen = new Set();
        const bySide = { BULL: [], BEAR: [] };
        for (const d of display) {
            const s = lensSide(d.row);
            if (bySide[s]) bySide[s].push(d);
        }
        const tagged = new Map();
        for (const side of ['BULL', 'BEAR']) {
            bySide[side].sort((a, b) => Math.abs(lensValue(b.row)) - Math.abs(lensValue(a.row)));
            const top = bySide[side].slice(0, TAGS_PER_SIDE);
            const y0 = side === 'BULL' ? 34 : g.gy + 30;
            const y1 = side === 'BULL' ? g.gy - 30 : H - 14;
            const step = (y1 - y0) / TAGS_PER_SIDE;
            top.forEach((d, i) => tagged.set(d.row.symbol, { y: y0 + step * (i + 0.5), rank: i + 1 }));
        }
        for (const d of display) {
            const r = d.row, sym = r.symbol;
            seen.add(sym);
            const v = lensValue(r);
            const n = clamp(v / scale, -1, 1);
            const np = clamp(d.pastValue / scale, -1, 1);
            const tag = tagged.get(sym);
            const strength = clamp(Math.abs(v) / scale, 0, 1);
            let s = strands.get(sym);
            const tgt = {
                src: g.gy - np * H * 0.40 + (hash(sym) - 0.5) * 18,
                gate: g.gy - n * 11,
                dst: tag ? tag.y : g.gy - n * H * 0.46 + (hash(sym + 'd') - 0.5) * 22,
                endX: tag ? W - TAG_W - 8 : W - 6 - hash(sym + 'x') * 40,
                str: strength,
            };
            if (!s) {
                s = { src: tgt.src, gate: g.gy, dst: tgt.dst, tagY: tgt.dst, endX: tgt.endX, str: 0, alpha: 0, col: colorOf(r), particles: [] };
                strands.set(sym, s);
            }
            if (tag && !s.tag) s.tagY = tgt.dst;   // newly tagged: appear in place, no slide
            Object.assign(s, { row: r, tgt, talpha: 1, tcol: colorOf(r), tag: tag ? tag.rank : null });
            const want = 1 + Math.round(strength * 3);
            while (s.particles.length < want) s.particles.push({ t: Math.random() * 2, spd: 0.7 + Math.random() * 0.6 });
            s.particles.length = want;
        }
        for (const [sym, s] of strands) if (!seen.has(sym)) { s.talpha = 0; s.tag = null; }
    }

    async function poll() {
        clearTimeout(pollTimer);
        if (!paused && isActive() && !inFlight) {
            inFlight = true;
            try {
                const res = await fetch('/api/market/fno-flow' + qs());
                const json = await res.json();
                if (!res.ok || json.error) throw new Error(json.error || res.statusText);
                snap = json;
                rebuild();
                renderPanels();
                setLive(demo ? 'demo' : 'on', demo ? 'DEMO' : 'LIVE');
                banner(json.baseline_loading
                    ? `Loading previous-session baselines (ATR, pivots, avg volume, OI) for ${json.total} stocks — money-flow factors, levels and plans fill in as they arrive.`
                    : '');
            } catch (e) {
                setLive('err', 'OFFLINE');
                banner(`Live data unavailable (${e.message}). Link your trading account, or tick DEMO to preview with simulated data.`);
            } finally {
                inFlight = false;
            }
        }
        clearTimeout(pollTimer);
        pollTimer = setTimeout(poll, 2000);
    }

    async function loadHistory() {
        clearTimeout(histTimer);
        if (isActive()) {
            try {
                const json = await (await fetch(`/api/market/fno-flow/history${qs() ? qs() + '&' : '?'}minutes=${RAIL_MIN + LOOKBACK_MIN}`)).json();
                history = json.points || [];
                rebuild();
            } catch (e) { /* the rail just stays empty */ }
        }
        histTimer = setTimeout(loadHistory, 60000);
    }

    // ── geometry ────────────────────────────────────────────────────────
    function geom() {
        const gx = Math.max(220, W * 0.38), gy = H * 0.5;
        return { gx, gy, railX: gx - Math.max(80, W * 0.12), y0: 52, y1: H - 40 };
    }

    function bz(p0, p1, p2, p3, t) {
        const u = 1 - t;
        return u * u * u * p0 + 3 * u * u * t * p1 + 3 * u * t * t * p2 + t * t * t * p3;
    }

    function pathPoint(s, T, g) {
        if (T < 1) {
            return [bz(0, g.gx * 0.42, g.gx * 0.72, g.gx, T), bz(s.src, s.src, lerp(s.src, s.gate, 0.88), s.gate, T)];
        }
        const t = T - 1, span = s.endX - g.gx;
        return [bz(g.gx, g.gx + span * 0.38, g.gx + span * 0.62, s.endX, t), bz(s.gate, s.gate, s.dst, s.dst, t)];
    }

    // ── render ──────────────────────────────────────────────────────────
    function frame(ts) {
        rafId = null;
        if (!isActive()) return;
        const dt = Math.min(0.05, (ts - (lastFrame || ts)) / 1000);
        lastFrame = ts;
        draw(dt);
        rafId = requestAnimationFrame(frame);
    }

    function draw(dt) {
        const g = geom();
        const k = 1 - Math.pow(0.004, dt);
        const kTag = 1 - Math.pow(1e-6, dt);
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.fillStyle = '#05080c';
        ctx.fillRect(0, 0, W, H);
        const vg = ctx.createRadialGradient(g.gx, g.gy, 0, g.gx, g.gy, Math.max(W, H) * 0.7);
        vg.addColorStop(0, 'rgba(30,48,60,.35)'); vg.addColorStop(1, 'rgba(0,0,0,0)');
        ctx.fillStyle = vg; ctx.fillRect(0, 0, W, H);

        // dust
        const tnow = performance.now() / 1000;
        for (const st of stars) {
            st.x += st.vx * dt; if (st.x > W) st.x = 0;
            ctx.fillStyle = `rgba(200,220,230,${st.a * (0.6 + 0.4 * Math.sin(tnow * st.tw + st.ph))})`;
            ctx.fillRect(st.x, st.y, st.s, st.s);
        }

        drawRail(g);

        const focus = hoverSym || pinSym;
        for (const [sym, s] of strands) {
            s.src = lerp(s.src, s.tgt.src, k); s.gate = lerp(s.gate, s.tgt.gate, k);
            s.dst = lerp(s.dst, s.tgt.dst, k); s.tagY = lerp(s.tagY, s.tgt.dst, kTag); s.endX = lerp(s.endX, s.tgt.endX, k);
            s.str = lerp(s.str, s.tgt.str, k); s.alpha = lerp(s.alpha, s.talpha, k);
            s.col = s.col.map((c, i) => lerp(c, s.tcol[i], k));
            if (s.talpha === 0 && s.alpha < 0.01) strands.delete(sym);
        }

        // strands
        ctx.globalCompositeOperation = 'lighter';
        const span = (s) => s.endX - g.gx;
        for (const [sym, s] of strands) {
            if (s.alpha < 0.01) continue;
            const on = focus === sym;
            const dim = focus && !on ? 0.25 : 1;
            ctx.strokeStyle = rgba(s.col, (0.10 + 0.45 * s.str + (on ? 0.6 : 0)) * s.alpha * dim);
            ctx.lineWidth = on ? 2.2 : 0.6 + s.str * 1.1;
            ctx.beginPath();
            ctx.moveTo(0, s.src);
            ctx.bezierCurveTo(g.gx * 0.42, s.src, g.gx * 0.72, lerp(s.src, s.gate, 0.88), g.gx, s.gate);
            ctx.bezierCurveTo(g.gx + span(s) * 0.38, s.gate, g.gx + span(s) * 0.62, s.dst, s.endX, s.dst);
            ctx.stroke();
        }

        // particles: bright packets riding each strand left → right
        for (const [sym, s] of strands) {
            if (s.alpha < 0.05) continue;
            const dim = focus && focus !== sym ? 0.25 : 1;
            const light = s.col.map(c => lerp(c, 255, 0.55));
            ctx.fillStyle = rgba(light, 0.9 * s.alpha * dim);
            ctx.beginPath();
            for (const p of s.particles) {
                if (!paused) { p.t += (0.10 + 0.32 * s.str) * p.spd * dt; if (p.t >= 2) p.t -= 2; }
                const [x, y] = pathPoint(s, p.t, g);
                const r = 0.8 + s.str * 1.0;
                ctx.rect(x - r, y - r, r * 2, r * 2);
            }
            ctx.fill();
        }
        ctx.globalCompositeOperation = 'source-over';

        drawGate(g);
        drawTags(g, focus);

        if (!snap) {
            ctx.fillStyle = '#6b7f8c'; ctx.font = `12px ${FONT}`; ctx.textAlign = 'center';
            ctx.fillText('AWAITING FEED…', W / 2, H / 2 + 60);
        }
    }

    function bracket(x, y, w, col, up = false) {
        ctx.strokeStyle = col; ctx.lineWidth = 1.5;
        ctx.beginPath();
        const d = up ? -4 : 4;
        ctx.moveTo(x - w, y + d); ctx.lineTo(x - w, y); ctx.lineTo(x + w, y); ctx.lineTo(x + w, y + d);
        ctx.stroke();
    }

    function drawRail(g) {
        const { railX, y0, y1 } = g;
        ctx.strokeStyle = 'rgba(56,214,255,.18)'; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(railX, y0); ctx.lineTo(railX, y1); ctx.stroke();
        ctx.font = `9px ${FONT}`; ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
        for (let m = 10; m < RAIL_MIN; m += 10) {
            const y = y0 + m / RAIL_MIN * (y1 - y0);
            bracket(railX, y, 6, 'rgba(56,214,255,.35)');
            ctx.fillStyle = 'rgba(107,127,140,.8)'; ctx.fillText(`-${m}m`, railX - 12, y + 2);
        }
        bracket(railX, y1, 9, 'rgba(56,214,255,.5)', true);
        ctx.textAlign = 'center'; ctx.fillStyle = 'rgba(107,127,140,.9)';
        ctx.fillText(`${RAIL_MIN} m`, railX, y1 + 14);

        // handle + connector into the gate
        const hy = y0 + scrubMin / RAIL_MIN * (y1 - y0);
        const c = scrubMin ? COL.amber : COL.cyan;
        ctx.strokeStyle = rgba(c, 0.85); ctx.lineWidth = 1.4;
        ctx.beginPath(); ctx.moveTo(railX, hy);
        ctx.bezierCurveTo(railX + (g.gx - railX) * 0.6, hy, g.gx - 6, lerp(hy, g.gy, 0.4), g.gx, g.gy - 18);
        ctx.stroke();
        ctx.strokeStyle = rgba(c, 1); ctx.lineWidth = 2;
        ctx.strokeRect(railX - 10, hy - 4, 20, 8);
        const at = new Date(Date.now() - scrubMin * 60000);
        const hhmm = at.toTimeString().slice(0, 5);
        ctx.font = `700 10px ${FONT}`; ctx.textAlign = 'center'; ctx.fillStyle = rgba(c, 1);
        ctx.fillText(scrubMin ? `-${scrubMin}m ${hhmm}` : `NOW  ${hhmm}`, railX, hy - 14);
    }

    function drawGate(g) {
        const { gx, gy } = g;
        const pulse = 1 + 0.15 * Math.sin(performance.now() / 300);
        ctx.strokeStyle = 'rgba(56,214,255,.25)'; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.arc(gx, gy, 13 * pulse, 0, Math.PI * 2); ctx.stroke();
        ctx.strokeStyle = 'rgba(56,214,255,.95)'; ctx.lineWidth = 2;
        ctx.beginPath(); ctx.arc(gx, gy, 6, 0, Math.PI * 2); ctx.stroke();
        ctx.fillStyle = 'rgba(56,214,255,.95)';
        ctx.beginPath(); ctx.arc(gx, gy, 2, 0, Math.PI * 2); ctx.fill();
        // gate frame
        bracket(gx, gy - 28, 12, 'rgba(91,227,160,.7)');
        bracket(gx, gy + 28, 12, 'rgba(255,90,110,.7)', true);
        ctx.strokeStyle = 'rgba(56,214,255,.15)';
        ctx.beginPath(); ctx.moveTo(gx - 12, gy - 24); ctx.lineTo(gx - 12, gy + 24); ctx.moveTo(gx + 12, gy - 24); ctx.lineTo(gx + 12, gy + 24); ctx.stroke();

        ctx.textBaseline = 'middle'; ctx.textAlign = 'left';
        // counts follow the lens: flow bias, price side, or futures build-up
        const [up, down, flat] = lens === 'flow' ? ['INFLOW', 'OUTFLOW', 'NEUTRAL']
            : lens === 'oi' ? ['BULLISH OI', 'BEARISH OI', 'NEUTRAL OI'] : ['ADVANCING', 'DECLINING', 'FLAT'];
        ctx.font = `800 12px ${FONT}`; ctx.fillStyle = rgba(COL.bull, 1);
        ctx.fillText(`▲ ${up} ${counts.bull}`, gx - 4, gy - 46);
        ctx.fillStyle = rgba(COL.bear, 1);
        ctx.fillText(`▼ ${down} ${counts.bear}`, gx - 30, gy + 48);
        ctx.font = `9px ${FONT}`; ctx.fillStyle = 'rgba(107,127,140,.9)';
        ctx.fillText(`${flat} ${counts.flat}`, gx - 14, gy + 64);
        if (universe === 'nifty50' && snap?.nifty) {
            const n = snap.nifty;
            ctx.font = `700 11px ${FONT}`; ctx.fillStyle = rgba(n.change >= 0 ? COL.bull : COL.bear, 1);
            ctx.fillText(`NIFTY ${n.last_price.toFixed(1)} ${sgn(n.change, 1)}`, gx - 4, gy - 66);
        }
    }

    function drawTags(g, focus) {
        const x = W - TAG_W;
        for (const [sym, s] of strands) {
            if (!s.tag || s.alpha < 0.05) continue;
            const r = s.row, on = focus === sym;
            const a = s.alpha * (focus && !on ? 0.45 : 1);
            const y = s.tagY - 14;
            ctx.fillStyle = `rgba(8,13,18,${0.9 * a})`;
            ctx.fillRect(x, y, TAG_W - 8, 28);
            ctx.strokeStyle = rgba(s.col, (on ? 0.95 : 0.35) * a); ctx.lineWidth = 1;
            ctx.strokeRect(x + 0.5, y + 0.5, TAG_W - 9, 27);
            ctx.fillStyle = rgba(s.col, a); ctx.fillRect(x, y, 2, 28);
            ctx.textBaseline = 'middle';
            ctx.font = `800 10.5px ${FONT}`; ctx.textAlign = 'left'; ctx.fillStyle = `rgba(255,255,255,${a})`;
            ctx.fillText(sym.length > 10 ? sym.slice(0, 10) : sym, x + 8, y + 9);
            ctx.textAlign = 'right'; ctx.fillStyle = rgba(s.col, a);
            const head = lens === 'flow' ? `${sgn(r.flow_score, 0)}`
                : universe === 'nifty50' && lens === 'price' ? `${sgn(r.nifty_points, 1)}pt`
                : lens === 'oi' ? `${sgn(r.oi_change_pct)}%` : `${sgn(r.change_pct)}%`;
            ctx.fillText(head, x + TAG_W - 14, y + 9);
            ctx.font = `9px ${FONT}`; ctx.textAlign = 'left'; ctx.fillStyle = `rgba(107,127,140,${a})`;
            const sub = lens === 'oi' ? `#${s.tag} ${r.buildup}`
                : lens === 'flow' ? `#${s.tag} · ${sgn(r.change_pct)}% · ${r.vol_ratio ? r.vol_ratio.toFixed(1) + '×' : 'rvol —'}`
                : `#${s.tag} · OI ${sgn(r.oi_change_pct)}%`;
            ctx.fillText(sub.length > 22 ? sub.slice(0, 22) : sub, x + 8, y + 20);
        }
    }

    // ── interaction ─────────────────────────────────────────────────────
    function hitTest(mx, my) {
        const g = geom();
        if (mx >= W - TAG_W) {
            for (const [sym, s] of strands) if (s.tag && Math.abs(my - s.tagY) <= 14 && s.alpha > 0.5) return sym;
        }
        let best = null;
        for (const [sym, s] of strands) {
            if (s.alpha < 0.5) continue;
            for (let T = 0.05; T < 2; T += 0.07) {
                const [x, y] = pathPoint(s, T, g);
                if (Math.abs(x - mx) > 20) continue;
                const d = Math.hypot(mx - x, my - y);
                if (d < 7 && (!best || d < best.d)) best = { sym, d };
            }
        }
        return best ? best.sym : null;
    }

    function onRail(mx, my) {
        const g = geom();
        return Math.abs(mx - g.railX) <= 16 && my >= g.y0 - 20 && my <= g.y1 + 8;
    }

    function setScrubFromY(my) {
        const g = geom();
        const m = Math.round(clamp((my - g.y0) / (g.y1 - g.y0), 0, 1) * RAIL_MIN);
        if (m !== scrubMin) {
            scrubMin = m;
            rebuild();
            renderReplay();
        }
    }

    function renderReplay() {
        const el = $('mf-replay');
        if (!scrubMin) { el.hidden = true; return; }
        const at = new Date(Date.now() - scrubMin * 60000).toTimeString().slice(0, 5);
        el.hidden = false;
        el.textContent = history.length ? `◀ REPLAY -${scrubMin}m ${at} · click for NOW` : `REPLAY -${scrubMin}m · no history yet`;
    }

    function rowFor(sym) {
        const s = strands.get(sym);
        return s ? s.row : snap?.rows.find(r => r.symbol === sym);
    }

    function onMove(e) {
        const rect = canvas.getBoundingClientRect();
        const mx = e.clientX - rect.left, my = e.clientY - rect.top;
        if (dragging) { setScrubFromY(my); return; }
        if (onRail(mx, my)) { canvas.style.cursor = 'ns-resize'; hoverSym = null; tip.hidden = true; return; }
        const sym = hitTest(mx, my);
        hoverSym = sym;
        canvas.style.cursor = sym ? 'pointer' : 'default';
        const r = sym && rowFor(sym);
        if (!r) { tip.hidden = true; return; }
        tip.innerHTML = cardHtml(r);
        tip.hidden = false;
        const tw = tip.offsetWidth, th = tip.offsetHeight;
        tip.style.left = clamp(mx + 16, 8, W - tw - 8) + 'px';
        tip.style.top = clamp(my + 16, 8, H - th - 8) + 'px';
    }

    // Click on any stock (strand, tag, card): highlight it here and open its
    // technical analysis. Pinning is not toggled so it stays highlighted
    // after the analysis is closed.
    function select(sym, scroll = true) {
        pinSym = sym;
        renderPanels();
        if (scroll) document.querySelector(`#mf-cards .mf-card[data-sym="${CSS.escape(sym)}"]`)?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
        if (typeof openStock === 'function') openStock(sym);
    }

    // ── panels ──────────────────────────────────────────────────────────
    const FACTOR_LABEL = {
        flow: '₹FLOW', move: 'MOVE', vwap: 'VWAP', rs: 'RS', clv: 'RANGE',
        oi: 'OI', momentum: 'MOM', breakout: 'PDH/L', book: 'BOOK',
    };

    function factorBars(r) {
        const f = r.factors || {};
        return `<div class="mf-factors">${Object.keys(FACTOR_LABEL).filter(k => f[k] !== undefined).map(k => {
            const v = clamp(f[k], -1, 1);
            const col = v >= 0 ? 'var(--mf-g)' : 'var(--mf-r)';
            const left = v >= 0 ? 50 : 50 + v * 50;
            return `<span title="${FACTOR_LABEL[k]} ${sgn(v)}"><em>${FACTOR_LABEL[k]}</em><i><b style="left:${left}%;width:${Math.abs(v) * 50}%;background:${col}"></b></i></span>`;
        }).join('')}</div>`;
    }

    const levelPair = (label, value) => `<span>${esc(label || '—')}</span><em>${lvl(value)}</em>`;

    function planHtml(p) {
        if (!p) return '';
        return `<div class="lv mf-plan"><span>PLAN</span><em>${p.side === 'LONG' ? 'BUY' : 'SELL'} ${lvl(p.entry)}</em>
            <span>SL</span><em>${lvl(p.stop)}</em><span>T1</span><em>${lvl(p.target1)}</em><span>T2</span><em>${lvl(p.target2)}</em></div>`;
    }

    function cardHtml(r) {
        const side = lensSide(r), cls = sideClass(side);
        const chgCls = r.change_pct > 0 ? 'mf-gt' : r.change_pct < 0 ? 'mf-rt' : '';
        const flowCls = r.flow_score > 0 ? 'mf-gt' : r.flow_score < 0 ? 'mf-rt' : '';
        const oi = r.buildup === 'CASH' ? 'cash only' : r.oi_change_pct === null || r.oi_change_pct === undefined ? 'OI —' : `OI ${sgn(r.oi_change_pct)}%`;
        const vol = r.vol_ratio === null || r.vol_ratio === undefined ? 'rvol —' : `rvol ${r.vol_ratio.toFixed(1)}×`;
        const np = r.nifty_points !== null && r.nifty_points !== undefined ? ` · NIFTY ${sgn(r.nifty_points)} pts` : '';
        const tag = r.bias === 'LONG' ? 'LONG' : r.bias === 'SHORT' ? 'SHORT' : 'NEUTRAL';
        return `<div class="mf-card ${cls}${r.symbol === pinSym ? ' pin' : ''}" data-sym="${esc(r.symbol)}">
            <div class="l1"><span class="mf-tag">${tag}</span><b>${esc(r.symbol)}</b><span class="chg ${chgCls}">${sgn(r.change_pct)}%</span><span class="sc" title="conviction 0-100">${r.score}</span></div>
            <div class="l2">FLOW <b class="${flowCls}">${sgn(r.flow_score, 0)}</b> · ${cr(r.money_flow_cr)} · ${vol} · ${r.vwap_dev_pct === null || r.vwap_dev_pct === undefined ? 'VWAP —'
                : `<span title="${r.vwap_source === 'session_typical' ? 'Session typical price (H+L+C)/3 — the feed sent no VWAP' : 'Price vs VWAP'}">VWAP${r.vwap_source === 'session_typical' ? '≈' : ''} ${sgn(r.vwap_dev_pct)}%</span>`}</div>
            <div class="l2"><span class="bu ${buClass(r.buildup)}">${r.buildup === 'CASH' ? 'NO F&amp;O' : r.buildup}</span> · ${oi}${r.atr_pct ? ` · ATR ${r.atr_pct.toFixed(1)}%` : ''}</div>
            ${factorBars(r)}
            <div class="lv">${levelPair(r.s1_label ? `S ${r.s1_label}` : 'S', r.s1)}${levelPair(r.r1_label ? `R ${r.r1_label}` : 'R', r.r1)}</div>
            ${planHtml(r.plan)}
            <div class="np">LTP ${lvl(r.ltp)} · ${esc(r.sector || '')}${np}</div></div>`;
    }

    function pickRow(r, rank, watch) {
        const p = r.plan || {};
        const flowCls = r.flow_score > 0 ? 'mf-gt' : 'mf-rt';
        return `<tr class="${watch ? 'watch' : ''}${r.symbol === pinSym ? ' pin' : ''}" data-sym="${esc(r.symbol)}">
            <td>${watch ? '<i title="misses the volume or liquidity bar">watch</i>' : rank}</td>
            <th>${esc(r.symbol)}<small>${esc(r.sector || '')}</small></th>
            <td class="num"><b>${r.score}</b></td>
            <td class="num ${flowCls}">${sgn(r.flow_score, 0)}</td>
            <td class="num ${r.change_pct >= 0 ? 'mf-gt' : 'mf-rt'}">${sgn(r.change_pct)}%</td>
            <td class="num">${r.vol_ratio ? r.vol_ratio.toFixed(1) + '×' : '—'}</td>
            <td class="num">${cr(r.money_flow_cr)}</td>
            <td class="num">${lvl(p.entry)}</td>
            <td class="num">${lvl(p.stop)}</td>
            <td class="num">${lvl(p.target1)}</td>
            <td class="num">${lvl(p.target2)}</td>
            <td class="num">${p.risk_pct === undefined ? '—' : p.risk_pct.toFixed(2) + '%'}</td>
        </tr>`;
    }

    function picksHtml(side, rowsBySym) {
        const picks = snap.picks?.[universe] || {};
        const live = (picks[side] || []).map(sym => rowsBySym.get(sym)).filter(Boolean);
        const watch = (picks[`${side}_watch`] || []).map(sym => rowsBySym.get(sym)).filter(Boolean)
            .slice(0, Math.max(3, 10 - live.length));
        if (!live.length && !watch.length) {
            return `<div class="mf-dim" style="padding:10px">No ${side === 'long' ? 'long' : 'short'} setups pass the filters in ${UNIVERSE_LABEL[universe]} right now.</div>`;
        }
        return `<div class="mf-picks-scroll"><table class="mf-pick-table">
            <thead><tr><th>#</th><th>SYMBOL</th><th>SCORE</th><th>FLOW</th><th>CHG</th><th>RVOL</th><th>₹ FLOW</th><th>ENTRY</th><th>STOP</th><th>T1</th><th>T2</th><th>RISK</th></tr></thead>
            <tbody>${live.map((r, i) => pickRow(r, i + 1, false)).join('')}${watch.map(r => pickRow(r, null, true)).join('')}</tbody>
        </table></div>`;
    }

    function sectorsHtml(rowsBySym) {
        const sectors = snap.sectors?.[universe] || [];
        if (!sectors.length) return '<div class="mf-dim" style="padding:10px">No sector has two or more quoted stocks yet.</div>';
        const maxFlow = Math.max(...sectors.map(s => Math.abs(s.net_flow_cr)), 1);
        return sectors.map(s => {
            const col = s.avg_flow_score >= 0 ? 'var(--mf-g)' : 'var(--mf-r)';
            const w = Math.abs(s.net_flow_cr) / maxFlow * 50;
            const left = s.net_flow_cr >= 0 ? 50 : 50 - w;
            return `<div class="mf-sector">
                <div class="nm" title="${esc(s.sector)}">${esc(s.sector)} <small>${s.stocks}</small></div>
                <div class="fs" style="color:${col}">${sgn(s.avg_flow_score, 0)}</div>
                <div class="br" title="${s.advancers} up · ${s.decliners} down"><i style="width:${s.advancers / s.stocks * 100}%"></i></div>
                <div class="nf"><i><b style="left:${left}%;width:${w}%;background:${s.net_flow_cr >= 0 ? 'var(--mf-g)' : 'var(--mf-r)'}"></b></i><span>${cr(s.net_flow_cr)}</span></div>
                <div class="ll"><span class="mf-gt" data-sym="${esc(s.leader)}">${esc(s.leader)}</span> / <span class="mf-rt" data-sym="${esc(s.laggard)}">${esc(s.laggard)}</span></div>
            </div>`;
        }).join('');
    }

    function renderPanels() {
        if (!snap) return;
        const rows = display.map(d => d.row);
        const label = UNIVERSE_LABEL[universe];
        $('mf-count').textContent = `${rows.length} STOCKS · ${label}`;
        const ub = snap.breadth?.[universe];
        $('mf-side-hd').textContent = ub
            ? `${label}: ${ub.stocks}/${ub.total} quoted · OI on ${ub.oi_count} · as of ${snap.as_of.slice(0, 5)} IST`
            : `${snap.quoted}/${snap.total} quoted · as of ${snap.as_of.slice(0, 5)} IST`;
        const n = snap.nifty;
        $('mf-nifty').innerHTML = n
            ? `NIFTY <b style="color:#fff">${n.last_price.toFixed(1)}</b> <span class="${n.change >= 0 ? 'mf-gt' : 'mf-rt'}">${sgn(n.change, 1)} (${sgn(n.change_pct)}%)</span> · <span class="mf-gt">${sgn(n.bullish_points, 1)}</span>/<span class="mf-rt">${sgn(n.bearish_points, 1)}</span> pts`
            : '';
        const b = snap.breadth?.[universe];
        $('mf-breadth').innerHTML = b ? `
            <span>${label}</span>
            <span>ADV <b class="mf-gt">${b.advancers}</b> · DEC <b class="mf-rt">${b.decliners}</b> · FLAT ${b.unchanged ?? '—'}</span>
            <span class="mf-adbar"><i style="width:${b.stocks ? b.advancers / b.stocks * 100 : 0}%"></i></span>
            <span>INFLOW BIAS <b class="mf-gt">${b.long_bias}</b> · OUTFLOW BIAS <b class="mf-rt">${b.short_bias}</b></span>
            <span>NET ₹ FLOW <b class="${b.net_flow_cr >= 0 ? 'mf-gt' : 'mf-rt'}">${cr(b.net_flow_cr)}</b></span>` : '';

        const bySide = side => rows.filter(r => lensSide(r) === side);
        const cards = filter === 'all'
            ? [...bySide('BULL').sort((a, b) => b.score - a.score), ...bySide('BEAR').sort((a, b) => b.score - a.score)]
            : bySide(filter).sort((a, b) => b.score - a.score);
        const pinned = pinSym && cards.find(r => r.symbol === pinSym);
        const shown = cards.slice(0, 40);
        if (pinned && !shown.includes(pinned)) shown.unshift(pinned);
        $('mf-cards').innerHTML = shown.map(cardHtml).join('') || '<div class="mf-dim" style="padding:10px">No stocks on this side.</div>';

        // picks and sectors always use the live snapshot, not a replayed minute
        const live = new Map(snap.rows.map(r => [r.symbol, r]));
        $('mf-picks-long').innerHTML = picksHtml('long', live);
        $('mf-picks-short').innerHTML = picksHtml('short', live);
        $('mf-picks-long-note').textContent = `${(snap.picks?.[universe]?.long || []).length} qualify`;
        $('mf-picks-short-note').textContent = `${(snap.picks?.[universe]?.short || []).length} qualify`;
        $('mf-sectors').innerHTML = sectorsHtml(live);
    }

    // ── misc ────────────────────────────────────────────────────────────
    function setLive(state, text) { $('mf-live').className = 'mf-live ' + state; $('mf-live-text').textContent = text; }
    function banner(msg) { const el = $('mf-banner'); el.hidden = !msg; el.textContent = msg; }

    function resize() {
        const r = stage.getBoundingClientRect();
        if (!r.width) return;
        dpr = window.devicePixelRatio || 1;
        W = r.width; H = r.height;
        canvas.width = W * dpr; canvas.height = H * dpr;
        stars = Array.from({ length: Math.round(W * H / 2600) }, () => ({
            x: Math.random() * W, y: Math.random() * H, s: Math.random() < 0.85 ? 1 : 1.6,
            a: 0.08 + Math.random() * 0.35, tw: 0.5 + Math.random() * 2, ph: Math.random() * 6, vx: 2 + Math.random() * 6,
        }));
        layout();
    }

    function segmented(id, onPick) {
        $(id).addEventListener('click', e => {
            const b = e.target.closest('button'); if (!b) return;
            $(id).querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b));
            onPick(b.dataset.v);
        });
    }

    function bind() {
        canvas = $('mf-canvas'); ctx = canvas.getContext('2d');
        stage = $('mf-stage'); tip = $('mf-tip');
        new ResizeObserver(resize).observe(stage);
        canvas.addEventListener('mousedown', e => {
            const rect = canvas.getBoundingClientRect();
            const mx = e.clientX - rect.left, my = e.clientY - rect.top;
            if (onRail(mx, my)) { dragging = true; setScrubFromY(my); e.preventDefault(); }
        });
        window.addEventListener('mouseup', () => { dragging = false; });
        canvas.addEventListener('mousemove', onMove);
        canvas.addEventListener('mouseleave', () => { hoverSym = null; tip.hidden = true; });
        canvas.addEventListener('click', e => {
            const rect = canvas.getBoundingClientRect();
            if (onRail(e.clientX - rect.left, e.clientY - rect.top)) return;
            if (hoverSym) select(hoverSym); else if (pinSym) { pinSym = null; renderPanels(); }
        });
        for (const id of ['mf-cards', 'mf-picks-long', 'mf-picks-short', 'mf-sectors']) {
            const el = $(id);
            el.addEventListener('mouseover', e => { const c = e.target.closest('[data-sym]'); hoverSym = c ? c.dataset.sym : null; });
            el.addEventListener('mouseleave', () => { hoverSym = null; });
            el.addEventListener('click', e => {
                const c = e.target.closest('[data-sym]'); if (c) select(c.dataset.sym, id !== 'mf-cards');
            });
        }
        segmented('mf-lens', v => { lens = v; rebuild(); renderPanels(); });
        segmented('mf-universe', v => { universe = v; pinSym = null; rebuild(); renderPanels(); });
        segmented('mf-filter', v => { filter = v; renderPanels(); });
        $('mf-replay').addEventListener('click', () => { scrubMin = 0; rebuild(); renderPanels(); renderReplay(); });
        $('mf-pause').addEventListener('click', e => {
            paused = !paused;
            e.target.textContent = paused ? 'RESUME' : 'PAUSE';
            if (paused) setLive('', 'PAUSED'); else poll();
        });
        $('mf-demo').addEventListener('change', e => {
            demo = e.target.checked;
            snap = null; history = []; scrubMin = 0; pinSym = null; strands.clear(); display = [];
            renderReplay();
            poll(); loadHistory();
        });
        document.addEventListener('visibilitychange', () => { if (isActive()) start(); });
    }

    function start() {
        if (!started) { bind(); started = true; loadHistory(); }
        poll();   // fetch right away; layout waits for the first resize
        requestAnimationFrame(() => {
            resize();
            if (!rafId) { lastFrame = 0; rafId = requestAnimationFrame(frame); }
        });
    }

    // Open the flow pinned on one stock (used by the stock analysis view).
    function focus(sym) {
        start();
        pinSym = sym;
        const waitForData = () => {
            if (!snap) { setTimeout(waitForData, 300); return; }
            const row = snap.rows.find(r => r.symbol === sym);
            if (!row) { banner(`${sym} is not in the F&O or NIFTY 500 universe, so it has no strand here.`); return; }
            if (!(row.universes || []).includes(universe)) {
                // switch to the narrowest universe that contains the stock
                universe = ['nifty50', 'fno', 'nifty500'].find(u => row.universes.includes(u)) || universe;
                $('mf-universe').querySelectorAll('button').forEach(b => b.classList.toggle('on', b.dataset.v === universe));
                rebuild();
            }
            renderPanels();
            document.querySelector(`#mf-cards .mf-card[data-sym="${CSS.escape(sym)}"]`)?.scrollIntoView({ block: 'nearest' });
        };
        waitForData();
    }

    return { start, focus };
})();
