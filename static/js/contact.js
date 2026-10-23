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
            const phone = String(data.phone || '').trim();
            const digits = phone.replace(/\D/g, '').length;
            if (!/^\+?[0-9() .-]{10,20}$/.test(phone) || digits < 10 || digits > 15) {
                return say('error', 'Please enter your phone number: 10 to 15 digits, with the country code if outside India.');
            }
            if (!data.access_level) return say('error', 'Please choose the access you want.');
            if (!String(data.platform || '').trim()) return say('error', 'Please tell us which broker you trade with.');
            if (data.confirm !== 'yes') return say('error', 'Please confirm the access and platform you selected.');
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
