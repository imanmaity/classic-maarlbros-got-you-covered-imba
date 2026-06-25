#!/usr/bin/env python3
"""
Pre-build self-test. Runs in the workflow BEFORE fetch/build so a broken parser
fails the build loudly instead of silently shipping mis-parsed change notices.

It extracts the REAL parse_change() out of fetch_email.py (without triggering its
IMAP side-effects) and runs it against a battery of room-change + regression cases.

Exit code 0 = all good; non-zero = a check failed (workflow stops).
No third-party deps.
"""
import ast, datetime, re, sys, os

HERE = os.path.dirname(os.path.abspath(__file__))


def load_parse_change():
    """Pull parse_change (+ its helper globals) from fetch_email.py and exec only those,
    so importing doesn't run the module-level IMAP/login code."""
    src = open(os.path.join(HERE, "fetch_email.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    want = {"ROOM_RE", "_WEEKDAYS", "_room_norm", "parse_change"}
    chunks = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in want:
            chunks.append(ast.get_source_segment(src, node))
        elif isinstance(node, ast.Assign):
            names = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if names & want:
                chunks.append(ast.get_source_segment(src, node))
    ns = {"re": re, "datetime": datetime}
    exec("\n".join(chunks), ns)
    if "parse_change" not in ns:
        print("SELFTEST ERROR: parse_change not found in fetch_email.py")
        sys.exit(2)
    return ns["parse_change"]


def main():
    parse_change = load_parse_change()
    E = datetime.date(2026, 6, 23)   # a fixed Tuesday, so "today" etc. are deterministic
    fails = []

    def check(name, cond, got=None):
        if not cond:
            fails.append((name, got))

    def first(text, edate=E):
        r = parse_change(text, edate)
        return r[0] if r else None

    # ---- room changes ----
    r = parse_change("Dear Students, TQM(A) and TQM(B) session scheduled today will be held in T3 classroom.", E)
    check("today/T3 two divisions", len(r) == 2, len(r))
    check("today/T3 type+room", all(c["type"] == "Room Change" and c["new_room"] == "T3" for c in r), r)
    check("today resolves to email date", all(c["new_date"] == "2026-06-23" and c["new_day"] == "Tuesday" for c in r))

    c = first("The CB(A) session will be conducted in classroom T5 today.")
    check("conducted in classroom T5", c and c["type"] == "Room Change" and c["new_room"] == "T5", c)

    c = first("RMKT(B) class on 25.06.2026 will be held in T4 instead of E6.")
    check("explicit date + instead-of old room",
          c and c["type"] == "Room Change" and c["new_room"] == "T4" and c["old_room"] == "E6"
          and c["new_date"] == "2026-06-25" and c["new_day"] == "Thursday", c)

    c = first("Venue for S&DM(A) on Wednesday changed from T4 to T6.")
    check("from T4 to T6 + weekday",
          c and c["type"] == "Room Change" and c["old_room"] == "T4" and c["new_room"] == "T6"
          and c["new_day"] == "Wednesday", c)

    c = first("FSA(C) session tomorrow has been shifted to room T5.")
    check("'shifted to room' is a room change",
          c and c["type"] == "Room Change" and c["new_room"] == "T5" and c["new_date"] == "2026-06-24", c)

    c = first("IPM(D) class moved to T6 for today only.")
    check("'moved to T6'", c and c["type"] == "Room Change" and c["new_room"] == "T6", c)

    check("three divisions in one mail",
          len(parse_change("BM(A), BM(B) and BM(C) will be held in T6 today.", E)) == 3)

    # ---- regressions: these must NOT become Room Changes ----
    c = first("Dear Students, The SBM(A) & SBM(B) session scheduled on 23.06.2026 is "
              "postponed to 24.06.2026 at 02:40-03:40 & 03:50-04:50PM respectively.")
    check("postpone stays Postponed", c and c["type"] == "Postponed", c)
    check("postpone new_date", c and c["new_date"] == "2026-06-24", c)

    c = first("TQM(A) session on 23.06.2026 is preponed to 22.06.2026 at 9:10-10:10AM.")
    check("prepone stays Preponed", c and c["type"] == "Preponed", c)

    c = first("CB(B) session scheduled on 24.06.2026 is cancelled.")
    check("cancel stays Cancelled", c and c["type"] == "Cancelled", c)

    c = first("MFS(A) session is postponed to 25.06.2026 at 02:40-03:40PM and will be held in T6.")
    check("postpone+room mention stays Postponed", c and c["type"] == "Postponed", c)

    c = first("BI(A) session scheduled on 23.06.2026 is rescheduled to 26.06.2026 (time TBA).")
    check("reschedule with new date stays Rescheduled", c and c["type"] == "Rescheduled", c)

    if fails:
        print(f"SELFTEST FAILED: {len(fails)} check(s) did not pass\n")
        for name, got in fails:
            print(f"  - {name}" + (f"   GOT: {got}" if got is not None else ""))
        sys.exit(1)
    print("SELFTEST OK: parser checks passed.")
    sys.exit(0)


if __name__ == "__main__":
    main()
