"""SMS over a SIM800C GSM module, spoken to as AT commands on a USB serial port.

Replaces the PhilSMS HTTP provider. The NotificationProvider abstraction is what makes
that a one-file change: the queue, guardian coalescing, the spend breaker, the QThread
worker and the kiosk are all untouched.

What a serial modem changes, and none of it is cosmetic:

  Slow          A 2G submit takes 3-10 seconds, against ~0.5s for an HTTP call. The queue
                is asynchronous so the kiosk never waits, but a morning rush drains for
                tens of minutes and the unsent counter visibly lags.

  No sender ID  Guardians see the SIM's own number. The body already opens with
                "TRACKIFY:", which now carries the whole burden of saying who sent it.

  No message id that means anything
                +CMGS returns a reference in 0..255 that wraps, and there is no dashboard
                to reconcile it against. It is stored for the log and nothing more.

  Ambiguity is common, not rare
                The module browns out when the supply cannot deliver the ~2A transmit
                burst. If that happens after Ctrl-Z, the message may well have been
                submitted. Those are reported ambiguous so the queue parks them as
                'unknown' rather than risking a duplicate text to a parent.

  2G only       SIM800C has no 3G or LTE radio. Under NTC MC 002-09-2025 2G is being
                phased out and 2G-only devices can no longer be type-approved or
                imported. Fine for the study; a known expiry for the school deployment.
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass

from .provider import Availability, NotificationProvider, SendResult

try:
    import serial
    from serial.tools import list_ports
except ImportError:                                   # degrade, never crash
    serial = None
    list_ports = None

# The USB-UART bridge on the LC SIM800C V3 board. Matching on VID:PID rather than a port
# name means the same code finds COM3 on Windows and /dev/ttyUSB0 on the Pi.
CH340_VID, CH340_PID = 0x1A86, 0x7523

CTRL_Z = b"\x1a"
ESC = b"\x1b"

# A working SIM800C answers a bare AT in milliseconds. Two seconds is already generous,
# and the difference matters: _read_until never returns early on silence, so without this
# probe the init sequence spends init_timeout on each of ~13 commands -- over two minutes
# against a port that is never going to answer.
PROBE_TIMEOUT = 2.0

# Tries within that same budget, not on top of it -- so a port that is never going to
# answer still costs PROBE_TIMEOUT and no more.
#
# One AT is too fragile a handshake on Linux. Opening the CH340 bridge toggles DTR/RTS,
# and on the Pi the first read after that came back as a single framing-error byte
# (b'\xe0') with the AT lost behind it -- the module answered perfectly on the second
# try, and on every open afterwards. A one-shot probe therefore declared a working
# SIM800C dead on the first open after boot, which is the only open that matters at a
# school gate. Windows' CH340 driver settles the lines differently and never showed it.
PROBE_ATTEMPTS = 3

# How often available() is allowed to touch the hardware while the module is missing.
# The worker ticks every 4s; probing every tick would be pointless traffic.
RECHECK_SECONDS = 15.0

NO_PORT = ("No serial port found. Is the SIM800C plugged in? "
           "Run: python scripts/test_sms.py --check")
NO_PYSERIAL = "pyserial is not installed -- run: pip install pyserial"

# +CREG <stat>. 1 and 5 are the only two that mean a message can leave.
REGISTRATION = {
    0: "not registered, not searching",
    1: "registered (home)",
    2: "searching for a network",
    3: "registration denied",
    4: "unknown",
    5: "registered (roaming)",
}

# Below this the module is browning out rather than misbehaving. The SIM800C wants
# 3.4-4.4V and pulls up to 2A while transmitting; a laptop USB port supplies 0.5-0.9A.
BROWNOUT_MV = 3600


class GsmError(RuntimeError):
    """The module could not be brought to a state where it could send."""


@dataclass(frozen=True)
class ModemHealth:
    """One snapshot of whether this thing can actually send a text.

    Exists because the failures are otherwise indistinguishable: no SIM, not registered,
    no signal, blank SMS centre and a failing power supply all present as 'AT+CMGS
    returned ERROR'.
    """

    port: str = ""
    identity: str = ""
    firmware: str = ""
    sim: str = ""
    voltage_mv: int | None = None
    signal: int | None = None            # 0-31; 99 means unknown/undetectable
    registration: int | None = None
    operator: str = ""
    smsc: str = ""
    storage_used: int | None = None
    storage_total: int | None = None

    @property
    def registered(self) -> bool:
        return self.registration in (1, 5)

    @property
    def signal_dbm(self) -> int | None:
        if self.signal is None or self.signal >= 99:
            return None
        return -113 + 2 * self.signal

    @property
    def signal_label(self) -> str:
        if self.signal is None or self.signal >= 99:
            return "none"
        if self.signal <= 9:
            return "marginal"
        if self.signal <= 14:
            return "usable"
        if self.signal <= 19:
            return "good"
        return "excellent"

    @property
    def power_suspect(self) -> bool:
        return self.voltage_mv is not None and self.voltage_mv < BROWNOUT_MV

    @property
    def storage_full(self) -> bool:
        if self.storage_used is None or not self.storage_total:
            return False
        return self.storage_used >= self.storage_total

    def blocker(self) -> str | None:
        """The one thing stopping a send, phrased so it points at the real fix."""
        if self.power_suspect:
            return (f"supply is {self.voltage_mv} mV, below {BROWNOUT_MV} mV -- this is a "
                    "power problem, not a software one. Feed VBAT from a supply that can "
                    "deliver 2A rather than from a laptop USB port")
        if not self.sim:
            return ("could not read the SIM status at all -- the module answered, so "
                    "check the SIM is seated and the tray is the right way round")
        if "READY" not in self.sim.upper():
            return f"SIM is not usable: {self.sim}"
        if not self.registered:
            state = REGISTRATION.get(self.registration, "unknown")
            # Ordered by what actually turns out to be wrong. A SIM the network can see
            # and refuses looks identical here to one with no coverage, which is what
            # scan_networks() exists to separate.
            return (f"not registered on a network ({state}). Usually the SIM rather than "
                    "the module: unregistered under the SIM Registration Act, expired, "
                    "barred, or out of load. Try it in an ordinary phone")
        if self.signal is None or self.signal >= 99:
            return "no measurable signal -- check the antenna is attached"
        if not self.smsc:
            return ("no SMS centre number set. Every send fails silently without one; "
                    'set it with AT+CSCA="+639...",145')
        return None


def find_port() -> str | None:
    """The CH340 bridge on the SIM800C board, or the first serial port as a fallback."""
    if list_ports is None:
        return None
    ports = list(list_ports.comports())
    for port in ports:
        if port.vid == CH340_VID and port.pid == CH340_PID:
            return port.device
    return ports[0].device if ports else None


def _digits(recipient: str) -> str:
    """639171234567 -> +639171234567, which is what AT+CMGS wants."""
    cleaned = "".join(c for c in recipient if c.isdigit())
    return "+" + cleaned


class GsmProvider(NotificationProvider):
    name = "gsm"

    def __init__(
        self,
        port: str | None = None,
        *,
        baud: int = 115200,
        send_timeout: float = 60.0,
        init_timeout: float = 10.0,
        clear_storage: bool = True,
        serial_factory=None,
    ) -> None:
        self.baud = baud
        self.send_timeout = send_timeout
        self.init_timeout = init_timeout
        self.clear_storage = clear_storage
        self._factory = serial_factory
        self._serial = None
        self._health: ModemHealth | None = None
        self._last_check = 0.0
        self._last_result: Availability | None = None

        # Constructing this object must never fail because the hardware is absent. The
        # kiosk builds its provider before window.show(), so a raise here means the
        # operator gets no screen at all -- a missing module has to be a status they can
        # see, not a traceback in a terminal nobody is looking at. Both conditions are
        # re-checked in _open(), which is where they turn into GsmError.
        self._missing = None
        if serial is None and serial_factory is None:
            self._missing = NO_PYSERIAL

        # Deliberately not remembered as final: a module plugged in after startup is
        # found the next time _open() runs.
        self.port = port or find_port()

    # -- port ---------------------------------------------------------------

    def _open(self):
        if self._serial is not None:
            return self._serial
        if self._missing:
            raise GsmError(self._missing)
        if not self.port:
            # Looked up again rather than trusted from __init__, so a module plugged in
            # after the kiosk started is picked up without a restart.
            self.port = find_port()
        if not self.port:
            raise GsmError(NO_PORT)
        if self._factory is not None:
            self._serial = self._factory()
        else:
            try:
                self._serial = serial.Serial(self.port, self.baud, timeout=1.0)
            except Exception as exc:
                # A serial port is exclusive: the kiosk and the test script cannot both
                # hold it, and that is by far the likeliest reason to land here.
                raise GsmError(
                    f"could not open {self.port}: {exc}. "
                    "If the kiosk is running it already holds the port."
                ) from exc
            time.sleep(0.3)
        self._initialise()
        return self._serial

    def close(self) -> None:
        if self._serial is not None:
            # Best effort. close() is what a mid-send brownout calls, so the port may
            # already be gone; failing to close a port that no longer exists must not
            # stop the rest of this teardown.
            with contextlib.suppress(Exception):
                self._serial.close()
            self._serial = None
            self._health = None
            # A cached "available" would outlive the port it described -- close() is
            # what a mid-send brownout calls, and that is exactly when the bar must
            # stop claiming the module is fine.
            self._last_result = None

    # -- AT plumbing --------------------------------------------------------

    def _write(self, data: bytes) -> None:
        self._serial.write(data)
        flush = getattr(self._serial, "flush", None)
        if flush:
            flush()

    def _read_until(self, terminators: tuple[bytes, ...], timeout: float) -> bytes:
        """Collect bytes until one of `terminators` appears, or time runs out.

        Line-oriented parsing would be neater and wrong: the '>' prompt AT+CMGS returns
        is not followed by a newline, so waiting for one hangs until the timeout.
        """
        deadline = time.monotonic() + timeout
        buffer = b""
        while time.monotonic() < deadline:
            chunk = self._serial.read(256)
            if chunk:
                buffer += chunk
                if any(t in buffer for t in terminators):
                    return buffer
            else:
                time.sleep(0.02)
        return buffer

    def _command(self, cmd: str, timeout: float = 5.0) -> str:
        reset = getattr(self._serial, "reset_input_buffer", None)
        if reset:
            reset()
        self._write((cmd + "\r\n").encode("ascii"))
        raw = self._read_until(
            (b"OK\r\n", b"ERROR\r\n", b"+CME ERROR", b"+CMS ERROR"), timeout
        )
        return raw.decode("utf8", "replace")

    # -- initialisation -----------------------------------------------------

    def _probe(self) -> None:
        """Confirm something is actually answering before spending the init sequence.

        On Windows find_port() falls back to the first COM port, which is usually a
        Bluetooth virtual port rather than the module. Without this check that port gets
        the full init sequence -- ~13 commands, each waiting out init_timeout in a
        _read_until that never returns early on silence, so roughly two minutes of the
        worker thread wedged on hardware that does not exist. A module that is there
        answers this in milliseconds.
        """
        # Never longer than the init timeout it exists to protect: a caller that has
        # already said "give up after n seconds" cannot mean "but wait longer than that
        # for the first byte". The attempts SHARE this budget rather than each getting
        # it, so tolerating a lost first byte costs a dead port nothing.
        budget = min(PROBE_TIMEOUT, self.init_timeout)
        for _ in range(PROBE_ATTEMPTS):
            if "OK" in self._command("AT", budget / PROBE_ATTEMPTS):
                return

        port = self.port
        self.close()
        raise GsmError(
            f"{port} did not answer AT in {PROBE_ATTEMPTS} tries within "
            f"{budget:.0f}s. Either it is a serial port that is not the module -- on "
            "Windows the first COM port is usually Bluetooth -- or the SIM800C is not "
            "powered: the CH340 bridge enumerates from USB alone, so the port exists "
            "whether or not the module behind it is alive. Check VBAT can supply 2A. "
            "Run: python scripts/test_sms.py --check"
        )

    def _initialise(self) -> None:
        """Bring the module to a known state. Every line here earns its place."""
        self._probe()
        # Echo off FIRST. With echo on, every command comes back before its reply and
        # parsing becomes guesswork.
        self._command("ATE0", self.init_timeout)
        # Verbose errors: '+CME ERROR: SIM not inserted' instead of a bare 'ERROR'.
        self._command("AT+CMEE=2", self.init_timeout)
        self._command("AT+CMGF=1", self.init_timeout)          # text mode, not PDU
        self._command('AT+CSCS="GSM"', self.init_timeout)      # matches notify/gsm7.py

        # Not housekeeping. Guardians can reply to the SIM's number and delivery reports
        # accumulate; once SIM storage fills, OUTGOING sends start failing. On a school
        # SIM that is weeks, not years. Clearing on open makes it a non-event.
        #
        # Optional only because it would otherwise wipe the inbox before read_inbox()
        # could look at it -- which is exactly what happened the first time.
        if self.clear_storage:
            self._command("AT+CMGD=1,4", self.init_timeout)

        self._health = self._read_health()

    def _read_health(self) -> ModemHealth:
        identity = _first_line(self._command("ATI", self.init_timeout))
        firmware = _after(self._command("AT+CGMR", self.init_timeout), "Revision:")
        # An errored reply must not read as an empty one. AT+CPIN? answers
        # "+CME ERROR: SIM not inserted" with no "+CPIN:" prefix, and treating that as
        # "unknown" hid a missing SIM behind the far vaguer "not registered".
        sim = _status_or_error(self._command("AT+CPIN?", self.init_timeout), "+CPIN:")
        operator = _after(self._command("AT+COPS?", self.init_timeout), "+COPS:")
        smsc_raw = _after(self._command("AT+CSCA?", self.init_timeout), "+CSCA:")
        smsc = smsc_raw.split(",")[0].strip().strip('"') if smsc_raw else ""

        voltage = None
        cbc = _after(self._command("AT+CBC", self.init_timeout), "+CBC:")
        parts = [p.strip() for p in cbc.split(",")] if cbc else []
        if len(parts) >= 3 and parts[2].isdigit():
            voltage = int(parts[2])

        signal = None
        csq = _after(self._command("AT+CSQ", self.init_timeout), "+CSQ:")
        if csq and csq.split(",")[0].strip().isdigit():
            signal = int(csq.split(",")[0].strip())

        registration = None
        creg = _after(self._command("AT+CREG?", self.init_timeout), "+CREG:")
        bits = [b.strip() for b in creg.split(",")] if creg else []
        if len(bits) >= 2 and bits[1].isdigit():
            registration = int(bits[1])

        used = total = None
        cpms = _after(self._command("AT+CPMS?", self.init_timeout), "+CPMS:")
        fields = [f.strip().strip('"') for f in cpms.split(",")] if cpms else []
        if len(fields) >= 3 and fields[1].isdigit() and fields[2].isdigit():
            used, total = int(fields[1]), int(fields[2])

        return ModemHealth(
            port=self.port, identity=identity, firmware=firmware, sim=sim,
            voltage_mv=voltage, signal=signal, registration=registration,
            operator=operator, smsc=smsc, storage_used=used, storage_total=total,
        )

    def health(self, *, refresh: bool = False) -> ModemHealth:
        self._open()
        if refresh or self._health is None:
            self._health = self._read_health()
        return self._health

    def available(self, *, now: float | None = None) -> Availability:
        """Is the module there and answering? Cheap enough for a 4-second tick.

        Three costs, in order: none at all once the port is open, none when there is no
        port to try, and at most one 2-second probe per RECHECK_SECONDS otherwise. The
        rate limit is what keeps a missing module from turning every tick into serial
        traffic -- the answer cannot change faster than someone can plug a cable in.

        This reports on the transport, not on the SIM. A module that is answering but
        has no signal or no load is 'available' here and refused by health().blocker()
        at send time, which is the check that can say exactly what is wrong.
        """
        if self._serial is not None:
            return Availability(ok=True)
        if self._missing:
            return Availability(ok=False, reason=self._missing)

        now = time.monotonic() if now is None else now
        if (self._last_result is not None
                and now - self._last_check < RECHECK_SECONDS):
            return self._last_result

        self._last_check = now
        try:
            # _open() re-resolves the port, probes, and turns every way this can fail
            # into one GsmError carrying a sentence worth showing someone. Exception
            # rather than GsmError because a status is never worth a crash: this runs
            # on the worker thread with the whole queue behind it.
            self._open()
        except Exception as exc:  # noqa: BLE001 - a status check is never worth a crash
            self._last_result = Availability(ok=False, reason=str(exc))
        else:
            self._last_result = Availability(ok=True)
        return self._last_result

    # -- sending ------------------------------------------------------------

    def send(self, recipient: str, body: str) -> SendResult:
        try:
            self._open()
        except GsmError as exc:
            return SendResult(ok=False, error=str(exc))

        # Checked before the message is written, so a refusal here is unambiguous: the
        # body never left the module and retrying cannot duplicate anything.
        #
        # Guarded because health() re-opens the port. Unplugged between passes, the
        # GsmError would otherwise leave send(), escape queue.drain -- which does not
        # wrap provider.send -- and abort the whole pass with the claimed row stranded
        # in 'sending', recoverable only by a restart and then only as 'unknown'.
        try:
            blocker = self.health(refresh=True).blocker()
        except GsmError as exc:
            return SendResult(ok=False, error=str(exc))
        if blocker:
            return SendResult(ok=False, error=blocker)

        try:
            reset = getattr(self._serial, "reset_input_buffer", None)
            if reset:
                reset()
            self._write(f'AT+CMGS="{_digits(recipient)}"\r'.encode("ascii"))

            prompt = self._read_until((b">", b"ERROR"), 10.0)
            if b">" not in prompt:
                # Never got the prompt, so the body was never written. Definite failure.
                detail = _error_text(prompt.decode("utf8", "replace"))
                self._abort()
                return SendResult(ok=False, error=f"no prompt from module: {detail}")

            # Past this point the module has the message. Everything below is ambiguous
            # on failure, because it may already have gone to the SMS centre.
            self._write(body.encode("ascii", "replace") + CTRL_Z)

            raw = self._read_until(
                (b"+CMGS:", b"ERROR\r\n", b"+CME ERROR", b"+CMS ERROR"),
                self.send_timeout,
            ).decode("utf8", "replace")

        except Exception as exc:  # noqa: BLE001 - any mid-send failure is ambiguous, see below
            # The port vanished mid-send: almost always a brownout reset. The message may
            # have been submitted first, so this must not be auto-retried.
            self.close()
            return SendResult(
                ok=False, ambiguous=True,
                error=f"module disconnected mid-send ({exc}) -- most likely a power "
                      "brownout during the transmit burst",
            )

        if "+CMGS:" in raw:
            reference = _after(raw, "+CMGS:").split(",")[0].strip()
            return SendResult(ok=True, provider_message_id=f"gsm-mr-{reference}")

        if "ERROR" in raw:
            # The module answered and said no. Definite, so safe to retry later.
            return SendResult(ok=False, error=_error_text(raw))

        # Silence after Ctrl-Z. The submit may have succeeded and the reply been lost.
        return SendResult(
            ok=False, ambiguous=True,
            error=f"no reply within {self.send_timeout:.0f}s of sending -- the message "
                  "may already have been submitted",
        )

    def _abort(self) -> None:
        """Escape a half-open AT+CMGS so the next send starts clean."""
        try:
            self._write(ESC)
            self._read_until((b"OK\r\n", b"ERROR\r\n"), 2.0)
        except Exception:  # noqa: BLE001 - best-effort escape; close() is the fallback
            self.close()

    def scan_networks(self) -> list[tuple[str, str]]:
        """AT+COPS=? -- every visible network and this SIM's standing with it.

        Worth having because AT+CREG cannot tell "no coverage" apart from "the network
        can see this SIM and is refusing it". The status field does:
        0 unknown, 1 available, 2 current, **3 forbidden**.

        Slow -- the module retunes across the whole band, which takes up to two minutes.
        Diagnostic only; never called on the sending path.
        """
        self._open()
        raw = self._command("AT+COPS=?", 125.0)

        verdicts = {"0": "unknown", "1": "available", "2": "current", "3": "forbidden"}
        found = []
        for chunk in raw.split("("):
            fields = chunk.split(",")
            if len(fields) >= 4 and fields[0].strip().isdigit():
                name = fields[1].strip().strip('"')
                found.append((name, verdicts.get(fields[0].strip(), fields[0].strip())))
        return found

    def read_inbox(self) -> list[dict]:
        """Messages sitting on the SIM: index, status, sender, timestamp, body.

        Not part of sending, and nothing in the product consumes replies. It is here
        because it answers a question nothing else can: what number is actually calling,
        as the network reports it. Guardians will reply to this SIM whether or not
        anyone reads it, so being able to look is worth the few lines.
        """
        self._open()
        raw = self._command('AT+CMGL="ALL"', 15.0)

        messages, header = [], None
        for line in _lines(raw):
            if line.startswith("+CMGL:"):
                fields = [f.strip().strip('"') for f in line[6:].split(",")]
                header = {
                    "index": fields[0] if fields else "",
                    "status": fields[1] if len(fields) > 1 else "",
                    "sender": fields[2] if len(fields) > 2 else "",
                    "received": ",".join(fields[4:]) if len(fields) > 4 else "",
                }
            elif header is not None and line not in ("OK", "ERROR"):
                header["body"] = line
                messages.append(header)
                header = None
        return messages

    def balance(self) -> int | None:
        """Always None. Prepaid load lives behind a USSD menu (*143#), not an API.

        The spend circuit breaker therefore stops guarding money and does one job: catch
        a loop bug. On an unli-text SIM it is the only brake there is.
        """
        return None


# -- response parsing -------------------------------------------------------

def _lines(raw: str) -> list[str]:
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _first_line(raw: str) -> str:
    for line in _lines(raw):
        if line not in ("OK", "ERROR"):
            return line
    return ""


def _after(raw: str, prefix: str) -> str:
    """The remainder of the first line carrying `prefix`."""
    for line in _lines(raw):
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return ""


def _status_or_error(raw: str, prefix: str) -> str:
    """The value after `prefix`, or the error the module gave instead.

    Returning "" for both "no answer" and "SIM not inserted" loses the single most
    useful fact in the reply.
    """
    value = _after(raw, prefix)
    if value:
        return value
    for line in _lines(raw):
        if "ERROR" in line:
            return line.split(":", 1)[-1].strip() if ":" in line else line
    return ""


def _error_text(raw: str) -> str:
    for line in _lines(raw):
        if "ERROR" in line:
            return line
    return raw.strip() or "unknown error"
