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
| `AUTOCLOCK_VAPID_SUBJECT` | `mailto:autoclock@example.invalid` | VAPID contact claim for web push (usually `mailto:you@your.domain`) |

## Phone setup

### Home-screen app with notifications (iPhone and Android) — recommended

The picker page doubles as an installable web app (PWA) with first-party
Web Push. No App Store app, no third-party push service, nothing to re-sign.
The VAPID keypair is generated on first start and kept next to the database.

Requirements:
- **HTTPS.** iOS only delivers push to home-screen web apps served over a
  trusted HTTPS origin, and browsers require a secure context for
  `PushManager` everywhere. The easiest path is
  [`tailscale serve`](https://tailscale.com/kb/1312/serve): run
  `tailscale serve --bg 8000` on the host, use the resulting
  `https://host.tailnet-name.ts.net` as `AUTOCLOCK_PUBLIC_URL`, and open
  that URL on the phone. A reverse proxy that terminates HTTPS works too.
- iOS 16.4+ for iPhone push; Android needs a current Chrome/Firefox.

One-time setup:
1. On the phone, open `https://your-server/?token=<AUTOCLOCK_TOKEN>` once so
   the picker loads. Add it to the home screen (Share → **Add to Home
   Screen** on iOS; the browser's *Install app* / *Add to Home screen* on
   Android). The manifest's `start_url` keeps the installed app signed in —
   a home-screen web app has its own cookie jar, separate from the browser.
2. Launch the app from its icon and tap **Enable notifications**. Grant the
   permission when prompted. That's it — the subscription lands in
   `push_subscriptions` and every new pending session pushes a
   "who for?" notification; a tap opens the picker with the ranked
   customers first. (iOS ignores notification action buttons, so the likely
   customer is named in the body instead.)

The Shortcuts and ntfy flows below still work — keep them as alternatives or
skip them if the home-screen app covers you.

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
  button per customer, Stop, Enable notifications
- `GET /week` — weekly totals per day and per customer, CSV download
- `GET /healthz` — no auth
- `GET /manifest.webmanifest`, `/sw.js`, `/icon.svg` — PWA install bits
- `GET /push/vapid-key`, `POST /push/subscribe`, `POST /push/unsubscribe` —
  web push subscription management

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
