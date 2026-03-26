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
from .paper import PaperTrader
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
        help="Execute real orders (default: paper trading)",
    )
    scan_p.add_argument(
        "--balance",
        type=float,
        default=None,
        help="Initial virtual balance in USD for paper trading (default: 200)",
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

    # --- portfolio ---
    port_p = sub.add_parser("portfolio", help="Show paper trading portfolio")
    port_p.add_argument("-v", "--verbose", action="store_true")

    # --- resolve ---
    res_p = sub.add_parser("resolve", help="Resolve a paper trading market")
    res_p.add_argument("market", type=str, help="Market question string (partial match)")
    res_p.add_argument("temp", type=int, help="Winning temperature value")
    res_p.add_argument("-v", "--verbose", action="store_true")

    # --- reset ---
    reset_p = sub.add_parser("reset", help="Reset paper trading portfolio")
    reset_p.add_argument(
        "--balance",
        type=float,
        default=200.0,
        help="New starting balance in USD (default: 200)",
    )
    reset_p.add_argument("-v", "--verbose", action="store_true")

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
    is_live = args.live

    if is_live:
        console.print(Panel("[red bold]LIVE MODE[/red bold] — real orders will be placed!"))
        console.print("[yellow]Press Ctrl+C within 5 seconds to cancel...[/yellow]")
        await asyncio.sleep(5)
        paper_trader = None
        dry_run = False
    else:
        # Paper trading mode (default)
        balance = args.balance or 200.0
        paper_trader = PaperTrader(initial_balance_usd=balance)
        console.print(Panel(
            f"[green]PAPER TRADING MODE[/green] — virtual balance: "
            f"[bold]${paper_trader.balance_usd:.2f}[/bold]\n"
            f"Real market data, virtual orders. No real money at risk."
        ))
        dry_run = True

    results = await run_full_scan(
        settings=settings,
        cities=args.cities,
        target_date=target,
        dry_run=dry_run,
        paper_trader=paper_trader,
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

    # Show paper trading portfolio summary
    if paper_trader is not None:
        console.print()
        console.print(paper_trader.summary())


async def cmd_weather(args: argparse.Namespace) -> None:
    city_info = CITIES[args.city]
    target = _parse_date(args.date)
    temp_unit = city_info.get("unit", "C")

    console.print(f"\nFetching forecasts for [cyan]{city_info['name']}[/cyan] "
                  f"(station: {city_info['station']}, ICAO: {city_info['icao']})")
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
    consensus = await find_consensus(snapshot, target_unit=temp_unit)
    style = {"high": "green", "medium": "yellow", "low": "red"}.get(
        consensus.confidence, "white"
    )
    unit_label = "°F" if temp_unit == "F" else "°C"
    console.print(
        f"\nConsensus: [{style}]{consensus.consensus_temp_c}{unit_label}[/{style}] "
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
        console.print(f"  End: {market.end_date} | Unit: {market.temp_unit}")
        if market.station_icao:
            console.print(f"  Station: {market.station_icao}")
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


def cmd_portfolio() -> None:
    """Show paper trading portfolio status."""
    paper = PaperTrader()
    console.print(paper.summary())


def cmd_resolve(args: argparse.Namespace) -> None:
    """Resolve a paper trading market with the actual winning temperature."""
    paper = PaperTrader()

    # Find matching positions
    query = args.market.lower()
    matching_markets = set()
    for pos in paper.positions:
        if query in pos.market_question.lower():
            matching_markets.add(pos.market_question)

    if not matching_markets:
        console.print(f"[yellow]No open positions matching '{args.market}'[/yellow]")
        if paper.positions:
            console.print("\nOpen positions in:")
            for mq in set(p.market_question for p in paper.positions):
                console.print(f"  - {mq}")
        return

    for market_q in matching_markets:
        console.print(f"\nResolving: [cyan]{market_q}[/cyan]")
        console.print(f"Winning temperature: [bold]{args.temp}[/bold]")

        records = paper.resolve_market(market_q, args.temp)

        if records:
            table = Table(title="Resolution Results")
            table.add_column("Outcome")
            table.add_column("Result", justify="center")
            table.add_column("P&L", justify="right")

            for r in records:
                result_style = "green bold" if r.resolved else "red"
                result_text = "WON" if r.resolved else "LOST"
                pnl_style = "green" if r.pnl_cents > 0 else "red"
                table.add_row(
                    r.outcome,
                    f"[{result_style}]{result_text}[/{result_style}]",
                    f"[{pnl_style}]{r.pnl_cents / 100:+.2f}$[/{pnl_style}]",
                )
            console.print(table)

    console.print(f"\n[bold]Updated balance: ${paper.balance_usd:.2f}[/bold]")


def cmd_reset(args: argparse.Namespace) -> None:
    """Reset paper trading portfolio."""
    paper = PaperTrader()
    old_balance = paper.balance_usd
    paper.reset(args.balance)
    console.print(
        f"Portfolio reset: ${old_balance:.2f} -> [bold green]${args.balance:.2f}[/bold green]"
    )


def cmd_cities() -> None:
    table = Table(title="Supported Cities")
    table.add_column("Key", style="cyan")
    table.add_column("City", style="bold")
    table.add_column("Station")
    table.add_column("ICAO", style="yellow")
    table.add_column("Unit")
    table.add_column("Lat", justify="right")
    table.add_column("Lon", justify="right")

    for key, info in CITIES.items():
        table.add_row(
            key,
            info["name"],
            info["station"],
            info["icao"],
            info["unit"],
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
        console.print("  [cyan]scan[/cyan]        Scan & trade (paper trading by default)")
        console.print("  [cyan]weather[/cyan]     Check weather forecast for a city")
        console.print("  [cyan]markets[/cyan]     List active weather markets")
        console.print("  [cyan]portfolio[/cyan]   Show paper trading portfolio & P&L")
        console.print("  [cyan]resolve[/cyan]     Resolve a market (mark actual winner)")
        console.print("  [cyan]reset[/cyan]       Reset paper trading portfolio")
        console.print("  [cyan]cities[/cyan]      List supported cities")
        console.print("\nRun [cyan]pogoda <command> --help[/cyan] for details.")
        console.print("\n[dim]Paper trading is the default mode — no API keys needed.[/dim]")
        console.print("[dim]Use --live for real orders (requires Polymarket API keys).[/dim]")
        sys.exit(0)

    # Sync commands
    if args.command == "cities":
        cmd_cities()
        return
    if args.command == "portfolio":
        cmd_portfolio()
        return
    if args.command == "resolve":
        cmd_resolve(args)
        return
    if args.command == "reset":
        cmd_reset(args)
        return

    # Async commands
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
