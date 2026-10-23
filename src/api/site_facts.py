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
    "VerquoAI is software: a browser-based trading terminal for the Indian stock market (NSE and BSE). "
    "It brings live market flow, intraday and swing setup boards, options open-interest analysis, "
    "backtesting, paper trading and AI agents into one workspace, with risk limits and a kill switch. "
    "You connect your own broker account through the broker's API - VerquoAI never holds your money or securities."
)

IS = [
    "A trading terminal and analysis tool for NSE and BSE stocks, stock futures and index options.",
    "Rule-based: every signal comes from published rules applied to closed price bars, and is shown with its "
    "entry, stop, targets and how that setup has performed after costs - losing setups included.",
    "Connected to your own broker account through its API. Orders, positions and funds stay with your broker.",
    "Available by request: workspaces are set up by our team. The demo runs on a simulated market and needs no account.",
    "In your control: automated trading is off until you switch it on, and it works only within the limits you set.",
]

ISNT = [
    "Not a broker or an exchange, and it does not hold client money or securities.",
    "Not investment advice: no stock tips, paid calls, recommendations or portfolio management. You make your own decisions.",
    "Not a promise of returns: backtests and past signals do not predict future results, and trading can lose money.",
    "Not a tips channel: VerquoAI does not offer tips or calls through social media, Telegram or WhatsApp.",
]

FAQ: list[tuple[str, str]] = [
    ("What is VerquoAI?",
     "VerquoAI is a browser-based trading terminal for the Indian stock market. It shows live market flow, "
     "rule-based intraday and swing setups, options open-interest analysis, backtests, paper trading and AI agents "
     "in one workspace. It is software you use with your own broker account."),
    ("Does VerquoAI give stock tips or investment advice?",
     "No. VerquoAI does not give tips, paid calls or recommendations. Its signals are the output of published, "
     "rule-based setups shown with their track record, as information for your own analysis. You decide what to trade."),
    ("Does VerquoAI hold my money or trade without me?",
     "No. VerquoAI never holds money or securities: orders go only through your own broker account. Automated "
     "trading is off by default; it runs only if you switch it on, within the risk, order-value and daily limits you set, "
     "and a kill switch stops all trading at once."),
    ("Which markets does it cover?",
     "Indian markets: NSE and BSE stocks, stock futures and index options (NIFTY, SENSEX). Your broker connection "
     "provides the live market data. When you request access, tell us your broker and we will confirm whether it can be connected."),
    ("How do I get access?",
     "Try the full terminal in the demo on a simulated market - no account needed. Then request access with the form "
     "on this page; our team sets up your workspace and replies by email. You link your broker account each trading day."),
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
            "logo": f"{url}/static/favicon.svg", "description": ABOUT, "areaServed": "IN",
            "knowsAbout": ["Indian stock market", "NSE", "BSE", "trading terminal", "options open interest",
                           "backtesting", "algorithmic trading software"],
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
    return {"site_about": ABOUT, "site_is": IS, "site_isnt": ISNT, "site_faq": FAQ, "site_json_ld": json_ld()}
