"""Check an approved plan against current data. No Streamlit code here."""
from datetime import date

import insights
import scheduler
import tables


def check(body, data):
    """Re-plan `data` with the saved plan's settings. Returns (changes, error)."""
    cfg = body.get("settings") or {}
    if not cfg.get("start"):
        return None, "This plan has no saved settings."
    start = date.fromisoformat(str(cfg["start"])[:10])
    S = {k: cfg[k] for k in scheduler.DEFAULT_SETTINGS if k in cfg}
    S["time_limit_seconds"] = min(int(S.get("time_limit_seconds", 20)), 20)
    d = tables.normalize(data)
    o = d["orders"]
    kept = body.get("statuses")
    if kept:
        blank_ok = o["status"].isna() | (o["status"].astype(str).str.strip() == "")
        d["orders"] = o[blank_ok | o["status"].astype(str).str.strip().isin(kept)]
    jobs, cap, days, problems, skipped = tables.prepare(d, start, S)
    if not jobs:
        return None, "There are no valid orders in the current data."
    new = scheduler.solve(jobs, cap, days, S)
    if new["status"] == "none":
        return None, "No plan could be built from the current data."
    return insights.drift(body.get("orders", []), new["orders"]), None


def alert_text(plan_name, changes):
    lines = [f"The plan '{plan_name}' is no longer accurate.", ""]
    lines += [f"  - {c['order_id']}: {c['change']}. Was {c['was']}, now {c['now']}" for c in changes]
    lines += ["", "Please review the plan and make a new decision."]
    return "\n".join(lines)
