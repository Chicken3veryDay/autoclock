from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app import db
from app.config import Settings
from app.main import create_app

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def make_settings(**over) -> Settings:
    kw = dict(token=TOKEN, db=":memory:", public_url="https://autoclock.example.com")
    kw.update(over)
    return Settings(**kw)


@pytest.fixture()
def app():
    return create_app(make_settings())


@pytest.fixture()
def client(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def conn(app):
    return app.state.db


def enter(client, place="work"):
    return client.post("/event", json={"type": "enter", "place": place}, headers=AUTH)


def exit_(client):
    return client.post("/event", json={"type": "exit"}, headers=AUTH)


def iso(y, m, d, h, mi=0, s=0):
    return datetime(y, m, d, h, mi, s, tzinfo=timezone.utc).isoformat()


def insert_session(conn, start, end=None, customer_id=None, source="geofence") -> int:
    cur = conn.execute(
        "INSERT INTO sessions(customer_id, start, end, source) VALUES (?, ?, ?, ?)",
        (customer_id, start, end, source),
    )
    conn.commit()
    return cur.lastrowid


def add_customer(client, name, rate=None) -> int:
    r = client.post("/customers", json={"name": name, "rate": rate}, headers=AUTH)
    assert r.status_code == 201, r.text
    return r.json()["id"]
