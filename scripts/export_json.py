#!/usr/bin/env python3
"""Export university.db -> schedule_data.json, scoped to the CURRENT week.

Everything (classes, holidays, exams, change notices) is filtered to the
Mon-Sun week that contains today's date (IST). If that week has no classes
(e.g. the file that arrived is for next week), it falls back to the nearest
upcoming week present in the data. This keeps far-future calendar entries
(like September exams) out of the current view, and makes the week advance
automatically as time passes."""
import sqlite3, json, re, sys, os, datetime

DB  = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "university.db")
OUT = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(__file__), "schedule_data.json")

con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
cur = con.cursor()

# ---- pick the week to display ----
today = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5, minutes=30)).date()  # IST
mdates = sorted({datetime.date.fromisoformat(r["date"])
                 for r in cur.execute("SELECT DISTINCT date FROM meetings WHERE date IS NOT NULL")})
mon = today - datetime.timedelta(days=today.weekday())
if mdates and not any(mon <= d <= mon + datetime.timedelta(days=6) for d in mdates):
    upcoming = [d for d in mdates if d >= today] or mdates
    t = min(upcoming); mon = t - datetime.timedelta(days=t.weekday())
WK_START, WK_END = mon, mon + datetime.timedelta(days=6)
def in_week(ds):
    try: return WK_START <= datetime.date.fromisoformat(ds) <= WK_END
    except Exception: return False

def to_min(t):
    m = re.match(r"(\d{1,2}):(\d{2})(AM|PM)", t or "")
    if not m: return 9999
    h, mi, ap = int(m.group(1)), int(m.group(2)), m.group(3)
    if ap == "PM" and h != 12: h += 12
    if ap == "AM" and h == 12: h = 0
    return h*60 + mi

# sessions actually used this week
sess = {r["session"]: (r["start_time"], r["end_time"])
        for r in cur.execute("SELECT DISTINCT session,start_time,end_time FROM meetings")}
sessions = sorted([{"name": k, "start": v[0], "end": v[1]} for k, v in sess.items()],
                  key=lambda s: to_min(s["start"]))

sections = {}
for c in cur.execute("""SELECT sec.section_id sid, s.abbr, s.name sname, s.area, sec.division,
                               f.name fname, f.email, sec.classroom_code room
                        FROM sections sec JOIN subjects s ON s.code=sec.subject_code
                        LEFT JOIN faculty f ON f.faculty_key=sec.faculty_key""").fetchall():
    seen=set(); mtg=[]
    for m in cur.execute("SELECT DISTINCT day,session,start_time,end_time FROM meetings WHERE section_id=?", (c["sid"],)).fetchall():
        k=(m["day"], m["session"])
        if k in seen: continue
        seen.add(k)
        mtg.append({"day": m["day"], "session": m["session"], "start": m["start_time"], "end": m["end_time"]})
    sections[str(c["sid"])] = {"abbr": c["abbr"], "name": c["sname"], "area": c["area"],
                               "division": c["division"], "faculty": c["fname"],
                               "email": c["email"], "room": c["room"], "meetings": mtg}

events = [{"date":e["date"],"day":e["day"],"type":e["type"],"name":e["name"]}
          for e in cur.execute("SELECT date,day,type,name FROM events ORDER BY date").fetchall()]

students = {}
for s in cur.execute("SELECT roll_no,name,batch FROM students").fetchall():
    sids = [str(e["section_id"]) for e in
            cur.execute("SELECT section_id FROM enrollments WHERE roll_no=?", (s["roll_no"],)).fetchall()]
    students[s["roll_no"]] = {"n": s["name"], "b": s["batch"], "s": sids}

changes = []
chg_path = os.path.join(os.path.dirname(os.path.abspath(DB)), "changes.json")
# current room per (abbr, division) — lets a "held in T3" mail show "was <current room>"
room_by = {(str(s["abbr"]).upper(), str(s["division"] or "").upper()): s["room"]
           for s in sections.values()}
if os.path.exists(chg_path):
    try:
        for c in json.load(open(chg_path, encoding="utf-8")):
            if c.get("type") == "Room Change" and not c.get("old_room"):
                c["old_room"] = room_by.get((str(c.get("abbr", "")).upper(),
                                             str(c.get("division", "")).upper()))
            changes.append(c)
    except Exception as e:
        print("changes.json skipped:", e)

updates = []
upd_path = os.path.join(os.path.dirname(os.path.abspath(DB)), "updates.json")
if os.path.exists(upd_path):
    try:
        for u in json.load(open(upd_path, encoding="utf-8")):
            updates.append(u)
    except Exception as e:
        print("updates.json skipped:", e)

data = {"meta": {"institute": "Institute of Management, Nirma University",
                 "term": "MBA Term-IV", "week_of": WK_START.isoformat(), "recurring": True},
        "days": ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday"],
        "sessions": sessions, "events": events, "changes": changes, "updates": updates,
        "sections": sections, "students": students}
open(OUT, "w", encoding="utf-8").write(json.dumps(data, separators=(",", ":"), ensure_ascii=False))
print(f"Week {WK_START}..{WK_END}: {len(sessions)} sessions, {len(events)} events, {len(changes)} changes")
con.close()
