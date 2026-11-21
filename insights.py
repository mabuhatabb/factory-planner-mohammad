"""Money view, capacity outlook, delivery promise and plan-drift checks. No Streamlit code here."""
from datetime import timedelta

import pandas as pd

import scheduler
import tables

DEFAULT_MONEY = {"ot_rate": 0.0, "penalty_pct": 0.0, "penalty_flat": 0.0}


def _isnum(v):
    try:
        return v is not None and not pd.isna(v)
    except Exception:
        return False


def money(orders, load, M):
    """orders: list of dicts with status, days_late, value. load: list of dicts with overtime.
    M: {'ot_rate': money per overtime hour, 'penalty_pct': % of order value per late day, 'penalty_flat': money per late day}."""
    M = {**DEFAULT_MONEY, **(M or {})}
    ot_hours = sum(int(r.get("overtime") or 0) for r in load)
    ot_cost = ot_hours * float(M["ot_rate"])
    penalty, at_risk, value_on_time, value_all = 0.0, 0.0, 0.0, 0.0
    for o in orders:
        v = float(o["value"]) if _isnum(o.get("value")) else 0.0
        value_all += v
        if o["status"] == "Late":
            days = int(o.get("days_late") or 0)
            penalty += days * (v * float(M["penalty_pct"]) / 100.0 + float(M["penalty_flat"]))
            at_risk += v
        elif o["status"] == "Not scheduled":
            at_risk += v
        elif o["status"] == "On time":
            value_on_time += v
    return {"overtime_cost": round(ot_cost), "late_penalty": round(penalty), "extra_cost": round(ot_cost + penalty),
            "value_at_risk": round(at_risk), "value_on_time": round(value_on_time), "value_all": round(value_all)}


def has_money(M, orders):
    M = {**DEFAULT_MONEY, **(M or {})}
    return any(float(M[k]) > 0 for k in M) or any(_isnum(o.get("value")) and float(o["value"]) > 0 for o in orders)


def fmt(n):
    return f"{int(round(n)):,}"


def weekly_load(load_rows):
    """Weekly utilisation per crew: DataFrame(week, resource, planned, capacity, used_pct)."""
    df = pd.DataFrame(load_rows)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    df["week"] = df["date"] - pd.to_timedelta(df["date"].dt.weekday, unit="D")
    g = df.groupby(["week", "resource"])[["planned", "capacity"]].sum().reset_index()
    g = g[g["capacity"] > 0]
    g["used_pct"] = (100 * g["planned"] / g["capacity"]).round()
    return g


def bottleneck_text(load_rows):
    g = weekly_load(load_rows)
    if g.empty:
        return ""
    tot = g.groupby("resource")[["planned", "capacity"]].sum()
    tot["pct"] = 100 * tot["planned"] / tot["capacity"]
    top = tot["pct"].idxmax()
    full_weeks = int((g[g["resource"] == top]["used_pct"] >= 95).sum())
    idle = tot["pct"].idxmin()
    return (f"Busiest crew: {top} ({tot.loc[top, 'pct']:.0f}% of its hours used over the plan, "
            f"{full_weeks} weeks at 95% or more). Least used: {idle} ({tot.loc[idle, 'pct']:.0f}%). "
            f"Add capacity at {top}; {idle} could take work or people could move from it.")


def promise(data, plan_start, settings, base_res, new_order, seconds=15):
    """Earliest finish for a new order without hurting existing ones.

    new_order: dict(product_type, quantity, start_not_before). Existing orders keep their weight x10 so the new order only
    takes leftover capacity. Returns dict(finish, days_after_start, hurt: list of order ids that got later, error)."""
    d = {k: v.copy() for k, v in data.items()}
    new_id = "NEW ORDER"
    row = {"order_id": new_id, "product_type": new_order["product_type"], "quantity": new_order["quantity"],
           "due_date": plan_start + timedelta(days=int(settings["horizon_days"]) - 1), "priority": 1,
           "start_not_before": new_order.get("start_not_before"), "status": "", "order_value": None}
    d["orders"] = pd.concat([d["orders"], pd.DataFrame([row])], ignore_index=True)
    s = dict(settings, time_limit_seconds=min(int(settings.get("time_limit_seconds", 20)), seconds), use_priority=True)
    jobs, cap, days, problems, skipped = tables.prepare(d, plan_start, s)
    for sk in skipped:
        if sk["order_id"] == new_id:
            return {"error": sk["reason"]}
    for j in jobs:
        if j["order_id"] != new_id:
            j["priority"] = int(j["priority"]) * 10 if s["use_priority"] else 10
    res = scheduler.solve(jobs, cap, days, s)
    if res["status"] == "none":
        return {"error": "No plan found. Try more solver time."}
    me = next(o for o in res["orders"] if o["order_id"] == new_id)
    if me["status"] == "Not scheduled":
        return {"error": "It cannot be completed within the planned days. Plan more days or add capacity.", "finish": None}
    before = {o["order_id"]: o for o in base_res["orders"]}
    hurt = []
    for o in res["orders"]:
        b = before.get(o["order_id"])
        if o["order_id"] == new_id or not b:
            continue
        if b["finish"] is not None and (o["finish"] is None or o["finish"] > b["finish"]):
            if o["status"] in ("Late", "Not scheduled") and (b["status"] == "On time" or b["status"] == "Late"):
                hurt.append(o["order_id"])
    return {"finish": me["finish"], "days": (me["finish"] - plan_start).days, "hurt": hurt, "error": None}


def drift(old_orders, new_orders):
    """Compare an approved plan with a fresh plan. Returns list of change dicts."""
    old = {o["order_id"]: o for o in old_orders}
    new = {o["order_id"]: o for o in new_orders}
    changes = []
    for oid, o in new.items():
        b = old.get(oid)
        if not b:
            changes.append({"order_id": oid, "change": "New order not in the approved plan", "was": "-", "now": o["status"]})
            continue
        f_old = str(b.get("finish") or "-")[:10]
        f_new = str(o.get("finish") or "-")[:10]
        if b["status"] != o["status"] or f_old != f_new:
            changes.append({"order_id": oid, "change": "Status or finish date changed",
                            "was": f"{b['status']} ({f_old})", "now": f"{o['status']} ({f_new})"})
    for oid in old:
        if oid not in new:
            changes.append({"order_id": oid, "change": "Order removed or no longer planned", "was": old[oid]["status"], "now": "-"})
    return changes


# ---------- plan comparison and history ----------
def plan_metrics(body):
    o = pd.DataFrame(body.get("orders", []))
    ld = pd.DataFrame(body.get("load", []))
    n = len(o)
    cnt = lambda s: int((o["status"] == s).sum()) if n else 0
    return {"orders": n, "on_time": cnt("On time"), "late": cnt("Late"), "not_scheduled": cnt("Not scheduled"),
            "late_days": int(o["days_late"].fillna(0).sum()) if n else 0,
            "overtime_h": int(ld["overtime"].sum()) if len(ld) else 0,
            "on_time_pct": round(100 * cnt("On time") / n, 1) if n else 0.0}


def weekly_history(items):
    """items: list of (saved_iso, metrics dict). One row per week, using the last plan saved that week."""
    rows = []
    for saved, m in items:
        t = pd.to_datetime(saved)
        rows.append({"week": (t - pd.Timedelta(days=t.weekday())).normalize(), "saved": t, **m})
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).sort_values("saved")
    return df.groupby("week", as_index=False).last().drop(columns=["saved"])


def compare_plans(a, b):
    """Line-by-line comparison of two saved plans. Returns (orders_df, steps_df)."""
    oa = {o["order_id"]: o for o in a.get("orders", [])}
    ob = {o["order_id"]: o for o in b.get("orders", [])}
    rows = []
    for oid in list(dict.fromkeys(list(oa) + list(ob))):
        x, y = oa.get(oid), ob.get(oid)
        fa = str((x or {}).get("finish") or "-")[:10]
        fb = str((y or {}).get("finish") or "-")[:10]
        if x is None:
            note = "Only in B"
        elif y is None:
            note = "Only in A"
        elif fa == fb and x["status"] == y["status"]:
            note = "Same"
        else:
            try:
                dd = (pd.to_datetime(fb) - pd.to_datetime(fa)).days
                note = f"Finishes {abs(dd)} days {'later' if dd > 0 else 'earlier'}" if dd else "Status changed"
            except Exception:
                note = "Changed"
        rows.append({"order_id": oid, "status A": (x or {}).get("status", "-"), "finish A": fa,
                     "status B": (y or {}).get("status", "-"), "finish B": fb, "change": note})
    sa = {(r["order_id"], r["step_no"]): r for r in a.get("steps", [])}
    sb = {(r["order_id"], r["step_no"]): r for r in b.get("steps", [])}
    srows = []
    for key in sorted(set(sa) | set(sb), key=lambda k: (str(k[0]), k[1])):
        x, y = sa.get(key), sb.get(key)
        fmt_ = lambda r: "-" if r is None else f"{r['resource']}: {str(r['start'])[:10]} to {str(r['end'])[:10]}"
        if fmt_(x) != fmt_(y):
            srows.append({"order_id": key[0], "step_no": key[1], "step": (x or y)["step_name"], "A": fmt_(x), "B": fmt_(y)})
    return pd.DataFrame(rows), pd.DataFrame(srows)
