"""Multi-step project scheduler (OR-Tools CP-SAT). No Streamlit code in this file.

Model in plain words
- Every order is a project with ordered steps (frame, paint, assembly, test ...).
- Each step needs a number of hours and is done by ONE of its allowed resources
  (a crew or a machine), spread over as many days as that resource's calendar allows.
- A step may only start after the previous step has finished (plus an optional gap).
- A step may not start before its parts have arrived or before the order's release date.
- Each resource has a capacity in hours for every calendar day (crew size x shift hours,
  zero on days off, holidays, vacations or maintenance). Overtime can add hours at a cost.
- The goal is: finish orders on time, weigh important orders more, finish early,
  use little overtime. An order that cannot fit in the horizon is reported as not scheduled.
"""
import os

from ortools.sat.python import cp_model

DEFAULT_SETTINGS = {
    "horizon_days": 90,        # calendar days planned from the plan start
    "late_cost": 100,          # points lost per day an order finishes after its due date
    "earliness_weight": 1,     # small points lost per day a step finishes later
    "use_priority": True,      # priority multiplies the cost of lateness
    "use_parts": True,         # steps wait for their parts to arrive
    "gap_days": 1,             # days between the end of one step and the start of the next
    "max_overtime": 0,         # extra hours per resource per working day
    "overtime_cost": 5,        # points lost per overtime hour
    "time_limit_seconds": 20,  # how long the solver may search
    "shift_start_hour": 8,     # clock hour the working day starts (used for hour-by-hour times)
}


def _solve_model(jobs, cap, days, settings=None):
    """Plan the jobs.

    jobs: list of dicts
        {"order_id", "priority", "due_idx", "steps": [
            {"step_no", "name", "hours", "resources": [names], "earliest": day index}]}
    cap:  {resource: [capacity hours for each day index]}
    days: list of datetime.date, one per day index

    Returns a dict with lists of dicts: orders, steps, daily, load, plus status text.
    Returns {"status": "none"} when no plan could be built in time.
    """
    s = dict(DEFAULT_SETTINGS)
    if settings:
        s.update(settings)
    horizon = len(days)
    gap = int(s["gap_days"])
    late_cost = int(s["late_cost"])
    earl = int(s["earliness_weight"])
    max_ot = int(s["max_overtime"])
    ot_cost = int(s["overtime_cost"])

    model = cp_model.CpModel()
    inc = {}          # job index -> bool: is the order planned
    assign = {}       # (j, k, r) -> bool
    x = {}            # (j, k, r, d) -> hours of step k on resource r on day d
    used = {}         # (j, k, d) -> bool: any work on that day
    finish = {}       # (j, k) -> last day index with work
    by_rd = {}        # (r, d) -> list of x vars
    by_rdp = {}       # (r, d) -> {product type: list of x vars}  (for changeover time)
    begun = {}        # (j, k) -> {day: bool "step has started by this day"}  (for materials)
    day_vars = {}     # (j, k) -> list of day indexes that exist

    step_res = {}     # (j, k) -> resources this step may use (one only when locked)
    for j, job in enumerate(jobs):
        inc[j] = model.NewBoolVar(f"inc_{j}")
        if any(st.get("lock") for st in job["steps"]):
            model.Add(inc[j] == 1)           # an order with a locked step must be planned
        for k, step in enumerate(job["steps"]):
            hours = int(step["hours"])
            earliest = max(0, int(step["earliest"]))
            lock = step.get("lock")
            step_res[j, k] = [lock["resource"]] if lock else list(step["resources"])
            last_day = horizon - 1 if not lock else min(horizon - 1, int(lock["end"]))
            first_day = earliest if not lock else max(earliest, int(lock["start"]))
            options = []
            for r in step_res[j, k]:
                avail = []
                for d in range(first_day, last_day + 1):
                    c = cap[r][d]
                    if c <= 0:
                        continue
                    ub = min(hours, c + max_ot)
                    if ub > 0:
                        x[j, k, r, d] = model.NewIntVar(0, ub, f"x_{j}_{k}_{r}_{d}")
                        by_rd.setdefault((r, d), []).append(x[j, k, r, d])
                        by_rdp.setdefault((r, d), {}).setdefault(job.get("product", ""), []).append(x[j, k, r, d])
                        avail.append(d)
                a = model.NewBoolVar(f"a_{j}_{k}_{r}")
                assign[j, k, r] = a
                if avail:
                    model.Add(sum(x[j, k, r, d] for d in avail) == hours * a)
                else:
                    model.Add(a == 0)
                options.append(a)
            model.Add(sum(options) == inc[j])

            dlist = sorted({d for (jj, kk, r, d) in x if jj == j and kk == k})
            day_vars[j, k] = dlist
            finish[j, k] = model.NewIntVar(0, horizon - 1, f"fin_{j}_{k}")
            for d in dlist:
                total = sum(x[j, k, r, d] for r in step_res[j, k] if (j, k, r, d) in x)
                u = model.NewBoolVar(f"u_{j}_{k}_{d}")
                used[j, k, d] = u
                model.Add(total >= 1).OnlyEnforceIf(u)
                model.Add(total == 0).OnlyEnforceIf(u.Not())
                model.Add(finish[j, k] >= d).OnlyEnforceIf(u)
                if k > 0:
                    model.Add(finish[j, k - 1] + gap <= d).OnlyEnforceIf(u)
            if lock and dlist:
                model.Add(used[j, k, dlist[0]] == 1)
                model.Add(used[j, k, dlist[-1]] == 1)
            if step.get("materials"):
                chain, prev = {}, None
                for d in dlist:
                    b = model.NewBoolVar(f"b_{j}_{k}_{d}")
                    model.Add(b >= used[j, k, d])
                    if prev is not None:
                        model.Add(b >= prev)
                    chain[d] = prev = b
                begun[j, k] = chain

    # materials: what steps have started by a day may not use more than the stock that has arrived by then
    supply = getattr(cap, "supply", {}) or {}
    mats = {m for job in jobs for st in job["steps"] for m in (st.get("materials") or {})}
    for m in mats:
        users = [(j, k, int(jobs[j]["steps"][k]["materials"][m])) for (j, k) in begun
                 if m in jobs[j]["steps"][k]["materials"]]
        check_days = sorted({d for (j, k, q) in users for d in begun[j, k]})
        have = supply.get(m, [0] * horizon)
        for d in check_days:
            terms = []
            for j, k, q in users:
                prior = [dd for dd in begun[j, k] if dd <= d]
                if prior:
                    terms.append(q * begun[j, k][max(prior)])
            if terms:
                model.Add(sum(terms) <= int(have[d]))

    # capacity per resource and day (+ overtime), including changeover time when different products share a day
    changeover = getattr(cap, "changeover", {}) or {}
    ot = {}
    for (r, d), terms in by_rd.items():
        base = cap[r][d]
        h = int(changeover.get(r, 0) or 0)
        prods = by_rdp.get((r, d), {})
        extra = 0
        if h > 0 and len(prods) > 1:
            big = base + max_ot
            zs = []
            for pi, vs in enumerate(prods.values()):
                z = model.NewBoolVar(f"z_{r}_{d}_{pi}")
                model.Add(sum(vs) <= big * z)
                zs.append(z)
            sw = model.NewIntVar(0, len(zs) - 1, f"sw_{r}_{d}")
            model.Add(sw >= sum(zs) - 1)
            extra = h * sw
        if max_ot > 0:
            ot[r, d] = model.NewIntVar(0, max_ot, f"ot_{r}_{d}")
            model.Add(sum(terms) + extra <= base + ot[r, d])
        else:
            model.Add(sum(terms) + extra <= base)

    # objective
    objective = []
    tard = {}
    for j, job in enumerate(jobs):
        weight = int(job["priority"]) if s["use_priority"] else 1
        last = len(job["steps"]) - 1
        due = int(job["due_idx"])
        hi = max(0, horizon - 1 - due) + 1
        tard[j] = model.NewIntVar(0, hi, f"tard_{j}")
        model.Add(tard[j] >= finish[j, last] - due)
        objective.append(late_cost * weight * tard[j])
        for k in range(len(job["steps"])):
            objective.append(earl * weight * finish[j, k])
    for var in ot.values():
        objective.append(ot_cost * var)
    cost = sum(objective)
    total_time = float(s["time_limit_seconds"])
    workers = max(1, min(8, os.cpu_count() or 1))

    # Phase 1: fit as many orders as possible (priority-weighted)
    weights = [int(j_["priority"]) if s["use_priority"] else 1 for j_ in jobs]
    model.Maximize(sum(weights[j] * inc[j] for j in range(len(jobs))))
    for j in range(len(jobs)):
        model.AddHint(inc[j], 0)             # "plan nothing" is always allowed: gives the solver a safe start
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(1.0, total_time / 3)
    solver.parameters.num_workers = workers
    solver.parameters.random_seed = 1
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        # large data: the first search needs more time. Try once more with the whole time limit.
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = max(2.0, total_time)
        solver.parameters.num_workers = workers
        solver.parameters.random_seed = 1
        status = solver.Solve(model)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return {"status": "none"}
    phase1_proven = status == cp_model.OPTIMAL
    best_fit = int(round(solver.ObjectiveValue()))
    model.Add(sum(weights[j] * inc[j] for j in range(len(jobs))) >= best_fit)
    model.ClearHints()
    for j in range(len(jobs)):
        model.AddHint(inc[j], solver.Value(inc[j]))

    # Phase 2: among those plans, minimise lateness, earliness and overtime cost
    model.Minimize(cost)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(1.0, total_time * 2 / 3)
    solver.parameters.num_workers = workers
    solver.parameters.relative_gap_limit = 0.01
    solver.parameters.random_seed = 1
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {"status": "none"}

    # read the result
    per_step = {}
    daily = []
    for (j, k, r, d), var in x.items():
        v = solver.Value(var)
        if v > 0:
            per_step.setdefault((j, k), {}).setdefault(d, 0)
            per_step[j, k][d] += v
            daily.append({"order_id": jobs[j]["order_id"], "step_no": jobs[j]["steps"][k]["step_no"],
                          "step_name": jobs[j]["steps"][k]["name"], "resource": r,
                          "date": days[d], "hours": v})

    steps_rows, orders_rows = [], []
    for j, job in enumerate(jobs):
        planned = bool(solver.Value(inc[j]))
        last_day = None
        for k, step in enumerate(job["steps"]):
            if not planned:
                continue
            chosen = [r for r in step_res[j, k] if solver.Value(assign[j, k, r])]
            dd = per_step.get((j, k), {})
            if not chosen or not dd:
                continue
            steps_rows.append({
                "order_id": job["order_id"], "step_no": step["step_no"], "step_name": step["name"],
                "resource": chosen[0], "start": days[min(dd)], "end": days[max(dd)],
                "hours": sum(dd.values()),
            })
            if k == len(job["steps"]) - 1:
                last_day = max(dd)
        due = int(job["due_idx"])
        if not planned or last_day is None:
            orders_rows.append({"order_id": job["order_id"], "finish": None,
                                "days_late": None, "status": "Not scheduled"})
        else:
            late = max(0, last_day - due)
            orders_rows.append({"order_id": job["order_id"], "finish": days[last_day],
                                "days_late": late, "status": "Late" if late > 0 else "On time"})

    load = []
    for r, caps in cap.items():
        h = int(changeover.get(r, 0) or 0)
        for d in range(horizon):
            hours = sum(solver.Value(v) for v in by_rd.get((r, d), []))
            setup = 0
            if h > 0:
                present = sum(1 for vs in by_rdp.get((r, d), {}).values() if sum(solver.Value(v) for v in vs) > 0)
                setup = h * max(0, present - 1)
            if caps[d] > 0 or hours > 0:
                load.append({"resource": r, "date": days[d], "capacity": int(caps[d]),
                             "planned": int(hours + setup), "setup": int(setup),
                             "overtime": int(max(0, hours + setup - caps[d]))})

    return {
        "status": "optimal" if (status == cp_model.OPTIMAL and phase1_proven) else "feasible",
        "orders": orders_rows, "steps": steps_rows, "daily": daily, "load": load,
    }


def _cannot_fit(job, cap, horizon, max_ot):
    """True when some step needs more hours than its best resource has in the whole horizon."""
    for st in job["steps"]:
        best = 0
        for r in st["resources"]:
            total = sum(c + max_ot for c in cap[r][max(0, st["earliest"]):] if c > 0)
            best = max(best, total)
        if st["hours"] > best:
            return True
    return False


def solve(jobs, cap, days, settings=None):
    """Same as before, but orders that can never fit are marked Not scheduled without searching."""
    s = dict(DEFAULT_SETTINGS)
    if settings:
        s.update(settings)
    horizon = len(days)
    max_ot = int(s["max_overtime"])
    def locked(j):
        return any(st.get("lock") for st in j["steps"])
    fits = [j for j in jobs if locked(j) or not _cannot_fit(j, cap, horizon, max_ot)]
    impossible = [j for j in jobs if not locked(j) and _cannot_fit(j, cap, horizon, max_ot)]
    if fits:
        res = _solve_model(fits, cap, days, s)
        if res["status"] == "none":
            return res
    else:
        res = {"status": "optimal", "orders": [], "steps": [], "daily": [],
               "load": [{"resource": r, "date": days[d], "capacity": int(cap[r][d]), "planned": 0, "setup": 0, "overtime": 0}
                        for r in cap for d in range(horizon) if cap[r][d] > 0]}
    for j in impossible:
        res["orders"].append({"order_id": j["order_id"], "finish": None, "days_late": None,
                              "status": "Not scheduled"})
    order_of = {j["order_id"]: i for i, j in enumerate(jobs)}
    res["orders"].sort(key=lambda o: order_of[o["order_id"]])
    try:
        import timing
        timing.add_times(res, jobs, cap, days, s)
    except Exception:
        res.setdefault("blocks", [])
    return res
