"""Backup market data: live quotes from Kotak Neo when the primary broker's quotes fail.

The primary broker connection (``KiteClient``) asks this module for quotes only
when its own quote call fails for an outage-type reason (network, timeout,
server error, rate limit, expired session). The backup then serves quotes for
``COOLDOWN_S`` before the primary is tried again, so a broker that is down does
not slow every request.

What it covers: live quotes - last price, OHLC, change, depth, volume / OI when
the backup reports them - for NSE cash, NSE futures and the main indices. That
is what Market Flow, prices on every page, the Day Trade marking and the shared
view-only feed read. Historical candles are not covered (the backup's API does
not offer them): those keep coming from the primary or from the stored bars.

Security: the backup needs only its API **access token** (from the broker's
Trade API dashboard). That token reads quotes and the instrument master; it
cannot log in or trade without the account's TOTP and MPIN, which this app never
stores. The token is kept encrypted in ``data/app_settings.json`` (admin panel)
or given as ``KOTAK_NEO_ACCESS_TOKEN`` in .env.

Instrument mapping (scrip master CSVs, public, refreshed daily): futures use the
same NSE symbols on both sides (``INFY26OCTFUT``); cash ``NSE:INFY`` is
``INFY-EQ`` (``pSymbolName`` + series); indices are queried by their exact,
case-sensitive names (``Nifty 50``).
"""
from __future__ import annotations

import asyncio
import csv
import io
import re
import time
from datetime import date, datetime, timezone
from typing import Any, Optional
from urllib.parse import quote as urlquote

import httpx

from src.data.models import OHLC, DepthItem, LTPQuote, MarketDepth, OHLCQuote, Quote
from src.utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_BASE_URL = "https://cis.kotaksecurities.com"
MASTER_PUBLIC = "https://lapi.kotaksecurities.com/wso2-scripmaster/v1/prod"
COOLDOWN_S = 60.0                  # after a primary failure, serve from the backup this long
BATCH = 40                          # instruments per quote request (they go in the URL path)
TIMEOUT_S = 10.0
NSE_EPOCH = 315513000              # the master's expiry seconds count from 1980-01-01 IST
CASH_SERIES = ("EQ", "BE", "BZ", "SM", "ST", "RR", "IV")   # preferred order; RR = REITs, IV = InvITs
INDEX_NAMES = {
    "NSE:NIFTY 50": ("nse_cm", "Nifty 50"),
    "NSE:NIFTY BANK": ("nse_cm", "Nifty Bank"),
    "NSE:NIFTY FIN SERVICE": ("nse_cm", "Nifty Fin Service"),
    "NSE:NIFTY MID SELECT": ("nse_cm", "Nifty Mid Select"),
    "NSE:NIFTY IT": ("nse_cm", "Nifty IT"),
    "NSE:INDIA VIX": ("nse_cm", "India VIX"),
    "BSE:SENSEX": ("bse_cm", "SENSEX"),
    "BSE:BANKEX": ("bse_cm", "BANKEX"),
}
# fields seen in the quote response, by what the app calls them (first one present wins)
VOLUME_FIELDS = ("volume", "total_volume", "vtt", "last_volume")
OI_FIELDS = ("open_int", "oi", "open_interest")
AVG_FIELDS = ("average_price", "avg_price", "atp")


def _num(d: dict[str, Any], *names: str, default: float = 0.0) -> float:
    for n in names:
        v = d.get(n)
        if v not in (None, "", "-"):
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return default


class BackupQuotes:
    def __init__(self) -> None:
        self._client: Optional[httpx.AsyncClient] = None
        self._cash: dict[str, str] = {}            # "INFY" -> pSymbol (nse_cm)
        self._fut: dict[str, str] = {}             # "INFY26OCTFUT" -> pSymbol (nse_fo)
        self._fut_meta: dict[str, dict[str, Any]] = {}   # "INFY26OCTFUT" -> {name, expiry, lot}
        self._master_day: Optional[str] = None
        self._master_lock = asyncio.Lock()
        self._prefer_until = 0.0
        self.stats: dict[str, Any] = {"served": 0, "failovers": 0, "last_ok": None, "last_error": None,
                                      "primary_error": None, "active_until": None}

    # ── configuration ───────────────────────────────────────────────
    @staticmethod
    def token() -> str:
        from src.auth.user_config import decrypt_secret
        from src.utils.app_settings import get_setting

        stored = get_setting("backup_quotes_token", "KOTAK_NEO_ACCESS_TOKEN")
        if not stored:
            return ""
        try:
            value, _legacy = decrypt_secret(stored)
            return value
        except ValueError:
            return ""

    @staticmethod
    def base_url() -> str:
        from src.utils.app_settings import get_setting

        url = get_setting("backup_quotes_base_url", "KOTAK_NEO_BASE_URL", DEFAULT_BASE_URL).strip().rstrip("/")
        # the token travels to this host: only the broker's own https hosts
        return url if re.fullmatch(r"https://[a-z0-9.-]+\.kotaksecurities\.com", url) else DEFAULT_BASE_URL

    @staticmethod
    def enabled() -> bool:
        from src.utils.app_settings import get_setting

        return get_setting("backup_quotes_enabled", "KOTAK_NEO_ENABLED", "1") not in ("0", "false", "False", "")

    @property
    def configured(self) -> bool:
        return self.enabled() and bool(self.token())

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=TIMEOUT_S, follow_redirects=False)
        return self._client

    # ── failover state ──────────────────────────────────────────────
    def prefer_backup(self) -> bool:
        return self.configured and time.monotonic() < self._prefer_until

    def primary_failed(self, error: Exception) -> None:
        if time.monotonic() >= self._prefer_until:
            self.stats["failovers"] += 1
            logger.warning("quotes_failover_to_backup", error=str(error)[:200])
        self._prefer_until = time.monotonic() + COOLDOWN_S
        self.stats["primary_error"] = {"at": datetime.now().isoformat(timespec="seconds"), "error": str(error)[:200]}
        self.stats["active_until"] = datetime.fromtimestamp(time.time() + COOLDOWN_S).isoformat(timespec="seconds")

    def status(self) -> dict[str, Any]:
        return {"configured": self.configured, "enabled": self.enabled(), "has_token": bool(self.token()),
                "base_url": self.base_url(), "active": self.prefer_backup(), "master_day": self._master_day,
                "mapped": {"cash": len(self._cash), "futures": len(self._fut)}, **self.stats}

    # ── instrument master ───────────────────────────────────────────
    async def _master_urls(self) -> dict[str, str]:
        """{"nse_cm": url, "nse_fo": url} from the file-paths API, or today's public paths."""
        urls: dict[str, str] = {}
        try:
            r = await self._http().get(f"{self.base_url()}/script-details/1.0/masterscrip/file-paths",
                                       headers={"Authorization": self.token()})
            if r.status_code == 200:
                for u in (r.json().get("data") or {}).get("filesPaths") or []:
                    name = u.rsplit("/", 1)[-1]
                    if name.startswith("nse_cm"):
                        urls["nse_cm"] = u
                    elif name == "nse_fo.csv":
                        urls["nse_fo"] = u
        except (httpx.HTTPError, ValueError) as e:
            logger.warning("backup_master_paths_failed", error=str(e))
        day = date.today().isoformat()
        urls.setdefault("nse_cm", f"{MASTER_PUBLIC}/{day}/transformed-v1/nse_cm-v1.csv")
        urls.setdefault("nse_fo", f"{MASTER_PUBLIC}/{day}/transformed/nse_fo.csv")
        return urls

    @staticmethod
    def parse_cash(text: str) -> dict[str, str]:
        best: dict[str, tuple[int, str]] = {}
        for row in csv.DictReader(io.StringIO(text)):
            name, group, token = (row.get("pSymbolName") or "").strip(), (row.get("pGroup") or "").strip(), \
                (row.get("pSymbol") or "").strip()
            if not name or not token or group not in CASH_SERIES:
                continue
            rank = CASH_SERIES.index(group)
            if name not in best or rank < best[name][0]:
                best[name] = (rank, token)
        return {k: v for k, (_, v) in best.items()}

    @staticmethod
    def parse_futures(text: str) -> dict[str, str]:
        return {ts: m["token"] for ts, m in BackupQuotes.parse_futures_meta(text).items()}

    @staticmethod
    def parse_futures_meta(text: str) -> dict[str, dict[str, Any]]:
        """{"INFY26OCTFUT": {token, name, expiry "YYYY-MM-DD", lot, kind FUTSTK/FUTIDX}}."""
        out: dict[str, dict[str, Any]] = {}
        for row in csv.DictReader(io.StringIO(text)):
            row = {k.strip(): v for k, v in row.items() if k}
            kind = (row.get("pInstType") or "").strip()
            if kind not in ("FUTSTK", "FUTIDX"):
                continue
            ts, token = (row.get("pTrdSymbol") or "").strip(), (row.get("pSymbol") or "").strip()
            try:
                expiry = datetime.fromtimestamp(int(float(row.get("lExpiryDate") or 0)) + NSE_EPOCH,
                                                tz=timezone.utc).date().isoformat()
            except (TypeError, ValueError, OSError):
                expiry = ""
            if ts and token:
                out[ts] = {"token": token, "name": (row.get("pSymbolName") or "").strip(), "expiry": expiry,
                           "lot": int(float(row.get("lLotSize") or 0) or 0), "kind": kind}
        return out

    async def _ensure_master(self) -> None:
        day = date.today().isoformat()
        if self._master_day == day and self._cash:
            return
        async with self._master_lock:
            if self._master_day == day and self._cash:
                return
            urls = await self._master_urls()
            cash = await self._download(urls["nse_cm"])
            fo = await self._download(urls["nse_fo"])
            # parse off the event loop: the F&O file is ~30 MB of options we skip
            self._cash = await asyncio.to_thread(self.parse_cash, cash)
            self._fut_meta = await asyncio.to_thread(self.parse_futures_meta, fo)
            self._fut = {ts: m["token"] for ts, m in self._fut_meta.items()}
            self._master_day = day
            logger.info("backup_master_loaded", cash=len(self._cash), futures=len(self._fut))

    async def _download(self, url: str) -> str:
        if not url.startswith("https://lapi.kotaksecurities.com/"):
            raise ValueError("unexpected instrument master location")
        r = await self._http().get(url, timeout=60.0)
        r.raise_for_status()
        return r.text

    # ── quotes ──────────────────────────────────────────────────────
    def query_for(self, key: str) -> Optional[tuple[str, str]]:
        """("nse_cm", "2885") for "NSE:RELIANCE"; None when the backup cannot map it."""
        if key in INDEX_NAMES:
            return INDEX_NAMES[key]
        exch, _, sym = key.partition(":")
        if exch == "NSE" and sym in self._cash:
            return "nse_cm", self._cash[sym]
        if exch == "NFO" and sym in self._fut:
            return "nse_fo", self._fut[sym]
        return None

    @staticmethod
    def to_quote(d: dict[str, Any]) -> Quote:
        ohlc = d.get("ohlc") or {}
        depth = d.get("depth") or {}

        def side(items: Any) -> list[DepthItem]:
            out = []
            for it in items or []:
                price, qty = _num(it, "price"), _num(it, "quantity")
                if price or qty:
                    out.append(DepthItem(price=price, quantity=int(qty), orders=int(_num(it, "orders"))))
            return out

        ts = d.get("lstup_time")
        try:
            stamp = datetime.fromtimestamp(int(float(ts)), tz=timezone.utc).astimezone().replace(tzinfo=None).isoformat()
        except (TypeError, ValueError, OSError):
            stamp = None
        o = OHLC(open=_num(ohlc, "open"), high=_num(ohlc, "high"), low=_num(ohlc, "low"), close=_num(ohlc, "close"))
        return Quote(
            instrument_token=0, timestamp=stamp, last_price=_num(d, "ltp", "last_price"),
            last_quantity=int(_num(d, "last_traded_quantity")), average_price=_num(d, *AVG_FIELDS),
            volume=int(_num(d, *VOLUME_FIELDS)), buy_quantity=int(_num(d, "total_buy")),
            sell_quantity=int(_num(d, "total_sell")), ohlc=o, day_high=o.high, day_low=o.low,
            net_change=_num(d, "change"), oi=int(_num(d, *OI_FIELDS)),
            depth=MarketDepth(buy=side(depth.get("buy")), sell=side(depth.get("sell"))),
            source="backup",
        )

    async def fetch_raw(self, queries: list[tuple[str, str]], quote_type: str = "all") -> list[dict[str, Any]]:
        path = ",".join(f"{seg}|{ident}" for seg, ident in queries)
        url = f"{self.base_url()}/script-details/1.0/quotes/neosymbol/{urlquote(path, safe='|,')}/{quote_type}"
        r = await self._http().get(url, headers={"Authorization": self.token(), "Content-Type": "application/json"})
        if r.status_code != 200:
            raise RuntimeError(f"backup quotes HTTP {r.status_code}: {r.text[:160]}")
        data = r.json()
        if isinstance(data, dict):
            if data.get("stat") == "Not_Ok" or data.get("error"):
                raise RuntimeError(f"backup quotes: {data.get('emsg') or data.get('message') or data}")
            data = data.get("data") or []
        return [d for d in data if isinstance(d, dict)]

    async def get_quote(self, instruments: list[str]) -> dict[str, Quote]:
        if not self.configured:
            raise RuntimeError("backup quotes are not configured")
        await self._ensure_master()
        wanted: dict[tuple[str, str], str] = {}
        for key in instruments:
            q = self.query_for(key)
            if q:
                wanted[q] = key
        out: dict[str, Quote] = {}
        items = list(wanted)
        try:
            for i in range(0, len(items), BATCH):
                for d in await self.fetch_raw(items[i:i + BATCH]):
                    seg, ident = str(d.get("exchange") or ""), str(d.get("exchange_token") or "")
                    key = wanted.get((seg, ident)) or next(
                        (k for (s, x), k in wanted.items() if s == seg and x.lower() == ident.lower()), None)
                    if key:
                        out[key] = self.to_quote(d)
        except (httpx.HTTPError, ValueError, RuntimeError) as e:
            self.stats["last_error"] = {"at": datetime.now().isoformat(timespec="seconds"), "error": str(e)[:200]}
            raise
        self.stats["served"] += len(out)
        self.stats["last_ok"] = datetime.now().isoformat(timespec="seconds")
        return out

    async def get_ohlc(self, instruments: list[str]) -> dict[str, OHLCQuote]:
        return {k: OHLCQuote(instrument_token=q.instrument_token, last_price=q.last_price, ohlc=q.ohlc)
                for k, q in (await self.get_quote(instruments)).items()}

    async def get_ltp(self, instruments: list[str]) -> dict[str, LTPQuote]:
        return {k: LTPQuote(instrument_token=q.instrument_token, last_price=q.last_price)
                for k, q in (await self.get_quote(instruments)).items()}


# Which source the app asks first (admin panel: Backup market data).
SOURCE_MODES = {
    "kite_first": "Kite first - the backup when Kite fails or is not linked today (recommended)",
    "kotak_first": "Kotak Neo first - Kite when the backup fails",
    "kite_only": "Kite only - never the backup",
}


def source_mode() -> str:
    from src.utils.app_settings import get_setting

    mode = get_setting("quote_source_mode", "QUOTE_SOURCE_MODE", "kite_first").strip()
    return mode if mode in SOURCE_MODES else "kite_first"


_BACKUP: Optional[BackupQuotes] = None


def backup_quotes() -> BackupQuotes:
    global _BACKUP
    if _BACKUP is None:
        _BACKUP = BackupQuotes()
    return _BACKUP


def is_outage(error: Exception) -> bool:
    """Network / timeout / server / rate-limit / session failures fail over; a
    bad request (unknown instrument, invalid input) does not."""
    from src.utils.exceptions import AuthenticationError, DataError, NetworkError, OrderError, RateLimitError

    if isinstance(error, (DataError, OrderError, ValueError)):
        return False
    if isinstance(error, (NetworkError, RateLimitError, AuthenticationError, httpx.HTTPError, asyncio.TimeoutError,
                          ConnectionError, OSError)):
        return True
    code = getattr(error, "status_code", None) or getattr(error, "code", None)
    try:
        return int(code) >= 500
    except (TypeError, ValueError):
        return True
