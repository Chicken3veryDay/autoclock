"""Web push: VAPID keys, subscribe/unsubscribe, fan-out, sw.js and manifest,
and a real RFC 8291 encryption round trip."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db, push
from app.main import create_app

from conftest import AUTH, TOKEN, add_customer, enter, make_settings


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_subscription(endpoint="https://push.example.com/sub/1"):
    """A PushSubscription-shaped dict as the browser's PushManager returns it."""
    return {
        "endpoint": endpoint,
        "expirationTime": None,
        "keys": {"p256dh": "BP" + "x" * 86, "auth": "a" * 22},
    }


def subscribe(client, endpoint="https://push.example.com/sub/1"):
    return client.post("/push/subscribe", json=make_subscription(endpoint), headers=AUTH)


# ---- VAPID keys --------------------------------------------------------------


def test_vapid_key_created_then_reused(tmp_path):
    db_path = str(tmp_path / "data" / "autoclock.db")
    app1 = create_app(make_settings(db=db_path))
    key1 = push.public_key_b64(app1.state.vapid)
    assert (tmp_path / "data" / "vapid.pem").exists()
    assert os.stat(tmp_path / "data" / "vapid.pem").st_mode & 0o777 == 0o600

    app2 = create_app(make_settings(db=db_path))
    key2 = push.public_key_b64(app2.state.vapid)
    assert key1 == key2  # second start reuses the stored key


def test_vapid_key_endpoint(client):
    r = client.get("/push/vapid-key", headers=AUTH)
    assert r.status_code == 200
    raw = _b64url_decode(r.json()["key"])
    assert len(raw) == 65 and raw[0] == 0x04  # uncompressed P-256 point


# ---- subscribe / unsubscribe -------------------------------------------------


def test_subscribe_requires_auth(client):
    assert client.post("/push/subscribe", json=make_subscription()).status_code == 401
    assert client.post(
        "/push/unsubscribe", json={"endpoint": "https://x"}
    ).status_code == 401


def test_subscribe_and_unsubscribe(client, conn):
    r = subscribe(client)
    assert r.status_code == 200
    subs = push.list_subscriptions(conn)
    assert len(subs) == 1
    assert subs[0]["endpoint"] == "https://push.example.com/sub/1"

    r = client.post(
        "/push/unsubscribe",
        json={"endpoint": "https://push.example.com/sub/1"},
        headers=AUTH,
    )
    assert r.status_code == 200
    assert push.list_subscriptions(conn) == []


def test_subscribe_upserts_same_endpoint(client, conn):
    subscribe(client)
    subscribe(client)  # same endpoint again: one row, refreshed keys
    assert len(push.list_subscriptions(conn)) == 1


def test_subscribe_requires_keys(client):
    r = client.post(
        "/push/subscribe", json={"endpoint": "https://x", "keys": {}}, headers=AUTH
    )
    assert r.status_code == 422


# ---- fan-out on enter ---------------------------------------------------------


def test_enter_sends_push_per_subscription(client, conn, monkeypatch):
    calls = []
    monkeypatch.setattr(push, "send_one", lambda *a, **kw: calls.append(a) or "ok")
    subscribe(client, "https://push.example.com/a")
    subscribe(client, "https://push.example.com/b")
    add_customer(client, "Acme")

    r = enter(client)
    assert r.status_code == 200
    # background task: give the test loop a tick via another request
    client.get("/healthz")
    assert len(calls) == 2
    payload = json.loads(calls[0][3])
    assert payload["title"] == "At work - who for?"
    assert payload["url"] == "/"
    assert payload["body"] == "Likely: Acme"


def test_gone_subscription_is_pruned(client, conn, monkeypatch):
    monkeypatch.setattr(push, "send_one", lambda *a, **kw: "gone")
    subscribe(client)
    enter(client)
    client.get("/healthz")
    assert push.list_subscriptions(conn) == []


def test_push_error_leaves_event_2xx(client, conn, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("push service down")

    monkeypatch.setattr(push, "send_one", boom)
    subscribe(client)
    r = enter(client)
    assert r.status_code == 200
    client.get("/healthz")
    # error result: subscription stays (only 404/410 prune)
    assert len(push.list_subscriptions(conn)) == 1


def test_no_subscriptions_means_no_send(client, monkeypatch):
    calls = []
    monkeypatch.setattr(push, "send_one", lambda *a, **kw: calls.append(1))
    enter(client)
    client.get("/healthz")
    assert calls == []


# ---- manifest / sw.js ----------------------------------------------------------


def test_manifest_start_url_carries_token(client):
    # an unauthenticated fetch gets a plain start_url (token is not leaked)
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200
    assert r.json()["start_url"] == "/"

    # the page visitor holds a cookie -> manifest embeds the token so the
    # installed app opens authenticated in its own cookie jar
    client.get(f"/?token={TOKEN}")
    r = client.get("/manifest.webmanifest")
    assert r.json()["start_url"] == f"/?token={TOKEN}"

    # ...and that start_url authenticates a fresh jar
    c2 = TestClient(client.app)
    assert c2.get(r.json()["start_url"]).status_code == 200


def test_sw_js_served_as_javascript(client):
    r = client.get("/sw.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert "showNotification" in r.text


def test_index_references_manifest_and_sw(client):
    r = client.get("/", headers=AUTH)
    assert r.status_code == 200
    assert 'rel="manifest"' in r.text
    assert "/sw.js" in r.text
    assert "push-enable" in r.text


# ---- real encryption round trip -------------------------------------------------


def test_real_encryption_round_trip(client, conn, monkeypatch):
    """Encrypt through pywebpush for real (mock only the HTTP POST) and
    decrypt the captured body with http_ece as a subscriber would."""
    import http_ece
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        PublicFormat,
    )

    # subscriber key pair: ECDH P-256 + 16-byte auth secret
    priv = ec.generate_private_key(ec.SECP256R1())
    pub_bytes = priv.public_key().public_bytes(
        Encoding.X962, PublicFormat.UncompressedPoint
    )
    auth_secret = os.urandom(16)
    endpoint = "https://push.example.com/real-sub"

    captured = {}

    class Resp:
        status_code = 201
        text = ""
        headers = {}

    def fake_post(url, **kw):
        captured["url"] = url
        captured["data"] = kw["data"]
        captured["headers"] = kw["headers"]
        return Resp()

    import requests

    monkeypatch.setattr(requests, "post", fake_post)

    client.post(
        "/push/subscribe",
        json={
            "endpoint": endpoint,
            "keys": {"p256dh": _b64url(pub_bytes), "auth": _b64url(auth_secret)},
        },
        headers=AUTH,
    )
    add_customer(client, "Acme")
    enter(client)
    client.get("/healthz")

    assert captured["url"] == endpoint
    hdrs = {k.lower(): str(v) for k, v in captured["headers"].items()}
    assert int(hdrs["ttl"]) > 0
    assert hdrs["urgency"] == "high"
    # aes128gcm: salt + rs + ciphertext; decrypt with the subscriber keys
    plaintext = http_ece.decrypt(
        captured["data"],
        private_key=priv,
        auth_secret=auth_secret,
        version="aes128gcm",
    )
    # http_ece returns the plaintext with the record delimiter stripped
    payload = json.loads(plaintext)
    assert payload["title"] == "At work - who for?"
    assert payload["body"] == "Likely: Acme"
    assert payload["url"] == "/"


def test_reminder_resends_prompt_for_pending_session(monkeypatch):
    import time
    from fastapi.testclient import TestClient
    from app.main import create_app
    import app.main as main_mod

    sent = []
    monkeypatch.setattr(main_mod, "send_work_prompt", lambda s, sid, c: sent.append(sid) or True)
    app = create_app(make_settings(remind_min=0.001))
    with TestClient(app) as c:
        r = c.post("/event", json={"type": "enter"}, headers=AUTH)
        assert r.status_code == 200
        deadline = time.time() + 3
        while len(sent) < 2 and time.time() < deadline:
            time.sleep(0.02)
    assert len(sent) == 2  # the arrival prompt, then one reminder
