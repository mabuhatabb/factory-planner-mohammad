"""Import orders from a customer's own Excel/CSV file into the Orders table."""
import csv
import io
import re

import pandas as pd
import streamlit as st

import tables

# field -> (label, words that suggest the column, required)
FIELDS = {
    "order_id": ("Order / PO number", ["po", "order", "project", "job", "contract"], True),
    "product_type": ("Product type", ["product", "type", "model", "unit", "equipment"], True),
    "quantity": ("Quantity", ["qty", "quantity", "units", "count", "no. of"], False),
    "due_date": ("Due / site date", ["due", "delivery", "site", "deadline", "need"], True),
    "priority": ("Priority", ["priority", "rank", "importance"], False),
    "start_not_before": ("Earliest start", ["release", "earliest", "start", "kickoff"], False),
    "status": ("Status", ["status", "state", "stage"], False),
}


def _norm(s):
    return re.sub(r"[^a-z0-9 ]", " ", str(s).lower())


def _score(col, words):
    c = _norm(col)
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


def build_orders(frames, mapping, status_col_values=None, default_priority=1, default_qty=1):
    """Combine the chosen sheets into the Orders table.

    mapping: {sheet: {field: column}}. status_col_values: statuses to keep (None = keep all).
    """
    rows = []
    for sheet, m in mapping.items():
        df = frames[sheet]
        for _, r in df.iterrows():
            get = lambda f: r[m[f]] if m.get(f) in df.columns else None
            row = {f: get(f) for f in tables.ORDERS}
            st_val = tables._text(row.get("status"))
            if status_col_values is not None and st_val.lower() not in [s.lower() for s in status_col_values]:
                continue
            if not tables._text(row["order_id"]):
                continue
            row["quantity"] = tables._num(row["quantity"], default_qty)
            row["priority"] = tables._num(row["priority"], default_priority)
            row["due_date"] = tables._date(row["due_date"])
            row["start_not_before"] = tables._date(row["start_not_before"])
            row["order_id"] = tables._text(row["order_id"])
            row["product_type"] = tables._text(row["product_type"])
            row["status"] = st_val
            rows.append(row)
    return pd.DataFrame(rows, columns=tables.ORDERS)


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
    st.dataframe(orders, hide_index=True, width="stretch")
    if len(orders) and st.button("Use these orders", type="primary", key="imp_use"):
        on_done(orders)
