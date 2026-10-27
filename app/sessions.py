"""Pure session state transitions.

Each function takes the current state plus ``now`` and returns the list of
ops to apply. No I/O happens here, so the rules are testable without HTTP
or a database.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class Session:
    id: int
    customer_id: int | None
    start: datetime
    end: datetime | None


@dataclass(frozen=True)
class Open:
    start: datetime
    customer_id: int | None
    source: str


@dataclass(frozen=True)
class Close:
    session_id: int
    end: datetime


@dataclass(frozen=True)
class Reopen:
    session_id: int


@dataclass(frozen=True)
class Assign:
    session_id: int
    customer_id: int


Op = Open | Close | Reopen | Assign


def plan_stale(now: datetime, open_session: Session | None, max_session_h: float) -> list[Op]:
    """Close an open session older than max_session_h at start + max_session_h."""
    if open_session is None:
        return []
    limit = open_session.start + timedelta(hours=max_session_h)
    if now > limit:
        return [Close(open_session.id, limit)]
    return []


def plan_enter(
    now: datetime,
    open_session: Session | None,
    last_closed: Session | None,
    debounce_min: float,
) -> list[Op]:
    """Idempotent enter: merge short geofence blips by reopening a just-closed session."""
    if open_session is not None:
        return []
    if (
        last_closed is not None
        and last_closed.end is not None
        and now - last_closed.end < timedelta(minutes=debounce_min)
    ):
        return [Reopen(last_closed.id)]
    return [Open(now, None, "geofence")]


def plan_exit(now: datetime, open_session: Session | None) -> list[Op]:
    if open_session is None:
        return []
    return [Close(open_session.id, now)]


def plan_tag(
    now: datetime,
    target: Session,
    open_session: Session | None,
    customer_id: int,
) -> list[Op]:
    """Assign a customer; split the open session if it already has another."""
    if open_session is not None and target.id == open_session.id:
        if target.customer_id is None:
            return [Assign(target.id, customer_id)]
        if target.customer_id == customer_id:
            return []
        return [Close(target.id, now), Open(now, customer_id, "split")]
    return [Assign(target.id, customer_id)]


def plan_manual_start(
    now: datetime,
    open_session: Session | None,
    customer_id: int,
) -> list[Op]:
    """Manual start behaves like a switch when a session is already open."""
    if open_session is None:
        return [Open(now, customer_id, "manual")]
    if open_session.customer_id is None:
        return [Assign(open_session.id, customer_id)]
    if open_session.customer_id == customer_id:
        return []
    return [Close(open_session.id, now), Open(now, customer_id, "split")]
