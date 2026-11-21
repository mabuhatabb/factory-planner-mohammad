import io
import pandas as pd
import streamlit as st
import importer
from planner import (make_plan, build_materials, empty_usage, empty_stock,
                     DEFAULT_SETTINGS)

st.set_page_config(page_title="Production Planner", layout="wide")

# Font and size. Change 112.5% (100% = normal) or 'Inter' to taste.
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap');
html { font-size: 112.5%; }
.stApp, .stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp p, .stApp label,
.stApp li, .stApp input, .stApp textarea, .stApp button {
    font-family: 'Inter', sans-serif !important;
}
.stApp h1 { font-size: 2.2rem; }
.stApp h2 { font-size: 1.6rem; }
.stApp h3 { font-size: 1.25rem; }
</style>
""", unsafe_allow_html=True)

# Limits that keep a public app responsive
MAX_PRODUCTS = 50
MAX_MACHINES = 10
MAX_DAYS = 31
MAX_QTY = 100000
MAX_UPLOAD_ROWS = 1000

PROD_COLS = ["product", "demand", "due_day", "priority", "minutes_per_unit"]
MACH_COLS = ["machine", "day", "minutes_available"]
ALLOWED_COLS = ["product", "machine"]
USAGE_COLS = ["product", "material", "kg_per_unit"]
STOCK_COLS = ["material", "day", "kg_available"]


# ---------- DATA HELPERS ----------
def example_data():
    prod = pd.DataFrame([
        {"product": "Product A", "demand": 40, "due_day": 1, "priority": 1, "minutes_per_unit": 10},
        {"product": "Product B", "demand": 30, "due_day": 2, "priority": 2, "minutes_per_unit": 15},
    ])
    mach = pd.DataFrame([
        {"machine": m, "day": d, "minutes_available": 200}
        for m in ["Line 1", "Line 2"] for d in [1, 2, 3]
    ])
    allowed = pd.DataFrame([
        {"product": "Product A", "machine": "Line 1"},
        {"product": "Product B", "machine": "Line 1"},
        {"product": "Product B", "machine": "Line 2"},
    ])
    usage = pd.DataFrame([
        {"product": "Product A", "material": "Steel", "kg_per_unit": 2},
        {"product": "Product B", "material": "Steel", "kg_per_unit": 3},
    ])
    stock = pd.DataFrame([
        {"material": "Steel", "day": d, "kg_available": 100} for d in [1, 2, 3]
    ])
    return prod, mach, allowed, usage, stock


def _blank(text_cols, num_cols):
    data = {c: pd.Series(dtype="str") for c in text_cols}
    data.update({c: pd.Series(dtype="float") for c in num_cols})
    return pd.DataFrame(data)


def blank_data():
    return (
        _blank(["product"], ["demand", "due_day", "priority", "minutes_per_unit"]),
        _blank(["machine"], ["day", "minutes_available"]),
        _blank(["product", "machine"], []),
        _blank(["product", "material"], ["kg_per_unit"]),
        _blank(["material"], ["day", "kg_available"]),
    )


def read_workbook(file):
    """Read an uploaded workbook into the five input tables."""
    xls = pd.ExcelFile(file)
    required = [("products", PROD_COLS), ("machines", MACH_COLS), ("allowed", ALLOWED_COLS)]
    optional = [("usage", USAGE_COLS), ("stock", STOCK_COLS)]
    blanks = blank_data()
    tables = []
    for i, (sheet, cols) in enumerate(required + optional):
        if sheet not in xls.sheet_names:
            if (sheet, cols) in required:
                raise ValueError(f"Sheet '{sheet}' is missing.")
            tables.append(blanks[i])
            continue
        df = xls.parse(sheet)
        missing = [c for c in cols if c not in df.columns]
        if missing:
            raise ValueError(f"Sheet '{sheet}' is missing columns: {', '.join(missing)}.")
        if len(df) > MAX_UPLOAD_ROWS:
            raise ValueError(f"Sheet '{sheet}' has more than {MAX_UPLOAD_ROWS} rows.")
        tables.append(df[cols].copy())
    return tuple(tables)


def whole_numbers(df, cols):
    return bool((df[cols] % 1 != 0).any().any())


def build_inputs(prod, mach, allowed, usage_df, stock_df, use_materials):
    """Check the tables and turn them into model inputs.
    Raises ValueError(list_of_messages) when something must be fixed."""
    errors, notes = [], []

    prod = prod.dropna(subset=PROD_COLS).copy()
    mach = mach.dropna(subset=MACH_COLS).copy()
    allowed = allowed.dropna(subset=ALLOWED_COLS).copy()
    usage_df = usage_df.dropna(subset=USAGE_COLS).copy()
    stock_df = stock_df.dropna(subset=STOCK_COLS).copy()

    for df, col in ((prod, "product"), (mach, "machine"),
                    (allowed, "product"), (allowed, "machine"),
                    (usage_df, "product"), (usage_df, "material"),
                    (stock_df, "material")):
        df[col] = df[col].astype(str).str.strip()

    try:
        num = [(prod, PROD_COLS[1:]), (mach, MACH_COLS[1:]),
               (usage_df, ["kg_per_unit"]), (stock_df, STOCK_COLS[1:])]
        for df, cols in num:
            for c in cols:
                df[c] = pd.to_numeric(df[c])
    except (ValueError, TypeError):
        raise ValueError(["A number column contains text. Use whole numbers only."])

    if prod.empty:
        errors.append("Table 1 has no complete rows. Add at least one order.")
    if mach.empty:
        errors.append("Table 2 has no complete rows. Add machine minutes for at least one day.")

    for df, cols in num:
        if not df.empty and whole_numbers(df, cols):
            errors.append("Use whole numbers only (no decimals) in the number columns.")
            break

    dup = prod["product"][prod["product"].duplicated()].unique()
    if len(dup):
        errors.append(f"Duplicate product names in table 1: {', '.join(dup)}.")
    if mach.duplicated(["machine", "day"]).any():
        errors.append("Table 2 lists the same machine and day more than once.")
    if ((prod["demand"] < 0) | (prod["demand"] > MAX_QTY)).any():
        errors.append(f"Quantity ordered must be between 0 and {MAX_QTY}.")
    if (prod["due_day"] < 1).any():
        errors.append("Due day must be 1 or more.")
    if (prod["priority"] < 1).any():
        errors.append("Priority must be 1 or more.")
    if (prod["minutes_per_unit"] < 1).any():
        errors.append("Minutes per unit must be 1 or more.")
    if (mach["day"] < 1).any():
        errors.append("Table 2: day must be 1 or more.")
    if (mach["minutes_available"] < 0).any():
        errors.append("Table 2: minutes available cannot be negative.")
    if (usage_df["kg_per_unit"] < 0).any() or (stock_df["kg_available"] < 0).any():
        errors.append("Material amounts cannot be negative.")
    if len(prod) > MAX_PRODUCTS:
        errors.append(f"This app handles up to {MAX_PRODUCTS} products.")
    if mach["machine"].nunique() > MAX_MACHINES:
        errors.append(f"This app handles up to {MAX_MACHINES} machines.")
    if mach["day"].nunique() > MAX_DAYS:
        errors.append(f"This app handles up to {MAX_DAYS} days.")
    if errors:
        raise ValueError(errors)

    machines = sorted(mach["machine"].unique())
    days = sorted(int(d) for d in mach["day"].unique())
    capacity = {(r.machine, int(r.day)): int(r.minutes_available) for r in mach.itertuples()}

    products, dropped = {}, 0
    for r in prod.itertuples():
        wanted = allowed[allowed["product"] == r.product]["machine"].unique().tolist()
        ok = [m for m in wanted if m in machines]
        dropped += len(wanted) - len(ok)
        products[r.product] = {
            "demand": int(r.demand),
            "due_day": int(r.due_day),
            "priority": int(r.priority),
            "minutes_per_unit": int(r.minutes_per_unit),
            "machines": ok,
        }

    if dropped:
        notes.append(f"{dropped} row(s) in table 3 name a machine that is not in table 2. They were ignored.")
    unknown = allowed[~allowed["product"].isin(list(products))]
    if len(unknown):
        notes.append(f"{len(unknown)} row(s) in table 3 name a product that is not in table 1. They were ignored.")
    no_machine = [p for p, info in products.items() if not info["machines"]]
    if no_machine:
        notes.append(f"No machine is allowed for: {', '.join(no_machine)}. Add a row in table 3.")

    usage, stock = build_materials(usage_df, stock_df)
    if usage and max(usage.values()) > 1000:
        notes.append("Very large material use per unit in table 4. Check for typos.")
    if use_materials:
        unlimited = sorted({m for _, m in usage} - {m for m, _ in stock})
        if unlimited:
            notes.append(f"No stock listed in table 5 for: {', '.join(unlimited)}. "
                         "These materials are treated as unlimited. Check the spelling.")
    return products, machines, days, capacity, usage, stock, notes


def make_workbook(prod, mach, allowed, usage_df, stock_df, settings, runs):
    def clean(df, cols):
        return df[cols].dropna(how="all")

    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        clean(prod, PROD_COLS).to_excel(writer, sheet_name="products", index=False)
        clean(mach, MACH_COLS).to_excel(writer, sheet_name="machines", index=False)
        clean(allowed, ALLOWED_COLS).to_excel(writer, sheet_name="allowed", index=False)
        clean(usage_df, USAGE_COLS).to_excel(writer, sheet_name="usage", index=False)
        clean(stock_df, STOCK_COLS).to_excel(writer, sheet_name="stock", index=False)
        pd.DataFrame([{"parameter": k, "value": str(v)} for k, v in settings.items()]
                     ).to_excel(writer, sheet_name="parameters", index=False)
        if runs:
            pd.DataFrame([{
                "plan_id": r["id"], "name": r["name"],
                "late_units": r["late_units"], "units_not_made": r["units_not_made"],
                "overtime_minutes": r["overtime_minutes"],
            } for r in runs]).to_excel(writer, sheet_name="runs", index=False)
            frames = []
            for r in runs:
                f = r["plan"].copy()
                f.insert(0, "plan_id", r["id"])
                f.insert(1, "plan_name", r["name"])
                frames.append(f)
            pd.concat(frames).to_excel(writer, sheet_name="plans", index=False)
    return buffer.getvalue()


def po_summary(summary, plan):
    """Roll the result up by PO. Works when product names look like 'PO | unit'."""
    s = summary.copy()
    s["PO"] = s["product"].map(lambda p: p.split(" | ")[0] if " | " in p else None)
    s = s[s["PO"].notna()]
    if s.empty:
        return None
    g = s.groupby("PO", sort=False).agg(
        ordered=("ordered", "sum"), made=("made", "sum"),
        on_time=("on_time", "sum"), late=("late", "sum")).reset_index()
    p = plan.copy()
    p["PO"] = p["product"].map(lambda x: x.split(" | ")[0] if " | " in x else None)
    last = p.dropna(subset=["PO"]).groupby("PO")["day"].max()
    g["last_day_made"] = g["PO"].map(last).astype("Int64")
    g["not_made"] = g["ordered"] - g["made"]

    def status(r):
        if r["not_made"] > 0:
            return "Not complete"
        return "Late" if r["late"] > 0 else "On time"

    g["status"] = g.apply(status, axis=1)
    return g[["PO", "ordered", "made", "not_made", "on_time", "late",
              "last_day_made", "status"]]


# ---------- SESSION STATE (private to each visitor) ----------
for key, default in (("ver", 0), ("runs", []), ("feedback", []), ("next_id", 1),
                     ("last", None), ("msgs", []), ("last_upload", None)):
    if key not in st.session_state:
        st.session_state[key] = default
if "src" not in st.session_state:
    st.session_state.src = example_data()


def set_src(data):
    st.session_state.src = data
    st.session_state.ver += 1  # new editor keys reset the tables
    st.session_state.last = None
    st.session_state.msgs = []


# ---------- HEADER ----------
st.title("Production Planner")
st.caption("Enter your products, machines, and limits. The planner suggests what to make, "
           "on which machine, and when. It is a recommendation for a person to review.")
st.info("Your tables and plans are kept only for your current session and cleared when you "
        "refresh or close the page. Use **Download my data** to keep your work.")

with st.expander("How to use this app"):
    st.markdown(
        "1. Click **Load example** to see how it works, **Start blank** to enter your own, "
        "or **import your own scheduling file** (Excel or CSV).\n"
        "2. Fill table 1 (orders) and table 2 (machine minutes per day).\n"
        "3. In table 3, choose which machines can make each product.\n"
        "4. Optional: tables 4 and 5 limit the material available each day.\n"
        "5. Adjust the **Parameters** in the sidebar (overtime, scoring, rules).\n"
        "6. Click **Make plan**, then review it, give feedback, and compare plans."
    )

with st.expander("What does each column mean?"):
    st.markdown("""
| Table | Column | Meaning |
| --- | --- | --- |
| 1. Orders | Product | Name of the product or order line (must be unique) |
| 1. Orders | Quantity ordered | How many units were ordered |
| 1. Orders | Due day | Day number the order is due; later units count as late |
| 1. Orders | Priority | Higher number = more important |
| 1. Orders | Minutes per unit | Machine minutes to make one unit |
| 2. Machine minutes | Machine | Machine, line, or team name |
| 2. Machine minutes | Day | Day number, starting at 1 |
| 2. Machine minutes | Minutes available | Minutes the machine can run that day (0 = down) |
| 3. Allowed machines | Product / Machine | Which machine can make which product |
| 4. Material per unit | Material, Kg per unit | Material name and kilograms used per unit |
| 5. Material per day | Material, Day, Kg available | Kilograms available each day (no carry-over) |
""")

b1, b2, b3 = st.columns([1, 1, 4])
if b1.button("Load example",
             help="Fills the tables with a small demo so you can see how the app works."):
    set_src(example_data())
if b2.button("Start blank",
             help="Empties all five tables so you can enter your own data."):
    set_src(blank_data())
file = b3.file_uploader(
    "Or upload a workbook saved from this app (.xlsx)", type="xlsx",
    help="A workbook you downloaded earlier with 'Download my data'. "
         "It must have the sheets products, machines, and allowed.")

if file is not None:
    fid = getattr(file, "file_id", None) or f"{file.name}-{file.size}"
    if st.session_state.last_upload != fid:
        st.session_state.last_upload = fid
        try:
            set_src(read_workbook(file))
        except Exception as e:
            st.error(f"Could not read the file: {e}")

st.download_button(
    "Download example workbook (fill it in and upload it above)",
    make_workbook(*example_data(), DEFAULT_SETTINGS, []),
    file_name="planner_example.xlsx")

# ---------- PARAMETERS (sidebar) ----------
D = DEFAULT_SETTINGS
st.sidebar.header("Parameters")

st.sidebar.subheader("Scoring")
on_time_points = st.sidebar.number_input(
    "On-time points per unit", min_value=0, max_value=10000,
    value=D["on_time_points"], step=10,
    help="Score earned for each unit finished by its due day. "
         "The planner looks for the plan with the highest total score.")
late_points = st.sidebar.number_input(
    "Late points per unit", min_value=0, max_value=10000,
    value=D["late_points"], step=10,
    help="Score for each unit finished after its due day. "
         "Keep it below the on-time points so the planner prefers finishing on time.")
earlier_bonus = st.sidebar.number_input(
    "Points lost per day later", min_value=0, max_value=1000,
    value=D["earlier_bonus"], step=1,
    help="Points lost for every day a unit finishes later. "
         "A small number makes the planner finish work early.")
use_priority = st.sidebar.checkbox(
    "Use order priority", value=D["use_priority"],
    help="When on, each unit's score is multiplied by its order's priority, "
         "so important orders are planned first.")
if late_points > on_time_points:
    st.sidebar.warning("Late points are higher than on-time points, so the planner "
                       "will prefer finishing late.")

st.sidebar.subheader("Rules")
use_materials = st.sidebar.checkbox(
    "Enforce material limits", value=D["use_materials"],
    help="When on, the plan cannot use more material than table 5 lists for each day.")

st.sidebar.subheader("Overtime")
max_overtime = st.sidebar.number_input(
    "Max overtime per machine per day (minutes)", min_value=0, max_value=1000,
    value=D["max_overtime"], step=10,
    help="Extra minutes each machine may run beyond table 2 on any day. 0 means no overtime.")
overtime_cost = st.sidebar.number_input(
    "Overtime cost (points per minute)", min_value=0, max_value=1000,
    value=D["overtime_cost"], step=1,
    help="Points lost for each overtime minute. Overtime is only used when "
         "finishing on time is worth more than this cost.")

st.sidebar.subheader("Solver")
time_limit = st.sidebar.number_input(
    "Time limit (seconds)", min_value=1, max_value=30,
    value=min(D["time_limit_seconds"], 30), step=1,
    help="How long the planner may search for a better plan. "
         "10 seconds is enough for small problems.")

settings = {
    "on_time_points": int(on_time_points),
    "late_points": int(late_points),
    "earlier_bonus": int(earlier_bonus),
    "use_priority": bool(use_priority),
    "use_materials": bool(use_materials),
    "max_overtime": int(max_overtime),
    "overtime_cost": int(overtime_cost),
    "time_limit_seconds": int(time_limit),
}

# ---------- EDITABLE TABLES ----------
prod0, mach0, allowed0, usage0, stock0 = st.session_state.src
v = st.session_state.ver


def num_col(label, help=None):
    return st.column_config.NumberColumn(label, min_value=0, step=1, format="%d", help=help)


def names(df, col):
    return sorted({str(x).strip() for x in df[col].dropna() if str(x).strip()})


st.subheader("1. Orders")
st.caption("One row per product: quantity, the day it is due (1 = first day), "
           "priority (higher = more important), and minutes to make one unit.")
prod = st.data_editor(
    prod0, num_rows="dynamic", key=f"prod_{v}", hide_index=True,
    column_config={
        "product": st.column_config.TextColumn(
            "Product",
            help="Name of the product or order line. Each name must be unique, "
                 "for example Chair or PO-1001 | Escalator 30."),
        "demand": num_col(
            "Quantity ordered",
            "How many units were ordered. Whole number."),
        "due_day": num_col(
            "Due day",
            "Day number the order is due. Day 1 is the first day in table 2. "
            "Units finished after this day count as late."),
        "priority": num_col(
            "Priority",
            "How important the order is. A higher number means more important. "
            "Used when capacity is short, if 'Use order priority' is on."),
        "minutes_per_unit": num_col(
            "Minutes per unit",
            "Machine minutes needed to make one unit."),
    })
product_names = names(prod, "product")

col1, col2 = st.columns(2)
with col1:
    st.subheader("2. Machine minutes per day")
    st.caption("One row per machine and day.")
    mach = st.data_editor(
        mach0, num_rows="dynamic", key=f"mach_{v}", hide_index=True,
        column_config={
            "machine": st.column_config.TextColumn(
                "Machine",
                help="Name of the machine, line, or team. Spell it the same way everywhere."),
            "day": num_col(
                "Day",
                "Day number, starting at 1. These are the days the planner can use."),
            "minutes_available": num_col(
                "Minutes available",
                "Minutes this machine can run on this day (480 = 8 hours). "
                "Use 0 for a day the machine is down."),
        })
machine_names = names(mach, "machine")

with col2:
    st.subheader("3. Which machine can make which product")
    st.caption("Pick from the products and machines you entered.")
    allowed_cfg = {}
    if product_names:
        allowed_cfg["product"] = st.column_config.SelectboxColumn(
            "Product", options=product_names,
            help="A product from table 1. Pick it from the list.")
    if machine_names:
        allowed_cfg["machine"] = st.column_config.SelectboxColumn(
            "Machine", options=machine_names,
            help="A machine from table 2 that can make this product. "
                 "Add one row for each allowed machine.")
    allowed = st.data_editor(allowed0, num_rows="dynamic", key=f"allowed_{v}",
                             hide_index=True, column_config=allowed_cfg)

col3, col4 = st.columns(2)
with col3:
    st.subheader("4. Material used per unit (whole kg)")
    usage_cfg = {
        "material": st.column_config.TextColumn(
            "Material",
            help="Name of the material, for example Steel. "
                 "Spell it exactly the same way in table 5."),
        "kg_per_unit": num_col(
            "Kg per unit",
            "Kilograms of this material used to make one unit. Whole number."),
    }
    if product_names:
        usage_cfg["product"] = st.column_config.SelectboxColumn(
            "Product", options=product_names,
            help="A product from table 1 that uses this material.")
    usage_df = st.data_editor(usage0, num_rows="dynamic", key=f"usage_{v}",
                              hide_index=True, column_config=usage_cfg)
with col4:
    st.subheader("5. Material available per day (kg)")
    stock_df = st.data_editor(
        stock0, num_rows="dynamic", key=f"stock_{v}", hide_index=True,
        column_config={
            "material": st.column_config.TextColumn(
                "Material",
                help="Material name, spelled exactly as in table 4."),
            "day": num_col(
                "Day",
                "Day this material is available. Uses the same day numbers as table 2."),
            "kg_available": num_col(
                "Kg available",
                "Kilograms available on that day. Each day is planned separately, "
                "so unused material does not carry over."),
        })

d1, d2 = st.columns([1, 3])
d1.download_button(
    "Download my data",
    make_workbook(prod, mach, allowed, usage_df, stock_df, settings, st.session_state.runs),
    file_name="planner_data.xlsx")
d2.caption("The workbook has your tables, parameters, and plans. Upload it later to continue.")

# ---------- IMPORT FROM YOUR OWN SYSTEM ----------
with st.expander("Import from your own scheduling file (Excel or CSV)"):
    importer.render_import(set_src)

plan_name = st.text_input(
    "Plan name (optional, helps you find it later)", value="",
    help="A label for this plan, such as 'With overtime'. "
         "It appears in the plans list and in comparisons.")

# ---------- RUN ----------
if st.button("Make plan", type="primary",
             help="Checks your tables and parameters, then builds the plan."):
    st.session_state.last = None
    msgs = []
    try:
        products, machines, days, capacity, usage, stock, notes = build_inputs(
            prod, mach, allowed, usage_df, stock_df, settings["use_materials"])
    except (ValueError, KeyError, TypeError) as e:
        errs = e.args[0] if e.args and isinstance(e.args[0], list) else [str(e)]
        msgs = [("error", m) for m in errs]
    else:
        msgs = [("warning", n) for n in notes]
        plan_rows, summary_rows, material_rows, overtime_rows = make_plan(
            products, machines, days, capacity, usage, stock, settings=settings)

        if plan_rows is None:
            msgs.append(("error", "No plan found. Try a longer time limit or check your data."))
        elif not plan_rows:
            msgs.append(("warning", "Nothing can be made with this data. "
                                    "Check capacity, quantities, and allowed machines."))
        else:
            plan = pd.DataFrame(plan_rows)
            summary = pd.DataFrame(summary_rows)
            late_total = int(summary["late"].sum())
            not_made_total = int((summary["ordered"] - summary["made"]).sum())
            total_overtime = int(sum(r["overtime_minutes"] for r in overtime_rows))

            rid = st.session_state.next_id
            st.session_state.next_id += 1
            name = plan_name.strip() or "Untitled"
            st.session_state.runs.append({
                "id": rid, "name": name, "settings": dict(settings),
                "late_units": late_total, "units_not_made": not_made_total,
                "overtime_minutes": total_overtime, "plan": plan,
            })
            st.session_state.last = {
                "id": rid, "name": name, "settings": dict(settings),
                "days": days, "machines": machines,
                "summary": summary, "plan": plan,
                "material_rows": material_rows, "overtime_rows": overtime_rows,
                "late_units": late_total, "units_not_made": not_made_total,
                "overtime_minutes": total_overtime,
            }
    st.session_state.msgs = msgs

for level, text in st.session_state.msgs:
    getattr(st, level)(text)


def show_result(r):
    st.success(f"Plan #{r['id']} ({r['name']}) added to this session's plans below.")
    st.caption(f"Days: {r['days']}. Machines: {r['machines']}.")
    st.caption("Parameters used: " + ", ".join(f"{k} = {v}" for k, v in r["settings"].items()))

    st.subheader("Result per product")
    st.dataframe(r["summary"], hide_index=True)

    po = po_summary(r["summary"], r["plan"])
    if po is not None:
        st.subheader("Result per PO")
        st.dataframe(po, hide_index=True)

    c1, c2, c3 = st.columns(3)
    c1.metric("Late units", r["late_units"])
    c2.metric("Units not made", r["units_not_made"])
    c3.metric("Overtime minutes", r["overtime_minutes"])

    st.subheader("Plan")
    st.dataframe(r["plan"], hide_index=True)

    if r["overtime_rows"]:
        st.subheader("Overtime used")
        st.dataframe(pd.DataFrame(r["overtime_rows"]), hide_index=True)

    if r["material_rows"]:
        st.subheader("Material used vs available")
        st.dataframe(pd.DataFrame(r["material_rows"]), hide_index=True)

    st.subheader("Units per day by machine")
    st.bar_chart(r["plan"].pivot_table(index="day", columns="machine",
                                       values="qty", aggfunc="sum").fillna(0))

    buffer = io.BytesIO()
    r["plan"].to_excel(buffer, index=False)
    st.download_button("Download this plan as Excel", buffer.getvalue(),
                       file_name=f"plan_{r['id']}.xlsx")


if st.session_state.last:
    show_result(st.session_state.last)

# ---------- PLANS FROM THIS SESSION ----------
st.divider()
st.header("Plans from this session")

if "flash" in st.session_state:
    st.success(st.session_state.pop("flash"))

runs = st.session_state.runs

if not runs:
    st.write("No plans yet. Click **Make plan** to create the first one.")
else:
    latest = {}
    for f in st.session_state.feedback:
        latest[f["plan"]] = f["decision"]

    st.dataframe(pd.DataFrame([{
        "plan": r["id"], "name": r["name"], "late units": r["late_units"],
        "units not made": r["units_not_made"], "overtime minutes": r["overtime_minutes"],
        "decision": latest.get(r["id"], "Not reviewed"),
    } for r in reversed(runs)]), hide_index=True)

    labels = {r["id"]: f"#{r['id']} {r['name']}" for r in runs}
    ids = [r["id"] for r in reversed(runs)]  # newest first
    by_id = {r["id"]: r for r in runs}

    # ----- planner feedback -----
    st.subheader("Planner feedback")
    st.write("Did the planner use this plan? If they changed it, write what they changed and why.")

    fb_id = st.selectbox("Plan to review", ids, index=0, format_func=lambda i: labels[i],
                         help="The plan this feedback is about.")
    decision = st.radio("Decision", ["Accepted as is", "Changed by planner", "Rejected"],
                        horizontal=True,
                        help="Accepted: used without changes. Changed: used after edits. "
                             "Rejected: not used.")
    reason = st.text_area("Reason (what was wrong, or what was changed)",
                          help="Required for Changed and Rejected. Repeated reasons show "
                               "which rule to add to the planner next.")

    if st.button("Save feedback"):
        if decision != "Accepted as is" and not reason.strip():
            st.warning("Please write a short reason. It tells you which rule to add next.")
        else:
            st.session_state.feedback.append({
                "plan": fb_id, "name": by_id[fb_id]["name"],
                "decision": decision, "reason": reason.strip(),
            })
            st.session_state["flash"] = f"Feedback saved for plan #{fb_id}."
            st.rerun()

    if st.session_state.feedback:
        st.write("**Feedback log**")
        st.dataframe(pd.DataFrame(list(reversed(st.session_state.feedback))),
                     hide_index=True)

    # ----- compare two plans -----
    if len(runs) >= 2:
        st.subheader("Compare two plans")
        pick1, pick2 = st.columns(2)
        old_id = pick1.selectbox("Older plan", ids, index=1, format_func=lambda i: labels[i],
                                 help="The plan to compare against.")
        new_id = pick2.selectbox("Newer plan", ids, index=0, format_func=lambda i: labels[i],
                                 help="The plan whose changes you want to see.")
        old, new = by_id[old_id], by_id[new_id]

        m1, m2, m3 = st.columns(3)
        m1.metric("Late units", new["late_units"],
                  delta=new["late_units"] - old["late_units"], delta_color="inverse")
        m2.metric("Units not made", new["units_not_made"],
                  delta=new["units_not_made"] - old["units_not_made"], delta_color="inverse")
        m3.metric("Overtime minutes", new["overtime_minutes"],
                  delta=new["overtime_minutes"] - old["overtime_minutes"], delta_color="inverse")

        a = old["plan"].rename(columns={"qty": "qty_older"})
        b = new["plan"].rename(columns={"qty": "qty_newer"})
        diff = a.merge(b, on=["day", "product", "machine"], how="outer").fillna(0)
        diff["change"] = diff["qty_newer"] - diff["qty_older"]
        diff = diff[diff["change"] != 0].sort_values(["day", "product", "machine"])

        st.write("**What changed in the schedule** (only rows that differ):")
        if diff.empty:
            st.write("The two plans are identical.")
        else:
            st.dataframe(diff.astype({"qty_older": int, "qty_newer": int, "change": int}),
                         hide_index=True)