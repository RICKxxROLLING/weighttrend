"""
WeightTrend - a self-hosted weight tracker with live Withings sync.

- OAuth2 web flow against the Withings Public Health Data API (nonce + signature).
- Stores tokens + raw measurements in SQLite (mounted as a Docker volume).
- Smooths the noisy daily scale data with an Exponentially Weighted Moving
  Average (EWMA) trend, then projects your goal date with a linear regression
  on the recent trend.

Everything is intentionally in one file so it's easy to read and tweak.
"""

import os
import time
import hmac
import hashlib
import sqlite3
import datetime as dt
from contextlib import closing

import requests
from flask import (
    Flask, request, redirect, jsonify, render_template, url_for, session
)

# --------------------------------------------------------------------------- #
# Config (all via environment variables - see .env.example)
# --------------------------------------------------------------------------- #
CLIENT_ID = os.environ.get("WITHINGS_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("WITHINGS_CLIENT_SECRET", "")
# The callback must exactly match what you registered in the Withings dashboard.
REDIRECT_URI = os.environ.get("WITHINGS_REDIRECT_URI", "http://localhost:8080/callback")
DB_PATH = os.environ.get("DB_PATH", "/data/weighttrend.db")
SECRET_KEY = os.environ.get("SECRET_KEY", "change-me-please")
# Display units: "kg" or "lb". Withings stores everything in kg internally.
DEFAULT_UNITS = os.environ.get("UNITS", "kg")
# EWMA smoothing factor. 0.1 ~= a 10-day effective window (recommended).
EWMA_ALPHA = float(os.environ.get("EWMA_ALPHA", "0.1"))
# Days of recent trend used to fit the goal-date projection.
PROJECTION_WINDOW_DAYS = int(os.environ.get("PROJECTION_WINDOW_DAYS", "30"))

API_BASE = "https://wbsapi.withings.net"
AUTHORIZE_URL = "https://account.withings.com/oauth2_user/authorize2"
SCOPE = "user.metrics"
MEASTYPE_WEIGHT = 1  # 1 = weight (kg). Others: 6=fat ratio, 8=fat mass, 76=muscle.

app = Flask(__name__)
app.secret_key = SECRET_KEY


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with closing(db()) as conn, conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS tokens (
                   id INTEGER PRIMARY KEY CHECK (id = 1),
                   userid TEXT,
                   access_token TEXT,
                   refresh_token TEXT,
                   expires_at INTEGER
               )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS measurements (
                   ts INTEGER PRIMARY KEY,   -- unix timestamp of the measure
                   weight_kg REAL NOT NULL
               )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS settings (
                   key TEXT PRIMARY KEY,
                   value TEXT
               )"""
        )


def get_setting(key, default=None):
    with closing(db()) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key, value):
    with closing(db()) as conn, conn:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


# --------------------------------------------------------------------------- #
# Withings request signing
# Withings signs certain calls with HMAC-SHA256 over a comma-joined, sorted
# subset of params, keyed by your client secret.
# --------------------------------------------------------------------------- #
def sign(params):
    keys = sorted(params.keys())
    payload = ",".join(str(params[k]) for k in keys)
    return hmac.new(
        CLIENT_SECRET.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()


def get_nonce():
    ts = int(time.time())
    base = {"action": "getnonce", "client_id": CLIENT_ID, "timestamp": ts}
    base["signature"] = sign(base)
    r = requests.post(f"{API_BASE}/v2/signature", data=base, timeout=20)
    r.raise_for_status()
    body = r.json()
    if body.get("status") != 0:
        raise RuntimeError(f"getnonce failed: {body}")
    return body["body"]["nonce"]


def _store_tokens(body):
    expires_at = int(time.time()) + int(body.get("expires_in", 10800)) - 60
    with closing(db()) as conn, conn:
        conn.execute("DELETE FROM tokens")
        conn.execute(
            "INSERT INTO tokens(id,userid,access_token,refresh_token,expires_at) "
            "VALUES(1,?,?,?,?)",
            (
                str(body.get("userid", "")),
                body["access_token"],
                body["refresh_token"],
                expires_at,
            ),
        )


def request_token_from_code(code):
    nonce = get_nonce()
    params = {
        "action": "requesttoken",
        "client_id": CLIENT_ID,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "nonce": nonce,
    }
    params["signature"] = sign(
        {"action": "requesttoken", "client_id": CLIENT_ID, "nonce": nonce}
    )
    r = requests.post(f"{API_BASE}/v2/oauth2", data=params, timeout=20)
    r.raise_for_status()
    body = r.json()
    if body.get("status") != 0:
        raise RuntimeError(f"requesttoken failed: {body}")
    _store_tokens(body["body"])


def refresh_token():
    with closing(db()) as conn:
        row = conn.execute("SELECT refresh_token FROM tokens WHERE id=1").fetchone()
    if not row:
        raise RuntimeError("No refresh token stored; reconnect your account.")
    nonce = get_nonce()
    params = {
        "action": "requesttoken",
        "client_id": CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": row["refresh_token"],
        "nonce": nonce,
    }
    params["signature"] = sign(
        {"action": "requesttoken", "client_id": CLIENT_ID, "nonce": nonce}
    )
    r = requests.post(f"{API_BASE}/v2/oauth2", data=params, timeout=20)
    r.raise_for_status()
    body = r.json()
    if body.get("status") != 0:
        raise RuntimeError(f"refresh failed: {body}")
    _store_tokens(body["body"])


def valid_access_token():
    """Return a fresh access token, refreshing if needed. None if not connected."""
    with closing(db()) as conn:
        row = conn.execute(
            "SELECT access_token, expires_at FROM tokens WHERE id=1"
        ).fetchone()
    if not row:
        return None
    if int(time.time()) >= row["expires_at"]:
        refresh_token()
        with closing(db()) as conn:
            row = conn.execute(
                "SELECT access_token FROM tokens WHERE id=1"
            ).fetchone()
    return row["access_token"]


# --------------------------------------------------------------------------- #
# Fetch measurements
# --------------------------------------------------------------------------- #
def fetch_measurements(since_ts=None):
    token = valid_access_token()
    if not token:
        raise RuntimeError("Not connected to Withings.")
    params = {
        "action": "getmeas",
        "meastype": MEASTYPE_WEIGHT,
        "category": 1,  # 1 = real measures (not user objectives)
    }
    if since_ts:
        params["lastupdate"] = int(since_ts)
    else:
        params["startdate"] = 0
        params["enddate"] = int(time.time())

    headers = {"Authorization": f"Bearer {token}"}
    r = requests.post(f"{API_BASE}/measure", data=params, headers=headers, timeout=30)
    r.raise_for_status()
    body = r.json()
    if body.get("status") != 0:
        raise RuntimeError(f"getmeas failed: {body}")

    rows = []
    for grp in body["body"].get("measuregrps", []):
        ts = grp["date"]
        for m in grp.get("measures", []):
            if m["type"] == MEASTYPE_WEIGHT:
                weight_kg = m["value"] * (10 ** m["unit"])
                rows.append((ts, round(weight_kg, 3)))
    return rows


def sync():
    """Pull new measurements from Withings into the local DB. Returns count added."""
    with closing(db()) as conn:
        row = conn.execute("SELECT MAX(ts) AS m FROM measurements").fetchone()
        last = row["m"] if row and row["m"] else None
    # lastupdate is inclusive; bump by 1s to avoid re-pulling the latest point.
    rows = fetch_measurements(since_ts=(last + 1) if last else None)
    added = 0
    with closing(db()) as conn, conn:
        for ts, kg in rows:
            cur = conn.execute(
                "INSERT OR IGNORE INTO measurements(ts, weight_kg) VALUES(?,?)",
                (ts, kg),
            )
            added += cur.rowcount
    return added


# --------------------------------------------------------------------------- #
# Analytics: daily averaging -> EWMA trend -> linear-regression projection
# --------------------------------------------------------------------------- #
def daily_series():
    """One averaged weight (kg) per calendar day, sorted ascending.

    Returns list of (date, weight_kg).
    """
    with closing(db()) as conn:
        rows = conn.execute(
            "SELECT ts, weight_kg FROM measurements ORDER BY ts ASC"
        ).fetchall()
    buckets = {}
    for r in rows:
        d = dt.date.fromtimestamp(r["ts"])
        buckets.setdefault(d, []).append(r["weight_kg"])
    return [(d, sum(v) / len(v)) for d, v in sorted(buckets.items())]


def ewma(series, alpha=EWMA_ALPHA):
    """Exponentially weighted moving average over the daily series.

    Seeded with the first reading so the trend starts on the data, not at zero.
    """
    out = []
    trend = None
    for d, w in series:
        trend = w if trend is None else trend + alpha * (w - trend)
        out.append((d, trend))
    return out


def linregress(xs, ys):
    """Ordinary least squares. Returns (slope, intercept, slope_stderr)."""
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx
    intercept = my - slope * mx
    # standard error of the slope
    resid = [y - (slope * x + intercept) for x, y in zip(xs, ys)]
    if n > 2:
        s2 = sum(e ** 2 for e in resid) / (n - 2)
        stderr = (s2 / sxx) ** 0.5
    else:
        stderr = 0.0
    return slope, intercept, stderr


def project(trend, goal_kg, window_days=PROJECTION_WINDOW_DAYS):
    """Project the date the EWMA trend reaches goal_kg.

    Fits a line to the trend over the recent window and extrapolates.
    Returns a dict, or None if there isn't enough data / wrong direction.
    """
    if not trend or goal_kg is None:
        return None
    last_date = trend[-1][0]
    cutoff = last_date - dt.timedelta(days=window_days)
    recent = [(d, w) for d, w in trend if d >= cutoff]
    if len(recent) < 2:
        recent = trend  # fall back to all data if the window is sparse
    base = recent[0][0]
    xs = [(d - base).days for d, w in recent]
    ys = [w for d, w in recent]
    fit = linregress(xs, ys)
    if not fit:
        return None
    slope, intercept, stderr = fit  # kg/day
    current = trend[-1][1]
    remaining = current - goal_kg

    # Sanity: are we even moving toward the goal?
    moving_toward = (remaining > 0 and slope < 0) or (remaining < 0 and slope > 0)
    rate_per_week = slope * 7

    result = {
        "rate_per_week_kg": rate_per_week,
        "slope_per_day_kg": slope,
        "current_trend_kg": current,
        "goal_kg": goal_kg,
        "moving_toward_goal": moving_toward,
        "eta": None,
        "eta_optimistic": None,
        "eta_pessimistic": None,
        "days_to_goal": None,
    }
    if not moving_toward or slope == 0:
        return result

    def eta_for(s):
        x_goal = (goal_kg - intercept) / s
        days_from_base = x_goal
        target = base + dt.timedelta(days=days_from_base)
        return target

    eta = eta_for(slope)
    result["eta"] = eta.isoformat()
    result["days_to_goal"] = (eta - last_date).days
    # Confidence band from the slope's standard error (clamped sensibly).
    if stderr > 0:
        fast = slope - stderr if slope < 0 else slope + stderr
        slow = slope + stderr if slope < 0 else slope - stderr
        # fast => sooner; only use if still moving toward goal
        if (remaining > 0 and fast < 0) or (remaining < 0 and fast > 0):
            result["eta_optimistic"] = eta_for(fast).isoformat()
        if (remaining > 0 and slow < 0) or (remaining < 0 and slow > 0):
            result["eta_pessimistic"] = eta_for(slow).isoformat()
    return result


# --------------------------------------------------------------------------- #
# Units helpers
# --------------------------------------------------------------------------- #
KG_PER_LB = 0.45359237


def to_display(kg, units):
    return kg / KG_PER_LB if units == "lb" else kg


def to_kg(val, units):
    return val * KG_PER_LB if units == "lb" else val


def current_units():
    return get_setting("units", DEFAULT_UNITS)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    connected = valid_access_token() is not None if CLIENT_ID else False
    return render_template(
        "index.html",
        connected=connected,
        configured=bool(CLIENT_ID and CLIENT_SECRET),
    )


@app.route("/connect")
def connect():
    state = hashlib.sha256(os.urandom(16)).hexdigest()[:16]
    session["oauth_state"] = state
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "scope": SCOPE,
        "redirect_uri": REDIRECT_URI,
        "state": state,
    }
    q = "&".join(f"{k}={requests.utils.quote(str(v), safe='')}" for k, v in params.items())
    return redirect(f"{AUTHORIZE_URL}?{q}")


@app.route("/callback")
def callback():
    if request.args.get("state") != session.get("oauth_state"):
        return "State mismatch - possible CSRF. Try connecting again.", 400
    code = request.args.get("code")
    if not code:
        return f"No code returned: {dict(request.args)}", 400
    try:
        request_token_from_code(code)
        sync()
    except Exception as e:  # surface the error so setup problems are visible
        return f"Token exchange failed: {e}", 500
    return redirect(url_for("index"))


@app.route("/sync", methods=["POST"])
def sync_route():
    try:
        added = sync()
        return jsonify({"ok": True, "added": added})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/data")
def api_data():
    units = current_units()
    series = daily_series()
    trend = ewma(series)
    goal_setting = get_setting("goal_kg")
    goal_kg = float(goal_setting) if goal_setting else None

    proj = project(trend, goal_kg)

    def pack(pairs):
        return [{"date": d.isoformat(), "value": round(to_display(w, units), 2)}
                for d, w in pairs]

    latest_kg = series[-1][1] if series else None
    trend_kg = trend[-1][1] if trend else None

    # convert projection numbers to display units where relevant
    proj_out = None
    if proj:
        proj_out = dict(proj)
        proj_out["rate_per_week"] = round(to_display(proj["rate_per_week_kg"], units), 3) \
            if proj.get("rate_per_week_kg") is not None else None
        proj_out["current_trend"] = round(to_display(proj["current_trend_kg"], units), 2)
        proj_out["goal"] = round(to_display(proj["goal_kg"], units), 2)

    return jsonify({
        "units": units,
        "points": pack(series),
        "trend": pack(trend),
        "goal": round(to_display(goal_kg, units), 2) if goal_kg is not None else None,
        "latest": round(to_display(latest_kg, units), 2) if latest_kg is not None else None,
        "current_trend": round(to_display(trend_kg, units), 2) if trend_kg is not None else None,
        "count": len(series),
        "projection": proj_out,
    })


@app.route("/api/goal", methods=["POST"])
def api_goal():
    data = request.get_json(force=True)
    units = current_units()
    goal_kg = to_kg(float(data["goal"]), units)
    set_setting("goal_kg", goal_kg)
    return jsonify({"ok": True})


@app.route("/api/units", methods=["POST"])
def api_units():
    data = request.get_json(force=True)
    u = data.get("units")
    if u in ("kg", "lb"):
        set_setting("units", u)
        return jsonify({"ok": True})
    return jsonify({"ok": False}), 400


@app.route("/health")
def health():
    return jsonify({"ok": True})


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
