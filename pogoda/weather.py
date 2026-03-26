"""Weather data fetching from Open-Meteo (ECMWF, GFS, ICON) and backup sources."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime

import httpx

logger = logging.getLogger(__name__)

OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_ECMWF = "https://api.open-meteo.com/v1/ecmwf"
OPEN_METEO_DWD_ICON = "https://api.open-meteo.com/v1/dwd-icon"
OPEN_METEO_GFS = "https://api.open-meteo.com/v1/gfs"

# Timeout for HTTP requests
TIMEOUT = 15.0


@dataclass
class ModelForecast:
    """Temperature forecast from a single weather model."""

    model_name: str
    date: date
    max_temp_c: float
    min_temp_c: float
    hourly_temps: list[float] = field(default_factory=list)


@dataclass
class WeatherSnapshot:
    """Aggregated weather data for a location/date across all models."""

    city: str
    lat: float
    lon: float
    target_date: date
    forecasts: dict[str, ModelForecast] = field(default_factory=dict)
    today_actual_max: float | None = None

    @property
    def model_names(self) -> list[str]:
        return list(self.forecasts.keys())

    @property
    def max_temps(self) -> dict[str, float]:
        return {name: f.max_temp_c for name, f in self.forecasts.items()}


async def _fetch_model(
    client: httpx.AsyncClient,
    url: str,
    model_name: str,
    lat: float,
    lon: float,
    target_date: date,
) -> ModelForecast | None:
    """Fetch forecast from a single Open-Meteo model endpoint."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max,temperature_2m_min",
        "hourly": "temperature_2m",
        "timezone": "auto",
        "start_date": str(target_date),
        "end_date": str(target_date),
    }
    try:
        resp = await client.get(url, params=params, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        daily = data.get("daily", {})
        max_temps = daily.get("temperature_2m_max", [])
        min_temps = daily.get("temperature_2m_min", [])

        if not max_temps:
            logger.warning("No daily data from %s for %s", model_name, target_date)
            return None

        hourly = data.get("hourly", {})
        hourly_temps = hourly.get("temperature_2m", [])

        return ModelForecast(
            model_name=model_name,
            date=target_date,
            max_temp_c=round(max_temps[0], 1),
            min_temp_c=round(min_temps[0], 1) if min_temps else 0.0,
            hourly_temps=[round(t, 1) for t in hourly_temps if t is not None],
        )
    except Exception:
        logger.exception("Failed to fetch %s forecast", model_name)
        return None


async def _fetch_today_actual(
    client: httpx.AsyncClient,
    lat: float,
    lon: float,
    today: date,
) -> float | None:
    """Fetch today's actual/observed max temperature for stability check."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max",
        "timezone": "auto",
        "start_date": str(today),
        "end_date": str(today),
    }
    try:
        resp = await client.get(OPEN_METEO_FORECAST, params=params, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        max_temps = data.get("daily", {}).get("temperature_2m_max", [])
        return round(max_temps[0], 1) if max_temps else None
    except Exception:
        logger.exception("Failed to fetch today's actual temperature")
        return None


async def fetch_weather(
    lat: float,
    lon: float,
    city: str,
    target_date: date,
) -> WeatherSnapshot:
    """Fetch forecasts from ECMWF, GFS, and ICON models for a given location and date."""
    snapshot = WeatherSnapshot(
        city=city,
        lat=lat,
        lon=lon,
        target_date=target_date,
    )

    models = [
        (OPEN_METEO_ECMWF, "ECMWF"),
        (OPEN_METEO_GFS, "GFS"),
        (OPEN_METEO_DWD_ICON, "ICON"),
    ]

    async with httpx.AsyncClient() as client:
        # Fetch all three models concurrently
        import asyncio

        tasks = [
            _fetch_model(client, url, name, lat, lon, target_date)
            for url, name in models
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, ModelForecast):
                snapshot.forecasts[result.model_name] = result
            elif isinstance(result, Exception):
                logger.error("Model fetch error: %s", result)

        # Fetch today's actual temperature for stability check
        today = date.today()
        if target_date > today:
            snapshot.today_actual_max = await _fetch_today_actual(client, lat, lon, today)

    return snapshot


async def fetch_backup_forecast(
    lat: float,
    lon: float,
    target_date: date,
) -> float | None:
    """Fetch backup forecast from Open-Meteo's best-match (multi-model ensemble).

    Used when the three primary models disagree.
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max",
        "timezone": "auto",
        "start_date": str(target_date),
        "end_date": str(target_date),
        "models": "best_match",
    }
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(OPEN_METEO_FORECAST, params=params, timeout=TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            max_temps = data.get("daily", {}).get("temperature_2m_max", [])
            if max_temps:
                return round(max_temps[0], 1)
    except Exception:
        logger.exception("Failed to fetch backup forecast")
    return None


async def fetch_wttr_backup(city: str, target_date: date) -> float | None:
    """Fetch backup forecast from wttr.in (uses multiple sources internally)."""
    try:
        url = f"https://wttr.in/{city}?format=j1"
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            # wttr.in returns forecast for next days
            today = date.today()
            day_offset = (target_date - today).days
            if 0 <= day_offset < len(data.get("weather", [])):
                day_data = data["weather"][day_offset]
                return float(day_data["maxtempC"])
    except Exception:
        logger.exception("Failed to fetch wttr.in backup forecast")
    return None


async def fetch_nws_backup(lat: float, lon: float, target_date: date) -> float | None:
    """Fetch backup forecast from NWS API (US locations only, free, no key).

    Returns max temperature in Celsius, or None if unavailable.
    """
    try:
        async with httpx.AsyncClient() as client:
            # Step 1: Get the forecast office and grid coordinates
            points_resp = await client.get(
                f"https://api.weather.gov/points/{lat},{lon}",
                headers={"User-Agent": "pogoda-bot/1.0"},
                timeout=TIMEOUT,
            )
            points_resp.raise_for_status()
            points_data = points_resp.json()
            forecast_url = points_data["properties"]["forecast"]

            # Step 2: Get the forecast
            fc_resp = await client.get(
                forecast_url,
                headers={"User-Agent": "pogoda-bot/1.0"},
                timeout=TIMEOUT,
            )
            fc_resp.raise_for_status()
            fc_data = fc_resp.json()

            for period in fc_data["properties"]["periods"]:
                # NWS periods alternate day/night; daytime has isDaytime=True
                if not period.get("isDaytime", False):
                    continue
                # Parse start time to check if it matches target date
                start = period.get("startTime", "")
                if str(target_date) in start:
                    temp = period["temperature"]
                    unit = period["temperatureUnit"]
                    if unit == "F":
                        from .utils import f_to_c
                        return round(f_to_c(temp), 1)
                    return round(float(temp), 1)
    except Exception:
        logger.exception("Failed to fetch NWS backup forecast")
    return None
