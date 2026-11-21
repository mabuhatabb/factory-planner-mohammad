"""Email helpers for sending a plan to management. No Streamlit code here."""
import io
import smtplib
import ssl
from email.message import EmailMessage
from urllib.parse import quote

import pandas as pd

import insights


def summary(body):
    o = pd.DataFrame(body.get("orders", []))
    ld = pd.DataFrame(body.get("load", []))
    n = len(o)
    count = lambda s: int((o["status"] == s).sum()) if n else 0
    return {"n": n, "on_time": count("On time"), "late": count("Late"), "not_scheduled": count("Not scheduled"),
            "late_days": int(o["days_late"].fillna(0).sum()) if n else 0,
            "overtime": int(ld["overtime"].sum()) if len(ld) else 0,
            "risk": o[o["status"].isin(["Late", "Not scheduled"])].to_dict("records") if n else []}


def summary_text(plan_name, body):
    s = summary(body)
    lines = [f"Production plan: {plan_name}", "",
             f"Orders delivered: {s['on_time'] + s['late']} of {s['n']} "
             f"({s['on_time']} on time, {s['late']} late, {s['late_days']} late days in total)",
             f"Not scheduled: {s['not_scheduled']}",
             f"Overtime hours: {s['overtime']}"]
    mm = None
    if insights.has_money(body.get("money"), body.get("orders", [])):
        mm = insights.money(body.get("orders", []), body.get("load", []), body.get("money"))
        lines.append(f"Money: overtime cost {insights.fmt(mm['overtime_cost'])}, late penalties {insights.fmt(mm['late_penalty'])}, "
                     f"revenue at risk {insights.fmt(mm['value_at_risk'])} of {insights.fmt(mm['value_all'])}")
    if s["risk"]:
        lines += ["", "Orders needing attention:"]
        for r in s["risk"]:
            if r["status"] == "Late":
                lines.append(f"  - {r['order_id']}: late by {int(r['days_late'])} day{'s' if int(r['days_late']) != 1 else ''} (finishes {r['finish']}). {r.get('why') or ''}")
            else:
                lines.append(f"  - {r['order_id']}: cannot be scheduled. {r.get('why') or ''}")
    if body.get("options"):
        lines += ["", "Options compared (on time / late / not scheduled / overtime h / extra cost):"]
        for o in body["options"]:
            lines.append(f"  - {o['option']}: {o['on time']} / {o['late']} / {o['not scheduled']} / {o['overtime hours']} / "
                         f"{insights.fmt(o.get('extra cost', 0))}")
    return "\n".join(lines)


def plan_excel_from_body(body):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for key in ("orders", "steps", "load"):
            pd.DataFrame(body.get(key, [])).to_excel(xw, sheet_name=key, index=False)
    return buf.getvalue()


def mailto(to, subject, text):
    return f"mailto:{quote(to)}?subject={quote(subject)}&body={quote(text)}"


def smtp_ready(cfg):
    return bool(cfg and cfg.get("SMTP_HOST") and cfg.get("SMTP_USER") and cfg.get("SMTP_PASSWORD"))


def send(cfg, to, subject, text, attachments=()):
    """Send through SMTP. Returns None on success, or an error text."""
    try:
        msg = EmailMessage()
        msg["From"] = cfg.get("SMTP_FROM") or cfg["SMTP_USER"]
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(text)
        subtypes = {"pdf": "pdf", "xlsx": "vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
        for data, filename, kind in attachments:
            msg.add_attachment(data, maintype="application", subtype=subtypes[kind], filename=filename)
        port = int(cfg.get("SMTP_PORT") or 587)
        if port == 465:
            with smtplib.SMTP_SSL(cfg["SMTP_HOST"], port, context=ssl.create_default_context(), timeout=20) as s:
                s.login(cfg["SMTP_USER"], cfg["SMTP_PASSWORD"])
                s.send_message(msg)
        else:
            with smtplib.SMTP(cfg["SMTP_HOST"], port, timeout=20) as s:
                s.starttls(context=ssl.create_default_context())
                s.login(cfg["SMTP_USER"], cfg["SMTP_PASSWORD"])
                s.send_message(msg)
        return None
    except Exception as e:
        return f"Could not send the email: {e}"
