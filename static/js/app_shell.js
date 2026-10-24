// Terminal layout for every screen size.
//
// * Tablets and wider: every header control stays on screen (they are the risk
//   and account controls); when they do not fit beside the logo they wrap onto a
//   second header row.
// * Phones (below 768 px): the header keeps Start / Stop Live, the Kill Switch and
//   Agents, and a Menu button holds the other controls (they move back when the
//   window grows). A bottom navigation bar gives four main tabs and "More" (every
//   tab, in a sheet), like a native app; the top tab bar is hidden.
// * Sheets are .modal-overlay pop-ups, so the Back button closes them (pwa.js).
// Each control is the original element, moved: its handlers and state stay as they were.
(function () {
    'use strict';
    const header = document.querySelector('.header');
    const right = header && header.querySelector('.header-right');
    const tabs = document.querySelector('.tabs');
    if (!header || !right || !tabs) return;

    const KEEP = new Set(['btn-start', 'btn-stop', 't-kill-btn', 't-agents-pill', 't-menu-btn']);
    const phone = window.matchMedia('(max-width: 767.98px)');
    const haptic = () => { try { if (document.documentElement.classList.contains('is-app')) navigator.vibrate?.(6); } catch (e) { /* none */ } };

    function sheet(id, title) {
        const el = document.createElement('div');
        el.id = id;
        el.className = 'modal-overlay vq-sheet-overlay';
        el.innerHTML = `<div class="modal vq-sheet" role="dialog" aria-modal="true" aria-label="${title}">
            <div class="vq-sheet-grip" aria-hidden="true"></div>
            <div class="vq-sheet-head"><b>${title}</b><button class="pwa-close" type="button" aria-label="Close">&times;</button></div>
            <div class="vq-sheet-body"></div></div>`;
        el.addEventListener('click', e => {
            if (e.target === el || e.target.closest('.pwa-close')) window.hideModal(id);
        });
        document.body.appendChild(el);
        return el;
    }

    // ── header menu ────────────────────────────────────────────────────
    const menuBtn = document.createElement('button');
    menuBtn.id = 't-menu-btn';
    menuBtn.type = 'button';
    menuBtn.className = 'btn btn-outline btn-sm vq-menu-btn';
    menuBtn.setAttribute('aria-haspopup', 'dialog');
    menuBtn.setAttribute('aria-label', 'Menu');
    menuBtn.innerHTML = '<span aria-hidden="true">☰</span><span class="vq-menu-label">Menu</span>';
    right.appendChild(menuBtn);
    const menu = sheet('vq-menu', 'Menu');
    const menuBody = menu.querySelector('.vq-sheet-body');
    const moved = [];          // [element, placeholder]

    function layoutHeader() {
        if (phone.matches && !moved.length) {
            [...right.children].forEach(el => {
                if (KEEP.has(el.id) || el.classList.contains('t-head-meta')) return;
                const mark = document.createComment('vq-menu');
                el.replaceWith(mark);
                menuBody.appendChild(el);
                moved.push([el, mark]);
            });
        } else if (!phone.matches && moved.length) {
            moved.splice(0).forEach(([el, mark]) => mark.replaceWith(el));
            if (menu.classList.contains('show')) window.hideModal('vq-menu');
        }
        menuBtn.hidden = !phone.matches;
    }
    menuBtn.addEventListener('click', () => { haptic(); window.showModal('vq-menu'); });
    // a control used from the menu closes it (a pop-up it opens appears on its own)
    menuBody.addEventListener('click', e => {
        if (e.target.closest('button, a') && menu.classList.contains('show')) {
            setTimeout(() => { if (menu.classList.contains('show')) window.hideModal('vq-menu'); }, 0);
        }
    });
    phone.addEventListener('change', layoutHeader);
    layoutHeader();

    // ── bottom navigation (phones) ─────────────────────────────────────
    const ICONS = {
        overview: '<path d="M4 13h6V4H4zm10 7h6v-9h-6zM4 20h6v-4H4zm10-13h6V4h-6z"/>',
        'market-flow': '<path d="M3 17l5-6 4 3 7-8M15 6h4v4"/>',
        'day-trade': '<path d="M6 4v16M6 8h2v8H6M14 4v16M14 6h2v6h-2"/>',
        positions: '<path d="M4 7h16v12H4zM8 7V5h8v2"/>',
        orders: '<path d="M6 4h12v16l-3-2-3 2-3-2-3 2zM9 9h6M9 13h6"/>',
        backtest: '<path d="M4 19V5M4 19h16M7 15l4-5 3 3 5-6"/>',
        agents: '<circle cx="12" cy="8" r="3"/><path d="M5 20c1-4 4-6 7-6s6 2 7 6"/>',
        strategies: '<path d="M5 5h6v6H5zM13 13h6v6h-6zM11 8h4v5"/>',
        signals: '<path d="M4 12h3l2-6 4 12 2-6h5"/>',
        risk: '<path d="M12 3l8 3v6c0 4.5-3.4 8-8 9-4.6-1-8-4.5-8-9V6z"/>',
        'oi-tracker': '<path d="M5 20V10M10 20V4M15 20v-7M20 20V8"/>',
        swing: '<path d="M3 15c3-8 6-8 9 0s6 8 9 0"/>',
        analysis: '<circle cx="10.5" cy="10.5" r="6"/><path d="M15 15l5 5"/>',
        charts: '<path d="M7 5v14M7 8h-2v6h2M17 4v16M17 7h-2v9h2M12 9v8"/>',
        reports: '<path d="M7 3h7l4 4v14H7zM14 3v4h4M10 12h5M10 16h5"/>',
        'paper-trading': '<path d="M5 4h14v16H5zM8 8h8M8 12h8M8 16h5"/>',
        replay: '<path d="M4 12a8 8 0 1 0 3-6.2M4 4v4h4"/><path d="M11 9l4 3-4 3z"/>',
        'strategy-builder': '<path d="M4 20l4-1L19 8l-3-3L5 16zM14 7l3 3"/>',
        journal: '<path d="M6 3h11a1 1 0 0 1 1 1v16a1 1 0 0 1-1 1H6zM9 3v18M12 8h4M12 12h4"/>',
        more: '<circle cx="5" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="19" cy="12" r="1.6"/>',
    };
    const LABELS = { overview: 'Home', 'market-flow': 'Flow', 'day-trade': 'Day Trade', positions: 'Positions',
                     'oi-tracker': 'OI', swing: 'Swing',
                     orders: 'Orders', backtest: 'Backtest', agents: 'Agents', strategies: 'Strategies' };
    const svg = name => `<svg viewBox="0 0 24 24" aria-hidden="true">${ICONS[name] || ICONS.more}</svg>`;
    const realTab = name => tabs.querySelector(`.tab[data-tab="${CSS.escape(name)}"]`);
    // the main tabs; one this session cannot open (public demo) gives way to the next that it can
    const picks = [];
    // (a view-only account has OI Tracker, Market Flow, Day Trade and Swing: all four fit)
    for (const name of ['overview', 'market-flow', 'day-trade', 'positions', 'oi-tracker', 'swing', 'backtest', 'agents',
                        'orders', 'strategies']) {
        if (picks.length < 4 && realTab(name)) picks.push(name);
    }

    const nav = document.createElement('nav');
    nav.className = 'vq-bnav';
    nav.setAttribute('aria-label', 'Main sections');
    nav.innerHTML = picks.map(n => `<button type="button" data-go="${n}">${svg(n)}<span>${LABELS[n]}</span></button>`).join('')
        + `<button type="button" data-more>${svg('more')}<span>More</span></button>`;
    document.body.appendChild(nav);

    const more = sheet('vq-more', 'All sections');
    const moreBody = more.querySelector('.vq-sheet-body');
    moreBody.classList.add('vq-more-grid');
    const item = t => {
        const name = t.dataset.tab || t.dataset.locked;
        const locked = !t.dataset.tab;
        const label = t.textContent.replace(/\W+$/, '').trim();
        return `<button type="button" class="vq-more-item${t.classList.contains('active') ? ' on' : ''}${locked ? ' locked' : ''}" data-go="${name}">
            ${svg(name)}<span>${label}</span>${locked ? '<i aria-label="locked">🔒</i>' : ''}</button>`;
    };
    // the same sections as the tab bar, each under its heading
    function fillMore() {
        const groups = [...tabs.querySelectorAll('.tab-group')];
        moreBody.innerHTML = groups.length
            ? groups.map(g => `<h3 class="vq-more-head">${g.getAttribute('aria-label')}</h3>${[...g.querySelectorAll('.tab')].map(item).join('')}`).join('')
            : [...tabs.querySelectorAll('.tab')].map(item).join('');
    }
    function go(name) {
        const btn = realTab(name) || tabs.querySelector(`.tab[data-locked="${CSS.escape(name)}"]`);
        if (!btn) return;
        haptic();
        btn.click();
        window.scrollTo({ top: 0, behavior: 'instant' in window ? 'instant' : 'auto' });
    }
    nav.addEventListener('click', e => {
        const b = e.target.closest('button');
        if (!b) return;
        if (b.hasAttribute('data-more')) { haptic(); fillMore(); window.showModal('vq-more'); return; }
        go(b.dataset.go);
    });
    moreBody.addEventListener('click', e => {
        const b = e.target.closest('[data-go]');
        if (!b) return;
        window.hideModal('vq-more');
        go(b.dataset.go);
    });
    function syncActive() {
        const active = tabs.querySelector('.tab.active')?.dataset.tab;
        let inNav = false;
        nav.querySelectorAll('[data-go]').forEach(b => {
            const on = b.dataset.go === active;
            inNav = inNav || on;
            b.classList.toggle('on', on);
            b.setAttribute('aria-current', on ? 'page' : 'false');
        });
        nav.querySelector('[data-more]').classList.toggle('on', !inNav && !!active);
    }
    new MutationObserver(syncActive).observe(tabs, { subtree: true, attributes: true, attributeFilter: ['class'] });
    syncActive();

    // the boards' long explanations fold into "How it works" on a phone (the same element, moved)
    const legends = [...document.querySelectorAll('.mf-legend')].map(legend => {
        const box = document.createElement('details');
        box.className = 'vq-howto';
        box.innerHTML = '<summary>How it works</summary>';
        return [legend, box, document.createComment('vq-howto')];
    });
    function layoutLegends() {
        legends.forEach(([legend, box, mark]) => {
            if (phone.matches && legend.parentElement !== box) {
                legend.replaceWith(mark);
                mark.after(box);
                box.append(legend);
            } else if (!phone.matches && legend.parentElement === box) {
                mark.replaceWith(legend);
                box.remove();
            }
        });
    }

    function layoutPhone() { document.body.classList.toggle('has-bnav', phone.matches); layoutLegends(); }
    phone.addEventListener('change', layoutPhone);
    layoutPhone();
})();
