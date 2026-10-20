"""Per-trade exit plan shared by the backtest and paper engines.

A strategy can attach an execution plan to its signal:

    signal.metadata["exec"] = {
        "fill": "next_open",          # or "close" (default): when the entry fills
        "trail": False,               # trail the stop by its initial distance (default True)
        "scale_out_at": 2.25,         # distance from entry where part is booked
        "scale_out_fraction": 0.5,    # share of the position booked there
        "breakeven_after_scale": True,
        "max_hold_bars": 12,          # exit at the close after this many bars
        "exit_by": "15:15",           # exit at the close of the bar that ends at/after this time
        "bar_minutes": 5,             # bar size, so exit_by is checked against bar close

        # Optional risk model: levels are rebuilt from the ACTUAL fill exactly
        # as the Day Trade panel does (see ``plan_levels``), instead of from
        # the stop / target distances measured at the signal bar's close.
        "natural_stop": 101.2, "atr": 4.0,
        "min_risk_atr": 0.15, "max_risk_atr": 0.6, "t1_r": 1.5, "t2_r": 3.0,
    }

``signal.stop_loss`` and ``signal.target`` stay distances from the fill, as
everywhere else in the codebase.

Every strategy is executed under the same rules in backtest, paper and live
trading (``execution_spec``); a strategy's own ``exec`` entries override them:

* entries fill at the next bar's open (a signal is only known at bar close);
* the stop trails by its initial distance, the target is ``signal.target``
  or 2R;
* the timeframe sets the product: intraday bars trade MIS - shorts allowed,
  flat by 15:15, no new entry on a bar that closes at or after 15:15
  (``entry_allowed``) - and daily bars trade CNC (delivery), which is
  long-only, so a SELL signal there only closes a long.

Within a bar the order is: stop (assumed first if the bar also reaches a
target), scale-out, final target, time exit; the trailing stop then moves
using the bar's best price, so it only acts from the next bar.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

EXEC_KEY = "exec"
INTRADAY_SQUAREOFF = "15:15"           # MIS positions are closed by this time (live engine does too)
DEFAULT_SLIPPAGE_PCT = 0.05            # % per market fill when a strategy does not set its own
TIMEFRAME_MINUTES: dict[str, int] = {
    "minute": 1, "3minute": 3, "5minute": 5, "10minute": 10,
    "15minute": 15, "30minute": 30, "60minute": 60, "day": 375,
}


def is_intraday(timeframe: str | None) -> bool:
    """Minute-bar timeframes trade intraday (MIS); daily bars trade delivery (CNC)."""
    return bool(timeframe) and timeframe != "day" and timeframe in TIMEFRAME_MINUTES


def product_for(timeframe: str | None) -> str:
    return "MIS" if is_intraday(timeframe) else "CNC"


def allows_short(timeframe: str | None) -> bool:
    """Delivery (CNC) positions cannot be shorted in Indian cash equities."""
    return is_intraday(timeframe)


def exec_spec(signal: Any) -> dict[str, Any]:
    meta = getattr(signal, "metadata", None) or {}
    spec = meta.get(EXEC_KEY)
    return spec if isinstance(spec, dict) else {}


def execution_spec(signal: Any, timeframe: str | None) -> dict[str, Any]:
    """The execution rules for ``signal`` on ``timeframe``: the shared defaults,
    overridden by the strategy's own ``metadata["exec"]``."""
    spec: dict[str, Any] = {"fill": "next_open", "trail": True}
    if is_intraday(timeframe):
        spec.update(exit_by=INTRADAY_SQUAREOFF, bar_minutes=TIMEFRAME_MINUTES[timeframe])
    spec.update(exec_spec(signal))
    return spec


def entry_allowed(timeframe: str | None, bar_start_hhmm: str) -> bool:
    """Whether a new position may open on the bar starting at ``bar_start_hhmm``.

    Intraday (MIS) entries need a bar that closes before the 15:15 square-off:
    a position opened later would be closed by the square-off on the same bar
    - all costs, no trade - and the broker refuses new MIS orders near the
    close anyway. Real sessions do have 15:15-15:25 bars, so signals there
    must not open a trade. Daily (delivery) entries are always allowed.
    """
    if not is_intraday(timeframe) or not bar_start_hhmm:
        return True
    hh, mm = int(bar_start_hhmm[:2]), int(bar_start_hhmm[3:5]) + TIMEFRAME_MINUTES[timeframe]
    return f"{hh + mm // 60:02d}:{mm % 60:02d}" < INTRADAY_SQUAREOFF


def fills_next_open(signal: Any, timeframe: str | None = None) -> bool:
    spec = execution_spec(signal, timeframe) if timeframe else exec_spec(signal)
    return spec.get("fill") == "next_open"


def plan_levels(side: int, fill: float, natural_stop: float, atr: float, *,
                min_risk_atr: float = 0.15, max_risk_atr: float = 0.6,
                t1_r: float = 1.5, t2_r: float = 3.0) -> dict[str, float]:
    """Stop / T1 / T2 from a fill price and the setup's natural stop.

    Risk is the distance to the natural stop, bounded to [min, max] x ATR.
    The single source for these levels: the Day Trade panel, backtest and
    paper plans and the live execution engine all call this.
    """
    risk = min(max(side * (fill - natural_stop), min_risk_atr * atr), max_risk_atr * atr)
    return {
        "risk": risk,
        "stop": fill - side * risk,
        "target1": fill + side * t1_r * risk,
        "target2": fill + side * t2_r * risk,
    }


def levels_from_spec(side: int, fill: float, spec: dict[str, Any]) -> dict[str, float] | None:
    """``plan_levels`` for a signal's exec spec, or None if it carries no risk model."""
    if spec.get("natural_stop") is None or not spec.get("atr"):
        return None
    levels = plan_levels(
        side, fill, float(spec["natural_stop"]), float(spec["atr"]),
        min_risk_atr=float(spec.get("min_risk_atr", 0.15)), max_risk_atr=float(spec.get("max_risk_atr", 0.6)),
        t1_r=float(spec.get("t1_r", 1.5)), t2_r=float(spec.get("t2_r", 3.0)),
    )
    # prices rounded to the paise exactly as the panel shows them, so a level
    # touched by a bar counts the same in every engine
    return {k: (v if k == "risk" else round(v, 2)) for k, v in levels.items()}


def _add_minutes(hhmm: str, minutes: int) -> str:
    if not hhmm or not minutes:
        return hhmm
    total = int(hhmm[:2]) * 60 + int(hhmm[3:5]) + minutes
    return f"{total // 60:02d}:{total % 60:02d}"


@dataclass
class Exit:
    reason: str          # STOP, BREAKEVEN, SCALE_OUT, TARGET, TIME_EXIT
    price: float
    fraction: float      # share of the ORIGINAL quantity closed by this exit
    final: bool


@dataclass
class ExitPlan:
    side: int                         # +1 long, -1 short
    entry: float
    entry_index: int
    stop: Optional[float] = None
    target: Optional[float] = None
    scale_at: Optional[float] = None
    scale_fraction: float = 0.5
    breakeven_after_scale: bool = True
    trail_distance: Optional[float] = None
    max_hold_bars: Optional[int] = None
    exit_by: Optional[str] = None
    bar_minutes: int = 0               # >0: exit_by is compared with the bar's close time
    remaining: float = 1.0
    scaled: bool = False
    best: float = field(default=0.0)

    @classmethod
    def from_signal(
        cls, signal: Any, side: int, fill: float, entry_index: int, timeframe: str | None = None,
    ) -> "ExitPlan":
        spec = execution_spec(signal, timeframe) if timeframe else exec_spec(signal)
        dist = float(signal.stop_loss) if getattr(signal, "stop_loss", None) else None
        target_dist = float(signal.target) if getattr(signal, "target", None) else (2 * dist if dist else None)
        scale_dist = spec.get("scale_out_at")
        stop = fill - side * dist if dist else None
        target = fill + side * target_dist if target_dist else None
        scale_at = fill + side * float(scale_dist) if scale_dist else None
        levels = levels_from_spec(side, fill, spec)
        if levels:
            stop, target = levels["stop"], levels["target2"]
            scale_at = levels["target1"] if scale_dist else None
            dist = levels["risk"]
        return cls(
            side=side,
            entry=fill,
            entry_index=entry_index,
            stop=stop,
            target=target,
            scale_at=scale_at,
            scale_fraction=float(spec.get("scale_out_fraction", 0.5)),
            breakeven_after_scale=bool(spec.get("breakeven_after_scale", True)),
            trail_distance=dist if dist and spec.get("trail", True) else None,
            max_hold_bars=spec.get("max_hold_bars"),
            exit_by=spec.get("exit_by"),
            bar_minutes=int(spec.get("bar_minutes") or 0),
            best=fill,
        )

    def on_bar(self, bar: Any, index: int, hhmm: str = "") -> list[Exit]:
        """Exits this bar triggers (prices are the plan levels, before slippage)."""
        side = self.side
        best = float(bar["high"] if side > 0 else bar["low"])
        worst = float(bar["low"] if side > 0 else bar["high"])
        exits: list[Exit] = []

        if self.stop is not None and side * (worst - self.stop) <= 0:
            reason = "BREAKEVEN" if self.scaled and self.stop == self.entry else "STOP"
            return [self._close(reason, self.stop)]
        if self.scale_at is not None and not self.scaled and side * (best - self.scale_at) >= 0:
            part = min(self.scale_fraction, self.remaining)
            self.remaining -= part
            self.scaled = True
            exits.append(Exit("SCALE_OUT", self.scale_at, part, final=self.remaining <= 1e-9))
            if self.breakeven_after_scale:
                self.stop = self.entry
                self.trail_distance = None
            if self.remaining <= 1e-9:
                return exits
        if self.target is not None and side * (best - self.target) >= 0:
            exits.append(self._close("TARGET", self.target))
            return exits
        held = index - self.entry_index
        closes_at = _add_minutes(hhmm, self.bar_minutes)
        if (self.max_hold_bars is not None and held >= self.max_hold_bars) or (
            self.exit_by and closes_at and closes_at >= self.exit_by
        ):
            exits.append(self._close("TIME_EXIT", float(bar["close"])))
            return exits

        if self.trail_distance and self.stop is not None:
            if side * (best - self.best) > 0:
                self.best = best
            trailed = self.best - side * self.trail_distance
            if side * (trailed - self.stop) > 0:
                self.stop = trailed
        return exits

    def _close(self, reason: str, price: float) -> Exit:
        part, self.remaining = self.remaining, 0.0
        return Exit(reason, price, part, final=True)


def bar_hhmm(bar: Any) -> str:
    ts = str(bar.get("timestamp", "") if hasattr(bar, "get") else "")
    return ts[11:16] if len(ts) >= 16 else ""
