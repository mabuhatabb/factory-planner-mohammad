import pandas as pd
from ortools.sat.python import cp_model

# All tunable numbers live here. The app can override any of them.
DEFAULT_SETTINGS = {
    "on_time_points": 100,     # reward per unit finished by its due date
    "late_points": 10,         # reward per unit finished after its due date
    "earlier_bonus": 1,        # points lost per day a unit finishes later
    "use_priority": True,      # priority multiplies the reward
    "use_materials": True,     # enforce material limits
    "max_overtime": 0,         # extra minutes per machine per day
    "overtime_cost": 1,        # points lost per overtime minute
    "time_limit_seconds": 10,  # how long the solver may search
}


def empty_usage():
    return pd.DataFrame({"product": pd.Series(dtype="str"),
                         "material": pd.Series(dtype="str"),
                         "kg_per_unit": pd.Series(dtype="int")})


def empty_stock():
    return pd.DataFrame({"material": pd.Series(dtype="str"),
                         "day": pd.Series(dtype="int"),
                         "kg_available": pd.Series(dtype="int")})


def build_materials(usage_df, stock_df):
    """Turn the two material tables into dictionaries (whole kg)."""
    usage, stock = {}, {}
    for _, r in usage_df.dropna().iterrows():
        usage[(r["product"], r["material"])] = int(r["kg_per_unit"])
    for _, r in stock_df.dropna().iterrows():
        stock[(r["material"], int(r["day"]))] = int(r["kg_available"])
    return usage, stock


def load_data(source):
    """source can be a file path or an uploaded file."""
    xls = pd.ExcelFile(source)
    prod = xls.parse("products")
    mach = xls.parse("machines")
    allowed = xls.parse("allowed")
    usage_df = xls.parse("usage") if "usage" in xls.sheet_names else empty_usage()
    stock_df = xls.parse("stock") if "stock" in xls.sheet_names else empty_stock()

    products = {}
    for _, r in prod.iterrows():
        products[r["product"]] = {
            "demand": int(r["demand"]),
            "due_day": int(r["due_day"]),
            "priority": int(r["priority"]),
            "minutes_per_unit": int(r["minutes_per_unit"]),
            "machines": allowed[allowed["product"] == r["product"]]["machine"].tolist(),
        }

    capacity = {
        (r["machine"], int(r["day"])): int(r["minutes_available"])
        for _, r in mach.iterrows()
    }
    machines = sorted(mach["machine"].unique())
    days = sorted(int(d) for d in mach["day"].unique())
    usage, stock = build_materials(usage_df, stock_df)
    return products, machines, days, capacity, usage, stock


def make_plan(products, machines, days, capacity, usage=None, stock=None, settings=None):
    """Returns (plan_rows, summary_rows, material_rows, overtime_rows),
    or (None, None, None, None) if no plan is found."""
    s = dict(DEFAULT_SETTINGS)
    if settings:
        s.update(settings)

    usage = usage or {}
    stock = stock or {}
    model = cp_model.CpModel()

    units = {}
    for p, info in products.items():
        for m in info["machines"]:
            for d in days:
                units[p, m, d] = model.NewIntVar(0, info["demand"], f"{p}_{m}_day{d}")

    overtime = {}
    for m in machines:
        for d in days:
            overtime[m, d] = model.NewIntVar(0, int(s["max_overtime"]), f"overtime_{m}_day{d}")

    # RULE 1: never make more than the order
    for p, info in products.items():
        model.Add(
            sum(units[p, m, d] for m in info["machines"] for d in days) <= info["demand"]
        )

    # RULE 2: machine time used <= normal capacity + overtime
    for m in machines:
        for d in days:
            model.Add(
                sum(
                    units[p, m, d] * info["minutes_per_unit"]
                    for p, info in products.items()
                    if m in info["machines"]
                )
                <= capacity.get((m, d), 0) + overtime[m, d]
            )

    # RULE 3 (switchable): never use more material than is available that day
    limited_materials = sorted(set(mat for mat, _ in stock)) if s["use_materials"] else []
    for mat in limited_materials:
        for d in days:
            terms = [
                units[p, m, d] * usage[(p, mat)]
                for p, info in products.items() if (p, mat) in usage
                for m in info["machines"]
            ]
            if terms:
                model.Add(sum(terms) <= stock.get((mat, d), 0))

    # GOAL: scored from the settings
    goal_terms = []
    for (p, m, d), var in units.items():
        info = products[p]
        base = int(s["on_time_points"]) if d <= info["due_day"] else int(s["late_points"])
        weight = info["priority"] if s["use_priority"] else 1
        goal_terms.append((base * weight - int(s["earlier_bonus"]) * d) * var)
    for var in overtime.values():
        goal_terms.append(-int(s["overtime_cost"]) * var)
    model.Maximize(sum(goal_terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(s["time_limit_seconds"])
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None, None, None, None

    plan_rows = []
    for d in days:
        for (p, m, day), var in units.items():
            qty = solver.Value(var)
            if day == d and qty > 0:
                plan_rows.append({"day": d, "product": p, "machine": m, "qty": qty})

    summary_rows = []
    for p, info in products.items():
        total = sum(solver.Value(units[p, m, d]) for m in info["machines"] for d in days)
        on_time = sum(solver.Value(units[p, m, d])
                      for m in info["machines"] for d in days if d <= info["due_day"])
        summary_rows.append({
            "product": p,
            "ordered": info["demand"],
            "made": total,
            "on_time": on_time,
            "late": total - on_time,
            "due_day": info["due_day"],
        })

    material_rows = []
    for mat in limited_materials:
        for d in days:
            used = sum(
                solver.Value(units[p, m, d]) * usage[(p, mat)]
                for p, info in products.items() if (p, mat) in usage
                for m in info["machines"]
            )
            material_rows.append({
                "material": mat,
                "day": d,
                "kg_used": used,
                "kg_available": stock.get((mat, d), 0),
            })

    overtime_rows = []
    for (m, d), var in overtime.items():
        minutes = solver.Value(var)
        if minutes > 0:
            overtime_rows.append({"machine": m, "day": d, "overtime_minutes": minutes})

    return plan_rows, summary_rows, material_rows, overtime_rows


# This part only runs when you use: py -3.12 planner.py
if __name__ == "__main__":
    products, machines, days, capacity, usage, stock = load_data("data.xlsx")
    plan_rows, summary_rows, material_rows, overtime_rows = make_plan(
        products, machines, days, capacity, usage, stock)
    if plan_rows is None:
        print("No plan found")
    else:
        print(pd.DataFrame(plan_rows).to_string(index=False))
        print()
        print(pd.DataFrame(summary_rows).to_string(index=False))
        if material_rows:
            print()
            print(pd.DataFrame(material_rows).to_string(index=False))