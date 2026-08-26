"""Starting the kiosk when the notification hardware is not there.

The kiosk builds its provider before window.show(), so anything that raises on the way
means the operator gets no screen at all -- and on a school morning the failure looks
like "the attendance system is broken", not "the SMS module is unplugged". Every other
subsystem here already degrades instead of crashing; these tests hold the provider to
the same standard.
"""

import pytest

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
