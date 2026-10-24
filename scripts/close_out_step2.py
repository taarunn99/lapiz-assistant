#!/usr/bin/env python3
"""Step 2 close out, v3 (22 Oct 2026, 16:00): make physical stock follow accounting stock on old open sales orders, then close them.
v3 changes: counts quantity already returned; Sharjah location on returns; ships invoiced-not-shipped lines through the backfill (heal on);
voids drop ships that have no invoice; when Zoho will not reduce a line, closes the order with a zero value invoice plus a credit note
against it (ZERO_CLOSE=1, default on).
v3.19 (24 Oct 2026, 15:40): SHIPRET skips orders that are not fully invoiced (they would stay open anyway) and honours the skip file.
v3.18 (24 Oct 2026, 14:45): zero invoice path OFF by default; sales returns fall back to the order header location when the line location is inactive (SOSJ26-00276).
v3.17 (24 Oct 2026): CNRET respects the cutoff date (SOSJ26-00441 of 8 Sep 2026 was being picked up); SHIPRET mode added in v3.16.
v3.2 (22 Oct 18:00): zero invoice quantity ignores voided invoices (quantity_invoiced counts them, Zoho's "left to invoice" does not);
zero invoices carry the order's salesperson, or a default one, because this org refuses invoices without a salesperson.

Rule agreed with Tarun and his manager on 22 Oct 2026: accounting stock is correct, physical must follow it.
For every open or partially invoiced sales order dated on or before CUTOFF (default 2026-08-20):
  1. if the customer is BLACKLISTED, untick the flag (and always tick it back at the end, even on error)
  2. if the order has picklists and DELETE_PICKLISTS=1, delete them (a picklist is a warehouse instruction, no stock, no money)
  3. per inventory line: live invoiced = quantity_invoiced minus quantity on VOID invoices of this order.
       out = shipped + manually fulfilled.
       if out > live invoiced  -> sales return for the difference, then receive it (physical goes up to meet accounting)
       if live invoiced > out  -> left for the daily backfill job (it ships invoiced quantity); logged, not touched here
  4. re-read the order. If nothing was ever invoiced, packed or shipped -> void.
       Otherwise reduce every line to max(live invoiced, shipped, packed, picked) so Zoho closes it. Zero lines are removed.
  5. tick BLACKLISTED back on.
Modes: dry (read only, prints what would happen) | live.
Env: ZOHO_* secrets (same as backfill), CUTOFF, SO_NUMBERS, MAX_CALLS, RATE_PER_MIN, DELETE_PICKLISTS (1/0), SKIP_FILE.
Everything is logged by order number to out/closeout_log.txt and out/closeout_done.json.
"""
import json, os, sys, time, datetime as dt
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
os.environ.setdefault("SO_NUMBERS", "")
import backfill_shipments as bf          # throttled Zoho Inventory client (Adil's token), same as every other job

MODE = (sys.argv[1] if len(sys.argv) > 1 else "dry").lower()
Z = bf.Z; OUT = bf.OUT
CUTOFF = os.environ.get("CUTOFF") or "2026-08-20"
ONLY = {s.strip() for s in os.environ.get("SO_NUMBERS", "").split(",") if s.strip()}
DEL_PICK = os.environ.get("DELETE_PICKLISTS", "0") == "1"
FIELD_ID = "2305879000017302163"         # contact custom field BLACKLISTED
SKIP = set()
sf = os.environ.get("SKIP_FILE", "")
if sf and os.path.exists(sf): SKIP = {l.strip() for l in open(sf) if l.strip() and not l.startswith("#")}
LOGF = open(f"{OUT}/closeout_log.txt", "a")
def log(line):
    print(line, flush=True); LOGF.write(f"{dt.datetime.utcnow():%Y-%m-%d %H:%M:%S}Z {line}\n"); LOGF.flush()
TODAY = dt.date.today().isoformat()
done = []; refused = []; unrestored = []

def read_flag(cid):
    j = Z.get(f"/contacts/{cid}")
    if j.get("code") != 0: return None, ""
    c = j["contact"]
    for f in c.get("custom_fields", []):
        if f.get("customfield_id") == FIELD_ID or f.get("api_name") == "cf_blacklisted":
            v = f.get("value"); return (v is True or str(v).lower() == "true"), c["contact_name"]
    return False, c["contact_name"]
def set_flag(cid, value):
    j = Z.req("PUT", f"/contacts/{cid}", json={"custom_fields": [{"customfield_id": FIELD_ID, "value": bool(value)}]})
    return j.get("code") == 0, j.get("message")
def restore(cid, name):
    bf.MAX_CALLS = max(bf.MAX_CALLS, Z.calls + 40)
    for attempt in range(1, 6):
        ok, msg = set_flag(cid, True)
        if ok and read_flag(cid)[0]: log(f"   {name}: BLACKLISTED ticked back on (confirmed, attempt {attempt})"); return True
        log(f"   {name}: re-tick attempt {attempt} failed: {msg}"); time.sleep(5 * attempt)
    log(f"   !!!! {name}: BLACKLISTED COULD NOT BE RESTORED. Fix by hand: Customers > {name} > Edit > tick BLACKLISTED")
    unrestored.append({"customer_id": cid, "customer": name}); return False

def list_sos():
    out = []
    for st in ("Open", "PartiallyInvoiced"):
        page = 1
        while True:
            j = Z.get("/salesorders", filter_by=f"Status.{st}", per_page=200, page=page, sort_column="date", sort_order="A")
            if j.get("code") != 0: log(f"list failed {st}: {j.get('message')}"); break
            rows = j.get("salesorders", []); out += [r for r in rows if r["date"] <= CUTOFF]
            if not j.get("page_context", {}).get("has_more_page") or (rows and rows[-1]["date"] > CUTOFF): break
            page += 1
    seen = set(); uniq = []
    for r in out:
        if r["salesorder_id"] in seen: continue
        seen.add(r["salesorder_id"]); uniq.append(r)
    return uniq

def void_qty_by_item(so):
    v = {}
    for inv in so.get("invoices", []):
        if inv.get("status") != "void": continue
        iv = bf.get_invoice(inv["invoice_id"]) or {}
        for li in iv.get("line_items", []):
            v[li.get("item_id")] = v.get(li.get("item_id"), 0) + float(li.get("quantity") or 0)
    return v

KEEP = ("line_item_id", "item_id", "name", "description", "rate", "quantity", "unit", "discount", "tax_id",
        "item_order", "location_id", "project_id", "tags", "item_custom_fields", "hsn_or_sac")

def handle(row):
    so_no = row["salesorder_number"]
    j = Z.get(f"/salesorders/{row['salesorder_id']}")
    if j.get("code") != 0: log(f"{so_no} fetch failed: {j.get('message')}"); refused.append([so_no, "fetch failed"]); return
    so = j["salesorder"]
    if so.get("status") in ("void", "closed", "draft") or (so.get("status") == "invoiced" and so.get("shipped_status") in ("fulfilled", "shipped")):
        log(f"{so_no} skip: now {so.get('status')}/{so.get('shipped_status')}"); return
    dropship = bool(so.get("is_dropshipped") or so.get("is_drop_shipment"))
    cust = so["customer_name"]; cid = so["customer_id"]
    L = [l for l in so["line_items"] if l.get("item_type") == "inventory"]
    voids = void_qty_by_item(so)
    rets = []; ship_needed = []; summary = []
    for l in L:
        qi = float(l.get("quantity_invoiced") or 0); v = min(voids.get(l["item_id"], 0), qi); voids[l["item_id"]] = voids.get(l["item_id"], 0) - v
        live = qi - v; out = float(l.get("quantity_shipped") or 0) + float(l.get("quantity_manuallyfulfilled") or 0) - float(l.get("quantity_returned") or 0)
        if out > live + 1e-6: rets.append((l, out - live)); summary.append(f"{l['name'][:25]}: out {out:g} after returns, invoiced {live:g}, return {out - live:g}")
        elif live > out + float(l.get("quantity_returned") or 0) + 1e-6:
            gapq = live - out - float(l.get("quantity_returned") or 0)
            ship_needed.append((l, gapq)); summary.append(f"{l['name'][:25]}: invoiced {live:g}, shipped {out + float(l.get('quantity_returned') or 0):g}, backfill must ship {gapq:g}")
    nothing = all(float(l.get("quantity_invoiced") or 0) == 0 and float(l.get("quantity_shipped") or 0) == 0 and float(l.get("quantity_packed") or 0) == 0 and float(l.get("quantity_manuallyfulfilled") or 0) == 0 for l in L)
    picks = so.get("picklists") or []
    flag, _ = read_flag(cid)
    plan = []
    if flag: plan.append("unblacklist")
    if picks: plan.append(f"delete {len(picks)} picklist(s)" if DEL_PICK else f"HAS {len(picks)} PICKLIST(S), set DELETE_PICKLISTS to clear")
    if rets: plan.append(f"sales return + receive {sum(q for _, q in rets):g} units")
    if ship_needed: plan.append(f"ship {sum(q for _, q in ship_needed):g} invoiced units (backfill)")
    if dropship: plan.append("DROP SHIP: void if nothing invoiced, else leave")
    plan.append("void" if nothing else "reduce lines, or zero invoice + credit note if Zoho refuses")
    if flag: plan.append("re-blacklist")
    log(f"{so_no} {so['date']} {cust[:30]} [{so['status']}/{so.get('shipped_status')}] -> {'; '.join(plan)}  | {' / '.join(summary)}")
    if MODE != "live": return
    unticked = False; reactivated = []; relocated = []
    try:
        if rets:
            for l, q in rets:
                it = Z.get(f"/items/{l['item_id']}").get("item", {})
                if it.get("status") == "inactive":
                    r = Z.post(f"/items/{l['item_id']}/active")
                    if r.get("code") == 0: reactivated.append((l["item_id"], l["name"])); log(f"   item {l['name'][:30]} was inactive, set active for the return")
                    else: log(f"   could not activate item {l['name'][:30]}: {r.get('message')}")
        if flag:
            ok, msg = set_flag(cid, False)
            if not ok or read_flag(cid)[0]: log(f"   could not untick BLACKLISTED ({msg}); skipped"); refused.append([so_no, "untick failed"]); return
            unticked = True; log("   BLACKLISTED unticked (confirmed)")
        if picks and DEL_PICK:
            for p in picks:
                r = Z.req("DELETE", f"/picklists/{p.get('picklist_id')}")
                log(f"   picklist {p.get('picklist_number', p.get('picklist_id'))} delete: {'ok' if r.get('code') == 0 else 'FAILED ' + str(r.get('message'))}")
        elif picks:
            log("   picklist present and DELETE_PICKLISTS off; stopping here for this order"); refused.append([so_no, "picklist"]); return
        if rets:
            # an earlier attempt may have created the return without receiving it: receive that one instead of making a second
            ex = Z.get("/salesreturns", salesorder_id=so["salesorder_id"]).get("salesreturns", [])
            ex = [e for e in ex if str(e.get("salesorder_id")) == str(so["salesorder_id"]) and float(e.get("quantity_yet_to_receive") or 0) > 0 and e.get("salesreturn_status") != "void"]
            if ex:
                e = ex[0]; srd = Z.get(f"/salesreturns/{e['salesreturn_id']}").get("salesreturn", {})
                rl = [{"line_item_id": li["line_item_id"], "quantity": float(li["quantity"]) - float(li.get("quantity_received") or 0)} for li in srd.get("line_items", [])]
                rl = [x for x in rl if x["quantity"] > 0]
                r2 = Z.post("/salesreturnreceives", {"date": TODAY, "line_items": rl, "notes": "Stock sync 22 Oct 2026"}, salesreturn_id=e["salesreturn_id"])
                if r2.get("code") != 0: log(f"   receive of existing {e['salesreturn_number']} FAILED: {r2.get('message')}"); refused.append([so_no, "receive: " + str(r2.get("message"))]); return
                q = sum(x["quantity"] for x in rl); log(f"   existing return {e['salesreturn_number']} received, physical stock up by {q:g}")
                done.append({"so": so_no, "action": "receive existing return", "units": q, "rma": e["salesreturn_number"]}); rets = []
        if rets:
            body = {"salesorder_id": so["salesorder_id"], "date": TODAY, "location_id": so.get("location_id"), "reason": "Stock sync 22 Oct 2026: shipped more than invoiced, physical aligned to accounting",
                    "line_items": [{"salesorder_item_id": l["line_item_id"], "item_id": l["item_id"], "quantity": q,
                                    "rate": l.get("rate", 0), "location_id": l.get("location_id") or so.get("location_id")} for l, q in rets]}
            body["line_items"] = [{k: v for k, v in li.items() if v not in (None, "")} for li in body["line_items"]]
            r = Z.post("/salesreturns", body, salesorder_id=so["salesorder_id"])
            if r.get("code") != 0 and "location" in str(r.get("message")).lower() and "inactive" in str(r.get("message")).lower():
                # Sharjah location is switched off: wake it for this return, put it back to sleep in finally
                for lid in {x for x in [so.get("location_id")] + [li.get("location_id") for li in body["line_items"]] if x}:
                    a = Z.post(f"/locations/{lid}/active")
                    if a.get("code") != 0: a = Z.post(f"/settings/locations/{lid}/active")
                    log(f"   location {lid} set active for the return: {'ok' if a.get('code') == 0 else 'FAILED ' + str(a.get('message'))}")
                    if a.get("code") == 0: relocated.append(lid)
                if relocated: r = Z.post("/salesreturns", body, salesorder_id=so["salesorder_id"])
            if r.get("code") != 0: log(f"   sales return FAILED: {r.get('message')}"); refused.append([so_no, "return: " + str(r.get("message"))]); return
            sr = r["salesreturn"]; log(f"   sales return {sr['salesreturn_number']} created, {sum(q for _, q in rets):g} units")
            rl = [{"line_item_id": li["line_item_id"], "quantity": li["quantity"]} for li in sr.get("line_items", [])]
            r2 = Z.post("/salesreturnreceives", {"date": TODAY, "line_items": rl, "notes": "Stock sync 22 Oct 2026"}, salesreturn_id=sr["salesreturn_id"])
            if r2.get("code") != 0:
                log(f"   receive FAILED: {r2.get('message')} (return {sr['salesreturn_number']} left unreceived, fix by hand)"); refused.append([so_no, "receive: " + str(r2.get("message"))]); return
            log(f"   received {r2.get('salesreturnreceive', {}).get('receive_number', '')}: physical stock up by {sum(q for _, q in rets):g}")
            done.append({"so": so_no, "action": "return+receive", "units": sum(q for _, q in rets), "rma": sr["salesreturn_number"]})
        if ship_needed and not dropship:
            bf.HEAL_STAFF = True
            bl = [str(x) for x in bf.execute_so({"salesorder_id": so["salesorder_id"], "salesorder_number": so_no})]
            for line in bl: log("   backfill: " + line[:200])
            if any(" OK " in x or "delivered" in x for x in bl):
                done.append({"so": so_no, "action": "backfill ship", "units": sum(q for _, q in ship_needed)})
            else:
                _, fl = bf.plan_so(so["salesorder_id"])          # say why the backfill found nothing to ship
                log("   backfill could not ship: " + ("; ".join(fl) if fl else "no live invoice line with room on the order"))
                refused.append([so_no, "backfill: " + ("; ".join(fl)[:150] if fl else "nothing packable")])
        # close the order
        so = Z.get(f"/salesorders/{so['salesorder_id']}").get("salesorder", so)
        if so.get("status") in ("closed", "void", "invoiced") and so.get("shipped_status") in ("fulfilled", "shipped"):
            log(f"   order now {so['status']}/{so['shipped_status']}, nothing more to do"); return
        L = [l for l in so["line_items"] if l.get("item_type") == "inventory"]
        if dropship and not any(float(l.get("quantity_invoiced") or 0) for l in L): nothing = True
        if dropship and not nothing:
            log("   drop ship with invoices, left alone"); refused.append([so_no, "drop ship with invoices"]); return
        if nothing:
            r = Z.post(f"/salesorders/{so['salesorder_id']}/status/void")
            log(f"   VOID {'ok' if r.get('code') == 0 else 'FAILED: ' + str(r.get('message'))}")
            if r.get("code") == 0: done.append({"so": so_no, "action": "void", "units": sum(float(l['quantity']) for l in L)})
            elif dropship and ZERO_CLOSE:
                log("   drop ship cannot be voided, closing it with a zero invoice + credit note instead"); zero_close(so, L, voids, so_no)
            else: refused.append([so_no, "void: " + str(r.get("message"))])
            return
        new_lines = []; released = 0.0
        for l in L:
            qi = float(l.get("quantity_invoiced") or 0); live = qi - min(voids.get(l["item_id"], 0), qi)
            tgt = max(live, qi, float(l.get("quantity_shipped") or 0), float(l.get("quantity_packed") or 0), float(l.get("quantity_picked") or 0))
            tgt = min(tgt, float(l["quantity"])); released += float(l["quantity"]) - tgt
            if tgt <= 0: continue
            nl = {k: l[k] for k in KEEP if k in l and l[k] not in (None, "", [])}; nl["quantity"] = tgt; new_lines.append(nl)
        if released <= 0 or not new_lines:
            log("   nothing to reduce")
            if ZERO_CLOSE and so.get("status") not in ("closed", "void"): zero_close(so, L, voids, so_no)
            return
        r = Z.req("PUT", f"/salesorders/{so['salesorder_id']}", params={"ignore_auto_number_generation": "true"}, json={"customer_id": cid, "line_items": new_lines})
        if r.get("code") == 0:
            st = r.get("salesorder", {}).get("status", "?"); log(f"   REDUCE ok, now {st}, released {released:g} units"); done.append({"so": so_no, "action": "reduce", "units": released, "status_after": st})
            if st in ("closed", "void", "invoiced", "fulfilled"): return
            so = r.get("salesorder", so); L = [l for l in so["line_items"] if l.get("item_type") == "inventory"]
        else:
            log(f"   REDUCE refused ({str(r.get('message'))[:100]}); closing with a zero invoice + credit note instead")
        if ZERO_CLOSE: zero_close(so, L, voids, so_no)
        else: refused.append([so_no, "reduce: " + str(r.get("message"))])
    finally:
        for lid in relocated:
            a = Z.post(f"/locations/{lid}/inactive")
            if a.get("code") != 0: a = Z.post(f"/settings/locations/{lid}/inactive")
            log(f"   location {lid} set back to inactive: {'ok' if a.get('code') == 0 else 'FAILED ' + str(a.get('message'))}")
        for iid, nm in reactivated:
            r = Z.post(f"/items/{iid}/inactive")
            log(f"   item {nm[:30]} set back to inactive: {'ok' if r.get('code') == 0 else 'FAILED ' + str(r.get('message'))}")
        if unticked: restore(cid, cust)

ZERO_CLOSE = os.environ.get("ZERO_CLOSE", "0") == "1"   # v3.18: OFF by default (Tarun, 23 Oct: no invoices or credit notes from this job, ever)
_sp = {}
def default_salesperson():
    """Used only when the sales order itself has no salesperson. Prefers the name in env SALESPERSON, else the first active one."""
    if "id" in _sp: return _sp["id"]
    _sp["id"] = None
    j = Z.get("/salespersons")
    people = [p for p in j.get("data", j.get("salespersons", [])) if p.get("is_active", True)]
    want = (os.environ.get("SALESPERSON") or "").strip().lower()
    for p in people:
        if want and want in (p.get("salesperson_name") or "").lower(): _sp["id"] = p["salesperson_id"]; break
    if not _sp["id"] and people: _sp["id"] = people[0]["salesperson_id"]
    log(f"   default salesperson for zero invoices: {_sp['id'] and next(p['salesperson_name'] for p in people if p['salesperson_id'] == _sp['id'])}")
    return _sp["id"]

def credit_zero_invoice(inv, so_no):
    """Credit note with the same item lines as the zero invoice, so accounting stock goes straight back up.
    Tries the lines exactly as Zoho wrote them on the invoice, first tied to the invoice, then standalone.
    If Zoho refuses both, the zero invoice is VOIDED so accounting stock is never left lower than before."""
    # this org has invoice approval on: a new invoice is born as a DRAFT (no stock effect, no credit note possible). Approve it first.
    if inv.get("status") in ("draft", "pending_approval", "approved"):
        a = Z.post(f"/invoices/{inv['invoice_id']}/approve") if inv.get("status") != "approved" else {"code": 0}
        if a.get("code") != 0:
            s = Z.post(f"/invoices/{inv['invoice_id']}/submit"); a = Z.post(f"/invoices/{inv['invoice_id']}/approve")
        # "approved" still counts as draft for Zoho until the invoice is marked as sent (no email goes out)
        s = Z.post(f"/invoices/{inv['invoice_id']}/status/sent")
        inv = Z.get(f"/invoices/{inv['invoice_id']}").get("invoice", inv)
        log(f"   invoice {inv['invoice_number']} approve: {'ok' if a.get('code') == 0 else 'FAILED ' + str(a.get('message'))}, mark sent: {'ok' if s.get('code') == 0 else 'FAILED ' + str(s.get('message'))}, status now {inv.get('status')}")
        if inv.get("status") in ("draft", "pending_approval", "approved"):
            v = Z.post(f"/invoices/{inv['invoice_id']}/status/void")
            log(f"   could not approve, void {inv['invoice_number']}: {'ok' if v.get('code') == 0 else 'FAILED ' + str(v.get('message'))}")
            refused.append([so_no, "zero invoice could not be approved" + (" (voided)" if v.get("code") == 0 else f" ({inv['invoice_number']} still draft)")]); return None
    CNKEEP = ("item_id", "name", "description", "quantity", "rate", "unit", "tax_id", "location_id", "account_id", "item_order")
    cn_lines = [{k: x[k] for k in CNKEEP if k in x and x[k] not in (None, "")} for x in inv.get("line_items", [])]
    for li in cn_lines: li["rate"] = 0
    body = {"customer_id": inv["customer_id"], "date": TODAY, "reference_number": inv["invoice_number"], "line_items": cn_lines,
            "notes": f"Stock sync close out 22 Oct 2026. Cancels zero value invoice {inv['invoice_number']} (sales order {so_no})."}
    if inv.get("salesperson_id"): body["salesperson_id"] = inv["salesperson_id"]
    if inv.get("location_id"): body["location_id"] = inv["location_id"]
    for k in ("place_of_supply", "vat_treatment", "tax_treatment", "is_inclusive_tax", "vat_reg_no", "tax_reg_no"):   # UAE VAT: must match the invoice
        if inv.get(k) not in (None, ""): body[k] = inv[k]
    r2 = Z.post("/creditnotes", body, invoice_id=inv["invoice_id"])
    if r2.get("code") != 0:
        log(f"   credit note tied to invoice refused ({r2.get('message')} | {json.dumps(r2)[:300]}), trying standalone")
        r2 = Z.post("/creditnotes", body)
    if r2.get("code") != 0:
        log(f"   standalone refused ({r2.get('message')}), trying minimal lines")
        body2 = dict(body); body2["line_items"] = [{"item_id": x["item_id"], "quantity": x["quantity"], "rate": 0} for x in cn_lines]
        r2 = Z.post("/creditnotes", body2, invoice_id=inv["invoice_id"])
    if r2.get("code") != 0:
        log(f"   credit note FAILED: {r2.get('message')}; voiding zero invoice {inv['invoice_number']} so stock is untouched")
        v = Z.post(f"/invoices/{inv['invoice_id']}/status/void")
        log(f"   void {inv['invoice_number']}: {'ok' if v.get('code') == 0 else 'FAILED ' + str(v.get('message'))}")
        refused.append([so_no, "credit note: " + str(r2.get("message")) + (" (zero invoice voided)" if v.get("code") == 0 else f" (zero invoice {inv['invoice_number']} STANDS, void by hand)")])
        return None
    cn = r2["creditnote"]; log(f"   credit note {cn['creditnote_number']} created against {inv['invoice_number']}")
    return cn

def repair_zero_invoices():
    """so_numbers = REPAIR: find today's zero value close out invoices that have no credit note yet and credit them."""
    page = 1; found = []
    while True:
        j = Z.get("/invoices", date=TODAY, per_page=200, page=page)
        for i in j.get("invoices", []):
            if float(i.get("total") or 0) == 0 and i.get("status") != "void": found.append(i)
        if not j.get("page_context", {}).get("has_more_page"): break
        page += 1
    log(f"repair: {len(found)} zero value invoices dated {TODAY}")
    for i in found:
        inv = Z.get(f"/invoices/{i['invoice_id']}").get("invoice", {})
        if "Stock sync close out" not in (inv.get("notes") or ""): continue
        cns = Z.get("/creditnotes", reference_number=inv["invoice_number"]).get("creditnotes", [])
        if any(c.get("status") != "void" for c in cns): log(f"{inv['invoice_number']} already has credit note {cns[0]['creditnote_number']}"); continue
        so_no = inv.get("reference_number") or "?"
        log(f"{inv['invoice_number']} ({so_no}) needs its credit note")
        if MODE == "live":
            cn = credit_zero_invoice(inv, so_no)
            if cn: done.append({"so": so_no, "action": "credit note for standing zero invoice", "units": sum(float(x.get("quantity") or 0) for x in inv.get("line_items", [])), "invoice": inv["invoice_number"], "credit_note": cn["creditnote_number"]})

def zero_close(so, L, voids, so_no):
    """Zoho closes a sales order only when invoiced quantity equals ordered quantity. When the line cannot be reduced,
    invoice the leftover quantity at zero price and immediately credit note that invoice with the same item lines.
    Accounting stock goes down and straight back up (net zero), physical does not move, no money, no VAT, order closes."""
    inv_lines = []
    vd = void_qty_by_item(so)                      # fresh copy: quantity_invoiced counts voided invoices too, Zoho's own "left to invoice" does not
    for l in L:
        qi = float(l.get("quantity_invoiced") or 0); v = min(vd.get(l["item_id"], 0), qi); vd[l["item_id"]] = vd.get(l["item_id"], 0) - v
        live = qi - v; q = float(l["quantity"]) - live - float(l.get("quantity_cancelled") or 0)
        if q > 1e-6:
            inv_lines.append({"item_id": l["item_id"], "salesorder_item_id": l["line_item_id"], "quantity": q, "rate": 0, "name": l.get("name"),
                              "description": "Stock sync close out 22 Oct 2026, zero value", "location_id": l.get("location_id") or so.get("location_id")})
    inv_lines = [{k: v for k, v in li.items() if v not in (None, "")} for li in inv_lines]
    if not inv_lines:
        log("   zero close: nothing left to invoice | " + " / ".join(f"{l['name'][:20]} ord {float(l['quantity']):g} inv {float(l.get('quantity_invoiced') or 0):g} canc {float(l.get('quantity_cancelled') or 0):g} shp {float(l.get('quantity_shipped') or 0):g}" for l in L))
        refused.append([so_no, "still open but nothing left to invoice (invoiced = ordered), check shipped status"]); return
    body = {"customer_id": so["customer_id"], "date": TODAY, "reference_number": so_no, "line_items": inv_lines,
            "notes": "Stock sync close out 22 Oct 2026. Zero value invoice so Zoho can close the sales order. A credit note is issued against it."}
    sp = so.get("salesperson_id") or default_salesperson()   # org rule: "Salesperson cannot be empty"
    if sp: body["salesperson_id"] = sp
    r = Z.post("/invoices", body, salesorderids=so["salesorder_id"])
    if r.get("code") != 0: log(f"   zero invoice FAILED: {r.get('message')}"); refused.append([so_no, "zero invoice: " + str(r.get("message"))]); return
    inv = r["invoice"]; log(f"   zero invoice {inv['invoice_number']} created for {sum(x['quantity'] for x in inv_lines):g} units")
    cn = credit_zero_invoice(inv, so_no)
    if not cn: return
    so2 = Z.get(f"/salesorders/{so['salesorder_id']}").get("salesorder", {})
    log(f"   order now {so2.get('status')}/{so2.get('shipped_status')}")
    done.append({"so": so_no, "action": "zero invoice + credit note", "units": sum(x["quantity"] for x in inv_lines), "invoice": inv["invoice_number"], "credit_note": cn["creditnote_number"], "status_after": so2.get("status")})

CN_FAILS = 0
_orig_credit = credit_zero_invoice
def credit_zero_invoice(inv, so_no):
    """Wrapper: after 2 credit note failures in one run, stop creating zero invoices at all (ZERO_CLOSE off)."""
    global CN_FAILS, ZERO_CLOSE
    cn = _orig_credit(inv, so_no)
    if cn is None:
        CN_FAILS += 1
        if CN_FAILS >= 2 and ZERO_CLOSE: ZERO_CLOSE = False; log("   two credit notes refused in this run: zero invoice closing switched OFF for the rest of the run")
    return cn

def reverse_zero_invoice(inv):
    """Undo one close out zero invoice: void its credit note(s), then void the invoice. The sales order reopens."""
    no = inv["invoice_number"]
    cns = [c for c in Z.get("/creditnotes", reference_number=no).get("creditnotes", []) if c.get("status") != "void"]
    for c in cns:
        v = Z.post(f"/creditnotes/{c['creditnote_id']}/status/void")
        log(f"   credit note {c['creditnote_number']} void: {'ok' if v.get('code') == 0 else 'FAILED ' + str(v.get('message'))}")
        if v.get("code") != 0: refused.append([no, "credit note void: " + str(v.get("message"))]); return False
    if inv.get("status") != "void":
        v = Z.post(f"/invoices/{inv['invoice_id']}/status/void")
        if v.get("code") != 0 and "BLACKLIST" in str(v.get("message")).upper():
            # same dance as the close out: untick the flag, void, tick it back (always)
            cid = inv["customer_id"]; cust = inv.get("customer_name", "")
            ok, msg = set_flag(cid, False)
            log(f"   {cust[:30]} BLACKLISTED unticked for the void: {'ok' if ok and not read_flag(cid)[0] else 'FAILED ' + str(msg)}")
            try: v = Z.post(f"/invoices/{inv['invoice_id']}/status/void")
            finally: restore(cid, cust)
        log(f"   invoice {no} void: {'ok' if v.get('code') == 0 else 'FAILED ' + str(v.get('message'))}")
        if v.get("code") != 0: refused.append([no, "invoice void: " + str(v.get("message"))]); return False
    so_no = inv.get("reference_number") or ""
    st = ""
    if so_no:
        j = Z.get("/salesorders", salesorder_number=so_no); s = (j.get("salesorders") or [{}])[0]
        st = f"{s.get('status')}/{s.get('shipped_status')} order_status {s.get('order_status')}"
    log(f"   {no} reversed, {so_no} now {st}")
    done.append({"so": so_no, "action": "reverse zero invoice", "units": 0, "invoice": no, "credit_notes": [c["creditnote_number"] for c in cns], "status_after": st})
    return True

def reverse_all(only=None):
    page = 1; found = []
    while True:
        j = Z.get("/invoices", date=TODAY, per_page=200, page=page)
        for i in j.get("invoices", []):
            if float(i.get("total") or 0) == 0 and i.get("status") != "void": found.append(i)
        if not j.get("page_context", {}).get("has_more_page"): break
        page += 1
    log(f"reverse: {len(found)} zero value non void invoices dated {TODAY}" + (f", restricted to {sorted(only)}" if only else ""))
    for i in found:
        if only and i["invoice_number"] not in only: continue
        inv = Z.get(f"/invoices/{i['invoice_id']}").get("invoice", {})
        if "Stock sync close out" not in (inv.get("notes") or ""): log(f"{i['invoice_number']}: not a close out invoice, left alone"); continue
        log(f"{inv['invoice_number']} ({inv.get('reference_number')}) status {inv.get('status')}")
        if MODE == "live": reverse_zero_invoice(inv)

def dedup_returns(only_so=None):
    """Find sales orders where the 22 Oct stock sync made the same return twice (run 6 then run 7) and delete the
    duplicate: its receive first, then the return. Keeps the oldest return. Only touches returns whose reason starts
    with 'Stock sync 22 Oct 2026'. Approved by Tarun 23 Oct 2026 after SO25-06700 showed RMA-00716 + RMA-00727."""
    page = 1; rets = []
    while True:
        j = Z.get("/salesreturns", per_page=200, page=page, sort_column="date", sort_order="D")
        rows = j.get("salesreturns", []); rets += [r for r in rows if r.get("date", "") >= "2026-10-22"]
        if not j.get("page_context", {}).get("has_more_page") or (rows and rows[-1].get("date", "") < "2026-10-22"): break
        page += 1
    by_so = {}
    for r in rets:
        if only_so and r.get("salesorder_number") not in only_so: continue
        by_so.setdefault(r.get("salesorder_number"), []).append(r)
    log(f"dedup: {len(rets)} returns since 22 Oct, {sum(1 for v in by_so.values() if len(v) > 1)} orders with more than one")
    for so_no, lst in sorted(by_so.items()):
        if len(lst) < 2: continue
        so = Z.get(f"/salesorders/{lst[0]['salesorder_id']}").get("salesorder", {})
        if any(float(i.get("total") or 0) == 0 and i.get("status") != "void" for i in so.get("invoices", [])):
            log(f"{so_no}: still has a live zero value invoice, reverse it first (REVERSE), skipped"); continue
        voids = void_qty_by_item(so)
        need = {}   # salesorder_item_id -> units that SHOULD have been returned = shipped - live invoiced
        for l in so.get("line_items", []):
            qi = float(l.get("quantity_invoiced") or 0); v = min(voids.get(l["item_id"], 0), qi); voids[l["item_id"]] = voids.get(l["item_id"], 0) - v
            out = float(l.get("quantity_shipped") or 0) + float(l.get("quantity_manuallyfulfilled") or 0)
            need[l["line_item_id"]] = max(out - (qi - v), 0.0)
        full = []
        for r in sorted(lst, key=lambda x: x["salesreturn_number"]):
            d = Z.get(f"/salesreturns/{r['salesreturn_id']}").get("salesreturn", {})
            if not (d.get("reason") or "").startswith("Stock sync 22 Oct 2026"): continue
            full.append(d)
        got = {}
        for d in full:
            for li in d.get("line_items", []): got[li["salesorder_item_id"]] = got.get(li["salesorder_item_id"], 0) + float(li["quantity"])
        over = {k: got[k] - need.get(k, 0) for k in got if got[k] - need.get(k, 0) > 1e-6}
        log(f"{so_no} {so.get('customer_name','')[:30]}: returns {[d['salesreturn_number'] for d in full]}, returned {sum(got.values()):g}, should be {sum(need.get(k,0) for k in got):g}" + (f", EXTRA {sum(over.values()):g}" if over else ", fine"))
        if not over: continue
        # delete newest returns while they are entirely surplus
        for d in reversed(full):
            qty = {li["salesorder_item_id"]: float(li["quantity"]) for li in d.get("line_items", [])}
            if not all(over.get(k, 0) + 1e-6 >= q for k, q in qty.items()): log(f"   {d['salesreturn_number']} is not a pure duplicate, left for a human"); continue
            if MODE != "live":
                log(f"   would delete {d['salesreturn_number']} ({sum(qty.values()):g} units) and its receive")
                for k, q in qty.items(): over[k] = over.get(k, 0) - q
                continue
            for rc in d.get("salesreturnreceives", []) or d.get("receives", []):
                x = Z.req("DELETE", f"/salesreturnreceives/{rc.get('receive_id') or rc.get('salesreturnreceive_id')}")
                log(f"   receive {rc.get('receive_number','')} of {d['salesreturn_number']} delete: {'ok' if x.get('code') == 0 else 'FAILED ' + str(x.get('message'))}")
                if x.get("code") != 0: refused.append([so_no, f"delete receive of {d['salesreturn_number']}: {x.get('message')}"]); break
            else:
                x = Z.req("DELETE", f"/salesreturns/{d['salesreturn_id']}")
                log(f"   return {d['salesreturn_number']} delete: {'ok' if x.get('code') == 0 else 'FAILED ' + str(x.get('message'))}")
                if x.get("code") == 0:
                    done.append({"so": so_no, "action": "deleted duplicate return", "units": sum(qty.values()), "rma": d["salesreturn_number"]})
                    for k, q in qty.items(): over[k] = over.get(k, 0) - q
                else: refused.append([so_no, f"delete {d['salesreturn_number']}: {x.get('message')}"])
        so2 = Z.get(f"/salesorders/{so['salesorder_id']}").get("salesorder", {})
        log(f"   {so_no} now: returned {sum(float(l.get('quantity_returned') or 0) for l in so2.get('line_items', [])):g}, shipped {sum(float(l.get('quantity_shipped') or 0) for l in so2.get('line_items', [])):g}, status {so2.get('status')}/{so2.get('shipped_status')}")

FM_FIELD = "2305879000128578077"     # custom field "Fulfillment Method" on sales orders (mandatory since ~Mar 2026)
FM_VALUES = ("Collect at Lapiz Blue", "Delivery From Lapiz Blue", "Delivery from Supplier")
def fm_map():
    """scripts/fulfillment_map.csv: so_number,method  (decided by Tarun or Adil for orders that carry no clue)"""
    m = {}; p = "scripts/fulfillment_map.csv"
    if os.path.exists(p):
        for line in open(p):
            parts = [x.strip() for x in line.split(",", 1)]
            if len(parts) == 2 and parts[1] in FM_VALUES: m[parts[0]] = parts[1]
    return m

def deliver_finished(only_so=None):
    """Group A (23 Oct 2026): orders where ordered = invoiced = shipped but a shipment was never marked delivered.
    Found by hand on SO-00544: Zoho's cached shipment status is stale, so 'Mark as Delivered' refuses with a quantity
    mismatch. Saving the order once refreshes the cache, after which the delivered tick works. Saving needs the
    mandatory custom field Fulfillment Method, which older orders lack: it is taken from the order itself, else from
    scripts/fulfillment_map.csv (decided by a person), else from the order's invoice, else from the drop ship flag.
    Orders with no source are listed and left alone. No document, no stock change."""
    rows = list_sos()
    if only_so: rows = [r for r in rows if r["salesorder_number"] in only_so]
    log(f"deliver: {len(rows)} open or partly invoiced orders dated <= {CUTOFF} to check")
    n_fix = 0; need_decision = []; mapping = fm_map()
    for r in rows:
        so = Z.get(f"/salesorders/{r['salesorder_id']}").get("salesorder", {})
        L = [l for l in so.get("line_items", []) if l.get("item_type") == "inventory"]
        if not L: continue
        full = all(abs(float(l["quantity"]) - float(l.get("quantity_invoiced") or 0)) < 1e-6 and abs(float(l["quantity"]) - float(l.get("quantity_shipped") or 0)) < 1e-6 for l in L)
        if not full: continue
        if so.get("order_status") == "closed" and so.get("shipped_status") in ("fulfilled", "shipped"): continue
        so_no = so["salesorder_number"]
        pend = [p for p in so.get("packages", []) if (p.get("status") or "") != "delivered"]
        # clues for the fulfillment method
        so_fm = next((c.get("value") for c in so.get("custom_fields", []) if str(c.get("customfield_id")) == FM_FIELD and c.get("value")), "")
        inv_fm = set()
        for i in so.get("invoices", []):
            if i.get("status") == "void": continue
            iv = bf.get_invoice(i["invoice_id"]) or {}
            for c in iv.get("custom_fields", []):
                if c.get("api_name") == "cf_fulfillment_method" and c.get("value"): inv_fm.add(c["value"])
        carriers = sorted({p.get("carrier") for p in so.get("packages", []) if p.get("carrier") and p.get("carrier") != "Al Quoz"})
        drop = bool(so.get("is_dropshipped") or so.get("is_drop_shipment"))
        if so_fm: method, src = so_fm, "order field"
        elif so_no in mapping: method, src = mapping[so_no], "decided in fulfillment_map.csv"
        else: method, src = "Collect at Lapiz Blue", "Adil's decision 23 Oct 2026: all old orders without the field get Collect at Lapiz Blue"
        log(f"{so_no} {so['date']} {so['customer_name'][:30]}: ordered=invoiced=shipped, status {so.get('status')}/{so.get('shipped_status')}, "
            f"packages not delivered: {[(p.get('package_number'), p.get('status'), p.get('shipment_number')) for p in pend]} | "
            f"fulfillment: order='{so_fm}' invoice={sorted(inv_fm)} staff carriers={carriers} dropship={drop} -> {method or 'UNKNOWN'} ({src})")
        if not pend: log("   no undelivered package found, Zoho status is stale, left alone"); refused.append([so_no, "all packages delivered already"]); continue
        if not method: need_decision.append([so_no, so["date"], so["customer_name"], ", ".join(l["name"][:30] for l in L)]); continue
        if MODE != "live": n_fix += 1; continue
        # 1. save the order once (same lines, fulfillment method set) so Zoho refreshes its cached shipment status
        body_lines = []
        for l in so["line_items"]:
            nl = {k: l[k] for k in KEEP if k in l and l[k] not in (None, "", [])}; body_lines.append(nl)
        body = {"customer_id": so["customer_id"], "line_items": body_lines, "custom_fields": [{"customfield_id": FM_FIELD, "value": method}]}
        u = Z.req("PUT", f"/salesorders/{so['salesorder_id']}", params={"ignore_auto_number_generation": "true"}, json=body)
        if u.get("code") != 0 and "BLACKLIST" in str(u.get("message")).upper():
            cid = so["customer_id"]; ok, msg = set_flag(cid, False)
            log(f"   {so['customer_name'][:30]} BLACKLISTED unticked for the save: {'ok' if ok and not read_flag(cid)[0] else 'FAILED ' + str(msg)}")
            try:
                u = Z.req("PUT", f"/salesorders/{so['salesorder_id']}", params={"ignore_auto_number_generation": "true"}, json=body)
                if u.get("code") == 0:
                    log(f"   order saved (fulfillment method {method}, {src}), shipment status now {u.get('salesorder', {}).get('shipped_status')}")
                    for p in pend:
                        sid = p.get("shipment_id")
                        d = Z.post(f"/shipmentorders/{sid}/status/delivered") if sid else {"code": -1, "message": "no shipment"}
                        log(f"   {p.get('package_number')} marked delivered: {'ok' if d.get('code') == 0 else 'FAILED ' + str(d.get('message'))}")
                        if d.get("code") == 0: done.append({"so": so_no, "action": "mark delivered", "units": float(p.get("quantity") or 0), "package": p.get("package_number")})
                        else: refused.append([so_no, f"deliver {p.get('package_number')}: {d.get('message')}"])
                    so2 = Z.get(f"/salesorders/{so['salesorder_id']}").get("salesorder", {})
                    log(f"   now {so2.get('status')}/{so2.get('shipped_status')} order_status {so2.get('order_status')}")
            finally: restore(cid, so["customer_name"])
            if u.get("code") != 0: log(f"   save FAILED: {u.get('message')}"); refused.append([so_no, "save: " + str(u.get("message"))])
            continue
        if u.get("code") != 0: log(f"   save FAILED: {u.get('message')}"); refused.append([so_no, "save: " + str(u.get("message"))]); continue
        log(f"   order saved (fulfillment method {method}, {src}), shipment status now {u.get('salesorder', {}).get('shipped_status')}")
        for p in pend:
            sid = p.get("shipment_id")
            if not sid:
                s = Z.post("/shipmentorders", {"date": p.get("date") or so["date"], "delivery_method": "Al Quoz", "notes": "Stock sync 23 Oct 2026: shipment for an already packed order"}, package_ids=p["package_id"], salesorder_id=so["salesorder_id"], send_notification="false")
                if s.get("code") != 0: log(f"   shipment for {p.get('package_number')} FAILED: {s.get('message')}"); refused.append([so["salesorder_number"], f"shipment {p.get('package_number')}: {s.get('message')}"]); continue
                sid = (s.get("shipmentorder") or {}).get("shipmentorder_id") or (s.get("shipmentorder") or {}).get("shipment_id")
            d = Z.post(f"/shipmentorders/{sid}/status/delivered")
            log(f"   {p.get('package_number')} marked delivered: {'ok' if d.get('code') == 0 else 'FAILED ' + str(d.get('message'))}")
            if d.get("code") == 0: done.append({"so": so["salesorder_number"], "action": "mark delivered", "units": float(p.get("quantity") or 0), "package": p.get("package_number")})
            else: refused.append([so["salesorder_number"], f"deliver {p.get('package_number')}: {d.get('message')}"])
        so2 = Z.get(f"/salesorders/{so['salesorder_id']}").get("salesorder", {})
        log(f"   now {so2.get('status')}/{so2.get('shipped_status')} order_status {so2.get('order_status')}")
    if need_decision:
        log(f"deliver: {len(need_decision)} orders have no clue for Fulfillment Method, a person decides (put them in scripts/fulfillment_map.csv as so_number,method):")
        for nd in need_decision: log("   NEEDS DECISION " + " | ".join(nd))
        json.dump(need_decision, open(f"{OUT}/fulfillment_needs_decision.json", "w"), indent=1)
    if MODE != "live": log(f"deliver dry: {n_fix} orders would be saved and marked delivered, {len(need_decision)} wait for a decision")

def cn_lines_to_so(cn):
    """Map a credit note's inventory lines to (salesorder_id, salesorder_item_id, quantity).
    First by the invoice line link Zoho stores when the credit note was made from the invoice; failing that, by the
    invoices the credit note was applied to, matching on item (older credit notes were made standalone and applied later)."""
    out = []
    applied = [i.get("invoice_id") for i in cn.get("invoices", []) if i.get("invoice_id")]
    if cn.get("invoice_id"): applied.append(cn["invoice_id"])
    for cl in cn.get("line_items", []):
        if cl.get("item_type") != "inventory": continue
        q = float(cl.get("quantity") or 0)
        if q <= 0: continue
        iid = cl.get("invoice_id") or cn.get("invoice_id"); iil = cl.get("invoice_item_id")
        hit = False
        if iid and iil:
            iv = bf.get_invoice(iid) or {}
            so_ids = [s["salesorder_id"] for s in iv.get("salesorders", [])] or ([iv["salesorder_id"]] if iv.get("salesorder_id") else [])
            for il in iv.get("line_items", []):
                if il["line_item_id"] == iil and il.get("salesorder_item_id") and so_ids:
                    out.append((so_ids[0], il["salesorder_item_id"], q)); hit = True; break
        if hit: continue
        for inv_id in dict.fromkeys(applied):            # fallback: applied invoices, match by item
            iv = bf.get_invoice(inv_id) or {}
            so_ids = [s["salesorder_id"] for s in iv.get("salesorders", [])] or ([iv["salesorder_id"]] if iv.get("salesorder_id") else [])
            if not so_ids: continue
            for il in iv.get("line_items", []):
                if il.get("item_id") == cl.get("item_id") and il.get("salesorder_item_id"):
                    out.append((so_ids[0], il["salesorder_item_id"], q)); hit = True; break
            if hit: break
    return out

def post_return(body, so, relocated):
    """v3.18: create a sales return; if Zoho says the line location is inactive, (a) try to wake that location,
    (b) if that fails, put the lines on the order's header location (the active branch) and say so in the log."""
    r = Z.post("/salesreturns", body, salesorder_id=so["salesorder_id"])
    msg = str(r.get("message")).lower()
    if r.get("code") != 0 and "location" in msg and "inactive" in msg:
        lids = {x for x in [so.get("location_id")] + [li.get("location_id") for li in body["line_items"]] if x}
        for lid in lids:
            a = Z.post(f"/settings/locations/{lid}/active")
            log(f"   location {lid} set active for the return: {'ok' if a.get('code') == 0 else 'FAILED ' + str(a.get('message'))}")
            if a.get("code") == 0: relocated.append(lid)
        if relocated: r = Z.post("/salesreturns", body, salesorder_id=so["salesorder_id"])
        if r.get("code") != 0 and so.get("location_id"):
            b2 = dict(body); b2["line_items"] = [dict(li, location_id=so["location_id"]) for li in body["line_items"]]; b2["location_id"] = so["location_id"]
            r = Z.post("/salesreturns", b2, salesorder_id=so["salesorder_id"])
            log(f"   retried with the order's header location {so.get('location_name', so['location_id'])}: {'ok' if r.get('code') == 0 else 'FAILED ' + str(r.get('message'))}")
    return r

def cn_returns(only_so=None):
    """Tarun's rule (23 Oct 2026): per sales order line, net shipped must equal net invoiced.
    net invoiced = invoiced - voided - credited (credit notes with item lines that were NOT born from a sales return).
    Where shipped - returned is more than net invoiced, create a sales return for the difference and receive it.
    Only returns and receives are created. Approved after CN26-00230 / SO26-00618 was checked by hand."""
    credited = {}; cn_of = {}; page = 1; seen = 0
    if only_so:   # test path: read only the credit notes hanging off the named orders
        for so_no in sorted(only_so):
            s = (Z.get("/salesorders", salesorder_number=so_no).get("salesorders") or [{}])[0]
            if not s.get("salesorder_id"): log(f"{so_no}: not found"); continue
            so = Z.get(f"/salesorders/{s['salesorder_id']}").get("salesorder", {})
            for c in so.get("creditnotes", []):
                if c.get("salesreturn_id") or str(c.get("status", "")).lower() in ("void", "draft"): continue
                cn = Z.get(f"/creditnotes/{c['creditnotes_id']}").get("creditnote")
                if not cn: continue
                seen += 1
                for so_id, lid, q in cn_lines_to_so(cn):
                    if so_id != so["salesorder_id"]: continue
                    d = credited.setdefault(so_id, {}); d[lid] = d.get(lid, 0.0) + q
                    cn_of.setdefault(so_id, set()).add((cn["creditnote_number"], cn["creditnote_id"]))
            credited.setdefault(so["salesorder_id"], {})
    while not only_so:
        j = Z.get("/creditnotes", per_page=200, page=page, sort_column="date")
        if j.get("code") != 0: log(f"credit note list failed: {j.get('message')}"); break
        for c in j.get("creditnotes", []):
            if c.get("status") in ("void", "draft"): continue
            cn = Z.get(f"/creditnotes/{c['creditnote_id']}").get("creditnote")
            if not cn: continue
            seen += 1
            if cn.get("salesreturn_id"): continue          # made from a sales return: stock already moved once
            for so_id, lid, q in cn_lines_to_so(cn):
                d = credited.setdefault(so_id, {}); d[lid] = d.get(lid, 0.0) + q
                cn_of.setdefault(so_id, set()).add((cn["creditnote_number"], cn["creditnote_id"]))
        if not j.get("page_context", {}).get("has_more_page"): break
        page += 1
    log(f"credit notes read: {seen}, sales orders with credited item lines: {len(credited)}")
    n_need = 0; units = 0.0; acc_issues = []; after_cutoff = []
    for so_id, cred in credited.items():
        so = Z.get(f"/salesorders/{so_id}").get("salesorder", {})
        if not so: continue
        so_no = so["salesorder_number"]
        if only_so and so_no not in only_so: continue
        if so.get("date", "") > CUTOFF:
            # v3.17 (24 Oct): credit notes are read org wide, so recent live orders turn up here; they are outside the project's scope
            log(f"{so_no} {so['date']} {so['customer_name'][:30]}: dated after cutoff {CUTOFF}, left alone (still being worked by sales)"); after_cutoff.append(so_no); continue
        voids = void_qty_by_item(so); rets = []; info = []
        for l in so.get("line_items", []):
            if l.get("item_type") != "inventory": continue
            qi = float(l.get("quantity_invoiced") or 0); v = min(voids.get(l["item_id"], 0), qi); voids[l["item_id"]] = voids.get(l["item_id"], 0) - v
            net_inv = qi - v - cred.get(l["line_item_id"], 0.0)
            out = float(l.get("quantity_shipped") or 0) + float(l.get("quantity_manuallyfulfilled") or 0)
            returned = float(l.get("quantity_returned") or 0)
            gap = (out - returned) - net_inv
            if net_inv < -1e-6:
                # credited (and/or voided) more than was ever invoiced: an accounts problem, no stock move can fix it
                info.append(f"{l['name'][:25]}: invoiced {qi:g}, voided {v:g}, credited {cred.get(l['line_item_id'],0):g} -> credited more than invoiced by {-net_inv:g}, ACCOUNTS to check, no return")
                acc_issues.append([so["salesorder_number"], l["name"], qi, v, cred.get(l["line_item_id"], 0), ", ".join(sorted(n for n, _ in cn_of.get(so_id, [])))]); continue
            gap = min(gap, out - returned)                 # never return more than physically left the warehouse
            if abs(gap) > 1e-6: info.append(f"{l['name'][:25]}: shipped {out:g}, returned {returned:g}, invoiced {qi:g}, voided {v:g}, credited {cred.get(l['line_item_id'],0):g} -> {'return ' + format(gap,'g') if gap > 0 else 'over returned by ' + format(-gap,'g')}")
            if gap > 1e-6: rets.append((l, gap))
        if not rets: continue
        n_need += 1; units += sum(q for _, q in rets)
        cns = ", ".join(sorted(n for n, _ in cn_of.get(so_id, [])))
        log(f"{so_no} {so['date']} {so['customer_name'][:30]} [{so.get('status')}] credit notes {cns} | " + " / ".join(info))
        if MODE != "live": continue
        cid = so["customer_id"]; flag, _ = read_flag(cid); unticked = False; reactivated = []; relocated = []
        try:
            if flag:
                ok, msg = set_flag(cid, False)
                if not ok or read_flag(cid)[0]: log(f"   could not untick BLACKLISTED ({msg}); skipped"); refused.append([so_no, "untick failed"]); continue
                unticked = True; log("   BLACKLISTED unticked")
            for l, q in rets:
                it = Z.get(f"/items/{l['item_id']}").get("item", {})
                if it.get("status") == "inactive":
                    r = Z.post(f"/items/{l['item_id']}/active")
                    if r.get("code") == 0: reactivated.append((l["item_id"], l["name"])); log(f"   item {l['name'][:30]} was inactive, set active for the return")
            body = {"salesorder_id": so_id, "date": TODAY, "location_id": so.get("location_id"),
                    "reason": f"Stock sync 23 Oct 2026: goods credited on {cns} never recorded back, physical aligned to accounting | " + " ".join(f"cn:{i}" for _, i in cn_of.get(so_id, [])),
                    "line_items": [{"salesorder_item_id": l["line_item_id"], "item_id": l["item_id"], "quantity": q, "rate": l.get("rate", 0), "location_id": l.get("location_id") or so.get("location_id")} for l, q in rets]}
            body["line_items"] = [{k: v for k, v in li.items() if v not in (None, "")} for li in body["line_items"]]
            r = post_return(body, so, relocated)
            if r.get("code") != 0: log(f"   sales return FAILED: {r.get('message')}"); refused.append([so_no, "return: " + str(r.get("message"))]); continue
            sr = r["salesreturn"]; log(f"   sales return {sr['salesreturn_number']} created, {sum(q for _, q in rets):g} units")
            rl = [{"line_item_id": li["line_item_id"], "quantity": li["quantity"]} for li in sr.get("line_items", [])]
            r2 = Z.post("/salesreturnreceives", {"date": TODAY, "line_items": rl, "notes": "Stock sync 23 Oct 2026"}, salesreturn_id=sr["salesreturn_id"])
            if r2.get("code") != 0: log(f"   receive FAILED: {r2.get('message')} ({sr['salesreturn_number']} left unreceived)"); refused.append([so_no, "receive: " + str(r2.get("message"))]); continue
            log(f"   received: physical stock up by {sum(q for _, q in rets):g}")
            done.append({"so": so_no, "action": "credit note return+receive", "units": sum(q for _, q in rets), "rma": sr["salesreturn_number"], "credit_notes": cns})
        finally:
            for lid in relocated:
                a = Z.post(f"/settings/locations/{lid}/inactive"); log(f"   location {lid} set back to inactive: {'ok' if a.get('code') == 0 else 'FAILED'}")
            for iid, nm in reactivated:
                a = Z.post(f"/items/{iid}/inactive"); log(f"   item {nm[:30]} set back to inactive: {'ok' if a.get('code') == 0 else 'FAILED'}")
            if unticked: restore(cid, so["customer_name"])
    if acc_issues:
        log(f"credit note returns: {len(acc_issues)} lines credited more than invoiced, for ACCOUNTS (no stock move):")
        for a in acc_issues: log("   ACCOUNTS " + " | ".join(str(x) for x in a))
        json.dump(acc_issues, open(f"{OUT}/credited_more_than_invoiced.json", "w"), indent=1)
    log(f"credit note returns: {n_need} orders need a return, {units:g} units" + ("" if MODE == "live" else " (dry, nothing changed)"))
    if after_cutoff: log(f"credit note returns: {len(after_cutoff)} orders dated after {CUTOFF} left alone: " + ", ".join(after_cutoff))

def ship_and_return(only_so=None):
    """Adil's method (24 Oct 2026) for orders Zoho will not cancel, void or reduce (a voided invoice still counts as
    invoiced, or shipped less than ordered with the rest never coming): ship the leftover quantity (package, shipment,
    delivered) and immediately sales return it and receive it. Net shipped is unchanged, physical stock is unchanged,
    no invoice or credit note is touched, and Zoho sees ordered = shipped and closes the order."""
    rows = list_sos()
    if only_so: rows = [r for r in rows if r["salesorder_number"] in only_so]
    log(f"ship+return: {len(rows)} open or partly invoiced orders dated <= {CUTOFF} to check")
    n = 0; units = 0.0
    for r in rows:
        so = Z.get(f"/salesorders/{r['salesorder_id']}").get("salesorder", {})
        if not so or so.get("order_status") == "closed" or so.get("status") in ("void", "closed"): continue
        so_no = so["salesorder_number"]
        if so.get("is_dropshipped") or so.get("is_drop_shipment"): log(f"{so_no}: drop ship, cannot be packaged, left for Adil"); refused.append([so_no, "drop ship"]); continue
        if so.get("picklists"): log(f"{so_no}: has a picklist, left alone"); refused.append([so_no, "picklist"]); continue
        if so_no in SKIP: log(f"{so_no}: in the skip file, left alone"); continue
        L = [l for l in so.get("line_items", []) if l.get("item_type") == "inventory"]
        plan = []; under = []
        for l in L:
            want = float(l["quantity"]) - float(l.get("quantity_cancelled") or 0) - float(l.get("quantity_dropshipped") or 0)
            left = want - float(l.get("quantity_shipped") or 0) - float(l.get("quantity_manuallyfulfilled") or 0)
            if left > 1e-6: plan.append((l, left))
            if float(l.get("quantity_invoiced") or 0) + 1e-6 < want: under.append(f"{l['name'][:25]} invoiced {float(l.get('quantity_invoiced') or 0):g} of {want:g}")
        if not plan: log(f"{so_no}: nothing left to ship on stock lines, status {so.get('status')}/{so.get('shipped_status')} (untracked lines? use Mark shipment as fulfilled)"); refused.append([so_no, "no stock line left to ship"]); continue
        if under:
            # v3.19: Zoho closes an order only when invoiced = ordered as well as shipped = ordered. Shipping and returning here
            # would create documents and still leave the order open, so it goes to the people list instead.
            log(f"{so_no} {so['date']} {so['customer_name'][:30]}: NOT fully invoiced ({'; '.join(under)}), ship+return would not close it, left for a person"); refused.append([so_no, "not fully invoiced: " + "; ".join(under)]); continue
        n += 1; units += sum(q for _, q in plan)
        log(f"{so_no} {so['date']} {so['customer_name'][:30]} [{so.get('status')}/{so.get('shipped_status')}]: ship then return " + ", ".join(f"{l['name'][:25]} x{q:g}" for l, q in plan))
        if MODE != "live": continue
        cid = so["customer_id"]; flag, _ = read_flag(cid); unticked = False; reactivated = []; relocated = []
        try:
            if flag:
                ok, msg = set_flag(cid, False)
                if not ok or read_flag(cid)[0]: log(f"   could not untick BLACKLISTED ({msg}); skipped"); refused.append([so_no, "untick failed"]); continue
                unticked = True; log("   BLACKLISTED unticked")
            for l, q in plan:
                it = Z.get(f"/items/{l['item_id']}").get("item", {})
                if it.get("status") == "inactive":
                    a = Z.post(f"/items/{l['item_id']}/active")
                    if a.get("code") == 0: reactivated.append((l["item_id"], l["name"])); log(f"   item {l['name'][:30]} was inactive, set active for this")
                    else: log(f"   item {l['name'][:30]} inactive and could not be activated: {a.get('message')}")
            note = f"Stock sync 24 Oct 2026: leftover quantity shipped and returned the same day to close the order (Adil's method), SO {so_no}"
            p = Z.post("/packages", {"date": TODAY, "notes": note, "line_items": [{"so_line_item_id": l["line_item_id"], "quantity": q} for l, q in plan]}, salesorder_id=so["salesorder_id"])
            if p.get("code") != 0: log(f"   package FAILED: {p.get('message')}"); refused.append([so_no, "package: " + str(p.get("message"))]); continue
            pid = p["package"]["package_id"]; log(f"   package {p['package'].get('package_number')} created")
            s = Z.post("/shipmentorders", {"date": TODAY, "delivery_method": "Al Quoz", "notes": note}, package_ids=pid, salesorder_id=so["salesorder_id"], send_notification="false")
            if s.get("code") != 0: log(f"   shipment FAILED: {s.get('message')} (package {pid} left, delete it by hand)"); refused.append([so_no, "shipment: " + str(s.get("message"))]); continue
            sh = s.get("shipmentorder") or {}; sid = sh.get("shipmentorder_id") or sh.get("shipment_id")
            d = Z.post(f"/shipmentorders/{sid}/status/delivered")
            log(f"   shipment {sh.get('shipment_number')} created, delivered: {'ok' if d.get('code') == 0 else 'FAILED ' + str(d.get('message'))}")
            body = {"salesorder_id": so["salesorder_id"], "date": TODAY, "location_id": so.get("location_id"), "reason": note,
                    "line_items": [{"salesorder_item_id": l["line_item_id"], "item_id": l["item_id"], "quantity": q, "rate": l.get("rate", 0), "location_id": l.get("location_id") or so.get("location_id")} for l, q in plan]}
            body["line_items"] = [{k: v for k, v in li.items() if v not in (None, "")} for li in body["line_items"]]
            rr = post_return(body, so, relocated)
            if rr.get("code") != 0: log(f"   sales return FAILED: {rr.get('message')} (shipment stands, physical stock is DOWN by {sum(q for _, q in plan):g} until a return is made by hand)"); refused.append([so_no, "return: " + str(rr.get("message"))]); continue
            sr = rr["salesreturn"]
            rl = [{"line_item_id": li["line_item_id"], "quantity": li["quantity"]} for li in sr.get("line_items", [])]
            rc = Z.post("/salesreturnreceives", {"date": TODAY, "line_items": rl, "notes": note}, salesreturn_id=sr["salesreturn_id"])
            if rc.get("code") != 0: log(f"   receive FAILED: {rc.get('message')} ({sr['salesreturn_number']} unreceived, physical DOWN until received by hand)"); refused.append([so_no, "receive: " + str(rc.get("message"))]); continue
            so2 = Z.get(f"/salesorders/{so['salesorder_id']}").get("salesorder", {})
            log(f"   return {sr['salesreturn_number']} received. Order now {so2.get('status')}/{so2.get('shipped_status')} order_status {so2.get('order_status')}")
            done.append({"so": so_no, "action": "ship+return", "units": sum(q for _, q in plan), "rma": sr["salesreturn_number"], "status_after": so2.get("order_status")})
        finally:
            for lid in relocated:
                a = Z.post(f"/settings/locations/{lid}/inactive"); log(f"   location {lid} set back to inactive: {'ok' if a.get('code') == 0 else 'FAILED'}")
            for iid, nm in reactivated:
                a = Z.post(f"/items/{iid}/inactive"); log(f"   item {nm[:30]} set back to inactive: {'ok' if a.get('code') == 0 else 'FAILED'}")
            if unticked: restore(cid, so["customer_name"])
    log(f"ship+return: {n} orders, {units:g} units" + ("" if MODE == "live" else " (dry, nothing changed)"))

if ONLY == {"SHIPRET"} or (ONLY and all(s.startswith("SHIPRET:") for s in ONLY)):
    ship_and_return(None if ONLY == {"SHIPRET"} else {s.split(":", 1)[1] for s in ONLY})
    json.dump({"mode": MODE, "shipret": True, "done": done, "refused": refused, "unrestored": unrestored, "calls": Z.calls}, open(f"{OUT}/closeout_done.json", "w"), indent=1)
    log(f"shipret finished: {len(done)} orders closed, {len(refused)} refused, calls {Z.calls}, customers left unblacklisted: {len(unrestored)}")
    sys.exit(1 if unrestored else 0)

if ONLY == {"CNRET"} or (ONLY and all(s.startswith("CNRET:") for s in ONLY)):
    cn_returns(None if ONLY == {"CNRET"} else {s.split(":", 1)[1] for s in ONLY})
    json.dump({"mode": MODE, "cnret": True, "done": done, "refused": refused, "unrestored": unrestored, "calls": Z.calls}, open(f"{OUT}/closeout_done.json", "w"), indent=1)
    log(f"cnret finished: {len(done)} returns made, {len(refused)} refused, calls {Z.calls}, customers left unblacklisted: {len(unrestored)}")
    sys.exit(1 if unrestored else 0)

if ONLY == {"DELIVER"} or (ONLY and all(s.startswith("DELIVER:") for s in ONLY)):
    deliver_finished(None if ONLY == {"DELIVER"} else {s.split(":", 1)[1] for s in ONLY})
    json.dump({"mode": MODE, "deliver": True, "done": done, "refused": refused, "calls": Z.calls}, open(f"{OUT}/closeout_done.json", "w"), indent=1)
    log(f"deliver finished: {len(done)} packages marked delivered, {len(refused)} refused, calls {Z.calls}")
    sys.exit(0)

if ONLY == {"DEDUP"} or (ONLY and all(s.startswith("DEDUP:") for s in ONLY)):
    dedup_returns(None if ONLY == {"DEDUP"} else {s.split(":", 1)[1] for s in ONLY})
    json.dump({"mode": MODE, "dedup": True, "done": done, "refused": refused, "calls": Z.calls}, open(f"{OUT}/closeout_done.json", "w"), indent=1)
    log(f"dedup finished: {len(done)} duplicate returns deleted, {len(refused)} refused, calls {Z.calls}")
    sys.exit(0)

if ONLY == {"REVERSE"} or (ONLY and all(s.startswith("REVERSE:") for s in ONLY)):
    reverse_all(None if ONLY == {"REVERSE"} else {s.split(":", 1)[1] for s in ONLY})
    json.dump({"mode": MODE, "reverse": True, "done": done, "refused": refused, "calls": Z.calls}, open(f"{OUT}/closeout_done.json", "w"), indent=1)
    log(f"reverse finished: {len(done)} invoices reversed, {len(refused)} refused, calls {Z.calls}")
    sys.exit(0)

if ONLY == {"REPAIR"}:
    repair_zero_invoices()
    json.dump({"mode": MODE, "repair": True, "done": done, "refused": refused, "calls": Z.calls}, open(f"{OUT}/closeout_done.json", "w"), indent=1)
    log(f"repair finished: {len(done)} credit notes made, {len(refused)} refused, calls {Z.calls}")
    sys.exit(0)
if ONLY and all(s.startswith("REPAIR:") for s in ONLY):   # REPAIR:LB26-20250 = repair that one invoice only (test)
    want = {s.split(":", 1)[1] for s in ONLY}
    for no in want:
        j = Z.get("/invoices", invoice_number=no); i = (j.get("invoices") or [{}])[0]
        inv = Z.get(f"/invoices/{i.get('invoice_id')}").get("invoice", {}) if i.get("invoice_id") else {}
        if not inv or "Stock sync close out" not in (inv.get("notes") or "") or float(inv.get("total") or 0) != 0: log(f"{no}: not a close out zero invoice, skipped"); continue
        log(f"{no} ({inv.get('reference_number')}) total {inv.get('total')} status {inv.get('status')}")
        if MODE == "live":
            cn = credit_zero_invoice(inv, inv.get("reference_number") or "?")
            if cn: done.append({"so": inv.get("reference_number"), "action": "credit note for standing zero invoice", "units": sum(float(x.get("quantity") or 0) for x in inv.get("line_items", [])), "invoice": no, "credit_note": cn["creditnote_number"]})
    log(f"repair finished: {len(done)} credit notes made, {len(refused)} refused, calls {Z.calls}"); sys.exit(0)

rows = list_sos()
if ONLY: rows = [r for r in rows if r["salesorder_number"] in ONLY]
rows = [r for r in rows if r["salesorder_number"] not in SKIP]
log(f"{MODE} run, cutoff {CUTOFF}, delete picklists {DEL_PICK}: {len(rows)} open or partly invoiced orders to look at")
for r in rows:
    try: handle(r)
    except SystemExit as e: log(f"stopped: {e}"); break
    except Exception as e: log(f"{r['salesorder_number']} ERROR {e!r}"); refused.append([r["salesorder_number"], repr(e)])
json.dump({"mode": MODE, "cutoff": CUTOFF, "done": done, "refused": refused, "unrestored": unrestored, "calls": Z.calls}, open(f"{OUT}/closeout_done.json", "w"), indent=1)
log(f"finished: {len(done)} actions, {len(refused)} refused, {sum(d['units'] for d in done):g} units, calls {Z.calls}, customers left unblacklisted: {len(unrestored)}")
if unrestored: sys.exit(1)
