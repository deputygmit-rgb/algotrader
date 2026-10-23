// Landing page: an interactive, simulated replica of the terminal (Market Flow,
// Day Trade board, OI Tracker, Agents, Backtest), plus demo start and sign-in.
// All values are generated in the browser - illustrative, never market data.
//
// Motion model: data changes once a second; what is on screen eases toward it
// every animation frame (strands, particles, bars, numbers), so the preview
// moves continuously instead of jumping. Elements are created once and updated
// in place. Animation runs only while the terminal is visible, and is reduced
// to plain value updates for visitors who prefer reduced motion.
(function () {
    'use strict';
    const $ = id => document.getElementById(id);
    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const phone = window.matchMedia('(max-width: 599px)').matches;
    const rnd = (a, b) => a + Math.random() * (b - a);
    const pick = arr => arr[Math.floor(Math.random() * arr.length)];
    const fmt = (n, d = 2) => n.toLocaleString('en-IN', { minimumFractionDigits: d, maximumFractionDigits: d });
    const sgn = (n, d = 2) => `${n >= 0 ? '+' : '−'}${Math.abs(n).toFixed(d)}`;
    const tick = n => Math.round(n * 20) / 20;
    const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

    function el(tag, cls, ...kids) {
        const node = document.createElement(tag);
        if (cls) node.className = cls;
        kids.flat().forEach(k => node.append(k instanceof Node ? k : document.createTextNode(String(k))));
        return node;
    }
    const svgNS = 'http://www.w3.org/2000/svg';
    function svg(tag, attrs) {
        const node = document.createElementNS(svgNS, tag);
        Object.entries(attrs).forEach(([k, v]) => node.setAttribute(k, v));
        return node;
    }
    function restartAnimation(node, cls) {
        node.classList.remove(cls);
        void node.offsetWidth;           // reflow so the same animation can play again
        node.classList.add(cls);
    }

    // Numbers count to their new value and flash the direction of the change.
    function setNum(node, value, format, flash = true) {
        const prev = node._v;
        node._v = value;
        if (prev === undefined || reducedMotion || prev === value) {
            node.textContent = format(value);
            return;
        }
        if (flash) restartAnimation(node, value > prev ? 'up-flash' : 'down-flash');
        const start = performance.now(), from = prev;
        cancelAnimationFrame(node._raf);
        const stepNum = now => {
            const k = Math.min(1, (now - start) / 450);
            const e = 1 - (1 - k) ** 3;
            node.textContent = format(from + (value - from) * e);
            if (k < 1) node._raf = requestAnimationFrame(stepNum);
        };
        node._raf = requestAnimationFrame(stepNum);
    }

    // ── simulated market ────────────────────────────────────────────────
    const STOCKS = [
        ['RELIANCE', 1412], ['HDFCBANK', 1975], ['ICICIBANK', 1388], ['INFY', 1518], ['TCS', 3103], ['BHARTIARTL', 1903],
        ['SBIN', 843], ['LT', 3621], ['ITC', 413], ['AXISBANK', 1176], ['KOTAKBANK', 2102], ['M&M', 3240], ['MARUTI', 12450],
        ['SUNPHARMA', 1688], ['TITAN', 3480], ['ULTRACEMCO', 11950], ['BAJFINANCE', 9010], ['ONGC', 244], ['NTPC', 341],
        ['TATASTEEL', 158], ['ADANIENT', 2410], ['CIPLA', 1505], ['DRREDDY', 1268], ['EICHERMOT', 6810], ['BSE', 2420],
        ['ASIANPAINT', 2390], ['HINDALCO', 702], ['JSWSTEEL', 1049], ['POWERGRID', 289], ['COALINDIA', 382],
    ].map(([s, p], i) => ({ s, close: p, price: p * (1 + rnd(-0.012, 0.012)), vol: rnd(0.6, 1.6), flow: 0, oi: rnd(-2, 2),
                           y0: 24 + ((i * 97) % 312) }));
    const INDICES = [['NIFTY 50', 25012], ['BANKNIFTY', 56120], ['SENSEX', 82214], ['FINNIFTY', 26488]]
        .map(([s, p]) => ({ s, close: p, price: p * (1 + rnd(-0.004, 0.004)) }));
    let marketBias = rnd(-0.6, 0.6);

    function step() {
        marketBias = marketBias * 0.97 + rnd(-0.12, 0.12);
        STOCKS.forEach(x => {
            const r = (marketBias * 0.0006) + rnd(-0.0018, 0.0018) * x.vol;
            x.price = Math.max(1, x.price * (1 + r));
            x.flow = x.flow * 0.85 + r * 4000 * x.vol;
            x.oi = x.oi * 0.97 + rnd(-0.15, 0.15);
        });
        INDICES.forEach(x => { x.price *= 1 + marketBias * 0.0003 + rnd(-0.0006, 0.0006); });
    }
    const chg = x => (x.price / x.close - 1) * 100;
    const score = x => Math.round(clamp(x.flow / 4, -99, 99));
    function buildup(x) {
        // follows the flow (what ranks a name), so an inflow leader never reads "short build-up"
        const up = x.flow > 0, oiUp = x.oi >= 0;
        return up ? (oiUp ? 'LONG BUILD-UP' : 'SHORT COVERING') : (oiUp ? 'SHORT BUILD-UP' : 'LONG UNWINDING');
    }

    // ── clock, session ─────────────────────────────────────────────────
    function istParts() {
        const f = new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Kolkata', weekday: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
        return Object.fromEntries(f.formatToParts(new Date()).map(x => [x.type, x.value]));
    }
    function marketOpen() {
        const p = istParts();
        const mins = Number(p.hour) * 60 + Number(p.minute);
        return !['Sat', 'Sun'].includes(p.weekday) && mins >= 555 && mins < 930;
    }
    // Outside market hours the preview replays a session: its clock runs from
    // 09:20 to 15:15, a minute every 5 seconds, so no signal is stamped 17:41.
    const replayStart = Date.now();
    function sessionMinutes() {
        if (marketOpen()) {
            const p = istParts();
            return Number(p.hour) * 60 + Number(p.minute);
        }
        return 560 + Math.floor((Date.now() - replayStart) / 5000 + 45) % 355;
    }
    const hhmm = m => `${String(Math.floor(m / 60)).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`;
    function updateClock() {
        const p = istParts();
        const open = marketOpen();
        $('hud-clock').textContent = `${p.hour}:${p.minute}:${p.second}`;
        $('hud-market').textContent = open ? 'MARKET OPEN' : 'MARKET CLOSED · SESSION REPLAY';
        $('hud-dot').classList.toggle('open', open);
    }

    // ── ticker tape ────────────────────────────────────────────────────
    function renderTape() {
        const items = [...INDICES, ...STOCKS.slice(0, 12)].map(x => {
            const c = chg(x);
            return el('span', 'tape-item', el('b', '', x.s), fmt(x.price), el('span', c >= 0 ? 'up' : 'down', `${c >= 0 ? '▲' : '▼'} ${Math.abs(c).toFixed(2)}%`));
        });
        $('tape-track').replaceChildren(...items, ...items.map(n => n.cloneNode(true)));
    }

    // ── Market Flow: strands ease toward their rank; particles ride them ─
    const flowSvg = $('mf-svg');
    let selected = null, hovered = null;
    const strands = STOCKS.map(x => {
        const path = svg('path', { class: 'strand' });
        $('mf-strands').append(path);
        const dots = [];
        const count = phone ? 1 : 2;
        for (let i = 0; i < count; i++) {
            const dot = svg('circle', { r: '2', class: 'flow-dot' });
            $('mf-dots').append(dot);
            dots.push({ node: dot, p: Math.random() });
        }
        return { x, path, dots, y1: 180, target: 180, side: 'in', hot: false };
    });
    const bez = (p0, p1, p2, p3, t) => {
        const u = 1 - t;
        return u * u * u * p0 + 3 * u * u * t * p1 + 3 * u * t * t * p2 + t * t * t * p3;
    };
    function pointOn(st, p) {
        // path = M 6 y0  C 120 y0, 170 180, 250 180  C 340 180, 430 y1, 594 y1
        if (p < 0.5) {
            const t = p * 2;
            return [bez(6, 120, 170, 250, t), bez(st.x.y0, st.x.y0, 180, 180, t)];
        }
        const t = (p - 0.5) * 2;
        return [bez(250, 340, 430, 594, t), bez(180, 180, st.y1, st.y1, t)];
    }
    function setFlowTargets() {
        const sorted = [...STOCKS].sort((a, b) => b.flow - a.flow);
        const inflow = sorted.filter(x => x.flow > 0);
        const outflow = sorted.filter(x => x.flow <= 0).reverse();
        const top = new Set([...inflow.slice(0, 3), ...outflow.slice(0, 3)].map(x => x.s));
        strands.forEach(st => {
            const i = inflow.indexOf(st.x), o = outflow.indexOf(st.x);
            st.side = st.x.flow > 0 ? 'in' : 'out';
            st.target = i >= 0 && i < 6 ? 34 + i * 22 : o >= 0 && o < 6 ? 216 + o * 22 : 180 - clamp(st.x.flow * 1.4, -150, 150);
            st.hot = top.has(st.x.s);
        });
        return { inflow, outflow };
    }
    function drawFlow(dt) {
        const focus = hovered || null;
        flowSvg.classList.toggle('focus', Boolean(focus));
        strands.forEach(st => {
            st.y1 += (st.target - st.y1) * (reducedMotion ? 1 : Math.min(1, dt * 3.5));
            const hot = focus ? st.x.s === focus : st.hot || st.x.s === selected;
            st.path.setAttribute('d', `M 6 ${st.x.y0.toFixed(1)} C 120 ${st.x.y0.toFixed(1)} 170 180 250 180 C 340 180 430 ${st.y1.toFixed(1)} 594 ${st.y1.toFixed(1)}`);
            st.path.setAttribute('class', `strand ${st.side}${hot ? ' hot' : ''}`);
            st.dots.forEach((d, i) => {
                const show = !reducedMotion && (hot || i === 0);
                d.node.style.display = show ? '' : 'none';
                if (!show) return;
                d.p = (d.p + dt * (0.12 + Math.min(Math.abs(st.x.flow), 80) / 260)) % 1;
                const [cx, cy] = pointOn(st, d.p);
                d.node.setAttribute('cx', cx.toFixed(1));
                d.node.setAttribute('cy', cy.toFixed(1));
                d.node.setAttribute('class', `flow-dot ${st.side}${hot ? ' hot' : ''}`);
                d.node.setAttribute('r', hot ? '2.6' : '1.8');
            });
        });
    }

    let prevScores = {};
    function renderFlowData() {
        const { inflow, outflow } = setFlowTargets();
        const adv = STOCKS.filter(x => chg(x) >= 0).length;
        const net = STOCKS.reduce((t, x) => t + x.flow, 0) / 40;
        setNum($('mf-adv'), adv, v => String(Math.round(v)));
        setNum($('mf-dec'), STOCKS.length - adv, v => String(Math.round(v)));
        $('mf-bias').style.width = `${(adv / STOCKS.length * 100).toFixed(0)}%`;
        const netEl = $('mf-net');
        setNum(netEl, net, v => `${v >= 0 ? '+' : '−'}₹${Math.abs(v).toFixed(1)}cr`);
        netEl.classList.toggle('c-green', net >= 0);
        netEl.classList.toggle('c-red', net < 0);
        $('mf-in-tag').textContent = `▲ INFLOW ${inflow.length}`;
        $('mf-out-tag').textContent = `▼ OUTFLOW ${outflow.length}`;

        const item = x => {
            const sc = score(x);
            const scoreEl = el('span', x.flow > 0 ? 'c-green' : 'c-red', `${sc > 0 ? '+' : ''}${sc}`);
            if (prevScores[x.s] !== undefined && prevScores[x.s] !== sc) scoreEl.classList.add(sc > prevScores[x.s] ? 'up-flash' : 'down-flash');
            const b = el('button', '', el('span', '', x.s, el('small', '', `${sgn(chg(x))}% · ${buildup(x).toLowerCase()}`)), scoreEl);
            b.type = 'button';
            b.setAttribute('aria-pressed', String(selected === x.s));
            b.addEventListener('click', () => { selected = x.s; renderFlowData(); });
            b.addEventListener('pointerenter', () => { hovered = x.s; });
            b.addEventListener('pointerleave', () => { hovered = null; });
            b.addEventListener('focus', () => { hovered = x.s; });
            b.addEventListener('blur', () => { hovered = null; });
            return el('li', '', b);
        };
        $('mf-long').replaceChildren(...inflow.slice(0, 5).map(item));
        $('mf-short').replaceChildren(...outflow.slice(0, 5).map(item));
        prevScores = Object.fromEntries(STOCKS.map(x => [x.s, score(x)]));
        if (!selected || !STOCKS.some(x => x.s === selected)) selected = (inflow[0] || outflow[0]).s;
        renderPick(STOCKS.find(x => x.s === selected));
    }
    function renderPick(x) {
        const long = x.flow > 0;
        const atr = x.price * 0.011 * x.vol;
        const entry = tick(x.price + (long ? 0.1 : -0.1) * atr);
        const sl = tick(long ? entry - atr : entry + atr);
        const t1 = tick(long ? entry + 1.5 * atr : entry - 1.5 * atr);
        const t2 = tick(long ? entry + 3 * atr : entry - 3 * atr);
        const kv = (k, v, cls) => el('span', '', `${k} `, el('b', cls || '', v));
        $('mf-pick').replaceChildren(
            el('div', 'pick-head', el('span', `side ${long ? 'long' : 'short'}`, long ? 'LONG' : 'SHORT'), x.s,
                el('span', long ? 'c-green' : 'c-red', `${sgn(chg(x))}%`)),
            el('div', '', `FLOW ${score(x) > 0 ? '+' : ''}${score(x)} · ${buildup(x)} · ATR ${(atr / x.price * 100).toFixed(1)}%`),
            el('div', 'pick-grid',
                kv('PLAN', `${long ? 'BUY' : 'SELL'} ${fmt(entry)}`), kv('SL', fmt(sl)),
                kv('T1', fmt(t1), 't'), kv('T2', fmt(t2), 't')),
        );
    }

    // ── Day Trade board: signal cards + the selected signal's setup chart ──
    // Each simulated signal carries 5-minute bars shaped so its setup's rule
    // really holds (opening range, previous-day high, squeeze box, VWAP), in
    // units of its risk; after the signal the bars follow the live price.
    const SETUPS = ['ORB', 'PD_BREAK', 'SQUEEZE', 'VWAP_RECLAIM'];
    const SETUP_INFO = {
        ORB: { label: 'Opening-range breakout', vol: 1.3, rule: 'close beyond the 09:15–09:30 range' },
        PD_BREAK: { label: 'Previous-day break', vol: 1.3, rule: 'close beyond the previous session’s high / low' },
        SQUEEZE: { label: 'Squeeze breakout', vol: 1.5, rule: 'six tight bars, then a close out of the box' },
        VWAP_RECLAIM: { label: 'VWAP reclaim', vol: 1.2, rule: 'two closes on the wrong side of VWAP, then back across' },
    };
    const STATUS = { ACTIVE: ['active', 'ACTIVE'], T1: ['t1', 'TARGET 1'], T2: ['t2', 'TARGET 2'], STOP: ['stop', 'STOPPED'] };
    let signals = [];
    let signalSeq = 0;
    let chosen = null;            // signal id picked by the visitor (null = follow the newest)
    const cards = new Map();

    // bars in risk units (signal close = 0, long orientation)
    function shapeBars(setup, n) {
        const bars = [];
        let c = rnd(-1.2, -0.6);
        const push = (o, cl, wick = 0.12) => bars.push({ o, c: cl, h: Math.max(o, cl) + rnd(0.02, wick), l: Math.min(o, cl) - rnd(0.02, wick), v: rnd(0.6, 1.3) });
        const walk = (lo, hi) => { const o = c; c = clamp(c + rnd(-0.22, 0.22), lo, hi); push(o, c); };
        const meta = {};
        if (setup === 'ORB') {
            for (let i = 0; i < 3; i++) { const o = c; c = clamp(c + rnd(-0.3, 0.3), -0.95, -0.35); push(o, c, 0.18); bars[i].v = rnd(1.5, 2.2); }
            meta.orh = Math.max(...bars.map(b => b.h)); meta.orl = Math.min(...bars.map(b => b.l));
            while (bars.length < n - 1) {
                walk(meta.orl + 0.05, meta.orh - 0.1);
                const b = bars[bars.length - 1];
                b.h = Math.min(b.h, meta.orh - 0.02); b.l = Math.max(b.l, meta.orl);
            }
        } else if (setup === 'PD_BREAK') {
            meta.pdh = -0.16;
            while (bars.length < n - 1) { walk(-1.5, -0.3); const b = bars[bars.length - 1]; b.h = Math.min(b.h, meta.pdh - 0.02); }
        } else if (setup === 'SQUEEZE') {
            while (bars.length < n - 7) walk(-1.8, -0.4);
            c = rnd(-0.5, -0.4);
            for (let i = 0; i < 6; i++) { const o = c; c = clamp(c + rnd(-0.08, 0.08), -0.55, -0.32); push(o, c, 0.04); bars[bars.length - 1].v = rnd(0.5, 0.9); }
            const box = bars.slice(-6);
            meta.box = { from: bars.length - 6, to: bars.length - 1, top: Math.max(...box.map(b => b.h)), bottom: Math.min(...box.map(b => b.l)) };
        } else {                                   // VWAP reclaim: drift lower through VWAP, two closes under it
            c = rnd(-0.1, 0.2);
            while (bars.length < n - 3) walk(-0.6, 0.3);
            for (let i = 0; i < 2; i++) { const o = c; c = rnd(-0.75, -0.55); push(o, c); }
        }
        // signal bar: a strong close on 2-2.8x the prior 10 bars' volume
        const recent = bars.slice(-10);
        const prior = recent.reduce((a, b) => a + b.v, 0) / recent.length;
        push(c, setup === 'VWAP_RECLAIM' ? 0.25 : 0, 0.08);
        const sb = bars[bars.length - 1];
        sb.o = bars[bars.length - 2].c + rnd(-0.05, 0.05);
        sb.l = Math.min(sb.l, sb.o - 0.03);
        sb.v = prior * rnd(2.0, 2.8);
        if (setup === 'VWAP_RECLAIM') {
            // VWAP from the bars: the two bars before close under it, the signal closes back above
            let pv = 0, vv = 0;
            const vwap = bars.map(b => { pv += (b.h + b.l + b.c) / 3 * b.v; vv += b.v; return pv / vv; });
            sb.c = Math.max(sb.c, vwap[vwap.length - 1] + 0.12);
            sb.h = sb.c + 0.06;
            for (const k of [bars.length - 3, bars.length - 2]) {
                const lim = vwap[k] - 0.08;
                if (bars[k].c > lim) { bars[k].c = lim; bars[k].l = Math.min(bars[k].l, lim - 0.05); bars[k].o = Math.max(bars[k].o, lim); bars[k].h = Math.max(bars[k].h, bars[k].o); }
            }
        }
        // signal close = 0
        const shift = sb.c;
        for (const b of bars) { b.o -= shift; b.c -= shift; b.h -= shift; b.l -= shift; }
        for (const k of ['orh', 'orl', 'pdh']) if (meta[k] !== undefined) meta[k] -= shift;
        if (meta.box) { meta.box.top -= shift; meta.box.bottom -= shift; }
        meta.volX = sb.v / prior;
        return { bars, meta };
    }

    function newSignal(minutesAgo) {
        const taken = new Set(signals.map(s => s.s));        // one open signal per stock, as on the board
        const x = pick(STOCKS.filter(st => !taken.has(st.s)).length ? STOCKS.filter(st => !taken.has(st.s)) : STOCKS);
        const long = x.flow > 0 ? Math.random() > 0.25 : Math.random() > 0.75;
        const atr = x.price * 0.008 * x.vol;
        const entry = tick(x.price);
        const side = long ? 1 : -1;
        let minute = Math.max(570, sessionMinutes() - minutesAgo);
        minute -= minute % 5;
        let setup = pick(SETUPS);
        if (setup === 'ORB' && minute > 705) setup = pick(['PD_BREAK', 'SQUEEZE', 'VWAP_RECLAIM']);   // ORB fires early
        const n = setup === 'ORB' ? Math.max(5, (minute - 555) / 5) : 22;
        const { bars, meta } = shapeBars(setup, n);
        // risk units -> prices (shorts mirror the shape)
        const P = u => tick(entry + side * u * atr);
        const toPrice = b => long ? { o: P(b.o), h: P(b.h), l: P(b.l), c: P(b.c), v: b.v }
            : { o: P(b.o), h: P(b.l), l: P(b.h), c: P(b.c), v: b.v };
        const level = meta.orh !== undefined ? { name: long ? 'ORH' : 'ORL', price: P(meta.orh), lo: P(meta.orl) }
            : meta.pdh !== undefined ? { name: long ? 'PDH' : 'PDL', price: P(meta.pdh) } : null;
        const box = meta.box ? { from: meta.box.from, to: meta.box.to, a: P(meta.box.top), b: P(meta.box.bottom) } : null;
        return {
            id: ++signalSeq, s: x.s, setup, long, entry, sl: tick(long ? entry - atr : entry + atr),
            t1: tick(long ? entry + 1.5 * atr : entry - 1.5 * atr), t2: tick(long ? entry + 3 * atr : entry - 3 * atr),
            last: entry, status: 'ACTIVE', time: hhmm(minute), bars: bars.map(toPrice), si: bars.length - 1,
            startMin: minute - 5 * bars.length, level, box, volX: meta.volX, steps: 0, t1At: null, exitAt: null, exitPx: null,
        };
    }

    function stepSignals() {
        signals.forEach(sig => {
            if (sig.status === 'T2' || sig.status === 'STOP') return;
            sig.last = tick(sig.last + (sig.long ? 1 : -1) * Math.abs(sig.t1 - sig.entry) * rnd(-0.18, 0.28));
            // the forming bar follows the price; a new bar every few ticks
            const lastBar = sig.bars[sig.bars.length - 1];
            if (sig.steps++ % 3 === 0 && sig.bars.length < sig.si + 26) {
                const prev = lastBar.c;
                sig.bars.push({ o: prev, h: Math.max(prev, sig.last), l: Math.min(prev, sig.last), c: sig.last, v: rnd(0.6, 1.4) * sig.bars[sig.si].v / 2.3 });
            } else if (sig.bars.length - 1 > sig.si) {
                lastBar.c = sig.last; lastBar.h = Math.max(lastBar.h, sig.last); lastBar.l = Math.min(lastBar.l, sig.last);
            }
            const up = sig.long ? sig.last - sig.entry : sig.entry - sig.last;
            const at = sig.bars.length - 1;
            if (up <= -Math.abs(sig.entry - sig.sl)) { sig.status = 'STOP'; sig.exitAt = at; sig.exitPx = sig.t1At !== null ? sig.entry : sig.sl; }
            else if (up >= Math.abs(sig.t2 - sig.entry)) { sig.status = 'T2'; sig.exitAt = at; sig.exitPx = sig.t2; if (sig.t1At === null) sig.t1At = at; }
            else if (up >= Math.abs(sig.t1 - sig.entry)) { if (sig.status !== 'T1') sig.t1At = at; sig.status = 'T1'; }
        });
        if (Math.random() < 0.18) {
            signals.unshift(newSignal(0));
            signals = signals.slice(0, 6);
        }
    }

    function selectedSignal() {
        return signals.find(s => s.id === chosen) || signals[0];
    }

    function drawSetup(sig) {
        const canvas = $('dt-chart');
        const rect = canvas.getBoundingClientRect();
        if (!rect.width || !sig) return;
        const dpr = window.devicePixelRatio || 1;
        canvas.width = rect.width * dpr; canvas.height = rect.height * dpr;
        const ctx = canvas.getContext('2d');
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        const W = rect.width, H = rect.height, right = W - 70, axisH = 14;
        const priceH = Math.round((H - axisH) * 0.76), volTop = priceH + 6, volH = H - axisH - volTop;
        ctx.clearRect(0, 0, W, H);
        const bars = sig.bars, si = sig.si;
        const lv = [sig.sl, sig.t1, sig.t2, sig.entry, sig.level && sig.level.price, sig.level && sig.level.lo, sig.box && sig.box.a, sig.box && sig.box.b]
            .filter(v => v !== null && v !== undefined);
        const hi = Math.max(...bars.map(b => b.h), ...lv), lo = Math.min(...bars.map(b => b.l), ...lv);
        const pad = (hi - lo) * 0.05 || 1;
        const y = v => 6 + (hi + pad - v) / (hi - lo + 2 * pad) * (priceH - 12);
        const slot = (right - 6) / Math.max(bars.length + 2, si + 14);
        const x = i => 6 + slot * (i + 0.5);
        const css = getComputedStyle(document.documentElement);
        const C = k => css.getPropertyValue(k).trim();
        const G = C('--g'), R = C('--r'), CY = C('--c'), A = C('--a'), DIM = C('--mute');
        const mono = s => `${s}px ${C('--mono')}`;
        const seg = (v, x0, x1, color, dash, w = 1) => {
            ctx.strokeStyle = color; ctx.setLineDash(dash); ctx.lineWidth = w;
            ctx.beginPath(); ctx.moveTo(x0, y(v)); ctx.lineTo(x1, y(v)); ctx.stroke(); ctx.setLineDash([]);
        };
        const placed = [];                                     // right-axis labels step aside instead of overlapping
        const tag = (v, color, text) => {
            let ly = clamp(y(v) + 3, 9, priceH);
            for (let k = 0; k < 6 && placed.some(p => Math.abs(p - ly) < 10); k++) ly += ly > priceH / 2 ? -10 : 10;
            placed.push(ly);
            ctx.fillStyle = color; ctx.font = mono(9); ctx.textAlign = 'left';
            ctx.fillText(`${text} ${fmt(v)}`, right + 4, ly);
        };

        // time labels, half-hourly (hourly when narrow)
        ctx.fillStyle = DIM; ctx.font = mono(9); ctx.textAlign = 'center';
        const every = slot * 6 >= 40 ? 30 : 60;
        bars.forEach((b, i) => { const m = sig.startMin + 5 * i; if ((m - 555) % every === 0) ctx.fillText(hhmm(m), x(i), H - 3); });

        // the setup's geometry
        ctx.fillStyle = 'rgba(255,209,102,.10)'; ctx.fillRect(x(si) - slot / 2, 0, slot, H - axisH);
        if (sig.setup === 'ORB' && sig.level) {
            const yt = Math.min(y(sig.level.price), y(sig.level.lo));
            ctx.fillStyle = 'rgba(255,209,102,.09)';
            ctx.fillRect(x(0) - slot / 2, yt, x(2) - x(0) + slot, Math.abs(y(sig.level.lo) - y(sig.level.price)));
            seg(sig.level.price, x(0) - slot / 2, x(si) + slot / 2, A, [4, 3]);
            seg(sig.level.lo, x(0) - slot / 2, x(si) + slot / 2, 'rgba(255,209,102,.5)', [4, 3]);
            ctx.fillStyle = A; ctx.font = mono(9); ctx.textAlign = 'left';
            ctx.fillText('OPENING RANGE', x(0) - slot / 2, clamp(yt - 4, 9, priceH));
        } else if (sig.setup === 'PD_BREAK' && sig.level) {
            seg(sig.level.price, 6, right, 'rgba(190,205,215,.85)', [6, 3], 1.4);
            tag(sig.level.price, '#bfcdd7', sig.level.name);
        } else if (sig.box) {
            const bx = x(sig.box.from) - slot / 2, bw = x(sig.box.to) - x(sig.box.from) + slot;
            const top = Math.min(y(sig.box.a), y(sig.box.b)), h = Math.abs(y(sig.box.a) - y(sig.box.b));
            ctx.fillStyle = 'rgba(56,214,255,.10)'; ctx.fillRect(bx, top, bw, h);
            ctx.strokeStyle = CY; ctx.setLineDash([3, 2]); ctx.strokeRect(bx, top, bw, h); ctx.setLineDash([]);
            ctx.fillStyle = CY; ctx.font = mono(9); ctx.textAlign = 'left';
            ctx.fillText('6-BAR BOX', bx, clamp(top + h + 11, 9, priceH));
        }
        // plan zones from the fill (the bar after the signal)
        const end = sig.exitAt !== null ? sig.exitAt : bars.length - 1;
        const from = x(si + 1) - slot / 2, to = x(Math.max(end, si + 4)) + slot / 2;
        ctx.fillStyle = 'rgba(255,90,110,.10)'; ctx.fillRect(from, Math.min(y(sig.entry), y(sig.sl)), to - from, Math.abs(y(sig.sl) - y(sig.entry)));
        ctx.fillStyle = 'rgba(91,227,160,.08)'; ctx.fillRect(from, Math.min(y(sig.entry), y(sig.t2)), to - from, Math.abs(y(sig.t2) - y(sig.entry)));

        // candles + VWAP
        const cw = Math.max(1.5, slot * 0.6);
        bars.forEach((b, i) => {
            ctx.strokeStyle = ctx.fillStyle = b.c >= b.o ? '#25c996' : '#f05d71';
            ctx.lineWidth = 1;
            ctx.beginPath(); ctx.moveTo(x(i), y(b.h)); ctx.lineTo(x(i), y(b.l)); ctx.stroke();
            ctx.fillRect(x(i) - cw / 2, Math.min(y(b.o), y(b.c)), cw, Math.max(1, Math.abs(y(b.o) - y(b.c))));
        });
        let pv = 0, vv = 0;
        ctx.strokeStyle = CY; ctx.lineWidth = sig.setup === 'VWAP_RECLAIM' ? 2 : 1.1; ctx.beginPath();
        bars.forEach((b, i) => {
            pv += (b.h + b.l + b.c) / 3 * b.v; vv += b.v;
            if (i) ctx.lineTo(x(i), y(pv / vv)); else ctx.moveTo(x(i), y(pv / vv));
        });
        ctx.stroke();

        // plan lines (stop moves to breakeven after T1), signal, T1 and exit
        const beAt = sig.t1At !== null ? x(sig.t1At) + slot / 2 : to;
        seg(sig.entry, from, to, CY, [], 1.3);
        seg(sig.sl, from, beAt, R, [4, 3], 1.3);
        if (sig.t1At !== null) seg(sig.entry, beAt, to, 'rgba(255,90,110,.7)', [2, 3], 1.3);
        seg(sig.t1, from, to, G, [4, 3]); seg(sig.t2, from, to, G, [4, 3]);
        tag(sig.entry, CY, 'IN'); tag(sig.sl, R, 'SL'); tag(sig.t1, G, 'T1'); tag(sig.t2, G, 'T2');
        if (sig.setup === 'ORB' && sig.level) tag(sig.level.price, A, sig.level.name);
        const sb = bars[si], py = sig.long ? y(sb.l) + 9 : y(sb.h) - 9;
        ctx.fillStyle = sig.long ? G : R;
        ctx.beginPath();
        ctx.moveTo(x(si), sig.long ? py - 6 : py + 6); ctx.lineTo(x(si) - 5, sig.long ? py + 3 : py - 3); ctx.lineTo(x(si) + 5, sig.long ? py + 3 : py - 3);
        ctx.fill();
        ctx.fillStyle = A; ctx.font = mono(9); ctx.textAlign = 'center';
        ctx.fillText(`SIGNAL ${sig.time}`, clamp(x(si), 40, right - 40), clamp(sig.long ? py + 14 : py - 9, 9, priceH));
        if (sig.t1At !== null) { ctx.fillStyle = G; ctx.beginPath(); ctx.arc(x(sig.t1At), y(sig.t1), 3.2, 0, Math.PI * 2); ctx.fill(); }
        if (sig.exitAt !== null) {
            ctx.strokeStyle = sig.status === 'STOP' ? R : G; ctx.lineWidth = 2;
            ctx.beginPath(); ctx.arc(x(sig.exitAt), y(sig.exitPx), 4.5, 0, Math.PI * 2); ctx.stroke();
        }

        // volume against the prior 10 bars' average
        const vmax = Math.max(...bars.map(b => b.v));
        const vy = v => volTop + volH - v / vmax * (volH - 2);
        bars.forEach((b, i) => {
            ctx.fillStyle = i === si ? A : b.c >= b.o ? 'rgba(37,201,150,.45)' : 'rgba(240,93,113,.45)';
            ctx.fillRect(x(i) - cw / 2, vy(b.v), cw, volTop + volH - vy(b.v));
        });
        const prior = bars.slice(Math.max(0, si - 10), si);
        const avg = prior.reduce((a, b) => a + b.v, 0) / prior.length;
        ctx.strokeStyle = 'rgba(215,227,234,.7)'; ctx.setLineDash([3, 2]); ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(x(si - prior.length) - slot / 2, vy(avg)); ctx.lineTo(x(si) + slot / 2, vy(avg)); ctx.stroke(); ctx.setLineDash([]);
        ctx.fillStyle = A; ctx.font = mono(9); ctx.textAlign = 'left';
        ctx.fillText(`${(sb.v / avg).toFixed(1)}× avg vol`, right + 4, clamp(vy(sb.v) + 3, volTop + 9, H - axisH));
    }

    function renderWhy(sig) {
        const info = SETUP_INFO[sig.setup];
        const risk = Math.abs(sig.entry - sig.sl);
        const now = (sig.long ? sig.last - sig.entry : sig.entry - sig.last) / risk;
        const kv = (k, v, cls) => el('span', '', `${k} `, el('b', cls || '', v));
        $('dt-why').replaceChildren(
            el('div', 'why-head', el('span', `side ${sig.long ? 'long' : 'short'}`, sig.long ? 'LONG' : 'SHORT'),
                el('b', '', sig.s), el('span', 'setup', info.label), el('span', 'c-dim', `@ ${sig.time} close`)),
            el('div', 'why-rule', `Rule: ${info.rule} on ≥ ${info.vol}× volume. This bar traded `,
                el('b', 'c-amber', `${sig.volX.toFixed(1)}×`), ' its 10-bar average.'),
            el('div', 'pick-grid',
                kv('ENTRY', fmt(sig.entry)), kv('SL', fmt(sig.sl), 'c-red'),
                kv('T1 · 1.5R', fmt(sig.t1), 't'), kv('T2 · 3R', fmt(sig.t2), 't'),
                kv('NOW', `${sgn(now)}R`, now >= 0 ? 'c-green' : 'c-red'), kv('STATUS', STATUS[sig.status][1])),
        );
    }

    function renderDay() {
        setNum($('dt-count'), signals.length, v => String(Math.round(v)), false);
        const list = $('dt-list');
        const keep = new Set(signals.map(s => s.id));
        if (chosen !== null && !keep.has(chosen)) chosen = null;
        const sel = selectedSignal();
        cards.forEach((card, id) => { if (!keep.has(id)) { card.node.remove(); cards.delete(id); } });
        signals.forEach((sig, index) => {
            let card = cards.get(sig.id);
            if (!card) {
                const ltp = el('b', '');
                const status = el('span', 'status');
                const node = el('div', 'dt-card new',
                    el('div', '', el('span', 'sym', sig.s), ' ', el('span', `side ${sig.long ? 'long' : 'short'}`, sig.long ? 'LONG' : 'SHORT'),
                        ' ', el('span', 'setup', SETUP_INFO[sig.setup].label), ` · ${sig.time}`),
                    status,
                    el('div', 'lv', el('span', '', 'ENTRY ', el('b', '', fmt(sig.entry))), el('span', '', 'SL ', el('b', '', fmt(sig.sl))),
                        el('span', '', 'T1 ', el('b', 'c-amber', fmt(sig.t1))), el('span', '', 'LTP ', ltp)));
                node.tabIndex = 0;
                node.setAttribute('role', 'button');
                node.setAttribute('aria-label', `Chart the ${sig.s} ${SETUP_INFO[sig.setup].label} setup`);
                const choose = () => { chosen = sig.id; renderDay(); };
                node.addEventListener('click', choose);
                node.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); choose(); } });
                card = { node, ltp, status, shown: null };
                cards.set(sig.id, card);
            }
            card.node.classList.toggle('sel', sig === sel);
            card.node.setAttribute('aria-pressed', String(sig === sel));
            if (list.children[index] !== card.node) list.insertBefore(card.node, list.children[index] || null);
            setNum(card.ltp, sig.last, v => fmt(v));
            if (card.shown !== sig.status) {
                const [cls, label] = STATUS[sig.status];
                card.status.className = `status ${cls}`;
                card.status.textContent = label;
                if (card.shown) restartAnimation(card.node, sig.status === 'STOP' ? 'miss' : 'hit');
                card.shown = sig.status;
            }
        });
        if (sel) { drawSetup(sel); renderWhy(sel); }
    }

    // ── OI Tracker: rows persist, bars ease ─────────────────────────────
    const oiRows = [];
    for (let i = 0; i < 13; i++) {
        const ce = el('span', 'bar ce'), pe = el('span', 'bar pe'), strike = el('span', 'strike');
        const row = el('div', 'oi-row', ce, strike, pe);
        $('oi-chart').append(row);
        oiRows.push({ row, ce, pe, strike });
    }
    function renderOI() {
        const spot = INDICES[0].price;
        const atm = Math.round(spot / 50) * 50;
        const data = [];
        let ceSum = 0, peSum = 0;
        for (let k = 6; k >= -6; k--) {
            const strike = atm + k * 50;
            const round = strike % 500 === 0 ? 22 : 0;
            const ce = Math.max(4, 60 * Math.exp(-(((k - 2.5) / 3.2) ** 2)) * rnd(0.95, 1.05) + round);
            const pe = Math.max(4, 60 * Math.exp(-(((k + 2.5) / 3.2) ** 2)) * rnd(0.95, 1.05) * (1 + marketBias * 0.2) + round);
            ceSum += ce; peSum += pe;
            data.push({ strike, ce, pe, k });
        }
        const ceMax = Math.max(...data.map(d => d.ce)), peMax = Math.max(...data.map(d => d.pe));
        const top = Math.max(ceMax, peMax);
        data.forEach((d, i) => {
            const r = oiRows[i];
            r.row.classList.toggle('atm', d.k === 0);
            r.strike.textContent = String(d.strike);
            r.ce.style.width = `${(d.ce / top * 100).toFixed(1)}%`;
            r.pe.style.width = `${(d.pe / top * 100).toFixed(1)}%`;
            r.ce.classList.toggle('wall', d.ce === ceMax);
            r.pe.classList.toggle('wall', d.pe === peMax);
        });
        const pcr = peSum / ceSum;
        const pain = data.reduce((best, d) => (Math.abs(d.ce - d.pe) < Math.abs(best.ce - best.pe) ? d : best)).strike;
        setNum($('oi-spot'), spot, v => fmt(v));
        setNum($('oi-pcr'), pcr, v => v.toFixed(2));
        $('oi-pain').textContent = String(pain);
        const bias = $('oi-bias');
        const label = pcr > 1.12 ? 'BULLISH' : pcr < 0.88 ? 'BEARISH' : 'NEUTRAL';
        if (bias.textContent !== label) {
            bias.textContent = label;
            bias.className = `pill ${label === 'BULLISH' ? 'pill-green' : label === 'NEUTRAL' ? 'pill-amber' : ''}`;
            bias.style.color = label === 'BEARISH' ? 'var(--r)' : '';
        }
    }

    // ── Agents: cards persist, log grows from the top ───────────────────
    const AGENTS = [
        { name: 'SCANNER', state: 'scanning', color: 'var(--c)' },
        { name: 'VERIFIER', state: 'checking', color: 'var(--g)' },
        { name: 'EXECUTOR', state: 'watching signals', color: 'var(--g)' },
        { name: 'TRACKER', state: 'syncing positions', color: 'var(--a)' },
    ].map(a => {
        const dot = el('i', '');
        dot.style.background = a.color;
        dot.style.boxShadow = `0 0 8px ${a.color}`;
        const st = el('div', 'st', a.state);
        const node = el('div', 'agent', el('b', '', dot, a.name), st);
        $('ag-grid').append(node);
        return { ...a, node, st };
    });
    let scanIdx = 120;
    function agentLine(animate) {
        const s = pick(STOCKS);
        const lines = [
            ['SCANNER', `Analysed ${s.s} (${(scanIdx = scanIdx % 500 + 1)}/500)`],
            ['SCANNER', 'Scan finished · 66 super performers · 154 Stage 2 · 10 RS leaders'],
            ['VERIFIER', `NIFTY OI bias ${$('oi-bias').textContent} · PCR ${$('oi-pcr').textContent} · max pain ${$('oi-pain').textContent}`],
            ['VERIFIER', `Preflight ${s.s}: margin ✓ session ✓ slippage ✓`],
            ['EXECUTOR', signals[0] ? `Signal ${signals[0].s} ${signals[0].setup} ${signals[0].long ? 'LONG' : 'SHORT'} → journaled (auto-trade off)` : 'Waiting for signals'],
            ['TRACKER', `Positions synced · ${Math.floor(rnd(0, 3))} open · day P&L ${sgn(rnd(-3000, 6000), 0)}`],
        ];
        const [who, msg] = pick(lines);
        const t = istParts();
        const line = el('p', animate ? 'new' : '', `${t.hour}:${t.minute}:${t.second} `, el('span', 'who', who), ` ${msg}`);
        const log = $('ag-log');
        log.prepend(line);
        while (log.children.length > 9) log.lastChild.remove();
        const agent = AGENTS.find(a => a.name === who);
        agent.st.textContent = msg.length > 42 ? `${msg.slice(0, 40)}…` : msg;
        if (animate) {
            agent.node.classList.add('changed');
            setTimeout(() => agent.node.classList.remove('changed'), 900);
        }
    }

    // ── Backtest ───────────────────────────────────────────────────────
    const BT = { ema: { edge: 0.18, n: 64 }, vwap: { edge: 0.26, n: 88 }, orb: { edge: 0.12, n: 120 } };
    let btKind = 'ema';
    let btAnim = 0;
    function runBacktest() {
        const cfg = BT[btKind];
        let eq = 100, peak = 100, dd = 0, wins = 0;
        const pts = [eq];
        const rets = [];
        for (let i = 0; i < cfg.n; i++) {
            const win = Math.random() < 0.46 + cfg.edge * 0.4;
            const r = win ? rnd(0.6, 1.9) : -rnd(0.4, 1.1);
            wins += win ? 1 : 0;
            eq *= 1 + r / 100;
            rets.push(r);
            peak = Math.max(peak, eq);
            dd = Math.max(dd, (peak - eq) / peak * 100);
            pts.push(eq);
        }
        const mean = rets.reduce((a, b) => a + b, 0) / rets.length;
        const sd = Math.sqrt(rets.reduce((a, b) => a + (b - mean) ** 2, 0) / rets.length) || 1;
        const lo = Math.min(...pts), hi = Math.max(...pts);
        const xy = pts.map((v, i) => `${(i / (pts.length - 1) * 600).toFixed(1)},${(210 - (v - lo) / (hi - lo || 1) * 200).toFixed(1)}`);
        const line = $('bt-line');
        const myRun = ++btAnim;
        const started = performance.now();
        const duration = reducedMotion ? 0 : 900;
        const draw = now => {
            if (myRun !== btAnim) return;
            const k = duration ? Math.min(1, (now - started) / duration) : 1;
            line.setAttribute('points', xy.slice(0, Math.max(2, Math.ceil(xy.length * k))).join(' '));
            if (k < 1) requestAnimationFrame(draw);
        };
        requestAnimationFrame(draw);
        const ret = pts[pts.length - 1] - 100;
        const retEl = $('bt-ret');
        setNum(retEl, ret, v => `${sgn(v, 1)}%`, false);
        retEl.className = ret >= 0 ? 'c-green' : 'c-red';
        setNum($('bt-win'), wins / cfg.n * 100, v => `${v.toFixed(0)}%`, false);
        setNum($('bt-sharpe'), mean / sd * Math.sqrt(252 / 3), v => v.toFixed(2), false);
        const ddEl = $('bt-dd');
        setNum(ddEl, dd, v => `−${v.toFixed(1)}%`, false);
        ddEl.className = 'c-red';
        setNum($('bt-trades'), cfg.n, v => String(Math.round(v)), false);
    }
    document.querySelectorAll('[data-bt]').forEach(b => b.addEventListener('click', () => {
        btKind = b.dataset.bt;
        document.querySelectorAll('[data-bt]').forEach(x => x.classList.toggle('active', x === b));
        runBacktest();
    }));
    $('bt-run').addEventListener('click', runBacktest);

    // ── tabs: sliding ink, fading views, auto-tour until the visitor acts ─
    const tabs = [...document.querySelectorAll('.term-tabs [role="tab"]')];
    const tabBar = document.querySelector('.term-tabs');
    const ink = $('tab-ink');
    const markOverflow = () => tabBar.classList.toggle('more', tabBar.scrollLeft + tabBar.clientWidth < tabBar.scrollWidth - 4);
    tabBar.addEventListener('scroll', markOverflow, { passive: true });
    let view = 'flow';
    const DEMO_LABEL = { 'market-flow': 'Market Flow', 'day-trade': 'Day Trade', 'oi-tracker': 'OI Tracker', agents: 'Agents', backtest: 'Backtest' };
    function moveInk(tab) {
        ink.style.width = `${tab.offsetWidth}px`;
        ink.style.transform = `translateX(${tab.offsetLeft}px)`;
    }
    function select(tab, focus) {
        tabs.forEach(t => {
            const on = t === tab;
            t.setAttribute('aria-selected', String(on));
            t.tabIndex = on ? 0 : -1;
            const panel = $(t.getAttribute('aria-controls'));
            panel.hidden = !on;
            if (on && !reducedMotion) restartAnimation(panel, 'enter');
        });
        view = tab.dataset.view;
        moveInk(tab);
        const open = $('term-open');
        open.dataset.startDemo = tab.dataset.demoTab;
        open.textContent = `Open ${view === 'day' ? 'the Day Trade board' : DEMO_LABEL[tab.dataset.demoTab]} in the demo →`;
        if (focus) tab.focus();
        const left = tab.offsetLeft - tabBar.scrollLeft, right = left + tab.offsetWidth;
        if (left < 0 || right > tabBar.clientWidth) tabBar.scrollTo({ left: tab.offsetLeft - 16, behavior: reducedMotion ? 'auto' : 'smooth' });
        renderView();
        if (view === 'bt' && !$('bt-line').getAttribute('points')) runBacktest();
    }

    const TOUR_MS = 7000;
    let touring = !reducedMotion;
    let tourTimer = null;
    function stopTour() {
        touring = false;
        clearTimeout(tourTimer);
        ink.classList.remove('touring');
    }
    function scheduleTour() {
        clearTimeout(tourTimer);
        if (!touring || !visible) { ink.classList.remove('touring'); return; }
        ink.style.setProperty('--tour-ms', `${TOUR_MS}ms`);
        restartAnimation(ink, 'touring');
        tourTimer = setTimeout(() => {
            const i = tabs.findIndex(t => t.getAttribute('aria-selected') === 'true');
            select(tabs[(i + 1) % tabs.length], false);
            scheduleTour();
        }, TOUR_MS);
    }
    ['pointerdown', 'keydown', 'wheel', 'touchstart'].forEach(type =>
        $('term').addEventListener(type, stopTour, { passive: true, once: false }));

    tabs.forEach((t, i) => {
        t.addEventListener('click', () => { stopTour(); select(t, false); });
        t.addEventListener('keydown', e => {
            const move = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
            if (move) { e.preventDefault(); select(tabs[(i + move + tabs.length) % tabs.length], true); }
            if (e.key === 'Home') { e.preventDefault(); select(tabs[0], true); }
            if (e.key === 'End') { e.preventDefault(); select(tabs[tabs.length - 1], true); }
        });
    });
    let touchX = null;
    $('term').addEventListener('touchstart', e => { touchX = e.touches[0].clientX; }, { passive: true });
    $('term').addEventListener('touchend', e => {
        if (touchX === null) return;
        const dx = e.changedTouches[0].clientX - touchX;
        touchX = null;
        if (Math.abs(dx) < 60 || e.target.closest('.term-tabs, .oi-chart, button')) return;
        const i = tabs.findIndex(t => t.getAttribute('aria-selected') === 'true');
        select(tabs[(i + (dx < 0 ? 1 : -1) + tabs.length) % tabs.length], false);
    }, { passive: true });

    function renderView() {
        if (view === 'flow') renderFlowData();
        else if (view === 'day') renderDay();
        else if (view === 'oi') renderOI();
    }

    // ── pointer spotlight on the terminal and the feature cards ─────────
    if (!reducedMotion && window.matchMedia('(hover: hover)').matches) {
        document.querySelectorAll('.spot').forEach(node => {
            let pending = false, ev = null;
            node.addEventListener('pointermove', e => {
                ev = e;
                if (pending) return;
                pending = true;
                requestAnimationFrame(() => {
                    pending = false;
                    const r = node.getBoundingClientRect();
                    node.style.setProperty('--mx', `${ev.clientX - r.left}px`);
                    node.style.setProperty('--my', `${ev.clientY - r.top}px`);
                });
            });
        });
    }

    // ── loops: data each second, motion each frame, only while visible ──
    let visible = true;
    function dataTick() {
        updateClock();
        if (!visible || document.hidden) return;
        step();
        stepSignals();
        if (Math.random() < 0.6) agentLine(true);
        renderTape();
        renderView();
    }
    let last = performance.now();
    function frame(now) {
        const dt = Math.min(0.1, (now - last) / 1000);
        last = now;
        if (visible && !document.hidden && view === 'flow') drawFlow(dt);
        requestAnimationFrame(frame);
    }
    if ('IntersectionObserver' in window) {
        new IntersectionObserver(entries => {
            const was = visible;
            visible = entries[0].isIntersecting;
            if (visible && !was) scheduleTour();
            if (!visible) { clearTimeout(tourTimer); ink.classList.remove('touring'); }
        }, { threshold: 0.35 }).observe($('term'));
    }

    // seed: a few signals from earlier in the session, a populated log
    for (let i = 0; i < 40; i++) step();
    signals = [6, 18, 31, 47, 66].map(m => newSignal(m));
    for (let i = 0; i < 12; i++) stepSignals();
    renderOI();
    for (let i = 0; i < 6; i++) agentLine(false);
    updateClock();
    renderTape();
    renderFlowData();
    strands.forEach(st => { st.y1 = st.target; });
    drawFlow(0);
    moveInk(tabs[0]);
    markOverflow();
    window.addEventListener('resize', () => {
        markOverflow();
        moveInk(tabs.find(t => t.getAttribute('aria-selected') === 'true'));
    });
    setInterval(dataTick, reducedMotion ? 4000 : 1000);
    requestAnimationFrame(frame);
    scheduleTour();

    // ── reveal on scroll (content stays visible without JS / on failure) ─
    const reveals = document.querySelectorAll('.reveal');
    const showAll = () => reveals.forEach(r => r.classList.add('shown'));
    if (reducedMotion || !('IntersectionObserver' in window)) {
        showAll();
    } else {
        const io = new IntersectionObserver(entries => entries.forEach(e => {
            if (e.isIntersecting) { e.target.classList.add('shown'); io.unobserve(e.target); }
        }), { rootMargin: '0px 0px -6% 0px', threshold: 0 });
        reveals.forEach(r => io.observe(r));
        setTimeout(showAll, 2500);  // never leave content hidden
    }

    // ── mobile CTA bar: out of the way at the form and while typing ─────
    const cta = $('mobile-cta');
    let ctaBlocked = false;
    if ('IntersectionObserver' in window) {
        new IntersectionObserver(entries => {
            ctaBlocked = entries[0].isIntersecting;
            cta.classList.toggle('hide', ctaBlocked);
        }).observe($('contact'));
    }
    document.addEventListener('focusin', e => { if (e.target.matches('input, textarea, select')) cta.classList.add('hide'); });
    document.addEventListener('focusout', () => { if (!ctaBlocked) cta.classList.remove('hide'); });

    $('year').textContent = String(new Date().getFullYear());

    // ── try the demo ───────────────────────────────────────────────────
    async function startDemo(tab, button) {
        const errors = document.querySelectorAll('[data-demo-error]');
        errors.forEach(e => { e.textContent = ''; });
        button?.setAttribute('aria-busy', 'true');
        try {
            const res = await fetch('/demo/start', { method: 'POST' });
            const data = await res.json().catch(() => ({}));
            if (!res.ok || !data.success) throw new Error(data.message || 'The demo could not start. Please try again.');
            window.location.href = `/app${tab ? '#tab=' + encodeURIComponent(tab) : ''}`;
        } catch (e) {
            errors.forEach(x => { x.textContent = e.message; });
            button?.removeAttribute('aria-busy');
        }
    }
    document.querySelectorAll('[data-start-demo]').forEach(node => node.addEventListener('click', event => {
        event.preventDefault();
        startDemo(node.dataset.startDemo || '', node);
    }));

    // ── sign-in panel ──────────────────────────────────────────────────
    const panel = $('login-panel');
    let lastFocus = null;
    let currentZerodhaId = '';
    let timer;
    const message = (id, kind, text) => { const m = $(id); m.className = `login-msg ${kind}`; m.textContent = text; };
    const showStep = s => {
        $('step-email').hidden = s !== 'email';
        $('step-otp').hidden = s !== 'otp';
        (s === 'otp' ? $('otp-input') : $('trading-id-input')).focus();
    };
    const open = () => {
        lastFocus = document.activeElement;
        panel.inert = false;  // closed, it is off-screen but would still take keyboard focus
        document.body.classList.add('login-open');
        panel.setAttribute('aria-hidden', 'false');
        if (location.pathname !== '/login') history.replaceState(null, '', '/login');
        setTimeout(() => $('trading-id-input').focus(), 60);
    };
    const close = () => {
        document.body.classList.remove('login-open');
        panel.setAttribute('aria-hidden', 'true');
        panel.inert = true;
        if (location.pathname === '/login') history.replaceState(null, '', '/');
        lastFocus?.focus?.();
    };
    document.querySelectorAll('[data-open-login]').forEach(node => node.addEventListener('click', e => { e.preventDefault(); open(); }));
    document.querySelectorAll('[data-close-login]').forEach(node => node.addEventListener('click', close));
    document.addEventListener('keydown', e => {
        if (!document.body.classList.contains('login-open')) return;
        if (e.key === 'Escape') close();
        if (e.key === 'Tab') {  // keep focus inside the dialog
            const focusable = [...panel.querySelectorAll('button, input, a')].filter(n => !n.closest('[hidden]'));
            const first = focusable[0], lastEl = focusable[focusable.length - 1];
            if (e.shiftKey && document.activeElement === first) { e.preventDefault(); lastEl.focus(); }
            else if (!e.shiftKey && document.activeElement === lastEl) { e.preventDefault(); first.focus(); }
        }
    });
    const startTimer = () => {
        let seconds = 300;
        clearInterval(timer);
        timer = setInterval(() => {
            seconds--;
            $('otp-timer').textContent = seconds > 0
                ? `Code expires in ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`
                : 'Code expired. Request a new one.';
            if (seconds <= 0) clearInterval(timer);
        }, 1000);
    };
    async function sendOTP() {
        const zerodhaId = $('trading-id-input').value.trim().toUpperCase();
        const email = $('email-input').value.trim();
        if (!zerodhaId) return message('msg-email', 'error', 'Please enter your Trading ID');
        if (!email) return message('msg-email', 'error', 'Please enter your email address');
        currentZerodhaId = zerodhaId;
        const btn = $('btn-send-otp');
        btn.setAttribute('aria-busy', 'true');
        message('msg-email', 'info', 'Sending code…');
        try {
            const res = await fetch('/auth/send-otp', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ email, zerodha_id: zerodhaId }),
            });
            const data = await res.json();
            if (!data.success) {
                message('msg-email', 'error', data.message || 'Failed to send the code');
            } else {
                $('otp-delivery-message').textContent = data.mode === 'development' && data.otp
                    ? 'Development-only code (email failed)' : 'Enter the 6-digit code sent to your email';
                if (data.mode === 'development' && data.otp) $('otp-input').value = data.otp;
                message('msg-email', 'success', '');
                message('msg-otp', 'info', '');
                showStep('otp');
                startTimer();
            }
        } catch (e) {
            message('msg-email', 'error', 'Connection error. Please try again.');
        }
        btn.removeAttribute('aria-busy');
    }
    async function verifyOTP() {
        const otp = $('otp-input').value.trim();
        if (otp.length !== 6) return message('msg-otp', 'error', 'Please enter the 6-digit code');
        const btn = $('btn-verify');
        btn.setAttribute('aria-busy', 'true');
        try {
            const res = await fetch('/auth/verify-otp', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ otp, zerodha_id: currentZerodhaId }),
            });
            const data = await res.json();
            if (data.success) {
                message('msg-otp', 'success', 'Signed in. Opening your workspace…');
                setTimeout(() => { window.location.href = '/app'; }, 400);
                return;
            }
            message('msg-otp', 'error', data.message || 'Invalid code');
        } catch (e) {
            message('msg-otp', 'error', 'Connection error. Please try again.');
        }
        btn.removeAttribute('aria-busy');
    }
    $('btn-send-otp').addEventListener('click', sendOTP);
    $('btn-verify').addEventListener('click', verifyOTP);
    $('btn-resend').addEventListener('click', () => showStep('email'));
    $('email-input').addEventListener('keydown', e => { if (e.key === 'Enter') sendOTP(); });
    $('trading-id-input').addEventListener('keydown', e => { if (e.key === 'Enter') $('email-input').focus(); });
    $('otp-input').addEventListener('keydown', e => { if (e.key === 'Enter') verifyOTP(); });
    if (document.body.dataset.loginOnLoad === 'true') open();
})();
