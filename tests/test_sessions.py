"""Session rules: enter/exit, debounce merge, stale close, tag, manual start."""
from __future__ import annotations

from app import db

from conftest import AUTH, enter, exit_, iso, insert_session, make_settings
from app.main import create_app
from fastapi.testclient import TestClient


def test_enter_opens_pending_session(client, conn):
    r = enter(client)
    assert r.status_code == 200
    row = conn.execute("SELECT * FROM sessions").fetchone()
    assert row["customer_id"] is None
    assert row["end"] is None
    assert row["source"] == "geofence"


def test_duplicate_enter_is_idempotent(client, conn):
    enter(client)
    r = enter(client)
    assert r.status_code == 200
    assert conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 1


def test_exit_with_nothing_open(client, conn):
    r = exit_(client)
    assert r.status_code == 200
    assert r.json()["closed"] is False
    assert conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 0


def test_enter_exit_cycle(client, conn):
    enter(client)
    r = exit_(client)
    assert r.json()["closed"] is True
    row = conn.execute("SELECT * FROM sessions").fetchone()
    assert row["end"] is not None


def test_debounce_merge_logic():
    """Pure-rule check: reopen within window, new session past it."""
    from datetime import datetime, timedelta, timezone
    from app.sessions import Session, Open, Reopen, plan_enter

    now = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)
    closed = Session(1, None, now - timedelta(minutes=30), now - timedelta(minutes=5))
    ops = plan_enter(now, None, closed, debounce_min=10)
    assert ops == [Reopen(1)]

    old = Session(1, None, now - timedelta(hours=2), now - timedelta(minutes=20))
    ops = plan_enter(now, None, old, debounce_min=10)
    assert ops == [Open(now, None, "geofence")]

    open_s = Session(2, None, now - timedelta(hours=1), None)
    assert plan_enter(now, open_s, closed, debounce_min=10) == []


def test_debounce_merge_end_to_end(conn):
    """Closed sessions reopen via the API when the blip is inside the window."""
    from datetime import timedelta

    app = create_app(make_settings(debounce_min=10))
    with TestClient(app) as c:
        enter(c)
        exit_(c)
        # immediate re-enter (well under 10 min) merges back into the row
        r = enter(c)
        assert r.json()["reopened"] is True
        rows = app.state.db.execute("SELECT * FROM sessions").fetchall()
        assert len(rows) == 1
        assert rows[0]["end"] is None


def test_new_session_past_debounce(conn):
    from app.main import create_app
    from fastapi.testclient import TestClient

    app = create_app(make_settings(debounce_min=10))
    insert_session(app.state.db, iso(2020, 1, 1, 8, 0), iso(2020, 1, 1, 9, 0))
    with TestClient(app) as c:
        r = enter(c)
        assert r.json()["reopened"] is False
        assert app.state.db.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 2


def test_stale_auto_close(conn):
    app = create_app(make_settings(max_session_h=14))
    insert_session(app.state.db, iso(2020, 1, 1, 8, 0), None)  # ancient open session
    with TestClient(app) as c:
        r = c.get("/sessions", headers=AUTH)
        assert r.status_code == 200
        rows = r.json()
        assert len(rows) == 1
        assert rows[0]["end"] == "2020-01-01T22:00:00+00:00"  # start + 14h


def test_assign_applies_from_start(client, conn):
    from conftest import add_customer

    cid = add_customer(client, "Acme")
    enter(client)
    sid = conn.execute("SELECT id FROM sessions").fetchone()["id"]
    # tag an hour later by directly invoking the endpoint; customer_id
    # applies to the session row (start unchanged)
    r = client.post(f"/sessions/{sid}/customer?customer_id={cid}", headers=AUTH)
    assert r.status_code == 200
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
    assert row["customer_id"] == cid
    assert row["end"] is None  # still the same open session


def _recent_open(conn, customer_id=None) -> int:
    """Open session started 1h ago (inside the stale window)."""
    from datetime import datetime, timedelta, timezone

    start = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    return insert_session(conn, start, None, customer_id=customer_id)


def test_switch_splits_at_now(client, conn):
    from conftest import add_customer

    a = add_customer(client, "Acme")
    g = add_customer(client, "Globex")
    sid = _recent_open(conn, customer_id=a)
    r = client.post(f"/sessions/{sid}/customer?customer_id={g}", headers=AUTH)
    assert r.status_code == 200
    rows = conn.execute("SELECT * FROM sessions ORDER BY id").fetchall()
    assert len(rows) == 2
    old, new = rows
    assert old["end"] is not None and old["customer_id"] == a
    assert new["customer_id"] == g and new["source"] == "split" and new["end"] is None
    assert new["start"] == old["end"]


def test_tagging_pending_after_switch_assigns(client, conn):
    from conftest import add_customer

    a = add_customer(client, "Acme")
    enter(client)
    sid = conn.execute("SELECT id FROM sessions").fetchone()["id"]
    r = client.post(
        f"/sessions/{sid}/customer", json={"customer_id": a}, headers=AUTH
    )
    assert r.status_code == 200
    assert conn.execute("SELECT customer_id FROM sessions WHERE id=?",
                        (sid,)).fetchone()["customer_id"] == a


def test_manual_start(client, conn):
    from conftest import add_customer

    cid = add_customer(client, "Acme")
    r = client.post(f"/start?customer_id={cid}", headers=AUTH)
    assert r.status_code == 200
    row = conn.execute("SELECT * FROM sessions").fetchone()
    assert row["customer_id"] == cid and row["source"] == "manual" and row["end"] is None


def test_manual_start_switches_open_session(client, conn):
    from conftest import add_customer

    a = add_customer(client, "Acme")
    g = add_customer(client, "Globex")
    _recent_open(conn, customer_id=a)
    r = client.post(f"/start?customer_id={g}", headers=AUTH)
    assert r.status_code == 200
    rows = conn.execute("SELECT * FROM sessions ORDER BY id").fetchall()
    assert len(rows) == 2
    assert rows[1]["customer_id"] == g and rows[1]["source"] == "split"


def test_stop_endpoint(client, conn):
    enter(client)
    r = client.post("/stop", headers=AUTH)
    assert r.json()["closed"] is True
