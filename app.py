import io
import pandas as pd
import streamlit as st
from planner import (make_plan, build_materials, empty_usage, empty_stock,
                     DEFAULT_SETTINGS)
from db import save_run, list_runs, load_lines, save_feedback, list_feedback

st.set_page_config(layout="wide")
st.title("Production Planner")
st.write("Edit the tables below (or upload an Excel file), set the parameters in the "
         "sidebar, then click **Make plan**.")

# ---------- PARAMETERS (sidebar) ----------
D = DEFAULT_SETTINGS
st.sidebar.header("Parameters")

st.sidebar.subheader("Scoring")
on_time_points = st.sidebar.number_input("On-time points per unit", min_value=0,
                                         max_value=10000, value=D["on_time_points"], step=10)
late_points = st.sidebar.number_input("Late points per unit", min_value=0,
                                      max_value=10000, value=D["late_points"], step=10)
earlier_bonus = st.sidebar.number_input("Points lost per day later", min_value=0,
                                        max_value=1000, value=D["earlier_bonus"], step=1)
use_priority = st.sidebar.checkbox("Use order priority", value=D["use_priority"])
if late_points > on_time_points:
    st.sidebar.warning("Late points are higher than on-time points, so the planner "
                       "will prefer finishing late.")

st.sidebar.subheader("Rules")
use_materials = st.sidebar.checkbox("Enforce material limits", value=D["use_materials"])

st.sidebar.subheader("Overtime")
max_overtime = st.sidebar.number_input("Max overtime per machine per day (minutes)",
                                       min_value=0, max_value=1000,
                                       value=D["max_overtime"], step=10)
overtime_cost = st.sidebar.number_input("Overtime cost (points per minute)",
                                        min_value=0, max_value=1000,
                                        value=D["overtime_cost"], step=1)

st.sidebar.subheader("Solver")
time_limit = st.sidebar.number_input("Time limit (seconds)", min_value=1,
                                     max_value=300, value=D["time_limit_seconds"], step=1)

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

file = st.file_uploader("Optional: upload data.xlsx", type="xlsx")


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


def build_inputs(prod, mach, allowed, usage_df, stock_df):
    prod = prod.dropna()
    mach = mach.dropna()
    allowed = allowed.dropna()

    products = {}
    for _, r in prod.iterrows():
        products[r["product"]] = {
            "demand": int(r["demand"]),
            "due_day": int(r["due_day"]),
            "priority": int(r["priority"]),
            "minutes_per_unit": int(r["minutes_per_unit"]),
            "machines": allowed[allowed["product"] == r["product"]]["machine"].tolist(),
        }
    capacity = {(r["machine"], int(r["day"])): int(r["minutes_available"])
                for _, r in mach.iterrows()}
    machines = sorted(mach["machine"].unique())
    days = sorted(int(d) for d in mach["day"].unique())
    usage, stock = build_materials(usage_df, stock_df)
    return products, machines, days, capacity, usage, stock


# ---------- LOAD STARTING DATA ----------
if file is not None:
    try:
        xls = pd.ExcelFile(file)
        prod0 = xls.parse("products")
        mach0 = xls.parse("machines")
        allowed0 = xls.parse("allowed")
        usage0 = xls.parse("usage") if "usage" in xls.sheet_names else empty_usage()
        stock0 = xls.parse("stock") if "stock" in xls.sheet_names else empty_stock()
    except Exception as e:
        st.error(f"Could not read the file. Check the sheet names. ({e})")
        st.stop()
    tag = file.name
else:
    prod0, mach0, allowed0, usage0, stock0 = example_data()
    tag = "example"

# ---------- EDITABLE TABLES ----------
st.subheader("1. Orders")
prod = st.data_editor(prod0, num_rows="dynamic", key=f"prod_{tag}", use_container_width=True)

col1, col2 = st.columns(2)
with col1:
    st.subheader("2. Machine minutes per day")
    mach = st.data_editor(mach0, num_rows="dynamic", key=f"mach_{tag}", use_container_width=True)
with col2:
    st.subheader("3. Which machine can make which product")
    allowed = st.data_editor(allowed0, num_rows="dynamic", key=f"allowed_{tag}", use_container_width=True)

col3, col4 = st.columns(2)
with col3:
    st.subheader("4. Material used per unit (whole kg)")
    usage_df = st.data_editor(usage0, num_rows="dynamic", key=f"usage_{tag}", use_container_width=True)
with col4:
    st.subheader("5. Material available per day (kg)")
    stock_df = st.data_editor(stock0, num_rows="dynamic", key=f"stock_{tag}", use_container_width=True)

plan_name = st.text_input("Plan name (optional, helps you find it later)", value="")

# ---------- RUN ----------
if st.button("Make plan", type="primary"):
    try:
        products, machines, days, capacity, usage, stock = build_inputs(
            prod, mach, allowed, usage_df, stock_df)
    except Exception as e:
        st.error(f"Check your tables for blank or wrong values. ({e})")
        st.stop()

    bad_days = [d for d in days if d < 1]
    if bad_days:
        st.warning(f"Table 2 has day values below 1: {bad_days}. Days should start at 1.")
        st.stop()

    huge = [k for k, kg in usage.items() if kg > 1000]
    if huge:
        st.warning(f"Very large material use per unit in table 4: {huge}. Check for typos.")

    st.info(f"Days in the plan: {days}. Machines: {machines}.")
    st.info("Parameters used: " + ", ".join(f"{k} = {v}" for k, v in settings.items()))

    missing = [p for p, info in products.items() if not info["machines"]]
    if missing:
        st.warning(f"No machine is allowed for: {', '.join(missing)}. Add a row in table 3.")

    if settings["use_materials"]:
        used_materials = set(mat for _, mat in usage)
        stocked_materials = set(mat for mat, _ in stock)
        unlimited = sorted(used_materials - stocked_materials)
        if unlimited:
            st.warning(f"No stock listed in table 5 for: {', '.join(unlimited)}. "
                       "These materials are treated as unlimited.")

    plan_rows, summary_rows, material_rows, overtime_rows = make_plan(
        products, machines, days, capacity, usage, stock, settings=settings)

    if plan_rows is None:
        st.error("No plan found.")
    elif not plan_rows:
        st.warning("Nothing can be made with this data.")
    else:
        plan = pd.DataFrame(plan_rows)
        summary = pd.DataFrame(summary_rows)

        late_total = int(summary["late"].sum())
        not_made_total = int((summary["ordered"] - summary["made"]).sum())
        total_overtime = sum(r["overtime_minutes"] for r in overtime_rows)

        run_id = save_run(plan_name.strip() or "Untitled",
                          settings["max_overtime"], settings["overtime_cost"],
                          late_total, not_made_total, total_overtime, plan)
        st.success(f"Plan saved as #{run_id}. Review it in the Saved plans section at the bottom of the page.")

        st.subheader("Result per product")
        st.dataframe(summary, hide_index=True)

        c1, c2, c3 = st.columns(3)
        c1.metric("Late units", late_total)
        c2.metric("Units not made", not_made_total)
        c3.metric("Overtime minutes", total_overtime)

        st.subheader("Plan")
        st.dataframe(plan, hide_index=True)

        if overtime_rows:
            st.subheader("Overtime used")
            st.dataframe(pd.DataFrame(overtime_rows), hide_index=True)

        if material_rows:
            st.subheader("Material used vs available")
            st.dataframe(pd.DataFrame(material_rows), hide_index=True)

        st.subheader("Units per day by machine")
        st.bar_chart(plan.pivot_table(index="day", columns="machine",
                                      values="qty", aggfunc="sum").fillna(0))

        buffer = io.BytesIO()
        plan.to_excel(buffer, index=False)
        st.download_button("Download plan as Excel", buffer.getvalue(), file_name="plan.xlsx")

# ---------- SAVED PLANS ----------
st.divider()
st.header("Saved plans")

if "flash" in st.session_state:
    st.success(st.session_state.pop("flash"))

runs = list_runs()

if runs.empty:
    st.write("No saved plans yet. Click **Make plan** to save the first one.")
else:
    st.dataframe(runs, hide_index=True)

    labels = {int(r.id): f"#{r.id} {r.name} ({r.created})" for r in runs.itertuples()}
    ids = list(labels.keys())  # newest first

    # ----- planner feedback -----
    st.subheader("Planner feedback")
    st.write("Did the planner use this plan? If they changed it, write what they changed and why.")

    fb_id = st.selectbox("Plan to review", ids, index=0, format_func=lambda i: labels[i])
    decision = st.radio("Decision", ["Accepted as is", "Changed by planner", "Rejected"],
                        horizontal=True)
    reason = st.text_area("Reason (what was wrong, or what was changed)")

    if st.button("Save feedback"):
        if decision != "Accepted as is" and not reason.strip():
            st.warning("Please write a short reason. It is what tells you which rule to add next.")
        else:
            save_feedback(fb_id, decision, reason.strip())
            st.session_state["flash"] = f"Feedback saved for plan #{fb_id}."
            st.rerun()

    feedback_log = list_feedback()
    if not feedback_log.empty:
        st.write("**Feedback log**")
        st.dataframe(feedback_log, hide_index=True)

    # ----- compare two plans -----
    if len(runs) >= 2:
        st.subheader("Compare two plans")

        pick1, pick2 = st.columns(2)
        old_id = pick1.selectbox("Older plan", ids, index=1, format_func=lambda i: labels[i])
        new_id = pick2.selectbox("Newer plan", ids, index=0, format_func=lambda i: labels[i])

        by_id = runs.set_index("id")
        old, new = by_id.loc[old_id], by_id.loc[new_id]

        m1, m2, m3 = st.columns(3)
        m1.metric("Late units", int(new["late_units"]),
                  delta=int(new["late_units"] - old["late_units"]), delta_color="inverse")
        m2.metric("Units not made", int(new["units_not_made"]),
                  delta=int(new["units_not_made"] - old["units_not_made"]), delta_color="inverse")
        m3.metric("Overtime minutes", int(new["overtime_minutes"]),
                  delta=int(new["overtime_minutes"] - old["overtime_minutes"]), delta_color="inverse")

        a = load_lines(old_id).rename(columns={"qty": "qty_older"})
        b = load_lines(new_id).rename(columns={"qty": "qty_newer"})
        diff = a.merge(b, on=["day", "product", "machine"], how="outer").fillna(0)
        diff["change"] = diff["qty_newer"] - diff["qty_older"]
        diff = diff[diff["change"] != 0].sort_values(["day", "product", "machine"])

        st.write("**What changed in the schedule** (only rows that differ):")
        if diff.empty:
            st.write("The two plans are identical.")
        else:
            st.dataframe(diff.astype({"qty_older": int, "qty_newer": int, "change": int}),
                         hide_index=True)