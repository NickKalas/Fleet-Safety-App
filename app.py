import os
import uuid
import sqlite3
import json
import base64
from datetime import datetime, date, timedelta
from functools import wraps
from flask import Flask, request, render_template, redirect, url_for, g, session, send_from_directory, jsonify
from flask_session import Session
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
from groq import Groq
from twilio.twiml.messaging_response import MessagingResponse
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

# File types the browser can actually display inline (used by the
# split-screen preview in Step 2 — PDFs go in an <iframe>, images in
# an <img>. DOCX has no reliable in-browser viewer, so we tell the
# frontend "no preview available" for those instead.
PREVIEWABLE_AS_IFRAME = {'pdf'}
PREVIEWABLE_AS_IMAGE = {'png', 'jpg', 'jpeg'}

# Valid document types the upload form accepts.
DOCUMENT_TYPES = ['Medical Card', "Driver's License", 'Annual Review', 'Other']

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ============================================================
# SESSION CONFIGURATION (Flask-Session — server-side storage)
# This stores session data in a folder on the server, NOT in a
# browser cookie. It is much more secure for production.
# ============================================================
app.secret_key = os.getenv('SECRET_KEY', 'change-me-in-production-use-a-random-string')

SESSION_DIR = os.path.join(os.path.dirname(__file__), 'flask_session')
os.makedirs(SESSION_DIR, exist_ok=True)

app.config['SESSION_TYPE'] = 'filesystem'          # Store sessions as files
app.config['SESSION_FILE_DIR'] = SESSION_DIR       # Where to store those files
app.config['SESSION_PERMANENT'] = False            # Session expires when browser closes
app.config['SESSION_USE_SIGNER'] = True            # Sign the session cookie for extra safety
app.config['SESSION_COOKIE_HTTPONLY'] = True       # JS cannot read the cookie (XSS protection)
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'     # CSRF protection

Session(app)  # Initialize Flask-Session with our app

# --- Admin Credentials (set these in your .env file!) ---
# Example .env entries:
#   ADMIN_USERNAME=myadminuser
#   ADMIN_PASSWORD=MySuperSecretPassword123
ADMIN_USERNAME = os.getenv('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'fleet2024')

# --- Groq Client ---
groq_client = Groq(api_key=os.getenv('GROQ_API_KEY'))


# ==============================================================
# DATABASE HELPERS
# ==============================================================

def get_db():
    """Opens a database connection if one doesn't already exist for this request."""
    if 'db' not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
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
            email         TEXT
        );

        CREATE TABLE IF NOT EXISTS uploads (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            client_uuid     TEXT NOT NULL,
            filename        TEXT NOT NULL,
            document_type   TEXT,
            driver_name     TEXT,
            expiration_date TEXT,
            uploaded_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (client_uuid) REFERENCES clients(uuid)
        );
    ''')

    # Migration: add columns if they don't exist in older databases.
    existing_client_cols = [
        row[1] for row in db.execute('PRAGMA table_info(clients)').fetchall()
    ]
    if 'email' not in existing_client_cols:
        db.execute('ALTER TABLE clients ADD COLUMN email TEXT')
        print("  → Added 'email' column to clients table.")

    existing_upload_cols = [
        row[1] for row in db.execute('PRAGMA table_info(uploads)').fetchall()
    ]
    if 'driver_name' not in existing_upload_cols:
        db.execute('ALTER TABLE uploads ADD COLUMN driver_name TEXT')
        print("  → Added 'driver_name' column to uploads table.")
    if 'expiration_date' not in existing_upload_cols:
        db.execute('ALTER TABLE uploads ADD COLUMN expiration_date TEXT')
        print("  → Added 'expiration_date' column to uploads table.")
    if 'document_type' not in existing_upload_cols:
        db.execute('ALTER TABLE uploads ADD COLUMN document_type TEXT')
        print("  → Added 'document_type' column to uploads table.")

    db.commit()
    db.close()
    print("✅ Database initialized successfully.")


init_db()


# ==============================================================
# ADMIN AUTHENTICATION (Flask-Session based)
# ==============================================================

def login_required(f):
    """
    Decorator that protects any route by requiring admin login.
    If the admin is not logged in, they are redirected to /admin/login.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin_logged_in'):
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated


@app.route('/admin', methods=['GET', 'POST'])
@login_required
def admin():
    """
    Main admin dashboard.
    GET:  Show all clients with their uploaded documents, stats, and charts.
    POST: Create a new client and safely redirect to prevent duplicate submissions on refresh.
    """
    error = None
    db = get_db()

    if request.method == 'POST':
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
            
            # Post/Redirect/Get Pattern: Redirect to self with the new UUID in the query string
            return redirect(url_for('admin', created_uuid=client_uuid))

    # Catch if a client was just created right before the redirect
    magic_link = None
    created_uuid = request.args.get('created_uuid')
    if created_uuid:
        # Verify it exists just to be safe, then build the link
        client_check = db.execute('SELECT 1 FROM clients WHERE uuid = ?', (created_uuid,)).fetchone()
        if client_check:
            magic_link = url_for('upload_file', client_uuid=created_uuid, _external=True)

    clients = db.execute('SELECT * FROM clients ORDER BY company_name').fetchall()

    total_docs = 0
    expiring_soon_count = 0
    expired_count = 0
    clients_data = []

    for client in clients:
        uploads = db.execute(
            '''SELECT * FROM uploads
               WHERE client_uuid = ?
               ORDER BY uploaded_at DESC''',
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

    # Build data for the Chart.js analytics dashboard
    chart_data = build_chart_data(clients_data)

    return render_template(
        'admin.html',
        magic_link=magic_link,
        clients_data=clients_data,
        error=error,
        stats=stats,
        chart_data=chart_data,
        admin_username=session.get('admin_username', 'Admin'),
    )

@app.route('/admin/logout')
def admin_logout():
    """Clears the server-side session and redirects to login."""
    session.clear()
    return redirect(url_for('admin_login'))


# ==============================================================
# SECURE FILE SERVING
# ==============================================================

@app.route('/admin/uploads/<filename>')
@login_required
def serve_upload(filename):
    """Serves an uploaded file to logged-in admins only."""
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


# ==============================================================
# AI DOCUMENT EXTRACTION (Groq)
# ==============================================================

def _build_extraction_prompt(document_type=None):
    """
    Builds the extraction prompt, optionally injecting the document type
    so the AI has more context about what it is reading.
    """
    doc_context = (
        f"The user has identified this document as a '{document_type}'. "
        if document_type
        else ""
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
    """
    Sends plain text to Groq and returns the raw JSON string response.
    Used for PDFs and DOCX files after text extraction.
    """
    prompt = _build_extraction_prompt(document_type)
    response = groq_client.chat.completions.create(
        model='llama3-8b-8192',
        messages=[
            {
                "role": "system",
                "content": (
                    "You extract structured data from documents. "
                    "Always respond with valid JSON only. No extra text."
                )
            },
            {
                "role": "user",
                "content": f"{prompt}\n\nDocument text:\n{text_content[:6000]}"
            }
        ],
        response_format={"type": "json_object"}
    )
    return response.choices[0].message.content


def _ask_groq_with_image(file_bytes, mime_type, document_type=None):
    """
    Sends an image to Groq's vision model using base64 encoding.
    Used for JPG and PNG uploads.
    """
    prompt = _build_extraction_prompt(document_type)
    b64_image = base64.b64encode(file_bytes).decode('utf-8')
    data_url = f"data:{mime_type};base64,{b64_image}"

    response = groq_client.chat.completions.create(
        model='meta-llama/llama-4-scout-17b-16e-instruct',
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": prompt}
                ]
            }
        ],
        response_format={"type": "json_object"}
    )
    return response.choices[0].message.content


def extract_document_data(file_path, filename, document_type=None):
    """
    Main extraction router. Detects file type, extracts content,
    sends to Groq with document_type context, and returns (driver_name, expiration_date).
    Returns (None, None) on any failure so the upload still succeeds.
    """
    extension = filename.rsplit('.', 1)[1].lower()

    try:
        raw_json_string = None

        if extension in ['jpg', 'jpeg', 'png']:
            print(f"  → Sending image to Groq vision: {filename}")
            mime_map = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png'}
            mime_type = mime_map[extension]
            with open(file_path, 'rb') as f:
                file_bytes = f.read()
            raw_json_string = _ask_groq_with_image(file_bytes, mime_type, document_type)

        elif extension == 'pdf':
            print(f"  → Extracting text from PDF with pdfplumber: {filename}")
            text_parts = []
            with pdfplumber.open(file_path) as pdf:
                for page in pdf.pages:
                    page_text = page.extract_text()
                    if page_text:
                        text_parts.append(page_text)

            full_text = '\n'.join(text_parts)

            if not full_text.strip():
                print("  ⚠️  PDF appears to be image-only (scanned). No text found.")
                return None, None

            raw_json_string = _ask_groq_with_text(full_text, document_type)

        elif extension == 'docx':
            print(f"  → Extracting text from DOCX: {filename}")
            document = docx.Document(file_path)
            text = '\n'.join([para.text for para in document.paragraphs])
            raw_json_string = _ask_groq_with_text(text, document_type)

        else:
            print(f"  ⚠️  Unsupported file type: {extension}")
            return None, None

        data = json.loads(raw_json_string)
        driver_name = data.get('driver_name')
        expiration_date = data.get('expiration_date')

        print(f"  ✅ Groq extracted — Name: {driver_name}, Expiry: {expiration_date}")
        return driver_name, expiration_date

    except Exception as e:
        print(f"  ❌ AI extraction failed for {filename}: {e}")
        return None, None


# ==============================================================
# HELPERS
# ==============================================================

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def make_unique_filename(original_filename):
    """
    Builds a safe, collision-proof filename for saving to disk.

    Why: two different clients could both upload "license.pdf". If we
    just used secure_filename() on its own, the second upload would
    silently overwrite the first one on disk. Stamping a short random
    prefix on the front guarantees every saved file is unique, while
    keeping the original name (and extension) readable for humans.
    """
    safe_name = secure_filename(original_filename)
    unique_prefix = uuid.uuid4().hex[:12]
    return f"{unique_prefix}_{safe_name}"


def get_expiry_status(expiration_date_str):
    """
    Given a date string (YYYY-MM-DD), returns a status label.

    'expired'  → date is in the past
    'expiring' → date is within the next 30 days
    'valid'    → date is more than 30 days away
    'unknown'  → no date provided or unparseable
    """
    if not expiration_date_str:
        return 'unknown'
    try:
        exp_date = datetime.strptime(expiration_date_str, '%Y-%m-%d').date()
        today = date.today()
        if exp_date < today:
            return 'expired'
        elif exp_date <= today + timedelta(days=30):
            return 'expiring'
        else:
            return 'valid'
    except ValueError:
        return 'unknown'


def build_chart_data(clients_data):
    """
    Builds the analytics data dictionaries needed for Chart.js.
    Returns a dict with compliance status counts and document type counts.

    This function loops through all uploads we already fetched for the
    admin dashboard — so we don't need an extra database query.
    """
    # Compliance Status Pie Chart data
    status_counts = {'Valid': 0, 'Expiring Soon': 0, 'Expired': 0, 'Unknown': 0}

    # Document Type Bar Chart data — start with known types, add 'Other' as catchall
    doc_type_counts = {dt: 0 for dt in DOCUMENT_TYPES}

    for item in clients_data:
        for row in item['uploads']:
            # Count compliance status
            status = row['status']
            if status == 'valid':
                status_counts['Valid'] += 1
            elif status == 'expiring':
                status_counts['Expiring Soon'] += 1
            elif status == 'expired':
                status_counts['Expired'] += 1
            else:
                status_counts['Unknown'] += 1

            # Count document type
            doc_type = row['upload']['document_type']
            if doc_type and doc_type in doc_type_counts:
                doc_type_counts[doc_type] += 1
            elif doc_type:
                # Handle any type not in our list
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


# ==============================================================
# PUBLIC LEGAL PAGES (Privacy Policy / Terms of Service)
# Required for Twilio Toll-Free Verification. These are public,
# unauthenticated routes — no @login_required here on purpose,
# since Twilio's reviewers and end users need to be able to view
# them without an admin account.
# ==============================================================

@app.route('/privacy')
def privacy_policy():
    """Public Privacy Policy page."""
    return render_template('privacy.html')


@app.route('/tos')
def terms_of_service():
    """Public Terms of Service page."""
    return render_template('tos.html')


@app.route('/admin', methods=['GET', 'POST'])
@login_required
def admin():
    """
    Main admin dashboard.
    GET:  Show all clients with their uploaded documents, stats, and charts.
    POST: Create a new client and generate their magic upload link.
    """
    magic_link = None
    error = None
    db = get_db()

    if request.method == 'POST':
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
            magic_link = url_for('upload_file', client_uuid=client_uuid, _external=True)

    clients = db.execute('SELECT * FROM clients ORDER BY company_name').fetchall()

    total_docs = 0
    expiring_soon_count = 0
    expired_count = 0
    clients_data = []

    for client in clients:
        uploads = db.execute(
            '''SELECT * FROM uploads
               WHERE client_uuid = ?
               ORDER BY uploaded_at DESC''',
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

    # Build data for the Chart.js analytics dashboard
    chart_data = build_chart_data(clients_data)

    return render_template(
        'admin.html',
        magic_link=magic_link,
        clients_data=clients_data,
        error=error,
        stats=stats,
        chart_data=chart_data,
        admin_username=session.get('admin_username', 'Admin'),
    )


@app.route('/admin/delete/<client_uuid>', methods=['POST'])
@login_required
def delete_client(client_uuid):
    """Deletes a client and all their uploaded files."""
    db = get_db()

    uploads = db.execute(
        'SELECT filename FROM uploads WHERE client_uuid = ?', (client_uuid,)
    ).fetchall()

    for upload in uploads:
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], upload['filename'])
        if os.path.exists(file_path):
            os.remove(file_path)
            print(f"  🗑️  Deleted file: {upload['filename']}")

    db.execute('DELETE FROM uploads WHERE client_uuid = ?', (client_uuid,))
    db.execute('DELETE FROM clients WHERE uuid = ?', (client_uuid,))
    db.commit()
    print(f"  ✅ Client {client_uuid} deleted.")

    return redirect(url_for('admin'))


@app.route('/upload/<client_uuid>', methods=['GET'])
def upload_file(client_uuid):
    """
    The client's magic link page (GET only).

    All the actual work — saving the file, running the AI, and writing
    to the database — happens in the JSON API routes below (/scan,
    /confirm, /preview, /discard). This route's only job is to render
    the page shell that the JavaScript then drives with fetch().
    """
    db = get_db()

    client = db.execute(
        'SELECT * FROM clients WHERE uuid = ?', (client_uuid,)
    ).fetchone()

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


@app.route('/upload/<client_uuid>/scan', methods=['POST'])
def scan_document(client_uuid):
    """
    STEP 1 of the two-step flow (AJAX, called via fetch from upload.html).

    What it does:
      1. Validates the client link, document type, and file.
      2. Saves the file to disk under a unique name (so it survives
         between this request and the /confirm request the user makes
         later — this is the "don't lose the file" piece).
      3. Runs Groq extraction on that saved file.
      4. Returns the extracted data as JSON. Nothing is written to the
         database yet — that only happens if the user confirms.

    The frontend uses the returned `filename` as a receipt: it hands
    that exact string back to /confirm (and uses it to build the
    /preview URL for the split-screen document viewer) so the server
    knows which file on disk this review session belongs to.
    """
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

    # Save the file to disk FIRST, before calling the AI. This is what
    # keeps the file alive across requests: /confirm never receives the
    # raw file again, only this filename, so the file has to already be
    # sitting on disk by the time we respond here.
    saved_filename = make_unique_filename(file.filename)
    save_path = os.path.join(app.config['UPLOAD_FOLDER'], saved_filename)
    file.save(save_path)

    print(f"\n🤖 Starting Groq extraction for: {saved_filename} (Type: {document_type})")
    driver_name, expiration_date = extract_document_data(
        save_path, saved_filename, document_type=document_type
    )

    extraction_ok = bool(driver_name and expiration_date)

    # Tell the frontend how (or whether) it can preview this file so
    # it knows whether to build an <iframe> or an <img>, or show a
    # "no preview available" message (e.g. for .docx).
    extension = saved_filename.rsplit('.', 1)[1].lower()
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
    """
    Serves a temporarily-scanned file so the Step 2 split-screen viewer
    can display it in an <iframe> (PDFs) or <img> (images) while the
    user checks the AI's work.

    This intentionally does NOT require admin login — the client is
    reviewing their own just-uploaded file using the same magic link
    they used to reach this page, before it's even in the database.
    Like the rest of the magic-link flow, the security here comes from
    the filename being an unguessable random string (see
    make_unique_filename) rather than from a login wall.
    """
    client = get_db().execute('SELECT 1 FROM clients WHERE uuid = ?', (client_uuid,)).fetchone()
    if client is None:
        return '', 404

    # Strip any directory info so a crafted filename can't escape the
    # uploads folder (e.g. "../../secrets.db").
    safe_filename = os.path.basename(filename)
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], safe_filename)
    if not os.path.isfile(file_path):
        return '', 404

    # as_attachment=False (the default) so the browser renders the file
    # inline instead of downloading it — required for the <iframe> to
    # actually show the PDF instead of triggering a download prompt.
    return send_from_directory(app.config['UPLOAD_FOLDER'], safe_filename)


@app.route('/upload/<client_uuid>/discard/<filename>', methods=['POST'])
def discard_scan(client_uuid, filename):
    """
    Housekeeping route: fired when the user clicks "Start Over" during
    Step 2 instead of confirming. Deletes the temp file /scan already
    wrote to disk so we don't accumulate orphaned scans that never got
    saved to the database. Safe to call more than once — it's a no-op
    if the file is already gone.
    """
    safe_filename = os.path.basename(filename)
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], safe_filename)
    try:
        if os.path.isfile(file_path):
            os.remove(file_path)
            print(f"  🗑️  Discarded unsaved scan: {safe_filename}")
    except OSError as e:
        print(f"  ⚠️  Could not discard {safe_filename}: {e}")
    return jsonify(success=True)


@app.route('/upload/<client_uuid>/confirm', methods=['POST'])
def confirm_document(client_uuid):
    """
    STEP 2 of the two-step flow (AJAX, called after the user reviews
    and possibly edits the AI's extracted data).

    Takes the filename that /scan already saved to disk (never a fresh
    file) plus the doc type / driver name / expiration date — which may
    have been hand-corrected by the user — and this is the point where
    the record actually gets written to the database.
    """
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

    # Security check: strip any path info and make sure the file we're
    # about to record actually exists in the uploads folder. This stops
    # someone from POSTing an arbitrary filename/path straight into the
    # database.
    safe_filename = os.path.basename(filename)
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], safe_filename)
    if not os.path.isfile(file_path):
        return jsonify(success=False, error='That file was not found on the server — please scan again.'), 400

    db.execute(
        '''INSERT INTO uploads
           (client_uuid, filename, document_type, driver_name, expiration_date)
           VALUES (?, ?, ?, ?, ?)''',
        (client_uuid, safe_filename, document_type, driver_name or None, expiration_date or None)
    )
    db.commit()

    print(f"  ✅ Saved to database — {safe_filename} for client {client['company_name']}.")

    return jsonify(
        success=True,
        message=f'"{safe_filename}" has been saved for {client["company_name"]}.'
    )


# ==============================================================
# TWILIO SMS WEBHOOK (Toll-Free Verification: HELP / STOP handling)
# ==============================================================

@app.route('/webhook/sms', methods=['POST'])
def sms_webhook():
    """
    Twilio calls this URL every time someone replies to your toll-free
    number. Required by Twilio's Toll-Free Verification for any number
    that sends SMS alerts.

    - "HELP"                  → sends back support contact info.
    - "STOP" / "UNSUBSCRIBE"  → sends back an opt-out confirmation.
      (Twilio itself blocks future sends to that number automatically;
      this just sends the required confirmation text.)
    - Anything else           → no reply (empty TwiML response).
    """
    incoming_msg = request.values.get('Body', '').strip().upper()

    response = MessagingResponse()

    if incoming_msg == 'HELP':
        response.message(
            "Fleet Safety Alerts: Need help? Email tryfleetsafety@gmail.com "
            "or visit our site. Reply STOP to cancel."
        )
    elif incoming_msg in ('STOP', 'UNSUBSCRIBE'):
        response.message(
            "You have been unsubscribed from Fleet Safety Alerts. "
            "You will not receive any more messages."
        )

    return str(response)


# --- Start the Server ---
if __name__ == '__main__':
    app.run(debug=True)
