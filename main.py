import os
import hashlib
from fastapi import FastAPI, Request, Form, Depends, HTTPException, status, UploadFile, File, Cookie
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
import sqlite3
import pandas as pd
import io
import secrets
from datetime import datetime, date
import calendar
from typing import Optional
from urllib.parse import quote

app = FastAPI()
templates = Jinja2Templates(directory="templates")

DB_FILE = "database.db"
SESSIONS: dict[str, str] = {}
CAPTCHAS: dict[str, str] = {}
RESET_TOKENS: dict[str, str] = {}
USERS = {
    "saksorn@rdthailand.com": "@Aunt0107",
    "adminit": "admin123",
}

ROLE_ADMIN = "Admin"
ROLE_EDITOR = "Editor"
ROLE_VIEWER = "Viewer"
ROLES = [ROLE_EDITOR, ROLE_VIEWER]
PO_STATUSES = [
    "ได้รับ Quotation",
    "รอ Approve Quotation",
    "อยู่ในระหว่างทำ PO",
    "รอ Approve PO",
    "ดำเนินการส่ง PO",
    "ดำเนินการทำรับ PO แล้ว",
]
DEFAULT_PO_STATUS = PO_STATUSES[0]
PLACEHOLDER_PO_NOS = {"173XXXXX", "175XXXXX"}

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120000).hex()
    return f"{salt}${digest}"

def verify_password(password: str, stored_hash: str) -> bool:
    if not stored_hash:
        return False
    if "$" not in stored_hash:
        return secrets.compare_digest(password, stored_hash)
    salt, digest = stored_hash.split("$", 1)
    check = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120000).hex()
    return secrets.compare_digest(check, digest)

def flash_redirect(url: str, message: str = "", error: str = ""):
    params = []
    if message:
        params.append("message=" + quote(message))
    if error:
        params.append("error=" + quote(error))
    suffix = ("?" + "&".join(params)) if params else ""
    return RedirectResponse(url=url + suffix, status_code=status.HTTP_303_SEE_OTHER)

def get_setting(key: str, default: str = "") -> str:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM app_settings WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    return row["value"] if row else default

def set_setting(key: str, value: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()

def make_captcha() -> tuple[str, str]:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    answer = "".join(secrets.choice(alphabet) for _ in range(5))
    token = secrets.token_urlsafe(16)
    CAPTCHAS[token] = answer
    return token, answer

def verify_captcha(token: str, answer: str) -> bool:
    expected = CAPTCHAS.pop(token, "")
    return bool(expected) and expected.upper() == answer.strip().upper()

def is_real_po_no(po_no: str) -> bool:
    clean_po = str(po_no or "").strip()
    return bool(clean_po) and clean_po.upper() not in PLACEHOLDER_PO_NOS

def quotation_has_real_po(cursor, quotation_no: str) -> bool:
    cursor.execute("SELECT po_no FROM po_covers WHERE quotation_no = ?", (str(quotation_no).strip(),))
    return any(is_real_po_no(r["po_no"]) for r in cursor.fetchall())

def quotation_exists(cursor, table_name: str, quotation_no: str) -> bool:
    cursor.execute(f"SELECT 1 FROM {table_name} WHERE quotation_no = ? LIMIT 1", (str(quotation_no).strip(),))
    return cursor.fetchone() is not None

def duplicate_quotation_set(cursor, table_name: str) -> set[str]:
    cursor.execute(f"""
        SELECT quotation_no
        FROM {table_name}
        WHERE quotation_no IS NOT NULL AND TRIM(quotation_no) != ''
        GROUP BY quotation_no
        HAVING COUNT(*) > 1
    """)
    return {r["quotation_no"] for r in cursor.fetchall()}

def export_counts(cursor, export_type: str) -> dict[str, int]:
    cursor.execute("""
        SELECT quotation_no, COUNT(*) AS total
        FROM export_logs
        WHERE export_type = ?
        GROUP BY quotation_no
    """, (export_type,))
    return {r["quotation_no"]: r["total"] for r in cursor.fetchall()}

def log_export_quotes(cursor, export_type: str, quotation_numbers):
    exported_at = datetime.now().isoformat(timespec="seconds")
    for quotation_no in sorted({str(q).strip() for q in quotation_numbers if str(q).strip()}):
        cursor.execute(
            "INSERT INTO export_logs (export_type, quotation_no, exported_at) VALUES (?, ?, ?)",
            (export_type, quotation_no, exported_at)
        )

def import_error_message(errors: list[tuple[str, str]]) -> str:
    return "Import Error: " + " | ".join([f"Quotation {quote or '-'}: {reason}" for quote, reason in errors[:20]])

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    
    # 1. ตารางหลักสำหรับ Dashboard
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS po_covers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        vendor_code TEXT, vendor_name TEXT, sq_date TEXT, quotation_no TEXT,
        sq_line INTEGER, po_no TEXT, cost_center_id TEXT, cost_center_id2 TEXT,
        cost_center_name TEXT, description TEXT, tax_code TEXT, quantity REAL,
        uom TEXT DEFAULT 'EA', price REAL, total REAL, vat REAL, price_vat TEXT, po_status TEXT,
        created_at TEXT
    )
    """)
    existing_po_cols = [r["name"] for r in cursor.execute("PRAGMA table_info(po_covers)").fetchall()]
    if "created_at" not in existing_po_cols:
        cursor.execute("ALTER TABLE po_covers ADD COLUMN created_at TEXT")
        cursor.execute("UPDATE po_covers SET created_at = COALESCE(NULLIF(sq_date, ''), date('now')) WHERE created_at IS NULL OR created_at = ''")
    status_migration = {
        "Receive Quotation": "ได้รับ Quotation",
        "Wait Approved SQ": "รอ Approve Quotation",
        "Wait PR": "อยู่ในระหว่างทำ PO",
        "Wait PO": "อยู่ในระหว่างทำ PO",
        "PO in Progress": "รอ Approve PO",
        "Goods Receive": "ดำเนินการทำรับ PO แล้ว",
    }
    for old_status, new_status in status_migration.items():
        cursor.execute("UPDATE po_covers SET po_status = ? WHERE po_status = ?", (new_status, old_status))
    cursor.execute("""
    CREATE TRIGGER IF NOT EXISTS trg_po_covers_created_at
    AFTER INSERT ON po_covers
    WHEN NEW.created_at IS NULL OR NEW.created_at = ''
    BEGIN
        UPDATE po_covers SET created_at = date('now') WHERE id = NEW.id;
    END
    """)
    
    # 2. ตาราง Cost Center
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS cost_centers (
        cost_center_id TEXT PRIMARY KEY, cost_center_id2 TEXT, cost_center_name TEXT
    )
    """)
    
    # 3. ตาราง Vendor
    cursor.execute("CREATE TABLE IF NOT EXISTS vendors (vendor_code TEXT PRIMARY KEY, vendor_name TEXT)")
    
    # 4. ตาราง Material
    cursor.execute("CREATE TABLE IF NOT EXISTS materials (material_code TEXT PRIMARY KEY, material_name TEXT)")

    # 5. ตาราง PO Service
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS po_services (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pur_org TEXT, vendor_code TEXT, vendor_name TEXT, short_text TEXT,
        cost_center_id TEXT, cost_center_id2 TEXT, quantity REAL, tax_code TEXT,
        gross_price REAL, gl_account TEXT, quotation_no TEXT, created_at TEXT
    )
    """)

    # 6. ตาราง PO Asset
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS po_assets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        vendor_code TEXT, vendor_name TEXT, material_code TEXT, material_name TEXT,
        quantity REAL, tax_code TEXT, price_ztax REAL, detail TEXT, project TEXT,
        quotation_no TEXT, cost_center_id TEXT, created_at TEXT
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        email TEXT PRIMARY KEY,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'Viewer',
        is_active INTEGER NOT NULL DEFAULT 0,
        is_admin INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS app_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS export_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        export_type TEXT NOT NULL,
        quotation_no TEXT NOT NULL,
        exported_at TEXT NOT NULL
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS equipment_transfers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        po_cover_id INTEGER,
        quotation_no TEXT,
        equipment_type TEXT,
        source_location TEXT NOT NULL,
        target_cost_center_id TEXT,
        target_cost_center_id2 TEXT,
        target_cost_center_name TEXT,
        item_name TEXT NOT NULL,
        asset_no TEXT,
        serial_no TEXT,
        quantity REAL NOT NULL DEFAULT 1,
        transfer_status TEXT NOT NULL DEFAULT 'Plan',
        note TEXT,
        created_at TEXT NOT NULL
    )
    """)
    existing_transfer_cols = [r["name"] for r in cursor.execute("PRAGMA table_info(equipment_transfers)").fetchall()]
    for col_name, col_type in [("po_cover_id", "INTEGER"), ("quotation_no", "TEXT"), ("equipment_type", "TEXT")]:
        if col_name not in existing_transfer_cols:
            cursor.execute(f"ALTER TABLE equipment_transfers ADD COLUMN {col_name} {col_type}")
    cursor.execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES ('signup_enabled', '1')")

    for username, password in USERS.items():
        is_admin = 1
        cursor.execute("""
            INSERT OR IGNORE INTO users (email, password_hash, role, is_active, is_admin, created_at)
            VALUES (?, ?, ?, 1, ?, ?)
        """, (username, hash_password(password), ROLE_ADMIN, is_admin, date.today().isoformat()))
    
    conn.commit()
    conn.close()

init_db()

def get_user_by_email(email: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE email = ?", (email,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def get_current_user(session_id: Optional[str] = Cookie(None)) -> dict:
    if not session_id or session_id not in SESSIONS:
        raise HTTPException(status_code=303, detail="Not authenticated")
    user = get_user_by_email(SESSIONS[session_id])
    if not user or not user["is_active"]:
        raise HTTPException(status_code=303, detail="Not authenticated")
    return user

def ensure_editor(session_id: Optional[str]):
    user = get_current_user(session_id)
    if not user["is_admin"] and user["role"] != ROLE_EDITOR:
        raise HTTPException(status_code=403, detail="Editor permission required")
    return user

def ensure_admin(session_id: Optional[str]):
    user = get_current_user(session_id)
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Admin permission required")
    return user

def compute_po_type(po_no: str) -> str:
    if not po_no: return "-"
    clean_po = str(po_no).strip()
    if clean_po.startswith("173"): return "Po Service"
    elif clean_po.startswith("175"): return "Po Asset"
    return "Other PO"

@app.get("/")
async def root(session_id: Optional[str] = Cookie(None)):
    if session_id in SESSIONS:
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = "", message: str = ""):
    return templates.TemplateResponse(request=request, name="login.html", context={"error": error, "message": message})

@app.post("/login")
async def do_login(request: Request, username: str = Form(...), password: str = Form(...)):
    user = get_user_by_email(username.strip())
    if user and user["is_active"] and verify_password(password, user["password_hash"]):
        token = secrets.token_hex(16)
        SESSIONS[token] = user["email"]
        response = RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie(key="session_id", value=token, httponly=True)
        return response
    return templates.TemplateResponse(request=request, name="login.html", context={"error": "Username หรือ Password ไม่ถูกต้อง หรือบัญชียังไม่ถูกเปิดใช้งาน", "message": ""})

@app.get("/logout")
async def logout(session_id: Optional[str] = Cookie(None)):
    if session_id in SESSIONS: del SESSIONS[session_id]
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie("session_id")
    return response

@app.get("/captcha/{purpose}")
async def captcha_image(purpose: str, token: str):
    answer = CAPTCHAS.get(token, "-----")
    svg = f"""
    <svg xmlns="http://www.w3.org/2000/svg" width="180" height="56" viewBox="0 0 180 56">
        <rect width="180" height="56" rx="10" fill="#e2e8f0"/>
        <path d="M10 42 C45 8, 76 58, 115 18 S155 45, 170 20" fill="none" stroke="#64748b" stroke-width="2"/>
        <text x="90" y="36" text-anchor="middle" font-family="Consolas, monospace" font-size="28" font-weight="700" letter-spacing="5" fill="#0f172a">{answer}</text>
    </svg>
    """
    return HTMLResponse(content=svg, media_type="image/svg+xml")

@app.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request, error: str = "", message: str = ""):
    token, _ = make_captcha()
    return templates.TemplateResponse(request=request, name="signup.html", context={
        "captcha_token": token,
        "signup_enabled": get_setting("signup_enabled", "1") == "1",
        "error": error,
        "message": message,
    })

@app.post("/signup")
async def signup_submit(request: Request, email: str = Form(...), password: str = Form(...), confirm_password: str = Form(...), captcha_token: str = Form(...), captcha_answer: str = Form(...)):
    if get_setting("signup_enabled", "1") != "1":
        return flash_redirect("/signup", error="Admin ปิดการสมัคร User ใหม่ชั่วคราว")
    email = email.strip().lower()
    if password != confirm_password:
        return flash_redirect("/signup", error="Password และ Confirm Password ไม่ตรงกัน")
    if len(password) < 6:
        return flash_redirect("/signup", error="Password ต้องมีอย่างน้อย 6 ตัวอักษร")
    if not verify_captcha(captcha_token, captcha_answer):
        return flash_redirect("/signup", error="รหัสยืนยันจากรูปภาพไม่ถูกต้อง")
    if get_user_by_email(email):
        return flash_redirect("/signup", error="Email นี้มีอยู่ในระบบแล้ว")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO users (email, password_hash, role, is_active, is_admin, created_at)
        VALUES (?, ?, ?, 0, 0, ?)
    """, (email, hash_password(password), ROLE_VIEWER, date.today().isoformat()))
    conn.commit()
    conn.close()
    return flash_redirect("/login", message="สมัครเรียบร้อยแล้ว กรุณารอ Admin กำหนดสิทธิและเปิดใช้งาน")

@app.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page(request: Request, error: str = "", message: str = ""):
    token, _ = make_captcha()
    return templates.TemplateResponse(request=request, name="forgot_password.html", context={"captcha_token": token, "error": error, "message": message})

@app.post("/forgot-password")
async def forgot_password_submit(email: str = Form(...), captcha_token: str = Form(...), captcha_answer: str = Form(...)):
    email = email.strip().lower()
    if not verify_captcha(captcha_token, captcha_answer):
        return flash_redirect("/forgot-password", error="รหัสยืนยันจากรูปภาพไม่ถูกต้อง")
    user = get_user_by_email(email)
    if not user:
        return flash_redirect("/forgot-password", error="ไม่พบ Email นี้ในระบบ")
    token = secrets.token_urlsafe(24)
    RESET_TOKENS[token] = email
    return RedirectResponse(url=f"/reset-password?token={token}", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/reset-password", response_class=HTMLResponse)
async def reset_password_page(request: Request, token: str, error: str = ""):
    if token not in RESET_TOKENS:
        return flash_redirect("/forgot-password", error="Reset link หมดอายุหรือไม่ถูกต้อง")
    return templates.TemplateResponse(request=request, name="reset_password.html", context={"token": token, "error": error})

@app.post("/reset-password")
async def reset_password_submit(token: str = Form(...), password: str = Form(...), confirm_password: str = Form(...)):
    if token not in RESET_TOKENS:
        return flash_redirect("/forgot-password", error="Reset link หมดอายุหรือไม่ถูกต้อง")
    if password != confirm_password:
        return flash_redirect(f"/reset-password?token={token}", error="Password และ Confirm Password ไม่ตรงกัน")
    if len(password) < 6:
        return flash_redirect(f"/reset-password?token={token}", error="Password ต้องมีอย่างน้อย 6 ตัวอักษร")
    email = RESET_TOKENS.pop(token)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET password_hash = ? WHERE email = ?", (hash_password(password), email))
    conn.commit()
    conn.close()
    return flash_redirect("/login", message="ตั้ง Password ใหม่เรียบร้อยแล้ว")

@app.get("/admin/users", response_class=HTMLResponse)
async def admin_users_page(request: Request, session_id: Optional[str] = Cookie(None)):
    try: current_user = ensure_admin(session_id)
    except HTTPException: return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users ORDER BY is_admin DESC, created_at DESC, email ASC")
    users = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return templates.TemplateResponse(request=request, name="admin_users.html", context={
        "users": users,
        "roles": ROLES,
        "signup_enabled": get_setting("signup_enabled", "1") == "1",
        "current_user": current_user,
    })

@app.post("/admin/users/update")
async def admin_user_update(email: str = Form(...), role: str = Form(...), is_active: Optional[str] = Form(None), session_id: Optional[str] = Cookie(None)):
    ensure_admin(session_id)
    user = get_user_by_email(email)
    if not user or user["is_admin"]:
        return RedirectResponse(url="/admin/users", status_code=status.HTTP_303_SEE_OTHER)
    role = role if role in ROLES else ROLE_VIEWER
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET role = ?, is_active = ? WHERE email = ?", (role, 1 if is_active == "on" else 0, email))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/admin/users", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/signup-toggle")
async def admin_signup_toggle(signup_enabled: Optional[str] = Form(None), session_id: Optional[str] = Cookie(None)):
    ensure_admin(session_id)
    set_setting("signup_enabled", "1" if signup_enabled == "on" else "0")
    return RedirectResponse(url="/admin/users", status_code=status.HTTP_303_SEE_OTHER)

# DASHBOARD MAIN
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request, search: Optional[str] = None, status_filter: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None, page: int = 1, session_id: Optional[str] = Cookie(None)):
    try: user = get_current_user(session_id)
    except HTTPException: return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("SELECT * FROM vendors ORDER BY vendor_name ASC")
    vendors = [dict(r) for r in cursor.fetchall()]
    
    cursor.execute("SELECT * FROM cost_centers ORDER BY cost_center_id ASC")
    cost_centers = [dict(r) for r in cursor.fetchall()]

    where_query = " FROM po_covers WHERE 1=1"
    params = []
    if search and search.strip():
        q = f"%{search.strip()}%"
        where_query += " AND (quotation_no LIKE ? OR po_no LIKE ? OR vendor_name LIKE ? OR cost_center_name LIKE ? OR cost_center_id LIKE ?)"
        params.extend([q, q, q, q, q])
        
    if status_filter and status_filter.strip():
        where_query += " AND po_status = ?"
        params.append(status_filter.strip())

    if date_from and date_from.strip():
        where_query += " AND date(COALESCE(NULLIF(created_at, ''), NULLIF(sq_date, ''))) >= date(?)"
        params.append(date_from.strip())

    if date_to and date_to.strip():
        where_query += " AND date(COALESCE(NULLIF(created_at, ''), NULLIF(sq_date, ''))) <= date(?)"
        params.append(date_to.strip())

    cursor.execute("SELECT COUNT(*) AS total" + where_query, params)
    total_records = cursor.fetchone()["total"]
    page_size = 25
    total_pages = max(1, (total_records + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))
    offset = (page - 1) * page_size

    query = "SELECT *" + where_query + " ORDER BY date(COALESCE(NULLIF(created_at, ''), NULLIF(sq_date, ''))) DESC, vendor_name ASC, quotation_no ASC LIMIT ? OFFSET ?"
    cursor.execute(query, [*params, page_size, offset])
    jobs = []
    for r in cursor.fetchall():
        d = dict(r)
        d['po_type'] = compute_po_type(d.get('po_no'))
        jobs.append(d)

    cursor.execute("SELECT po_status, COUNT(*) AS total FROM po_covers GROUP BY po_status ORDER BY total DESC")
    status_counts = [dict(r) for r in cursor.fetchall()]

    conn.close()
    return templates.TemplateResponse(request=request, name="dashboard.html", context={
        "vendors": vendors, "cost_centers": cost_centers, "jobs": jobs,
        "status_counts": status_counts,
        "search": search or "", "status_filter": status_filter or "", "date_from": date_from or "", "date_to": date_to or "",
        "page": page, "page_size": page_size, "total_pages": total_pages, "total_records": total_records,
        "current_user": user,
        "can_edit": user["is_admin"] or user["role"] == ROLE_EDITOR,
        "po_statuses": PO_STATUSES,
    })

@app.post("/dashboard/update-line")
async def dashboard_update_line(job_id: int = Form(...), po_no: str = Form(""), po_status: str = Form(...), session_id: Optional[str] = Cookie(None)):
    ensure_editor(session_id)
    po_status = po_status if po_status in PO_STATUSES else DEFAULT_PO_STATUS
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT po_no, quotation_no FROM po_covers WHERE id = ?", (job_id,))
    row = cursor.fetchone()
    clean_po = po_no.strip()
    if row and not clean_po and str(row["po_no"] or "").strip().upper() in PLACEHOLDER_PO_NOS:
        clean_po = row["po_no"]
    if row:
        cursor.execute("UPDATE po_covers SET po_no = ?, po_status = ? WHERE quotation_no = ?", (clean_po, po_status, row["quotation_no"]))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/dashboard/bulk-status")
async def dashboard_bulk_status(selected_ids: Optional[list[int]] = Form(None), po_status: str = Form(...), session_id: Optional[str] = Cookie(None)):
    ensure_editor(session_id)
    po_status = po_status if po_status in PO_STATUSES else DEFAULT_PO_STATUS
    if not selected_ids:
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    placeholders = ",".join("?" for _ in selected_ids)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(f"UPDATE po_covers SET po_status = ? WHERE id IN ({placeholders})", [po_status, *selected_ids])
    conn.commit()
    conn.close()
    return RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/key-po-number", response_class=HTMLResponse)
async def key_po_number_page(request: Request, error: str = "", message: str = "", session_id: Optional[str] = Cookie(None)):
    try: current_user = ensure_editor(session_id)
    except HTTPException: return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT
            quotation_no,
            MAX(vendor_name) AS vendor_name,
            MAX(po_no) AS po_no,
            GROUP_CONCAT(po_no, '|') AS po_numbers,
            COUNT(*) AS line_count,
            MAX(created_at) AS created_at
        FROM po_covers
        WHERE quotation_no IS NOT NULL AND TRIM(quotation_no) != ''
        GROUP BY quotation_no
        ORDER BY date(COALESCE(NULLIF(MAX(created_at), ''), date('now'))) DESC, quotation_no ASC
    """)
    quotations = []
    for row in cursor.fetchall():
        d = dict(row)
        po_numbers = str(d.get("po_numbers") or "").split("|")
        if not any(is_real_po_no(po_no) for po_no in po_numbers):
            d["po_no"] = ""
            quotations.append(d)
    conn.close()
    return templates.TemplateResponse(request=request, name="key_po_number.html", context={"quotations": quotations, "current_user": current_user, "error": error, "message": message})

@app.post("/key-po-number/update")
async def key_po_number_update(quotation_no: str = Form(...), po_no: str = Form(...), session_id: Optional[str] = Cookie(None)):
    ensure_editor(session_id)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE po_covers SET po_no = ? WHERE quotation_no = ?", (po_no.strip(), quotation_no.strip()))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/key-po-number", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/key-po-number/batch-update")
async def key_po_number_batch_update(quotation_no: list[str] = Form(...), po_no: list[str] = Form(...), session_id: Optional[str] = Cookie(None)):
    ensure_editor(session_id)
    conn = get_db()
    cursor = conn.cursor()
    for quote, po in zip(quotation_no, po_no):
        clean_quote = quote.strip()
        clean_po = po.strip()
        if clean_quote and clean_po:
            cursor.execute("UPDATE po_covers SET po_no = ? WHERE quotation_no = ?", (clean_po, clean_quote))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/key-po-number", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/key-po-number/template")
async def key_po_number_template(session_id: Optional[str] = Cookie(None)):
    ensure_editor(session_id)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT quotation_no, MAX(vendor_name) AS vendor_name, MAX(po_no) AS po_no, GROUP_CONCAT(po_no, '|') AS po_numbers
        FROM po_covers
        WHERE quotation_no IS NOT NULL AND TRIM(quotation_no) != ''
        GROUP BY quotation_no
        ORDER BY quotation_no ASC
    """)
    data = []
    for r in cursor.fetchall():
        po_numbers = str(r["po_numbers"] or "").split("|")
        current_po = next((po for po in po_numbers if is_real_po_no(po)), "")
        data.append({"Quotation No": r["quotation_no"], "Vendor Name": r["vendor_name"], "PO No": current_po})
    conn.close()
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        pd.DataFrame(data).to_excel(writer, index=False, sheet_name="Key_PO_Number")
    output.seek(0)
    return StreamingResponse(output, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": "attachment; filename=Key_PO_Number_Template.xlsx"})

@app.post("/key-po-number/import")
async def key_po_number_import(file: UploadFile = File(...), session_id: Optional[str] = Cookie(None)):
    ensure_editor(session_id)
    contents = await file.read()
    df = pd.read_csv(io.BytesIO(contents)) if file.filename and file.filename.lower().endswith(".csv") else pd.read_excel(io.BytesIO(contents))
    conn = get_db()
    cursor = conn.cursor()
    errors = []
    updates = []
    for idx, row in df.iterrows():
        q_no = str(row.get("Quotation No", "")).strip()
        po_no = str(row.get("PO No", "")).strip()
        row_label = q_no if q_no and q_no.lower() != "nan" else f"Row {idx + 2}"
        if not q_no or q_no.lower() == "nan":
            errors.append((row_label, "ไม่พบเลข Quotation No"))
            continue
        cursor.execute("SELECT 1 FROM po_covers WHERE quotation_no = ? LIMIT 1", (q_no,))
        if not cursor.fetchone():
            errors.append((q_no, "ไม่พบ Quotation ในฐานข้อมูล PO Service/PO Asset"))
            continue
        if not po_no or po_no.lower() == "nan":
            errors.append((q_no, "ไม่พบเลข PO No"))
            continue
        updates.append((po_no, q_no))
    if errors:
        conn.close()
        return flash_redirect("/key-po-number", error=import_error_message(errors))
    for po_no, q_no in updates:
        cursor.execute("UPDATE po_covers SET po_no = ? WHERE quotation_no = ?", (po_no, q_no))
    conn.commit()
    conn.close()
    return flash_redirect("/key-po-number", message=f"Import PO Number สำเร็จ {len(updates)} Quotation")

@app.get("/key-po-number/edit", response_class=HTMLResponse)
async def key_po_number_edit_page(request: Request, session_id: Optional[str] = Cookie(None)):
    try: current_user = ensure_editor(session_id)
    except HTTPException: return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT
            quotation_no,
            MAX(vendor_name) AS vendor_name,
            MAX(po_no) AS po_no,
            GROUP_CONCAT(po_no, '|') AS po_numbers,
            COUNT(*) AS line_count,
            MAX(created_at) AS created_at
        FROM po_covers
        WHERE quotation_no IS NOT NULL AND TRIM(quotation_no) != ''
        GROUP BY quotation_no
        ORDER BY date(COALESCE(NULLIF(MAX(created_at), ''), date('now'))) DESC, quotation_no ASC
    """)
    quotations = []
    for row in cursor.fetchall():
        d = dict(row)
        po_numbers = [po for po in str(d.get("po_numbers") or "").split("|") if is_real_po_no(po)]
        if po_numbers:
            d["po_no"] = po_numbers[0]
            quotations.append(d)
    conn.close()
    return templates.TemplateResponse(request=request, name="key_po_number_edit.html", context={"quotations": quotations, "current_user": current_user})

@app.post("/key-po-number/edit-save")
async def key_po_number_edit_save(quotation_no: list[str] = Form(...), po_no: list[str] = Form(...), session_id: Optional[str] = Cookie(None)):
    ensure_editor(session_id)
    conn = get_db()
    cursor = conn.cursor()
    for quote, po in zip(quotation_no, po_no):
        clean_quote = quote.strip()
        if clean_quote:
            cursor.execute("UPDATE po_covers SET po_no = ? WHERE quotation_no = ?", (po.strip(), clean_quote))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/key-po-number", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/transfer-equipment", response_class=HTMLResponse)
async def transfer_equipment_page(request: Request, error: str = "", message: str = "", session_id: Optional[str] = Cookie(None)):
    try: current_user = ensure_editor(session_id)
    except HTTPException: return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM cost_centers ORDER BY cost_center_id ASC")
    cost_centers = [dict(r) for r in cursor.fetchall()]
    cursor.execute("SELECT * FROM equipment_transfers ORDER BY id DESC")
    transfers = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return templates.TemplateResponse(request=request, name="transfer_equipment.html", context={
        "cost_centers": cost_centers,
        "transfers": transfers,
        "current_user": current_user,
        "error": error,
        "message": message,
    })

@app.post("/transfer-equipment/add")
async def transfer_equipment_add(source_location: str = Form(...), target_cost_center_id: str = Form(...), item_name: str = Form(...), asset_no: str = Form(""), serial_no: str = Form(""), quantity: float = Form(1), note: str = Form(""), session_id: Optional[str] = Cookie(None)):
    ensure_editor(session_id)
    source_location = source_location if source_location in ["7590", "7593"] else "7590"
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT cost_center_id2, cost_center_name FROM cost_centers WHERE cost_center_id = ?", (target_cost_center_id,))
    cc = cursor.fetchone()
    target_id2 = cc["cost_center_id2"] if cc else ""
    target_name = cc["cost_center_name"] if cc else ""
    try:
        unit_count = max(1, int(float(quantity or 1)))
    except Exception:
        unit_count = 1
    for unit_no in range(1, unit_count + 1):
        row_item_name = item_name.strip()
        if unit_count > 1:
            row_item_name = f"{row_item_name} #{unit_no}"
        cursor.execute("""
            INSERT INTO equipment_transfers (source_location, target_cost_center_id, target_cost_center_id2, target_cost_center_name, item_name, asset_no, serial_no, quantity, transfer_status, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'Plan', ?, ?)
        """, (source_location, target_cost_center_id, target_id2, target_name, row_item_name, asset_no.strip(), serial_no.strip(), note.strip(), date.today().isoformat()))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/transfer-equipment", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/transfer-equipment/sync-from-po")
async def transfer_equipment_sync_from_po(session_id: Optional[str] = Cookie(None)):
    ensure_editor(session_id)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM po_covers
        WHERE cost_center_id IN ('7590', '7593')
           OR cost_center_id2 IN ('7590', '7593')
        ORDER BY id ASC
    """)
    inserted = 0
    for row in cursor.fetchall():
        cursor.execute("SELECT COUNT(*) AS total FROM equipment_transfers WHERE po_cover_id = ?", (row["id"],))
        if cursor.fetchone()["total"]:
            continue
        try:
            unit_count = max(1, int(float(row["quantity"] or 1)))
        except Exception:
            unit_count = 1
        source_location = row["cost_center_id"] if row["cost_center_id"] in ["7590", "7593"] else row["cost_center_id2"]
        for unit_no in range(1, unit_count + 1):
            note = f"From PO {row['po_no'] or '-'} / Quotation {row['quotation_no']}"
            item_name = (row["description"] or "Equipment")[:40]
            if unit_count > 1:
                item_name = f"{item_name} #{unit_no}"
            cursor.execute("""
                INSERT INTO equipment_transfers (po_cover_id, quotation_no, equipment_type, source_location, target_cost_center_id, target_cost_center_id2, target_cost_center_name, item_name, asset_no, serial_no, quantity, transfer_status, note, created_at)
                VALUES (?, ?, ?, ?, '', '', '', ?, '', '', 1, 'Plan', ?, ?)
            """, (row["id"], row["quotation_no"], row["description"], source_location, item_name, note, date.today().isoformat()))
            inserted += 1
    conn.commit()
    conn.close()
    return flash_redirect("/transfer-equipment", message=f"Sync จาก PO สำเร็จ เพิ่ม {inserted} รายการ")

@app.post("/transfer-equipment/update-status")
async def transfer_equipment_update_status(transfer_id: int = Form(...), transfer_status: str = Form(...), target_cost_center_id: str = Form(""), asset_no: str = Form(""), serial_no: str = Form(""), note: str = Form(""), session_id: Optional[str] = Cookie(None)):
    ensure_editor(session_id)
    if transfer_status not in ["Plan", "Packed", "Transferred", "Received", "Cancel"]:
        transfer_status = "Plan"
    conn = get_db()
    cursor = conn.cursor()
    target_id2 = ""
    target_name = ""
    if target_cost_center_id:
        cursor.execute("SELECT cost_center_id2, cost_center_name FROM cost_centers WHERE cost_center_id = ?", (target_cost_center_id,))
        cc = cursor.fetchone()
        target_id2 = cc["cost_center_id2"] if cc else ""
        target_name = cc["cost_center_name"] if cc else ""
    cursor.execute("""
        UPDATE equipment_transfers
        SET transfer_status = ?, target_cost_center_id = ?, target_cost_center_id2 = ?, target_cost_center_name = ?, asset_no = ?, serial_no = ?, note = ?
        WHERE id = ?
    """, (transfer_status, target_cost_center_id, target_id2, target_name, asset_no.strip(), serial_no.strip(), note.strip(), transfer_id))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/transfer-equipment", status_code=status.HTTP_303_SEE_OTHER)

# IMPORT EXCEL
@app.post("/import-excel")
async def import_excel(file: UploadFile = File(...), session_id: Optional[str] = Cookie(None)):
    ensure_editor(session_id)
    contents = await file.read()
    df = pd.read_excel(io.BytesIO(contents))
    
    conn = get_db()
    cursor = conn.cursor()
    
    for _, row in df.iterrows():
        v_code = str(row.get('Vendor Code', '')).strip()
        v_name = str(row.get('Vendor Name', '')).strip()
        q_no = str(row.get('Quotation No', '')).strip()
        cc_id = str(row.get('Cost Center ID', '')).strip()
        cc_name = str(row.get('Cost Center Name', '')).strip()
        
        if not q_no: continue
        
        cursor.execute("SELECT MAX(sq_line) as max_line FROM po_covers WHERE quotation_no = ?", (q_no,))
        res = cursor.fetchone()
        sq_line = 10 if (not res or res['max_line'] is None) else res['max_line'] + 10
        
        qty = float(row.get('Quantity', 1))
        price = float(row.get('Price', 0))
        total = qty * price
        vat = total * 0.07
        price_vat = total + vat
        
        cursor.execute("SELECT cost_center_id2 FROM cost_centers WHERE cost_center_id = ?", (cc_id,))
        cc_res = cursor.fetchone()
        cc_id2 = cc_res['cost_center_id2'] if cc_res else cc_id

        cursor.execute("""
            INSERT INTO po_covers (vendor_code, vendor_name, sq_date, quotation_no, sq_line, po_no, cost_center_id, cost_center_id2, cost_center_name, description, tax_code, quantity, price, total, vat, price_vat, po_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (v_code, v_name, str(row.get('SQ Date', '')), q_no, sq_line, str(row.get('PO No', '')), cc_id, cc_id2, cc_name, str(row.get('Description', ''))[:40], str(row.get('Tax Code', 'I2')), qty, price, total, vat, price_vat, str(row.get('Status', DEFAULT_PO_STATUS))))
        
    conn.commit()
    conn.close()
    return RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/download-template")
async def download_template():
    columns = ["Vendor Code", "Vendor Name", "Quotation No", "SQ Date", "PO No", "Cost Center ID", "Cost Center Name", "Description", "Quantity", "Price", "Tax Code", "Status"]
    df = pd.DataFrame(columns=columns)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Template')
    output.seek(0)
    return StreamingResponse(output, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": "attachment; filename=PO_Template.xlsx"})

# MANAGE VENDORS
@app.get("/master/vendors", response_class=HTMLResponse)
async def master_vendors_page(request: Request, session_id: Optional[str] = Cookie(None)):
    try: current_user = get_current_user(session_id)
    except HTTPException: return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM vendors ORDER BY vendor_name ASC")
    vendors = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return templates.TemplateResponse(request=request, name="master_vendors.html", context={"vendors": vendors, "current_user": current_user})

@app.post("/master/vendors/add")
async def master_add_vendor(vendor_code: str = Form(...), vendor_name: str = Form(...), session_id: Optional[str] = Cookie(None)):
    ensure_editor(session_id)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO vendors (vendor_code, vendor_name) VALUES (?, ?)", (vendor_code.strip(), vendor_name.strip()))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/master/vendors", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/vendors/bulk-paste")
async def master_bulk_vendors(paste_data: str = Form(...), session_id: Optional[str] = Cookie(None)):
    ensure_editor(session_id)
    conn = get_db()
    cursor = conn.cursor()
    for line in paste_data.strip().split('\n'):
        if not line.strip(): continue
        parts = line.split('\t') if '\t' in line else line.split(None, 1)
        if len(parts) >= 2:
            cursor.execute("INSERT OR REPLACE INTO vendors (vendor_code, vendor_name) VALUES (?, ?)", (parts[0].strip(), parts[1].strip()))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/master/vendors", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/vendors/import-excel")
async def import_vendors(file: UploadFile = File(...), session_id: Optional[str] = Cookie(None)):
    ensure_editor(session_id)
    contents = await file.read()
    df = pd.read_csv(io.BytesIO(contents)) if file.filename and file.filename.lower().endswith(".csv") else pd.read_excel(io.BytesIO(contents))
    conn = get_db()
    cursor = conn.cursor()
    for _, row in df.iterrows():
        code = str(row.iloc[0]).strip()
        name = str(row.iloc[1]).strip() if len(row) > 1 else ""
        if code and name and code.lower() != "nan" and name.lower() != "nan":
            cursor.execute("INSERT OR REPLACE INTO vendors (vendor_code, vendor_name) VALUES (?, ?)", (code, name))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/master/vendors", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/master/vendors/delete/{code}")
async def master_delete_vendor(code: str, session_id: Optional[str] = Cookie(None)):
    ensure_editor(session_id)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM vendors WHERE vendor_code = ?", (code,))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/master/vendors", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/vendors/delete/{code}")
async def master_delete_vendor_alias(code: str, session_id: Optional[str] = Cookie(None)):
    return await master_delete_vendor(code, session_id)

# MANAGE COST CENTERS
@app.get("/master/cost-centers", response_class=HTMLResponse)
async def master_cc_page(request: Request, session_id: Optional[str] = Cookie(None)):
    try: current_user = get_current_user(session_id)
    except HTTPException: return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM cost_centers ORDER BY cost_center_id ASC")
    ccs = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return templates.TemplateResponse(request=request, name="master_cost-centers.html", context={"cost_centers": ccs, "current_user": current_user})

@app.post("/master/cost-centers/add")
async def master_add_cc(cost_center_id: str = Form(...), cost_center_id2: str = Form(...), cost_center_name: str = Form(...), session_id: Optional[str] = Cookie(None)):
    ensure_editor(session_id)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO cost_centers (cost_center_id, cost_center_id2, cost_center_name) VALUES (?, ?, ?)", (cost_center_id.strip(), cost_center_id2.strip(), cost_center_name.strip()))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/master/cost-centers", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/cost-centers/bulk-paste")
async def master_bulk_cc(paste_data: str = Form(...), session_id: Optional[str] = Cookie(None)):
    ensure_editor(session_id)
    conn = get_db()
    cursor = conn.cursor()
    for line in paste_data.strip().split('\n'):
        if not line.strip(): continue
        parts = line.split('\t')
        if len(parts) >= 3:
            cursor.execute("INSERT OR REPLACE INTO cost_centers (cost_center_id, cost_center_id2, cost_center_name) VALUES (?, ?, ?)", (parts[0].strip(), parts[2].strip(), parts[1].strip()))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/master/cost-centers", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/cost-centers/import-excel")
async def import_cost_centers(file: UploadFile = File(...), session_id: Optional[str] = Cookie(None)):
    ensure_editor(session_id)
    contents = await file.read()
    df = pd.read_csv(io.BytesIO(contents)) if file.filename and file.filename.lower().endswith(".csv") else pd.read_excel(io.BytesIO(contents))
    conn = get_db()
    cursor = conn.cursor()
    for _, row in df.iterrows():
        cc1 = str(row.iloc[0]).strip()
        cc2 = str(row.iloc[1]).strip() if len(row) > 1 else ""
        name = str(row.iloc[2]).strip() if len(row) > 2 else ""
        if cc1 and cc2 and name and cc1.lower() != "nan":
            cursor.execute("INSERT OR REPLACE INTO cost_centers (cost_center_id, cost_center_id2, cost_center_name) VALUES (?, ?, ?)", (cc1, cc2, name))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/master/cost-centers", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/master/cost-centers/delete/{cc_id}")
async def master_delete_cc(cc_id: str, session_id: Optional[str] = Cookie(None)):
    ensure_editor(session_id)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM cost_centers WHERE cost_center_id = ?", (cc_id,))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/master/cost-centers", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/cost-centers/delete/{cc_id}")
async def master_delete_cc_alias(cc_id: str, session_id: Optional[str] = Cookie(None)):
    return await master_delete_cc(cc_id, session_id)


# =====================================================
# PO SERVICE SECTION (REVIEW & YYYYMMDD EXPORT)
# =====================================================
@app.get("/po-service", response_class=HTMLResponse)
async def po_service_page(request: Request, error: str = "", message: str = "", session_id: Optional[str] = Cookie(None)):
    try: user = get_current_user(session_id)
    except HTTPException: return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM vendors ORDER BY vendor_name ASC")
    v_list = [dict(r) for r in cursor.fetchall()]
    cursor.execute("SELECT * FROM cost_centers ORDER BY cost_center_id ASC")
    cc_list = [dict(r) for r in cursor.fetchall()]
    
    cursor.execute("SELECT * FROM po_services ORDER BY id DESC")
    saved_services = [dict(r) for r in cursor.fetchall()]
    duplicate_quotes = duplicate_quotation_set(cursor, "po_services")
    service_quotes = sorted({r["quotation_no"] for r in saved_services if r.get("quotation_no")})
    counts = export_counts(cursor, "service")
    conn.close()
    return templates.TemplateResponse(request=request, name="po_service.html", context={"vendors": v_list, "cost_centers": cc_list, "services": saved_services, "duplicate_quotes": duplicate_quotes, "existing_quotes": service_quotes, "export_counts": counts, "current_user": user, "can_edit": user["is_admin"] or user["role"] == ROLE_EDITOR, "error": error, "message": message})

@app.post("/po-service/add")
async def add_po_service(pur_org: str = Form(...), vendor_code: str = Form(...), vendor_name: str = Form(...), short_text: str = Form(...), cost_center_id: str = Form(...), quantity: float = Form(...), tax_code: str = Form(...), gross_price: float = Form(...), gl_account: str = Form(...), quotation_no: str = Form(...), confirm_duplicate: str = Form("0"), session_id: Optional[str] = Cookie(None)):
    ensure_editor(session_id)
    conn = get_db()
    cursor = conn.cursor()
    quotation_no = quotation_no.strip()
    if quotation_has_real_po(cursor, quotation_no):
        conn.close()
        return flash_redirect("/po-service", error=f"Quotation {quotation_no} มีเลข PO แล้ว ไม่อนุญาตให้คีย์ซ้ำ")
    if quotation_exists(cursor, "po_services", quotation_no) and confirm_duplicate != "1":
        conn.close()
        return flash_redirect("/po-service", error=f"Quotation {quotation_no} ซ้ำ กรุณายืนยันก่อนบันทึก")
    
    cursor.execute("SELECT cost_center_id2, cost_center_name FROM cost_centers WHERE cost_center_id = ?", (cost_center_id.strip(),))
    cc_res = cursor.fetchone()
    cc_id2 = cc_res['cost_center_id2'] if cc_res else cost_center_id
    cc_name = cc_res['cost_center_name'] if cc_res else "Service Branch"

    cursor.execute("""
        INSERT INTO po_services (pur_org, vendor_code, vendor_name, short_text, cost_center_id, cost_center_id2, quantity, tax_code, gross_price, gl_account, quotation_no, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (pur_org, vendor_code, vendor_name, short_text[:40], cost_center_id, cc_id2, quantity, tax_code, gross_price, gl_account, quotation_no, date.today().isoformat()))
    
    total = quantity * gross_price
    cursor.execute("SELECT MAX(sq_line) as max_line FROM po_covers WHERE quotation_no = ?", (quotation_no,))
    line_res = cursor.fetchone()
    sq_line = 10 if (not line_res or line_res['max_line'] is None) else line_res['max_line'] + 10

    cursor.execute("""
        INSERT INTO po_covers (vendor_code, vendor_name, sq_date, quotation_no, sq_line, po_no, cost_center_id, cost_center_id2, cost_center_name, description, tax_code, quantity, price, total, vat, price_vat, po_status)
        VALUES (?, ?, ?, ?, ?, '173XXXXX', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (vendor_code, vendor_name, date.today().isoformat(), quotation_no, sq_line, cost_center_id, cc_id2, cc_name, short_text[:40], tax_code, quantity, gross_price, total, total*0.07, total*1.07, DEFAULT_PO_STATUS))
    
    conn.commit()
    conn.close()
    return RedirectResponse(url="/po-service", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/po-service/delete/{service_id}")
async def delete_po_service(service_id: int, session_id: Optional[str] = Cookie(None)):
    ensure_editor(session_id)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM po_services WHERE id = ?", (service_id,))
    row = cursor.fetchone()
    if row:
        cursor.execute("DELETE FROM po_services WHERE id = ?", (service_id,))
        cursor.execute("""
            DELETE FROM po_covers
            WHERE id = (
                SELECT id FROM po_covers
                WHERE quotation_no = ? AND po_no LIKE '173%'
                ORDER BY id DESC
                LIMIT 1
            )
        """, (row["quotation_no"],))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/po-service", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/po-service/update")
async def update_po_service(id: int = Form(...), pur_org: str = Form(...), vendor_code: str = Form(...), vendor_name: str = Form(...), short_text: str = Form(...), cost_center_id: str = Form(...), quantity: float = Form(...), tax_code: str = Form(...), gross_price: float = Form(...), gl_account: str = Form(...), quotation_no: str = Form(...), session_id: Optional[str] = Cookie(None)):
    ensure_editor(session_id)
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("SELECT cost_center_id2, cost_center_name FROM cost_centers WHERE cost_center_id = ?", (cost_center_id.strip(),))
    cc_res = cursor.fetchone()
    cc_id2 = cc_res['cost_center_id2'] if cc_res else cost_center_id
    cc_name = cc_res['cost_center_name'] if cc_res else "Service Branch"

    cursor.execute("""
        UPDATE po_services SET pur_org=?, vendor_code=?, vendor_name=?, short_text=?, cost_center_id=?, cost_center_id2=?, quantity=?, tax_code=?, gross_price=?, gl_account=?, quotation_no=?
        WHERE id=?
    """, (pur_org, vendor_code, vendor_name, short_text[:40], cost_center_id, cc_id2, quantity, tax_code, gross_price, gl_account, quotation_no, id))
    
    total = quantity * gross_price
    cursor.execute("""
        UPDATE po_covers SET vendor_code=?, vendor_name=?, cost_center_id=?, cost_center_id2=?, cost_center_name=?, description=?, tax_code=?, quantity=?, price=?, total=?, vat=?, price_vat=?
        WHERE quotation_no=? AND po_no LIKE '173%'
    """, (vendor_code, vendor_name, cost_center_id, cc_id2, cc_name, short_text[:40], tax_code, quantity, gross_price, total, total*0.07, total*1.07, quotation_no))
    
    conn.commit()
    conn.close()
    return RedirectResponse(url="/po-service", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/po-service/template")
async def download_po_service_template():
    columns = ["Purchasing org", "Vendor Code", "Vendor Name", "Short Text", "Cost Center ID", "Quantity", "Tax Code", "Gross Price", "G/L Account", "Quotation No"]
    df = pd.DataFrame(columns=columns)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='PO_Service_Template')
    output.seek(0)
    return StreamingResponse(output, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": "attachment; filename=PO_Service_Template.xlsx"})

@app.post("/po-service/import")
async def import_po_service(file: UploadFile = File(...), session_id: Optional[str] = Cookie(None)):
    ensure_editor(session_id)
    contents = await file.read()
    df = pd.read_csv(io.BytesIO(contents)) if file.filename and file.filename.lower().endswith(".csv") else pd.read_excel(io.BytesIO(contents))
    conn = get_db()
    cursor = conn.cursor()
    errors = []
    for idx, row in df.iterrows():
        q_no = str(row.get("Quotation No", "")).strip()
        row_label = q_no if q_no and q_no.lower() != "nan" else f"Row {idx + 2}"
        if not q_no or q_no.lower() == "nan":
            errors.append((row_label, "ไม่พบเลข Quotation No"))
            continue
        if quotation_has_real_po(cursor, q_no):
            errors.append((q_no, "Quotation นี้มีเลข PO แล้ว"))
        if not str(row.get("Vendor Code", "")).strip() or not str(row.get("Vendor Name", "")).strip():
            errors.append((q_no, "ไม่พบ Vendor Code หรือ Vendor Name"))
        if not str(row.get("Cost Center ID", "")).strip():
            errors.append((q_no, "ไม่พบ Cost Center ID"))
        try:
            float(row.get("Gross Price", 0) or 0)
            float(row.get("Quantity", 1) or 1)
        except Exception:
            errors.append((q_no, "Quantity หรือ Gross Price ไม่ใช่ตัวเลข"))
    if errors:
        conn.close()
        return flash_redirect("/po-service", error=import_error_message(errors))

    for _, row in df.iterrows():
        quotation_no = str(row.get("Quotation No", "")).strip()
        if not quotation_no or quotation_no.lower() == "nan":
            continue
        pur_org = str(row.get("Purchasing org", "RD00")).strip()
        vendor_code = str(row.get("Vendor Code", "")).strip()
        vendor_name = str(row.get("Vendor Name", "")).strip()
        short_text = str(row.get("Short Text", ""))[:40]
        cost_center_id = str(row.get("Cost Center ID", "")).strip()
        quantity = float(row.get("Quantity", 1) or 1)
        tax_code = str(row.get("Tax Code", "I2")).strip()
        gross_price = float(row.get("Gross Price", 0) or 0)
        gl_account = str(row.get("G/L Account", "")).strip()

        cursor.execute("SELECT cost_center_id2, cost_center_name FROM cost_centers WHERE cost_center_id = ?", (cost_center_id,))
        cc_res = cursor.fetchone()
        cc_id2 = cc_res['cost_center_id2'] if cc_res else cost_center_id
        cc_name = cc_res['cost_center_name'] if cc_res else "Service Branch"
        cursor.execute("""
            INSERT INTO po_services (pur_org, vendor_code, vendor_name, short_text, cost_center_id, cost_center_id2, quantity, tax_code, gross_price, gl_account, quotation_no, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (pur_org, vendor_code, vendor_name, short_text, cost_center_id, cc_id2, quantity, tax_code, gross_price, gl_account, quotation_no, date.today().isoformat()))
        total = quantity * gross_price
        cursor.execute("SELECT MAX(sq_line) as max_line FROM po_covers WHERE quotation_no = ?", (quotation_no,))
        line_res = cursor.fetchone()
        sq_line = 10 if (not line_res or line_res['max_line'] is None) else line_res['max_line'] + 10
        cursor.execute("""
            INSERT INTO po_covers (vendor_code, vendor_name, sq_date, quotation_no, sq_line, po_no, cost_center_id, cost_center_id2, cost_center_name, description, tax_code, quantity, price, total, vat, price_vat, po_status)
            VALUES (?, ?, ?, ?, ?, '173XXXXX', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (vendor_code, vendor_name, date.today().isoformat(), quotation_no, sq_line, cost_center_id, cc_id2, cc_name, short_text, tax_code, quantity, gross_price, total, total*0.07, total*1.07, DEFAULT_PO_STATUS))
    conn.commit()
    conn.close()
    return flash_redirect("/po-service", message="Import PO Service สำเร็จ")

@app.get("/po-service/export")
async def export_po_service():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT ps.*, COALESCE(NULLIF(cc.cost_center_name, ''), NULLIF(cc.cost_center_id2, ''), NULLIF(ps.cost_center_id2, ''), ps.cost_center_id) AS export_cost_center_id2
        FROM po_services ps
        LEFT JOIN cost_centers cc ON cc.cost_center_id = ps.cost_center_id
        ORDER BY ps.quotation_no ASC, ps.id ASC
    """)
    rows = cursor.fetchall()
    log_export_quotes(cursor, "service", [r["quotation_no"] for r in rows])
    conn.commit()
    conn.close()
    
    # เงื่อนไข: ฟอร์แมตวันที่แบบติดกัน YYYYMMDD
    today_str = datetime.now().strftime("%Y%m%d")
    last_day = calendar.monthrange(datetime.now().year, datetime.now().month)[1]
    end_of_month = datetime.now().replace(day=last_day).strftime("%Y%m%d")
    
    export_data = []
    prev_quote = None
    for r in rows:
        po_val = "N" if (prev_quote is None or r['quotation_no'] != prev_quote) else ""
        prev_quote = r['quotation_no']
        
        export_data.append({
            "PO": po_val, 
            "Company Code": "7590", 
            "Document Type": "SED", 
            "creating date": today_str, 
            "Purchasing Group": "104", 
            "Purchasing org": r['pur_org'], 
            "Vendor code": r['vendor_code'], 
            "Validty Start": today_str, 
            "Validty End": end_of_month, 
            "Account Ass, Cat": "K", 
            "Item Cat.": "D", 
            "Material": "", 
            "Short Text (Service Item)": r['short_text'], 
            "Plant": r['cost_center_id'], 
            "Quantity": r['quantity'], 
            "Tax Code": r['tax_code'], 
            "Net Price": "", 
            "Material Group": "DIL128", 
            "Store Location": "MS01", 
            "Short Text (Activity)": r['short_text'], 
            "Quantity (Activity)": r['quantity'], 
            "Unit of Meas.": "NO", 
            "Gross Price": r['gross_price'], # แทรกช่องราคาดึงค่าสดจากแบบฟอร์มต่อท้าย Unit of Meas.
            "G/L Account": r['gl_account'], 
            "Cost Center (HQ/WH)": r['export_cost_center_id2'], 
            "Condition": "", 
            "HSN/SAC Code": "", 
            "Header Text": "", 
            "Work Order NO / Quote No": r['quotation_no']
        })
        
    df = pd.DataFrame(export_data)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='PO_Service')
    output.seek(0)
    return StreamingResponse(output, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f"attachment; filename=PO_Service_Export_{today_str}.xlsx"})

@app.post("/po-service/export-selected")
async def export_selected_po_service(selected_ids: Optional[list[int]] = Form(None), session_id: Optional[str] = Cookie(None)):
    ensure_editor(session_id)
    if not selected_ids:
        return flash_redirect("/po-service", error="กรุณาเลือกรายการที่ต้องการ Export")
    placeholders = ",".join("?" for _ in selected_ids)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(f"""
        SELECT ps.*, COALESCE(NULLIF(cc.cost_center_name, ''), NULLIF(cc.cost_center_id2, ''), NULLIF(ps.cost_center_id2, ''), ps.cost_center_id) AS export_cost_center_id2
        FROM po_services ps
        LEFT JOIN cost_centers cc ON cc.cost_center_id = ps.cost_center_id
        WHERE ps.id IN ({placeholders})
        ORDER BY ps.quotation_no ASC, ps.id ASC
    """, selected_ids)
    rows = cursor.fetchall()
    log_export_quotes(cursor, "service", [r["quotation_no"] for r in rows])
    conn.commit()
    conn.close()

    today_str = datetime.now().strftime("%Y%m%d")
    last_day = calendar.monthrange(datetime.now().year, datetime.now().month)[1]
    end_of_month = datetime.now().replace(day=last_day).strftime("%Y%m%d")
    export_data = []
    prev_quote = None
    for r in rows:
        po_val = "N" if (prev_quote is None or r['quotation_no'] != prev_quote) else ""
        prev_quote = r['quotation_no']
        export_data.append({
            "PO": po_val,
            "Company Code": "7590",
            "Document Type": "SED",
            "creating date": today_str,
            "Purchasing Group": "104",
            "Purchasing org": r['pur_org'],
            "Vendor code": r['vendor_code'],
            "Validty Start": today_str,
            "Validty End": end_of_month,
            "Account Ass, Cat": "K",
            "Item Cat.": "D",
            "Material": "",
            "Short Text (Service Item)": r['short_text'],
            "Plant": r['cost_center_id'],
            "Quantity": r['quantity'],
            "Tax Code": r['tax_code'],
            "Net Price": "",
            "Material Group": "DIL128",
            "Store Location": "MS01",
            "Short Text (Activity)": r['short_text'],
            "Quantity (Activity)": r['quantity'],
            "Unit of Meas.": "NO",
            "Gross Price": r['gross_price'],
            "G/L Account": r['gl_account'],
            "Cost Center (HQ/WH)": r['export_cost_center_id2'],
            "Condition": "",
            "HSN/SAC Code": "",
            "Header Text": "",
            "Work Order NO / Quote No": r['quotation_no']
        })
    df = pd.DataFrame(export_data)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='PO_Service')
    output.seek(0)
    return StreamingResponse(output, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f"attachment; filename=PO_Service_Export_{today_str}.xlsx"})


# =====================================================
# PO ASSET SECTION (REVIEW & DD.MM.YYYY EXPORT)
# =====================================================
@app.get("/po-asset", response_class=HTMLResponse)
async def po_asset_page(request: Request, error: str = "", message: str = "", session_id: Optional[str] = Cookie(None)):
    try: user = get_current_user(session_id)
    except HTTPException: return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM vendors ORDER BY vendor_name ASC")
    v_list = [dict(r) for r in cursor.fetchall()]
    cursor.execute("SELECT * FROM materials ORDER BY material_name ASC")
    m_list = [dict(r) for r in cursor.fetchall()]
    cursor.execute("SELECT * FROM cost_centers ORDER BY cost_center_id ASC")
    cc_list = [dict(r) for r in cursor.fetchall()]
    cursor.execute("SELECT * FROM po_assets ORDER BY id DESC")
    saved_assets = [dict(r) for r in cursor.fetchall()]
    duplicate_quotes = duplicate_quotation_set(cursor, "po_assets")
    asset_quotes = sorted({r["quotation_no"] for r in saved_assets if r.get("quotation_no")})
    counts = export_counts(cursor, "asset")
    conn.close()
    return templates.TemplateResponse(request=request, name="po_asset.html", context={"vendors": v_list, "materials": m_list, "cost_centers": cc_list, "assets": saved_assets, "duplicate_quotes": duplicate_quotes, "existing_quotes": asset_quotes, "export_counts": counts, "current_user": user, "can_edit": user["is_admin"] or user["role"] == ROLE_EDITOR, "error": error, "message": message})

@app.post("/po-asset/add")
async def add_po_asset(vendor_code: str = Form(...), vendor_name: str = Form(...), material_code: str = Form(...), material_name: str = Form(...), quantity: float = Form(...), tax_code: str = Form(...), price_ztax: float = Form(...), detail: str = Form(...), project: str = Form(...), quotation_no: str = Form(...), cost_center_id: str = Form(...), confirm_duplicate: str = Form("0"), session_id: Optional[str] = Cookie(None)):
    ensure_editor(session_id)
    conn = get_db()
    cursor = conn.cursor()
    quotation_no = quotation_no.strip()
    if quotation_has_real_po(cursor, quotation_no):
        conn.close()
        return flash_redirect("/po-asset", error=f"Quotation {quotation_no} มีเลข PO แล้ว ไม่อนุญาตให้คีย์ซ้ำ")
    if quotation_exists(cursor, "po_assets", quotation_no) and confirm_duplicate != "1":
        conn.close()
        return flash_redirect("/po-asset", error=f"Quotation {quotation_no} ซ้ำ กรุณายืนยันก่อนบันทึก")
    cursor.execute("""
        INSERT INTO po_assets (vendor_code, vendor_name, material_code, material_name, quantity, tax_code, price_ztax, detail, project, quotation_no, cost_center_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (vendor_code, vendor_name, material_code, material_name, quantity, tax_code, price_ztax, detail[:40], project, quotation_no, cost_center_id, date.today().isoformat()))
    
    total = quantity * price_ztax
    cursor.execute("""
        INSERT INTO po_covers (vendor_code, vendor_name, sq_date, quotation_no, sq_line, po_no, cost_center_id, cost_center_id2, cost_center_name, description, tax_code, quantity, price, total, vat, price_vat, po_status)
        VALUES (?, ?, ?, ?, 10, '175XXXXX', ?, ?, 'Asset Branch', ?, ?, ?, ?, ?, ?, ?, ?)
    """, (vendor_code, vendor_name, date.today().isoformat(), quotation_no, cost_center_id, cost_center_id, detail[:40], tax_code, quantity, price_ztax, total, total*0.07, total*1.07, DEFAULT_PO_STATUS))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/po-asset", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/po-asset/delete/{asset_id}")
async def delete_po_asset(asset_id: int, session_id: Optional[str] = Cookie(None)):
    ensure_editor(session_id)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM po_assets WHERE id = ?", (asset_id,))
    row = cursor.fetchone()
    if row:
        cursor.execute("DELETE FROM po_assets WHERE id = ?", (asset_id,))
        cursor.execute("""
            DELETE FROM po_covers
            WHERE id = (
                SELECT id FROM po_covers
                WHERE quotation_no = ? AND po_no LIKE '175%'
                ORDER BY id DESC
                LIMIT 1
            )
        """, (row["quotation_no"],))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/po-asset", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/po-asset/update")
async def update_po_asset(id: int = Form(...), vendor_code: str = Form(...), vendor_name: str = Form(...), material_code: str = Form(...), material_name: str = Form(...), quantity: float = Form(...), tax_code: str = Form(...), price_ztax: float = Form(...), detail: str = Form(...), project: str = Form(...), quotation_no: str = Form(...), cost_center_id: str = Form(...), session_id: Optional[str] = Cookie(None)):
    ensure_editor(session_id)
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("""
        UPDATE po_assets SET vendor_code=?, vendor_name=?, material_code=?, material_name=?, quantity=?, tax_code=?, price_ztax=?, detail=?, project=?, quotation_no=?, cost_center_id=?
        WHERE id=?
    """, (vendor_code, vendor_name, material_code, material_name, quantity, tax_code, price_ztax, detail[:40], project, quotation_no, cost_center_id, id))
    
    total = quantity * price_ztax
    cursor.execute("""
        UPDATE po_covers SET vendor_code=?, vendor_name=?, cost_center_id=?, cost_center_id2=?, description=?, tax_code=?, quantity=?, price=?, total=?, vat=?, price_vat=?
        WHERE quotation_no=? AND po_no LIKE '175%'
    """, (vendor_code, vendor_name, cost_center_id, cost_center_id, detail[:40], tax_code, quantity, price_ztax, total, total*0.07, total*1.07, quotation_no))
    
    conn.commit()
    conn.close()
    return RedirectResponse(url="/po-asset", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/po-asset/template")
async def download_po_asset_template():
    columns = ["Vendor Code", "Vendor Name", "Material Code", "Material Name", "Quantity", "Tax Code", "Price for ZTAX", "Detail", "Project", "Quotation No", "Cost Center ID"]
    df = pd.DataFrame(columns=columns)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='PO_Asset_Template')
    output.seek(0)
    return StreamingResponse(output, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": "attachment; filename=PO_Asset_Template.xlsx"})

@app.post("/po-asset/import")
async def import_po_asset(file: UploadFile = File(...), session_id: Optional[str] = Cookie(None)):
    ensure_editor(session_id)
    contents = await file.read()
    df = pd.read_csv(io.BytesIO(contents)) if file.filename and file.filename.lower().endswith(".csv") else pd.read_excel(io.BytesIO(contents))
    conn = get_db()
    cursor = conn.cursor()
    errors = []
    for idx, row in df.iterrows():
        q_no = str(row.get("Quotation No", "")).strip()
        row_label = q_no if q_no and q_no.lower() != "nan" else f"Row {idx + 2}"
        if not q_no or q_no.lower() == "nan":
            errors.append((row_label, "ไม่พบเลข Quotation No"))
            continue
        if quotation_has_real_po(cursor, q_no):
            errors.append((q_no, "Quotation นี้มีเลข PO แล้ว"))
        if not str(row.get("Vendor Code", "")).strip() or not str(row.get("Vendor Name", "")).strip():
            errors.append((q_no, "ไม่พบ Vendor Code หรือ Vendor Name"))
        if not str(row.get("Material Code", "")).strip() or not str(row.get("Material Name", "")).strip():
            errors.append((q_no, "ไม่พบ Material Code หรือ Material Name"))
        if not str(row.get("Cost Center ID", "")).strip():
            errors.append((q_no, "ไม่พบ Cost Center ID"))
        try:
            float(row.get("Price for ZTAX", 0) or 0)
            float(row.get("Quantity", 1) or 1)
        except Exception:
            errors.append((q_no, "Quantity หรือ Price for ZTAX ไม่ใช่ตัวเลข"))
    if errors:
        conn.close()
        return flash_redirect("/po-asset", error=import_error_message(errors))

    for _, row in df.iterrows():
        quotation_no = str(row.get("Quotation No", "")).strip()
        if not quotation_no or quotation_no.lower() == "nan":
            continue
        vendor_code = str(row.get("Vendor Code", "")).strip()
        vendor_name = str(row.get("Vendor Name", "")).strip()
        material_code = str(row.get("Material Code", "")).strip()
        material_name = str(row.get("Material Name", "")).strip()
        quantity = float(row.get("Quantity", 1) or 1)
        tax_code = str(row.get("Tax Code", "I2")).strip()
        price_ztax = float(row.get("Price for ZTAX", 0) or 0)
        detail = str(row.get("Detail", ""))[:40]
        project = str(row.get("Project", "")).strip()
        cost_center_id = str(row.get("Cost Center ID", "")).strip()
        cursor.execute("""
            INSERT INTO po_assets (vendor_code, vendor_name, material_code, material_name, quantity, tax_code, price_ztax, detail, project, quotation_no, cost_center_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (vendor_code, vendor_name, material_code, material_name, quantity, tax_code, price_ztax, detail, project, quotation_no, cost_center_id, date.today().isoformat()))
        total = quantity * price_ztax
        cursor.execute("SELECT MAX(sq_line) as max_line FROM po_covers WHERE quotation_no = ?", (quotation_no,))
        line_res = cursor.fetchone()
        sq_line = 10 if (not line_res or line_res['max_line'] is None) else line_res['max_line'] + 10
        cursor.execute("""
            INSERT INTO po_covers (vendor_code, vendor_name, sq_date, quotation_no, sq_line, po_no, cost_center_id, cost_center_id2, cost_center_name, description, tax_code, quantity, price, total, vat, price_vat, po_status)
            VALUES (?, ?, ?, ?, ?, '175XXXXX', ?, ?, 'Asset Branch', ?, ?, ?, ?, ?, ?, ?, ?)
        """, (vendor_code, vendor_name, date.today().isoformat(), quotation_no, sq_line, cost_center_id, cost_center_id, detail, tax_code, quantity, price_ztax, total, total*0.07, total*1.07, DEFAULT_PO_STATUS))
    conn.commit()
    conn.close()
    return flash_redirect("/po-asset", message="Import PO Asset สำเร็จ")

@app.get("/po-asset/export")
async def export_po_asset():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM po_assets ORDER BY quotation_no ASC, id ASC")
    rows = cursor.fetchall()
    log_export_quotes(cursor, "asset", [r["quotation_no"] for r in rows])
    conn.commit()
    conn.close()
    
    # เงื่อนไข: รูปแบบวันที่แบบมีจุดคั่น DD.MM.YYYY
    today_str = datetime.now().strftime("%d.%m.%Y")
    last_day = calendar.monthrange(datetime.now().year, datetime.now().month)[1]
    end_of_month = datetime.now().replace(day=last_day).strftime("%d.%m.%Y")
    
    export_data = []
    sr_no = 0
    prev_quote = None
    for r in rows:
        if prev_quote is None or r['quotation_no'] != prev_quote: sr_no += 1
        prev_quote = r['quotation_no']
        
        # คอลัมน์ "Not Required" ปลดตัวเลขออกทั้งหมด และเพิ่มคอลัมน์เปล่า (Blank Column) ถัดไปช่องที่ 5
        export_data.append({
            "Sr No.": sr_no, 
            "Company Code": "7590", 
            "Document Type": "FAP", 
            "creating date": today_str, 
            "Purchasing Group": "119", 
            "Pur Org": r['cost_center_id'], 
            "Vendor code": r['vendor_code'], 
            "Material code": r['material_code'], 
            "Plant": r['cost_center_id'], 
            "Quantity": r['quantity'], 
            "Tax Code": r['tax_code'], 
            "Price for ZTAX": r['price_ztax'], 
            "Asset Code": "", 
            "Not Required": r['project'], 
            "Not Required ": "", 
            "Not Required  ": "", 
            "Not Required   ": "", 
            "Not Required    ": "", 
            "Blank Column": "", # คอลัมน์ว่างตามเงื่อนไขที่สั่งเพิ่ม
            "Deliver Date": end_of_month, 
            "OLD PO NO. for Open PO": "", 
            "Storage Location": "MS01", 
            "QO details": r['detail'], 
            "Work Order NO / Quote No": r['quotation_no']
        })
        
    df = pd.DataFrame(export_data)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='PO_Asset')
    output.seek(0)
    return StreamingResponse(output, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f"attachment; filename=PO_Asset_Export_{today_str}.xlsx"})

@app.post("/po-asset/export-selected")
async def export_selected_po_asset(selected_ids: Optional[list[int]] = Form(None), session_id: Optional[str] = Cookie(None)):
    ensure_editor(session_id)
    if not selected_ids:
        return flash_redirect("/po-asset", error="กรุณาเลือกรายการที่ต้องการ Export")
    placeholders = ",".join("?" for _ in selected_ids)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(f"SELECT * FROM po_assets WHERE id IN ({placeholders}) ORDER BY quotation_no ASC, id ASC", selected_ids)
    rows = cursor.fetchall()
    log_export_quotes(cursor, "asset", [r["quotation_no"] for r in rows])
    conn.commit()
    conn.close()

    today_str = datetime.now().strftime("%d.%m.%Y")
    last_day = calendar.monthrange(datetime.now().year, datetime.now().month)[1]
    end_of_month = datetime.now().replace(day=last_day).strftime("%d.%m.%Y")
    export_data = []
    sr_no = 0
    prev_quote = None
    for r in rows:
        if prev_quote is None or r['quotation_no'] != prev_quote:
            sr_no += 1
        prev_quote = r['quotation_no']
        export_data.append({
            "Sr No.": sr_no,
            "Company Code": "7590",
            "Document Type": "FAP",
            "creating date": today_str,
            "Purchasing Group": "119",
            "Pur Org": r['cost_center_id'],
            "Vendor code": r['vendor_code'],
            "Material code": r['material_code'],
            "Plant": r['cost_center_id'],
            "Quantity": r['quantity'],
            "Tax Code": r['tax_code'],
            "Price for ZTAX": r['price_ztax'],
            "Asset Code": "",
            "Not Required": r['project'],
            "Not Required ": "",
            "Not Required  ": "",
            "Not Required   ": "",
            "Not Required    ": "",
            "Blank Column": "",
            "Deliver Date": end_of_month,
            "OLD PO NO. for Open PO": "",
            "Storage Location": "MS01",
            "QO details": r['detail'],
            "Work Order NO / Quote No": r['quotation_no']
        })
    df = pd.DataFrame(export_data)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='PO_Asset')
    output.seek(0)
    return StreamingResponse(output, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f"attachment; filename=PO_Asset_Export_{today_str}.xlsx"})


# MATERIALS MASTER
@app.get("/materials", response_class=HTMLResponse)
async def manage_materials(request: Request, session_id: Optional[str] = Cookie(None)):
    try: current_user = get_current_user(session_id)
    except HTTPException: return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM materials ORDER BY material_name ASC")
    mats = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return templates.TemplateResponse(request=request, name="master_materials.html", context={"materials": mats, "current_user": current_user})

@app.post("/materials/add")
async def add_material(material_code: str = Form(...), material_name: str = Form(...), session_id: Optional[str] = Cookie(None)):
    ensure_editor(session_id)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO materials (material_code, material_name) VALUES (?, ?)", (material_code.strip(), material_name.strip()))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/materials", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/materials/bulk-paste")
async def bulk_paste_materials(paste_data: str = Form(...), session_id: Optional[str] = Cookie(None)):
    ensure_editor(session_id)
    conn = get_db()
    cursor = conn.cursor()
    for line in paste_data.strip().split('\n'):
        if not line.strip(): continue
        parts = line.split('\t') if '\t' in line else line.split(None, 1)
        if len(parts) >= 2:
            cursor.execute("INSERT OR REPLACE INTO materials (material_code, material_name) VALUES (?, ?)", (parts[0].strip(), parts[1].strip()))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/materials", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/materials/delete/{code}")
async def delete_material(code: str, session_id: Optional[str] = Cookie(None)):
    ensure_editor(session_id)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM materials WHERE material_code = ?", (code,))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/materials", status_code=status.HTTP_303_SEE_OTHER)

# JOB OPERATIONS
@app.post("/add-job")
async def add_job(vendor_name: str = Form(...), vendor_code: str = Form(...), sq_date: str = Form(...), quotation_no: str = Form(...), po_no: str = Form(None), cost_center_id: str = Form(...), cost_center_name: str = Form(...), description: str = Form(...), tax_code: str = Form(...), quantity: float = Form(...), price: float = Form(...), po_status: str = Form(...), session_id: Optional[str] = Cookie(None)):
    ensure_editor(session_id)
    po_status = po_status if po_status in PO_STATUSES else DEFAULT_PO_STATUS
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT MAX(sq_line) as max_line FROM po_covers WHERE quotation_no = ?", (quotation_no,))
    res = cursor.fetchone()
    sq_line = 10 if (not res or res['max_line'] is None) else res['max_line'] + 10
    total = price * quantity
    cursor.execute("SELECT cost_center_id2 FROM cost_centers WHERE cost_center_id = ?", (cost_center_id.strip(),))
    cc_res = cursor.fetchone()
    cc_id2 = cc_res['cost_center_id2'] if cc_res else cost_center_id
    cursor.execute("INSERT INTO po_covers (vendor_code, vendor_name, sq_date, quotation_no, sq_line, po_no, cost_center_id, cost_center_id2, cost_center_name, description, tax_code, quantity, price, total, vat, price_vat, po_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (vendor_code, vendor_name, sq_date, quotation_no, sq_line, po_no or "", cost_center_id, cc_id2, cost_center_name, description[:40], tax_code, quantity, price, total, total*0.07, total*1.07, po_status))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/update-job")
async def update_job(job_id: int = Form(...), vendor_name: str = Form(...), vendor_code: str = Form(...), cost_center_id: str = Form(...), cost_center_name: str = Form(...), po_no: str = Form(None), po_status: str = Form(...), description: str = Form(...), session_id: Optional[str] = Cookie(None)):
    ensure_editor(session_id)
    po_status = po_status if po_status in PO_STATUSES else DEFAULT_PO_STATUS
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT quantity, price FROM po_covers WHERE id = ?", (job_id,))
    row = cursor.fetchone()
    total = row['quantity']*row['price'] if row else 0
    cursor.execute("SELECT cost_center_id2 FROM cost_centers WHERE cost_center_id = ?", (cost_center_id.strip(),))
    cc_res = cursor.fetchone()
    cc_id2 = cc_res['cost_center_id2'] if cc_res else cost_center_id
    cursor.execute("UPDATE po_covers SET vendor_code=?, vendor_name=?, cost_center_id=?, cost_center_id2=?, cost_center_name=?, po_no=?, po_status=?, description=?, total=?, vat=?, price_vat=? WHERE id=?",
                   (vendor_code, vendor_name, cost_center_id, cc_id2, cost_center_name, po_no or "", po_status, description[:40], total, total*0.07, total*1.07, job_id))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/job/{job_id}/edit", response_class=HTMLResponse)
async def edit_job_page(job_id: int, request: Request, session_id: Optional[str] = Cookie(None)):
    try: current_user = ensure_editor(session_id)
    except HTTPException: return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM po_covers WHERE id = ?", (job_id,))
    job = cursor.fetchone()
    if not job:
        conn.close()
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    cursor.execute("SELECT * FROM vendors ORDER BY vendor_name ASC")
    vendors = [dict(r) for r in cursor.fetchall()]
    cursor.execute("SELECT * FROM cost_centers ORDER BY cost_center_id ASC")
    cost_centers = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return templates.TemplateResponse(request=request, name="edit_job.html", context={
        "job": dict(job),
        "vendors": vendors,
        "cost_centers": cost_centers,
        "current_user": current_user,
        "po_statuses": PO_STATUSES,
    })

@app.post("/job/{job_id}/edit")
async def edit_job_submit(job_id: int, vendor_name: str = Form(...), vendor_code: str = Form(...), sq_date: str = Form(...), quotation_no: str = Form(...), po_no: str = Form(None), cost_center_id: str = Form(...), cost_center_name: str = Form(...), description: str = Form(...), tax_code: str = Form(...), quantity: float = Form(...), price: float = Form(...), po_status: str = Form(...), session_id: Optional[str] = Cookie(None)):
    ensure_editor(session_id)
    po_status = po_status if po_status in PO_STATUSES else DEFAULT_PO_STATUS
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT cost_center_id2 FROM cost_centers WHERE cost_center_id = ?", (cost_center_id.strip(),))
    cc_res = cursor.fetchone()
    cc_id2 = cc_res['cost_center_id2'] if cc_res else cost_center_id
    total = quantity * price
    cursor.execute("""
        UPDATE po_covers
        SET vendor_code=?, vendor_name=?, sq_date=?, quotation_no=?, po_no=?, cost_center_id=?, cost_center_id2=?,
            cost_center_name=?, description=?, tax_code=?, quantity=?, price=?, total=?, vat=?, price_vat=?, po_status=?
        WHERE id=?
    """, (vendor_code, vendor_name, sq_date, quotation_no, po_no or "", cost_center_id, cc_id2, cost_center_name, description[:40], tax_code, quantity, price, total, total*0.07, total*1.07, po_status, job_id))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/delete-job/{job_id}")
async def delete_job(job_id: int, session_id: Optional[str] = Cookie(None)):
    ensure_editor(session_id)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM po_covers WHERE id = ?", (job_id,))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)
