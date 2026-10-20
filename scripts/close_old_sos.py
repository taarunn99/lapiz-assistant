#!/usr/bin/env python3
"""v2 (20 Oct 2026 evening): dropped warehouse_id, zero lines are removed instead of set to 0, picked quantity respected.
Step 2: close old open sales orders in Zoho Books (Dubai org) to release committed stock.

Two groups, both older than CUTOFF (default 2026-08-20):
  A  status open, nothing invoiced / packed / shipped        -> mark as void
  B  partially invoiced, shipped >= invoiced, ordered > invoiced -> reduce every line to the invoiced quantity,
     Zoho then closes the order by itself. Lines with 0 invoiced are set to 0.

Modes:  dry  = read everything, print what would happen, change nothing (default)
        live = do it. Every change is logged with the SO number to out/close_log.txt and out/close_done.json.

Env (same secrets as the backfill job): ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN, ZOHO_ORG_ID_DXB
Optional: CUTOFF (YYYY-MM-DD), GROUPS (A, B or AB), SO_NUMBERS (comma list to restrict), MAX_CALLS, RATE_PER_MIN,
          SKIP_FILE (path to a text file with SO numbers to leave alone, one per line)
"""
import os, sys, json, time, urllib.request, urllib.parse, urllib.error, datetime

MODE = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("MODE", "dry")).lower()
CUTOFF = os.environ.get("CUTOFF") or "2026-08-20"
GROUPS = (os.environ.get("GROUPS") or "AB").upper()
ONLY = {s.strip() for s in (os.environ.get("SO_NUMBERS") or "").split(",") if s.strip()}
MAX_CALLS = int(os.environ.get("MAX_CALLS") or 4000)
RATE = float(os.environ.get("RATE_PER_MIN") or 40)
SKIP = set()
if os.environ.get("SKIP_FILE") and os.path.exists(os.environ["SKIP_FILE"]):
    SKIP = {l.strip() for l in open(os.environ["SKIP_FILE"]) if l.strip()}
ORG = os.environ["ZOHO_ORG_ID_DXB"]
BASE = "https://www.zohoapis.com/books/v3"
os.makedirs("out", exist_ok=True)
LOG = open("out/close_log.txt", "a")

def log(*a):
    line = " ".join(str(x) for x in a)
    print(line, flush=True); LOG.write(line + "\n"); LOG.flush()

# ---- auth + paced client -------------------------------------------------------------------
_tok = {"v": None, "t": 0}
def token():
    if _tok["v"] and time.time() - _tok["t"] < 3000: return _tok["v"]
    data = urllib.parse.urlencode({"refresh_token": os.environ["ZOHO_REFRESH_TOKEN"], "client_id": os.environ["ZOHO_CLIENT_ID"],
                                   "client_secret": os.environ["ZOHO_CLIENT_SECRET"], "grant_type": "refresh_token"}).encode()
    j = json.load(urllib.request.urlopen("https://accounts.zoho.com/oauth/v2/token", data, timeout=60))
    _tok["v"] = j["access_token"]; _tok["t"] = time.time(); return _tok["v"]

calls = 0; _last = 0.0
def call(method, path, params=None, body=None):
    global calls, _last
    if calls >= MAX_CALLS: raise SystemExit(f"MAX_CALLS {MAX_CALLS} reached, stopping cleanly")
    gap = 60.0 / RATE; wait = _last + gap - time.time()
    if wait > 0: time.sleep(wait)
    q = dict(params or {}); q["organization_id"] = ORG
    url = f"{BASE}{path}?{urllib.parse.urlencode(q)}"
    data = None; headers = {"Authorization": "Zoho-oauthtoken " + token()}
    if body is not None:
        data = json.dumps(body).encode(); headers["Content-Type"] = "application/json"
    for attempt in range(4):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                _last = time.time(); calls += 1
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            txt = e.read().decode(errors="replace"); _last = time.time(); calls += 1
            try: j = json.loads(txt)
            except Exception: j = {"code": e.code, "message": txt[:300]}
            if j.get("code") == 44 or e.code == 429:          # rate limit: back off a full minute
                log("   rate limited, sleeping 65 s"); time.sleep(65); continue
            if e.code >= 500 and attempt < 3: time.sleep(10); continue
            return j
        except Exception as ex:
            if attempt < 3: time.sleep(10); continue
            return {"code": -1, "message": str(ex)}
    return {"code": -1, "message": "gave up"}

# ---- 1. list candidate orders -------------------------------------------------------------
def list_sos(status):
    out = []; page = 1
    while True:
        j = call("GET", "/salesorders", {"filter_by": f"Status.{status}", "per_page": 200, "page": page,
                                         "sort_column": "date", "sort_order": "A"})
        if j.get("code") != 0: log("list failed", status, j.get("message")); break
        rows = j.get("salesorders", []); out.extend(rows)
        if not j.get("page_context", {}).get("has_more_page") or (rows and rows[-1]["date"] > CUTOFF): break
        page += 1
    return [s for s in out if s["date"] <= CUTOFF]

cands = []
if "A" in GROUPS: cands += [("A", s) for s in list_sos("Open")]
if "B" in GROUPS: cands += [("B", s) for s in list_sos("PartiallyInvoiced")]
if ONLY: cands = [(g, s) for g, s in cands if s["salesorder_number"] in ONLY]
cands = [(g, s) for g, s in cands if s["salesorder_number"] not in SKIP]
log(f"{MODE} run, cutoff {CUTOFF}, groups {GROUPS}: {len(cands)} candidate orders "
    f"(A {sum(1 for g,_ in cands if g=='A')}, B {sum(1 for g,_ in cands if g=='B')}), skip list {len(SKIP)}")

# ---- 2. check each one live and act --------------------------------------------------------
done = []; skipped = []
KEEP = ("line_item_id", "item_id", "name", "description", "rate", "quantity", "unit", "discount", "tax_id",
        "item_order", "location_id", "project_id", "tags", "item_custom_fields", "hsn_or_sac")
for grp, s in cands:
    so_no = s["salesorder_number"]
    j = call("GET", f"/salesorders/{s['salesorder_id']}")
    if j.get("code") != 0: log(so_no, "fetch failed:", j.get("message")); skipped.append(so_no); continue
    so = j["salesorder"]
    if so.get("status") in ("void", "closed", "draft") or so["date"] > CUTOFF:
        log(so_no, "skip: now", so.get("status")); skipped.append(so_no); continue
    if so.get("is_dropshipped") or so.get("is_drop_shipment") or so.get("picklists"):
        log(so_no, "skip: drop ship or picklist, needs a human"); skipped.append(so_no); continue
    L = so["line_items"]
    inv = sum(float(l.get("quantity_invoiced", 0) or 0) for l in L)
    shp = sum(float(l.get("quantity_shipped", 0) or 0) for l in L)
    pk = sum(float(l.get("quantity_packed", 0) or 0) for l in L)
    qty = sum(float(l["quantity"]) for l in L)
    if grp == "A":
        if inv or shp or pk:
            log(so_no, f"skip: not clean any more (inv {inv} shp {shp} pk {pk})"); skipped.append(so_no); continue
        if MODE == "live":
            r = call("POST", f"/salesorders/{so['salesorder_id']}/status/void")
            ok = r.get("code") == 0
            log(so_no, so["date"], so["customer_name"][:30], f"VOID {'ok' if ok else 'FAILED: ' + str(r.get('message'))}", f"released {qty:g} units")
            if ok: done.append({"so": so_no, "action": "void", "units": qty})
        else:
            log(so_no, so["date"], so["customer_name"][:30], f"would VOID, releases {qty:g} units")
    else:  # B
        new_lines = []; released = 0.0; bad = False
        for l in L:
            li = float(l.get("quantity_invoiced", 0) or 0); ls = float(l.get("quantity_shipped", 0) or 0)
            lp = float(l.get("quantity_packed", 0) or 0); lq = float(l["quantity"])
            lpk = float(l.get("quantity_picked", l.get("picked_quantity", 0)) or 0)
            target = max(li, ls, lp, lpk)                  # Zoho will not go below what is already shipped, packed or picked
            if target > lq: bad = True
            released += lq - target
            if target <= 0:
                continue                                   # nothing ever happened on this line: drop it (Zoho refuses zero quantity lines)
            nl = {k: l[k] for k in KEEP if k in l and l[k] not in (None, "", [])}
            nl["quantity"] = target
            new_lines.append(nl)
        if bad or released <= 0 or not new_lines:
            log(so_no, "skip: nothing to reduce"); skipped.append(so_no); continue
        if MODE == "live":
            body = {"customer_id": so["customer_id"], "line_items": new_lines}
            r = call("PUT", f"/salesorders/{so['salesorder_id']}", {"ignore_auto_number_generation": "true"}, body)
            ok = r.get("code") == 0
            st = r.get("salesorder", {}).get("status", "?") if ok else ""
            log(so_no, so["date"], so["customer_name"][:30], f"REDUCE {'ok, now ' + st if ok else 'FAILED: ' + str(r.get('message'))}", f"released {released:g} units")
            if ok: done.append({"so": so_no, "action": "reduce", "units": released, "status_after": st})
        else:
            log(so_no, so["date"], so["customer_name"][:30], f"would REDUCE lines to invoiced, releases {released:g} units")

json.dump({"mode": MODE, "cutoff": CUTOFF, "done": done, "skipped": skipped, "calls": calls,
           "run_at": datetime.datetime.utcnow().isoformat()}, open("out/close_done.json", "w"), indent=1)
log(f"finished: {len(done)} changed, {len(skipped)} skipped, {sum(d['units'] for d in done):g} units released, calls {calls}")
