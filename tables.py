"""Table definitions, example data and the step that turns tables into solver input."""
import io
import math
from datetime import date, timedelta

import pandas as pd

ORDERS = ["order_id", "product_type", "quantity", "due_date", "priority", "start_not_before", "status", "order_value"]
ROUTING = ["product_type", "step_no", "step_name", "fixed_hours", "hours_per_unit", "resources",
           "skill_required", "materials"]
RESOURCES = ["resource", "crew_size", "hours_per_shift", "efficiency_pct", "work_days",
             "shifts_per_day", "changeover_hours", "skills"]
EXCEPTIONS = ["resource", "from_date", "to_date", "crew_available", "note"]
PARTS = ["order_id", "part", "needed_for_step", "arrival_date"]
STOCK = ["material", "available_from", "quantity"]

COLUMNS = {"orders": ORDERS, "routing": ROUTING, "resources": RESOURCES,
           "exceptions": EXCEPTIONS, "parts": PARTS, "stock": STOCK}
TITLES = {"orders": "Orders", "routing": "Routing", "resources": "Crews and machines",
          "exceptions": "Calendar exceptions", "parts": "Parts arrival",
          "stock": "Materials and stock"}

DAYNAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def blank():
    return {k: pd.DataFrame(columns=v) for k, v in COLUMNS.items()}


def example(today=None):
    t = today or date.today()
    monday = t - timedelta(days=t.weekday())
    d = lambda n: monday + timedelta(days=n)
    orders = pd.DataFrame([
        ["PO-1001", "Escalator 30deg", 2, d(35), 3, d(0), "Confirmed", 850000],
        ["PO-1002", "Escalator 30deg", 3, d(49), 2, d(7), "Confirmed", 1200000],
        ["PO-1003", "Moving walk", 2, d(42), 1, d(0), "Confirmed", 420000],
        ["PO-1004", "Escalator 35deg", 1, d(28), 3, d(0), "Confirmed", 380000],
        ["PO-1005", "Escalator 30deg", 4, d(70), 1, d(14), "Tentative", 1500000],
    ], columns=ORDERS)
    esc = [("Frame", 16, 24, "Frame crew", "", "Steel profile:2"),
           ("Welding", 8, 16, "Weld crew A, Weld crew B", "Welding", "Welding wire:1"),
           ("Paint", 4, 8, "Paint booth", "", "Paint:1"),
           ("Assembly", 16, 28, "Assembly crew", "", ""),
           ("Test", 4, 6, "Test bay", "Testing", "")]
    routing = []
    for pt, mult in [("Escalator 30deg", 1.0), ("Escalator 35deg", 1.1), ("Moving walk", 0.8)]:
        for i, (name, fixed, per, res, skill, mat) in enumerate(esc, 1):
            routing.append([pt, i, name, fixed, round(per * mult, 1), res, skill, mat])
    routing = pd.DataFrame(routing, columns=ROUTING)
    resources = pd.DataFrame([
        ["Frame crew", 3, 8, 100, "Mon,Tue,Wed,Thu,Fri", 1, 0, "Framing"],
        ["Weld crew A", 2, 8, 90, "Mon,Tue,Wed,Thu,Fri", 1, 0, "Welding"],
        ["Weld crew B", 2, 8, 100, "Mon,Tue,Wed,Thu,Fri", 1, 0, "Welding"],
        ["Paint booth", 2, 8, 100, "Mon,Tue,Wed,Thu,Fri", 1, 2, "Painting"],
        ["Assembly crew", 4, 8, 100, "Mon,Tue,Wed,Thu,Fri", 1, 4, "Assembly"],
        ["Test bay", 1, 8, 100, "Mon,Tue,Wed,Thu,Fri", 1, 0, "Testing"],
    ], columns=RESOURCES)
    exceptions = pd.DataFrame([
        ["ALL", d(21), d(21), 0, "Public holiday"],
        ["Assembly crew", d(14), d(18), 2, "Two people on vacation"],
        ["Paint booth", d(9), d(10), 0, "Maintenance"],
    ], columns=EXCEPTIONS)
    parts = pd.DataFrame([
        ["PO-1001", "Drive motors", 4, d(24)],
        ["PO-1002", "Steel truss", 1, d(10)],
        ["PO-1004", "Steps and chain", 4, d(18)],
    ], columns=PARTS)
    stock = pd.DataFrame([
        ["Steel profile", None, 40], ["Steel profile", d(14), 30],
        ["Welding wire", None, 60], ["Paint", None, 12], ["Paint", d(21), 20],
    ], columns=STOCK)
    return {"orders": orders, "routing": routing, "resources": resources,
            "exceptions": exceptions, "parts": parts, "stock": stock}


# ---------- cleaning ----------
def _date(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    try:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            out = pd.to_datetime(v, errors="coerce")
    except Exception:
        return None
    if pd.isna(out):
        return None
    return out.date()


def _num(v, default=None):
    try:
        f = float(v)
        if math.isnan(f):
            return default
        return f
    except (TypeError, ValueError):
        return default


def _text(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return ""
    return str(v).strip()


def normalize(tables):
    """Make sure every table exists with the right columns and no empty rows."""
    out = {}
    for name, cols in COLUMNS.items():
        df = tables.get(name)
        if df is None:
            df = pd.DataFrame(columns=cols)
        df = df.copy()
        for c in cols:
            if c not in df.columns:
                df[c] = None
        df = df[cols].dropna(how="all").reset_index(drop=True)
        out[name] = df
    return out


def parse_days_checked(text):
    """'Mon,Tue' -> ({0,1}, []). Also accepts 'Mon-Fri' and numbers 1-7. Second item lists words not understood."""
    s = _text(text).lower()
    if not s:
        return {0, 1, 2, 3, 4}, []
    names = [n.lower() for n in DAYNAMES]
    found, bad = set(), []
    for part in s.replace(";", ",").replace(" ", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = [p.strip()[:3] for p in part.split("-", 1)]
            if a in names and b in names:
                i, j = names.index(a), names.index(b)
                k = i
                while True:
                    found.add(k)
                    if k == j:
                        break
                    k = (k + 1) % 7
            else:
                bad.append(part)
            continue
        if part[:3] in names:
            found.add(names.index(part[:3]))
        elif part.isdigit() and 1 <= int(part) <= 7:
            found.add(int(part) - 1)
        else:
            bad.append(part)
    return found, bad


def parse_days(text):
    return parse_days_checked(text)[0]


class Cap(dict):
    """Capacity per resource and day, plus extra inputs for the solver (stock, changeover, clock hours)."""
    supply = {}      # material -> cumulative units available by day index
    changeover = {}  # resource -> labor hours lost when two product types share a day
    clock = {}       # resource -> clock hours of the working day, per day index


def parse_materials(text):
    """'Steel profile:2, Bolts:10' -> {'Steel profile': 2.0, 'Bolts': 10.0}; None if a quantity is not a number."""
    out = {}
    for part in _text(text).split(","):
        part = part.strip()
        if not part:
            continue
        name, _, q = part.partition(":")
        name = name.strip()
        try:
            qty = float(q) if q.strip() else 1.0
        except ValueError:
            return None
        if name:
            out[name] = out.get(name, 0.0) + qty
    return out


def prepare(tables, plan_start, settings):
    """Turn the tables into solver input.

    Returns (jobs, cap, days, problems, skipped).
    problems: list of text messages that stop or change the plan.
    skipped: list of dicts for orders left out, with a reason.
    """
    t = normalize(tables)
    problems, skipped = [], []
    horizon = int(settings.get("horizon_days", 90))
    days = [plan_start + timedelta(days=i) for i in range(horizon)]
    index = {d: i for i, d in enumerate(days)}

    # resources and capacity
    cap = Cap()
    cap.supply, cap.changeover, cap.clock = {}, {}, {}
    skills = {}
    for _, r in t["resources"].iterrows():
        name = _text(r["resource"])
        if not name:
            continue
        crew = _num(r["crew_size"], 1)
        shift = _num(r["hours_per_shift"], 8)
        eff = _num(r["efficiency_pct"], 100)
        shifts = _num(r["shifts_per_day"], 1)
        co = _num(r["changeover_hours"], 0)
        if crew is None or crew < 0 or shift is None or shift < 0 or eff is None or eff < 0:
            problems.append(f"Crews and machines: '{name}' has a negative or blank number.")
            continue
        if shifts is None or shifts < 1 or shifts > 3:
            problems.append(f"Crews and machines: '{name}' shifts per day must be 1, 2 or 3. Using 1.")
            shifts = 1
        if co is None or co < 0:
            problems.append(f"Crews and machines: '{name}' changeover hours must be 0 or more. Using 0.")
            co = 0
        wd, bad_days = parse_days_checked(r["work_days"])
        if bad_days:
            problems.append(f"Crews and machines: '{name}' work days has '{', '.join(bad_days)}', which is not a day. "
                            "Use names like Mon,Tue,Wed or Mon-Fri. " +
                            ("Using the days that were understood." if wd else "Using Mon-Fri until you fix it."))
        if not wd:
            wd = {0, 1, 2, 3, 4}
        base = int(math.floor(crew * shift * shifts * eff / 100.0))
        cap[name] = [base if d.weekday() in wd else 0 for d in days]
        cap.clock[name] = [shift * shifts if d.weekday() in wd else 0 for d in days]
        cap.changeover[name] = int(math.ceil(co))
        skills[name] = {x.strip().lower() for x in _text(r["skills"]).split(",") if x.strip()}
    for _, e in t["exceptions"].iterrows():
        target = _text(e["resource"])
        a, b = _date(e["from_date"]), _date(e["to_date"]) or _date(e["from_date"])
        if a is None:
            continue
        crew_left = _num(e["crew_available"], 0)
        names = list(cap) if target.upper() in ("ALL", "") else [target]
        for name in names:
            if name not in cap:
                problems.append(f"Calendar exceptions: resource '{target}' is not in Crews and machines.")
                break
            row = t["resources"][t["resources"]["resource"].astype(str).str.strip() == name].iloc[0]
            crew = _num(row["crew_size"], 1) or 0
            shift = _num(row["hours_per_shift"], 8)
            eff = _num(row["efficiency_pct"], 100)
            shifts = min(3, max(1, _num(row["shifts_per_day"], 1) or 1))
            wd = parse_days(row["work_days"]) or {0, 1, 2, 3, 4}
            hours_left = int(math.floor(min(crew_left, crew if crew > 0 else crew_left) * shift * shifts * eff / 100.0))
            d = a
            while d <= b:
                if d in index and d.weekday() in wd:
                    cap[name][index[d]] = hours_left
                d += timedelta(days=1)

    # stock: units available by each day (blank date = already on hand)
    for _, sk in t["stock"].iterrows():
        m = _text(sk["material"])
        q = _num(sk["quantity"], 0)
        if not m or q is None or q < 0:
            continue
        arr = _date(sk["available_from"])
        i0 = 0 if (arr is None or arr <= plan_start) else (arr - plan_start).days
        cum = cap.supply.setdefault(m, [0] * horizon)
        if i0 < horizon:
            for i in range(i0, horizon):
                cum[i] += int(math.floor(q))

    # routing
    routes = {}
    for _, r in t["routing"].iterrows():
        pt = " ".join(_text(r["product_type"]).lower().split())
        if not pt:
            continue
        routes.setdefault(pt, []).append(r)

    # parts arrival: step key -> latest arrival date
    part_dates = {}
    if settings.get("use_parts", True):
        for _, p in t["parts"].iterrows():
            oid = _text(p["order_id"])
            arr = _date(p["arrival_date"])
            if not oid or arr is None:
                continue
            step = _text(p["needed_for_step"])
            part_dates.setdefault((oid, step), []).append(arr)

    jobs = []
    seen = set()
    for _, o in t["orders"].iterrows():
        oid = _text(o["order_id"])
        if not oid:
            continue
        if oid in seen:
            problems.append(f"Orders: '{oid}' appears more than once.")
            continue
        seen.add(oid)
        pt_shown = _text(o["product_type"])
        pt = " ".join(pt_shown.lower().split())          # capital letters and extra spaces do not matter
        qty = _num(o["quantity"], None)
        due = _date(o["due_date"])
        if pt not in routes:
            skipped.append({"order_id": oid, "reason": f"No routing for product type '{pt_shown}'. Add it in Routing, or use a name that exists there."})
            continue
        if qty is None or qty <= 0:
            skipped.append({"order_id": oid, "reason": "Quantity is blank or not above zero."})
            continue
        if due is None:
            skipped.append({"order_id": oid, "reason": "Due date is blank or not a date."})
            continue
        prio = _num(o["priority"], 1)
        prio = int(min(max(prio, 1), 10))
        release = _date(o["start_not_before"]) or plan_start
        steps = []
        bad = None
        for r in sorted(routes[pt], key=lambda z: _num(z["step_no"], 0)):
            fixed = _num(r["fixed_hours"], 0)
            per = _num(r["hours_per_unit"], 0)
            names = [x.strip() for x in _text(r["resources"]).split(",") if x.strip()]
            need_skills = {x.strip().lower() for x in _text(r["skill_required"]).split(",") if x.strip()}
            if not names and need_skills:
                names = [n for n in cap if need_skills <= skills.get(n, set())]
            if not names:
                bad = f"Routing for '{pt}' step '{_text(r['step_name'])}' lists no resource."
                break
            unknown = [n for n in names if n not in cap]
            if unknown:
                bad = f"Routing step '{_text(r['step_name'])}' uses unknown resource: {', '.join(unknown)}."
                break
            if need_skills:
                names = [n for n in names if need_skills <= skills.get(n, set())]
                if not names:
                    bad = (f"No listed crew has the skill '{_text(r['skill_required'])}' needed for step "
                           f"'{_text(r['step_name'])}'. Add the skill to a crew in Crews and machines.")
                    break
            mats = parse_materials(r["materials"])
            if mats is None:
                bad = f"Routing step '{_text(r['step_name'])}': materials must look like  Steel:2, Bolts:10."
                break
            no_stock = [m for m in mats if m not in cap.supply]
            if no_stock:
                bad = (f"Routing step '{_text(r['step_name'])}' needs material '{no_stock[0]}' which is not in "
                       "Materials and stock.")
                break
            hours = int(math.ceil(max(0.0, fixed + per * qty)))
            if hours <= 0:
                bad = f"Routing step '{_text(r['step_name'])}' has zero hours."
                break
            sno = int(_num(r["step_no"], len(steps) + 1))
            sname = _text(r["step_name"]) or f"Step {sno}"
            earliest_date = release
            for key in ((oid, str(sno)), (oid, sname), (oid, str(float(sno)))):
                for arr in part_dates.get(key, []):
                    earliest_date = max(earliest_date, arr)
            earliest_idx = max(0, (earliest_date - plan_start).days)
            steps.append({"step_no": sno, "name": sname, "hours": hours,
                          "resources": names, "earliest": earliest_idx,
                          "materials": {m: int(math.ceil(q * qty)) for m, q in mats.items() if q * qty > 0}})
        if bad:
            skipped.append({"order_id": oid, "reason": bad})
            continue
        jobs.append({"order_id": oid, "priority": prio, "product": pt,
                     "due_idx": (due - plan_start).days, "steps": steps})
    return jobs, cap, days, problems, skipped


# ---------- workbook / JSON ----------
def to_workbook(tables):
    buf = io.BytesIO()
    t = normalize(tables)
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for name, df in t.items():
            df.to_excel(xw, sheet_name=name, index=False)
    return buf.getvalue()


def from_workbook(file):
    """Read a planner workbook (sheets named orders, routing, resources, exceptions, parts, stock)."""
    xls = pd.ExcelFile(file)
    keyed = {}
    for sn in xls.sheet_names:
        keyed[sn.strip().lower().replace(" ", "_")] = sn
    for k, title in TITLES.items():                       # the sheet may also carry the screen title
        keyed.setdefault(title.strip().lower().replace(" ", "_"), None)
    out, found = {}, 0
    for name in COLUMNS:
        sheet = keyed.get(name) or next((keyed[t] for t in (TITLES[name].strip().lower().replace(" ", "_"),) if keyed.get(t)), None)
        if sheet:
            out[name] = xls.parse(sheet)
            found += 1
        else:
            out[name] = pd.DataFrame(columns=COLUMNS[name])
    if not found:
        raise ValueError("This is not a planner workbook: it has no sheet named orders, routing, resources, exceptions, parts or stock "
                         f"(its sheets are: {', '.join(xls.sheet_names)}). To bring in your customer's schedule, use "
                         "'Import orders from your own schedule file' just below. Nothing was changed.")
    return normalize(out)


def to_json(tables):
    t = normalize(tables)
    return {k: v.astype(object).where(v.notna(), None).astype(str).replace("None", "").to_dict("records")
            if False else _records(v) for k, v in t.items()}


def _records(df):
    rows = []
    for _, r in df.iterrows():
        row = {}
        for c in df.columns:
            v = r[c]
            if v is None or (isinstance(v, float) and math.isnan(v)):
                row[c] = None
            elif isinstance(v, (date, pd.Timestamp)):
                row[c] = _date(v).isoformat()
            elif hasattr(v, "item"):
                row[c] = v.item()
            else:
                row[c] = v
        rows.append(row)
    return rows


def from_json(data):
    out = {}
    for name, cols in COLUMNS.items():
        df = pd.DataFrame(data.get(name, []), columns=cols)
        for c in cols:
            if c.endswith("_date") or c in ("start_not_before", "available_from"):
                df[c] = df[c].map(_date)
        out[name] = df
    return normalize(out)
