"""Backup market data (src/api/backup_quotes.py) and the broker client's quote failover."""
import asyncio
import json

import pytest

from src.api import backup_quotes as bq_module
from src.api.backup_quotes import BackupQuotes, is_outage
from src.utils import app_settings
from src.utils.exceptions import AuthenticationError, DataError, NetworkError

HEAD = ("pSymbol,pGroup,pExchSeg,pInstType,pSymbolName,pTrdSymbol,pOptionType,pScripRefKey,pISIN,pAssetCode,"
        "pSubGroup,pCombinedSymbol,pDesc,pAmcCode,pContractId,dTickSize ,lLotSize,lExpiryDate \n")
CASH = HEAD + "\n".join([
    "26000,,nse_cm,,NIFTY,NIFTY,,NIFTY,,,,,NIFTY,,,-1,1,-1",
    "2885,EQ,nse_cm,,RELIANCE,RELIANCE-EQ,,RELIANCE,,,,,,,,5,1,-1",
    "2031,EQ,nse_cm,,M&M,M&M-EQ,,M&M,,,,,,,,5,1,-1",
    "9999,BE,nse_cm,,ODD,ODD-BE,,ODD,,,,,,,,5,1,-1",
    "9998,EQ,nse_cm,,TWIN,TWIN-EQ,,TWIN,,,,,,,,5,1,-1",
    "9997,BE,nse_cm,,TWIN,TWIN-BE,,TWIN,,,,,,,,5,1,-1",
])
FO = HEAD + "\n".join([
    "48886,XX,nse_fo,FUTSTK,INFY,INFY26OCTFUT,,INFY,,,,,,,,10,400,1477578600",
    "48704,XX,nse_fo,FUTIDX,NIFTY,NIFTY26OCTFUT,,NIFTY,,,,,,,,10,65,1477578600",
    "131539,XX,nse_fo,OPTSTK,IOC,IOC26NOV84CE,CE,IOC,,,,,,,,1,4875,1479911400",
])
# the documented quote response
SENSEX = {"exchange_token": "SENSEX", "display_symbol": "SENSEX-IN", "exchange": "bse_cm", "lstup_time": "1757915078",
          "ltp": "81809.3400", "last_traded_quantity": "0", "total_buy": "0", "total_sell": "0", "last_volume": "0",
          "change": "-95.3600", "per_change": "-0.1200",
          "ohlc": {"open": "81925.5100", "high": "81998.5100", "low": "81779.8200", "close": "81904.7000"},
          "depth": {"buy": [{"price": "81809.0", "quantity": "10", "orders": "2"}], "sell": []}}


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr(app_settings, "SETTINGS_FILE", str(tmp_path / "app_settings.json"))
    monkeypatch.delenv("KOTAK_NEO_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("KOTAK_NEO_BASE_URL", raising=False)
    monkeypatch.setattr(bq_module, "_BACKUP", None)
    return tmp_path


def loaded() -> BackupQuotes:
    b = BackupQuotes()
    b._cash, b._fut = BackupQuotes.parse_cash(CASH), BackupQuotes.parse_futures(FO)
    return b


def test_instrument_mapping():
    b = loaded()
    assert b.query_for("NSE:RELIANCE") == ("nse_cm", "2885")
    assert b.query_for("NSE:M&M") == ("nse_cm", "2031")
    assert b.query_for("NSE:ODD") == ("nse_cm", "9999")                    # BE series when that is all there is
    assert b.query_for("NSE:TWIN") == ("nse_cm", "9998")                   # EQ preferred
    assert b.query_for("NFO:INFY26OCTFUT") == ("nse_fo", "48886")          # same futures symbols as the primary
    assert b.query_for("NFO:IOC26NOV84CE") is None                         # options are not loaded
    assert b.query_for("NSE:NIFTY 50") == ("nse_cm", "Nifty 50")
    assert b.query_for("BSE:SENSEX") == ("bse_cm", "SENSEX")
    assert b.query_for("NSE:NOPE") is None


def test_quote_parsing_from_the_documented_response():
    q = BackupQuotes.to_quote(SENSEX)
    assert q.last_price == 81809.34 and q.net_change == -95.36
    assert (q.ohlc.open, q.ohlc.high, q.ohlc.low, q.ohlc.close) == (81925.51, 81998.51, 81779.82, 81904.7)
    assert q.day_high == 81998.51 and q.depth.buy[0].quantity == 10 and q.source == "backup"


def test_get_quote_batches_and_maps_back(settings, monkeypatch):
    b = loaded()
    b._master_day = __import__("datetime").date.today().isoformat()
    monkeypatch.setattr(BackupQuotes, "token", staticmethod(lambda: "tok"))
    calls = []

    async def fake_fetch(self, queries, quote_type="all"):
        calls.append(queries)
        return [{**SENSEX, "exchange": seg, "exchange_token": ident, "ltp": "100"} for seg, ident in queries]
    monkeypatch.setattr(BackupQuotes, "fetch_raw", fake_fetch)
    monkeypatch.setattr(bq_module, "BATCH", 2)
    out = asyncio.run(b.get_quote(["NSE:RELIANCE", "NSE:M&M", "NFO:INFY26OCTFUT", "NSE:NOPE"]))
    assert set(out) == {"NSE:RELIANCE", "NSE:M&M", "NFO:INFY26OCTFUT"} and len(calls) == 2
    assert b.stats["served"] == 3


def test_token_only_goes_to_the_brokers_hosts(settings):
    app_settings.set_setting("backup_quotes_base_url", "https://evil.example.com")
    assert BackupQuotes.base_url() == bq_module.DEFAULT_BASE_URL
    app_settings.set_setting("backup_quotes_base_url", "https://e22.kotaksecurities.com")
    assert BackupQuotes.base_url() == "https://e22.kotaksecurities.com"


def test_what_counts_as_an_outage():
    assert is_outage(NetworkError("down")) and is_outage(AuthenticationError("expired")) and is_outage(TimeoutError())
    assert not is_outage(DataError("unknown instrument")) and not is_outage(ValueError("bad"))


class _Primary:
    """A KiteClient whose quote request we control."""

    def __init__(self, error=None):
        from src.api.client import KiteClient

        self.client = KiteClient.__new__(KiteClient)
        self.client._quote_limiter = None
        self.calls = 0
        self.error = error

        async def request(method, path, limiter=None, params=None):
            self.calls += 1
            if self.error:
                raise self.error
            return {k: {"last_price": 1.0} for _, k in params}
        self.client._request = request


class _FakeBackup:
    configured = True

    def __init__(self):
        self.calls, self.until, self.failed = 0, 0, []

    def prefer_backup(self):
        return bool(self.failed)

    def primary_failed(self, e):
        self.failed.append(e)

    async def get_quote(self, instruments):
        from src.data.models import Quote

        self.calls += 1
        return {k: Quote(last_price=2.0) for k in instruments}


def test_client_fails_over_and_stays_on_the_backup_for_a_while(monkeypatch):
    backup = _FakeBackup()
    monkeypatch.setattr(bq_module, "backup_quotes", lambda: backup)
    p = _Primary(NetworkError("primary down"))
    out = asyncio.run(p.client.get_quote(["NSE:INFY"]))
    assert out["NSE:INFY"].last_price == 2.0 and p.client.last_quote_source == "backup" and backup.failed
    asyncio.run(p.client.get_quote(["NSE:INFY"]))
    assert p.calls == 1 and backup.calls == 2                          # cooldown: the primary is not retried yet


def test_bad_requests_do_not_fail_over_and_no_backup_means_the_error(monkeypatch):
    backup = _FakeBackup()
    monkeypatch.setattr(bq_module, "backup_quotes", lambda: backup)
    with pytest.raises(DataError):
        asyncio.run(_Primary(DataError("unknown instrument")).client.get_quote(["NSE:X"]))
    assert backup.calls == 0
    backup.configured = False
    with pytest.raises(NetworkError):
        asyncio.run(_Primary(NetworkError("down")).client.get_quote(["NSE:X"]))


def test_healthy_primary_is_used(monkeypatch):
    backup = _FakeBackup()
    monkeypatch.setattr(bq_module, "backup_quotes", lambda: backup)
    p = _Primary()
    assert asyncio.run(p.client.get_quote(["NSE:INFY"]))["NSE:INFY"].last_price == 1.0
    assert p.client.last_quote_source == "primary" and backup.calls == 0


# --- the backup feed: live data on days nobody links the primary broker -------------

FO_FULL = HEAD + "\n".join([
    "48886,XX,nse_fo,FUTSTK,INFY,INFY26OCTFUT,,INFY,,,,,,,,10,400,1477578600",
    "48924,XX,nse_fo,FUTSTK,M&M,M&M26OCTFUT,,M&M,,,,,,,,10,200,1477578600",
])
CASH_FULL = CASH + "\n1594,EQ,nse_cm,,INFY,INFY-EQ,,INFY,,,,,,,,5,1,-1"


@pytest.fixture
def feed(settings, monkeypatch, tmp_path):
    import datetime as dt

    from src.api import backup_feed

    monkeypatch.setattr(backup_feed, "TOKENS_PATH", str(tmp_path / "tokens.json"))
    monkeypatch.setattr(backup_feed, "_tokens", {})
    monkeypatch.setattr(backup_feed, "_tokens_mtime", 0.0)
    monkeypatch.setattr(backup_feed, "_saved_on", {})
    monkeypatch.setattr(backup_feed, "_SERVICE", None)
    monkeypatch.setattr(BackupQuotes, "token", staticmethod(lambda: "tok"))
    monkeypatch.setattr(bq_module, "backup_quotes", lambda: backup)
    monkeypatch.setattr(backup_feed, "backup_quotes", lambda: backup)
    backup = BackupQuotes()
    backup._cash = BackupQuotes.parse_cash(CASH_FULL)
    backup._fut_meta = BackupQuotes.parse_futures_meta(FO_FULL)
    backup._fut = {k: m["token"] for k, m in backup._fut_meta.items()}
    backup._master_day = dt.date.today().isoformat()

    async def fake_fetch(self, queries, quote_type="all"):
        return [{"exchange": s, "exchange_token": i, "ltp": "1500", "change": "15",
                 "ohlc": {"open": "1490", "high": "1510", "low": "1480", "close": "1485"}} for s, i in queries]
    monkeypatch.setattr(BackupQuotes, "fetch_raw", fake_fetch)
    return backup


def test_instruments_keep_the_primarys_numbers(feed):
    from src.api.backup_feed import TOKEN_OFFSET, BackupMarketClient, remember_tokens
    from src.data.models import Instrument

    remember_tokens("NSE", [Instrument(instrument_token=408065, tradingsymbol="INFY", instrument_type="EQ")])
    nse = {i.tradingsymbol: i for i in asyncio.run(BackupMarketClient().get_instruments("NSE"))}
    assert nse["INFY"].instrument_token == 408065                        # stored bars still match
    assert nse["RELIANCE"].instrument_token == TOKEN_OFFSET + 2885      # unknown: a number that cannot clash
    nfo = {i.tradingsymbol: i for i in asyncio.run(BackupMarketClient().get_instruments("NFO"))}
    assert nfo["INFY26OCTFUT"].instrument_type == "FUT" and nfo["INFY26OCTFUT"].name == "INFY"
    assert nfo["INFY26OCTFUT"].expiry == "2026-10-27" and nfo["INFY26OCTFUT"].lot_size == 400


def test_hub_uses_the_backup_when_the_source_is_not_linked(feed, monkeypatch):
    from types import SimpleNamespace

    from src.api.shared_market import SharedMarketHub

    unlinked = SimpleNamespace(is_demo=False, is_authenticated=False)
    hub = SharedMarketHub(account_id="AC1", source_factory=lambda a: unlinked)
    assert hub.source_kind() == "backup" and hub.available_for("VW1")
    svc = hub._source()
    flow = asyncio.run(svc.get_fno_flow())
    assert not flow.get("error"), flow
    assert {r["symbol"] for r in flow["rows"]} >= {"INFY"} and flow["data_source"] == "backup"
    app_settings.set_setting("quote_source_mode", "kite_only")
    assert hub.source_kind() is None and not hub.available_for("VW1")
    app_settings.set_setting("quote_source_mode", "kite_first")
    hub_off = SharedMarketHub(account_id="", source_factory=lambda a: unlinked)
    assert hub_off.source_kind() is None                                 # shared market data switched off


def test_kotak_first_asks_the_backup_before_kite(monkeypatch, settings):
    backup = _FakeBackup()
    monkeypatch.setattr(bq_module, "backup_quotes", lambda: backup)
    app_settings.set_setting("quote_source_mode", "kotak_first")
    p = _Primary()
    assert asyncio.run(p.client.get_quote(["NSE:INFY"]))["NSE:INFY"].last_price == 2.0 and p.calls == 0
    app_settings.set_setting("quote_source_mode", "kite_only")
    p2 = _Primary(NetworkError("down"))
    with pytest.raises(NetworkError):
        asyncio.run(p2.client.get_quote(["NSE:INFY"]))
    assert backup.calls == 1
