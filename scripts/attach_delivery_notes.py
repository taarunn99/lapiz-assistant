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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests
from datetime import timedelta, timezone
DUBAI = timezone(timedelta(hours=4))
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
OCR_WORKERS = int(os.environ.get("OCR_WORKERS", "4"))              # pages read in parallel (runner has 4 cores)
os.environ.setdefault("OMP_THREAD_LIMIT", "1")                      # one thread per tesseract process, else parallel OCR crawls
OWN_TRN = "100493956500003"   # Lapiz Blue's own TRN, printed on every note, ignored
OVERDUE_DAYS = int(os.environ.get("OVERDUE_DAYS", "2"))          # a note is overdue this many days after the invoice date
OVERDUE_FROM = os.environ.get("OVERDUE_FROM", "2026-10-22")       # invoices before the system went live are not chased
CLIQ_WEBHOOK = os.environ.get("CLIQ_DN_WEBHOOK", "").strip()      # full post URL, or just the webhook token
CLIQ_COMPANY = os.environ.get("CLIQ_COMPANY_ID", "719269379")
CLIQ_CHANNEL = os.environ.get("CLIQ_CHANNEL", "deliverynotes")
if CLIQ_WEBHOOK and "://" not in CLIQ_WEBHOOK:
    if "zapikey=" in CLIQ_WEBHOOK:
        CLIQ_WEBHOOK = "https://" + CLIQ_WEBHOOK.lstrip("/")
    else:   # a bare token was given: build the channel post URL around it
        CLIQ_WEBHOOK = (f"https://cliq.zoho.com/company/{CLIQ_COMPANY}/api/v2/channelsbyname/"
                        f"{CLIQ_CHANNEL}/message?zapikey={CLIQ_WEBHOOK}")
RESEND_KEY = os.environ.get("RESEND_API_KEY", "").strip()          # email sender; blank = no email
SEND_EMAIL = os.environ.get("DN_SEND_EMAIL", "").strip().lower() in ("1", "true", "yes")   # only the 08:10 run emails
SCAN_BY = os.environ.get("SCAN_BY", "").strip()
REPORT_TO = [x.strip() for x in os.environ.get("DN_REPORT_TO", "sumon.a@lapizblue.com").split(",") if x.strip()]
REPORT_CC = [x.strip() for x in os.environ.get("DN_REPORT_CC", "").split(",") if x.strip()]
REPORT_FROM = os.environ.get("DN_REPORT_FROM", "Delivery Notes <delivery-notes@lapizblue.com>")
SHORT_TO = [x.strip() for x in os.environ.get("DN_SHORT_TO", "sumon.a@lapizblue.com").split(",") if x.strip()]
REMIND_EVERY = int(os.environ.get("REMIND_EVERY_DAYS", "2"))     # pending deliveries are re-sent every N days
LEDGER = os.environ.get("DN_LEDGER", "dn-ledger.json")           # per invoice: invoiced vs delivered, kept in the repo
BOOKS_UI = os.environ.get("ZOHO_BOOKS_UI", "https://books.zoho.com/app/" + ORG + "#/invoices/")
RUN_URL = (f"{os.environ.get('GITHUB_SERVER_URL', '')}/{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/"
           f"{os.environ.get('GITHUB_RUN_ID', '')}") if os.environ.get("GITHUB_RUN_ID") else ""

NUM_RE = re.compile(r"#\s*(LB[A-Z]*\s?\d{2}\s?-\s?\d{4,6})")
LOOSE_RE = re.compile(r"(LB[A-Z]*\s?\d{2}\s?-\s?\d{4,6})")
DATE_RE = re.compile(r"\b(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(20\d{2})\b", re.IGNORECASE)
TRN_RE = re.compile(r"\b(1\d{14})\b")
BILL_RE = re.compile(r"Bill\s*To.*?\n(.+)", re.IGNORECASE)

for d in ("split", "unmatched", "needs-check", "pages"):
    os.makedirs(f"{OUT}/{d}", exist_ok=True)


def log(msg):
    print(f"[{datetime.now(DUBAI).strftime('%H:%M:%S')}] {msg}", flush=True)


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


def read_quantities(png):
    """Quantities from the Qty column, paired to SKU rows by vertical position.
    Returns {sku: qty}. Full page OCR often drops the right hand column, so the column is read on its own."""
    im = Image.open(png)
    w, h = im.size
    top = int(h * 0.30)
    strip = im.crop((int(w * 0.84), top, w, int(h * 0.95)))
    strip = strip.resize((strip.width * 2, strip.height * 2))
    spath = png.replace(".png", "-qty.png")
    strip.save(spath)
    nums = []
    tsv = subprocess.run(["tesseract", spath, "-", "--psm", "6", "tsv"], capture_output=True, text=True).stdout
    for line in tsv.split("\n")[1:]:
        c = line.split("\t")
        if len(c) < 12:
            continue
        t = c[11].strip().replace(",", ".")
        if re.fullmatch(r"\d{1,5}\.\d{1,2}", t):
            nums.append([int(c[7]) // 2 + top, float(t), False])
    skus = []
    tsv = subprocess.run(["tesseract", png, "-", "--psm", "3", "tsv"], capture_output=True, text=True).stdout
    by_line = {}
    for line in tsv.split("\n")[1:]:
        c = line.split("\t")
        if len(c) < 12 or not c[11].strip():
            continue
        by_line.setdefault((c[2], c[3], c[4]), []).append((int(c[7]), c[11].strip()))
    item_rows = []   # (y, name) for lines that look like "<n> <item name> ..."
    for words in by_line.values():
        joined = " ".join(t for _, t in words)
        m = re.search(r"(?:SKU|SkU|5KU|SKV)\s*[:;]?\s*(\S+)", joined)
        if m:
            skus.append((words[0][0], squash(m.group(1))))
        elif re.match(r"^\d{1,2}[.,]?\s+[A-Za-z]", joined) and words[0][0] > top:
            name = re.sub(r"^\d{1,2}[.,]?\s+", "", joined)
            name = re.sub(r"\s+\d{1,5}[.,]\d{1,2}\s*$", "", name)       # strip the qty if it was read on the same line
            item_rows.append((words[0][0], name))
    out, by_row = {}, []
    # first by item row (same height as the quantity), then by SKU line (quantity sits ~30px above it)
    for y, name in item_rows:
        best = None
        for n in nums:
            if n[2] or not (y - 40 <= n[0] <= y + 40):
                continue
            if best is None or abs(n[0] - y) < abs(best[0] - y):
                best = n
        if best:
            best[2] = True
            by_row.append((name, best[1]))
            # the SKU line directly under this row inherits the quantity
            under = [sk for sk in skus if y < sk[0] <= y + 110]
            if under:
                out[under[0][1]] = best[1]
    for y, sku in skus:
        if sku in out:
            continue
        best = None
        for n in nums:
            if n[2] or not (y - 110 <= n[0] <= y + 15):
                continue
            if best is None or abs(n[0] - (y - 30)) < abs(best[0] - (y - 30)):
                best = n
        if best:
            best[2] = True
            out[sku] = best[1]
    out["__rows__"] = by_row
    return out


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
    try:
        qty = read_quantities(png)
    except Exception as e:
        log(f"  qty column not read: {e}")
        qty = {}
    return {"text": text, "number": num, "customer": customer, "date": date, "trns": trns, "qty": qty}


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

    # items. Rules:
    #   a. every item row printed on the page must be a line of this invoice (SKU and name both agree);
    #      a row that belongs to no invoice line means the page is NOT this invoice -> FAIL
    #   b. at least one invoice line must be on the page
    #   c. invoice lines that are NOT on the page are allowed: a partial delivery. They and any
    #      quantity shortfall are recorded for the pending-delivery reminder, not treated as a mismatch.
    lines = [li for li in inv.get("line_items", []) if li.get("name") or li.get("sku")]
    rows = page_items(page["text"])
    qmap = {k: v for k, v in page.get("qty", {}).items() if k != "__rows__"}
    qrows = page.get("qty", {}).get("__rows__", [])
    for r in rows:
        for k, v in qmap.items():
            if r["sku"] and edit_distance(r["sku"], k) <= 1:
                r["qty"] = v
                break
    # item rows with no SKU line under them (blank SKU in Zoho)
    for name, q in qrows:
        if not any(squash(r["name"])[:14] == squash(name)[:14] for r in rows):
            rows.append({"sku": "", "name": name, "qty": q})
    text_sq_tokens = set(re.findall(r"[A-Z0-9]{4,}", text_up.replace("-", "")))
    page_lines = [l.strip() for l in page["text"].split("\n") if l.strip()]

    row_skus = [r["sku"] for r in rows if r["sku"]]

    def sku_on_page(sku):
        # exact anywhere; else within one OCR slip (O/0, I/1, S/5, B/8, Z/2 treated as equal) of a parsed
        # "SKU : xxx" row or of any token of similar length; a SKU with spaces is matched on its first word
        if sku in text_sq:
            return True
        if any(sku_same(sku, k) for k in row_skus):
            return True
        return any(edit_distance(canon(sku), canon(t)) <= 1 for t in text_sq_tokens if abs(len(t) - len(sku)) <= 1)

    def name_on_page(name):
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

    # one-to-one assignment of printed rows to invoice lines: exact SKU first, then one-slip SKU,
    # then (lines with no SKU in Zoho) by item name. A row can satisfy only one line.
    problems, present, absent = [], [], []
    sku_lines = [li for li in lines if len(squash(li.get("sku", ""))) >= 4]
    free_rows = [r for r in rows if r["sku"]]
    assigned = {}
    for exact in (True, False):
        for li in sku_lines:
            if id(li) in assigned:
                continue
            k = squash(li["sku"])
            for r in free_rows:
                hit = (canon(r["sku"]) == canon(k)) if exact else sku_same(r["sku"], k)
                if hit:
                    assigned[id(li)] = r
                    free_rows.remove(r)
                    break
    for li in lines:
        sku = squash(li.get("sku", ""))
        name = li.get("name", "")
        n_ok = name_on_page(name)
        if len(sku) >= 4:
            r = assigned.get(id(li))
            if r is None and sku_on_page(sku) and free_rows == [] and n_ok:
                # SKU seen in the text but its row was not parsed (OCR broke the SKU line); accept on name
                r = {"sku": sku, "name": name, "qty": None}
                for q, v in qmap.items():
                    if sku_same(q, sku):
                        r["qty"] = v
            found = r is not None and n_ok
            if r is not None and not n_ok:
                problems.append(f"SKU {li.get('sku')} is on the page but its item name '{name}' is not")
            elif r is None and n_ok and not free_rows:
                pass   # name present, SKU row missing: treated as not on this note (pending), not a mismatch
        else:
            # no SKU in Zoho, so the note prints no "SKU :" line and no row is parsed: the item name decides
            r = None
            for cand in rows:
                if cand["sku"] == "" and edit_distance(squash(cand["name"])[:14], squash(name)[:14]) <= 2:
                    r = cand
                    break
            found = n_ok
        (present if found else absent).append(li)
        if found:
            li["_delivered"] = r.get("qty") if r else None
    # printed rows that match no invoice line at all: the page is not this invoice
    # (a repeated row of an item that IS on the invoice is not foreign, it is a duplicate print)
    foreign = [r for r in free_rows if not any(sku_same(r["sku"], squash(li["sku"])) for li in sku_lines)]
    if foreign:
        problems.append("page has item rows that are not on this invoice: " +
                        ", ".join(f"SKU {r['sku']}" for r in foreign[:5]))
    if lines and not present:
        problems.append("none of the invoice lines are on the page")
    checks["items"] = bool(lines) and not problems
    short = [li for li in present if li.get("_delivered") is not None and li["_delivered"] < float(li.get("quantity") or 0)]
    unread = [li for li in present if li.get("_delivered") is None]
    items_note = f"{len(present)}/{len(lines)} lines on page"
    if absent:
        items_note += f"; {len(absent)} line(s) not on this note (pending)"
    if short:
        items_note += f"; {len(short)} line(s) delivered less than invoiced"
    if unread:
        items_note += f"; qty not readable on {len(unread)} line(s)"
    if problems:
        items_note += "; ITEM MISMATCH: " + " | ".join(problems)
    inv["_present"], inv["_absent"] = present, absent

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


QTY_RE = re.compile(r"(\d{1,5})[.,](\d{2})\s*$")


def page_items(text):
    """Item rows printed on the note: [{'sku','name','qty'}]. qty is None when the number was not readable.
    Layout (Zoho delivery note): '<n> <item name> <origin> <qty>' then 'SKU : <sku> <unit>'."""
    lines = text.split("\n")
    rows = []
    for i, l in enumerate(lines):
        m = re.search(r"SKU\s*[:;]\s*(\S+)", l, re.IGNORECASE)
        if not m:
            continue
        sku = squash(m.group(1))
        # the item row is the nearest non-empty line above the SKU line
        j = i - 1
        while j >= 0 and not lines[j].strip():
            j -= 1
        row = lines[j].strip() if j >= 0 else ""
        qm = QTY_RE.search(row)
        qty = None
        if qm:
            qty = float(f"{qm.group(1)}.{qm.group(2)}")
            row = row[:qm.start()].strip()
        else:
            # quantity sometimes lands on the SKU line or the line after
            for cand in (l, lines[i + 1] if i + 1 < len(lines) else ""):
                qm = QTY_RE.search(cand)
                if qm:
                    qty = float(f"{qm.group(1)}.{qm.group(2)}")
                    break
        name = re.sub(r"^\d{1,2}\s+", "", row)
        rows.append({"sku": sku, "name": name, "qty": qty})
    return rows


CONFUSABLE = str.maketrans("OIL" + "SBZG", "011" + "5826")


def canon(s):
    """SKU in a form where the usual OCR look-alikes compare equal."""
    return squash(s).translate(CONFUSABLE)


def sku_same(a, b):
    """Two SKUs are the same if, after canonicalising, they are within one edit, or one is a
    prefix (at least 4 chars) of the other (a SKU with a space is printed cut at the space)."""
    a, b = canon(a), canon(b)
    if not a or not b:
        return False
    if a == b or edit_distance(a, b) <= 1:
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= 4 and long_.startswith(short)


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

    def invoices_between(self, start, end):
        """Every invoice dated start..end inclusive (all statuses)."""
        found, page = [], 1
        while True:
            code, j = self.call("GET", "/invoices", params={"date_start": start, "date_end": end,
                                                           "per_page": 200, "page": page})
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


def images_to_pdf(folder):
    """Any photo (jpg/png/heic) in the inbox becomes a one page PDF so the engine only sees PDFs."""
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except ImportError:
        pass
    for img in sorted(glob.glob(os.path.join(folder, "*"))):
        if not re.search(r"\.(jpe?g|png|heic|webp)$", img, re.IGNORECASE):
            continue
        try:
            im = Image.open(img)
            im = ImageOps.exif_transpose(im).convert("RGB")
            out = os.path.splitext(img)[0] + ".pdf"
            im.save(out, "PDF", resolution=200)
            os.remove(img)
            log(f"photo {os.path.basename(img)} converted to {os.path.basename(out)}")
        except Exception as e:
            log(f"could not convert {img}: {e}")


def esc(s):
    return str(s if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_daily_email(day_runs, held, due, overdue, overdue_void, cutoff, today):
    """The one daily email, house style (dark card, lavender band, stat tiles, one card per document).
    day_runs: scans processed since the previous email. held: pages still waiting for a person."""
    BG, CARD, BAND, TEXT, MUTED, LINE = "#1c1c1e", "#242426", "#b7bdf5", "#f2f2f4", "#9a9aa3", "#3a3a3e"
    RED, GREEN, PURPLE_BG, PURPLE = "#ff8a6b", "#7bd88f", "#2e2a4a", "#c9c3ff"
    day = today.strftime("%A, %d %b %Y")
    pending_lines = sum(len(sf) for _, _, sf in due)
    attached = sum(r["attached"] for r in day_runs)
    needs = len(held)

    def tile(num, label, hot=False):
        return (f"<td width='33%' style='padding:6px'><div style='background:{'#3a2a28' if hot else '#2a2a2d'};border-radius:12px;"
                f"padding:22px 10px;text-align:center'><div style='font-size:34px;font-weight:600;color:{RED if hot else TEXT}'>{num}</div>"
                f"<div style='font-size:13px;color:{RED if hot else MUTED};margin-top:4px'>{label}</div></div></td>")

    def th(t, align="left"):
        return f"<th align='{align}' style='padding:10px 12px;font-size:11px;letter-spacing:.08em;color:{MUTED};font-weight:500;text-transform:uppercase'>{t}</th>"

    def pill(t):
        return f"<span style='background:{PURPLE_BG};color:{PURPLE};border-radius:999px;padding:2px 9px;font-size:12px'>{esc(t)}</span>"

    h = [f"<div style='background:{BG};padding:24px 0;font-family:Roboto,Helvetica,Arial,sans-serif;color:{TEXT}'>"
         f"<div style='max-width:960px;margin:0 auto;background:{CARD};border-radius:18px;overflow:hidden'>",
         f"<div style='background:{BAND};padding:26px 32px;color:#1f1f2e'>"
         f"<div style='font-size:12px;letter-spacing:.12em;text-transform:uppercase'>Lapiz Blue · Delivery Notes · Daily</div>"
         f"<div style='font-size:30px;font-weight:600;margin-top:6px'>Delivery notes to chase</div>"
         f"<div style='font-size:15px;margin-top:4px'>{day}</div></div>",
         "<div style='padding:22px 26px'>",
         "<table width='100%' cellspacing='0' cellpadding='0' style='color:{TEXT}'><tr>"
         + tile(len(overdue), f"invoices with no delivery note for {OVERDUE_DAYS}+ days", hot=bool(overdue))
         + tile(len(due), "invoices delivered less than invoiced", hot=bool(due))
         + tile(attached, f"notes attached since the last report ({len(day_runs)} scan{'s' if len(day_runs) != 1 else ''})")
         + "</tr></table>"]

    # ---- section 0: what was scanned since the last email, and what still needs a person
    h.append(f"<div style='margin:26px 6px 10px;font-size:20px;font-weight:600'><span style='color:{PURPLE};font-size:14px'>&#9679;</span> "
             f"Scans since the last report <span style='font-size:14px;color:{MUTED};font-weight:400'>{len(day_runs)}</span></div>")
    if not day_runs:
        h.append(f"<div style='margin:0 6px 10px;color:{MUTED};font-size:14px'>No scan was sent since the last report.</div>")
    else:
        h.append(f"<div style='border:1px solid {LINE};border-radius:14px;margin:0 6px 10px;overflow:hidden'><table width='100%' cellspacing='0' cellpadding='0' style='color:{TEXT}'><tr>"
                 + th("When") + th("File") + th("Sent by") + th("Pages", "right") + th("Attached", "right") + th("Already on invoice", "right") + th("Need a person", "right") + "</tr>")
        for r in day_runs:
            h.append(f"<tr style='font-size:14px;border-top:1px solid {LINE}'><td style='padding:10px 12px;color:{MUTED}'>{esc(r['at'][5:])}</td>"
                     f"<td style='padding:10px 12px'>{esc(', '.join(r['files']))[:60]}</td><td style='padding:10px 12px'>{esc(r['by'])}</td>"
                     f"<td style='padding:10px 12px;text-align:right'>{r['pages']}</td><td style='padding:10px 12px;text-align:right'>{r['attached']}</td>"
                     f"<td style='padding:10px 12px;text-align:right;color:{MUTED}'>{r['already']}</td>"
                     f"<td style='padding:10px 12px;text-align:right;color:{RED if r['held'] else TEXT}'>{r['held']}</td></tr>")
        h.append("</table></div>")
    h.append(f"<div style='margin:22px 6px 10px;font-size:20px;font-weight:600'><span style='color:{RED if held else PURPLE};font-size:14px'>&#9679;</span> "
             f"Pages that need a person <span style='font-size:14px;color:{MUTED};font-weight:400'>{len(held)}</span></div>")
    if not held:
        h.append(f"<div style='margin:0 6px 10px;color:{MUTED};font-size:14px'>None. Every scanned page was matched and attached.</div>")
    else:
        h.append(f"<div style='margin:0 6px 8px;color:{MUTED};font-size:13px'>These pages were NOT attached. Open the scan, find the page, check the invoice and attach it in Zoho by hand. "
                 f"A page drops off this list once its invoice has a note.</div>"
                 f"<div style='border:1px solid {LINE};border-radius:14px;margin:0 6px 10px;overflow:hidden'><table width='100%' cellspacing='0' cellpadding='0' style='color:{TEXT}'><tr>"
                 + th("Scan") + th("Page") + th("Number on page") + th("Customer on page") + th("Date") + th("Why") + "</tr>")
        for x in held:
            h.append(f"<tr style='font-size:14px;border-top:1px solid {LINE}'><td style='padding:10px 12px;color:{MUTED}'>"
                     + (f"<a href='{x['url']}' style='color:{PURPLE}'>{esc(x['file'][:34])}</a>" if x.get('url') else esc(x['file'][:34])) +
                     f"</td><td style='padding:10px 12px'>{esc(x['page'])}</td><td style='padding:10px 12px;font-weight:600'>{esc(x['number'] or '(not readable)')}</td>"
                     f"<td style='padding:10px 12px'>{esc(x['customer'][:40])}</td><td style='padding:10px 12px;color:{MUTED}'>{esc(x.get('date', ''))}</td>"
                     f"<td style='padding:10px 12px;color:{RED}'>{esc(x['why'])}</td></tr>")
        h.append("</table></div>")

    # ---- section 1: delivered less than invoiced
    h.append(f"<div style='margin:26px 6px 10px;font-size:20px;font-weight:600'><span style='color:{PURPLE};font-size:14px'>&#9679;</span> "
             f"Delivered less than invoiced <span style='font-size:14px;color:{MUTED};font-weight:400'>{len(due)} invoices, {pending_lines} lines</span></div>")
    if not due:
        h.append(f"<div style='margin:0 6px 10px;color:{MUTED};font-size:14px'>Nothing pending. Every attached note matches its invoice.</div>")
    for key, e, shortfall in due:
        url = BOOKS_UI + e["invoice_id"]
        first = e["first_attached"][:10]
        age = (today - datetime.strptime(first, "%Y-%m-%d").date()).days
        h.append(f"<div style='border:1px solid {LINE};border-radius:14px;margin:0 6px 14px;overflow:hidden'>"
                 "<table width='100%' cellspacing='0' cellpadding='0' style='color:{TEXT}'><tr>"
                 + th("Number") + th("Customer") + th("Invoice date") + th("Note attached") + th("Status") + th("Notes", "right") +
                 "</tr><tr style='font-size:15px'>"
                 f"<td style='padding:12px;font-weight:600'><a href='{url}' style='color:{TEXT};text-decoration:none'>{esc(key)}</a></td>"
                 f"<td style='padding:12px'>{esc(e['customer'])}</td>"
                 f"<td style='padding:12px;color:{MUTED}'>{esc(e['date'])}</td>"
                 f"<td style='padding:12px;color:{MUTED}'>{first} ({age} days ago)</td>"
                 f"<td style='padding:12px'>{pill(e.get('status', ''))}</td>"
                 f"<td style='padding:12px;text-align:right'>{len(e['notes'])}</td></tr></table>"
                 f"<table width='100%' cellspacing='0' cellpadding='0' style='background:#2a2a2d;border-top:1px solid {LINE};color:{TEXT}'><tr>"
                 + th("Short item") + th("SKU") + th("Invoiced", "right") + th("On note", "right") + th("Balance", "right") + "</tr>")
        for r in shortfall:
            bal = r["invoiced"] - r["delivered"]
            h.append(f"<tr style='font-size:15px'><td style='padding:10px 12px'>{esc(r['name'])}</td>"
                     f"<td style='padding:10px 12px;color:{MUTED}'>{esc(r['sku'])}</td>"
                     f"<td style='padding:10px 12px;text-align:right'>{r['invoiced']:g}</td>"
                     f"<td style='padding:10px 12px;text-align:right'>{r['delivered']:g}</td>"
                     f"<td style='padding:10px 12px;text-align:right'><span style='background:#3a2a28;color:{RED};border-radius:999px;padding:2px 9px'>{bal:g}</span></td></tr>")
        h.append(f"</table><div style='padding:8px 12px;font-size:12px;color:{MUTED}'>Open the invoice to see the attached note. "
                 f"Reminder repeats every {REMIND_EVERY} days until the balance is delivered and its note attached.</div></div>")

    # ---- section 2: no delivery note at all
    h.append(f"<div style='margin:30px 6px 10px;font-size:20px;font-weight:600'><span style='color:{PURPLE};font-size:14px'>&#9679;</span> "
             f"No delivery note for {OVERDUE_DAYS}+ days <span style='font-size:14px;color:{MUTED};font-weight:400'>{len(overdue)} invoices, dated {OVERDUE_FROM} to {cutoff}</span></div>")
    groups = {}
    for inv in overdue:
        groups.setdefault(inv.get("cf_fulfillment_method", "") or "No fulfillment method", []).append(inv)
    for method, invs in groups.items():
        h.append(f"<div style='margin:14px 6px 6px;font-size:12px;letter-spacing:.08em;color:{MUTED};text-transform:uppercase'>{esc(method)} · {len(invs)}</div>"
                 f"<div style='border:1px solid {LINE};border-radius:14px;margin:0 6px 10px;overflow:hidden'>"
                 "<table width='100%' cellspacing='0' cellpadding='0' style='color:{TEXT}'><tr>"
                 + th("Number") + th("Customer") + th("Invoice date") + th("Days") + th("Salesperson") + th("Status", "right") + "</tr>")
        for inv in invs:
            age = (today - datetime.strptime(inv.get("date"), "%Y-%m-%d").date()).days
            url = BOOKS_UI + str(inv.get("invoice_id", ""))
            h.append(f"<tr style='font-size:15px;border-top:1px solid {LINE}'>"
                     f"<td style='padding:10px 12px;font-weight:600'><a href='{url}' style='color:{TEXT};text-decoration:none'>{esc(inv.get('invoice_number'))}</a></td>"
                     f"<td style='padding:10px 12px'>{esc(inv.get('customer_name'))}</td>"
                     f"<td style='padding:10px 12px;color:{MUTED}'>{esc(inv.get('date'))}</td>"
                     f"<td style='padding:10px 12px;color:{RED if age >= OVERDUE_DAYS * 2 else TEXT}'>{age}</td>"
                     f"<td style='padding:10px 12px;color:{MUTED}'>{esc(inv.get('salesperson_name', ''))}</td>"
                     f"<td style='padding:10px 12px;text-align:right'>{pill(inv.get('status', ''))}</td></tr>")
        h.append("</table></div>")
    if overdue_void:
        h.append(f"<div style='margin:10px 6px;font-size:13px;color:{MUTED}'>Void invoices with no note, not chased: {len(overdue_void)} "
                 f"({esc(', '.join(i.get('invoice_number', '') for i in overdue_void[:12]))}{', ...' if len(overdue_void) > 12 else ''})</div>")

    h.append(f"<div style='margin:28px 6px 4px;font-size:12px;color:{MUTED}'>Sent automatically by the Lapiz Blue delivery note system. "
             f"One email a day at 08:10. Scan signed notes through the Delivery Notes app; an invoice drops off these lists the day its note is attached."
             + (f" <a href='{RUN_URL}' style='color:{PURPLE}'>Full report</a>" if RUN_URL else "") + "</div>")
    h.append("</div></div></div>")
    return "".join(h)


def send_daily_email(html, subject, attachments):
    """Send through Resend. attachments = list of file paths (csv). Returns True on success."""
    if not RESEND_KEY:
        log("RESEND_API_KEY not set, skipping email")
        return False
    import base64
    payload = {"from": REPORT_FROM, "to": REPORT_TO, "subject": subject, "html": html}
    if REPORT_CC:
        payload["cc"] = REPORT_CC
    atts = []
    for p in attachments:
        if os.path.exists(p) and os.path.getsize(p) > 0:
            atts.append({"filename": os.path.basename(p), "content": base64.b64encode(open(p, "rb").read()).decode()})
    if atts:
        payload["attachments"] = atts
    try:
        r = requests.post("https://api.resend.com/emails", json=payload,
                          headers={"Authorization": f"Bearer {RESEND_KEY}"}, timeout=30)
        log(f"email to {', '.join(REPORT_TO)}: HTTP {r.status_code} {r.text[:120]}")
        return r.status_code < 300
    except Exception as e:
        log(f"email failed: {e}")
        return False


def load_ledger():
    if os.path.exists(LEDGER):
        try:
            return json.load(open(LEDGER))
        except Exception:
            pass
    return {}


def save_ledger(led):
    with open(LEDGER, "w") as fh:
        json.dump(led, fh, indent=1, sort_keys=True)


def record_delivery(led, inv, source, pages):
    """Store what this note says was delivered, per invoice line, summed over all notes for the invoice."""
    key = inv.get("invoice_number", "").upper()
    now = datetime.now(DUBAI).strftime("%Y-%m-%d %H:%M")
    e = led.setdefault(key, {"invoice_id": inv["invoice_id"], "customer": inv.get("customer_name", ""),
                             "date": inv.get("date", ""), "status": inv.get("status", ""),
                             "notes": [], "lines": {}, "first_attached": now, "last_reminded": "", "resolved": ""})
    e["status"] = inv.get("status", "")
    note_id = f"{source}|{pages}"
    if any(n["id"] == note_id for n in e["notes"]):
        return e
    e["notes"].append({"id": note_id, "at": now})
    for li in inv.get("line_items", []):
        k = str(li.get("line_item_id") or li.get("sku") or li.get("name"))
        row = e["lines"].setdefault(k, {"sku": li.get("sku", ""), "name": li.get("name", ""),
                                        "invoiced": float(li.get("quantity") or 0), "delivered": 0.0, "unreadable": 0})
        row["invoiced"] = float(li.get("quantity") or 0)
        if li in inv.get("_present", []):
            d = li.get("_delivered")
            if d is None:
                row["unreadable"] += 1
            else:
                row["delivered"] = round(row["delivered"] + d, 2)
    pending = [r for r in e["lines"].values() if r["delivered"] < r["invoiced"] and not r["unreadable"]]
    e["resolved"] = "" if pending else now
    return e


def overrides():
    """Invoice numbers listed in dn-overrides.txt (one per line) are treated as fully delivered.
    Used when the note was misread, or the balance was settled another way."""
    path = os.environ.get("DN_OVERRIDES", "dn-overrides.txt")
    if not os.path.exists(path):
        return set()
    return {l.split("#")[0].strip().upper() for l in open(path) if l.split("#")[0].strip()}


def pending_reminders(led, today):
    """Invoices with a shortfall, first note attached REMIND_EVERY+ days ago, not reminded in the last REMIND_EVERY days."""
    due = []
    for key, e in sorted(led.items()):
        if key.startswith("_") or not isinstance(e, dict) or "lines" not in e:
            continue
        if e.get("resolved") or e.get("status") == "void":
            continue
        if key in overrides():
            continue
        # lines whose quantity could not be read are not chased (they were flagged at attach time)
        shortfall = [r for r in e["lines"].values() if r["delivered"] < r["invoiced"] and not r["unreadable"]]
        if not shortfall:
            continue
        first = datetime.strptime(e["first_attached"][:10], "%Y-%m-%d").date()
        if (today - first).days < REMIND_EVERY:
            continue
        if e.get("last_reminded"):
            last = datetime.strptime(e["last_reminded"][:10], "%Y-%m-%d").date()
            if (today - last).days < REMIND_EVERY:
                continue
        due.append((key, e, shortfall))
    return due


# ---------------------------------------------------------------- main

def main():
    if MODE not in ("dry", "live"):
        raise SystemExit("MODE must be dry or live")
    if not ORG:
        raise SystemExit("ZOHO_ORG_ID_DXB is not set")

    images_to_pdf(INBOX)
    pdfs = sorted(glob.glob(os.path.join(INBOX, "*.pdf")) + glob.glob(os.path.join(INBOX, "*.PDF")))
    if ONLY_FILE:
        pdfs = [p for p in pdfs if os.path.basename(p) == ONLY_FILE]
    if not pdfs:
        log(f"No PDFs found in {INBOX}" + (f" named {ONLY_FILE}" if ONLY_FILE else "") + "; overdue check only")

    z = Zoho()
    led = load_ledger()
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

        # 1. read every page (OCR runs OCR_WORKERS pages at a time; a 500 page scan takes minutes, not an hour)
        log(f"  {n} pages, OCR with {OCR_WORKERS} workers")
        with ThreadPoolExecutor(max_workers=OCR_WORKERS) as ex:
            pages = list(ex.map(read_page, pngs))
        for idx, p in enumerate(pages):
            p["page"] = idx + 1
            p["is_first"] = "DELIVERY NOTE" in p["text"].upper().replace("  ", " ")
            log(f"  page {idx + 1:>3}: {p['number'] or 'NUMBER NOT READ'} | {p['customer'][:40]} | {p['date']}")

        # 2. group pages into notes. A note can run over 2, 3 or 4 pages:
        #    - a page with the same number as the previous page continues that note
        #    - a page with NO number, and no "DELIVERY NOTE" heading, directly after a numbered page
        #      is a continuation sheet of that note (items spilled over), so it travels with it
        groups = []
        for p in pages:
            prev = groups[-1] if groups else None
            same_number = prev and p["number"] and prev["number"] == p["number"]
            continuation = prev and not p["number"] and prev["number"] and not p["is_first"]
            if (same_number or continuation) and prev["pages"][-1]["page"] == p["page"] - 1:
                prev["pages"].append(p)
            else:
                groups.append({"number": p["number"], "pages": [p]})

        # 3. cut, look up, verify, attach
        seen_in_this_file = {}
        for g in groups:
            first = g["pages"][0]
            # merge OCR of all pages in the group for verification
            page = {"text": "\n".join(p["text"] for p in g["pages"]), "number": g["number"],
                    "customer": first["customer"], "date": first["date"],
                    "trns": sorted({t for p in g["pages"] for t in p["trns"]}),
                    "qty": {k: v for p in g["pages"] for k, v in p.get("qty", {}).items()}}
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
                passing, failed = [], []
                for inv in cands[:8]:
                    cnum = inv.get("invoice_number", "").upper()
                    if num and edit_distance(num, cnum) > 2:
                        continue
                    full = z.full_invoice(inv["invoice_id"])
                    ok, checks, n2 = verify(page, full, strict=True)
                    if ok:
                        passing.append((full, n2 + " (strict)"))
                    else:
                        failed.append(f"{cnum}: {n2}")
                if len(passing) == 1:
                    target, note = passing[0]
                    corrected = (num != target.get("invoice_number", "").upper())
                elif len(passing) > 1:
                    note += f"; {len(passing)} same-day invoices for this customer pass all checks: " + \
                            ", ".join(p[0].get("invoice_number", "") for p in passing)
                else:
                    note += f"; searched {len(cands)} same-day invoice(s) for this customer, none pass all checks"
                    if failed:
                        note += " [" + " | ".join(failed) + "]"

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
            if (fname in existing and inv_num not in led) or (fname not in existing and MODE == "live"):
                record_delivery(led, target, os.path.basename(pdf), pg_label)
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
    fields = ["source_pdf", "pages", "number_on_page", "customer_on_page", "date_on_page", "invoice_number",
              "customer", "invoice_id", "invoice_status", "result", "checks", "detail"]
    with open(f"{OUT}/report.csv", "w", newline="") as fh:
        wtr = csv.DictWriter(fh, fieldnames=fields)
        wtr.writeheader()
        wtr.writerows(rows)

    counts = {}
    for r in rows:
        counts[r["result"]] = counts.get(r["result"], 0) + 1
    lines = [f"# Delivery note attach report ({MODE} mode)", "",
             f"Run: {datetime.now(DUBAI).strftime('%Y-%m-%d %H:%M')}   API calls: {z.calls}", ""]
    if os.environ.get("SCAN_URL"):
        lines += [f"Scan sent from the app by {os.environ.get('SCAN_BY') or 'unknown'}: {os.environ['SCAN_URL']}", ""]
    lines += [
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

    # 6. overdue: invoices older than OVERDUE_DAYS with still no attachment (since go-live)
    today = datetime.now(DUBAI).date()
    cutoff = (today - timedelta(days=OVERDUE_DAYS)).strftime("%Y-%m-%d")
    overdue, overdue_void = [], []
    if OVERDUE_FROM <= cutoff:
        for inv in z.invoices_between(OVERDUE_FROM, cutoff):
            if inv.get("has_attachment") or inv.get("status") == "draft":
                continue
            if inv.get("invoice_number", "").upper() in matched_numbers:
                continue
            (overdue_void if inv.get("status") == "void" else overdue).append(inv)
    overdue.sort(key=lambda i: (i.get("cf_fulfillment_method", ""), i.get("date", ""), i.get("invoice_number", "")))
    lines.append(f"## NO DELIVERY NOTE FOR {OVERDUE_DAYS}+ DAYS ({len(overdue)}) "
                 f"(invoices dated {OVERDUE_FROM} to {cutoff}, no attachment)")
    for inv in overdue:
        age = (today - datetime.strptime(inv.get("date"), "%Y-%m-%d").date()).days
        lines.append(f"- {inv.get('invoice_number')} | {inv.get('date')} ({age} days) | {inv.get('customer_name')} | "
                     f"{inv.get('status')} | {inv.get('cf_fulfillment_method', '')} | {inv.get('salesperson_name', '')}")
    if overdue_void:
        lines.append(f"- plus {len(overdue_void)} void invoice(s) with no note: " +
                     ", ".join(i.get("invoice_number", "") for i in overdue_void))
    lines.append("")
    with open(f"{OUT}/overdue.csv", "w", newline="") as fh:
        wtr = csv.writer(fh)
        wtr.writerow(["invoice_number", "date", "days_old", "customer", "status", "fulfillment_method", "salesperson"])
        for inv in overdue + overdue_void:
            age = (today - datetime.strptime(inv.get("date"), "%Y-%m-%d").date()).days
            wtr.writerow([inv.get("invoice_number"), inv.get("date"), age, inv.get("customer_name"), inv.get("status"),
                          inv.get("cf_fulfillment_method", ""), inv.get("salesperson_name", "")])

    lines.append("## Every note")
    for r in rows:
        lines.append(f"- p{r['pages']} {r['invoice_number'] or r['number_on_page'] or '(no number)'} | "
                     f"{r['customer']} | {r['invoice_status']} | {r['result']} | {r['checks']}")
    with open(f"{OUT}/report.md", "w") as fh:
        fh.write("\n".join(lines) + "\n")
    with open(f"{OUT}/processed.json", "w") as fh:
        json.dump(processed, fh)
    print("\n".join(lines))

    # 7. remember this scan and its held pages, so the one daily email can report every scan of the day
    now_s = datetime.now(DUBAI).strftime("%Y-%m-%d %H:%M")
    if pdfs and MODE == "live":
        runs = led.setdefault("_runs", [])
        runs.append({"at": now_s, "files": [os.path.basename(p) for p in pdfs], "by": SCAN_BY or "manual run",
                     "pages": sum(len(r["pages"].split(",")) for r in rows),
                     "attached": counts.get("ATTACHED", 0), "already": counts.get("ALREADY ATTACHED", 0),
                     "held": counts.get("NEEDS CHECK", 0) + counts.get("NUMBER NOT READ", 0) + counts.get("ATTACH FAILED", 0),
                     "url": RUN_URL})
        held = led.setdefault("_held", [])
        for r in rows:
            if r["result"] in ("NEEDS CHECK", "NUMBER NOT READ", "ATTACH FAILED"):
                why = "number not readable" if r["result"] == "NUMBER NOT READ" else \
                    ", ".join(c for c in r["checks"].split("; ")[0].split(", ") if "FAIL" in c) or r["detail"][:80]
                held.append({"at": now_s, "file": r["source_pdf"], "page": r["pages"], "number": r["number_on_page"],
                             "customer": r["customer_on_page"], "date": r["date_on_page"], "why": why,
                             "detail": r["detail"][:200], "url": RUN_URL})
        # a held page is cleared once its invoice has a note (attached in this run, or by hand before a re-scan)
        done = {r["invoice_number"] for r in rows if r["result"] in ("ATTACHED", "ALREADY ATTACHED")}
        led["_held"] = [h for h in held if not (h["number"] and h["number"] in done)
                        and (today - datetime.strptime(h["at"][:10], "%Y-%m-%d").date()).days <= 14]
    # 8. pending deliveries: delivery note shows less than the invoice, remind every REMIND_EVERY days
    due = pending_reminders(led, today)
    lines_pd = [f"## PENDING DELIVERIES DUE FOR REMINDER ({len(due)})"]
    for key, e, shortfall in due:
        for r in shortfall:
            lines_pd.append(f"- {key} | {e['customer']} | {r['name']} | invoiced {r['invoiced']:g} | on note {r['delivered']:g}")
    print("\n".join(lines_pd))
    with open(f"{OUT}/report.md", "a") as fh:
        fh.write("\n".join(lines_pd) + "\n")
    # 9. ONE email a day, from the 08:10 scheduled run only: every scan since the last email, every page that
    #    still needs a person, invoices delivered short, invoices with no note for 2+ days
    since = led.get("_last_email", "")
    day_runs = [r for r in led.get("_runs", []) if r["at"] > since]
    held_now = led.get("_held", [])
    html = build_daily_email(day_runs, held_now, due, overdue, overdue_void, cutoff, today)
    with open(f"{OUT}/daily_email.html", "w") as fh:
        fh.write(html)
    if MODE == "live" and SEND_EMAIL:
        subject = (f"Delivery notes {today.strftime('%d %b')}: {sum(r['attached'] for r in day_runs)} attached, "
                   f"{len(held_now)} need a person, {len(overdue)} with no note, {len(due)} delivered short")
        if send_daily_email(html, subject, [f"{OUT}/overdue.csv"]):
            for key, e, _ in due:
                e["last_reminded"] = today.strftime("%Y-%m-%d")
            led["_last_email"] = now_s
            led["_runs"] = [r for r in led.get("_runs", []) if r["at"] > since][-200:]
    elif MODE == "live":
        log("not the 08:10 run, email not sent (DN_SEND_EMAIL not set)")
    if MODE == "live":
        save_ledger(led)

    if any(r["result"] == "ATTACH FAILED" for r in rows):
        sys.exit(2)


def post_cliq(rows, counts, missing, overdue, overdue_void, pdfs, cutoff):
    """One brief message for the counter channel. Full detail stays in the run report."""
    if not CLIQ_WEBHOOK:
        log("CLIQ_DN_WEBHOOK not set, skipping Cliq post")
        return
    stamp = datetime.now(DUBAI).strftime("%d %b %Y %H:%M")
    msg = [f"*Delivery notes, {stamp}* ({MODE} run)"]
    if pdfs:
        msg.append("Scans: " + ", ".join(os.path.basename(p) for p in pdfs) +
                   f" ({sum(len(r['pages'].split(',')) for r in rows)} pages)")
        word = "Attached" if MODE == "live" else "Would attach"
        msg.append(f"{word}: {counts.get('ATTACHED', 0) + counts.get('WOULD ATTACH', 0)} | "
                   f"Already on the invoice: {counts.get('ALREADY ATTACHED', 0)} | "
                   f"Need a person: {counts.get('NEEDS CHECK', 0) + counts.get('NUMBER NOT READ', 0)} | "
                   f"Failed: {counts.get('ATTACH FAILED', 0)}")
        dup = [r for r in rows if r["result"] == "ALREADY ATTACHED"]
        if dup:
            # the note was already on the invoice (same scan sent twice, or a page scanned twice): nothing uploaded
            msg.append(f"Same delivery note sent twice ({len(dup)}): the invoice already has this note, "
                       f"so the repeat was not attached, only one copy stays on the invoice: " +
                       ", ".join(f"p{r['pages']} {r['invoice_number']}" for r in dup[:20]) +
                       (f" +{len(dup) - 20} more in the report" if len(dup) > 20 else ""))
        bad = [r for r in rows if r["result"] in ("NEEDS CHECK", "NUMBER NOT READ", "ATTACH FAILED")]
        for r in bad[:15]:
            why = "number not readable" if r["result"] == "NUMBER NOT READ" else \
                  ", ".join(c for c in r["checks"].split("; ")[0].split(", ") if "FAIL" in c) or r["detail"][:80]
            if "ITEM MISMATCH:" in r["checks"]:
                why += " (" + r["checks"].split("ITEM MISMATCH:")[1].split(" | ")[0].split("; TRN")[0].strip()[:70] + ")"
            msg.append(f"  page {r['pages']}: {r['number_on_page'] or '?'} {r['customer_on_page'][:35]} -> {why}")
        if len(bad) > 15:
            msg.append(f"  +{len(bad) - 15} more in the report")
    else:
        msg.append("No new scan today.")
    msg.append(f"*No delivery note for {OVERDUE_DAYS}+ days: {len(overdue)}* (invoices up to {cutoff})")
    by_method = {}
    for inv in overdue:
        by_method.setdefault(inv.get("cf_fulfillment_method", "") or "No method", []).append(inv)
    for method, invs in by_method.items():
        msg.append(f"  {method} ({len(invs)}):")
        for inv in invs[:12]:
            msg.append(f"    {inv.get('invoice_number')} {inv.get('date')} {inv.get('customer_name', '')[:32]} "
                       f"({inv.get('salesperson_name', '')})")
        if len(invs) > 12:
            msg.append(f"    +{len(invs) - 12} more in the report")
    if overdue_void:
        msg.append(f"  Void with no note: {len(overdue_void)} (" +
                   ", ".join(i.get("invoice_number", "") for i in overdue_void[:8]) + ")")
    if RUN_URL:
        msg.append(f"Full report: {RUN_URL}")
    text = "\n".join(msg)
    try:
        r = requests.post(CLIQ_WEBHOOK, json={"text": text}, timeout=30)
        log(f"Cliq post: HTTP {r.status_code} {r.text[:120]}")
    except Exception as e:
        log(f"Cliq post failed: {e}")


if __name__ == "__main__":
    main()
