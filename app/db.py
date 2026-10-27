"""SQLite storage layer. Timestamps are UTC ISO-8601 text everywhere."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .sessions import Open, Close, Reopen, Assign, Op, Session

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    rate REAL,
    archived INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER REFERENCES customers(id),
    start TEXT NOT NULL,
    end TEXT,
    source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_start ON sessions(start);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def from_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _row_to_session(row: sqlite3.Row) -> Session:
    return Session(
        id=row["id"],
        customer_id=row["customer_id"],
        start=from_iso(row["start"]),
        end=from_iso(row["end"]) if row["end"] else None,
    )


def connect(path: str) -> sqlite3.Connection:
    if path != ":memory:":
        Path(os.path.dirname(path) or ".").mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


# ---- customers -------------------------------------------------------------

def create_customer(conn: sqlite3.Connection, name: str, rate: float | None = None) -> dict:
    cur = conn.execute("INSERT INTO customers(name, rate) VALUES (?, ?)", (name, rate))
    conn.commit()
    return get_customer(conn, cur.lastrowid)


def get_customer(conn: sqlite3.Connection, customer_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    return dict(row) if row else None


def list_customers(conn: sqlite3.Connection, include_archived: bool = False) -> list[dict]:
    sql = "SELECT * FROM customers" + ("" if include_archived else " WHERE archived = 0")
    return [dict(r) for r in conn.execute(sql + " ORDER BY name").fetchall()]


def update_customer(
    conn: sqlite3.Connection,
    customer_id: int,
    name: str | None = None,
    rate: float | None | object = ...,
    archived: bool | None = None,
) -> dict | None:
    if get_customer(conn, customer_id) is None:
        return None
    if name is not None:
        conn.execute("UPDATE customers SET name = ? WHERE id = ?", (name, customer_id))
    if rate is not ...:
        conn.execute("UPDATE customers SET rate = ? WHERE id = ?", (rate, customer_id))
    if archived is not None:
        conn.execute("UPDATE customers SET archived = ? WHERE id = ?", (int(archived), customer_id))
    conn.commit()
    return get_customer(conn, customer_id)


def ranked_customers(conn: sqlite3.Connection, weekday: int) -> list[dict]:
    """Active customers ranked: most recently used on this weekday first,
    then most recent use overall, then name."""
    rows = conn.execute(
        """
        SELECT c.*,
               MAX(CASE WHEN strftime('%w', s.start) = ? THEN s.start END) AS last_same_wd,
               MAX(s.start) AS last_any
        FROM customers c
        LEFT JOIN sessions s ON s.customer_id = c.id
        WHERE c.archived = 0
        GROUP BY c.id
        ORDER BY last_same_wd IS NULL, last_same_wd DESC,
                 last_any IS NULL, last_any DESC, c.name
        """,
        (str(weekday),),
    ).fetchall()
    return [dict(r) for r in rows]


# ---- sessions --------------------------------------------------------------

def _session_row(conn: sqlite3.Connection, session_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return dict(row) if row else None


def get_session_row(conn: sqlite3.Connection, session_id: int) -> dict | None:
    return _session_row(conn, session_id)


def get_open_session(conn: sqlite3.Connection) -> Session | None:
    row = conn.execute(
        "SELECT * FROM sessions WHERE end IS NULL ORDER BY start DESC LIMIT 1"
    ).fetchone()
    return _row_to_session(row) if row else None


def get_latest_session(conn: sqlite3.Connection) -> Session | None:
    row = conn.execute("SELECT * FROM sessions ORDER BY start DESC LIMIT 1").fetchone()
    return _row_to_session(row) if row else None


def list_sessions(
    conn: sqlite3.Connection, start: str | None = None, end: str | None = None
) -> list[dict]:
    sql, params = "SELECT * FROM sessions WHERE 1=1", []
    if start:
        sql += " AND end >= ?"
        params.append(start)
    if end:
        sql += " AND start <= ?"
        params.append(end)
    sql += " ORDER BY start"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def update_session(
    conn: sqlite3.Connection,
    session_id: int,
    start: datetime | None = None,
    end: datetime | None | object = ...,
    customer_id: int | None | object = ...,
) -> dict | None:
    if _session_row(conn, session_id) is None:
        return None
    if start is not None:
        conn.execute("UPDATE sessions SET start = ? WHERE id = ?", (to_iso(start), session_id))
    if end is not ...:
        conn.execute(
            "UPDATE sessions SET end = ? WHERE id = ?",
            (to_iso(end) if end else None, session_id),
        )
    if customer_id is not ...:
        conn.execute("UPDATE sessions SET customer_id = ? WHERE id = ?", (customer_id, session_id))
    conn.commit()
    return _session_row(conn, session_id)


def delete_session(conn: sqlite3.Connection, session_id: int) -> bool:
    cur = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    conn.commit()
    return cur.rowcount > 0


def apply_ops(conn: sqlite3.Connection, ops: list[Op]) -> dict[str, Any]:
    """Apply session ops from app.sessions in one transaction."""
    result: dict[str, Any] = {"opened": None, "reopened": False, "closed": [], "assigned": []}
    try:
        for op in ops:
            if isinstance(op, Open):
                cur = conn.execute(
                    "INSERT INTO sessions(customer_id, start, end, source) VALUES (?, ?, NULL, ?)",
                    (op.customer_id, to_iso(op.start), op.source),
                )
                result["opened"] = _session_row(conn, cur.lastrowid)
            elif isinstance(op, Close):
                conn.execute(
                    "UPDATE sessions SET end = ? WHERE id = ? AND end IS NULL",
                    (to_iso(op.end), op.session_id),
                )
                result["closed"].append(op.session_id)
            elif isinstance(op, Reopen):
                conn.execute("UPDATE sessions SET end = NULL WHERE id = ?", (op.session_id,))
                result["reopened"] = True
            elif isinstance(op, Assign):
                conn.execute(
                    "UPDATE sessions SET customer_id = ? WHERE id = ?",
                    (op.customer_id, op.session_id),
                )
                result["assigned"].append(op.session_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return result


def pending_sessions(conn: sqlite3.Connection) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM sessions WHERE customer_id IS NULL ORDER BY start DESC"
        ).fetchall()
    ]


def sessions_between(
    conn: sqlite3.Connection, start: datetime, end: datetime
) -> list[dict]:
    """Sessions overlapping [start, end), clipped to the window. Open sessions
    are clipped at ``end``."""
    rows = conn.execute(
        "SELECT * FROM sessions WHERE start < ? AND (end IS NULL OR end > ?) ORDER BY start",
        (to_iso(end), to_iso(start)),
    ).fetchall()
    out = []
    for r in rows:
        s = from_iso(r["start"])
        e = from_iso(r["end"]) if r["end"] else end
        s2, e2 = max(s, start), min(e, end)
        if e2 > s2:
            out.append({**dict(r), "clip_start": s2, "clip_end": e2})
    return out


# ---- summary ---------------------------------------------------------------

def summarize(
    conn: sqlite3.Connection,
    start: datetime,
    end: datetime,
    group: str,
    tzname: str,
    now: datetime,
) -> list[dict]:
    """Group seconds per day / week / customer. Days and weeks (Mon-Sun)
    are bucketed in ``tzname``."""
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(tzname)
    rows = sessions_between(conn, start, min(end, now))
    customer_names = {c["id"]: c["name"] for c in list_customers(conn, include_archived=True)}
    buckets: dict[str, dict[str, Any]] = {}

    def bucket_key(s: datetime, e: datetime):
        if group == "customer":
            yield "customer", s, e
        elif group == "week":
            cur = s
            while cur < e:
                local = cur.astimezone(tz)
                monday = (local - timedelta(days=local.weekday())).date()
                nxt = datetime.combine(
                    monday + timedelta(days=7), datetime.min.time(), tzinfo=tz
                ).astimezone(timezone.utc)
                yield monday.isoformat(), cur, min(e, nxt)
                cur = nxt
        else:  # day
            cur = s
            while cur < e:
                local = cur.astimezone(tz)
                nxt = datetime.combine(
                    local.date() + timedelta(days=1), datetime.min.time(), tzinfo=tz
                ).astimezone(timezone.utc)
                yield local.date().isoformat(), cur, min(e, nxt)
                cur = nxt

    for r in rows:
        cid = r["customer_id"]
        for key, seg_s, seg_e in bucket_key(r["clip_start"], r["clip_end"]):
            b = buckets.setdefault(key, {"key": key, "hours": 0.0, "customers": {}})
            secs = (seg_e - seg_s).total_seconds()
            b["hours"] += secs
            cname = customer_names.get(cid, "unassigned")
            b["customers"][cname] = b["customers"].get(cname, 0.0) + secs

    out = []
    for key in sorted(buckets):
        b = buckets[key]
        cust = [
            {"customer": n, "hours": round(sec / 3600, 4)}
            for n, sec in sorted(b["customers"].items(), key=lambda kv: -kv[1])
        ]
        out.append({"key": key, "hours": round(b["hours"] / 3600, 4), "customers": cust})
    return out
