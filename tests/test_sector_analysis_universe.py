from src.analysis.scanner import StockScanner


def _stock(sym, trend):
    return {"symbol": sym, "trend_score": trend, "change_1d": 1.0, "rs_rating": 50, "stage": 2}


def test_sector_breakdown_is_per_universe_and_not_replaced_by_later_scan():
    sc = StockScanner()
    big = {s: _stock(s, 60) for s in ("RELIANCE", "TCS", "INFY", "HDFCBANK", "DIXON", "POLYCAB")}
    small = {s: _stock(s, 40) for s in ("RELIANCE", "TCS")}
    # same bookkeeping run_scan does after each universe scan
    for universe, results in (("nifty500", big), ("nifty50", small)):
        sc._analysis_cache.update(results)
        sc._universe_results[universe] = results

    n500 = sc.get_sector_analysis("nifty500")
    n50 = sc.get_sector_analysis("nifty50")
    assert n500["stock_count"] == 6 and n500["universe"] == "nifty500"
    assert n50["stock_count"] == 2
    assert sum(v["stock_count"] for v in n500["sectors"].values()) == 6
    # deep-profile cache keeps every stock from both scans
    assert set(sc._analysis_cache) == set(big)


def test_unscanned_universe_reports_a_clear_error():
    sc = StockScanner()
    sc._universe_results["nifty50"] = {"TCS": _stock("TCS", 50)}
    sc._analysis_cache.update(sc._universe_results["nifty50"])
    assert "NIFTY500" in sc.get_sector_analysis("nifty500")["error"]
    assert sc.get_sector_analysis()["stock_count"] == 1


def test_universe_lists_match_official_counts_and_sectors():
    from src.analysis.nifty500 import NIFTY_500_SECTOR_MAP, NIFTY_500_SYMBOLS
    from src.analysis.nifty_contribution import NIFTY50_WEIGHTS
    from src.analysis.scanner import NIFTY_50_SYMBOLS

    assert len(NIFTY_500_SYMBOLS) >= 500 and len(set(NIFTY_500_SYMBOLS)) == len(NIFTY_500_SYMBOLS)
    assert all(s in NIFTY_500_SECTOR_MAP for s in NIFTY_500_SYMBOLS)
    assert len(NIFTY_50_SYMBOLS) == 50 and set(NIFTY_50_SYMBOLS) <= set(NIFTY_500_SYMBOLS)
    assert set(NIFTY50_WEIGHTS) == set(NIFTY_50_SYMBOLS)


import asyncio
from types import SimpleNamespace as _NS


def test_series_suffixed_listings_resolve_to_index_symbols():
    import src.analysis.scanner as scanner_mod

    class Client:
        async def get_instruments(self, exchange):
            return [_NS(tradingsymbol="HFCL-BE", instrument_type="EQ", instrument_token=11),
                    _NS(tradingsymbol="EMBASSY-RR", instrument_type="EQ", instrument_token=12),
                    _NS(tradingsymbol="BAJAJ-AUTO", instrument_type="EQ", instrument_token=13),
                    _NS(tradingsymbol="INFY", instrument_type="EQ", instrument_token=14)]

    scanner_mod._ALL_NSE_TOKENS.clear()
    tokens = asyncio.run(StockScanner()._resolve_tokens(Client(), ["HFCL", "EMBASSY", "BAJAJ-AUTO", "INFY"]))
    assert tokens == {"HFCL": 11, "EMBASSY": 12, "BAJAJ-AUTO": 13, "INFY": 14}
