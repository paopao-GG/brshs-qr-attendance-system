"""Configuration loading. Settings from config.toml, secrets from the environment."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class RiskConfig:
    mu_tardiness: float
    nu_early_departure: float
    band_low: float
    band_monitor: float
    band_elevated: float


@dataclass(frozen=True)
class Secrets:
    qr_secret: str = ""
    # Recipients allowed to receive a real text. EMPTY MEANS UNRESTRICTED, which is
    # correct for production and dangerous while testing: the demo roster holds 19
    # valid-format Philippine numbers, and an unli-text SIM has no cost brake to stop a
    # loop bug texting all of them. Lives in .env rather than config.toml so a personal
    # number never lands in git.
    allowlist: tuple[str, ...] = ()

    def allows(self, mobile: str) -> bool:
        return not self.allowlist or mobile in self.allowlist


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


def _allowlist(raw: str) -> tuple[str, ...]:
    """Comma-separated numbers, normalised so 09XX and 639XX both match what is stored."""
    from .mobile import InvalidMobile, normalise

    out = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            number = normalise(item)
        except InvalidMobile:
            continue
        if number:
            out.append(number)
    return tuple(out)


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
        notifications=NotificationConfig(**raw["notifications"]),
        limits=LimitsConfig(**raw["limits"]),
        risk=RiskConfig(
            mu_tardiness=risk["mu_tardiness"],
            nu_early_departure=risk["nu_early_departure"],
            band_low=bands["low"],
            band_monitor=bands["monitor"],
            band_elevated=bands["elevated"],
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
        secrets=Secrets(
            qr_secret=os.environ.get("TRACKIFY_QR_SECRET", ""),
            allowlist=_allowlist(os.environ.get("SMS_ALLOWLIST", "")),
        ),
    )
