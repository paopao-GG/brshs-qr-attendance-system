"""Starting the kiosk when the notification hardware is not there.

The kiosk builds its provider before window.show(), so anything that raises on the way
means the operator gets no screen at all -- and on a school morning the failure looks
like "the attendance system is broken", not "the SMS module is unplugged". Every other
subsystem here already degrades instead of crashing; these tests hold the provider to
the same standard.
"""


import app as app_module
from trackify.notify.provider import Availability, ConsoleProvider, NullProvider


def test_console_and_null_are_always_available():
    """They have no hardware to be missing. The gate must never hold them up."""
    assert ConsoleProvider().available() == Availability(ok=True)
    assert NullProvider().available() == Availability(ok=True)


def test_build_provider_never_raises_without_a_module(monkeypatch, config):
    """The reported bug: this call is what stopped the window from ever being shown."""
    monkeypatch.setattr("trackify.notify.gsm.find_port", lambda: None)

    provider = app_module.build_provider("gsm", config)

    assert provider.name == "gsm"
    assert provider.available().ok is False


def test_a_module_that_is_absent_reports_the_reason(monkeypatch, config):
    monkeypatch.setattr("trackify.notify.gsm.find_port", lambda: None)

    reason = app_module.build_provider("gsm", config).available().reason
    assert "SIM800C" in reason
    assert "scripts/test_sms.py --check" in reason, "tell them how to diagnose it"


def test_the_configured_port_and_timeouts_are_passed_through(config):
    import dataclasses

    cfg = dataclasses.replace(
        config, gsm=dataclasses.replace(config.gsm, port="COM9", baud=9600))
    provider = app_module.build_provider("gsm", cfg)

    assert provider.port == "COM9"
    assert provider.baud == 9600


def test_an_unbuildable_provider_never_falls_back_to_console():
    """Console reports ok=True and prints. Falling back to it would mark parent
    notifications sent when nothing was sent -- a lie in the attendance record, and
    worse than sending nothing."""
    import inspect

    source = inspect.getsource(app_module.main)
    fallback = source.split("could not be created")[1]
    assert "NullProvider" in fallback
    assert "ConsoleProvider" not in fallback


def test_only_the_real_transport_claims_to_send(config):
    """Each provider declares whether a send reaches a handset, and the kiosk status bar
    reads that rather than matching on names.

    The declaration lives on the provider because trackify/ui holds no domain logic
    (TDD.md section 4). A hardcoded ("console", "null") tuple in kiosk.py would pass
    today and be wrong the day a fourth provider is written by someone who never opens
    that file -- and the failure is silent: a bar confidently naming a transport that
    sends nothing.
    """
    from trackify.notify.provider import ConsoleProvider, NullProvider

    assert ConsoleProvider().sends_real_messages is False
    assert NullProvider().sends_real_messages is False
    assert app_module.build_provider("gsm", config).sends_real_messages is True


def test_the_probe_survives_a_lost_first_byte():
    """Opening the CH340 bridge on Linux toggles DTR/RTS, and the first read came back
    as a framing-error byte with the AT lost behind it -- so a one-shot probe declared a
    working SIM800C dead on the first open after boot, the only open that matters at a
    school gate.

    The retries share the probe budget rather than multiplying it, so a port that will
    never answer still costs PROBE_TIMEOUT and no more.
    """
    from trackify.notify.gsm import PROBE_ATTEMPTS, GsmProvider

    class Settling:
        """Silent on the first AT, answers from the second -- the observed behaviour."""

        def __init__(self):
            self.writes = 0
            self._pending = b""

        def reset_input_buffer(self):
            pass

        def write(self, data):
            self.writes += 1
            self._pending = b"\xe0" if self.writes == 1 else b"AT\r\r\nOK\r\n"
            return len(data)

        def read(self, _n=1):
            out, self._pending = self._pending, b""
            return out

        def close(self):
            pass

    port = Settling()
    provider = GsmProvider("/dev/fake", serial_factory=lambda: port, clear_storage=False)
    provider._serial = port
    provider._probe()                      # must not raise

    assert port.writes == 2, "the second AT is what should have succeeded"
    assert PROBE_ATTEMPTS >= 2, "one attempt cannot tolerate a lost first byte"
