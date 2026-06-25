#!/usr/bin/env python3
"""Fetch from the admin's email via IMAP:
  1) the newest schedule .xlsx attachment  -> argv[1] (default rosters/schedule_latest.xlsx)
  2) recent "Change in class schedule" notices -> data/changes.json (parsed)

Env vars (set as GitHub repository secrets):
  MAIL_USER, MAIL_PASS, SENDER, IMAP_HOST (default imap.gmail.com),
  MAIL_SUBJECT (optional, required substring in the schedule email's subject),
  CHANGE_SUBJECT (optional, default "change in class")
Exits non-zero if no schedule attachment is found (so a stale week is never published).
Change parsing is best-effort and never fails the build.
"""
import imaplib, email, os, sys, re, json, datetime
from email.header import decode_header
from email.utils import parsedate_to_datetime

OUT  = sys.argv[1] if len(sys.argv) > 1 else "rosters/schedule_latest.xlsx"
CHANGES_OUT = os.environ.get("CHANGES_OUT", "data/changes.json")
HOST = os.environ.get("IMAP_HOST", "imap.gmail.com")
USER = os.environ.get("MAIL_USER"); PWD = os.environ.get("MAIL_PASS")
SENDER = os.environ.get("SENDER", "mba.im@nirmauni.ac.in")
SUBJ = os.environ.get("MAIL_SUBJECT", "")
CHANGE_SUBJECT = os.environ.get("CHANGE_SUBJECT", "change in class")
if not (USER and PWD):
    sys.exit("MAIL_USER / MAIL_PASS not set.")

def decode(s):
    return "".join(p.decode(enc or "utf-8", "ignore") if isinstance(p, bytes) else p
                   for p, enc in decode_header(s or ""))

def body_text(msg):
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                try: return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "ignore")
                except Exception: pass
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                html = part.get_payload(decode=True).decode("utf-8", "ignore")
                return re.sub(r"<[^>]+>", " ", html)
        return ""
    try: return msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", "ignore")
    except Exception: return ""

# room/venue token, e.g. "T3", "E6", "T-3", "309-F", "LH1" (letters+digits, or digits-letters)
ROOM_RE = r'(?:[A-Za-z]{1,4}-?\d{1,3}[A-Za-z]?|\d{2,4}-[A-Za-z]{1,3})'
_WEEKDAYS = {"monday":0,"tuesday":1,"wednesday":2,"thursday":3,"friday":4,"saturday":5,"sunday":6}
def _room_norm(s): return re.sub(r'\s+', '', str(s)).upper()

def parse_change(text, edate=None):
    # edate = the email's own date, so "today"/"tomorrow"/weekday resolve correctly
    edate = edate or datetime.date.today()
    # sections like "SBM(A)", "SBM(A & B)", "SDM(A), SDM(B)" -> one (abbr, division) per division
    secs = []
    for ab, dvgroup in re.findall(r'([A-Za-z&]{2,6})\(\s*([A-Za-z][A-Za-z&,\s]*?)\s*\)', text):
        for dv in re.findall(r'[A-Za-z]', dvgroup):
            secs.append((ab, dv))
    if not secs: return []
    raw_dates = re.findall(r'(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})', text)
    dates = [f"{int(('20'+y) if len(y)==2 else y):04d}-{int(m):02d}-{int(d):02d}" for d, m, y in raw_dates]
    # time ranges, capturing a trailing AM/PM if present (e.g. "02:40-03:40 & 03:50-04:50PM")
    tmatches = re.findall(r'(\d{1,2}[:.]\d{2})\s*(?:[-\u2013\u2014]|to)\s*\d{1,2}[:.]\d{2}\s*([AaPp][Mm])?', text)
    starts = [s.replace('.', ':') for s, _ in tmatches]
    meris = [m.upper() for _, m in tmatches]
    # if a single meridiem is stated for the whole sentence, apply it to the bare times too
    known = [m for m in meris if m]
    fill = known[-1] if known and len(set(known)) == 1 else None
    times = [s + (m if m else (fill or "")) for s, m in zip(starts, meris)]
    low = text.lower()
    ctype = ('Preponed' if 'prepon' in low else 'Postponed' if 'postpon' in low
             else 'Cancelled' if 'cancel' in low
             else 'Rescheduled' if ('reschedul' in low or 'shift' in low) else 'Changed')
    old_date = dates[0] if dates else None
    new_date = dates[-1] if len(dates) >= 2 else None
    tba = len(times) == 0  # no new time announced -> "to be announced"
    def day(ds):
        try: return datetime.date.fromisoformat(ds).strftime("%A")
        except Exception: return None
    # resolve a relative / weekday-only date ("today", "tomorrow", "on Wednesday") to a real date
    def _add(n): return (edate + datetime.timedelta(days=n)).isoformat()
    rel_date = None
    if   re.search(r'\bday\s+after\s+tomorrow\b', low): rel_date = _add(2)
    elif re.search(r'\btomorrow\b', low):              rel_date = _add(1)
    elif re.search(r'\btoday\b', low):                 rel_date = _add(0)
    else:
        wd = re.search(r'\b(?:on|this|coming)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b', low)
        if wd:
            rel_date = _add((_WEEKDAYS[wd.group(1)] - edate.weekday()) % 7)
    # ---- room / venue detection (only the classroom changes; day & time stay put) ----
    new_room = old_room = None
    m = re.search(r'\b(' + ROOM_RE + r')\s+to\s+(' + ROOM_RE + r')\b', text, re.I)   # "from E6 to T3"
    if m: old_room, new_room = _room_norm(m.group(1)), _room_norm(m.group(2))
    if new_room is None:                                                              # "T3 classroom"
        m = re.search(r'\b(' + ROOM_RE + r')\s+class\s*-?\s*room\b', text, re.I)
        if m: new_room = _room_norm(m.group(1))
    if new_room is None:                                                              # "held in / shifted to / venue: T3"
        m = re.search(r'(?:held|conducted|shifted|moved|take\s*place|venue|class\s*-?\s*room|classroom|room|hall)\b'
                      r'[^.\n]{0,25}?\b(?:in|to|at|:)\s*(?:room\s*(?:no\.?)?\s*|class\s*-?\s*room\s*|venue\s*|hall\s*)?'
                      r'(' + ROOM_RE + r')\b', text, re.I)
        if m: new_room = _room_norm(m.group(1))
    if old_room is None:                                                              # "instead of E6"
        m = re.search(r'(?:instead of|in place of|rather than|in lieu of|not in)\s+(?:room\s*)?(' + ROOM_RE + r')\b', text, re.I)
        if m: old_room = _room_norm(m.group(1))
    has_time_shift = bool(times)
    has_date_shift = bool(new_date) and (new_date != old_date)
    is_room_change = (new_room is not None) and not has_time_shift and not has_date_shift \
                     and not any(k in low for k in ('postpon', 'prepon', 'cancel'))
    # keep the message, drop the office sign-off / signature
    cut = len(text)
    for pat in (r'\bregards\b', r'\bthanks\b', r'\bthank you\b', r'\bwarm regards\b',
                r'\bbest regards\b', r'\bsincerely\b', r'\byours\b',
                r'(MBA\s+)?Programme Office'):
        mm = re.search(pat, text, re.I)
        if mm: cut = min(cut, mm.start())
    raw = re.sub(r'\s+', ' ', text[:cut]).strip()[:400]
    out = []
    if is_room_change:
        d0 = old_date or new_date or rel_date or edate.isoformat()   # the day the relocated class meets
        d_day = day(d0)
        hhmm = times[0] if len(times) == 1 else None                 # optional; room change attaches by day
        for ab, dv in secs:
            out.append({"abbr": ab.upper(), "division": dv.upper(), "type": "Room Change",
                        "old_date": d0, "old_day": d_day, "new_date": d0, "new_day": d_day,
                        "old_hhmm": hhmm, "new_hhmm": hhmm,
                        "old_room": old_room, "new_room": new_room, "tba": False, "raw": raw})
        return out
    # a dateless time/cancel change ("cancelled today") still gets a concrete day to render on
    if old_date is None and rel_date:
        old_date = rel_date
        if new_date is None and not times: new_date = rel_date
    for i, (ab, dv) in enumerate(secs):
        if not times:                 hhmm = None
        elif len(times) == len(secs): hhmm = times[i]            # "respectively" -> per division
        elif len(times) == 1:         hhmm = times[0]            # one time for all
        else:                         hhmm = times[i] if i < len(times) else times[-1]
        out.append({"abbr": ab.upper(), "division": dv.upper(), "type": ctype,
                    "old_date": old_date, "old_day": day(old_date),
                    "new_date": new_date, "new_day": day(new_date),
                    "new_hhmm": hhmm, "tba": tba, "raw": raw})
    return out

M = imaplib.IMAP4_SSL(HOST); M.login(USER, PWD); M.select("INBOX")

# ---- 1) change notices (best effort) ----
changes, seen = [], set()
since = (datetime.date.today() - datetime.timedelta(days=14)).strftime("%d-%b-%Y")
crit = ["FROM", SENDER, "SINCE", since] if SENDER else ["SINCE", since]
try:
    typ, data = M.search(None, *crit)
    for num in reversed(data[0].split()):
        typ, md = M.fetch(num, "(RFC822)")
        msg = email.message_from_bytes(md[0][1])
        if CHANGE_SUBJECT.lower() not in decode(msg.get("Subject", "")).lower():
            continue
        try: _edate = parsedate_to_datetime(msg.get("Date")).date()
        except Exception: _edate = datetime.date.today()
        for c in parse_change(body_text(msg), _edate):
            # dedup by the actual change (subject + division + dates + time + room), NOT by the shared
            # message text -- otherwise a single mail naming two divisions keeps only one of them
            key = (c["abbr"], c["division"], c["old_date"], c["new_date"], c["new_hhmm"], c["type"], c.get("new_room"))
            if key not in seen:
                changes.append(c); seen.add(key)
    os.makedirs(os.path.dirname(CHANGES_OUT) or ".", exist_ok=True)
    json.dump(changes, open(CHANGES_OUT, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"Parsed {len(changes)} change notice(s) -> {CHANGES_OUT}")
except Exception as e:
    print("Change-notice fetch skipped:", e)

# ---- 1b) committee notices -> data/updates.json (best effort, never fails the build) ----
UPDATES_OUT = os.environ.get("UPDATES_OUT", "data/updates.json")
COMMITTEES = [
    ("PLACECOMM", "Placement Committee",        os.environ.get("PLACECOMM_FROM", "placecomm.im@nirmauni.ac.in")),
    ("SAC",       "Student Advisory Committee", os.environ.get("SAC_FROM",       "sac.im@nirmauni.ac.in")),
    ("SWC",       "Student Welfare Committee",  os.environ.get("SWC_FROM",       "studentwelfare.im@nirmauni.ac.in")),
    ("NICHE",     "The Marketing Club",         os.environ.get("NICHE_FROM",     "niche.im@nirmauni.ac.in")),
    ("FINESSE",   "Finance Club",               os.environ.get("FINESSE_FROM",   "finesse.im@nirmauni.ac.in")),
    ("NEWSJN",    "The News Club",              os.environ.get("NEWSJN_FROM",    "newsjunction.im@nirmauni.ac.in")),
    ("CULT",      "The Cultural Committee",     os.environ.get("CULT_FROM",      "cultcomm.im@nirmauni.ac.in")),
    ("PRATIKRITI","Photography Club",           os.environ.get("PRATIKRITI_FROM","pratikriti.im@nirmauni.ac.in")),
    ("CLIQUE",    "The IT Club",                os.environ.get("CLIQUE_FROM",    "clique.im@nirmauni.ac.in")),
    ("XQUIZIT",   "Quiz Club",                  os.environ.get("XQUIZIT_FROM",   "xquizit.im@nirmauni.ac.in")),
    ("SPORTZZZ",  "Sports Committee",           os.environ.get("SPORTZZZ_FROM",  "sportzzzcomm.im@nirmauni.ac.in")),
    ("OPTIMUS",   "Operations Club",            os.environ.get("OPTIMUS_FROM",   "optimus.im@nirmauni.ac.in")),
]
def msg_date(msg):
    try: return parsedate_to_datetime(msg.get("Date")).date().isoformat()
    except Exception: return None
updates = []
_today = datetime.date.today()
_month = _today.strftime("%Y-%m")                       # current year-month
since_u = _today.replace(day=1).strftime("%d-%b-%Y")    # 1st of this month
for code, cname, addr in COMMITTEES:
    try:
        typ, data = M.search(None, "FROM", addr, "SINCE", since_u)
        for num in data[0].split()[-15:][::-1]:         # this month's mails per committee
            typ, md = M.fetch(num, "(RFC822)")
            msg = email.message_from_bytes(md[0][1])
            subj = re.sub(r"\s+", " ", decode(msg.get("Subject", ""))).strip()
            if not subj:
                continue
            d = msg_date(msg)
            if not d or d[:7] != _month:                # current month only
                continue
            body = re.sub(r"\s+", " ", body_text(msg)).strip()
            updates.append({"code": code, "committee": cname, "subject": subj[:140],
                            "date": d, "snippet": body[:200], "from": addr})
    except Exception as e:
        print(f"Committee fetch skipped ({code}):", e)
updates.sort(key=lambda u: (u["date"] or ""), reverse=True)
updates = updates[:120]
try:
    os.makedirs(os.path.dirname(UPDATES_OUT) or ".", exist_ok=True)
    json.dump(updates, open(UPDATES_OUT, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"Parsed {len(updates)} committee update(s) -> {UPDATES_OUT}")
except Exception as e:
    print("Committee updates write skipped:", e)

# ---- 2) schedule attachment (required) ----
crit = ["FROM", SENDER] if SENDER else ["ALL"]
typ, data = M.search(None, *crit)
ids = data[0].split()
if not ids:
    M.logout(); sys.exit(f"No emails found from {SENDER or 'anyone'}.")
for num in reversed(ids):
    typ, md = M.fetch(num, "(RFC822)")
    msg = email.message_from_bytes(md[0][1])
    if SUBJ and SUBJ.lower() not in decode(msg.get("Subject", "")).lower():
        continue
    for part in msg.walk():
        fn = part.get_filename()
        if fn and decode(fn).lower().endswith((".xlsx", ".xls")):
            os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
            open(OUT, "wb").write(part.get_payload(decode=True))
            print(f"Saved {decode(fn)!r} (subject: {decode(msg.get('Subject',''))!r}) -> {OUT}")
            M.logout(); sys.exit(0)
M.logout(); sys.exit("No .xlsx attachment found in matching emails — not publishing.")
