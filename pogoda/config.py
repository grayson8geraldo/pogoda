"""Bot configuration loaded from environment variables."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    # Polymarket CLOB credentials
    polymarket_api_key: str = ""
    polymarket_api_secret: str = ""
    polymarket_api_passphrase: str = ""
    polymarket_host: str = "https://clob.polymarket.com"
    private_key: str = ""
    chain_id: int = 137

    # Trading parameters
    max_position_size_usd: float = 50.0
    shares_per_bin: int = 10
    min_profit_percent: float = 10.0
    max_total_cost_cents: float = 96.0
    enable_lottery_bins: bool = True

    # Weather stability threshold (max °C difference today vs tomorrow for "stable" weather)
    stability_threshold_c: float = 3.0

    # Limit order price offset — place limit bids slightly below current ask (in cents)
    limit_price_offset_cents: float = 0.5


# Supported cities with their airport weather-station coordinates
# unit: "C" or "F" — Polymarket US markets typically use Fahrenheit
CITIES: dict[str, dict] = {
    "tokyo": {
        "name": "Tokyo",
        "station": "Tokyo Haneda Airport",
        "icao": "RJTT",
        "lat": 35.5494,
        "lon": 139.7798,
        "unit": "C",
    },
    "london": {
        "name": "London",
        "station": "London Heathrow Airport",
        "icao": "EGLL",
        "lat": 51.4700,
        "lon": -0.4543,
        "unit": "C",
    },
    "seoul": {
        "name": "Seoul",
        "station": "Incheon International Airport",
        "icao": "RKSI",
        "lat": 37.4602,
        "lon": 126.4407,
        "unit": "C",
    },
    "ankara": {
        "name": "Ankara",
        "station": "Esenboğa Airport",
        "icao": "LTAC",
        "lat": 40.1281,
        "lon": 32.9951,
        "unit": "C",
    },
    "new_york": {
        "name": "New York",
        "station": "LaGuardia Airport",
        "icao": "KLGA",
        "lat": 40.7772,
        "lon": -73.8726,
        "unit": "F",
    },
    "chicago": {
        "name": "Chicago",
        "station": "O'Hare International Airport",
        "icao": "KORD",
        "lat": 41.9742,
        "lon": -87.9073,
        "unit": "F",
    },
    "paris": {
        "name": "Paris",
        "station": "Charles de Gaulle Airport",
        "icao": "LFPG",
        "lat": 49.0097,
        "lon": 2.5479,
        "unit": "C",
    },
    "berlin": {
        "name": "Berlin",
        "station": "Berlin Brandenburg Airport",
        "icao": "EDDB",
        "lat": 52.3667,
        "lon": 13.5033,
        "unit": "C",
    },
    "sydney": {
        "name": "Sydney",
        "station": "Sydney Kingsford Smith Airport",
        "icao": "YSSY",
        "lat": -33.9461,
        "lon": 151.1772,
        "unit": "C",
    },
    "mumbai": {
        "name": "Mumbai",
        "station": "Chhatrapati Shivaji Maharaj Airport",
        "icao": "VABB",
        "lat": 19.0896,
        "lon": 72.8656,
        "unit": "C",
    },
}

# Reverse lookup: ICAO code → city key
ICAO_TO_CITY: dict[str, str] = {
    info["icao"]: key for key, info in CITIES.items()
}


def get_settings() -> Settings:
    return Settings()
