#!/usr/bin/env python3
"""
Attach scanned delivery notes to their Zoho Books invoices, with verification.

How it works, in plain words:
  1. Every PDF in the inbox folder is a day's scan of delivery notes, one note per page.
  2. Each page is read with OCR. From the page we take: the number printed top right
     ("# LB26-20198", which is the invoice number), the customer name, the date,
     the customer TRN, and the item SKUs and names.
  3. Each page is cut out into its own small PDF named DN-<invoice number>.pdf.
  4. The invoice is looked up in Zoho Books by number (void or not, it does not matter).
  5. VERIFICATION. The page is only attached if the invoice in Zoho agrees with the page on
     ALL of these: customer name, date, items (at least half of the invoice lines are found
     on the page), and TRN (only if the TRN was readable on the page). One disagreement and
     the page goes to NEEDS CHECK, never attached.
  6. If the number could not be read, or the invoice it names fails verification, the script
     searches every invoice of the same date for the same customer and runs the same checks.
     If exactly one invoice passes, the page is attached there and the report says the number
     was corrected. If none or more than one pass, NEEDS CHECK.
  7. If the invoice already has a file with the same name it is skipped (safe to re-run).
  8. The report lists every page with its result and which checks passed or failed, plus the
     invoices of that day that have no delivery note at all.

Modes:
  dry  = read, cut, look up, verify, write the report. Uploads nothing.
  live = everything in dry, plus the upload.

Environment variables (set by the GitHub workflow):
  ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN, ZOHO_ORG_ID_DXB
  ZOHO_ACCOUNTS_URL  default https://accounts.zoho.com
  ZOHO_API_BASE      default https://www.zohoapis.com/books/v3
  MODE               dry or live
  INBOX              folder of scanned PDFs, default dn-inbox
  ONLY_FILE          optional single file name inside INBOX
  OUT                output folder, default out
"""

import csv
import difflib
import glob
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

import requests
from PIL import Image, ImageOps
from pypdf import PdfReader, PdfWriter

MODE = (os.environ.get("MODE") or (sys.argv[1] if len(sys.argv) > 1 else "dry")).lower()
INBOX = os.environ.get("INBOX", "dn-inbox")
ONLY_FILE = os.environ.get("ONLY_FILE", "").strip()
OUT = os.environ.get("OUT", "out")
ACCOUNTS = os.environ.get("ZOHO_ACCOUNTS_URL", "https://accounts.zoho.com").rstrip("/")
API = os.environ.get("ZOHO_API_BASE", "https://www.zohoapis.com/books/v3").rstrip("/")
ORG = os.environ.get("ZOHO_ORG_ID_DXB", "")
PACE_SECONDS = float(os.environ.get("PACE_SECONDS", "1.5"))
OWN_TRN = "100493956500003"   # Lapiz Blue's own TRN, printed on every note, ignored

NUM_RE = re.compile(r"#\s*(LB[A-Z]*\s?\d{2}\s?-\s?\d{4,6})")
LOOSE_RE = re.compile(r"(LB[A-Z]*\s?\d{2}\s?-\s?\d{4,6})")
DATE_RE = re.compile(r"\b(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(20\d{2})\b", re.IGNORECASE)
TRN_RE = re.compile(r"\b(1\d{14})\b")
BILL_RE = re.compile(r"Bill\s*To.*?\n(.+)", re.IGNORECASE)

for d in ("split", "unmatched", "needs-check", "pages"):
    os.makedirs(f"{OUT}/{d}", exist_ok=True)


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------- OCR

def ocr_text(png, psm="3", whitelist=None):
    cmd = ["tesseract", png, "-", "--psm", psm]
    if whitelist:
        cmd += ["-c", f"tessedit_char_whitelist={whitelist}"]
    return subprocess.run(cmd, capture_output=True, text=True).stdout


def norm_number(s):
    s = s.replace(" ", "").upper()
    head, _, tail = s.partition("-")
    # digits after the dash are sometimes read as letters on a weak scan
    tail = tail.translate(str.maketrans("OIlSBZ", "011582"))
    return f"{head}-{tail}"


def read_page(png):
    """OCR one page. Returns dict with number, customer line, date, trns, and the raw text."""
    text = ocr_text(png, "3")
    num = None
    m = NUM_RE.search(text)
    if m:
        num = norm_number(m.group(1))
    else:
        # second pass: tight crop of the band under "DELIVERY NOTE", enlarged and thresholded
        im = Image.open(png)
        w, h = im.size
        crop = im.crop((int(w * 0.55), int(h * 0.12), w, int(h * 0.20)))
        crop = ImageOps.grayscale(crop).resize((crop.width * 3, crop.height * 3))
        crop = crop.point(lambda x: 0 if x < 150 else 255)
        cpath = png.replace(".png", "-band.png")
        crop.save(cpath)
        for psm in ("6", "11"):
            m = LOOSE_RE.search(ocr_text(cpath, psm, "#LBSJ0123456789- "))
            if m:
                num = norm_number(m.group(1))
                break
    date = None
    m = DATE_RE.search(text)
    if not m:
        # the date sits in the right hand column; read that column on its own
        im = Image.open(png)
        w, h = im.size
        col = im.crop((int(w * 0.5), int(h * 0.22), w, int(h * 0.55)))
        cpath = png.replace(".png", "-col.png")
        col.save(cpath)
        coltext = ocr_text(cpath, "6")
        text += "\n" + coltext
        m = DATE_RE.search(coltext)
    if m:
        try:
            date = datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%d %b %Y").strftime("%Y-%m-%d")
        except ValueError:
            date = None
    customer = ""
    lines = text.split("\n")
    for i, l in enumerate(lines):
        if re.search(r"Bill\s*To", l, re.IGNORECASE):
            after = [x.strip() for x in lines[i + 1:i + 4] if x.strip()]
            customer = after[0] if after else ""
            break
    customer = re.split(r"\s+(Fulfillment|Date|Ref|VAT)\b", customer)[0].strip()
    trns = [t for t in TRN_RE.findall(text) if t != OWN_TRN]
    return {"text": text, "number": num, "customer": customer, "date": date, "trns": trns}


# ---------------------------------------------------------------- verification

GENERIC = set("""LLC LLCS LTD CO EST SPC DMCC FZE FZC FZCO INC COMPANY AND THE FOR OF BRANCH GENERAL TRADING TRADERS
BUILDING MATERIAL MATERIALS CONTRACTING CONTRACTORS CONSTRUCTION TECHNICAL SERVICES SERVICE DESIGN DESIGNS
INTERIORS INTERIOR FIT OUT FITOUT DECORATION DECOR WORKS WORK LANDSCAPING LANDSCAPE ENGINEERING INDUSTRIES
INDUSTRY MAINTENANCE SOLUTIONS SOLUTION GOODS WHOLESALERS WHOLESALE SUPPLIES SUPPLY DUBAI SHARJAH ABU DHABI
UAE ART GALLERY LAPIZ BLUE MR MRS""".split())


def tokens(s):
    return [t for t in re.sub(r"[^A-Z0-9 ]+", " ", s.upper()).split() if len(t) >= 3]


def name_tokens(s):
    """Words of a customer name that actually identify it (generic trade words removed)."""
    toks = tokens(s)
    keep = [t for t in toks if t not in GENERIC]
    return keep if keep else toks


def squash(s):
    return re.sub(r"[^A-Z0-9]+", "", s.upper())


def verify(page, inv, strict=False):
    """Compare an OCR'd page with a full Zoho invoice. Returns (ok, checks, note)."""
    text_up = page["text"].upper()
    text_tokens = set(tokens(page["text"]))
    text_sq = squash(page["text"])
    checks = {}

    # customer name: every identifying word of the name appears on the page (fuzzy, OCR tolerant);
    # generic words like LLC, CONTRACTING, TRADING do not count
    name_toks = name_tokens(inv.get("customer_name", ""))
    hit = 0
    for t in name_toks:
        if t in text_tokens or difflib.get_close_matches(t, text_tokens, n=1, cutoff=0.85):
            hit += 1
    checks["name"] = bool(name_toks) and hit == len(name_toks)

    # date: the date printed on the note equals the invoice date
    checks["date"] = bool(page["date"]) and page["date"] == inv.get("date")

    # items: at least half of the invoice lines appear on the page, by SKU or by item name
    lines = [li for li in inv.get("line_items", []) if li.get("name") or li.get("sku")]
    matched = 0
    for li in lines:
        sku = squash(li.get("sku", ""))
        name = squash(li.get("name", ""))[:14]
        if (len(sku) >= 4 and sku in text_sq) or (len(name) >= 6 and name in text_sq):
            matched += 1
    need = len(lines) if strict else max(1, (len(lines) + 1) // 2)
    checks["items"] = bool(lines) and matched >= need
    items_note = f"{matched}/{len(lines)} lines found"

    # TRN: only judged when the page shows a customer TRN and the invoice has one
    inv_trn = (inv.get("tax_reg_no") or "").strip()
    if inv_trn and (page["trns"] or strict):
        checks["trn"] = inv_trn in page["trns"]
        trn_note = "TRN match" if checks["trn"] else f"TRN on page {page['trns']} != invoice {inv_trn}"
    else:
        trn_note = "TRN not checked"

    ok = all(checks.values())
    note = ", ".join(f"{k}:{'ok' if v else 'FAIL'}" for k, v in checks.items()) + f"; {items_note}; {trn_note}"
    return ok, checks, note


def edit_distance(a, b):
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


# ---------------------------------------------------------------- Zoho

class Zoho:
    def __init__(self):
        self.token = None
        self.token_time = 0
        self.calls = 0
        self.last_call = 0
        self._full = {}

    def _refresh(self):
        r = requests.post(f"{ACCOUNTS}/oauth/v2/token", data={
            "grant_type": "refresh_token",
            "client_id": os.environ["ZOHO_CLIENT_ID"],
            "client_secret": os.environ["ZOHO_CLIENT_SECRET"],
            "refresh_token": os.environ["ZOHO_REFRESH_TOKEN"],
        }, timeout=30)
        j = r.json()
        if "access_token" not in j:
            raise SystemExit(f"Zoho token refresh failed: {j}")
        self.token = j["access_token"]
        self.token_time = time.time()

    def _headers(self):
        if not self.token or time.time() - self.token_time > 50 * 60:
            self._refresh()
        return {"Authorization": f"Zoho-oauthtoken {self.token}"}

    def call(self, method, path, **kw):
        gap = time.time() - self.last_call
        if gap < PACE_SECONDS:
            time.sleep(PACE_SECONDS - gap)
        params = kw.pop("params", {})
        params["organization_id"] = ORG
        for attempt in range(4):
            self.last_call = time.time()
            self.calls += 1
            r = requests.request(method, f"{API}{path}", headers=self._headers(),
                                 params=params, timeout=120, **kw)
            try:
                j = r.json()
            except ValueError:
                j = {"code": -1, "message": r.text[:200]}
            if r.status_code == 429 or j.get("code") == 44:
                log(f"  rate limited, waiting 90s (attempt {attempt + 1})")
                time.sleep(90)
                continue
            if r.status_code == 401:
                self._refresh()
                continue
            return r.status_code, j
        return r.status_code, j

    def find_invoice(self, number):
        code, j = self.call("GET", "/invoices", params={"invoice_number": number})
        invs = [i for i in j.get("invoices", []) if i.get("invoice_number", "").upper() == number]
        return invs[0] if invs else None

    def full_invoice(self, invoice_id):
        """Full invoice with line items, TRN and attachments (cached)."""
        if invoice_id not in self._full:
            code, j = self.call("GET", f"/invoices/{invoice_id}")
            self._full[invoice_id] = j.get("invoice", {})
        return self._full[invoice_id]

    def invoices_on_date(self, ymd):
        """Every invoice dated ymd (all statuses, void included)."""
        found, page = [], 1
        while True:
            code, j = self.call("GET", "/invoices", params={"date": ymd, "per_page": 200, "page": page})
            found += j.get("invoices", [])
            if not j.get("page_context", {}).get("has_more_page"):
                return found
            page += 1

    def attach(self, invoice_id, pdf_path):
        with open(pdf_path, "rb") as fh:
            code, j = self.call("POST", f"/invoices/{invoice_id}/attachment",
                                params={"can_send_in_mail": "false"},
                                files={"attachment": (os.path.basename(pdf_path), fh, "application/pdf")})
        return code, j


# ---------------------------------------------------------------- main

def main():
    if MODE not in ("dry", "live"):
        raise SystemExit("MODE must be dry or live")
    if not ORG:
        raise SystemExit("ZOHO_ORG_ID_DXB is not set")

    pdfs = sorted(glob.glob(os.path.join(INBOX, "*.pdf")) + glob.glob(os.path.join(INBOX, "*.PDF")))
    if ONLY_FILE:
        pdfs = [p for p in pdfs if os.path.basename(p) == ONLY_FILE]
    if not pdfs:
        log(f"No PDFs found in {INBOX}" + (f" named {ONLY_FILE}" if ONLY_FILE else ""))
        return

    z = Zoho()
    rows = []
    processed = []
    invoice_dates = set()
    matched_numbers = set()
    day_cache = {}

    def same_day(ymd):
        if ymd not in day_cache:
            day_cache[ymd] = z.invoices_on_date(ymd)
        return day_cache[ymd]

    for pdf in pdfs:
        base = os.path.splitext(os.path.basename(pdf))[0]
        log(f"Reading {pdf}")
        reader = PdfReader(pdf)
        n = len(reader.pages)
        prefix = f"{OUT}/pages/{base}"
        subprocess.run(["pdftoppm", "-r", "200", "-png", pdf, prefix], check=True)
        pngs = sorted(p for p in glob.glob(f"{prefix}-*.png") if not p.endswith("-band.png"))
        if len(pngs) != n:
            raise SystemExit(f"{pdf}: rasterised {len(pngs)} pages but PDF has {n}")

        # 1. read every page
        pages = []
        for idx, png in enumerate(pngs):
            p = read_page(png)
            p["page"] = idx + 1
            pages.append(p)
            log(f"  page {idx + 1:>3}: {p['number'] or 'NUMBER NOT READ'} | {p['customer'][:40]} | {p['date']}")

        # 2. group consecutive pages that carry the same number (multi page note)
        groups = []
        for p in pages:
            if p["number"] and groups and groups[-1]["number"] == p["number"] \
                    and groups[-1]["pages"][-1]["page"] == p["page"] - 1:
                groups[-1]["pages"].append(p)
            else:
                groups.append({"number": p["number"], "pages": [p]})

        # 3. cut, look up, verify, attach
        seen_in_this_file = {}
        for g in groups:
            first = g["pages"][0]
            # merge OCR of all pages in the group for verification
            page = {"text": "\n".join(p["text"] for p in g["pages"]), "number": g["number"],
                    "customer": first["customer"], "date": first["date"],
                    "trns": sorted({t for p in g["pages"] for t in p["trns"]})}
            pg_label = ",".join(str(p["page"]) for p in g["pages"])
            num = g["number"]
            row = {"source_pdf": os.path.basename(pdf), "pages": pg_label, "number_on_page": num or "",
                   "customer_on_page": first["customer"], "date_on_page": first["date"] or "",
                   "invoice_number": "", "customer": "", "invoice_id": "", "invoice_status": "",
                   "result": "", "checks": "", "detail": ""}
            w = PdfWriter()
            for p in g["pages"]:
                w.add_page(reader.pages[p["page"] - 1])

            def save(folder, fname):
                outp = f"{OUT}/{folder}/{fname}"
                with open(outp, "wb") as fh:
                    w.write(fh)
                return outp

            if num and num in seen_in_this_file:
                row["result"] = "DUPLICATE PAGE"
                row["detail"] = f"same number already on page(s) {seen_in_this_file[num]} of this file"
                rows.append(row)
                continue

            # a. the invoice the page names
            target, note = None, ""
            if num:
                inv = z.find_invoice(num)
                if inv:
                    full = z.full_invoice(inv["invoice_id"])
                    ok, checks, note = verify(page, full)
                    if ok:
                        target = full
                    else:
                        log(f"  {num}: named invoice FAILS verification ({note})")
                else:
                    note = f"no invoice numbered {num}"

            # b. fallback: same date, same customer, exactly one invoice that passes all checks
            corrected = False
            if target is None and page["date"]:
                cands = []
                for inv in same_day(page["date"]):
                    probe = {"customer_name": inv.get("customer_name", ""), "date": inv.get("date"),
                             "line_items": [], "tax_reg_no": ""}
                    _, c, _ = verify(page, probe)
                    if c["name"]:
                        cands.append(inv)
                passing = []
                for inv in cands[:8]:
                    cnum = inv.get("invoice_number", "").upper()
                    if num and edit_distance(num, cnum) > 2:
                        continue
                    full = z.full_invoice(inv["invoice_id"])
                    ok, checks, n2 = verify(page, full, strict=True)
                    if ok:
                        passing.append((full, n2 + " (strict)"))
                if len(passing) == 1:
                    target, note = passing[0]
                    corrected = (num != target.get("invoice_number", "").upper())
                elif len(passing) > 1:
                    note += f"; {len(passing)} same-day invoices for this customer pass all checks: " + \
                            ", ".join(p[0].get("invoice_number", "") for p in passing)
                else:
                    note += f"; searched {len(cands)} same-day invoice(s) for this customer, none pass all checks"

            if target is None:
                fname = f"{base}-page-{first['page']}.pdf"
                folder = "needs-check" if num else "unmatched"
                outp = save(folder, fname)
                row["result"] = "NEEDS CHECK" if num else "NUMBER NOT READ"
                row["checks"] = note
                row["detail"] = f"saved as {outp}; a person must attach this one"
                rows.append(row)
                log(f"  page {pg_label}: {row['result']} ({note})")
                continue

            inv_num = target.get("invoice_number", "").upper()
            row.update({"invoice_number": inv_num, "customer": target.get("customer_name", ""),
                        "invoice_id": target["invoice_id"], "invoice_status": target.get("status", ""),
                        "checks": note})
            invoice_dates.add(target.get("date", ""))
            matched_numbers.add(inv_num)
            seen_in_this_file[inv_num] = pg_label
            fname = f"DN-{inv_num}.pdf"
            outp = save("split", fname)
            corr = f" (number on page read as {num}, corrected to {inv_num})" if corrected else ""

            existing = [d.get("file_name", "") for d in (target.get("documents") or [])]
            if fname in existing:
                row["result"] = "ALREADY ATTACHED"
                row["detail"] = "a file with this name is already on the invoice, skipped" + corr
                rows.append(row)
                log(f"  {inv_num}: already attached, skipped")
                continue

            if MODE == "dry":
                row["result"] = "WOULD ATTACH"
                row["detail"] = f"dry run; verified{corr}; {len(existing)} existing attachment(s)"
                rows.append(row)
                log(f"  {inv_num}: verified, would attach ({target.get('status')}){corr}")
                continue

            code, j = z.attach(target["invoice_id"], outp)
            if j.get("code") == 0:
                row["result"] = "ATTACHED"
                row["detail"] = (j.get("message", "") or "attached") + corr
                log(f"  {inv_num}: attached{corr}")
            else:
                row["result"] = "ATTACH FAILED"
                row["detail"] = f"HTTP {code} code {j.get('code')}: {j.get('message')}"
                log(f"  {inv_num}: ATTACH FAILED {row['detail']}")
            rows.append(row)

        processed.append(pdf)

    # 4. report
    with open(f"{OUT}/report.csv", "w", newline="") as fh:
        wtr = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        wtr.writeheader()
        wtr.writerows(rows)

    counts = {}
    for r in rows:
        counts[r["result"]] = counts.get(r["result"], 0) + 1
    lines = [f"# Delivery note attach report ({MODE} mode)", "",
             f"Run: {datetime.now().strftime('%Y-%m-%d %H:%M')}   API calls: {z.calls}", "",
             f"Pages read: {sum(len(r['pages'].split(',')) for r in rows)}   Notes: {len(rows)}", ""]
    for k in ("ATTACHED", "WOULD ATTACH", "ALREADY ATTACHED", "NEEDS CHECK",
              "NUMBER NOT READ", "DUPLICATE PAGE", "ATTACH FAILED"):
        if counts.get(k):
            lines.append(f"- {k}: {counts[k]}")
    corrected = [r for r in rows if "corrected to" in r["detail"]]
    if corrected:
        lines.append(f"- of which number corrected by verification: {len(corrected)}")
    lines.append("")
    for k in ("ATTACH FAILED", "NEEDS CHECK", "NUMBER NOT READ", "DUPLICATE PAGE"):
        bad = [r for r in rows if r["result"] == k]
        if bad:
            lines.append(f"## {k} ({len(bad)})")
            for r in bad:
                lines.append(f"- {r['source_pdf']} page {r['pages']} | on page: {r['number_on_page']} "
                             f"{r['customer_on_page']} {r['date_on_page']} | {r['checks']} | {r['detail']}")
            lines.append("")

    # 5. invoices of the same day(s) with no delivery note in this batch and no attachment yet
    missing = []
    for d in sorted(x for x in invoice_dates if x):
        for inv in same_day(d):
            if inv.get("invoice_number", "").upper() in matched_numbers or inv.get("has_attachment"):
                continue
            missing.append(inv)
    lines.append(f"## NO DELIVERY NOTE RECEIVED ({len(missing)}) for invoice date(s) "
                 f"{', '.join(sorted(x for x in invoice_dates if x))}")
    lines.append("Invoices dated the same day as this batch that have no note in the scan and no attachment yet.")
    for inv in missing:
        lines.append(f"- {inv.get('invoice_number')} | {inv.get('customer_name')} | {inv.get('status')} | "
                     f"{inv.get('cf_fulfillment_method', '')} | {inv.get('salesperson_name', '')}")
    lines.append("")
    with open(f"{OUT}/missing.csv", "w", newline="") as fh:
        wtr = csv.writer(fh)
        wtr.writerow(["invoice_number", "customer", "status", "fulfillment_method", "salesperson", "date"])
        for inv in missing:
            wtr.writerow([inv.get("invoice_number"), inv.get("customer_name"), inv.get("status"),
                          inv.get("cf_fulfillment_method", ""), inv.get("salesperson_name", ""), inv.get("date")])

    lines.append("## Every note")
    for r in rows:
        lines.append(f"- p{r['pages']} {r['invoice_number'] or r['number_on_page'] or '(no number)'} | "
                     f"{r['customer']} | {r['invoice_status']} | {r['result']} | {r['checks']}")
    with open(f"{OUT}/report.md", "w") as fh:
        fh.write("\n".join(lines) + "\n")
    with open(f"{OUT}/processed.json", "w") as fh:
        json.dump(processed, fh)
    print("\n".join(lines))

    if any(r["result"] == "ATTACH FAILED" for r in rows):
        sys.exit(2)


if __name__ == "__main__":
    main()
