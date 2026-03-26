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

    def execute_decision(self, decision: TradeDecision) -> list[VirtualPosition]:
        """Simulate executing all orders in a trade decision.

        Orders "fill" instantly at the limit price (best-case simulation).
        Returns list of new positions opened.
        """
        if not decision.checks_passed:
            logger.warning("[PAPER] Decision did not pass checks — skipping")
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

    def reset(self, initial_balance_usd: float = 200.0) -> None:
        """Reset portfolio to initial state."""
        self.balance_cents = initial_balance_usd * 100.0
        self.initial_balance_cents = initial_balance_usd * 100.0
        self.positions = []
        self.history = []
        self.save()
        logger.info("[PAPER] Portfolio reset to $%.2f", initial_balance_usd)
