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
    let chartState = null;
    let chartHoverIndex = null;
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

    function drawChart() {
        if (!chartState || !chartCanvas.clientWidth) return;
        const rect = chartCanvas.getBoundingClientRect();
        const dpr = window.devicePixelRatio || 1;
        chartCanvas.width = Math.round(rect.width * dpr);
        chartCanvas.height = Math.round(rect.height * dpr);
        const ctx = chartContext;
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.clearRect(0, 0, rect.width, rect.height);

        const candles = chartState.candles;
        const indicators = chartState.indicators || {};
        if (!candles.length) return;

        const width = rect.width;
        const height = rect.height;
        const left = 14;
        const right = width - 66;
        const plotWidth = right - left;
        const priceTop = 18;
        const priceBottom = height * .61;
        const volumeTop = height * .66;
        const volumeBottom = height * .77;
        const rsiTop = height * .83;
        const rsiBottom = height - 25;
        const slot = plotWidth / candles.length;
        const candleWidth = Math.max(1, Math.min(8, slot * .62));
        const prices = candles.flatMap(candle => [
            Number(candle.high), Number(candle.low),
        ]).filter(Number.isFinite);
        const minPrice = Math.min(...prices);
        const maxPrice = Math.max(...prices);
        const pricePadding = (maxPrice - minPrice || maxPrice * .02 || 1) * .08;
        const priceMin = minPrice - pricePadding;
        const priceMax = maxPrice + pricePadding;
        const yPrice = value => priceBottom - ((value - priceMin) / (priceMax - priceMin)) * (priceBottom - priceTop);
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
        chartLine(ctx, indicators.sma_20 || [], xFor, yPrice, '#ffad60', 1.35);
        chartLine(ctx, indicators.sma_50 || [], xFor, yPrice, '#f0d06c', 1.2);
        chartLine(ctx, indicators.sma_200 || [], xFor, yPrice, '#ef7697', 1.2);
        chartLine(ctx, indicators.ema_9 || [], xFor, yPrice, '#4bbcff', 1.25);

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

        const rsi = indicators.rsi_14 || [];
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

        const dateIndexes = [0, Math.floor((candles.length - 1) / 2), candles.length - 1];
        ctx.textBaseline = 'top';
        dateIndexes.forEach(index => {
            ctx.textAlign = index === 0 ? 'left' : index === candles.length - 1 ? 'right' : 'center';
            ctx.fillStyle = '#708492';
            ctx.fillText(chartDate(candles[index].timestamp), xFor(index), height - 17);
        });

        if (chartHoverIndex != null) {
            const index = Math.max(0, Math.min(candles.length - 1, chartHoverIndex));
            const candle = candles[index];
            const x = xFor(index);
            ctx.strokeStyle = 'rgba(210, 225, 235, .3)';
            ctx.setLineDash([3, 4]);
            ctx.beginPath();
            ctx.moveTo(x, priceTop);
            ctx.lineTo(x, rsiBottom);
            ctx.stroke();
            ctx.setLineDash([]);
            const tooltip = [
                chartDate(candle.timestamp, { weekday: 'short', day: '2-digit', month: 'short', year: 'numeric' }),
                `O ${formatNumber(candle.open)}   H ${formatNumber(candle.high)}`,
                `L ${formatNumber(candle.low)}   C ${formatNumber(candle.close)}`,
                `VOL ${formatNumber(candle.volume, 0)}`,
            ];
            const boxWidth = 192;
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

    async function loadChart(days = 180) {
        const requestId = ++chartRequest;
        chartHoverIndex = null;
        chartEmpty.hidden = false;
        chartEmpty.textContent = 'Loading historical candles…';
        const rangeLabel = { 30: '1 month view', 90: '3 month view', 180: '6 month view', 365: '1 year view' };
        document.getElementById('stock-chart-range-label').textContent = rangeLabel[days] || `${days} day view`;
        try {
            const data = await getJson(
                `/api/chart/historical/${encodedSymbol}?exchange=NSE&interval=day&days=${days}&include_indicators=true`,
            );
            if (requestId !== chartRequest) return;
            if (!Array.isArray(data.candles) || !data.candles.length) {
                throw new Error('No historical candles are available for this symbol and period.');
            }
            chartState = data;
            chartEmpty.hidden = true;
            drawChart();
        } catch (error) {
            if (requestId !== chartRequest) return;
            chartState = null;
            chartEmpty.hidden = false;
            chartEmpty.textContent = `Chart unavailable: ${error.message}`;
        }
    }

    function bindChart() {
        document.querySelectorAll('.stock-range-btn').forEach(button => {
            button.addEventListener('click', () => {
                document.querySelectorAll('.stock-range-btn').forEach(item => item.classList.remove('active'));
                button.classList.add('active');
                loadChart(Number(button.dataset.days));
            });
        });
        chartCanvas.addEventListener('pointermove', event => {
            if (!chartState?.candles?.length) return;
            const bounds = chartCanvas.getBoundingClientRect();
            const left = 14;
            const right = bounds.width - 66;
            const slot = (right - left) / chartState.candles.length;
            const index = Math.floor((event.clientX - bounds.left - left) / slot);
            chartHoverIndex = Math.max(0, Math.min(chartState.candles.length - 1, index));
            drawChart();
        });
        chartCanvas.addEventListener('pointerleave', () => {
            chartHoverIndex = null;
            drawChart();
        });
        window.addEventListener('resize', drawChart);
    }

    document.getElementById('screener-link').href = screenerUrl;
    document.getElementById('nse-company-link').href = `https://www.nseindia.com/get-quotes/equity?symbol=${encodedSymbol}`;

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

    function metricGrid(entries) {
        if (!entries.length) return '<div class="stock-empty">No metrics returned by the source.</div>';
        return `<div class="stock-metric-grid">${entries.map(([label, value]) => `
            <div class="stock-metric">
                <div class="stock-metric-label">${esc(label)}</div>
                <div class="stock-metric-value">${display(value)}</div>
            </div>`).join('')}</div>`;
    }

    function renderFinancialHighlights(highlights, sourceError = '') {
        const ignored = new Set([
            'company_name', 'pros', 'cons', 'quarterly_results', 'profit_loss',
            'balance_sheet', 'cash_flow', 'shareholding_pattern', 'peers',
        ]);
        const ratios = Object.entries(highlights || {})
            .filter(([key, value]) => !ignored.has(key) && (typeof value !== 'object' || value === null));
        document.getElementById('fundamental-ratios').innerHTML = ratios.length
            ? `<div class="stock-table-wrap"><table><tbody>${ratios.map(([key, value]) =>
                `<tr><th>${esc(key)}</th><td>${display(value)}</td></tr>`).join('')}</tbody></table></div>`
            : `<div class="stock-empty">${esc(sourceError || `Screener.in did not return ratio data for ${symbol}.`)}</div>
               <p class="stock-section-note"><a href="${esc(screenerUrl)}" target="_blank" rel="noopener noreferrer">Open the Screener company page</a></p>`;

        const summaryKeys = [
            'Market Cap', 'Current Price', 'High / Low', 'Stock P/E', 'Book Value',
            'Dividend Yield', 'ROCE', 'ROE',
        ];
        const summary = summaryKeys
            .filter(key => highlights && highlights[key] != null)
            .map(key => [key, highlights[key]]);
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

    function renderStatement(id, statement, fallbackName, sourceError = '') {
        if (!statement?.rows?.length) {
            sectionError(id, sourceError || `${fallbackName} are unavailable from the current Screener.in response.`);
            return;
        }
        const periods = statement.periods || [];
        const head = ['Metric', ...periods].map(item => `<th>${esc(item)}</th>`).join('');
        const rows = statement.rows.map(row => `<tr>
            <th>${esc(row.metric)}</th>
            ${(row.values || []).map(value => `<td>${display(value)}</td>`).join('')}
        </tr>`).join('');
        document.getElementById(id).innerHTML = `<div class="stock-table-wrap"><table>
            <thead><tr>${head}</tr></thead><tbody>${rows}</tbody>
        </table></div>`;
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
            ${renderList(otherArticles)}`;
    }

    function renderReports(research) {
        const reports = research?.annual_reports?.reports || [];
        if (!reports.length) {
            const message = research?.annual_reports?.error
                ? `Filing source error: ${research.annual_reports.error}`
                : 'No annual report links or filing documents were returned.';
            sectionError('company-reports', message);
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
        }).join('')}</ul>`;
    }

    function renderResearch(research) {
        const annual = research?.annual_reports || {};
        const highlights = annual.financial_highlights || {};
        const financialError = annual.financial_highlights_error || '';
        document.getElementById('stock-title').textContent = highlights.company_name || research?.latest_news?.company_name || symbol;
        document.title = `${symbol} — VerquoAI Stock Research`;
        document.getElementById('stock-subtitle').textContent =
            `NSE symbol ${symbol} · Screener.in financials · NSE filings · company news`;
        renderFinancialHighlights(highlights, financialError);
        renderStatement('quarterly-results', highlights.quarterly_results, 'Quarterly results', financialError);
        renderStatement('profit-loss', highlights.profit_loss, 'Profit & loss statements', financialError);
        renderStatement('balance-sheet', highlights.balance_sheet, 'Balance-sheet statements', financialError);
        renderStatement('cash-flow', highlights.cash_flow, 'Cash-flow statements', financialError);
        renderStatement('shareholding-pattern', highlights.shareholding_pattern, 'Shareholding pattern', financialError);
        renderStatement('peers', highlights.peers, 'Peer comparison data', financialError);
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
                <p class="stock-section-note"><a href="/app">Return to workspace to check market data access.</a></p>`;
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
        const recommendation = (verdict.recommendation_text || '')
            .match(/Primary recommendation:\s*([^.]+)\.?$/i)?.[1]?.trim();
        const contextCaution = (verdict.recommendation_text || '')
            .match(/\(([^()]*(?:size positions cautiously|aligned with the stock)[^()]*)\)/i)?.[1];
        const gauges = [
            ['Trend score', formatNumber(profile.trend_score, 0), 'out of 100'],
            ['Relative strength', formatNumber(profile.rs_rating, 0), 'RS rating'],
            ['Trend stage', stage, 'market structure'],
        ];
        const rsiTone = profile.rsi_14 == null ? '' : profile.rsi_14 >= 70 ? 'negative' : profile.rsi_14 <= 30 ? 'positive' : '';
        const dailyTone = profile.change_1d == null ? '' : profile.change_1d >= 0 ? 'positive' : 'negative';
        const mediumTone = profile.change_20d == null ? '' : profile.change_20d >= 0 ? 'positive' : 'negative';
        const macd = profile.daily_trend?.macd_histogram;
        const macdTone = macd == null ? '' : macd >= 0 ? 'positive' : 'negative';
        const technicalRows = [
            ['Last price', formatPrice(profile.ltp), null],
            ['RSI · 14', formatNumber(profile.rsi_14), rsiTone],
            ['ADX · 14', formatNumber(profile.adx_14), null],
            ['ATR · 14', formatPrice(profile.atr_14), null],
            ['SMA · 20 / 50', `${formatNumber(profile.sma_20)} / ${formatNumber(profile.sma_50)}`, null],
            ['SMA · 200', formatPrice(profile.sma_200), null],
            ['EMA · 9 / 21', `${formatNumber(profile.ema_9)} / ${formatNumber(profile.ema_21)}`, null],
            ['1D / 5D', `${formatNumber(profile.change_1d)}% / ${formatNumber(profile.change_5d)}%`, dailyTone],
            ['20D / 60D', `${formatNumber(profile.change_20d)}% / ${formatNumber(profile.change_60d)}%`, mediumTone],
            ['52W high / low', `${formatPrice(profile.high_52w)} / ${formatPrice(profile.low_52w)}`, null],
            ['Volume ratio', profile.vol_ratio == null ? '—' : `${formatNumber(profile.vol_ratio)}×`, null],
            ['MACD histogram', formatNumber(macd), macdTone],
        ];
        const support = profile.support_resistance;
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
        const accumulation = profile.accumulation;
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
        document.getElementById('technical-summary').innerHTML = `
            <div class="stock-verdict-board">
                <div class="stock-verdict-primary">
                    <span class="stock-tech-label">SYSTEM VERDICT</span>
                    <strong class="stock-verdict-action ${actionClass}">${esc(action)}</strong>
                    <span class="stock-verdict-confidence">CONFIDENCE ${profile.verdict?.confidence == null ? '—' : `${formatNumber(profile.verdict.confidence, 0)}%`}</span>
                </div>
                <div class="stock-verdict-context">
                    <div class="stock-context-pills">
                        <span class="stock-context-pill ${contextClass(marketStatus)}">MARKET <b>${esc(marketStatus.toUpperCase())}</b></span>
                        <span class="stock-context-pill ${contextClass(sectorStatus)}">${esc(sectorName.toUpperCase())} <b>${esc(sectorStatus.toUpperCase())}</b></span>
                        <span class="stock-context-pill ${actionClass}">STOCK <b>${esc(action.toUpperCase())}</b></span>
                    </div>
                    <div class="stock-context-footnote">${esc(contextCaution || 'Verdict combines market, sector and daily / weekly / monthly stock structure.')}</div>
                    ${recommendation ? `<div class="stock-primary-recommendation"><span>PRIMARY RECOMMENDATION</span><strong>${esc(recommendation)}</strong></div>` : ''}
                </div>
            </div>
            <div class="stock-tech-gauges">${gauges.map(([label, value, note]) => `
                <div class="stock-tech-gauge"><span>${esc(label)}</span><strong>${display(value)}</strong><small>${esc(note)}</small></div>
            `).join('')}</div>
            <div class="stock-tech-table">${technicalRows.map(([label, value, tone]) => `
                <div class="stock-tech-row"><span>${esc(label)}</span><strong class="${tone || ''}">${esc(value)}</strong></div>
            `).join('')}</div>
            ${supportMarkup ? `<div class="stock-tech-reasons">KEY LEVELS &nbsp; ${supportMarkup}</div>` : ''}
            ${signals.length ? `<div class="stock-signal-section">
                <div class="stock-tech-label">SUPPORTING SIGNALS</div>
                <div class="stock-signal-list">${signals.map(signal => `<span class="stock-signal-chip">${esc(signal)}</span>`).join('')}</div>
            </div>` : ''}`;
    }

    function renderResearchFailure(message) {
        sectionError('fundamental-ratios', `Research service error: ${message}`);
        for (const id of [
            'fundamentals-summary', 'quarterly-results', 'profit-loss', 'balance-sheet',
            'cash-flow', 'shareholding-pattern', 'peers', 'company-news', 'company-reports',
        ]) {
            sectionError(id, `Research data unavailable: ${message}`, false);
        }
    }

    async function load() {
        bindChart();
        const chartPromise = loadChart(180);
        const [researchResult, technicalResult] = await Promise.allSettled([
            getJson(`/api/research/stock/${encodedSymbol}`),
            getJson(`/api/analysis/deep/${encodedSymbol}`),
        ]);
        const failures = [];
        if (researchResult.status === 'fulfilled') {
            renderResearch(researchResult.value);
        } else {
            failures.push(`Research: ${researchResult.reason.message}`);
            renderResearchFailure(researchResult.reason.message);
        }
        if (technicalResult.status === 'fulfilled') {
            renderTechnical(technicalResult.value);
        } else {
            failures.push(`Technical analysis: ${technicalResult.reason.message}`);
            sectionError('technical-summary', technicalResult.reason.message, false);
        }
        const status = document.getElementById('stock-load-status');
        status.textContent = failures.length
            ? `Some data sources could not be loaded: ${failures.join(' · ')}`
            : 'Analysis loaded. Check source dates and reporting periods before interpreting financial metrics.';
        status.classList.toggle('stock-load-error', failures.length > 0);
        await chartPromise;
    }

    document.addEventListener('DOMContentLoaded', load);
})();
