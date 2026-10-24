// Admin panel. Every value is written with textContent, never parsed as HTML:
// access-request fields come from anonymous visitors.
(function () {
    'use strict';
    const $ = id => document.getElementById(id);
    const me = document.body.dataset.admin;
    let data = null;

    function el(tag, attrs, ...children) {
        const node = document.createElement(tag);
        Object.entries(attrs || {}).forEach(([k, v]) => {
            if (k === 'class') node.className = v;
            else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
            else if (v !== false && v !== null && v !== undefined) node.setAttribute(k, v === true ? '' : v);
        });
        children.flat().forEach(c => node.append(c instanceof Node ? c : document.createTextNode(String(c ?? ''))));
        return node;
    }

    function flash(text, kind) {
        const box = $('adm-flash');
        box.textContent = text;
        box.className = `adm-flash ${kind || 'info'}`;
        box.hidden = false;
        clearTimeout(flash.timer);
        flash.timer = setTimeout(() => { box.hidden = true; }, 6000);
    }

    async function call(method, url, body) {
        const res = await fetch(url, {
            method,
            headers: body ? { 'Content-Type': 'application/json' } : {},
            body: body ? JSON.stringify(body) : undefined,
        });
        const json = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(json.detail || json.message || `Request failed (${res.status})`);
        return json;
    }

    async function act(method, url, body, done) {
        try {
            await call(method, url, body);
            flash(done, 'success');
            await load();
            return true;
        } catch (e) {
            flash(e.message, 'error');
            return false;
        }
    }

    function levelSelect(value, onchange) {
        return el('select', { onchange }, data.levels.map(l => el('option', { value: l.value, selected: l.value === value }, l.label)));
    }

    function renderUsers() {
        const body = $('adm-users');
        body.replaceChildren(...data.users.map(u => {
            const session = u.kite_session.linked ? `Linked (${u.kite_session.saved_at.slice(11)})`
                : u.kite_session.saved_at ? `Expired (${u.kite_session.saved_at.slice(0, 10)})` : 'Not linked';
            const actions = [];
            if (u.zerodha_id !== me) {
                actions.push(el('button', { class: 'btn btn-outline btn-sm', type: 'button', onclick: () => {
                    if (confirm(`Sign ${u.zerodha_id} out everywhere and stop their running services?`)) {
                        act('POST', `/api/admin/users/${encodeURIComponent(u.zerodha_id)}/revoke-sessions`, {}, `${u.zerodha_id} signed out everywhere.`);
                    }
                } }, 'Sign out'));
            }
            if (!u.is_admin) {
                actions.push(el('button', { class: 'btn btn-danger btn-sm', type: 'button', onclick: () => {
                    if (confirm(`Remove ${u.zerodha_id}? Their sessions end and their API credentials are deleted.`)) {
                        act('DELETE', `/api/admin/users/${encodeURIComponent(u.zerodha_id)}`, null, `${u.zerodha_id} removed.`);
                    }
                } }, 'Remove'));
            }
            const access = levelSelect(u.access_level, e => {
                const level = e.target.value;
                if (!confirm(`Change ${u.zerodha_id} to "${e.target.selectedOptions[0].textContent}"?`)) { e.target.value = u.access_level; return; }
                act('POST', `/api/admin/users/${encodeURIComponent(u.zerodha_id)}/access`, { access_level: level }, `${u.zerodha_id} access updated.`)
                    .then(ok => { if (!ok) e.target.value = u.access_level; });
            });
            const email = el('button', { class: 'adm-link', type: 'button', title: 'Change email', onclick: () => {
                const next = prompt(`New sign-in email for ${u.zerodha_id}:`, u.email);
                if (next && next.trim() !== u.email) {
                    act('POST', `/api/admin/users/${encodeURIComponent(u.zerodha_id)}/email`, { email: next.trim() }, `${u.zerodha_id} email updated.`);
                }
            } }, u.email || '—');
            const statusClass = u.status === 'idle' ? 'adm-muted' : u.status === 'active today' ? 'positive' : '';
            return el('tr', {},
                el('td', {}, el('b', {}, u.zerodha_id), u.is_admin ? el('span', { class: 'adm-badge' }, 'ADMIN') : '',
                   data.market_data.account === u.zerodha_id ? el('span', { class: 'adm-badge adm-badge-feed' }, 'MARKET DATA') : ''),
                el('td', { class: statusClass }, u.status),
                el('td', { title: u.days_active ? `${u.days_active} day(s) active, ${u.requests} requests in the last 30 days` : '' },
                   u.last_active || el('span', { class: 'adm-muted' }, 'not in 30 days')),
                el('td', {}, email),
                el('td', {}, access),
                el('td', {}, u.has_api_key ? u.api_key_hint : el('span', { class: 'adm-muted' }, 'none')),
                el('td', { class: u.kite_session.linked ? 'positive' : 'adm-muted' }, session),
                el('td', {}, String(u.active_sessions)),
                el('td', { class: 'adm-actions' }, actions));
        }));
        $('adm-user-count').textContent = `${data.users.length} account${data.users.length === 1 ? '' : 's'}`;
        $('adm-creds-user').replaceChildren(...data.users.map(u => el('option', { value: u.zerodha_id }, u.zerodha_id)));
        $('adm-add-level').replaceChildren(...data.levels.map(l => el('option', { value: l.value }, l.label)));
    }

    function renderMarket() {
        const m = data.market_data;
        const select = $('adm-market-account');
        select.replaceChildren(el('option', { value: '' }, 'Off (view-only users see the demo market)'),
            ...m.candidates.map(id => el('option', { value: id, selected: id === m.account }, id)));
        $('adm-feed-state').textContent = !m.account ? 'off'
            : m.source === 'backup' ? 'live from the BACKUP source (account not linked today) · no tick stream'
            : m.available ? (m.feed_connected ? 'live · feed connected' : 'live · feed idle') : 'waiting for the account to link today';
        $('adm-feed-state').className = m.available ? 'positive' : 'adm-muted';
        // OI + flow history: recorded every session by the market-data account (src/api/market_recorder.py)
        const r = m.recorder || {};
        if (r.status) {
            $('adm-feed-state').textContent += ` · history: ${r.status}${r.checked_at ? ` (${r.checked_at.slice(11, 16)})` : ''}`;
        }
    }

    function renderBackup() {
        const b = data.backup_quotes || {};
        const f = $('adm-backup');
        if (!f.contains(document.activeElement)) {
            f.base_url.value = b.base_url || '';
            f.enabled.checked = !!b.enabled;
            $('adm-backup-mode').replaceChildren(...(b.modes || []).map(m => el('option', { value: m.value, selected: m.value === b.mode }, m.label)));
        }
        const state = !b.has_token ? 'no token' : !b.enabled ? 'off'
            : b.active ? `IN USE until ${(b.active_until || '').slice(11, 19)}` : 'ready (standby)';
        const extra = [b.failovers ? `${b.failovers} failover${b.failovers === 1 ? '' : 's'}` : '',
            b.last_ok ? `last served ${b.last_ok.slice(11, 19)}` : '',
            b.last_error ? `last error: ${b.last_error.error}` : ''].filter(Boolean).join(' · ');
        $('adm-backup-state').textContent = state + (extra ? ` · ${extra}` : '');
        $('adm-backup-state').className = b.active ? 'negative' : b.configured ? 'positive' : 'adm-muted';
    }

    // the 7-day invitation, then their confirmation and the invite link (src/api/webapp.py)
    function telegramState(c) {
        const day = ts => (ts || '').slice(0, 10);
        if (c.link_sent_at) return `link sent ${day(c.link_sent_at)}`;
        if (c.optin_at) return `confirmed ${day(c.optin_at)}`;
        if (c.followup_at) return `invited ${day(c.followup_at)}`;
        return '—';
    }

    function renderContacts() {
        $('adm-contacts').replaceChildren(...data.contacts.map(c => {
            const status = el('select', { onchange: e => act('POST', `/api/admin/contacts/${c.id}/status`, { status: e.target.value }, `Request #${c.id} marked ${e.target.value}.`) },
                data.contact_statuses.map(s => el('option', { value: s, selected: s === c.status }, s)));
            const add = el('button', { class: 'btn btn-outline btn-sm', type: 'button', onclick: () => {
                const form = $('adm-add');
                form.zerodha_id.value = c.trading_id || '';
                form.email.value = c.email || '';
                if (c.access_level) form.access_level.value = c.access_level;
                form.contact_id.value = String(c.id);
                form.scrollIntoView({ behavior: 'smooth', block: 'center' });
                (c.trading_id ? form.api_key : form.zerodha_id).focus();
            } }, 'Add user');
            const erase = el('button', { class: 'btn btn-danger btn-sm', type: 'button', onclick: () => {
                if (confirm(`Delete request #${c.id} from ${c.name}? Their name, email, phone and message are erased for good.`)) {
                    act('DELETE', `/api/admin/contacts/${c.id}`, null, `Request #${c.id} deleted.`);
                }
            } }, 'Delete');
            return el('tr', {},
                el('td', {}, String(c.id)), el('td', {}, (c.ts || '').replace('T', ' ').slice(0, 16)),
                el('td', {}, c.name), el('td', {}, c.email), el('td', {}, c.phone || '—'), el('td', {}, c.trading_id || '—'),
                el('td', {}, c.access_level || '—'), el('td', {}, c.platform || '—'),
                el('td', { class: 'adm-message' }, c.message || '—'), el('td', {}, telegramState(c)), el('td', {}, status), el('td', { class: 'adm-actions' }, add, erase));
        }));
        const open = data.contacts.filter(c => c.status === 'new').length;
        $('adm-contact-count').textContent = `${open} new`;
    }

    function renderAudit() {
        $('adm-audit').replaceChildren(...data.audit.map(a => el('tr', {},
            el('td', {}, (a.ts || '').replace('T', ' ').slice(0, 19)), el('td', {}, a.admin), el('td', {}, a.action.replace(/_/g, ' ')),
            el('td', {}, a.target),
            el('td', { class: 'adm-muted' }, Object.entries(a.detail || {}).filter(([, v]) => v !== null && v !== '').map(([k, v]) => `${k.replace(/_/g, ' ')}: ${v}`).join(' · ')))));
    }

    // ── site metrics ───────────────────────────────────────────────────
    const FUNNEL_COLS = ['demo_start', 'otp_sent', 'login_success', 'login_failed', 'account_linked', 'contact_submitted'];
    let metricDays = 7;

    async function loadMetrics() {
        let m;
        try {
            m = await call('GET', `/api/admin/metrics?days=${metricDays}`);
        } catch (e) {
            $('met-kpis').replaceChildren(el('p', { class: 'adm-muted' }, e.message));
            return;
        }
        $('met-days').replaceChildren(...m.windows.map(d => el('option', { value: d, selected: d === m.days }, d === 1 ? 'Today' : `${d} days`)));
        const t = m.totals;
        const devices = Object.entries(m.devices || {}).map(([d, n]) => `${n} ${d}`).join(' · ');
        const kpi = (label, value, hint) => el('div', { class: 'adm-kpi' }, el('b', {}, Number(value || 0).toLocaleString('en-IN')),
            el('span', {}, label), hint ? el('small', {}, hint) : '');
        $('met-kpis').replaceChildren(
            kpi('visitors', t.visitors, devices),
            kpi('page views', t.page_views),
            kpi('demo starts', t.demo_start),
            kpi('sign-ins', t.login_success, t.login_failed ? `${t.login_failed} failed` : ''),
            kpi('active accounts', t.active_accounts),
            kpi('access requests', t.contact_submitted),
            kpi('server errors', t.server_errors));
        const num = v => el('td', {}, Number(v || 0).toLocaleString('en-IN'));
        $('met-daily').replaceChildren(...(m.daily.length ? m.daily.map(d => el('tr', {}, el('td', {}, d.day),
            num(d.visitors), num(d.page_views), ...FUNNEL_COLS.map(k => num(d[k])), num(d.active_accounts), num(d.demo_sessions),
            el('td', { class: d.server_errors ? 'negative' : '' }, String(d.server_errors || 0))))
            : [el('tr', {}, el('td', { colspan: 12, class: 'adm-muted' }, 'No activity in this window.'))]));
        $('met-accounts').replaceChildren(...(m.accounts.length ? m.accounts.map(a => el('tr', {},
            el('td', {}, el('b', {}, a.zerodha_id)), el('td', {}, a.last_day), num(a.days_active), num(a.requests)))
            : [el('tr', {}, el('td', { colspan: 4, class: 'adm-muted' }, 'No signed-in accounts in this window.'))]));
        $('met-features').replaceChildren(...m.features.map(f => el('tr', {},
            el('td', {}, `${f.method} ${f.route}`), num(f.people), num(f.calls), el('td', {}, String(f.avg_ms ?? '')),
            el('td', { class: f.errors ? 'negative' : '' }, String(f.errors || 0)))));
        $('met-clicks').replaceChildren(...m.clicks.map(c => el('tr', {},
            el('td', {}, c.page), el('td', {}, c.target), num(c.people), num(c.count))));
    }
    $('met-days').addEventListener('change', e => { metricDays = Number(e.target.value); loadMetrics(); });

    async function load() {
        try {
            data = await call('GET', '/api/admin/overview');
        } catch (e) {
            flash(e.message, 'error');
            return;
        }
        renderUsers();
        renderMarket();
        renderBackup();
        renderContacts();
        renderAudit();
        loadMetrics();
    }

    $('adm-add').addEventListener('submit', async e => {
        e.preventDefault();
        const f = e.target;
        const body = {
            zerodha_id: f.zerodha_id.value.trim().toUpperCase(), email: f.email.value.trim(),
            access_level: f.access_level.value, api_key: f.api_key.value.trim(), api_secret: f.api_secret.value.trim(),
        };
        if (f.contact_id.value) body.contact_id = Number(f.contact_id.value);
        f.api_secret.value = '';  // never keep a secret in the page longer than needed
        if (await act('POST', '/api/admin/users', body, `${body.zerodha_id} added.`)) f.reset();
    });

    $('adm-creds').addEventListener('submit', async e => {
        e.preventDefault();
        const f = e.target;
        const id = f.zerodha_id.value;
        const body = { api_key: f.api_key.value.trim(), api_secret: f.api_secret.value.trim() };
        f.api_secret.value = '';
        if (!confirm(`Replace ${id}'s API credentials? Their running services and broker session end.`)) return;
        if (await act('POST', `/api/admin/users/${encodeURIComponent(id)}/credentials`, body, `${id} credentials updated.`)) f.reset();
    });

    $('adm-market').addEventListener('submit', e => {
        e.preventDefault();
        const id = e.target.account_id.value;
        act('POST', '/api/admin/market-data', { account_id: id }, id ? `Shared market data now comes from ${id}.` : 'Shared market data turned off.');
    });

    $('adm-backup').addEventListener('submit', async e => {
        e.preventDefault();
        const f = e.target;
        const body = { base_url: f.base_url.value.trim(), enabled: f.enabled.checked, mode: f.mode.value };
        if (f.token.value.trim()) body.token = f.token.value.trim();
        f.token.value = '';  // never keep a secret in the page longer than needed
        await act('POST', '/api/admin/backup-quotes', body, 'Backup market data saved.');
    });

    $('adm-backup-clear').addEventListener('click', () => {
        if (confirm('Remove the backup API token? The backup stops until a new one is saved.')) {
            act('POST', '/api/admin/backup-quotes', { clear_token: true }, 'Backup token removed.');
        }
    });

    $('adm-backup-test').addEventListener('click', async () => {
        const out = $('adm-backup-result');
        out.hidden = false;
        out.textContent = 'Fetching live quotes through the backup…';
        try {
            const r = await call('POST', '/api/admin/backup-quotes/test');
            out.textContent = r.success
                ? `OK. Fields returned: ${r.fields.join(', ')}\n\n` + Object.entries(r.quotes).map(([k, q]) =>
                    `${k}: last ${q.last_price} · change ${q.change} · volume ${q.volume} · OI ${q.oi} · OHLC ${q.ohlc.open}/${q.ohlc.high}/${q.ohlc.low}/${q.ohlc.close} · depth ${q.depth_levels}`).join('\n')
                : `Failed: ${r.error}`;
            await load();
        } catch (e) {
            out.textContent = `Failed: ${e.message}`;
        }
    });

    $('adm-refresh').addEventListener('click', load);
    load();
})();
