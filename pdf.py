"""One-page management summary as a PDF (reportlab). No Streamlit code here."""
import io
from datetime import date, datetime

import pandas as pd
from reportlab.graphics.shapes import Drawing, Line, Rect, String
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

import insights
import mailer

PALETTE = ["#3b82c4", "#2a9d8f", "#e9a23b", "#c1553d", "#7a5cb8", "#8d99a6", "#d46a9c"]


def _d(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except Exception:
        return None


def _gantt(steps, width, row_h=11):
    df = pd.DataFrame(steps)
    if df.empty:
        return None
    df["s"] = df["start"].map(_d)
    df["e"] = df["end"].map(_d)
    orders = list(dict.fromkeys(df["order_id"]))
    names = list(dict.fromkeys(df.sort_values("step_no")["step_name"]))
    color = {n: colors.HexColor(PALETTE[i % len(PALETTE)]) for i, n in enumerate(names)}
    t0, t1 = df["s"].min(), df["e"].max()
    span = max(1, (t1 - t0).days + 1)
    left, top, legend_h = 52, 14, 16
    h = top + row_h * len(orders) + legend_h + 12
    dr = Drawing(width, h)
    plot_w = width - left - 6
    for i, o in enumerate(orders):
        y = h - top - (i + 1) * row_h
        dr.add(String(0, y + 2, str(o)[:14], fontSize=6.5, fillColor=colors.HexColor("#333333")))
        for _, r in df[df["order_id"] == o].iterrows():
            x = left + plot_w * (r["s"] - t0).days / span
            w = max(1.5, plot_w * ((r["e"] - r["s"]).days + 1) / span)
            dr.add(Rect(x, y + 1, w, row_h - 3, fillColor=color[r["step_name"]], strokeColor=None))
    # month ticks
    d = date(t0.year, t0.month, 1)
    while d <= t1:
        if d >= t0:
            x = left + plot_w * (d - t0).days / span
            dr.add(Line(x, 10 + legend_h, x, h - top, strokeColor=colors.HexColor("#dddddd"), strokeWidth=0.4))
            dr.add(String(x + 1, h - top + 3, d.strftime("%b %Y"), fontSize=6, fillColor=colors.HexColor("#666666")))
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    x = 0
    for n in names:
        dr.add(Rect(x, 2, 7, 7, fillColor=color[n], strokeColor=None))
        dr.add(String(x + 10, 3, n, fontSize=6.5))
        x += 24 + 4.2 * len(n)
    return dr


def build_pdf(plan_name, body, author=""):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=14 * mm, rightMargin=14 * mm,
                            topMargin=10 * mm, bottomMargin=10 * mm, title=f"Plan: {plan_name}")
    ss = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=ss["Heading1"], fontSize=15, spaceAfter=2)
    h2 = ParagraphStyle("h2", parent=ss["Heading3"], fontSize=9.5, spaceBefore=5, spaceAfter=2)
    small = ParagraphStyle("small", parent=ss["BodyText"], fontSize=7.5, leading=9)
    cell = ParagraphStyle("cell", parent=small, fontSize=7, leading=8.4)
    sm = mailer.summary(body)
    dec = body.get("decision") or {"status": "Draft"}
    story = [Paragraph(f"Production plan: {plan_name}", h1),
             Paragraph(f"Created {datetime.now().strftime('%d %b %Y')}"
                       + (f" by {author}" if author else "") + f". Decision: <b>{dec.get('status', 'Draft')}</b>"
                       + (f" by {dec.get('by')} on {dec.get('at')}" if dec.get("by") else "")
                       + (f". Comment: {dec.get('comment')}" if dec.get("comment") else ""), small), Spacer(1, 4)]

    kpi = [["Orders delivered", "On time", "Late", "Not scheduled", "Overtime hours"],
           [f"{sm['on_time'] + sm['late']} of {sm['n']}", sm["on_time"], f"{sm['late']} ({sm['late_days']} days)",
            sm["not_scheduled"], sm["overtime"]]]
    t = Table(kpi, colWidths=[55 * mm] * 5)
    t.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, 0), 7.5), ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#666666")),
                           ("FONTSIZE", (0, 1), (-1, 1), 13), ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"),
                           ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f3f6fa")),
                           ("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor("#cfd8e3")),
                           ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    story.append(t)

    orders_rows = body.get("orders", [])
    M = body.get("money") or {}
    if insights.has_money(M, orders_rows):
        mon = insights.money(orders_rows, body.get("load", []), M)
        story.append(Paragraph(
            f"Money (your currency): overtime cost <b>{insights.fmt(mon['overtime_cost'])}</b>, late penalties "
            f"<b>{insights.fmt(mon['late_penalty'])}</b>, revenue at risk (late or not scheduled) "
            f"<b>{insights.fmt(mon['value_at_risk'])}</b> of {insights.fmt(mon['value_all'])} total order value.", small))

    story.append(Paragraph("Orders needing a decision", h2))
    if sm["risk"]:
        rows = [["Order", "Status", "Finish", "Days late", "Why"]]
        for r in sm["risk"]:
            rows.append([r["order_id"], r["status"], str(r.get("finish") or "-"),
                         "-" if r.get("days_late") in (None, "") or pd.isna(r.get("days_late")) else int(r["days_late"]),
                         Paragraph(str(r.get("why") or ""), cell)])
        t = Table(rows, colWidths=[22 * mm, 24 * mm, 24 * mm, 16 * mm, 161 * mm], repeatRows=1)
        t.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), 7.5), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e6ecf3")),
                               ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cfd8e3")), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
        story.append(t)
    else:
        story.append(Paragraph("Every order is delivered on time.", small))

    opts = body.get("options")
    if opts:
        story.append(Paragraph("Options compared", h2))
        rows = [["Option", "On time", "Late", "Not scheduled", "Late days", "Overtime h", "Extra cost", "Revenue at risk", "What changes"]]
        for o in opts:
            rows.append([o["option"], o["on time"], o["late"], o["not scheduled"], o["late days"], o["overtime hours"],
                         insights.fmt(o.get("extra cost", 0)), insights.fmt(o.get("revenue at risk", 0)),
                         Paragraph(str(o["change"]), cell)])
        t = Table(rows, colWidths=[40 * mm, 14 * mm, 12 * mm, 22 * mm, 15 * mm, 18 * mm, 20 * mm, 26 * mm, 80 * mm], repeatRows=1)
        t.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), 7.5), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e6ecf3")),
                               ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cfd8e3")), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
        story.append(t)

    g = _gantt(body.get("steps", []), 262 * mm / 1.0)
    if g is not None:
        story.append(Paragraph("Schedule (each bar is a step)", h2))
        story.append(g)
    steps = body.get("steps", [])
    if steps:
        from reportlab.platypus import PageBreak
        story += [PageBreak(), Paragraph(f"Step detail: {plan_name}", h1),
                  Paragraph("Every step of every order, with the crew or machine, the first and last working day and the hours.", small),
                  Spacer(1, 4)]
        rows = [["Order", "Step no.", "Step", "Crew / machine", "Start", "End", "Hours"]]
        for r in sorted(steps, key=lambda z: (str(z["order_id"]), z["step_no"])):
            rows.append([r["order_id"], r["step_no"], r["step_name"], r["resource"], str(_d(r["start"])), str(_d(r["end"])), r["hours"]])
        t = Table(rows, colWidths=[30 * mm, 18 * mm, 45 * mm, 55 * mm, 30 * mm, 30 * mm, 20 * mm], repeatRows=1)
        t.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), 7.5), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e6ecf3")),
                               ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cfd8e3")),
                               ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
                               ("TOPPADDING", (0, 0), (-1, -1), 1.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5)]))
        story.append(t)
    doc.build(story)
    return buf.getvalue()
