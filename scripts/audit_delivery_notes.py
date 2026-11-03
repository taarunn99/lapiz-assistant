#!/usr/bin/env python3
"""
Audit: which invoices have no delivery note attached, and which invoice lines have no note.

One-off backlog check, separate from the daily attach engine. It only READS Zoho. Nothing is attached,
changed or emailed to staff. The result is one Excel file (plus CSVs) sent to AUDIT_TO.

For every invoice dated AUDIT_FROM..AUDIT_TO (Dubai org, all statuses except draft):
  1. No file attached in Zoho                         -> sheet "No delivery note"
  2. Files attached: every file is downloaded, every page read with the same OCR as the engine,
     and every invoice line is looked for on the pages (SKU and item name, OCR tolerant).
       - every line found                                -> covered (counted, not listed)
       - some lines found, some not                      -> sheet "Items with no note" (one row per missing line)
       - line found but quantity on the notes < invoiced -> sheet "Delivered short"
       - no page could be read as a delivery note        -> sheet "Check by hand" (scan too faint, photo, wrong file)
  3. Void invoices need no note; they are only counted.

Environment: ZOHO_* as the engine, AUDIT_FROM (default 2026-10-01), AUDIT_TO (default today Dubai),
AUDIT_TO_EMAIL (default tarun.s@lapizblue.com), AUDIT_LIMIT (test: stop after N invoices with files),
RESEND_API_KEY (blank = no email, the file is still in the run artifact).
"""

import base64
import csv
import glob
import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("MODE", "dry")
os.environ.setdefault("OUT", "audit/engine-out")
import attach_delivery_notes as dn                                   # noqa: E402
from attach_delivery_notes import (DUBAI, Zoho, edit_distance, log, name_tokens, page_items,   # noqa: E402
                                   read_page, sku_same, squash, tokens)

AUDIT_FROM = os.environ.get("AUDIT_FROM", "2026-10-01").strip()
AUDIT_TO = os.environ.get("AUDIT_TO", "").strip() or datetime.now(DUBAI).strftime("%Y-%m-%d")
AUDIT_EMAIL = [x.strip() for x in os.environ.get("AUDIT_TO_EMAIL", "tarun.s@lapizblue.com").split(",") if x.strip()]
LIMIT = int(os.environ.get("AUDIT_LIMIT", "0") or 0)
WORKERS = int(os.environ.get("OCR_WORKERS", "4"))
OUT = "audit"
DOCS = f"{OUT}/docs"
PAGES = f"{OUT}/pages"
for d in (OUT, DOCS, PAGES):
    os.makedirs(d, exist_ok=True)
BOOKS = dn.BOOKS_UI


# ---------------------------------------------------------------- Zoho file download

def download_document(z, invoice_id, doc, path):
    """Download one attached file of an invoice. Returns (ok, why)."""
    doc_id = doc.get("document_id", "")
    urls = [f"{dn.API}/invoices/{invoice_id}/documents/{doc_id}",
            f"{dn.API}/invoices/{invoice_id}/attachment"]
    for i, url in enumerate(urls):
        params = {"organization_id": dn.ORG}
        if i == 1:
            params["document_id"] = doc_id
        gap = dn.time.time() - z.last_call
        if gap < dn.PACE_SECONDS:
            dn.time.sleep(dn.PACE_SECONDS - gap)
        z.last_call = dn.time.time()
        z.calls += 1
        try:
            r = requests.get(url, headers=z._headers(), params=params, timeout=120)
        except Exception as e:
            return False, f"download error: {e}"
        ctype = r.headers.get("content-type", "")
        if r.status_code == 200 and "json" not in ctype and len(r.content) > 100:
            with open(path, "wb") as fh:
                fh.write(r.content)
            return True, ""
        if r.status_code == 429:
            log("  rate limited, waiting 90s")
            dn.time.sleep(90)
            return download_document(z, invoice_id, doc, path)
        if r.status_code == 401:
            z._refresh()
            return download_document(z, invoice_id, doc, path)
        why = r.text[:160].replace("\n", " ")
    return False, f"HTTP {r.status_code} {why}"


def to_pngs(path, prefix):
    """Any attached file to page PNGs at 200 dpi. Returns list of png paths ([] if not an image/pdf)."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        subprocess.run(["pdftoppm", "-r", "200", "-png", path, prefix], check=False,
                       capture_output=True)
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
            out = prefix + "-1.png"
            im.save(out)
            return [out]
        except Exception as e:
            log(f"  image not readable: {e}")
    return []


# ---------------------------------------------------------------- matching lines to pages

def page_rows(page):
    """Printed item rows of one page with their quantities, same way the engine reads them."""
    rows = page_items(page["text"])
    qmap = {k: v for k, v in page.get("qty", {}).items() if k != "__rows__"}
    qrows = page.get("qty", {}).get("__rows__", [])
    for r in rows:
        if r.get("qty") is None:
            for k, v in qmap.items():
                if r["sku"] and edit_distance(r["sku"], k) <= 1:
                    r["qty"] = v
                    break
    for name, q in qrows:
        if not any(squash(r["name"])[:14] == squash(name)[:14] for r in rows):
            rows.append({"sku": "", "name": name, "qty": q})
    return rows


def line_on_page(li, page, rows, used):
    """Is invoice line li printed on this page? Returns (found, qty or None, row index or None)."""
    text_sq = squash(page["text"])
    text_up = page["text"].upper()
    text_sq_tokens = set(re.findall(r"[A-Z0-9]{4,}", text_up.replace("-", "")))
    page_lines = [l.strip() for l in page["text"].split("\n") if l.strip()]
    sku = squash(li.get("sku", ""))
    name = li.get("name", "")

    def name_on_page():
        name_sq = squash(name)
        head = name_sq[:14]
        if len(name_sq) < 6:
            return False
        if head in text_sq:
            return True
        for l in page_lines:
            lsq = squash(l)
            if any(edit_distance(lsq[i:i + len(head)], head) <= 2 for i in range(0, max(1, len(lsq) - len(head) + 1))):
                return True
        return False

    n_ok = name_on_page()
    if len(sku) >= 4:
        # a printed row with this SKU, not already given to another line
        for exact in (True, False):
            for idx, r in enumerate(rows):
                if idx in used or not r["sku"]:
                    continue
                hit = (dn.canon(r["sku"]) == dn.canon(sku)) if exact else sku_same(r["sku"], sku)
                if hit:
                    return True, r.get("qty"), idx
        # SKU seen in the text (row line broken by OCR) and the name agrees
        seen = sku in text_sq or any(edit_distance(dn.canon(sku), dn.canon(t)) <= 1
                                     for t in text_sq_tokens if abs(len(t) - len(sku)) <= 1)
        if seen and n_ok:
            q = None
            for k, v in page.get("qty", {}).items():
                if k != "__rows__" and sku_same(k, sku):
                    q = v
            return True, q, None
        return False, None, None
    # no SKU in Zoho: the item name decides
    for idx, r in enumerate(rows):
        if idx in used or r["sku"]:
            continue
        if edit_distance(squash(r["name"])[:14], squash(name)[:14]) <= 2:
            return True, r.get("qty"), idx
    return n_ok, None, None


def customer_on_page(inv, page):
    toks = name_tokens(inv.get("customer_name", ""))
    text_tokens = set(tokens(page["text"]))
    hit = sum(1 for t in toks if t in text_tokens or
              dn.difflib.get_close_matches(t, text_tokens, n=1, cutoff=0.85 if len(t) > 3 else 0.75))
    return bool(toks) and hit >= max(1, len(toks) - 1)


# ---------------------------------------------------------------- one invoice

def audit_invoice(z, inv_list_row):
    """Returns a result dict for one invoice that has files attached."""
    inv_id = inv_list_row["invoice_id"]
    number = inv_list_row["invoice_number"]
    inv = z.full_invoice(inv_id)
    # goods only: a service line (delivery charge, labour) is not something a delivery note has to show
    lines = [li for li in inv.get("line_items", []) if (li.get("name") or li.get("sku"))
             and float(li.get("quantity") or 0) > 0 and li.get("product_type", "goods") != "service"]
    docs = inv.get("documents") or []
    res = {"invoice": number, "invoice_id": inv_id, "date": inv.get("date", ""), "customer": inv.get("customer_name", ""),
           "status": inv.get("status", ""), "fulfillment": inv.get("cf_fulfillment_method", ""),
           "salesperson": inv.get("salesperson_name", ""), "files": [d.get("file_name", "") for d in docs], "pages": 0, "dn_pages": 0,
           "lines": [], "result": "", "why": "", "numbers_seen": []}
    if not docs:
        res["result"] = "no attachment"
        return res
    folder = f"{DOCS}/{re.sub(r'[^A-Za-z0-9-]', '_', number)}"
    os.makedirs(folder, exist_ok=True)
    pngs, bad_files = [], []
    for k, d in enumerate(docs):
        fname = d.get("file_name", f"file{k}")
        ext = os.path.splitext(fname)[1].lower() or ".bin"
        path = f"{folder}/{k}{ext}"
        ok, why = download_document(z, inv_id, d, path)
        if not ok:
            bad_files.append(f"{fname}: {why}")
            continue
        got = to_pngs(path, f"{folder}/p{k}")
        if not got:
            bad_files.append(f"{fname}: not a PDF or image")
        pngs += got
    res["pages"] = len(pngs)
    if not pngs:
        res["result"] = "check by hand"
        res["why"] = "attached file could not be opened as a scan (" + "; ".join(bad_files)[:300] + ")"
        return res
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        pages = list(ex.map(read_page, pngs))
    # pages that look like a delivery note of this invoice (or of nobody in particular)
    dn_pages = []
    for p in pages:
        if p.get("kind") == "cn":
            continue
        if p.get("number"):
            res["numbers_seen"].append(p["number"])
        dn_pages.append(p)
    res["dn_pages"] = len(dn_pages)
    delivered = {id(li): {"qty": 0.0, "found": 0, "unreadable": 0, "other_number": 0} for li in lines}
    for p in dn_pages:
        rows = page_rows(p)
        used = set()
        other = bool(p.get("number")) and p["number"] != number.upper()
        for li in lines:
            found, q, idx = line_on_page(li, p, rows, used)
            if not found:
                continue
            if idx is not None:
                used.add(idx)
            d = delivered[id(li)]
            d["found"] += 1
            if other:
                d["other_number"] += 1
            if q is None:
                d["unreadable"] += 1
            else:
                d["qty"] = round(d["qty"] + q, 2)
    missing, short, covered = [], [], []
    for li in lines:
        d = delivered[id(li)]
        row = {"item": li.get("name", ""), "sku": li.get("sku", ""), "invoiced": float(li.get("quantity") or 0),
               "on_notes": d["qty"], "pages_found": d["found"], "unreadable": d["unreadable"], "other_number": d["other_number"]}
        if d["found"] == 0:
            missing.append(row)
        elif d["unreadable"] == 0 and d["qty"] + 1e-6 < row["invoiced"]:
            short.append(row)
        else:
            covered.append(row)
    res["lines"] = {"missing": missing, "short": short, "covered": covered}
    any_rows = any(page_rows(p) for p in dn_pages)
    any_customer = any(customer_on_page(inv, p) for p in dn_pages)
    any_number = any(n == number.upper() for n in res["numbers_seen"])
    if not lines:
        res["result"] = "no items"
    elif not dn_pages or (not any_rows and not any_number and not any_customer):
        res["result"] = "check by hand"
        res["why"] = ("the attached file does not read as a delivery note (no invoice number, no customer name, no item rows found by OCR); "
                      "open it and check" + (f"; files: {', '.join(res['files'])[:200]}"))
    elif not missing and not short:
        res["result"] = "covered"
    elif missing and len(missing) == len(lines):
        res["result"] = "check by hand"
        if any_number and any_customer:
            res["why"] = ("the attached note carries this invoice number and customer, but none of the invoice items as they are now in Zoho "
                          "are on it; the invoice was probably edited after the note was printed, or the scan is too faint to read the items")
        elif res["numbers_seen"]:
            res["why"] = (f"the attached note reads as number {', '.join(sorted(set(res['numbers_seen'])))}, not this invoice, "
                          "and none of this invoice's items are on it; probably the wrong note is attached")
        else:
            res["why"] = ("a file is attached but the invoice number on it is not readable and none of the invoice items were found; "
                          "the scan is too faint or it is not a delivery note")
    elif missing:
        res["result"] = "partial"
    else:
        res["result"] = "short"
    if bad_files:
        res["why"] = (res["why"] + "; " if res["why"] else "") + "files not opened: " + "; ".join(bad_files)[:200]
    shutil.rmtree(folder, ignore_errors=True)
    return res


# ---------------------------------------------------------------- main

def main():
    if not dn.ORG:
        raise SystemExit("ZOHO_ORG_ID_DXB is not set")
    z = Zoho()
    log(f"audit of invoices dated {AUDIT_FROM} to {AUDIT_TO}")
    invoices = z.invoices_between(AUDIT_FROM, AUDIT_TO)
    log(f"{len(invoices)} invoices in Zoho for that range")
    invoices = [i for i in invoices if i.get("status") != "draft"]
    invoices.sort(key=lambda i: (i.get("date", ""), i.get("invoice_number", "")))
    void = [i for i in invoices if i.get("status") == "void"]
    live = [i for i in invoices if i.get("status") != "void"]
    no_file = [i for i in live if not i.get("has_attachment") and not i.get("documents")]
    with_file = [i for i in live if i not in no_file]
    log(f"{len(void)} void (no note needed), {len(no_file)} with no file attached, {len(with_file)} with files to read")

    results, done = [], 0
    for n, row in enumerate(with_file, 1):
        if LIMIT and done >= LIMIT:
            log(f"AUDIT_LIMIT {LIMIT} reached, stopping early (test run)")
            break
        try:
            r = audit_invoice(z, row)
        except Exception as e:
            r = {"invoice": row.get("invoice_number"), "invoice_id": row.get("invoice_id"), "date": row.get("date", ""),
                 "customer": row.get("customer_name", ""), "status": row.get("status", ""), "files": [], "pages": 0,
                 "fulfillment": row.get("cf_fulfillment_method", ""), "salesperson": row.get("salesperson_name", ""),
                 "dn_pages": 0, "lines": [], "result": "check by hand", "why": f"error while reading: {e}", "numbers_seen": []}
        results.append(r)
        done += 1
        if r["result"] == "no attachment":
            no_file.append(row)
        log(f"[{n}/{len(with_file)}] {r['invoice']} {r['customer'][:30]}: {r['result']}"
            + (f" ({len(r['lines']['missing'])} line(s) missing)" if r["result"] == "partial" else "")
            + (f" ({r['why'][:80]})" if r["why"] and r["result"] == "check by hand" else ""))
        if n % 50 == 0:
            write_outputs(results, no_file, void, len(with_file), partial=True)
    write_outputs(results, no_file, void, len(with_file), partial=False)
    log(f"done. Zoho calls: {z.calls}")


def link(inv_id):
    return BOOKS + inv_id if inv_id else ""


def write_outputs(results, no_file, void, total_with_file, partial):
    results = [r for r in results if r["result"] != "no attachment"]
    counts = {k: sum(1 for r in results if r["result"] == k) for k in ("covered", "partial", "short", "check by hand", "no items")}
    # CSVs
    with open(f"{OUT}/no-delivery-note.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Invoice", "Date", "Customer", "Salesperson", "Fulfillment", "Status", "Total AED", "Link"])
        for i in sorted(no_file, key=lambda i: (i.get("date", ""), i.get("invoice_number", ""))):
            w.writerow([i.get("invoice_number"), i.get("date"), i.get("customer_name"), i.get("salesperson_name", ""),
                        i.get("cf_fulfillment_method", ""), i.get("status"), i.get("total"), link(i.get("invoice_id"))])
    with open(f"{OUT}/items-with-no-note.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Invoice", "Date", "Customer", "Fulfillment", "Salesperson", "Item", "SKU", "Invoiced qty", "Qty on attached notes",
                    "Missing qty", "Files attached", "Note numbers read on the pages", "Link"])
        for r in results:
            if r["result"] != "partial":
                continue
            for m in r["lines"]["missing"]:
                w.writerow([r["invoice"], r["date"], r["customer"], r["fulfillment"], r["salesperson"], m["item"], m["sku"], m["invoiced"], 0,
                            m["invoiced"], "; ".join(r["files"]), ", ".join(sorted(set(r["numbers_seen"]))), link(r["invoice_id"])])
    with open(f"{OUT}/delivered-short.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Invoice", "Date", "Customer", "Fulfillment", "Salesperson", "Item", "SKU", "Invoiced qty", "Qty on attached notes", "Balance", "Link"])
        for r in results:
            if r["result"] not in ("partial", "short"):
                continue
            for m in r["lines"]["short"]:
                w.writerow([r["invoice"], r["date"], r["customer"], r["fulfillment"], r["salesperson"], m["item"], m["sku"], m["invoiced"],
                            m["on_notes"], round(m["invoiced"] - m["on_notes"], 2), link(r["invoice_id"])])
    with open(f"{OUT}/check-by-hand.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Invoice", "Date", "Customer", "Fulfillment", "Salesperson", "Files attached", "Pages", "Why", "Link"])
        for r in results:
            if r["result"] == "check by hand":
                w.writerow([r["invoice"], r["date"], r["customer"], r["fulfillment"], r["salesperson"], "; ".join(r["files"]), r["pages"],
                            r["why"], link(r["invoice_id"])])
    with open(f"{OUT}/all-invoices.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Invoice", "Date", "Customer", "Fulfillment", "Status", "Files", "Pages", "Result", "Lines", "Lines on notes", "Lines missing", "Note"])
        for r in results:
            ln = r["lines"] if isinstance(r["lines"], dict) else {"missing": [], "short": [], "covered": []}
            w.writerow([r["invoice"], r["date"], r["customer"], r["fulfillment"], r["status"], len(r["files"]), r["pages"], r["result"],
                        sum(len(v) for v in ln.values()), len(ln["covered"]) + len(ln["short"]), len(ln["missing"]), r["why"]])
    # Excel
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment
        from openpyxl.utils import get_column_letter
        wb = Workbook()
        ws = wb.active
        ws.title = "Summary"
        rows = [["Delivery note audit", f"invoices dated {AUDIT_FROM} to {AUDIT_TO}, Dubai"],
                ["Made", datetime.now(DUBAI).strftime("%Y-%m-%d %H:%M") + (" (PARTIAL, run still going)" if partial else "")],
                [],
                ["Invoices with NO delivery note attached", len(no_file)],
                ["Invoices where some items have no note (partial)", counts["partial"]],
                ["Invoices delivered short (all items on notes, qty less than invoiced)", counts["short"]],
                ["Invoices to check by hand (file attached but not readable)", counts["check by hand"]],
                ["Invoices fully covered", counts["covered"]],
                ["Invoices with no item lines", counts["no items"]],
                ["Void invoices (no note needed)", len(void)],
                ["Invoices with files read so far", f"{len(results)} of {total_with_file}"],
                [],
                ["How to read it", "No delivery note: nothing is attached in Zoho, find the signed note and attach it."],
                ["", "Items with no note: a note is attached but these item lines are on none of its pages. Find the note for those items."],
                ["", "Delivered short: the item is on the note but the quantity on the note is less than invoiced."],
                ["", "Check by hand: a file is attached but OCR could not read it as a delivery note. Open the invoice and look."]]
        for r in rows:
            ws.append(r)
        ws["A1"].font = Font(bold=True, size=14)
        ws.column_dimensions["A"].width = 62
        ws.column_dimensions["B"].width = 110
        for name, path in (("No delivery note", "no-delivery-note.csv"), ("Items with no note", "items-with-no-note.csv"),
                           ("Delivered short", "delivered-short.csv"), ("Check by hand", "check-by-hand.csv"),
                           ("All invoices with files", "all-invoices.csv")):
            s = wb.create_sheet(name)
            with open(f"{OUT}/{path}", newline="") as fh:
                for i, row in enumerate(csv.reader(fh)):
                    s.append(row)
            for c in s[1]:
                c.font = Font(bold=True, color="FFFFFF")
                c.fill = PatternFill("solid", fgColor="1D4ED8")
                c.alignment = Alignment(vertical="center")
            s.freeze_panes = "A2"
            if s.max_row > 1:
                s.auto_filter.ref = s.dimensions
            for col in range(1, s.max_column + 1):
                width = max((len(str(s.cell(row=r, column=col).value or "")) for r in range(1, min(s.max_row, 300) + 1)), default=8)
                s.column_dimensions[get_column_letter(col)].width = min(max(10, width + 2), 70)
        xlsx = f"{OUT}/delivery-note-audit-{AUDIT_FROM}-to-{AUDIT_TO}.xlsx"
        wb.save(xlsx)
    except Exception as e:
        log(f"excel not written: {e}")
        xlsx = ""
    if not partial:
        subject = (f"Delivery note audit {AUDIT_FROM} to {AUDIT_TO}: {len(no_file)} with no note, "
                   f"{counts['partial']} with items missing, {counts['check by hand']} to check by hand")
        html = ("<div style='font-family:Arial,sans-serif;font-size:14px'>"
                f"<p>Audit of {len(no_file) + len(results) + len(void)} invoices dated {AUDIT_FROM} to {AUDIT_TO} (Dubai).</p><ul>"
                f"<li><b>{len(no_file)}</b> invoices with no delivery note attached</li>"
                f"<li><b>{counts['partial']}</b> invoices where some items are on no note</li>"
                f"<li><b>{counts['short']}</b> invoices delivered short</li>"
                f"<li><b>{counts['check by hand']}</b> invoices to check by hand (file attached, not readable)</li>"
                f"<li><b>{counts['covered']}</b> invoices fully covered</li>"
                f"<li><b>{len(void)}</b> void (no note needed)</li></ul>"
                "<p>Details in the attached Excel.</p></div>")
        dn.REPORT_TO, dn.REPORT_CC = AUDIT_EMAIL, []
        dn.send_daily_email(html, subject, [xlsx] if xlsx else [f"{OUT}/no-delivery-note.csv", f"{OUT}/items-with-no-note.csv",
                                                                   f"{OUT}/check-by-hand.csv"])


if __name__ == "__main__":
    main()
