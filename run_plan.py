import pandas as pd
from planner import load_data, make_plan

# Change these numbers, save the file, and run it. No web page needed.
SETTINGS = {
    "on_time_points": 100,
    "late_points": 10,
    "earlier_bonus": 1,
    "use_priority": True,
    "use_materials": True,
    "max_overtime": 100,
    "overtime_cost": 1,
    "time_limit_seconds": 10,
}

products, machines, days, capacity, usage, stock = load_data("data.xlsx")
plan_rows, summary_rows, material_rows, overtime_rows = make_plan(
    products, machines, days, capacity, usage, stock, settings=SETTINGS)

if plan_rows is None:
    print("No plan found")
else:
    print(pd.DataFrame(plan_rows).to_string(index=False))
    print()
    print(pd.DataFrame(summary_rows).to_string(index=False))
    if overtime_rows:
        print()
        print(pd.DataFrame(overtime_rows).to_string(index=False))
    pd.DataFrame(plan_rows).to_excel("plan.xlsx", index=False)
    print("\nSaved plan.xlsx")