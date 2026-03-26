"""Consensus engine — determines the "true" forecast temperature from multiple models."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from .utils import convert_temp
from .weather import WeatherSnapshot, fetch_backup_forecast, fetch_nws_backup, fetch_wttr_backup

logger = logging.getLogger(__name__)

# Maximum allowed spread (°C) between all models for automatic consensus
CONSENSUS_SPREAD = 1.0


@dataclass
class ConsensusResult:
    """Result of the consensus analysis."""

    consensus_temp_c: int  # The integer temperature to bet on
    confidence: str  # "high" (all agree), "medium" (backup confirmed), "low"
    model_temps: dict[str, float]  # Raw temps from each model
    spread: float  # Max difference between models
    backup_temp: float | None = None
    method: str = ""  # How consensus was reached

    @property
    def is_tradeable(self) -> bool:
        return self.confidence in ("high", "medium")


def _round_temp(temp: float) -> int:
    """Round temperature to nearest integer (standard rounding)."""
    return round(temp)


def _compute_spread(temps: list[float]) -> float:
    """Max difference between any two model temperatures."""
    if len(temps) < 2:
        return 0.0
    return max(temps) - min(temps)


def _median(values: list[float]) -> float:
    """Simple median calculation."""
    s = sorted(values)
    n = len(s)
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2


async def find_consensus(
    snapshot: WeatherSnapshot,
    target_unit: str = "C",
) -> ConsensusResult:
    """Analyze weather models and determine consensus temperature.

    Strategy:
    1. If all 3 models agree within ±1°C → high confidence, use median rounded.
    2. If spread > 1°C → fetch backup sources (NWS for US, wttr.in, Open-Meteo ensemble).
    3. Majority vote among all sources. If still no agreement → low confidence.

    Args:
        snapshot: Weather data from all models.
        target_unit: "C" or "F" — convert consensus to match market bins.
    """
    temps = snapshot.max_temps
    temp_values = list(temps.values())

    if len(temp_values) < 2:
        raw = temp_values[0] if temp_values else 0.0
        converted = convert_temp(raw, target_unit) if target_unit == "F" else raw
        return ConsensusResult(
            consensus_temp_c=_round_temp(converted),
            confidence="low",
            model_temps=temps,
            spread=0.0,
            method="insufficient_models",
        )

    spread = _compute_spread(temp_values)
    median_temp = _median(temp_values)

    logger.info(
        "Model temps: %s | Spread: %.1f°C | Median: %.1f°C",
        temps,
        spread,
        median_temp,
    )

    # Convert to target unit for bin matching
    def to_target(t: float) -> float:
        return convert_temp(t, target_unit) if target_unit == "F" else t

    # Case 1: All models agree within CONSENSUS_SPREAD
    if spread <= CONSENSUS_SPREAD:
        consensus_val = _round_temp(to_target(median_temp))
        return ConsensusResult(
            consensus_temp_c=consensus_val,
            confidence="high",
            model_temps=temps,
            spread=spread,
            method="all_models_agree",
        )

    # Case 2: Models disagree — fetch backup sources
    logger.info("Models disagree (spread=%.1f°C), fetching backup sources...", spread)

    backup_temp = await fetch_backup_forecast(
        snapshot.lat, snapshot.lon, snapshot.target_date
    )
    wttr_temp = await fetch_wttr_backup(snapshot.city, snapshot.target_date)

    # NWS backup for US locations (lat ~25-50, lon ~-130 to -60)
    nws_temp = None
    if -130 <= snapshot.lon <= -60 and 24 <= snapshot.lat <= 50:
        nws_temp = await fetch_nws_backup(
            snapshot.lat, snapshot.lon, snapshot.target_date
        )

    # Collect all available temperatures (primary + backup, all in °C)
    all_temps = list(temp_values)
    backup_used = None
    for src in [backup_temp, wttr_temp, nws_temp]:
        if src is not None:
            all_temps.append(src)
            if backup_used is None:
                backup_used = src

    # Convert all to target unit, then round for voting
    converted_all = [to_target(t) for t in all_temps]
    rounded_all = [_round_temp(t) for t in converted_all]
    from collections import Counter

    vote_counts = Counter(rounded_all)
    most_common_temp, most_common_count = vote_counts.most_common(1)[0]

    # If majority (>= 3 of 5 or >= 2 of 3) agrees
    if most_common_count >= max(2, math.ceil(len(all_temps) / 2)):
        return ConsensusResult(
            consensus_temp_c=most_common_temp,
            confidence="medium",
            model_temps=temps,
            spread=spread,
            backup_temp=backup_used,
            method=f"majority_vote ({most_common_count}/{len(all_temps)})",
        )

    # No strong consensus — use median but mark as low confidence
    rounded_median = _round_temp(to_target(median_temp))
    return ConsensusResult(
        consensus_temp_c=rounded_median,
        confidence="low",
        model_temps=temps,
        spread=spread,
        backup_temp=backup_used,
        method="no_consensus_median_fallback",
    )
