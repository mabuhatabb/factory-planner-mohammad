"""Plain-language reasons for late orders, and what-if options. No Streamlit code here."""
import copy
from collections import defaultdict

import pandas as pd

import insights
import scheduler
import tables


def _fmt(d):
    return d.strftime("%d %b %Y")


def _asap(job, cap, horizon, gap, max_ot, ignore_earliest=False):
    """Earliest possible finish of this order if every crew were free. Returns (finish_idx, [(start, end, resource)]) or None."""
    prev_end, out = None, []
    for st in job["steps"]:
        start_min = 0 if ignore_earliest else max(0, int(st["earliest"]))
        if prev_end is not None:
            start_min = max(start_min, prev_end + gap)
        best = None
        for r in st["resources"]:
            remaining, first, last = int(st["hours"]), None, None
            for d in range(start_min, horizon):
                c = cap[r][d]
                if c <= 0:
                    continue
                take = min(remaining, c + max_ot)
                if first is None:
                    first = d
                remaining -= take
                last = d
                if remaining <= 0:
                    break
            if remaining <= 0 and (best is None or last < best[1]):
                best = (first, last, r)
        if best is None:
            return None
        out.append(best)
        prev_end = best[1]
    return prev_end, out


def explain(jobs, cap, days, res, settings):
    """Returns {order_id: reason text} for late and not scheduled orders."""
    s = dict(scheduler.DEFAULT_SETTINGS)
    s.update(settings or {})
    horizon, gap, max_ot = len(days), int(s["gap_days"]), int(s["max_overtime"])
    idx = {d: i for i, d in enumerate(days)}
    by_id = {j["order_id"]: j for j in jobs}
    steps_of = defaultdict(list)
    for r in res["steps"]:
        steps_of[r["order_id"]].append(r)
    daily = defaultdict(lambda: defaultdict(int))     # (resource, day idx) -> {order: hours}
    for r in res["daily"]:
        daily[r["resource"], idx[r["date"]]][r["order_id"]] += r["hours"]

    out = {}
    for o in res["orders"]:
        oid, status = o["order_id"], o["status"]
        job = by_id[oid]
        if status == "On time":
            continue
        alone = _asap(job, cap, horizon, gap, max_ot)
        if status == "Not scheduled":
            bad = None
            for st in job["steps"]:
                total = {r: sum(c + max_ot for c in cap[r][max(0, st["earliest"]):] if c > 0) for r in st["resources"]}
                r_best = max(total, key=total.get)
                if st["hours"] > total[r_best]:
                    bad = (st, r_best, total[r_best])
                    break
            if bad:
                st, r, t = bad
                out[oid] = (f"Step '{st['name']}' needs {st['hours']} h, but the best crew ({r}) can give only {t} h in the "
                            f"{horizon} days planned. Add crew, allow overtime, split the order or plan more days.")
            elif alone is None:
                out[oid] = ("The steps cannot all be completed in the days planned, because each step must wait for the "
                            "previous one. Plan more days or add capacity.")
            else:
                out[oid] = (f"It fits on its own (earliest finish {_fmt(days[alone[0]])}), but not together with the other "
                            f"orders. The plan gave the capacity to orders with higher priority or earlier due dates.")
            continue

        # Late
        due = int(job["due_idx"])
        if alone is None:
            out[oid] = "It cannot be completed within the days planned."
            continue
        lb, chain = alone
        if lb > due:
            free = _asap(job, cap, horizon, gap, max_ot, ignore_earliest=True)
            if free and free[0] <= due:
                late_step = max(job["steps"], key=lambda st: st["earliest"])
                out[oid] = (f"Cannot be on time because work cannot start until {_fmt(days[min(horizon - 1, late_step['earliest'])])} "
                            f"(its earliest start or parts arrival, step '{late_step['name']}'). Even with every crew free "
                            f"it finishes {_fmt(days[lb])}, after the due date {_fmt(days[min(horizon - 1, max(0, due))]) if due < horizon else 'in the future'}.")
            else:
                lens = sorted(((e - b + 1, st["name"]) for (b, e, _), st in zip(chain, job["steps"])), reverse=True)[:2]
                longest = ", ".join(f"{n} ({d} calendar days)" for d, n in lens)
                out[oid] = (f"The work itself is too long for the due date: even with every crew free the earliest finish is "
                            f"{_fmt(days[lb])}. Longest steps: {longest}. Add crew, allow overtime or move the due date.")
            continue

        # alone it could be on time: find where other orders delayed it
        mine = sorted(steps_of[oid], key=lambda r: r["step_no"])
        prev_extra, worst = 0, None
        for k, (row, (b, e, _)) in enumerate(zip(mine, chain)):
            actual_end = idx[row["end"]]
            extra_total = actual_end - e
            extra = extra_total - prev_extra
            prev_extra = max(prev_extra, extra_total)
            if worst is None or extra > worst[0]:
                worst = (extra, row, b, actual_end)
        extra, row, b, actual_end = worst
        r = row["resource"]
        others = defaultdict(int)
        for d in range(max(0, b), actual_end + 1):
            for other, h in daily.get((r, d), {}).items():
                if other != oid:
                    others[other] += h
        names = ", ".join(sorted(others, key=others.get, reverse=True)[:3])
        if names:
            out[oid] = (f"On its own it could finish by {_fmt(days[lb])}. Step '{row['step_name']}' lost about {max(extra, 1)} days "
                        f"because {r} was busy with {names}.")
        else:
            out[oid] = (f"On its own it could finish by {_fmt(days[lb])}. Step '{row['step_name']}' on {r} ran later because of "
                        f"the crew calendar (holidays, vacation, maintenance) or limited hours.")
    return out


def run_options(data, plan_start, settings, base_res, seconds=15, M=None):
    """Compare the current plan with simple changes. Returns list of dict rows."""
    s0 = dict(settings)
    s0["time_limit_seconds"] = min(int(s0.get("time_limit_seconds", 20)), seconds)

    values = {str(r["order_id"]).strip(): r.get("order_value") for _, r in data["orders"].iterrows()}

    def summarize(label, change, res):
        o = pd.DataFrame(res["orders"])
        ld = pd.DataFrame(res["load"])
        rows_ = [{**r, "value": values.get(r["order_id"])} for r in res["orders"]]
        mm = insights.money(rows_, res["load"], M)
        return {"option": label, "change": change, "extra cost": mm["extra_cost"], "revenue at risk": mm["value_at_risk"],
                "on time": int((o["status"] == "On time").sum()), "late": int((o["status"] == "Late").sum()),
                "not scheduled": int((o["status"] == "Not scheduled").sum()),
                "late days": int(o["days_late"].fillna(0).sum()),
                "overtime hours": int(ld["overtime"].sum()) if len(ld) else 0}

    rows = [summarize("Current plan", "As planned", base_res)]
    # busiest crew
    ld = pd.DataFrame(base_res["load"])
    busiest = None
    if len(ld):
        g = ld.groupby("resource")[["planned", "capacity"]].sum()
        g = g[g["capacity"] > 0]
        if len(g):
            busiest = (g["planned"] / g["capacity"]).idxmax()

    def run(d, st):
        jobs, cap, days, _, _ = tables.prepare(d, plan_start, st)
        if not jobs:
            return None
        r = scheduler.solve(jobs, cap, days, st)
        return None if r["status"] == "none" else r

    extra_ot = 2
    s1 = dict(s0, max_overtime=int(s0["max_overtime"]) + extra_ot)
    r = run(data, s1)
    if r:
        rows.append(summarize(f"+{extra_ot} h overtime per day", f"Every crew may work {s1['max_overtime']} extra h per day", r))
    d2 = None
    if busiest:
        d2 = {k: v.copy() for k, v in data.items()}
        res_t = d2["resources"]
        m = res_t["resource"].astype(str).str.strip() == busiest
        res_t.loc[m, "crew_size"] = pd.to_numeric(res_t.loc[m, "crew_size"], errors="coerce").fillna(0) + 1
        r = run(d2, s0)
        if r:
            rows.append(summarize(f"+1 person in {busiest}", f"{busiest} is the busiest crew", r))
        r = run(d2, s1)
        if r:
            rows.append(summarize("Both", f"+1 person in {busiest} and +{extra_ot} h overtime", r))
    return rows
