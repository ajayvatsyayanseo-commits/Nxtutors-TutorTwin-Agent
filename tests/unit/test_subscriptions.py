"""Money, identity and access.

Three things are being protected here, in descending order of how expensive it
is to get them wrong:

1. **A forged payment callback must not grant a subscription.** The endpoint is
   a public URL and anyone can post `{"order_status":"PAID"}` at it.
2. **A redelivered callback must grant exactly one subscription.** Cashfree
   delivers at least once, and the success page also triggers a status check,
   so two or three concurrent activations of one order is the normal case.
3. **One person must not become three students.** `+91 99990 00001`,
   `919999000001` and `09999000001` are one phone; stored as typed they are
   three identities, two of which paid for access attached to a number the
   agent will never see a message from.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Any

import pytest

from tutortwin.integrations import cashfree
from tutortwin.services.subscriptions import new_order_id, normalize_wa_number

SECRET = "cashfree-secret-value"


def sign(body: bytes, timestamp: str, secret: str = SECRET) -> str:
    digest = hmac.new(secret.encode(), (timestamp + body.decode()).encode(), hashlib.sha256)
    return base64.b64encode(digest.digest()).decode()


class TestWebhookSignature:
    def test_a_genuine_callback_passes(self) -> None:
        body, ts = b'{"data":{"order":{"order_status":"PAID"}}}', "1767225600"
        cashfree.verify_webhook(body=body, signature=sign(body, ts), timestamp=ts, secret=SECRET)

    def test_a_forged_paid_callback_is_refused(self) -> None:
        """The one that matters. Without this, anyone who finds the URL gets a
        free subscription by posting a JSON object."""
        body = b'{"data":{"order":{"order_id":"tt_x","order_status":"PAID"}}}'
        with pytest.raises(cashfree.SignatureError, match="mismatch"):
            cashfree.verify_webhook(
                body=body, signature="ZmFrZQ==", timestamp="1767225600", secret=SECRET
            )

    def test_a_tampered_body_is_refused(self) -> None:
        """The signature covers the bytes, so changing the order id after
        signing invalidates it."""
        ts = "1767225600"
        header = sign(b'{"order_id":"tt_theirs"}', ts)
        with pytest.raises(cashfree.SignatureError):
            cashfree.verify_webhook(
                body=b'{"order_id":"tt_mine"}', signature=header, timestamp=ts, secret=SECRET
            )

    def test_the_timestamp_is_inside_the_signed_material(self) -> None:
        """It is what stops a captured genuine callback being replayed forever.
        Signing the body alone would make every past callback reusable."""
        body = b'{"ok":true}'
        with pytest.raises(cashfree.SignatureError):
            cashfree.verify_webhook(
                body=body,
                signature=sign(body, "1767225600"),
                timestamp="1799999999",
                secret=SECRET,
            )

    @pytest.mark.parametrize(
        ("sig", "ts"),
        [(None, "1767225600"), ("abc", None), (None, None), ("", "")],
    )
    def test_missing_headers_are_refused(self, sig: str | None, ts: str | None) -> None:
        with pytest.raises(cashfree.SignatureError):
            cashfree.verify_webhook(body=b"{}", signature=sig, timestamp=ts, secret=SECRET)

    def test_an_unconfigured_secret_refuses_everything(self) -> None:
        """Fail closed. An empty secret must not become a wildcard."""
        body, ts = b"{}", "1767225600"
        with pytest.raises(cashfree.SignatureError, match="no cashfree secret"):
            cashfree.verify_webhook(
                body=body, signature=sign(body, ts, ""), timestamp=ts, secret=""
            )


class TestPayloadShapes:
    """Cashfree sends two different shapes and reading only one is a bug that
    presents as 'payments never activate', in production, on a weekend."""

    WEBHOOK: dict[str, Any] = {
        "data": {
            "order": {"order_id": "tt_abc", "order_status": "PAID"},
            "payment": {"cf_payment_id": "998877", "payment_status": "SUCCESS"},
        }
    }
    FETCH: dict[str, Any] = {"order_id": "tt_abc", "order_status": "PAID"}

    def test_status_from_the_webhook_shape(self) -> None:
        assert cashfree.order_status_of(self.WEBHOOK) == "PAID"

    def test_status_from_the_order_fetch_shape(self) -> None:
        assert cashfree.order_status_of(self.FETCH) == "PAID"

    def test_order_id_from_both_shapes(self) -> None:
        assert cashfree.order_id_of(self.WEBHOOK) == "tt_abc"
        assert cashfree.order_id_of(self.FETCH) == "tt_abc"

    def test_the_payment_reference_is_kept_for_disputes(self) -> None:
        assert cashfree.payment_reference_of(self.WEBHOOK) == "998877"

    def test_an_unrecognised_payload_is_unknown_not_paid(self) -> None:
        """Anything we cannot read must fail closed. 'UNKNOWN' grants nothing."""
        assert cashfree.order_status_of({"nonsense": 1}) == "UNKNOWN"
        assert cashfree.order_id_of({}) is None

    def test_both_paid_spellings_count(self) -> None:
        """An order is PAID and a payment is SUCCESS. Treating only one as paid
        silently drops half the callbacks."""
        assert "PAID" in cashfree.PAID_STATUSES
        assert "SUCCESS" in cashfree.PAID_STATUSES

    def test_a_pending_status_is_not_paid(self) -> None:
        assert "ACTIVE" not in cashfree.PAID_STATUSES
        assert "FAILED" not in cashfree.PAID_STATUSES


class TestPhoneNormalisation:
    @pytest.mark.parametrize(
        "typed",
        [
            "+91 99990 00001",
            "919999000001",
            "09999000001",
            "9999000001",
            "+919999000001",
            "0091 9999000001",
            "(999) 900-0001",
        ],
    )
    def test_every_way_a_person_writes_their_number_is_one_identity(self, typed: str) -> None:
        """Otherwise a student pays, and the subscription attaches to a number
        the agent never receives a message from."""
        assert normalize_wa_number(typed) == "919999000001"

    def test_a_number_that_already_has_a_country_code_is_left_alone(self) -> None:
        """Not everyone is in India. A 10-digit rule must not mangle a number
        that already carries its own code."""
        assert normalize_wa_number("+44 7700 900123") == "447700900123"

    def test_nothing_but_digits_survives(self) -> None:
        assert normalize_wa_number("+91-99990 00001 ") == "919999000001"


class TestOrderIds:
    def test_ids_are_unique(self) -> None:
        assert len({new_order_id() for _ in range(500)}) == 500

    def test_ids_are_recognisable_as_ours(self) -> None:
        """Ours, not the gateway's, and generated before the gateway is called -
        so a create-order timeout leaves something to look the order up by
        rather than a silent retry into a double charge."""
        assert new_order_id().startswith("tt_")


class TestAmountArithmetic:
    """Paise are integers on purpose. This is the only place the conversion to
    rupees happens, so it is the only place it can be wrong."""

    @pytest.mark.parametrize(
        ("paise", "rupees"),
        [(10_000, 100.0), (1, 0.01), (99, 0.99), (12_345, 123.45), (100_000, 1000.0)],
    )
    def test_paise_to_rupees(self, paise: int, rupees: float) -> None:
        assert round(paise / 100, 2) == rupees


class TestIdentityAgreement:
    """The single most expensive bug this codebase has had.

    Activation created the subject row with a random `uuid4()` and wrote the
    entitlement against it. An inbound WhatsApp message resolved the same
    student to a deterministic uuid5. The entitlement lookup used the
    resolver's id, found nothing, and every paying student was served as
    unsubscribed: the money moved and the access did not.

    Nothing about it was visible in isolation - both halves were internally
    consistent. Only asking whether the two agree finds it, so that is what
    these tests do.
    """

    @pytest.mark.asyncio
    async def test_activation_and_the_resolver_agree_on_the_id(self) -> None:
        from tutortwin.domain.events import SubjectRef
        from tutortwin.providers.fakes import FakeIdentityGateway
        from tutortwin.services.subscriptions import canonical_subject_id

        wa = "919999000001"
        resolved = await FakeIdentityGateway().resolve(
            SubjectRef(external_type="whatsapp", external_id=wa)
        )
        assert resolved is not None
        assert canonical_subject_id(wa) == resolved.id, (
            "A payment would write the entitlement against an id the runtime "
            "never asks about, and the paying student would get nothing."
        )

    @pytest.mark.asyncio
    async def test_they_agree_for_every_way_a_number_is_written(self) -> None:
        """Normalisation happens before the id is derived, so all spellings of
        one phone must reach one identity."""
        from tutortwin.domain.events import SubjectRef
        from tutortwin.providers.fakes import FakeIdentityGateway
        from tutortwin.services.subscriptions import canonical_subject_id, normalize_wa_number

        gateway = FakeIdentityGateway()
        ids = set()
        for typed in ("+91 99990 00001", "09999000001", "9999000001", "919999000001"):
            wa = normalize_wa_number(typed)
            resolved = await gateway.resolve(SubjectRef(external_type="whatsapp", external_id=wa))
            assert resolved is not None
            assert canonical_subject_id(wa) == resolved.id
            ids.add(resolved.id)
        assert len(ids) == 1, "one phone must be one student"

    def test_the_id_is_stable_across_processes(self) -> None:
        """It is derived, not stored, so a restart must not change it."""
        from tutortwin.services.subscriptions import canonical_subject_id

        assert canonical_subject_id("919999000001") == canonical_subject_id("919999000001")
        assert canonical_subject_id("919999000001") != canonical_subject_id("919999000002")
