// "Request access" forms: any <form data-contact-form="<source>"> posts to /contact.
(function () {
    'use strict';
    function bind(form) {
        const status = form.querySelector('[data-contact-status]');
        const button = form.querySelector('button[type="submit"]');
        const say = (kind, text) => {
            if (!status) return;
            status.className = `contact-status ${kind}`;
            status.textContent = text;
        };
        form.addEventListener('submit', async (e) => {
            e.preventDefault();
            const data = Object.fromEntries(new FormData(form).entries());
            if (!String(data.name || '').trim()) return say('error', 'Please tell us your name.');
            if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(String(data.email || '').trim())) return say('error', 'Please enter a valid email address.');
            data.source = form.dataset.contactForm || 'landing';
            button?.setAttribute('aria-busy', 'true');
            if (button) button.disabled = true;
            say('info', 'Sending…');
            try {
                const res = await fetch('/contact', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(data),
                });
                const body = await res.json().catch(() => ({}));
                if (!res.ok || !body.success) throw new Error(body.message || 'Could not send your request. Please try again.');
                form.reset();
                say('success', body.message || "Thanks! We'll get back to you by email.");
            } catch (err) {
                say('error', err.message);
            }
            button?.removeAttribute('aria-busy');
            if (button) button.disabled = false;
        });
    }
    document.querySelectorAll('form[data-contact-form]').forEach(bind);
})();
