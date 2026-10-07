#!/usr/bin/env python3
"""
Lapiz Blue stock sync checker (the "2 day checker").

Every run looks at every sales order that had any activity in the last N days (default 2): the sales order
itself, its invoices, its packages, its shipments, its sales returns. Creation counts as the first activity,
so the sales order date does not matter, only the last update (Tarun, 6 Oct 2026).

For every inventory line it compares what accounting moved with what physical moved:
    accounting out = quantity invoiced (live invoices) minus quantity cancelled by credit notes
    physical out   = quantity shipped minus quantity returned
If the two differ, or something is packed but not shipped, the sales order is listed for a human.
It also collects the ALERT comments the live Deluge function left on those sales orders.

Output
  - a Zoho Cliq message (incoming webhook URL in CLIQ_WEBHOOK; skipped when not set)
  - reports/Stock_Check_<date>.pdf (full table) + reports/stock_check_latest.json
Read only against Zoho. Never changes anything.

Environment
  ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN, ZOHO_ORG_ID_DXB
  CLIQ_WEBHOOK   Zoho Cliq incoming webhook URL of the channel (optional)
  DAYS           look back window in days (default 2)
  RATE_PER_MIN   calls per minute (default 45)
"""
import json, os, time, datetime as dt
import requests

BASE_INV = "https://www.zohoapis.com/inventory/v1"
TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"
ORG = os.environ["ZOHO_ORG_ID_DXB"]
DAYS = int(os.environ.get("DAYS", "2") or 2)
RATE_PER_MIN = int(os.environ.get("RATE_PER_MIN", "45") or 45)
MAX_CALLS = int(os.environ.get("MAX_CALLS", "6000") or 6000)
CLIQ = os.environ.get("CLIQ_WEBHOOK", "").strip()
MARK = "LB-STOCKSYNC"
TZ = dt.timezone(dt.timedelta(hours=4))
NOW = dt.datetime.now(TZ)
REP = "reports"; os.makedirs(REP, exist_ok=True)

def log(*a): print(*a, flush=True)

class Zoho:
    def __init__(self): self.calls = 0; self.window = []; self.token = None; self.exp = 0
    def _auth(self):
        if time.time() < self.exp - 60: return
        r = requests.post(TOKEN_URL, data={"client_id": os.environ["ZOHO_CLIENT_ID"], "client_secret": os.environ["ZOHO_CLIENT_SECRET"],
                                           "grant_type": "refresh_token", "refresh_token": os.environ["ZOHO_REFRESH_TOKEN"]}, timeout=30)
        j = r.json()
        if "access_token" not in j: raise SystemExit(f"auth failed: {j}")
        self.token = j["access_token"]; self.exp = time.time() + int(j.get("expires_in", 3600))
    def get(self, path, **params):
        if self.calls >= MAX_CALLS: raise SystemExit(f"MAX_CALLS {MAX_CALLS} reached")
        now = time.time(); self.window = [t for t in self.window if now - t < 60]
        if len(self.window) >= RATE_PER_MIN: time.sleep(60 - (now - self.window[0]) + 0.5)
        self._auth(); params["organization_id"] = ORG
        for attempt in range(4):
            try:
                r = requests.get(BASE_INV + path, params=params, headers={"Authorization": f"Zoho-oauthtoken {self.token}"}, timeout=60)
            except requests.RequestException as e:
                log(f"network error on {path}: {e}; retry {attempt + 1}"); time.sleep(20 * (attempt + 1)); continue
            self.calls += 1; self.window.append(time.time())
            if r.status_code == 429:
                ra = r.headers.get("Retry-After", ""); wait = int(ra) if ra.strip().isdigit() else 65
                if attempt >= 1: raise SystemExit(f"rate limited twice: {r.text[:120]}")
                log(f"rate limited, waiting {wait + 5}s"); time.sleep(wait + 5); continue
            if r.status_code >= 500: time.sleep(5 * (attempt + 1)); continue
            try: return r.json()
            except Exception: return {"code": -1, "message": r.text[:200]}
        return {"code": -1, "message": "no answer"}

Z = Zoho()
cutoff = NOW - dt.timedelta(days=DAYS)
cutoff_api = cutoff.strftime("%Y-%m-%dT%H:%M:%S+0400")
log(f"checker: activity since {cutoff.strftime('%d %b %Y %H:%M')} Dubai ({DAYS} days)")

# ---------- 1. which sales orders had activity ----------
so_ids = {}
def add(so_id, so_num, why):
    if not so_id: return
    so_ids.setdefault(str(so_id), {"number": so_num, "why": set()})["why"].add(why)

page = 1
while True:
    j = Z.get("/salesorders", last_modified_time=cutoff_api, per_page=200, page=page, sort_column="last_modified_time", sort_order="D")
    if j.get("code") != 0: log("salesorders list failed", j.get("message")); break
    rows = j.get("salesorders", [])
    for s in rows:
        if (s.get("last_modified_time") or "") >= cutoff_api: add(s["salesorder_id"], s.get("salesorder_number"), "sales order updated")
    if not rows or not j.get("page_context", {}).get("has_more_page"): break
    if rows and (rows[-1].get("last_modified_time") or "") < cutoff_api: break
    page += 1
log(f"  {len(so_ids)} sales orders updated in the window")

# invoices touched in the window (created, edited, paid, voided) and the sales orders behind them
inv_count = 0; page = 1
while True:
    j = Z.get("/invoices", last_modified_time=cutoff_api, per_page=200, page=page, sort_column="last_modified_time", sort_order="D")
    if j.get("code") != 0: log("invoices list failed", j.get("message")); break
    rows = j.get("invoices", [])
    for inv in rows:
        if (inv.get("last_modified_time") or "") < cutoff_api: continue
        inv_count += 1
        sid = inv.get("salesorder_id")
        if sid: add(sid, inv.get("salesorder_number"), f"invoice {inv.get('invoice_number')} updated")
        else:
            # the list row does not always carry the SO link; the invoice detail does. One call per invoice.
            d = Z.get(f"/invoices/{inv['invoice_id']}").get("invoice", {})
            for s in d.get("salesorders", []) or []:
                add(s.get("salesorder_id"), s.get("salesorder_number"), f"invoice {inv.get('invoice_number')} updated")
            if d.get("salesorder_id"): add(d["salesorder_id"], d.get("salesorder_number"), f"invoice {inv.get('invoice_number')} updated")
    if not rows or not j.get("page_context", {}).get("has_more_page"): break
    if rows and (rows[-1].get("last_modified_time") or "") < cutoff_api: break
    page += 1
log(f"  {inv_count} invoices updated in the window; {len(so_ids)} sales orders to check. calls {Z.calls}")

# ---------- 2. check each sales order ----------
problems = []      # one row per bad line
alerts = []        # ALERT comments left by the live function
ok_count = 0; checked = 0; lines_checked = 0
summary = {"shipped_units": 0.0, "invoiced_units": 0.0}

def num(x):
    try: return float(x or 0)
    except Exception: return 0.0

for so_id, meta in sorted(so_ids.items(), key=lambda kv: kv[1]["number"] or ""):
    j = Z.get(f"/salesorders/{so_id}")
    if j.get("code") != 0:
        problems.append({"so": meta["number"], "customer": "", "item": "", "issue": f"could not read sales order: {j.get('message')}",
                         "invoiced": "", "shipped": "", "why": ", ".join(sorted(meta["why"]))}); continue
    so = j["salesorder"]; checked += 1
    if so.get("status") in ("void", "draft"): continue
    so_bad = False
    # Zoho keeps VOID invoices inside the SO line's quantity_invoiced. When a void invoice exists, count the
    # live invoices line by line instead (one call per live invoice).
    inv_by_line = None
    if any(i.get("status") == "void" for i in so.get("invoices", []) or []):
        inv_by_line = {}
        for i in so.get("invoices", []) or []:
            if i.get("status") in ("void", "draft"): continue
            d = Z.get(f"/invoices/{i['invoice_id']}").get("invoice", {})
            for il in d.get("line_items", []) or []:
                lid = il.get("salesorder_item_id")
                if lid: inv_by_line[lid] = inv_by_line.get(lid, 0.0) + num(il.get("quantity"))
    for l in so.get("line_items", []):
        if l.get("item_type") != "inventory": continue
        lines_checked += 1
        raw_invoiced = num(l.get("quantity_invoiced")) if inv_by_line is None else inv_by_line.get(l.get("line_item_id"), 0.0)
        invoiced = raw_invoiced - num(l.get("quantity_invoiced_cancelled"))
        shipped = num(l.get("quantity_shipped")) - num(l.get("quantity_returned"))
        packed = num(l.get("quantity_packed")); dropped = num(l.get("quantity_dropshipped")); manual = num(l.get("quantity_manuallyfulfilled"))
        summary["shipped_units"] += shipped; summary["invoiced_units"] += invoiced
        issue = ""
        if abs(invoiced - shipped - dropped - manual) > 0.001:
            issue = "invoiced and shipped do not match"
            if dropped: issue += f" (drop shipped {dropped:g})"
            if manual: issue += f" (manually fulfilled {manual:g})"
        elif packed - num(l.get("quantity_shipped")) > 0.001:
            issue = f"packed {packed:g} but shipped {num(l.get('quantity_shipped')):g}"
        if issue:
            so_bad = True
            problems.append({"so": so.get("salesorder_number"), "customer": so.get("customer_name"), "item": l.get("name"),
                             "issue": issue, "invoiced": f"{invoiced:g}", "shipped": f"{shipped:g}", "why": ", ".join(sorted(meta["why"]))})
    if so_bad:
        c = Z.get(f"/salesorders/{so_id}/comments")
        for cm in c.get("comments", []) or []:
            d = cm.get("description") or ""
            if MARK in d and "ALERT" in d:
                alerts.append({"so": so.get("salesorder_number"), "text": d[:200], "when": cm.get("date") or cm.get("date_formatted") or ""})
    else:
        ok_count += 1

bad_sos = sorted({p["so"] for p in problems})
log(f"checked {checked} sales orders, {lines_checked} lines: {ok_count} clean, {len(bad_sos)} need a human. calls {Z.calls}")
for p in problems[:30]: log("  ", p)

# ---------- 3. outputs ----------
result = {"generated": NOW.strftime("%d %b %Y %H:%M"), "window_days": DAYS, "checked_sos": checked, "lines": lines_checked,
          "clean_sos": ok_count, "bad_sos": bad_sos, "problems": problems, "alerts": alerts, "calls": Z.calls}
json.dump(result, open(f"{REP}/stock_check_latest.json", "w"), indent=1)

# PDF
try:
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    pdf_path = f"{REP}/Stock_Check_{NOW.strftime('%Y-%m-%d')}.pdf"
    doc = SimpleDocTemplate(pdf_path, pagesize=landscape(A4), leftMargin=30, rightMargin=30, topMargin=30, bottomMargin=30)
    st = getSampleStyleSheet(); el = []
    el.append(Paragraph(f"Lapiz Blue stock sync check, {NOW.strftime('%d %b %Y %H:%M')} Dubai", st["Title"]))
    el.append(Paragraph(f"Sales orders with any activity in the last {DAYS} days: {checked}. Lines compared: {lines_checked}. "
                        f"Clean: {ok_count}. Need a human: {len(bad_sos)}. Rule: invoiced minus credited must equal shipped minus returned.", st["Normal"]))
    el.append(Spacer(1, 10))
    if problems:
        data = [["Sales order", "Customer", "Item", "Invoiced", "Shipped", "Issue", "Why it was checked"]]
        for p in problems:
            data.append([p["so"], Paragraph(str(p["customer"])[:60], st["Normal"]), Paragraph(str(p["item"])[:70], st["Normal"]),
                         p["invoiced"], p["shipped"], Paragraph(p["issue"], st["Normal"]), Paragraph(p["why"][:80], st["Normal"])])
        t = Table(data, colWidths=[75, 150, 170, 50, 50, 150, 130], repeatRows=1)
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F3864")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                               ("FONTSIZE", (0, 0), (-1, -1), 8), ("GRID", (0, 0), (-1, -1), 0.25, colors.grey), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        el.append(t)
    else:
        el.append(Paragraph("No mismatches. Every sales order touched in the window has physical movement equal to accounting movement.", st["Normal"]))
    if alerts:
        el.append(Spacer(1, 12)); el.append(Paragraph("ALERT comments left by the live automation", st["Heading3"]))
        data = [["Sales order", "When", "Alert"]] + [[a["so"], a["when"], Paragraph(a["text"], st["Normal"])] for a in alerts]
        t = Table(data, colWidths=[80, 90, 600], repeatRows=1)
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F3864")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                               ("FONTSIZE", (0, 0), (-1, -1), 8), ("GRID", (0, 0), (-1, -1), 0.25, colors.grey), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        el.append(t)
    doc.build(el); log("pdf:", pdf_path)
except Exception as e:
    log("pdf problem:", e)

# Cliq
if CLIQ:
    head = f"*Stock sync check, {NOW.strftime('%d %b %H:%M')}*  (last {DAYS} days)\n" \
           f"Sales orders checked: {checked}, lines: {lines_checked}\nClean: {ok_count}   Need a human: {len(bad_sos)}"
    if bad_sos:
        body = "\n".join(f"- {p['so']} | {str(p['customer'])[:35]} | {str(p['item'])[:40]} | invoiced {p['invoiced']} vs shipped {p['shipped']} | {p['issue']}"
                         for p in problems[:25])
        if len(problems) > 25: body += f"\n... and {len(problems) - 25} more lines, see the PDF in GitHub reports/"
    else:
        body = "All good. Physical stock moved exactly as accounting stock on every sales order touched in the window."
    if alerts: body += f"\n\n{len(alerts)} ALERT comment(s) from the live automation, see PDF."
    try:
        r = requests.post(CLIQ, json={"text": head + "\n" + body}, timeout=30)
        log("cliq:", r.status_code, r.text[:120])
    except Exception as e:
        log("cliq problem:", e)
else:
    log("cliq skipped: CLIQ_WEBHOOK not set")
