// Landing page motion: the hero's phone slideshow, the features slider and the
// counting stats. Everything pauses on hover / focus / a hidden tab, works by
// touch (swipe) and keyboard, and stays still for people who ask for less motion.
(function () {
    'use strict';
    const still = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    // ── phone slideshow ────────────────────────────────────────────────
    const stage = document.querySelector('[data-phone-slides]');
    if (stage) {
        const slides = [...stage.querySelectorAll('.phone-slide')];
        const dots = [...stage.querySelectorAll('.phone-dots button')];
        const caption = stage.querySelector('.phone-caption');
        let index = 0, timer = null, paused = false;
        const show = next => {
            if (next === index) return;
            const prev = slides[index];
            prev.classList.remove('is-active');
            prev.classList.add('is-leaving');
            setTimeout(() => prev.classList.remove('is-leaving'), 700);
            index = (next + slides.length) % slides.length;
            slides[index].classList.add('is-active');
            const img = slides[index].querySelector('img[loading="lazy"]');
            if (img) img.loading = 'eager';
            dots.forEach((d, i) => d.setAttribute('aria-current', String(i === index)));
            if (caption) caption.innerHTML = dots[index].dataset.caption;   // our own markup (data-caption)
        };
        const tick = () => { if (!paused && !document.hidden) show(index + 1); };
        const start = () => { if (!still && !timer) timer = setInterval(tick, 4500); };
        const restart = () => { clearInterval(timer); timer = null; start(); };
        dots.forEach((d, i) => d.addEventListener('click', () => { show(i); restart(); }));
        stage.addEventListener('pointerenter', () => { paused = true; });
        stage.addEventListener('pointerleave', () => { paused = false; });
        stage.addEventListener('focusin', () => { paused = true; });
        stage.addEventListener('focusout', () => { paused = false; });
        let x0 = null;
        const screen = stage.querySelector('.phone-screen');
        screen.addEventListener('pointerdown', e => { x0 = e.clientX; });
        screen.addEventListener('pointercancel', () => { x0 = null; });
        screen.addEventListener('pointerup', e => {
            if (x0 === null) return;
            const dx = e.clientX - x0;
            x0 = null;
            if (Math.abs(dx) > 40) { show(index + (dx < 0 ? 1 : -1)); restart(); }
        });
        start();
    }

    // ── the OI Tracker slide, live: tiles flow in, values move (simulated data) ──
    const oi = document.querySelector('[data-oi-live]');
    if (oi) {
        const $o = k => oi.querySelector(`[data-oi="${k}"]`);
        const fmt = (v, d = 0) => v.toLocaleString('en-IN', { minimumFractionDigits: d, maximumFractionDigits: d });
        const GAP = 50;
        let spot = 22612.4, pcr = 1.08, tickTimer = null;
        // a plausible recent history, so the trend line has a shape from the first frame
        const history = Array.from({ length: 24 }, (_, i) => pcr + Math.sin((i - 23) / 4) * 0.06 + (i < 23 ? (Math.random() - 0.5) * 0.03 : 0));
        const drawTrend = () => {
            const pts = history.slice(-30);
            const line = $o('trend');
            if (!line) return;
            // y: PCR 0.8 (bottom) .. 1.2 (top), 1.0 on the dashed mid line
            line.setAttribute('points', pts.map((v, i) => `${(i / (pts.length - 1) * 100).toFixed(1)},${(40 - (Math.max(0.8, Math.min(1.2, v)) - 0.8) / 0.4 * 40).toFixed(1)}`).join(' '));
            const now = $o('pcrnow');
            if (now) { now.textContent = pcr.toFixed(2); now.className = pcr >= 1 ? 'up' : 'dn'; }
        };
        const strikes = () => { const atm = Math.round(spot / GAP) * GAP; return [-2, -1, 0, 1, 2].map(k => atm + k * GAP); };
        // OI per strike (lakh): puts heavier below spot, calls heavier above
        const oiAt = (k, side) => {
            const d = (k - spot) / GAP;
            const base = side === 'ce' ? 40 + Math.max(0, d) * 14 : 40 + Math.max(0, -d) * 14;
            return base * (0.9 + Math.random() * 0.2) * (side === 'pe' ? pcr : 1);
        };
        const chain = $o('chain');
        function paint(first) {
            const ks = strikes(), atm = ks[2];
            const rows = ks.map(k => ({ k, ce: oiAt(k, 'ce'), pe: oiAt(k, 'pe') }));
            const max = Math.max(...rows.flatMap(r => [r.ce, r.pe]));
            const ceWall = rows.reduce((a, b) => (b.ce > a.ce ? b : a)), peWall = rows.reduce((a, b) => (b.pe > a.pe ? b : a));
            chain.innerHTML = rows.map(r => `<div class="oi-l-row${r.k === atm ? ' atm' : ''}${r === ceWall ? ' wall-ce' : ''}${r === peWall ? ' wall-pe' : ''}">
                <div class="ce"><i style="width:${first ? 0 : (r.ce / max * 100).toFixed(0)}%"></i></div><div class="k">${fmt(r.k)}</div>
                <div class="pe"><i style="width:${first ? 0 : (r.pe / max * 100).toFixed(0)}%"></i></div></div>`).join('');
            if (first) requestAnimationFrame(() => requestAnimationFrame(() => chain.querySelectorAll('.oi-l-row').forEach((row, j) => {
                row.querySelector('.ce i').style.width = `${(rows[j].ce / max * 100).toFixed(0)}%`;
                row.querySelector('.pe i').style.width = `${(rows[j].pe / max * 100).toFixed(0)}%`;
            })));
            $o('atm').textContent = fmt(atm);
            $o('maxpain').textContent = fmt(atm);
            $o('cewall').textContent = fmt(ceWall.k);
            $o('pewall').textContent = fmt(peWall.k);
            $o('ceoi').textContent = `${(rows.reduce((s, r) => s + r.ce, 0) / 5 * 2.1).toFixed(1)} L`;
            $o('peoi').textContent = `${(rows.reduce((s, r) => s + r.pe, 0) / 5 * 2.1).toFixed(1)} L`;
        }
        function set(key, text, cls, dir) {
            const el = $o(key);
            if (el.textContent === text) return;
            el.textContent = text;
            if (cls !== undefined) el.className = cls;
            const tile = el.closest('.oi-l-tile');
            if (tile && dir) { tile.classList.remove('flash-up', 'flash-dn'); void tile.offsetWidth; tile.classList.add(dir > 0 ? 'flash-up' : 'flash-dn'); }
        }
        function tick(first) {
            const prev = spot;
            spot = Math.max(22300, Math.min(22900, spot + (Math.random() - 0.48) * 9));
            pcr = Math.max(0.72, Math.min(1.34, pcr + (Math.random() - 0.5) * 0.035));
            set('spot', fmt(spot, 2), undefined, spot - prev);
            set('pcr', pcr.toFixed(2), undefined, 0);
            history.push(pcr);
            if (history.length > 60) history.shift();
            drawTrend();
            $o('pcrbar').style.width = `${Math.min(100, pcr / 2 * 100).toFixed(0)}%`;
            const bias = pcr > 1.2 ? ['BULLISH', 'up', 'PCR above 1.2'] : pcr > 1 ? ['MILDLY BULLISH', 'up', 'PCR above 1']
                : pcr < 0.8 ? ['BEARISH', 'dn', 'PCR below 0.8'] : pcr < 0.9 ? ['MILDLY BEARISH', 'dn', 'PCR below 0.9'] : ['NEUTRAL', '', 'PCR near 1'];
            set('bias', bias[0], bias[1], bias[1] === 'up' ? 1 : bias[1] === 'dn' ? -1 : 0);
            $o('why').textContent = bias[2];
            $o('straddle').textContent = fmt(195 + Math.random() * 30);
            const now = new Date();
            $o('clock').textContent = now.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
            paint(first);
        }
        const start = () => {
            if (still) return;
            oi.classList.remove('enter');
            void oi.offsetWidth;                            // restart the flow-in each time the slide comes round
            oi.classList.add('enter');
            tick(true);
            clearInterval(tickTimer);
            if (!still) tickTimer = setInterval(() => { if (!document.hidden) tick(false); }, 1600);
        };
        const stop = () => { clearInterval(tickTimer); tickTimer = null; };
        // react only when the slide becomes active / inactive (start() changes classes too)
        let showing = false;
        new MutationObserver(() => {
            const now = oi.classList.contains('is-active');
            if (now === showing) return;
            showing = now;
            if (now) start(); else stop();
        }).observe(oi, { attributes: true, attributeFilter: ['class'] });
        if (still) { oi.classList.add('enter'); tick(false); }
        else if (oi.classList.contains('is-active')) { showing = true; start(); }
    }

    // ── sliders ([data-slider]: a scroll-snap track, arrows, dots, autoplay) ──
    document.querySelectorAll('[data-slider]').forEach(slider => {
        const track = slider.querySelector('.slider-track');
        const items = [...track.children];
        const dotsBox = slider.querySelector('[data-dots]');
        let dots = [], timer = null, paused = false;
        const perView = () => Math.max(1, Math.round(track.clientWidth / (items[0].getBoundingClientRect().width || 1)));
        const pages = () => Math.max(1, items.length - perView() + 1);
        const current = () => {
            const left = track.scrollLeft;
            let best = 0;
            items.forEach((it, i) => { if (Math.abs(it.offsetLeft - track.offsetLeft - left) < Math.abs(items[best].offsetLeft - track.offsetLeft - left)) best = i; });
            return Math.min(best, pages() - 1);
        };
        const go = i => {
            const n = pages();
            const target = items[((i % n) + n) % n];
            track.scrollTo({ left: target.offsetLeft - track.offsetLeft, behavior: still ? 'auto' : 'smooth' });
        };
        const buildDots = () => {
            dotsBox.replaceChildren(...Array.from({ length: pages() }, (_, i) => {
                const b = document.createElement('button');
                b.type = 'button';
                b.setAttribute('aria-label', `Show item ${i + 1}`);
                b.addEventListener('click', () => { go(i); restart(); });
                return b;
            }));
            dots = [...dotsBox.children];
            mark();
        };
        const mark = () => { const c = current(); dots.forEach((d, i) => d.setAttribute('aria-current', String(i === c))); };
        const restart = () => { clearInterval(timer); timer = null; start(); };
        const start = () => {
            const every = Number(slider.dataset.autoplay || 0);
            if (still || !every || timer) return;
            timer = setInterval(() => { if (!paused && !document.hidden) go(current() + 1); }, every);
        };
        slider.querySelector('[data-prev]')?.addEventListener('click', () => { go(current() - 1); restart(); });
        slider.querySelector('[data-next]')?.addEventListener('click', () => { go(current() + 1); restart(); });
        track.addEventListener('keydown', e => {
            if (e.key === 'ArrowRight') { e.preventDefault(); go(current() + 1); restart(); }
            if (e.key === 'ArrowLeft') { e.preventDefault(); go(current() - 1); restart(); }
        });
        let raf = 0;
        track.addEventListener('scroll', () => { cancelAnimationFrame(raf); raf = requestAnimationFrame(mark); }, { passive: true });
        ['pointerenter', 'focusin', 'touchstart'].forEach(ev => slider.addEventListener(ev, () => { paused = true; }, { passive: true }));
        ['pointerleave', 'focusout'].forEach(ev => slider.addEventListener(ev, () => { paused = false; }));
        window.addEventListener('resize', () => { buildDots(); }, { passive: true });
        buildDots();
        start();
    });

    // ── counting stats ─────────────────────────────────────────────────
    const counters = [...document.querySelectorAll('[data-count]')];
    const render = (el, v) => { el.textContent = `${el.dataset.prefix || ''}${Math.round(v).toLocaleString('en-IN')}${el.dataset.suffix || ''}`; };
    const run = el => {
        const end = Number(el.dataset.count), t0 = performance.now(), dur = 1200;
        const step = t => {
            const k = Math.min(1, (t - t0) / dur);
            render(el, end * (1 - Math.pow(1 - k, 3)));           // ease-out
            if (k < 1) requestAnimationFrame(step);
        };
        requestAnimationFrame(step);
    };
    if (!still && 'IntersectionObserver' in window) {
        const io = new IntersectionObserver(entries => entries.forEach(en => {
            if (en.isIntersecting) { io.unobserve(en.target); run(en.target); }
        }), { threshold: 0.6 });
        counters.forEach(el => { render(el, 0); io.observe(el); });
    }
})();
