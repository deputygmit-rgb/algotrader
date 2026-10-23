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
