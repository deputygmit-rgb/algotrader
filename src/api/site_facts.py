"""What VerquoAI is and is not: first-party statements for the landing page and search engines.

The landing page's About / FAQ section and its structured data (JSON-LD) are both
built from these lists, so the visible text and what search engines and AI
summaries read always say the same thing. Keep every statement true of the
product as it is; do not name a broker (pages stay broker-neutral).
"""
from __future__ import annotations

import json
import os
from typing import Any

SITE_NAME = "VerquoAI"


def site_url() -> str:
    return (os.environ.get("PUBLIC_URL") or "https://verquoai.com").rstrip("/")


ABOUT = (
    "VerquoAI is market analytics software with a mock trading terminal for the Indian stock market (NSE and BSE). "
    "It brings live market flow, intraday and swing setup boards, options open-interest analysis, backtesting, "
    "paper trading and AI monitoring agents into one workspace. Practise on a simulated market before risking money: "
    "VerquoAI never holds your money or securities, and it gives no investment advice."
)

IS = [
    "Market analytics for NSE and BSE stocks, stock futures and index options, with a mock trading terminal.",
    "A practice ground: the demo and paper trading fill orders against a simulated market with simulated cash.",
    "Rule-based: every signal comes from published rules applied to closed price bars, and is shown with its "
    "entry, stop, targets and how that setup has performed after costs - losing setups included.",
    "Available by request: workspaces are set up by our team. The demo runs on a simulated market and needs no account.",
]

ISNT = [
    "Not a broker or an exchange, and it does not hold client money or securities.",
    "Not investment advice: no stock tips, paid calls, recommendations or portfolio management. You make your own decisions.",
    "Not a promise of returns: backtests, paper results and past signals do not predict future results, and trading can lose money.",
    "Not a tips channel: VerquoAI does not offer tips or calls through social media, Telegram or WhatsApp.",
]

FAQ: list[tuple[str, str]] = [
    ("What is VerquoAI?",
     "VerquoAI is market analytics software with a mock trading terminal for the Indian stock market. It shows live "
     "market flow, rule-based intraday and swing setups, options open-interest analysis, backtests, paper trading and "
     "AI monitoring agents in one workspace."),
    ("What is the mock trading terminal?",
     "A place to practise: orders in the demo and in paper trading fill against a simulated market with simulated "
     "cash, so you can test your own rules and habits without risking money. Simulated fills are not real fills - "
     "real orders can fill at other prices and pay real charges."),
    ("Does VerquoAI give stock tips or investment advice?",
     "No. VerquoAI does not give tips, paid calls or recommendations. Its signals are the output of published, "
     "rule-based setups shown with their track record, as information for your own analysis. You decide what to trade."),
    ("Does VerquoAI hold my money?",
     "No. VerquoAI never holds money or securities, and nothing you do in the mock terminal reaches an exchange."),
    ("Which markets does it cover?",
     "Indian markets: NSE and BSE stocks, stock futures and index options (NIFTY, SENSEX)."),
    ("How do I get access?",
     "Try the demo on a simulated market - no account needed. It opens backtests, strategies, paper trading, the OI "
     "tools and agents; Market Flow, the setup boards and Analysis are part of a workspace. Request access with the "
     "form on this page and our team replies by email."),
    ("Are the signals profitable?",
     "There is no guarantee. Every signal is followed to its outcome, net of costs, and shown on the board - winners and "
     "losers alike. Setups with a poor recent record are automatically weighted down or suspended."),
]


def json_ld() -> str:
    """Structured data for the landing page: the organization, the website, the
    software and the FAQ (the same answers as the visible FAQ)."""
    url = site_url()
    org_id, site_id = f"{url}/#organization", f"{url}/#website"
    graph: list[dict[str, Any]] = [
        {
            "@type": "Organization", "@id": org_id, "name": SITE_NAME, "url": f"{url}/",
            "logo": f"{url}/static/img/apple-touch-icon.png", "description": ABOUT, "areaServed": "IN",
            "knowsAbout": ["Indian stock market", "NSE", "BSE", "market analytics", "mock trading", "paper trading",
                           "options open interest", "backtesting"],
        },
        {
            "@type": "WebSite", "@id": site_id, "name": SITE_NAME, "url": f"{url}/", "inLanguage": "en-IN",
            "publisher": {"@id": org_id},
        },
        {
            "@type": "SoftwareApplication", "name": SITE_NAME, "url": f"{url}/",
            "applicationCategory": "FinanceApplication", "operatingSystem": "Web browser",
            "description": ABOUT, "publisher": {"@id": org_id},
            "featureList": IS,
        },
        {
            "@type": "FAQPage", "@id": f"{url}/#faq",
            "mainEntity": [{"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}}
                           for q, a in FAQ],
        },
    ]
    # "</" cannot end the <script> block early
    return json.dumps({"@context": "https://schema.org", "@graph": graph}, ensure_ascii=False).replace("</", "<\\/")


def context() -> dict[str, Any]:
    from src.api.legal_pages import RISK_LINE

    return {"risk_line": RISK_LINE, "site_about": ABOUT, "site_is": IS, "site_isnt": ISNT, "site_faq": FAQ, "site_json_ld": json_ld()}
