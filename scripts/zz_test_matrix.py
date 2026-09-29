#!/usr/bin/env python3
"""
Phase 1 test matrix: what Zoho itself allows and refuses, on the ZZ TEST sandbox only.
Runs against a SECOND sandbox customer ("ZZ TEST CUSTOMER 2 (raw zoho tests)") so the live
Deluge rules (which are restricted to customer LBCUS-9203) do NOT fire. Everything here is
done by hand through the API, so each result is Zoho's own behaviour, not our automation's.

Output: out/matrix_results.md and out/matrix_results.json. Every record created carries
reference "ZZMATRIX <case>" so it can be found and voided afterwards.
Env: ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN, ZOHO_ORG_ID_DXB
Optional: CASES=T01,T04 to run a subset. CLEANUP=1 voids everything with ZZMATRIX in the reference.
"""
import json, os, sys, time, datetime as dt, traceback
import requests

BASE = "https://www.zohoapis.com/inventory/v1"
TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"
ORG = os.environ["ZOHO_ORG_ID_DXB"]
OUT = os.environ.get("OUT_DIR", "out"); os.makedirs(OUT, exist_ok=True)
RATE = 40
TODAY = dt.date.today().isoformat()
ITEM_A = "2305879000148186068"   # ZZ TEST ITEM A, returnable
ITEM_B = "2305879000148203675"   # ZZ TEST ITEM B, returnable
ITEM_C = "2305879000148203724"   # ZZ TEST ITEM C, NOT returnable
SALESPERSON = "2305879000013572379"  # Aswin Asokan (no commission)
WH_ALQUOZ = "2305879000001030007"
WH_SHARJAH = "2305879000090061123"
CUST2_NAME = "ZZ TEST CUSTOMER 2 (raw zoho tests, do not invoice)"
ONLY = {c.strip() for c in os.environ.get("CASES", "").split(",") if c.strip()}

def log(*a): print(*a, flush=True)

class Zoho:
    def __init__(self): self.calls = 0; self.win = []; self.tok = None; self.exp = 0
    def _auth(self):
        if time.time() < self.exp - 60: return
        r = requests.post(TOKEN_URL, data={"client_id": os.environ["ZOHO_CLIENT_ID"], "client_secret": os.environ["ZOHO_CLIENT_SECRET"],
                                           "grant_type": "refresh_token", "refresh_token": os.environ["ZOHO_REFRESH_TOKEN"]}, timeout=30).json()
        if "access_token" not in r: raise SystemExit(f"auth failed: {r}")
        self.tok = r["access_token"]; self.exp = time.time() + int(r.get("expires_in", 3600))
    def req(self, m, path, params=None, body=None):
        now = time.time(); self.win = [t for t in self.win if now - t < 60]
        if len(self.win) >= RATE: time.sleep(60 - (now - self.win[0]) + 0.5)
        self._auth(); p = dict(params or {}); p["organization_id"] = ORG
        for attempt in range(3):
            r = requests.request(m, BASE + path, params=p, json=body, headers={"Authorization": f"Zoho-oauthtoken {self.tok}"}, timeout=60)
            self.calls += 1; self.win.append(time.time())
            if r.status_code == 429:
                ra = r.headers.get("Retry-After", ""); wait = int(ra) if ra.strip().isdigit() else 65
                if wait > 3700 or attempt: raise SystemExit(f"rate limited: {r.text[:120]}")
                log(f"429, waiting {wait+5}s"); time.sleep(wait + 5); continue
            if r.status_code >= 500: time.sleep(5); continue
            try: return r.json()
            except Exception: return {"code": -1, "message": r.text[:200]}
        return {"code": -1, "message": "no response"}
    def get(self, path, **params): return self.req("GET", path, params)
    def post(self, path, body=None, **params): return self.req("POST", path, params, body or {})
    def put(self, path, body, **params): return self.req("PUT", path, params, body)
    def delete(self, path, **params): return self.req("DELETE", path, params)

Z = Zoho()
RESULTS = []
def rec(case, step, expect, resp, note=""):
    ok = resp.get("code") == 0
    msg = resp.get("message", "")
    RESULTS.append({"case": case, "step": step, "expected": expect, "zoho": "OK" if ok else f"REFUSED: {msg}", "note": note})
    log(f"[{case}] {step} -> {'OK' if ok else 'REFUSED: ' + str(msg)}  {note}")
    return ok

# ---------- helpers ----------
def customer2():
    j = Z.get("/contacts", contact_name_startswith="ZZ TEST CUSTOMER 2")
    for c in j.get("contacts", []):
        if c["contact_name"].startswith("ZZ TEST CUSTOMER 2"): return c["contact_id"]
    j = Z.post("/contacts", {"contact_name": CUST2_NAME, "company_name": "ZZ TEST CUSTOMER 2", "contact_type": "customer",
                             "notes": "Sandbox customer for raw Zoho behaviour tests. Never invoice for real."})
    if j.get("code") != 0: raise SystemExit(f"cannot create customer 2: {j}")
    return j["contact"]["contact_id"]

CUST = None
BR_HEAD = "2305879000008690419"     # Head Office - Dubai Al Quoz (branch)
BR_SHARJAH = "2305879000090771523"  # Sharjah - Lapiz Blue (branch)
WH_BULK = "2305879000085186098"     # Bulk Stock Warehouse
def mk_so(case, lines, location=None, branch=None):
    li = [{"item_id": it, "quantity": q, "rate": 10} | ({"location_id": location} if location else {}) for it, q in lines]
    body = {"customer_id": CUST, "date": TODAY, "salesperson_id": SALESPERSON, "reference_number": f"ZZMATRIX {case}", "line_items": li}
    if branch: body["location_id"] = branch
    j = Z.post("/salesorders", body)
    if j.get("code") != 0: raise RuntimeError(f"SO create failed: {j.get('message')}")
    so = j["salesorder"]; sid = so["salesorder_id"]
    # this org has sales order approval switched on: submit, approve, then confirm (each step may say "already")
    for step in ("submit", "approve", "status/confirmed"):
        c = Z.post(f"/salesorders/{sid}/{step}")
        if c.get("code") != 0: log(f"{step}:", c.get("message"))
    so = Z.get(f"/salesorders/{sid}")["salesorder"]
    if so.get("status") not in ("confirmed", "open"): raise RuntimeError(f"SO {so.get('salesorder_number')} not confirmed: status {so.get('status')}")
    return so

def so_lines(so_id):
    so = Z.get(f"/salesorders/{so_id}")["salesorder"]
    return so, {l["item_id"]: l for l in so["line_items"]}

def qty_str(l):
    return f"ord {l['quantity']:g} inv {l.get('quantity_invoiced',0):g} pk {l.get('quantity_packed',0):g} sh {l.get('quantity_shipped',0):g} ret {l.get('quantity_returned',0):g}"

class Stop(Exception): pass
def need(resp, what):
    if resp.get("code") != 0: raise Stop(f"{what}: {resp.get('message')}")
    return resp
def mk_inv(case, so, lines, date=TODAY):
    """lines: list of (item_id, qty). Links to SO lines via salesorder_item_id."""
    byitem = {l["item_id"]: l for l in so["line_items"]}
    li = [{"item_id": it, "quantity": q, "rate": 10, "salesorder_item_id": byitem[it]["line_item_id"]} for it, q in lines]
    body = {"customer_id": CUST, "date": date, "reference_number": f"ZZMATRIX {case}", "salesperson_id": SALESPERSON, "line_items": li}
    j = Z.post("/invoices", body, salesorder_id=so["salesorder_id"])
    if j.get("code") != 0: return j
    inv = j["invoice"]
    for step in ("submit", "approve", "status/sent"):   # invoice approval is switched on in this org
        r = Z.post(f"/invoices/{inv['invoice_id']}/{step}")
        if r.get("code") != 0: log(f"invoice {step}:", r.get("message"))
    j["status_after"] = Z.get(f"/invoices/{inv['invoice_id']}").get("invoice", {}).get("status")
    return j

def mk_pkg(so, lines, date=TODAY, note="ZZMATRIX"):
    byitem = {l["item_id"]: l for l in so["line_items"]}
    body = {"date": date, "notes": note, "line_items": [{"so_line_item_id": byitem[it]["line_item_id"], "quantity": q} for it, q in lines]}
    return Z.post("/packages", body, salesorder_id=so["salesorder_id"])

def mk_ship(so, pkg_id, date=TODAY, note="ZZMATRIX"):
    return Z.post("/shipmentorders", {"date": date, "delivery_method": "Al Quoz", "notes": note},
                  package_ids=pkg_id, salesorder_id=so["salesorder_id"], send_notification="false")

def ship_full(case, so, lines, date=TODAY):
    p = mk_pkg(so, lines, date)
    if not rec(case, f"create package {lines}", "OK", p): raise Stop("package refused")
    pid = p["package"]["package_id"]
    s = mk_ship(so, pid, date)
    rec(case, "create shipment", "OK", s)
    return pid, (s.get("shipmentorder") or {}).get("shipmentorder_id") or (s.get("shipmentorder") or {}).get("shipment_id")

def deliver(case, ship_id):
    r = Z.post(f"/shipmentorders/{ship_id}/status/delivered")
    rec(case, "mark shipment delivered", "OK", r); return r.get("code") == 0

def undeliver(case, ship_id):
    tried = []
    for path in [f"/shipmentorders/{ship_id}/status/notdelivered", f"/shipmentorders/{ship_id}/status/shipped", f"/shipmentorders/{ship_id}/status/undelivered"]:
        r = Z.post(path); tried.append((path.split("/")[-1], r.get("message")))
        if r.get("code") == 0:
            rec(case, f"un-deliver via status/{path.split('/')[-1]}", "OK", r); return True
    rec(case, "un-deliver shipment", "one of the status endpoints works", {"code": -1, "message": "; ".join(f"{a}: {b}" for a, b in tried)})
    return False

def mk_return(case, so, lines, note="ZZMATRIX", date=TODAY):
    byitem = {l["item_id"]: l for l in so["line_items"]}
    loc_by_so_line = {l["line_item_id"]: l.get("location_id") for l in so["line_items"]}
    li = []
    for it, q in lines:
        d = {"salesorder_item_id": byitem[it]["line_item_id"], "item_id": it, "quantity": q}
        loc = loc_by_so_line.get(byitem[it]["line_item_id"])
        if loc: d["location_id"] = loc      # ask for the SO line's warehouse on the return itself (warehouse_id is rejected: "Invalid Element")
        li.append(d)
    body = {"salesorder_id": so["salesorder_id"], "date": date, "reason": "ZZMATRIX test", "notes": note, "line_items": li}
    hdr_loc = so.get("location_id")
    if hdr_loc: body["location_id"] = hdr_loc
    r = Z.post("/salesreturns", body, salesorder_id=so["salesorder_id"])
    if r.get("code") != 0: return r
    sr = r["salesreturn"]
    if not getattr(mk_return, "dumped", False):
        mk_return.dumped = True
        log("RETURN KEYS", sorted(sr.keys())); log("RETURN LINE KEYS", sorted((sr.get("line_items") or [{}])[0].keys()))
    rl = []
    for x in sr.get("line_items", []):
        d = {"line_item_id": x["line_item_id"], "quantity": x["quantity"]}
        loc = loc_by_so_line.get(x.get("salesorder_item_id") or x.get("so_line_item_id"))
        if loc: d["location_id"] = loc
        rl.append(d)
    rcv = {"salesreturn_id": sr["salesreturn_id"], "date": date, "notes": note, "line_items": rl}
    rr = Z.post("/salesreturnreceives", rcv, salesreturn_id=sr["salesreturn_id"])
    r["receive"] = rr
    try:
        rec_full = Z.get(f"/salesreturnreceives/{rr['salesreturnreceive']['receive_id']}").get("salesreturnreceive", {})
        if not getattr(mk_return, "dumped2", False):
            mk_return.dumped2 = True
            log("RECEIVE KEYS", sorted(rec_full.keys())); log("RECEIVE LINE KEYS", sorted((rec_full.get("line_items") or [{}])[0].keys()))
        sr_full = Z.get(f"/salesreturns/{sr['salesreturn_id']}").get("salesreturn", {})
        r["receive_locs"] = sorted({(l.get("location_name") or l.get("warehouse_name") or rec_full.get("location_name") or "?") for l in rec_full.get("line_items", [])}) + \
                            ["return:" + (l.get("location_name") or "?") for l in sr_full.get("line_items", [])]
    except Exception as e: r["receive_locs"] = f"n/a {e}"
    return r

def void_inv(inv_id): return Z.post(f"/invoices/{inv_id}/status/void")

# ---------- cases ----------
def T01():
    """Delivered shipment: can it be deleted? Then un-deliver and delete."""
    so = mk_so("T01", [(ITEM_A, 10)])
    inv = mk_inv("T01", so, [(ITEM_A, 10)]); rec("T01", "invoice 10 (sent)", "OK", inv)
    pid, sid = ship_full("T01", so, [(ITEM_A, 10)])
    if not sid: return
    deliver("T01", sid)
    d = Z.delete(f"/shipmentorders/{sid}")
    blocked = not rec("T01", "delete DELIVERED shipment", "unknown, this is the question", d)
    if blocked:
        if undeliver("T01", sid):
            d = Z.delete(f"/shipmentorders/{sid}"); rec("T01", "delete shipment after un-deliver", "OK", d)
    dp = Z.delete(f"/packages/{pid}"); rec("T01", "delete package", "OK", dp)
    so2, L = so_lines(so["salesorder_id"]); rec("T01", "line after cleanup", "pk 0 sh 0", {"code": 0}, qty_str(L[ITEM_A]))

def T02():
    """Rebuild at 8 after delivered 10, then invoice 2 more and ship 2."""
    so = mk_so("T02", [(ITEM_A, 10)])
    inv = mk_inv("T02", so, [(ITEM_A, 10)]); rec("T02", "invoice 10", "OK", inv)
    pid, sid = ship_full("T02", so, [(ITEM_A, 10)]); deliver("T02", sid)
    # edit invoice to 8
    iv = Z.get(f"/invoices/{need(inv,'invoice')['invoice']['invoice_id']}")["invoice"]
    li = [{"line_item_id": iv["line_items"][0]["line_item_id"], "item_id": ITEM_A, "quantity": 8, "rate": 10, "salesorder_item_id": so["line_items"][0]["line_item_id"]}]
    e = Z.put(f"/invoices/{iv['invoice_id']}", {"line_items": li, "reason": "ZZMATRIX test edit"}); rec("T02", "edit invoice 10 -> 8 while 10 shipped+delivered", "OK or refused?", e)
    # rebuild shipment: undeliver, delete, recreate at 8
    if not Z.delete(f"/shipmentorders/{sid}").get("code") == 0:
        undeliver("T02", sid); rec("T02", "delete shipment (2nd try)", "OK", Z.delete(f"/shipmentorders/{sid}"))
    rec("T02", "delete package 10", "OK", Z.delete(f"/packages/{pid}"))
    pid2, sid2 = ship_full("T02", so, [(ITEM_A, 8)]); deliver("T02", sid2)
    so2, L = so_lines(so["salesorder_id"]); rec("T02", "line after rebuild", "inv 8 pk 8 sh 8", {"code": 0}, qty_str(L[ITEM_A]))
    inv2 = mk_inv("T02", so2, [(ITEM_A, 2)]); rec("T02", "second invoice for 2", "OK", inv2)
    pid3, sid3 = ship_full("T02", so2, [(ITEM_A, 2)])
    if sid3: deliver("T02", sid3)
    so3, L = so_lines(so["salesorder_id"]); rec("T02", "final line", "inv 10 pk 10 sh 10", {"code": 0}, qty_str(L[ITEM_A]))

def T03():
    """Return 2 (no rebuild) then try to pack 2 again: does a return free packing room?"""
    so = mk_so("T03", [(ITEM_A, 10)])
    inv = mk_inv("T03", so, [(ITEM_A, 10)]); pid, sid = ship_full("T03", so, [(ITEM_A, 10)]); deliver("T03", sid)
    r = mk_return("T03", so, [(ITEM_A, 2)]); rec("T03", "sales return 2 + receive", "OK", r, f"receive: {r.get('receive', {}).get('message')}")
    so2, L = so_lines(so["salesorder_id"]); rec("T03", "line after return", "pk 10 sh 10 ret 2 (room 0?)", {"code": 0}, qty_str(L[ITEM_A]))
    iv = Z.get(f"/invoices/{need(inv,'invoice')['invoice']['invoice_id']}")["invoice"]
    li = [{"line_item_id": iv["line_items"][0]["line_item_id"], "item_id": ITEM_A, "quantity": 8, "rate": 10, "salesorder_item_id": so["line_items"][0]["line_item_id"]}]
    rec("T03", "edit invoice 10 -> 8", "OK", Z.put(f"/invoices/{iv['invoice_id']}", {"line_items": li, "reason": "ZZMATRIX test edit"}))
    inv2 = mk_inv("T03", so2, [(ITEM_A, 2)]); rec("T03", "invoice 2 more", "OK", inv2)
    p = mk_pkg(so2, [(ITEM_A, 2)]); rec("T03", "pack 2 again after return", "THE QUESTION: refused if returns do not free room", p)
    so3, L = so_lines(so["salesorder_id"]); rec("T03", "final line", "", {"code": 0}, qty_str(L[ITEM_A]))

def T04():
    """Void an invoice whose 10 are shipped: what does the SO line say, can it be re-invoiced, does a return of 10 work."""
    so = mk_so("T04", [(ITEM_A, 10)])
    inv = mk_inv("T04", so, [(ITEM_A, 10)]); pid, sid = ship_full("T04", so, [(ITEM_A, 10)]); deliver("T04", sid)
    rec("T04", "void invoice", "OK", void_inv(need(inv,"invoice")["invoice"]["invoice_id"]))
    so2, L = so_lines(so["salesorder_id"]); rec("T04", "line after void", "does inv still say 10?", {"code": 0}, qty_str(L[ITEM_A]) + f" so status {so2['status']}")
    r = mk_return("T04", so2, [(ITEM_A, 10)]); rec("T04", "sales return 10 + receive (goods back)", "OK", r, f"receive: {r.get('receive', {}).get('message')}")
    so3, L = so_lines(so["salesorder_id"]); rec("T04", "line after return", "ret 10", {"code": 0}, qty_str(L[ITEM_A]))
    inv2 = mk_inv("T04", so3, [(ITEM_A, 10)]); rec("T04", "re-invoice 10 on same SO after void", "Tarun says refused", inv2)

def T05():
    """Partial invoices 4 then 6, each shipped."""
    so = mk_so("T05", [(ITEM_A, 10)])
    for q in (4, 6):
        so, L = so_lines(so["salesorder_id"])
        inv = mk_inv("T05", so, [(ITEM_A, q)]); rec("T05", f"invoice {q}", "OK", inv)
        ship_full("T05", so, [(ITEM_A, q)])
    so, L = so_lines(so["salesorder_id"]); rec("T05", "final line", "inv 10 pk 10 sh 10", {"code": 0}, qty_str(L[ITEM_A]))

def T06():
    """Over-invoice and over-pack: what are Zoho's exact refusal messages."""
    so = mk_so("T06", [(ITEM_A, 10)])
    inv = mk_inv("T06", so, [(ITEM_A, 12)]); rec("T06", "invoice 12 on a line of 10", "refused?", inv)
    inv = mk_inv("T06", so, [(ITEM_A, 10)]); rec("T06", "invoice 10", "OK", inv)
    p = mk_pkg(so, [(ITEM_A, 12)]); rec("T06", "pack 12 on a line of 10", "refused", p)
    p = mk_pkg(so, [(ITEM_A, 10)]); rec("T06", "pack 10", "OK", p)
    p2 = mk_pkg(so, [(ITEM_A, 1)]); rec("T06", "pack 1 more (room 0)", "refused, message needed for alerts", p2)

def T07():
    """Non-returnable item C: ship 5, then try a return."""
    so = mk_so("T07", [(ITEM_C, 5)])
    inv = mk_inv("T07", so, [(ITEM_C, 5)]); pid, sid = ship_full("T07", so, [(ITEM_C, 5)])
    r = mk_return("T07", so, [(ITEM_C, 5)]); rec("T07", "sales return on NOT returnable item", "refused?", r)
    if r.get("code") == 0: rec("T07", "receive", "", r.get("receive", {}))

def T08():
    """Sharjah warehouse line: where does the package go, where does the return come back."""
    so = mk_so("T08", [(ITEM_B, 3)], location=WH_SHARJAH, branch=BR_SHARJAH)
    so, L = so_lines(so["salesorder_id"]); rec("T08", "SO line location", "Sharjah", {"code": 0}, L[ITEM_B].get("location_name", "?"))
    inv = mk_inv("T08", so, [(ITEM_B, 3)]); pid, sid = ship_full("T08", so, [(ITEM_B, 3)])
    if pid:
        pk = Z.get(f"/packages/{pid}").get("package", {})
        locs = {(l.get("location_name") or l.get("warehouse_name") or "?") for l in pk.get("line_items", [])}
        rec("T08", "package line location", "Sharjah", {"code": 0}, str(locs))
    r = mk_return("T08", so, [(ITEM_B, 3)]); rec("T08", "return 3 + receive", "OK, into Sharjah", r, f"received into {r.get('receive_locs')}")

def T09():
    """Delete (not void) an invoice that has a shipped package."""
    so = mk_so("T09", [(ITEM_A, 2)])
    inv = mk_inv("T09", so, [(ITEM_A, 2)]); pid, sid = ship_full("T09", so, [(ITEM_A, 2)])
    d = Z.delete(f"/invoices/{need(inv,'invoice')['invoice']['invoice_id']}"); rec("T09", "DELETE invoice with shipped package", "allowed? then package is orphaned", d)
    so2, L = so_lines(so["salesorder_id"]); rec("T09", "line after delete", "", {"code": 0}, qty_str(L[ITEM_A]))

def T10():
    """Void then un-void (mark as sent again)."""
    so = mk_so("T10", [(ITEM_A, 2)])
    inv = mk_inv("T10", so, [(ITEM_A, 2)]); pid, sid = ship_full("T10", so, [(ITEM_A, 2)])
    rec("T10", "void", "OK", void_inv(need(inv,"invoice")["invoice"]["invoice_id"]))
    u = Z.post(f"/invoices/{need(inv,'invoice')['invoice']['invoice_id']}/status/sent"); rec("T10", "un-void (status sent) after void", "allowed?", u)
    so2, L = so_lines(so["salesorder_id"]); rec("T10", "line after un-void", "", {"code": 0}, qty_str(L[ITEM_A]))

def T11():
    """Two returns on one line (2 then 3), and a return larger than shipped."""
    so = mk_so("T11", [(ITEM_A, 6)])
    inv = mk_inv("T11", so, [(ITEM_A, 6)]); pid, sid = ship_full("T11", so, [(ITEM_A, 6)])
    rec("T11", "return 2", "OK", mk_return("T11", so, [(ITEM_A, 2)]))
    rec("T11", "return 3 (second return, same line)", "OK", mk_return("T11", so, [(ITEM_A, 3)]))
    rec("T11", "return 5 (more than the 1 left)", "refused", mk_return("T11", so, [(ITEM_A, 5)]))
    so2, L = so_lines(so["salesorder_id"]); rec("T11", "line", "sh 6 ret 5", {"code": 0}, qty_str(L[ITEM_A]))

def T12():
    """Item swap on an invoice (A -> B) after A was shipped."""
    so = mk_so("T12", [(ITEM_A, 2), (ITEM_B, 2)])
    inv = mk_inv("T12", so, [(ITEM_A, 2)]); pid, sid = ship_full("T12", so, [(ITEM_A, 2)])
    iv = Z.get(f"/invoices/{need(inv,'invoice')['invoice']['invoice_id']}")["invoice"]
    li = [{"line_item_id": iv["line_items"][0]["line_item_id"], "item_id": ITEM_B, "quantity": 2, "rate": 10, "salesorder_item_id": so["line_items"][1]["line_item_id"]}]
    e = Z.put(f"/invoices/{iv['invoice_id']}", {"line_items": li, "reason": "ZZMATRIX test edit"}); rec("T12", "edit invoice: swap A for B while A shipped", "allowed?", e)
    so2, L = so_lines(so["salesorder_id"]); rec("T12", "lines after swap", "", {"code": 0}, "A: " + qty_str(L[ITEM_A]) + " | B: " + qty_str(L[ITEM_B]))

def T13():
    """Date-only edit: does last_modified change and do quantities stay."""
    so = mk_so("T13", [(ITEM_A, 1)])
    inv = mk_inv("T13", so, [(ITEM_A, 1)]); pid, sid = ship_full("T13", so, [(ITEM_A, 1)])
    iv = Z.get(f"/invoices/{need(inv,'invoice')['invoice']['invoice_id']}")["invoice"]; before = iv["last_modified_time"]
    e = Z.put(f"/invoices/{iv['invoice_id']}", {"date": (dt.date.today() - dt.timedelta(days=1)).isoformat(), "reason": "ZZMATRIX test edit"})
    after = Z.get(f"/invoices/{iv['invoice_id']}")["invoice"]["last_modified_time"]
    rec("T13", "date-only edit", "OK, last_modified changes", e, f"{before} -> {after}")

def T14():
    """Credit note with item lines on a shipped invoice: does Zoho itself move stock or SO quantities?"""
    so = mk_so("T14", [(ITEM_A, 4)])
    inv = mk_inv("T14", so, [(ITEM_A, 4)]); pid, sid = ship_full("T14", so, [(ITEM_A, 4)])
    iv = Z.get(f"/invoices/{need(inv,'invoice')['invoice']['invoice_id']}")["invoice"]
    cn = Z.post("/creditnotes", {"customer_id": CUST, "date": TODAY, "reference_number": "ZZMATRIX T14", "salesperson_id": SALESPERSON, **({"place_of_supply": iv["place_of_supply"]} if iv.get("place_of_supply") else {}),
                                 "line_items": [{"item_id": ITEM_A, "quantity": 2, "rate": 10}]}, invoice_id=iv["invoice_id"])
    rec("T14", "credit note 2 units against shipped invoice", "OK", cn)
    so2, L = so_lines(so["salesorder_id"]); rec("T14", "SO line after credit note (no return yet)", "ret 0, Zoho does not auto-return", {"code": 0}, qty_str(L[ITEM_A]))

def T15():
    """Ship an item with zero physical stock (negative allowed?)."""
    j = Z.get("/items", name_startswith="ZZ TEST ITEM D")
    items = [i for i in j.get("items", []) if i["name"].startswith("ZZ TEST ITEM D")]
    if items: item_d = items[0]["item_id"]
    else:
        c = Z.post("/items", {"name": "ZZ TEST ITEM D (zero stock, do not sell)", "rate": 10, "purchase_rate": 5, "item_type": "inventory",
                              "product_type": "goods", "sku": "ZZTEST-D", "initial_stock": 0, "initial_stock_rate": 5,
                              "description": "Automation test item with zero stock. Not for sale."})
        if c.get("code") != 0: rec("T15", "create zero-stock item D", "OK", c); return
        item_d = c["item"]["item_id"]
    so = mk_so("T15", [(item_d, 3)])
    inv = mk_inv("T15", so, [(item_d, 3)]); rec("T15", "invoice 3 of zero-stock item", "OK", inv)
    pid, sid = ship_full("T15", so, [(item_d, 3)])
    it = Z.get(f"/items/{item_d}").get("item", {}); rec("T15", "stock after", "-3 physical", {"code": 0}, f"stock_on_hand {it.get('stock_on_hand')} actual_available {it.get('actual_available_stock')}")

def T16():
    """Two invoices on one SO, both voided within a second: pure Zoho side (no automation on customer 2)."""
    so = mk_so("T16", [(ITEM_A, 2), (ITEM_B, 2)])
    i1 = mk_inv("T16", so, [(ITEM_A, 2)]); so, L = so_lines(so["salesorder_id"]); i2 = mk_inv("T16", so, [(ITEM_B, 2)])
    p1, s1 = ship_full("T16", so, [(ITEM_A, 2)]); p2, s2 = ship_full("T16", so, [(ITEM_B, 2)])
    v1 = void_inv(need(i1,"invoice 1")["invoice"]["invoice_id"]); v2 = void_inv(need(i2,"invoice 2")["invoice"]["invoice_id"])
    rec("T16", "void invoice 1", "OK", v1); rec("T16", "void invoice 2 immediately after", "OK", v2)
    r1 = mk_return("T16", so, [(ITEM_A, 2)]); r2 = mk_return("T16", so, [(ITEM_B, 2)])
    rec("T16", "return A (right after void)", "OK", r1); rec("T16", "return B (back to back)", "OK", r2)

def item_stock(item_id):
    it = Z.get(f"/items/{item_id}").get("item", {})
    return f"acct {it.get('stock_on_hand')} phys {it.get('actual_available_stock')}"

def T17():
    """Bulk Stock warehouse line under Head Office branch: package and return locations."""
    so = mk_so("T17", [(ITEM_A, 2)], location=WH_BULK, branch=BR_HEAD)
    so, L = so_lines(so["salesorder_id"]); rec("T17", "SO line location", "Bulk Stock", {"code": 0}, L[ITEM_A].get("location_name", "?"))
    inv = mk_inv("T17", so, [(ITEM_A, 2)]); pid, sid = ship_full("T17", so, [(ITEM_A, 2)])
    pk = Z.get(f"/packages/{pid}").get("package", {})
    rec("T17", "package line location", "Bulk Stock", {"code": 0}, str({(l.get("location_name") or "?") for l in pk.get("line_items", [])}))
    r = mk_return("T17", so, [(ITEM_A, 2)]); rec("T17", "return 2 + receive", "OK, into Bulk Stock", r, f"received into {r.get('receive_locs')}")
    sr = Z.get(f"/salesreturns/{r['salesreturn']['salesreturn_id']}").get("salesreturn", {}) if r.get("code") == 0 else {}
    rec("T17", "return location", "Bulk Stock", {"code": 0}, str({(l.get("location_name") or "?") for l in sr.get("line_items", [])}))

def T18():
    """Void with 10 shipped, then sales return 10: does physical stock come back, what does the line say."""
    before = item_stock(ITEM_B)
    so = mk_so("T18", [(ITEM_B, 10)])
    inv = mk_inv("T18", so, [(ITEM_B, 10)]); pid, sid = ship_full("T18", so, [(ITEM_B, 10)]); deliver("T18", sid)
    shipped = item_stock(ITEM_B)
    rec("T18", "invoice status after create", "sent", {"code": 0}, str(inv.get("status_after")))
    rec("T18", "void invoice", "OK", void_inv(need(inv, "invoice")["invoice"]["invoice_id"]))
    r = mk_return("T18", so, [(ITEM_B, 10)]); rec("T18", "sales return 10 + receive", "OK", r, f"received into {r.get('receive_locs')}")
    so2, L = so_lines(so["salesorder_id"]); rec("T18", "line after void+return", "inv 10 pk 10 sh 10 ret 10", {"code": 0}, qty_str(L[ITEM_B]) + f" so {so2['status']}")
    rec("T18", "SO GET carries a salesreturns list (needed by the void rule)", "yes", {"code": 0}, f"{len(so2['salesreturns'])} returns listed" if "salesreturns" in so2 else "KEY MISSING: " + ",".join(k for k in so2 if "return" in k or "package" in k))
    rec("T18", "item B stock: before / shipped / after return", "phys back to before", {"code": 0}, f"{before} / {shipped} / {item_stock(ITEM_B)}")
    r2 = mk_return("T18", so, [(ITEM_B, 1)]); rec("T18", "one more return after full return", "refused", r2)

def T19():
    """Edit invoice UP (8 -> 10) with room on the SO, and a mixed edit (A down, B up) on a two-line invoice."""
    so = mk_so("T19", [(ITEM_A, 10), (ITEM_B, 10)])
    inv = mk_inv("T19", so, [(ITEM_A, 8), (ITEM_B, 5)]); pid, sid = ship_full("T19", so, [(ITEM_A, 8), (ITEM_B, 5)])
    iv = Z.get(f"/invoices/{need(inv,'invoice')['invoice']['invoice_id']}")["invoice"]
    byitem = {l["item_id"]: l for l in iv["line_items"]}
    li = [{"line_item_id": byitem[ITEM_A]["line_item_id"], "item_id": ITEM_A, "quantity": 10, "rate": 10, "salesorder_item_id": so["line_items"][0]["line_item_id"]},
          {"line_item_id": byitem[ITEM_B]["line_item_id"], "item_id": ITEM_B, "quantity": 3, "rate": 10, "salesorder_item_id": so["line_items"][1]["line_item_id"]}]
    e = Z.put(f"/invoices/{iv['invoice_id']}", {"line_items": li, "reason": "ZZMATRIX test edit"}); rec("T19", "edit invoice A 8->10, B 5->3 while shipped 8/5", "OK", e)
    so2, L = so_lines(so["salesorder_id"]); rec("T19", "lines after edit (packages untouched)", "A inv 10 pk 8, B inv 3 pk 5", {"code": 0}, "A: " + qty_str(L[ITEM_A]) + " | B: " + qty_str(L[ITEM_B]))
    # rebuild like the automation would: delete shipment+package, recreate at 10/3
    rec("T19", "delete shipment", "OK", Z.delete(f"/shipmentorders/{sid}")); rec("T19", "delete package", "OK", Z.delete(f"/packages/{pid}"))
    pid2, sid2 = ship_full("T19", so2, [(ITEM_A, 10), (ITEM_B, 3)])
    so3, L = so_lines(so["salesorder_id"]); rec("T19", "lines after rebuild", "A 10/10/10, B 3/3/3", {"code": 0}, "A: " + qty_str(L[ITEM_A]) + " | B: " + qty_str(L[ITEM_B]))

def T20():
    """Three invoices on one SO (A, B, C lines), each shipped; void two; return each voided one."""
    so = mk_so("T20", [(ITEM_A, 4), (ITEM_B, 4), (ITEM_C, 4)])
    invs = []
    for it in (ITEM_A, ITEM_B, ITEM_C):
        so, L = so_lines(so["salesorder_id"]); i = mk_inv("T20", so, [(it, 4)]); rec("T20", f"invoice for {it[-4:]}", "OK", i); invs.append(i)
        ship_full("T20", so, [(it, 4)])
    for i in invs[:2]: rec("T20", "void", "OK", void_inv(need(i, "invoice")["invoice"]["invoice_id"]))
    ra = mk_return("T20", so, [(ITEM_A, 4)]); rec("T20", "return A", "OK", ra)
    rb = mk_return("T20", so, [(ITEM_B, 4)]); rec("T20", "return B", "OK", rb)
    so2, L = so_lines(so["salesorder_id"]); rec("T20", "lines", "A ret 4, B ret 4, C ret 0", {"code": 0}, " | ".join(f"{k[-4:]}: {qty_str(L[k])}" for k in (ITEM_A, ITEM_B, ITEM_C)))

def T21():
    """Credit note WITH items on a shipped SO-linked invoice, then the sales return: stock and line."""
    before = item_stock(ITEM_A)
    so = mk_so("T21", [(ITEM_A, 6)])
    inv = mk_inv("T21", so, [(ITEM_A, 6)]); pid, sid = ship_full("T21", so, [(ITEM_A, 6)])
    iv = Z.get(f"/invoices/{need(inv,'invoice')['invoice']['invoice_id']}")["invoice"]
    cn = Z.post("/creditnotes", {"customer_id": CUST, "date": TODAY, "reference_number": "ZZMATRIX T21", "salesperson_id": SALESPERSON, **({"place_of_supply": iv["place_of_supply"]} if iv.get("place_of_supply") else {}),
                                 "line_items": [{"item_id": ITEM_A, "quantity": 2, "rate": 10}]}, invoice_id=iv["invoice_id"])
    rec("T21", "credit note 2 (with item line) on shipped invoice", "OK", cn)
    mid = item_stock(ITEM_A)
    r = mk_return("T21", so, [(ITEM_A, 2)]); rec("T21", "sales return 2 + receive", "OK", r, f"receive: {r.get('receive', {}).get('message')}")
    so2, L = so_lines(so["salesorder_id"]); rec("T21", "line", "inv 6 sh 6 ret 2", {"code": 0}, qty_str(L[ITEM_A]))
    rec("T21", "item A stock before / after CN / after return", "acct -4 after CN, phys -4 after return", {"code": 0}, f"{before} / {mid} / {item_stock(ITEM_A)}")

def T22():
    """Non-returnable item C: sales return after void (needed for the void-to-return rule)."""
    so = mk_so("T22", [(ITEM_C, 3)])
    inv = mk_inv("T22", so, [(ITEM_C, 3)]); pid, sid = ship_full("T22", so, [(ITEM_C, 3)])
    rec("T22", "void", "OK", void_inv(need(inv, "invoice")["invoice"]["invoice_id"]))
    r = mk_return("T22", so, [(ITEM_C, 3)]); rec("T22", "return on non-returnable item after void", "refused?", r, f"receive: {r.get('receive', {}).get('message') if r.get('code') == 0 else ''}")

CASES = {n: f for n, f in globals().items() if n.startswith("T") and n[1:].isdigit()}

def cleanup():
    """Void every ZZMATRIX invoice and SO (returns and shipments stay as history, as agreed)."""
    for path, key in (("/invoices", "invoices"), ("/salesorders", "salesorders")):
        page = 1
        while True:
            j = Z.get(path, reference_number_startswith="ZZMATRIX", per_page=200, page=page)
            rows = j.get(key, [])
            for r in rows:
                if r.get("status") in ("void", "draft"): continue
                idk = "invoice_id" if key == "invoices" else "salesorder_id"
                v = Z.post(f"{path}/{r[idk]}/status/void"); log("void", r.get("invoice_number") or r.get("salesorder_number"), v.get("message"))
            if not j.get("page_context", {}).get("has_more_page"): break
            page += 1

def main():
    global CUST
    if os.environ.get("CLEANUP") == "1": cleanup(); return
    CUST = customer2(); log("customer 2:", CUST)
    for name in sorted(CASES):
        if ONLY and name not in ONLY: continue
        log(f"\n===== {name}: {CASES[name].__doc__.strip()}")
        try: CASES[name]()
        except Stop as e:
            RESULTS.append({"case": name, "step": "stopped", "expected": "", "zoho": str(e), "note": "case could not continue"}); log("STOP", e)
        except Exception as e:
            RESULTS.append({"case": name, "step": "CRASH", "expected": "", "zoho": str(e), "note": traceback.format_exc()[-300:]})
            log("CRASH", e)
    json.dump(RESULTS, open(f"{OUT}/matrix_results.json", "w"), indent=1)
    with open(f"{OUT}/matrix_results.md", "w") as f:
        f.write(f"# ZZ TEST matrix, phase 1 (Zoho rules), {TODAY}, {Z.calls} API calls\n\n| Case | Step | Expected | Zoho said | Note |\n|---|---|---|---|---|\n")
        for r in RESULTS: f.write(f"| {r['case']} | {r['step']} | {r['expected']} | {r['zoho']} | {r['note']} |\n")
    log(open(f"{OUT}/matrix_results.md").read())

if __name__ == "__main__": main()
