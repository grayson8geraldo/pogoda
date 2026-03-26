"""Polymarket CLOB API client for weather markets."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
TIMEOUT = 15.0


@dataclass
class TemperatureBin:
    """A single temperature outcome bin in a weather market."""

    token_id: str
    outcome: str  # e.g. "12" or "12°C" or "≥30"
    temp_value: int | None  # parsed integer temp, None for range bins
    price: float  # current price in cents (0-100)
    is_range: bool = False  # True for bins like "≥30" or "≤-5"

    @property
    def price_dollars(self) -> float:
        return self.price / 100.0


@dataclass
class WeatherMarket:
    """A Polymarket weather market event with its temperature bins."""

    condition_id: str
    question: str
    description: str
    city: str
    end_date: str
    bins: list[TemperatureBin] = field(default_factory=list)
    resolution_source: str = ""
    min_tick_size: float = 0.01
    temp_unit: str = "C"  # "C" or "F" — detected from market title/bins
    station_icao: str = ""  # Parsed from market rules (e.g. "KLGA")

    @property
    def sorted_bins(self) -> list[TemperatureBin]:
        """Return bins sorted by temperature value."""
        return sorted(
            [b for b in self.bins if b.temp_value is not None],
            key=lambda b: b.temp_value,  # type: ignore[arg-type]
        )

    def get_bin(self, temp: int) -> TemperatureBin | None:
        """Find bin matching a specific temperature."""
        for b in self.bins:
            if b.temp_value == temp:
                return b
        return None

    def get_bins_range(self, center: int, radius: int) -> list[TemperatureBin]:
        """Get bins within ±radius of center temperature."""
        result = []
        for offset in range(-radius, radius + 1):
            b = self.get_bin(center + offset)
            if b:
                result.append(b)
        return result


def _parse_temp_from_outcome(outcome: str) -> tuple[int | None, bool]:
    """Parse temperature integer from outcome string.

    Handles formats like: "12", "12°C", "12°F", "12 °C", "≥30", "≤-5", ">30", "<-5"
    """
    outcome = outcome.strip()

    # Range bins (≥, ≤, >, <)
    range_match = re.match(r"[≥≤><]\s*(-?\d+)", outcome)
    if range_match:
        return int(range_match.group(1)), True

    # Regular temperature bins
    temp_match = re.match(r"(-?\d+)", outcome)
    if temp_match:
        return int(temp_match.group(1)), False

    return None, False


async def search_weather_markets(city: str | None = None) -> list[dict]:
    """Search Polymarket for active weather/temperature markets.

    Uses the Gamma API to find events related to weather/temperature.
    """
    async with httpx.AsyncClient() as client:
        # Search for temperature-related markets
        params: dict = {
            "active": "true",
            "closed": "false",
            "limit": 100,
        }

        try:
            resp = await client.get(
                f"{GAMMA_API}/events",
                params=params,
                timeout=TIMEOUT,
            )
            resp.raise_for_status()
            events = resp.json()
        except Exception:
            logger.exception("Failed to fetch events from Gamma API")
            return []

        # Filter for weather/temperature markets
        weather_events = []
        keywords = ["temperature", "weather", "°c", "°f", "degrees", "high temp"]
        city_lower = city.lower() if city else None

        for event in events:
            title = event.get("title", "").lower()
            desc = event.get("description", "").lower()
            combined = title + " " + desc

            is_weather = any(kw in combined for kw in keywords)
            city_match = city_lower is None or city_lower in combined

            if is_weather and city_match:
                weather_events.append(event)

        logger.info("Found %d weather markets%s", len(weather_events),
                     f" for {city}" if city else "")
        return weather_events


async def get_market_details(condition_id: str) -> dict | None:
    """Fetch detailed market info including token IDs and prices."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{CLOB_API}/markets/{condition_id}",
                timeout=TIMEOUT,
            )
            resp.raise_for_status()
            return resp.json()
    except Exception:
        logger.exception("Failed to fetch market details for %s", condition_id)
        return None


async def get_market_orderbook(token_id: str) -> dict:
    """Fetch the orderbook for a specific token (outcome)."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{CLOB_API}/book",
                params={"token_id": token_id},
                timeout=TIMEOUT,
            )
            resp.raise_for_status()
            return resp.json()
    except Exception:
        logger.exception("Failed to fetch orderbook for token %s", token_id)
        return {"bids": [], "asks": []}


async def fetch_weather_market(event: dict) -> WeatherMarket | None:
    """Convert a raw Gamma API event into a structured WeatherMarket with priced bins."""
    markets = event.get("markets", [])
    if not markets:
        return None

    title = event.get("title", "")
    description = event.get("description", "")
    end_date = event.get("endDate", "")

    # Extract city name from title (heuristic)
    city = ""
    for word in title.split():
        if word[0].isupper() and len(word) > 2 and word.lower() not in {
            "what", "will", "the", "high", "low", "temperature", "daily", "max", "min",
            "for", "on", "in", "be", "degrees",
        }:
            city = word
            break

    all_bins: list[TemperatureBin] = []
    condition_id = ""
    resolution_source = ""
    min_tick = 0.01

    for mkt in markets:
        cid = mkt.get("conditionId", "") or mkt.get("condition_id", "")
        if not condition_id and cid:
            condition_id = cid

        if not resolution_source:
            resolution_source = mkt.get("resolutionSource", "") or mkt.get("description", "")

        outcomes = mkt.get("outcomes", "")
        if isinstance(outcomes, str):
            outcomes = [o.strip() for o in outcomes.split(",")]

        outcome_prices = mkt.get("outcomePrices", "")
        if isinstance(outcome_prices, str):
            try:
                outcome_prices = [float(p.strip()) for p in outcome_prices.strip("[]").split(",")]
            except ValueError:
                outcome_prices = []

        tokens = mkt.get("clobTokenIds", "")
        if isinstance(tokens, str):
            tokens = [t.strip() for t in tokens.strip("[]").split(",") if t.strip()]

        group_slug = mkt.get("groupItemTitle", "") or ""

        # If this is a single-outcome market within a group (each market = one bin)
        if group_slug:
            temp_val, is_range = _parse_temp_from_outcome(group_slug)
            price = outcome_prices[0] * 100 if outcome_prices else 0.0
            token_id = tokens[0] if tokens else ""
            if token_id:
                all_bins.append(TemperatureBin(
                    token_id=token_id,
                    outcome=group_slug,
                    temp_value=temp_val,
                    price=round(price, 2),
                    is_range=is_range,
                ))
        else:
            # Multi-outcome market — each outcome is a bin
            for i, outcome_name in enumerate(outcomes):
                temp_val, is_range = _parse_temp_from_outcome(outcome_name)
                price = outcome_prices[i] * 100 if i < len(outcome_prices) else 0.0
                token_id = tokens[i] if i < len(tokens) else ""
                if token_id:
                    all_bins.append(TemperatureBin(
                        token_id=token_id,
                        outcome=outcome_name,
                        temp_value=temp_val,
                        price=round(price, 2),
                        is_range=is_range,
                    ))

    if not all_bins:
        return None

    # Detect temperature unit from title/bins
    temp_unit = _detect_temp_unit(title, [b.outcome for b in all_bins])

    # Parse station ICAO code from description/resolution rules
    station_icao = _parse_station_icao(description + " " + resolution_source)

    return WeatherMarket(
        condition_id=condition_id,
        question=title,
        description=description,
        city=city,
        end_date=end_date,
        bins=all_bins,
        resolution_source=resolution_source,
        min_tick_size=min_tick,
        temp_unit=temp_unit,
        station_icao=station_icao,
    )


def _detect_temp_unit(title: str, outcomes: list[str]) -> str:
    """Detect whether market uses Celsius or Fahrenheit from title and outcome labels."""
    combined = title.lower() + " " + " ".join(outcomes).lower()
    if "°f" in combined or "fahrenheit" in combined:
        return "F"
    if "°c" in combined or "celsius" in combined:
        return "C"
    # Heuristic: if bin values > 50, likely Fahrenheit (most cities don't hit 50°C)
    for outcome in outcomes:
        match = re.match(r"(-?\d+)", outcome.strip())
        if match and int(match.group(1)) > 50:
            return "F"
    return "C"


def _parse_station_icao(text: str) -> str:
    """Extract ICAO airport code from market rules text.

    Looks for 4-letter codes like KLGA, EGLL, RJTT near words like
    'airport', 'station', 'weather'.
    """
    # Pattern: 4 uppercase letters that look like ICAO codes (start with K for US, etc.)
    icao_pattern = re.compile(r"\b([A-Z]{4})\b")
    matches = icao_pattern.findall(text)

    # Known ICAO prefixes for major regions
    valid_prefixes = {
        "K",    # US
        "C",    # Canada
        "EG",   # UK
        "LF",   # France
        "ED",   # Germany
        "LT",   # Turkey
        "RJ",   # Japan
        "RK",   # South Korea
        "ZS",   # China
        "VA",   # India (west)
        "VO",   # India (south)
        "VE",   # India (east)
        "YS",   # Australia (Sydney)
        "YM",   # Australia (Melbourne)
    }

    for match in matches:
        for prefix in valid_prefixes:
            if match.startswith(prefix):
                return match
    return ""
