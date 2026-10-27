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

One-time setup, App Store apps only, nothing to re-sign.

1. Shortcuts → Automation → **Arrive** → pick your work location →
   **Run Immediately** → New Blank Automation, then add:
   1. **Get contents of URL** `https://your-server/event`, Method `POST`,
      Header `Authorization` = `Bearer <AUTOCLOCK_TOKEN>`,
      Request Body (JSON) `{"type":"enter","place":"work"}`.
   2. **Get contents of URL** `https://your-server/customers?ranked=true`
      with the same header. Customers come back most likely first.
   3. **Get Dictionary Value** `name` from *Contents of URL* (gives the list
      of names).
   4. **Choose from List** with prompt "Who for?".
   5. **URL Encode** *Chosen Item*, then **Get contents of URL**
      `https://your-server/start?name=<URL Encoded Text>`, Method `POST`, same
      header.
2. Add a **Leave** automation with one **Get contents of URL**: `POST`
   `https://your-server/event`, body `{"type":"exit","place":"work"}`.

On arrival you get a "Who for?" prompt; tapping a name tags the session from
the moment you arrived. Ignore it and the session stays *unassigned* until
you fix it on the picker page.

Optional extras:
- **ntfy** (App Store) subscribed to `NTFY_TOPIC` for button prompts and
  reminders (`AUTOCLOCK_REMIND_MIN`) if you skip steps 1.2-1.5.
- **Picker page:** bookmark `https://your-server/?token=<AUTOCLOCK_TOKEN>`
  to your home screen (Share → Add to Home Screen) to switch customers
  mid-day or stop. The token is kept in an HttpOnly cookie after the first
  visit.

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
