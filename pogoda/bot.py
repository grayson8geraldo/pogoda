"""Main bot orchestrator — ties together weather, consensus, strategy, and trading."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, timedelta

from .config import CITIES, Settings
from .consensus import ConsensusResult, find_consensus
from .ensemble import EnsembleAnalysis, fetch_ensemble_analysis
from .paper import PaperTrader
from .polymarket import (
    WeatherMarket,
    fetch_weather_market,
    search_weather_markets,
)
from .strategy import TradeDecision, generate_orders
from .trader import ExecutionReport, Trader
from .weather import WeatherSnapshot, fetch_weather

logger = logging.getLogger(__name__)

# Month name → number mapping
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


def _extract_market_date(title: str) -> date | None:
    """Extract date from market title like 'Highest temperature in NYC on March 28?'"""
    m = re.search(r"on\s+(\w+)\s+(\d{1,2})", title, re.I)
    if m:
        month_name = m.group(1).lower()
        day = int(m.group(2))
        month = _MONTHS.get(month_name)
        if month:
            year = date.today().year
            try:
                return date(year, month, day)
            except ValueError:
                pass
    return None


async def scan_city(
    city_key: str,
    settings: Settings,
    target_date: date | None = None,
    dry_run: bool = True,
    paper_trader: PaperTrader | None = None,
) -> list[tuple[TradeDecision, ExecutionReport | None]]:
    """Run the full pipeline for a single city.

    1. Fetch weather forecasts from 3 models
    2. Find consensus temperature
    3. Search for matching Polymarket markets
    4. Generate trade decisions
    5. Execute orders (or dry-run)
    """
    city_info = CITIES.get(city_key)
    if not city_info:
        logger.error("Unknown city: %s. Available: %s", city_key, list(CITIES.keys()))
        return []

    if target_date is None:
        target_date = date.today() + timedelta(days=1)

    city_name = city_info["name"]
    lat = city_info["lat"]
    lon = city_info["lon"]
    station = city_info["station"]

    logger.info("=" * 60)
    logger.info("Scanning %s (station: %s)", city_name, station)
    logger.info("Target date: %s", target_date)
    logger.info("=" * 60)

    # Step 1: Search for matching markets first (to know which dates we need)
    logger.info("Step 1: Searching Polymarket for %s weather markets...", city_name)
    raw_events = await search_weather_markets(city_name, target_date=target_date)

    if not raw_events:
        logger.info("  No weather markets found for %s", city_name)
        return []

    temp_unit = city_info.get("unit", "C")
    unit_label = "°F" if temp_unit == "F" else "°C"
    results = []
    trader = Trader(settings)

    # Cache forecasts/consensus by date to avoid re-fetching
    forecast_cache: dict[date, tuple[WeatherSnapshot, ConsensusResult]] = {}

    for event in raw_events:
        # Step 2: Parse market
        market = await fetch_weather_market(event)
        if not market:
            continue

        # Determine the market's actual date from its title
        market_date = _extract_market_date(market.question) or target_date
        logger.info("  Found market: %s (%d bins) [date=%s]",
                     market.question, len(market.bins), market_date)

        # Step 3: Get forecast + consensus for this specific date (cached)
        if market_date not in forecast_cache:
            logger.info("  Fetching forecasts for %s...", market_date)
            snapshot = await fetch_weather(lat, lon, city_name, market_date)

            if len(snapshot.forecasts) < 2:
                logger.warning("  Only %d model(s) for %s — skipping",
                               len(snapshot.forecasts), market_date)
                continue

            for name, fc in snapshot.forecasts.items():
                logger.info("    %s: max=%.1f°C, min=%.1f°C", name, fc.max_temp_c, fc.min_temp_c)

            consensus = await find_consensus(snapshot, target_unit=temp_unit)
            logger.info(
                "    Consensus for %s: %d%s (confidence=%s, method=%s)",
                market_date, consensus.consensus_temp_c, unit_label,
                consensus.confidence, consensus.method,
            )
            forecast_cache[market_date] = (snapshot, consensus)
        else:
            snapshot, consensus = forecast_cache[market_date]

        # Log bin prices
        for b in market.sorted_bins:
            marker = " ← CONSENSUS" if market.get_bin(consensus.consensus_temp_c) == b else ""
            logger.info("    %s: %.1f¢%s", b.outcome, b.price, marker)

        # Step 4: Fetch ensemble probabilities for this market's date
        sorted_bins = market.sorted_bins
        ensemble = None
        if sorted_bins:
            temp_lo = sorted_bins[0].temp_value or 0
            temp_hi = sorted_bins[-1].temp_value or 100
            bin_width = 2.0 if market.temp_unit == "F" else 1.0
            ensemble = await fetch_ensemble_analysis(
                lat, lon, market_date,
                temp_range=(temp_lo, temp_hi),
                target_unit=market.temp_unit,
                bin_width=bin_width,
            )

        # Step 5: Generate trade decision
        decision = generate_orders(market, consensus, snapshot, settings, ensemble=ensemble)
        logger.info("\n%s", decision.summary())

        # Step 6: Execute
        report = None
        if decision.checks_passed:
            if paper_trader is not None:
                logger.info("Step 6: Paper trading (virtual balance)...")
                new_positions = paper_trader.execute_decision(decision)
                if new_positions:
                    logger.info(
                        "  Opened %d virtual positions (balance: $%.2f)",
                        len(new_positions),
                        paper_trader.balance_usd,
                    )
            else:
                logger.info("Step 6: %s orders...", "Simulating" if dry_run else "Executing")
                report = await trader.execute(decision, dry_run=dry_run)
                logger.info("\n%s", report.summary())
        else:
            logger.warning("Trade rejected: %s", ", ".join(decision.rejection_reasons))

        results.append((decision, report))

    return results


async def run_full_scan(
    settings: Settings,
    cities: list[str] | None = None,
    target_date: date | None = None,
    dry_run: bool = True,
    paper_trader: PaperTrader | None = None,
) -> dict[str, list[tuple[TradeDecision, ExecutionReport | None]]]:
    """Scan multiple cities for trading opportunities.

    Args:
        settings: Bot settings.
        cities: List of city keys to scan (default: all).
        target_date: Date to forecast for (default: tomorrow).
        dry_run: If True, don't place real orders.
    """
    if cities is None:
        cities = list(CITIES.keys())

    if target_date is None:
        target_date = date.today() + timedelta(days=1)

    all_results: dict[str, list[tuple[TradeDecision, ExecutionReport | None]]] = {}

    # Auto-resolve expired markets before scanning for new ones
    if paper_trader and paper_trader.positions:
        logger.info("Checking for expired markets to auto-resolve...")
        city_coords = {
            info["name"]: (info["lat"], info["lon"])
            for info in CITIES.values()
        }
        # Add NYC alias
        city_coords["NYC"] = city_coords.get("New York", (40.7772, -73.8726))
        resolved = await paper_trader.auto_resolve_expired(city_coords)
        if resolved:
            wins = sum(1 for r in resolved if r.resolved)
            losses = sum(1 for r in resolved if not r.resolved)
            total_pnl = sum(r.pnl_cents for r in resolved) / 100
            logger.info(
                "Auto-resolved %d positions: %d wins, %d losses, P&L: $%.2f",
                len(resolved), wins, losses, total_pnl,
            )

    for city_key in cities:
        try:
            results = await scan_city(city_key, settings, target_date, dry_run, paper_trader)
            all_results[city_key] = results
        except Exception:
            logger.exception("Error scanning %s", city_key)
            all_results[city_key] = []

    # Summary
    total_trades = sum(
        1 for results in all_results.values()
        for decision, _ in results
        if decision.checks_passed
    )
    total_rejected = sum(
        1 for results in all_results.values()
        for decision, _ in results
        if not decision.checks_passed
    )

    logger.info("\n" + "=" * 60)
    logger.info("SCAN COMPLETE")
    logger.info("Cities scanned: %d", len(cities))
    logger.info("Trades %s: %d", "simulated" if dry_run else "executed", total_trades)
    logger.info("Trades rejected: %d", total_rejected)
    logger.info("=" * 60)

    return all_results
