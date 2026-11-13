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
    # Sentiment: the Claude model and effort level; the API key comes from ANTHROPIC_API_KEY.
    sentiment_model: str = "claude-haiku-5-5"
    sentiment_effort: str = "low"
    # Run the background sentiment worker inside the website (off in tests).
    ai_worker: bool = True
    # Comments older than this many days are deleted automatically (0 = keep forever).
    retention_days: int = 365
    # Backups: where they go (default data/backups) and how many to keep.
    backups: Path | None = None
    backup_keep: int = 12  # a year of monthly backups
    # Run hourly cleanup and the monthly backup inside the website (off in tests).
    maintenance: bool = True

    @property
    def db_path(self) -> Path:
        return self.data_dir / "monitor.db"

    @property
    def backup_dir(self) -> Path:
        return self.backups or self.data_dir / "backups"

    @property
    def tz(self) -> timezone:
        return timezone(timedelta(hours=self.utc_offset_hours))

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            data_dir=Path(os.environ.get("LCM_DATA_DIR", "data")),
            secure_cookies=_flag("LCM_SECURE_COOKIES", False),
            utc_offset_hours=int(os.environ.get("LCM_UTC_OFFSET", "8")),
            sentiment_model=os.environ.get("LCM_SENTIMENT_MODEL", "claude-haiku-5-5").strip(),
            sentiment_effort=os.environ.get("LCM_SENTIMENT_EFFORT", "low").strip(),
            ai_worker=_flag("LCM_AI_WORKER", True),
            retention_days=int(os.environ.get("LCM_RETENTION_DAYS", "365")),
            backups=Path(os.environ["LCM_BACKUP_DIR"]) if os.environ.get("LCM_BACKUP_DIR") else None,
            backup_keep=max(1, int(os.environ.get("LCM_BACKUP_KEEP", "12"))),
            maintenance=_flag("LCM_MAINTENANCE", True),
        )
