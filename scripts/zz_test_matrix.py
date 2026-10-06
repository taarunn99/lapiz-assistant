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
BASE_BOOKS = "https://www.zohoapis.com/books/v3"
# Books workflow rules did not fire for invoices created through the Inventory API (run #7, 29 Sep).
# Humans work in Books, so the live cases (L*) send every /invoices call through the Books API instead.
USE_BOOKS_FOR_INVOICES = False
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
            base = BASE_BOOKS if (USE_BOOKS_FOR_INVOICES and (path.startswith("/invoices") or path.startswith("/creditnotes"))) else BASE
            r = requests.request(m, base + path, params=p, json=body, headers={"Authorization": f"Zoho-oauthtoken {self.tok}"}, timeout=60)
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
def mk_inv(case, so, lines, date=TODAY, extra=None, approve=True, so_param=True):
    """lines: list of (item_id, qty). Links to SO lines via salesorder_item_id.
    extra: list of (item_id, qty) NOT linked to the SO. approve=False leaves the invoice as a draft.
    so_param=False sends no salesorder_id query param (multi SO invoice)."""
    byitem = {l["item_id"]: l for l in so["line_items"]}
    li = [{"item_id": it, "quantity": q, "rate": 10, "salesorder_item_id": byitem[it]["line_item_id"]} for it, q in lines]
    li += [{"item_id": it, "quantity": q, "rate": 10} for it, q in (extra or [])]
    body = {"customer_id": CUST, "date": date, "reference_number": f"ZZMATRIX {case}", "salesperson_id": SALESPERSON, "line_items": li}
    j = Z.post("/invoices", body, **({"salesorder_id": so["salesorder_id"]} if so_param else {}))
    if j.get("code") != 0: return j
    if approve: approve_inv(j["invoice"]["invoice_id"])
    j["status_after"] = Z.get(f"/invoices/{j['invoice']['invoice_id']}").get("invoice", {}).get("status")
    return j

def approve_inv(inv_id):
    for step in ("submit", "approve", "status/sent"):   # invoice approval is switched on in this org
        r = Z.post(f"/invoices/{inv_id}/{step}")
        if r.get("code") != 0: log(f"invoice {step}:", r.get("message"))

def edit_inv(inv_id, so, keep, add=None, extra=None):
    """Rebuild the invoice line list. keep: {item_id: qty} for existing SO linked lines (missing = deleted, item changed = swap on same line);
    add: list of (item_id, qty) new SO linked lines; extra: list of (item_id, qty) new lines not on the SO."""
    iv = Z.get(f"/invoices/{inv_id}")["invoice"]
    so_by_item = {l["item_id"]: l for l in so["line_items"]}
    li = []
    for il in iv["line_items"]:
        it = il["item_id"]
        if it in keep:
            li.append({"line_item_id": il["line_item_id"], "item_id": it, "quantity": keep[it], "rate": 10, "salesorder_item_id": so_by_item[it]["line_item_id"]})
    for it, q in (add or []): li.append({"item_id": it, "quantity": q, "rate": 10, "salesorder_item_id": so_by_item[it]["line_item_id"]})
    for it, q in (extra or []): li.append({"item_id": it, "quantity": q, "rate": 10})
    return Z.put(f"/invoices/{inv_id}", {"line_items": li, "reason": "ZZMATRIX test edit"})

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

def T23():
    """Human style swap: invoice has line A (from SO). Edit: remove line A, add line B (B is on the SO). Then rebuild like the automation."""
    so = mk_so("T23", [(ITEM_A, 2), (ITEM_B, 2)])
    inv = mk_inv("T23", so, [(ITEM_A, 2)]); pid, sid = ship_full("T23", so, [(ITEM_A, 2)])
    iv = Z.get(f"/invoices/{need(inv,'invoice')['invoice']['invoice_id']}")["invoice"]
    li = [{"item_id": ITEM_B, "quantity": 2, "rate": 10, "salesorder_item_id": so["line_items"][1]["line_item_id"]}]   # no line_item_id = new line; A's line is left out = deleted
    e = Z.put(f"/invoices/{iv['invoice_id']}", {"line_items": li, "reason": "ZZMATRIX test edit"}); rec("T23", "edit invoice: delete line A, add line B (SO item)", "allowed?", e)
    iv2 = Z.get(f"/invoices/{iv['invoice_id']}").get("invoice", {})
    rec("T23", "invoice lines after edit", "only B, linked to SO", {"code": 0}, "; ".join(f"{l.get('name')} x{l.get('quantity'):g} so_line={'yes' if l.get('salesorder_item_id') else 'NO'}" for l in iv2.get("line_items", [])))
    so2, L = so_lines(so["salesorder_id"]); rec("T23", "SO lines after edit (package untouched)", "A inv 0 pk 2, B inv 2 pk 0", {"code": 0}, "A: " + qty_str(L[ITEM_A]) + " | B: " + qty_str(L[ITEM_B]))
    if e.get("code") == 0:
        rec("T23", "delete shipment A", "OK", Z.delete(f"/shipmentorders/{sid}")); rec("T23", "delete package A", "OK", Z.delete(f"/packages/{pid}"))
        pid2, sid2 = ship_full("T23", so2, [(ITEM_B, 2)])
        so3, L = so_lines(so["salesorder_id"]); rec("T23", "SO lines after rebuild", "A 0/0/0, B 2/2/2", {"code": 0}, "A: " + qty_str(L[ITEM_A]) + " | B: " + qty_str(L[ITEM_B]))

def T24():
    """Extra item on an SO invoice: add item C which is NOT on the sales order. What happens to C's stock, and can the SO lines still be packed."""
    before = item_stock(ITEM_C)
    so = mk_so("T24", [(ITEM_A, 2)])
    inv = mk_inv("T24", so, [(ITEM_A, 2)])
    iv = Z.get(f"/invoices/{need(inv,'invoice')['invoice']['invoice_id']}")["invoice"]
    li = [{"line_item_id": iv["line_items"][0]["line_item_id"], "item_id": ITEM_A, "quantity": 2, "rate": 10, "salesorder_item_id": so["line_items"][0]["line_item_id"]},
          {"item_id": ITEM_C, "quantity": 3, "rate": 10}]
    e = Z.put(f"/invoices/{iv['invoice_id']}", {"line_items": li, "reason": "ZZMATRIX test edit"}); rec("T24", "edit invoice: add item C (not on SO)", "allowed?", e)
    iv2 = Z.get(f"/invoices/{iv['invoice_id']}").get("invoice", {})
    rec("T24", "invoice lines after edit", "A linked, C not linked", {"code": 0}, "; ".join(f"{l.get('name')} x{l.get('quantity'):g} so_line={'yes' if l.get('salesorder_item_id') else 'NO'}" for l in iv2.get("line_items", [])))
    rec("T24", "item C stock before / after invoice with extra line", "does Zoho move C's physical stock on its own?", {"code": 0}, f"{before} / {item_stock(ITEM_C)}")
    pid, sid = ship_full("T24", so, [(ITEM_A, 2)])
    so2, L = so_lines(so["salesorder_id"]); rec("T24", "SO line A", "2/2/2", {"code": 0}, qty_str(L[ITEM_A]))
    rec("T24", "item C stock after A shipped", "unchanged by A", {"code": 0}, item_stock(ITEM_C))

# =====================================================================================
# Phase 2: the AUTOMATION itself, on ZZ TEST CUSTOMER (LBCUS-9203) where the Deluge rules fire.
# Each case creates real records, waits for the workflow, and checks what the automation did.
# =====================================================================================
CUST_LIVE = "2305879000148206348"   # ZZ TEST CUSTOMER (the Deluge rules are limited to this customer)
WAIT = int(os.environ.get("WAIT_SECS", "150"))

def wait_for(desc, check, timeout=None):
    """Poll `check()` (returns truthy when the automation has done its job) for up to timeout seconds."""
    t0 = time.time(); last = None
    while time.time() - t0 < (timeout or WAIT):
        try: last = check()
        except Exception as e: last = f"err {e}"
        if last and not (isinstance(last, str) and last.startswith("err")): return last, round(time.time() - t0)
        time.sleep(8)
    return None, round(time.time() - t0)

def so_state(so_id):
    so = Z.get(f"/salesorders/{so_id}")["salesorder"]
    L = {l["item_id"]: l for l in so["line_items"]}
    pk = []
    for p in so.get("packages", []):
        full = Z.get(f"/packages/{p['package_id']}").get("package", {})
        pk.append({"number": full.get("package_number"), "status": full.get("status"), "ship_status": full.get("shipment_status") or (full.get("shipment_order") or {}).get("status"),
                   "notes": full.get("notes", ""), "shipment_id": full.get("shipment_id"), "lines": {l.get("so_line_item_id"): l.get("quantity") for l in full.get("line_items", [])},
                   "locs": sorted({l.get("location_name") or "?" for l in full.get("line_items", [])})})
    rets = []
    for r in so.get("salesreturns", []):
        full = Z.get(f"/salesreturns/{r['salesreturn_id']}").get("salesreturn", {})
        rets.append({"number": full.get("salesreturn_number"), "status": full.get("status"), "notes": (full.get("reason") or "") + " " + (full.get("notes") or ""), "date": full.get("date"),
                     "qty": sum(float(l.get("quantity", 0)) for l in full.get("line_items", [])), "received": full.get("receive_status") or full.get("salesreturn_status") or full.get("status"), "receives": len(full.get("salesreturnreceives") or []),
                     "locs": sorted({l.get("location_name") or "?" for l in full.get("line_items", [])})})
    comments = [c.get("description", "") for c in Z.get(f"/salesorders/{so_id}/comments").get("comments", []) if "LB-STOCKSYNC" in (c.get("description") or "")]
    return so, L, pk, rets, comments

def ship_status(ship_id):
    if not ship_id: return None
    return Z.get(f"/shipmentorders/{ship_id}").get("shipmentorder", {}).get("status")

def mirror_ok(so_id, want):
    """want: {item_id: qty}. True when every SO line shows pk == sh == want and the mirror package is delivered."""
    so, L, pk, rets, cm = so_state(so_id)
    for it, q in want.items():
        l = L[it]
        if float(l.get("quantity_packed", 0)) != q or float(l.get("quantity_shipped", 0)) != q: return False
    mine = [p for p in pk if "LB-STOCKSYNC" in p["notes"]]
    if sum(want.values()) > 0 and not mine: return False
    for p in mine:
        if ship_status(p["shipment_id"]) != "delivered": return False
    return (so, L, pk, rets, cm)

def returned_ok(so_id, want_ret):
    so, L, pk, rets, cm = so_state(so_id)
    for it, q in want_ret.items():
        if float(L[it].get("quantity_returned", 0)) != q: return False
    return (so, L, pk, rets, cm)

def report(case, step, res, secs, detail=""):
    rec(case, step, "automation did it", {"code": 0 if res else -1, "message": f"not seen within {secs}s"}, detail)

def L01():
    """Create: invoice 10 -> automation packs, ships and marks delivered; re-save with no change -> still one package."""
    so = mk_so("L01", [(ITEM_A, 10)]); inv = need(mk_inv("L01", so, [(ITEM_A, 10)]), "invoice")
    res, secs = wait_for("mirror", lambda: mirror_ok(so["salesorder_id"], {ITEM_A: 10}))
    report("L01", "package + shipment + delivered after invoice", res, secs, qty_str(so_state(so["salesorder_id"])[1][ITEM_A]))
    iv = Z.get(f"/invoices/{inv['invoice']['invoice_id']}")["invoice"]
    Z.put(f"/invoices/{iv['invoice_id']}", {"notes": "ZZ re-save, nothing changed", "reason": "ZZMATRIX re-save test"})
    time.sleep(60)
    so2, L, pk, rets, cm = so_state(so["salesorder_id"])
    rec("L01", "re-save with no quantity change", "still exactly one package, no alert", {"code": 0 if len(pk) == 1 and not cm else -1, "message": f"{len(pk)} packages, alerts {cm}"}, f"{len(pk)} package(s), comments {len(cm)}")

def L02():
    """Edit down 10 -> 8: shipment rebuilt at 8 (delivered); then invoice 2 more -> second package of 2."""
    so = mk_so("L02", [(ITEM_A, 10)]); inv = need(mk_inv("L02", so, [(ITEM_A, 10)]), "invoice")
    res, secs = wait_for("mirror 10", lambda: mirror_ok(so["salesorder_id"], {ITEM_A: 10})); report("L02", "first mirror 10", res, secs)
    iv = Z.get(f"/invoices/{inv['invoice']['invoice_id']}")["invoice"]
    li = [{"line_item_id": iv["line_items"][0]["line_item_id"], "item_id": ITEM_A, "quantity": 8, "rate": 10, "salesorder_item_id": so["line_items"][0]["line_item_id"]}]
    rec("L02", "edit invoice 10 -> 8", "OK", Z.put(f"/invoices/{iv['invoice_id']}", {"line_items": li, "reason": "ZZMATRIX test edit"}))
    res, secs = wait_for("mirror 8", lambda: mirror_ok(so["salesorder_id"], {ITEM_A: 8})); report("L02", "rebuilt at 8, delivered", res, secs, qty_str(so_state(so["salesorder_id"])[1][ITEM_A]))
    so2, L = so_lines(so["salesorder_id"]); inv2 = mk_inv("L02", so2, [(ITEM_A, 2)]); rec("L02", "invoice 2 more", "OK", inv2)
    res, secs = wait_for("mirror 10 again", lambda: mirror_ok(so["salesorder_id"], {ITEM_A: 10})); report("L02", "second package of 2, line back to 10/10/10", res, secs, qty_str(so_state(so["salesorder_id"])[1][ITEM_A]))
    so3, L, pk, rets, cm = so_state(so["salesorder_id"]); rec("L02", "packages on SO", "2 (8 and 2)", {"code": 0}, str([(p["number"], sum(p["lines"].values())) for p in pk]) + f" alerts {cm}")

def L03():
    """Void: invoice 10 shipped -> void -> sales return 10 received into stock, shipment kept, stock back."""
    before = item_stock(ITEM_B)
    so = mk_so("L03", [(ITEM_B, 10)]); inv = need(mk_inv("L03", so, [(ITEM_B, 10)]), "invoice")
    res, secs = wait_for("mirror", lambda: mirror_ok(so["salesorder_id"], {ITEM_B: 10})); report("L03", "shipped 10", res, secs)
    shipped = item_stock(ITEM_B)
    rec("L03", "void invoice", "OK", void_inv(inv["invoice"]["invoice_id"]))
    res, secs = wait_for("return", lambda: returned_ok(so["salesorder_id"], {ITEM_B: 10})); report("L03", "sales return 10 after void", res, secs)
    so2, L, pk, rets, cm = so_state(so["salesorder_id"])
    rec("L03", "return details", "1 return, note has 'void inv:', received, dated today", {"code": 0}, str(rets) + f" alerts {cm}")
    rec("L03", "shipment still there", "package still shipped/delivered", {"code": 0}, str([(p["number"], ship_status(p["shipment_id"])) for p in pk]))
    rec("L03", "item B stock before / shipped / after void", "back to before", {"code": 0}, f"{before} / {shipped} / {item_stock(ITEM_B)}")
    # void is irreversible: a second save of the void invoice must not create a second return
    time.sleep(30); so3, L, pk, rets2, cm = so_state(so["salesorder_id"])
    rec("L03", "still exactly one return", "1", {"code": 0 if len(rets2) == 1 else -1, "message": f"{len(rets2)} returns"}, f"{len(rets2)} return(s)")

def L04():
    """Three invoices on one SO (A, B, C), all shipped; void two -> two separate returns; third untouched."""
    so = mk_so("L04", [(ITEM_A, 4), (ITEM_B, 4), (ITEM_C, 4)]); invs = []
    for it in (ITEM_A, ITEM_B, ITEM_C):
        so, L = so_lines(so["salesorder_id"]); i = need(mk_inv("L04", so, [(it, 4)]), "invoice"); invs.append(i)
        res, secs = wait_for("mirror", lambda it=it: mirror_ok(so["salesorder_id"], {it: 4})); report("L04", f"invoice for {it[-4:]} shipped", res, secs)
    for i in invs[:2]: rec("L04", "void", "OK", void_inv(i["invoice"]["invoice_id"]))
    res, secs = wait_for("returns", lambda: returned_ok(so["salesorder_id"], {ITEM_A: 4, ITEM_B: 4})); report("L04", "two returns after two voids", res, secs)
    so2, L, pk, rets, cm = so_state(so["salesorder_id"])
    rec("L04", "lines", "A ret 4, B ret 4, C ret 0", {"code": 0}, " | ".join(f"{k[-4:]}: {qty_str(L[k])}" for k in (ITEM_A, ITEM_B, ITEM_C)))
    rec("L04", "returns", "2, each with its own 'void inv:' note", {"code": 0 if len(rets) == 2 else -1, "message": f"{len(rets)} returns"}, str([(r["number"], r["qty"], r["notes"][:40]) for r in rets]) + f" alerts {cm}")

def L05():
    """Human swap: invoice line A shipped; edit removes A and adds B -> automation un-ships A and ships B."""
    so = mk_so("L05", [(ITEM_A, 2), (ITEM_B, 2)]); inv = need(mk_inv("L05", so, [(ITEM_A, 2)]), "invoice")
    res, secs = wait_for("mirror A", lambda: mirror_ok(so["salesorder_id"], {ITEM_A: 2})); report("L05", "A shipped", res, secs)
    iv = Z.get(f"/invoices/{inv['invoice']['invoice_id']}")["invoice"]
    li = [{"item_id": ITEM_B, "quantity": 2, "rate": 10, "salesorder_item_id": so["line_items"][1]["line_item_id"]}]
    rec("L05", "edit: delete line A, add line B", "OK", Z.put(f"/invoices/{iv['invoice_id']}", {"line_items": li, "reason": "ZZMATRIX test edit"}))
    res, secs = wait_for("mirror B", lambda: mirror_ok(so["salesorder_id"], {ITEM_A: 0, ITEM_B: 2})); report("L05", "A back to 0, B shipped 2", res, secs)
    so2, L, pk, rets, cm = so_state(so["salesorder_id"]); rec("L05", "lines", "A 0/0/0, B 2/2/2", {"code": 0}, "A: " + qty_str(L[ITEM_A]) + " | B: " + qty_str(L[ITEM_B]) + f" alerts {cm}")

def L06():
    """Sharjah warehouse: package from Sharjah, and after void the return goes back into Sharjah."""
    so = mk_so("L06", [(ITEM_B, 3)], location=WH_SHARJAH, branch=BR_SHARJAH); inv = need(mk_inv("L06", so, [(ITEM_B, 3)]), "invoice")
    res, secs = wait_for("mirror", lambda: mirror_ok(so["salesorder_id"], {ITEM_B: 3})); report("L06", "shipped from Sharjah", res, secs)
    so2, L, pk, rets, cm = so_state(so["salesorder_id"]); rec("L06", "package warehouse", "Sharjah", {"code": 0}, str([p["locs"] for p in pk]))
    rec("L06", "void", "OK", void_inv(inv["invoice"]["invoice_id"]))
    res, secs = wait_for("return", lambda: returned_ok(so["salesorder_id"], {ITEM_B: 3})); report("L06", "return after void", res, secs)
    so3, L, pk, rets, cm = so_state(so["salesorder_id"]); rec("L06", "return warehouse", "Sharjah", {"code": 0}, str([(r["number"], r["locs"]) for r in rets]) + f" alerts {cm}")

def L07():
    """Extra item not on the SO added to the invoice: SO line still shipped, extra line ignored, no alert."""
    so = mk_so("L07", [(ITEM_A, 2)]); inv = need(mk_inv("L07", so, [(ITEM_A, 2)]), "invoice")
    res, secs = wait_for("mirror", lambda: mirror_ok(so["salesorder_id"], {ITEM_A: 2})); report("L07", "A shipped", res, secs)
    iv = Z.get(f"/invoices/{inv['invoice']['invoice_id']}")["invoice"]
    li = [{"line_item_id": iv["line_items"][0]["line_item_id"], "item_id": ITEM_A, "quantity": 2, "rate": 10, "salesorder_item_id": so["line_items"][0]["line_item_id"]},
          {"item_id": ITEM_C, "quantity": 3, "rate": 10}]
    rec("L07", "edit: add item C (not on SO)", "OK", Z.put(f"/invoices/{iv['invoice_id']}", {"line_items": li, "reason": "ZZMATRIX test edit"}))
    time.sleep(90); so2, L, pk, rets, cm = so_state(so["salesorder_id"])
    rec("L07", "after edit", "A still 2/2/2, one package, no alert", {"code": 0 if len(pk) == 1 and not cm else -1, "message": f"{len(pk)} packages, alerts {cm}"}, qty_str(L[ITEM_A]))

def L08():
    """Two invoices on the same SO saved back to back (the collision case): both must end up shipped, or leave an alert."""
    so = mk_so("L08", [(ITEM_A, 3), (ITEM_B, 3)])
    i1 = need(mk_inv("L08", so, [(ITEM_A, 3)]), "invoice"); so2, L = so_lines(so["salesorder_id"]); i2 = need(mk_inv("L08", so2, [(ITEM_B, 3)]), "invoice")
    res, secs = wait_for("both", lambda: mirror_ok(so["salesorder_id"], {ITEM_A: 3, ITEM_B: 3}), timeout=WAIT + 60); report("L08", "both invoices shipped despite back to back saves", res, secs)
    so3, L, pk, rets, cm = so_state(so["salesorder_id"]); rec("L08", "state", "2 packages, no alert", {"code": 0}, "A: " + qty_str(L[ITEM_A]) + " | B: " + qty_str(L[ITEM_B]) + f" packages {len(pk)} alerts {cm}")
    v1 = void_inv(i1["invoice"]["invoice_id"]); v2 = void_inv(i2["invoice"]["invoice_id"]); rec("L08", "void both back to back", "OK", v1 if v1.get("code") else v2)
    res, secs = wait_for("returns", lambda: returned_ok(so["salesorder_id"], {ITEM_A: 3, ITEM_B: 3}), timeout=WAIT + 60); report("L08", "both returns after back to back voids", res, secs)
    so4, L, pk, rets, cm = so_state(so["salesorder_id"]); rec("L08", "returns", "2", {"code": 0 if len(rets) == 2 else -1, "message": f"{len(rets)} returns"}, str([(r["number"], r["qty"]) for r in rets]) + f" alerts {cm}")

def L09():
    """Partial invoices 4 then 6 on a line of 10: two packages, line 10/10/10, both delivered."""
    so = mk_so("L09", [(ITEM_A, 10)])
    need(mk_inv("L09", so, [(ITEM_A, 4)]), "invoice"); res, secs = wait_for("4", lambda: mirror_ok(so["salesorder_id"], {ITEM_A: 4})); report("L09", "first 4 shipped", res, secs)
    so2, L = so_lines(so["salesorder_id"]); need(mk_inv("L09", so2, [(ITEM_A, 6)]), "invoice"); res, secs = wait_for("10", lambda: mirror_ok(so["salesorder_id"], {ITEM_A: 10})); report("L09", "then 6 shipped, line 10/10/10", res, secs)
    so3, L, pk, rets, cm = so_state(so["salesorder_id"]); rec("L09", "packages", "2, both delivered", {"code": 0}, str([(p["number"], sum(p["lines"].values()), ship_status(p["shipment_id"])) for p in pk]) + f" alerts {cm}")

def L10():
    """Non-returnable item C: ship, then void -> return still created (Zoho allows it through the API)."""
    so = mk_so("L10", [(ITEM_C, 3)]); inv = need(mk_inv("L10", so, [(ITEM_C, 3)]), "invoice")
    res, secs = wait_for("mirror", lambda: mirror_ok(so["salesorder_id"], {ITEM_C: 3})); report("L10", "C shipped", res, secs)
    rec("L10", "void", "OK", void_inv(inv["invoice"]["invoice_id"]))
    res, secs = wait_for("return", lambda: returned_ok(so["salesorder_id"], {ITEM_C: 3})); report("L10", "return of non-returnable item after void", res, secs)
    so2, L, pk, rets, cm = so_state(so["salesorder_id"]); rec("L10", "state", "ret 3, 1 return", {"code": 0}, qty_str(L[ITEM_C]) + f" returns {len(rets)} alerts {cm}")

def L11():
    """Bulk Stock warehouse line: ship from Bulk Stock, void -> return into Bulk Stock."""
    so = mk_so("L11", [(ITEM_A, 2)], location=WH_BULK, branch=BR_HEAD); inv = need(mk_inv("L11", so, [(ITEM_A, 2)]), "invoice")
    res, secs = wait_for("mirror", lambda: mirror_ok(so["salesorder_id"], {ITEM_A: 2})); report("L11", "shipped from Bulk Stock", res, secs)
    so2, L, pk, rets, cm = so_state(so["salesorder_id"]); rec("L11", "package warehouse", "Bulk Stock", {"code": 0}, str([p["locs"] for p in pk]))
    rec("L11", "void", "OK", void_inv(inv["invoice"]["invoice_id"]))
    res, secs = wait_for("return", lambda: returned_ok(so["salesorder_id"], {ITEM_A: 2})); report("L11", "return after void", res, secs)
    so3, L, pk, rets, cm = so_state(so["salesorder_id"]); rec("L11", "return warehouse", "Bulk Stock", {"code": 0}, str([(r["number"], r["locs"]) for r in rets]) + f" alerts {cm}")

def L12():
    """Mixed edit on a two line invoice (A 8->10, B 5->3) with the shipment already delivered."""
    so = mk_so("L12", [(ITEM_A, 10), (ITEM_B, 10)]); inv = need(mk_inv("L12", so, [(ITEM_A, 8), (ITEM_B, 5)]), "invoice")
    res, secs = wait_for("mirror", lambda: mirror_ok(so["salesorder_id"], {ITEM_A: 8, ITEM_B: 5})); report("L12", "8 and 5 shipped", res, secs)
    iv = Z.get(f"/invoices/{inv['invoice']['invoice_id']}")["invoice"]; byitem = {l["item_id"]: l for l in iv["line_items"]}
    li = [{"line_item_id": byitem[ITEM_A]["line_item_id"], "item_id": ITEM_A, "quantity": 10, "rate": 10, "salesorder_item_id": so["line_items"][0]["line_item_id"]},
          {"line_item_id": byitem[ITEM_B]["line_item_id"], "item_id": ITEM_B, "quantity": 3, "rate": 10, "salesorder_item_id": so["line_items"][1]["line_item_id"]}]
    rec("L12", "edit A 8->10, B 5->3", "OK", Z.put(f"/invoices/{iv['invoice_id']}", {"line_items": li, "reason": "ZZMATRIX test edit"}))
    res, secs = wait_for("rebuilt", lambda: mirror_ok(so["salesorder_id"], {ITEM_A: 10, ITEM_B: 3})); report("L12", "rebuilt at 10 and 3, delivered", res, secs)
    so2, L, pk, rets, cm = so_state(so["salesorder_id"]); rec("L12", "lines", "A 10/10/10, B 3/3/3, one package", {"code": 0}, "A: " + qty_str(L[ITEM_A]) + " | B: " + qty_str(L[ITEM_B]) + f" packages {len(pk)} alerts {cm}")


def L13():
    """Extra item NOT on the sales order included in the very first save of the invoice (A 2 from SO plus C 3 extra)."""
    so = mk_so("L13", [(ITEM_A, 2)]); inv = need(mk_inv("L13", so, [(ITEM_A, 2)], extra=[(ITEM_C, 3)]), "invoice")
    iv = Z.get(f"/invoices/{inv['invoice']['invoice_id']}")["invoice"]
    rec("L13", "invoice lines", "A linked, C not linked", {"code": 0}, "; ".join(f"{l.get('name')} x{l.get('quantity'):g} so_line={'yes' if l.get('salesorder_item_id') else 'NO'}" for l in iv.get("line_items", [])))
    res, secs = wait_for("mirror", lambda: mirror_ok(so["salesorder_id"], {ITEM_A: 2})); report("L13", "A shipped, C ignored", res, secs)
    so2, L, pk, rets, cm = so_state(so["salesorder_id"]); rec("L13", "state", "A 2/2/2, 1 package, 0 alerts", {"code": 0 if len(pk) == 1 and not cm else -1, "message": f"{len(pk)} packages, alerts {cm}"}, qty_str(L[ITEM_A]) + f" packages {len(pk)} alerts {cm}")

def L14():
    """SO has A and B, invoice A only. Edit the SAME line from item A to item B (through the Books door)."""
    so = mk_so("L14", [(ITEM_A, 2), (ITEM_B, 2)]); inv = need(mk_inv("L14", so, [(ITEM_A, 2)]), "invoice")
    res, secs = wait_for("mirror A", lambda: mirror_ok(so["salesorder_id"], {ITEM_A: 2})); report("L14", "A shipped", res, secs)
    iv = Z.get(f"/invoices/{inv['invoice']['invoice_id']}")["invoice"]
    li = [{"line_item_id": iv["line_items"][0]["line_item_id"], "item_id": ITEM_B, "quantity": 2, "rate": 10, "salesorder_item_id": so["line_items"][1]["line_item_id"]}]
    e = Z.put(f"/invoices/{iv['invoice_id']}", {"line_items": li, "reason": "ZZMATRIX test edit"}); allowed = rec("L14", "edit: same line, item A changed to B", "Zoho refuses, or if allowed the automation swaps", e)
    if allowed:
        res, secs = wait_for("mirror B", lambda: mirror_ok(so["salesorder_id"], {ITEM_A: 0, ITEM_B: 2})); report("L14", "A back to 0, B shipped 2", res, secs)
    so2, L, pk, rets, cm = so_state(so["salesorder_id"]); rec("L14", "lines", "A 0/0/0 B 2/2/2 if allowed, else A 2/2/2 untouched", {"code": 0}, "A: " + qty_str(L[ITEM_A]) + " | B: " + qty_str(L[ITEM_B]) + f" packages {len(pk)} alerts {cm}")

def L15():
    """SO has A and B, invoice A only. Edit: keep A, add B. Save. Later edit again: remove B."""
    so = mk_so("L15", [(ITEM_A, 2), (ITEM_B, 3)]); inv = need(mk_inv("L15", so, [(ITEM_A, 2)]), "invoice"); iid = inv["invoice"]["invoice_id"]
    res, secs = wait_for("mirror A", lambda: mirror_ok(so["salesorder_id"], {ITEM_A: 2})); report("L15", "A shipped", res, secs)
    rec("L15", "edit: keep A, add B 3", "OK", edit_inv(iid, so, {ITEM_A: 2}, add=[(ITEM_B, 3)]))
    res, secs = wait_for("mirror A+B", lambda: mirror_ok(so["salesorder_id"], {ITEM_A: 2, ITEM_B: 3})); report("L15", "rebuilt with A 2 and B 3", res, secs)
    so2, L, pk, rets, cm = so_state(so["salesorder_id"]); rec("L15", "after add", "A 2/2/2, B 3/3/3, 1 package", {"code": 0 if len(pk) == 1 else -1, "message": f"{len(pk)} packages"}, "A: " + qty_str(L[ITEM_A]) + " | B: " + qty_str(L[ITEM_B]) + f" alerts {cm}")
    rec("L15", "edit: remove B again", "OK", edit_inv(iid, so, {ITEM_A: 2}))
    res, secs = wait_for("mirror A only", lambda: mirror_ok(so["salesorder_id"], {ITEM_A: 2, ITEM_B: 0})); report("L15", "rebuilt with A only, B back to 0", res, secs)
    so3, L, pk, rets, cm = so_state(so["salesorder_id"]); rec("L15", "after remove", "A 2/2/2, B 0/0/0, 1 package", {"code": 0 if len(pk) == 1 else -1, "message": f"{len(pk)} packages"}, "A: " + qty_str(L[ITEM_A]) + " | B: " + qty_str(L[ITEM_B]) + f" alerts {cm}")

def L16():
    """Edit removes every SO line and leaves only an extra non-SO item: the package must go away, nothing else."""
    so = mk_so("L16", [(ITEM_A, 2)]); inv = need(mk_inv("L16", so, [(ITEM_A, 2)]), "invoice"); iid = inv["invoice"]["invoice_id"]
    res, secs = wait_for("mirror A", lambda: mirror_ok(so["salesorder_id"], {ITEM_A: 2})); report("L16", "A shipped", res, secs)
    e = edit_inv(iid, so, {}, extra=[(ITEM_C, 1)]); rec("L16", "edit: remove A, leave only extra item C", "OK (or Zoho refuses)", e)
    if e.get("code") == 0:
        res, secs = wait_for("unshipped", lambda: mirror_ok(so["salesorder_id"], {ITEM_A: 0})); report("L16", "A back to 0, package removed", res, secs)
    so2, L, pk, rets, cm = so_state(so["salesorder_id"]); rec("L16", "state", "A 0/0/0, 0 packages, 0 alerts", {"code": 0}, qty_str(L[ITEM_A]) + f" packages {len(pk)} alerts {cm}")

def L17():
    """Invoice saved as DRAFT first (not approved). No package until it is approved and sent later."""
    so = mk_so("L17", [(ITEM_A, 2)]); inv = need(mk_inv("L17", so, [(ITEM_A, 2)], approve=False), "invoice"); iid = inv["invoice"]["invoice_id"]
    rec("L17", "invoice status after save", "draft", {"code": 0}, str(inv.get("status_after")))
    time.sleep(75); so2, L, pk, rets, cm = so_state(so["salesorder_id"])
    rec("L17", "while draft: nothing packed", "pk 0, 0 packages", {"code": 0 if len(pk) == 0 and float(L[ITEM_A].get("quantity_packed", 0)) == 0 else -1, "message": f"{len(pk)} packages"}, qty_str(L[ITEM_A]) + f" alerts {cm}")
    approve_inv(iid)
    res, secs = wait_for("mirror", lambda: mirror_ok(so["salesorder_id"], {ITEM_A: 2})); report("L17", "after approve and send: shipped", res, secs)
    so3, L, pk, rets, cm = so_state(so["salesorder_id"]); rec("L17", "state", "A 2/2/2, 1 package", {"code": 0}, qty_str(L[ITEM_A]) + f" packages {len(pk)} alerts {cm}")

def L18():
    """Decimal quantity: 2.5 units."""
    so = mk_so("L18", [(ITEM_A, 2.5)]); inv = need(mk_inv("L18", so, [(ITEM_A, 2.5)]), "invoice")
    res, secs = wait_for("mirror", lambda: mirror_ok(so["salesorder_id"], {ITEM_A: 2.5})); report("L18", "2.5 shipped", res, secs)
    so2, L, pk, rets, cm = so_state(so["salesorder_id"]); rec("L18", "state", "2.5/2.5/2.5", {"code": 0}, qty_str(L[ITEM_A]) + f" packages {[(p['number'], sum(p['lines'].values())) for p in pk]} alerts {cm}")

def L19():
    """Warehouse packed by hand BEFORE the invoice: automation must not add a second package and must not touch the hand package."""
    so = mk_so("L19", [(ITEM_A, 2)])
    hp = mk_pkg(so, [(ITEM_A, 2)], note="hand packed by warehouse"); rec("L19", "hand package (no marker)", "OK", hp)
    hpid = hp.get("package", {}).get("package_id")
    inv = need(mk_inv("L19", so, [(ITEM_A, 2)]), "invoice")
    time.sleep(90); so2, L, pk, rets, cm = so_state(so["salesorder_id"])
    still = any(str(p.get("number")) == str(hp.get("package", {}).get("package_number")) for p in pk)
    rec("L19", "after invoice", "hand package still there, no second package, an ALERT for a human", {"code": 0 if len(pk) == 1 and still else -1, "message": f"{len(pk)} packages, hand package present {still}"}, qty_str(L[ITEM_A]) + f" packages {[(p['number'], p['notes'][:20]) for p in pk]} alerts {cm}")

def L20():
    """Invoice dated yesterday on a sales order dated today: Zoho refuses a package before the SO date, so the package must carry the SO date."""
    y = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    so = mk_so("L20", [(ITEM_A, 1)]); inv = need(mk_inv("L20", so, [(ITEM_A, 1)], date=y), "invoice")
    res, secs = wait_for("mirror", lambda: mirror_ok(so["salesorder_id"], {ITEM_A: 1})); report("L20", "shipped", res, secs)
    so2, L, pk, rets, cm = so_state(so["salesorder_id"])
    dates = []
    for p in so2.get("packages", []):
        full = Z.get(f"/packages/{p['package_id']}").get("package", {}); dates.append((full.get("package_number"), full.get("date"), (full.get("shipment_order") or {}).get("date") or full.get("shipment_date")))
    # Zoho refuses a package dated before its sales order, so the automation uses the later of the two dates (SO today, invoice yesterday -> today)
    want_date = max(y, so2.get("date", TODAY))
    rec("L20", "package date", f"later of invoice date {y} and SO date {so2.get('date')}", {"code": 0 if dates and all(d[1] == want_date for d in dates) else -1, "message": str(dates)}, str(dates) + f" alerts {cm}")

def L21():
    """One invoice covering TWO sales orders (A from SO1, B from SO2)."""
    so1 = mk_so("L21", [(ITEM_A, 2)]); so2 = mk_so("L21", [(ITEM_B, 3)])
    li = [{"item_id": ITEM_A, "quantity": 2, "rate": 10, "salesorder_item_id": so1["line_items"][0]["line_item_id"]},
          {"item_id": ITEM_B, "quantity": 3, "rate": 10, "salesorder_item_id": so2["line_items"][0]["line_item_id"]}]
    j = Z.post("/invoices", {"customer_id": CUST, "date": TODAY, "reference_number": "ZZMATRIX L21", "salesperson_id": SALESPERSON, "line_items": li})
    ok = rec("L21", "create one invoice from two sales orders", "allowed?", j)
    if not ok: return
    approve_inv(j["invoice"]["invoice_id"])
    iv = Z.get(f"/invoices/{j['invoice']['invoice_id']}")["invoice"]
    rec("L21", "invoice links", "2 sales orders", {"code": 0}, f"salesorders on invoice: {[s.get('salesorder_number') for s in iv.get('salesorders', [])]} lines so_line={[bool(l.get('salesorder_item_id')) for l in iv['line_items']]}")
    r1, s1 = wait_for("so1", lambda: mirror_ok(so1["salesorder_id"], {ITEM_A: 2})); report("L21", "SO1 A shipped", r1, s1)
    r2, s2 = wait_for("so2", lambda: mirror_ok(so2["salesorder_id"], {ITEM_B: 3}), timeout=60); report("L21", "SO2 B shipped", r2, s2)
    for so in (so1, so2):
        s_, L, pk, rets, cm = so_state(so["salesorder_id"]); rec("L21", f"state {so['salesorder_number']}", "", {"code": 0}, " | ".join(qty_str(l) for l in L.values()) + f" packages {len(pk)} alerts {cm}")

def L22():
    """Same item on two lines of one sales order (A 3 and A 4); invoice both lines."""
    body = {"customer_id": CUST, "date": TODAY, "salesperson_id": SALESPERSON, "reference_number": "ZZMATRIX L22",
            "line_items": [{"item_id": ITEM_A, "quantity": 3, "rate": 10}, {"item_id": ITEM_A, "quantity": 4, "rate": 10}]}
    j = Z.post("/salesorders", body)
    if not rec("L22", "SO with item A on two lines", "OK", j): return
    sid = j["salesorder"]["salesorder_id"]
    for step in ("submit", "approve", "status/confirmed"): Z.post(f"/salesorders/{sid}/{step}")
    so = Z.get(f"/salesorders/{sid}")["salesorder"]
    li = [{"item_id": ITEM_A, "quantity": l["quantity"], "rate": 10, "salesorder_item_id": l["line_item_id"]} for l in so["line_items"]]
    inv = Z.post("/invoices", {"customer_id": CUST, "date": TODAY, "reference_number": "ZZMATRIX L22", "salesperson_id": SALESPERSON, "line_items": li}, salesorder_id=sid)
    if not rec("L22", "invoice both lines", "OK", inv): return
    approve_inv(inv["invoice"]["invoice_id"])
    def ok():
        s_ = Z.get(f"/salesorders/{sid}")["salesorder"]
        return all(float(l.get("quantity_shipped", 0)) == float(l["quantity"]) for l in s_["line_items"]) and s_
    res, secs = wait_for("both lines", ok); report("L22", "both lines shipped (3 and 4)", res, secs)
    s_, L, pk, rets, cm = so_state(sid); rec("L22", "state", "lines 3/3/3 and 4/4/4, 1 package", {"code": 0}, " | ".join(qty_str(l) for l in s_["line_items"]) + f" packages {[(p['number'], p['lines']) for p in pk]} alerts {cm}")

def L23():
    """Credit note WITH item lines on a shipped invoice (second function), then the credit note voided."""
    before = item_stock(ITEM_A)
    so = mk_so("L23", [(ITEM_A, 6)]); inv = need(mk_inv("L23", so, [(ITEM_A, 6)]), "invoice")
    res, secs = wait_for("mirror", lambda: mirror_ok(so["salesorder_id"], {ITEM_A: 6})); report("L23", "6 shipped", res, secs)
    shipped = item_stock(ITEM_A)
    iv = Z.get(f"/invoices/{inv['invoice']['invoice_id']}")["invoice"]
    cn = Z.post("/creditnotes", {"customer_id": CUST, "date": TODAY, "reference_number": "ZZMATRIX L23", "salesperson_id": SALESPERSON, **({"place_of_supply": iv["place_of_supply"]} if iv.get("place_of_supply") else {}),
                                 "line_items": [{"item_id": ITEM_A, "quantity": 2, "rate": 10}]}, invoice_id=iv["invoice_id"])
    if not rec("L23", "credit note 2 of A against the invoice", "OK", cn): return
    cnid = cn["creditnote"]["creditnote_id"]
    for step in ("submit", "approve"):
        r = Z.post(f"/creditnotes/{cnid}/{step}")
        if r.get("code") != 0: log("cn", step, r.get("message"))
    res, secs = wait_for("return 2", lambda: returned_ok(so["salesorder_id"], {ITEM_A: 2})); report("L23", "sales return 2 received after credit note", res, secs)
    so2, L, pk, rets, cm = so_state(so["salesorder_id"]); rec("L23", "return details", "1 return, marker cn:, received", {"code": 0}, str(rets) + f" alerts {cm}")
    rec("L23", "item A stock before / shipped / after credit note", "physical back by 2", {"code": 0}, f"{before} / {shipped} / {item_stock(ITEM_A)}")
    v = Z.post(f"/creditnotes/{cnid}/status/void"); rec("L23", "void the credit note", "OK", v)
    res, secs = wait_for("return removed", lambda: returned_ok(so["salesorder_id"], {ITEM_A: 0})); report("L23", "return removed after credit note void", res, secs)
    so3, L, pk, rets, cm = so_state(so["salesorder_id"]); rec("L23", "after void", "ret 0, 0 returns", {"code": 0}, qty_str(L[ITEM_A]) + f" returns {len(rets)} alerts {cm} stock {item_stock(ITEM_A)}")


def L24():
    """Sales order A 10, invoice A for 20 on the one line (over invoice) through the Books door."""
    so = mk_so("L24", [(ITEM_A, 10)]); inv = mk_inv("L24", so, [(ITEM_A, 20)])
    ok = rec("L24", "invoice 20 on a sales order line of 10", "Zoho refuses, or if allowed automation packs only what the SO has room for and alerts", inv)
    if ok:
        time.sleep(90); so2, L, pk, rets, cm = so_state(so["salesorder_id"])
        rec("L24", "state", "packed at most 10, alert for the rest", {"code": 0}, qty_str(L[ITEM_A]) + f" packages {[(p['number'], sum(p['lines'].values())) for p in pk]} alerts {cm}")

def L25():
    """Sales order A 10, invoice has item A twice, 11 and 9, both lines linked to the same sales order line."""
    so = mk_so("L25", [(ITEM_A, 10)]); lid = so["line_items"][0]["line_item_id"]
    li = [{"item_id": ITEM_A, "quantity": 11, "rate": 10, "salesorder_item_id": lid}, {"item_id": ITEM_A, "quantity": 9, "rate": 10, "salesorder_item_id": lid}]
    j = Z.post("/invoices", {"customer_id": CUST, "date": TODAY, "reference_number": "ZZMATRIX L25", "salesperson_id": SALESPERSON, "line_items": li}, salesorder_id=so["salesorder_id"])
    ok = rec("L25", "invoice A twice (11 + 9) on one SO line of 10", "Zoho refuses, or automation packs 10 and alerts", j)
    if ok:
        approve_inv(j["invoice"]["invoice_id"]); time.sleep(90); so2, L, pk, rets, cm = so_state(so["salesorder_id"])
        rec("L25", "state", "packed at most 10", {"code": 0}, qty_str(L[ITEM_A]) + f" packages {[(p['number'], sum(p['lines'].values())) for p in pk]} alerts {cm}")

def L26():
    """Sales order A 10, invoice has A 10 linked to the SO plus a second A 10 line NOT linked (the way a human adds a second line). Then void."""
    before = item_stock(ITEM_A)
    so = mk_so("L26", [(ITEM_A, 10)]); inv = mk_inv("L26", so, [(ITEM_A, 10)], extra=[(ITEM_A, 10)])
    ok = rec("L26", "invoice A 10 (SO) + A 10 (extra, no SO link)", "allowed", inv)
    if not ok: return
    res, secs = wait_for("mirror", lambda: mirror_ok(so["salesorder_id"], {ITEM_A: 10})); report("L26", "SO line shipped 10, extra 10 not packed", res, secs)
    mid = item_stock(ITEM_A)
    so2, L, pk, rets, cm = so_state(so["salesorder_id"]); rec("L26", "after invoice", "A 10/10/10, 1 package of 10, no alert", {"code": 0 if len(pk) == 1 and not cm else -1, "message": f"{len(pk)} packages alerts {cm}"}, qty_str(L[ITEM_A]) + f" packages {[(p['number'], sum(p['lines'].values())) for p in pk]}")
    rec("L26", "item A stock before / after", "accounting down 20 (Zoho), physical down 20 (10 by Zoho for the extra line, 10 by the automation)", {"code": 0}, f"{before} / {mid}")
    rec("L26", "void invoice", "OK", void_inv(inv["invoice"]["invoice_id"]))
    res, secs = wait_for("return", lambda: returned_ok(so["salesorder_id"], {ITEM_A: 10})); report("L26", "return 10 for the SO line after void", res, secs)
    so3, L, pk, rets, cm = so_state(so["salesorder_id"]); rec("L26", "after void", "ret 10, stock back to before", {"code": 0}, qty_str(L[ITEM_A]) + f" returns {len(rets)} alerts {cm} stock {item_stock(ITEM_A)} (before {before})")

def L99():
    """Probe (no records made): which filters the packages LIST accepts, and what each list row carries. Needed for the L16 fix."""
    cust = CUST
    for name, params in [("customer_id + date_start/date_end", {"customer_id": cust, "date_start": TODAY, "date_end": TODAY, "per_page": 200}),
                         ("customer_id + date", {"customer_id": cust, "date": TODAY, "per_page": 200}),
                         ("customer_id only", {"customer_id": cust, "per_page": 5, "sort_column": "date", "sort_order": "D"}),
                         ("date_after yesterday", {"date_after": (dt.date.today() - dt.timedelta(days=1)).isoformat(), "per_page": 5}),
                         ("filter_by Status.Delivered + customer_id", {"customer_id": cust, "filter_by": "Status.Delivered", "per_page": 5})]:
        j = Z.get("/packages", **params)
        rows = j.get("packages", [])
        rec("L99", f"packages list: {name}", "OK", j, f"{len(rows)} rows; " + (f"row keys {sorted(rows[0].keys())}; first {rows[0].get('package_number')} {rows[0].get('date')} cust {rows[0].get('customer_name')} so {rows[0].get('salesorder_number')} notes={'notes' in rows[0]} ship={rows[0].get('shipment_id')}" if rows else ""))
    j = Z.get("/invoices", customer_id=cust, per_page=3, sort_column="date", sort_order="D")
    rows = j.get("invoices", [])
    rec("L99", "invoices list keys (for later use)", "OK", j, f"{len(rows)} rows; keys {sorted(rows[0].keys()) if rows else ''}")

CASES = {n: f for n, f in globals().items() if n[:1] in ("T", "L") and n[1:].isdigit()}

def cleanup():
    """Void every invoice and sales order of BOTH ZZ TEST customers (robot runs, Adil's hand tests, everything).
    Live customer voids fire the automation, which brings the shipped goods back with received sales returns.
    Shipments and returns stay as history. Prints the ZZ TEST item stock at the end."""
    cust2 = customer2()
    custs = {CUST_LIVE: "ZZ TEST CUSTOMER", cust2: "ZZ TEST CUSTOMER 2"}
    done = {"invoices": [], "salesorders": []}; refused = []
    for path, key in (("/invoices", "invoices"), ("/salesorders", "salesorders")):
        for cid, cname in custs.items():
            page = 1
            while True:
                j = Z.get(path, customer_id=cid, per_page=200, page=page, sort_column="created_time", sort_order="D")
                rows = j.get(key, [])
                for r in rows:
                    num = r.get("invoice_number") or r.get("salesorder_number"); st = r.get("status")
                    if st in ("void", "closed", "cancelled"): continue
                    idk = "invoice_id" if key == "invoices" else "salesorder_id"
                    if st == "draft":
                        v = Z.delete(f"{path}/{r[idk]}")
                        if v.get("code") == 0: done[key].append(num + " (draft deleted)"); log("deleted draft", num); continue
                    v = Z.post(f"{path}/{r[idk]}/status/void")
                    if v.get("code") == 0: done[key].append(num); log("void", num)
                    else: refused.append((key, num, cname, st, v.get("message"))); log("REFUSED", key, num, st, v.get("message"))
                    if key == "invoices" and cid == CUST_LIVE: time.sleep(3)   # give the automation room: each void makes a return + receive
                if not j.get("page_context", {}).get("has_more_page"): break
                page += 1
    log(f"\nvoided {len(done['invoices'])} invoices, {len(done['salesorders'])} sales orders; refused {len(refused)}")
    for x in refused: log("  refused:", x)
    time.sleep(90)   # let the last automation runs finish before reading stock
    for name, iid in (("A", ITEM_A), ("B", ITEM_B), ("C", ITEM_C)):
        log(f"ZZ TEST ITEM {name}: {item_stock(iid)}")
    RESULTS.append({"case": "CLEANUP", "step": "voided", "expected": "", "zoho": f"{len(done['invoices'])} invoices, {len(done['salesorders'])} SOs", "note": "; ".join(f"{k} {n}: {m}" for k, n, c, st, m in refused)})

def deep_cleanup():
    """CLEANUP=2. For the two ZZ TEST customers only: remove what blocks a void (payments, credit note credits,
    packages, shipments, sales returns and their receives), then void every remaining invoice and sales order.
    Test data only. Prints per-warehouse stock of the ZZ TEST items at the end so Tarun can decide the final adjustment."""
    cust2 = customer2()
    custs = {CUST_LIVE: "ZZ TEST CUSTOMER", cust2: "ZZ TEST CUSTOMER 2"}
    acts = []; fails = []
    def act(label, resp):
        ok = resp.get("code") == 0
        (acts if ok else fails).append(f"{label}: {'OK' if ok else resp.get('message')}"); log(("OK " if ok else "FAIL ") + label, "" if ok else resp.get("message"))
        return ok
    # 1. credit notes of the test customers: un-apply credits, then delete (or void)
    for cid, cname in custs.items():
        j = Z.get("/creditnotes", customer_id=cid, per_page=200)
        for cn in j.get("creditnotes", []):
            if cn.get("status") == "void": continue
            full = Z.get(f"/creditnotes/{cn['creditnote_id']}").get("creditnote", {})
            for ci in full.get("invoices_credited", []) or full.get("invoices", []):
                cii = ci.get("creditnote_invoice_id")
                if cii: act(f"unapply {cn['creditnote_number']} from {ci.get('invoice_number')}", Z.delete(f"/creditnotes/{cn['creditnote_id']}/invoices/{cii}"))
            for rf in full.get("refunds", []) or []:
                act(f"delete refund on {cn['creditnote_number']}", Z.delete(f"/creditnotes/{cn['creditnote_id']}/refunds/{rf.get('creditnote_refund_id')}"))
            d = Z.delete(f"/creditnotes/{cn['creditnote_id']}")
            if not act(f"delete credit note {cn['creditnote_number']}", d):
                act(f"void credit note {cn['creditnote_number']}", Z.post(f"/creditnotes/{cn['creditnote_id']}/status/void"))
    # 2. invoices: delete payments, then void
    for cid, cname in custs.items():
        page = 1
        while True:
            j = Z.get("/invoices", customer_id=cid, per_page=200, page=page)
            for r in j.get("invoices", []):
                if r.get("status") in ("void",): continue
                full = Z.get(f"/invoices/{r['invoice_id']}").get("invoice", {})
                for pm in full.get("payments", []) or []:
                    pid = pm.get("payment_id")
                    if pid: act(f"delete payment on {r['invoice_number']}", Z.delete(f"/customerpayments/{pid}"))
                for cr in full.get("credits", []) or []:
                    cii = cr.get("creditnote_invoice_id") or cr.get("creditnotes_invoice_id")
                    if cii: act(f"unapply credit on {r['invoice_number']}", Z.delete(f"/creditnotes/{cr.get('creditnote_id')}/invoices/{cii}"))
                if r.get("status") == "draft":
                    act(f"delete draft {r['invoice_number']}", Z.delete(f"/invoices/{r['invoice_id']}")); continue
                act(f"void {r['invoice_number']}", Z.post(f"/invoices/{r['invoice_id']}/status/void"))
            if not j.get("page_context", {}).get("has_more_page"): break
            page += 1
    # 3. sales orders: delete receives, returns, shipments, packages, then void
    for cid, cname in custs.items():
        page = 1
        while True:
            j = Z.get("/salesorders", customer_id=cid, per_page=200, page=page)
            for r in j.get("salesorders", []):
                if r.get("status") == "void": continue
                so = Z.get(f"/salesorders/{r['salesorder_id']}").get("salesorder", {})
                for sr in so.get("salesreturns", []) or []:
                    full = Z.get(f"/salesreturns/{sr['salesreturn_id']}").get("salesreturn", {})
                    for rc in full.get("salesreturnreceives", []) or []:
                        act(f"delete receive on {full.get('salesreturn_number')}", Z.delete(f"/salesreturnreceives/{rc.get('receive_id')}"))
                    act(f"delete return {full.get('salesreturn_number')}", Z.delete(f"/salesreturns/{sr['salesreturn_id']}"))
                for pk in so.get("packages", []) or []:
                    full = Z.get(f"/packages/{pk['package_id']}").get("package", {})
                    sh = full.get("shipment_id")
                    if sh: act(f"delete shipment of {full.get('package_number')}", Z.delete(f"/shipmentorders/{sh}"))
                    act(f"delete package {full.get('package_number')}", Z.delete(f"/packages/{pk['package_id']}"))
                if r.get("status") == "draft":
                    act(f"delete draft SO {r['salesorder_number']}", Z.delete(f"/salesorders/{r['salesorder_id']}")); continue
                act(f"void SO {r['salesorder_number']}", Z.post(f"/salesorders/{r['salesorder_id']}/status/void"))
            if not j.get("page_context", {}).get("has_more_page"): break
            page += 1
    log(f"\nDEEP CLEANUP: {len(acts)} actions OK, {len(fails)} failed")
    for f in fails: log("  FAIL", f)
    time.sleep(20)
    for name, iid in (("A", ITEM_A), ("B", ITEM_B), ("C", ITEM_C)):
        it = Z.get(f"/items/{iid}").get("item", {})
        locs = [(l.get("location_name") or l.get("warehouse_name"), l.get("location_stock_on_hand", l.get("warehouse_stock_on_hand")), l.get("location_actual_available_stock", l.get("warehouse_actual_available_stock"))) for l in (it.get("locations") or it.get("warehouses") or [])]
        log(f"ZZ TEST ITEM {name}: total acct {it.get('stock_on_hand')} phys {it.get('actual_available_stock')}; per warehouse (name, acct, phys): {locs}")
    RESULTS.append({"case": "DEEP CLEANUP", "step": "done", "expected": "", "zoho": f"{len(acts)} OK, {len(fails)} failed", "note": "; ".join(fails)[:1500]})


def L98():
    """Probe (read only): how many items in the org are marked NOT returnable, with a sample of names. For Tarun's 'mark all returnable' request."""
    page = 1; total = 0; notret = []; inactive = 0
    while True:
        j = Z.get("/items", per_page=200, page=page)
        if j.get("code") != 0: rec("L98", "items list", "OK", j); return
        for it in j.get("items", []):
            total += 1
            if it.get("status") != "active": inactive += 1
            if it.get("is_returnable") is False and it.get("item_type") == "inventory": notret.append((it.get("name"), it.get("sku"), it.get("status")))
        if not j.get("page_context", {}).get("has_more_page"): break
        page += 1
    rec("L98", "items scanned", "OK", {"code": 0}, f"{total} items ({inactive} inactive); NOT returnable inventory items: {len(notret)}; first 15: {notret[:15]}")

CASES["L98"] = L98

def main():
    global CUST, USE_BOOKS_FOR_INVOICES
    if os.environ.get("CLEANUP") == "1": cleanup(); return
    if os.environ.get("CLEANUP") == "2": deep_cleanup(); return
    cust2 = customer2(); log("customer 2:", cust2)
    for name in sorted(CASES):
        if ONLY and name not in ONLY: continue
        CUST = CUST_LIVE if name.startswith("L") else cust2   # L cases run on ZZ TEST CUSTOMER where the automation fires
        USE_BOOKS_FOR_INVOICES = name.startswith("L")
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
