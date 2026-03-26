"""Consensus engine — determines the "true" forecast temperature from multiple models."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from .weather import WeatherSnapshot, fetch_backup_forecast, fetch_wttr_backup

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


async def find_consensus(snapshot: WeatherSnapshot) -> ConsensusResult:
    """Analyze weather models and determine consensus temperature.

    Strategy:
    1. If all 3 models agree within ±1°C → high confidence, use median rounded.
    2. If spread > 1°C → fetch backup sources, pick the value closest to majority.
    3. If still no agreement → low confidence, not tradeable.
    """
    temps = snapshot.max_temps
    model_names = list(temps.keys())
    temp_values = list(temps.values())

    if len(temp_values) < 2:
        return ConsensusResult(
            consensus_temp_c=_round_temp(temp_values[0]) if temp_values else 0,
            confidence="low",
            model_temps=temps,
            spread=0.0,
            method="insufficient_models",
        )

    spread = _compute_spread(temp_values)
    median_temp = _median(temp_values)
    rounded_median = _round_temp(median_temp)

    logger.info(
        "Model temps: %s | Spread: %.1f°C | Median: %.1f°C",
        temps,
        spread,
        median_temp,
    )

    # Case 1: All models agree within CONSENSUS_SPREAD
    if spread <= CONSENSUS_SPREAD:
        return ConsensusResult(
            consensus_temp_c=rounded_median,
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

    # Collect all available temperatures (primary + backup)
    all_temps = list(temp_values)
    backup_used = None
    if backup_temp is not None:
        all_temps.append(backup_temp)
        backup_used = backup_temp
    if wttr_temp is not None:
        all_temps.append(wttr_temp)
        if backup_used is None:
            backup_used = wttr_temp

    # Find the integer temperature that most sources agree on (±0.5°C)
    rounded_all = [_round_temp(t) for t in all_temps]
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
    return ConsensusResult(
        consensus_temp_c=rounded_median,
        confidence="low",
        model_temps=temps,
        spread=spread,
        backup_temp=backup_used,
        method="no_consensus_median_fallback",
    )
