import os
import uuid
import sqlite3
import json
import base64
import time
import hmac
import secrets
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from functools import wraps
from flask import Flask, request, render_template, redirect, url_for, g, session, send_from_directory, jsonify
from flask_session import Session
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
from groq import Groq
from twilio.twiml.messaging_response import MessagingResponse
from twilio.request_validator import RequestValidator
import pdfplumber
import docx

# ============================================================
# Load all secret keys from your .env file.
# ============================================================
load_dotenv()

# --- App Configuration ---
app = Flask(__name__)

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'uploads')
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

DATABASE = os.path.join(os.path.dirname(__file__), 'database.db')

ALLOWED_EXTENSIONS = {'pdf', 'png', 'jpg', 'jpeg', 'docx'}

# FIX C-3: Cap upload size at 10 MB. Without this, one giant file could
# fill the server disk or eat all its memory. Flask automatically rejects
# anything bigger with a 413 error (handled below).
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024  # 10 megabytes

# File types the browser can display inline in the Step 2 preview.
PREVIEWABLE_AS_IFRAME = {'pdf'}
PREVIEWABLE_AS_IMAGE = {'png', 'jpg', 'jpeg'}

# Valid document types the upload form accepts.
DOCUMENT_TYPES = ['Medical Card', "Driver's License", 'Annual Review', 'Other']

# The timezone your BUSINESS runs on. FIX H-4: the server clock may be in
# UTC/Europe, but your clients are in the US. If we used the server's date,
# a document could show "Expired" a day early (or alerts fire on the wrong
# day). We pin everything to one US timezone so dates are consistent.
BUSINESS_TZ = ZoneInfo('America/Chicago')

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ============================================================
# SESSION CONFIGURATION (Flask-Session — server-side storage)
# ============================================================
# FIX C-2: NO fallback secret key. If SECRET_KEY is missing from .env,
# the app refuses to start instead of quietly running with a public key
# that anyone could use to forge admin sessions.
app.secret_key = os.environ['SECRET_KEY']

SESSION_DIR = os.path.join(os.path.dirname(__file__), 'flask_session')
os.makedirs(SESSION_DIR, exist_ok=True)

app.config['SESSION_TYPE'] = 'filesystem'
app.config['SESSION_FILE_DIR'] = SESSION_DIR
app.config['SESSION_PERMANENT'] = False
app.config['SESSION_USE_SIGNER'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = True       # JS cannot read the cookie
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'      # partial CSRF protection
app.config['SESSION_COOKIE_SECURE'] = True          # FIX M-6: cookie only sent over HTTPS

Session(app)

# --- Admin Credentials ---
# FIX C-2: These MUST be set in .env. No 'admin'/'fleet2024' fallback.
ADMIN_USERNAME = os.environ['ADMIN_USERNAME']
ADMIN_PASSWORD = os.environ['ADMIN_PASSWORD']

# --- Groq Client (created lazily so a missing key doesn't crash boot) ---
# FIX C-7: If GROQ_API_KEY is missing, Groq(api_key=None) used to crash the
# whole app at startup. Now we only build the client the first time we
# actually need it, with a hard 30-second timeout so a slow AI call can't
# hang a web worker forever.
_groq_client = None


def get_groq():
    global _groq_client
    if _groq_client is None:
        key = os.getenv('GROQ_API_KEY')
        if not key:
            raise RuntimeError('GROQ_API_KEY is not set in .env')
        _groq_client = Groq(api_key=key, timeout=30.0, max_retries=1)
    return _groq_client


# ==============================================================
# CSRF PROTECTION (lightweight, no extra libraries needed)
# ==============================================================
# FIX H-2: A CSRF token is a secret random string tied to your session.
# Every admin form must send it back. A malicious website can't read it,
# so it can't trick your logged-in browser into deleting a client, etc.

def get_csrf_token():
    """Returns the session's CSRF token, creating one if needed."""
    if '_csrf_token' not in session:
        session['_csrf_token'] = secrets.token_hex(32)
    return session['_csrf_token']


@app.context_processor
def inject_csrf_token():
    """Makes csrf_token() callable inside every template."""
    return {'csrf_token': get_csrf_token}


def check_csrf():
    """Returns True if the submitted CSRF token matches the session's."""
    sent = request.form.get('csrf_token', '')
    real = session.get('_csrf_token', '')
    return bool(real) and hmac.compare_digest(sent, real)


# ==============================================================
# DATABASE HELPERS
# ==============================================================

def get_db():
    """Opens a database connection if one doesn't already exist for this request."""
    if 'db' not in g:
        # FIX M-1: timeout + WAL mode let multiple uploads happen at once
        # without hitting "database is locked" errors.
        g.db = sqlite3.connect(DATABASE, timeout=15)
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA journal_mode=WAL')
        g.db.execute('PRAGMA busy_timeout=15000')
    return g.db


@app.teardown_appcontext
def close_db(error):
    """Automatically closes the database connection when a request finishes."""
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    """
    Creates our database tables if they don't exist.
    Also runs migrations: adds new columns to old databases without losing data.
    """
    db = sqlite3.connect(DATABASE)

    db.executescript('''
        CREATE TABLE IF NOT EXISTS clients (
            uuid          TEXT PRIMARY KEY,
            company_name  TEXT NOT NULL,
            phone_number  TEXT,
            email         TEXT,
            sms_opted_out INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS uploads (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            client_uuid     TEXT NOT NULL,
            filename        TEXT NOT NULL UNIQUE,
            document_type   TEXT,
            driver_name     TEXT,
            expiration_date TEXT,
            uploaded_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (client_uuid) REFERENCES clients(uuid)
        );

        -- FIX C-5: remembers which documents we've already alerted about,
        -- so alarm.py doesn't text the same client every single day.
        CREATE TABLE IF NOT EXISTS alert_log (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            upload_id INTEGER NOT NULL,
            channel   TEXT,
            sent_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    ''')

    # --- Migrations for older databases ---
    client_cols = [row[1] for row in db.execute('PRAGMA table_info(clients)').fetchall()]
    if 'email' not in client_cols:
        db.execute('ALTER TABLE clients ADD COLUMN email TEXT')
    if 'sms_opted_out' not in client_cols:
        db.execute('ALTER TABLE clients ADD COLUMN sms_opted_out INTEGER DEFAULT 0')

    upload_cols = [row[1] for row in db.execute('PRAGMA table_info(uploads)').fetchall()]
    if 'driver_name' not in upload_cols:
        db.execute('ALTER TABLE uploads ADD COLUMN driver_name TEXT')
    if 'expiration_date' not in upload_cols:
        db.execute('ALTER TABLE uploads ADD COLUMN expiration_date TEXT')
    if 'document_type' not in upload_cols:
        db.execute('ALTER TABLE uploads ADD COLUMN document_type TEXT')

    db.commit()
    db.close()
    print("Database initialized successfully.")


init_db()


# ==============================================================
# SECURITY HEADERS (applied to every response)
# ==============================================================

@app.after_request
def set_security_headers(resp):
    # FIX M-5: stop other websites from framing our pages (clickjacking),
    # while still allowing our own iframe preview to work.
    resp.headers['X-Frame-Options'] = 'SAMEORIGIN'
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['Content-Security-Policy'] = "frame-ancestors 'self'"
    return resp


@app.errorhandler(413)
def file_too_large(e):
    # FIX C-3: friendly message when an upload exceeds MAX_CONTENT_LENGTH.
    return jsonify(success=False, error='File too large. Maximum size is 10 MB.'), 413


# ==============================================================
# ADMIN AUTHENTICATION
# ==============================================================

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin_logged_in'):
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated


# FIX C-1: this route DID NOT EXIST before, so /admin crashed with a 500
# because it tried to redirect to a login page that had no route.
# We also add brute-force protection (FIX) and session-fixation protection.
_FAILED_LOGINS = {}          # ip_address -> (fail_count, last_fail_timestamp)
_MAX_ATTEMPTS = 5
_LOCKOUT_SECONDS = 300       # 5 minutes


@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    # Already logged in? Skip the form.
    if session.get('admin_logged_in'):
        return redirect(url_for('admin'))

    error = None
    if request.method == 'POST':
        ip = request.headers.get('X-Real-IP', request.remote_addr or 'unknown')
        fails, last = _FAILED_LOGINS.get(ip, (0, 0))

        # Locked out from too many wrong guesses?
        if fails >= _MAX_ATTEMPTS and (time.time() - last) < _LOCKOUT_SECONDS:
            error = "Too many failed attempts. Please wait 5 minutes and try again."
        elif not check_csrf():
            error = "Your session expired. Please try again."
        else:
            username = request.form.get('username', '')
            password = request.form.get('password', '')

            # compare_digest = constant-time compare, which prevents a
            # "timing attack" where an attacker measures response speed
            # to guess the password one character at a time.
            user_ok = hmac.compare_digest(username, ADMIN_USERNAME)
            pass_ok = hmac.compare_digest(password, ADMIN_PASSWORD)

            if user_ok and pass_ok:
                # Wipe any existing session data BEFORE marking as logged in.
                # This stops "session fixation" (an attacker planting a known
                # session id before you log in).
                session.clear()
                session['admin_logged_in'] = True
                session['admin_username'] = username
                _FAILED_LOGINS.pop(ip, None)
                return redirect(url_for('admin'))
            else:
                _FAILED_LOGINS[ip] = (fails + 1, time.time())
                error = "Invalid username or password."

    return render_template('login.html', error=error)


@app.route('/admin/logout')
def admin_logout():
    session.clear()
    return redirect(url_for('admin_login'))


# ==============================================================
# SECURE FILE SERVING
# ==============================================================

@app.route('/admin/uploads/<filename>')
@login_required
def serve_upload(filename):
    """Serves an uploaded file to logged-in admins only."""
    safe = os.path.basename(filename)  # block path traversal
    return send_from_directory(app.config['UPLOAD_FOLDER'], safe)


# ==============================================================
# AI DOCUMENT EXTRACTION (Groq)
# ==============================================================

def _build_extraction_prompt(document_type=None):
    doc_context = (
        f"The user has identified this document as a '{document_type}'. "
        if document_type else ""
    )
    return (
        f"You are a document reader for a trucking compliance company. "
        f"{doc_context}"
        f"Read this document and extract exactly two things:\n"
        f"1. The driver's full name\n"
        f"2. The expiration date of the document\n\n"
        f"Return ONLY a JSON object in this exact format (no extra text, no markdown):\n"
        '{"driver_name": "John Smith", "expiration_date": "2025-12-31"}\n\n'
        f"Use YYYY-MM-DD format for dates. "
        f"If you cannot find a value, use null instead of guessing."
    )


def _ask_groq_with_text(text_content, document_type=None):
    prompt = _build_extraction_prompt(document_type)
    response = get_groq().chat.completions.create(
        model='llama3-8b-8192',
        messages=[
            {"role": "system", "content": (
                "You extract structured data from documents. "
                "Always respond with valid JSON only. No extra text."
            )},
            {"role": "user", "content": f"{prompt}\n\nDocument text:\n{text_content[:6000]}"},
        ],
        response_format={"type": "json_object"},
    )
    return response.choices[0].message.content


def _ask_groq_with_image(file_bytes, mime_type, document_type=None):
    prompt = _build_extraction_prompt(document_type)
    b64_image = base64.b64encode(file_bytes).decode('utf-8')
    data_url = f"data:{mime_type};base64,{b64_image}"
    response = get_groq().chat.completions.create(
        model='meta-llama/llama-4-scout-17b-16e-instruct',
        messages=[
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": data_url}},
                {"type": "text", "text": prompt},
            ]},
        ],
        response_format={"type": "json_object"},
    )
    return response.choices[0].message.content


def safe_extension(filename):
    """
    FIX H-1: safely pulls the file extension. A filename like ".pdf" or one
    with no dot used to crash with an IndexError. This returns '' instead of
    crashing.
    """
    parts = filename.rsplit('.', 1)
    return parts[1].lower() if len(parts) == 2 and parts[1] else ''


def extract_document_data(file_path, filename, document_type=None):
    """
    Detects file type, extracts content, sends to Groq, returns
    (driver_name, expiration_date). Returns (None, None) on ANY failure so
    the upload still succeeds and the human can fill in the fields.
    """
    extension = safe_extension(filename)  # FIX H-1: no more IndexError

    try:
        raw_json_string = None

        if extension in ('jpg', 'jpeg', 'png'):
            mime_map = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png'}
            with open(file_path, 'rb') as f:
                file_bytes = f.read()
            raw_json_string = _ask_groq_with_image(file_bytes, mime_map[extension], document_type)

        elif extension == 'pdf':
            text_parts = []
            with pdfplumber.open(file_path) as pdf:
                # FIX M-2: only read the first 10 pages. We only send 6000
                # characters to the AI anyway, so parsing a 500-page PDF is
                # a waste of memory.
                for page in pdf.pages[:10]:
                    page_text = page.extract_text()
                    if page_text:
                        text_parts.append(page_text)
                    page.flush_cache()  # free memory as we go
            full_text = '\n'.join(text_parts)
            if not full_text.strip():
                print("  PDF appears to be image-only (scanned). No text found.")
                return None, None
            raw_json_string = _ask_groq_with_text(full_text, document_type)

        elif extension == 'docx':
            document = docx.Document(file_path)
            text = '\n'.join(para.text for para in document.paragraphs)
            raw_json_string = _ask_groq_with_text(text, document_type)

        else:
            print(f"  Unsupported file type: {extension}")
            return None, None

        # FIX (edge case): Groq could return text that isn't valid JSON.
        # json.loads would throw — but this whole block is inside try/except,
        # so a bad response just becomes (None, None) instead of a crash.
        data = json.loads(raw_json_string)
        return data.get('driver_name'), data.get('expiration_date')

    except Exception as e:
        print(f"  AI extraction failed for {filename}: {e}")
        return None, None


# ==============================================================
# HELPERS
# ==============================================================

def allowed_file(filename):
    return safe_extension(filename) in ALLOWED_EXTENSIONS


def make_unique_filename(original_filename, client_uuid):
    """
    Builds a safe, collision-proof, OWNER-TAGGED filename.

    FIX C-4: We prefix the client's uuid so we can later prove which client
    a file belongs to. This stops one client (with a valid magic link) from
    previewing, confirming, or deleting another client's files.
    secure_filename can return '' for exotic names, so we fall back to
    'document'.
    """
    safe_name = secure_filename(original_filename) or 'document'
    unique_prefix = uuid.uuid4().hex[:12]
    return f"{client_uuid}_{unique_prefix}_{safe_name}"


def business_today():
    """FIX H-4: 'today' according to our business timezone, not the server clock."""
    return datetime.now(BUSINESS_TZ).date()


def get_expiry_status(expiration_date_str):
    if not expiration_date_str:
        return 'unknown'
    try:
        exp_date = datetime.strptime(expiration_date_str, '%Y-%m-%d').date()
        today = business_today()
        if exp_date < today:
            return 'expired'
        elif exp_date <= today + timedelta(days=30):
            return 'expiring'
        else:
            return 'valid'
    except ValueError:
        return 'unknown'


def build_chart_data(clients_data):
    status_counts = {'Valid': 0, 'Expiring Soon': 0, 'Expired': 0, 'Unknown': 0}
    doc_type_counts = {dt: 0 for dt in DOCUMENT_TYPES}

    for item in clients_data:
        for row in item['uploads']:
            status = row['status']
            if status == 'valid':
                status_counts['Valid'] += 1
            elif status == 'expiring':
                status_counts['Expiring Soon'] += 1
            elif status == 'expired':
                status_counts['Expired'] += 1
            else:
                status_counts['Unknown'] += 1

            doc_type = row['upload']['document_type']
            if doc_type and doc_type in doc_type_counts:
                doc_type_counts[doc_type] += 1
            elif doc_type:
                doc_type_counts[doc_type] = doc_type_counts.get(doc_type, 0) + 1

    return {
        'compliance_labels': list(status_counts.keys()),
        'compliance_data': list(status_counts.values()),
        'doctype_labels': list(doc_type_counts.keys()),
        'doctype_data': list(doc_type_counts.values()),
    }


# ==============================================================
# ROUTES
# ==============================================================

@app.route('/')
def index():
    return redirect(url_for('admin'))


# --- Public legal pages (no login: Twilio reviewers must reach these) ---
@app.route('/privacy')
def privacy_policy():
    return render_template('privacy.html')


@app.route('/tos')
def terms_of_service():
    return render_template('tos.html')


@app.route('/admin', methods=['GET', 'POST'])
@login_required
def admin():
    error = None
    db = get_db()

    if request.method == 'POST':
        if not check_csrf():                       # FIX H-2
            error = "Your session expired. Please try again."
        else:
            company_name = request.form.get('company_name', '').strip()
            phone_number = request.form.get('phone_number', '').strip()
            email = request.form.get('email', '').strip()

            if not company_name:
                error = "Company name is required."
            else:
                client_uuid = str(uuid.uuid4())
                db.execute(
                    'INSERT INTO clients (uuid, company_name, phone_number, email) VALUES (?, ?, ?, ?)',
                    (client_uuid, company_name, phone_number or None, email or None)
                )
                db.commit()
                return redirect(url_for('admin', created_uuid=client_uuid))

    magic_link = None
    created_uuid = request.args.get('created_uuid')
    if created_uuid:
        magic_link = url_for('upload_file', client_uuid=created_uuid, _external=True)

    clients = db.execute('SELECT * FROM clients ORDER BY company_name').fetchall()

    total_docs = 0
    expiring_soon_count = 0
    expired_count = 0
    clients_data = []

    for client in clients:
        uploads = db.execute(
            'SELECT * FROM uploads WHERE client_uuid = ? ORDER BY uploaded_at DESC',
            (client['uuid'],)
        ).fetchall()
        total_docs += len(uploads)

        uploads_with_status = []
        for u in uploads:
            status = get_expiry_status(u['expiration_date'])
            if status == 'expiring':
                expiring_soon_count += 1
            elif status == 'expired':
                expired_count += 1
            uploads_with_status.append({'upload': u, 'status': status})

        clients_data.append({
            'client': client,
            'uploads': uploads_with_status,
            'upload_count': len(uploads),
        })

    stats = {
        'total_clients': len(clients),
        'total_docs': total_docs,
        'expiring_soon': expiring_soon_count,
        'expired': expired_count,
    }

    return render_template(
        'admin.html',
        magic_link=magic_link,
        clients_data=clients_data,
        error=error,
        stats=stats,
        chart_data=build_chart_data(clients_data),
        admin_username=session.get('admin_username', 'Admin'),
    )


@app.route('/admin/delete/<client_uuid>', methods=['POST'])
@login_required
def delete_client(client_uuid):
    if not check_csrf():                            # FIX H-2
        return "Invalid CSRF token.", 400

    db = get_db()
    uploads = db.execute(
        'SELECT filename FROM uploads WHERE client_uuid = ?', (client_uuid,)
    ).fetchall()

    for upload in uploads:
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], os.path.basename(upload['filename']))
        if os.path.exists(file_path):
            os.remove(file_path)

    db.execute('DELETE FROM uploads WHERE client_uuid = ?', (client_uuid,))
    db.execute('DELETE FROM clients WHERE uuid = ?', (client_uuid,))
    db.commit()
    return redirect(url_for('admin'))


@app.route('/upload/<client_uuid>', methods=['GET'])
def upload_file(client_uuid):
    db = get_db()
    client = db.execute('SELECT * FROM clients WHERE uuid = ?', (client_uuid,)).fetchone()
    if client is None:
        return '<h2>Invalid Link</h2><p>This upload link is not valid or has expired.</p>', 404

    past_uploads = db.execute(
        '''SELECT filename, uploaded_at, document_type, driver_name, expiration_date
           FROM uploads WHERE client_uuid = ? ORDER BY uploaded_at DESC''',
        (client_uuid,)
    ).fetchall()

    return render_template(
        'upload.html',
        client=client,
        past_uploads=past_uploads,
        document_types=DOCUMENT_TYPES,
    )


def _client_owns_file(client_uuid, safe_filename):
    """FIX C-4: a client may only touch files whose name starts with their uuid."""
    return safe_filename.startswith(client_uuid + '_')


@app.route('/upload/<client_uuid>/scan', methods=['POST'])
def scan_document(client_uuid):
    db = get_db()
    client = db.execute('SELECT * FROM clients WHERE uuid = ?', (client_uuid,)).fetchone()
    if client is None:
        return jsonify(success=False, error='Invalid or expired upload link.'), 404

    document_type = request.form.get('document_type', '').strip()
    file = request.files.get('file')

    if document_type not in DOCUMENT_TYPES:
        return jsonify(success=False, error='Please select a valid document type.'), 400
    if not file or file.filename == '':
        return jsonify(success=False, error='No file selected. Please choose a file.'), 400
    if not allowed_file(file.filename):
        return jsonify(success=False, error='File type not allowed. Please upload a PDF, image, or DOCX.'), 400

    # Save to disk under an owner-tagged unique name.
    saved_filename = make_unique_filename(file.filename, client_uuid)
    save_path = os.path.join(app.config['UPLOAD_FOLDER'], saved_filename)
    file.save(save_path)

    driver_name, expiration_date = extract_document_data(
        save_path, saved_filename, document_type=document_type
    )
    extraction_ok = bool(driver_name and expiration_date)

    extension = safe_extension(saved_filename)      # FIX H-1
    if extension in PREVIEWABLE_AS_IFRAME:
        preview_type = 'iframe'
    elif extension in PREVIEWABLE_AS_IMAGE:
        preview_type = 'image'
    else:
        preview_type = 'none'

    return jsonify(
        success=True,
        filename=saved_filename,
        original_filename=file.filename,
        document_type=document_type,
        driver_name=driver_name or '',
        expiration_date=expiration_date or '',
        extraction_ok=extraction_ok,
        preview_type=preview_type,
        preview_url=url_for('preview_document', client_uuid=client_uuid, filename=saved_filename),
    )


@app.route('/upload/<client_uuid>/preview/<filename>', methods=['GET'])
def preview_document(client_uuid, filename):
    client = get_db().execute('SELECT 1 FROM clients WHERE uuid = ?', (client_uuid,)).fetchone()
    if client is None:
        return '', 404

    safe_filename = os.path.basename(filename)
    if not _client_owns_file(client_uuid, safe_filename):   # FIX C-4
        return '', 403

    file_path = os.path.join(app.config['UPLOAD_FOLDER'], safe_filename)
    if not os.path.isfile(file_path):
        return '', 404
    return send_from_directory(app.config['UPLOAD_FOLDER'], safe_filename)


@app.route('/upload/<client_uuid>/discard/<filename>', methods=['POST'])
def discard_scan(client_uuid, filename):
    # FIX C-4: this route used to be an unauthenticated "delete any file"
    # hole. Now: the client must exist, must own the file, and the file must
    # NOT already be saved in the database (so a committed document can never
    # be deleted through here).
    db = get_db()
    if db.execute('SELECT 1 FROM clients WHERE uuid = ?', (client_uuid,)).fetchone() is None:
        return jsonify(success=False), 404

    safe_filename = os.path.basename(filename)
    if not _client_owns_file(client_uuid, safe_filename):
        return jsonify(success=False), 403
    if db.execute('SELECT 1 FROM uploads WHERE filename = ?', (safe_filename,)).fetchone():
        return jsonify(success=False, error='File already saved.'), 400

    file_path = os.path.join(app.config['UPLOAD_FOLDER'], safe_filename)
    try:
        if os.path.isfile(file_path):
            os.remove(file_path)
    except OSError as e:
        print(f"  Could not discard {safe_filename}: {e}")
    return jsonify(success=True)


@app.route('/upload/<client_uuid>/confirm', methods=['POST'])
def confirm_document(client_uuid):
    db = get_db()
    client = db.execute('SELECT * FROM clients WHERE uuid = ?', (client_uuid,)).fetchone()
    if client is None:
        return jsonify(success=False, error='Invalid or expired upload link.'), 404

    data = request.get_json(silent=True) or {}
    filename = (data.get('filename') or '').strip()
    document_type = (data.get('document_type') or '').strip()
    driver_name = (data.get('driver_name') or '').strip()
    expiration_date = (data.get('expiration_date') or '').strip()

    if document_type not in DOCUMENT_TYPES:
        return jsonify(success=False, error='Invalid document type.'), 400
    if not filename:
        return jsonify(success=False, error='Missing filename — please scan the document again.'), 400

    safe_filename = os.path.basename(filename)
    if not _client_owns_file(client_uuid, safe_filename):   # FIX C-4
        return jsonify(success=False, error='That file does not belong to this client.'), 403

    file_path = os.path.join(app.config['UPLOAD_FOLDER'], safe_filename)
    if not os.path.isfile(file_path):
        return jsonify(success=False, error='That file was not found on the server — please scan again.'), 400

    # FIX H-3: never store a date the alarm can't understand. An empty date is
    # allowed (means "unknown"), but a non-empty one MUST be YYYY-MM-DD.
    if expiration_date:
        try:
            datetime.strptime(expiration_date, '%Y-%m-%d')
        except ValueError:
            return jsonify(success=False, error='Expiration date must be in YYYY-MM-DD format.'), 400

    try:
        db.execute(
            '''INSERT INTO uploads
               (client_uuid, filename, document_type, driver_name, expiration_date)
               VALUES (?, ?, ?, ?, ?)''',
            (client_uuid, safe_filename, document_type, driver_name or None, expiration_date or None)
        )
        db.commit()
    except sqlite3.IntegrityError:
        # FIX M-3: filename is UNIQUE, so a double-submit is caught here.
        return jsonify(success=False, error='This document has already been saved.'), 400

    return jsonify(
        success=True,
        message=f'"{safe_filename}" has been saved for {client["company_name"]}.'
    )


# ==============================================================
# TWILIO SMS WEBHOOK
# ==============================================================
# FIX H-5: handle ALL required opt-out keywords, authenticate the request
# came from Twilio, and actually record opt-outs in the database.

_OPT_OUT = {'STOP', 'STOPALL', 'UNSUBSCRIBE', 'CANCEL', 'END', 'QUIT'}
_OPT_IN = {'START', 'YES', 'UNSTOP'}


@app.route('/webhook/sms', methods=['POST'])
def sms_webhook():
    # Verify Twilio's signature so random people can't hit this endpoint.
    auth_token = os.getenv('TWILIO_AUTH_TOKEN', '')
    validator = RequestValidator(auth_token)
    signature = request.headers.get('X-Twilio-Signature', '')
    if not validator.validate(request.url, request.form, signature):
        return '', 403

    incoming = request.values.get('Body', '').strip().upper()
    from_number = request.values.get('From', '')
    # Look at the first word so "STOP please" or "Stop." still counts.
    first_word = incoming.split()[0].rstrip('.,!?') if incoming else ''

    response = MessagingResponse()
    db = get_db()

    if first_word in _OPT_OUT:
        db.execute('UPDATE clients SET sms_opted_out = 1 WHERE phone_number = ?', (from_number,))
        db.commit()
        response.message(
            "You have been unsubscribed from Fleet Safety Alerts. "
            "You will not receive any more messages. Reply START to resubscribe."
        )
    elif first_word in _OPT_IN:
        db.execute('UPDATE clients SET sms_opted_out = 0 WHERE phone_number = ?', (from_number,))
        db.commit()
        response.message(
            "You are re-subscribed to Fleet Safety Alerts. "
            "Reply HELP for help, STOP to cancel."
        )
    elif first_word == 'HELP':
        response.message(
            "Fleet Safety Alerts: Email tryfleetsafety@gmail.com for support. "
            "Msg & data rates may apply. Reply STOP to cancel."
        )

    return str(response)


# --- Start the Server ---
if __name__ == '__main__':
    # FIX C-6: NEVER run the debugger in production (it's a remote code
    # execution hole). Only on if you explicitly set FLASK_DEBUG=1 locally.
    app.run(debug=os.getenv('FLASK_DEBUG') == '1')
