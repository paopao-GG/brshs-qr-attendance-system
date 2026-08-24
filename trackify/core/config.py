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
    low_balance_warn_at: int


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
class RiskConfig:
    mu_tardiness: float
    nu_early_departure: float
    band_low: float
    band_monitor: float
    band_elevated: float


@dataclass(frozen=True)
class Secrets:
    philsms_api_token: str = ""
    philsms_sender_id: str = ""
    qr_secret: str = ""

    @property
    def can_send_sms(self) -> bool:
        return bool(self.philsms_api_token and self.philsms_sender_id)


@dataclass(frozen=True)
class Config:
    school: SchoolConfig
    scanning: ScanningConfig
    notifications: NotificationConfig
    limits: LimitsConfig
    risk: RiskConfig
    camera: CameraConfig = field(default_factory=CameraConfig)
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
        secrets=Secrets(
            philsms_api_token=os.environ.get("PHILSMS_API_TOKEN", ""),
            philsms_sender_id=os.environ.get("PHILSMS_SENDER_ID", ""),
            qr_secret=os.environ.get("TRACKIFY_QR_SECRET", ""),
        ),
    )
