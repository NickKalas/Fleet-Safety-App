"""
alarm.py — Fleet Safety Background Alarm Script
================================================
Run this script on a schedule (e.g., every morning via cron or Task Scheduler)
to automatically notify clients whose DOT documents are expiring within 30 days.

Notification logic:
  - If the client HAS a phone number  → send an SMS via Twilio
  - If the client has NO phone number → send an email via smtplib (SMTP)

Usage:
    python alarm.py

Cron example (runs every day at 8:00 AM):
    0 8 * * * /path/to/venv/bin/python /path/to/alarm.py >> /var/log/fleet_alarm.log 2>&1
"""

import os
import sqlite3
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import date, timedelta, datetime
from dotenv import load_dotenv

# Load credentials from your .env file
load_dotenv()

# ==============================================================
# CONFIGURATION — plug your credentials into .env
# ==============================================================

# --- Twilio (SMS) ---
# Add these to your .env file:
#   TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
#   TWILIO_AUTH_TOKEN=your_auth_token
#   TWILIO_FROM_NUMBER=+12125550000
TWILIO_ACCOUNT_SID  = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN   = os.getenv('TWILIO_AUTH_TOKEN')
TWILIO_FROM_NUMBER  = os.getenv('TWILIO_FROM_NUMBER')

# --- Email / SMTP ---
# Add these to your .env file:
#   SMTP_HOST=smtp.gmail.com
#   SMTP_PORT=587
#   SMTP_USERNAME=your@gmail.com
#   SMTP_PASSWORD=your_app_password        ← use a Gmail App Password, not your real password
#   SMTP_FROM_EMAIL=your@gmail.com
#   SMTP_FROM_NAME=Fleet Safety Alerts
SMTP_HOST       = os.getenv('SMTP_HOST', 'smtp.gmail.com')
SMTP_PORT       = int(os.getenv('SMTP_PORT', 587))
SMTP_USERNAME   = os.getenv('SMTP_USERNAME')
SMTP_PASSWORD   = os.getenv('SMTP_PASSWORD')
SMTP_FROM_EMAIL = os.getenv('SMTP_FROM_EMAIL', SMTP_USERNAME)
SMTP_FROM_NAME  = os.getenv('SMTP_FROM_NAME', 'Fleet Safety Alerts')

# --- Database ---
DATABASE = os.path.join(os.path.dirname(__file__), 'database.db')

# --- Alert window ---
ALERT_DAYS = 30  # Warn about documents expiring within this many days


# ==============================================================
# SMS NOTIFICATION — Twilio
# ==============================================================

def send_sms_notification(to_phone: str, driver_name: str, doc_type: str,
                           expiration_date: str, company_name: str) -> bool:
    """
    Sends an SMS alert to the client's phone number via Twilio.

    Returns True if the message was sent successfully, False on error.
    """
    try:
        from twilio.rest import Client
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

        body = (
            f"[Fleet Safety Alert] Hi {company_name},\n"
            f"Driver: {driver_name}\n"
            f"Document: {doc_type}\n"
            f"Expires: {expiration_date}\n"
            f"Please renew this document before the expiration date to stay DOT compliant."
        )

        message = client.messages.create(
            body=body,
            from_=TWILIO_FROM_NUMBER,
            to=to_phone,
        )
        print(f"  📱 SMS sent to {to_phone} — SID: {message.sid}")
        return True

    except ImportError:
        print("  ❌ Twilio library not installed. Run: pip install twilio")
        return False
    except Exception as e:
        print(f"  ❌ Twilio SMS failed for {to_phone}: {e}")
        return False


# ==============================================================
# EMAIL NOTIFICATION — smtplib (SMTP)
# ==============================================================

def send_email_notification(to_email: str, driver_name: str, doc_type: str,
                             expiration_date: str, company_name: str) -> bool:
    """
    Sends an email alert via standard Python smtplib.
    Used as a fallback when the client has no phone number on file.

    To use this with Gmail:
      1. Enable 2-Factor Authentication on your Google account.
      2. Go to Google Account → Security → App Passwords.
      3. Generate an App Password for "Mail".
      4. Set SMTP_PASSWORD in your .env to that App Password.

    Returns True if sent successfully, False on error.
    """
    if not SMTP_USERNAME or not SMTP_PASSWORD:
        print("  ⚠️  SMTP credentials not set in .env — skipping email.")
        return False

    try:
        # Build the email message
        msg = MIMEMultipart('alternative')
        msg['Subject'] = f"[Action Required] DOT Document Expiring — {company_name}"
        msg['From']    = f"{SMTP_FROM_NAME} <{SMTP_FROM_EMAIL}>"
        msg['To']      = to_email

        # Plain-text version (for email clients that don't render HTML)
        text_body = (
            f"Hello {company_name},\n\n"
            f"This is an automated reminder from Fleet Safety.\n\n"
            f"The following DOT compliance document is expiring soon:\n"
            f"  Driver:       {driver_name}\n"
            f"  Document:     {doc_type}\n"
            f"  Expires:      {expiration_date}\n\n"
            f"Please upload a renewed document as soon as possible to remain DOT compliant.\n\n"
            f"– Fleet Safety Compliance Team"
        )

        # HTML version (richer formatting displayed by most modern email clients)
        html_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; color: #333; max-width: 600px; margin: 0 auto;">
            <div style="background: #1a1a2e; padding: 20px 30px; border-radius: 8px 8px 0 0;">
                <h2 style="color: #fff; margin: 0;">🚚 Fleet Safety Alert</h2>
            </div>
            <div style="background: #fff; border: 1px solid #dee2e6; border-top: none;
                        padding: 30px; border-radius: 0 0 8px 8px;">
                <p>Hello <strong>{company_name}</strong>,</p>
                <p>The following DOT compliance document is expiring within <strong>{ALERT_DAYS} days</strong>
                   and requires your immediate attention:</p>

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
                    ⚠️ Please upload a renewed document before the expiration date to
                    stay DOT compliant and avoid penalties.
                </p>

                <p style="color:#6c757d; font-size:0.85em; margin-top:30px; border-top:1px solid #dee2e6; padding-top:16px;">
                    This is an automated message from the Fleet Safety compliance system.
                    Please do not reply to this email.
                </p>
            </div>
        </body>
        </html>
        """

        # Attach both versions — email clients pick the best one they support
        msg.attach(MIMEText(text_body, 'plain'))
        msg.attach(MIMEText(html_body, 'html'))

        # Connect to the SMTP server and send
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()       # Upgrade to encrypted connection
            server.ehlo()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM_EMAIL, to_email, msg.as_string())

        print(f"  📧 Email sent to {to_email}")
        return True

    except smtplib.SMTPAuthenticationError:
        print(f"  ❌ SMTP authentication failed. Check your SMTP_USERNAME and SMTP_PASSWORD in .env")
        return False
    except Exception as e:
        print(f"  ❌ Email failed for {to_email}: {e}")
        return False


# ==============================================================
# MAIN — Query DB and fire notifications
# ==============================================================

def run_alarm():
    """
    Main alarm function.

    Steps:
      1. Connect to the database.
      2. Find all uploads expiring within the next ALERT_DAYS days.
      3. For each expiring doc:
         - If the client has a phone number → send SMS.
         - Else if the client has an email   → send email.
         - Else                              → log a warning (no contact info).
    """
    today       = date.today()
    alert_until = today + timedelta(days=ALERT_DAYS)

    print(f"\n🚨 Fleet Safety Alarm — {today.isoformat()}")
    print(f"   Checking for documents expiring between {today} and {alert_until}...\n")

    try:
        conn = sqlite3.connect(DATABASE)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # JOIN uploads with clients to get contact info alongside document data.
        # We filter for expiration dates that fall within our alert window.
        rows = cursor.execute(
            """
            SELECT
                u.driver_name,
                u.document_type,
                u.expiration_date,
                c.company_name,
                c.phone_number,
                c.email
            FROM uploads u
            JOIN clients c ON u.client_uuid = c.uuid
            WHERE u.expiration_date IS NOT NULL
              AND u.expiration_date >= ?
              AND u.expiration_date <= ?
            ORDER BY u.expiration_date ASC
            """,
            (today.isoformat(), alert_until.isoformat())
        ).fetchall()

        conn.close()

    except Exception as e:
        print(f"❌ Database error: {e}")
        return

    if not rows:
        print("✅ No documents expiring in the next 30 days. Nothing to do.")
        return

    print(f"⚠️  Found {len(rows)} expiring document(s):\n")

    sent_count  = 0
    failed_count = 0

    for row in rows:
        driver_name     = row['driver_name']     or 'Unknown Driver'
        doc_type        = row['document_type']   or 'Unknown Type'
        expiration_date = row['expiration_date']
        company_name    = row['company_name']
        phone_number    = row['phone_number']
        email           = row['email']

        # How many days until expiry?
        try:
            exp_date    = datetime.strptime(expiration_date, '%Y-%m-%d').date()
            days_left   = (exp_date - today).days
        except ValueError:
            days_left = '?'

        print(f"  → {company_name} | {driver_name} | {doc_type} | expires {expiration_date} ({days_left} days)")

        success = False

        if phone_number:
            # Primary: SMS via Twilio
            success = send_sms_notification(
                to_phone=phone_number,
                driver_name=driver_name,
                doc_type=doc_type,
                expiration_date=expiration_date,
                company_name=company_name,
            )
        elif email:
            # Fallback: email via SMTP
            success = send_email_notification(
                to_email=email,
                driver_name=driver_name,
                doc_type=doc_type,
                expiration_date=expiration_date,
                company_name=company_name,
            )
        else:
            print(f"  ⚠️  No phone or email on file for {company_name} — cannot send alert!")

        if success:
            sent_count += 1
        elif phone_number or email:
            failed_count += 1

    print(f"\n✅ Done. Sent: {sent_count} | Failed: {failed_count} | Total: {len(rows)}\n")


# ==============================================================
# ENTRY POINT
# ==============================================================
if __name__ == '__main__':
    run_alarm()