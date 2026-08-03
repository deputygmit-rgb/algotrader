"""
OI Signal Engine
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Advanced open-interest signal generation on top of the base OI analyzers.

Provides five independent signal generators:

  1. OIWallScorer        — quantify CE/PE wall strength as a 0-100 score;
                           classify as STRONG / MODERATE / WEAK
  2. OIVelocityDetector  — detect sudden large OI additions ("smart money moves")
                           using Z-score of OI change magnitude
  3. OIDivergenceDetector— price vs OI divergence signals:
                           price ↑ OI ↓ = distribution / unwinding
                           price ↓ OI ↑ = accumulation / short buildup
  4. MaxPainTracker      — track max-pain drift over multiple snapshots;
                           generate price-to-pain convergence signals
  5. OIHeatmapBuilder    — sector-level OI heatmap: bullish/bearish ratio
                           per sector with intensity scores

All generators work on data dicts (no live API required) so they can be
used in backtesting, paper-trading, or live sessions unchanged.

Usage:
    from src.options.oi_signals import OISignalEngine

    engine = OISignalEngine()
    signals = engine.run(options_report, futures_report, spot_price)
    for sig in signals:
        print(sig)
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


# ─────────────────────────────────────────────────────────────
# Signal containers
# ─────────────────────────────────────────────────────────────

@dataclass
class OISignal:
    """A single actionable OI-derived signal."""
    timestamp:    str
    source:       str        # "WALL" | "VELOCITY" | "DIVERGENCE" | "PAIN" | "HEATMAP"
    symbol:       str
    signal_type:  str        # e.g. "STRONG_SUPPORT_WALL", "SMART_MONEY_CALL_WRITING"
    sentiment:    str        # "BULLISH" | "BEARISH" | "NEUTRAL"
    strength:     int        # 0-100
    description:  str
    strike:       Optional[float] = None
    sector:       Optional[str]   = None
    metadata:     dict[str, Any]  = field(default_factory=dict)

    def __str__(self) -> str:
        loc = f" @ {self.strike:.0f}" if self.strike else ""
        sec = f" [{self.sector}]" if self.sector else ""
        return (
            f"[{self.source:10s}] {self.sentiment:8s} | {self.signal_type}{loc}{sec} "
            f"| strength={self.strength:3d} | {self.description}"
        )


@dataclass
class WallAnalysis:
    strike:        float
    option_type:   str          # "CE" | "PE"
    oi:            int
    oi_change:     int
    wall_score:    int          # 0-100
    wall_class:    str          # "STRONG" | "MODERATE" | "WEAK"
    distance_pct:  float        # % from spot
    sentiment:     str          # "RESISTANCE" (CE) | "SUPPORT" (PE)


@dataclass
class VelocityEvent:
    """Sudden large OI addition detected via Z-score."""
    strike:       float
    option_type:  str
    oi_change:    int
    z_score:      float
    sentiment:    str           # "SMART_MONEY_CALL_WRITE" | "SMART_MONEY_PUT_WRITE" etc.
    description:  str


@dataclass
class DivergenceSignal:
    symbol:        str
    price_change:  float        # % change
    oi_change:     float        # % change
    divergence_type: str        # "LONG_UNWINDING" | "SHORT_BUILDUP" | "SHORT_COVERING" | "LONG_BUILDUP"
    strength:      int          # 0-100
    implication:   str


@dataclass
class MaxPainSignal:
    underlying:   str
    spot:         float
    max_pain:     float
    distance_pct: float         # (max_pain - spot) / spot * 100
    drift:        float         # recent drift of max pain
    signal:       str           # "GRAVITATING_UP" | "GRAVITATING_DOWN" | "ABOVE_PAIN" | "BELOW_PAIN" | "AT_PAIN"
    description:  str


@dataclass
class SectorHeatmapEntry:
    sector:         str
    bull_oi_score:  int         # 0-100 (long_buildup + short_covering)
    bear_oi_score:  int         # 0-100 (short_buildup + long_unwinding)
    net_score:      int         # bull - bear (-100 to +100)
    dominant:       str         # "BULL" | "BEAR" | "NEUTRAL"
    intensity:      str         # "HIGH" | "MODERATE" | "LOW"
    top_stocks:     list[str]   # top contributors


# ─────────────────────────────────────────────────────────────
# 1. OI Wall Scorer
# ─────────────────────────────────────────────────────────────

class OIWallScorer:
    """
    Score CE/PE OI walls as STRONG / MODERATE / WEAK using:
      • Absolute OI magnitude vs total OI at that expiry
      • OI change (fresh writing = stronger wall)
      • Distance from spot (closer = more relevant)
      • Concentration ratio (how dominant is this single strike?)
    """

    def score_walls(
        self,
        strikes: list[dict[str, Any]],
        spot: float,
    ) -> list[WallAnalysis]:
        """
        strikes: list of dicts with keys:
            strike, ce_oi, ce_oi_change, pe_oi, pe_oi_change
        """
        if not strikes:
            return []

        total_ce = sum(s.get("ce_oi", 0) for s in strikes) or 1
        total_pe = sum(s.get("pe_oi", 0) for s in strikes) or 1
        max_ce   = max((s.get("ce_oi", 0) for s in strikes), default=1)
        max_pe   = max((s.get("pe_oi", 0) for s in strikes), default=1)

        walls: list[WallAnalysis] = []

        for s in strikes:
            strike = float(s.get("strike", 0))
            dist   = abs(strike - spot) / spot * 100 if spot > 0 else 100
            if dist > 5:
                continue  # only care about walls within 5% of spot

            # --- CE wall (resistance) ---
            ce_oi  = int(s.get("ce_oi", 0))
            ce_chg = int(s.get("ce_oi_change", 0))
            if ce_oi > 0:
                conc_score = ce_oi / max_ce * 40          # 0-40: how dominant
                share_score = ce_oi / total_ce * 40        # 0-40: share of total OI
                fresh_bonus = min(20, ce_chg / (ce_oi + 1) * 200) if ce_chg > 0 else 0
                prox_bonus  = max(0, 20 - int(dist * 4))   # closer = higher
                raw = conc_score + share_score + fresh_bonus + prox_bonus
                wall_score = max(0, min(100, int(raw)))
                wall_class = "STRONG" if wall_score >= 65 else "MODERATE" if wall_score >= 40 else "WEAK"
                walls.append(WallAnalysis(
                    strike=strike, option_type="CE", oi=ce_oi, oi_change=ce_chg,
                    wall_score=wall_score, wall_class=wall_class,
                    distance_pct=round(dist, 2),
                    sentiment="RESISTANCE",
                ))

            # --- PE wall (support) ---
            pe_oi  = int(s.get("pe_oi", 0))
            pe_chg = int(s.get("pe_oi_change", 0))
            if pe_oi > 0:
                conc_score = pe_oi / max_pe * 40
                share_score = pe_oi / total_pe * 40
                fresh_bonus = min(20, pe_chg / (pe_oi + 1) * 200) if pe_chg > 0 else 0
                prox_bonus  = max(0, 20 - int(dist * 4))
                raw = conc_score + share_score + fresh_bonus + prox_bonus
                wall_score = max(0, min(100, int(raw)))
                wall_class = "STRONG" if wall_score >= 65 else "MODERATE" if wall_score >= 40 else "WEAK"
                walls.append(WallAnalysis(
                    strike=strike, option_type="PE", oi=pe_oi, oi_change=pe_chg,
                    wall_score=wall_score, wall_class=wall_class,
                    distance_pct=round(dist, 2),
                    sentiment="SUPPORT",
                ))

        walls.sort(key=lambda w: w.wall_score, reverse=True)
        return walls

    def to_signals(
        self,
        walls: list[WallAnalysis],
        symbol: str,
    ) -> list[OISignal]:
        signals: list[OISignal] = []
        ts = datetime.now().isoformat()

        for w in walls:
            if w.wall_class == "WEAK":
                continue
            sig_type = "STRONG_RESISTANCE_WALL" if w.option_type == "CE" else "STRONG_SUPPORT_WALL"
            if w.wall_class == "MODERATE":
                sig_type = sig_type.replace("STRONG", "MODERATE")
            sentiment = "BEARISH" if w.option_type == "CE" else "BULLISH"
            desc = (
                f"{w.wall_class} {w.sentiment} wall at {w.strike:.0f} "
                f"(OI={w.oi:,}, fresh_add={w.oi_change:+,}, dist={w.distance_pct:.1f}%)"
            )
            signals.append(OISignal(
                timestamp=ts, source="WALL", symbol=symbol,
                signal_type=sig_type, sentiment=sentiment,
                strength=w.wall_score, description=desc, strike=w.strike,
                metadata={"oi": w.oi, "oi_change": w.oi_change, "wall_class": w.wall_class},
            ))
        return signals


# ─────────────────────────────────────────────────────────────
# 2. OI Velocity Detector
# ─────────────────────────────────────────────────────────────

class OIVelocityDetector:
    """
    Detect sudden large OI additions ("smart money moves") using Z-score.
    An OI change with Z > 2.0 on a single strike is unusual and actionable.

    History window: keeps last 20 snapshots per symbol to compute rolling mean/std.
    """

    def __init__(self, z_threshold: float = 2.0, history_size: int = 20) -> None:
        self._threshold    = z_threshold
        self._history_size = history_size
        self._ce_history:  dict[str, list[int]] = {}   # symbol:strike → list of oi_changes
        self._pe_history:  dict[str, list[int]] = {}

    def update_and_detect(
        self,
        strikes: list[dict[str, Any]],
        symbol: str,
    ) -> list[VelocityEvent]:
        """
        Feed in fresh strike data and return any velocity anomalies.
        strikes: same format as OIWallScorer.score_walls()
        """
        events: list[VelocityEvent] = []

        all_ce_chg = [abs(int(s.get("ce_oi_change", 0))) for s in strikes]
        all_pe_chg = [abs(int(s.get("pe_oi_change", 0))) for s in strikes]

        ce_mean, ce_std = _mean_std(all_ce_chg)
        pe_mean, pe_std = _mean_std(all_pe_chg)

        for s in strikes:
            strike = float(s.get("strike", 0))

            # CE velocity
            ce_chg = int(s.get("ce_oi_change", 0))
            if ce_chg != 0 and ce_std > 0:
                z = (abs(ce_chg) - ce_mean) / ce_std
                if z >= self._threshold:
                    if ce_chg > 0:
                        sent = "SMART_MONEY_CALL_WRITE"
                        desc = f"Unusual CE OI addition +{ce_chg:,} at {strike:.0f} (Z={z:.1f})"
                        implication = "BEARISH — strong call writing = resistance confirmed"
                    else:
                        sent = "SMART_MONEY_CALL_UNWIND"
                        desc = f"Unusual CE OI unwinding {ce_chg:,} at {strike:.0f} (Z={z:.1f})"
                        implication = "BULLISH — call writers closing = resistance weakening"
                    events.append(VelocityEvent(
                        strike=strike, option_type="CE",
                        oi_change=ce_chg, z_score=round(z, 2),
                        sentiment=sent, description=desc,
                    ))

            # PE velocity
            pe_chg = int(s.get("pe_oi_change", 0))
            if pe_chg != 0 and pe_std > 0:
                z = (abs(pe_chg) - pe_mean) / pe_std
                if z >= self._threshold:
                    if pe_chg > 0:
                        sent = "SMART_MONEY_PUT_WRITE"
                        desc = f"Unusual PE OI addition +{pe_chg:,} at {strike:.0f} (Z={z:.1f})"
                        implication = "BULLISH — put writing = support confirmed"
                    else:
                        sent = "SMART_MONEY_PUT_UNWIND"
                        desc = f"Unusual PE OI unwinding {pe_chg:,} at {strike:.0f} (Z={z:.1f})"
                        implication = "BEARISH — put writers closing = support eroding"
                    events.append(VelocityEvent(
                        strike=strike, option_type="PE",
                        oi_change=pe_chg, z_score=round(z, 2),
                        sentiment=sent, description=desc,
                    ))

        events.sort(key=lambda e: abs(e.z_score), reverse=True)
        return events

    def to_signals(
        self, events: list[VelocityEvent], symbol: str
    ) -> list[OISignal]:
        ts = datetime.now().isoformat()
        signals: list[OISignal] = []
        for e in events:
            bullish_types = {"SMART_MONEY_PUT_WRITE", "SMART_MONEY_CALL_UNWIND"}
            sentiment = "BULLISH" if e.sentiment in bullish_types else "BEARISH"
            strength = min(100, int(e.z_score * 25))
            signals.append(OISignal(
                timestamp=ts, source="VELOCITY", symbol=symbol,
                signal_type=e.sentiment, sentiment=sentiment,
                strength=strength, description=e.description, strike=e.strike,
                metadata={"z_score": e.z_score, "oi_change": e.oi_change},
            ))
        return signals


# ─────────────────────────────────────────────────────────────
# 3. OI Divergence Detector
# ─────────────────────────────────────────────────────────────

class OIDivergenceDetector:
    """
    Detect price vs OI divergence in futures data.

    Uses the standard 4-quadrant interpretation:
      Price ↑ + OI ↑ → Long Buildup   (BULLISH)
      Price ↓ + OI ↑ → Short Buildup  (BEARISH)
      Price ↑ + OI ↓ → Short Covering (BULLISH — but weaker, driven by covering)
      Price ↓ + OI ↓ → Long Unwinding (BEARISH — trend ending)

    Divergence signal: when price and OI move in opposite directions
    and the magnitude is significant (both > threshold).
    """

    def __init__(
        self,
        price_threshold_pct: float = 0.3,
        oi_threshold_pct: float    = 1.0,
    ) -> None:
        self._price_thr = price_threshold_pct
        self._oi_thr    = oi_threshold_pct

    def detect(
        self,
        futures_entries: list[dict[str, Any]],
    ) -> list[DivergenceSignal]:
        """
        futures_entries: list of dicts with keys:
            symbol, change_pct, oi_change_pct, buildup
        (matches FuturesOIEntry fields)
        """
        signals: list[DivergenceSignal] = []

        for entry in futures_entries:
            sym        = entry.get("symbol", "")
            price_chg  = float(entry.get("change_pct", 0))
            oi_chg_pct = float(entry.get("oi_change_pct", 0))
            buildup    = entry.get("buildup", "")

            # Skip neutral / small moves
            if abs(price_chg) < self._price_thr and abs(oi_chg_pct) < self._oi_thr:
                continue

            div_type, sentiment, implication, strength = self._classify(
                price_chg, oi_chg_pct, buildup
            )
            if div_type:
                signals.append(DivergenceSignal(
                    symbol=sym, price_change=round(price_chg, 2),
                    oi_change=round(oi_chg_pct, 2),
                    divergence_type=div_type, strength=strength,
                    implication=implication,
                ))

        signals.sort(key=lambda s: s.strength, reverse=True)
        return signals

    def _classify(
        self,
        price_chg: float, oi_chg: float, buildup: str
    ) -> tuple[str, str, str, int]:
        """Returns (type, sentiment, implication, strength) or ("","","",0) if skip."""
        abs_p = abs(price_chg)
        abs_o = abs(oi_chg)
        strength = min(100, int((abs_p * 15 + abs_o * 5)))

        if buildup == "LONG_BUILDUP":
            return ("LONG_BUILDUP", "BULLISH",
                    f"Price +{price_chg:.1f}% with OI +{oi_chg:.1f}% — fresh longs entering",
                    strength)
        elif buildup == "SHORT_BUILDUP":
            return ("SHORT_BUILDUP", "BEARISH",
                    f"Price {price_chg:.1f}% with OI +{oi_chg:.1f}% — fresh shorts adding",
                    strength)
        elif buildup == "SHORT_COVERING":
            return ("SHORT_COVERING", "BULLISH",
                    f"Price +{price_chg:.1f}% with OI {oi_chg:.1f}% — shorts being squeezed out",
                    max(10, strength - 15))
        elif buildup == "LONG_UNWINDING":
            return ("LONG_UNWINDING", "BEARISH",
                    f"Price {price_chg:.1f}% with OI {oi_chg:.1f}% — longs exiting",
                    max(10, strength - 15))
        return ("", "", "", 0)

    def to_signals(
        self, divergences: list[DivergenceSignal]
    ) -> list[OISignal]:
        ts = datetime.now().isoformat()
        bullish = {"LONG_BUILDUP", "SHORT_COVERING"}
        return [
            OISignal(
                timestamp=ts, source="DIVERGENCE", symbol=d.symbol,
                signal_type=d.divergence_type,
                sentiment="BULLISH" if d.divergence_type in bullish else "BEARISH",
                strength=d.strength, description=d.implication,
                metadata={"price_chg": d.price_change, "oi_chg": d.oi_change},
            )
            for d in divergences
        ]


# ─────────────────────────────────────────────────────────────
# 4. Max Pain Tracker
# ─────────────────────────────────────────────────────────────

class MaxPainTracker:
    """
    Track max pain across multiple snapshots and generate
    price-to-pain convergence signals.

    Theory: As expiry approaches, market tends to gravitate toward
    max pain (the price at which option writers lose the least).
    Tracking drift of max pain helps predict near-term direction.
    """

    def __init__(self, history_size: int = 20) -> None:
        self._history: dict[str, list[dict[str, Any]]] = {}
        self._size = history_size

    def update(
        self,
        underlying: str,
        spot: float,
        max_pain: float,
        days_to_expiry: int = 0,
    ) -> MaxPainSignal:
        key = underlying.upper()
        if key not in self._history:
            self._history[key] = []

        self._history[key].append({
            "ts": datetime.now().isoformat(),
            "spot": spot, "max_pain": max_pain, "dte": days_to_expiry,
        })
        if len(self._history[key]) > self._size:
            self._history[key] = self._history[key][-self._size:]

        return self._generate_signal(key, spot, max_pain, days_to_expiry)

    def _generate_signal(
        self, key: str, spot: float, max_pain: float, dte: int
    ) -> MaxPainSignal:
        hist = self._history.get(key, [])
        dist_pct = (max_pain - spot) / spot * 100 if spot > 0 else 0

        # Drift: direction max pain has been moving recently
        drift = 0.0
        if len(hist) >= 3:
            pains = [h["max_pain"] for h in hist[-3:]]
            drift = (pains[-1] - pains[0]) / pains[0] * 100 if pains[0] > 0 else 0

        # Signal classification
        if abs(dist_pct) < 0.3:
            sig = "AT_PAIN"
            desc = f"Spot {spot:.0f} ≈ max pain {max_pain:.0f} — expiry pin likely"
        elif dist_pct > 1.5 and dte <= 5:
            sig = "GRAVITATING_UP"
            desc = f"Max pain {max_pain:.0f} is {dist_pct:.1f}% above spot — upward pull likely (DTE={dte})"
        elif dist_pct < -1.5 and dte <= 5:
            sig = "GRAVITATING_DOWN"
            desc = f"Max pain {max_pain:.0f} is {abs(dist_pct):.1f}% below spot — downward pull likely (DTE={dte})"
        elif dist_pct > 0:
            sig = "ABOVE_PAIN"
            desc = f"Max pain {max_pain:.0f} above spot {spot:.0f} by {dist_pct:.1f}%"
        else:
            sig = "BELOW_PAIN"
            desc = f"Max pain {max_pain:.0f} below spot {spot:.0f} by {abs(dist_pct):.1f}%"

        return MaxPainSignal(
            underlying=key, spot=spot, max_pain=max_pain,
            distance_pct=round(dist_pct, 2), drift=round(drift, 2),
            signal=sig, description=desc,
        )

    def to_signal(
        self, pain_sig: MaxPainSignal
    ) -> OISignal:
        ts = datetime.now().isoformat()
        bullish_sigs = {"GRAVITATING_UP", "ABOVE_PAIN"}
        bearish_sigs = {"GRAVITATING_DOWN", "BELOW_PAIN"}
        if pain_sig.signal in bullish_sigs:
            sentiment = "BULLISH"
        elif pain_sig.signal in bearish_sigs:
            sentiment = "BEARISH"
        else:
            sentiment = "NEUTRAL"

        strength = min(100, int(abs(pain_sig.distance_pct) * 20 + abs(pain_sig.drift) * 10))
        return OISignal(
            timestamp=ts, source="PAIN", symbol=pain_sig.underlying,
            signal_type=pain_sig.signal, sentiment=sentiment,
            strength=strength, description=pain_sig.description,
            metadata={"max_pain": pain_sig.max_pain, "spot": pain_sig.spot,
                      "dist_pct": pain_sig.distance_pct, "drift": pain_sig.drift},
        )


# ─────────────────────────────────────────────────────────────
# 5. OI Heatmap Builder
# ─────────────────────────────────────────────────────────────

class OIHeatmapBuilder:
    """
    Build sector-level OI heatmap from futures OI data.

    Aggregates buildup classifications by sector and produces
    intensity-scored bull/bear assessments per sector.
    """

    def build(
        self, futures_entries: list[dict[str, Any]]
    ) -> list[SectorHeatmapEntry]:
        """
        futures_entries: list of dicts with keys:
            symbol, buildup, sector, oi_change, change_pct, volume
        (matches FuturesOIEntry fields)
        """
        sectors: dict[str, dict[str, Any]] = {}

        for e in futures_entries:
            sec = e.get("sector") or "Other"
            if sec not in sectors:
                sectors[sec] = {
                    "long_buildup": 0, "short_buildup": 0,
                    "long_unwinding": 0, "short_covering": 0,
                    "total_oi_chg": 0, "stocks": [], "count": 0,
                }
            d = sectors[sec]
            d["count"] += 1
            buildup = (e.get("buildup") or "").upper()
            if buildup == "LONG_BUILDUP":   d["long_buildup"]   += 1
            elif buildup == "SHORT_BUILDUP":  d["short_buildup"]  += 1
            elif buildup == "LONG_UNWINDING": d["long_unwinding"] += 1
            elif buildup == "SHORT_COVERING": d["short_covering"] += 1
            d["total_oi_chg"] += int(e.get("oi_change", 0))
            d["stocks"].append(e.get("symbol", ""))

        result: list[SectorHeatmapEntry] = []
        for sec_name, d in sectors.items():
            total = d["count"] or 1
            bull = d["long_buildup"] + d["short_covering"]
            bear = d["short_buildup"] + d["long_unwinding"]

            # Score 0-100 each
            bull_score = min(100, int(bull / total * 100))
            bear_score = min(100, int(bear / total * 100))
            net = bull_score - bear_score

            if net >= 40:   dominant = "BULL"
            elif net <= -40: dominant = "BEAR"
            else:            dominant = "NEUTRAL"

            abs_net = abs(net)
            if abs_net >= 60:    intensity = "HIGH"
            elif abs_net >= 30:  intensity = "MODERATE"
            else:                intensity = "LOW"

            # Top contributing stocks (by absolute oi_change — use raw list here)
            top_stocks = d["stocks"][:5]

            result.append(SectorHeatmapEntry(
                sector=sec_name,
                bull_oi_score=bull_score,
                bear_oi_score=bear_score,
                net_score=net,
                dominant=dominant,
                intensity=intensity,
                top_stocks=top_stocks,
            ))

        result.sort(key=lambda x: abs(x.net_score), reverse=True)
        return result

    def to_signals(
        self, heatmap: list[SectorHeatmapEntry]
    ) -> list[OISignal]:
        ts = datetime.now().isoformat()
        signals: list[OISignal] = []
        for entry in heatmap:
            if entry.intensity == "LOW":
                continue
            sentiment = "BULLISH" if entry.dominant == "BULL" else (
                "BEARISH" if entry.dominant == "BEAR" else "NEUTRAL"
            )
            sig_type = f"SECTOR_{entry.dominant}_{entry.intensity}"
            desc = (
                f"{entry.sector}: bull_score={entry.bull_oi_score}, "
                f"bear_score={entry.bear_oi_score}, net={entry.net_score:+d}"
            )
            signals.append(OISignal(
                timestamp=ts, source="HEATMAP", symbol="INDEX",
                signal_type=sig_type, sentiment=sentiment,
                strength=abs(entry.net_score), description=desc,
                sector=entry.sector,
                metadata={"bull": entry.bull_oi_score, "bear": entry.bear_oi_score,
                          "net": entry.net_score, "top_stocks": entry.top_stocks},
            ))
        return signals


# ─────────────────────────────────────────────────────────────
# Consolidated Signal Engine
# ─────────────────────────────────────────────────────────────

class OISignalEngine:
    """
    Run all five OI signal generators in one call and return a
    consolidated, deduplicated, sorted signal list.

    Accepts data in the dict format produced by:
      • OptionsOIAnalyzer.analyze() → options_report (OptionsOIReport.model_dump())
      • FuturesOIAnalyzer.analyze()  → futures_report (FuturesOIReport.model_dump())
    """

    def __init__(
        self,
        velocity_z_threshold: float = 2.0,
        min_signal_strength:  int   = 25,
    ) -> None:
        self._wall       = OIWallScorer()
        self._velocity   = OIVelocityDetector(z_threshold=velocity_z_threshold)
        self._divergence = OIDivergenceDetector()
        self._pain       = MaxPainTracker()
        self._heatmap    = OIHeatmapBuilder()
        self._min_str    = min_signal_strength

    def run(
        self,
        options_report: dict[str, Any],
        futures_report: dict[str, Any],
        spot: float = 0.0,
        days_to_expiry: int = 0,
    ) -> list[OISignal]:
        """
        Single call to generate all OI signals.

        options_report: dict with keys 'underlying', 'strikes', 'max_pain', 'spot_price'
            (from OptionsOIReport.model_dump())
        futures_report: dict with keys 'stock_futures', 'index_futures'
            (from FuturesOIReport.model_dump())
        """
        all_signals: list[OISignal] = []
        underlying = options_report.get("underlying", "NIFTY")

        # ── 1. Wall signals ──────────────────────────────────
        strikes_raw = options_report.get("strikes", [])
        if strikes_raw:
            strikes = [
                s.model_dump() if hasattr(s, "model_dump") else s
                for s in strikes_raw
            ]
            spot_px = float(options_report.get("spot_price", spot))
            walls = self._wall.score_walls(strikes, spot_px)
            all_signals.extend(self._wall.to_signals(walls, underlying))

            # ── 2. Velocity signals ──────────────────────────
            events = self._velocity.update_and_detect(strikes, underlying)
            all_signals.extend(self._velocity.to_signals(events, underlying))

        # ── 3. Divergence signals ────────────────────────────
        stock_futures = futures_report.get("stock_futures", [])
        index_futures = futures_report.get("index_futures", [])
        all_futures = [
            f.model_dump() if hasattr(f, "model_dump") else f
            for f in (stock_futures + index_futures)
        ]
        divs = self._divergence.detect(all_futures)
        all_signals.extend(self._divergence.to_signals(divs))

        # ── 4. Max pain signal ───────────────────────────────
        max_pain_val = float(options_report.get("max_pain", 0))
        spot_px = float(options_report.get("spot_price", spot))
        if max_pain_val > 0 and spot_px > 0:
            pain_sig = self._pain.update(underlying, spot_px, max_pain_val, days_to_expiry)
            all_signals.append(self._pain.to_signal(pain_sig))

        # ── 5. Heatmap signals ───────────────────────────────
        if all_futures:
            heatmap = self._heatmap.build(all_futures)
            all_signals.extend(self._heatmap.to_signals(heatmap))

        # Filter weak signals and sort by strength
        filtered = [s for s in all_signals if s.strength >= self._min_str]
        filtered.sort(key=lambda s: s.strength, reverse=True)
        return filtered

    def summarize(self, signals: list[OISignal]) -> dict[str, Any]:
        """High-level summary of signals for dashboard display."""
        if not signals:
            return {"bias": "NEUTRAL", "confidence": 0, "signal_count": 0, "signals": []}

        bull = sum(1 for s in signals if s.sentiment == "BULLISH")
        bear = sum(1 for s in signals if s.sentiment == "BEARISH")
        total = bull + bear or 1

        bull_pct = bull / total * 100
        if bull_pct >= 65:   bias = "BULLISH"
        elif bull_pct <= 35: bias = "BEARISH"
        elif bull_pct >= 55: bias = "MILDLY BULLISH"
        elif bull_pct <= 45: bias = "MILDLY BEARISH"
        else:                bias = "NEUTRAL"

        confidence = min(95, int(abs(bull_pct - 50) * 2))
        avg_str = int(sum(s.strength for s in signals) / len(signals))

        # Top signal per source
        by_source: dict[str, OISignal] = {}
        for sig in signals:
            if sig.source not in by_source or sig.strength > by_source[sig.source].strength:
                by_source[sig.source] = sig

        return {
            "bias":           bias,
            "confidence":     confidence,
            "bullish_count":  bull,
            "bearish_count":  bear,
            "signal_count":   len(signals),
            "avg_strength":   avg_str,
            "top_signals": [
                {
                    "source":  s.source,
                    "type":    s.signal_type,
                    "sentiment": s.sentiment,
                    "strength": s.strength,
                    "description": s.description,
                }
                for s in list(by_source.values())[:8]
            ],
            "sector_bias": self._sector_bias(signals),
        }

    @staticmethod
    def _sector_bias(signals: list[OISignal]) -> list[dict[str, Any]]:
        sector_map: dict[str, dict[str, int]] = {}
        for s in signals:
            if s.sector and s.source == "HEATMAP":
                sector_map[s.sector] = {
                    "bull": s.metadata.get("bull", 0),
                    "bear": s.metadata.get("bear", 0),
                    "net":  s.metadata.get("net", 0),
                }
        return [
            {"sector": k, **v}
            for k, v in sorted(sector_map.items(), key=lambda x: abs(x[1]["net"]), reverse=True)
        ]


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _mean_std(values: list[int | float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    m = sum(values) / len(values)
    if len(values) < 2:
        return m, 0.0
    v = sum((x - m) ** 2 for x in values) / len(values)
    return m, math.sqrt(v)
