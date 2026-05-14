import httpx
import pytest
import respx

from app.models.distributor import Distributor
from app.models.rfp import EmailStatus
from app.services.email_sender import (
    RESEND_URL,
    _demo_recipient,
    _distributor_slug,
    send_rfp_email,
)
from app.services.rfp_composer import RfpEmailContent


def test_distributor_slug_normalizes() -> None:
    assert _distributor_slug("Carolina Fresh Produce Co.") == "carolina-fresh-produce-co"
    assert _distributor_slug("Tidewater Seafood Distributors") == "tidewater-seafood-distributors"
    assert _distributor_slug("  ") == "distributor"


def test_demo_recipient_plus_addresses() -> None:
    assert (
        _demo_recipient("Carolina Fresh", "daniel@getserviceledger.com")
        == "daniel+carolina-fresh@getserviceledger.com"
    )


@pytest.mark.asyncio
async def test_send_success_persists_message_id_recipient_and_resend_id(
    monkeypatch,
) -> None:
    monkeypatch.setattr("app.services.email_sender.settings.resend_api_key", "test-key")
    monkeypatch.setattr(
        "app.services.email_sender.settings.rfp_from_email",
        "procurement@getserviceledger.com",
    )
    monkeypatch.setattr(
        "app.services.email_sender.settings.rfp_demo_inbox",
        "daniel@getserviceledger.com",
    )

    captured_body: dict = {}

    def _resend_handler(request: httpx.Request) -> httpx.Response:
        import json

        captured_body.update(json.loads(request.content))
        return httpx.Response(200, json={"id": "resend-uuid-abc"})

    distributor = Distributor(
        id=42,
        name="Carolina Fresh Produce Co.",
        specialties=["produce"],
        email="orders@carolinafresh.example",
    )

    async with httpx.AsyncClient() as http:
        with respx.mock(assert_all_called=True) as router:
            router.post(RESEND_URL).mock(side_effect=_resend_handler)
            result = await send_rfp_email(
                rfp_request_id=99,
                distributor=distributor,
                content=RfpEmailContent(
                    subject_tail="Ingredient quote request",
                    body="Hello — please quote.",
                ),
                subject="[RFP-99] Ingredient quote request",
                http=http,
            )

    assert result.ok
    row = result.rfp_email
    assert row.status == EmailStatus.sent
    assert row.resend_id == "resend-uuid-abc"
    assert row.recipient_actual == "daniel+carolina-fresh-produce-co@getserviceledger.com"
    assert row.recipient_nominal == "orders@carolinafresh.example"
    # Our minted Message-ID format
    assert row.message_id.startswith("<rfp-99-42-")
    assert row.message_id.endswith("@getserviceledger.com>")

    # Verify what we put on the wire
    assert captured_body["from"] == "procurement@getserviceledger.com"
    assert captured_body["to"] == ["daniel+carolina-fresh-produce-co@getserviceledger.com"]
    assert captured_body["reply_to"] == "procurement@getserviceledger.com"
    assert captured_body["subject"] == "[RFP-99] Ingredient quote request"
    assert captured_body["text"] == "Hello — please quote."
    assert captured_body["headers"]["Message-ID"] == row.message_id


@pytest.mark.asyncio
async def test_send_422_persists_failed_row_does_not_raise(monkeypatch) -> None:
    monkeypatch.setattr("app.services.email_sender.settings.resend_api_key", "test-key")
    monkeypatch.setattr(
        "app.services.email_sender.settings.rfp_from_email",
        "procurement@getserviceledger.com",
    )
    monkeypatch.setattr(
        "app.services.email_sender.settings.rfp_demo_inbox",
        "daniel@getserviceledger.com",
    )

    distributor = Distributor(
        id=42,
        name="Carolina Fresh",
        specialties=["produce"],
        email="orders@carolinafresh.example",
    )

    async with httpx.AsyncClient() as http:
        with respx.mock(assert_all_called=True) as router:
            router.post(RESEND_URL).mock(
                return_value=httpx.Response(422, json={"name": "validation_error"})
            )
            result = await send_rfp_email(
                rfp_request_id=99,
                distributor=distributor,
                content=RfpEmailContent(subject_tail="x", body="y"),
                subject="[RFP-99] x",
                http=http,
            )

    assert not result.ok
    assert result.rfp_email.status == EmailStatus.failed
    assert result.rfp_email.resend_id is None
    assert "422" in (result.rfp_email.raw_payload or {}).get("error", "")
    # Recipient + message_id still recorded so we know what we *tried* to send.
    assert result.rfp_email.recipient_actual.startswith("daniel+carolina-fresh@")
    assert result.rfp_email.message_id.startswith("<rfp-99-42-")


@pytest.mark.asyncio
async def test_send_with_missing_api_key_records_failure_does_not_raise(
    monkeypatch,
) -> None:
    monkeypatch.setattr("app.services.email_sender.settings.resend_api_key", "")
    monkeypatch.setattr(
        "app.services.email_sender.settings.rfp_from_email",
        "procurement@getserviceledger.com",
    )
    monkeypatch.setattr(
        "app.services.email_sender.settings.rfp_demo_inbox",
        "daniel@getserviceledger.com",
    )

    distributor = Distributor(
        id=7,
        name="Test Co",
        specialties=["produce"],
        email="t@example.com",
    )
    async with httpx.AsyncClient() as http:
        result = await send_rfp_email(
            rfp_request_id=1,
            distributor=distributor,
            content=RfpEmailContent(subject_tail="x", body="y"),
            subject="[RFP-1] x",
            http=http,
        )
    assert not result.ok
    assert result.rfp_email.status == EmailStatus.failed
    assert "RESEND_API_KEY" in (result.rfp_email.raw_payload or {}).get("error", "")
