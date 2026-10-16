from src.analysis.scanner import StockScanner


def test_deep_verdict_uses_market_sector_and_stock_context() -> None:
    scanner = StockScanner()
    stock = {
        "stage": 2,
        "super_performance": {"grade": "A"},
        "rs_rating": 82,
        "accumulation": {"status": "strong_accumulation"},
        "vcp_detected": True,
        "ichimoku_signal": "bullish",
    }
    weekly = {"trend": "bullish"}
    monthly = {"trend": "bullish"}
    market = {"trend": "bullish", "status": "bullish"}
    sector = {"sector": "IT", "status": "bullish"}

    verdict = scanner._compute_verdict(stock, weekly, monthly, market, sector)

    assert verdict["action"] in {"BUY", "STRONG BUY"}
    assert "market" in verdict["recommendation_text"].lower()
    assert "sector" in verdict["recommendation_text"].lower()
    assert "daily" in verdict["recommendation_text"].lower() or "weekly" in verdict["recommendation_text"].lower() or "month" in verdict["recommendation_text"].lower()
