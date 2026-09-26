#!/usr/bin/env python3
"""
Email today's progress PDF. Skips quietly when SMTP secrets are not set.
Secrets (GitHub → Settings → Secrets and variables → Actions):
  SMTP_USER   mailbox that sends, e.g. automation@lapizblue.com or adil@lapizblue.com
  SMTP_PASS   an app-specific password for that mailbox (Zoho Mail: Account → Security → App Passwords)
  REPORT_TO   comma separated recipients, e.g. tarun.s@lapizblue.com,adil@lapizblue.com
Optional: SMTP_HOST (default smtp.zoho.com), SMTP_PORT (default 465, SSL)
"""
import os, json, smtplib, ssl, datetime as dt
from email.message import EmailMessage

user = os.environ.get("SMTP_USER", "").strip()
pw = os.environ.get("SMTP_PASS", "").strip()
to = [x.strip() for x in os.environ.get("REPORT_TO", "").split(",") if x.strip()]
host = os.environ.get("SMTP_HOST", "smtp.zoho.com").strip() or "smtp.zoho.com"
port = int(os.environ.get("SMTP_PORT", "465") or 465)
if not (user and pw and to):
    print("email skipped: SMTP_USER / SMTP_PASS / REPORT_TO not set"); raise SystemExit(0)

today = dt.datetime.now(dt.timezone(dt.timedelta(hours=4))).strftime("%Y-%m-%d")
pdf = f"reports/Backfill_Progress_{today}.pdf"
try:
    hist = json.load(open("reports/history.json"))
    row = hist[-1] if hist else {}
except Exception:
    row = {}
status = row.get("status", "no status recorded")
subj = f"Stock sync daily report {today}: {status[:60]}"
body = (f"Zoho stock sync history scan, daily report for {today} (Dubai).\n\n"
        f"Status: {status}\n"
        f"Examined so far: {row.get('examined_cumulative', 'n/a')} of {row.get('total_candidates', 'n/a')}\n"
        f"Examined today: {row.get('examined_today', 'n/a')}\n"
        f"Remaining: {row.get('remaining', 'n/a')}\n"
        f"API calls used today: {row.get('calls_used', 'n/a')}\n"
        f"Run: {row.get('run_url', '')}\n\n"
        f"The PDF is attached. This mail is sent automatically by the GitHub workflow.\n")
msg = EmailMessage()
msg["From"] = user; msg["To"] = ", ".join(to); msg["Subject"] = subj
msg.set_content(body)
if os.path.exists(pdf):
    msg.add_attachment(open(pdf, "rb").read(), maintype="application", subtype="pdf", filename=os.path.basename(pdf))
else:
    msg.set_content(body + "\nNOTE: no PDF was produced today; check the GitHub run.")
try:
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=60) as s:
            s.login(user, pw); s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=60) as s:
            s.starttls(context=ssl.create_default_context()); s.login(user, pw); s.send_message(msg)
    print("email sent to", to)
except Exception as e:
    print("email failed:", e)
