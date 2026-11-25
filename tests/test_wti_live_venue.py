import asyncio
from base64 import b64decode, b64encode
from datetime import datetime, timezone
from decimal import Decimal as D
import json

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import httpx
import pytest

from backend.wti.live_venue import AmbiguousOrder, LiveVenue, VenueError


SLUG = "cpwtif-wti-updown-15m-2026-11-24-1830z"
START = datetime(2026, 11, 24, 18, 30, tzinfo=timezone.utc).timestamp()
NOW = START + 120
KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
SECRET = b64encode(bytes(range(32))).decode()


def check_signed(request):
    assert request.url.host == "api.polymarket.us"
    assert request.headers["X-PM-Access-Key"] == "test-key"
    assert request.headers["X-PM-Timestamp"] == str(int(NOW * 1000))
    message = f"{int(NOW * 1000)}{request.method}{request.url.path}".encode()
    KEY.public_key().verify(b64decode(request.headers["X-PM-Signature"]), message)


def venue(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


def order(**fields):
    return {
        "id": "exec-order",
        "marketSlug": SLUG,
        "quantity": 1.2,
        "cumQuantity": 0.4,
        "leavesQuantity": 0,
        "price": {"value": "0.59", "currency": "USD"},
        "intent": "ORDER_INTENT_BUY_SHORT",
        "state": "ORDER_STATE_CANCELED",
        **fields,
    }


def test_ioc_down_limit_is_yes_priced_and_partial_fill_uses_execution_totals():
    async def run():
        def handler(request):
            check_signed(request)
            assert request.method == "POST" and request.url.path == "/v1/orders"
            assert request.headers["Idempotency-Key"] == "pilot-entry"
            body = json.loads(request.content)
            assert body == {
                "marketSlug": SLUG,
                "type": "ORDER_TYPE_LIMIT",
                "price": {"value": "0.59", "currency": "USD"},
                "quantity": 1.2,
                "intent": "ORDER_INTENT_BUY_SHORT",
                "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
                "tif": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
                "synchronousExecution": True,
                "maxBlockTime": "2",
            }
            return httpx.Response(200, json={"id": "exec-order", "executions": [
                {"id": "exec-fill", "type": "EXECUTION_TYPE_PARTIAL_FILL",
                 "order": order(state="ORDER_STATE_PARTIALLY_FILLED", leavesQuantity=0.8), "lastShares": "0.4",
                 "lastPx": {"value": "0.61", "currency": "USD"},
                 "transactTime": "2026-11-24T18:32:00Z",
                 "commissionNotionalCollected": {"value": "0.01", "currency": "USD"}},
                {"id": "exec-cancel", "type": "EXECUTION_TYPE_CANCELED", "order": order()},
            ]})

        async with venue(handler) as transport:
            api = LiveVenue(transport, "test-key", SECRET, clock=lambda: NOW)
            receipt = await api.submit_limit(slug=SLUG, side="DOWN", action="buy", quantity=D("1.2"),
                                             price=D("0.41"), key="pilot-entry")
        assert receipt.terminal and receipt.state == "ORDER_STATE_CANCELED"
        assert (receipt.price, receipt.filled, receipt.average_price, receipt.fees) == (
            D("0.41"), D("0.4"), D("0.39"), D("0.01"),
        )
        fill, = receipt.executions
        assert (fill.id, fill.at, fill.quantity, fill.price, fill.fee) == (
            "exec-fill", NOW, D("0.4"), D("0.39"), D("0.01"),
        )

    asyncio.run(run())


def test_partial_execution_records_missing_fee_without_manufacturing_one():
    async def run():
        def handler(request):
            check_signed(request)
            return httpx.Response(200, json={"id": "exec-order", "executions": [
                {"id": "exec-fill", "type": "EXECUTION_TYPE_PARTIAL_FILL",
                 "order": order(), "lastShares": "0.4",
                 "lastPx": {"value": "0.61", "currency": "USD"}},
            ]})

        async with venue(handler) as transport:
            api = LiveVenue(transport, "test-key", SECRET, clock=lambda: NOW)
            receipt = await api.submit_limit(slug=SLUG, side="DOWN", action="buy", quantity=D("1.2"),
                                             price=D("0.41"), key="pilot-entry")
        assert receipt.average_price == D("0.39") and receipt.fees is None
        assert len(receipt.executions) == 1
        assert receipt.executions[0].at is None and receipt.executions[0].fee is None

    asyncio.run(run())


def test_resting_exit_reconciles_through_order_and_filled_cancellation():
    async def run():
        def handler(request):
            check_signed(request)
            if request.url.path == "/v1/orders":
                body = json.loads(request.content)
                assert body["tif"] == "TIME_IN_FORCE_GOOD_TILL_DATE"
                assert body["intent"] == "ORDER_INTENT_SELL_SHORT"
                assert body["goodTillTime"] == "2026-11-24T18:44:50Z"
                assert body["synchronousExecution"] is False
                return httpx.Response(200, json={"id": "exec-order"})
            if request.url.path == "/v1/order/exec-order/cancel":
                assert json.loads(request.content) == {"marketSlug": SLUG}
                return httpx.Response(200, json={})
            assert request.url.path == "/v1/order/exec-order"
            return httpx.Response(200, json={"order": order(
                intent="ORDER_INTENT_SELL_SHORT", avgPx={"value": "0.58", "currency": "USD"},
                commissionNotionalTotalCollected={"value": "0.01", "currency": "USD"},
            )})

        async with venue(handler) as transport:
            api = LiveVenue(transport, "test-key", SECRET, clock=lambda: NOW)
            receipt = await api.submit_limit(slug=SLUG, side="DOWN", action="sell", quantity=D("1.2"),
                                             price=D("0.41"), key="pilot-target", expires_at=START + 890)
            assert receipt.id == "exec-order" and not receipt.terminal and receipt.filled == 0
            await api.cancel("exec-order", SLUG)
            receipt = await api.order("exec-order")
            assert receipt.terminal and receipt.filled == D("0.4") and receipt.average_price == D("0.42")

    asyncio.run(run())


def test_reconciliation_uses_decimal_positions_and_public_metadata_has_no_auth():
    async def run():
        def handler(request):
            path = request.url.path
            if path == f"/v1/market/slug/{SLUG}":
                assert request.url.host == "gateway.polymarket.us"
                assert "X-PM-Access-Key" not in request.headers
                return httpx.Response(200, json={"market": {
                    "slug": SLUG, "minimumTradeQty": 0.01, "orderPriceMinTickSize": 0.01,
                    "status": "MARKET_STATUS_OPEN", "assetPriceTerms": {
                        "asset": {"symbol": "wti"}, "horizon": "15m",
                        "marketType": "ASSET_PRICE_MARKET_TYPE_UP_DOWN",
                        "windowStart": "2026-11-24T18:30:00Z", "windowEnd": "2026-11-24T18:45:00Z",
                    },
                }})
            check_signed(request)
            if path == "/v1/account/balances":
                return httpx.Response(200, json={"balances": [{"currency": "USD", "buyingPower": 10.53}]})
            if path == "/v1/orders/open":
                return httpx.Response(200, json={"orders": [order(state="ORDER_STATE_PARTIALLY_FILLED",
                                                                   leavesQuantity=0.8)]})
            assert path == "/v1/portfolio/positions"
            if not request.url.query:
                return httpx.Response(200, json={"positions": {SLUG: {"netPositionDecimal": "-0.4",
                                                                      "netPosition": "0"}},
                                                 "nextCursor": "page-2", "eof": False})
            assert request.url.params.get("cursor") == "page-2"
            return httpx.Response(200, json={"positions": {"other": {"netPositionDecimal": "0"}}, "eof": True})

        async with venue(handler) as transport:
            api = LiveVenue(transport, "test-key", SECRET, clock=lambda: NOW)
            rules = await api.market(SLUG)
            assert rules.is_open and rules.min_quantity == rules.tick_size == D("0.01")
            assert rules.closes_at == START + 900
            assert await api.buying_power() == D("10.53")
            assert await api.positions() == {SLUG: D("-0.4")}
            assert (await api.open_orders())[0].remaining == D("0.8")

    asyncio.run(run())


@pytest.mark.parametrize("status", [307, 401, 429, 503])
def test_ambiguous_submission_never_redirects_or_retries(status):
    async def run():
        calls = []

        def handler(request):
            check_signed(request)
            calls.append(request)
            return httpx.Response(status, headers={"Location": "https://example.com"}, text="omit response body")

        async with venue(handler) as transport:
            api = LiveVenue(transport, "test-key", SECRET, clock=lambda: NOW)
            with pytest.raises(AmbiguousOrder, match=f"HTTP {status}"):
                await api.submit_limit(slug=SLUG, side="UP", action="buy", quantity=D("1"),
                                       price=D("0.41"), key="pilot-entry")
        assert len(calls) == 1

    asyncio.run(run())


def test_refuses_other_markets_and_closed_window_before_network_request():
    async def run():
        def handler(request):
            pytest.fail("No HTTP request was expected")

        async with venue(handler) as transport:
            api = LiveVenue(transport, "test-key", SECRET, clock=lambda: START + 900)
            for slug in (SLUG, "bitcoin-updown-15m", "cpwtif-wti-updown-1h-2026-11-24-1830z"):
                with pytest.raises(VenueError):
                    await api.submit_limit(slug=slug, side="UP", action="buy", quantity=D("1"),
                                           price=D("0.41"), key="pilot-entry")

    asyncio.run(run())
