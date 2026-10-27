"""ntfy notification publishing. Never raises into request handling."""
from __future__ import annotations

import hashlib
import hmac
import logging

import httpx

from .config import Settings

log = logging.getLogger("autoclock.notify")


def action_sig(token: str, session_id: int, customer_id: int) -> str:
    """Signature that authorizes one tag action, so the API token never leaves
    the server (ntfy relays and topic subscribers see the action URLs)."""
    msg = f"tag:{session_id}:{customer_id}".encode()
    return hmac.new(token.encode(), msg, hashlib.sha256).hexdigest()[:32]


def _actions(settings: Settings, session_id: int, customers: list[dict]) -> list[dict]:
    base = settings.public_url
    actions = [
        {
            "action": "http",
            "label": c["name"],
            "url": (
                f"{base}/sessions/{session_id}/customer?customer_id={c['id']}"
                f"&sig={action_sig(settings.token, session_id, c['id'])}"
            ),
            "method": "POST",
            "clear": True,
        }
        for c in customers[:2]
    ]
    actions.append({"action": "view", "label": "Other...", "url": f"{base}/", "clear": True})
    return actions[:3]


def build_payload(settings: Settings, session_id: int, customers: list[dict]) -> dict:
    return {
        "topic": settings.ntfy_topic,
        "title": "At work - who for?",
        "message": "Tap to tag this work session.",
        "tags": ["briefcase"],
        "actions": _actions(settings, session_id, customers),
    }


def send_work_prompt(settings: Settings, session_id: int, customers: list[dict]) -> bool:
    """POST the 'who for?' notification. Returns True on success, False when
    disabled or on any ntfy error (logged)."""
    if not settings.ntfy_topic:
        return False
    headers = {"Content-Type": "application/json"}
    if settings.ntfy_token:
        headers["Authorization"] = f"Bearer {settings.ntfy_token}"
    try:
        resp = httpx.post(
            settings.ntfy_url,
            json=build_payload(settings, session_id, customers),
            headers=headers,
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except Exception:
        log.exception("ntfy publish failed for session %s", session_id)
        return False
