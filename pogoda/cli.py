"""CLI interface for the Pogoda weather trading bot."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date, timedelta

from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table

from .config import CITIES, get_settings
from .bot import run_full_scan, scan_city
from .consensus import find_consensus
from .weather import fetch_weather

console = Console()


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True)],
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pogoda",
        description="Pogoda — Weather trading bot for Polymarket",
    )
    sub = parser.add_subparsers(dest="command", help="Command to run")

    # --- scan ---
    scan_p = sub.add_parser("scan", help="Scan for trading opportunities")
    scan_p.add_argument(
        "-c", "--cities",
        nargs="+",
        choices=list(CITIES.keys()),
        help="Cities to scan (default: all)",
    )
    scan_p.add_argument(
        "-d", "--date",
        type=str,
        default=None,
        help="Target date (YYYY-MM-DD, default: tomorrow)",
    )
    scan_p.add_argument(
        "--live",
        action="store_true",
        help="Execute real orders (default: dry-run)",
    )
    scan_p.add_argument("-v", "--verbose", action="store_true")

    # --- weather ---
    wx_p = sub.add_parser("weather", help="Check weather forecast for a city")
    wx_p.add_argument("city", choices=list(CITIES.keys()), help="City to check")
    wx_p.add_argument(
        "-d", "--date",
        type=str,
        default=None,
        help="Target date (YYYY-MM-DD, default: tomorrow)",
    )
    wx_p.add_argument("-v", "--verbose", action="store_true")

    # --- markets ---
    mkt_p = sub.add_parser("markets", help="List active weather markets")
    mkt_p.add_argument(
        "-c", "--city",
        type=str,
        default=None,
        help="Filter by city name",
    )
    mkt_p.add_argument("-v", "--verbose", action="store_true")

    # --- cities ---
    sub.add_parser("cities", help="List supported cities")

    return parser.parse_args()


def _parse_date(date_str: str | None) -> date:
    if date_str:
        return date.fromisoformat(date_str)
    return date.today() + timedelta(days=1)


async def cmd_scan(args: argparse.Namespace) -> None:
    settings = get_settings()
    target = _parse_date(args.date)
    dry_run = not args.live

    if dry_run:
        console.print(Panel("[yellow]DRY RUN MODE[/yellow] — no real orders will be placed"))
    else:
        console.print(Panel("[red bold]LIVE MODE[/red bold] — real orders will be placed!"))
        console.print("[yellow]Press Ctrl+C within 5 seconds to cancel...[/yellow]")
        await asyncio.sleep(5)

    results = await run_full_scan(
        settings=settings,
        cities=args.cities,
        target_date=target,
        dry_run=dry_run,
    )

    # Print summary table
    table = Table(title="Trading Summary")
    table.add_column("City", style="cyan")
    table.add_column("Markets", justify="right")
    table.add_column("Trades", justify="right", style="green")
    table.add_column("Rejected", justify="right", style="red")

    for city_key, city_results in results.items():
        trades = sum(1 for d, _ in city_results if d.checks_passed)
        rejected = sum(1 for d, _ in city_results if not d.checks_passed)
        table.add_row(
            CITIES[city_key]["name"],
            str(len(city_results)),
            str(trades),
            str(rejected),
        )

    console.print(table)


async def cmd_weather(args: argparse.Namespace) -> None:
    city_info = CITIES[args.city]
    target = _parse_date(args.date)

    console.print(f"\nFetching forecasts for [cyan]{city_info['name']}[/cyan] "
                  f"(station: {city_info['station']})")
    console.print(f"Target date: [yellow]{target}[/yellow]\n")

    snapshot = await fetch_weather(
        city_info["lat"], city_info["lon"], city_info["name"], target
    )

    # Model forecasts table
    table = Table(title="Weather Model Forecasts")
    table.add_column("Model", style="cyan")
    table.add_column("Max °C", justify="right", style="red")
    table.add_column("Min °C", justify="right", style="blue")

    for name, fc in snapshot.forecasts.items():
        table.add_row(name, f"{fc.max_temp_c:.1f}", f"{fc.min_temp_c:.1f}")

    if snapshot.today_actual_max is not None:
        table.add_row("Today (actual)", f"{snapshot.today_actual_max:.1f}", "—")

    console.print(table)

    # Consensus
    consensus = await find_consensus(snapshot)
    style = {"high": "green", "medium": "yellow", "low": "red"}.get(
        consensus.confidence, "white"
    )
    console.print(
        f"\nConsensus: [{style}]{consensus.consensus_temp_c}°C[/{style}] "
        f"(confidence=[{style}]{consensus.confidence}[/{style}], "
        f"method={consensus.method}, spread={consensus.spread:.1f}°C)"
    )
    if consensus.backup_temp is not None:
        console.print(f"Backup source: {consensus.backup_temp:.1f}°C")


async def cmd_markets(args: argparse.Namespace) -> None:
    from .polymarket import search_weather_markets, fetch_weather_market

    console.print("Searching for weather markets on Polymarket...")
    events = await search_weather_markets(args.city)

    if not events:
        console.print("[yellow]No weather markets found.[/yellow]")
        return

    for event in events:
        market = await fetch_weather_market(event)
        if not market:
            continue

        console.print(f"\n[bold cyan]{market.question}[/bold cyan]")
        console.print(f"  End: {market.end_date}")
        if market.resolution_source:
            console.print(f"  Resolution: {market.resolution_source[:100]}")

        table = Table(show_header=True, header_style="bold")
        table.add_column("Temp", justify="right")
        table.add_column("Price (¢)", justify="right")
        table.add_column("Token ID", max_width=20)

        for b in market.sorted_bins:
            table.add_row(
                b.outcome,
                f"{b.price:.1f}",
                b.token_id[:20] + "..." if len(b.token_id) > 20 else b.token_id,
            )
        console.print(table)


def cmd_cities() -> None:
    table = Table(title="Supported Cities")
    table.add_column("Key", style="cyan")
    table.add_column("City", style="bold")
    table.add_column("Station")
    table.add_column("Lat", justify="right")
    table.add_column("Lon", justify="right")

    for key, info in CITIES.items():
        table.add_row(
            key,
            info["name"],
            info["station"],
            f"{info['lat']:.4f}",
            f"{info['lon']:.4f}",
        )
    console.print(table)


def main() -> None:
    args = parse_args()
    setup_logging(getattr(args, "verbose", False))

    if args.command is None:
        console.print("[bold]Pogoda[/bold] — Weather trading bot for Polymarket\n")
        console.print("Usage: pogoda <command> [options]\n")
        console.print("Commands:")
        console.print("  [cyan]scan[/cyan]     Scan for trading opportunities")
        console.print("  [cyan]weather[/cyan]  Check weather forecast for a city")
        console.print("  [cyan]markets[/cyan]  List active weather markets")
        console.print("  [cyan]cities[/cyan]   List supported cities")
        console.print("\nRun [cyan]pogoda <command> --help[/cyan] for details.")
        sys.exit(0)

    if args.command == "cities":
        cmd_cities()
        return

    # Run async commands
    coro = {
        "scan": cmd_scan,
        "weather": cmd_weather,
        "markets": cmd_markets,
    }.get(args.command)

    if coro:
        asyncio.run(coro(args))
    else:
        console.print(f"[red]Unknown command: {args.command}[/red]")
        sys.exit(1)


if __name__ == "__main__":
    main()
