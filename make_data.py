import pandas as pd

products = pd.DataFrame([
    {"product": "Product A", "demand": 40, "due_day": 1, "priority": 1, "minutes_per_unit": 10},
    {"product": "Product B", "demand": 30, "due_day": 2, "priority": 2, "minutes_per_unit": 15},
])

machines = pd.DataFrame([
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

with pd.ExcelWriter("data.xlsx") as writer:
    products.to_excel(writer, sheet_name="products", index=False)
    machines.to_excel(writer, sheet_name="machines", index=False)
    allowed.to_excel(writer, sheet_name="allowed", index=False)
    usage.to_excel(writer, sheet_name="usage", index=False)
    stock.to_excel(writer, sheet_name="stock", index=False)

print("data.xlsx created")