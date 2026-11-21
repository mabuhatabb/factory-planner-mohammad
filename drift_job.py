"""Scheduled drift alerts. Run this on a timer (cron, a scheduled container, GitHub Actions).

For every company account it takes each APPROVED plan, re-plans the company's newest saved data set with the same
settings, and emails an alert when orders changed. The same change is only alerted once.

Settings come from environment variables: DATABASE_URL, SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM,
ALERT_EMAIL (fallback recipient when the approver's email is unknown).
"""
import hashlib
import json
import os
import sys

import drift
import mailer
import store
import tables


def run(engine=None, send=mailer.send):
    eng = engine or store.get_engine()
    cfg = {k: os.environ.get(k) for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM")}
    fallback = os.environ.get("ALERT_EMAIL")
    report = []
    for owner in store.list_owner_ids(eng):
        datasets = store.list_items("datasets", owner, eng)
        if not datasets:
            continue
        data = tables.from_json(store.load_item("datasets", owner, datasets[0]["id"], eng))   # newest saved data set
        for it in store.list_items("plans", owner, eng):
            body = store.load_item("plans", owner, it["id"], eng) or {}
            dec = body.get("decision") or {}
            if dec.get("status") != "Approved":
                continue
            changes, err = drift.check(body, data)
            if err:
                report.append((it["name"], "error: " + err))
                continue
            if not changes:
                report.append((it["name"], "still true"))
                if body.get("last_alert_sig"):
                    body.pop("last_alert_sig")
                    store.update_item("plans", owner, it["id"], body, eng)
                continue
            sig = hashlib.sha256(json.dumps(changes, sort_keys=True, default=str).encode()).hexdigest()[:16]
            if body.get("last_alert_sig") == sig:
                report.append((it["name"], "changed, already alerted"))
                continue
            to = dec.get("by") if "@" in str(dec.get("by") or "") else fallback
            text_body = drift.alert_text(it["name"], changes)
            note = "no recipient"
            if to and mailer.smtp_ready(cfg):
                err = send(cfg, to, f"Plan changed: {it['name']}", text_body)
                note = f"emailed {to}" if not err else err
            elif to:
                note = "SMTP not set up, not emailed"
            body["last_alert_sig"] = sig
            store.update_item("plans", owner, it["id"], body, eng)
            store.log(owner, "drift job", "Drift detected", f"{it['name']}: {len(changes)} orders changed. {note}", eng)
            report.append((it["name"], f"{len(changes)} orders changed, {note}"))
    return report


if __name__ == "__main__":
    for name, result in run():
        print(f"{name}: {result}")
    sys.exit(0)
