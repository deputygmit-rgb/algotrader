// fetch() for signed-in pages: adds the CSRF token to state-changing requests
// and, when the server answers 403 step_up_required, emails a confirmation
// code, asks for it, and retries the request once.
(function () {
    'use strict';
    const csrf = document.body.dataset.csrf || '';
    const originalFetch = window.fetch.bind(window);
    let pending = null;  // one confirmation prompt at a time

    function post(url, body) {
        return originalFetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
            body: JSON.stringify(body || {}),
        }).then(r => r.json()).catch(() => ({}));
    }

    async function confirmWithEmailCode() {
        const sent = await post('/auth/step-up/send');
        if (!sent.success) {
            alert(sent.message || 'Could not send a confirmation code.');
            return false;
        }
        const code = window.prompt('Enter the confirmation code sent to your email:');
        if (!code) return false;
        const verified = await post('/auth/step-up/verify', { otp: code.trim() });
        if (!verified.success) {
            alert(verified.message || 'Invalid or expired code.');
            return false;
        }
        return true;
    }

    window.fetch = async function (input, init) {
        init = init || {};
        const method = (init.method || 'GET').toUpperCase();
        if (method !== 'GET' && method !== 'HEAD') {
            init.headers = Object.assign({}, init.headers, { 'X-CSRF-Token': csrf });
        }
        const res = await originalFetch(input, init);
        if (res.status !== 403) return res;
        const body = await res.clone().json().catch(() => null);
        if (!body || body.detail !== 'step_up_required') return res;
        pending = pending || confirmWithEmailCode().finally(() => { pending = null; });
        if (await pending) return originalFetch(input, init);
        const message = 'Cancelled: this needs the confirmation code from your email.';
        return new Response(JSON.stringify({ detail: message }), {
            status: 403, headers: { 'Content-Type': 'application/json' },
        });
    };
})();
