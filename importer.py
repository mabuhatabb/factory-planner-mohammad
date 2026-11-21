import csv
import io
import json
import re
from bisect import bisect_right
from datetime import date

import numpy as np
import pandas as pd
import streamlit as st

from planner import empty_usage, empty_stock

PROD_COLS = ["product", "demand", "due_day", "priority", "minutes_per_unit"]
MACH_COLS = ["machine", "day", "minutes_available"]
ALLOWED_COLS = ["product", "machine"]

MAX_PRODUCTS = 50      # keep equal to app.py
MAX_MACHINES = 10      # keep equal to app.py
MAX_DAYS = 31          # keep equal to app.py
MAX_IMPORT_ROWS = 5000

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# key, label, required, words that suggest the column (best first)
FIELDS = [
    ("item", "Unit / item", True,
     ["item", "product", "model", "unit name", "part", "sku", "description", "unit", "name"]),
    ("due", "Due / delivery date", True,
     ["due date", "delivery date", "ship date", "due", "deliver", "ship", "need by",
      "required", "promise", "date"]),
    ("po", "PO number", False,
     ["purchase order", "po number", "po no", "po", "order number", "order no", "order"]),
    ("qty", "Quantity", False, ["quantity", "qty", "units", "pcs", "count"]),
    ("status", "Status", False, ["status", "state", "stage"]),
    ("priority", "Priority", False, ["priority", "rank", "urgency"]),
    ("minutes", "Time per unit", False,
     ["minutes per unit", "min per unit", "hours per unit", "hrs per unit", "cycle time",
      "minutes", "hours", "hrs", "duration", "time"]),
    ("machine", "Machine / line / work center", False,
     ["machine", "work center", "workcenter", "line", "resource", "station", "cell"]),
]


# ---------- READING THE FILE ----------
def clean_text(v):
    try:
        if pd.isna(v):
            return ""
    except (TypeError, ValueError):
        pass
    if isinstance(v, (float, np.floating)) and float(v).is_integer():
        v = int(v)
    return str(v).strip()


def _number(v, default):
    n = pd.to_numeric(v, errors="coerce")
    return default if pd.isna(n) else float(n)


def to_date(v, dayfirst=False):
    try:
        if pd.isna(v):
            return pd.NaT
    except (TypeError, ValueError):
        pass
    if isinstance(v, (int, float, np.integer, np.floating)) and not isinstance(v, bool):
        if 20000 < float(v) < 80000:   # Excel date number
            return pd.to_datetime(float(v), unit="D", origin="1899-12-30")
        return pd.NaT
    d = pd.to_datetime(v, errors="coerce", dayfirst=dayfirst)
    if not pd.isna(d) and getattr(d, "tzinfo", None) is not None:
        d = d.tz_localize(None)
    return d


def _is_label(v):
    """A text cell that is not just a number."""
    if not isinstance(v, str) or not v.strip():
        return False
    try:
        float(v.replace(",", ""))
        return False
    except ValueError:
        return True


def detect_header(raw, max_scan=20):
    """The header row is the early row with the most text labels."""
    best_row, best_count = 0, -1
    for i in range(min(max_scan, len(raw))):
        count = sum(1 for v in raw.iloc[i] if _is_label(v))
        if count > best_count:
            best_row, best_count = i, count
    return best_row


def _names(row):
    out, seen = [], {}
    for i, v in enumerate(row):
        base = clean_text(v) or f"Column {i + 1}"
        n = seen.get(base, 0)
        seen[base] = n + 1
        out.append(base if n == 0 else f"{base} ({n + 1})")
    return out


def _read_csv_raw(data):
    text = data.decode("utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = []
    for i, r in enumerate(csv.reader(io.StringIO(text), dialect)):
        if i >= MAX_IMPORT_ROWS + 50:
            break
        rows.append(r)
    width = max((len(r) for r in rows), default=0)
    raw = pd.DataFrame([r + [None] * (width - len(r)) for r in rows], dtype=object)
    return raw.mask(raw == "")


def load_frames(name, data, tabs, header_override):
    frames, notes, headers = {}, [], {}
    for t in tabs:
        try:
            if name.lower().endswith(".csv"):
                raw = _read_csv_raw(data)
            else:
                raw = pd.read_excel(io.BytesIO(data), sheet_name=t, header=None,
                                    nrows=MAX_IMPORT_ROWS + 50)
        except Exception as e:
            notes.append(f"Tab '{t}' could not be read ({e}).")
            continue
        raw = raw.reset_index(drop=True)
        if raw.empty:
            notes.append(f"Tab '{t}' is empty.")
            continue
        h = int(header_override) - 1 if header_override else detect_header(raw)
        h = min(max(h, 0), len(raw) - 1)
        df = raw.iloc[h + 1:].copy()
        df.columns = _names(raw.iloc[h])
        df = df.dropna(how="all").reset_index(drop=True)
        if len(df) > MAX_IMPORT_ROWS:
            df = df.head(MAX_IMPORT_ROWS)
            notes.append(f"Tab '{t}' has more than {MAX_IMPORT_ROWS} rows. "
                         f"Only the first {MAX_IMPORT_ROWS} were read.")
        frames[t] = df
        headers[t] = h + 1
    return frames, notes, headers


# ---------- GUESSING THE COLUMNS ----------
def _norm(text):
    t = " " + re.sub(r"[^a-z0-9]+", " ", str(text).lower()).strip() + " "
    return t.replace(" per unit ", " perunit ")


def _score(col, words):
    n = _norm(col)
    tokens = n.split()
    best = 0
    for i, w in enumerate(words):
        w = _norm(w).strip()
        if " " + w + " " in n:
            best = max(best, 100 - i)
        elif len(w) >= 5 and any(t.startswith(w) for t in tokens):
            best = max(best, 50 - i)
    return best


def auto_guess(cols):
    used, out = set(), {}
    for key, _label, _required, words in FIELDS:
        best, best_col = 0, None
        for c in cols:
            if c in used:
                continue
            s = _score(c, words)
            if s > best:
                best, best_col = s, c
        if best_col:
            out[key] = best_col
            used.add(best_col)
    return out


# ---------- BUILDING THE ORDERS ----------
def parse_rows(frames, m, status_col, status_values, minutes_unit,
               default_minutes, default_priority, dayfirst):
    notes, rows = [], []
    skipped = {"no unit name": 0, "no valid quantity": 0, "no valid due date": 0}
    factor = 60 if minutes_unit == "hours" else 1

    def getv(r, col):
        return r[col] if (col and col in r.index) else None

    for tab, df in frames.items():
        need = [m["item"], m["due"]]
        if status_col and status_values:
            need.append(status_col)
        missing = sorted({c for c in need if c not in df.columns})
        if missing:
            notes.append(f"Tab '{tab}' skipped: no column named {', '.join(missing)}.")
            continue

        d = df
        if status_col and status_values:
            keep = d[status_col].map(clean_text).str.lower().isin(status_values)
            d = d[keep]

        for _, r in d.iterrows():
            item = clean_text(getv(r, m["item"]))
            if not item:
                skipped["no unit name"] += 1
                continue

            qv = getv(r, m.get("qty"))
            qty = 1 if qv is None else int(round(_number(qv, 0)))
            if qty < 1:
                skipped["no valid quantity"] += 1
                continue

            due = to_date(getv(r, m["due"]), dayfirst)
            if pd.isna(due):
                skipped["no valid due date"] += 1
                continue

            po = clean_text(getv(r, m.get("po")))
            pv = getv(r, m.get("priority"))
            priority = default_priority if pv is None else _number(pv, default_priority)
            mv = getv(r, m.get("minutes"))
            n = _number(mv, float("nan")) if mv is not None else float("nan")
            minutes = default_minutes if pd.isna(n) else n * factor

            rows.append({
                "product": f"{po} | {item}" if po else item,
                "demand": qty,
                "due": due.normalize(),
                "priority": priority,
                "minutes": minutes,
                "machine": clean_text(getv(r, m.get("machine"))),
                "tab": tab,
            })

    for reason, count in skipped.items():
        if count:
            notes.append(f"{count} matching row(s) skipped: {reason}.")
    return rows, notes


def working_calendar(start, weekdays, last_due, extra_days):
    d = pd.Timestamp(start).normalize()
    end = last_due.normalize() + pd.Timedelta(days=int(extra_days))
    dates, guard = [], 0
    while len(dates) < MAX_DAYS and guard < 4000 and (d <= end or not dates):
        if d.weekday() in weekdays:
            dates.append(d)
        d += pd.Timedelta(days=1)
        guard += 1
    return dates


def assemble(rows, start, weekdays, extra_days):
    df = pd.DataFrame(rows)
    dates = working_calendar(start, weekdays, df["due"].max(), extra_days)

    notes, before, beyond, days = [], 0, 0, []
    for due in df["due"]:
        n = bisect_right(dates, due)
        if n < 1:
            n, before = 1, before + 1
        if due > dates[-1]:
            beyond += 1
        days.append(n)
    df["due_day"] = days

    if before:
        notes.append(f"{before} row(s) were due before the first working day and were set to due day 1.")
    if beyond:
        notes.append(f"{beyond} row(s) are due after the last planned day (at most {MAX_DAYS} working days). "
                     "They were set to the last day.")

    machine_sets = {}
    for p, mname in zip(df["product"], df["machine"]):
        if mname:
            machine_sets.setdefault(p, set()).add(mname)

    merged = df.groupby("product", sort=False).agg(
        demand=("demand", "sum"),
        due_day=("due_day", "min"),
        priority=("priority", "max"),
        minutes_per_unit=("minutes", "first"),
        source_tabs=("tab", lambda s: ", ".join(sorted(set(s)))),
    ).reset_index()
    merged["demand"] = merged["demand"].astype(int)
    merged["priority"] = merged["priority"].round().astype(int).clip(lower=1)
    merged["minutes_per_unit"] = merged["minutes_per_unit"].round().astype(int).clip(lower=1)

    if len(merged) < len(df):
        notes.append(f"{len(df) - len(merged)} line(s) share the same PO and unit. Their quantities "
                     "were added and the earliest due date was kept. Check that these are not repeats.")
    return merged[PROD_COLS + ["source_tabs"]], machine_sets, dates, notes


def build_tables(orders, machine_sets, dates, machine_names, minutes_per_day):
    prod = orders[PROD_COLS].copy()
    mach = pd.DataFrame(
        [{"machine": m, "day": d, "minutes_available": int(minutes_per_day)}
         for m in machine_names for d in range(1, len(dates) + 1)],
        columns=MACH_COLS)
    pairs = []
    for p in prod["product"]:
        named = [m for m in machine_names if m in machine_sets.get(p, set())]
        for m in (named or machine_names):
            pairs.append({"product": p, "machine": m})
    return prod, mach, pd.DataFrame(pairs, columns=ALLOWED_COLS)


# ---------- THE SCREEN ----------
def apply_profile():
    f = st.session_state.get("imp_profile_file")
    try:
        prof = json.loads(f.getvalue().decode("utf-8"))
        if not isinstance(prof, dict):
            raise ValueError("this is not a column profile")
        st.session_state["imp_profile"] = prof
        st.session_state["imp_gen"] = st.session_state.get("imp_gen", 0) + 1
        st.session_state["imp_profile_msg"] = "Profile applied. Your column choices below now follow it."
    except Exception as e:
        st.session_state["imp_profile_msg"] = f"Could not read the profile: {e}"


def render_import(set_src):
    P = st.session_state.get("imp_profile", {})
    gen = st.session_state.get("imp_gen", 0)

    def pnum(name, default, lo, hi):
        try:
            return int(min(max(int(P.get(name, default)), lo), hi))
        except (TypeError, ValueError):
            return default

    st.write("Upload a scheduling file exported from your own system (.xlsx or .csv). "
             "The app finds the header row, guesses which column is which, and builds the orders, "
             "machine capacity, and working calendar. Check the guesses, then import.")

    c1, c2 = st.columns(2)
    with c1:
        up = st.file_uploader("Scheduling file (.xlsx or .csv)", type=["xlsx", "csv"],
                              key="import_file")
    with c2:
        pf = st.file_uploader("Saved column profile (optional, .json)", type="json",
                              key="imp_profile_file")
        st.button("Apply profile", on_click=apply_profile, disabled=pf is None)
    if st.session_state.get("imp_profile_msg"):
        st.caption(st.session_state["imp_profile_msg"])

    if up is None:
        st.info("Upload a file to start. Nothing is saved on the server.")
        return

    data, name = up.getvalue(), up.name
    fid = f"{name}-{len(data)}"

    def k(n):
        return f"imp_{n}_{fid}_{gen}"

    is_csv = name.lower().endswith(".csv")
    try:
        tabs_all = ["(csv file)"] if is_csv else pd.ExcelFile(io.BytesIO(data)).sheet_names
    except Exception as e:
        st.error(f"Could not read the file: {e}")
        return

    t1, t2 = st.columns([3, 1])
    tabs = t1.multiselect("Tabs to read", tabs_all, default=tabs_all, key=k("tabs"))
    header_override = t2.number_input("Header row (0 = detect automatically)", min_value=0,
                                      max_value=100, value=pnum("header_row", 0, 0, 100),
                                      step=1, key=k("hdr"))
    frames, read_notes, headers = load_frames(name, data, tabs, header_override)
    for n in read_notes:
        st.warning(n)
    if not frames:
        st.warning("No readable tabs. Check the tab list and the header row.")
        return
    st.caption("Header row used: " + ", ".join(f"{t}: row {h}" for t, h in headers.items()))

    cols = []
    for f in frames.values():
        for c in f.columns:
            if c not in cols:
                cols.append(c)
    guesses = auto_guess(cols)

    st.write("**Which column is which?** (guessed from the names; change any that are wrong)")
    raw_choice, chosen = {}, {}
    boxes = st.columns(3)
    for i, (key, label, required, _w) in enumerate(FIELDS):
        opts = cols if required else ["(none)"] + cols
        pref = P.get(key)
        g = pref if pref in opts else guesses.get(key)
        idx = opts.index(g) if g in opts else 0
        with boxes[i % 3]:
            v = st.selectbox(label + (" (required)" if required else ""), opts,
                             index=idx, key=k(key))
        raw_choice[key] = v
        chosen[key] = None if v == "(none)" else v

    status_values, keep = [], ""
    if chosen["status"]:
        seen = sorted({clean_text(v) for f in frames.values() if chosen["status"] in f.columns
                       for v in f[chosen["status"]].dropna().unique()})
        st.caption("Values found in the status column: " + ", ".join(seen[:30]))
        keep = st.text_input("Keep rows whose status is (comma-separated; empty keeps every row)",
                             value=str(P.get("keep_values", "Tentative")), key=k("keep"))
        status_values = [x.strip().lower() for x in keep.split(",") if x.strip()]
    else:
        st.caption("No status column chosen, so every row will be imported.")

    time_unit = "minutes"
    if chosen["minutes"]:
        d_unit = P.get("time_unit")
        if d_unit not in ("minutes", "hours"):
            low = chosen["minutes"].lower()
            d_unit = "hours" if ("hour" in low or "hrs" in low) else "minutes"
        time_unit = st.selectbox("The time column is in", ["minutes", "hours"],
                                 index=["minutes", "hours"].index(d_unit), key=k("tunit"))

    s1, s2, s3, s4 = st.columns(4)
    start = s1.date_input("Day 1 is", value=date.today(), key=k("start"))
    def_min = s2.number_input("Default minutes per unit", min_value=1, max_value=100000,
                              value=pnum("default_minutes", 60, 1, 100000), step=1, key=k("defmin"))
    def_pri = s3.number_input("Default priority", min_value=1, max_value=1000,
                              value=pnum("default_priority", 1, 1, 1000), step=1, key=k("defpri"))
    dayfirst = s4.checkbox("Dates are day-first (31/12/2026)", value=bool(P.get("dayfirst", False)),
                           key=k("dayfirst"))

    w1, w2 = st.columns([3, 1])
    wd_default = [d for d in P.get("weekdays", DAY_NAMES[:5]) if d in DAY_NAMES] or DAY_NAMES[:5]
    wd = w1.multiselect("Working days", DAY_NAMES, default=wd_default, key=k("wd"))
    extra_days = w2.number_input("Extra days after the last due date", min_value=0, max_value=60,
                                 value=pnum("extra_days", 7, 0, 60), step=1, key=k("extra"))
    if not wd:
        st.error("Choose at least one working day.")
        return

    m1, m2 = st.columns(2)
    per_day = m1.number_input("Minutes available per machine per working day", min_value=0,
                              max_value=1440, value=pnum("minutes_per_day", 480, 0, 1440),
                              step=10, key=k("perday"))
    machines_text = m2.text_input("Machines to use when the file doesn't name them (comma-separated)",
                                  value=str(P.get("machines_text", "Line 1, Line 2")), key=k("mtext"))

    rows, pnotes = parse_rows(frames, chosen, chosen["status"], status_values, time_unit,
                              float(def_min), float(def_pri), dayfirst)
    for n in pnotes:
        st.warning(n)
    if not rows:
        st.info("No matching rows found. Check the column choices and the status value.")
        return

    weekdays = [DAY_NAMES.index(d) for d in wd]
    orders, machine_sets, dates, anotes = assemble(rows, start, weekdays, int(extra_days))
    for n in anotes:
        st.warning(n)

    file_machines = sorted({mm for s in machine_sets.values() for mm in s})
    typed, seen_m = [], set()
    for x in machines_text.split(","):
        x = x.strip()
        if x and x not in seen_m:
            seen_m.add(x)
            typed.append(x)
    machine_names = file_machines or typed
    if not machine_names:
        st.error("The file names no machines. Type at least one machine above.")
        return

    st.write(f"**{len(rows)} matching row(s) became {len(orders)} order line(s).**")
    st.caption(f"Machines: {', '.join(machine_names)}"
               + (" (named in the file)." if file_machines else " (typed above)."))
    st.caption(f"Day 1 = {dates[0]:%a %Y-%m-%d}, last planned day (day {len(dates)}) = "
               f"{dates[-1]:%a %Y-%m-%d}. Non-working days are skipped.")
    st.dataframe(orders, hide_index=True)

    if len(orders) > MAX_PRODUCTS:
        st.error(f"This app handles up to {MAX_PRODUCTS} order lines. Filter the file or raise the limit in app.py.")
    if len(machine_names) > MAX_MACHINES:
        st.error(f"This app handles up to {MAX_MACHINES} machines.")

    profile = dict(raw_choice)
    profile.update({
        "keep_values": keep, "time_unit": time_unit, "header_row": int(header_override),
        "weekdays": wd, "extra_days": int(extra_days), "default_minutes": int(def_min),
        "default_priority": int(def_pri), "minutes_per_day": int(per_day),
        "machines_text": machines_text, "dayfirst": bool(dayfirst),
    })
    b1, b2 = st.columns(2)
    go = b1.button("Import and replace tables 1-3", type="primary", key=k("go"))
    b2.download_button("Save these column choices as a profile", json.dumps(profile, indent=2),
                       file_name="column_profile.json", mime="application/json")

    if go:
        prod_tbl, mach_tbl, allowed_tbl = build_tables(orders, machine_sets, dates,
                                                       machine_names, int(per_day))
        set_src((prod_tbl, mach_tbl, allowed_tbl, empty_usage(), empty_stock()))
        st.session_state.msgs = [(
            "success",
            f"Imported {len(prod_tbl)} order lines, {len(machine_names)} machine(s), and "
            f"{len(dates)} working day(s). Materials were not imported. "
            "Check tables 1-3, then click Make plan.")]
        st.rerun()