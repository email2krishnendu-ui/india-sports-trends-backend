"""
India Sports Trends Dashboard — Backend
Render.com free-tier deployment ready.
Includes: Web Push notifications (VAPID), hourly trend digest.
"""

import os, time, json, threading, traceback
from datetime import datetime
from flask import Flask, jsonify, request
from flask_cors import CORS
from pytrends.request import TrendReq
from pywebpush import webpush, WebPushException

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

# ── VAPID config (set as Render environment variables) ────────────────────────
VAPID_PRIVATE_KEY  = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC_KEY   = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_CLAIMS_EMAIL = os.environ.get("VAPID_CLAIMS_EMAIL", "mailto:admin@example.com")

# ── in-memory subscription store ─────────────────────────────────────────────
# Resets on redeploy — fine for a team dashboard.
# For persistence swap for a SQLite file or Render's free Redis add-on.
SUBSCRIPTIONS: dict = {}   # endpoint → full subscription object
_sub_lock = threading.Lock()

# ── trends cache ──────────────────────────────────────────────────────────────
CACHE     = {}
CACHE_TTL = 360
_lock     = threading.Lock()

pytrends = TrendReq(hl="en-IN", tz=330, timeout=(10, 30), retries=2, backoff_factor=1.0)

SPORT_TOPICS = {
    "ipl":      ["IPL 2026", "IPL today match", "IPL points table", "IPL live score", "IPL tickets"],
    "cricket":  ["India cricket", "Virat Kohli", "Rohit Sharma", "India vs Australia", "BCCI"],
    "kabaddi":  ["Pro Kabaddi League", "PKL 2026", "Kabaddi live"],
    "football": ["ISL 2026", "Indian Super League", "India football"],
    "hockey":   ["India hockey", "Hockey World Cup", "FIH"],
}

IPL_TEAMS = [
    "Mumbai Indians", "Chennai Super Kings", "Royal Challengers Bengaluru",
    "Kolkata Knight Riders", "Rajasthan Royals", "Delhi Capitals",
    "Punjab Kings", "Sunrisers Hyderabad",
]

# ── fetchers ──────────────────────────────────────────────────────────────────

def _fetch_interest_over_time(keywords, timeframe="now 7-d", geo="IN"):
    try:
        pytrends.build_payload(keywords[:5], timeframe=timeframe, geo=geo)
        df = pytrends.interest_over_time()
        if df.empty:
            return {}
        return {kw: [int(v) for v in df[kw].tolist()] for kw in keywords[:5] if kw in df.columns}
    except Exception:
        traceback.print_exc()
        return {}

def _fetch_related_queries(keyword, geo="IN"):
    try:
        pytrends.build_payload([keyword], timeframe="now 7-d", geo=geo)
        rq   = pytrends.related_queries()
        data = rq.get(keyword, {})
        out  = {"top": [], "rising": []}
        if data.get("top") is not None:
            out["top"]    = data["top"].head(5)[["query", "value"]].to_dict("records")
        if data.get("rising") is not None:
            out["rising"] = data["rising"].head(5)[["query", "value"]].to_dict("records")
        return out
    except Exception:
        traceback.print_exc()
        return {"top": [], "rising": []}

def _fetch_trending_searches():
    try:
        df = pytrends.trending_searches(pn="india")
        return df[0].head(20).tolist()
    except Exception:
        traceback.print_exc()
        return []

def _fetch_realtime_trending():
    try:
        df = pytrends.realtime_trending_searches(pn="IN")
        return [{"title": str(r.get("title", "")), "traffic": str(r.get("entityNames", ""))}
                for _, r in df.head(10).iterrows()]
    except Exception:
        traceback.print_exc()
        return []

def _fetch_ipl_team_interest():
    results = {}
    for i in range(0, len(IPL_TEAMS), 5):
        batch = IPL_TEAMS[i:i+5]
        data  = _fetch_interest_over_time(batch)
        for team, vals in data.items():
            results[team] = max(vals) if vals else 0
        time.sleep(2)
    if results:
        mx = max(results.values()) or 1
        results = {k: round(v / mx * 100) for k, v in results.items()}
    return results

def build_all_data():
    print(f"[{datetime.now().isoformat()}] Refreshing trends data...")
    payload = {"refreshed_at": datetime.now().isoformat(), "geo": "IN"}
    payload["trending_searches"] = _fetch_trending_searches()
    time.sleep(2)
    sport_interest = {}
    for sport, kws in SPORT_TOPICS.items():
        sport_interest[sport] = _fetch_interest_over_time(kws[:3])
        time.sleep(2)
    payload["sport_interest"]  = sport_interest
    payload["ipl_teams"]       = _fetch_ipl_team_interest()
    time.sleep(2)
    payload["ipl_related"]     = _fetch_related_queries("IPL 2026")
    time.sleep(2)
    payload["cricket_related"] = _fetch_related_queries("India cricket")
    time.sleep(2)
    payload["realtime"]        = _fetch_realtime_trending()
    print(f"[{datetime.now().isoformat()}] Refresh complete.")
    return payload

# ── push notification helpers ─────────────────────────────────────────────────

def _build_digest_payload(data):
    """Build the JSON payload sent to every subscriber."""
    trending   = data.get("trending_searches", [])[:3]
    ipl_rising = data.get("ipl_related", {}).get("rising", [])[:2]
    breakouts  = [r["query"] for r in ipl_rising if r.get("query")]

    top_str      = ", ".join(trending) if trending else "No data"
    breakout_str = ", ".join(breakouts) if breakouts else None
    ts           = data.get("refreshed_at", "")
    try:
        time_str = datetime.fromisoformat(ts).strftime("%I:%M %p IST")
    except Exception:
        time_str = ""

    body = f"Trending: {top_str}"
    if breakout_str:
        body += f"\nBreakouts: {breakout_str}"

    return {
        "title": f"India Sports Trends · {time_str}",
        "body":  body,
        "icon":  "/icon-192.png",
        "badge": "/icon-192.png",
        "tag":   "trends-hourly",
        "data":  {"url": "/"},
    }

def send_push_to_all(payload):
    if not VAPID_PRIVATE_KEY:
        print("[push] VAPID_PRIVATE_KEY not set — skipping.")
        return

    with _sub_lock:
        subs = dict(SUBSCRIPTIONS)

    stale = []
    sent  = 0
    for endpoint, sub in subs.items():
        try:
            webpush(
                subscription_info=sub,
                data=json.dumps(payload),
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_CLAIMS_EMAIL},
            )
            sent += 1
        except WebPushException as e:
            status = e.response.status_code if e.response else 0
            if status in (404, 410):
                stale.append(endpoint)
            else:
                print(f"[push] Failed ({status}): {e}")
        except Exception as e:
            print(f"[push] Error: {e}")

    if stale:
        with _sub_lock:
            for ep in stale:
                SUBSCRIPTIONS.pop(ep, None)
        print(f"[push] Removed {len(stale)} stale subscriptions.")

    print(f"[push] Sent to {sent}/{len(subs)} subscribers.")

# ── background threads ────────────────────────────────────────────────────────

def refresh_loop():
    global CACHE
    while True:
        try:
            data = build_all_data()
            with _lock:
                CACHE = data
        except Exception:
            traceback.print_exc()
        time.sleep(CACHE_TTL)

def hourly_push_loop():
    """Wait for cache, then fire push at the top of every hour."""
    while True:
        with _lock:
            ready = bool(CACHE)
        if ready:
            break
        time.sleep(10)

    while True:
        now = datetime.now()
        seconds_to_next_hour = 3600 - (now.minute * 60 + now.second)
        print(f"[push] Next hourly digest in {seconds_to_next_hour}s")
        time.sleep(seconds_to_next_hour)
        with _lock:
            data = dict(CACHE)
        if data:
            payload = _build_digest_payload(data)
            send_push_to_all(payload)

threading.Thread(target=refresh_loop,     daemon=True).start()
threading.Thread(target=hourly_push_loop, daemon=True).start()
time.sleep(2)

# ── routes ────────────────────────────────────────────────────────────────────

@app.route("/api/health")
def health():
    with _lock:
        with _sub_lock:
            sub_count = len(SUBSCRIPTIONS)
        return jsonify({
            "status":           "ok",
            "cache_populated":  bool(CACHE),
            "refreshed_at":     CACHE.get("refreshed_at"),
            "subscribers":      sub_count,
            "vapid_configured": bool(VAPID_PUBLIC_KEY),
        })

@app.route("/api/vapid-public-key")
def vapid_public_key():
    if not VAPID_PUBLIC_KEY:
        return jsonify({"error": "VAPID not configured on server"}), 503
    return jsonify({"publicKey": VAPID_PUBLIC_KEY})

@app.route("/api/push/subscribe", methods=["POST"])
def push_subscribe():
    sub = request.get_json(silent=True)
    if not sub or "endpoint" not in sub:
        return jsonify({"error": "Invalid subscription object"}), 400
    with _sub_lock:
        SUBSCRIPTIONS[sub["endpoint"]] = sub
    print(f"[push] New subscriber. Total: {len(SUBSCRIPTIONS)}")
    return jsonify({"ok": True, "subscribers": len(SUBSCRIPTIONS)}), 201

@app.route("/api/push/unsubscribe", methods=["POST"])
def push_unsubscribe():
    sub = request.get_json(silent=True)
    if sub and "endpoint" in sub:
        with _sub_lock:
            SUBSCRIPTIONS.pop(sub["endpoint"], None)
    return jsonify({"ok": True})

@app.route("/api/push/test", methods=["POST"])
def push_test():
    """Sends an immediate test push to all subscribers."""
    with _lock:
        data = dict(CACHE)
    if not data:
        return jsonify({"error": "Cache not ready yet"}), 503
    payload = _build_digest_payload(data)
    payload["title"] = "Test · India Sports Trends"
    payload["body"]  = "Push notifications are working correctly on your device."
    threading.Thread(target=send_push_to_all, args=(payload,), daemon=True).start()
    return jsonify({"ok": True, "subscribers": len(SUBSCRIPTIONS)})

@app.route("/api/trends")
def all_trends():
    with _lock:
        if not CACHE:
            return jsonify({"error": "Data loading, retry in 60s"}), 503
        return jsonify(CACHE)

@app.route("/api/trends/ipl")
def ipl_trends():
    with _lock:
        return jsonify({"teams": CACHE.get("ipl_teams", {}),
                        "interest": CACHE.get("sport_interest", {}).get("ipl", {}),
                        "related": CACHE.get("ipl_related", {}),
                        "refreshed_at": CACHE.get("refreshed_at")})

@app.route("/api/trends/sport/<sport>")
def sport_trends(sport):
    if sport not in SPORT_TOPICS:
        return jsonify({"error": f"Valid sports: {list(SPORT_TOPICS.keys())}"}), 400
    with _lock:
        return jsonify({"sport": sport,
                        "interest": CACHE.get("sport_interest", {}).get(sport, {}),
                        "refreshed_at": CACHE.get("refreshed_at")})

@app.route("/api/trends/breakouts")
def breakouts_route():
    with _lock:
        return jsonify({"ipl_rising": CACHE.get("ipl_related", {}).get("rising", []),
                        "cricket_rising": CACHE.get("cricket_related", {}).get("rising", []),
                        "trending": CACHE.get("trending_searches", [])[:10],
                        "refreshed_at": CACHE.get("refreshed_at")})

@app.route("/api/trends/realtime")
def realtime():
    with _lock:
        return jsonify({"stories": CACHE.get("realtime", []),
                        "refreshed_at": CACHE.get("refreshed_at")})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
