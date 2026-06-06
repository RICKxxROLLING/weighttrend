# WeightTrend

A small, self-hosted weight tracker that syncs live from your **Withings** scale,
smooths the daily noise with an **exponentially weighted moving average (EWMA)**,
and **projects the date you'll hit your goal**. Built to run as a single Docker
container on Unraid.

## Why these methods

- **Weigh in daily, same time** (morning, after the bathroom, before food/water).
  A single reading swings ±1–2 kg from water, food and glycogen, so you never
  act on one number — you track the *trend* of the noisy data.
- **EWMA trend, not a simple average.** EWMA reacts faster to real change than a
  7-day simple moving average while still killing daily noise, and it doesn't lag
  a full week. This is what Hacker's Diet, Happy Scale, Libra and TrendWeight all
  use. Default smoothing `α = 0.1` ≈ a 10-day window.
- **Projection = linear regression on the recent trend.** It fits a line to the
  last ~30 days of the EWMA trend (your rate changes over time, so old data is
  excluded) and extrapolates to your goal. It shows a likely *range*, not a single
  date, and reminds you that loss slows as you approach goal.

## What you get

- Withings OAuth connect button (one-time authorize)
- Automatic token refresh; "Sync now" button to pull new readings
- Chart: raw daily readings + EWMA trend + goal line
- Cards: latest reading, smoothed trend, rate/week, goal
- Projected goal date with a confidence range
- kg/lb toggle, editable goal

---

## 1. Register a Withings developer app

1. Go to the **Withings Partner / Developer dashboard**:
   https://developer.withings.com/dashboard/ and sign in (your normal Withings
   account works).
2. Create an application of type **"Public API / Developer"** (the free public
   health-data tier — no medical cloud).
3. Set the **Callback URI** to exactly the URL the app will be reached at, with
   `/callback` on the end. This must match `WITHINGS_REDIRECT_URI` byte-for-byte.
   - LAN only: `http://YOUR-UNRAID-IP:8080/callback`
   - With a reverse proxy + domain (recommended, and required if you want HTTPS):
     `https://weight.yourdomain.com/callback`
4. Copy the **Client ID** and **Client Secret**.

> Note: Withings sometimes requires the callback to be `https` for production
> apps. If `http://<ip>` is rejected, put the app behind your Unraid reverse
> proxy (SWAG / Nginx Proxy Manager) and use an `https://` domain.

## 2. Configure

```bash
cp .env.example .env
# edit .env: paste CLIENT_ID / CLIENT_SECRET, set the REDIRECT_URI,
# and generate a SECRET_KEY:
python -c "import secrets; print(secrets.token_hex(32))"
```

## 3a. Run with docker-compose (any machine)

```bash
docker compose up -d --build
# open http://localhost:8080  (or your server IP)
```

Click **Connect my Withings account**, authorize, and your full history pulls in.

## 3b. Deploy on Unraid

**Option A — Compose Manager plugin (easiest):**

1. Install the **Compose Manager** plugin from Community Applications.
2. Add a new stack, paste in `docker-compose.yml`, and create a `.env` next to it.
3. Change the volume line to an appdata path, e.g.:
   ```yaml
   volumes:
     - /mnt/user/appdata/weighttrend:/data
   ```
4. Compose Up.

**Option B — build an image, add a container manually:**

```bash
docker build -t weighttrend .
```
Then *Docker → Add Container*:
- **Repository:** `weighttrend` (or push to a registry and use that)
- **Port:** `8080` → `8080`
- **Path:** container `/data` → host `/mnt/user/appdata/weighttrend`
- **Variables:** `WITHINGS_CLIENT_ID`, `WITHINGS_CLIENT_SECRET`,
  `WITHINGS_REDIRECT_URI`, `SECRET_KEY`, and optionally `UNITS`, `EWMA_ALPHA`,
  `PROJECTION_WINDOW_DAYS`.

## 4. Keeping data fresh

The scale syncs to Withings' cloud over Wi‑Fi automatically. This app pulls from
that cloud. New readings appear when you:
- open the dashboard and click **Sync now**, or
- (optional) hit `POST /sync` on a cron / Unraid User Script, e.g. every few hours:
  ```bash
  curl -X POST http://localhost:8080/sync
  ```

For true push updates you can later add a Withings webhook (`notify` subscribe)
pointing at this app; polling is simpler and plenty for weight data.

---

## Configuration reference

| Variable | Default | Meaning |
|---|---|---|
| `WITHINGS_CLIENT_ID` | – | From the Withings dashboard |
| `WITHINGS_CLIENT_SECRET` | – | From the Withings dashboard |
| `WITHINGS_REDIRECT_URI` | `http://localhost:8080/callback` | Must match the dashboard callback |
| `SECRET_KEY` | `change-me-please` | Flask session signing key |
| `UNITS` | `kg` | `kg` or `lb` (display only) |
| `EWMA_ALPHA` | `0.1` | Trend smoothing; lower = smoother |
| `PROJECTION_WINDOW_DAYS` | `30` | Recent days used for the goal projection |
| `DB_PATH` | `/data/weighttrend.db` | SQLite location (keep on the volume) |

## How the math works (in the code)

- `daily_series()` — averages multiple same-day weigh-ins into one point per day.
- `ewma()` — `trend = trend + α·(weight − trend)`, seeded on the first reading.
- `project()` — least-squares line over the recent trend → slope (kg/day) →
  `goal_date = (goal − intercept) / slope`; the slope's standard error gives the
  optimistic/pessimistic range.

## Notes / limitations

- Single-user by design (one Withings account). Don't expose it to the open
  internet without auth in front of it.
- Weight only by default. To also track body fat, add meastypes (6 = fat ratio,
  8 = fat mass, 76 = muscle mass) in `fetch_measurements()`.
- The projection is a straight-line estimate; real weight loss decelerates near
  goal, so later dates in the range are usually the realistic ones.
```
