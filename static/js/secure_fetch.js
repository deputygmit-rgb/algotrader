// fetch() for signed-in pages (dashboard, stock page, admin):
// * adds the CSRF token to state-changing requests;
// * if the token is stale - the browser's session changed after this page
//   loaded, e.g. a new sign-in in another tab - fetches the current one and
//   retries once; if the session now belongs to another account, says so and
//   reloads instead of acting as the wrong user;
// * when the server answers 403 step_up_required, emails a confirmation code,
//   asks for it, and retries the request once.
(function () {
    'use strict';
    const body = document.body;
    const pageUser = body.dataset.user || body.dataset.admin || '';
    let csrf = body.dataset.csrf || '';
    const originalFetch = window.fetch.bind(window);
    let pendingStepUp = null;    // one confirmation prompt at a time
    let pendingRefresh = null;   // one token refresh at a time

    function post(url, payload) {
        return originalFetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
            body: JSON.stringify(payload || {}),
        }).then(r => r.json()).catch(() => ({}));
    }

    async function refreshToken() {
        const res = await originalFetch('/auth/csrf', { headers: { Accept: 'application/json' } });
        if (!res.ok) return false;                       // signed out: let the caller see the 403
        const data = await res.json().catch(() => ({}));
        if (pageUser && data.zerodha_id && data.zerodha_id !== pageUser) {
            alert(`This browser is now signed in as ${data.zerodha_id}, not ${pageUser}. The page will reload.`);
            window.location.reload();
            return false;
        }
        if (!data.csrf_token) return false;
        csrf = data.csrf_token;
        body.dataset.csrf = csrf;                        // pages that read it directly stay in sync
        return true;
    }

    // one code per confirmation (valid 2 minutes, 2 attempts); on the admin pages every
    // change asks for its own
    async function confirmWithEmailCode() {
        const sent = await post('/auth/step-up/send');
        if (!sent.success) {
            alert(sent.message || 'Could not send a confirmation code.');
            return false;
        }
        const admin = document.body.classList.contains('t-admin');
        let ask = admin
            ? 'Admin: enter the confirmation code sent to your email (valid 2 minutes).'
            : 'This action can trade real money. Enter the confirmation code sent to your email (valid 2 minutes):';
        for (;;) {
            const code = window.prompt(ask);
            if (!code) return false;
            const verified = await post('/auth/step-up/verify', { otp: code.trim() });
            if (verified.success) return true;
            if (!verified.attempts_left) {
                alert((verified.message || 'Invalid or expired code.') + ' Try the action again for a new code.');
                return false;
            }
            ask = `${verified.message} Enter the code again:`;
        }
    }

    function withToken(init) {
        const method = (init.method || 'GET').toUpperCase();
        if (method !== 'GET' && method !== 'HEAD') {
            init.headers = Object.assign({}, init.headers, { 'X-CSRF-Token': csrf });
        }
        return method;
    }

    window.fetch = async function (input, init) {
        init = init || {};
        const method = withToken(init);
        let res = await originalFetch(input, init);
        if (res.status !== 403) return res;
        let reply = await res.clone().json().catch(() => null);

        if (reply && reply.error === 'CSRF validation failed' && method !== 'GET' && method !== 'HEAD') {
            pendingRefresh = pendingRefresh || refreshToken().finally(() => { pendingRefresh = null; });
            if (!(await pendingRefresh)) return res;
            withToken(init);
            res = await originalFetch(input, init);
            if (res.status !== 403) return res;
            reply = await res.clone().json().catch(() => null);
        }

        if (!reply || reply.detail !== 'step_up_required') return res;
        pendingStepUp = pendingStepUp || confirmWithEmailCode().finally(() => { pendingStepUp = null; });
        if (await pendingStepUp) {
            withToken(init);
            return originalFetch(input, init);
        }
        const message = 'Cancelled: this action needs the confirmation code from your email.';
        return new Response(JSON.stringify({ detail: message, error: message }), {
            status: 403, headers: { 'Content-Type': 'application/json' },
        });
    };
})();
