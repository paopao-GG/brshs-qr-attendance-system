"""PhilSMS failure classification.

The distinction that matters: a request that never reached the server is safe to
retry; one that timed out after being written is not, because it may have been
delivered. Getting this wrong means either lost or duplicated parent notifications.
"""
import httpx
import pytest

from trackify.notify.philsms import PhilSMSProvider


def provider(handler):
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, headers={"Authorization": "Bearer t"})
    return PhilSMSProvider("token", "TRACKIFY", client=client)


def test_successful_send_returns_message_id():
    def handler(request):
        assert request.url.path == "/api/v3/sms/send"
        return httpx.Response(200, json={"data": {"message_id": "abc123"}})

    result = provider(handler).send("639171234567", "hello")
    assert result.ok
    assert result.provider_message_id == "abc123"
    assert not result.ambiguous


def test_payload_shape():
    seen = {}

    def handler(request):
        import json
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"message_id": "1"})

    provider(handler).send("639171234567", "body text")
    assert seen == {"recipient": "639171234567", "sender_id": "TRACKIFY",
                    "type": "plain", "message": "body text"},         "PhilSMS documents type as required; without it the send is rejected"


def test_connect_failure_is_retryable_not_ambiguous():
    """Never reached the server, so retrying cannot duplicate."""
    def handler(request):
        raise httpx.ConnectError("no route to host")

    result = provider(handler).send("639171234567", "hi")
    assert not result.ok
    assert not result.ambiguous


def test_read_timeout_is_ambiguous():
    """Request was written; delivery unknown. Must NOT be auto-retried."""
    def handler(request):
        raise httpx.ReadTimeout("timed out waiting for response")

    result = provider(handler).send("639171234567", "hi")
    assert not result.ok
    assert result.ambiguous


def test_4xx_is_a_definite_rejection():
    def handler(request):
        return httpx.Response(422, text="invalid sender_id")

    result = provider(handler).send("639171234567", "hi")
    assert not result.ok
    assert not result.ambiguous
    assert "422" in result.error


def test_5xx_is_retryable():
    def handler(request):
        return httpx.Response(503, text="upstream unavailable")

    result = provider(handler).send("639171234567", "hi")
    assert not result.ok
    assert not result.ambiguous


def test_429_carries_retry_after():
    def handler(request):
        return httpx.Response(429, headers={"Retry-After": "30"})

    result = provider(handler).send("639171234567", "hi")
    assert not result.ok
    assert result.retry_after == 30.0


def test_missing_sender_id_fails_loudly_with_the_lead_time():
    with pytest.raises(ValueError, match="2-3 days"):
        PhilSMSProvider("token", "")


def test_missing_token_fails_loudly():
    with pytest.raises(ValueError, match="PHILSMS_API_TOKEN"):
        PhilSMSProvider("", "TRACKIFY")


def test_balance_parsed_from_nested_response():
    def handler(request):
        return httpx.Response(200, json={"data": {"balance": "1250.0"}})

    assert provider(handler).balance() == 1250


def test_balance_returns_none_when_unavailable():
    def handler(request):
        return httpx.Response(500)

    assert provider(handler).balance() is None


def test_200_with_error_body_is_not_a_success():
    """PhilSMS answers HTTP 200 even when it refuses the message.

    Trusting the status code marks the row 'sent' while the guardian is never texted --
    a silent failure that the queue reports as success. Verified against the live API,
    which returns exactly this for an expired token.
    """
    def handler(request):
        return httpx.Response(
            200, json={"status": "error", "message": "Unauthenticated."}
        )

    result = provider(handler).send("639171234567", "hi")
    assert not result.ok, "a refused message must never be recorded as sent"
    assert "Unauthenticated" in result.error
    assert not result.ambiguous


def test_200_success_body_still_succeeds():
    def handler(request):
        return httpx.Response(
            200, json={"status": "success", "data": {"uid": "abc123"}}
        )

    result = provider(handler).send("639171234567", "hi")
    assert result.ok
    assert result.provider_message_id == "abc123"


def test_balance_strips_the_peso_sign():
    """The live response is {"remaining_balance": "\u20b1286"} -- money, not a count."""
    def handler(request):
        return httpx.Response(200, json={
            "status": "success",
            "data": {"remaining_balance": "\u20b1286", "expired_on": "23rd Aug 27"},
        })

    assert provider(handler).balance() == 286.0


def test_balance_returns_none_on_an_error_body():
    def handler(request):
        return httpx.Response(200, json={"status": "error", "message": "nope"})

    assert provider(handler).balance() is None
