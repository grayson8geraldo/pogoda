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
CITIES: dict[str, dict] = {
    "tokyo": {
        "name": "Tokyo",
        "station": "Tokyo Haneda Airport",
        "lat": 35.5494,
        "lon": 139.7798,
    },
    "london": {
        "name": "London",
        "station": "London Heathrow Airport",
        "lat": 51.4700,
        "lon": -0.4543,
    },
    "seoul": {
        "name": "Seoul",
        "station": "Incheon International Airport",
        "lat": 37.4602,
        "lon": 126.4407,
    },
    "ankara": {
        "name": "Ankara",
        "station": "Esenboğa Airport",
        "lat": 40.1281,
        "lon": 32.9951,
    },
    "new_york": {
        "name": "New York",
        "station": "JFK International Airport",
        "lat": 40.6413,
        "lon": -73.7781,
    },
    "chicago": {
        "name": "Chicago",
        "station": "O'Hare International Airport",
        "lat": 41.9742,
        "lon": -87.9073,
    },
    "paris": {
        "name": "Paris",
        "station": "Charles de Gaulle Airport",
        "lat": 49.0097,
        "lon": 2.5479,
    },
    "berlin": {
        "name": "Berlin",
        "station": "Berlin Brandenburg Airport",
        "lat": 52.3667,
        "lon": 13.5033,
    },
    "sydney": {
        "name": "Sydney",
        "station": "Sydney Kingsford Smith Airport",
        "lat": -33.9461,
        "lon": 151.1772,
    },
    "mumbai": {
        "name": "Mumbai",
        "station": "Chhatrapati Shivaji Maharaj Airport",
        "lat": 19.0896,
        "lon": 72.8656,
    },
}


def get_settings() -> Settings:
    return Settings()
