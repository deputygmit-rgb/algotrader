from datetime import datetime

from src.data.bar_aggregator import BarAggregator, bucket_start

D = "2026-10-16"


def t(hhmmss):
    return datetime.fromisoformat(f"{D}T{hhmmss}")


def test_bucket_alignment():
    assert bucket_start(t("09:17:59"), 5) == t("09:15:00")
    assert bucket_start(t("09:20:00"), 5) == t("09:20:00")
    assert bucket_start(t("09:02:00"), 5) == t("09:15:00")


def test_bar_ohlc_and_volume_from_cumulative_ticks():
    agg = BarAggregator(5)
    assert agg.update(1, 100.0, 10_000, t("09:15:05")) == []
    agg.update(1, 101.5, 10_400, t("09:16:00"))
    agg.update(1, 99.8, 10_900, t("09:19:30"))
    agg.update(1, 100.6, 11_000, t("09:19:59"))
    done = agg.update(1, 100.7, 11_300, t("09:20:01"))
    assert len(done) == 1
    bar = done[0]
    assert (bar.start, bar.open, bar.high, bar.low, bar.close) == (t("09:15:00"), 100.0, 101.5, 99.8, 100.6)
    assert bar.volume == 1_000              # 11_000 - 10_000 (first tick is the baseline)
    # the next bar's volume starts from where the previous one ended
    nxt = agg.update(1, 100.9, 11_500, t("09:24:00")) or agg.forming(1)
    assert agg.forming(1).volume == 500


def test_flush_closes_quiet_instruments_and_ignores_late_ticks():
    agg = BarAggregator(5)
    agg.update(1, 50.0, 100, t("09:21:00"))
    assert agg.flush(t("09:24:59")) == []
    closed = agg.flush(t("09:25:00"))
    assert [(tok, b.start) for tok, b in closed] == [(1, t("09:20:00"))]
    assert agg.update(1, 49.0, 120, t("09:24:58")) == []     # late tick for the closed bar
    assert agg.forming(1) is None
    agg.update(1, 51.0, 150, t("09:26:00"))
    assert agg.forming(1).volume == 30


def test_prime_keeps_volume_traded_before_the_first_tick():
    agg = BarAggregator(5)
    agg.prime(1, {"timestamp": f"{D}T09:30:00+05:30", "open": 200, "high": 203, "low": 199, "close": 202, "volume": 7_000})
    agg.update(1, 204.0, 500_000, t("09:32:00"))
    agg.update(1, 201.0, 500_600, t("09:33:00"))
    bar = agg.update(1, 205.0, 501_000, t("09:35:02"))[0]
    assert (bar.open, bar.high, bar.low, bar.close) == (200, 204.0, 199, 201.0)
    assert bar.volume == 7_600


def test_live_bar_close_feeds_strategies_completed_bars_only():
    import asyncio

    import pandas as pd

    from src.api.service import TradingService
    from src.data.bar_aggregator import Bar

    seen, processed = [], []

    class FakeStrategy:
        name, is_active, params = "fake", True, {}

        def __init__(self):
            self.df = None

        def update_bar_data(self, token, df):
            self.df = df

        async def on_bar(self, token, bar):
            seen.append((token, bar["timestamp"], len(self.df)))
            return ["SIGNAL"]

    async def process(sig):
        processed.append(sig)

    svc = TradingService.__new__(TradingService)
    seed = pd.DataFrame([{"timestamp": f"{D}T09:15:00+05:30", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 10}])
    svc._live_bars = {("5minute", 7): seed}
    strat = FakeStrategy()
    svc._strategies = [strat]
    svc._process_signal = process

    asyncio.run(svc._on_bar_close("5minute", 7, Bar(t("09:20:00"), 1, 2, 0.5, 1.5, 99)))
    assert seen == [(7, f"{D}T09:20:00+05:30", 2)]
    assert processed == ["SIGNAL"]
    assert strat.df["volume"].tolist() == [10, 99]
    # a re-delivered bar for the same time replaces, never duplicates
    asyncio.run(svc._on_bar_close("5minute", 7, Bar(t("09:20:00"), 1, 2, 0.5, 1.6, 120)))
    assert svc._live_bars[("5minute", 7)]["close"].tolist() == [1, 1.6]
