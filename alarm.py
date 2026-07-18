"""
alarm.py — Fleet Safety Background Alarm Script
================================================
Run this on a schedule (e.g., every morning via cron / Task Scheduler) to
notify clients whose DOT documents are expiring within 30 days.

  - Client HAS a phone number  -> send an SMS via Twilio
  - Client has NO phone number  -> send an email via SMTP

IMPORTANT SCHEDULING NOTE (compliance):
  US law (TCPA) forbids texting people outside 8 AM - 9 PM in THEIR local
  time. Schedule this to run mid-morning US time. Example (runs 9:00 AM
  US Central = 14:00 UTC):
      0 14 * * *  /path/to/venv/bin/python /path/to/alarm.py >> alarm.log 2>&1
"""

import os
import sqlite3
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import date, timedelta, datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

# ==============================================================
# CONFIGURATION
# ==============================================================
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
TWILIO_FROM_NUMBER = os.getenv('TWILIO_FROM_NUMBER')

SMTP_HOST = os.getenv('SMTP_HOST', 'smtp.gmail.com')
SMTP_PORT = int(os.getenv('SMTP_PORT', 587))
SMTP_USERNAME = os.getenv('SMTP_USERNAME')
SMTP_PASSWORD = os.getenv('SMTP_PASSWORD')
SMTP_FROM_EMAIL = os.getenv('SMTP_FROM_EMAIL', SMTP_USERNAME)
SMTP_FROM_NAME = os.getenv('SMTP_FROM_NAME', 'Fleet Safety Alerts')

DATABASE = os.path.join(os.path.dirname(__file__), 'database.db')
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'uploads')

ALERT_DAYS = 30
# FIX H-4: use the same business timezone as the web app so "today" matches.
BUSINESS_TZ = ZoneInfo('America/Chicago')
# FIX C-5: don't re-alert about the same document more often than this.
REALERT_AFTER_DAYS = 7

# Network timeouts so a hung server can't freeze the whole cron job. (FIX H-8)
SMTP_TIMEOUT = 30
TWILIO_TIMEOUT = 30


def business_today():
    return datetime.now(BUSINESS_TZ).date()


# ==============================================================
# SMS NOTIFICATION — Twilio
# ==============================================================

def send_sms_notification(to_phone, driver_name, doc_type, expiration_date, company_name):
    try:
        from twilio.rest import Client
        from twilio.http.http_client import TwilioHttpClient
        http_client = TwilioHttpClient(timeout=TWILIO_TIMEOUT)   # FIX H-8
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, http_client=http_client)

        body = (
            f"[Fleet Safety Alert] Hi {company_name},\n"
            f"Driver: {driver_name}\n"
            f"Document: {doc_type}\n"
            f"Expires: {expiration_date}\n"
            f"Please renew before the expiration date to stay DOT compliant. "
            f"Reply STOP to cancel."
        )
        message = client.messages.create(body=body, from_=TWILIO_FROM_NUMBER, to=to_phone)
        print(f"  SMS sent to {to_phone} — SID: {message.sid}")
        return True
    except ImportError:
        print("  Twilio library not installed. Run: pip install twilio")
        return False
    except Exception as e:
        print(f"  Twilio SMS failed for {to_phone}: {e}")
        return False


# ==============================================================
# EMAIL NOTIFICATION — smtplib
# ==============================================================

def send_email_notification(to_email, driver_name, doc_type, expiration_date, company_name):
    if not SMTP_USERNAME or not SMTP_PASSWORD:
        print("  SMTP credentials not set in .env — skipping email.")
        return False

    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = f"[Action Required] DOT Document Expiring — {company_name}"
        msg['From'] = f"{SMTP_FROM_NAME} <{SMTP_FROM_EMAIL}>"
        msg['To'] = to_email

        text_body = (
            f"Hello {company_name},\n\n"
            f"This is an automated reminder from Fleet Safety.\n\n"
            f"The following DOT compliance document is expiring soon:\n"
            f"  Driver:   {driver_name}\n"
            f"  Document: {doc_type}\n"
            f"  Expires:  {expiration_date}\n\n"
            f"Please upload a renewed document as soon as possible to remain DOT compliant.\n\n"
            f"This reminder is provided as a courtesy and does not guarantee compliance; "
            f"you remain responsible for verifying your own records.\n\n"
            f"- Fleet Safety Compliance Team"
        )

        html_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; color: #333; max-width: 600px; margin: 0 auto;">
            <div style="background: #1a1a2e; padding: 20px 30px; border-radius: 8px 8px 0 0;">
                <h2 style="color: #fff; margin: 0;">Fleet Safety Alert</h2>
            </div>
            <div style="background: #fff; border: 1px solid #dee2e6; border-top: none;
                        padding: 30px; border-radius: 0 0 8px 8px;">
                <p>Hello <strong>{company_name}</strong>,</p>
                <p>The following DOT compliance document is expiring within
                   <strong>{ALERT_DAYS} days</strong> and requires your attention:</p>
                <table style="width:100%; border-collapse:collapse; margin: 20px 0;">
                    <tr style="background:#f8f9fa;">
                        <td style="padding:10px 14px; font-weight:bold; width:40%;">Driver Name</td>
                        <td style="padding:10px 14px;">{driver_name}</td>
                    </tr>
                    <tr>
                        <td style="padding:10px 14px; font-weight:bold;">Document Type</td>
                        <td style="padding:10px 14px;">{doc_type}</td>
                    </tr>
                    <tr style="background:#f8f9fa;">
                        <td style="padding:10px 14px; font-weight:bold;">Expiration Date</td>
                        <td style="padding:10px 14px; color:#dc3545; font-weight:bold;">{expiration_date}</td>
                    </tr>
                </table>
                <p style="background:#fff3cd; border:1px solid #ffc107; border-radius:6px;
                          padding:12px 16px; color:#856404;">
                    Please upload a renewed document before the expiration date to
                    stay DOT compliant and avoid penalties.
                </p>
                <p style="color:#6c757d; font-size:0.85em; margin-top:30px;
                          border-top:1px solid #dee2e6; padding-top:16px;">
                    This automated reminder is provided as a courtesy and does not
                    guarantee compliance. You remain responsible for verifying your
                    own records. Please do not reply to this email.
                </p>
            </div>
        </body>
        </html>
        """

        msg.attach(MIMEText(text_body, 'plain'))
        msg.attach(MIMEText(html_body, 'html'))

        # FIX H-8: timeout so a dead SMTP server can't hang the job forever.
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM_EMAIL, to_email, msg.as_string())

        print(f"  Email sent to {to_email}")
        return True
    except smtplib.SMTPAuthenticationError:
        print("  SMTP authentication failed. Check SMTP_USERNAME / SMTP_PASSWORD in .env")
        return False
    except Exception as e:
        print(f"  Email failed for {to_email}: {e}")
        return False


# ==============================================================
# HOUSEKEEPING — delete orphaned temp scans
# ==============================================================

def cleanup_orphans(conn, max_age_hours=24):
    """
    FIX H-7: If a client scans a file but closes the tab without confirming,
    the temp file sits on disk forever (unreferenced PII). Once a day we
    delete any file that is not in the database AND is older than 24h.
    """
    import time
    known = {r[0] for r in conn.execute('SELECT filename FROM uploads')}
    if not os.path.isdir(UPLOAD_FOLDER):
        return
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    for name in os.listdir(UPLOAD_FOLDER):
        path = os.path.join(UPLOAD_FOLDER, name)
        if name not in known and os.path.isfile(path) and os.path.getmtime(path) < cutoff:
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass
    if removed:
        print(f"  Cleaned up {removed} orphaned temp file(s).")


# ==============================================================
# MAIN
# ==============================================================

def run_alarm():
    today = business_today()
    alert_until = today + timedelta(days=ALERT_DAYS)

    print(f"\nFleet Safety Alarm — {today.isoformat()}")
    print(f"   Checking for documents expiring between {today} and {alert_until}...\n")

    try:
        conn = sqlite3.connect(DATABASE, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA busy_timeout=15000')
        cursor = conn.cursor()

        # FIX H-5: skip clients who replied STOP (sms_opted_out = 1).
        # We still allow email fallback for opted-out SMS users below.
        rows = cursor.execute(
            """
            SELECT u.id AS upload_id,
                   u.driver_name, u.document_type, u.expiration_date,
                   c.company_name, c.phone_number, c.email, c.sms_opted_out
            FROM uploads u
            JOIN clients c ON u.client_uuid = c.uuid
            WHERE u.expiration_date IS NOT NULL
              AND u.expiration_date >= ?
              AND u.expiration_date <= ?
            ORDER BY u.expiration_date ASC
            """,
            (today.isoformat(), alert_until.isoformat())
        ).fetchall()

    except Exception as e:
        print(f"Database error: {e}")
        return

    if not rows:
        print("No documents expiring in the next 30 days.")
        cleanup_orphans(conn)
        conn.close()
        return

    print(f"Found {len(rows)} expiring document(s):\n")
    sent_count = 0
    failed_count = 0
    skipped_count = 0

    for row in rows:
        upload_id = row['upload_id']

        # FIX C-5: have we already alerted about THIS document recently?
        already = cursor.execute(
            "SELECT 1 FROM alert_log WHERE upload_id = ? "
            "AND sent_at > datetime('now', ?)",
            (upload_id, f'-{REALERT_AFTER_DAYS} days')
        ).fetchone()
        if already:
            skipped_count += 1
            continue

        driver_name = row['driver_name'] or 'Unknown Driver'
        doc_type = row['document_type'] or 'Unknown Type'
        expiration_date = row['expiration_date']
        company_name = row['company_name']
        phone_number = row['phone_number']
        email = row['email']
        opted_out = bool(row['sms_opted_out'])

        try:
            exp_date = datetime.strptime(expiration_date, '%Y-%m-%d').date()
            days_left = (exp_date - today).days
        except ValueError:
            days_left = '?'

        print(f"  -> {company_name} | {driver_name} | {doc_type} | "
              f"expires {expiration_date} ({days_left} days)")

        success = False
        channel = None

        # Primary: SMS (only if they have a number AND haven't opted out).
        if phone_number and not opted_out:
            channel = 'sms'
            success = send_sms_notification(
                phone_number, driver_name, doc_type, expiration_date, company_name)
        elif email:
            channel = 'email'
            success = send_email_notification(
                email, driver_name, doc_type, expiration_date, company_name)
        elif phone_number and opted_out:
            print(f"     {company_name} opted out of SMS and has no email — skipped.")
        else:
            print(f"     No phone or email on file for {company_name} — cannot send!")

        if success:
            sent_count += 1
            # FIX C-5: record it so we don't re-send tomorrow.
            conn.execute(
                'INSERT INTO alert_log (upload_id, channel) VALUES (?, ?)',
                (upload_id, channel)
            )
            conn.commit()
        elif channel:
            failed_count += 1

    cleanup_orphans(conn)
    conn.close()

    print(f"\nDone. Sent: {sent_count} | Failed: {failed_count} | "
          f"Skipped (recently alerted): {skipped_count} | Total: {len(rows)}\n")


if __name__ == '__main__':
    run_alarm()
