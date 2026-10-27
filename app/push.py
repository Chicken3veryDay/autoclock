"""Web Push (RFC 8030/8291) for the home-screen web app. Never raises into
request handling; mirrors app.notify's contract."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import sqlite3
from pathlib import Path

from py_vapid import Vapid
from pywebpush import WebPushException, webpush

from .config import Settings

log = logging.getLogger("autoclock.push")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def public_key_b64(vapid: Vapid) -> str:
    """Uncompressed P-256 point, the form PushManager.subscribe wants."""
    n = vapid.public_key.public_numbers()
    return _b64url(b"\x04" + n.x.to_bytes(32, "big") + n.y.to_bytes(32, "big"))


def load_or_create_vapid(db_path: str) -> Vapid:
    """VAPID keypair stored next to the DB; generated on first start and
    reused after that."""
    if db_path == ":memory:":
        v = Vapid()
        v.generate_keys()
        return v
    key_path = Path(db_path).parent / "vapid.pem"
    if key_path.exists():
        return Vapid.from_pem(key_path.read_bytes())
    v = Vapid()
    v.generate_keys()
    key_path.write_bytes(v.private_pem())
    key_path.chmod(0o600)
    return v


# ---- subscriptions ---------------------------------------------------------


def add_subscription(conn: sqlite3.Connection, endpoint: str, keys: dict) -> None:
    conn.execute(
        "INSERT INTO push_subscriptions(endpoint, p256dh, auth) VALUES (?, ?, ?) "
        "ON CONFLICT(endpoint) DO UPDATE SET p256dh=excluded.p256dh, auth=excluded.auth",
        (endpoint, keys.get("p256dh", ""), keys.get("auth", "")),
    )
    conn.commit()


def remove_subscription(conn: sqlite3.Connection, endpoint: str) -> None:
    conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
    conn.commit()


def list_subscriptions(conn: sqlite3.Connection) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT endpoint, p256dh, auth FROM push_subscriptions"
        ).fetchall()
    ]


# ---- sending ---------------------------------------------------------------


def build_payload(customers: list[dict]) -> str:
    body = (
        f"Likely: {customers[0]['name']}" if customers else "Tap to tag this work session."
    )
    return json.dumps({"title": "At work - who for?", "body": body, "url": "/"})


def send_one(
    settings: Settings, vapid: Vapid, sub: dict, payload: str
) -> str:
    """One push POST. Returns 'ok', 'gone' (404/410), or 'error'. Pure network
    I/O — no db access — so callers can run it outside the db lock."""
    info = {
        "endpoint": sub["endpoint"],
        "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
    }
    try:
        webpush(
            subscription_info=info,
            data=payload,
            vapid_private_key=vapid,
            vapid_claims={"sub": settings.vapid_subject},
            # pywebpush defaults to TTL 0, which drops the push if the phone
            # is briefly unreachable. Keep it for an hour, delivered promptly.
            ttl=3600,
            headers={"Urgency": "high"},
            timeout=10,
        )
        return "ok"
    except WebPushException as exc:
        status = getattr(exc.response, "status_code", None)
        if status in (404, 410):
            return "gone"
        log.exception("web push failed for %s", sub["endpoint"])
        return "error"
    except Exception:
        log.exception("web push failed for %s", sub["endpoint"])
        return "error"


async def send_all(
    settings: Settings,
    conn: sqlite3.Connection,
    vapid: Vapid,
    customers: list[dict],
    db_lock,
) -> int:
    """Async fan-out used by request handlers: snapshot subscriptions under
    the db lock, then do the network calls in a thread WITHOUT the lock, and
    re-take it only to prune dead subscriptions."""
    async with db_lock:
        subs = list_subscriptions(conn)
    payload = build_payload(customers)
    results = await asyncio.gather(
        *(asyncio.to_thread(send_one, settings, vapid, s, payload) for s in subs)
    )
    gone = [s["endpoint"] for s, r in zip(subs, results) if r == "gone"]
    if gone:
        async with db_lock:
            for ep in gone:
                remove_subscription(conn, ep)
    return sum(1 for r in results if r == "ok")
