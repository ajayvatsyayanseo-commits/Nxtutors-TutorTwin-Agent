"""Cashfree Payment Gateway: create an order, and trust a callback about it.

Two operations, and the second one is the one that matters. Creating an order
is an ordinary API call; **verifying the callback is what stands between a
signed payment and a free subscription**, because the callback endpoint is a
public URL and anyone can post `{"order_status": "PAID"}` to it.

The browser is never trusted to report success either. The student's browser
returns to a success page after paying, but that page's only job is to say
"thanks" - the entitlement is granted by the server-to-server webhook, or by an
explicit status fetch from Cashfree. A student who edits the return URL gets a
nice page and no subscription.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass
from typing import Any

import httpx

from tutortwin.observability.logging import get_logger

logger = get_logger(__name__)

SIGNATURE_HEADER = "x-webhook-signature"
TIMESTAMP_HEADER = "x-webhook-timestamp"


class CashfreeError(Exception):
    """The gateway refused, or could not be reached."""


class SignatureError(Exception):
    """A callback that did not come from Cashfree."""


def verify_webhook(
    *, body: bytes, signature: str | None, timestamp: str | None, secret: str
) -> None:
    """Cashfree signs `timestamp + raw_body` with the secret key, base64'd.

    The timestamp is inside the signed material, which is what stops a captured
    genuine callback from being replayed forever. It is verified as part of the
    signature rather than compared to the clock: rejecting on clock skew would
    drop real payments, and the signature already makes the timestamp
    unforgeable.

    Raises rather than returning a bool - a caller who forgets to check a
    returned bool has an endpoint that grants free subscriptions.
    """
    if not secret:
        raise SignatureError("no cashfree secret configured")
    if not signature or not timestamp:
        raise SignatureError("missing signature or timestamp header")

    digest = hmac.new(
        secret.encode(), (timestamp + body.decode("utf-8", "replace")).encode(), hashlib.sha256
    ).digest()
    expected = base64.b64encode(digest).decode()

    if not hmac.compare_digest(expected, signature.strip()):
        raise SignatureError("signature mismatch")


@dataclass(slots=True)
class CashfreeOrder:
    order_id: str
    payment_session_id: str
    raw: dict[str, Any]


@dataclass(slots=True)
class CashfreeClient:
    app_id: str
    secret_key: str
    base_url: str
    api_version: str = "2025-01-01"
    timeout_seconds: float = 20.0
    _client: httpx.AsyncClient | None = None

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "x-client-id": self.app_id,
            "x-client-secret": self.secret_key,
            "x-api-version": self.api_version,
            "Content-Type": "application/json",
        }

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout_seconds)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def create_order(
        self,
        *,
        order_id: str,
        amount_paise: int,
        customer_id: str,
        customer_phone: str,
        customer_name: str,
        return_url: str,
        notify_url: str,
        currency: str = "INR",
    ) -> CashfreeOrder:
        """Create an order and get the session id the checkout needs.

        Cashfree takes the amount in **rupees**, not paise. We store paise
        because integers cannot drift; the conversion happens here, once, at the
        boundary - and only here, so there is exactly one line to check when the
        arithmetic is ever in doubt.
        """
        payload = {
            "order_id": order_id,
            "order_amount": round(amount_paise / 100, 2),
            "order_currency": currency,
            "customer_details": {
                "customer_id": customer_id,
                "customer_phone": customer_phone,
                "customer_name": customer_name,
            },
            "order_meta": {"return_url": return_url, "notify_url": notify_url},
        }

        try:
            response = await self._http().post(
                f"{self.base_url}/orders", headers=self._headers, json=payload
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # The body carries the reason - a disabled account and a malformed
            # phone number are both 400 - and it is the only thing worth having
            # when a student says "it would not let me pay".
            logger.error(
                "cashfree_order_failed",
                status=exc.response.status_code,
                detail=exc.response.text[:500],
                order_id=order_id,
            )
            raise CashfreeError(f"cashfree refused the order: {exc.response.text[:200]}") from exc
        except httpx.HTTPError as exc:
            logger.error("cashfree_unreachable", error_type=type(exc).__name__)
            raise CashfreeError("cashfree could not be reached") from exc

        data = response.json()
        session = data.get("payment_session_id")
        if not session:
            raise CashfreeError("cashfree returned no payment_session_id")

        logger.info("cashfree_order_created", order_id=order_id, amount_paise=amount_paise)
        return CashfreeOrder(order_id=order_id, payment_session_id=str(session), raw=data)

    async def fetch_order(self, order_id: str) -> dict[str, Any]:
        """Ask Cashfree what actually happened to an order.

        This is the authority, not the webhook and certainly not the browser.
        It is used to confirm a payment when the student returns from checkout,
        so a subscription still activates promptly if the webhook is delayed or
        the callback URL is briefly unreachable.
        """
        try:
            response = await self._http().get(
                f"{self.base_url}/orders/{order_id}", headers=self._headers
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("cashfree_fetch_failed", order_id=order_id, error_type=type(exc).__name__)
            raise CashfreeError("could not fetch the order") from exc
        result: dict[str, Any] = response.json()
        return result


def order_status_of(payload: dict[str, Any]) -> str:
    """Pull the order status out of either shape Cashfree sends.

    The webhook nests it under `data.order.order_status`; a direct order fetch
    returns `order_status` at the top level. Reading only one of the two is a
    bug that shows up as "payments never activate", in production, on a
    weekend.
    """
    data = payload.get("data")
    if isinstance(data, dict):
        order = data.get("order")
        if isinstance(order, dict) and order.get("order_status"):
            return str(order["order_status"]).upper()
        payment = data.get("payment")
        if isinstance(payment, dict) and payment.get("payment_status"):
            return str(payment["payment_status"]).upper()
    if payload.get("order_status"):
        return str(payload["order_status"]).upper()
    return "UNKNOWN"


def order_id_of(payload: dict[str, Any]) -> str | None:
    data = payload.get("data")
    if isinstance(data, dict):
        order = data.get("order")
        if isinstance(order, dict) and order.get("order_id"):
            return str(order["order_id"])
    if payload.get("order_id"):
        return str(payload["order_id"])
    return None


def payment_reference_of(payload: dict[str, Any]) -> str | None:
    data = payload.get("data")
    if isinstance(data, dict):
        payment = data.get("payment")
        if isinstance(payment, dict):
            for key in ("cf_payment_id", "payment_id"):
                if payment.get(key):
                    return str(payment[key])
    return None


PAID_STATUSES = frozenset({"PAID", "SUCCESS", "SUCCESSFUL"})
"""Cashfree says PAID for an order and SUCCESS for a payment. Both mean the
money arrived; treating only one as paid silently drops half the callbacks."""


__all__ = [
    "PAID_STATUSES",
    "SIGNATURE_HEADER",
    "TIMESTAMP_HEADER",
    "CashfreeClient",
    "CashfreeError",
    "CashfreeOrder",
    "SignatureError",
    "order_id_of",
    "order_status_of",
    "payment_reference_of",
    "verify_webhook",
]
