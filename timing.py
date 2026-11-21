"""Turn the day-by-day plan into clock times (hour level). No Streamlit code here.

The planner decides which crew works on which step on which day. This module then lays the work out inside each
day: urgent orders first, then in step order, with changeover time between different products. A crew of 3 doing
24 labor hours works 8 clock hours, so times are clock hours, not labor hours.
"""
from datetime import datetime, time, timedelta


def add_times(res, jobs, cap, days, settings):
    start_hour = int(settings.get("shift_start_hour", 8))
    clock = getattr(cap, "clock", {}) or {}
    changeover = getattr(cap, "changeover", {}) or {}
    info = {j["order_id"]: (i, j["priority"], j["due_idx"], j.get("product", "")) for i, j in enumerate(jobs)}
    idx = {d: i for i, d in enumerate(days)}
    rows = sorted(res.get("daily", []), key=lambda r: (r["date"], -info[r["order_id"]][1], info[r["order_id"]][2],
                                                       info[r["order_id"]][0], r["step_no"]))
    free, last_prod, order_end = {}, {}, {}
    blocks = []

    def mins(labor_hours, r, d):
        c = cap[r][idx[d]]
        clk = (clock.get(r) or [8] * len(days))[idx[d]] or 8
        return max(1, round(labor_hours * clk * 60 / c)) if c > 0 else round(labor_hours * 60)

    for row in rows:
        r, d, oid = row["resource"], row["date"], row["order_id"]
        key = (r, d)
        ptr = free.get(key, 0)
        prod = info[oid][3]
        h = int(changeover.get(r, 0) or 0)
        if h > 0 and last_prod.get(key) is not None and last_prod[key] != prod:
            m = mins(h, r, d)
            blocks.append({"order_id": "Setup", "step_no": 0, "step_name": "Changeover", "resource": r, "date": d,
                           "_s": ptr, "_e": ptr + m, "hours": h, "kind": "setup"})
            ptr += m
        ptr = max(ptr, order_end.get((oid, d), 0))
        m = mins(row["hours"], r, d)
        blocks.append({"order_id": oid, "step_no": row["step_no"], "step_name": row["step_name"], "resource": r,
                       "date": d, "_s": ptr, "_e": ptr + m, "hours": row["hours"], "kind": "work"})
        free[key] = ptr + m
        order_end[(oid, d)] = ptr + m
        last_prod[key] = prod
    for b in blocks:
        base = datetime.combine(b["date"], time(start_hour))
        b["start_time"] = (base + timedelta(minutes=b.pop("_s")))
        b["end_time"] = (base + timedelta(minutes=b.pop("_e")))
    res["blocks"] = blocks
    fmt = lambda t: t.strftime("%Y-%m-%d %H:%M")
    for st in res.get("steps", []):
        mine = [b for b in blocks if b["kind"] == "work" and b["order_id"] == st["order_id"] and b["step_no"] == st["step_no"]]
        if mine:
            st["start_time"] = fmt(min(b["start_time"] for b in mine))
            st["end_time"] = fmt(max(b["end_time"] for b in mine))
    return res
