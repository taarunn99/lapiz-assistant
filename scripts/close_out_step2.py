#!/usr/bin/env python3
"""Step 2 close out, v3 (22 Oct 2026, 16:00): make physical stock follow accounting stock on old open sales orders, then close them.
v3 changes: counts quantity already returned; Sharjah location on returns; ships invoiced-not-shipped lines through the backfill (heal on);
voids drop ships that have no invoice; when Zoho will not reduce a line, closes the order with a zero value invoice plus a credit note
against it (ZERO_CLOSE=1, default on).
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

ZERO_CLOSE = os.environ.get("ZERO_CLOSE", "1") == "1"
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
