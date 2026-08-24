"""Live PhilSMS test. Edit TEST_RECIPIENT below, then run this.

    python scripts/test_sms.py            # stages 1 and 2 only -- costs nothing
    python scripts/test_sms.py --send     # stage 3: one real SMS, costs P0.35

There is no PhilSMS sandbox. Every call is production, so this runs in stages and only
the last one spends anything:

    1  --check    GET /me and /balance. Free. Proves the token, names the account,
                  and reports remaining credits.
    2  --preview  Renders every real template offline and validates GSM-7. Free.
    3  --send     One message, to TEST_RECIPIENT only.

Stage 1 is the one that earns its keep. From inside the kiosk, a bad token, an
unapproved sender ID and an empty credit balance all surface as the same generic 4xx.
This tells them apart, which is why the response body is printed verbatim on failure --
that is where PhilSMS says which of the three it actually is.

This script deliberately touches neither the database nor the notification queue, so a
failure here implicates PhilSMS and nothing else.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# ---------------------------------------------------------------------------
# PUT YOUR OWN MOBILE NUMBER HERE.
#
# Any of these formats works -- it is normalised the same way the roster importer
# normalises guardian numbers:  09171234567  /  +639171234567  /  639171234567
#
# Use YOUR number. The demo roster is seeded with valid-format Philippine numbers on
# live Globe and Smart prefixes, so a stray digit here texts a stranger a child's
# attendance record.
TEST_RECIPIENT = "09171234567"
# ---------------------------------------------------------------------------

from trackify.core import mobile
from trackify.core.attendance import Trigger
from trackify.core.config import load_config
from trackify.notify import coalesce, gsm7, queue

RULE = "-" * 68


def heading(text: str) -> None:
    print(f"\n{text}\n{RULE}")


# -- stage 1: does the account work at all ----------------------------------

def _client(token: str):
    """A bare client for the free probes.

    Deliberately not PhilSMSProvider: that constructor demands a sender ID, and the
    whole point of stage 1 is to verify the token BEFORE a sender ID exists.
    """
    import httpx

    return httpx.Client(
        timeout=httpx.Timeout(15.0, connect=5.0),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )


def _redact(obj):
    """The /me response echoes the API token straight back. Never print it."""
    if isinstance(obj, dict):
        return {k: ("<redacted>" if "token" in k.lower() or "key" in k.lower()
                    else _redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(i) for i in obj]
    return obj


def _call(client, url: str):
    """Returns (ok, payload_or_message).

    PhilSMS answers HTTP 200 even when it refuses, so the status code proves nothing --
    the JSON "status" field is the real answer.
    """
    import httpx

    try:
        response = client.get(url)
    except httpx.HTTPError as exc:
        return False, f"unreachable: {exc}"
    try:
        data = response.json()
    except ValueError:
        return False, f"HTTP {response.status_code}: {response.text[:200]}"
    if not isinstance(data, dict) or str(data.get("status")).lower() == "error":
        return False, str(data.get("message") if isinstance(data, dict) else data)
    return True, data.get("data", data)


def check(token: str, sender_id: str) -> bool:
    """Hits /me and /balance. Sends nothing, costs nothing, needs no sender ID."""
    heading("1. ACCOUNT CHECK  (no SMS sent)")

    from trackify.notify.philsms import API_BASE

    print(f"  host       {API_BASE}")

    ok = True
    client = _client(token)
    try:
        good, payload = _call(client, f"{API_BASE}/me")
        if good:
            profile = _redact(payload)
            print("  token      OK")
            print(f"  account    {profile.get('first_name')} <{profile.get('email')}>")
            print(f"  timezone   {profile.get('timezone')}")
        else:
            ok = False
            print(f"  token      REJECTED -- {payload}")
            print("\n  'Unauthenticated' means one of two things:")
            print("    - the token in .env is wrong or expired, or")
            print(f"    - your account is not on {API_BASE}")
            print("  The public docs at app.philsms.com name a different host from the")
            print("  one that actually accepts account tokens. If this keeps failing,")
            print("  compare API_BASE in trackify/notify/philsms.py with the base URL")
            print("  shown in your own dashboard.")

        good, payload = _call(client, f"{API_BASE}/balance")
        if good:
            amount = payload.get("remaining_balance", payload)
            expires = payload.get("expired_on", "?")
            pesos = _peso_value(amount)
            # Print the number, not the raw string: PhilSMS returns a peso sign, and a
            # Windows console is cp1252, which cannot encode it. The same character is
            # outside GSM-7, which is why gsm7.py rejects it in message bodies too.
            shown = f"PHP {pesos:,.2f}" if pesos is not None else str(amount)
            print(f"  balance    {shown}   (expires {expires})")
            if pesos is not None:
                print(f"             ~= {int(pesos / 0.35)} message(s) at PHP 0.35 each")
                if pesos < 1:
                    ok = False
                    print("\n  No balance. A send will fail until the account is topped up.")
        else:
            print(f"  balance    could not be read -- {payload}")
    finally:
        client.close()

    if not sender_id:
        print("  sender_id  NOT SET -- required before anything can be sent")
        print("\n  There is no API that lists or creates sender IDs; you register one")
        print("  by hand in the PhilSMS dashboard. Free, several per account, 2-3 days")
        print("  for telco approval.")
        print("  You do not have to wait: the field also accepts a phone number with")
        print("  country code, so putting your own number in PHILSMS_SENDER_ID lets you")
        print("  test today and switch to a branded ID once it is approved.")
    else:
        print(f"  sender_id  {sender_id!r}")
        if not sender_id.lstrip("+").isdigit():
            print("             (alphanumeric -- needs telco approval. If a send is")
            print("              rejected, try your own number here instead.)")
    return ok


def _peso_value(raw):
    text = "".join(c for c in str(raw) if c.isdigit() or c == ".")
    try:
        return float(text)
    except ValueError:
        return None


# -- stage 2: what the parent actually receives -----------------------------

def _fake_student(first: str, section: str) -> dict:
    """queue.render only ever subscripts the row, so a dict stands in for one."""
    return {"first_name": first, "section_name": section}


def preview() -> bool:
    """Renders the real templates. No network, no cost.

    The bodies come from notify/queue and notify/coalesce rather than being retyped
    here. A test that previews a message the product does not send proves nothing.
    """
    heading("2. MESSAGE PREVIEW  (offline, no SMS sent)")

    at = datetime(2026, 8, 24, 7, 12)
    student = _fake_student("Juan", "Rizal")
    ok = True

    rows = []
    for trigger in Trigger:
        body = queue.render(trigger, student, at)
        rows.append(("single", trigger.value, body))

    # The sibling case: the one most likely to overflow a segment.
    sibling_rows = [
        {"body": queue.render(Trigger.ARRIVAL, _fake_student("Juan", "Rizal"), at)},
        {"body": queue.render(Trigger.ARRIVAL, _fake_student("Maria", "Bonifacio"), at)},
    ]
    rows.append(("coalesced", "2 siblings", coalesce.render_group(sibling_rows)))

    for kind, label, body in rows:
        chars, segs = len(body), gsm7.segments(body)
        flag = "" if segs == 1 else f"  <-- {segs} SEGMENTS, costs {segs}x"
        if segs != 1:
            ok = False
        print(f"\n  [{kind}/{label}]  {chars} chars, {segs} segment{flag}")
        print(f"  {body}")

        bad = gsm7.offenders(body)
        if bad:
            ok = False
            print("  NOT GSM-7:")
            for char, why in bad:
                print(f"      {char!r}  {why}")

    print()
    return ok


# -- stage 3: the only part that costs money --------------------------------

def send_one(config, recipient: str) -> bool:
    heading("3. LIVE SEND  (this costs ~P0.35)")

    from trackify.notify.philsms import PhilSMSProvider

    provider = PhilSMSProvider(
        config.secrets.philsms_api_token, config.secrets.philsms_sender_id
    )

    body = queue.render(
        Trigger.ARRIVAL, _fake_student("Juan", "Rizal"), datetime.now()
    )
    print(f"  to      {mobile.for_display(recipient)}  ({recipient})")
    print(f"  from    {provider.sender_id}")
    print(f"  body    {body}")
    print(f"          {len(body)} chars, {gsm7.segments(body)} segment\n")

    try:
        result = provider.send(recipient, body)
    finally:
        provider.close()

    if result.ok:
        print(f"  SENT.  provider message id: {result.provider_message_id}")
        print("\n  Check the handset. If nothing arrives within a minute or two, the")
        print("  message was accepted by PhilSMS but dropped by the telco -- usually an")
        print("  unapproved sender ID. Look at the dashboard's delivery log.")
        return True

    # The failure path is the reason this script exists: say which failure it was.
    if result.ambiguous:
        print(f"  AMBIGUOUS: {result.error}")
        print("\n  The request was written but the outcome is unknown. It may have been")
        print("  delivered. Do not simply re-run -- check the PhilSMS dashboard first.")
    else:
        print(f"  FAILED: {result.error}")
        print("\n  The error body above is PhilSMS telling you which of these it is:")
        print("    - token wrong or expired      -> PHILSMS_API_TOKEN in .env")
        print("    - sender ID not approved      -> register it in the dashboard, or")
        print("                                     use your own number as the sender")
        print("    - no credits                  -> top up")
        print("    - recipient malformed         -> must be 639XXXXXXXXX")
    return False


# -- wiring -----------------------------------------------------------------

def resolve_recipient(raw: str) -> str | None:
    if not raw.strip():
        print("\nTEST_RECIPIENT is empty.")
        print(f"Open {Path(__file__).name} and put your mobile number in it, near the top.")
        return None
    try:
        number = mobile.normalise(raw)
    except mobile.InvalidMobile as exc:
        print(f"\nTEST_RECIPIENT {raw!r} is not a valid PH mobile number: {exc}")
        return None
    if number is None:
        print(f"\nTEST_RECIPIENT {raw!r} did not normalise to a number.")
        return None
    return number


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="PhilSMS live test")
    parser.add_argument("--send", action="store_true",
                        help="actually send one SMS (costs ~P0.35)")
    parser.add_argument("--check-only", action="store_true",
                        help="stage 1 only")
    parser.add_argument("--preview-only", action="store_true",
                        help="stage 2 only, no network at all")
    args = parser.parse_args(argv)

    config = load_config()

    if args.preview_only:
        return 0 if preview() else 1

    token = config.secrets.philsms_api_token
    sender_id = config.secrets.philsms_sender_id

    if not token:
        print("PHILSMS_API_TOKEN is not set in .env.")
        print("Copy it from the PhilSMS dashboard, then re-run.")
        return 1

    healthy = check(token, sender_id)
    if args.check_only:
        return 0 if healthy else 1

    clean = preview()

    if not args.send:
        print(RULE)
        print("Nothing was sent. Re-run with --send to deliver one real message.")
        return 0 if (healthy and clean) else 1

    if not healthy:
        print(RULE)
        print("The account check failed above. Fix that before spending a credit.")
        return 1
    if not sender_id:
        print(RULE)
        print("PHILSMS_SENDER_ID is not set -- see the note above. Nothing sent.")
        return 1

    recipient = resolve_recipient(TEST_RECIPIENT)
    if recipient is None:
        return 1

    return 0 if send_one(config, recipient) else 1


if __name__ == "__main__":
    raise SystemExit(main())
