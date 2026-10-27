"""autoclock HTTP API and mobile picker."""
from __future__ import annotations

import asyncio
import csv
import io
import logging
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import (
    Body,
    Depends,
    FastAPI,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .config import Settings, load_settings
from . import db, sessions as rules
from .notify import send_work_prompt

log = logging.getLogger("autoclock")


class EventIn(BaseModel):
    type: str
    place: str | None = None


class SessionPatch(BaseModel):
    start: datetime | None = None
    end: datetime | None = None
    clear_end: bool = False
    customer_id: int | None = None
    clear_customer: bool = False


class CustomerIn(BaseModel):
    name: str
    rate: float | None = None


class CustomerPatch(BaseModel):
    name: str | None = None
    rate: float | None = None
    clear_rate: bool = False
    archived: bool | None = None


class TagBody(BaseModel):
    customer_id: int | None = None


def _fmt_elapsed(seconds: float) -> str:
    m = int(seconds // 60)
    return f"{m // 60}h {m % 60}m" if m >= 60 else f"{m}m"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    conn = db.connect(settings.db)
    tz = ZoneInfo(settings.tz)
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    app = FastAPI(title="autoclock")
    app.state.settings = settings
    app.state.db = conn
    remind_tasks: set[asyncio.Task] = set()

    # ---- auth ---------------------------------------------------------------

    def require_auth(request: Request) -> None:
        auth = request.headers.get("authorization", "")
        if auth == f"Bearer {settings.token}":
            return
        qtoken = request.query_params.get("token")
        if qtoken and qtoken == settings.token:
            return
        if request.cookies.get("autoclock_token") == settings.token:
            return
        raise HTTPException(status_code=401, detail="unauthorized")

    Auth = Depends(require_auth)

    @app.middleware("http")
    async def token_cookie_middleware(request: Request, call_next):
        resp = await call_next(request)
        if request.query_params.get("token") == settings.token:
            resp.set_cookie(
                "autoclock_token",
                settings.token,
                httponly=True,
                samesite="lax",
                max_age=3600 * 24 * 365,
            )
        return resp

    # ---- helpers ------------------------------------------------------------

    def now() -> datetime:
        return db.utcnow()

    def apply_stale() -> None:
        open_s = db.get_open_session(conn)
        ops = rules.plan_stale(now(), open_s, settings.max_session_h)
        if ops:
            db.apply_ops(conn, ops)

    def parse_dt(value: str | None, default: datetime) -> datetime:
        if not value:
            return default
        v = value.strip()
        if len(v) == 10:  # date only
            return datetime.combine(datetime.fromisoformat(v).date(), time.min, tzinfo=timezone.utc)
        dt = datetime.fromisoformat(v)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    def schedule_reminder(session_id: int) -> None:
        if settings.remind_min <= 0:
            return

        async def _remind() -> None:
            await asyncio.sleep(settings.remind_min * 60)
            row = db.get_session_row(conn, session_id)
            if row and row["end"] is None and row["customer_id"] is None:
                send_work_prompt(settings, session_id, ranked_customers())

        task = asyncio.create_task(_remind())
        remind_tasks.add(task)
        task.add_done_callback(remind_tasks.discard)

    def session_view(row: dict) -> dict:
        out = dict(row)
        cid = row["customer_id"]
        cust = db.get_customer(conn, cid) if cid else None
        out["customer_name"] = cust["name"] if cust else None
        end = db.from_iso(row["end"]) if row["end"] else now()
        out["elapsed"] = _fmt_elapsed((end - db.from_iso(row["start"])).total_seconds())
        return out

    def tag_session(session_id: int, customer_id: int) -> dict:
        apply_stale()
        if db.get_customer(conn, customer_id) is None:
            raise HTTPException(status_code=404, detail="customer not found")
        target = db.get_session_row(conn, session_id)
        if target is None:
            raise HTTPException(status_code=404, detail="session not found")
        open_s = db.get_open_session(conn)
        target_s = rules.Session(
            id=target["id"],
            customer_id=target["customer_id"],
            start=db.from_iso(target["start"]),
            end=db.from_iso(target["end"]) if target["end"] else None,
        )
        result = db.apply_ops(conn, rules.plan_tag(now(), target_s, open_s, customer_id))
        sid = result["opened"]["id"] if result["opened"] else session_id
        return {"ok": True, "session": db.get_session_row(conn, sid)}

    def weekday_param() -> int:
        return (now().astimezone(tz).weekday() + 1) % 7  # python Mon=0 -> sqlite %w Sun=0

    def ranked_customers() -> list[dict]:
        return db.ranked_customers(conn, weekday_param())

    # ---- routes -------------------------------------------------------------

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.post("/event", dependencies=[Auth])
    def post_event(event: EventIn) -> dict:
        apply_stale()
        open_s = db.get_open_session(conn)
        if event.type == "enter":
            latest = db.get_latest_session(conn)
            ops = rules.plan_enter(now(), open_s, latest, settings.debounce_min)
            result = db.apply_ops(conn, ops)
            if result["opened"] and result["opened"]["customer_id"] is None:
                sid = result["opened"]["id"]
                send_work_prompt(settings, sid, ranked_customers())
                schedule_reminder(sid)
            return {"ok": True, "reopened": result["reopened"],
                    "session": result["opened"] or (db.get_session_row(conn, latest.id) if result["reopened"] else None)}
        if event.type == "exit":
            ops = rules.plan_exit(now(), open_s)
            db.apply_ops(conn, ops)
            return {"ok": True, "closed": bool(ops)}
        raise HTTPException(status_code=422, detail="type must be enter or exit")

    @app.get("/sessions", dependencies=[Auth])
    def get_sessions(from_: str | None = Query(None, alias="from"),
                     to: str | None = Query(None)) -> list[dict]:
        apply_stale()
        return db.list_sessions(conn, from_, to)

    @app.patch("/sessions/{session_id}", dependencies=[Auth])
    def patch_session(session_id: int, patch: SessionPatch) -> dict:
        apply_stale()
        if patch.clear_end and patch.end is not None:
            raise HTTPException(status_code=422, detail="end and clear_end conflict")
        if patch.clear_customer and patch.customer_id is not None:
            raise HTTPException(status_code=422, detail="customer_id and clear_customer conflict")
        row = db.update_session(
            conn,
            session_id,
            start=patch.start,
            end=None if patch.clear_end else (patch.end if patch.end is not None else ...),
            customer_id=None if patch.clear_customer
            else (patch.customer_id if patch.customer_id is not None else ...),
        )
        if row is None:
            raise HTTPException(status_code=404, detail="session not found")
        return row

    @app.delete("/sessions/{session_id}", dependencies=[Auth])
    def delete_session(session_id: int) -> dict:
        apply_stale()
        if not db.delete_session(conn, session_id):
            raise HTTPException(status_code=404, detail="session not found")
        return {"ok": True}

    @app.post("/sessions/{session_id}/customer", dependencies=[Auth])
    def post_session_customer(
        session_id: int,
        customer_id: int | None = Query(None),
        body: TagBody | None = Body(None),
    ) -> dict:
        cid = customer_id if customer_id is not None else (body.customer_id if body else None)
        if cid is None:
            raise HTTPException(status_code=422, detail="customer_id required")
        return tag_session(session_id, cid)

    @app.post("/start", dependencies=[Auth])
    def post_start(customer_id: int = Query(...)) -> dict:
        apply_stale()
        if db.get_customer(conn, customer_id) is None:
            raise HTTPException(status_code=404, detail="customer not found")
        open_s = db.get_open_session(conn)
        result = db.apply_ops(conn, rules.plan_manual_start(now(), open_s, customer_id))
        sid = result["opened"]["id"] if result["opened"] else (open_s.id if open_s else None)
        return {"ok": True, "session": db.get_session_row(conn, sid) if sid else None}

    @app.post("/stop", dependencies=[Auth])
    def post_stop() -> dict:
        apply_stale()
        open_s = db.get_open_session(conn)
        ops = rules.plan_exit(now(), open_s)
        db.apply_ops(conn, ops)
        return {"ok": True, "closed": bool(ops)}

    @app.get("/customers", dependencies=[Auth])
    def get_customers() -> list[dict]:
        apply_stale()
        return db.list_customers(conn)

    @app.post("/customers", dependencies=[Auth], status_code=201)
    def post_customer(body: CustomerIn) -> dict:
        apply_stale()
        try:
            return db.create_customer(conn, body.name, body.rate)
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.patch("/customers/{customer_id}", dependencies=[Auth])
    def patch_customer(customer_id: int, patch: CustomerPatch) -> dict:
        apply_stale()
        row = db.update_customer(
            conn,
            customer_id,
            name=patch.name,
            rate=None if patch.clear_rate else (patch.rate if patch.rate is not None else ...),
            archived=patch.archived,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="customer not found")
        return row

    # ---- summary ------------------------------------------------------------

    def summary_range(from_: str | None, to: str | None) -> tuple[datetime, datetime]:
        end = parse_dt(to, now())
        start = parse_dt(from_, end - timedelta(days=7))
        return start, end

    def summary_rows(start: datetime, end: datetime, group: str) -> list[dict]:
        return db.summarize(conn, start, end, group, settings.tz, now())

    def summary_csv(rows: list[dict], group: str) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["bucket", "customer", "hours"])
        for r in rows:
            for c in r["customers"]:
                w.writerow([r["key"], c["customer"], f"{c['hours']:.4f}"])
            w.writerow([r["key"], "", f"{r['hours']:.4f}"])
        return buf.getvalue()

    @app.get("/summary", dependencies=[Auth])
    def get_summary(request: Request,
                    from_: str | None = Query(None, alias="from"),
                    to: str | None = Query(None),
                    group: str = Query("day"),
                    format: str = Query("json")):
        apply_stale()
        if group not in ("day", "week", "customer"):
            raise HTTPException(status_code=422, detail="group must be day|week|customer")
        start, end = summary_range(from_, to)
        rows = summary_rows(start, end, group)
        if format == "csv":
            return Response(
                content=summary_csv(rows, group),
                media_type="text/csv",
                headers={"Content-Disposition": f"attachment; filename=autoclock-{group}.csv"},
            )
        return {"from": start.isoformat(), "to": end.isoformat(), "group": group, "rows": rows}

    # ---- pages --------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse, dependencies=[Auth])
    def index(request: Request):
        apply_stale()
        open_row = db.get_session_row(
            conn, db.get_open_session(conn).id
        ) if db.get_open_session(conn) else None
        pending = [session_view(r) for r in db.pending_sessions(conn)]
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "open": session_view(open_row) if open_row else None,
                "pending": pending,
                "customers": ranked_customers(),
            },
        )

    @app.post("/tag", dependencies=[Auth])
    def tag_form(customer_id: Annotated[int, Form()]):
        apply_stale()
        open_s = db.get_open_session(conn)
        target_id = open_s.id if open_s else (
            r["id"] if (r := next(iter(db.pending_sessions(conn)), None)) else None
        )
        if target_id is None:
            return RedirectResponse("/", status_code=303)
        tag_session(target_id, customer_id)
        return RedirectResponse("/", status_code=303)

    @app.post("/stop-form", dependencies=[Auth])
    def stop_form():
        apply_stale()
        open_s = db.get_open_session(conn)
        db.apply_ops(conn, rules.plan_exit(now(), open_s))
        return RedirectResponse("/", status_code=303)

    @app.get("/week", response_class=HTMLResponse, dependencies=[Auth])
    def week(request: Request):
        apply_stale()
        local_now = now().astimezone(tz)
        monday = (local_now - timedelta(days=local_now.weekday())).date()
        start = datetime.combine(monday, time.min, tzinfo=tz).astimezone(timezone.utc)
        end = datetime.combine(monday + timedelta(days=7), time.min, tzinfo=tz).astimezone(timezone.utc)
        day_rows = summary_rows(start, end, "day")
        cust_rows = summary_rows(start, end, "customer")
        cust = cust_rows[0]["customers"] if cust_rows else []
        return templates.TemplateResponse(
            request,
            "week.html",
            {
                "monday": monday.isoformat(),
                "days": [{"day": r["key"], "hours": r["hours"]} for r in day_rows],
                "total": sum(r["hours"] for r in day_rows),
                "customers": cust,
                "csv_url": f"/summary?from={monday.isoformat()}&to={(monday + timedelta(days=7)).isoformat()}&group=day&format=csv",
            },
        )

    return app


class _LazyApp:
    """ASGI entry point that builds the real app on first request so importing
    this module does not require environment variables."""

    _instance: FastAPI | None = None

    async def __call__(self, scope, receive, send):
        if self._instance is None:
            self._instance = create_app()
        return await self._instance(scope, receive, send)


app = _LazyApp()
