"""Import orders from a customer's own Excel/CSV file into the Orders table."""
import csv
import io
import re

import pandas as pd
import streamlit as st

import tables

# field -> (label, words that suggest the column, required)
FIELDS = {
    "order_id": ("Order / PO number", ["po", "order", "project", "proj", "job", "contract", "ref", "reference", "id", "number"], True),
    "product_type": ("Product type", ["product", "type", "model", "unit", "equipment"], True),
    "quantity": ("Quantity", ["qty", "quantity", "units", "count", "no. of"], False),
    "due_date": ("Due / site date", ["due", "delivery", "handover", "hand over", "completion", "site", "deadline", "need", "ship"], True),
    "priority": ("Priority", ["priority", "rank", "importance"], False),
    "start_not_before": ("Earliest start", ["release", "earliest", "start", "kickoff"], False),
    "status": ("Status", ["status", "state", "stage"], False),
    "order_value": ("Order value", ["value", "amount", "price", "revenue", "contract"], False),
}


def _norm(s):
    return re.sub(r"[^a-z0-9 ]", " ", str(s).lower())


NOT_A_FIELD = ["manager", "comment", "note", "name", "crane", "phone", "email", "address", "remark"]


def _score(col, words):
    c = _norm(col)
    if any(w in c for w in NOT_A_FIELD):
        return 0
    return max([len(w) / (len(c) + 1) + 1 for w in words if w in c] or [0])


def auto_guess(columns):
    """field -> best matching column (each column used once)."""
    out, used = {}, set()
    pairs = sorted(((_score(c, FIELDS[f][1]), f, c) for f in FIELDS for c in columns), reverse=True)
    for sc, f, c in pairs:
        if sc > 0 and f not in out and c not in used:
            out[f] = c
            used.add(c)
    return out


def detect_header(raw, max_scan=15):
    """Row number of the most header-looking row (many short text cells)."""
    best, best_row = -1, 0
    for i in range(min(max_scan, len(raw))):
        cells = [x for x in raw.iloc[i].tolist() if isinstance(x, str) and x.strip()]
        score = len(cells) - sum(len(x) > 40 for x in cells)
        if score > best:
            best, best_row = score, i
    return best_row


def read_frames(name, data):
    """Return {sheet name: DataFrame with real headers}."""
    frames = {}
    if name.lower().endswith((".xlsx", ".xlsm", ".xls")):
        xls = pd.ExcelFile(io.BytesIO(data))
        raws = {s: xls.parse(s, header=None) for s in xls.sheet_names}
    else:
        text = data.decode("utf-8-sig", errors="replace")
        try:
            sep = csv.Sniffer().sniff(text[:4000], delimiters=",;\t|").delimiter
        except csv.Error:
            sep = ","
        rows = list(csv.reader(io.StringIO(text), delimiter=sep))
        width = max((len(r) for r in rows), default=0)
        rows = [r + [None] * (width - len(r)) for r in rows]
        raws = {"file": pd.DataFrame(rows, dtype=object).replace("", None)}
    for s, raw in raws.items():
        raw = raw.dropna(how="all").dropna(axis=1, how="all").reset_index(drop=True)
        if raw.empty:
            continue
        h = detect_header(raw)
        cols, seen = [], {}
        for i, v in enumerate(raw.iloc[h].tolist()):
            v = str(v).strip() if isinstance(v, str) and v.strip() else f"Column {i + 1}"
            seen[v] = seen.get(v, 0) + 1
            cols.append(v if seen[v] == 1 else f"{v} ({seen[v]})")
        df = raw.iloc[h + 1:].copy()
        df.columns = cols
        frames[s] = df.dropna(how="all").reset_index(drop=True)
    return frames


def _money(v):
    """'$450,000' -> 450000.0, '1.2M' -> 1200000.0, 'n/a' -> None."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return None if pd.isna(v) else float(v)
    t = re.sub(r"[^0-9.kKmM-]", "", str(v or ""))
    if not t or t in ("-", "."):
        return None
    mult = 1
    if t[-1] in "kK":
        mult, t = 1_000, t[:-1]
    elif t[-1] in "mM":
        mult, t = 1_000_000, t[:-1]
    try:
        return float(t) * mult
    except ValueError:
        return None


def _count(v):
    """5, '5', '5 pcs', '12' -> number; 'two', '' -> None."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return None if pd.isna(v) else float(v)
    m = re.match(r"\s*(-?\d+(?:[.,]\d+)?)", str(v or ""))
    return float(m.group(1).replace(",", ".")) if m else None


def _when(v):
    """Dates written as text, real dates, or Excel serial numbers. Anything else -> None."""
    if isinstance(v, (int, float)) and not isinstance(v, bool) and not pd.isna(v):
        if 20000 < v < 80000:
            return (pd.Timestamp("1899-12-30") + pd.Timedelta(days=float(v))).date()
        return None
    return tables._date(v)


SKIP_IDS = ("total", "sum", "subtotal", "grand total", "average", "count")


def build_orders(frames, mapping, status_col_values=None, default_priority=1, default_qty=1):
    """Combine the chosen sheets into the Orders table.

    mapping: {sheet: {field: column}}. status_col_values: statuses to keep (None = keep all).
    """
    rows, notes, seen = [], [], {}
    for sheet, m in mapping.items():
        df = frames[sheet]
        for n, (_, r) in enumerate(df.iterrows(), start=1):
            get = lambda f: r[m[f]] if m.get(f) in df.columns else None
            row = {f: get(f) for f in tables.ORDERS}
            st_val = tables._text(row.get("status"))
            if status_col_values is not None and st_val.lower() not in [s.lower() for s in status_col_values]:
                continue
            oid = tables._text(row["order_id"])
            if not oid or oid.lower().startswith(SKIP_IDS):
                continue
            where = f"{sheet}, row {n}: {oid}"
            q = _count(row["quantity"])
            if q is None:
                if tables._text(row["quantity"]):
                    notes.append(f"{where}: quantity '{tables._text(row['quantity'])}' is not a number. Used {default_qty:g}.")
                q = default_qty
            elif q <= 0:
                notes.append(f"{where}: quantity {q:g} is not above zero. Used {default_qty:g}.")
                q = default_qty
            elif q != int(q):
                notes.append(f"{where}: quantity {q:g} is not a whole number. Rounded up.")
                q = float(-(-q // 1))
            p = _count(row["priority"])
            if p is None or not (1 <= p <= 10):
                if tables._text(row["priority"]):
                    notes.append(f"{where}: priority '{tables._text(row['priority'])}' is not 1 to 10. Used {default_priority}.")
                p = default_priority
            due = _when(row["due_date"])
            if due is None:
                notes.append(f"{where}: due date '{tables._text(row['due_date'])}' is missing or not a date. The order cannot be planned until you fix it.")
            rel = _when(row["start_not_before"])
            if rel is None and tables._text(row["start_not_before"]):
                notes.append(f"{where}: earliest start '{tables._text(row['start_not_before'])}' is not a date. Ignored.")
            val = _money(row["order_value"])
            if val is None and tables._text(row["order_value"]):
                notes.append(f"{where}: order value '{tables._text(row['order_value'])}' is not a number. Left blank.")
            if oid in seen:
                notes.append(f"{where}: the same order number appears again (first on {seen[oid]}). Both rows were kept: rename one.")
            seen.setdefault(oid, where)
            rows.append({"order_id": oid, "product_type": tables._text(row["product_type"]), "quantity": q,
                         "due_date": due, "priority": p, "start_not_before": rel, "status": st_val, "order_value": val})
    out = pd.DataFrame(rows, columns=tables.ORDERS)
    out.attrs["notes"] = notes
    return out


def render_import(on_done):
    """Streamlit panel. on_done(orders_df, missing_product_types) is called on 'Use these orders'."""
    up = st.file_uploader("Upload the customer's schedule file (Excel or CSV)", type=["xlsx", "xlsm", "csv"],
                          key="imp_file")
    if not up:
        return
    try:
        frames = read_frames(up.name, up.getvalue())
    except Exception as e:
        st.error(f"Could not read this file. ({e})")
        return
    if not frames:
        st.warning("The file looks empty.")
        return
    sheets = st.multiselect("Which tabs hold orders?", list(frames), default=list(frames)[:1], key="imp_sheets")
    mapping = {}
    for s in sheets:
        df = frames[s]
        guess = auto_guess(list(df.columns))
        st.markdown(f"**Tab: {s}** ({len(df)} rows). Tell me which column is which.")
        cols = ["(none)"] + list(df.columns)
        m = {}
        grid = st.columns(len(FIELDS))
        for g, (f, (label, _, req)) in zip(grid, FIELDS.items()):
            default = cols.index(guess[f]) if f in guess else 0
            pick = g.selectbox(label + (" *" if req else ""), cols, index=default, key=f"imp_{s}_{f}")
            if pick != "(none)":
                m[f] = pick
        missing = [FIELDS[f][0] for f, v in FIELDS.items() if v[2] and f not in m]
        if missing:
            st.warning("Still needed on this tab: " + ", ".join(missing))
            continue
        mapping[s] = m
    if not mapping:
        return
    keep = None
    status_cols = {s: m["status"] for s, m in mapping.items() if "status" in m}
    if status_cols:
        vals = sorted({tables._text(v) for s, c in status_cols.items() for v in frames[s][c]} - {""})
        default = [v for v in vals if v.lower() == "tentative"] or vals
        keep = st.multiselect("Keep only rows with these statuses", vals, default=default, key="imp_status")
    orders = build_orders(frames, mapping, keep)
    st.write(f"{len(orders)} orders found.")
    notes = orders.attrs.get("notes", [])
    if notes:
        with st.expander("1 thing to check in this file" if len(notes) == 1 else f"{len(notes)} things to check in this file", expanded=True):
            for n_ in notes:
                st.write("- " + n_)
    st.dataframe(orders, hide_index=True, width="stretch")
    if len(orders) and st.button("Use these orders", type="primary", key="imp_use"):
        on_done(orders)
