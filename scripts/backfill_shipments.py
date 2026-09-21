#!/usr/bin/env python3
"""
Lapiz Blue stock sync backfill.

For every sales order that has been invoiced but not (fully) shipped, compute per line
    to_ship = quantity_invoiced - quantity_shipped
and create one package + one shipment per source invoice, dated to that invoice's date.

Modes
  dry   (default)  read only. Writes plan.json + Backfill_Plan.xlsx. Creates nothing.
  live             reads plan.json produced by a dry run, re-verifies each SO against Zoho
                   right before acting, then creates packages and shipments. Resumable.

Environment
  ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN, ZOHO_ORG_ID_DXB
Optional
  MAX_CALLS   hard cap on API calls per run (default 8000, leaves room for other apps)
  SO_LIMIT    only process the first N sales orders (for testing, e.g. 5)
  SO_NUMBERS  comma separated SO numbers to restrict to (for testing on ZZ TEST)
"""
import json, os, sys, time, datetime as dt
from collections import defaultdict
import requests

BASE_INV = "https://www.zohoapis.com/inventory/v1"
TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"
ORG = os.environ["ZOHO_ORG_ID_DXB"]
MAX_CALLS = int(os.environ.get("MAX_CALLS", "8000"))
SO_LIMIT = int(os.environ.get("SO_LIMIT", "0") or 0)
SO_NUMBERS = {s.strip() for s in os.environ.get("SO_NUMBERS", "").split(",") if s.strip()}
MODE = sys.argv[1] if len(sys.argv) > 1 else "dry"
OUT = os.environ.get("OUT_DIR", "out"); os.makedirs(OUT, exist_ok=True)
MARK = "LB-STOCKSYNC-BACKFILL"

# ---------- auth + throttled client ----------
class Zoho:
    def __init__(self):
        self.calls = 0; self.window = []; self.token = None; self.exp = 0
    def _auth(self):
        if time.time() < self.exp - 60: return
        r = requests.post(TOKEN_URL, data={
            "client_id": os.environ["ZOHO_CLIENT_ID"], "client_secret": os.environ["ZOHO_CLIENT_SECRET"],
            "grant_type": "refresh_token", "refresh_token": os.environ["ZOHO_REFRESH_TOKEN"]}, timeout=30)
        j = r.json()
        if "access_token" not in j: raise SystemExit(f"auth failed: {j}")
        self.token = j["access_token"]; self.exp = time.time() + int(j.get("expires_in", 3600))
    def req(self, method, path, **kw):
        if self.calls >= MAX_CALLS: raise SystemExit(f"MAX_CALLS {MAX_CALLS} reached, stopping cleanly")
        # 100/min limit: keep to 80
        now = time.time(); self.window = [t for t in self.window if now - t < 60]
        if len(self.window) >= 80: time.sleep(60 - (now - self.window[0]) + 0.5)
        self._auth()
        params = kw.pop("params", {}); params["organization_id"] = ORG
        for attempt in range(4):
            r = requests.request(method, BASE_INV + path, params=params,
                                 headers={"Authorization": f"Zoho-oauthtoken {self.token}"}, timeout=60, **kw)
            self.calls += 1; self.window.append(time.time())
            if r.status_code == 429:
                print("rate limited, sleeping 65s"); time.sleep(65); continue
            if r.status_code >= 500:
                time.sleep(5 * (attempt + 1)); continue
            try: j = r.json()
            except Exception: j = {"code": -1, "message": r.text[:200]}
            return j
        return {"code": -1, "message": "retries exhausted"}
    def get(self, path, **params): return self.req("GET", path, params=params)
    def post(self, path, body, **params): return self.req("POST", path, params=params, json=body)

Z = Zoho()

# ---------- discovery ----------
def list_to_be_packed():
    """All SOs with invoiced > shipped. Uses list rows only (1 call per 200 SOs)."""
    page, out = 1, []
    while True:
        j = Z.get("/salesorders", filter_by="Status.ToBePacked", per_page=200, page=page, sort_column="date")
        if j.get("code") != 0: raise SystemExit(f"list failed: {j}")
        for s in j.get("salesorders", []):
            if SO_NUMBERS and s["salesorder_number"] not in SO_NUMBERS: continue
            if float(s.get("quantity_invoiced", 0)) > float(s.get("quantity_shipped", 0)):
                out.append(s)
        if not j.get("page_context", {}).get("has_more_page"): break
        page += 1
        if SO_LIMIT and len(out) >= SO_LIMIT: break
    return out[:SO_LIMIT] if SO_LIMIT else out

# ---------- planning for one SO ----------
def plan_so(so_id):
    """Returns (plan, flags). plan = list of {invoice_number, invoice_date, lines:[{so_line_item_id,item,qty}]}"""
    j = Z.get(f"/salesorders/{so_id}")
    if j.get("code") != 0: return None, [f"fetch failed: {j.get('message')}"]
    so = j["salesorder"]; flags = []
    if so.get("is_dropshipped") or so.get("is_drop_shipment"): flags.append("DROP SHIP, skip")
    if so.get("status") in ("void", "draft"): flags.append(f"status {so['status']}, skip")
    lines = {l["line_item_id"]: l for l in so["line_items"] if l.get("item_type") == "inventory" or l.get("product_type") == "goods"}
    remaining = {lid: float(l["quantity_invoiced"]) - float(l["quantity_shipped"]) for lid, l in lines.items()}
    already_covered = {lid: float(l["quantity_shipped"]) for lid, l in lines.items()}
    for lid, l in lines.items():
        if l.get("batches") or l.get("serial_numbers"): flags.append(f"tracked item {l['name']}, needs human")
        if remaining[lid] < 0: flags.append(f"shipped > invoiced on {l['name']}, needs human")
    if any("skip" in f or "human" in f for f in flags): return None, flags
    invoices = sorted([i for i in so.get("invoices", []) if i.get("status") != "void"], key=lambda i: i["date"])
    plan = []
    for inv in invoices:
        ij = Z.get(f"/invoices/{inv['invoice_id']}")
        if ij.get("code") != 0: flags.append(f"invoice {inv['invoice_number']} fetch failed"); continue
        pkg_lines = []
        for il in ij["invoice"]["line_items"]:
            lid = il.get("salesorder_item_id")
            if not lid or lid not in lines: continue
            q = float(il["quantity"])
            skip = min(q, already_covered[lid]); already_covered[lid] -= skip
            take = min(q - skip, max(remaining[lid], 0))
            if take > 0:
                pkg_lines.append({"so_line_item_id": lid, "item": lines[lid]["name"], "quantity": take})
                remaining[lid] -= take
        if pkg_lines:
            plan.append({"invoice_id": inv["invoice_id"], "invoice_number": inv["invoice_number"],
                         "invoice_date": inv["date"], "lines": pkg_lines})
    left = {lines[l]["name"]: r for l, r in remaining.items() if r > 0.0001}
    if left: flags.append(f"unallocated after invoices: {left}")
    return {"salesorder_id": so_id, "salesorder_number": so["salesorder_number"],
            "customer": so["customer_name"], "location_id": so.get("location_id"),
            "packages": plan}, flags

# ---------- execution for one SO ----------
def execute_so(p):
    """Re-verify then create. Returns list of result strings."""
    fresh, flags = plan_so(p["salesorder_id"])
    if fresh is None or not fresh["packages"]:
        return [f"{p['salesorder_number']}: nothing to do now ({'; '.join(flags) or 'already in sync'})"]
    res = []
    for pk in fresh["packages"]:
        body = {"date": pk["invoice_date"],
                "notes": f"{MARK} invoice {pk['invoice_number']}",
                "line_items": [{"so_line_item_id": l["so_line_item_id"], "quantity": l["quantity"]} for l in pk["lines"]]}
        r = Z.post("/packages", body, salesorder_id=p["salesorder_id"])
        if r.get("code") != 0:
            res.append(f"{p['salesorder_number']} pkg for {pk['invoice_number']} FAILED: {r.get('message')}"); continue
        pid = r["package"]["package_id"]
        sbody = {"date": pk["invoice_date"], "delivery_method": "Al Quoz",
                 "notes": f"{MARK} invoice {pk['invoice_number']}"}
        s = Z.post("/shipmentorders", sbody, package_ids=pid, salesorder_id=p["salesorder_id"], send_notification="false")
        if s.get("code") != 0:
            res.append(f"{p['salesorder_number']} shipment for {pk['invoice_number']} FAILED after package {pid}: {s.get('message')}"); continue
        res.append(f"{p['salesorder_number']} OK {pk['invoice_number']} pkg {r['package'].get('package_number')} ship {s['shipmentorder'].get('shipment_number')}")
    return res

# ---------- main ----------
def main():
    if MODE == "dry":
        sos = list_to_be_packed()
        print(f"{len(sos)} sales orders invoiced but not fully shipped")
        plans, rows, skipped = [], [], []
        for i, s in enumerate(sos, 1):
            p, flags = plan_so(s["salesorder_id"])
            if p and p["packages"]:
                plans.append(p)
                for pk in p["packages"]:
                    for l in pk["lines"]:
                        rows.append([p["salesorder_number"], p["customer"], pk["invoice_number"], pk["invoice_date"],
                                     l["item"], l["quantity"], "; ".join(flags)])
            else:
                skipped.append([s["salesorder_number"], s["customer_name"], "; ".join(flags) or "no packable lines"])
            if i % 100 == 0: print(f"  {i}/{len(sos)} planned, {Z.calls} calls used")
        json.dump(plans, open(f"{OUT}/plan.json", "w"), indent=1)
        write_xlsx(rows, skipped, plans)
        print(f"plan: {len(plans)} SOs, {sum(len(p['packages']) for p in plans)} packages, {len(rows)} lines, {len(skipped)} skipped. calls {Z.calls}")
    elif MODE == "live":
        plans = json.load(open(f"{OUT}/plan.json"))
        done_path = f"{OUT}/done.json"
        done = set(json.load(open(done_path))) if os.path.exists(done_path) else set()
        log = open(f"{OUT}/live_log.txt", "a")
        for p in plans:
            if p["salesorder_id"] in done: continue
            try:
                for line in execute_so(p): print(line); log.write(line + "\n")
            except SystemExit as e:
                print(e); break
            done.add(p["salesorder_id"]); json.dump(sorted(done), open(done_path, "w"))
            log.flush()
        print(f"live run stopped. {len(done)}/{len(plans)} SOs processed. calls {Z.calls}")
    else:
        raise SystemExit("mode must be dry or live")

def write_xlsx(rows, skipped, plans):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    wb = Workbook(); ws = wb.active; ws.title = "Planned shipments"
    hdr = ["SO Number", "Customer", "Source Invoice", "Ship Date (=invoice date)", "Item", "Qty to ship", "Notes"]
    ws.append(hdr)
    for c in ws[1]: c.font = Font(bold=True, color="FFFFFF"); c.fill = PatternFill("solid", fgColor="1F3864")
    for r in rows: ws.append(r)
    ws.freeze_panes = "A2"; ws.auto_filter.ref = f"A1:G{max(len(rows)+1,2)}"
    for col, w in zip("ABCDEFG", [14, 40, 16, 20, 45, 12, 50]): ws.column_dimensions[col].width = w
    ws2 = wb.create_sheet("Skipped, needs human")
    ws2.append(["SO Number", "Customer", "Reason"])
    for c in ws2[1]: c.font = Font(bold=True, color="FFFFFF"); c.fill = PatternFill("solid", fgColor="1F3864")
    for r in skipped: ws2.append(r)
    for col, w in zip("ABC", [14, 40, 80]): ws2.column_dimensions[col].width = w
    ws3 = wb.create_sheet("Summary", 0)
    ws3.append(["Generated", dt.datetime.now().strftime("%d %b %Y %H:%M")])
    ws3.append(["Sales orders to fix", len(plans)])
    ws3.append(["Packages + shipments to create", sum(len(p["packages"]) for p in plans)])
    ws3.append(["Line rows", len(rows)])
    ws3.append(["Skipped for human review", len(skipped)])
    ws3.append(["API calls used by dry run", Z.calls])
    ws3.append(["Estimated live run calls", sum(len(p["packages"]) for p in plans) * 2 + len(plans) * 3])
    ws3.column_dimensions["A"].width = 34; ws3.column_dimensions["B"].width = 20
    wb.save(f"{OUT}/Backfill_Plan.xlsx")

if __name__ == "__main__":
    main()
