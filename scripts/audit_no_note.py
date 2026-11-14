#!/usr/bin/env python3
"""
Deep check: which invoices have NO delivery note attached. Read only, nothing is changed in Zoho.

For both books (Dubai and Abu Dhabi), every invoice dated AUDIT_FROM..AUDIT_TO that is not a draft:
  - void                                   -> counted, no note needed
  - nothing attached                       -> NO DELIVERY NOTE (certain: Zoho says there is no file)
  - a file named DN-... is attached        -> has a note (put there by the delivery notes system or by hand from it)
  - other files attached                   -> every file is downloaded and read:
        a page says DELIVERY NOTE          -> has a note (attached by hand)
        readable, no page says so          -> OTHER FILE ONLY (LPO, photo, receipt...): no delivery note found
        cannot be opened or read           -> CHECK BY HAND
The "nothing attached" flag from the invoice list is double checked against the full invoice for a sample;
if a single one disagrees, every invoice is checked in full.
"""
import csv
import glob
import os
import random
import re
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("MODE", "dry")
os.environ.setdefault("OUT", "audit/engine-out")
import attach_delivery_notes as dn                     # noqa: E402
from attach_delivery_notes import DUBAI, Zoho, log     # noqa: E402
import audit_delivery_notes as ad                      # noqa: E402  (item by item reading of attached notes)

FROM = os.environ.get("AUDIT_FROM", "2026-09-21").strip()
TO = os.environ.get("AUDIT_TO", "").strip() or datetime.now(DUBAI).strftime("%Y-%m-%d")
EMAIL = [x.strip() for x in os.environ.get("AUDIT_TO_EMAIL", "tarun.s@lapizblue.com").split(",") if x.strip()]
LIMIT = int(os.environ.get("AUDIT_LIMIT", "0") or 0)
OUT = "audit"
WORK = f"{OUT}/deep"
os.makedirs(WORK, exist_ok=True)
ORGS = [(dn.ORG, "Dubai"), (dn.ORG_AUH, "Abu Dhabi")]
MAX_PAGES = 8


def list_invoices(z, org):
    found, page = [], 1
    while True:
        code, j = z.call("GET", "/invoices", org=org, params={"date_start": FROM, "date_end": TO, "per_page": 200, "page": page})
        if j.get("code") not in (0, None) and not j.get("invoices"):
            raise SystemExit(f"Zoho list failed for org {org}: {j}")
        found += j.get("invoices", [])
        if not j.get("page_context", {}).get("has_more_page"):
            return found
        page += 1


def download(z, org, invoice_id, doc, path):
    doc_id = doc.get("document_id", "")
    for attempt in range(4):
        gap = dn.time.time() - z.last_call
        if gap < dn.PACE_SECONDS:
            dn.time.sleep(dn.PACE_SECONDS - gap)
        z.last_call = dn.time.time()
        z.calls += 1
        try:
            r = requests.get(f"{dn.API}/invoices/{invoice_id}/documents/{doc_id}", headers=z._headers(),
                             params={"organization_id": org}, timeout=120)
        except Exception as e:
            why = f"download error: {e}"
            continue
        if r.status_code == 200 and "json" not in r.headers.get("content-type", "") and len(r.content) > 100:
            with open(path, "wb") as fh:
                fh.write(r.content)
            return True, ""
        why = f"HTTP {r.status_code} {r.text[:120]}"
        if r.status_code == 429:
            dn.time.sleep(90)
        elif r.status_code == 401:
            z._refresh()
        else:
            break
    return False, why


def to_pngs(path, prefix):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        subprocess.run(["pdftoppm", "-r", "150", "-l", str(MAX_PAGES), "-png", path, prefix], check=False, capture_output=True)
        return sorted(glob.glob(prefix + "-*.png"))
    if ext in (".jpg", ".jpeg", ".png", ".heic", ".webp", ".tif", ".tiff", ".bmp"):
        try:
            from PIL import Image, ImageOps
            try:
                import pillow_heif
                pillow_heif.register_heif_opener()
            except ImportError:
                pass
            im = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
            im.thumbnail((2400, 2400))
            im.save(prefix + "-1.png")
            return [prefix + "-1.png"]
        except Exception:
            return []
    return []


def ocr(png):
    best = ""
    for psm in ("6", "3"):
        r = subprocess.run(["tesseract", png, "-", "--psm", psm], capture_output=True, text=True,
                           env={**os.environ, "OMP_THREAD_LIMIT": "1"})
        if len(r.stdout) > len(best):
            best = r.stdout
        if says_note(best):
            break
    return best


def says_note(text):
    sq = re.sub(r"[^A-Z]", "", text.upper())
    if "DELIVERYNOTE" in sq or "DELIVERYCHALLAN" in sq or "DELIVERYORDER" in sq:
        return True
    # one or two OCR slips in the heading
    for i in range(0, max(0, len(sq) - 12)):
        if sq[i] in "DO0" and dn.edit_distance(sq[i:i + 12], "DELIVERYNOTE") <= 2:
            return True
    return False


def full(z, org, invoice_id):
    """Full invoice, and never an empty answer: an empty answer would look like 'no file attached'."""
    for attempt in range(4):
        inv = z.full_invoice(invoice_id, org)
        if inv.get("invoice_id") == invoice_id:
            return inv
        z._full.pop((org, invoice_id), None)
        dn.time.sleep(20 * (attempt + 1))
    raise RuntimeError("Zoho did not return this invoice")


def classify(z, org, row):
    """Returns (result, detail, file names)."""
    inv = full(z, org, row["invoice_id"])
    docs = inv.get("documents") or []
    names = [d.get("file_name", "") for d in docs]
    if not docs:
        return "NO FILE", "", names
    if any(re.match(r"^(DN|CN)[-_ ]", n, re.I) for n in names):
        return "HAS NOTE", "file named DN-... attached", names
    folder = f"{WORK}/{row['invoice_id']}"
    os.makedirs(folder, exist_ok=True)
    readable, problems, chars = 0, [], 0
    try:
        for k, d in enumerate(docs):
            name = d.get("file_name", f"file{k}")
            path = f"{folder}/{k}{os.path.splitext(name)[1].lower() or '.bin'}"
            ok, why = download(z, org, row["invoice_id"], d, path)
            if not ok:
                problems.append(f"{name}: {why}")
                continue
            pngs = to_pngs(path, f"{folder}/p{k}")
            if not pngs:
                problems.append(f"{name}: not a PDF or image")
                continue
            with ThreadPoolExecutor(4) as ex:
                texts = list(ex.map(ocr, pngs))
            for t in texts:
                if says_note(t):
                    return "HAS NOTE", f"'{name}' reads as a delivery note (attached by hand)", names
            n = sum(len(re.sub(r"\s", "", t)) for t in texts)
            chars += n
            if n >= 150:
                readable += 1
            else:
                problems.append(f"{name}: too faint to read")
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    if problems:
        return "CHECK BY HAND", "; ".join(problems)[:300], names
    return "OTHER FILE ONLY", "the attached file(s) were read and none says DELIVERY NOTE", names


def main():
    z = Zoho()
    log(f"deep check, invoices dated {FROM} to {TO}, Dubai and Abu Dhabi")
    rows_out, counts, item_rows = [], defaultdict(Counter), []
    for org, org_name in ORGS:
        invs = list_invoices(z, org)
        seen_ids = {i["invoice_id"] for i in invs}
        assert len(seen_ids) == len(invs), "Zoho returned the same invoice twice"
        log(f"{org_name}: {len(invs)} invoices in Zoho for the range")
        drafts = [i for i in invs if i.get("status") == "draft"]
        void = [i for i in invs if i.get("status") == "void"]
        live = [i for i in invs if i.get("status") not in ("draft", "void")]
        flag_no = [i for i in live if not i.get("has_attachment")]
        flag_yes = [i for i in live if i.get("has_attachment")]
        counts[org_name].update({"in Zoho": len(invs), "draft": len(drafts), "void": len(void)})
        log(f"{org_name}: {len(drafts)} draft, {len(void)} void, {len(flag_no)} flagged no file, {len(flag_yes)} flagged with file")

        # double check the "no file" flag on a sample against the full invoice
        sample = random.Random(7).sample(flag_no, min(150, len(flag_no)))
        wrong = [i for i in sample if (full(z, org, i["invoice_id"]).get("documents") or [])]
        log(f"{org_name}: flag check on {len(sample)} 'no file' invoices: {len(wrong)} disagree")
        full_check = bool(wrong)
        if full_check:
            log(f"{org_name}: flag is not reliable, checking every invoice in full")
        counts[org_name]["flag sample"] = len(sample)
        counts[org_name]["flag sample wrong"] = len(wrong)

        def out(i, result, detail="", names=()):
            counts[org_name][result] += 1
            rows_out.append({"org": org_name, "org_id": org, "invoice": i.get("invoice_number"), "date": i.get("date"),
                             "customer": i.get("customer_name"), "salesperson": i.get("salesperson_name", ""),
                             "fulfillment": i.get("cf_fulfillment_method", ""), "status": i.get("status"),
                             "total": i.get("total"), "result": result, "detail": detail, "files": "; ".join(names),
                             "link": f"https://books.zoho.com/app/{org}#/invoices/{i.get('invoice_id')}"})

        for i in void:
            out(i, "VOID")
        todo = live if full_check else flag_yes
        if not full_check:
            for i in flag_no:
                out(i, "NO FILE")
        todo.sort(key=lambda i: (i.get("date", ""), i.get("invoice_number", "")))
        for n, i in enumerate(todo, 1):
            if LIMIT and n > LIMIT:
                log(f"limit {LIMIT} reached (test)")
                break
            items = None
            try:
                dn.ORG = org                         # the item reader works on one set of books at a time
                a = ad.audit_invoice(z, i)
                names = a.get("files", [])
                if a["result"] == "no attachment":
                    result, detail = "NO FILE", ""
                elif a["result"] in ("covered", "no items"):
                    result, detail = "HAS NOTE", "every item of the invoice is on the attached note(s)"
                elif a["result"] in ("partial", "short"):
                    result, detail, items = "HAS NOTE", "", a["lines"]
                else:                                # items could not be matched: is there a note at all?
                    result, detail, names = classify(z, org, i)
                    if result == "HAS NOTE":
                        result, detail = "NOTE, ITEMS NOT CONFIRMED", a.get("why", "")[:300]
            except Exception as e:
                result, detail, names = "CHECK BY HAND", f"error: {e}", []
            finally:
                dn.ORG = ORGS[0][0]
            out(i, result, detail, names)
            if items:
                for m in items["missing"]:
                    item_rows.append([org_name, i.get("invoice_number"), i.get("date"), i.get("customer_name"), i.get("salesperson_name", ""),
                                      m["item"], m["sku"], m["invoiced"], 0, m["invoiced"], "item is on none of the attached notes",
                                      "; ".join(names), rows_out[-1]["link"]])
                    counts[org_name]["missing item rows"] += 1
                for m in items["short"]:
                    item_rows.append([org_name, i.get("invoice_number"), i.get("date"), i.get("customer_name"), i.get("salesperson_name", ""),
                                      m["item"], m["sku"], m["invoiced"], m["on_notes"], round(m["invoiced"] - m["on_notes"], 2),
                                      "quantity on the note(s) is less than invoiced", "; ".join(names), rows_out[-1]["link"]])
                    counts[org_name]["short item rows"] += 1
                counts[org_name]["invoices with fewer items on notes"] += 1
            if result != "HAS NOTE" or n % 100 == 0:
                log(f"  [{org_name} {n}/{len(todo)}] {i.get('invoice_number')} {result} {detail[:80]} {'; '.join(names)[:60]}")
    write(rows_out, counts, item_rows)
    log(f"done. Zoho calls: {z.calls}")


def write(rows, counts, item_rows):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
    rows.sort(key=lambda r: (r["org"] != "Dubai", r["date"] or "", r["invoice"] or ""))
    head = ["Books", "Invoice", "Date", "Customer", "Salesperson", "Fulfillment", "Status", "Total AED", "Link"]
    line = lambda r: [r["org"], r["invoice"], r["date"], r["customer"], r["salesperson"], r["fulfillment"], r["status"], r["total"], r["link"]]
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws.append(["Invoices with no delivery note attached", f"invoice dates {FROM} to {TO}"])
    ws.append(["Made", datetime.now(DUBAI).strftime("%Y-%m-%d %H:%M") + " Dubai time, read live from Zoho Books"])
    ws.append([])
    ws.append(["", "Dubai", "Abu Dhabi", "Both"])
    labels = [("Invoices in Zoho for these dates", "in Zoho"), ("Draft (left out)", "draft"), ("Void (no note needed)", "VOID"),
              ("NO DELIVERY NOTE: nothing attached at all", "NO FILE"),
              ("NO DELIVERY NOTE FOUND: only other files attached (LPO, photo...)", "OTHER FILE ONLY"),
              ("Check by hand: a file is attached but could not be read", "CHECK BY HAND"),
              ("Has a delivery note", "HAS NOTE"),
              ("   of those: note has FEWER ITEMS or less quantity than the invoice", "invoices with fewer items on notes"),
              ("Note attached, items could not be confirmed (faint scan or edited invoice)", "NOTE, ITEMS NOT CONFIRMED")]
    for label, key in labels:
        d, a = counts["Dubai"][key if key != "VOID" else "void"], counts["Abu Dhabi"][key if key != "VOID" else "void"]
        ws.append([label, d, a, d + a])
    ws.append([])
    for org in ("Dubai", "Abu Dhabi"):
        c = counts[org]
        ws.append([f"Self check, {org}", f"{c['in Zoho']} in Zoho = {c['draft']} draft + {c['void']} void + {c['NO FILE']} no file + "
                   f"{c['OTHER FILE ONLY']} other file only + {c['CHECK BY HAND']} check by hand + {c['HAS NOTE']} with note + "
                   f"{c['NOTE, ITEMS NOT CONFIRMED']} note with items not confirmed"
                   f" (sum {c['draft'] + c['void'] + c['NO FILE'] + c['OTHER FILE ONLY'] + c['CHECK BY HAND'] + c['HAS NOTE'] + c['NOTE, ITEMS NOT CONFIRMED']})"])
        ws.append([f"'Nothing attached' double checked, {org}", f"{c['flag sample']} invoices opened in full, {c['flag sample wrong']} had a file after all"])
    ws.append([])
    ws.append(["Per invoice date", "No file (Dubai)", "No file (Abu Dhabi)"])
    per = defaultdict(Counter)
    for r in rows:
        if r["result"] == "NO FILE":
            per[r["date"]][r["org"]] += 1
    for d in sorted(per):
        ws.append([d, per[d]["Dubai"], per[d]["Abu Dhabi"]])
    ws["A1"].font = Font(bold=True, size=14)
    ws.column_dimensions["A"].width = 66
    for col in "BCD":
        ws.column_dimensions[col].width = 22

    def sheet(title, keep, extra=()):
        s = wb.create_sheet(title)
        s.append(head + [e[0] for e in extra])
        for r in rows:
            if keep(r):
                s.append(line(r) + [r[e[1]] for e in extra])
        for c in s[1]:
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor="1343C0")
        s.freeze_panes = "A2"
        s.auto_filter.ref = s.dimensions
        for col in range(1, s.max_column + 1):
            w = max((len(str(s.cell(row=x, column=col).value or "")) for x in range(1, min(s.max_row, 400) + 1)), default=8)
            s.column_dimensions[get_column_letter(col)].width = min(max(10, w + 2), 60)
        return s.max_row - 1

    n1 = sheet("No delivery note", lambda r: r["result"] == "NO FILE")
    s2 = wb.create_sheet("Fewer items than invoice")
    s2.append(["Books", "Invoice", "Date", "Customer", "Salesperson", "Item", "SKU", "Invoiced qty", "Qty on attached notes",
               "Not covered qty", "What", "Files attached", "Link"])
    for r in sorted(item_rows, key=lambda r: (r[0] != "Dubai", r[2] or "", r[1] or "")):
        s2.append(r)
    for c in s2[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1343C0")
    s2.freeze_panes = "A2"
    s2.auto_filter.ref = s2.dimensions
    for col in range(1, s2.max_column + 1):
        w = max((len(str(s2.cell(row=x, column=col).value or "")) for x in range(1, min(s2.max_row, 400) + 1)), default=8)
        s2.column_dimensions[get_column_letter(col)].width = min(max(10, w + 2), 60)
    sheet("Note, items not confirmed", lambda r: r["result"] == "NOTE, ITEMS NOT CONFIRMED", [("Files attached", "files"), ("Why", "detail")])
    n2 = sheet("Other file only", lambda r: r["result"] == "OTHER FILE ONLY", [("Files attached", "files"), ("What was found", "detail")])
    n3 = sheet("Check by hand", lambda r: r["result"] == "CHECK BY HAND", [("Files attached", "files"), ("Why", "detail")])
    sheet("All invoices", lambda r: True, [("Result", "result"), ("Files attached", "files"), ("Detail", "detail")])
    xlsx = f"{OUT}/no-delivery-note-{FROM}-to-{TO}.xlsx"
    wb.save(xlsx)
    with open(f"{OUT}/no-delivery-note-deep.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["org"])
        w.writeheader()
        w.writerows(rows)
    log(f"excel: {n1} no file, {n2} other file only, {n3} check by hand")
    for org in ("Dubai", "Abu Dhabi"):
        log(f"{org}: {dict(counts[org])}")
    subject = f"Invoices with no delivery note, {FROM} to {TO}: {n1} with nothing attached, {n2} with other files only, {n3} to check"
    html = (f"<div style='font-family:Arial,sans-serif;font-size:14px'><p>Invoice dates {FROM} to {TO}, Dubai and Abu Dhabi.</p><ul>"
            f"<li><b>{n1}</b> invoices with nothing attached</li><li><b>{n2}</b> with only other files attached, no delivery note found</li>"
            f"<li><b>{n3}</b> to check by hand</li></ul><p>Details in the attached Excel.</p></div>")
    dn.REPORT_TO, dn.REPORT_CC = EMAIL, []
    try:
        dn.send_daily_email(html, subject, [xlsx])
    except Exception as e:
        log(f"email not sent: {e}")


if __name__ == "__main__":
    main()
