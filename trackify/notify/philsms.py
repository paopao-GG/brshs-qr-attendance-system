"""PhilSMS provider.

    POST https://app.philsms.com/api/v3/sms/send
    Authorization: Bearer {api_token}
    {"recipient": "639XXXXXXXXX", "sender_id": "...", "type": "plain",
     "message": "..."}

There is no sandbox. Every call here is production and every send costs credits, which
is why scripts/test_sms.py exists: it probes /me and /balance -- both free -- before
anything is sent.

The delicate part is classifying failures. A request that timed out *after* being
written may or may not have been delivered, and re-sending it risks texting a parent
twice. Those are reported as ambiguous so the queue parks them for human
reconciliation instead of retrying.
"""

from __future__ import annotations

import httpx

from .provider import NotificationProvider, SendResult

# dashboard.philsms.com, NOT app.philsms.com. The public documentation is hosted on
# app.philsms.com and its examples name that host, but a real account token is
# rejected there with "Unauthenticated." while the same token succeeds here.
# Verified against the live account, not inferred from the docs.
API_BASE = "https://dashboard.philsms.com/api/v3"
SEND_URL = f"{API_BASE}/sms/send"
BALANCE_URL = f"{API_BASE}/balance"


class PhilSMSProvider(NotificationProvider):
    name = "philsms"

    def __init__(
        self,
        api_token: str,
        sender_id: str,
        *,
        timeout: float = 15.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_token:
            raise ValueError("PHILSMS_API_TOKEN is not set")
        if not sender_id:
            raise ValueError(
                "PHILSMS_SENDER_ID is not set. Register a sender ID in the PhilSMS "
                "dashboard -- telco approval takes 2-3 days."
            )
        self.sender_id = sender_id
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(timeout, connect=5.0),
            headers={
                "Authorization": f"Bearer {api_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

    def send(self, recipient: str, body: str) -> SendResult:
        payload = {
            "recipient": recipient,
            "sender_id": self.sender_id,
            # Documented as required. Omitting it gets the request rejected with a 4xx
            # that reads like an auth failure, which is a long way to travel for a
            # missing constant.
            "type": "plain",
            "message": body,
        }

        try:
            response = self._client.post(SEND_URL, json=payload)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # Never reached the server. Safe to retry.
            return SendResult(ok=False, error=f"connect failed: {exc}")
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError) as exc:
            # The request was written. Delivery is unknown -- do not retry.
            return SendResult(ok=False, ambiguous=True, error=f"timeout after send: {exc}")
        except httpx.HTTPError as exc:
            return SendResult(ok=False, ambiguous=True, error=f"transport error: {exc}")

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            return SendResult(
                ok=False,
                error="rate limited by provider",
                retry_after=float(retry_after) if retry_after else 60.0,
            )

        if response.status_code >= 500:
            # Server-side error; the message almost certainly was not queued.
            return SendResult(ok=False, error=f"provider {response.status_code}")

        if response.status_code >= 400:
            return SendResult(ok=False, error=f"rejected {response.status_code}: "
                                              f"{response.text[:200]}")

        # PhilSMS answers HTTP 200 even when it refuses the message -- an expired
        # token, an unapproved sender ID and an empty balance all arrive as
        # {"status": "error", ...} with a 200. Trusting the status code alone would
        # mark those rows 'sent', and the guardian would simply never be texted while
        # the queue reported success. Verified against the live API.
        error = _api_error(response)
        if error:
            return SendResult(ok=False, error=f"provider refused: {error}")

        return SendResult(ok=True, provider_message_id=_message_id(response))

    def balance(self) -> float | None:
        """Remaining balance in pesos, or None if it cannot be read.

        PhilSMS reports money, not a message count: the live response is
        {"remaining_balance": "₱286", "expired_on": "..."}. The peso sign is part
        of the string, so it has to be stripped before the number parses.
        """
        try:
            response = self._client.get(BALANCE_URL)
            data = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        if _api_error(response):
            return None
        for key in ("remaining_balance", "balance", "credits", "credit_balance"):
            value = _dig(data, key)
            if value is not None:
                return _peso(value)
        return None

    def close(self) -> None:
        self._client.close()


def _peso(value) -> float | None:
    """'₱286' or '286.50' or 286 -> 286.0"""
    text = str(value)
    cleaned = "".join(c for c in text if c.isdigit() or c in ".-")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _api_error(response: httpx.Response) -> str | None:
    """The refusal message from a 200 response, if it is actually a refusal."""
    try:
        data = response.json()
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    if str(data.get("status", "")).lower() == "error":
        return str(data.get("message") or "unspecified error")
    return None


def _dig(data, key):
    """Find `key` anywhere in a small nested response without assuming its shape."""
    if isinstance(data, dict):
        if key in data:
            return data[key]
        for value in data.values():
            found = _dig(value, key)
            if found is not None:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _dig(item, key)
            if found is not None:
                return found
    return None


def _message_id(response: httpx.Response) -> str | None:
    try:
        data = response.json()
    except ValueError:
        return None
    for key in ("message_id", "uid", "id", "message_uid"):
        value = _dig(data, key)
        if value is not None:
            return str(value)
    return None
