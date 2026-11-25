"""Authenticated Polymarket US REST orders for the WTI 15-minute worker."""
from __future__ import annotations

from base64 import b64decode, b64encode
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import math
import re
import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import httpx

from ..trading.venue import BASE as MARKET_BASE
from ..trading.request_budget import RequestBudget
from ..trading.venue import PublicRequestDeadline, acquire_public_slot, record_public_throttle
from .live_store import TERMINAL_STATES

TRADING_BASE = "https://api.polymarket.us"
WTI_SLUG = re.compile(r"cpwtif-wti-updown-15m-(\d{4}-\d{2}-\d{2}-\d{4}z)")
ORDER_ID = re.compile(r"[A-Za-z0-9_-]{1,200}")
CANCEL_PATH = re.compile(r"/v1/order/[A-Za-z0-9_-]{1,200}/cancel")
PRICE_MIN = Decimal("0.01")
PRICE_MAX = Decimal("0.99")
ZERO = Decimal(0)
ONE = Decimal(1)

INTENT = {
    ("UP", "buy"): "ORDER_INTENT_BUY_LONG",
    ("UP", "sell"): "ORDER_INTENT_SELL_LONG",
    ("DOWN", "buy"): "ORDER_INTENT_BUY_SHORT",
    ("DOWN", "sell"): "ORDER_INTENT_SELL_SHORT",
}
ORDER_INTENTS = {value: key for key, value in INTENT.items()}
OPEN_STATES = frozenset(
    "ORDER_STATE_" + state
    for state in ("NEW", "PARTIALLY_FILLED", "PENDING_NEW", "PENDING_REPLACE", "PENDING_CANCEL", "PENDING_RISK")
)


class VenueError(RuntimeError):
    """Order state was unavailable or a request failed local validation."""


class PublicMarketUnavailable(VenueError):
    """A transient failure fetching public market rules."""


class AmbiguousOrder(VenueError):
    """A write might have reached the venue. Reconcile before sending another intent."""

    def __init__(self, message: str, order_id: str | None = None):
        super().__init__(message)
        self.order_id = order_id


def _amount(value) -> Decimal:
    if isinstance(value, dict):
        if value.get("currency", "USD") != "USD":
            raise VenueError("Unexpected venue currency")
        value = value.get("value")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise VenueError("Invalid venue amount") from None
    if not result.is_finite():
        raise VenueError("Non-finite venue amount")
    return result


def _timestamp(value: str) -> float:
    try:
        date = datetime.fromisoformat(value)
        if date.tzinfo is None:
            raise ValueError
        return date.timestamp()
    except (TypeError, ValueError, OverflowError):
        raise VenueError("Invalid venue timestamp") from None


def _window_end(slug: str) -> float:
    slug_match = WTI_SLUG.fullmatch(slug)
    if not slug_match:
        raise VenueError("Live orders are limited to WTI 15-minute markets")
    try:
        start = datetime.strptime(slug_match[1], "%Y-%m-%d-%H%Mz").replace(tzinfo=timezone.utc)
    except ValueError:
        raise VenueError("Invalid WTI market window") from None
    if start.minute % 15 != 0:
        raise VenueError("Invalid WTI market window")
    return start.timestamp() + 900


def _wire_price(side: str, price: Decimal) -> Decimal:
    # Retail REST quotes orders in YES price units even for a NO position.
    return ONE - price if side == "DOWN" else price


@dataclass(frozen=True)
class MarketRules:
    slug: str
    min_quantity: Decimal
    tick_size: Decimal
    status: str
    closes_at: float

    @property
    def is_open(self) -> bool:
        return self.status == "MARKET_STATUS_OPEN"


@dataclass(frozen=True)
class LiveExecution:
    id: str
    at: float | None
    quantity: Decimal
    price: Decimal
    fee: Decimal | None


@dataclass(frozen=True)
class LiveOrder:
    id: str
    slug: str
    side: str
    action: str
    quantity: Decimal
    filled: Decimal
    remaining: Decimal
    price: Decimal
    average_price: Decimal | None
    state: str
    fees: Decimal | None = None
    executions: tuple[LiveExecution, ...] = ()

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES


def _parse_order(raw) -> LiveOrder:
    if not isinstance(raw, dict):
        raise VenueError("Order details are missing")
    order_id, slug, state = raw.get("id"), raw.get("marketSlug"), raw.get("state")
    if (not isinstance(order_id, str) or not ORDER_ID.fullmatch(order_id) or not isinstance(slug, str)
            or not slug or not isinstance(state, str) or state not in TERMINAL_STATES | OPEN_STATES):
        raise VenueError("Invalid venue order identity or state")
    intent = raw.get("intent")
    if intent in ORDER_INTENTS:
        side, action = ORDER_INTENTS[intent]
    else:
        side = {"OUTCOME_SIDE_YES": "UP", "OUTCOME_SIDE_NO": "DOWN"}.get(raw.get("outcomeSide"))
        action = {"ORDER_ACTION_BUY": "buy", "ORDER_ACTION_SELL": "sell"}.get(raw.get("action"))
        if not side or not action:
            raise VenueError("Invalid venue order direction")
    quantity = _amount(raw.get("quantity"))
    filled = _amount(raw.get("cumQuantity"))
    remaining = _amount(raw["leavesQuantity"]) if "leavesQuantity" in raw else quantity - filled
    if quantity < ZERO or not ZERO <= filled <= quantity or not ZERO <= remaining <= quantity - filled:
        raise VenueError("Invalid cumulative venue order quantities")
    if state == "ORDER_STATE_FILLED" and filled != quantity:
        raise VenueError("Filled order quantity does not reconcile")
    price = _wire_price(side, _amount(raw.get("price")))
    average_field = raw.get("avgPx")
    average = _wire_price(side, _amount(average_field)) if filled > ZERO and average_field is not None else None
    if not ZERO <= price <= ONE or (average is not None and not ZERO < average < ONE):
        raise VenueError("Invalid venue order price")
    commission = raw.get("commissionNotionalTotalCollected")
    fees = _amount(commission) if commission is not None else None
    return LiveOrder(order_id, slug, side, action, quantity, filled, remaining, price, average, state, fees)


def _with_execution_totals(order: LiveOrder, executions: list) -> LiveOrder:
    fills = {}
    for execution in executions:
        if (execution.get("type") not in ("EXECUTION_TYPE_PARTIAL_FILL", "EXECUTION_TYPE_FILL")
                or (execution.get("order") or {}).get("id") != order.id):
            continue
        execution_id = execution.get("id")
        if not isinstance(execution_id, str) or not execution_id or execution_id in fills:
            continue
        shares = _amount(execution.get("lastShares"))
        price = _wire_price(order.side, _amount(execution.get("lastPx")))
        if shares <= ZERO or not ZERO < price < ONE:
            raise VenueError("Invalid venue execution price or quantity")
        timestamp = execution.get("transactTime")
        commission = execution.get("commissionNotionalCollected")
        fills[execution_id] = LiveExecution(
            execution_id,
            _timestamp(timestamp) if timestamp is not None else None,
            shares,
            price,
            _amount(commission) if commission is not None else None,
        )
    fills = tuple(fills.values())
    quantity = sum((fill.quantity for fill in fills), ZERO)
    if quantity > order.filled:
        raise VenueError("Execution quantities exceed the cumulative order fill")
    average, fees = order.average_price, order.fees
    if quantity and quantity == order.filled:
        if average is None:
            average = sum((fill.quantity * fill.price for fill in fills), ZERO) / quantity
        if fees is None and all(fill.fee is not None for fill in fills):
            fees = sum((fill.fee for fill in fills), ZERO)
    return replace(order, average_price=average, fees=fees, executions=fills)


class LiveVenue:
    def __init__(self, client: httpx.AsyncClient, key_id: str, secret_key: str, clock=time.time):
        if not key_id or not key_id.isascii() or any(c.isspace() for c in key_id):
            raise VenueError("Invalid Polymarket US key ID")
        try:
            seed = b64decode(secret_key, validate=True)
            if len(seed) not in (32, 64):
                raise ValueError
            self._key = Ed25519PrivateKey.from_private_bytes(seed[:32])
        except (TypeError, ValueError):
            raise VenueError("Invalid Polymarket US signing key") from None
        self._client, self._key_id, self._clock = client, key_id, clock
        self._budget = RequestBudget.from_env()

    async def _request(self, method: str, path: str, *, public=False, params=None, payload=None, key=None) -> dict:
        if method == "POST":
            if public or (path != "/v1/orders" and not CANCEL_PATH.fullmatch(path)):
                raise VenueError("Unsupported live write route")
            slug = payload.get("marketSlug") if isinstance(payload, dict) else None
            if not isinstance(slug, str) or (path == "/v1/orders" and key is None):
                raise VenueError("Live write requires its WTI market and order identity")
            _window_end(slug)
        headers = {}
        error_type = AmbiguousOrder if method == "POST" else VenueError
        base = MARKET_BASE if public else TRADING_BASE
        if not public:
            stamp = str(int(self._clock() * 1000))
            signature = self._key.sign(f"{stamp}{method}{path}".encode())
            headers = {"X-PM-Access-Key": self._key_id, "X-PM-Timestamp": stamp,
                       "X-PM-Signature": b64encode(signature).decode("ascii")}
        if key is not None:
            headers["Idempotency-Key"] = key
        try:
            if public:
                await acquire_public_slot(self._budget)
            response = await self._client.request(method, base + path, headers=headers, params=params,
                                                  json=payload, follow_redirects=False, timeout=7)
            if public:
                await record_public_throttle(self._budget, response)
        except PublicRequestDeadline as exc:
            raise PublicMarketUnavailable(str(exc)) from None
        except httpx.RequestError:
            if public:
                raise PublicMarketUnavailable(f"Polymarket US {method} request failed") from None
            raise error_type(f"Polymarket US {method} request failed") from None
        if not response.is_success:
            if public and (response.status_code == 429 or response.status_code >= 500):
                error_type = PublicMarketUnavailable
            raise error_type(f"Polymarket US {method} returned HTTP {response.status_code}")
        if not response.content and method == "POST" and path.endswith("/cancel"):
            return {}
        try:
            data = response.json()
        except ValueError:
            raise error_type("Polymarket US response could not be read") from None
        if not isinstance(data, dict):
            raise error_type("Unexpected Polymarket US response")
        return data

    async def market(self, slug: str) -> MarketRules:
        closes_at = _window_end(slug)
        raw = (await self._request("GET", f"/v1/market/slug/{slug}", public=True)).get("market")
        if not isinstance(raw, dict) or raw.get("slug") != slug:
            raise VenueError("WTI market metadata does not match the requested window")
        terms = raw.get("assetPriceTerms") or {}
        asset = terms.get("asset") if isinstance(terms, dict) else None
        if (not isinstance(asset, dict) or terms.get("marketType") != "ASSET_PRICE_MARKET_TYPE_UP_DOWN"
                or asset.get("symbol") != "wti" or terms.get("horizon") != "15m"
                or _timestamp(terms.get("windowEnd")) != closes_at
                or _timestamp(terms.get("windowStart")) != closes_at - 900):
            raise VenueError("Venue metadata is not for the expected WTI 15-minute window")
        minimum = _amount(raw.get("minimumTradeQty"))
        tick = _amount(raw.get("orderPriceMinTickSize"))
        if minimum <= ZERO or not ZERO < tick < ONE:
            raise VenueError("Invalid WTI market increments")
        status = raw.get("status", "")
        return MarketRules(slug, minimum, tick, status, closes_at)

    async def buying_power(self) -> Decimal:
        balances = (await self._request("GET", "/v1/account/balances")).get("balances")
        if not isinstance(balances, list):
            raise VenueError("Account balances were unavailable")
        usd = [row for row in balances if isinstance(row, dict) and row.get("currency") == "USD"]
        if len(usd) != 1:
            raise VenueError("Account USD balance was unavailable")
        return _amount(usd[0].get("buyingPower"))

    async def positions(self) -> dict[str, Decimal]:
        positions = {}
        seen = set()
        params = None
        while True:
            data = await self._request("GET", "/v1/portfolio/positions", params=params)
            page = data.get("positions")
            if not isinstance(page, dict):
                raise VenueError("Account positions were unavailable")
            for slug, raw in page.items():
                if not isinstance(raw, dict) or slug in positions:
                    raise VenueError("Invalid account position page")
                # The legacy netPosition field is rounded, which loses fractional inventory.
                positions[slug] = _amount(raw.get("netPositionDecimal"))
            cursor = data.get("nextCursor")
            if data.get("eof") is True or (data.get("eof") is None and not cursor):
                return {slug: qty for slug, qty in positions.items() if qty != ZERO}
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise VenueError("Could not finish reconciling account positions")
            seen.add(cursor)
            params = {"cursor": cursor}

    async def open_orders(self) -> tuple[LiveOrder, ...]:
        orders = (await self._request("GET", "/v1/orders/open")).get("orders")
        if not isinstance(orders, list):
            raise VenueError("Open orders were unavailable")
        return tuple(_parse_order(raw) for raw in orders)

    async def order(self, order_id: str) -> LiveOrder:
        if not ORDER_ID.fullmatch(order_id):
            raise VenueError("Invalid venue order ID")
        result = _parse_order((await self._request("GET", f"/v1/order/{order_id}")).get("order"))
        if result.id != order_id:
            raise VenueError("Venue returned an unexpected order ID")
        return result

    async def submit_limit(
        self, *, slug: str, side: str, action: str, quantity: Decimal, price: Decimal, key: str,
        expires_at: float | None = None,
    ) -> LiveOrder:
        closes_at = _window_end(slug)
        now = self._clock()
        if not closes_at - 900 <= now < closes_at or (expires_at is not None and not now < expires_at <= closes_at):
            raise VenueError("Order is outside its WTI market deadline")
        intent = INTENT.get((side, action))
        quantity, price = _amount(quantity), _amount(price)
        wire_quantity = float(quantity)
        if intent is None or not 0 < wire_quantity or not math.isfinite(wire_quantity) or not PRICE_MIN <= price <= PRICE_MAX:
            raise VenueError("Invalid limit order parameters")
        if not key or not key.isascii() or len(key) > 255 or any(c.isspace() for c in key):
            raise VenueError("Invalid venue idempotency key")
        ioc = expires_at is None
        payload = {
            "marketSlug": slug,
            "type": "ORDER_TYPE_LIMIT",
            "price": {"value": format(_wire_price(side, price), "f"), "currency": "USD"},
            "quantity": wire_quantity,
            "intent": intent,
            "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
            "tif": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL" if ioc else "TIME_IN_FORCE_GOOD_TILL_DATE",
            "synchronousExecution": ioc,
        }
        if ioc:
            payload["maxBlockTime"] = "2"
        else:
            payload["goodTillTime"] = datetime.fromtimestamp(expires_at, timezone.utc).isoformat().replace("+00:00", "Z")
        data = await self._request("POST", "/v1/orders", payload=payload, key=key)
        order_id = data.get("id")
        if not isinstance(order_id, str) or not ORDER_ID.fullmatch(order_id):
            raise AmbiguousOrder("Submitted order returned no usable venue order ID")
        executions = data.get("executions")
        if executions is None or executions == []:
            return LiveOrder(order_id, slug, side, action, quantity, ZERO, quantity, price, None,
                             "ORDER_STATE_PENDING_NEW")
        try:
            result = _with_execution_totals(_parse_order(executions[-1]["order"]), executions)
        except (VenueError, AttributeError, KeyError, IndexError, TypeError):
            raise AmbiguousOrder("Submitted order details could not be confirmed", order_id) from None
        if (result.id, result.slug, result.side, result.action, result.quantity) != (order_id, slug, side, action, quantity):
            raise AmbiguousOrder("Submitted order details do not match the intent", order_id)
        return result

    async def cancel(self, order_id: str, slug: str) -> None:
        _window_end(slug)
        if not ORDER_ID.fullmatch(order_id):
            raise VenueError("Invalid venue order ID")
        await self._request("POST", f"/v1/order/{order_id}/cancel", payload={"marketSlug": slug})
