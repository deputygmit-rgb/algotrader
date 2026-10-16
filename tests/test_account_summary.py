import pytest

from src.api.market_data import build_account_summary


def test_account_summary_matches_kite_conventions():
    positions = {"net": [
        # carried F&O position: today's m2m differs from total pnl
        {"tradingsymbol": "NIFTY26OCT25200CE", "quantity": 75, "last_price": 131.9, "multiplier": 1,
         "pnl": 1462.5, "m2m": 600.0, "realised": 0},
        # closed intraday: no exposure, realised counts in pnl and m2m
        {"tradingsymbol": "SBIN", "quantity": 0, "last_price": 800.0, "pnl": -120.0, "m2m": -120.0, "realised": -120.0},
    ]}
    holdings = [
        {"tradingsymbol": "MCX", "total_quantity": 50, "average_price": 3216.6, "last_price": 3162.0,
         "close_price": 3150.0, "pnl": -2730.0},
        # MTF-only holding: top-level quantity 0, resolved total 50
        {"tradingsymbol": "ATHERENERG", "quantity": 0, "total_quantity": 50, "average_price": 1554.6,
         "last_price": 1470.0, "close_price": 1480.0, "pnl": -4230.0},
    ]
    margins = {"equity": {"net": 60000.0, "utilised": {"debits": 40000.0}}}

    a = build_account_summary(positions, holdings, margins, trades_today=3)

    assert a["positions_day_pnl"] == 480.0
    assert a["holdings_day_pnl"] == pytest.approx(12 * 50 - 10 * 50)
    assert a["day_pnl"] == pytest.approx(480.0 + 100.0)
    assert a["total_pnl"] == pytest.approx(1462.5 - 120.0 - 2730.0 - 4230.0)
    assert a["realised_today"] == -120.0
    assert a["open_positions"] == 1
    assert a["exposure"] == pytest.approx(75 * 131.9)
    assert a["utilisation_pct"] == pytest.approx(40.0)
    assert a["trades_today"] == 3
    assert a["holdings_value"] == pytest.approx(50 * 3162.0 + 50 * 1470.0)


def test_account_summary_tolerates_missing_parts():
    a = build_account_summary(None, [], None, None)
    assert a["day_pnl"] == 0 and a["total_pnl"] == 0 and a["exposure"] == 0
    assert a["utilisation_pct"] == 0 and a["trades_today"] is None


def test_holding_without_previous_close_adds_no_day_change():
    h = [{"tradingsymbol": "X", "total_quantity": 10, "average_price": 100, "last_price": 110, "close_price": 0, "pnl": 0}]
    a = build_account_summary({"net": []}, h, None, 0)
    assert a["holdings_day_pnl"] == 0
    assert a["holdings_pnl"] == 100.0   # broker pnl missing -> computed from prices
