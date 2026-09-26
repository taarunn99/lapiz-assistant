#!/usr/bin/env python3
"""
Daily progress report for the stock sync history scan.

Runs at the end of every GitHub Actions run (success or failure), reads what the backfill left in out/
and writes reports/Backfill_Progress_<date>.pdf + reports/history.json + reports/history.csv.
Counts only, no customer names, so the report is safe to keep in the repository.
Never fails the workflow: any problem is written into the PDF instead.
"""
import json, os, re, sys, datetime as dt, csv

OUT = os.environ.get("OUT_DIR", "out")
REP = "reports"
os.makedirs(REP, exist_ok=True)
RUN_OUTCOME = os.environ.get("RUN_OUTCOME", "unknown")      # success | failure | cancelled | skipped
RUN_URL = os.environ.get("RUN_URL", "")
MODE = os.environ.get("RUN_MODE", "dry")
CAP = os.environ.get("MAX_CALLS", "?")
TZ = dt.timezone(dt.timedelta(hours=4))
NOW = dt.datetime.now(TZ)
TODAY = NOW.strftime("%Y-%m-%d")

def load(path, default):
    try:
        return json.load(open(path))
    except Exception:
        return default

log_text = ""
try:
    log_text = open(f"{OUT}/run_log.txt", encoding="utf-8", errors="replace").read()
except Exception:
    pass

state = load(f"{OUT}/state.json", {})
plan = load(f"{OUT}/plan.json", [])
history = load(f"{REP}/history.json", [])

examined = len(state.get("examined", []))
skipped = len(state.get("skipped", []))
cn_skipped = len(state.get("cn_skipped", []))
cn_done = bool(state.get("cn_done"))
planned_sos = len(plan)
packages = sum(len(p.get("packages", [])) for p in plan)
lines = sum(len(l) for p in plan for l in [pk.get("lines", []) for pk in p.get("packages", [])])
returns = sum(len(p.get("returns", [])) for p in plan)

m_total = re.search(r"(\d+) sales orders to examine this run", log_text)
m_resume = re.search(r"resuming: (\d+) SOs already examined", log_text)
to_examine_this_run = int(m_total.group(1)) if m_total else None
already = int(m_resume.group(1)) if m_resume else 0
total_candidates = (to_examine_this_run + already) if to_examine_this_run is not None else None
if total_candidates is None and history:
    total_candidates = history[-1].get("total_candidates")

calls = 0
for pat in [r"after (\d+) calls", r"(\d+) calls used", r"calls (\d+)\b", r"MAX_CALLS (\d+) reached"]:
    for m in re.finditer(pat, log_text):
        calls = max(calls, int(m.group(1)))

partial = ""
m = re.search(r"(PARTIAL.*)", log_text)
if m: partial = m.group(1).strip()
complete = bool(re.search(r"^plan: ", log_text, re.M)) and not partial
error_tail = ""
if RUN_OUTCOME not in ("success",) or re.search(r"Traceback|Error|failed", log_text, re.I) and not complete and not partial:
    error_tail = "\n".join(log_text.strip().splitlines()[-25:])

prev_examined = history[-1]["examined_cumulative"] if history else 0
examined_today = max(examined - prev_examined, 0)
remaining = (total_candidates - examined) if total_candidates is not None else None

if RUN_OUTCOME != "success" and not partial and not complete:
    status, status_kind = f"FAILED (GitHub run outcome: {RUN_OUTCOME})", "bad"
elif complete and cn_done:
    status, status_kind = "COMPLETE: all sales orders examined and credit notes scanned. Plan is ready for review.", "good"
elif complete:
    status, status_kind = "Sales orders complete, credit note scan still to run.", "warn"
elif partial:
    status, status_kind = "PARTIAL (expected): stopped at today's call cap and saved progress.", "warn"
else:
    status, status_kind = "Ran, but no progress marker found in the log. Needs a look.", "bad"

# rate and forecast
rate_days = [h for h in history if h.get("examined_today", 0) > 0]
avg_per_day = (sum(h["examined_today"] for h in rate_days) + examined_today) / (len(rate_days) + (1 if examined_today > 0 else 0)) if (rate_days or examined_today) else 0
days_left = None
if remaining is not None and avg_per_day > 0:
    days_left = int(-(-remaining // avg_per_day))   # ceil

row = {"date": TODAY, "run_outcome": RUN_OUTCOME, "status": status, "mode": MODE, "cap": CAP, "calls_used": calls,
       "examined_today": examined_today, "examined_cumulative": examined, "total_candidates": total_candidates,
       "remaining": remaining, "planned_sos": planned_sos, "packages": packages, "returns": returns,
       "skipped_human": skipped, "cn_lines_skipped": cn_skipped, "cn_done": cn_done, "run_url": RUN_URL}
history = [h for h in history if h.get("date") != TODAY] + [row]
json.dump(history, open(f"{REP}/history.json", "w"), indent=1)
with open(f"{REP}/history.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(row.keys())); w.writeheader(); w.writerows(history)

# ---------- PDF ----------
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Preformatted

NAVY = colors.HexColor("#1F3864"); GREY = colors.HexColor("#555555"); LIGHT = colors.HexColor("#F3F5F9")
KIND = {"good": colors.HexColor("#2E7D32"), "warn": colors.HexColor("#B26A00"), "bad": colors.HexColor("#B71C1C")}
ss = getSampleStyleSheet()
H1 = ParagraphStyle("H1", parent=ss["Title"], fontSize=18, leading=22, textColor=NAVY, alignment=0, spaceAfter=2)
SUB = ParagraphStyle("SUB", parent=ss["Normal"], fontSize=9.5, textColor=GREY, spaceAfter=10)
H2 = ParagraphStyle("H2", parent=ss["Heading2"], fontSize=12.5, textColor=NAVY, spaceBefore=12, spaceAfter=5)
P = ParagraphStyle("P", parent=ss["Normal"], fontSize=10, leading=14, spaceAfter=5)
B = ParagraphStyle("B", parent=P, leftIndent=12, bulletIndent=2, spaceAfter=2)
ST = ParagraphStyle("ST", parent=P, fontSize=11, leading=15, textColor=KIND[status_kind], fontName="Helvetica-Bold")
CELL = ParagraphStyle("CELL", parent=P, fontSize=9, leading=11.5, spaceAfter=0)
MONO = ParagraphStyle("MONO", parent=ss["Code"], fontSize=7.5, leading=9.5)

def tbl(data, widths):
    t = Table(data, colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), NAVY), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                           ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 9),
                           ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT]),
                           ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#C9CED8")), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                           ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    return t

def footer(c, d):
    c.saveState(); c.setFont("Helvetica", 8); c.setFillColor(GREY)
    c.drawString(20*mm, 12*mm, f"Lapiz Blue | Zoho stock sync | Daily progress report {TODAY} | Internal, counts only")
    c.drawRightString(190*mm, 12*mm, f"Page {d.page}"); c.restoreState()

pdf_path = f"{REP}/Backfill_Progress_{TODAY}.pdf"
doc = SimpleDocTemplate(pdf_path, pagesize=A4, leftMargin=20*mm, rightMargin=20*mm, topMargin=18*mm, bottomMargin=20*mm,
                        title=f"Stock sync daily progress {TODAY}")
S = [Paragraph("Stock sync history scan: daily progress", H1),
     Paragraph(f"Generated {NOW.strftime('%A %d %B %Y, %H:%M')} Dubai time, automatically at the end of the scheduled run. "
               f"Mode: {MODE}. Call cap this run: {CAP}. Prepared for Tarun Shukla.", SUB),
     Paragraph("Status today", H2), Paragraph(status, ST)]
if partial: S.append(Paragraph(partial, P))

def fmt(v): return "n/a" if v is None else f"{v:,}"
pct = f"{(examined / total_candidates * 100):.1f}%" if total_candidates else "n/a"
S.append(Paragraph("Where the scan stands", H2))
S.append(tbl([["Measure", "Value"],
              ["Sales orders that need history repair (total found)", fmt(total_candidates)],
              ["Examined so far (all days together)", f"{fmt(examined)}  ({pct})"],
              ["Examined today", fmt(examined_today)],
              ["Still to examine", fmt(remaining)],
              ["Zoho API calls used today", fmt(calls)],
              ["Average sales orders per day so far", f"{avg_per_day:,.0f}"],
              ["Estimated days of scanning left", fmt(days_left) if days_left is not None else "n/a"],
              ["Credit note scan", "done" if cn_done else "not yet (runs after all sales orders are examined)"]],
             [105*mm, 65*mm]))

S.append(Paragraph("What the plan contains so far (nothing has been changed in Zoho)", H2))
S.append(tbl([["Item", "Count"],
              ["Sales orders with shipments to create", fmt(planned_sos)],
              ["Packages + shipments to create", fmt(packages)],
              ["Shipment line rows", fmt(lines)],
              ["Sales returns to create (item credit notes)", fmt(returns)],
              ["Sales orders set aside for a human to check", fmt(skipped)],
              ["Credit note lines set aside", fmt(cn_skipped)]], [105*mm, 65*mm]))

S.append(Paragraph("Day by day", H2))
hist_rows = [["Date", "Outcome", "Examined that day", "Cumulative", "Remaining", "Calls used"]]
for h in history[-14:]:
    hist_rows.append([h["date"], "ok" if h["run_outcome"] == "success" else h["run_outcome"], fmt(h.get("examined_today")),
                      fmt(h.get("examined_cumulative")), fmt(h.get("remaining")), fmt(h.get("calls_used"))])
S.append(tbl(hist_rows, [24*mm, 22*mm, 34*mm, 30*mm, 30*mm, 30*mm]))

S.append(Paragraph("What happens next", H2))
if status_kind == "bad":
    nxt = ["The run did not finish normally. The log tail is below. Claude reviews it at the 15:30 check and fixes the cause before the next scheduled run.",
           "No data is lost: everything examined before the failure is saved and the next run continues from there.",
           "Nothing in Zoho has been changed by this scan; it only reads."]
elif complete and cn_done:
    nxt = ["The dry scan is complete. Backfill_Plan.xlsx is attached to this run in GitHub Actions (artifact 'backfill-out').",
           "Next: Tarun and accounts review the 'Skipped, needs human' sheet (about 20 minutes), then management approves the live run.",
           "Claude prepares the management presentation from these final numbers.",
           "The daily scheduled run keeps going but does almost nothing now (about 40 calls a day); it will be switched off after go live."]
elif complete:
    nxt = ["All sales orders are examined. Tomorrow's run scans the credit notes with item lines and adds the returns to the plan.",
           "After that the plan is final and goes for review."]
else:
    nxt = [f"Tomorrow at 13:00 Dubai the run resumes automatically from sales order {examined + 1:,}, capped at {CAP} calls.",
           f"At the current pace the sales order scan finishes in about {fmt(days_left)} more day(s), then one more day for the credit note scan.",
           "Nothing is required from anyone in the meantime. Zoho keeps working normally; the scan only reads.",
           "Claude checks each run at 15:30 Dubai and sends this report."]
S += [Paragraph(x, B, bulletText="•") for x in nxt]

if error_tail:
    S.append(Paragraph("Log tail (for troubleshooting)", H2))
    S.append(Preformatted(error_tail[-3000:], MONO))
if RUN_URL:
    S.append(Spacer(1, 8)); S.append(Paragraph(f"GitHub run: {RUN_URL}", SUB))

try:
    doc.build(S, onFirstPage=footer, onLaterPages=footer)
    print("report written:", pdf_path)
except Exception as e:
    print("report build failed:", e)
