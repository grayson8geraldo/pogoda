"""Paper trading simulator — virtual balance with real market data.

Tracks a virtual USDC balance, simulates order fills, and records P&L
without ever touching real Polymarket orders.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .strategy import PlannedOrder, TradeDecision

logger = logging.getLogger(__name__)

DEFAULT_DATA_DIR = Path.home() / ".pogoda"
PORTFOLIO_FILE = "portfolio.json"


@dataclass
class VirtualPosition:
    """A single virtual position (shares held in one bin)."""

    token_id: str
    outcome: str
    temp_value: int | None
    shares: int
    entry_price_cents: float  # Price per share at entry
    order_type: str  # "core" or "lottery"
    market_question: str = ""
    opened_at: str = ""

    @property
    def cost_cents(self) -> float:
        return self.entry_price_cents * self.shares

    def pnl_if_win(self) -> float:
        """Profit in cents if this bin resolves YES."""
        return (100.0 - self.entry_price_cents) * self.shares

    def pnl_if_lose(self) -> float:
        """Loss in cents if this bin resolves NO."""
        return -self.cost_cents

    def to_dict(self) -> dict:
        return {
            "token_id": self.token_id,
            "outcome": self.outcome,
            "temp_value": self.temp_value,
            "shares": self.shares,
            "entry_price_cents": self.entry_price_cents,
            "order_type": self.order_type,
            "market_question": self.market_question,
            "opened_at": self.opened_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> VirtualPosition:
        return cls(**d)


@dataclass
class TradeRecord:
    """Record of a completed (resolved) trade."""

    market_question: str
    outcome: str
    temp_value: int | None
    shares: int
    entry_price_cents: float
    resolved: bool  # True = this bin won
    pnl_cents: float
    resolved_at: str = ""

    def to_dict(self) -> dict:
        return {
            "market_question": self.market_question,
            "outcome": self.outcome,
            "temp_value": self.temp_value,
            "shares": self.shares,
            "entry_price_cents": self.entry_price_cents,
            "resolved": self.resolved,
            "pnl_cents": self.pnl_cents,
            "resolved_at": self.resolved_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> TradeRecord:
        return cls(**d)


class PaperTrader:
    """Virtual trading engine with persistent state.

    Simulates order execution against real market prices,
    tracks positions and P&L in a local JSON file.
    """

    def __init__(self, initial_balance_usd: float = 200.0, data_dir: Path | None = None):
        self.data_dir = data_dir or DEFAULT_DATA_DIR
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.portfolio_path = self.data_dir / PORTFOLIO_FILE

        # State
        self.balance_cents: float = initial_balance_usd * 100.0
        self.initial_balance_cents: float = initial_balance_usd * 100.0
        self.positions: list[VirtualPosition] = []
        self.history: list[TradeRecord] = []

        # Load existing state if available
        self._load()

    # --- Persistence ---

    def _load(self) -> None:
        if not self.portfolio_path.exists():
            logger.info("No existing portfolio found — starting fresh with $%.2f",
                        self.balance_cents / 100)
            return

        try:
            data = json.loads(self.portfolio_path.read_text())
            self.balance_cents = data.get("balance_cents", self.balance_cents)
            self.initial_balance_cents = data.get("initial_balance_cents", self.initial_balance_cents)
            self.positions = [VirtualPosition.from_dict(p) for p in data.get("positions", [])]
            self.history = [TradeRecord.from_dict(t) for t in data.get("history", [])]
            logger.info(
                "Loaded portfolio: balance=$%.2f, %d open positions, %d historical trades",
                self.balance_cents / 100,
                len(self.positions),
                len(self.history),
            )
        except Exception:
            logger.exception("Failed to load portfolio — starting fresh")

    def save(self) -> None:
        data = {
            "balance_cents": self.balance_cents,
            "initial_balance_cents": self.initial_balance_cents,
            "positions": [p.to_dict() for p in self.positions],
            "history": [t.to_dict() for t in self.history],
            "last_updated": datetime.now().isoformat(),
        }
        self.portfolio_path.write_text(json.dumps(data, indent=2))

    # --- Trading ---

    def has_position_for_market(self, market_question: str) -> bool:
        """Check if we already have open positions for this market."""
        return any(p.market_question == market_question for p in self.positions)

    def execute_decision(self, decision: TradeDecision) -> list[VirtualPosition]:
        """Simulate executing all orders in a trade decision.

        Orders "fill" instantly at the limit price (best-case simulation).
        Skips if we already have positions for this market (prevents duplicates).
        Returns list of new positions opened.
        """
        if not decision.checks_passed:
            logger.warning("[PAPER] Decision did not pass checks — skipping")
            return []

        # Prevent duplicate positions on the same market
        if self.has_position_for_market(decision.market.question):
            logger.info(
                "[PAPER] Already have positions for '%s' — skipping",
                decision.market.question,
            )
            return []

        new_positions = []
        now = datetime.now().isoformat()

        for order in decision.orders:
            cost = order.cost_cents

            if cost > self.balance_cents:
                logger.warning(
                    "[PAPER] Insufficient balance for %s: need %.1f¢, have %.1f¢",
                    order.outcome, cost, self.balance_cents,
                )
                continue

            # Deduct cost from balance
            self.balance_cents -= cost

            pos = VirtualPosition(
                token_id=order.token_id,
                outcome=order.outcome,
                temp_value=order.temp_value,
                shares=order.size,
                entry_price_cents=order.price,
                order_type=order.order_type,
                market_question=decision.market.question,
                opened_at=now,
            )
            self.positions.append(pos)
            new_positions.append(pos)

            logger.info(
                "[PAPER] Bought %d shares of %s @ %.1f¢ = %.1f¢ (balance: $%.2f)",
                order.size,
                order.outcome,
                order.price,
                cost,
                self.balance_cents / 100,
            )

        self.save()
        return new_positions

    def resolve_market(self, market_question: str, winning_temp: int | None) -> list[TradeRecord]:
        """Resolve a market — mark positions as won/lost and update balance.

        Args:
            market_question: The market question string to match.
            winning_temp: The actual temperature that won, or None to lose all.
        """
        records = []
        remaining_positions = []
        now = datetime.now().isoformat()

        for pos in self.positions:
            if pos.market_question != market_question:
                remaining_positions.append(pos)
                continue

            won = pos.temp_value == winning_temp
            if won:
                pnl = pos.pnl_if_win()
                self.balance_cents += 100.0 * pos.shares  # Payout
            else:
                pnl = pos.pnl_if_lose()

            record = TradeRecord(
                market_question=pos.market_question,
                outcome=pos.outcome,
                temp_value=pos.temp_value,
                shares=pos.shares,
                entry_price_cents=pos.entry_price_cents,
                resolved=won,
                pnl_cents=pnl,
                resolved_at=now,
            )
            records.append(record)
            self.history.append(record)

            status = "WON" if won else "LOST"
            logger.info(
                "[PAPER] %s: %s — P&L: %+.1f¢",
                status, pos.outcome, pnl,
            )

        self.positions = remaining_positions
        self.save()
        return records

    # --- Reporting ---

    @property
    def balance_usd(self) -> float:
        return self.balance_cents / 100.0

    @property
    def total_invested_cents(self) -> float:
        return sum(p.cost_cents for p in self.positions)

    @property
    def total_invested_usd(self) -> float:
        return self.total_invested_cents / 100.0

    @property
    def equity_usd(self) -> float:
        """Current equity = cash + invested capital (at cost)."""
        return (self.balance_cents + self.total_invested_cents) / 100.0

    @property
    def total_pnl_cents(self) -> float:
        return sum(t.pnl_cents for t in self.history)

    @property
    def total_pnl_usd(self) -> float:
        return self.total_pnl_cents / 100.0

    @property
    def win_rate(self) -> float:
        if not self.history:
            return 0.0
        wins = sum(1 for t in self.history if t.resolved)
        return wins / len(self.history) * 100

    @property
    def roi_percent(self) -> float:
        if self.initial_balance_cents == 0:
            return 0.0
        current_total = self.balance_cents + self.total_invested_cents
        return (current_total - self.initial_balance_cents) / self.initial_balance_cents * 100

    def summary(self) -> str:
        lines = [
            f"{'═' * 50}",
            f"  PAPER TRADING PORTFOLIO",
            f"{'═' * 50}",
            f"  Cash balance:     ${self.balance_usd:.2f}",
            f"  Invested:         ${self.total_invested_usd:.2f}",
            f"  Equity:           ${self.equity_usd:.2f}",
            f"  Initial balance:  ${self.initial_balance_cents / 100:.2f}",
            f"  ROI:              {self.roi_percent:+.1f}%",
            f"{'─' * 50}",
            f"  Open positions:   {len(self.positions)}",
            f"  Completed trades: {len(self.history)}",
        ]
        if self.history:
            lines.extend([
                f"  Realized P&L:     ${self.total_pnl_usd:+.2f}",
                f"  Win rate:         {self.win_rate:.0f}%",
            ])
        lines.append(f"{'═' * 50}")

        if self.positions:
            lines.append("\n  OPEN POSITIONS:")
            for p in self.positions:
                lines.append(
                    f"    {p.outcome}: {p.shares} shares @ {p.entry_price_cents:.1f}¢ "
                    f"(cost: ${p.cost_cents / 100:.2f}) [{p.order_type}]"
                )
                lines.append(f"      Market: {p.market_question}")

        return "\n".join(lines)

    def get_open_market_dates(self) -> dict[str, str]:
        """Get unique market questions and their dates from open positions.

        Returns dict of {market_question: date_str} for markets that may need resolution.
        """
        import re
        markets: dict[str, str] = {}
        months = {
            "january": "01", "february": "02", "march": "03", "april": "04",
            "may": "05", "june": "06", "july": "07", "august": "08",
            "september": "09", "october": "10", "november": "11", "december": "12",
        }
        for p in self.positions:
            if p.market_question in markets:
                continue
            m = re.search(r"on\s+(\w+)\s+(\d{1,2})", p.market_question, re.I)
            if m:
                month_name = m.group(1).lower()
                day = m.group(2).zfill(2)
                month = months.get(month_name, "")
                if month:
                    year = datetime.now().year
                    markets[p.market_question] = f"{year}-{month}-{day}"
        return markets

    async def auto_resolve_expired(
        self,
        city_coords: dict[str, tuple[float, float]],
    ) -> list[TradeRecord]:
        """Automatically resolve markets whose date has passed.

        Fetches actual observed temperature from Open-Meteo historical API
        and resolves positions accordingly.

        Args:
            city_coords: Mapping of city name → (lat, lon) for fetching actuals.
        """
        import re
        from datetime import date

        import httpx

        today = date.today()
        market_dates = self.get_open_market_dates()
        all_records: list[TradeRecord] = []

        for market_q, date_str in market_dates.items():
            market_date = date.fromisoformat(date_str)
            if market_date >= today:
                continue  # Market hasn't expired yet

            # Find city from market question
            city_name = None
            for city in city_coords:
                if city.lower() in market_q.lower():
                    city_name = city
                    break
            # Also check abbreviations
            if city_name is None and "nyc" in market_q.lower():
                city_name = "New York"

            if city_name is None or city_name not in city_coords:
                logger.warning("[PAPER] Cannot find city for '%s' — skip auto-resolve", market_q)
                continue

            lat, lon = city_coords[city_name]

            # Fetch actual observed max temperature
            try:
                async with httpx.AsyncClient() as client:
                    # Determine unit from market question
                    is_fahrenheit = "°f" in market_q.lower() or "f " in market_q.lower()

                    params = {
                        "latitude": lat,
                        "longitude": lon,
                        "daily": "temperature_2m_max",
                        "timezone": "auto",
                        "start_date": date_str,
                        "end_date": date_str,
                    }
                    if is_fahrenheit:
                        params["temperature_unit"] = "fahrenheit"

                    resp = await client.get(
                        "https://api.open-meteo.com/v1/forecast",
                        params=params,
                        timeout=15.0,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    max_temps = data.get("daily", {}).get("temperature_2m_max", [])

                    if not max_temps:
                        logger.warning("[PAPER] No actual temp data for %s on %s", city_name, date_str)
                        continue

                    actual_temp = round(max_temps[0])
                    logger.info(
                        "[PAPER] Actual temp for %s on %s: %d%s",
                        city_name, date_str, actual_temp,
                        "°F" if is_fahrenheit else "°C",
                    )

                    # Resolve the market
                    records = self.resolve_market(market_q, actual_temp)
                    all_records.extend(records)

            except Exception:
                logger.exception("[PAPER] Failed to fetch actual temp for %s", market_q)

        return all_records

    def reset(self, initial_balance_usd: float = 200.0) -> None:
        """Reset portfolio to initial state."""
        self.balance_cents = initial_balance_usd * 100.0
        self.initial_balance_cents = initial_balance_usd * 100.0
        self.positions = []
        self.history = []
        self.save()
        logger.info("[PAPER] Portfolio reset to $%.2f", initial_balance_usd)
