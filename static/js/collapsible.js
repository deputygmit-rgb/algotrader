(() => {
    // Cards with an id remember being collapsed, so a re-render that replaces
    // the whole card (title included) does not pop it back open.
    const collapsedCardIds = new Set();

    function setExpanded(title, expanded) {
        const card = title.closest('.card');
        if (!card) return;
        if (card.id) {
            if (expanded) collapsedCardIds.delete(card.id);
            else collapsedCardIds.add(card.id);
        }
        title.setAttribute('role', 'button');
        title.setAttribute('tabindex', '0');
        title.setAttribute('aria-expanded', String(expanded));
        for (const child of card.children) {
            if (child !== title) child.hidden = !expanded;
        }
    }

    function initializeTitle(title) {
        if (title.dataset.collapseInitialized === 'true') return;
        const card = title.closest('.card');
        if (!card || title.parentElement !== card) return;

        title.dataset.collapseInitialized = 'true';
        title.setAttribute('role', 'button');
        title.setAttribute('tabindex', '0');
        title.setAttribute('aria-expanded', 'true');
        title.addEventListener('click', event => {
            if (event.target.closest('a, button, input, select, textarea')) return;
            setExpanded(title, title.getAttribute('aria-expanded') !== 'true');
        });
        title.addEventListener('keydown', event => {
            if (event.target !== title) return;
            if (event.key !== 'Enter' && event.key !== ' ') return;
            event.preventDefault();
            setExpanded(title, title.getAttribute('aria-expanded') !== 'true');
        });
        if (card.id && collapsedCardIds.has(card.id)) setExpanded(title, false);
    }

    function initialize(root = document) {
        if (root.matches?.('.card > .card-title')) initializeTitle(root);
        root.querySelectorAll?.('.card > .card-title').forEach(initializeTitle);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => {
            initialize();
            new MutationObserver(records => records.forEach(record => {
                record.addedNodes.forEach(node => {
                    if (node.nodeType === 1) initialize(node);
                });
            })).observe(document.body, { childList: true, subtree: true });
        });
    } else {
        initialize();
        new MutationObserver(records => records.forEach(record => {
            record.addedNodes.forEach(node => {
                if (node.nodeType === 1) initialize(node);
            });
        })).observe(document.body, { childList: true, subtree: true });
    }
})();
