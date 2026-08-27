"""Configuration loading. Settings from config.toml, secrets from the environment."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from datetime import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "config.toml"


def _parse_time(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


@dataclass(frozen=True)
class SchoolConfig:
    name: str
    entry_open: time
    late_threshold: time
    dismissal_time: time
    early_departure_cutoff: time


@dataclass(frozen=True)
class ScanningConfig:
    debounce_minutes: int
    max_scans_per_day: int
    input_rate_limit_per_sec: int


@dataclass(frozen=True)
class NotificationConfig:
    policy: str
    coalesce_window_minutes: int
    retry_limit: int
    backoff_seconds: list[int]
    # Periodic notifications. Defaulted so a config.toml predating them still loads,
    # and defaulted OFF: turning on a job that texts every guardian should be a
    # decision someone made, not something that happens on upgrade.
    weekly_summary: bool = False
    absence_reminders: bool = False
    monthly_absence_limit: int = 3
    absence_warn_at: int = 2

    @property
    def notify_on_arrival(self) -> bool:
        return self.policy == "in_and_out"

    @property
    def notify_on_departure(self) -> bool:
        return self.policy == "in_and_out"


@dataclass(frozen=True)
class LimitsConfig:
    daily_message_cap: int
    per_recipient_daily_cap: int
    requests_per_second: float


@dataclass(frozen=True)
class GsmConfig:
    """SIM800C over USB serial. Defaults so a config.toml without [gsm] still loads."""

    port: str = ""              # "" = auto-detect by USB VID:PID; or COM3, /dev/ttyUSB0
    baud: int = 115200
    send_timeout_s: float = 60.0    # a 2G submit is genuinely this slow
    init_timeout_s: float = 10.0


@dataclass(frozen=True)
class CameraConfig:
    """Webcam scanning. Every field has a default so a config.toml written before
    the camera existed still loads -- see load_config."""

    enabled: bool = True
    device: str = ""            # substring of the camera name; "" = first available
    width: int = 1280
    height: int = 720
    decode_fps: int = 10        # decode attempts/sec; preview stays at native rate
    absence_frames: int = 5     # attempts a code must be gone before it may re-fire
    cooldown_ms: int = 1500     # floor; the kiosk raises it to the result hold time


@dataclass(frozen=True)
class ScreeningConfig:
    """Gate screening. Defaulted throughout so an older config.toml still loads."""

    enabled: bool = True
    # Printed on the screen so the guard knows what to put in the tray.
    declared_items_hint: str = "phone, laptop, tablet, tumbler, coins"

    # There is deliberately no timeout here. The screening prompt stays until a person
    # clicks: a screen that closes itself would record an outcome nobody chose. If a
    # timeout is ever reintroduced, read prohibited-items.md 5 first.


# Minimum band a confirmed prohibited-item incident forces, indexed by severity 1-4.
# Not a weighted criterion: one incident saturates to 0.2212, below the 0.30 Monitor
# cutoff, so no weight that sums to 1 with the others could raise a band on its own.
# See docs/prohibited-items.md section 9.
DEFAULT_INCIDENT_FLOOR = ("Monitor", "Monitor", "Elevated", "High")


@dataclass(frozen=True)
class RiskConfig:
    mu_tardiness: float
    nu_early_departure: float
    band_low: float
    band_monitor: float
    band_elevated: float
    # Defaulted, unlike the five above: those predate the section and are read by
    # direct subscripting, so a config.toml without [risk.incident_floor] must still
    # load rather than raising KeyError at startup. A tuple because RiskConfig is
    # frozen and severity is CHECK (severity BETWEEN 1 AND 4).
    incident_floor: tuple[str, str, str, str] = DEFAULT_INCIDENT_FLOOR
    # Who set the band cutoffs, and when. Empty means nobody has, and the export says
    # so -- a boundary decides whether a real student is referred, so an unattributed
    # one is a researcher's guess wearing an institution's authority.
    bands_set_by: str = ""
    bands_set_on: str = ""


@dataclass(frozen=True)
class Sf2Config:
    """Header and signature fields on the DepEd SF2 that live nowhere else.

    Everything else on the form is derived -- the section, the grade level, the days,
    the counts. These four are institutional facts the database has no column for, and
    guessing them would put an invented School ID on a document a principal signs.

    Every field defaults to "", and "" is printed as a blank line rather than as a
    placeholder: an unfilled SF2 is obviously unfinished, whereas one filled with
    plausible-looking defaults is quietly wrong.
    """

    school_id: str = ""
    school_year: str = ""
    # The adviser named here wins over sections.adviser_id, because seed_demo seeds
    # role placeholders ('Class Adviser') rather than people and a placeholder over a
    # signature line reads as a real name.
    adviser_name: str = ""
    school_head_name: str = ""


@dataclass(frozen=True)
class Secrets:
    qr_secret: str = ""
    # The station-wide switch on outbound SMS. True and texts really reach guardians;
    # false and nothing leaves the machine whatever else is configured.
    #
    # Together with consent_on_file this is the whole permission model: consent decides
    # WHICH students may be texted about, SMS_LIVE decides whether this station texts at
    # all. Spend caps still apply on top -- those are a runaway brake, not permission.
    #
    # Defaults FALSE, and that inversion is the point. It replaced SMS_ALLOWLIST, whose
    # empty value meant UNRESTRICTED -- so a .env that was missing a line texted every
    # guardian on the roster. A missing line must fail towards silence, not towards 81
    # real families. Lives in .env rather than config.toml because going live is a
    # property of one station on one morning, not of the source tree.
    sms_live: bool = False


@dataclass(frozen=True)
class Config:
    school: SchoolConfig
    scanning: ScanningConfig
    notifications: NotificationConfig
    limits: LimitsConfig
    risk: RiskConfig
    camera: CameraConfig = field(default_factory=CameraConfig)
    screening: ScreeningConfig = field(default_factory=ScreeningConfig)
    gsm: GsmConfig = field(default_factory=GsmConfig)
    sf2: Sf2Config = field(default_factory=Sf2Config)
    secrets: Secrets = field(default_factory=Secrets)


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader. Existing environment variables always win."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


# Anything else -- "false", "no", "0", a typo, an empty string, the key absent -- is
# False. A switch that decides whether real families get texted must not be turned on by
# a value somebody guessed at.
_TRUTHY = frozenset({"true", "1", "yes", "on"})


def _flag(raw: str) -> bool:
    """A .env boolean. Only the four spellings in _TRUTHY mean yes."""
    return raw.strip().lower() in _TRUTHY



def _incident_floor(section) -> tuple[str, str, str, str]:
    """[risk.incident_floor] as severity_1..severity_4, defaulted per level.

    Missing keys fall back individually rather than all-or-nothing, so a config that
    sets only severity_4 still gets sensible behaviour for the rest.
    """
    section = section or {}
    return tuple(
        section.get(f"severity_{level}", DEFAULT_INCIDENT_FLOOR[level - 1])
        for level in (1, 2, 3, 4)
    )

def load_config(path: Path | None = None) -> Config:
    path = path or DEFAULT_CONFIG
    with open(path, "rb") as handle:
        raw = tomllib.load(handle)

    _load_dotenv(PROJECT_ROOT / ".env")

    school = raw["school"]
    risk = raw["risk"]
    bands = risk["bands"]

    return Config(
        school=SchoolConfig(
            name=school["name"],
            entry_open=_parse_time(school["entry_open"]),
            late_threshold=_parse_time(school["late_threshold"]),
            dismissal_time=_parse_time(school["dismissal_time"]),
            early_departure_cutoff=_parse_time(school["early_departure_cutoff"]),
        ),
        scanning=ScanningConfig(**raw["scanning"]),
        # Merged over defaults rather than **raw["notifications"], so a config.toml
        # written before the periodic keys existed still loads.
        notifications=NotificationConfig(**{
            key: value for key, value in raw["notifications"].items()
            if key in {f.name for f in fields(NotificationConfig)}
        }),
        limits=LimitsConfig(**raw["limits"]),
        risk=RiskConfig(
            mu_tardiness=risk["mu_tardiness"],
            nu_early_departure=risk["nu_early_departure"],
            band_low=bands["low"],
            band_monitor=bands["monitor"],
            band_elevated=bands["elevated"],
            incident_floor=_incident_floor(risk.get("incident_floor")),
            bands_set_by=bands.get("set_by", ""),
            bands_set_on=bands.get("set_on", ""),
        ),
        # Merged over defaults rather than CameraConfig(**raw["camera"]), so a
        # config.toml predating the [camera] section still loads instead of raising
        # KeyError on startup at a school gate.
        camera=CameraConfig(**{
            k: v for k, v in raw.get("camera", {}).items()
            if k in CameraConfig.__dataclass_fields__
        }),
        screening=ScreeningConfig(**{
            k: v for k, v in raw.get("screening", {}).items()
            if k in ScreeningConfig.__dataclass_fields__
        }),
        gsm=GsmConfig(**{
            k: v for k, v in raw.get("gsm", {}).items()
            if k in GsmConfig.__dataclass_fields__
        }),
        sf2=Sf2Config(**{
            k: v for k, v in raw.get("sf2", {}).items()
            if k in Sf2Config.__dataclass_fields__
        }),
        secrets=Secrets(
            qr_secret=os.environ.get("TRACKIFY_QR_SECRET", ""),
            sms_live=_flag(os.environ.get("SMS_LIVE", "")),
        ),
    )
