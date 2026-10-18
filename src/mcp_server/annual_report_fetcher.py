"""
Annual Report Fetcher — Retrieves annual report links and key financial
highlights from BSE India, NSE India, and public company investor-relations
pages for Indian equities.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime
from typing import Any

import httpx
from bs4 import BeautifulSoup


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
}

# ──────────────────────────────────────────────────────────────
# BSE India — Annual Reports
# ──────────────────────────────────────────────────────────────

# Map of popular NSE symbols to BSE scrip codes for API lookup
_BSE_SCRIP_CODES: dict[str, str] = {
    "RELIANCE": "500325", "TCS": "532540", "INFY": "500209",
    "HDFCBANK": "500180", "ICICIBANK": "532174", "HINDUNILVR": "500696",
    "SBIN": "500112", "BHARTIARTL": "532454", "ITC": "500875",
    "KOTAKBANK": "500247", "LT": "500510", "HCLTECH": "532281",
    "AXISBANK": "532215", "ASIANPAINT": "500820", "MARUTI": "532500",
    "SUNPHARMA": "524715", "TITAN": "500114", "BAJFINANCE": "500034",
    "WIPRO": "507685", "ULTRACEMCO": "532538", "NESTLEIND": "500790",
    "TATAMOTORS": "500570", "TATASTEEL": "500470", "POWERGRID": "532898",
    "NTPC": "532555", "JSWSTEEL": "500228", "M&M": "500520",
    "ADANIENT": "512599", "ADANIPORTS": "532921", "COALINDIA": "533278",
    "ONGC": "500312", "DRREDDY": "500124", "CIPLA": "500087",
    "GRASIM": "500300", "TECHM": "532755", "APOLLOHOSP": "508869",
    "DIVISLAB": "532488", "EICHERMOT": "505200", "HEROMOTOCO": "500182",
    "BAJAJ-AUTO": "532977", "BRITANNIA": "500825", "INDUSINDBK": "532187",
    "HDFCLIFE": "540777", "SBILIFE": "540719", "DABUR": "500096",
    "GODREJCP": "532424", "PIDILITIND": "500331", "HAVELLS": "517354",
    "DLF": "532868", "VEDL": "500295", "TATAPOWER": "500400",
}


def _clean_financial_value(value: str) -> str:
    """Normalize Screener cell text and discard unit-only placeholders."""
    cleaned = re.sub(r"\s+", " ", value).strip()
    if not cleaned or re.fullmatch(r"[\-–—₹%.,\s]+", cleaned):
        return ""
    if cleaned.lower() in {"n/a", "na", "not available"}:
        return ""
    return cleaned


async def _fetch_bse_annual_reports(symbol: str, limit: int = 5) -> list[dict[str, str]]:
    """Fetch annual report PDFs from BSE India corporate filings."""
    scrip_code = _BSE_SCRIP_CODES.get(symbol.upper())
    if not scrip_code:
        return []

    url = (
        f"https://api.bseindia.com/BseIndiaAPI/api/AnnualReport/w"
        f"?scripcode={scrip_code}&strType=C"
    )
    reports: list[dict[str, str]] = []

    async with httpx.AsyncClient(headers=_HEADERS, timeout=20, follow_redirects=True) as client:
        try:
            resp = await client.get(url)
            if resp.status_code != 200:
                return reports
            data = resp.json()
            if isinstance(data, list):
                for item in data[:limit]:
                    reports.append({
                        "title": item.get("SLONGNAME", f"{symbol} Annual Report"),
                        "year": item.get("Year", ""),
                        "type": "Annual Report",
                        "url": item.get("ATTACHMENTNAME", ""),
                        "source": "BSE India",
                    })
        except Exception:
            pass

    return reports


# ──────────────────────────────────────────────────────────────
# NSE India — Corporate Filings (Annual Reports)
# ──────────────────────────────────────────────────────────────

async def _fetch_nse_annual_reports(symbol: str, limit: int = 5) -> list[dict[str, str]]:
    """Fetch annual reports from NSE India corporate filings API."""
    url = (
        f"https://www.nseindia.com/api/annual-reports?index=equities&symbol={symbol}"
    )
    reports: list[dict[str, str]] = []

    nse_headers = {
        **_HEADERS,
        "Referer": "https://www.nseindia.com/",
        "Accept": "application/json",
    }

    async with httpx.AsyncClient(headers=nse_headers, timeout=20, follow_redirects=True) as client:
        try:
            # Get session cookies
            await client.get("https://www.nseindia.com/")
            resp = await client.get(url)
            if resp.status_code != 200:
                return reports
            data = resp.json()
            if isinstance(data, list):
                for item in data[:limit]:
                    reports.append({
                        "title": item.get("companyName", symbol),
                        "year": item.get("yearEnding", ""),
                        "type": "Annual Report",
                        "url": item.get("fileName", ""),
                        "source": "NSE India",
                    })
        except Exception:
            pass

    return reports


# ──────────────────────────────────────────────────────────────
# Google Search — Annual Report PDF links
# ──────────────────────────────────────────────────────────────

async def _search_annual_report_links(
    symbol: str, company_name: str, limit: int = 5
) -> list[dict[str, str]]:
    """
    Search Google for annual report PDFs for a company.
    Falls back to scraping search results.
    """
    query = f"{company_name} annual report {datetime.now().year} filetype:pdf site:bseindia.com OR site:nseindia.com OR site:{symbol.lower()}.com"
    url = f"https://www.google.com/search?q={query}&num={limit}"
    reports: list[dict[str, str]] = []

    async with httpx.AsyncClient(headers=_HEADERS, timeout=15, follow_redirects=True) as client:
        try:
            resp = await client.get(url)
            if resp.status_code != 200:
                return reports
            soup = BeautifulSoup(resp.text, "html.parser")
            for a in soup.select("a[href]"):
                href = a.get("href", "")
                # Extract actual URL from Google redirect
                match = re.search(r"/url\?q=(https?://[^&]+)", href)
                if match:
                    real_url = match.group(1)
                    if ".pdf" in real_url.lower() or "annual" in real_url.lower():
                        text = a.get_text(strip=True)[:200]
                        reports.append({
                            "title": text or f"{symbol} Annual Report",
                            # a search hit carries no reliable fiscal year
                            "year": "",
                            "type": "Annual Report (Search)",
                            "url": real_url,
                            "source": "Google Search",
                        })
                        if len(reports) >= limit:
                            break
        except Exception:
            pass

    return reports


# ──────────────────────────────────────────────────────────────
# Screener.in — Financial Highlights
# ──────────────────────────────────────────────────────────────

async def _fetch_screener_financials(symbol: str) -> dict[str, Any]:
    """Scrape key financial highlights from Screener.in (public page).

    Prefers consolidated figures and falls back to standalone; the basis
    actually used is returned so the UI never mislabels the numbers.
    """
    async with httpx.AsyncClient(headers=_HEADERS, timeout=15, follow_redirects=True) as client:
        try:
            resp = await client.get(f"https://www.screener.in/company/{symbol}/consolidated/")
            financials = _extract_screener_financials(resp.text) if resp.status_code == 200 else {}
            if not _has_statements(financials):
                # no consolidated accounts (or page unavailable): try standalone
                standalone = await client.get(f"https://www.screener.in/company/{symbol}/")
                if standalone.status_code == 200:
                    resp, financials = standalone, _extract_screener_financials(standalone.text)
                elif resp.status_code != 200:
                    return {"error": f"Screener.in returned HTTP {standalone.status_code}"}

            if not financials.keys() - {"company_name", "warehouse_id"}:
                return {"error": "Screener.in returned no financial data; the page may require sign-in or the symbol may be unavailable"}
            financials["basis"] = "consolidated" if "/consolidated" in str(resp.url) else "standalone"
            financials["source_url"] = str(resp.url)

            # Screener loads its peer table with JavaScript; fetch it directly.
            warehouse_id = financials.pop("warehouse_id", None)
            if not financials.get("peers") and warehouse_id:
                peers = await _fetch_screener_peers(client, warehouse_id)
                if peers:
                    financials["peers"] = peers

            derived = _derive_fundamentals(financials)
            if derived:
                financials["derived"] = derived
            return financials
        except Exception as e:
            return {"error": f"Unable to fetch Screener.in financials: {e}"}


async def _fetch_screener_peers(client: httpx.AsyncClient, warehouse_id: str) -> dict[str, Any] | None:
    try:
        resp = await client.get(f"https://www.screener.in/api/company/{warehouse_id}/peers/")
        if resp.status_code != 200:
            return None
        table = BeautifulSoup(resp.text, "html.parser").select_one("table")
        return _parse_statement_table(table, "Peers") if table else None
    except Exception:
        return None


def _has_statements(financials: dict[str, Any]) -> bool:
    return any(financials.get(key) for key in ("quarterly_results", "profit_loss", "balance_sheet"))


_UNIT_PATTERN = re.compile(r"figures in\s+(Rs\.?\s*[A-Za-z]+)", re.IGNORECASE)


def _section_unit(section: Any) -> str:
    """'Consolidated Figures in Rs. Crores' -> '₹ crore'."""
    match = _UNIT_PATTERN.search(section.get_text(" ", strip=True)) if section else None
    if not match:
        return ""
    scale = re.sub(r"^Rs\.?\s*", "", match.group(1)).lower().rstrip("s")
    return f"₹ {scale}"


def _parse_statement_table(table: Any, label: str) -> dict[str, Any] | None:
    headers = [cell.get_text(" ", strip=True) for cell in table.select("thead tr th")]
    if not headers:
        first_row = table.select_one("tr")
        headers = [cell.get_text(" ", strip=True) for cell in first_row.find_all("th")] if first_row else []
    # Peer tables lead with a serial-number column; the name column is the label.
    skip = 1 if headers and re.match(r"^s\.?\s*no", headers[0], re.IGNORECASE) else 0
    rows: list[dict[str, Any]] = []
    for row in table.select("tbody tr") or table.select("tr")[1:]:
        cells = row.find_all(["th", "td"], recursive=False)[skip:]
        if len(cells) < 2:
            continue
        # strip Screener's expand-button glyph ("Sales +")
        metric = re.sub(r"\s*\+$", "", cells[0].get_text(" ", strip=True))
        values = [_clean_financial_value(cell.get_text(" ", strip=True)) for cell in cells[1:]]
        if metric and any(values):
            rows.append({"metric": metric, "values": values})
    if not rows:
        return None
    return {
        "label": label,
        "periods": headers[skip + 1:] if len(headers) > skip + 1 else [],
        "rows": rows,
    }


def _extract_screener_financials(page_html: str) -> dict[str, Any]:
    """Extract available top ratios, statements, shareholding and peers from Screener HTML."""
    soup = BeautifulSoup(page_html, "html.parser")
    financials: dict[str, Any] = {}

    for li in soup.select("#top-ratios li"):
        name_el = li.select_one(".name")
        if not name_el:
            continue
        candidates = [
            _clean_financial_value(element.get_text(" ", strip=True))
            for element in li.select(".value, .number")
        ]
        value = next((candidate for candidate in candidates if re.search(r"\d", candidate)), "")
        if value:
            financials[name_el.get_text(" ", strip=True)] = value

    pros = [li.get_text(" ", strip=True) for li in soup.select(".pros li")]
    cons = [li.get_text(" ", strip=True) for li in soup.select(".cons li")]
    if pros:
        financials["pros"] = pros
    if cons:
        financials["cons"] = cons

    title_el = soup.select_one("h1")
    if title_el:
        financials["company_name"] = title_el.get_text(" ", strip=True)

    sections = {
        "quarterly_results": ("quarters", "Quarterly Results"),
        "profit_loss": ("profit-loss", "Profit & Loss"),
        "balance_sheet": ("balance-sheet", "Balance Sheet"),
        "cash_flow": ("cash-flow", "Cash Flow"),
        "shareholding_pattern": ("shareholding", "Shareholding Pattern"),
        "peers": ("peers", "Peers"),
    }
    for key, (section_id, label) in sections.items():
        section = soup.select_one(f"#{section_id}")
        table = section.select_one("table") if section else None
        statement = _parse_statement_table(table, label) if table else None
        if statement:
            unit = _section_unit(section)
            if unit:
                statement["unit"] = unit
            financials[key] = statement

    company_info = soup.select_one("#company-info[data-warehouse-id]")
    if company_info:
        financials["warehouse_id"] = company_info["data-warehouse-id"]

    return financials


# ──────────────────────────────────────────────────────────────
# Derived fundamentals (growth, CAGR, margins)
# ──────────────────────────────────────────────────────────────

_ROW_ALIASES = {
    "revenue": ("sales", "revenue"),
    "operating_profit": ("operating profit", "financing profit"),
    "net_profit": ("net profit",),
    "eps": ("eps",),
}
_ROW_LABELS = {
    "revenue": "Revenue", "operating_profit": "Operating profit",
    "net_profit": "Net profit", "eps": "EPS",
}


def _to_number(text: Any) -> float | None:
    cleaned = re.sub(r"[,%₹\s]", "", str(text or ""))
    try:
        return float(cleaned)
    except ValueError:
        return None


def _row_values(statement: dict[str, Any], key: str) -> list[float | None] | None:
    for row in statement.get("rows", []):
        name = row.get("metric", "").strip().lower()
        if any(name.startswith(alias) for alias in _ROW_ALIASES[key]):
            return [_to_number(value) for value in row.get("values", [])]
    return None


def _pct_change(current: float | None, previous: float | None) -> float | None:
    # growth across a loss/profit flip is not meaningful
    if current is None or previous is None or previous == 0 or (current < 0) != (previous < 0):
        return None
    return round((current - previous) / abs(previous) * 100, 1)


def _cagr(first: float | None, last: float | None, years: int) -> float | None:
    if first is None or last is None or first <= 0 or last <= 0 or years <= 0:
        return None
    return round(((last / first) ** (1 / years) - 1) * 100, 1)


def _dated_indexes(periods: list[str]) -> list[int]:
    """Indexes of real reporting periods (drops TTM and blank headers), oldest first."""
    return [
        i for i, period in enumerate(periods)
        if re.search(r"(19|20)\d{2}", period or "") and "ttm" not in period.lower()
    ]


def _derive_fundamentals(financials: dict[str, Any]) -> dict[str, Any]:
    """Growth and margin metrics computed from Screener's statements (oldest-to-newest columns)."""
    derived: dict[str, Any] = {}

    quarterly = financials.get("quarterly_results") or {}
    q_idx = _dated_indexes(quarterly.get("periods", []))
    if len(q_idx) >= 2:
        latest, prev = q_idx[-1], q_idx[-2]
        year_ago = q_idx[-5] if len(q_idx) >= 5 else None
        metrics = []
        for key in ("revenue", "operating_profit", "net_profit", "eps"):
            values = _row_values(quarterly, key)
            if not values or latest >= len(values):
                continue
            metrics.append({
                "metric": _ROW_LABELS[key],
                "latest": values[latest],
                "qoq_pct": _pct_change(values[latest], values[prev] if prev < len(values) else None),
                "yoy_pct": _pct_change(values[latest], values[year_ago]) if year_ago is not None else None,
            })
        if metrics:
            derived["quarterly"] = {
                "period": quarterly["periods"][latest],
                "unit": quarterly.get("unit", ""),
                "metrics": metrics,
            }

    annual = financials.get("profit_loss") or {}
    a_idx = _dated_indexes(annual.get("periods", []))
    if len(a_idx) >= 2:
        latest = a_idx[-1]
        metrics = []
        for key in ("revenue", "operating_profit", "net_profit", "eps"):
            values = _row_values(annual, key)
            if not values or latest >= len(values):
                continue
            at = lambda n: values[a_idx[-1 - n]] if len(a_idx) > n and a_idx[-1 - n] < len(values) else None
            metrics.append({
                "metric": _ROW_LABELS[key],
                "latest": values[latest],
                "yoy_pct": _pct_change(at(0), at(1)),
                "cagr_3y_pct": _cagr(at(3), at(0), 3),
                "cagr_5y_pct": _cagr(at(5), at(0), 5),
            })
        revenue = _row_values(annual, "revenue") or []
        operating = _row_values(annual, "operating_profit") or []
        net = _row_values(annual, "net_profit") or []
        recent = a_idx[-5:]
        margin = lambda numer, i: (
            round(numer[i] / revenue[i] * 100, 1)
            if i < len(numer) and i < len(revenue) and numer[i] is not None and revenue[i] else None
        )
        if metrics:
            derived["annual"] = {
                "period": annual["periods"][latest],
                "unit": annual.get("unit", ""),
                "metrics": metrics,
            }
        if revenue:
            derived["margins"] = {
                "periods": [annual["periods"][i] for i in recent],
                "operating_margin_pct": [margin(operating, i) for i in recent],
                "net_margin_pct": [margin(net, i) for i in recent],
            }
    return derived


# Research pages are slow-moving; cache successful fetches per symbol.
_REPORTS_TTL_SECONDS = 6 * 60 * 60
_reports_cache: dict[tuple[str, int], tuple[float, dict[str, Any]]] = {}


# ──────────────────────────────────────────────────────────────
# TrendLyne — Annual Report Links
# ──────────────────────────────────────────────────────────────

async def _fetch_trendlyne_reports(symbol: str, limit: int = 5) -> list[dict[str, str]]:
    """Fetch annual report links from Trendlyne."""
    url = f"https://trendlyne.com/equity/{symbol}/latest-annual-reports/"
    reports: list[dict[str, str]] = []

    async with httpx.AsyncClient(headers=_HEADERS, timeout=15, follow_redirects=True) as client:
        try:
            resp = await client.get(url)
            if resp.status_code != 200:
                return reports
            soup = BeautifulSoup(resp.text, "html.parser")
            for a in soup.select("a[href*='.pdf']"):
                text = a.get_text(strip=True)
                href = a.get("href", "")
                if text and href:
                    reports.append({
                        "title": text,
                        "year": "",
                        "type": "Annual Report",
                        "url": href if href.startswith("http") else f"https://trendlyne.com{href}",
                        "source": "TrendLyne",
                    })
                    if len(reports) >= limit:
                        break
        except Exception:
            pass

    return reports


# ──────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────

_COMPANY_NAMES: dict[str, str] = {
    "RELIANCE": "Reliance Industries", "TCS": "Tata Consultancy Services",
    "INFY": "Infosys", "HDFCBANK": "HDFC Bank", "ICICIBANK": "ICICI Bank",
    "HINDUNILVR": "Hindustan Unilever", "SBIN": "State Bank of India",
    "BHARTIARTL": "Bharti Airtel", "ITC": "ITC Limited",
    "KOTAKBANK": "Kotak Mahindra Bank", "LT": "Larsen & Toubro",
    "HCLTECH": "HCL Technologies", "AXISBANK": "Axis Bank",
    "ASIANPAINT": "Asian Paints", "MARUTI": "Maruti Suzuki",
    "SUNPHARMA": "Sun Pharma", "TITAN": "Titan Company",
    "BAJFINANCE": "Bajaj Finance", "WIPRO": "Wipro",
    "TATAMOTORS": "Tata Motors", "TATASTEEL": "Tata Steel",
    "ADANIENT": "Adani Enterprises", "ADANIPORTS": "Adani Ports",
    "DRREDDY": "Dr Reddy's Laboratories", "CIPLA": "Cipla",
}


async def fetch_annual_reports(symbol: str, limit: int = 10) -> dict[str, Any]:
    """
    Aggregate annual report links and financial highlights from multiple
    public sources for a given NSE stock symbol.
    """
    symbol = symbol.upper().strip()
    cached = _reports_cache.get((symbol, limit))
    if cached and time.monotonic() - cached[0] < _REPORTS_TTL_SECONDS:
        return cached[1]

    bse, nse, trendlyne, screener = await asyncio.gather(
        _fetch_bse_annual_reports(symbol, limit=limit),
        _fetch_nse_annual_reports(symbol, limit=limit),
        _fetch_trendlyne_reports(symbol, limit=5),
        _fetch_screener_financials(symbol),
        return_exceptions=True,
    )
    # Search by the company's real name when Screener supplied one.
    company_name = (
        screener.get("company_name") if isinstance(screener, dict) and screener.get("company_name")
        else _COMPANY_NAMES.get(symbol, symbol)
    )
    try:
        google = await _search_annual_report_links(symbol, company_name, limit=5)
    except Exception as e:
        google = e

    all_reports: list[dict[str, str]] = []
    for result in [bse, nse, google, trendlyne]:
        if isinstance(result, list):
            all_reports.extend(result)

    # De-duplicate by URL
    seen_urls: set[str] = set()
    unique_reports: list[dict[str, str]] = []
    for r in all_reports:
        url = r.get("url", "")
        if url and url not in seen_urls:
            seen_urls.add(url)
            unique_reports.append(r)
        elif not url:
            unique_reports.append(r)

    financial_highlights = (
        screener if isinstance(screener, dict) and not screener.get("error") else {}
    )
    financial_highlights_error = (
        screener.get("error") if isinstance(screener, dict) else
        f"Unable to fetch Screener.in financials: {screener}"
        if isinstance(screener, Exception) else
        "Screener.in financial highlights were unavailable"
    )

    result = {
        "symbol": symbol,
        "company_name": company_name,
        "fetched_at": datetime.now().isoformat(),
        "total_reports": len(unique_reports),
        "reports": unique_reports[:limit],
        "financial_highlights": financial_highlights,
        "financial_highlights_error": financial_highlights_error if not financial_highlights else "",
        "sources_queried": ["BSE India", "NSE India", "Google Search", "TrendLyne", "Screener.in"],
    }
    if financial_highlights:
        _reports_cache[(symbol, limit)] = (time.monotonic(), result)
    return result
