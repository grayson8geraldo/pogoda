"""Main bot orchestrator — ties together weather, consensus, strategy, and trading."""

from __future__ import annotations

import asyncio
import logging
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

    # Step 1: Fetch weather data
    logger.info("Step 1: Fetching weather forecasts...")
    snapshot = await fetch_weather(lat, lon, city_name, target_date)

    if len(snapshot.forecasts) < 2:
        logger.warning("Only %d model(s) returned data — skipping", len(snapshot.forecasts))
        return []

    for name, fc in snapshot.forecasts.items():
        logger.info("  %s: max=%.1f°C, min=%.1f°C", name, fc.max_temp_c, fc.min_temp_c)

    if snapshot.today_actual_max is not None:
        logger.info("  Today's actual max: %.1f°C", snapshot.today_actual_max)

    # Step 2: Find consensus (use city's temperature unit for bin matching)
    temp_unit = city_info.get("unit", "C")
    logger.info("Step 2: Finding consensus (unit=%s)...", temp_unit)
    consensus = await find_consensus(snapshot, target_unit=temp_unit)
    unit_label = "°F" if temp_unit == "F" else "°C"
    logger.info(
        "  Consensus: %d%s (confidence=%s, method=%s)",
        consensus.consensus_temp_c,
        unit_label,
        consensus.confidence,
        consensus.method,
    )

    # Step 3: Search for matching markets
    logger.info("Step 3: Searching Polymarket for %s weather markets...", city_name)
    raw_events = await search_weather_markets(city_name, target_date=target_date)

    if not raw_events:
        logger.info("  No weather markets found for %s", city_name)
        return []

    results = []
    trader = Trader(settings)

    for event in raw_events:
        # Step 4: Parse market
        market = await fetch_weather_market(event)
        if not market:
            continue

        logger.info("  Found market: %s (%d bins)", market.question, len(market.bins))

        # Log bin prices around consensus
        for b in market.sorted_bins:
            marker = " ← CONSENSUS" if b.temp_value == consensus.consensus_temp_c else ""
            logger.info("    %s: %.1f¢%s", b.outcome, b.price, marker)

        # Step 4b: Fetch ensemble probabilities for better EV estimation
        logger.info("Step 4b: Fetching ensemble probability estimates...")
        sorted_bins = market.sorted_bins
        if sorted_bins:
            temp_lo = sorted_bins[0].temp_value or 0
            temp_hi = sorted_bins[-1].temp_value or 100
            bin_width = 2.0 if market.temp_unit == "F" else 1.0
            ensemble = await fetch_ensemble_analysis(
                lat, lon, target_date,
                temp_range=(temp_lo, temp_hi),
                target_unit=market.temp_unit,
                bin_width=bin_width,
            )
        else:
            ensemble = None

        # Step 5: Generate trade decision
        decision = generate_orders(market, consensus, snapshot, settings, ensemble=ensemble)
        logger.info("\n%s", decision.summary())

        # Step 6: Execute
        report = None
        if decision.checks_passed:
            if paper_trader is not None:
                # Paper trading mode — simulate with virtual balance
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
