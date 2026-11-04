"""Settings, read from environment variables so the same code runs locally and on AWS."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta, timezone
from pathlib import Path


def _flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    # Send cookies only over HTTPS. Must be on in production; off for http://localhost.
    secure_cookies: bool = False
    # Philippine time has no daylight saving, so a fixed offset is exact.
    utc_offset_hours: int = 8
    session_days: int = 7
    max_failed_logins: int = 5
    lockout_minutes: int = 15
    # A running job whose agent has not reported for this long is put back in the queue.
    stale_job_minutes: int = 15

    @property
    def db_path(self) -> Path:
        return self.data_dir / "monitor.db"

    @property
    def tz(self) -> timezone:
        return timezone(timedelta(hours=self.utc_offset_hours))

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            data_dir=Path(os.environ.get("LCM_DATA_DIR", "data")),
            secure_cookies=_flag("LCM_SECURE_COOKIES", False),
            utc_offset_hours=int(os.environ.get("LCM_UTC_OFFSET", "8")),
        )
