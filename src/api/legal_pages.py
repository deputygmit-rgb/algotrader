"""About, Disclaimer and Disclosures: what VerquoAI is, the risks, and how it works.

Positioning: market analytics software with a mock trading terminal - practise on a
simulated market, backtest, paper trade. It is not a broker, not an adviser, and it
holds no money or securities. Pages stay broker-neutral and do not describe API
connections (accounts set up by our team are told how to connect directly).

Every statement must stay true of the product. Facts only the operator can confirm
come from .env and are shown only when set:

* ``LEGAL_ENTITY_NAME``   - the legal entity that runs VerquoAI (e.g. a company name);
* ``LEGAL_ADDRESS``       - its registered address;
* ``LEGAL_CIN``           - company identification number, if any;
* ``SEBI_REGISTRATION``   - a SEBI registration (type and number) if the operator holds
  one. Unset, the pages state that VerquoAI is not registered with SEBI;
* ``PRIVACY_CONTACT_EMAIL`` - a public contact address (else pages point to the contact form).
"""
from __future__ import annotations

import os
from typing import Any

SITE = "VerquoAI"
RISK_LINE = ("VerquoAI is market analytics software with a mock trading terminal. Nothing on this website or in the "
             "app is investment advice or a recommendation to buy or sell any security. Trading in equities and "
             "derivatives carries a high risk of loss.")
SEBI_STUDIES = ("SEBI's study published in January 2023 found that 9 out of 10 individual traders in the equity "
                "futures and options segment incurred net losses in FY 2021-22, and its study published in "
                "September 2024 found that about 93% of individual F&O traders made losses between FY 2021-22 and "
                "FY 2023-24.")


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def contact_email() -> str:
    """Shown on public pages only when set on purpose (never the admin's own address)."""
    return _env("PRIVACY_CONTACT_EMAIL").split(",")[0].strip()


def operator() -> str:
    return _env("LEGAL_ENTITY_NAME") or SITE


def regulatory_status() -> str:
    reg = _env("SEBI_REGISTRATION")
    if reg:
        return f"{operator()} holds SEBI registration {reg}. Its scope is limited to what that registration covers."
    return (f"{SITE} is not registered with the Securities and Exchange Board of India (SEBI) as an investment "
            "adviser, research analyst, portfolio manager or stock broker. We do not manage money, give "
            "personalised advice or tell anyone what to buy or sell. If you want advice suited to your finances, "
            "consult a SEBI-registered investment adviser.")


def pages() -> dict[str, dict[str, Any]]:
    """{slug: {title, description, sections: [(heading, [paragraphs])]}}."""
    who_runs_it = ([("Who runs it", [
        f"{SITE} is operated by {_env('LEGAL_ENTITY_NAME')}" + (f", {_env('LEGAL_ADDRESS')}" if _env("LEGAL_ADDRESS") else "")
        + (f" (CIN {_env('LEGAL_CIN')})" if _env("LEGAL_CIN") else "") + "."])] if _env("LEGAL_ENTITY_NAME") else [])
    return {
        "about": {
            "title": "About VerquoAI",
            "description": "VerquoAI is market analytics software with a mock trading terminal for the Indian stock market.",
            "sections": [
                ("What VerquoAI is", [
                    f"{SITE} is market analytics software for the Indian stock market (NSE and BSE), with a mock "
                    "trading terminal. It brings live market flow, rule-based intraday and swing setup boards, "
                    "options open-interest analysis, backtesting, session replay, paper trading and AI monitoring "
                    "agents into one workspace.",
                    "The mock trading terminal lets you practise: orders in the demo and in paper trading fill "
                    "against a simulated market, with simulated cash. Nothing you do there reaches an exchange.",
                ]),
                ("Who it is for", [
                    "Traders who want to study the market and test their own rules before they risk money, and who "
                    "make their own decisions.",
                    "It is not for anyone looking for tips, calls or guaranteed returns. Trading is risky, and no tool "
                    "changes that.",
                ]),
                ("What it is not", [
                    "Not a broker or an exchange, and it holds no client money or securities.",
                    "Not investment advice: no tips, paid calls, recommendations or portfolio management.",
                    "Not a promise of returns: backtests, paper results and past signals do not predict future results.",
                ]),
                ("Access", [
                    "The demo runs on a simulated market and needs no account. Workspaces are set up by our team on "
                    "request; use the request-access form on the home page.",
                ]),
                *who_runs_it,
            ],
        },
        "disclaimer": {
            "title": "Disclaimer",
            "description": "VerquoAI is analytics software, not investment advice. Read the risks before you trade.",
            "sections": [
                ("Not investment advice", [
                    f"{SITE} provides market data analytics, software tools and a mock trading terminal for "
                    "information and practice only. A stock appearing on a board, a signal, a score or a backtest "
                    "result is not a recommendation to buy or sell, and does not mean the price will move that way.",
                ]),
                ("Regulatory status", [regulatory_status()]),
                ("Trading risk", [
                    "Trading in equities, futures and options involves substantial risk and is not suitable for "
                    "everyone. Leverage magnifies losses: in futures and in options writing you can lose more than "
                    "the margin you deposit.",
                    SEBI_STUDIES,
                ]),
                ("Mock trading is not real trading", [
                    "Fills in the demo and in paper trading are simulated. Real orders can fill at different prices, "
                    "partly or not at all, and pay real charges and taxes. Results in the mock terminal do not show "
                    "what you would have earned or lost in the market.",
                ]),
                ("Market data and accuracy", [
                    "Prices come from third-party market data. Data can be delayed, incomplete, adjusted late for "
                    "corporate actions, or occasionally wrong, and the service can be unavailable. We do not guarantee "
                    "the accuracy, completeness, timeliness or availability of any information. Confirm prices, "
                    "levels and contract details independently before you act.",
                ]),
                ("Past results do not predict future results", [
                    "Signals, track records, backtests and replays describe what happened under the stated rules, net "
                    "of the costs the engine assumes. Past performance, actual or simulated, does not guarantee "
                    "future results.",
                ]),
                ("Your responsibility", [
                    "You alone decide whether to trade, what, how much to risk and when to exit, and you are "
                    "responsible for your margin, exchange requirements and taxes.",
                ]),
                ("Limitation of liability", [
                    f"To the extent permitted by law, {operator()}, its team and its data providers are not liable "
                    f"for any loss or damage arising from the use of {SITE} or reliance on its information.",
                ]),
            ],
        },
        "disclosures": {
            "title": "Disclosures",
            "description": "How VerquoAI works, its regulatory status, data sources and limits.",
            "sections": [
                ("Nature of the service", [
                    f"{SITE} is software: market analytics and a mock trading terminal. It gives everyone with the "
                    "same access the same information at the same time. It does not give personalised advice, manage "
                    "portfolios, or hold money or securities.",
                ]),
                ("Regulatory status", [
                    regulatory_status(),
                    "We will update this page before offering any service that needs registration.",
                ]),
                ("How signals are produced", [
                    "Every signal comes from fixed rules applied mechanically to closed price bars - the same rules "
                    "for every stock and every user. Nobody picks a stock for the boards by hand; symbols you track "
                    "yourself are visible only to you.",
                    "Each signal is followed to its outcome and shown with it, winners and losers alike, net of the "
                    "slippage and charges the engine assumes. A setup's recent record raises or lowers the quality of "
                    "its new signals and can suspend it.",
                ]),
                ("Data sources and names", [
                    "Prices are exchange market data received from third-party market data providers. VerquoAI is not "
                    "affiliated with or endorsed by any exchange or by SEBI. Index and company names belong to their "
                    "owners.",
                ]),
                ("Examples and results", [
                    "Examples, previews and the demo use simulated data and are illustrative. Track records shown in "
                    "the product are statistics of the stated rules, not a promise or a record of client profits.",
                ]),
                ("Derivatives risk", [SEBI_STUDIES]),
            ],
        },
    }
