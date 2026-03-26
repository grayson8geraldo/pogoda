"""Betting strategy — position sizing, entry checklist, and order generation."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .config import Settings
from .consensus import ConsensusResult
from .ensemble import EnsembleAnalysis
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
    order_type: str  # "core" (±1 bin) or "lottery" (±2 bins)
    ensemble_prob: float = 0.0  # Model-estimated probability (0-1)

    @property
    def cost_cents(self) -> float:
        return self.price * self.size

    @property
    def potential_payout_cents(self) -> float:
        return 100.0 * self.size

    @property
    def potential_profit_cents(self) -> float:
        return self.potential_payout_cents - self.cost_cents

    @property
    def expected_value_cents(self) -> float:
        """EV = P(win) * payout - cost. Positive = +EV bet."""
        return self.ensemble_prob * self.potential_payout_cents - self.cost_cents


@dataclass
class TradeDecision:
    """Complete trade decision with all planned orders and risk checks."""

    market: WeatherMarket
    consensus: ConsensusResult
    orders: list[PlannedOrder] = field(default_factory=list)
    total_cost_cents: float = 0.0
    best_case_profit_pct: float = 0.0
    worst_case_loss_cents: float = 0.0
    total_ev_cents: float = 0.0  # Sum of EV across all orders
    checks_passed: bool = False
    rejection_reasons: list[str] = field(default_factory=list)
    weather_stable: bool = True

    def summary(self) -> str:
        unit = "°F" if self.market.temp_unit == "F" else "°C"
        lines = [
            f"Market: {self.market.question}",
            f"Consensus: {self.consensus.consensus_temp_c}{unit} "
            f"(confidence={self.consensus.confidence}, method={self.consensus.method})",
            f"Models: {self.consensus.model_temps}",
            f"Orders: {len(self.orders)} | Total cost: {self.total_cost_cents:.1f}¢",
            f"Best-case profit: {self.best_case_profit_pct:.1f}%",
        ]
        if self.total_ev_cents != 0:
            lines.append(f"Expected value: {self.total_ev_cents:+.1f}¢")
        lines.append(f"Checks passed: {self.checks_passed}")
        if self.rejection_reasons:
            lines.append(f"Rejections: {', '.join(self.rejection_reasons)}")
        for o in self.orders:
            ev_str = f" EV={o.expected_value_cents:+.1f}¢" if o.ensemble_prob > 0 else ""
            prob_str = f" P={o.ensemble_prob:.1%}" if o.ensemble_prob > 0 else ""
            lines.append(
                f"  {o.order_type:>7} {o.outcome}: {o.price:.1f}¢ x{o.size}"
                f"{prob_str}{ev_str}"
            )
        return "\n".join(lines)


def _detect_bin_step(market: WeatherMarket) -> int:
    """Detect the step size between temperature bins (1 for °C, 2 for °F typically)."""
    sorted_bins = market.sorted_bins
    if len(sorted_bins) < 2:
        return 2 if market.temp_unit == "F" else 1
    # Look at the most common gap between consecutive bins
    gaps = []
    for i in range(1, len(sorted_bins)):
        if sorted_bins[i].temp_value is not None and sorted_bins[i - 1].temp_value is not None:
            gaps.append(sorted_bins[i].temp_value - sorted_bins[i - 1].temp_value)
    if gaps:
        from collections import Counter
        most_common_gap = Counter(gaps).most_common(1)[0][0]
        return max(1, most_common_gap)
    return 2 if market.temp_unit == "F" else 1


def check_weather_stability(
    snapshot: WeatherSnapshot,
    consensus_temp: int,
    threshold: float,
) -> bool:
    """Check if weather is stable (today's actual vs tomorrow's forecast are similar)."""
    if snapshot.today_actual_max is None:
        return True
    diff = abs(snapshot.today_actual_max - consensus_temp)
    stable = diff <= threshold
    if not stable:
        logger.info(
            "Weather unstable: today=%.1f°C, forecast=%d, diff=%.1f (threshold=%.1f)",
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
    ensemble: EnsembleAnalysis | None = None,
) -> TradeDecision:
    """Generate a complete trade decision with limit orders and risk checks.

    Strategy:
    - Detect bin step (1°C or 2°F) from market data
    - Core bins (±1 step from consensus): equal shares at limit price
    - Lottery bins (±2 steps from consensus): equal shares at current price
    - All orders are LIMIT (bid slightly below current ask)
    - If ensemble data available, compute EV per bin
    """
    decision = TradeDecision(market=market, consensus=consensus)

    # --- Pre-checks ---
    if not consensus.is_tradeable:
        decision.rejection_reasons.append(
            f"Low confidence consensus ({consensus.method})"
        )
        return decision

    center = consensus.consensus_temp_c
    bin_step = _detect_bin_step(market)

    logger.info("Bin step detected: %d (unit=%s)", bin_step, market.temp_unit)

    # Check weather stability
    decision.weather_stable = check_weather_stability(
        snapshot, center, settings.stability_threshold_c
    )
    if not decision.weather_stable:
        decision.rejection_reasons.append("Weather unstable (large day-to-day variation)")

    # --- Collect bins ---
    # Core: consensus bin + adjacent bins (±1 step)
    core_bins: list[TemperatureBin] = []
    for offset in range(-bin_step, bin_step + 1, bin_step):
        b = market.get_bin(center + offset)
        if b:
            core_bins.append(b)

    # Lottery: ±2 steps away
    lottery_bins: list[TemperatureBin] = []
    if settings.enable_lottery_bins:
        for offset in [-2 * bin_step, 2 * bin_step]:
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
        limit_price = max(0.5, b.price - offset)
        prob = ensemble.get_probability(b.temp_value) if ensemble and b.temp_value is not None else 0.0
        decision.orders.append(PlannedOrder(
            token_id=b.token_id,
            outcome=b.outcome,
            temp_value=b.temp_value,
            side="BUY",
            price=round(limit_price, 2),
            size=shares,
            order_type="core",
            ensemble_prob=prob,
        ))

    for b in lottery_bins:
        limit_price = max(0.5, b.price)
        prob = ensemble.get_probability(b.temp_value) if ensemble and b.temp_value is not None else 0.0
        decision.orders.append(PlannedOrder(
            token_id=b.token_id,
            outcome=b.outcome,
            temp_value=b.temp_value,
            side="BUY",
            price=round(limit_price, 2),
            size=shares,
            order_type="lottery",
            ensemble_prob=prob,
        ))

    # --- Calculate totals ---
    decision.total_cost_cents = sum(o.cost_cents for o in decision.orders)
    decision.total_ev_cents = sum(o.expected_value_cents for o in decision.orders)

    # Best case: center bin wins → payout = 100¢ * shares, minus cost of all orders
    center_bin_order = next(
        (o for o in decision.orders if o.temp_value == center), None
    )
    if center_bin_order:
        payout = center_bin_order.potential_payout_cents
        profit = payout - decision.total_cost_cents
        decision.best_case_profit_pct = (
            (profit / decision.total_cost_cents * 100)
            if decision.total_cost_cents > 0 else 0
        )

    # Worst case: none of our bins win → we lose total cost
    decision.worst_case_loss_cents = decision.total_cost_cents

    # --- Final checks ---
    if decision.total_cost_cents >= settings.max_total_cost_cents * shares:
        decision.rejection_reasons.append(
            f"Total cost ({decision.total_cost_cents:.1f}¢) too high"
        )

    if decision.best_case_profit_pct < settings.min_profit_percent:
        decision.rejection_reasons.append(
            f"Expected profit ({decision.best_case_profit_pct:.1f}%) < "
            f"minimum ({settings.min_profit_percent}%)"
        )

    total_cost_usd = decision.total_cost_cents / 100.0
    if total_cost_usd > settings.max_position_size_usd:
        decision.rejection_reasons.append(
            f"Position size (${total_cost_usd:.2f}) > max (${settings.max_position_size_usd:.2f})"
        )

    # EV check: reject if ensemble says negative EV
    if ensemble and ensemble.total_members > 0 and decision.total_ev_cents < 0:
        decision.rejection_reasons.append(
            f"Negative expected value ({decision.total_ev_cents:+.1f}¢) per ensemble models"
        )

    decision.checks_passed = len(decision.rejection_reasons) == 0
    return decision
