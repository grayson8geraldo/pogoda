"""Betting strategy — position sizing, entry checklist, and order generation."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .config import Settings
from .consensus import ConsensusResult
from .polymarket import TemperatureBin, WeatherMarket
from .weather import WeatherSnapshot

logger = logging.getLogger(__name__)


@dataclass
class PlannedOrder:
    """A single limit order to be placed."""

    token_id: str
    outcome: str
    temp_value: int | None
    side: str  # "BUY"
    price: float  # limit price in cents (0-100)
    size: int  # number of shares
    order_type: str  # "core" (±1°C) or "lottery" (±2°C)

    @property
    def cost_cents(self) -> float:
        return self.price * self.size

    @property
    def potential_payout_cents(self) -> float:
        return 100.0 * self.size

    @property
    def potential_profit_cents(self) -> float:
        return self.potential_payout_cents - self.cost_cents


@dataclass
class TradeDecision:
    """Complete trade decision with all planned orders and risk checks."""

    market: WeatherMarket
    consensus: ConsensusResult
    orders: list[PlannedOrder] = field(default_factory=list)
    total_cost_cents: float = 0.0
    best_case_profit_pct: float = 0.0
    worst_case_loss_cents: float = 0.0
    checks_passed: bool = False
    rejection_reasons: list[str] = field(default_factory=list)
    weather_stable: bool = True

    def summary(self) -> str:
        lines = [
            f"Market: {self.market.question}",
            f"Consensus: {self.consensus.consensus_temp_c}°C "
            f"(confidence={self.consensus.confidence}, method={self.consensus.method})",
            f"Models: {self.consensus.model_temps}",
            f"Orders: {len(self.orders)} | Total cost: {self.total_cost_cents:.1f}¢",
            f"Best-case profit: {self.best_case_profit_pct:.1f}%",
            f"Checks passed: {self.checks_passed}",
        ]
        if self.rejection_reasons:
            lines.append(f"Rejections: {', '.join(self.rejection_reasons)}")
        return "\n".join(lines)


def check_weather_stability(
    snapshot: WeatherSnapshot,
    consensus_temp: int,
    threshold: float,
) -> bool:
    """Check if weather is stable (today's actual vs tomorrow's forecast are similar)."""
    if snapshot.today_actual_max is None:
        # Can't check stability without today's data — assume stable
        return True
    diff = abs(snapshot.today_actual_max - consensus_temp)
    stable = diff <= threshold
    if not stable:
        logger.info(
            "Weather unstable: today=%.1f°C, forecast=%d°C, diff=%.1f°C (threshold=%.1f)",
            snapshot.today_actual_max,
            consensus_temp,
            diff,
            threshold,
        )
    return stable


def generate_orders(
    market: WeatherMarket,
    consensus: ConsensusResult,
    snapshot: WeatherSnapshot,
    settings: Settings,
) -> TradeDecision:
    """Generate a complete trade decision with limit orders and risk checks.

    Strategy:
    - Core bins (±1°C from consensus): equal shares at limit price
    - Lottery bins (±2°C from consensus): equal shares at current price (cheap)
    - All orders are LIMIT (bid slightly below current ask)
    """
    decision = TradeDecision(market=market, consensus=consensus)

    # --- Pre-checks ---
    if not consensus.is_tradeable:
        decision.rejection_reasons.append(
            f"Low confidence consensus ({consensus.method})"
        )
        return decision

    center = consensus.consensus_temp_c

    # Check weather stability
    decision.weather_stable = check_weather_stability(
        snapshot, center, settings.stability_threshold_c
    )
    if not decision.weather_stable:
        decision.rejection_reasons.append("Weather unstable (large day-to-day variation)")

    # --- Collect bins ---
    core_bins = market.get_bins_range(center, radius=1)  # ±1°C
    lottery_bins: list[TemperatureBin] = []
    if settings.enable_lottery_bins:
        for offset in [-2, 2]:
            b = market.get_bin(center + offset)
            if b and b not in core_bins:
                lottery_bins.append(b)

    if not core_bins:
        decision.rejection_reasons.append("No matching bins found in market")
        return decision

    if len(core_bins) < 2:
        decision.rejection_reasons.append(
            f"Only {len(core_bins)} core bin(s) found — need at least 2 for hedging"
        )

    # --- Check total cost < max_total_cost_cents ---
    core_prices_sum = sum(b.price for b in core_bins)
    lottery_prices_sum = sum(b.price for b in lottery_bins)

    if core_prices_sum >= settings.max_total_cost_cents:
        decision.rejection_reasons.append(
            f"Core bins total price ({core_prices_sum:.1f}¢) ≥ {settings.max_total_cost_cents}¢ — "
            "guaranteed loss"
        )
        return decision

    # --- Generate limit orders ---
    shares = settings.shares_per_bin
    offset = settings.limit_price_offset_cents

    for b in core_bins:
        limit_price = max(0.5, b.price - offset)  # Bid slightly below current price
        decision.orders.append(PlannedOrder(
            token_id=b.token_id,
            outcome=b.outcome,
            temp_value=b.temp_value,
            side="BUY",
            price=round(limit_price, 2),
            size=shares,
            order_type="core",
        ))

    for b in lottery_bins:
        # Lottery bins: buy at current price (usually ~1¢)
        limit_price = max(0.5, b.price)
        decision.orders.append(PlannedOrder(
            token_id=b.token_id,
            outcome=b.outcome,
            temp_value=b.temp_value,
            side="BUY",
            price=round(limit_price, 2),
            size=shares,
            order_type="lottery",
        ))

    # --- Calculate totals ---
    decision.total_cost_cents = sum(o.cost_cents for o in decision.orders)

    # Best case: center bin wins → payout = 100¢ * shares, minus cost of all orders
    center_bin_order = next(
        (o for o in decision.orders if o.temp_value == center), None
    )
    if center_bin_order:
        payout = center_bin_order.potential_payout_cents
        profit = payout - decision.total_cost_cents
        decision.best_case_profit_pct = (profit / decision.total_cost_cents * 100) if decision.total_cost_cents > 0 else 0

    # Worst case: none of our bins win → we lose total cost
    decision.worst_case_loss_cents = decision.total_cost_cents

    # --- Final checks ---
    # Check: total cost < max allowed
    if decision.total_cost_cents >= settings.max_total_cost_cents * shares:
        decision.rejection_reasons.append(
            f"Total cost ({decision.total_cost_cents:.1f}¢) too high"
        )

    # Check: expected profit ≥ min_profit_percent
    if decision.best_case_profit_pct < settings.min_profit_percent:
        decision.rejection_reasons.append(
            f"Expected profit ({decision.best_case_profit_pct:.1f}%) < "
            f"minimum ({settings.min_profit_percent}%)"
        )

    # Check: max position size
    total_cost_usd = decision.total_cost_cents / 100.0
    if total_cost_usd > settings.max_position_size_usd:
        decision.rejection_reasons.append(
            f"Position size (${total_cost_usd:.2f}) > max (${settings.max_position_size_usd:.2f})"
        )

    decision.checks_passed = len(decision.rejection_reasons) == 0
    return decision
