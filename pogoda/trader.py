"""Order execution via Polymarket CLOB API."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .config import Settings
from .strategy import PlannedOrder, TradeDecision

logger = logging.getLogger(__name__)


@dataclass
class OrderResult:
    """Result of a single order placement."""

    order: PlannedOrder
    success: bool
    order_id: str = ""
    error: str = ""


@dataclass
class ExecutionReport:
    """Summary of all order executions for a trade decision."""

    decision: TradeDecision
    results: list[OrderResult] = field(default_factory=list)

    @property
    def successful(self) -> list[OrderResult]:
        return [r for r in self.results if r.success]

    @property
    def failed(self) -> list[OrderResult]:
        return [r for r in self.results if not r.success]

    def summary(self) -> str:
        lines = [
            f"Execution: {len(self.successful)}/{len(self.results)} orders placed",
        ]
        for r in self.results:
            status = "OK" if r.success else f"FAIL: {r.error}"
            lines.append(
                f"  {r.order.outcome} @ {r.order.price:.1f}¢ x{r.order.size} "
                f"[{r.order.order_type}] → {status}"
            )
        return "\n".join(lines)


class Trader:
    """Handles order execution via the Polymarket CLOB API.

    Uses py-clob-client for authenticated order placement.
    All orders are placed as LIMIT orders (GTC — Good Till Cancelled).
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = None

    def _get_client(self):
        """Lazy-initialize the py-clob-client."""
        if self._client is not None:
            return self._client

        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds

            creds = ApiCreds(
                api_key=self.settings.polymarket_api_key,
                api_secret=self.settings.polymarket_api_secret,
                api_passphrase=self.settings.polymarket_api_passphrase,
            )
            self._client = ClobClient(
                host=self.settings.polymarket_host,
                key=self.settings.private_key,
                chain_id=self.settings.chain_id,
                creds=creds,
            )
            return self._client
        except ImportError:
            logger.error(
                "py-clob-client not installed. Install with: pip install py-clob-client"
            )
            raise
        except Exception:
            logger.exception("Failed to initialize CLOB client")
            raise

    async def execute(self, decision: TradeDecision, dry_run: bool = True) -> ExecutionReport:
        """Execute all orders in a trade decision.

        Args:
            decision: The trade decision with planned orders.
            dry_run: If True, only simulate — don't place real orders.
        """
        report = ExecutionReport(decision=decision)

        if not decision.checks_passed:
            logger.warning("Trade decision did not pass checks — skipping execution")
            return report

        for order in decision.orders:
            result = await self._place_order(order, dry_run=dry_run)
            report.results.append(result)

        return report

    async def _place_order(
        self, order: PlannedOrder, dry_run: bool = True
    ) -> OrderResult:
        """Place a single limit order."""
        if dry_run:
            logger.info(
                "[DRY RUN] Would place: BUY %s @ %.1f¢ x%d shares [%s]",
                order.outcome,
                order.price,
                order.size,
                order.order_type,
            )
            return OrderResult(order=order, success=True, order_id="dry-run")

        try:
            client = self._get_client()

            from py_clob_client.order_builder.constants import BUY

            # Build and sign the order
            signed_order = client.create_and_sign_order(
                {
                    "token_id": order.token_id,
                    "price": order.price / 100.0,  # Convert cents to decimal
                    "size": order.size,
                    "side": BUY,
                }
            )

            # Post the order
            resp = client.post_order(signed_order)
            order_id = resp.get("orderID", resp.get("id", "unknown"))

            logger.info(
                "Order placed: BUY %s @ %.1f¢ x%d → ID=%s",
                order.outcome,
                order.price,
                order.size,
                order_id,
            )
            return OrderResult(order=order, success=True, order_id=str(order_id))

        except Exception as e:
            logger.exception("Failed to place order for %s", order.outcome)
            return OrderResult(order=order, success=False, error=str(e))

    async def cancel_all_orders(self) -> bool:
        """Cancel all open orders."""
        try:
            client = self._get_client()
            client.cancel_all()
            logger.info("All open orders cancelled")
            return True
        except Exception:
            logger.exception("Failed to cancel orders")
            return False

    async def get_open_orders(self) -> list[dict]:
        """Get all currently open orders."""
        try:
            client = self._get_client()
            orders = client.get_orders()
            return orders if isinstance(orders, list) else []
        except Exception:
            logger.exception("Failed to get open orders")
            return []
