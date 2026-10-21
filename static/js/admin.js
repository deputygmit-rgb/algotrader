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
            return el('tr', {},
                el('td', {}, el('b', {}, u.zerodha_id), u.is_admin ? el('span', { class: 'adm-badge' }, 'ADMIN') : '',
                   data.market_data.account === u.zerodha_id ? el('span', { class: 'adm-badge adm-badge-feed' }, 'MARKET DATA') : ''),
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
            : m.available ? (m.feed_connected ? 'live · feed connected' : 'live · feed idle') : 'waiting for the account to link today';
        $('adm-feed-state').className = m.available ? 'positive' : 'adm-muted';
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
            return el('tr', {},
                el('td', {}, String(c.id)), el('td', {}, (c.ts || '').replace('T', ' ').slice(0, 16)),
                el('td', {}, c.name), el('td', {}, c.email), el('td', {}, c.phone || '—'), el('td', {}, c.trading_id || '—'),
                el('td', {}, c.access_level || '—'), el('td', {}, c.platform || '—'),
                el('td', { class: 'adm-message' }, c.message || '—'), el('td', {}, status), el('td', {}, add));
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

    async function load() {
        try {
            data = await call('GET', '/api/admin/overview');
        } catch (e) {
            flash(e.message, 'error');
            return;
        }
        renderUsers();
        renderMarket();
        renderContacts();
        renderAudit();
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

    $('adm-refresh').addEventListener('click', load);
    load();
})();
