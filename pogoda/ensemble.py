"""Ensemble probability estimation via Open-Meteo ensemble API.

Fetches ensemble forecasts (31-51 members) and estimates the probability
that the daily max temperature falls within each market bin.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

import httpx

from .utils import convert_temp

logger = logging.getLogger(__name__)

ENSEMBLE_API = "https://ensemble-api.open-meteo.com/v1/ensemble"
TIMEOUT = 20.0

# Available ensemble models and their member counts
ENSEMBLE_MODELS = {
    "gfs_seamless": 31,
    "ecmwf_ifs025": 51,
    "icon_seamless": 40,
}
# Fallback URL if the primary endpoint fails
ENSEMBLE_API_FALLBACK = "https://api.open-meteo.com/v1/ensemble"


@dataclass
class EnsembleForecast:
    """Ensemble forecast with per-member daily max temperatures."""

    model_name: str
    date: date
    member_maxes: list[float]  # Daily max temp per ensemble member (in °C)

    @property
    def num_members(self) -> int:
        return len(self.member_maxes)

    def probability_in_range(self, low: float, high: float) -> float:
        """Fraction of ensemble members with daily max in [low, high]."""
        if not self.member_maxes:
            return 0.0
        count = sum(1 for t in self.member_maxes if low <= t <= high)
        return count / len(self.member_maxes)

    def probability_at_temp(self, temp: int, bin_width: float = 1.0) -> float:
        """Probability that daily max rounds to `temp` (±bin_width/2)."""
        return self.probability_in_range(temp - bin_width / 2, temp + bin_width / 2)


@dataclass
class BinProbability:
    """Estimated probability for a single temperature bin."""

    temp_value: int
    probability: float  # 0.0 to 1.0
    bin_width: float = 1.0


@dataclass
class EnsembleAnalysis:
    """Combined ensemble analysis from multiple models."""

    date: date
    models_used: list[str] = field(default_factory=list)
    total_members: int = 0
    bin_probabilities: dict[int, float] = field(default_factory=dict)

    def get_probability(self, temp: int) -> float:
        """Get estimated probability for a specific temperature bin."""
        return self.bin_probabilities.get(temp, 0.0)

    def expected_value(self, temp: int, price_cents: float) -> float:
        """Expected value of buying a bin: P(win) * 100¢ - price."""
        prob = self.get_probability(temp)
        return prob * 100.0 - price_cents


async def _fetch_ensemble(
    client: httpx.AsyncClient,
    model: str,
    lat: float,
    lon: float,
    target_date: date,
) -> EnsembleForecast | None:
    """Fetch ensemble forecast from a single model."""
    num_members = ENSEMBLE_MODELS.get(model, 31)
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m",
        "models": model,
        "timezone": "auto",
        "start_date": str(target_date),
        "end_date": str(target_date),
    }

    try:
        # Try primary endpoint, fallback to alternative
        resp = await client.get(ENSEMBLE_API, params=params, timeout=TIMEOUT)
        if resp.status_code == 404:
            resp = await client.get(ENSEMBLE_API_FALLBACK, params=params, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        hourly = data.get("hourly", {})

        # Extract daily max for each ensemble member
        member_maxes = []
        for i in range(num_members):
            key = f"temperature_2m_member{i:02d}"
            temps = hourly.get(key, [])
            if temps:
                valid_temps = [t for t in temps if t is not None]
                if valid_temps:
                    member_maxes.append(max(valid_temps))

        if not member_maxes:
            logger.warning("No ensemble data from %s", model)
            return None

        return EnsembleForecast(
            model_name=model,
            date=target_date,
            member_maxes=member_maxes,
        )
    except Exception:
        logger.exception("Failed to fetch ensemble from %s", model)
        return None


async def fetch_ensemble_analysis(
    lat: float,
    lon: float,
    target_date: date,
    temp_range: tuple[int, int],
    target_unit: str = "C",
    bin_width: float = 1.0,
) -> EnsembleAnalysis:
    """Fetch ensemble forecasts and compute bin probabilities.

    Args:
        lat, lon: Location coordinates.
        target_date: Date to forecast.
        temp_range: (min_temp, max_temp) range of bins to analyze.
        target_unit: "C" or "F" — convert ensemble data before analysis.
        bin_width: Width of each temperature bin (1 for °C, 2 for °F).

    Returns:
        EnsembleAnalysis with probability estimates for each bin.
    """
    import asyncio

    analysis = EnsembleAnalysis(date=target_date)

    async with httpx.AsyncClient() as client:
        tasks = [
            _fetch_ensemble(client, model, lat, lon, target_date)
            for model in ENSEMBLE_MODELS
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    # Merge all members from all models
    all_maxes: list[float] = []
    for result in results:
        if isinstance(result, EnsembleForecast):
            analysis.models_used.append(result.model_name)
            all_maxes.extend(result.member_maxes)
        elif isinstance(result, Exception):
            logger.error("Ensemble fetch error: %s", result)

    analysis.total_members = len(all_maxes)

    if not all_maxes:
        return analysis

    # Convert to target unit if needed
    if target_unit == "F":
        all_maxes = [convert_temp(t, "F") for t in all_maxes]

    # Compute bin probabilities
    low, high = temp_range
    for temp in range(low, high + 1):
        half = bin_width / 2
        count = sum(1 for t in all_maxes if temp - half <= t < temp + half)
        prob = count / len(all_maxes)
        if prob > 0:
            analysis.bin_probabilities[temp] = round(prob, 4)

    logger.info(
        "Ensemble: %d members from %s | Top bins: %s",
        analysis.total_members,
        analysis.models_used,
        sorted(analysis.bin_probabilities.items(), key=lambda x: -x[1])[:5],
    )

    return analysis
