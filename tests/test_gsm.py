"""SIM800C provider, driven against a fake serial port.

Nothing here opens COM3. The AT responses are the ones the real module actually returned
during bring-up -- `SIM800 R14.18`, `+CBC: 0,100,4213`, a Smart SMS centre -- so the
parsing is tested against real output rather than invented output.

The distinction that matters most, exactly as it did for the HTTP provider: a failure the
module reported is safe to retry, and a silence after Ctrl-Z is not, because the message
may already have reached the SMS centre. Getting that wrong means either a lost text or a
duplicate one to a parent.
"""

import time

import pytest

from trackify.notify.gsm import (
    RECHECK_SECONDS,
    GsmError,
    GsmProvider,
    ModemHealth,
    _digits,
    find_port,
)
from trackify.notify.provider import Availability, SendResult

# What the real module answered, verbatim.
HEALTHY = {
    "ATI": "SIM800 R14.18\r\nOK\r\n",
    "AT+CGMR": "Revision:1418B04SIM800C24\r\nOK\r\n",
    "AT+CPIN?": "+CPIN: READY\r\nOK\r\n",
    "AT+CBC": "+CBC: 0,100,4213\r\nOK\r\n",
    "AT+CSQ": "+CSQ: 12,0\r\nOK\r\n",
    "AT+CREG?": "+CREG: 0,1\r\nOK\r\n",
    "AT+COPS?": '+COPS: 0,0,"SMART"\r\nOK\r\n',
    "AT+CSCA?": '+CSCA: "+639934444400",145\r\nOK\r\n',
    "AT+CPMS?": '+CPMS: "SM_P",0,40,"SM_P",0,40,"SM_P",0,40\r\nOK\r\n',
}


class FakeSerial:
    """Replays canned AT responses and records what was written."""

    def __init__(self, responses=None, *, prompt=True, send_reply="+CMGS: 23\r\n\r\nOK\r\n",
                 explode_on_body=None):
        self.responses = dict(HEALTHY)
        self.responses.update(responses or {})
        self.prompt = prompt
        self.send_reply = send_reply
        self.explode_on_body = explode_on_body
        self.written = []
        self._pending = b""
        self._in_message = False
        self.closed = False

    # -- the pyserial surface the provider actually uses --

    def reset_input_buffer(self):
        self._pending = b""

    def flush(self):
        pass

    def close(self):
        self.closed = True

    def write(self, data):
        self.written.append(data)
        if self._in_message:
            if self.explode_on_body:
                raise self.explode_on_body
            if data.endswith(b"\x1a"):
                self._in_message = False
                self._pending = self.send_reply.encode()
            return len(data)

        text = data.decode("ascii", "replace").strip()
        if text.startswith("AT+CMGS="):
            self._in_message = True
            self._pending = b">" if self.prompt else b"+CMS ERROR: 304\r\n"
            if not self.prompt:
                self._in_message = False
            return len(data)
        self._pending = self.responses.get(text, "OK\r\n").encode()
        return len(data)

    def read(self, size=1):
        out, self._pending = self._pending[:size], self._pending[size:]
        return out


def provider_no_clear(**kwargs):
    """A provider that leaves SIM storage alone, as the --inbox path does."""
    fake = FakeSerial(**kwargs)
    gsm = GsmProvider("COM-TEST", serial_factory=lambda: fake, send_timeout=1.0,
                      init_timeout=1.0, clear_storage=False)
    return gsm, fake


def provider(**kwargs):
    fake = FakeSerial(**kwargs)
    gsm = GsmProvider("COM-TEST", serial_factory=lambda: fake, send_timeout=1.0,
                      init_timeout=1.0)
    return gsm, fake


# -- helpers ----------------------------------------------------------------

def test_recipient_is_converted_to_international_form():
    assert _digits("639171234567") == "+639171234567"


def test_find_port_returns_none_or_a_string():
    """Must not raise on a machine with no serial ports at all."""
    result = find_port()
    assert result is None or isinstance(result, str)


# -- health parsing ---------------------------------------------------------

def test_health_parses_the_real_module_output():
    gsm, _ = provider()
    health = gsm.health()

    assert health.identity == "SIM800 R14.18"
    assert health.firmware == "1418B04SIM800C24"
    assert health.sim == "READY"
    assert health.voltage_mv == 4213
    assert health.signal == 12
    assert health.signal_dbm == -89
    assert health.registration == 1
    assert health.smsc == "+639934444400"
    assert health.storage_used == 0 and health.storage_total == 40
    assert health.registered
    assert health.blocker() is None


def test_echo_is_disabled_before_anything_parsed():
    """With echo on, every command comes back before its reply and parsing is guesswork.

    The bare AT probe is allowed to precede it -- it only looks for OK in the reply, so
    echo cannot mislead it, and it is what stops a port that is not the module from
    costing the whole init sequence in timeouts.
    """
    gsm, fake = provider()
    gsm.health()                       # the port opens lazily, on first use
    sent = [w.decode().strip() for w in fake.written]

    assert sent[0] == "AT", "the liveness probe comes first"
    assert sent[1] == "ATE0", "and nothing is parsed before echo is off"


def test_stored_messages_are_cleared_on_open():
    """SIM storage fills with replies and delivery reports, and a full store makes
    OUTGOING sends fail. Clearing on open turns that from a bug into a non-event."""
    gsm, fake = provider()
    gsm.health()
    sent = [w.decode().strip() for w in fake.written]
    assert "AT+CMGD=1,4" in sent


# -- blockers ---------------------------------------------------------------

def test_unregistered_module_is_blocked_with_a_useful_reason():
    """Points at the SIM, not the module.

    This is what actually happened during bring-up: signal present, SIM READY, SMS
    centre set, and the network still refusing. A message blaming coverage would send
    the reader looking in entirely the wrong place.
    """
    gsm, _ = provider(responses={"AT+CREG?": "+CREG: 0,0\r\nOK\r\n"})
    blocker = gsm.health().blocker()
    assert blocker and "not registered" in blocker
    assert "SIM Registration Act" in blocker
    assert "ordinary phone" in blocker, "should name the fastest way to confirm"


def test_registration_denied_is_blocked():
    gsm, _ = provider(responses={"AT+CREG?": "+CREG: 0,3\r\nOK\r\n"})
    assert "denied" in gsm.health().blocker()


def test_locked_sim_is_blocked():
    gsm, _ = provider(responses={"AT+CPIN?": "+CPIN: SIM PIN\r\nOK\r\n"})
    assert "SIM is not usable: SIM PIN" in gsm.health().blocker()


def test_no_signal_is_blocked():
    gsm, _ = provider(responses={"AT+CSQ": "+CSQ: 99,99\r\nOK\r\n"})
    assert "signal" in gsm.health().blocker()


def test_blank_smsc_is_blocked_before_a_send_is_attempted():
    """Without an SMS centre number every send fails silently. Catch it up front."""
    gsm, _ = provider(responses={"AT+CSCA?": '+CSCA: "",145\r\nOK\r\n'})
    assert "SMS centre" in gsm.health().blocker()


def test_low_voltage_is_reported_as_power_not_software():
    """The whole point: a brownout looks like a bad SIM or a bad AT sequence."""
    gsm, _ = provider(responses={"AT+CBC": "+CBC: 0,20,3400\r\nOK\r\n"})
    blocker = gsm.health().blocker()

    assert "power problem" in blocker
    assert "2A" in blocker
    assert gsm.health().power_suspect


def test_voltage_takes_priority_over_every_other_blocker():
    """When the supply is failing, everything else is a symptom."""
    gsm, _ = provider(responses={
        "AT+CBC": "+CBC: 0,20,3400\r\nOK\r\n",
        "AT+CREG?": "+CREG: 0,0\r\nOK\r\n",
        "AT+CSQ": "+CSQ: 99,99\r\nOK\r\n",
    })
    assert "power problem" in gsm.health().blocker()


# -- sending ----------------------------------------------------------------

def test_successful_send_parses_the_message_reference():
    gsm, fake = provider()
    result = gsm.send("639171234567", "TRACKIFY: Juan (Rizal) arrived 7:12 AM.")

    assert result.ok
    assert result.provider_message_id == "gsm-mr-23"
    assert not result.ambiguous

    body = b"".join(fake.written)
    assert b'AT+CMGS="+639171234567"' in body
    assert body.endswith(b"\x1a"), "the message must be terminated with Ctrl-Z"


def test_send_is_refused_when_the_module_cannot_send():
    """Refused before the body is written, so retrying cannot duplicate anything."""
    gsm, fake = provider(responses={"AT+CREG?": "+CREG: 0,0\r\nOK\r\n"})
    result = gsm.send("639171234567", "hi")

    assert not result.ok
    assert not result.ambiguous
    assert b"AT+CMGS=" not in b"".join(fake.written)


def test_cms_error_is_a_definite_failure_not_ambiguous():
    gsm, _ = provider(send_reply="+CMS ERROR: 305\r\n")
    result = gsm.send("639171234567", "hi")

    assert not result.ok
    assert not result.ambiguous, "the module answered; nothing was submitted"
    assert "305" in result.error


def test_silence_after_ctrl_z_is_ambiguous():
    """The most important test here.

    The body has been handed to the module. If the reply never arrives, the message may
    already be at the SMS centre -- so this must never be auto-retried, or a parent gets
    the same text twice.
    """
    gsm, _ = provider(send_reply="")
    result = gsm.send("639171234567", "hi")

    assert not result.ok
    assert result.ambiguous
    assert "may already have been submitted" in result.error


def test_port_vanishing_mid_send_is_ambiguous():
    """A brownout during the transmit burst. Same reasoning as a timeout."""
    gsm, _ = provider(explode_on_body=OSError("device disconnected"))
    result = gsm.send("639171234567", "hi")

    assert not result.ok
    assert result.ambiguous
    assert "brownout" in result.error


def test_missing_prompt_is_a_definite_failure():
    """No '>' means the body was never written."""
    gsm, _ = provider(prompt=False)
    result = gsm.send("639171234567", "hi")

    assert not result.ok
    assert not result.ambiguous


def test_balance_is_always_none():
    """Prepaid load is a USSD menu, not an API. The spend breaker is the only brake."""
    gsm, _ = provider()
    assert gsm.balance() is None
    assert isinstance(gsm.send("639171234567", "x"), SendResult)


# -- health helpers ---------------------------------------------------------

def test_signal_labels_span_the_useful_range():
    assert ModemHealth(signal=99).signal_label == "none"
    assert ModemHealth(signal=5).signal_label == "marginal"
    assert ModemHealth(signal=12).signal_label == "usable"
    assert ModemHealth(signal=17).signal_label == "good"
    assert ModemHealth(signal=25).signal_label == "excellent"


def test_storage_full_is_detected():
    assert ModemHealth(storage_used=40, storage_total=40).storage_full
    assert not ModemHealth(storage_used=0, storage_total=40).storage_full


def test_missing_sim_is_reported_as_missing_not_as_unregistered():
    """Regression: AT+CPIN? answers '+CME ERROR: SIM not inserted' with no '+CPIN:'
    prefix. Parsing that as an empty string made health.sim falsy, the SIM check was
    skipped, and a physically absent SIM was reported as 'not registered on a network'
    -- true, useless, and pointing at the wrong thing entirely."""
    gsm, _ = provider(responses={
        "AT+CPIN?": "+CME ERROR: SIM not inserted\r\n",
        "AT+CSCA?": "+CME ERROR: SIM not inserted\r\n",
        "AT+CREG?": "+CREG: 0,0\r\nOK\r\n",
    })
    health = gsm.health()

    assert health.sim == "SIM not inserted"
    assert "SIM not inserted" in health.blocker()


def test_unreadable_sim_status_is_a_blocker():
    """An empty reply must not read as 'fine'."""
    gsm, _ = provider(responses={"AT+CPIN?": "OK\r\n"})
    assert "could not read the SIM status" in gsm.health().blocker()


def test_inbox_is_not_wiped_when_reading_it():
    """Regression: init runs AT+CMGD=1,4 to stop SIM storage filling up and blocking
    outgoing sends. With clear_storage left on, that deleted the messages before
    read_inbox() could ever see them, so the inbox always looked empty."""
    gsm, fake = provider_no_clear()
    gsm.health()
    assert "AT+CMGD=1,4" not in [w.decode().strip() for w in fake.written]


def test_inbox_parses_sender_and_body():
    listing = ('+CMGL: 1,"REC UNREAD","+639472698918","","26/08/24,20:47:42+32"\r\n'
               'self-check\r\nOK\r\n')
    gsm, _ = provider_no_clear(responses={'AT+CMGL="ALL"': listing})
    messages = gsm.read_inbox()

    assert len(messages) == 1
    assert messages[0]["sender"] == "+639472698918"
    assert messages[0]["body"] == "self-check"
    assert messages[0]["status"] == "REC UNREAD"


# --- the module is not plugged in -------------------------------------------
#
# The kiosk builds its provider before window.show(), so anything that raises here is a
# morning with no attendance system at all. A missing module has to be a status.


class SilentSerial(FakeSerial):
    """A port that opens and then says nothing.

    Not a hypothetical: find_port() falls back to the first COM port, and on Windows
    that is usually a Bluetooth virtual port rather than the module.
    """

    def write(self, data):
        self.written.append(data)
        return len(data)

    def read(self, size=1):
        return b""


def test_a_missing_port_is_a_status_not_a_crash(monkeypatch):
    monkeypatch.setattr("trackify.notify.gsm.find_port", lambda: None)
    gsm = GsmProvider()                    # must not raise

    result = gsm.available()
    assert result.ok is False
    assert "plugged in" in result.reason


def test_the_missing_port_message_still_names_the_fix(monkeypatch):
    """It moved from __init__ into _open(); the operator still needs the command."""
    monkeypatch.setattr("trackify.notify.gsm.find_port", lambda: None)

    with pytest.raises(GsmError) as excinfo:
        GsmProvider()._open()
    assert "scripts/test_sms.py --check" in str(excinfo.value)


def test_a_port_appearing_later_is_picked_up(monkeypatch):
    """The module is plugged in after the kiosk has started. Nobody restarts a kiosk
    mid-morning to make a cable work."""
    ports = []
    monkeypatch.setattr("trackify.notify.gsm.find_port",
                        lambda: ports[0] if ports else None)

    fake = FakeSerial()
    gsm = GsmProvider(serial_factory=lambda: fake)
    assert gsm.available(now=0.0).ok is False

    ports.append("COM-LATE")
    assert gsm.available(now=RECHECK_SECONDS + 1).ok is True
    assert gsm.port == "COM-LATE"


def test_a_silent_port_fails_fast_instead_of_timing_out():
    """The whole point of the probe. Thirteen init commands at init_timeout each is
    over two minutes of the worker thread wedged on hardware that is not there."""
    fake = SilentSerial()
    gsm = GsmProvider("COM-BLUETOOTH", serial_factory=lambda: fake,
                      init_timeout=10.0)

    started = time.monotonic()
    with pytest.raises(GsmError) as excinfo:
        gsm._open()
    elapsed = time.monotonic() - started

    assert elapsed < 5.0, f"took {elapsed:.1f}s -- the probe is not being reached"
    assert "did not answer AT" in str(excinfo.value)
    assert "COM-BLUETOOTH" in str(excinfo.value)


def test_a_silent_port_is_reported_unavailable_not_raised():
    gsm = GsmProvider("COM-BLUETOOTH", serial_factory=SilentSerial, init_timeout=10.0)

    result = gsm.available()
    assert result.ok is False
    assert "did not answer AT" in result.reason


def test_a_failed_probe_closes_the_port():
    """Left open, the next attempt would find _serial set and skip the probe -- and
    report a dead port as healthy for the rest of the day."""
    fake = SilentSerial()
    gsm = GsmProvider("COM-BLUETOOTH", serial_factory=lambda: fake, init_timeout=10.0)

    gsm.available()
    assert fake.closed
    assert gsm._serial is None


def test_available_does_no_io_once_the_port_is_open():
    """Called on every 4-second tick, so it has to stay free once the answer is known."""
    gsm, fake = provider()
    gsm.health()
    before = len(fake.written)

    assert gsm.available().ok is True
    assert len(fake.written) == before, "available() must not talk to the module"


def test_available_reprobes_at_most_every_fifteen_seconds():
    """Otherwise a missing module turns a 4s tick into constant serial traffic. The
    answer cannot change faster than someone can plug in a cable."""
    opens = []

    def factory():
        opens.append(1)
        return SilentSerial()

    gsm = GsmProvider("COM-BLUETOOTH", serial_factory=factory, init_timeout=1.0)

    for tick in range(0, 12):              # 12 ticks x 4s = 44 simulated seconds
        gsm.available(now=tick * 4.0)

    assert len(opens) == 3, f"probed {len(opens)} times in 44s, expected 3"


def test_a_health_failure_is_returned_not_raised(monkeypatch):
    """health() reopens the port. Unplugged between passes, that GsmError used to
    leave send(), escape queue.drain -- which does not wrap provider.send -- and strand
    the claimed row in 'sending' until a restart."""
    gsm, _ = provider()

    def boom(*args, **kwargs):
        raise GsmError("module disappeared")

    monkeypatch.setattr(gsm, "health", boom)
    result = gsm.send("639171234567", "hello")

    assert isinstance(result, SendResult)
    assert result.ok is False
    assert result.ambiguous is False, "nothing was written, so this is safe to retry"
    assert "module disappeared" in result.error


def test_a_working_module_reports_itself_available():
    gsm, _ = provider()
    assert gsm.available() == Availability(ok=True)
