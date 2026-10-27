"""Runtime configuration, read from environment variables only."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    token: str
    db: str = "./data/autoclock.db"
    tz: str = "UTC"
    debounce_min: float = 10
    max_session_h: float = 14
    ntfy_url: str = "https://ntfy.sh"
    ntfy_topic: str = ""
    ntfy_token: str | None = None
    public_url: str = "http://localhost:8000"
    remind_min: float = 0
    vapid_subject: str = "mailto:autoclock@example.invalid"


def _default_vapid_subject() -> str:
    # Apple's push service rejects placeholder contacts; an https URL you
    # control is accepted, so prefer the public URL when it is one.
    url = os.environ.get("AUTOCLOCK_PUBLIC_URL", "").rstrip("/")
    return url if url.startswith("https://") else "mailto:autoclock@example.invalid"


def load_settings() -> Settings:
    token = os.environ.get("AUTOCLOCK_TOKEN", "").strip()
    if not token:
        raise RuntimeError("AUTOCLOCK_TOKEN environment variable is required")
    return Settings(
        token=token,
        db=os.environ.get("AUTOCLOCK_DB", "./data/autoclock.db"),
        tz=os.environ.get("AUTOCLOCK_TZ", "UTC"),
        debounce_min=float(os.environ.get("AUTOCLOCK_DEBOUNCE_MIN", "10")),
        max_session_h=float(os.environ.get("AUTOCLOCK_MAX_SESSION_H", "14")),
        ntfy_url=os.environ.get("NTFY_URL", "https://ntfy.sh").rstrip("/"),
        ntfy_topic=os.environ.get("NTFY_TOPIC", ""),
        ntfy_token=os.environ.get("NTFY_TOKEN") or None,
        public_url=os.environ.get("AUTOCLOCK_PUBLIC_URL", "http://localhost:8000").rstrip("/"),
        remind_min=float(os.environ.get("AUTOCLOCK_REMIND_MIN", "0")),
        vapid_subject=os.environ.get("AUTOCLOCK_VAPID_SUBJECT") or _default_vapid_subject(),
    )
