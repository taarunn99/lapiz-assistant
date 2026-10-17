#!/usr/bin/env python3
"""Blacklist cycle (16 Oct 2026): close the invoiced-not-shipped sales orders of BLACKLISTED customers.

For each customer, in this exact order:
  1. read the contact, confirm the BLACKLISTED checkbox (cf_blacklisted) is ticked
  2. untick it (PUT contact), read again to confirm it is unticked
  3. run the normal backfill for that customer's sales orders (same execute_so as the history repair:
     package + shipment + delivered per live invoice, dated the invoice date)
  4. ALWAYS tick the checkbox back (finally block), read again to confirm it is ticked; retry up to 5 times
  5. if the flag could not be restored, the run ends with exit code 1 and the customer is listed in
     out/blacklist_cycle_UNRESTORED.json so a person fixes it at once

Modes:   dry  = read flags and show what would be shipped, change nothing
         live = do it
Env:     CUSTOMER_IDS  comma separated contact ids to limit the run (default: every customer in the list file)
         LIMIT         max customers this run (default 0 = all)
         + everything backfill_shipments.py needs (ZOHO_* secrets, MAX_CALLS, RATE_PER_MIN, OUT_DIR)
List:    scripts/blacklist_customers.json  [{customer_id, customer, sos:[...]}] made from the 8 Oct human review.
"""
import json, os, sys, time, datetime as dt

sys.argv = [sys.argv[0]] + sys.argv[1:]
MODE = sys.argv[1] if len(sys.argv) > 1 else "dry"
os.environ.setdefault("SO_NUMBERS", "")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import backfill_shipments as bf   # reuses the throttled Zoho client, plan_so and execute_so

Z = bf.Z; log = bf.log; OUT = bf.OUT
FIELD_ID = "2305879000017302163"   # contact custom field BLACKLISTED (cf_blacklisted), check box
CUSTOMER_IDS = {s.strip() for s in os.environ.get("CUSTOMER_IDS", "").split(",") if s.strip()}
LIMIT = int(os.environ.get("LIMIT", "0") or 0)
LIST = json.load(open(os.path.join(HERE, "blacklist_customers.json")))
LOGF = open(f"{OUT}/blacklist_cycle_log.txt", "a")

def out(line):
    log(line); LOGF.write(f"{dt.datetime.utcnow():%Y-%m-%d %H:%M:%S}Z {line}\n"); LOGF.flush()

def read_flag(cid):
    j = Z.get(f"/contacts/{cid}")
    if j.get("code") != 0: raise SystemExit(f"cannot read contact {cid}: {j.get('message')}")
    c = j["contact"]
    for f in c.get("custom_fields", []):
        if f.get("customfield_id") == FIELD_ID or f.get("api_name") == "cf_blacklisted":
            v = f.get("value")
            return (v is True or str(v).lower() == "true"), c["contact_name"]
    return False, c["contact_name"]   # field not set at all = not blacklisted

def set_flag(cid, value, name):
    # contact_name is sent unchanged because Zoho lists it as required on a contact update; nothing else is sent,
    # so no other field of the customer changes
    j = Z.req("PUT", f"/contacts/{cid}", json={"contact_name": name, "custom_fields": [{"customfield_id": FIELD_ID, "value": bool(value)}]})
    return j.get("code") == 0, j.get("message")

def restore(cid, name, live_name):
    """Tick BLACKLISTED back on. Returns True when confirmed. Never blocked by the call cap."""
    bf.MAX_CALLS = max(bf.MAX_CALLS, Z.calls + 60)   # restoring the flag must always be allowed to make its calls
    for attempt in range(1, 6):
        try:
            ok, msg = set_flag(cid, True, live_name)
            if ok:
                flag, _ = read_flag(cid)
                if flag:
                    out(f"  {name}: BLACKLISTED ticked back on (confirmed, attempt {attempt})"); return True
                out(f"  {name}: PUT said ok but flag reads {flag}, attempt {attempt}")
            else:
                out(f"  {name}: PUT to re-tick failed: {msg}, attempt {attempt}")
        except SystemExit as e:
            out(f"  {name}: re-tick attempt {attempt} interrupted: {e}")
        time.sleep(5 * attempt)
    return False

def plan_entries_for(sos):
    """Plan entries from out/plan.json (restored artifact), or minimal ones from the SO number lookup."""
    plans = {}
    pp = f"{OUT}/plan.json"
    if os.path.exists(pp):
        for p in json.load(open(pp)):
            if p["salesorder_number"] in sos: plans[p["salesorder_number"]] = p
    for n in sos:
        if n in plans: continue
        j = Z.get("/salesorders", salesorder_number=n)
        for s in j.get("salesorders", []):
            if s["salesorder_number"] == n:
                plans[n] = {"salesorder_id": s["salesorder_id"], "salesorder_number": n, "customer": s["customer_name"], "packages": [], "returns": []}
    return plans

def main():
    todo = [c for c in LIST if not CUSTOMER_IDS or c["customer_id"] in CUSTOMER_IDS]
    if LIMIT: todo = todo[:LIMIT]
    out(f"blacklist cycle {MODE}: {len(todo)} customers, {sum(len(c['sos']) for c in todo)} sales orders, field {FIELD_ID}")
    unrestored = []; summary = []
    for c in todo:
        cid, name, sos = c["customer_id"], c["customer"], c["sos"]
        flag, live_name = read_flag(cid)
        out(f"== {live_name} ({cid}): BLACKLISTED={flag}, {len(sos)} SOs: {', '.join(sos)}")
        plans = plan_entries_for(sos)
        missing = [n for n in sos if n not in plans]
        if missing: out(f"  not found in Zoho, skipped: {', '.join(missing)}")
        if MODE != "live":
            for n, p in plans.items():
                fresh, flags = bf.plan_so(p["salesorder_id"])
                if fresh is None: out(f"  {n}: would skip ({'; '.join(flags)})"); continue
                for pk in fresh["packages"]:
                    out(f"  {n}: would pack {pk['invoice_number']} dated {pk['invoice_date']}: " + ", ".join(f"{l['item']} x{l['quantity']:g}" for l in pk["lines"]))
                if not fresh["packages"]: out(f"  {n}: nothing to do" + (f" ({'; '.join(flags)})" if flags else ""))
            continue
        # ---------- live ----------
        # The untick sits INSIDE the try, so whatever happens after it (a failed read, a Zoho stop, a cancel),
        # the finally block ticks BLACKLISTED back on.
        shipped = 0; failed = 0
        try:
            if not flag:
                out(f"  flag already off, running backfill without touching the contact")
            else:
                ok, msg = set_flag(cid, False, live_name)
                now, _ = read_flag(cid)
                if not ok or now:
                    out(f"  could not untick (PUT ok={ok} {msg}, reads {now}); customer skipped, no sales order touched")
                    summary.append((name, "untick failed, skipped", len(sos))); continue
                out(f"  BLACKLISTED unticked (confirmed)")
            for n, p in plans.items():
                for line in bf.execute_so(p):
                    out(f"  {line}")
                    if " OK " in line: shipped += 1
                    if "FAILED" in line: failed += 1
        finally:
            if flag:
                if not restore(cid, name, live_name):
                    unrestored.append({"customer_id": cid, "customer": name})
                    json.dump(unrestored, open(f"{OUT}/blacklist_cycle_UNRESTORED.json", "w"), indent=1)
                    out(f"  !!!! {name}: BLACKLISTED COULD NOT BE RESTORED. FIX BY HAND NOW: Zoho Books > Customers > {name} > Edit > Custom Fields > tick BLACKLISTED")
        summary.append((name, f"{shipped} packages shipped, {failed} failed", len(sos)))
    out("---- summary ----")
    for name, res, n in summary: out(f"{name}: {n} SOs, {res}")
    json.dump(unrestored, open(f"{OUT}/blacklist_cycle_UNRESTORED.json", "w"), indent=1)
    out(f"calls used {Z.calls}. customers left UNBLACKLISTED by mistake: {len(unrestored)}")
    if unrestored:
        out("!!!! " + ", ".join(u["customer"] for u in unrestored) + " are NOT blacklisted right now. Re-tick them in Zoho immediately.")
        sys.exit(1)

if __name__ == "__main__":
    import signal
    def _term(signum, frame): raise SystemExit("cancelled (SIGTERM)")
    signal.signal(signal.SIGTERM, _term)
    try:
        main()
    except SystemExit as e:
        # a cancel or a Zoho stop mid customer: the finally block above has already re-ticked the flag for the
        # customer in progress; say so in the log so nobody wonders
        out(f"run ended: {e}")
        raise
