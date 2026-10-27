"""HTTP surface: auth, summary bucketing, ntfy payload, pages."""
from __future__ import annotations

import pytest

from app import db

from conftest import AUTH, TOKEN, add_customer, enter, exit_, iso, insert_session, make_settings


# ---- auth -------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/sessions", "/customers", "/summary", "/", "/week"])
def test_401_without_token(client, path):
    assert client.get(path).status_code == 401


def test_401_post_without_token(client):
    assert client.post("/event", json={"type": "enter"}).status_code == 401


def test_wrong_token(client):
    r = client.get("/sessions", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_token_query_param_sets_cookie(client):
    r = client.get(f"/?token={TOKEN}")
    assert r.status_code == 200
    assert "autoclock_token" in r.cookies
    r2 = client.get("/")  # cookie carried by the client jar
    assert r2.status_code == 200


def test_healthz_no_auth(client):
    assert client.get("/healthz").status_code == 200


# ---- customers CRUD ----------------------------------------------------------

def test_customer_crud(client):
    cid = add_customer(client, "Acme", rate=95.0)
    r = client.patch(f"/customers/{cid}", json={"name": "Acme Corp", "archived": True}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["archived"] == 1
    assert client.get("/customers", headers=AUTH).json() == []  # archived hidden


def test_session_patch_and_delete(client, conn):
    enter(client)
    sid = conn.execute("SELECT id FROM sessions").fetchone()["id"]
    r = client.patch(f"/sessions/{sid}", json={"start": "2026-01-05T07:30:00Z"}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["start"].startswith("2026-01-05T07:30")
    assert client.delete(f"/sessions/{sid}", headers=AUTH).status_code == 200
    assert conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 0


# ---- summary -----------------------------------------------------------------

def test_summary_day_buckets():
    from app.main import create_app
    from fastapi.testclient import TestClient

    app = create_app(make_settings())
    insert_session(app.state.db, iso(2026, 1, 5, 9, 0), iso(2026, 1, 5, 11, 0))   # 2h Mon
    insert_session(app.state.db, iso(2026, 1, 6, 9, 0), iso(2026, 1, 6, 10, 0))   # 1h Tue
    with TestClient(app) as c:
        r = c.get("/summary?from=2026-01-05&to=2026-01-07&group=day", headers=AUTH)
        rows = {b["key"]: b for b in r.json()["rows"]}
        assert rows["2026-01-05"]["hours"] == 2.0
        assert rows["2026-01-06"]["hours"] == 1.0


def test_summary_local_midnight_boundary():
    """A session crossing UTC midnight stays in one local day for zones ahead
    of UTC, and splits across local days for zones behind."""
    from app.main import create_app
    from fastapi.testclient import TestClient

    # Sydney is UTC+11 in January: 2026-01-05 23:00Z -> 2026-01-06 10:00 local
    app = create_app(make_settings(tz="Australia/Sydney"))
    insert_session(app.state.db, iso(2026, 1, 5, 23, 0), iso(2026, 1, 6, 1, 0))
    with TestClient(app) as c:
        r = c.get("/summary?from=2026-01-05&to=2026-01-08&group=day", headers=AUTH)
        rows = {b["key"]: b for b in r.json()["rows"]}
        # 10:00-12:00 local on the 6th: all 2h land on 2026-01-06, none on 5th
        assert rows["2026-01-06"]["hours"] == 2.0
        assert "2026-01-05" not in rows


def test_summary_splits_across_local_midnight():
    from app.main import create_app
    from fastapi.testclient import TestClient

    # Chicago is UTC-6 in January: session 02:00-04:00Z is 20:00-22:00 local 5th
    app = create_app(make_settings(tz="America/Chicago"))
    insert_session(app.state.db, iso(2026, 1, 6, 4, 0), iso(2026, 1, 6, 8, 0))
    with TestClient(app) as c:
        r = c.get("/summary?from=2026-01-05&to=2026-01-08&group=day", headers=AUTH)
        rows = {b["key"]: b for b in r.json()["rows"]}
        # local: Jan 5 22:00-24:00 = 2h, Jan 6 00:00-02:00 = 2h
        assert rows["2026-01-05"]["hours"] == 2.0
        assert rows["2026-01-06"]["hours"] == 2.0


def test_summary_unassigned_bucket():
    from app.main import create_app
    from fastapi.testclient import TestClient

    app = create_app(make_settings())
    cid_row = app.state.db.execute(
        "INSERT INTO customers(name) VALUES ('Acme')"
    )
    app.state.db.commit()
    cid = cid_row.lastrowid
    insert_session(app.state.db, iso(2026, 1, 5, 9, 0), iso(2026, 1, 5, 10, 0), customer_id=cid)
    insert_session(app.state.db, iso(2026, 1, 5, 10, 0), iso(2026, 1, 5, 11, 30))
    with TestClient(app) as c:
        r = c.get("/summary?from=2026-01-05&to=2026-01-06&group=day", headers=AUTH)
        row = r.json()["rows"][0]
        assert row["hours"] == 2.5
        cust = {x["customer"]: x["hours"] for x in row["customers"]}
        assert cust["Acme"] == 1.0
        assert cust["unassigned"] == 1.5


def test_summary_customer_group_and_csv():
    from app.main import create_app
    from fastapi.testclient import TestClient

    app = create_app(make_settings())
    cur = app.state.db.execute("INSERT INTO customers(name) VALUES ('Globex')")
    app.state.db.commit()
    insert_session(app.state.db, iso(2026, 1, 5, 9, 0), iso(2026, 1, 5, 11, 0), customer_id=cur.lastrowid)
    with TestClient(app) as c:
        r = c.get("/summary?from=2026-01-05&to=2026-01-06&group=customer", headers=AUTH)
        row = r.json()["rows"][0]
        assert row["customers"][0]["customer"] == "Globex"
        r2 = c.get("/summary?from=2026-01-05&to=2026-01-06&group=day&format=csv", headers=AUTH)
        assert r2.headers["content-type"].startswith("text/csv")
        assert "Globex" in r2.text


# ---- ntfy --------------------------------------------------------------------

def test_ntfy_payload_shape(monkeypatch):
    from app.main import create_app
    from fastapi.testclient import TestClient
    import app.notify as notify

    calls = []

    class FakeResp:
        def raise_for_status(self):
            pass

    monkeypatch.setattr(notify.httpx, "post", lambda *a, **kw: calls.append((a, kw)) or FakeResp())

    settings = make_settings(ntfy_topic="test-topic", ntfy_url="https://ntfy.example.com")
    app = create_app(settings)
    for name in ("Acme", "Globex", "Initech"):
        app.state.db.execute("INSERT INTO customers(name) VALUES (?)", (name,))
    app.state.db.commit()
    with TestClient(app) as c:
        enter(c)
    assert len(calls) == 1
    _, kw = calls[0]
    payload = kw["json"]
    assert payload["topic"] == "test-topic"
    assert payload["title"] == "At work - who for?"
    assert len(payload["actions"]) <= 3
    assert len(payload["actions"]) == 3  # top 2 customers + Other...
    http_actions = [a for a in payload["actions"] if a["action"] == "http"]
    assert len(http_actions) == 2
    sid = app.state.db.execute("SELECT id FROM sessions").fetchone()["id"]
    for a in http_actions:
        assert a["url"].startswith(f"https://autoclock.example.com/sessions/{sid}/customer?customer_id=")
        assert a["method"] == "POST"
        assert "&sig=" in a["url"]
        assert "headers" not in a
        assert a["clear"] is True
    assert TOKEN not in repr(payload)
    view = [a for a in payload["actions"] if a["action"] == "view"][0]
    assert view["label"] == "Other..."
    assert view["url"] == "https://autoclock.example.com/"


def test_signed_action_url_tags_without_token():
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.notify import action_sig

    app = create_app(make_settings())
    with TestClient(app) as c:
        acme = add_customer(c, "Acme")
        globex = add_customer(c, "Globex")
        enter(c)
        sid = app.state.db.execute("SELECT id FROM sessions").fetchone()["id"]
        sig = action_sig(TOKEN, sid, acme)
        # a signature only covers its own customer and session
        assert c.post(f"/sessions/{sid}/customer?customer_id={globex}&sig={sig}").status_code == 401
        assert c.post(f"/sessions/{sid + 1}/customer?customer_id={acme}&sig={sig}").status_code == 401
        assert c.post(f"/sessions/{sid}/customer?customer_id={acme}&sig=bad").status_code == 401
        # and it grants nothing beyond tagging
        assert c.get(f"/sessions?sig={sig}").status_code == 401
        r = c.post(f"/sessions/{sid}/customer?customer_id={acme}&sig={sig}")
        assert r.status_code == 200
    row = app.state.db.execute("SELECT customer_id FROM sessions WHERE id=?", (sid,)).fetchone()
    assert row["customer_id"] == acme


def test_date_only_range_uses_local_midnight():
    from fastapi.testclient import TestClient
    from app.main import create_app

    app = create_app(make_settings(tz="America/Chicago"))
    # 2026-10-27 03:00 UTC is 22:00 on Oct 26 in Chicago, so it's outside Oct 27
    insert_session(app.state.db, iso(2026, 10, 27, 3), iso(2026, 10, 27, 4))
    open_id = insert_session(app.state.db, iso(2026, 10, 27, 15))
    with TestClient(app) as c:
        r = c.get("/sessions?from=2026-10-27&to=2026-10-28", headers=AUTH)
    assert r.status_code == 200
    # the Oct 26 local session is excluded; the open session is included
    assert [x["id"] for x in r.json()] == [open_id]


def test_ntfy_failure_does_not_fail_event(monkeypatch):
    from app.main import create_app
    from fastapi.testclient import TestClient
    import app.notify as notify

    def boom(*a, **kw):
        raise RuntimeError("ntfy down")

    monkeypatch.setattr(notify.httpx, "post", boom)
    app = create_app(make_settings(ntfy_topic="t"))
    with TestClient(app) as c:
        r = enter(c)
        assert r.status_code == 200
        assert app.state.db.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 1


def test_ntfy_disabled_when_no_topic():
    from app.main import create_app
    from fastapi.testclient import TestClient
    import app.notify as notify

    calls = []
    notify.httpx.post = lambda *a, **kw: calls.append(1)
    app = create_app(make_settings(ntfy_topic=""))
    with TestClient(app) as c:
        enter(c)
    assert calls == []


# ---- pages -------------------------------------------------------------------

def test_index_page(client, conn):
    r = client.get("/", headers=AUTH)
    assert r.status_code == 200
    assert "Not clocked in" in r.text
    enter(client)
    r = client.get("/", headers=AUTH)
    assert "unassigned" in r.text


def test_week_page(client, conn):
    r = client.get("/week", headers=AUTH)
    assert r.status_code == 200
    assert "Download CSV" in r.text


def test_tag_form_assigns_open_session(client, conn):
    cid = add_customer(client, "Acme")
    enter(client)
    r = client.post("/tag", data={"customer_id": str(cid)}, headers=AUTH, follow_redirects=False)
    assert r.status_code == 303
    row = conn.execute("SELECT customer_id FROM sessions").fetchone()
    assert row["customer_id"] == cid
