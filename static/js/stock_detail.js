(() => {
    const symbol = document.body.dataset.symbol || '';
    const encodedSymbol = encodeURIComponent(symbol);
    const screenerUrl = `https://www.screener.in/company/${encodedSymbol}/consolidated/`;
    const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    })[char]);
    const textValue = value => typeof value === 'string' || typeof value === 'number' ? String(value) : '';
    const display = value => textValue(value) ? esc(value) : '—';
    const formatNumber = (value, digits = 2) => {
        if (value == null || value === '') return '—';
        const number = Number(value);
        return Number.isFinite(number)
            ? number.toLocaleString('en-IN', { maximumFractionDigits: digits })
            : '—';
    };
    const formatPrice = value => value == null ? '—' : `₹${formatNumber(value)}`;
    const formatDate = value => {
        if (!value) return '';
        const parsed = new Date(value);
        return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString();
    };
    const chartCanvas = document.getElementById('stock-price-chart');
    const chartContext = chartCanvas.getContext('2d');
    const chartEmpty = document.getElementById('stock-chart-empty');
    const CHART_LEFT = 14;
    const CHART_RIGHT_GUTTER = 66;
    const MIN_VISIBLE_BARS = 20;
    const NIFTY_TOKEN = 256265;
    const SERIES_STYLE = {
        sma_20: ['#ffad60', 1.35], sma_50: ['#f0d06c', 1.2],
        sma_200: ['#ef7697', 1.2], ema_9: ['#4bbcff', 1.25],
    };
    const chartPrefs = {
        days: 180,
        interval: 'day',
        series: new Set(['sma_20', 'sma_50', 'sma_200', 'ema_9', 'levels']),
    };
    let chartState = null;
    let compareState = null;
    let chartLevels = [];
    let chartView = { start: 0, end: 0 };
    let chartHover = null;
    let chartDrag = null;
    let chartRequest = 0;

    function chartDate(value, options = { day: '2-digit', month: 'short' }) {
        const date = new Date(value);
        return Number.isNaN(date.getTime())
            ? String(value || '')
            : date.toLocaleDateString('en-IN', options);
    }

    function chartLine(ctx, values, xFor, yFor, color, width = 1.3) {
        ctx.beginPath();
        ctx.strokeStyle = color;
        ctx.lineWidth = width;
        let started = false;
        values.forEach((value, index) => {
            if (value == null || !Number.isFinite(Number(value))) {
                started = false;
                return;
            }
            const x = xFor(index);
            const y = yFor(Number(value));
            if (!started) {
                ctx.moveTo(x, y);
                started = true;
            } else {
                ctx.lineTo(x, y);
            }
        });
        ctx.stroke();
    }

    function visibleSlice(values) {
        return (values || []).slice(chartView.start, chartView.end);
    }

    // NIFTY closes rebased to the stock's first visible close, aligned by date.
    function compareSeries(candles) {
        if (!compareState) return [];
        const byDate = compareState;
        const first = candles.findIndex(candle => byDate.has(String(candle.timestamp).slice(0, 10)));
        if (first < 0) return [];
        const base = Number(candles[first].close) / byDate.get(String(candles[first].timestamp).slice(0, 10));
        return candles.map(candle => {
            const close = byDate.get(String(candle.timestamp).slice(0, 10));
            return close == null ? null : close * base;
        });
    }

    function drawChart() {
        if (!chartState || !chartCanvas.clientWidth) return;
        const rect = chartCanvas.getBoundingClientRect();
        const dpr = window.devicePixelRatio || 1;
        chartCanvas.width = Math.round(rect.width * dpr);
        chartCanvas.height = Math.round(rect.height * dpr);
        const ctx = chartContext;
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.clearRect(0, 0, rect.width, rect.height);

        const candles = visibleSlice(chartState.candles);
        const indicators = chartState.indicators || {};
        if (!candles.length) return;
        const show = key => chartPrefs.series.has(key);
        const overlays = Object.keys(SERIES_STYLE)
            .filter(show)
            .map(key => [key, visibleSlice(indicators[key])]);
        const bands = show('bb') ? [visibleSlice(indicators.bb_upper), visibleSlice(indicators.bb_lower)] : [];
        const compare = show('compare') ? compareSeries(candles) : [];

        const width = rect.width;
        const height = rect.height;
        const left = CHART_LEFT;
        const right = width - CHART_RIGHT_GUTTER;
        const plotWidth = right - left;
        const priceTop = 18;
        const priceBottom = height * .61;
        const volumeTop = height * .66;
        const volumeBottom = height * .77;
        const rsiTop = height * .83;
        const rsiBottom = height - 25;
        const slot = plotWidth / candles.length;
        const candleWidth = Math.max(1, Math.min(8, slot * .62));
        // Scale to candles plus every drawn overlay so nothing is clipped.
        const prices = [
            ...candles.flatMap(candle => [Number(candle.high), Number(candle.low)]),
            ...overlays.flatMap(([, values]) => values),
            ...bands.flat(),
            ...compare,
        ].filter(value => value != null && Number.isFinite(Number(value))).map(Number);
        const minPrice = Math.min(...prices);
        const maxPrice = Math.max(...prices);
        const pricePadding = (maxPrice - minPrice || maxPrice * .02 || 1) * .08;
        const priceMin = minPrice - pricePadding;
        const priceMax = maxPrice + pricePadding;
        const yPrice = value => priceBottom - ((value - priceMin) / (priceMax - priceMin)) * (priceBottom - priceTop);
        const priceAt = y => priceMax - ((y - priceTop) / (priceBottom - priceTop)) * (priceMax - priceMin);
        const xFor = index => left + slot * (index + .5);

        ctx.font = '9px "JetBrains Mono", monospace';
        ctx.textAlign = 'right';
        ctx.textBaseline = 'middle';
        for (let line = 0; line <= 4; line += 1) {
            const y = priceTop + (priceBottom - priceTop) * line / 4;
            const value = priceMax - (priceMax - priceMin) * line / 4;
            ctx.strokeStyle = 'rgba(148, 168, 184, .13)';
            ctx.beginPath();
            ctx.moveTo(left, y);
            ctx.lineTo(right, y);
            ctx.stroke();
            ctx.fillStyle = '#708492';
            ctx.fillText(formatNumber(value), width - 7, y);
        }

        if (bands.length) {
            chartLine(ctx, bands[0], xFor, yPrice, 'rgba(140, 170, 190, .55)', 1);
            chartLine(ctx, bands[1], xFor, yPrice, 'rgba(140, 170, 190, .55)', 1);
        }

        if (show('levels')) {
            ctx.setLineDash([5, 5]);
            ctx.font = '9px "JetBrains Mono", monospace';
            chartLevels
                .filter(level => level.price > priceMin && level.price < priceMax)
                .forEach(level => {
                    const y = yPrice(level.price);
                    const color = level.kind === 'support' ? 'rgba(37, 201, 150, .55)'
                        : level.kind === 'resistance' ? 'rgba(240, 93, 113, .55)' : 'rgba(200, 210, 220, .4)';
                    ctx.strokeStyle = color;
                    ctx.lineWidth = 1;
                    ctx.beginPath();
                    ctx.moveTo(left, y);
                    ctx.lineTo(right, y);
                    ctx.stroke();
                    ctx.fillStyle = color;
                    ctx.textAlign = 'left';
                    ctx.textBaseline = 'bottom';
                    ctx.fillText(`${level.label} ${formatNumber(level.price)}`, left + 4, y - 2);
                });
            ctx.setLineDash([]);
        }

        candles.forEach((candle, index) => {
            const x = xFor(index);
            const open = Number(candle.open);
            const close = Number(candle.close);
            const up = close >= open;
            const color = up ? '#25c996' : '#f05d71';
            ctx.strokeStyle = color;
            ctx.fillStyle = color;
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(x, yPrice(Number(candle.high)));
            ctx.lineTo(x, yPrice(Number(candle.low)));
            ctx.stroke();
            const top = Math.min(yPrice(open), yPrice(close));
            const bodyHeight = Math.max(1.4, Math.abs(yPrice(open) - yPrice(close)));
            ctx.fillRect(x - candleWidth / 2, top, candleWidth, bodyHeight);
        });
        overlays.forEach(([key, values]) => chartLine(ctx, values, xFor, yPrice, ...SERIES_STYLE[key]));
        if (compare.length) {
            ctx.setLineDash([2, 3]);
            chartLine(ctx, compare, xFor, yPrice, '#d9e4ea', 1.2);
            ctx.setLineDash([]);
        }

        const maxVolume = Math.max(...candles.map(candle => Number(candle.volume) || 0), 1);
        candles.forEach((candle, index) => {
            const x = xFor(index);
            const volume = Number(candle.volume) || 0;
            const barHeight = (volume / maxVolume) * (volumeBottom - volumeTop);
            ctx.fillStyle = Number(candle.close) >= Number(candle.open)
                ? 'rgba(37, 201, 150, .48)'
                : 'rgba(240, 93, 113, .48)';
            ctx.fillRect(x - candleWidth / 2, volumeBottom - barHeight, candleWidth, barHeight);
        });
        ctx.strokeStyle = 'rgba(148, 168, 184, .13)';
        ctx.beginPath();
        ctx.moveTo(left, volumeBottom);
        ctx.lineTo(right, volumeBottom);
        ctx.moveTo(left, rsiTop);
        ctx.lineTo(right, rsiTop);
        ctx.stroke();
        ctx.fillStyle = '#607784';
        ctx.textAlign = 'left';
        ctx.textBaseline = 'top';
        ctx.fillText('VOL', left + 2, volumeTop + 1);
        ctx.fillText('RSI 14', left + 2, rsiTop + 2);

        const rsi = visibleSlice(indicators.rsi_14);
        const yRsi = value => rsiBottom - (value / 100) * (rsiBottom - rsiTop);
        [30, 50, 70].forEach(level => {
            const y = yRsi(level);
            ctx.strokeStyle = level === 50 ? 'rgba(148,168,184,.1)' : 'rgba(255,173,96,.2)';
            ctx.setLineDash(level === 50 ? [] : [3, 4]);
            ctx.beginPath();
            ctx.moveTo(left, y);
            ctx.lineTo(right, y);
            ctx.stroke();
        });
        ctx.setLineDash([]);
        chartLine(ctx, rsi, xFor, yRsi, '#b891ff', 1.4);
        ctx.textAlign = 'right';
        ctx.textBaseline = 'middle';
        ctx.fillStyle = '#6e8190';
        [70, 30].forEach(level => ctx.fillText(String(level), width - 7, yRsi(level)));

        // ~1 date label per 110px, always including both ends
        const labelCount = Math.max(2, Math.min(candles.length, Math.floor(plotWidth / 110)));
        const dateOptions = chartPrefs.interval === 'week' || chartView.end - chartView.start > 300
            ? { month: 'short', year: '2-digit' } : { day: '2-digit', month: 'short' };
        ctx.textBaseline = 'top';
        ctx.fillStyle = '#708492';
        for (let label = 0; label < labelCount; label += 1) {
            const index = Math.round(label * (candles.length - 1) / (labelCount - 1));
            ctx.textAlign = label === 0 ? 'left' : label === labelCount - 1 ? 'right' : 'center';
            ctx.fillText(chartDate(candles[index].timestamp, dateOptions), xFor(index), height - 17);
        }

        if (chartHover) {
            const index = Math.max(0, Math.min(candles.length - 1, chartHover.index));
            const candle = candles[index];
            const x = xFor(index);
            ctx.strokeStyle = 'rgba(210, 225, 235, .3)';
            ctx.setLineDash([3, 4]);
            ctx.beginPath();
            ctx.moveTo(x, priceTop);
            ctx.lineTo(x, rsiBottom);
            if (chartHover.y >= priceTop && chartHover.y <= priceBottom) {
                ctx.moveTo(left, chartHover.y);
                ctx.lineTo(right, chartHover.y);
            }
            ctx.stroke();
            ctx.setLineDash([]);
            if (chartHover.y >= priceTop && chartHover.y <= priceBottom) {
                // price label on the axis under the crosshair
                ctx.fillStyle = '#2a3b48';
                ctx.fillRect(right + 2, chartHover.y - 8, CHART_RIGHT_GUTTER - 4, 16);
                ctx.fillStyle = '#e6f0f5';
                ctx.textAlign = 'right';
                ctx.textBaseline = 'middle';
                ctx.fillText(formatNumber(priceAt(chartHover.y)), width - 7, chartHover.y);
            }
            const absoluteIndex = chartView.start + index;
            const rsiValue = indicators.rsi_14?.[absoluteIndex];
            const tooltip = [
                chartDate(candle.timestamp, { weekday: 'short', day: '2-digit', month: 'short', year: 'numeric' }),
                `O ${formatNumber(candle.open)}   H ${formatNumber(candle.high)}`,
                `L ${formatNumber(candle.low)}   C ${formatNumber(candle.close)}`,
                `VOL ${formatNumber(candle.volume, 0)}${rsiValue == null ? '' : `   RSI ${formatNumber(rsiValue, 1)}`}`,
            ];
            const boxWidth = 200;
            const boxHeight = 67;
            const boxX = Math.min(Math.max(left, x + 10), right - boxWidth);
            const boxY = priceTop + 5;
            ctx.fillStyle = 'rgba(8, 16, 24, .94)';
            ctx.strokeStyle = 'rgba(128, 156, 176, .35)';
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.roundRect(boxX, boxY, boxWidth, boxHeight, 5);
            ctx.fill();
            ctx.stroke();
            ctx.textAlign = 'left';
            ctx.textBaseline = 'top';
            tooltip.forEach((line, lineIndex) => {
                ctx.fillStyle = lineIndex === 0 ? '#dbe8ef' : '#91a5b2';
                ctx.fillText(line, boxX + 9, boxY + 7 + lineIndex * 14);
            });
        }
    }

    let drawQueued = false;
    function requestDraw() {
        if (drawQueued) return;
        drawQueued = true;
        requestAnimationFrame(() => {
            drawQueued = false;
            drawChart();
        });
    }

    function renderChartTable() {
        const details = document.getElementById('stock-chart-data');
        if (!details.open || !chartState) return;
        const candles = chartState.candles;
        const rsi = chartState.indicators?.rsi_14 || [];
        document.getElementById('stock-chart-table').innerHTML = `<table>
            <thead><tr><th>Date</th><th>Open</th><th>High</th><th>Low</th><th>Close</th><th>Volume</th><th>RSI 14</th></tr></thead>
            <tbody>${candles.map((candle, index) => ({ candle, index })).reverse().map(({ candle, index }) => `<tr>
                <th>${esc(chartDate(candle.timestamp, { day: '2-digit', month: 'short', year: 'numeric' }))}</th>
                <td>${formatNumber(candle.open)}</td><td>${formatNumber(candle.high)}</td>
                <td>${formatNumber(candle.low)}</td><td>${formatNumber(candle.close)}</td>
                <td>${formatNumber(candle.volume, 0)}</td><td>${formatNumber(rsi[index], 1)}</td>
            </tr>`).join('')}</tbody></table>`;
    }

    const rangeLabel = days => ({
        30: '1 month', 90: '3 months', 180: '6 months', 365: '1 year', 730: '2 years', 1825: '5 years',
    })[days] || `${days} days`;

    async function loadChart() {
        const requestId = ++chartRequest;
        const { days, interval } = chartPrefs;
        chartHover = null;
        chartEmpty.hidden = false;
        chartEmpty.textContent = 'Loading historical candles…';
        document.getElementById('stock-chart-range-label').textContent =
            `${rangeLabel(days)} · ${interval === 'week' ? 'weekly' : 'daily'} candles`;
        try {
            const query = `interval=${interval}&days=${days}&include_indicators=true`;
            const data = await getJson(`/api/chart/historical/${encodedSymbol}?exchange=NSE&${query}`);
            if (requestId !== chartRequest) return;
            if (!Array.isArray(data.candles) || !data.candles.length) {
                throw new Error('No historical candles are available for this symbol and period.');
            }
            chartState = data;
            chartView = { start: 0, end: data.candles.length };
            chartEmpty.hidden = true;
            const last = data.candles[data.candles.length - 1];
            document.getElementById('stock-chart-as-of').textContent =
                `Last candle ${chartDate(last.timestamp, { day: '2-digit', month: 'short', year: 'numeric' })} · fetched ${formatDate(data.as_of)}`;
            if (chartPrefs.series.has('compare')) await loadCompare(requestId);
            renderChartTable();
            requestDraw();
        } catch (error) {
            if (requestId !== chartRequest) return;
            chartState = null;
            chartEmpty.hidden = false;
            chartEmpty.textContent = `Chart unavailable: ${error.message}`;
            document.getElementById('stock-chart-as-of').textContent = '';
        }
    }

    async function loadCompare(requestId = chartRequest) {
        try {
            const { days, interval } = chartPrefs;
            const data = await getJson(`/api/chart/historical?token=${NIFTY_TOKEN}&interval=${interval}&days=${days}`);
            if (requestId !== chartRequest) return;
            compareState = new Map((data.candles || []).map(candle => [String(candle.timestamp).slice(0, 10), Number(candle.close)]));
        } catch {
            compareState = null;
        }
    }

    function setRange(days) {
        chartPrefs.days = days;
        document.querySelectorAll('.stock-range-btn[data-days]').forEach(item =>
            item.classList.toggle('active', Number(item.dataset.days) === days));
        loadChart();
    }

    function setInterval_(interval) {
        chartPrefs.interval = interval;
        document.querySelectorAll('.stock-interval-btn').forEach(item =>
            item.classList.toggle('active', item.dataset.interval === interval));
        loadChart();
    }

    async function toggleSeries(series) {
        const on = !chartPrefs.series.has(series);
        if (on) chartPrefs.series.add(series);
        else chartPrefs.series.delete(series);
        document.querySelectorAll(`.stock-series-btn[data-series="${series}"]`).forEach(button =>
            button.setAttribute('aria-pressed', String(on)));
        if (series === 'compare' && on && !compareState) await loadCompare();
        requestDraw();
    }

    function hoverIndexAt(clientX) {
        const bounds = chartCanvas.getBoundingClientRect();
        const slot = (bounds.width - CHART_RIGHT_GUTTER - CHART_LEFT) / (chartView.end - chartView.start);
        return Math.floor((clientX - bounds.left - CHART_LEFT) / slot);
    }

    function zoomChart(factor, anchorIndex) {
        const total = chartState.candles.length;
        const span = chartView.end - chartView.start;
        const nextSpan = Math.max(Math.min(MIN_VISIBLE_BARS, total), Math.min(total, Math.round(span * factor)));
        const anchor = chartView.start + Math.max(0, Math.min(span - 1, anchorIndex));
        const ratio = span > 1 ? (anchor - chartView.start) / span : .5;
        let start = Math.round(anchor - ratio * nextSpan);
        start = Math.max(0, Math.min(total - nextSpan, start));
        chartView = { start, end: start + nextSpan };
        requestDraw();
    }

    function bindChart() {
        document.querySelectorAll('.stock-range-btn[data-days]').forEach(button => {
            button.addEventListener('click', () => setRange(Number(button.dataset.days)));
        });
        document.querySelectorAll('.stock-interval-btn').forEach(button => {
            button.addEventListener('click', () => setInterval_(button.dataset.interval));
        });
        document.querySelectorAll('.stock-series-btn').forEach(button => {
            button.addEventListener('click', () => toggleSeries(button.dataset.series));
        });
        document.getElementById('stock-chart-data').addEventListener('toggle', renderChartTable);
        chartCanvas.addEventListener('wheel', event => {
            if (!chartState?.candles?.length) return;
            event.preventDefault();
            zoomChart(event.deltaY > 0 ? 1.15 : 1 / 1.15, hoverIndexAt(event.clientX));
        }, { passive: false });
        chartCanvas.addEventListener('pointerdown', event => {
            if (!chartState?.candles?.length) return;
            chartDrag = { x: event.clientX, view: { ...chartView } };
            chartCanvas.setPointerCapture(event.pointerId);
        });
        chartCanvas.addEventListener('pointerup', event => {
            chartDrag = null;
            if (chartCanvas.hasPointerCapture(event.pointerId)) chartCanvas.releasePointerCapture(event.pointerId);
        });
        chartCanvas.addEventListener('pointermove', event => {
            if (!chartState?.candles?.length) return;
            const bounds = chartCanvas.getBoundingClientRect();
            if (chartDrag) {
                const span = chartDrag.view.end - chartDrag.view.start;
                const slot = (bounds.width - CHART_RIGHT_GUTTER - CHART_LEFT) / span;
                const shift = Math.round((chartDrag.x - event.clientX) / slot);
                const start = Math.max(0, Math.min(chartState.candles.length - span, chartDrag.view.start + shift));
                chartView = { start, end: start + span };
            }
            const span = chartView.end - chartView.start;
            chartHover = {
                index: Math.max(0, Math.min(span - 1, hoverIndexAt(event.clientX))),
                y: event.clientY - bounds.top,
            };
            requestDraw();
        });
        chartCanvas.addEventListener('pointerleave', () => {
            chartHover = null;
            requestDraw();
        });
        chartCanvas.addEventListener('dblclick', () => {
            if (!chartState) return;
            chartView = { start: 0, end: chartState.candles.length };
            requestDraw();
        });
        window.addEventListener('resize', requestDraw);
    }

    function bindKeyboard() {
        const form = document.getElementById('stock-symbol-form');
        const input = document.getElementById('stock-symbol-input');
        form.addEventListener('submit', event => {
            event.preventDefault();
            const next = input.value.trim().toUpperCase();
            if (/^[A-Z0-9&.-]{1,32}$/.test(next)) window.location.assign(`/app/stock/${encodeURIComponent(next)}`);
        });
        const ranges = [30, 90, 180, 365, 730, 1825];
        document.addEventListener('keydown', event => {
            if (event.ctrlKey || event.metaKey || event.altKey) return;
            if (event.target.closest('input, textarea, select, [contenteditable="true"]')) {
                if (event.key === 'Escape') event.target.blur();
                return;
            }
            const key = event.key.toLowerCase();
            if (key === '/') {
                event.preventDefault();
                input.focus();
            } else if (/^[1-6]$/.test(key)) {
                setRange(ranges[Number(key) - 1]);
            } else if (key === 'd' || key === 'w') {
                setInterval_(key === 'w' ? 'week' : 'day');
            } else if (key === 'n') {
                toggleSeries('compare');
            }
        });
    }

    // ── Live quote ────────────────────────────────────────────
    const QUOTE_POLL_MS = 15000;

    function marketIsOpen(now = new Date()) {
        const ist = new Date(now.toLocaleString('en-US', { timeZone: 'Asia/Kolkata' }));
        const minutes = ist.getHours() * 60 + ist.getMinutes();
        return ist.getDay() >= 1 && ist.getDay() <= 5 && minutes >= 9 * 60 + 15 && minutes <= 15 * 60 + 30;
    }

    async function loadQuote() {
        const el = document.getElementById('stock-live-quote');
        const key = `NSE:${symbol}`;
        try {
            const data = await getJson(`/api/market/quote?instruments=${encodeURIComponent(key)}`);
            const quote = data[key];
            if (!quote || quote.last_price == null) throw new Error('No quote returned');
            const prevClose = Number(quote.ohlc?.close);
            const last = Number(quote.last_price);
            const change = Number.isFinite(prevClose) && prevClose > 0 ? last - prevClose : null;
            const changePct = change == null ? null : change / prevClose * 100;
            const tone = change == null ? '' : change >= 0 ? 'positive' : 'negative';
            const open = marketIsOpen();
            el.innerHTML = `
                <strong class="stock-live-price">${formatPrice(last)}</strong>
                ${change == null ? '' : `<span class="${tone}">${change >= 0 ? '+' : ''}${formatNumber(change)} (${change >= 0 ? '+' : ''}${formatNumber(changePct)}%)</span>`}
                <span>Day ${formatPrice(quote.day_low ?? quote.ohlc?.low)} – ${formatPrice(quote.day_high ?? quote.ohlc?.high)}</span>
                <span>Vol ${formatNumber(quote.volume, 0)}</span>
                <span class="stock-live-state ${open ? 'open' : ''}">${open ? 'LIVE' : 'MARKET CLOSED'} · ${esc(formatDate(quote.last_trade_time || quote.timestamp) || 'time unavailable')}</span>`;
        } catch (error) {
            el.innerHTML = `<span class="stock-live-state">Live quote unavailable: ${esc(error.message)}</span>`;
        }
    }

    function startQuotePolling() {
        loadQuote();
        const tick = () => {
            setTimeout(async () => {
                if (!document.hidden && marketIsOpen()) await loadQuote();
                tick();
            }, QUOTE_POLL_MS);
        };
        tick();
        document.addEventListener('visibilitychange', () => {
            if (!document.hidden && marketIsOpen()) loadQuote();
        });
    }

    document.getElementById('screener-link').href = screenerUrl;
    const nseCompanyUrl = `https://www.nseindia.com/get-quotes/equity?symbol=${encodedSymbol}`;
    const nseAnnouncementsUrl = `https://www.nseindia.com/companies-listing/corporate-filings-announcements?symbol=${encodedSymbol}`;
    const nseAnnualReportsUrl = 'https://www.nseindia.com/companies-listing/corporate-filings-annual-reports';
    const bseAnnualReportsUrl = 'https://www.bseindia.com/corporates/AnnualReports.aspx?flag=1';
    document.getElementById('nse-company-link').href = nseCompanyUrl;

    async function getJson(url) {
        const response = await fetch(url, {
            credentials: 'same-origin',
            headers: { Accept: 'application/json' },
        });
        const body = await response.json();
        if (!response.ok) throw new Error(body.detail || body.error || `Request failed (${response.status})`);
        if (body?.error) throw new Error(body.error);
        return body;
    }

    function sectionError(id, message, fallback = true) {
        const el = document.getElementById(id);
        if (!el) return;
        el.innerHTML = `<div class="stock-empty">${esc(message)}</div>` +
            (fallback ? `<p class="stock-section-note"><a href="${esc(screenerUrl)}" target="_blank" rel="noopener noreferrer">View ${esc(symbol)} on Screener.in</a></p>` : '');
    }

    function sourceLinks(links) {
        return `<nav class="stock-source-links" aria-label="External data sources">${links.map(([label, url]) => `
            <a class="stock-source-link" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(label)} <span aria-hidden="true">↗</span></a>
        `).join('')}</nav>`;
    }

    function metricGrid(entries) {
        if (!entries.length) return '<div class="stock-empty">No metrics returned by the source.</div>';
        return `<div class="stock-metric-grid">${entries.map(([label, value]) => `
            <div class="stock-metric">
                <div class="stock-metric-label">${esc(label)}</div>
                <div class="stock-metric-value">${display(value)}</div>
            </div>`).join('')}</div>`;
    }

    function usableFinancialValue(value) {
        if (value == null) return false;
        const normalized = String(value).trim();
        return Boolean(normalized && /[\dA-Za-z]/.test(normalized) && !/^(?:n\/?a|not available)$/i.test(normalized));
    }

    function renderFinancialHighlights(highlights, sourceError = '') {
        const ignored = new Set([
            'company_name', 'pros', 'cons', 'quarterly_results', 'profit_loss',
            'balance_sheet', 'cash_flow', 'shareholding_pattern', 'peers',
            'basis', 'source_url', 'derived', 'unit',
        ]);
        const ratios = Object.entries(highlights || {})
            .filter(([key, value]) => !ignored.has(key) && (typeof value !== 'object' || value === null) && usableFinancialValue(value));
        const summaryKeys = [
            'Market Cap', 'Current Price', 'High / Low', 'Stock P/E', 'Book Value',
            'Dividend Yield', 'ROCE', 'ROE',
        ];
        const summary = summaryKeys
            .filter(key => highlights && usableFinancialValue(highlights[key]))
            .map(key => [key, highlights[key]]);
        const summaryKeySet = new Set(summary.map(([key]) => key));
        const additionalRatios = ratios.filter(([key]) => !summaryKeySet.has(key));
        document.getElementById('fundamental-ratios').innerHTML = additionalRatios.length
            ? `<div class="stock-table-wrap"><table><tbody>${additionalRatios.map(([key, value]) =>
                `<tr><th>${esc(key)}</th><td>${display(value)}</td></tr>`).join('')}</tbody></table></div>`
            : `<div class="stock-empty">${esc(ratios.length
                ? 'No additional headline metrics were returned.'
                : sourceError || `Screener.in did not return ratio data for ${symbol}.`)}</div>`;
        document.getElementById('fundamentals-summary').innerHTML = summary.length
            ? metricGrid(summary)
            : `<div class="stock-empty">${esc(sourceError || 'No financial highlights returned by the source.')}</div>`;

        const pros = Array.isArray(highlights?.pros) ? highlights.pros : [];
        const cons = Array.isArray(highlights?.cons) ? highlights.cons : [];
        if (pros.length || cons.length) {
            const list = values => values.length
                ? `<ul class="stock-list">${values.map(item => `<li>${esc(item)}</li>`).join('')}</ul>`
                : '<div class="stock-empty">No items returned.</div>';
            document.getElementById('fundamental-ratios').innerHTML += `
                <div class="stock-subheading">Screener pros</div>${list(pros)}
                <div class="stock-subheading">Screener concerns</div>${list(cons)}`;
        }
    }

    let financialBasis = '';

    function signedPercent(value) {
        if (value == null || !Number.isFinite(Number(value))) return '<span class="stock-nm" title="Not meaningful (missing data or a loss/profit flip)">n.m.</span>';
        const number = Number(value);
        return `<span class="${number >= 0 ? 'positive' : 'negative'}">${number >= 0 ? '+' : ''}${formatNumber(number, 1)}%</span>`;
    }

    function renderGrowth(derived, sourceError = '') {
        const el = document.getElementById('fundamental-growth');
        const blocks = [];
        if (derived?.quarterly?.metrics?.length) {
            const q = derived.quarterly;
            blocks.push(`<div class="stock-growth-block">
                <h3>Latest quarter · ${esc(q.period)}</h3>
                <div class="stock-table-wrap"><table>
                    <thead><tr><th>Metric</th><th>Value${q.unit ? ` (${esc(q.unit)})` : ''}</th><th>QoQ</th><th>YoY</th></tr></thead>
                    <tbody>${q.metrics.map(row => `<tr><th>${esc(row.metric)}</th><td>${formatNumber(row.latest)}</td>
                        <td>${signedPercent(row.qoq_pct)}</td><td>${signedPercent(row.yoy_pct)}</td></tr>`).join('')}</tbody>
                </table></div></div>`);
        }
        if (derived?.annual?.metrics?.length) {
            const a = derived.annual;
            blocks.push(`<div class="stock-growth-block">
                <h3>Latest fiscal year · ${esc(a.period)}</h3>
                <div class="stock-table-wrap"><table>
                    <thead><tr><th>Metric</th><th>Value${a.unit ? ` (${esc(a.unit)})` : ''}</th><th>YoY</th><th>3Y CAGR</th><th>5Y CAGR</th></tr></thead>
                    <tbody>${a.metrics.map(row => `<tr><th>${esc(row.metric)}</th><td>${formatNumber(row.latest)}</td>
                        <td>${signedPercent(row.yoy_pct)}</td><td>${signedPercent(row.cagr_3y_pct)}</td><td>${signedPercent(row.cagr_5y_pct)}</td></tr>`).join('')}</tbody>
                </table></div></div>`);
        }
        const margins = derived?.margins;
        if (margins?.periods?.length) {
            const marginCell = value => value == null ? '—' : `${formatNumber(value, 1)}%`;
            blocks.push(`<div class="stock-growth-block">
                <h3>Margin trend</h3>
                <div class="stock-table-wrap"><table>
                    <thead><tr><th>Margin</th>${[...margins.periods].reverse().map(period => `<th>${esc(period)}</th>`).join('')}</tr></thead>
                    <tbody>
                        <tr><th>Operating margin</th>${[...margins.operating_margin_pct].reverse().map(value => `<td>${marginCell(value)}</td>`).join('')}</tr>
                        <tr><th>Net margin</th>${[...margins.net_margin_pct].reverse().map(value => `<td>${marginCell(value)}</td>`).join('')}</tr>
                    </tbody>
                </table></div></div>`);
        }
        el.classList.remove('stock-section-loading');
        el.innerHTML = blocks.length
            ? `<div class="stock-growth-grid">${blocks.join('')}</div>
               <p class="stock-section-note">Computed from the reported statements below; n.m. = not meaningful (missing period or a loss/profit flip).</p>`
            : `<div class="stock-empty">${esc(sourceError || 'Not enough reported periods to compute growth.')}</div>`;
    }

    function statementPeriodOrder(periods, valueCount = periods.length) {
        const monthNumbers = {
            jan: 1, feb: 2, mar: 3, apr: 4, may: 5, jun: 6,
            jul: 7, aug: 8, sep: 9, oct: 10, nov: 11, dec: 12,
        };
        const datedPeriods = periods.map((period, index) => {
            const normalized = String(period || '').trim().toLowerCase();
            if (/\bttm\b/.test(normalized)) {
                return { index, year: Number.POSITIVE_INFINITY, month: 12 };
            }
            const year = normalized.match(/\b(19|20)\d{2}\b/);
            if (!year) return { index, year: null, month: 0 };
            const monthName = normalized.match(/\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b/);
            return {
                index,
                year: Number(year[0]),
                month: monthName ? monthNumbers[monthName[1]] : 12,
            };
        });
        const datedCount = datedPeriods.filter(period => period.year !== null).length;
        const order = datedCount > 1
            ? [
                ...datedPeriods.filter(period => period.year !== null)
                    .sort((left, right) => right.year - left.year || right.month - left.month)
                    .map(period => period.index),
                ...datedPeriods.filter(period => period.year === null).map(period => period.index),
            ]
            : periods.map((_period, index) => index);
        for (let index = periods.length; index < valueCount; index += 1) {
            order.push(index);
        }
        return order;
    }

    function renderStatement(id, statement, fallbackName, sourceError = '') {
        if (!statement?.rows?.length) {
            sectionError(id, sourceError || `${fallbackName} are unavailable from the current Screener.in response.`);
            return;
        }
        const periods = statement.periods || [];
        const maxValueCount = Math.max(periods.length, ...statement.rows.map(row => row.values?.length || 0));
        const periodOrder = statementPeriodOrder(periods, maxValueCount);
        const orderedPeriods = periodOrder.map(index => periods[index] || '');
        const hasDatedPeriods = periods.some(period => /\b(?:(?:19|20)\d{2}|ttm)\b/i.test(String(period)));
        const head = ['Metric', ...orderedPeriods].map(item => `<th>${esc(item)}</th>`).join('');
        const rows = statement.rows.map(row => {
            const values = periodOrder.map(index => row.values?.[index] ?? '');
            return `<tr>
                <th>${esc(row.metric)}</th>
                ${values.map(value => `<td>${display(value)}</td>`).join('')}
            </tr>`;
        }).join('');
        document.getElementById(id).innerHTML = `<div class="stock-table-wrap"><table>
            <thead><tr>${head}</tr></thead><tbody>${rows}</tbody>
        </table></div>${statement.unit
            ? `<p class="stock-table-unit">Figures in ${esc(statement.unit)}${financialBasis ? ` · ${esc(financialBasis)}` : ''}</p>`
            : ''}${hasDatedPeriods
            ? '<p class="stock-table-period-note">Newest reported period first; scroll horizontally for older periods.</p>'
            : ''}`;
    }

    function renderNews(research) {
        const articles = research?.latest_news?.articles || [];
        if (!articles.length) {
            const message = research?.latest_news?.error
                ? `News source error: ${research.latest_news.error}`
                : 'No recent news or NSE announcements were returned.';
            sectionError('company-news', message);
            return;
        }
        const nseArticles = articles.filter(article => /nse/i.test(article.source || ''));
        const otherArticles = articles.filter(article => !/nse/i.test(article.source || ''));
        const renderList = list => list.length
            ? `<ul class="stock-list">${list.map(article => {
                const url = /^https:\/\//i.test(article.url || '') ? article.url : '';
                const title = esc(article.title || 'Company update');
                return `<li>${url
                    ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${title}</a>`
                    : title}
                    <div class="stock-section-note">${esc(article.source || 'News source')}${article.published ? ` · ${esc(article.published)}` : ''}</div>
                </li>`;
            }).join('')}</ul>`
            : '<div class="stock-empty">No items returned.</div>';
        document.getElementById('company-news').innerHTML = `
            <div class="stock-subheading">NSE corporate announcements</div>
            ${renderList(nseArticles)}
            <div class="stock-subheading">Other company news</div>
            ${renderList(otherArticles)}
            <p class="stock-section-note">Fetched ${esc(formatDate(research?.latest_news?.fetched_at) || 'time unavailable')}</p>`;
    }

    function renderReports(research) {
        const reports = research?.annual_reports?.reports || [];
        if (!reports.length) {
            const message = research?.annual_reports?.error
                ? `Filing source error: ${research.annual_reports.error}`
                : 'No annual report links or filing documents were returned.';
            document.getElementById('company-reports').innerHTML = `
                <div class="stock-empty">${esc(message)}</div>
                <p class="stock-section-note">Open the exchange filing portals to search for ${esc(symbol)} reports and disclosures.</p>
                ${sourceLinks([
                    ['NSE annual reports', nseAnnualReportsUrl],
                    ['NSE announcements', nseAnnouncementsUrl],
                    ['BSE annual reports', bseAnnualReportsUrl],
                ])}`;
            return;
        }
        document.getElementById('company-reports').innerHTML = `<ul class="stock-list">${reports.map(report => {
            const url = /^https:\/\//i.test(report.url || '') ? report.url : '';
            const title = esc(report.title || `${symbol} report`);
            return `<li>${url
                ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${title}</a>`
                : title}
                <div class="stock-section-note">${esc(report.source || 'Filing source')}${report.year ? ` · ${esc(report.year)}` : ''}</div>
            </li>`;
        }).join('')}</ul>${sourceLinks([
            ['NSE annual reports', nseAnnualReportsUrl],
            ['NSE announcements', nseAnnouncementsUrl],
            ['BSE annual reports', bseAnnualReportsUrl],
        ])}`;
    }

    function renderPeers(statement, sourceError = '') {
        if (statement?.rows?.length) {
            renderStatement('peers', statement, 'Peer comparison', sourceError);
            return;
        }
        const message = sourceError
            ? `Peer comparison data could not be loaded: ${sourceError}`
            : 'The current Screener.in response did not include peer-comparison rows.';
        document.getElementById('peers').innerHTML = `
            <div class="stock-empty">${esc(message)}</div>
            <p class="stock-section-note">Open the company’s peer comparison on Screener.in or review its official exchange profile.</p>
            ${sourceLinks([
                ['Screener company & peers', `${screenerUrl}#peers`],
                ['NSE company quote', nseCompanyUrl],
                ['NSE announcements', nseAnnouncementsUrl],
                ['BSE annual reports', bseAnnualReportsUrl],
            ])}`;
    }

    function renderResearch(research) {
        const annual = research?.annual_reports || {};
        const highlights = annual.financial_highlights || {};
        const financialError = annual.financial_highlights_error || '';
        document.getElementById('stock-title').textContent = highlights.company_name || research?.latest_news?.company_name || symbol;
        document.title = `${symbol} — VerquoAI Stock Research`;
        document.getElementById('stock-subtitle').textContent =
            `NSE symbol ${symbol} · Screener.in financials · NSE filings · company news`;
        financialBasis = highlights.basis ? `${highlights.basis} figures` : '';
        const basisEl = document.getElementById('fundamentals-basis');
        if (highlights.basis) {
            const sourceUrl = /^https:\/\/www\.screener\.in\//.test(highlights.source_url || '') ? highlights.source_url : screenerUrl;
            document.getElementById('screener-link').href = sourceUrl;
            const unit = highlights.profit_loss?.unit || highlights.quarterly_results?.unit || '';
            basisEl.innerHTML = `<span class="stock-basis-badge">${esc(highlights.basis.toUpperCase())}</span>
                ${unit ? `<span>Statement figures in ${esc(unit)}</span>` : ''}
                <span>Fetched ${esc(formatDate(annual.fetched_at) || 'time unavailable')}</span>
                <a href="${esc(sourceUrl)}" target="_blank" rel="noopener noreferrer">Source page ↗</a>`;
        } else {
            basisEl.textContent = '';
        }
        renderFinancialHighlights(highlights, financialError);
        renderGrowth(highlights.derived, financialError);
        renderStatement('quarterly-results', highlights.quarterly_results, 'Quarterly results', financialError);
        renderStatement('profit-loss', highlights.profit_loss, 'Profit & loss statements', financialError);
        renderStatement('balance-sheet', highlights.balance_sheet, 'Balance-sheet statements', financialError);
        renderStatement('cash-flow', highlights.cash_flow, 'Cash-flow statements', financialError);
        renderStatement('shareholding-pattern', highlights.shareholding_pattern, 'Shareholding pattern', financialError);
        renderPeers(highlights.peers, financialError);
        renderNews(research);
        renderReports(research);
        const fetchedAt = research?.latest_news?.fetched_at || annual.fetched_at;
        document.getElementById('stock-data-attribution').textContent =
            `Financials: Screener.in · news and announcements: ${research?.latest_news?.sources_queried?.join(', ') || 'research sources'} · fetched ${formatDate(fetchedAt) || 'time unavailable'}`;
    }

    function renderTechnical(profile) {
        if (!profile || profile.error) {
            const message = profile?.error || 'Technical analysis data was not returned.';
            document.getElementById('technical-summary').innerHTML = `
                <div class="stock-empty">${esc(message)}</div>
                <p class="stock-section-note"><a href="/app" onclick="if (history.length > 1) { history.back(); return false; }">Return to workspace to check market data access.</a></p>`;
            return;
        }
        const verdict = profile.verdict || {};
        const stage = textValue(profile.stage_name) || (profile.stage != null ? `Stage ${formatNumber(profile.stage, 0)}` : 'Unavailable');
        const action = textValue(verdict.action) || textValue(verdict.recommendation) || 'Technical read unavailable';
        const actionClass = /bull|buy|outperform/i.test(action) ? 'bullish'
            : /bear|sell|underperform/i.test(action) ? 'bearish' : 'neutral';
        const marketContext = profile.market_context || {};
        const sectorContext = profile.sector_context || {};
        const marketStatus = textValue(marketContext.status) || textValue(marketContext.trend) || 'unavailable';
        const sectorStatus = textValue(sectorContext.status) || textValue(sectorContext.trend) || 'unavailable';
        const sectorName = textValue(sectorContext.sector) || textValue(profile.sector) || 'SECTOR';
        const recommendation = textValue(verdict.advice);
        const contextCaution = textValue(verdict.context_note);
        const gauges = [
            ['Trend score', formatNumber(profile.trend_score, 0), 'out of 100'],
            ['Relative strength', formatNumber(profile.rs_rating, 0), 'RS rating'],
            ['Trend stage', stage, 'market structure'],
        ];
        const indicators = profile.technical_indicators || profile;
        const accumulation = profile.accumulation;
        const asPercent = value => {
            const formatted = formatNumber(value);
            return formatted === '—' ? '' : `${formatted}%`;
        };
        const indicatorGroups = [
            ['Moving averages', [
                ['SMA · 20', 'sma_20', 'price'], ['SMA · 50', 'sma_50', 'price'],
                ['SMA · 150', 'sma_150', 'price'], ['SMA · 200', 'sma_200', 'price'],
                ['EMA · 9', 'ema_9', 'price'], ['EMA · 12', 'ema_12', 'price'],
                ['EMA · 21', 'ema_21', 'price'], ['EMA · 26', 'ema_26', 'price'],
            ]],
            ['Momentum & MACD', [
                ['RSI · 14', 'rsi_14', 'number'], ['Stochastic · %K', 'stoch_k', 'number'],
                ['Stochastic · %D', 'stoch_d', 'number'], ['Williams %R', 'williams_r', 'number'],
                ['CCI · 20', 'cci_20', 'number'], ['ROC · 12', 'roc_12', 'percent'],
                ['MACD line', 'macd_line', 'number'], ['MACD signal', 'macd_signal', 'number'],
                ['MACD histogram', 'macd_histogram', 'number'],
            ]],
            ['Trend & Ichimoku', [
                ['ADX · 14', 'adx_14', 'number'], ['+DI', 'plus_di', 'number'],
                ['−DI', 'minus_di', 'number'], ['Ichimoku signal', 'ichimoku_signal', 'text'],
                ['Tenkan-sen', 'ichimoku_tenkan', 'price'], ['Kijun-sen', 'ichimoku_kijun', 'price'],
                ['Cloud top', 'ichimoku_cloud_top', 'price'], ['Cloud bottom', 'ichimoku_cloud_bot', 'price'],
                ['20-bar regression slope', 'lr_slope_20', 'number'],
            ]],
            ['Volatility & bands', [
                ['ATR · 14', 'atr_14', 'price'], ['Bollinger upper', 'bb_upper', 'price'],
                ['Bollinger middle', 'bb_middle', 'price'], ['Bollinger lower', 'bb_lower', 'price'],
                ['Band width', 'bb_bandwidth', 'percent'], ['Historical volatility', 'hist_volatility', 'percent'],
            ]],
            ['Volume & money flow', [
                ['Volume vs average', 'vol_ratio', 'multiple'], ['OBV trend', 'obv_trend', 'text'],
                ['MFI · 14', 'mfi_14', 'number'],
            ]],
            ['Returns & trading range', [
                ['1 day', 'change_1d', 'percent'], ['5 days', 'change_5d', 'percent'],
                ['20 sessions', 'change_20d', 'percent'], ['~3 months · 63 sessions', 'change_60d', 'percent'],
                ['~6 months · 126 sessions', 'change_120d', 'percent'], ['~1 year · 252 sessions', 'change_250d', 'percent'],
                ['52-week high', 'high_52w', 'price'], ['52-week low', 'low_52w', 'price'],
                ['Distance from 52-week high', 'dist_from_52w_high', 'percent'],
                ['Distance from 52-week low', 'dist_from_52w_low', 'percent'],
                ['Data-history high', 'all_time_high', 'price'], ['Data-history low', 'all_time_low', 'price'],
                ['Distance from data-history high', 'dist_from_all_time_high', 'percent'],
                ['Distance from data-history low', 'dist_from_all_time_low', 'percent'],
            ]],
        ];
        const historyBars = Number(profile.history_bars) || 0;
        const minimumBarsForReturn = { change_1d: 2, change_5d: 6, change_20d: 21, change_60d: 64, change_120d: 127, change_250d: 253 };
        const techValue = (key, kind) => {
            const value = indicators[key];
            if (value == null || value === '' || (key in minimumBarsForReturn && historyBars && historyBars < minimumBarsForReturn[key])) return '';
            if (kind === 'price') return formatPrice(value);
            if (kind === 'percent') return asPercent(value);
            if (kind === 'multiple') return `${formatNumber(value)}×`;
            return textValue(value) || formatNumber(value);
        };
        // Only signed readings (returns, momentum) get a gain/loss colour;
        // widths, volatility and distances from highs/lows are not good or bad by sign.
        const signedToneKeys = new Set([
            'change_1d', 'change_5d', 'change_20d', 'change_60d', 'change_120d', 'change_250d',
            'roc_12', 'macd_histogram',
        ]);
        const renderRows = rows => rows
            .map(([label, key, kind]) => {
                const value = techValue(key, kind);
                if (!value || value === '—') return '';
                const numericValue = Number(indicators[key]);
                const tone = Number.isFinite(numericValue) && signedToneKeys.has(key)
                    ? numericValue >= 0 ? 'positive' : 'negative'
                    : '';
                return `<div class="stock-tech-row"><span>${esc(label)}</span><strong class="${tone}">${esc(value)}</strong></div>`;
            })
            .filter(Boolean).join('');
        const technicalSections = indicatorGroups.map(([title, rows]) => {
            const content = renderRows(rows);
            return content
                ? `<section class="stock-tech-group"><h3>${esc(title)}</h3><div class="stock-tech-table">${content}</div></section>`
                : '';
        }).filter(Boolean).join('');
        const timeframeRows = [
            ['Daily', profile.daily_trend],
            ['Weekly', profile.weekly_trend],
            ['Monthly', profile.monthly_trend],
        ].map(([label, data]) => {
            if (!data || typeof data !== 'object') return '';
            const values = (label === 'Daily'
                ? [
                    ['1-day change', data.change_1d, 'percent'],
                    ['5-day change', data.change_5d, 'percent'],
                    ['RSI', data.rsi, 'number'],
                    ['MACD histogram', data.macd_histogram, 'number'],
                ]
                : [
                    ['Trend', data.trend || data.status, 'text'],
                    ['Strength', data.strength, 'number'],
                    ['Change', data.change_pct, 'percent'],
                    ['Last close', data.last_close, 'price'],
                    ['Bars', data.bars, 'number'],
                ]).map(([name, value, kind]) => {
                if (value == null || value === '') return '';
                const formatted = kind === 'price' ? formatPrice(value)
                    : kind === 'percent' ? asPercent(value)
                        : kind === 'number' ? formatNumber(value, 0) : textValue(value);
                return formatted ? `<span>${esc(name)} <strong>${esc(formatted)}</strong></span>` : '';
            }).filter(Boolean);
            return values.length
                ? `<div class="stock-timeframe-row"><strong>${esc(label)}</strong>${values.join('')}</div>`
                : '';
        }).filter(Boolean).join('');
        const superPerformance = profile.super_performance;
        const accumulationSignals = accumulation && typeof accumulation === 'object'
            ? (Array.isArray(accumulation.signals) ? accumulation.signals : [])
                .filter(signal => typeof signal === 'string' && signal.trim())
            : [];
        const patternRows = [
            ['Relative strength rating', profile.rs_rating == null ? '' : formatNumber(profile.rs_rating, 0)],
            ['Trend-stage confidence', profile.stage_confidence == null ? '' : `${formatNumber(profile.stage_confidence, 0)}%`],
            ['VCP detected', profile.vcp_detected == null ? '' : profile.vcp_detected ? 'Yes' : 'No'],
            ['VCP tightness', profile.vcp_tightness == null ? '' : formatNumber(profile.vcp_tightness)],
            ['VCP pivot', profile.vcp_pivot == null ? '' : formatPrice(profile.vcp_pivot)],
            ['Pocket pivot', profile.pocket_pivot == null ? '' : profile.pocket_pivot ? 'Yes' : 'No'],
            ['Super-performance grade', superPerformance?.grade || ''],
            ['Super-performance checks', superPerformance?.score != null && superPerformance?.total != null
                ? `${formatNumber(superPerformance.score, 0)} / ${formatNumber(superPerformance.total, 0)}` : ''],
            ['Accumulation / distribution', accumulation?.status ? accumulation.status.replaceAll('_', ' ') : ''],
        ].filter(([, value]) => value);
        const superPerformanceChecks = superPerformance?.checks && typeof superPerformance.checks === 'object'
            ? Object.entries(superPerformance.checks).map(([name, passed]) => [
                name.replaceAll('_', ' '),
                passed ? 'Pass' : 'Not met',
            ])
            : [];
        const structuralRows = [
            ['Last price', profile.ltp, 'price'], ['Day high', profile.day_high, 'price'],
            ['Pivot point', profile.pivot_pp, 'price'], ['Resistance · R1', profile.pivot_r1, 'price'],
            ['Support · S1', profile.pivot_s1, 'price'],
        ];
        const structuralMarkup = structuralRows.map(([label, value, kind]) => {
            if (value == null || value === '') return '';
            const formatted = kind === 'price' ? formatPrice(value) : formatNumber(value);
            return formatted !== '—'
                ? `<div class="stock-tech-row"><span>${esc(label)}</span><strong>${esc(formatted)}</strong></div>`
                : '';
        }).filter(Boolean).join('');
        const support = profile.support_resistance;
        chartLevels = [
            ...(Array.isArray(support?.support) ? support.support.slice(0, 3).map(price => ({ label: 'S', price: Number(price), kind: 'support' })) : []),
            ...(Array.isArray(support?.resistance) ? support.resistance.slice(0, 3).map(price => ({ label: 'R', price: Number(price), kind: 'resistance' })) : []),
            ...[['PP', profile.pivot_pp], ['R1', profile.pivot_r1], ['S1', profile.pivot_s1]]
                .filter(([, price]) => price != null)
                .map(([label, price]) => ({ label, price: Number(price), kind: 'pivot' })),
        ].filter(level => Number.isFinite(level.price) && level.price > 0);
        requestDraw();
        const supportMarkup = support && typeof support === 'object'
            ? Object.entries(support).map(([key, levels]) => {
                const values = (Array.isArray(levels) ? levels : [levels])
                    .filter(value => value != null)
                    .slice(0, 3)
                    .map(formatPrice);
                return values.length
                    ? `<span>${esc(key)}: <strong>${values.map(esc).join(' / ')}</strong></span>`
                    : '';
            }).filter(Boolean).join(' · ')
            : '';
        const triggerLabels = (Array.isArray(profile.triggers) ? profile.triggers : [])
            .map(trigger => {
                if (typeof trigger === 'string') return trigger;
                if (!trigger || typeof trigger !== 'object') return '';
                const label = trigger.signal || trigger.label || trigger.message || trigger.reason || trigger.name;
                if (typeof label !== 'string' || !label.trim()) return '';
                const strength = textValue(trigger.strength);
                return strength
                    ? `${label.trim()} · ${strength.toUpperCase()}`
                    : label.trim();
            })
            .filter(Boolean);
        const verdictReasons = Array.isArray(verdict.reasons)
            ? verdict.reasons.filter(reason => typeof reason === 'string' && reason.trim())
            : [];
        const accumulationStatus = typeof accumulation === 'string'
            ? accumulation
            : accumulation && typeof accumulation === 'object' && typeof accumulation.status === 'string'
                ? accumulation.status
                : '';
        const accumulationLabel = /strong_accumulation|^accumulation$/i.test(accumulationStatus)
            ? 'Institutional accumulation'
            : /strong_distribution|^distribution$/i.test(accumulationStatus)
            ? 'Institutional distribution'
            : '';
        const signals = [...new Set([
            ...verdictReasons,
            ...(profile.vcp_detected ? ['Volatility contraction pattern detected'] : []),
            ...(accumulationLabel ? [accumulationLabel] : []),
            ...triggerLabels,
        ])];
        const contextClass = status => /bull/i.test(status) ? 'bullish'
            : /bear/i.test(status) ? 'bearish' : 'neutral';
        const patternMarkup = patternRows.length || superPerformanceChecks.length || accumulationSignals.length || accumulation?.status != null
            ? `${patternRows.length ? `<div class="stock-tech-table">${patternRows.map(([label, value]) =>
                `<div class="stock-tech-row"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`).join('')}</div>
                ` : ''}
                ${superPerformanceChecks.length
                    ? `<div class="stock-subheading">Super-performance criteria</div>
                        <div class="stock-tech-table">${superPerformanceChecks.map(([label, value]) =>
                            `<div class="stock-tech-row"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`).join('')}</div>`
                    : ''}
                ${accumulation?.status != null || accumulation?.score != null || accumulationSignals.length
                    ? `<div class="stock-accumulation-details">
                        ${accumulation?.status != null || accumulation?.score != null
                            ? `<div class="stock-accumulation-summary">
                                ${accumulation?.status != null ? `<span>Net read <strong>${esc(textValue(accumulation.status).replaceAll('_', ' '))}</strong></span>` : ''}
                                ${accumulation?.score != null ? `<span>Composite score <strong>${formatNumber(accumulation.score, 0)}</strong></span>` : ''}
                            </div>`
                            : ''}
                        ${accumulationSignals.length
                            ? `<ul class="stock-accumulation-signals">${accumulationSignals.map(signal => `<li>${esc(signal)}</li>`).join('')}</ul>`
                            : ''}
                        <p class="stock-accumulation-note">The net score combines OBV, MFI, volume-price behavior, pocket pivots and directional indicators. Individual signals can disagree.</p>
                    </div>`
                    : ''}`
            : '';
        const asOfNote = [
            profile.as_of ? `Indicators as of ${formatDate(profile.as_of)}` : 'Indicator time unavailable',
            profile.context_as_of ? `market & sector context from scan at ${formatDate(profile.context_as_of)}` : 'market & sector context: no universe scan yet',
        ].join(' · ');
        document.getElementById('technical-summary').innerHTML = `
            <div class="stock-as-of ${profile.stale ? 'stale' : ''}">${esc(asOfNote)}${profile.stale
                ? ` — <strong>refresh failed, showing the last good analysis</strong> (${esc(profile.refresh_error || 'unknown error')})`
                : ''}</div>
            <div class="stock-verdict-board">
                <div class="stock-verdict-primary">
                    <span class="stock-tech-label">SYSTEM VERDICT</span>
                    <strong class="stock-verdict-action ${actionClass}">${esc(action)}</strong>
                    <span class="stock-verdict-confidence">CONFIDENCE ${profile.verdict?.confidence == null ? '—' : `${formatNumber(profile.verdict.confidence, 0)}%`}</span>
                    ${verdict.bull_points != null && verdict.bear_points != null
                        ? `<span class="stock-verdict-points"><b class="positive">BULL ${formatNumber(verdict.bull_points, 0)}</b> · <b class="negative">BEAR ${formatNumber(verdict.bear_points, 0)}</b></span>`
                        : ''}
                </div>
                <div class="stock-verdict-context">
                    <div class="stock-context-pills">
                        <span class="stock-context-pill ${contextClass(marketStatus)}">MARKET <b>${esc(marketStatus.toUpperCase())}</b></span>
                        <span class="stock-context-pill ${contextClass(sectorStatus)}">${esc(sectorName.toUpperCase())} <b>${esc(sectorStatus.toUpperCase())}</b></span>
                        <span class="stock-context-pill ${actionClass}">STOCK <b>${esc(action.toUpperCase())}</b></span>
                    </div>
                    <div class="stock-context-footnote">${esc(contextCaution
                        ? `${contextCaution.charAt(0).toUpperCase()}${contextCaution.slice(1)}.`
                        : 'Verdict combines market, sector and daily / weekly / monthly stock structure.')}</div>
                    ${recommendation ? `<div class="stock-primary-recommendation"><span>PRIMARY RECOMMENDATION</span><strong>${esc(recommendation)}</strong></div>` : ''}
                </div>
            </div>
            <div class="stock-tech-gauges">${gauges.map(([label, value, note]) => `
                <div class="stock-tech-gauge"><span>${esc(label)}</span><strong>${display(value)}</strong><small>${esc(note)}</small></div>
            `).join('')}</div>
            <section class="stock-tech-section">
                <h3>Price structure &amp; pivots</h3>
                <div class="stock-tech-table">${structuralMarkup || '<div class="stock-empty">Price levels are unavailable.</div>'}</div>
                ${supportMarkup ? `<div class="stock-tech-reasons">SUPPORT / RESISTANCE &nbsp; ${supportMarkup}</div>` : ''}
            </section>
            ${timeframeRows ? `<section class="stock-tech-section"><h3>Multi-timeframe structure</h3><div class="stock-timeframes">${timeframeRows}</div></section>` : ''}
            ${technicalSections ? `<details class="stock-tech-all">
                <summary>All indicators <span>moving averages · momentum · trend · volatility · volume · returns</span></summary>
                <div class="stock-tech-groups">${technicalSections}</div>
            </details>` : ''}
            ${patternMarkup ? `<section class="stock-tech-section"><h3>Patterns &amp; accumulation</h3>${patternMarkup}</section>` : ''}
            <div class="stock-section-note">${historyBars ? `Computed from ${formatNumber(historyBars, 0)} available daily bars. ` : ''}Indicators that lack sufficient history or source values are omitted.</div>
            ${signals.length ? `<div class="stock-signal-section">
                <div class="stock-tech-label">SUPPORTING SIGNALS</div>
                <div class="stock-signal-list">${signals.map(signal => `<span class="stock-signal-chip">${esc(signal)}</span>`).join('')}</div>
            </div>` : ''}`;
    }

    function renderResearchFailure(message) {
        sectionError('fundamental-ratios', `Research service error: ${message}`);
        for (const id of [
            'fundamentals-summary', 'fundamental-growth', 'quarterly-results', 'profit-loss', 'balance-sheet',
            'cash-flow', 'shareholding-pattern', 'peers', 'company-news', 'company-reports',
        ]) {
            sectionError(id, `Research data unavailable: ${message}`, false);
        }
    }

    // ── Strategy test ───────────────────────────────────────────
    const TIMEFRAME_LABEL = { '5minute': '5-minute · 100 days', '15minute': '15-minute · 200 days', '60minute': 'Hourly · 400 days', day: 'Daily · 2000 days' };
    let strategyCatalog = [];

    async function loadStrategyCatalog() {
        const select = document.getElementById('strategy-test-select');
        try {
            strategyCatalog = await getJson('/api/strategies/catalog');
            const groups = [...new Set(strategyCatalog.map(item => item.group))];
            select.innerHTML = groups.map(group => `<optgroup label="${esc(group)}">${strategyCatalog
                .filter(item => item.group === group)
                .map(item => `<option value="${esc(item.name)}">${esc(item.label)}</option>`).join('')}</optgroup>`).join('');
            select.disabled = false;
            document.getElementById('strategy-test-run').disabled = false;
            showStrategyFrames();
        } catch (error) {
            select.innerHTML = '<option>Strategies unavailable</option>';
            document.getElementById('strategy-test-status').textContent = `Could not load strategies: ${error.message}`;
        }
    }

    function showStrategyFrames() {
        const item = strategyCatalog.find(x => x.name === document.getElementById('strategy-test-select').value);
        document.getElementById('strategy-test-frames').textContent = item
            ? `Tests: ${item.timeframes.map(tf => TIMEFRAME_LABEL[tf] || tf).join(', ')}`
            : '';
    }

    function renderStrategyTest(result) {
        const rows = Object.entries(result.timeframes || {}).map(([tf, r]) => {
            const metrics = r.error
                ? `<td colspan="5" class="negative">${esc(r.error)}</td>`
                : `<td>${formatNumber(r.win_rate, 1)}%</td><td>${formatNumber(r.sharpe_ratio)}</td>
                   <td>${formatNumber(r.max_drawdown_pct, 1)}%</td><td>${formatNumber(r.total_return_pct)}%</td><td>${formatNumber(r.total_trades, 0)}</td>`;
            const oos = r.out_of_sample
                ? `<tr class="stock-test-reasons"><td></td><td colspan="6">Out-of-sample (last 30%): ${formatNumber(r.out_of_sample.trades, 0)} trades · P&amp;L ₹${formatNumber(r.out_of_sample.pnl)} · win rate ${formatNumber(r.out_of_sample.win_rate, 1)}%</td></tr>` : '';
            const reasons = (r.failure_reasons || []).length
                ? `<tr class="stock-test-reasons"><td></td><td colspan="6">${esc(r.failure_reasons.join(' · '))}</td></tr>` : '';
            return `<tr><th>${esc(TIMEFRAME_LABEL[tf] || tf)}</th>
                <td><span class="stock-test-badge ${r.passed ? 'pass' : 'fail'}">${r.passed ? 'PASS' : 'FAIL'}</span></td>${metrics}</tr>${oos}${reasons}`;
        }).join('');
        document.getElementById('strategy-test-results').innerHTML = `<div class="stock-table-wrap"><table>
            <thead><tr><th>Timeframe</th><th>Result</th><th>Win rate</th><th>Sharpe</th><th>Max DD</th><th>Return</th><th>Trades</th></tr></thead>
            <tbody>${rows}</tbody></table></div>`;
        const status = document.getElementById('strategy-test-status');
        status.className = `stock-test-status ${result.overall_passed ? 'positive' : 'negative'}`;
        status.textContent = result.overall_passed
            ? `Passed on: ${result.passing_timeframes.map(tf => TIMEFRAME_LABEL[tf] || tf).join(', ')}`
            : 'Did not pass on any tested timeframe.';
    }

    function bindStrategyTest() {
        const form = document.getElementById('strategy-test-form');
        document.getElementById('strategy-test-select').addEventListener('change', showStrategyFrames);
        form.addEventListener('submit', async event => {
            event.preventDefault();
            const strategy = document.getElementById('strategy-test-select').value;
            const button = document.getElementById('strategy-test-run');
            const status = document.getElementById('strategy-test-status');
            button.disabled = true;
            status.className = 'stock-test-status';
            const startedAt = Date.now();
            const showElapsed = () => {
                const s = Math.round((Date.now() - startedAt) / 1000);
                status.textContent = `Downloading history and backtesting — ${s < 60 ? s + 's' : Math.floor(s / 60) + 'm ' + (s % 60) + 's'} elapsed (the first run for a stock can take a minute or two)…`;
            };
            showElapsed();
            const ticker = setInterval(showElapsed, 1000);
            document.getElementById('strategy-test-results').innerHTML = '';
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), 10 * 60 * 1000);
            try {
                const response = await fetch('/api/analysis/stock/test-strategy', {
                    method: 'POST',
                    credentials: 'same-origin',
                    signal: controller.signal,
                    headers: {
                        'Content-Type': 'application/json',
                        Accept: 'application/json',
                        'X-CSRF-Token': document.body.dataset.csrf || '',
                    },
                    body: JSON.stringify({ symbol, strategy, exchange: 'NSE' }),
                });
                const result = await response.json();
                if (!response.ok || result.error) throw new Error(result.detail || result.error || `Request failed (${response.status})`);
                renderStrategyTest(result);
            } catch (error) {
                status.className = 'stock-test-status negative';
                status.textContent = error.name === 'AbortError' ? 'The test took longer than 10 minutes and was stopped.' : `Test failed: ${error.message}`;
            } finally {
                clearTimeout(timer);
                clearInterval(ticker);
                button.disabled = false;
            }
        });
    }

    async function load() {
        bindBack();
        bindChart();
        bindKeyboard();
        bindStrategyTest();
        loadStrategyCatalog();
        startQuotePolling();
        const chartPromise = loadChart();
        // Each section renders as soon as its own data arrives: technical analysis
        // (usually cached) must not wait for the research sources (external news
        // and filings sites that can take many seconds).
        const failures = [];
        const status = document.getElementById('stock-load-status');
        const updateStatus = (done) => {
            status.textContent = failures.length
                ? `Some data sources could not be loaded: ${failures.join(' · ')}`
                : done ? 'Analysis loaded. Check source dates and reporting periods before interpreting financial metrics.'
                    : 'Technical analysis loaded · fetching news, filings and financials…';
            status.classList.toggle('stock-load-error', failures.length > 0);
        };
        const technical = loadTechnical().catch(error => {
            failures.push(`Technical analysis: ${error.message}`);
            sectionError('technical-summary', error.message, false);
        }).finally(() => updateStatus(false));
        const research = getJson(`/api/research/stock/${encodedSymbol}`).then(renderResearch).catch(error => {
            failures.push(`Research: ${error.message}`);
            renderResearchFailure(error.message);
        });
        await Promise.allSettled([technical, research]);
        updateStatus(true);
        await chartPromise;
    }

    // Technical analysis is served from the analysis cache; a profile from
    // earlier today comes back at once marked "refreshing" and is re-fetched
    // a few seconds later so the page updates in place.
    async function loadTechnical(attempt = 0) {
        const profile = await getJson(`/api/analysis/deep/${encodedSymbol}`);
        renderTechnical(profile);
        showFreshness(profile);
        if (profile && profile.refreshing && attempt < 3) {
            setTimeout(() => loadTechnical(attempt + 1).catch(() => {}), 5000);
        }
    }

    function showFreshness(profile) {
        const host = document.getElementById('technical-summary');
        if (!host || !profile || profile.error) return;
        const asOf = profile.as_of ? new Date(profile.as_of) : null;
        const when = asOf && !isNaN(asOf) ? asOf.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' }) : null;
        const label = profile.refreshing ? `cached ${when ? 'as of ' + when : ''} · refreshing…`
            : profile.cache === 'hit' ? `cached ${when ? 'as of ' + when : ''}`
                : profile.cache === 'stale' ? `last analysed ${when ? 'at ' + when : 'earlier'}${profile.refresh_error ? ' (refresh failed)' : ''}`
                    : `analysed ${when ? 'at ' + when : 'just now'}`;
        let badge = document.getElementById('technical-freshness');
        if (!badge) {
            badge = document.createElement('div');
            badge.id = 'technical-freshness';
            badge.className = 'stock-freshness';
            host.prepend(badge);
        }
        badge.textContent = label;
        badge.classList.toggle('refreshing', !!profile.refreshing);
    }

    // Back returns to the screen the user came from (dashboard tab, sub-view and
    // scroll are restored there); a direct visit falls back to the workspace.
    function bindBack() {
        const back = document.getElementById('stock-back');
        if (!back) return;
        back.addEventListener('click', event => {
            let sameApp = false;
            try { sameApp = new URL(document.referrer).origin === location.origin; } catch (e) { /* no referrer */ }
            if (sameApp && history.length > 1) {
                event.preventDefault();
                history.back();
            }
        });
    }

    document.addEventListener('DOMContentLoaded', load);
})();
