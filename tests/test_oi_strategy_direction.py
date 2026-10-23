"""The OI Strategy tab follows the OI bias the Options OI tab shows; it never reports the opposite."""
from src.options.oi_analysis import OptionsOIReport, OptionsOIStrike
from src.options.oi_strategy import OIStrategyEngine, bias_side


def _report(bias):
    # three bearish option-buy readings: low PCR, spot just under the CE wall, spot well above max pain
    strikes = [OptionsOIStrike(strike=k, ce_ltp=120.0, pe_ltp=110.0, is_atm=(k == 25000))
               for k in (24900, 25000, 25100)]
    return OptionsOIReport(underlying="NIFTY", spot_price=25000.0, atm_strike=25000.0, expiry="2026-10-28",
                           pcr_oi=0.45, max_pain=24600.0, max_ce_oi_strike=25100.0, max_ce_oi=1000,
                           max_pe_oi_strike=24500.0, strikes=strikes, bias=bias)


def test_signals_against_the_bias_are_flagged_and_form_no_combined_trade():
    r = OIStrategyEngine().scan_for_signals(_report("MILDLY BULLISH"))
    bearish = [s for s in r.signals if s.direction == "BEARISH"]
    assert len(bearish) >= 3 and all(s.metadata.get("against_bias") for s in bearish)
    assert r.combined_direction == "NEUTRAL"
    assert not any(s.signal_type.startswith("COMBINED") for s in r.signals)
    assert "against the OI bias (MILDLY BULLISH)" in r.direction_note
    assert r.bias == "MILDLY BULLISH"                    # the tab headline is the OI bias


def test_signals_that_agree_with_the_bias_make_the_combined_trade():
    r = OIStrategyEngine().scan_for_signals(_report("MILDLY BEARISH"))
    assert r.combined_direction == "BEARISH" and any(s.signal_type == "COMBINED_BEARISH" for s in r.signals)
    assert not any(s.metadata.get("against_bias") for s in r.signals)
    assert "confirmed" in r.direction_note


def test_bias_side():
    assert bias_side("MILDLY BULLISH") == "BULLISH" and bias_side("BEARISH") == "BEARISH"
    assert bias_side("NEUTRAL") == "" and bias_side("") == ""
