#!/usr/bin/env python3
"""
send_digests.py - IMNU "My Week" daily reminder sender.

Runs on a schedule (GitHub Actions, ~every 10 min). For each subscriber whose
chosen reminder time falls in this run's window, it builds that student's
class digest and pushes it via Web Push (VAPID).

  - A MORNING reminder (chosen time before 15:00) briefs TODAY's classes.
  - An EVENING reminder (15:00 or later) previews TOMORROW's classes.
This mirrors the app's own morning/evening hint, so the push matches the card.

The digest content is computed the same way the app's home card is (same
canon()/ckey()/room-change rules), so a student sees the same thing in the
push as they would on the site.

DATA SOURCES
  schedule_data.json  fetched live from SITE_DATA_URL (the same data the app
                      shows - so nothing can drift out of sync).
  subscriptions       fetched from the collector's PROTECTED export:
                      NOTIFY_ENDPOINT?export=1&key=NOTIFY_EXPORT_KEY

ENV / SECRETS (set in the workflow)
  NOTIFY_ENDPOINT     collector /exec URL (same value as in build_app.py)
  NOTIFY_EXPORT_KEY   shared secret guarding the subscription export
  VAPID_PRIVATE       raw base64url VAPID private key (matches app public key)
  VAPID_SUB           VAPID contact, e.g. "mailto:you@example.com"
  SITE_DATA_URL       URL of schedule_data.json on the live site
  WINDOW_MIN          run cadence in minutes (default 10) = the "due" window
  DRY_RUN             "1" to compute + log without sending (safe for testing)

A push that returns 404/410 (the browser dropped the subscription) is
auto-deactivated via the collector's unsubscribe action, so the list
self-cleans over time.

Timing is best-effort: GitHub Actions cron drifts, so a reminder may land a
few minutes early or late, and web push won't reach every device. The app
tells students this; treat reminders as a nudge, not an alarm.
"""

import os, sys, re, json, time, datetime
import urllib.request, urllib.error, urllib.parse

# ---------------------------------------------------------------- config (env)
NOTIFY_ENDPOINT   = os.environ.get("NOTIFY_ENDPOINT", "").strip()
NOTIFY_EXPORT_KEY = os.environ.get("NOTIFY_EXPORT_KEY", "").strip()
VAPID_PRIVATE     = os.environ.get("VAPID_PRIVATE", "").strip()
VAPID_SUB         = os.environ.get("VAPID_SUB", "mailto:imnu.myweek@example.com").strip()
SITE_DATA_URL     = os.environ.get("SITE_DATA_URL", "").strip()
WINDOW_MIN        = int(os.environ.get("WINDOW_MIN", "10"))
DRY_RUN           = os.environ.get("DRY_RUN", "") in ("1", "true", "yes")
IST               = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

def log(*a): print(*a, flush=True)

# ---------------------------------------------------------------- tiny http
def _get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": "imnu-sender"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")

def _post_json(url, obj, timeout=20):
    body = json.dumps(obj).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "text/plain"})  # collector reads text
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8")
    except Exception as e:
        return "err:" + str(e)

# ---------------------------------------------------------------- load inputs
def load_schedule():
    if not SITE_DATA_URL:
        sys.exit("SITE_DATA_URL not set - point it at the live schedule_data.json")
    return json.loads(_get(SITE_DATA_URL))

def load_subscriptions():
    if not (NOTIFY_ENDPOINT and NOTIFY_EXPORT_KEY):
        sys.exit("NOTIFY_ENDPOINT / NOTIFY_EXPORT_KEY not set")
    sep = "&" if "?" in NOTIFY_ENDPOINT else "?"
    url = f"{NOTIFY_ENDPOINT}{sep}export=1&key={urllib.parse.quote(NOTIFY_EXPORT_KEY)}"
    payload = json.loads(_get(url))
    if not payload.get("ok"):
        sys.exit("subscription export refused: " + json.dumps(payload)[:200])
    return [s for s in payload.get("subs", []) if s.get("active")]

# ---------------------------------------------------------------- digest logic
# These mirror the app's home-card logic (build_app.py homeStats) exactly.
def canon(a):
    u = str(a or "").upper()
    return "I&PM" if u == "I&PM" else u.replace("&", "")
def ckey(a, d): return canon(a) + "|" + (d or "")
def norm_hm(s):
    m = re.search(r"(\d{1,2})[:.](\d{2})", str(s or ""))
    return f"{int(m.group(1))}:{m.group(2)}" if m else ""
def to_min(hhmm):
    m = re.search(r"(\d{1,2}):(\d{2})\s*(AM|PM)?", str(hhmm or ""), re.I)
    if not m: return 0
    h = int(m.group(1)) % 12
    if m.group(3) and m.group(3).upper() == "PM": h += 12
    return h * 60 + int(m.group(2))
def pretty(t):
    m = re.search(r"(\d{1,2}):(\d{2})\s*(AM|PM)", str(t or ""), re.I)
    return f"{int(m.group(1))}:{m.group(2)} {m.group(3).upper()}" if m else str(t)

def session_by_hm(data, hm):
    n = norm_hm(hm)
    for s in data.get("sessions", []):
        if norm_hm(s.get("start")) == n:
            return s
    return None

def clean_sub(name):
    # mirror the app's cleanSub: take text before the first '*' or '(' , collapse ws
    s = str(name or "").split("*")[0].split("(")[0]
    return re.sub(r"\s+", " ", s).strip()

def build_digest(data, roll, target_date):
    """Return (title, body, url) for a roll's classes on target_date, or None
    if the student has no classes that day (so we skip sending an empty push)."""
    student = data.get("students", {}).get(roll)
    if not student: return None
    sections = data.get("sections", {})
    secs = [sections[i] for i in student.get("s", []) if i in sections]
    if not secs: return None

    target_iso = target_date.isoformat()
    target_day = target_date.strftime("%A")
    my_keys = {ckey(e.get("abbr"), e.get("division")) for e in secs}
    el_by   = {ckey(e.get("abbr"), e.get("division")): e for e in secs}

    is_room  = lambda c: c.get("type") == "Room Change" and bool(c.get("new_room"))
    my_chg   = [c for c in data.get("changes", []) if ckey(c.get("abbr"), c.get("division")) in my_keys]

    # base meetings on the target weekday
    meetings = []
    for e in secs:
        for m in e.get("meetings", []):
            if m.get("day") == target_day:
                meetings.append({"sec": e, "start": m.get("start"),
                                 "room": e.get("room"), "changed": None})

    # moved OUT of target_date (postponed/cancelled away)  -> drop
    out_keys = {ckey(c.get("abbr"), c.get("division"))
                for c in my_chg if c.get("old_date") == target_iso}
    moved_out = 0
    kept = []
    for mt in meetings:
        if ckey(mt["sec"].get("abbr"), mt["sec"].get("division")) in out_keys:
            moved_out += 1
        else:
            kept.append(mt)
    meetings = kept

    # moved INTO target_date (rescheduled here) -> add
    for c in my_chg:
        if is_room(c) or c.get("type") == "Cancelled": continue
        if c.get("new_date") != target_iso: continue
        el = el_by.get(ckey(c.get("abbr"), c.get("division")))
        if not el: continue
        ses = session_by_hm(data, c.get("new_hhmm"))
        start = ses["start"] if ses else c.get("new_hhmm")  # TBA-ish if no session match
        meetings.append({"sec": el, "start": start, "room": el.get("room"),
                         "changed": "resched", "tba": ses is None})

    # room changes that apply on target_date (or by weekday when no date)
    for c in my_chg:
        if not is_room(c): continue
        if not (c.get("new_date") == target_iso or
                (not c.get("new_date") and c.get("new_day") == target_day)):
            continue
        k = ckey(c.get("abbr"), c.get("division"))
        for mt in meetings:
            if ckey(mt["sec"].get("abbr"), mt["sec"].get("division")) == k:
                mt["old_room"], mt["room"], mt["changed"] = mt.get("room"), c.get("new_room"), "room"

    if not meetings:
        return None  # nothing to remind about

    meetings.sort(key=lambda m: to_min(m["start"]))

    # ---- compose text
    when_word = "Today" if target_date == datetime.datetime.now(IST).date() else "Tomorrow"
    dstr = target_date.strftime("%a %-d %b")
    lines = []
    for m in meetings:
        nm = clean_sub(m["sec"].get("name")) or m["sec"].get("abbr")
        line = f"{pretty(m['start'])}  {nm}"
        room = m.get("room")
        if room: line += f"  \u00b7 {room}"
        if m.get("changed") == "resched":
            line += "  (rescheduled)" if not m.get("tba") else "  (rescheduled, time TBA)"
        elif m.get("changed") == "room" and m.get("old_room"):
            line += f"  (was {m['old_room']})"
        lines.append(line)

    n = len(meetings)
    title = f"{when_word} at IMNU \u00b7 {n} class{'es' if n != 1 else ''}"
    body  = dstr + "\n" + "\n".join(lines[:6])
    if n > 6: body += f"\n+{n-6} more"
    if moved_out:
        body += f"\n({moved_out} class{'es' if moved_out != 1 else ''} postponed off today)" \
                if when_word == "Today" else \
                f"\n({moved_out} class{'es' if moved_out != 1 else ''} postponed)"
    return title, body, "./#timetable"

# ---------------------------------------------------------------- due window
def norm_when(w):
    """Return 'HH:MM' (IST) from a subscriber's stored time. Handles either a
    clean string ("20:00") or a value Google Sheets coerced into a date-time
    ("1899-12-30T14:38:50.000Z"). In the mangled case the ISO is UTC and the
    sheet is IST, so we add the IST offset to recover the time actually picked.
    Returns "" if it can't be parsed (caller logs + skips, never guesses)."""
    s = str(w or "").strip()
    m = re.match(r"^(\d{1,2}):(\d{2})$", s)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    m = re.match(r"^\d{4}-\d{2}-\d{2}T(\d{2}):(\d{2})", s)   # Sheets date-time (UTC)
    if m:
        total = (int(m.group(1)) * 60 + int(m.group(2)) + 330) % (24 * 60)  # +5:30
        return f"{total // 60:02d}:{total % 60:02d}"
    return ""

def is_due(when_hhmm, now_min, window):
    m = re.match(r"^(\d{1,2}):(\d{2})$", str(when_hhmm or ""))
    if not m: return False
    wmin = int(m.group(1)) * 60 + int(m.group(2))
    return (now_min - window) < wmin <= now_min   # caught once, in this slot

def target_for(when_hhmm, today):
    """morning (<15:00) -> today; evening (>=15:00) -> tomorrow."""
    m = re.match(r"^(\d{1,2}):(\d{2})$", str(when_hhmm or ""))
    wmin = (int(m.group(1)) * 60 + int(m.group(2))) if m else 1200
    return today if wmin < 900 else (today + datetime.timedelta(days=1))

# ---------------------------------------------------------------- push
def send_push(sub, title, body, url):
    from pywebpush import webpush, WebPushException
    info = {"endpoint": sub["endpoint"],
            "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}}
    payload = json.dumps({"title": title, "body": body, "url": url, "tag": "imnu-digest"})
    try:
        webpush(subscription_info=info, data=payload,
                vapid_private_key=VAPID_PRIVATE,
                vapid_claims={"sub": VAPID_SUB}, ttl=3600)
        return "ok"
    except WebPushException as e:
        code = getattr(getattr(e, "response", None), "status_code", None)
        if code in (404, 410):
            return "gone"
        return f"err:{code or e}"

def deactivate(endpoint):
    _post_json(NOTIFY_ENDPOINT, {"action": "unsubscribe", "sub": {"endpoint": endpoint}})

# ---------------------------------------------------------------- main
def main():
    now = datetime.datetime.now(IST)
    now_min = now.hour * 60 + now.minute
    log(f"[send_digests] IST {now:%Y-%m-%d %H:%M}  window={WINDOW_MIN}m  dry_run={DRY_RUN}")

    data = load_schedule()
    subs = load_subscriptions()
    log(f"  loaded {len(subs)} active subscription(s); "
        f"schedule week_of={data.get('meta',{}).get('week_of')}")

    # normalize each subscriber's chosen time (handles Sheets-mangled values)
    for s in subs:
        s["_when"] = norm_when(s.get("when"))
        if not s["_when"]:
            log(f"  ! {(s.get('roll') or '??')}: unreadable time {s.get('when')!r} - skipping")

    due = [s for s in subs if s["_when"] and is_due(s["_when"], now_min, WINDOW_MIN)]
    log(f"  {len(due)} subscriber(s) due this slot")
    if not due:
        return

    sent = skipped = gone = errs = 0
    for s in due:
        roll = (s.get("roll") or "").upper()
        tgt  = target_for(s["_when"], now.date())
        dig  = build_digest(data, roll, tgt)
        if not dig:
            skipped += 1
            log(f"   - {roll or '??'} {s['_when']} -> no classes {tgt}, skip")
            continue
        title, body, url = dig
        if DRY_RUN:
            log(f"   ~ {roll} {s['_when']} -> [{title}] {body!r}")
            sent += 1
            continue
        res = send_push(s, title, body, url)
        if res == "ok":
            sent += 1; log(f"   + {roll} {s['_when']} sent")
        elif res == "gone":
            gone += 1; deactivate(s["endpoint"]); log(f"   x {roll} subscription gone, deactivated")
        else:
            errs += 1; log(f"   ! {roll} {res}")
        time.sleep(0.05)  # be gentle on the push service

    log(f"[done] sent={sent} skipped(no-class)={skipped} cleaned={gone} errors={errs}")

if __name__ == "__main__":
    main()
