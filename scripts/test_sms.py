"""Live SMS test for the SIM800C module. Edit TEST_RECIPIENT below, then run this.

    python scripts/test_sms.py                # stages 1 and 2 -- sends nothing
    python scripts/test_sms.py --send         # stage 3: one real SMS
    python scripts/test_sms.py --scan         # which networks this SIM may use

Staged, because the failures are otherwise indistinguishable. From inside the kiosk,
"no SIM", "not registered", "no signal", "blank SMS centre" and "the module is browning
out under the transmit burst" all present as the same AT error.

    1  --check    Opens the port, runs the init sequence, prints module identity,
                  SUPPLY VOLTAGE, signal, registration and SMS centre. No SMS.
    2  --preview  Renders every real template offline and validates GSM-7. No port.
    3  --send     One message, to TEST_RECIPIENT only, allowlist enforced.

The serial port is exclusive: this script and the kiosk cannot both hold it. If the
kiosk is running, close it first.
"""
from __future__ import annotations

import argparse
import sys
import time
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
from trackify.notify.gsm import REGISTRATION, GsmError, GsmProvider, find_port

RULE = "-" * 68


def _operator_name(cops: str) -> str:
    """+COPS: 0,0,"SMART" -> SMART. Bare "+COPS: 0" means no operator attached."""
    fields = [f.strip().strip('"') for f in cops.split(",")]
    return fields[2] if len(fields) >= 3 and fields[2] else "-"


def heading(text: str) -> None:
    print(f"\n{text}\n{RULE}")


# -- stage 1: can this module send at all ------------------------------------

def check(provider: GsmProvider) -> bool:
    heading("1. MODULE CHECK  (no SMS sent)")

    try:
        health = provider.health(refresh=True)
    except GsmError as exc:
        print(f"  {exc}")
        return False

    print(f"  port       {health.port}")
    print(f"  module     {health.identity}  (firmware {health.firmware})")
    print(f"  SIM        {health.sim or 'no answer'}")

    # Printed even when healthy. Idle voltage looking fine proves little -- the sag
    # happens during the transmit burst -- but a low reading here settles it instantly.
    volts = f"{health.voltage_mv} mV" if health.voltage_mv else "unknown"
    flag = "  <-- TOO LOW, see below" if health.power_suspect else ""
    print(f"  supply     {volts}{flag}")

    dbm = f" ({health.signal_dbm} dBm)" if health.signal_dbm is not None else ""
    print(f"  signal     {health.signal}{dbm}  {health.signal_label}")
    # `or -1` would be wrong here: state 0 ("not registered, not searching") is falsy
    # and would print as "unknown", hiding the single most useful diagnostic.
    reg = ("unknown" if health.registration is None
           else REGISTRATION.get(health.registration, str(health.registration)))
    print(f"  network    {reg}   operator {_operator_name(health.operator)}")
    print(f"  SMS centre {health.smsc or 'NOT SET'}")
    if health.storage_total:
        print(f"  storage    {health.storage_used}/{health.storage_total} messages")

    blocker = health.blocker()
    if blocker is None:
        print("\n  Ready to send.")
        return True

    print(f"\n  BLOCKED: {blocker}")

    sim_ready = "READY" in (health.sim or "").upper()
    if not sim_ready and not health.power_suspect:
        print("\n  The module itself is fine -- it answered, and a signal is showing.")
        print("  Reseat the SIM: check it is the right way round, pushed fully")
        print("  home, and that it is the size the tray expects. Then re-run.")
    elif not health.registered and not health.power_suspect:
        print("\n  The module and antenna are working if a signal is showing above.")
        print("  A SIM that cannot register is usually one of these, in order:")
        print("    - not registered under the SIM Registration Act -> deactivated")
        print("    - expired: prepaid SIMs deactivate after months without use")
        print("    - no load")
        print("  Fastest test by far: put the SIM in an ordinary phone. If it cannot")
        print("  register there either, the problem is the SIM, not this project.")
        print("  Run --scan to see whether the network is refusing this SIM outright.")
    return False


def scan(provider: GsmProvider) -> bool:
    """Which networks are visible, and may this SIM use them.

    This is what distinguishes "no coverage" from "the network is refusing this SIM".
    AT+CREG cannot tell those apart, and they need completely different fixes.
    """
    heading("NETWORK SCAN  (up to 2 minutes)")
    print("  Asking the module which networks it can see...\n")

    try:
        networks = provider.scan_networks()
    except (GsmError, OSError) as exc:
        print(f"  scan failed: {exc}")
        return False

    if not networks:
        print("  No networks found. With 2G being phased out, that may mean there is")
        print("  no 2G service in range at all.")
        return False

    for name, verdict in networks:
        print(f"  {name:<18} {verdict.upper()}")

    if any(v == "forbidden" for _, v in networks):
        print("\n  FORBIDDEN means the network can see this SIM and is refusing it.")
        print("  That is a SIM problem, not a coverage or module problem:")
        print("    - not registered under the SIM Registration Act -> deactivated")
        print("    - expired, barred, or never activated")
        print("    - no load")
        print("  Confirm by putting the SIM in an ordinary phone.")
    return True


def inbox(provider: GsmProvider) -> bool:
    """Messages received on the SIM.

    The practical use: text this SIM from your own handset, then run this. The
    sender column is your number exactly as the network reports it, which settles
    any doubt about digits far better than reading it off a screen.
    """
    heading("SIM INBOX")

    try:
        messages = provider.read_inbox()
    except (GsmError, OSError) as exc:
        print(f"  could not read: {exc}")
        return False

    if not messages:
        print("  No messages on the SIM.")
        print(f"  Text {_own_number(provider)} from your phone, then run this again.")
        return False

    for m in messages:
        print(f"  [{m['index']}] {m['status']}  from {m['sender']}  {m['received']}")
        print(f"      {m.get('body', '')}" + chr(10))
    return True


def _own_number(provider: GsmProvider) -> str:
    """The SIM's own MSISDN via AT+CNUM. Often blank on PH prepaid SIMs."""
    try:
        raw = provider._command("AT+CNUM", 5.0)
    except Exception:
        return "the module's number"
    for line in raw.splitlines():
        if "+CNUM:" in line:
            parts = [f.strip().strip(chr(34)) for f in line.split(',')]
            if len(parts) > 1 and parts[1]:
                return parts[1]
    return "the module's number"


# -- stage 2: what the guardian actually receives ----------------------------

def _fake_student(first: str, section: str) -> dict:
    """queue.render only ever subscripts the row, so a dict stands in for one."""
    return {"first_name": first, "section_name": section}


def preview() -> bool:
    """The real templates, rendered offline. No port, no SMS.

    Bodies come from notify/queue and notify/coalesce rather than being retyped here. A
    preview of a message the product does not send proves nothing.
    """
    heading("2. MESSAGE PREVIEW  (offline, no SMS sent)")

    at_time = datetime(2026, 8, 24, 7, 12)
    ok = True

    rows = [("single", t.value, queue.render(t, _fake_student("Juan", "Rizal"), at_time))
            for t in Trigger]
    siblings = [
        {"body": queue.render(Trigger.ARRIVAL, _fake_student("Juan", "Rizal"), at_time)},
        {"body": queue.render(Trigger.ARRIVAL, _fake_student("Maria", "Bonifacio"), at_time)},
    ]
    rows.append(("coalesced", "2 siblings", coalesce.render_group(siblings)))

    for kind, label, body in rows:
        segs = gsm7.segments(body)
        flag = "" if segs == 1 else f"  <-- {segs} SEGMENTS"
        if segs != 1:
            ok = False
        print(f"\n  [{kind}/{label}]  {len(body)} chars, {segs} segment{flag}")
        print(f"  {body}")

        bad = gsm7.offenders(body)
        if bad:
            ok = False
            print("  NOT GSM-7:")
            for char, why in bad:
                print(f"      {char!r}  {why}")

    print("\n  Note: guardians will see the SIM's own number as the sender, not a")
    print("  sender ID. The 'TRACKIFY:' prefix is what identifies the school.")
    return ok


# -- stage 3: the only part that sends anything ------------------------------

def send_one(provider: GsmProvider, recipient: str, text: str | None = None) -> bool:
    heading("3. LIVE SEND")

    # Default to a real template rather than "test": it exercises the length and the
    # GSM-7 charset the product actually uses.
    body = text or queue.render(
        Trigger.ARRIVAL, _fake_student("Juan", "Rizal"), datetime.now()
    )
    print(f"  to      {mobile.for_display(recipient)}  ({recipient})")
    print(f"  body    {body}")
    print(f"          {len(body)} chars, {gsm7.segments(body)} segment")
    print("\n  Sending. A 2G submit takes 3-10 seconds...\n")

    started = time.monotonic()
    result = provider.send(recipient, body)
    elapsed = time.monotonic() - started

    if result.ok:
        print(f"  SENT in {elapsed:.1f}s.  reference: {result.provider_message_id}")
        print("\n  Check the handset. The reference is a modem-local counter that wraps")
        print("  at 255 -- it is for the log, and cannot be reconciled with anything.")
        return True

    if result.ambiguous:
        print(f"  AMBIGUOUS after {elapsed:.1f}s: {result.error}")
        print("\n  The message may already have been submitted. Do NOT simply re-run:")
        print("  check the handset first, or a parent gets the same text twice. This is")
        print("  the case the queue parks as 'unknown' rather than retrying.")
    else:
        print(f"  FAILED after {elapsed:.1f}s: {result.error}")
    return False


# -- wiring -----------------------------------------------------------------

def resolve_recipient(raw: str, config) -> str | None:
    if not raw.strip():
        print(f"\nTEST_RECIPIENT is empty. Put your number near the top of "
              f"{Path(__file__).name}.")
        return None
    try:
        number = mobile.normalise(raw)
    except mobile.InvalidMobile as exc:
        print(f"\nTEST_RECIPIENT {raw!r} is not a valid PH mobile number: {exc}")
        return None
    if number and not config.secrets.allows(number):
        print(f"\n{number} is not on SMS_ALLOWLIST in .env, so it will not be texted.")
        print("That is the safety net working. Add it there if it really is your number.")
        return None
    return number


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="SIM800C live SMS test")
    parser.add_argument("--send", action="store_true", help="actually send one SMS")
    parser.add_argument("--to", default=None, metavar="NUMBER",
                        help="recipient for --send, overriding TEST_RECIPIENT")
    parser.add_argument("--text", default=None, metavar="BODY",
                        help="message body for --send (default: the arrival template)")
    parser.add_argument("--inbox", action="store_true",
                        help="list messages received on the SIM, with sender numbers")
    parser.add_argument("--check-only", action="store_true", help="stage 1 only")
    parser.add_argument("--preview-only", action="store_true",
                        help="stage 2 only -- no serial port touched")
    parser.add_argument("--scan", action="store_true",
                        help="list visible networks and whether this SIM may use them")
    parser.add_argument("--port", default=None, help="override the serial port")
    args = parser.parse_args(argv)

    config = load_config()

    if args.preview_only:
        return 0 if preview() else 1

    port = args.port or config.gsm.port or find_port()
    if not port:
        print("No serial port found. Is the SIM800C plugged in?")
        return 1

    try:
        provider = GsmProvider(
            port, baud=config.gsm.baud,
            send_timeout=config.gsm.send_timeout_s,
            init_timeout=config.gsm.init_timeout_s,
            # Reading the inbox means NOT wiping it during init.
            clear_storage=not args.inbox,
        )
    except GsmError as exc:
        print(exc)
        return 1

    try:
        if args.inbox:
            return 0 if inbox(provider) else 1
        if args.scan:
            return 0 if scan(provider) else 1

        healthy = check(provider)
        if args.check_only:
            return 0 if healthy else 1

        clean = preview()

        if not args.send:
            print(f"\n{RULE}\nNothing was sent. Re-run with --send to deliver one message.")
            return 0 if (healthy and clean) else 1

        if not healthy:
            print(f"\n{RULE}\nThe module check failed above. Fix that first.")
            return 1

        recipient = resolve_recipient(args.to or TEST_RECIPIENT, config)
        if recipient is None:
            return 1
        return 0 if send_one(provider, recipient, args.text) else 1
    finally:
        provider.close()


if __name__ == "__main__":
    raise SystemExit(main())
