import io
from datetime import date, datetime, timezone

import altair as alt
import pandas as pd
import streamlit as st

import explain
import importer
import insights
import mailer

try:
    import pdf
except ImportError:      # PDF library not installed: the app still works, without PDF files
    pdf = None
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


def start_session(login_id, action):
    user = store.get_user(login_id, engine())
    st.session_state["login_id"] = user["id"]
    st.session_state["uid"] = user["owner_id"]       # whose data this login works on
    st.session_state["username"] = user["username"]
    st.session_state["role"] = user["role"]
    store.log(user["owner_id"], user["username"], action, "", engine())


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
                    start_session(uid, "Signed in")
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
                            start_session(uid, "Account created")
                            st.rerun()


def make_pdf(name, body, author=""):
    """PDF bytes, or None when the PDF library is missing."""
    if pdf is None:
        return None
    try:
        return pdf.build_pdf(name, body, author)
    except Exception:
        return None


def pdf_button(container, name, body, author="", key=None):
    data = make_pdf(name, body, author)
    if data is None:
        container.caption("PDF summary needs the reportlab library: run  py -3.12 -m pip install reportlab")
    else:
        container.download_button("Download one-page summary (PDF)", data, file_name="plan_summary.pdf",
                                  mime="application/pdf", key=key)


def secret(name, default=None):
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default


def approval_page(token):
    st.title("Production plan: management decision")
    eng = engine()
    ap = store.get_approval(token, eng)
    if not ap:
        st.error("This link is not valid.")
        return
    if ap["error"]:
        st.warning(ap["error"])
        return
    body = store.load_item("plans", ap["uid"], ap["plan_id"], eng)
    if not body:
        st.error("This plan no longer exists.")
        return
    items = {i["id"]: i["name"] for i in store.list_items("plans", ap["uid"], eng)}
    st.subheader(items.get(ap["plan_id"], "Plan"))
    sm = mailer.summary(body)
    a, b, c, d = st.columns(4)
    a.metric("Delivered", sm["on_time"] + sm["late"], help=f"of {sm['n']} orders")
    b.metric("Late", sm["late"], help=f"{sm['late_days']} late days in total")
    c.metric("Not scheduled", sm["not_scheduled"])
    d.metric("Overtime hours", sm["overtime"])
    if insights.has_money(body.get("money"), body.get("orders", [])):
        mon = insights.money(body.get("orders", []), body.get("load", []), body.get("money"))
        st.info(f"Money (your currency): overtime cost {insights.fmt(mon['overtime_cost'])}, late penalties "
                f"{insights.fmt(mon['late_penalty'])}, revenue at risk {insights.fmt(mon['value_at_risk'])} "
                f"of {insights.fmt(mon['value_all'])} total order value.")
    if sm["risk"]:
        st.markdown("**Orders needing attention**")
        st.dataframe(pd.DataFrame(sm["risk"]).reindex(columns=["order_id", "status", "finish", "days_late", "why"]),
                     hide_index=True, width="stretch")
    else:
        st.success("Every order is delivered on time.")
    if body.get("options"):
        st.markdown("**Options compared**")
        st.dataframe(pd.DataFrame(body["options"]), hide_index=True, width="stretch")
    with st.expander("All orders"):
        st.dataframe(pd.DataFrame(body["orders"]), hide_index=True, width="stretch")
    pdf_button(st, items.get(ap["plan_id"], "Plan"), body, key="pdf_mgr")
    st.write(f"Decision requested from: **{ap['email']}**")
    with st.form("email_decision"):
        comment = st.text_input("Comment (optional)")
        c1, c2 = st.columns(2)
        ok = c1.form_submit_button("Approve plan", type="primary")
        no = c2.form_submit_button("Reject plan")
        if ok or no:
            old = body.get("decision") or {}
            body["decision"] = {"status": "Approved" if ok else "Rejected", "comment": comment.strip(),
                                "by": ap["email"], "via": "email link",
                                "at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                                "submitted_by": old.get("submitted_by")}
            store.update_item("plans", ap["uid"], ap["plan_id"], body, eng)
            store.mark_approval_used(token, eng)
            store.log(ap["uid"], ap["email"], "Plan " + body["decision"]["status"].lower() + " (email link)",
                      f"{items.get(ap['plan_id'], 'Plan')}. {comment.strip()}", eng)
            st.success("Thank you. Your decision was recorded: " + body["decision"]["status"] + ".")
            st.session_state["decided"] = True


if "approve" in st.query_params:
    approval_page(str(st.query_params["approve"]))
    st.stop()

if "uid" not in st.session_state:
    login_screen()
    st.stop()

if "login_id" not in st.session_state:      # session from before roles existed
    for k in list(st.session_state.keys()):
        del st.session_state[k]
    st.rerun()
uid = st.session_state["uid"]
login_id = st.session_state["login_id"]
role = st.session_state["role"]
me = st.session_state["username"]
eng = engine()
CAN_EDIT = store.can(role, "edit")
CAN_DECIDE = store.can(role, "decide")
CAN_TEAM = store.can(role, "team")
CAN_AUDIT = store.can(role, "audit")


def audit(action, detail=""):
    store.log(uid, me, action, detail, eng)


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
    st.write(f"Signed in as **{me}** ({role})")
    if st.button("Sign out"):
        for k in list(st.session_state.keys()):
            del st.session_state[k]
        st.rerun()
    with st.expander("Change password"):
        with st.form("pw"):
            old = st.text_input("Current password", type="password")
            new = st.text_input("New password", type="password")
            if st.form_submit_button("Change"):
                err = store.change_password(login_id, old, new, eng)
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
    st.divider()
    st.header("Money (optional)")
    st.caption("Used only for the cost view. Enter amounts in your own currency.")
    M = {"ot_rate": st.number_input("Cost per overtime hour", 0.0, 100000.0, 0.0, step=10.0,
                                    help="Real money (not planning points) paid for one overtime hour."),
         "penalty_pct": st.number_input("Late penalty, % of order value per day", 0.0, 100.0, 0.0, step=0.05,
                                        help="Contract penalty for each day an order is late, as a percent of its value. Needs Order value in Orders."),
         "penalty_flat": st.number_input("Late penalty, fixed amount per day", 0.0, 10000000.0, 0.0, step=100.0,
                                         help="Extra fixed amount for each late day, per order.")}

st.title("Production Planner")
_tabs = st.tabs(["1. Data", "2. Plan", "3. Saved and feedback"] + (["4. Team and audit"] if (CAN_TEAM or CAN_AUDIT) else []))
tab_data, tab_plan, tab_saved = _tabs[:3]
tab_team = _tabs[3] if len(_tabs) > 3 else None

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
        "order_value": st.column_config.NumberColumn("Order value", help="Contract value in your currency. Used for the money view (revenue at risk, late penalties). Optional.", min_value=0),
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
       "hours_per_shift", "efficiency_pct", "crew_available", "needed_for_step", "order_value"}
DATE = {"due_date", "start_not_before", "from_date", "to_date", "arrival_date"}


# ---------------- plain-language guides ----------------
HOW_TO = """
**What this tool does:** it builds a production schedule. For every order it decides which crew or machine works on
each step, and on which days, so orders finish on time while respecting crew capacity, holidays and parts arrival.

**How to use it**
1. **Data tab:** fill the five tables below (or load the example, upload your own file, or import your schedule).
2. **Plan tab:** choose which statuses to plan (for example leave out Tentative), then click **Make plan**.
3. Read the result: click the boxes (on time, late, not scheduled, overtime) to see which orders are in each group.
4. Not happy? Change something (add overtime, more crew, a later due date, an earlier parts arrival) and plan again.
5. Under the results: **Compare options** (overtime, one more person), the **capacity outlook**, and the **delivery promise checker** to quote a new order.
6. **Save and submit** the plan. A manager approves or rejects it in **Saved and feedback**, or from an email link. Download the one-page PDF to print or attach.
7. Later, use **Check against my current data** to see whether an approved plan is still true.
8. Admins add team members (planner, manager, viewer) in **Team and audit**, where the audit trail shows who did what.

**Fill the tables in this order:** Crews and machines, then Routing, then Orders, then Calendar exceptions and Parts arrival (these two are optional).
"""

TABLE_INFO = {
    "orders": ("What it is: one row per customer order or project (PO). Each row is built as a whole.",
               [("Order / PO", "Your PO or project number. Must be unique.", "PO-1001", "Yes"),
                ("Product type", "What is built. Must match a Product type in Routing.", "Escalator 30deg", "Yes"),
                ("Units", "How many units in this order, built as one project.", "3", "Yes"),
                ("Due / site date", "Date the order must be finished and ready.", "2027-01-15", "Yes"),
                ("Priority", "1 to 10. Higher = more important when orders compete for the same crew.", "3", "No (default 1)"),
                ("Earliest start", "Do not start before this date (design approval, contract signed). Blank = plan start date.", "2026-12-01", "No"),
                ("Status", "For example Confirmed or Tentative. On the Plan tab you choose which statuses to include.", "Tentative", "No"),
                ("Order value", "Contract value in your currency. Used for revenue at risk and late penalties.", "850000", "No")]),
    "routing": ("What it is: the recipe for each product type. A list of steps in order, with the hours each step takes.",
                [("Product type", "Name of the product. Same spelling as in Orders.", "Escalator 30deg", "Yes"),
                 ("Step no.", "Order of the step: 1 first, then 2, 3 ...", "1", "Yes"),
                 ("Step", "Name of the step.", "Welding", "Yes"),
                 ("Fixed hours", "Hours needed no matter how many units (setup, drawings, preparation).", "8", "Yes"),
                 ("Hours per unit", "Extra hours for each unit. Total hours = fixed + per unit x units.", "16", "Yes"),
                 ("Allowed crews / machines", "Who can do this step. Names from the Crews table, separated by commas. The planner picks one.", "Weld crew A, Weld crew B", "Yes")]),
    "resources": ("What it is: your crews and machines and how many hours of work they can do each day.",
                  [("Crew / machine", "Name. Used in Routing and Calendar exceptions.", "Frame crew", "Yes"),
                   ("Crew size", "People who work together. Use 1 for a single machine.", "3", "Yes"),
                   ("Hours per shift", "Hours each person works per day.", "8", "Yes"),
                   ("Efficiency %", "100 = normal speed. 85 = a slower or newer crew.", "90", "No (default 100)"),
                   ("Work days", "Days of the week it works. Mon,Tue,Wed or Mon-Fri.", "Mon-Fri", "No (default Mon-Fri)")]),
    "exceptions": ("What it is: days when capacity is lower than normal: public holidays, vacations, maintenance. Optional.",
                   [("Crew / machine", "A name from Crews, or ALL for everyone (public holiday).", "ALL", "Yes"),
                    ("From", "First day of the exception.", "2026-12-25", "Yes"),
                    ("To", "Last day. Blank = just one day.", "2026-12-26", "No"),
                    ("People available", "How many people are present. 0 = closed. 2 = two people present.", "0", "Yes"),
                    ("Note", "Reason, for your own reference.", "Christmas", "No")]),
    "parts": ("What it is: parts that must arrive before a step can start. Optional.",
              [("Order / PO", "The order the part belongs to. Same as in Orders.", "PO-1001", "Yes"),
               ("Part", "Description of the part.", "Drive motors", "No"),
               ("Needed for step no.", "Step number that cannot start before the part arrives.", "4", "Yes"),
               ("Arrival date", "Date the part arrives at the factory.", "2026-12-17", "Yes")]),
}


RESULT_INFO = """
**The five boxes**
- **Orders delivered:** orders the plan finishes inside the planned days (on time + late).
- **Orders on time:** finish on or before the due date.
- **Late orders:** finish after the due date. The table shows how many days late.
- **Not scheduled:** orders that cannot fit in the planned days at all (not enough capacity or too many units). Add crew, overtime or days.
- **Overtime hours:** extra hours the plan uses beyond normal capacity. Only appears when overtime is allowed in the sidebar.

**Result per order (the table)**
- **status:** On time, Late or Not scheduled. **finish:** the day the last step (Test) ends. **due_date:** the date you asked for.
- **days_late:** finish minus due date. **priority / quantity / product_type:** copied from your Orders table.

**Schedule chart:** one bar per step. In *Order* view each row is an order and colors are steps. In *Crew / machine* view each row is a crew and colors are orders, so you can see who is busy when. Hover for dates and hours.
The table under it lists every step with its crew, start day, end day and hours.

**Why late:** the last column explains each late or not scheduled order in plain words (work too long, parts date, or crew busy with other orders).

**Money:** set cost per overtime hour and late penalties in the sidebar, and add Order value in Orders. The plan then shows overtime cost, late penalties and revenue at risk.

**Crew and machine load:** a heat map of how full each crew is each day (dark blue = full). Hover to see planned hours, capacity and overtime.
"""

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
    with st.expander("How to use this planner (start here)", expanded=False):
        st.markdown(HOW_TO)
    if not CAN_EDIT:
        st.info(f"Your role ({role}) can view the data and plans but not change them.")
    else:
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
                audit("Data loaded from workbook", up.name)
                st.rerun()
            except Exception as e:
                st.error(f"Could not read the workbook: {e}")

        with st.expander("Import orders from your own schedule file"):
            def done(orders):
                set_tables({"orders": orders}, keep_others=True)
                st.session_state["import_note"] = f"{len(orders)} orders imported."
                audit("Orders imported", f"{len(orders)} orders")
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
            intro, rows = TABLE_INFO[name]
            st.caption(intro)
            with st.expander("What goes in each column"):
                st.dataframe(pd.DataFrame(rows, columns=["Column", "What it means", "Example", "Required"]),
                             hide_index=True, width="stretch")
            st.session_state["edited"][name] = st.data_editor(
                for_editor(st.session_state["base"][name]), num_rows="dynamic", width="stretch",
                column_config=CFG[name], key=f"ed_{name}_{st.session_state['ver']}", hide_index=True,
                disabled=not CAN_EDIT)

    st.download_button("Download data as Excel", tables.to_workbook(current_tables()), file_name="planner_data.xlsx")
    if CAN_EDIT:
        with st.form("save_ds", clear_on_submit=True):
            nm = st.text_input("Save this data set in my account as")
            if st.form_submit_button("Save data set") and nm.strip():
                store.save_item("datasets", uid, nm.strip(), tables.to_json(current_tables()), eng)
                audit("Data set saved", nm.strip())
                st.success("Saved.")

# ---------------- plan tab ----------------
def result_to_json(res, settings, status_note):
    def conv(rows):
        return [{k: (v.isoformat() if isinstance(v, date) else v) for k, v in r.items()} for r in rows]
    return {"orders": conv(res["orders"]), "steps": conv(res["steps"]), "load": conv(res["load"]),
            "settings": {k: (v if not isinstance(v, date) else v.isoformat()) for k, v in settings.items()},
            "status": status_note}


def full_body(res, meta):
    b = result_to_json(res, {**meta["S"], "start": meta["start"]}, res["status"])
    b["money"] = meta.get("M")
    b["options"] = st.session_state.get("options")
    return b


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
    with st.expander("What the results show"):
        st.markdown(RESULT_INFO)
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
                why = explain.explain(jobs, cap, days, res, S)
                values = {str(r_["order_id"]).strip(): r_.get("order_value") for _, r_ in d["orders"].iterrows()}
                for o_ in res["orders"]:
                    o_["why"] = why.get(o_["order_id"], "")
                    v_ = values.get(o_["order_id"])
                    o_["value"] = None if (v_ is None or pd.isna(v_)) else float(v_)
                st.session_state["result"] = res
                st.session_state["result_meta"] = {"start": plan_start, "S": dict(S), "n": len(jobs), "M": dict(M),
                                                   "statuses": list(chosen)}
                st.session_state["plan_inputs"] = {k: v.copy() for k, v in d.items()}
                st.session_state.pop("options", None)
                st.session_state.pop("promise", None)

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
            ["order_id", "product_type", "quantity", "due_date", "priority", "order_value"]]
        due["due_date"] = pd.to_datetime(due["due_date"], errors="coerce").dt.date
        show = orders.merge(due, on="order_id", how="left")
        show = show[["order_id", "product_type", "quantity", "status", "finish", "due_date", "days_late", "priority", "order_value", "why"]]
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

        dl1, dl2 = st.columns(2)
        dl1.download_button("Download plan as Excel", plan_excel(res), file_name="plan.xlsx")
        _body = full_body(res, meta)
        pdf_button(dl2, "Production plan", _body, me, key="pdf_plan")
        st.subheader("Decision summary for management")
        n_all = len(orders)
        late_days = int(orders["days_late"].fillna(0).sum())
        st.markdown(
            f"- **{delivered} of {n_all}** orders are delivered: **{int((orders['status'] == 'On time').sum())} on time**, "
            f"**{late} late** ({late_days} late days in total).\n"
            f"- **{notp}** orders cannot be scheduled in the {meta['S']['horizon_days']} days planned.\n"
            f"- Overtime used: **{ot_hours} hours** (limit {meta['S']['max_overtime']} h per crew per day).")
        rows_m = [{**r_} for r_ in res["orders"]]
        if insights.has_money(meta.get("M"), rows_m):
            mon = insights.money(rows_m, res["load"], meta.get("M"))
            c_a, c_b, c_c = st.columns(3)
            c_a.metric("Overtime cost", insights.fmt(mon["overtime_cost"]))
            c_b.metric("Late penalties", insights.fmt(mon["late_penalty"]))
            c_c.metric("Revenue at risk", insights.fmt(mon["value_at_risk"]),
                       help=f"Value of late and not scheduled orders, out of {insights.fmt(mon['value_all'])} in total.")
        risky = show[show["status"].isin(["Late", "Not scheduled"])]
        if len(risky):
            st.markdown("**Orders at risk (need a decision):**")
            st.dataframe(risky, hide_index=True, width="stretch")
        else:
            st.success("Every order is delivered on time.")
        st.markdown("**Compare options**")
        st.caption("Tries the same orders with extra overtime and one more person in the busiest crew, so management can "
                   "choose with the numbers in front of them. Can take up to a minute.")
        if st.button("Compare options"):
            with st.spinner("Trying the options ..."):
                st.session_state["options"] = explain.run_options(
                    st.session_state["plan_inputs"], meta["start"], meta["S"], res, M=meta.get("M"))
        if st.session_state.get("options"):
            st.dataframe(pd.DataFrame(st.session_state["options"]), hide_index=True, width="stretch")
        if CAN_EDIT:
            with st.form("save_plan", clear_on_submit=True):
                nm = st.text_input("Name this plan and send it for management decision")
                if st.form_submit_button("Save and submit for decision") and nm.strip():
                    body = full_body(res, meta)
                    body["statuses"] = meta.get("statuses")
                    body["decision"] = {"status": "Pending", "by": None, "at": None, "comment": "", "submitted_by": me}
                    store.save_item("plans", uid, nm.strip(), body, eng)
                    audit("Plan saved and submitted", nm.strip())
                    st.success("Saved as Pending. A manager can approve or reject it in the Saved tab.")
        else:
            st.info("Your role cannot save or submit plans.")

        # ----- capacity outlook -----
        st.subheader("Capacity outlook (weekly)")
        wk = insights.weekly_load(res["load"])
        if len(wk):
            st.caption("How full each crew is, week by week. Dark = nearly full. " + insights.bottleneck_text(res["load"]))
            chart = alt.Chart(wk).mark_rect().encode(
                x=alt.X("week:T", title="Week starting"), y=alt.Y("resource:N", title=None),
                color=alt.Color("used_pct:Q", title="% used", scale=alt.Scale(domain=[0, 100], scheme="orangered")),
                tooltip=["resource", "week", "planned", "capacity", "used_pct"])
            st.altair_chart(chart.properties(height=40 * wk["resource"].nunique() + 40), width="stretch")
            with st.expander("Weekly numbers"):
                st.dataframe(wk.pivot_table(index="week", columns="resource", values="used_pct"), width="stretch")

        # ----- delivery promise checker -----
        st.subheader("Delivery promise checker (for sales)")
        st.caption("Quote a new order: it finds the earliest finish date using only the capacity left over, "
                   "without delaying the orders already in this plan.")
        prods = sorted(set(data["routing"]["product_type"].astype(str).str.strip()) - {""})
        with st.form("promise_form"):
            pc1, pc2, pc3 = st.columns(3)
            p_type = pc1.selectbox("Product type", prods) if prods else pc1.text_input("Product type")
            p_qty = pc2.number_input("Units", 1, 1000, 1)
            p_start = pc3.date_input("Earliest start (optional)", value=None)
            if st.form_submit_button("Find earliest delivery date"):
                with st.spinner("Checking ..."):
                    st.session_state["promise"] = insights.promise(
                        st.session_state["plan_inputs"], meta["start"], meta["S"], res,
                        {"product_type": p_type, "quantity": p_qty, "start_not_before": p_start})
        pr = st.session_state.get("promise")
        if pr:
            if pr.get("error"):
                st.error(pr["error"])
            else:
                st.success(f"Earliest delivery: **{pr['finish'].strftime('%d %b %Y')}** ({pr['days']} days after the plan start).")
                if pr["hurt"]:
                    st.warning("This would delay: " + ", ".join(pr["hurt"]) + ". Consider overtime or more crew first.")
                else:
                    st.caption("The orders already in the plan are not made late by this order.")

# ---------------- saved tab ----------------
def drift_check(body):
    """Re-plan today's data with the saved plan's settings. Returns (changes, error)."""
    cfg = body.get("settings") or {}
    if not cfg.get("start"):
        return None, "This plan has no saved settings."
    start = date.fromisoformat(str(cfg["start"])[:10])
    S2 = {k: cfg[k] for k in scheduler.DEFAULT_SETTINGS if k in cfg}
    S2["time_limit_seconds"] = min(int(S2.get("time_limit_seconds", 20)), 20)
    d = current_tables()
    o = d["orders"]
    kept = body.get("statuses")
    if kept:
        blank_ok = o["status"].isna() | (o["status"].astype(str).str.strip() == "")
        d["orders"] = o[blank_ok | o["status"].astype(str).str.strip().isin(kept)]
    jobs, cap, days, problems, skipped = tables.prepare(d, start, S2)
    if not jobs:
        return None, "There are no valid orders in your current data."
    new = scheduler.solve(jobs, cap, days, S2)
    if new["status"] == "none":
        return None, "No plan could be built from the current data."
    return insights.drift(body.get("orders", []), new["orders"]), None


with tab_saved:
    st.subheader("My data sets")
    for it in store.list_items("datasets", uid, eng):
        a, b, c = st.columns([4, 1, 1])
        a.write(f"{it['name']}  \n:gray[{it['created']}]")
        if b.button("Load", key=f"ld{it['id']}"):
            set_tables(tables.from_json(store.load_item("datasets", uid, it["id"], eng)))
            audit("Data set loaded", it["name"])
            st.rerun()
        if CAN_EDIT and c.button("Delete", key=f"dd{it['id']}"):
            store.delete_item("datasets", uid, it["id"], eng)
            audit("Data set deleted", it["name"])
            st.rerun()

    st.subheader("Saved plans and management decisions")
    plans = store.list_items("plans", uid, eng)
    rows = []
    for it in plans:
        body = store.load_item("plans", uid, it["id"], eng) or {}
        o = pd.DataFrame(body.get("orders", []))
        ld = pd.DataFrame(body.get("load", []))
        dec = body.get("decision") or {"status": "Draft"}
        mon = insights.money(body.get("orders", []), body.get("load", []), body.get("money"))
        rows.append({"id": it["id"], "name": it["name"], "decision": dec.get("status", "Draft"), "saved": it["created"],
                     "on time": int((o["status"] == "On time").sum()) if len(o) else 0,
                     "late": int((o["status"] == "Late").sum()) if len(o) else 0,
                     "not scheduled": int((o["status"] == "Not scheduled").sum()) if len(o) else 0,
                     "late days": int(o["days_late"].fillna(0).sum()) if len(o) else 0,
                     "overtime h": int(ld["overtime"].sum()) if len(ld) else 0,
                     "extra cost": mon["extra_cost"], "revenue at risk": mon["value_at_risk"]})
    if rows:
        st.caption("Compare saved plans side by side:")
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        pick = st.selectbox("Open a plan", [r["id"] for r in rows],
                            format_func=lambda i: next(r["name"] for r in rows if r["id"] == i))
        plan_name = next(r["name"] for r in rows if r["id"] == pick)
        body = store.load_item("plans", uid, pick, eng)
        if body:
            st.dataframe(pd.DataFrame(body["orders"]), hide_index=True, width="stretch")
            pdf_button(st, plan_name, body, me, key=f"pdf_{pick}")
            dec = body.get("decision") or {"status": "Draft", "comment": ""}
            st.markdown(f"**Decision: {dec.get('status', 'Draft')}**"
                        + (f" by {dec.get('by')} on {dec.get('at')}" if dec.get("by") else "")
                        + (f". Comment: {dec.get('comment')}" if dec.get("comment") else ""))
            if CAN_DECIDE:
                with st.form(f"decide_{pick}"):
                    comment = st.text_input("Comment (reason for the decision)")
                    a, b = st.columns(2)
                    ok = a.form_submit_button("Approve plan", type="primary")
                    no = b.form_submit_button("Reject plan")
                    if ok or no:
                        body["decision"] = {"status": "Approved" if ok else "Rejected", "comment": comment.strip(),
                                            "by": me, "at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                                            "submitted_by": (dec.get("submitted_by") if isinstance(dec, dict) else None)}
                        store.update_item("plans", uid, pick, body, eng)
                        audit("Plan " + body["decision"]["status"].lower(), f"{plan_name}. {comment.strip()}")
                        st.rerun()
            else:
                st.caption("Only a manager or admin can approve or reject.")

            # ----- drift alert -----
            st.markdown("**Is this plan still true?**")
            st.caption("Load your latest data in the Data tab first, then check. This plans today's data again with the "
                       "same settings and lists every order whose status or finish date changed.")
            if st.button("Check against my current data", key=f"drift_{pick}"):
                with st.spinner("Checking ..."):
                    changes, err = drift_check(body)
                st.session_state["drift"] = {"pick": pick, "changes": changes, "err": err}
                audit("Plan drift checked", plan_name)
            dr = st.session_state.get("drift")
            if dr and dr["pick"] == pick:
                if dr["err"]:
                    st.error(dr["err"])
                elif not dr["changes"]:
                    st.success("Nothing has changed. The approved plan is still true.")
                else:
                    st.warning(f"{len(dr['changes'])} orders have changed since this plan was made.")
                    st.dataframe(pd.DataFrame(dr["changes"]), hide_index=True, width="stretch")
                    if CAN_EDIT:
                        with st.form(f"alert_{pick}"):
                            to_a = st.text_input("Send an alert to", value=(dec.get("by") or "") if "@" in str(dec.get("by") or "") else "")
                            if st.form_submit_button("Prepare alert email") and "@" in to_a:
                                lines = [f"The plan '{plan_name}' is no longer accurate.", ""]
                                lines += [f"  - {c['order_id']}: {c['change']}. Was {c['was']}, now {c['now']}" for c in dr["changes"]]
                                lines += ["", "Please review the plan and make a new decision."]
                                st.session_state["alert_out"] = {"to": to_a.strip(), "subject": f"Plan changed: {plan_name}",
                                                                 "text": "\n".join(lines), "pick": pick}
                        ao = st.session_state.get("alert_out")
                        if ao and ao["pick"] == pick:
                            cfg = {k: secret(k) for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM")}
                            if mailer.smtp_ready(cfg):
                                err = mailer.send(cfg, ao["to"], ao["subject"], ao["text"])
                                st.session_state.pop("alert_out", None)
                                if err:
                                    st.error(err)
                                else:
                                    audit("Drift alert emailed", f"{plan_name} to {ao['to']}")
                                    st.success(f"Alert sent to {ao['to']}.")
                            else:
                                st.link_button("Open the alert in my email program", mailer.mailto(ao["to"], ao["subject"], ao["text"]))
                                st.text_area("Or copy this text", ao["text"], height=160)

            if CAN_EDIT:
                st.markdown("**Send to management for approval by email**")
                with st.form(f"mail_{pick}"):
                    to = st.text_input("Manager email")
                    note = st.text_area("Message (optional)")
                    go = st.form_submit_button("Create approval email")
                    if go:
                        if "@" not in to or "." not in to:
                            st.error("Enter a valid email address.")
                        else:
                            token = store.create_approval(uid, pick, to.strip(), eng)
                            base = (secret("APP_URL") or "").rstrip("/")
                            link = f"{base}/?approve={token}" if base else f"?approve={token}"
                            text_body = (note.strip() + "\n\n" if note.strip() else "") + mailer.summary_text(plan_name, body)
                            text_body += f"\n\nOpen this link to approve or reject (no login needed):\n{link}\n"
                            subject = f"Plan for approval: {plan_name}"
                            audit("Approval email created", f"{plan_name} for {to.strip()}")
                            st.session_state["mail_out"] = {"to": to.strip(), "subject": subject, "text": text_body,
                                                            "link": link, "base": bool(base), "pick": pick, "name": plan_name}
                out = st.session_state.get("mail_out")
                if out and out["pick"] == pick:
                    cfg = {k: secret(k) for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM")}
                    if mailer.smtp_ready(cfg):
                        files = []
                        pdf_bytes = make_pdf(out["name"], body, me)
                        if pdf_bytes:
                            files.append((pdf_bytes, "plan_summary.pdf", "pdf"))
                        files.append((mailer.plan_excel_from_body(body), "plan.xlsx", "xlsx"))
                        err = mailer.send(cfg, out["to"], out["subject"], out["text"], files)
                        st.session_state.pop("mail_out", None)
                        if err:
                            st.error(err)
                        else:
                            audit("Approval email sent", f"{out['name']} to {out['to']}")
                            st.success(f"Email sent to {out['to']}. The decision will show here when they answer.")
                    else:
                        st.info("Email sending is not set up on this server yet, so use the ready-made email below.")
                        st.link_button("Open in my email program", mailer.mailto(out["to"], out["subject"], out["text"]))
                        st.text_area("Or copy this text into any email", out["text"], height=220)
                    if not out["base"]:
                        st.warning("The link above is incomplete because APP_URL is not set. Add APP_URL to the app secrets "
                                   "(your app's web address) so the link works for the manager.")
                if st.button("Delete this plan", key=f"delplan_{pick}"):
                    store.delete_item("plans", uid, pick, eng)
                    audit("Plan deleted", plan_name)
                    st.rerun()
    else:
        st.write("No saved plans yet.")

    st.subheader("Feedback")
    with st.form("fb", clear_on_submit=True):
        txt = st.text_area("What worked, what is missing, what was wrong?")
        if st.form_submit_button("Send feedback") and txt.strip():
            store.save_item("feedback", uid, txt.strip()[:60], {"text": txt.strip(), "by": me}, eng)
            st.success("Thank you.")
    for it in store.list_items("feedback", uid, eng)[:20]:
        body = store.load_item("feedback", uid, it["id"], eng) or {}
        st.write(f":gray[{it['created']}] {body.get('text', '')}")

# ---------------- team and audit tab ----------------
if tab_team is not None:
    with tab_team:
        if CAN_TEAM:
            st.subheader("Team")
            st.caption("Everyone here shares the same data and plans. The role decides what each person may do.")
            st.markdown("\n".join(f"- **{r}**: {t}" for r, t in store.ROLE_TEXT.items()))
            members = store.list_members(uid, eng)
            st.dataframe(pd.DataFrame(members).drop(columns=["id"]), hide_index=True, width="stretch")
            with st.form("add_member", clear_on_submit=True):
                m1, m2, m3 = st.columns(3)
                nu = m1.text_input("New username")
                npw = m2.text_input("Temporary password (8+ characters)", type="password")
                nr = m3.selectbox("Role", ["planner", "manager", "viewer"])
                if st.form_submit_button("Add team member"):
                    err = store.add_member(uid, nu, npw, nr, eng)
                    if err:
                        st.error(err)
                    else:
                        audit("Team member added", f"{nu.strip().lower()} as {nr}")
                        st.success("Added. Tell them their username and temporary password, and to change it after signing in.")
                        st.rerun()
            others = [m for m in members if m["role"] != "admin"]
            if others:
                who = st.selectbox("Change or remove a member", [m["id"] for m in others],
                                   format_func=lambda i: next(f"{m['username']} ({m['role']})" for m in others if m["id"] == i))
                with st.form("member_actions", clear_on_submit=True):
                    new_pw = st.text_input("New temporary password", type="password")
                    r1, r2 = st.columns(2)
                    do_pw = r1.form_submit_button("Reset password")
                    do_rm = r2.form_submit_button("Remove member")
                    uname = next(m["username"] for m in others if m["id"] == who)
                    if do_pw:
                        err = store.reset_member_password(uid, who, new_pw, eng)
                        if err:
                            st.error(err)
                        else:
                            audit("Member password reset", uname)
                            st.success("Password reset.")
                    if do_rm:
                        store.remove_member(uid, who, eng)
                        audit("Team member removed", uname)
                        st.rerun()
        if CAN_AUDIT:
            st.subheader("Audit trail")
            st.caption("Who did what and when. Newest first.")
            log_rows = store.list_audit(uid, 300, eng)
            st.dataframe(pd.DataFrame(log_rows), hide_index=True, width="stretch")
            if log_rows:
                st.download_button("Download audit trail (CSV)", pd.DataFrame(log_rows).to_csv(index=False),
                                   file_name="audit_trail.csv")
