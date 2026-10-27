# autoclock

A personal, self-hosted work-hours tracker. A geofence automation on your
phone POSTs enter/exit events when you arrive at or leave a place; autoclock
keeps the sessions in SQLite, asks you (via ntfy buttons or a small mobile
web page) which customer each session was for, and reports hours per day,
week and customer.

No accounts, no cloud. One bearer token protects everything.

## Quick start (docker compose)

```sh
cp .env.example .env      # edit: set AUTOCLOCK_TOKEN at minimum
docker compose up -d --build
curl -H "Authorization: Bearer <token>" http://localhost:8000/healthz
```

The database lives on the `autoclock-data` volume at `/data/autoclock.db`.

### Reaching it from your phone

The preferred option is **Tailscale**: install it on the phone and the host,
and use the machine's tailnet address (e.g. `http://host.tailnet-name.ts.net:8000`)
as `AUTOCLOCK_PUBLIC_URL`. Nothing is exposed to the public internet.

Alternatively, put autoclock behind a reverse proxy that terminates HTTPS
and set `AUTOCLOCK_PUBLIC_URL` to the external https URL. Do not expose the
bare HTTP port.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `AUTOCLOCK_TOKEN` | *(required)* | Bearer token for every request |
| `AUTOCLOCK_DB` | `./data/autoclock.db` | SQLite file path (`/data/autoclock.db` in the container) |
| `AUTOCLOCK_TZ` | `UTC` | IANA zone for day/week bucketing |
| `AUTOCLOCK_DEBOUNCE_MIN` | `10` | Re-entering within this window merges with the last session |
| `AUTOCLOCK_MAX_SESSION_H` | `14` | Open sessions auto-close at start + this many hours |
| `NTFY_URL` | `https://ntfy.sh` | ntfy server (self-hosted works too) |
| `NTFY_TOPIC` | *(empty)* | ntfy topic; empty disables notifications |
| `NTFY_TOKEN` | *(empty)* | Optional ntfy access token |

Notification buttons never carry `AUTOCLOCK_TOKEN`. Each button URL has a
signature that only lets it tag that one session with that one customer, so
someone reading the topic can at worst mis-tag a session. Still pick a long,
random topic name (`openssl rand -hex 16`) or self-host ntfy with access
control.
| `AUTOCLOCK_PUBLIC_URL` | `http://localhost:8000` | Base URL the phone uses; goes into notification action URLs |
| `AUTOCLOCK_REMIND_MIN` | `0` | Re-send the "who for?" prompt after N minutes; 0 = off |

## Phone setup

### iOS Shortcuts

1. Subscribe to your `NTFY_TOPIC` in the ntfy app (optional but recommended).
2. Shortcuts → Automation → **Arrive** → pick your work location →
   **Run Immediately** → New Blank Automation → add **Get contents of URL**:
   - URL: `https://your-server/event`
   - Method: `POST`
   - Headers: `Authorization` = `Bearer <AUTOCLOCK_TOKEN>`
   - Request Body (JSON): `{"type":"enter","place":"work"}`
3. Duplicate for **Leave** with body `{"type":"exit","place":"work"}`.

When a notification arrives while you're at work, tap a customer button to
tag the session, or **Other...** to open the picker page. Bookmark
`https://your-server/?token=<AUTOCLOCK_TOKEN>` on your home screen: the
token is stored in an HttpOnly cookie on first visit.

### Android

Use a geofence-capable automation app — e.g. **Tasker** (Location context →
HTTP Request task) or **HTTP Shortcuts** with a location trigger — to POST
the same JSON body and `Authorization` header to `/event` on arrive/leave.
Subscribe to the ntfy topic in the ntfy Android app.

## API overview

All routes need `Authorization: Bearer <AUTOCLOCK_TOKEN>` (browser pages
also accept `?token=` once and set an HttpOnly cookie).

- `POST /event` — `{"type":"enter"|"exit","place":"work"}`
- `GET /sessions?from=&to=`, `PATCH /sessions/{id}`, `DELETE /sessions/{id}`
- `POST /sessions/{id}/customer?customer_id=` — tag or switch a session
- `POST /start?customer_id=` / `POST /stop` — manual clock in/out
- `GET`/`POST /customers`, `PATCH /customers/{id}` — name, rate, archive
- `GET /summary?from=&to=&group=day|week|customer[&format=csv]`
- `GET /` — mobile picker: current session, unassigned warning, one big
  button per customer, Stop
- `GET /week` — weekly totals per day and per customer, CSV download
- `GET /healthz` — no auth

Unassigned time shows up as its own `unassigned` bucket and still counts
toward totals.

## Development

```sh
uv sync
uv run pytest -q
AUTOCLOCK_TOKEN=dev uv run uvicorn app.main:app --reload --port 8000
```

Session rules live in `app/sessions.py` as pure functions
(state + now → ops), so they're testable without HTTP or a database.
