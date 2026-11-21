import io
from datetime import date

import altair as alt
import pandas as pd
import streamlit as st

import importer
import scheduler
import store
import tables

st.set_page_config(page_title="Production Planner", layout="wide")

HELP_SETTINGS = {
    "horizon_days": "How many calendar days ahead the plan looks. Orders that do not fit are shown as Not scheduled.",
    "late_cost": "Points lost for every day an order finishes after its due date. Higher = the planner fights harder to be on time.",
    "earliness_weight": "Small push to finish things sooner. Keep it low (0 to 5) so it never beats on-time delivery.",
    "use_priority": "If on, a priority 3 order counts three times as much as priority 1 when deciding who is late.",
    "use_parts": "If on, a step cannot start before its parts arrive (Parts arrival tab).",
    "gap_days": "Days between the end of one step and the start of the next (curing, transport, inspection). 0 = next day can follow immediately on the same day.",
    "max_overtime": "Extra hours each crew may work on a working day beyond normal capacity. 0 = no overtime.",
    "overtime_cost": "Points lost for each overtime hour. Higher = overtime only when really needed.",
    "time_limit_seconds": "How long the solver may think. More time can give a better plan on big data.",
}


# ---------------- database + login ----------------
@st.cache_resource
def engine():
    url = None
    try:
        url = st.secrets.get("DATABASE_URL")
    except Exception:
        url = None
    return store.get_engine(url)


def invite_code():
    try:
        return st.secrets.get("INVITE_CODE")
    except Exception:
        return None


def login_screen():
    st.title("Production Planner")
    st.caption("Sign in to plan your factory. Your data is private to your account.")
    eng = engine()
    if not store.is_hosted(eng):
        st.warning("No hosted database is connected. Accounts and saved data are kept in a local file "
                   "and will be lost when this app restarts on Streamlit Cloud. See the setup notes to add DATABASE_URL.")
    tab_in, tab_up = st.tabs(["Sign in", "Create account"])
    with tab_in:
        with st.form("login"):
            u = st.text_input("Username")
            p = st.text_input("Password", type="password")
            if st.form_submit_button("Sign in", type="primary"):
                uid, err = store.check_login(u, p, eng)
                if err:
                    st.error(err)
                else:
                    st.session_state["uid"] = uid
                    st.session_state["username"] = u.strip().lower()
                    st.rerun()
    with tab_up:
        code_needed = store.is_hosted(eng) and invite_code()
        if store.is_hosted(eng) and not invite_code():
            st.info("Sign-up is closed: the owner has not set an invite code.")
        else:
            with st.form("signup"):
                u = st.text_input("Choose a username")
                p = st.text_input("Choose a password (8+ characters)", type="password")
                code = st.text_input("Invite code", type="password") if code_needed else ""
                if st.form_submit_button("Create account"):
                    if code_needed and code != str(invite_code()):
                        st.error("Wrong invite code.")
                    else:
                        uid, err = store.create_user(u, p, eng)
                        if err:
                            st.error(err)
                        else:
                            st.session_state["uid"] = uid
                            st.session_state["username"] = u.strip().lower()
                            st.rerun()


if "uid" not in st.session_state:
    login_screen()
    st.stop()

uid = st.session_state["uid"]
eng = engine()

# ---------------- session data ----------------
if "base" not in st.session_state:
    st.session_state["base"] = tables.example()
    st.session_state["ver"] = 0
    st.session_state["edited"] = {}


def set_tables(new, keep_others=False):
    cur = dict(st.session_state["base"]) if keep_others else tables.blank()
    cur.update(tables.normalize(new) if not keep_others else {k: v for k, v in tables.normalize(new).items()})
    st.session_state["base"] = tables.normalize(cur)
    st.session_state["ver"] += 1
    st.session_state["edited"] = {}


def current_tables():
    out = dict(st.session_state["base"])
    out.update(st.session_state["edited"])
    return tables.normalize(out)


# ---------------- sidebar ----------------
with st.sidebar:
    st.write(f"Signed in as **{st.session_state['username']}**")
    if st.button("Sign out"):
        for k in list(st.session_state.keys()):
            del st.session_state[k]
        st.rerun()
    with st.expander("Change password"):
        with st.form("pw"):
            old = st.text_input("Current password", type="password")
            new = st.text_input("New password", type="password")
            if st.form_submit_button("Change"):
                err = store.change_password(uid, old, new, eng)
                st.error(err) if err else st.success("Password changed.")
    st.divider()
    st.header("Planning settings")
    plan_start = st.date_input("Plan start date", value=date.today(), help="First day of the plan.")
    S = {}
    S["horizon_days"] = st.number_input("Days to plan", 14, 365, 90, help=HELP_SETTINGS["horizon_days"])
    S["late_cost"] = st.number_input("Cost per late day", 0, 10000, 100, help=HELP_SETTINGS["late_cost"])
    S["earliness_weight"] = st.number_input("Finish-early push", 0, 100, 1, help=HELP_SETTINGS["earliness_weight"])
    S["use_priority"] = st.checkbox("Use priority", True, help=HELP_SETTINGS["use_priority"])
    S["use_parts"] = st.checkbox("Wait for parts", True, help=HELP_SETTINGS["use_parts"])
    S["gap_days"] = st.number_input("Days between steps", 0, 30, 1, help=HELP_SETTINGS["gap_days"])
    S["max_overtime"] = st.number_input("Max overtime hours per day", 0, 12, 0, help=HELP_SETTINGS["max_overtime"])
    S["overtime_cost"] = st.number_input("Cost per overtime hour", 0, 1000, 5, help=HELP_SETTINGS["overtime_cost"])
    S["time_limit_seconds"] = st.number_input("Solver time (seconds)", 5, 300, 20, help=HELP_SETTINGS["time_limit_seconds"])

st.title("Production Planner")
tab_data, tab_plan, tab_saved = st.tabs(["1. Data", "2. Plan", "3. Saved and feedback"])

# ---------------- data tab ----------------
CFG = {
    "orders": {
        "order_id": st.column_config.TextColumn("Order / PO", help="Your PO or project number. Must be unique.", required=True),
        "product_type": st.column_config.TextColumn("Product type", help="Must match a product type in Routing."),
        "quantity": st.column_config.NumberColumn("Units", help="Units built as one project (for example 3 escalators).", min_value=0),
        "due_date": st.column_config.DateColumn("Due / site date", help="Date the order must be finished."),
        "priority": st.column_config.NumberColumn("Priority", help="1 to 10. Higher = more important.", min_value=1, max_value=10),
        "start_not_before": st.column_config.DateColumn("Earliest start", help="Do not start before this date. Blank = plan start."),
        "status": st.column_config.TextColumn("Status", help="For example Confirmed or Tentative. Use the filter in the Plan tab."),
    },
    "routing": {
        "product_type": st.column_config.TextColumn("Product type", help="Name used in Orders."),
        "step_no": st.column_config.NumberColumn("Step no.", help="Order of the step: 1, 2, 3 ...", min_value=1),
        "step_name": st.column_config.TextColumn("Step", help="Frame, Welding, Paint, Assembly, Test ..."),
        "fixed_hours": st.column_config.NumberColumn("Fixed hours", help="Hours needed regardless of quantity (setup).", min_value=0),
        "hours_per_unit": st.column_config.NumberColumn("Hours per unit", help="Extra hours for each unit.", min_value=0),
        "resources": st.column_config.TextColumn("Allowed crews / machines", help="Names from the Crews tab, separated by commas. The planner picks one."),
    },
    "resources": {
        "resource": st.column_config.TextColumn("Crew / machine", help="Name used in Routing."),
        "crew_size": st.column_config.NumberColumn("Crew size", help="People working together (1 for a machine).", min_value=0),
        "hours_per_shift": st.column_config.NumberColumn("Hours per shift", help="Hours each person works per day.", min_value=0),
        "efficiency_pct": st.column_config.NumberColumn("Efficiency %", help="100 = normal. 85 = slower crew.", min_value=0),
        "work_days": st.column_config.TextColumn("Work days", help="For example Mon,Tue,Wed,Thu,Fri or Mon-Sat."),
    },
    "exceptions": {
        "resource": st.column_config.TextColumn("Crew / machine", help="A name from Crews, or ALL for everyone (public holiday)."),
        "from_date": st.column_config.DateColumn("From", help="First day."),
        "to_date": st.column_config.DateColumn("To", help="Last day (blank = same as From)."),
        "crew_available": st.column_config.NumberColumn("People available", help="0 = closed. 2 = two people present.", min_value=0),
        "note": st.column_config.TextColumn("Note", help="Holiday, vacation, maintenance ..."),
    },
    "parts": {
        "order_id": st.column_config.TextColumn("Order / PO", help="Order the part belongs to."),
        "part": st.column_config.TextColumn("Part", help="Description."),
        "needed_for_step": st.column_config.NumberColumn("Needed for step no.", help="The step cannot start before the arrival date.", min_value=1),
        "arrival_date": st.column_config.DateColumn("Arrival date", help="Date the part arrives."),
    },
}
NUM = {"quantity", "priority", "step_no", "fixed_hours", "hours_per_unit", "crew_size",
       "hours_per_shift", "efficiency_pct", "crew_available", "needed_for_step"}
DATE = {"due_date", "start_not_before", "from_date", "to_date", "arrival_date"}


def for_editor(df):
    df = df.copy()
    for c in df.columns:
        if c in DATE:
            df[c] = pd.to_datetime(df[c], errors="coerce")
        elif c in NUM:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        else:
            df[c] = df[c].astype("object").where(df[c].notna(), None)
    return df


with tab_data:
    c1, c2, c3 = st.columns(3)
    if c1.button("Load escalator example"):
        set_tables(tables.example())
        st.rerun()
    if c2.button("Start blank"):
        set_tables(tables.blank())
        st.rerun()
    up = c3.file_uploader("Load a saved workbook", type=["xlsx"], key=f"wb_{st.session_state['ver']}",
                          label_visibility="collapsed")
    if up is not None:
        try:
            set_tables(tables.from_workbook(up))
            st.rerun()
        except Exception as e:
            st.error(f"Could not read the workbook: {e}")

    with st.expander("Import orders from your own schedule file"):
        def done(orders):
            set_tables({"orders": orders}, keep_others=True)
            st.session_state["import_note"] = f"{len(orders)} orders imported."
        importer.render_import(done)
    if st.session_state.get("import_note"):
        st.success(st.session_state.pop("import_note") + " Check the Routing tab has a route for each product type.")
        orders_now = st.session_state["base"]["orders"]
        known = set(st.session_state["base"]["routing"]["product_type"].astype(str))
        missing = sorted(set(orders_now["product_type"].astype(str)) - known - {""})
        if missing:
            st.warning("No routing yet for: " + ", ".join(missing))

    sub = st.tabs([tables.TITLES[k] for k in tables.COLUMNS])
    for tab, name in zip(sub, tables.COLUMNS):
        with tab:
            st.session_state["edited"][name] = st.data_editor(
                for_editor(st.session_state["base"][name]), num_rows="dynamic", width="stretch",
                column_config=CFG[name], key=f"ed_{name}_{st.session_state['ver']}", hide_index=True)

    st.download_button("Download data as Excel", tables.to_workbook(current_tables()), file_name="planner_data.xlsx")
    with st.form("save_ds", clear_on_submit=True):
        nm = st.text_input("Save this data set in my account as")
        if st.form_submit_button("Save data set") and nm.strip():
            store.save_item("datasets", uid, nm.strip(), tables.to_json(current_tables()), eng)
            st.success("Saved.")

# ---------------- plan tab ----------------
def result_to_json(res, settings, status_note):
    def conv(rows):
        return [{k: (v.isoformat() if isinstance(v, date) else v) for k, v in r.items()} for r in rows]
    return {"orders": conv(res["orders"]), "steps": conv(res["steps"]), "load": conv(res["load"]),
            "settings": {k: (v if not isinstance(v, date) else v.isoformat()) for k, v in settings.items()},
            "status": status_note}


def plan_excel(res):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        pd.DataFrame(res["orders"]).to_excel(xw, sheet_name="orders", index=False)
        pd.DataFrame(res["steps"]).to_excel(xw, sheet_name="steps", index=False)
        pd.DataFrame(res["daily"]).to_excel(xw, sheet_name="daily_hours", index=False)
        pd.DataFrame(res["load"]).to_excel(xw, sheet_name="resource_load", index=False)
    return buf.getvalue()


with tab_plan:
    data = current_tables()
    statuses = sorted({str(s).strip() for s in data["orders"]["status"].dropna() if str(s).strip()})
    chosen = statuses
    if statuses:
        chosen = st.multiselect("Plan orders with these statuses", statuses, default=statuses,
                                help="Untick a status (for example Tentative) to leave those orders out.")
    if st.button("Make plan", type="primary"):
        d = dict(data)
        o = d["orders"]
        if statuses:
            blank_ok = o["status"].isna() | (o["status"].astype(str).str.strip() == "")
            d["orders"] = o[blank_ok | o["status"].astype(str).str.strip().isin(chosen)]
        jobs, cap, days, problems, skipped = tables.prepare(d, plan_start, S)
        for p in problems:
            st.warning(p)
        if skipped:
            st.warning("Left out of the plan:")
            st.dataframe(pd.DataFrame(skipped), hide_index=True)
        if not jobs:
            st.error("There are no valid orders to plan.")
        else:
            with st.spinner("Planning ..."):
                res = scheduler.solve(jobs, cap, days, S)
            if res["status"] == "none":
                st.error("No plan found. Try more solver time or more days.")
            else:
                st.session_state["result"] = res
                st.session_state["result_meta"] = {"start": plan_start, "S": dict(S), "n": len(jobs)}

    res = st.session_state.get("result")
    if res:
        meta = st.session_state["result_meta"]
        orders = pd.DataFrame(res["orders"])
        load = pd.DataFrame(res["load"])
        late = int((orders["status"] == "Late").sum())
        notp = int((orders["status"] == "Not scheduled").sum())
        ot_hours = int(load["overtime"].sum()) if len(load) else 0
        delivered = int(orders["status"].isin(["On time", "Late"]).sum())
        if "focus" not in st.session_state:
            st.session_state["focus"] = "All orders"
        cards = [("Orders delivered", delivered, "Delivered", "On time + late"),
                 ("Orders on time", int((orders["status"] == "On time").sum()), "On time", None),
                 ("Late orders", late, "Late", None),
                 ("Not scheduled", notp, "Not scheduled", None),
                 ("Overtime hours", ot_hours, "Overtime", None)]
        for col, (label, value, key, hint) in zip(st.columns(5), cards):
            col.metric(label, value, help=hint)
            if col.button("Show list", key=f"focus_{key}", type="primary" if st.session_state["focus"] == key else "secondary"):
                st.session_state["focus"] = key
                st.rerun()
        if res["status"] == "feasible":
            st.caption("Good plan found within the time limit. More solver time may improve it.")
        if notp:
            st.warning("Some orders do not fit in the days planned. Add capacity, add days, or move due dates.")

        due = data["orders"].assign(order_id=data["orders"]["order_id"].astype(str).str.strip())[
            ["order_id", "product_type", "quantity", "due_date", "priority"]]
        due["due_date"] = pd.to_datetime(due["due_date"], errors="coerce").dt.date
        show = orders.merge(due, on="order_id", how="left")
        show = show[["order_id", "product_type", "quantity", "status", "finish", "due_date", "days_late", "priority"]]
        focus = st.session_state["focus"]
        head, allbtn = st.columns([5, 1])
        titles = {"All orders": "Result per order", "Delivered": "Orders that will be delivered",
                  "On time": "Orders on time", "Late": "Late orders",
                  "Not scheduled": "Orders not scheduled", "Overtime": "Overtime by crew and day"}
        head.subheader(titles[focus])
        if focus != "All orders" and allbtn.button("Show all orders"):
            st.session_state["focus"] = "All orders"
            st.rerun()
        if focus == "Overtime":
            ot_rows = load[load["overtime"] > 0] if len(load) else load
            if len(ot_rows):
                st.dataframe(ot_rows[["resource", "date", "capacity", "planned", "overtime"]],
                             hide_index=True, width="stretch")
            else:
                st.info("No overtime in this plan. Raise 'Max overtime hours per day' in the sidebar to allow it.")
        else:
            if focus == "Delivered":
                view_df = show[show["status"].isin(["On time", "Late"])]
            elif focus in ("On time", "Late", "Not scheduled"):
                view_df = show[show["status"] == focus]
            else:
                view_df = show
            if len(view_df):
                st.dataframe(view_df, hide_index=True, width="stretch")
            else:
                st.info("No orders in this group.")

        steps = pd.DataFrame(res["steps"])
        if len(steps):
            st.subheader("Schedule")
            view = st.radio("Show by", ["Order", "Crew / machine"], horizontal=True)
            g = steps.copy()
            g["start"] = pd.to_datetime(g["start"])
            g["end"] = pd.to_datetime(g["end"]) + pd.Timedelta(days=1)
            if view == "Order":
                ch = alt.Chart(g).mark_bar().encode(
                    x=alt.X("start:T", title=None), x2="end:T", y=alt.Y("order_id:N", title=None),
                    color=alt.Color("step_name:N", title="Step"),
                    tooltip=["order_id", "step_name", "resource", "start", "end", "hours"])
            else:
                ch = alt.Chart(g).mark_bar().encode(
                    x=alt.X("start:T", title=None), x2="end:T", y=alt.Y("resource:N", title=None),
                    color=alt.Color("order_id:N", title="Order"),
                    tooltip=["order_id", "step_name", "resource", "start", "end", "hours"])
            st.altair_chart(ch.properties(height=max(200, 34 * g[("order_id" if view == "Order" else "resource")].nunique())),
                            width="stretch")
            st.dataframe(steps, hide_index=True, width="stretch")

        if len(load):
            st.subheader("Crew and machine load")
            l2 = load[load["capacity"] > 0].copy()
            l2["date"] = pd.to_datetime(l2["date"])
            l2["used_pct"] = (100 * l2["planned"] / l2["capacity"]).round()
            heat = alt.Chart(l2).mark_rect().encode(
                x=alt.X("date:T", title=None), y=alt.Y("resource:N", title=None),
                color=alt.Color("used_pct:Q", title="% used", scale=alt.Scale(domain=[0, 100], scheme="blues")),
                tooltip=["resource", "date", "planned", "capacity", "overtime"])
            st.altair_chart(heat.properties(height=40 * l2["resource"].nunique() + 40), width="stretch")

        st.download_button("Download plan as Excel", plan_excel(res), file_name="plan.xlsx")
        with st.form("save_plan", clear_on_submit=True):
            nm = st.text_input("Save this plan in my account as")
            if st.form_submit_button("Save plan") and nm.strip():
                store.save_item("plans", uid, nm.strip(), result_to_json(res, {**meta["S"], "start": meta["start"]}, res["status"]), eng)
                st.success("Saved.")

# ---------------- saved tab ----------------
with tab_saved:
    st.subheader("My data sets")
    for it in store.list_items("datasets", uid, eng):
        a, b, c = st.columns([4, 1, 1])
        a.write(f"{it['name']}  \n:gray[{it['created']}]")
        if b.button("Load", key=f"ld{it['id']}"):
            set_tables(tables.from_json(store.load_item("datasets", uid, it["id"], eng)))
            st.rerun()
        if c.button("Delete", key=f"dd{it['id']}"):
            store.delete_item("datasets", uid, it["id"], eng)
            st.rerun()

    st.subheader("My saved plans")
    plans = store.list_items("plans", uid, eng)
    rows = []
    for it in plans:
        body = store.load_item("plans", uid, it["id"], eng) or {}
        o = pd.DataFrame(body.get("orders", []))
        ld = pd.DataFrame(body.get("load", []))
        rows.append({"id": it["id"], "name": it["name"], "saved": it["created"],
                     "on time": int((o["status"] == "On time").sum()) if len(o) else 0,
                     "late": int((o["status"] == "Late").sum()) if len(o) else 0,
                     "not scheduled": int((o["status"] == "Not scheduled").sum()) if len(o) else 0,
                     "late days": int(o["days_late"].fillna(0).sum()) if len(o) else 0,
                     "overtime h": int(ld["overtime"].sum()) if len(ld) else 0})
    if rows:
        st.caption("Compare saved plans side by side:")
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        pick = st.selectbox("Open or delete a plan", [r["id"] for r in rows],
                            format_func=lambda i: next(r["name"] for r in rows if r["id"] == i))
        body = store.load_item("plans", uid, pick, eng)
        if body:
            st.dataframe(pd.DataFrame(body["orders"]), hide_index=True, width="stretch")
        if st.button("Delete this plan"):
            store.delete_item("plans", uid, pick, eng)
            st.rerun()
    else:
        st.write("No saved plans yet.")

    st.subheader("Feedback")
    with st.form("fb", clear_on_submit=True):
        txt = st.text_area("What worked, what is missing, what was wrong?")
        if st.form_submit_button("Send feedback") and txt.strip():
            store.save_item("feedback", uid, txt.strip()[:60], {"text": txt.strip()}, eng)
            st.success("Thank you.")
    for it in store.list_items("feedback", uid, eng)[:20]:
        body = store.load_item("feedback", uid, it["id"], eng) or {}
        st.write(f":gray[{it['created']}] {body.get('text', '')}")
