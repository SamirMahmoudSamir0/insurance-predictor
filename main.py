import os
import json
import time
import re
import asyncio
import secrets
import traceback
import urllib.parse
import urllib.request
import sqlite3
import subprocess
import hashlib
import base64
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from google import genai
from google.genai import types
import companion_auth
import exam_studio

app = FastAPI(title="S-Tesseract Platform")

ai = genai.Client()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_default_db_dir = "/data" if os.path.isdir("/data") and os.access("/data", os.W_OK) else BASE_DIR
DB_FILE = os.environ.get("STESSERACT_DB_FILE", os.path.join(_default_db_dir, "stesseract_core.db"))
os.makedirs(os.path.dirname(os.path.abspath(DB_FILE)), exist_ok=True)
GENERATED_VIDEO_DIR = os.environ.get("GENERATED_VIDEO_DIR", os.path.join(_default_db_dir, "generated_videos"))
os.makedirs(GENERATED_VIDEO_DIR, exist_ok=True)
STUDY_UPLOAD_DIR = os.environ.get("STUDY_UPLOAD_DIR", os.path.join(_default_db_dir, "study_uploads"))
os.makedirs(STUDY_UPLOAD_DIR, exist_ok=True)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_MODE = os.environ.get("TELEGRAM_MODE", "polling").lower()
APP_TIMEZONE_NAME = os.environ.get("APP_TIMEZONE", "Africa/Cairo")
try:
    APP_TZ = ZoneInfo(APP_TIMEZONE_NAME)
except Exception:
    APP_TZ = ZoneInfo("UTC")

# -----------------------------------------------------------------------------
# Private admin dashboard configuration
# -----------------------------------------------------------------------------
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin").strip()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
ADMIN_SESSION_DAYS = int(os.environ.get("ADMIN_SESSION_DAYS", "7"))
ADMIN_COOKIE = "st_admin_session"
USER_COOKIE = "st_user_session"

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

def _request_is_https(request: Request) -> bool:
    """Detect HTTPS correctly behind Blitz/reverse proxies."""
    forwarded = (request.headers.get("x-forwarded-proto", "") or "").split(",")[0].strip().lower()
    return request.url.scheme.lower() == "https" or forwarded == "https" or os.environ.get("COOKIE_SECURE", "").lower() == "true"

def _set_user_session_cookie(response: JSONResponse, request: Request, token: str) -> None:
    # Persistent 30-day server-side session. The token itself is only in an
    # HttpOnly cookie; the DB stores its SHA-256 hash.
    response.set_cookie(
        USER_COOKIE, token, max_age=60*60*24*30,
        httponly=True, samesite="lax", secure=_request_is_https(request), path="/"
    )

def _hash_admin_password(password: str) -> str:
    # Stored only as a one-way PBKDF2 digest if ADMIN_PASSWORD_HASH is supplied.
    salt = os.environ.get("ADMIN_PASSWORD_SALT", "s-tesseract-admin-salt").encode("utf-8")
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 240_000).hex()

def _admin_password_ok(password: str) -> bool:
    expected = os.environ.get("ADMIN_PASSWORD_HASH", "").strip()
    if expected:
        return secrets.compare_digest(_hash_admin_password(password), expected)
    return bool(ADMIN_PASSWORD) and secrets.compare_digest(password, ADMIN_PASSWORD)

def _audit(user_id, event_type: str, details: str = ""):
    try:
        conn = get_db()
        conn.execute("INSERT INTO admin_audit_log(user_id,event_type,details,created_at) VALUES(?,?,?,?)",
                     (user_id, event_type, details[:2000], datetime.now(APP_TZ).isoformat()))
        conn.commit(); conn.close()
    except Exception:
        pass

def _admin_auth(request):
    token = request.cookies.get(ADMIN_COOKIE, "")
    if not token:
        raise HTTPException(status_code=401, detail="Admin authentication required")
    conn = get_db(); row = conn.execute(
        "SELECT token_hash,expires_at FROM admin_sessions WHERE token_hash=?", (_hash_token(token),)
    ).fetchone()
    if not row or datetime.fromisoformat(row["expires_at"]) <= datetime.now(APP_TZ):
        conn.execute("DELETE FROM admin_sessions WHERE token_hash=?", (_hash_token(token),)); conn.commit(); conn.close()
        raise HTTPException(status_code=401, detail="Admin session expired")
    conn.close()
    return True

app.mount("/generated-videos", StaticFiles(directory=GENERATED_VIDEO_DIR), name="generated-videos")
app.mount("/study-uploads", StaticFiles(directory=STUDY_UPLOAD_DIR), name="study-uploads")

@app.middleware("http")
async def analytics_middleware(request: Request, call_next):
    response = await call_next(request)
    try:
        if request.url.path == "/" or request.url.path.startswith("/api/"):
            token = request.cookies.get("st_visitor") or secrets.token_urlsafe(20)
            user_token = request.cookies.get(USER_COOKIE, "")
            user_id = None
            now=datetime.now(APP_TZ).isoformat()
            conn=get_db(); cur=conn.cursor()
            if user_token:
                row=cur.execute("SELECT user_id FROM user_sessions WHERE token_hash=? AND expires_at>?", (_hash_token(user_token), now)).fetchone()
                if row:
                    user_id=row["user_id"]
                    cur.execute("UPDATE users SET last_seen_at=? WHERE id=?", (now,user_id))
                    cur.execute("UPDATE user_sessions SET expires_at=? WHERE token_hash=?", ((datetime.now(APP_TZ)+timedelta(days=30)).isoformat(), _hash_token(user_token)))
            vh=_hash_token(token)
            row=cur.execute("SELECT id FROM site_visits WHERE visitor_hash=? AND path=?",(vh,request.url.path)).fetchone()
            if row: cur.execute("UPDATE site_visits SET user_id=?,last_seen_at=?,visit_count=visit_count+1 WHERE id=?",(user_id,now,row["id"]))
            else: cur.execute("INSERT INTO site_visits(visitor_hash,user_id,path,first_seen_at,last_seen_at,visit_count) VALUES(?,?,?,?,?,1)",(vh,user_id,request.url.path,now,now))
            conn.commit(); conn.close()
            if not request.cookies.get("st_visitor"): response.set_cookie("st_visitor",token,max_age=60*60*24*90,httponly=True,samesite="lax",secure=(request.url.scheme == "https"),path="/")
    except Exception:
        pass
    return response

# -----------------------------------------------------------------------------
# Database Setup
# -----------------------------------------------------------------------------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            field_of_study TEXT NOT NULL,
            role_model TEXT DEFAULT '',
            xp INTEGER DEFAULT 0,
            streak INTEGER DEFAULT 0,
            last_active_date TEXT,
            tests_taken INTEGER DEFAULT 0,
            tests_passed INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS admin_xp_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            delta INTEGER NOT NULL,
            old_xp INTEGER NOT NULL,
            new_xp INTEGER NOT NULL,
            reason TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    cur.execute("PRAGMA table_info(users)")
    cols = [col[1] for col in cur.fetchall()]
    if "role_model" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN role_model TEXT DEFAULT ''")
    if "telegram_chat_id" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN telegram_chat_id TEXT DEFAULT ''")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS schedule_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            target_date TEXT NOT NULL,
            target_time TEXT DEFAULT '',
            is_completed INTEGER DEFAULT 0,
            source TEXT DEFAULT 'manual',
            external_id TEXT DEFAULT '',
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    """)

    cur.execute("PRAGMA table_info(schedule_items)")
    s_cols = [col[1] for col in cur.fetchall()]
    if "target_time" not in s_cols:
        cur.execute("ALTER TABLE schedule_items ADD COLUMN target_time TEXT DEFAULT ''")
    if "source" not in s_cols:
        cur.execute("ALTER TABLE schedule_items ADD COLUMN source TEXT DEFAULT 'manual'")
    if "external_id" not in s_cols:
        cur.execute("ALTER TABLE schedule_items ADD COLUMN external_id TEXT DEFAULT ''")
    if "reminder_sent" not in s_cols:
        cur.execute("ALTER TABLE schedule_items ADD COLUMN reminder_sent INTEGER DEFAULT 0")

    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS video_jobs (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            progress INTEGER DEFAULT 0,
            stage TEXT DEFAULT '',
            filename TEXT DEFAULT '',
            perspective INTEGER DEFAULT 1,
            source_pdf TEXT DEFAULT '',
            result_json TEXT DEFAULT '',
            video_url TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            error TEXT DEFAULT ''
        )
    """)

    cur.execute("PRAGMA table_info(video_jobs)")
    v_cols = [col[1] for col in cur.fetchall()]
    for col_name, col_def in [
        ("perspective", "INTEGER DEFAULT 1"),
        ("source_pdf", "TEXT DEFAULT ''"),
        ("result_json", "TEXT DEFAULT ''"),
        ("video_url", "TEXT DEFAULT ''"),
        ("error", "TEXT DEFAULT ''"),
    ]:
        if col_name not in v_cols:
            cur.execute(f"ALTER TABLE video_jobs ADD COLUMN {col_name} {col_def}")
    cur.execute("PRAGMA table_info(video_jobs)")
    v_cols = [c[1] for c in cur.fetchall()]
    for col_name, col_def in [
        ("user_id", "INTEGER"),
        ("language", "TEXT DEFAULT 'ar'"),
        ("source_hash", "TEXT DEFAULT ''"),
        ("engine", "TEXT DEFAULT 'cinematic'"),
        ("mode", "TEXT DEFAULT 'ai'"),
        ("youtube_json", "TEXT DEFAULT '[]'"),
        ("material_id", "INTEGER"),
    ]:
        if col_name not in v_cols:
            cur.execute(f"ALTER TABLE video_jobs ADD COLUMN {col_name} {col_def}")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_video_jobs_queue ON video_jobs(status,created_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_video_jobs_user ON video_jobs(user_id)")

    cur.execute("""CREATE TABLE IF NOT EXISTS study_room_settings (
        user_id INTEGER PRIMARY KEY, focus_mode INTEGER DEFAULT 1, audio_type TEXT DEFAULT 'none',
        audio_volume REAL DEFAULT 0.35, quran_reciter TEXT DEFAULT '1', quran_surah INTEGER DEFAULT 1,
        noise_type TEXT DEFAULT 'white', updated_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS app_notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, title TEXT NOT NULL, message TEXT DEFAULT '',
        kind TEXT DEFAULT 'info', is_read INTEGER DEFAULT 0, created_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_notifications_user ON app_notifications(user_id,created_at)")

    # Existing users get a stable public ID once.
    # IMPORTANT: this runs only AFTER the full users-column migration above.
    cur.execute("PRAGMA table_info(users)")
    final_user_cols = {c[1] for c in cur.fetchall()}
    if "public_user_id" in final_user_cols:
        rows = cur.execute("SELECT id, public_user_id FROM users ORDER BY id").fetchall()
        for row in rows:
            if not row[1]:
                code = "STX-" + secrets.token_hex(5).upper()
                cur.execute("UPDATE users SET public_user_id=? WHERE id=?", (code, row[0]))

    # If the server restarted while a video was processing, put it back in the durable queue.
    cur.execute("UPDATE video_jobs SET status='queued', stage='أعيد إلى قائمة الانتظار بعد إعادة تشغيل السيرفر' WHERE status='processing'")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS telegram_link_tokens (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            expires_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS focus_todos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            task TEXT NOT NULL,
            is_done INTEGER DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    """)
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS blueprint_nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            node_order INTEGER NOT NULL,
            title TEXT NOT NULL,
            philosophy TEXT NOT NULL,
            timeframe TEXT DEFAULT '',
            course_title TEXT DEFAULT '',
            course_link TEXT DEFAULT '',
            course_provider TEXT DEFAULT '',
            challenge_question TEXT NOT NULL,
            challenge_options TEXT NOT NULL,
            correct_index INTEGER NOT NULL,
            is_unlocked INTEGER DEFAULT 0,
            is_completed INTEGER DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    """)

    # ترحيل الأعمدة الجديدة لو كان الجدول منشأ مسبقاً. لا نحذف أو نعيد إنشاء بيانات الطلاب.
    cur.execute("PRAGMA table_info(blueprint_nodes)")
    b_cols = [col[1] for col in cur.fetchall()]
    for col_name in ["timeframe", "course_title", "course_link", "course_provider", "learning_plan", "progress_json"]:
        if col_name not in b_cols:
            cur.execute(f"ALTER TABLE blueprint_nodes ADD COLUMN {col_name} TEXT DEFAULT ''")

    # Private admin/analytics tables. No passwords, API keys, or auth tokens are stored in clear text.
    cur.execute("PRAGMA table_info(users)")
    user_cols = [c[1] for c in cur.fetchall()]
    for col_name, col_def in [
        ("last_login_at", "TEXT DEFAULT ''"),
        ("login_count", "INTEGER DEFAULT 0"),
        ("last_seen_at", "TEXT DEFAULT ''"),
        ("last_platform", "TEXT DEFAULT ''"),
    ]:
        if col_name not in user_cols:
            cur.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_def}")
    # Stable public account ID + moderation fields.
    cur.execute("PRAGMA table_info(users)")
    user_cols = [c[1] for c in cur.fetchall()]
    for col_name, col_def in [
        ("public_user_id", "TEXT DEFAULT ''"),
        ("account_status", "TEXT DEFAULT 'active'"),
        ("ban_reason", "TEXT DEFAULT ''"),
        ("banned_at", "TEXT DEFAULT ''"),
    ]:
        if col_name not in user_cols:
            cur.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_def}")


    # Existing users get a stable public ID once, after all legacy migrations.
    cur.execute("PRAGMA table_info(users)")
    final_user_cols = {c[1] for c in cur.fetchall()}
    if "public_user_id" in final_user_cols:
        rows = cur.execute("SELECT id, public_user_id FROM users ORDER BY id").fetchall()
        for row in rows:
            if not row[1]:
                code = "STX-" + secrets.token_hex(5).upper()
                cur.execute("UPDATE users SET public_user_id=? WHERE id=?", (code, row[0]))

    cur.execute("""CREATE TABLE IF NOT EXISTS admin_sessions (
        token_hash TEXT PRIMARY KEY, created_at TEXT NOT NULL, expires_at TEXT NOT NULL
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS user_sessions (
        token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS site_visits (
        id INTEGER PRIMARY KEY AUTOINCREMENT, visitor_hash TEXT NOT NULL, user_id INTEGER,
        path TEXT DEFAULT '/', first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, visit_count INTEGER DEFAULT 1,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_site_visits_last_seen ON site_visits(last_seen_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_site_visits_user ON site_visits(user_id)")
    cur.execute("""CREATE TABLE IF NOT EXISTS admin_audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, event_type TEXT NOT NULL,
        details TEXT DEFAULT '', created_at TEXT NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id)
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_created ON admin_audit_log(created_at)")
    

    # -------------------------------------------------------------------------
    # Unified Phase 2/3 study platform tables
    # -------------------------------------------------------------------------
    cur.execute("""CREATE TABLE IF NOT EXISTS study_materials (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        filename TEXT DEFAULT '',
        source_type TEXT DEFAULT 'pdf',
        text_content TEXT DEFAULT '',
        summary TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_materials_user ON study_materials(user_id)")

    cur.execute("""CREATE TABLE IF NOT EXISTS assessments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        material_id INTEGER,
        mode TEXT NOT NULL,
        question_types TEXT DEFAULT '',
        question_count INTEGER DEFAULT 0,
        language TEXT DEFAULT 'ar',
        status TEXT DEFAULT 'ready',
        score REAL DEFAULT 0,
        max_score REAL DEFAULT 0,
        created_at TEXT NOT NULL,
        completed_at TEXT DEFAULT '',
        FOREIGN KEY(user_id) REFERENCES users(id),
        FOREIGN KEY(material_id) REFERENCES study_materials(id)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS assessment_questions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        assessment_id INTEGER NOT NULL,
        question_number INTEGER NOT NULL,
        question_type TEXT NOT NULL,
        question_text TEXT NOT NULL,
        options_json TEXT DEFAULT '[]',
        correct_answer TEXT DEFAULT '',
        explanation TEXT DEFAULT '',
        rubric_json TEXT DEFAULT '{}',
        source_refs TEXT DEFAULT '',
        FOREIGN KEY(assessment_id) REFERENCES assessments(id)
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_questions_assessment ON assessment_questions(assessment_id)")

    cur.execute("""CREATE TABLE IF NOT EXISTS assessment_submissions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        assessment_id INTEGER NOT NULL,
        question_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        answer_text TEXT DEFAULT '',
        answer_file TEXT DEFAULT '',
        score REAL DEFAULT 0,
        max_score REAL DEFAULT 1,
        feedback TEXT DEFAULT '',
        graded_at TEXT NOT NULL,
        FOREIGN KEY(assessment_id) REFERENCES assessments(id),
        FOREIGN KEY(question_id) REFERENCES assessment_questions(id),
        FOREIGN KEY(user_id) REFERENCES users(id)
    )""")

    cur.execute("""CREATE TABLE IF NOT EXISTS flashcards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        material_id INTEGER,
        front TEXT NOT NULL,
        back TEXT NOT NULL,
        due_at TEXT NOT NULL,
        ease REAL DEFAULT 2.5,
        interval_days INTEGER DEFAULT 1,
        repetitions INTEGER DEFAULT 0,
        last_result TEXT DEFAULT '',
        FOREIGN KEY(user_id) REFERENCES users(id),
        FOREIGN KEY(material_id) REFERENCES study_materials(id)
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_flashcards_due ON flashcards(user_id,due_at)")

    cur.execute("""CREATE TABLE IF NOT EXISTS study_notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        material_id INTEGER,
        title TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )""")

    cur.execute("""CREATE TABLE IF NOT EXISTS study_weaknesses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        topic TEXT NOT NULL,
        score REAL DEFAULT 0,
        evidence TEXT DEFAULT '',
        recommended_action TEXT DEFAULT '',
        updated_at TEXT NOT NULL,
        UNIQUE(user_id,topic),
        FOREIGN KEY(user_id) REFERENCES users(id)
    )""")

    cur.execute("""CREATE TABLE IF NOT EXISTS knowledge_nodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        material_id INTEGER,
        title TEXT NOT NULL,
        prerequisite TEXT DEFAULT '',
        relation TEXT DEFAULT 'depends_on',
        mastery REAL DEFAULT 0,
        created_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id),
        FOREIGN KEY(material_id) REFERENCES study_materials(id)
    )""")

    cur.execute("""CREATE TABLE IF NOT EXISTS tutor_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        material_id INTEGER,
        mode TEXT DEFAULT 'source',
        strategy TEXT DEFAULT 'normal',
        created_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id),
        FOREIGN KEY(material_id) REFERENCES study_materials(id)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS tutor_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        material_id INTEGER,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(session_id) REFERENCES tutor_sessions(id),
        FOREIGN KEY(user_id) REFERENCES users(id),
        FOREIGN KEY(material_id) REFERENCES study_materials(id)
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_tutor_messages_session ON tutor_messages(session_id,id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_tutor_sessions_user ON tutor_sessions(user_id,created_at)")

    conn.commit()
    conn.close()

init_db()

def get_db():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

companion_auth.migrate(get_db)
ALLOW_PASSWORDLESS_LEGACY = os.environ.get("ALLOW_PASSWORDLESS_LEGACY", "true").lower() != "false"


# -----------------------------------------------------------------------------
# Telegram Bot helpers + 15-minute reminders
# -----------------------------------------------------------------------------
def telegram_configured() -> bool:
    return bool(TELEGRAM_BOT_TOKEN)

def telegram_api(method: str, data: dict | None = None, timeout: int = 30) -> dict:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    payload = urllib.parse.urlencode(data or {}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if not body.get("ok"):
        raise RuntimeError(body.get("description", f"Telegram API error in {method}"))
    return body

def telegram_send(chat_id: str, message: str):
    if chat_id and telegram_configured():
        telegram_api("sendMessage", {
            "chat_id": str(chat_id),
            "text": message,
            "disable_web_page_preview": "true",
        })

def create_telegram_link_token(user_id: int) -> str:
    token = secrets.token_urlsafe(18)
    expires = (datetime.now(APP_TZ) + timedelta(minutes=15)).isoformat()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM telegram_link_tokens WHERE user_id = ?", (user_id,))
    cur.execute(
        "INSERT INTO telegram_link_tokens(token, user_id, expires_at) VALUES(?,?,?)",
        (token, user_id, expires),
    )
    conn.commit()
    conn.close()
    return token

def consume_telegram_link_token(token: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT token,user_id,expires_at FROM telegram_link_tokens WHERE token = ?", (token.strip(),))
    row = cur.fetchone()
    if not row:
        conn.close()
        return None
    try:
        expires = datetime.fromisoformat(row["expires_at"])
    except Exception:
        expires = datetime.now(APP_TZ) - timedelta(minutes=1)
    if datetime.now(APP_TZ) >= expires:
        cur.execute("DELETE FROM telegram_link_tokens WHERE token = ?", (token,))
        conn.commit()
        conn.close()
        return None
    cur.execute("DELETE FROM telegram_link_tokens WHERE token = ?", (token,))
    cur.execute("SELECT * FROM users WHERE id = ?", (row["user_id"],))
    user = cur.fetchone()
    conn.commit()
    conn.close()
    return user

def parse_event_date(value: str) -> str:
    value = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return ""

def parse_event_time(value: str) -> str:
    value = str(value or "").strip().replace(".", ":")
    for fmt in ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I %p", "%H"):
        try:
            return datetime.strptime(value, fmt).strftime("%H:%M")
        except ValueError:
            pass
    return ""

def add_schedule_event(user_id: int, title: str, target_date: str, target_time: str = "", source: str = "manual", external_id: str = ""):
    d = parse_event_date(target_date)
    t = parse_event_time(target_time)
    title = str(title or "").strip()
    if not title or not d:
        return None, False
    conn = get_db()
    cur = conn.cursor()
    if external_id:
        cur.execute("SELECT id FROM schedule_items WHERE user_id=? AND external_id=?", (user_id, external_id))
        old = cur.fetchone()
        if old:
            conn.close()
            return int(old["id"]), False
    cur.execute(
        """INSERT INTO schedule_items
           (user_id,title,target_date,target_time,is_completed,source,external_id,reminder_sent)
           VALUES(?,?,?,?,0,?,?,0)""",
        (user_id, title, d, t, source, external_id),
    )
    item_id = cur.lastrowid
    conn.commit()
    conn.close()
    return int(item_id), True

def parse_telegram_text_events(message_text: str):
    now_local = datetime.now(APP_TZ)
    prompt = f"""
You are the calendar parser for S-Tesseract.
Current local datetime: {now_local.strftime('%Y-%m-%d %H:%M')} ({APP_TIMEZONE_NAME})
User message: {message_text!r}

Extract ONLY events the user clearly wants in their calendar:
- exams
- lectures/classes
- assignments/deadlines
- study sessions
- appointments
- reminders

Understand Arabic and English.
Resolve relative dates like today/tomorrow/next Monday using the current date.
Do not invent missing dates or times.
Return JSON only:
{{"events":[{{"title":"...","date":"YYYY-MM-DD","time":"HH:MM"}}]}}
Use an empty string for time if no time is given.
"""
    return generate_ai_json(prompt).get("events", [])

def parse_telegram_pdf_events(pdf_bytes: bytes, filename: str):
    now_local = datetime.now(APP_TZ)
    prompt = f"""
You are extracting calendar events from an uploaded study PDF.
Current local datetime: {now_local.strftime('%Y-%m-%d %H:%M')} ({APP_TIMEZONE_NAME})
Filename: {filename!r}

Extract only explicit calendar information from the PDF.
Possible items: exams, lectures, assignments, deadlines, classes, appointments.
Do not invent missing dates/times.
Return JSON only:
{{"events":[{{"title":"...","date":"YYYY-MM-DD","time":"HH:MM"}}]}}
Use an empty string for time if the PDF has a date but no time.
"""
    contents = [
        types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
        prompt,
    ]
    return generate_ai_json(prompt, contents).get("events", [])

def process_telegram_text(user_id: int, chat_id: str, text_value: str):
    events = parse_telegram_text_events(text_value)
    created = []
    for i, event in enumerate(events if isinstance(events, list) else []):
        if not isinstance(event, dict):
            continue
        title = event.get("title", "")
        d = event.get("date", "")
        t = event.get("time", "")
        _, inserted = add_schedule_event(
            user_id, title, d, t, "telegram",
            f"telegram:{chat_id}:{int(time.time())}:{i}"
        )
        if inserted:
            created.append((title, d, t))
    if not created:
        return "ملقتش موعد واضح. جرّب: Math exam tomorrow at 9 PM"
    return "تمت إضافة المواعيد ✅\n" + "\n".join(
        f"• {title} — {parse_event_date(d)} {parse_event_time(t) or 'بدون وقت'}"
        for title, d, t in created
    )

def telegram_get_file_bytes(file_id: str) -> bytes:
    meta = telegram_api("getFile", {"file_id": file_id})
    path = meta.get("result", {}).get("file_path")
    if not path:
        raise RuntimeError("Telegram did not return the file path")
    url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{path}"
    with urllib.request.urlopen(url, timeout=60) as resp:
        return resp.read()

def process_telegram_pdf(user_id: int, chat_id: str, pdf_bytes: bytes, filename: str, message_id: int):
    events = parse_telegram_pdf_events(pdf_bytes, filename)
    created = []
    for i, event in enumerate(events if isinstance(events, list) else []):
        if not isinstance(event, dict):
            continue
        title = event.get("title", "")
        d = event.get("date", "")
        t = event.get("time", "")
        _, inserted = add_schedule_event(
            user_id, title, d, t, "telegram_pdf",
            f"telegram-pdf:{chat_id}:{message_id}:{i}"
        )
        if inserted:
            created.append((title, d, t))
    if not created:
        return "استلمت الـPDF، لكن ملقتش مواعيد واضحة أقدر أضيفها."
    return f"اتحلل الـPDF واتضاف {len(created)} موعد ✅\n" + "\n".join(
        f"• {title} — {parse_event_date(d)} {parse_event_time(t) or 'بدون وقت'}"
        for title, d, t in created[:20]
    )

def process_due_telegram_reminders():
    if not telegram_configured():
        return
    now = datetime.now(APP_TZ).replace(second=0, microsecond=0)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT s.id,s.title,s.target_date,s.target_time,s.reminder_sent,u.telegram_chat_id
        FROM schedule_items s JOIN users u ON u.id=s.user_id
        WHERE s.target_time!='' AND s.is_completed=0 AND s.reminder_sent=0
              AND u.telegram_chat_id!=''
    """)
    rows = cur.fetchall()
    conn.close()
    for row in rows:
        try:
            event_dt = datetime.strptime(
                f"{row['target_date']} {row['target_time']}", "%Y-%m-%d %H:%M"
            ).replace(tzinfo=APP_TZ)
        except ValueError:
            continue
        mins = (event_dt - now).total_seconds() / 60
        if 14 <= mins <= 16.5:
            telegram_send(
                str(row["telegram_chat_id"]),
                f"⏰ تذكير قبل الموعد بربع ساعة\n\n{row['title']}\n📅 {row['target_date']}\n🕒 {row['target_time']}"
            )
            conn2 = get_db()
            cur2 = conn2.cursor()
            cur2.execute("UPDATE schedule_items SET reminder_sent=1 WHERE id=?", (row["id"],))
            conn2.commit()
            conn2.close()

async def telegram_worker():
    offset = 0
    while True:
        try:
            updates = await asyncio.to_thread(
                lambda: telegram_api(
                    "getUpdates",
                    {"offset": str(offset), "timeout": "25", "allowed_updates": json.dumps(["message"])},
                    timeout=35,
                ).get("result", [])
            )
            for update in updates:
                offset = max(offset, int(update.get("update_id", 0)) + 1)
                message = update.get("message") or {}
                chat = message.get("chat") or {}
                chat_id = str(chat.get("id", ""))
                text_value = (message.get("text") or "").strip()
                doc = message.get("document") or {}
                if not chat_id:
                    continue
                if text_value.startswith("/start"):
                    parts = text_value.split(maxsplit=1)
                    token = parts[1].strip() if len(parts) > 1 else ""
                    user = consume_telegram_link_token(token) if token else None
                    if user:
                        conn = get_db()
                        conn.execute("UPDATE users SET telegram_chat_id=? WHERE id=?", (chat_id, user["id"]))
                        conn.commit()
                        conn.close()
                        telegram_send(chat_id, "تم ربط Telegram بنجاح ✅\nابعت مواعيد أو PDF فيه جدول، وهضيفهم للـCalendar.")
                    else:
                        telegram_send(chat_id, "الرابط غير صالح أو انتهت صلاحيته. اعمل Link Telegram من S-Tesseract.")
                    continue
                conn = get_db()
                cur = conn.cursor()
                cur.execute("SELECT * FROM users WHERE telegram_chat_id=?", (chat_id,))
                user = cur.fetchone()
                conn.close()
                if not user:
                    telegram_send(chat_id, "الحساب غير مربوط. استخدم رابط Link Telegram من المنصة.")
                    continue
                if doc:
                    mime = doc.get("mime_type", "")
                    name = doc.get("file_name", "uploaded.pdf")
                    if mime == "application/pdf" or name.lower().endswith(".pdf"):
                        pdf_bytes = await asyncio.to_thread(telegram_get_file_bytes, doc.get("file_id", ""))
                        result = await asyncio.to_thread(process_telegram_pdf, int(user["id"]), chat_id, pdf_bytes, name, int(message.get("message_id", 0)))
                        telegram_send(chat_id, result)
                        continue
                if text_value:
                    result = await asyncio.to_thread(process_telegram_text, int(user["id"]), chat_id, text_value)
                    telegram_send(chat_id, result)
        except asyncio.CancelledError:
            raise
        except Exception:
            traceback.print_exc()
            await asyncio.sleep(3)

async def reminder_worker():
    while True:
        try:
            await asyncio.to_thread(process_due_telegram_reminders)
        except asyncio.CancelledError:
            raise
        except Exception:
            traceback.print_exc()
        await asyncio.sleep(30)

TELEGRAM_TASKS = []

@app.on_event("startup")
async def startup_workers():
    if TELEGRAM_BOT_TOKEN:
        TELEGRAM_TASKS.append(asyncio.create_task(reminder_worker()))
        if TELEGRAM_MODE == "polling":
            TELEGRAM_TASKS.append(asyncio.create_task(telegram_worker()))

@app.on_event("shutdown")
async def shutdown_workers():
    for task in TELEGRAM_TASKS:
        task.cancel()
    if TELEGRAM_TASKS:
        await asyncio.gather(*TELEGRAM_TASKS, return_exceptions=True)
    TELEGRAM_TASKS.clear()

# -----------------------------------------------------------------------------
# Gemini JSON helper
# -----------------------------------------------------------------------------
def strip_json_fences(text: str) -> str:
    """Clean model output so JSON parsing is resilient."""
    if not text:
        return ""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()

def generate_ai_json(prompt: str, contents: list | None = None, models: list | None = None) -> dict:
    """Generate strict JSON with model fallbacks and retry handling."""
    req_contents = contents if contents else [prompt]
    models = models or [
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.5-flash-lite",
        "gemini-2.5-flash",
    ]
    retryable = (
        "503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED",
        "500", "INTERNAL", "HIGH DEMAND", "TEMPORARILY UNAVAILABLE"
    )
    last_err = None

    for model_name in models:
        for attempt in range(3):
            try:
                response = ai.models.generate_content(
                    model=model_name,
                    contents=req_contents,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json"
                    ),
                )
                raw = getattr(response, "text", None) or ""
                cleaned = strip_json_fences(raw)
                if not cleaned:
                    raise ValueError("Gemini returned an empty response.")
                data = json.loads(cleaned)
                if not isinstance(data, dict):
                    raise ValueError("Gemini returned JSON, but it was not an object.")
                return data
            except Exception as exc:
                last_err = exc
                msg = str(exc)
                upper = msg.upper()
                print(f"[generate_ai_json] model={model_name} attempt={attempt + 1} failed: {msg}")

                # A missing/unsupported model should move to the next fallback immediately.
                if "404" in msg or "NOT_FOUND" in upper or "INVALID_ARGUMENT" in upper:
                    break

                if not any(token in upper for token in retryable):
                    # Bad JSON or another non-transient request error is not helped by
                    # repeating the exact same request. Move to the next model.
                    break

                delay = min(12.0, 1.5 * (2 ** attempt))
                time.sleep(delay)

    raise RuntimeError(
        "AI generation failed after trying all configured Gemini models. "
        f"Last error: {last_err}"
    )

# -----------------------------------------------------------------------------
# Schemas
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
class AuthRequest(BaseModel):
    username: str
    field: str = ""
    password: str = ""

class TaskRequest(BaseModel):
    user_id: int
    title: str
    target_date: str
    target_time: str = ""

class TaskDeleteRequest(BaseModel):
    schedule_id: int

class TaskToggleRequest(BaseModel):
    schedule_id: int
    is_completed: int

class TodoRequest(BaseModel):
    user_id: int
    task: str

class TodoToggleRequest(BaseModel):
    todo_id: int
    is_done: int

class ResultRequest(BaseModel):
    user_id: int
    earned_xp: int
    passed: int
    node_id: int = 0

class RoadmapPromptRequest(BaseModel):
    user_id: int
    goal: str
    duration: str = "شهر"
    daily_minutes: int = 60
    study_days_per_week: int = 5
    lang: str = "ar"

# -----------------------------------------------------------------------------
# Endpoints
# -----------------------------------------------------------------------------
@app.post("/api/auth/guest")
def auth_guest(request: Request):
    """Create or restore a browser-scoped guest session without asking for credentials.

    Guest sessions use the existing users/user_sessions tables so existing user-scoped
    features continue to work without weakening authentication for registered accounts.
    """
    # Reuse a valid existing session if the browser already has one.
    existing_uid = _current_user_id(request)
    if existing_uid:
        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE id=?", (existing_uid,)).fetchone()
        conn.close()
        if user and (user["account_status"] or "active").lower() == "active":
            response = JSONResponse(content={**companion_auth.public_user(user), "guest": True})
            token = request.cookies.get(USER_COOKIE, "")
            if token:
                _set_user_session_cookie(response, request, token)
            return response

    conn = get_db()
    cur = conn.cursor()
    now = datetime.now(APP_TZ)
    now_iso = now.isoformat()
    # Random unique username; collision retry is safe and does not alter existing users.
    user = None
    for _ in range(8):
        username = "Guest" + str(secrets.randbelow(900000) + 100000)
        if not cur.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            cur.execute("""INSERT INTO users
                (username, field_of_study, role_model, xp, streak, last_active_date,
                 tests_taken, tests_passed, created_at, public_user_id, account_status,
                 password_hash, password_set_at)
                VALUES (?, 'General', '', 0, 0, ?, 0, 0, ?, ?, 'active', '', '')""",
                (username, str(date.today()), now_iso, "GST-" + secrets.token_hex(5).upper()))
            uid = cur.lastrowid
            conn.commit()
            user = cur.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            break
    if not user:
        conn.close()
        raise HTTPException(status_code=503, detail="Could not create a guest session. Please retry.")

    platform = "mobile" if re.search(r"Android|iPhone|iPad|Mobile", request.headers.get("user-agent", ""), re.I) else "desktop"
    cur.execute("UPDATE users SET last_login_at=?, last_seen_at=?, last_platform=? WHERE id=?",
                (now_iso, now_iso, platform, user["id"]))
    token = secrets.token_urlsafe(32)
    cur.execute("INSERT INTO user_sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)",
                (_hash_token(token), user["id"], now_iso, (now + timedelta(days=30)).isoformat()))
    conn.commit()
    conn.close()
    response = JSONResponse(content={**companion_auth.public_user(user), "guest": True})
    _set_user_session_cookie(response, request, token)
    return response


@app.post("/api/auth/register-or-login")
def auth_user(payload: AuthRequest, request: Request):
    clean_username = payload.username.strip()
    if not clean_username:
        raise HTTPException(status_code=400, detail="Username is required")
        
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE username = ?", (clean_username,))
    user = cur.fetchone()
    today_str = str(date.today())
    
    if not user:
        # New accounts must choose a password up front.
        pw_err = companion_auth.validate_new_password(payload.password, username=clean_username)
        if pw_err:
            conn.close()
            raise HTTPException(status_code=400, detail=pw_err)
        public_id = "STX-" + secrets.token_hex(5).upper()
        cur.execute("""
            INSERT INTO users (username, field_of_study, role_model, xp, streak, last_active_date, tests_taken, tests_passed, created_at, public_user_id, account_status, password_hash, password_set_at)
            VALUES (?, ?, '', 0, 0, ?, 0, 0, ?, ?, 'active', ?, ?)
        """, (clean_username, payload.field.strip(), today_str, datetime.now().isoformat(), public_id,
              companion_auth.hash_password(payload.password), datetime.now(APP_TZ).isoformat()))
        user_id = cur.lastrowid
        conn.commit()
        cur.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        user = cur.fetchone()

    if user["password_hash"]:
        # Accounts with a password can never be entered by name alone.
        try:
            companion_auth.check_password_with_lockout(conn, user, payload.password, APP_TZ, _audit)
        except HTTPException as e:
            conn.close()
            d = e.detail if isinstance(e.detail, dict) else {}
            if d.get("code") == "locked":
                raise HTTPException(status_code=429, detail=f"محاولات كتير غلط. جرّب بعد {d.get('minutes', 15)} دقيقة.")
            raise HTTPException(status_code=401, detail="الاسم أو كلمة المرور غير صحيحة.")
    elif not ALLOW_PASSWORDLESS_LEGACY:
        conn.close()
        raise HTTPException(status_code=403, detail="الحساب ده محتاج كلمة مرور. اطلب من الإدارة إعادة تفعيله.")

    if (user["account_status"] or "active") != "active":
        conn.close()
        raise HTTPException(status_code=403, detail=(user["ban_reason"] or "This account is not active."))
    
    # Record a real login/session so the private dashboard can show active users.
    now_iso = datetime.now(APP_TZ).isoformat()
    platform = "mobile" if re.search(r"Android|iPhone|iPad|Mobile", request.headers.get("user-agent", ""), re.I) else "desktop"
    cur.execute("UPDATE users SET last_login_at=?, login_count=COALESCE(login_count,0)+1, last_seen_at=?, last_platform=? WHERE id=?",
                (now_iso, now_iso, platform, user["id"]))
    conn.commit()
    cur.execute("SELECT * FROM users WHERE id=?", (user["id"],))
    user = cur.fetchone()
    session_token = secrets.token_urlsafe(32)
    cur.execute("INSERT OR REPLACE INTO user_sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)",
                (_hash_token(session_token), user["id"], now_iso, (datetime.now(APP_TZ)+timedelta(days=30)).isoformat()))
    # Re-use the same small table for user sessions; the prefix keeps admin tokens separate.
    conn.commit()
    conn.close()
    _audit(user["id"], "login", f"platform={platform}")
    response = JSONResponse(content=companion_auth.public_user(user))
    _set_user_session_cookie(response, request, session_token)
    return response

@app.get("/api/auth/session")
def auth_session(request: Request):
    token = request.cookies.get(USER_COOKIE, "")
    if not token:
        raise HTTPException(status_code=401, detail="No active session")
    token_hash = _hash_token(token)
    now = datetime.now(APP_TZ)
    conn = get_db()
    row = conn.execute(
        "SELECT user_id, expires_at FROM user_sessions WHERE token_hash=?",
        (token_hash,)
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=401, detail="Session not found")
    try:
        expires = datetime.fromisoformat(row["expires_at"])
    except Exception:
        expires = now - timedelta(seconds=1)
    if expires <= now:
        conn.execute("DELETE FROM user_sessions WHERE token_hash=?", (token_hash,))
        conn.commit(); conn.close()
        raise HTTPException(status_code=401, detail="Session expired")
    user = conn.execute("SELECT * FROM users WHERE id=?", (row["user_id"],)).fetchone()
    if not user:
        conn.execute("DELETE FROM user_sessions WHERE token_hash=?", (token_hash,))
        conn.commit(); conn.close()
        raise HTTPException(status_code=401, detail="User not found")
    account_status = (user["account_status"] or "active").lower()
    if account_status != "active":
        conn.execute("DELETE FROM user_sessions WHERE token_hash=?", (token_hash,))
        conn.commit(); conn.close()
        reason = user["ban_reason"] if account_status == "banned" else "تم إيقاف جلستك بواسطة الإدارة."
        raise HTTPException(status_code=403, detail=reason or "Account is not active")
    new_expiry = (now + timedelta(days=30)).isoformat()
    conn.execute("UPDATE user_sessions SET expires_at=? WHERE token_hash=?", (new_expiry, token_hash))
    conn.execute("UPDATE users SET last_seen_at=? WHERE id=?", (now.isoformat(), user["id"]))
    conn.commit(); conn.close()
    response = JSONResponse(content=companion_auth.public_user(user))
    _set_user_session_cookie(response, request, token)
    return response

@app.post("/api/auth/logout")
def auth_logout(request: Request):
    token = request.cookies.get(USER_COOKIE, "")
    conn = get_db()
    if token:
        conn.execute("DELETE FROM user_sessions WHERE token_hash=?", (_hash_token(token),))
        conn.commit()
    conn.close()
    response = JSONResponse(content={"success": True})
    response.delete_cookie(USER_COOKIE, path="/")
    return response

@app.get("/api/user/{user_id}/dashboard")
def get_user_dashboard(user_id: int, request: Request):
    session_user = _current_user_id(request)
    if not session_user:
        raise HTTPException(status_code=401, detail="Login required")
    if int(session_user) != int(user_id):
        raise HTTPException(status_code=403, detail="Not your dashboard")
    conn = get_db()
    cur = conn.cursor()
    
    cur.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    user = cur.fetchone()
    if not user:
        conn.close()
        raise HTTPException(status_code=404, detail="User not found")
        
    cur.execute("SELECT * FROM schedule_items WHERE user_id = ? ORDER BY id DESC LIMIT 5", (user_id,))
    schedule = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT * FROM focus_todos WHERE user_id = ? ORDER BY id DESC", (user_id,))
    todos = [dict(r) for r in cur.fetchall()]
    
    cur.execute("SELECT * FROM blueprint_nodes WHERE user_id = ? ORDER BY node_order ASC", (user_id,))
    nodes = [dict(r) for r in cur.fetchall()]
    for n in nodes:
        try:
            n["challenge_options"] = json.loads(n.get("challenge_options") or "[]")
        except Exception:
            n["challenge_options"] = []
        try:
            n["learning_plan"] = json.loads(n.get("learning_plan") or "{}")
        except Exception:
            n["learning_plan"] = {}
        try:
            n["progress_json"] = json.loads(n.get("progress_json") or "[]")
        except Exception:
            n["progress_json"] = []
        
    cur.execute("SELECT username, field_of_study, xp, streak, public_user_id FROM users WHERE COALESCE(account_status, 'active')='active' ORDER BY xp DESC, id ASC LIMIT 100")
    leaderboard = [dict(r) for r in cur.fetchall()]
    conn.close()
    
    honesty_ratio = 100
    if user["tests_taken"] > 0:
        honesty_ratio = int((user["tests_passed"] / user["tests_taken"]) * 100)
        
    total_nodes = len(nodes)
    completed_nodes = sum(1 for n in nodes if n["is_completed"])
    gps_progress = int((completed_nodes / total_nodes * 100)) if total_nodes > 0 else 0
        
    return {
        "user": companion_auth.public_user(user),
        "honesty_ratio": honesty_ratio,
        "gps_progress": gps_progress,
        "schedule": schedule,
        "todos": todos,
        "blueprint": nodes,
        "leaderboard": leaderboard
    }

@app.get("/api/leaderboard")
def get_leaderboard(request: Request):
    if not _current_user_id(request):
        raise HTTPException(status_code=401, detail="Login required")
    conn = get_db()
    rows = conn.execute("SELECT username, field_of_study, xp, streak, public_user_id FROM users WHERE COALESCE(account_status, 'active')='active' ORDER BY xp DESC, id ASC LIMIT 100").fetchall()
    conn.close()
    return {"success": True, "leaderboard": [dict(r) for r in rows]}

@app.post("/api/todos/add")
def add_focus_todo(payload: TodoRequest):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO focus_todos (user_id, task, is_done) VALUES (?, ?, 0)", (payload.user_id, payload.task.strip()))
    conn.commit()
    conn.close()
    return {"success": True}

@app.post("/api/todos/toggle")
def toggle_focus_todo(payload: TodoToggleRequest):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE focus_todos SET is_done = ? WHERE id = ?", (payload.is_done, payload.todo_id))
    conn.commit()
    conn.close()
    return {"success": True}

@app.post("/api/homework/smart-evaluate")
async def evaluate_homework_or_solve(
    user_id: int = Form(...),
    questionText: str = Form(...),
    studentSolutionText: str = Form(""),
    solutionImage: UploadFile = File(None),
    lang: str = Form("ar")
):
    try:
        clean_q = questionText.strip()
        clean_ans = studentSolutionText.strip()
        has_image = solutionImage is not None and len(solutionImage.filename) > 0
        has_student_work = bool(clean_ans or has_image)

        contents = []
        if has_image:
            img_bytes = await solutionImage.read()
            contents.append(types.Part.from_bytes(data=img_bytes, mime_type=solutionImage.content_type or "image/jpeg"))

        language_instruction = "Respond entirely in English." if lang == "en" else "أجب باللغة العربية حصراً."

        if has_student_work:
            prompt = f"""
            {language_instruction}
            Question: "{clean_q}"
            Student Solution: "{clean_ans if clean_ans else 'Attached in image'}"

            Evaluate the student's solution against the correct steps.
            Return STRICT JSON:
            {{
                "mode": "CORRECTION",
                "score": 85,
                "is_model": false,
                "verdict": "Model / Good / Incorrect",
                "earned_xp": 35,
                "detailed_feedback": "Detailed feedback",
                "model_solution": "Step by step model solution"
            }}
            """
        else:
            prompt = f"""
            {language_instruction}
            Question: "{clean_q}"
            The student did not provide a solution. Solve the problem thoroughly step-by-step with formulas.
            Return STRICT JSON:
            {{
                "mode": "STEP_BY_STEP_SOLUTION",
                "score": 0,
                "is_model": true,
                "verdict": "Step-by-Step Solution",
                "earned_xp": 0,
                "detailed_feedback": "Key insights to understand",
                "model_solution": "Thorough step-by-step solution"
            }}
            """

        contents.append(prompt)
        res_data = generate_ai_json(prompt, contents)

        if res_data.get("earned_xp", 0) > 0:
            conn = get_db()
            cur = conn.cursor()
            cur.execute("UPDATE users SET xp = xp + ? WHERE id = ?", (res_data["earned_xp"], user_id))
            conn.commit()
            conn.close()

        return JSONResponse(content={"success": True, "result": res_data})
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"error": f"Evaluation error: {str(e)}"})

# -----------------------------------------------------------------------------
# توليد مسار تعليمي مقسم بالمدة مع كورسات مجانية موثوقة واختبار مفاهيم
# -----------------------------------------------------------------------------
@app.post("/api/gps/generate-roadmap")
async def generate_gps_roadmap(payload: RoadmapPromptRequest, request: Request):
    # Roadmaps contain private study plans; only the authenticated owner may generate one.
    _require_user(request, payload.user_id)
    try:
        goal = payload.goal.strip()
        if not goal:
            return JSONResponse(status_code=400, content={"error": "Goal is required."})

        lang_note = "Respond strictly in English." if payload.lang == "en" else "اكتب بالعربية الواضحة المناسبة للطالب، مع إبقاء المصطلحات التقنية المهمة بالإنجليزية بين قوسين عند الحاجة."
        prompt = f"""
You are S-TESSERACT's Learning Path Engine. Create a usable, domain-specific learning plan, not a generic list of four topics.
{lang_note}
Student goal / subject: {json.dumps(goal, ensure_ascii=False)}
Available timeframe: {json.dumps(payload.duration, ensure_ascii=False)}
Available study time: {max(15, min(480, int(payload.daily_minutes)))} minutes per study day, {max(1, min(7, int(payload.study_days_per_week)))} days per week. Weekly budget: {max(15, min(480, int(payload.daily_minutes))) * max(1, min(7, int(payload.study_days_per_week)))} minutes.

First classify the goal into one dominant discipline: coding/software, mathematics, physics/science, language learning, engineering/hardware, or theory/research/other. Choose an activity design that fits that discipline. Do not force coding exercises onto theory subjects or generic reading onto coding subjects.

Build 4 sequential milestones that fit BOTH the overall timeframe and actual daily/weekly study budget. Allocate realistic minutes to activities and resources; never assign more time than the budget permits. Keep the original roadmap structure: four connected milestones, activities, projects/deliverables, evaluation, quizzes, and review.

DAILY LEARNING IS REQUIRED: In EACH milestone, create 3-7 ordered dailyLessons (depending on total timeframe and realistic daily availability). Each daily lesson must name exactly what to study TODAY: a specific video/lecture topic and searchQuery, a specific book chapter/section or written reference and searchQuery, one practical task, and a same-day quiz built ONLY from the concepts taught in that lesson. Each lesson must contain 3-5 questions, each with four options, correctIndex, hint, explanation, solutionSteps, remediation, conceptTested, questionType. Questions must not test untaught future concepts. Set estimatedMinutes for every component. The total required time for that day's video + reading + practice + quiz/review must be <= the student's daily_minutes. Optional resources do not count against that budget and must be labelled optional. Use exact chapter/section names when reliable; never invent page numbers or editions. The student should not have to search for what to study: provide a precise title and targeted query for each resource, and use a direct URL only when known and reliable.

REAL-WORK READINESS: Include a professionalWorkSimulation for each milestone or a substantial capstone spanning milestones. This must resemble a real task from the target role, not a toy Kaggle-only exercise: describe stakeholder/context, realistic data/artifacts, incomplete or messy requirements, constraints, deliverables, acceptance criteria, testing/quality checks, and a rubric. Prefer real public datasets/repos/documents/APIs with legal access and credible provenance; if real confidential data is unavailable, explicitly use realistic synthetic/sanitized data and explain the limitation. Do not claim access to private company data. Add a finalJobReadinessAssessment with a novel task, explicit rubric, pass threshold, and required artifacts. Distinguish course completion from evidence of job readiness; never guarantee employment.

RESOURCE QUALITY: recommend 3-5 useful, complementary learning resources per milestone, but fit their combined planned use into the student's real time budget. Do NOT prescribe a full course or playlist as the default task. Start with one focused video or a small set of short videos, then a relevant written reference (official docs, textbook section, university notes, trusted tutorial), then a worked example or practice resource. Include different resource types rather than videos alone. Choose based on topic match, learner level, clarity, credibility, recency where relevant, and actual time cost. Each resource must include resourceType (video/reference/practice/documentation/book/chapter/article), estimatedMinutes, provider, title, a specific searchQuery, and why it is worth the learner's time. Explicitly prioritize only the most useful items that fit the session; label optional deeper resources separately and do not count them inside the required session. For low daily time prioritize one short focused video plus one concise reference and a tiny practice task. Do NOT invent direct URLs, video IDs, durations, popularity, or claim verification without evidence. The app opens targeted search results when a direct URL is unavailable.

IMPORTANT PROJECT-BASED ROADMAP RULES:
- Design the roadmap as a connected learning journey, not four unrelated lists. Explain which skills each milestone teaches and how those skills are used in its project.
- For coding/software goals, every milestone must include at least one practical implementation task that becomes part of a named project. Across the 4 milestones, build toward a final portfolio-ready project. Include editor tasks, expected behavior, test cases (normal, edge, and invalid inputs where relevant), debugging checkpoints, and a short reflection on the student's own errors. Do not give the student only passive videos or reading.
- For mathematics, physics, engineering, languages, research, arts, and all other disciplines, connect each concept to an appropriate artifact or project: worked problem set, simulation, experiment report, design prototype, presentation, case study, writing sample, or other observable output. Do not force programming when it is not relevant.
- Each activity must say which project/component it advances, what the learner must submit, and how success will be checked. Projects should grow in complexity across milestones and reuse earlier work where appropriate.
- Include a clear progression in every milestone: (1) a focused video/lesson first, (2) a relevant reference or documentation page, (3) an applied task suited to the discipline, (4) an AI-designed assessment carefully aligned to only the taught concepts, (5) actionable feedback and a step-by-step solution available when the learner chooses to solve/review, then (6) a next action selected from the result: targeted remediation resource, easier practice, another example, or unlock the next milestone. Keep the workload realistic for the stated timeframe and level. The assessment must test understanding and transfer, not trick wording; include a mix of conceptual and practical questions appropriate to the discipline, plausible distractors, correct answer rationale, and explicit mapping to milestone concepts. Do not expose answer keys before the assessment is submitted.

Discipline rules:
- coding/software: short implementation labs, runnable deliverables, test cases including edge cases, debugging prompts, tools/libraries only when relevant, a small project; never claim code has been run by this app unless a real runner is connected.
- mathematics/physics/science: named concepts, graduated problem sets, show-your-work tasks, units/assumptions where relevant, a way to check reasoning and diagnose the first incorrect step; simulation only if useful.
- language: listening/reading/writing/speaking or grammar activities as relevant, a concrete output, and a feedback rubric.
- engineering/hardware: design constraints, diagrams/calculations, parts/tools only when appropriate, safe bench/simulation work, and a measurable prototype/report deliverable.
- theory/research: readings, specific chapters/sections when reliably known (otherwise say to verify edition/page), retrieval practice, concept maps/case studies, and evidence-based written output.

For every milestone provide:
1) title, timeframe, objective, prerequisites, core concepts.
2) activityType chosen from Coding Lab, Problem Solver, Language Practice, Engineering Design, Reading & References, Case Study, or Mixed Practice.
3) 3-5 activities, each with title, exact instructions, deliverable, estimatedMinutes, and optional tools.
4) 3-5 complementary resources with title, provider, resource type, specific search query, estimated time, required/optional status, and why relevant. Keep required resource use within the daily budget; label optional deep dives clearly.
5) 3-5 evaluation criteria with observable evidence; masteryThreshold as integer percent from 60 to 100.
6) 4-6 meaningful assessment questions (or fewer only when the session is very short), with question, 4 options, correctIndex, explanation, hint, solutionSteps, remediation, conceptTested, and questionType. Aim for balanced medium difficulty: accessible but not trivial; test understanding and application, include realistic scenarios, common mistakes, interpretation/debugging/calculation when relevant, and avoid trick wording. Ensure exactly one defensible best answer. Hints should guide thinking without revealing the answer. Provide concise step-by-step solutions and targeted remediation after submission, not before.
7) a miniProject or concrete deliverable, commonMistakes (2-4), reviewPlan, and nextStep.

Return strict JSON only with this schema:
{{
 "trackSummary":"...",
 "discipline":"one of the discipline labels above",
 "assumptions":["..."],
 "finalJobReadinessAssessment":{{"task":"...","deliverables":["..."],"rubric":[{{"criterion":"...","weight":25}}],"passThreshold":80}},
 "nodes":[{{
  "title":"...","timeframe":"...","objective":"...","prerequisites":["..."],
  "coreConcepts":["..."],"activityType":"...",
  "activities":[{{"title":"...","instructions":"...","deliverable":"...","estimatedMinutes":30,"tools":["..."]}}],
  "dailyLessons":[{{"dayNumber":1,"title":"...","objective":"...","estimatedMinutes":180,"video":{{"title":"...","provider":"...","searchQuery":"...","estimatedMinutes":25}},"reading":{{"title":"...","provider":"...","resourceType":"book|chapter|reference|documentation|article","searchQuery":"...","estimatedMinutes":20}},"practice":{{"title":"...","instructions":"...","deliverable":"...","estimatedMinutes":90}},"quizMinutes":20,"quizQuestions":[{{"question":"...","conceptTested":"...","questionType":"conceptual|application|debugging|calculation|case-study","options":["...","...","...","..."],"correctIndex":0,"hint":"...","explanation":"...","solutionSteps":["..."],"remediation":"..."}}]}}],
  "professionalWorkSimulation":{{"scenario":"...","role":"...","inputsOrData":"...","datasetOrArtifactName":"...","sourceUrl":"...","sourceSearchQuery":"...","licenseOrAccess":"...","fallbackIfUnavailable":"...","constraints":["..."],"deliverables":["..."],"acceptanceCriteria":["..."],"rubric":[{{"criterion":"...","weight":25}}],"dataProvenance":"real-public|synthetic-realistic|open-source-project"}},
  "resources":[{{"title":"...","provider":"...","resourceType":"video|reference|documentation|book|chapter|article|practice","estimatedMinutes":15,"searchQuery":"...","why":"...","requiredOrOptional":"required|optional"}}],
  "evaluation":{{"criteria":["..."],"masteryThreshold":80}},
  "quizQuestions":[{{"question":"...","conceptTested":"...","questionType":"conceptual|application|debugging|calculation|case-study","options":["...","...","...","..."],"correctIndex":0,"hint":"...","explanation":"...","solutionSteps":["..."],"remediation":"..."}}],
  "miniProject":"...","commonMistakes":["..."],"reviewPlan":"...","nextStep":"...",
  "challengeQuestion":"...","challengeOptions":["...","...","...","..."],"correctIndex":0
 }}]
}}
Return exactly 4 nodes. All content must be internally consistent and appropriate to the goal/timeframe. No markdown fences.
"""
        res_data = generate_ai_json(prompt)
        raw_nodes = res_data.get("nodes", [])
        if not isinstance(raw_nodes, list) or not raw_nodes:
            raise ValueError("AI response did not include any roadmap nodes. Try rephrasing your goal.")

        conn = get_db()
        cur = conn.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS roadmap_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            goal TEXT NOT NULL DEFAULT '',
            duration TEXT NOT NULL DEFAULT '',
            lang TEXT NOT NULL DEFAULT 'ar',
            roadmap_json TEXT NOT NULL DEFAULT '{}',
            node_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )""")

        # Replace only the active path. Historical snapshots remain untouched.
        cur.execute("DELETE FROM blueprint_nodes WHERE user_id = ?", (payload.user_id,))
        inserted = 0
        normalized_nodes = []
        for idx, node in enumerate(raw_nodes[:4]):
            if not isinstance(node, dict):
                continue
            title = str(node.get("title") or "").strip() or f"المرحلة {idx + 1}"
            objective = str(node.get("objective") or "هدف هذه المرحلة")
            timeframe = str(node.get("timeframe") or f"المرحلة {idx + 1}")
            resources = node.get("resources") if isinstance(node.get("resources"), list) else []
            first_resource = resources[0] if resources and isinstance(resources[0], dict) else {}
            course_title = str(first_resource.get("title") or node.get("courseTitle") or "مورد مقترح — راجع نتائج البحث")
            course_provider = str(first_resource.get("provider") or node.get("courseProvider") or "Online resource")
            course_link = str(first_resource.get("searchQuery") or node.get("courseSearchQuery") or title)
            raw_activities = node.get("activities") if isinstance(node.get("activities"), list) else []
            activities = []
            for a in raw_activities[:8]:
                if not isinstance(a, dict):
                    continue
                activities.append({
                    "title": str(a.get("title") or "نشاط تطبيقي"),
                    "instructions": str(a.get("instructions") or "نفّذ النشاط باستخدام المفاهيم المذكورة في هدف المرحلة."),
                    "deliverable": str(a.get("deliverable") or "ملخص أو حل موثق للنشاط"),
                    "estimatedMinutes": max(5, min(480, int(a.get("estimatedMinutes") or 30))) if str(a.get("estimatedMinutes") or "30").lstrip("-+").isdigit() else 30,
                    "tools": [str(x) for x in a.get("tools", [])[:8] if isinstance(x, (str, int, float))] if isinstance(a.get("tools"), list) else []
                })
            if not activities:
                activities = [
                    {"title": "استرجاع المفاهيم", "instructions": f"اكتب من ذاكرتك أهم المفاهيم المرتبطة بهدف المرحلة: {objective}", "deliverable": "ملخص منظم للمفاهيم", "estimatedMinutes": 20, "tools": []},
                    {"title": "تطبيق موجّه", "instructions": f"طبّق ما تعلمته على مثال أو مسألة مرتبطة بـ {title}، وسجّل خطواتك وأسبابها.", "deliverable": "حل أو تطبيق موثق", "estimatedMinutes": 35, "tools": []},
                    {"title": "مراجعة الأخطاء", "instructions": "راجع الناتج مقابل مرجع موثوق، وحدد أول خطأ أو نقطة غير مؤكدة وصححها.", "deliverable": "قائمة بالأخطاء والتصحيحات", "estimatedMinutes": 20, "tools": []}
                ]
            clean_resources = []
            for r in resources[:5]:
                if isinstance(r, dict):
                    clean_resources.append({"title": str(r.get("title") or "مورد تعليمي"), "provider": str(r.get("provider") or "Online resource"), "resourceType": str(r.get("resourceType") or r.get("type") or "video"), "estimatedMinutes": max(1, min(480, int(r.get("estimatedMinutes") or 15))) if str(r.get("estimatedMinutes") or "15").lstrip("-+").isdigit() else 15, "searchQuery": str(r.get("searchQuery") or r.get("title") or title), "why": str(r.get("why") or "مرتبط بموضوع المرحلة")})
            resources = clean_resources
            raw_questions = node.get("quizQuestions") if isinstance(node.get("quizQuestions"), list) else []
            questions = []
            for q in raw_questions[:8]:
                if not isinstance(q, dict) or not str(q.get("question") or "").strip() or not isinstance(q.get("options"), list):
                    continue
                options = [str(x) for x in q["options"][:4] if str(x).strip()]
                if len(options) < 2:
                    continue
                try:
                    q_index = int(q.get("correctIndex", 0))
                except (TypeError, ValueError):
                    q_index = 0
                q_index = max(0, min(len(options)-1, q_index))
                questions.append({"question": str(q["question"]), "options": options, "correctIndex": q_index, "hint": str(q.get("hint") or "ارجع للمفهوم الأساسي، وحدد المعطيات وما الذي يطلبه السؤال قبل اختيار الإجابة."), "explanation": str(q.get("explanation") or "راجع المفهوم الأساسي المرتبط بالسؤال."), "solutionSteps": clean_string_list(q.get("solutionSteps"), 8) if "clean_string_list" in locals() else ([str(x) for x in q.get("solutionSteps", [])[:8]] if isinstance(q.get("solutionSteps"), list) else []), "remediation": str(q.get("remediation") or "راجع المرجع المرتبط بالمفهوم ثم جرّب مثالًا مشابهًا.") , "conceptTested": str(q.get("conceptTested") or title), "questionType": str(q.get("questionType") or "application")})
            # Normalize a concrete day-by-day curriculum and keep each day's required workload within the learner's daily budget.
            daily_budget = max(15, min(480, int(payload.daily_minutes)))
            raw_days = node.get("dailyLessons") if isinstance(node.get("dailyLessons"), list) else []
            daily_lessons = []
            for day_idx, day in enumerate(raw_days[:14]):
                if not isinstance(day, dict):
                    continue
                video = day.get("video") if isinstance(day.get("video"), dict) else {}
                reading = day.get("reading") if isinstance(day.get("reading"), dict) else {}
                practice = day.get("practice") if isinstance(day.get("practice"), dict) else {}
                def _mins(obj, default):
                    try: return max(1, min(daily_budget, int(obj.get("estimatedMinutes", default))))
                    except (TypeError, ValueError): return default
                vmins = _mins(video, 20)
                rmins = _mins(reading, 15)
                pmins = _mins(practice, 40)
                try: qmins = max(1, min(max(1, daily_budget - 3), int(day.get("quizMinutes", 15))))
                except (TypeError, ValueError): qmins = min(15, max(1, daily_budget - 3))
                # Reserve time for the same-day test, then proportionally fit the learning components.
                if vmins + rmins + pmins + qmins > daily_budget:
                    available = max(3, daily_budget - qmins)
                    total_learn = vmins + rmins + pmins
                    vmins = max(1, int(available * vmins / total_learn))
                    rmins = max(1, int(available * rmins / total_learn))
                    pmins = max(1, available - vmins - rmins)
                    while vmins + rmins + pmins > available:
                        if pmins > 1: pmins -= 1
                        elif rmins > 1: rmins -= 1
                        else: vmins -= 1
                if qmins >= daily_budget:
                    qmins = max(1, daily_budget - vmins - rmins - pmins)
                day_questions_raw = day.get("quizQuestions") if isinstance(day.get("quizQuestions"), list) else []
                day_questions = []
                for dq in day_questions_raw[:5]:
                    if not isinstance(dq, dict) or not str(dq.get("question") or "").strip() or not isinstance(dq.get("options"), list):
                        continue
                    opts = [str(x) for x in dq["options"][:4] if str(x).strip()]
                    if len(opts) < 2: continue
                    try: ci = int(dq.get("correctIndex", 0))
                    except (TypeError, ValueError): ci = 0
                    ci = max(0, min(len(opts)-1, ci))
                    day_questions.append({"question": str(dq["question"]), "conceptTested": str(dq.get("conceptTested") or day.get("title") or title), "questionType": str(dq.get("questionType") or "application"), "options": opts, "correctIndex": ci, "hint": str(dq.get("hint") or "حدد الفكرة التي تعلمتها اليوم واستبعد الخيارات غير المتوافقة."), "explanation": str(dq.get("explanation") or "راجع المفهوم المرتبط بالسؤال."), "solutionSteps": [str(x) for x in dq.get("solutionSteps", [])[:8]] if isinstance(dq.get("solutionSteps"), list) else [], "remediation": str(dq.get("remediation") or "راجع مصدر اليوم ثم حل مثالًا مشابهًا.")})
                # A day without an aligned quiz cannot be considered complete; avoid silently inventing answer keys.
                try: normalized_day_number = max(1, min(365, int(day.get("dayNumber", day_idx+1) or day_idx+1)))
                except (TypeError, ValueError): normalized_day_number = day_idx + 1
                daily_lessons.append({"dayNumber": normalized_day_number, "title": str(day.get("title") or f"اليوم {day_idx+1}: {title}"), "objective": str(day.get("objective") or objective), "estimatedMinutes": min(daily_budget, vmins+rmins+pmins+qmins), "video": {"title": str(video.get("title") or "شرح مركز للموضوع"), "provider": str(video.get("provider") or "مصدر تعليمي"), "searchQuery": str(video.get("searchQuery") or f"{title} {day.get('title','')} tutorial"), "estimatedMinutes": vmins}, "reading": {"title": str(reading.get("title") or "مرجع مكتوب للموضوع"), "provider": str(reading.get("provider") or "توثيق/كتاب تعليمي"), "resourceType": str(reading.get("resourceType") or "reference"), "searchQuery": str(reading.get("searchQuery") or f"{title} {day.get('title','')} documentation chapter"), "estimatedMinutes": rmins}, "practice": {"title": str(practice.get("title") or "تطبيق اليوم"), "instructions": str(practice.get("instructions") or "طبّق مفهوم اليوم على مثال مستقل."), "deliverable": str(practice.get("deliverable") or "حل موثق أو مخرج عملي"), "estimatedMinutes": pmins}, "quizMinutes": qmins, "quizQuestions": day_questions, "lastAttemptScore": None, "passed": False})
            work_sim = node.get("professionalWorkSimulation") if isinstance(node.get("professionalWorkSimulation"), dict) else {}
            work_sim_clean = {"scenario": str(work_sim.get("scenario") or f"نفّذ مهمة عملية مرتبطة بهدف {title} تحاكي احتياجًا مهنيًا."), "role": str(work_sim.get("role") or "متدرب في المجال المستهدف"), "inputsOrData": str(work_sim.get("inputsOrData") or "استخدم بيانات عامة موثوقة أو بيانات صناعية موصوفة بوضوح؛ لا تدّع أنها بيانات شركة خاصة."), "datasetOrArtifactName": str(work_sim.get("datasetOrArtifactName") or "لم يُحدد اسم مجموعة بيانات بعينها"), "sourceUrl": str(work_sim.get("sourceUrl") or ""), "sourceSearchQuery": str(work_sim.get("sourceSearchQuery") or f"{goal} public dataset open source real-world project"), "licenseOrAccess": str(work_sim.get("licenseOrAccess") or "تحقق من الترخيص وشروط الوصول قبل الاستخدام"), "fallbackIfUnavailable": str(work_sim.get("fallbackIfUnavailable") or "استخدم بيانات صناعية واقعية مع توضيح أنها ليست بيانات شركة حقيقية"), "constraints": [str(x) for x in work_sim.get("constraints", [])[:8] if isinstance(x, (str,int,float))] if isinstance(work_sim.get("constraints"), list) else [], "deliverables": [str(x) for x in work_sim.get("deliverables", [])[:8] if isinstance(x, (str,int,float))] if isinstance(work_sim.get("deliverables"), list) else [], "acceptanceCriteria": [str(x) for x in work_sim.get("acceptanceCriteria", [])[:8] if isinstance(x, (str,int,float))] if isinstance(work_sim.get("acceptanceCriteria"), list) else [], "rubric": work_sim.get("rubric", [])[:8] if isinstance(work_sim.get("rubric"), list) else [], "dataProvenance": str(work_sim.get("dataProvenance") or "synthetic-realistic")}
            challenge_question = str(node.get("challengeQuestion") or (questions[0].get("question") if questions else f"ما الفكرة الأساسية التي تعلمتها في {title}؟"))
            challenge_options = node.get("challengeOptions")
            if not isinstance(challenge_options, list) or len(challenge_options) < 2:
                challenge_options = questions[0].get("options") if questions else ["تفسير صحيح مرتبط بالمفاهيم", "تفسير لا يطابق المفاهيم", "إجابة غير مكتملة", "لا توجد معلومات كافية"]
            challenge_options = [str(x) for x in challenge_options[:4]]
            try:
                correct_index = int(node.get("correctIndex", questions[0].get("correctIndex", 0) if questions else 0))
            except (TypeError, ValueError):
                correct_index = 0
            correct_index = max(0, min(len(challenge_options)-1, correct_index))
            raw_eval = node.get("evaluation") if isinstance(node.get("evaluation"), dict) else {}
            criteria = raw_eval.get("criteria") if isinstance(raw_eval.get("criteria"), list) else []
            try:
                threshold = int(raw_eval.get("masteryThreshold", 80))
            except (TypeError, ValueError):
                threshold = 80
            evaluation = {"criteria": [str(x) for x in criteria[:8]], "masteryThreshold": max(60, min(100, threshold))}
            def clean_string_list(value, limit=8):
                return [str(x) for x in value[:limit] if str(x).strip()] if isinstance(value, list) else []
            plan = {
                "trackGoal": goal,
                "studyBudget": {"dailyMinutes": max(15, min(480, int(payload.daily_minutes))), "daysPerWeek": max(1, min(7, int(payload.study_days_per_week)))},
                "programmingLanguage": ("python" if re.search(r"\bpython\b", goal, re.I) else "cpp" if re.search(r"(?:c\+\+|\bcpp\b)", goal, re.I) else "javascript" if re.search(r"\b(javascript|js|node\.js)\b", goal, re.I) else "java" if re.search(r"\bjava\b", goal, re.I) else "c" if re.search(r"\blanguage c\b|\bc programming\b", goal, re.I) else "python"),
                "activityType": str(node.get("activityType") or "Mixed Practice"),
                "prerequisites": clean_string_list(node.get("prerequisites")),
                "coreConcepts": clean_string_list(node.get("coreConcepts"), 12),
                "activities": activities,
                "dailyLessons": daily_lessons,
                "professionalWorkSimulation": work_sim_clean,
                "resources": resources,
                "evaluation": evaluation,
                "quizQuestions": questions,
                "miniProject": str(node.get("miniProject") or ""),
                "commonMistakes": clean_string_list(node.get("commonMistakes"), 8),
                "reviewPlan": str(node.get("reviewPlan") or "راجع ما تعلمته بعد يوم ثم بعد أسبوع."),
                "nextStep": str(node.get("nextStep") or "انتقل للمرحلة التالية بعد تحقيق معيار الإتقان."),
                "discipline": str(res_data.get("discipline") or "other"),
                "assumptions": clean_string_list(res_data.get("assumptions"), 10)
            }
            # Keep the full detailed plan in the history snapshot and active node.
            normalized = dict(node)
            normalized.update({"title": title, "timeframe": timeframe, "objective": objective, "courseTitle": course_title, "courseProvider": course_provider, **plan})
            normalized_nodes.append(normalized)
            cur.execute("""INSERT INTO blueprint_nodes (
                user_id, node_order, title, philosophy, timeframe, course_title, course_provider, course_link,
                challenge_question, challenge_options, correct_index, is_unlocked, is_completed,
                learning_plan, progress_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, '[]')""",
                (payload.user_id, idx + 1, title, objective, timeframe, course_title, course_provider, course_link,
                 challenge_question, json.dumps(challenge_options, ensure_ascii=False), correct_index,
                 1 if idx == 0 else 0, json.dumps(plan, ensure_ascii=False)))
            inserted += 1

        if inserted == 0:
            conn.rollback(); conn.close()
            raise ValueError("Could not parse any valid roadmap nodes from the AI response.")

        # Preserve an explicit capstone/job-readiness rubric in the saved roadmap snapshot.
        raw_job = res_data.get("finalJobReadinessAssessment") if isinstance(res_data.get("finalJobReadinessAssessment"), dict) else {}
        try: job_threshold = max(60, min(100, int(raw_job.get("passThreshold", 80) or 80)))
        except (TypeError, ValueError): job_threshold = 80
        res_data["finalJobReadinessAssessment"] = {
            "task": str(raw_job.get("task") or f"نفّذ مشروعًا نهائيًا مستقلًا يحقق هدف المسار: {goal}"),
            "deliverables": [str(x) for x in raw_job.get("deliverables", [])[:10] if isinstance(x, (str,int,float))] if isinstance(raw_job.get("deliverables"), list) else [],
            "rubric": raw_job.get("rubric", [])[:10] if isinstance(raw_job.get("rubric"), list) else [],
            "passThreshold": job_threshold
        }
        # Attach the capstone to the final milestone so it is visible in the current roadmap UI.
        if normalized_nodes:
            normalized_nodes[-1]["finalJobReadinessAssessment"] = res_data["finalJobReadinessAssessment"]
            last_node = cur.execute("SELECT id, learning_plan FROM blueprint_nodes WHERE user_id=? ORDER BY node_order DESC LIMIT 1", (payload.user_id,)).fetchone()
            if last_node:
                try: last_plan = json.loads(last_node["learning_plan"] or "{}")
                except Exception: last_plan = {}
                if not isinstance(last_plan, dict): last_plan = {}
                last_plan["finalJobReadinessAssessment"] = res_data["finalJobReadinessAssessment"]
                cur.execute("UPDATE blueprint_nodes SET learning_plan=? WHERE id=? AND user_id=?", (json.dumps(last_plan, ensure_ascii=False), last_node["id"], payload.user_id))
        # Save the exact detailed path snapshot before returning success.
        res_data["nodes"] = normalized_nodes
        res_data["studyBudget"] = {"dailyMinutes": max(15, min(480, int(payload.daily_minutes))), "daysPerWeek": max(1, min(7, int(payload.study_days_per_week)))}
        roadmap_created_at = datetime.now(APP_TZ).isoformat()
        cur.execute("INSERT INTO roadmap_history (user_id, goal, duration, lang, roadmap_json, node_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (payload.user_id, goal, payload.duration, payload.lang, json.dumps(res_data, ensure_ascii=False), inserted, roadmap_created_at))
        conn.commit(); conn.close()
        return JSONResponse(content={"success": True, "data": res_data})
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"error": f"Failed: {str(e)}"})

class ProfessionalSimulationSubmission(BaseModel):
    user_id: int
    node_id: int
    submission: str

class DailyRoadmapAssessmentRequest(BaseModel):
    user_id: int
    node_id: int
    day_index: int
    score: int
    passed: bool
    answers: list[dict] = []

@app.post("/api/gps/professional-assessment/submit")
def submit_professional_roadmap_assessment(payload: ProfessionalSimulationSubmission, request: Request):
    uid = _require_user(request, payload.user_id)
    submission = payload.submission.strip()
    if not submission:
        raise HTTPException(status_code=400, detail="Submit your work before requesting an evaluation")
    if len(submission) > 30000:
        raise HTTPException(status_code=413, detail="Submission is too long; limit is 30,000 characters")
    conn = get_db()
    row = conn.execute("SELECT id, title, learning_plan, is_unlocked, is_completed FROM blueprint_nodes WHERE id=? AND user_id=?", (payload.node_id, uid)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Roadmap node not found")
    if not row["is_unlocked"] and not row["is_completed"]:
        conn.close()
        raise HTTPException(status_code=403, detail="Complete the previous milestone first")
    try: plan = json.loads(row["learning_plan"] or "{}")
    except Exception: plan = {}
    sim = plan.get("professionalWorkSimulation") if isinstance(plan, dict) else None
    if not isinstance(sim, dict):
        conn.close()
        raise HTTPException(status_code=400, detail="No professional work simulation exists for this milestone")
    rubric = sim.get("rubric", []) if isinstance(sim.get("rubric"), list) else []
    prompt = f"""You are a careful professional reviewer for an educational work simulation. Evaluate only the submitted evidence against the task and rubric; do not assume missing work was done. Be fair, specific, and actionable. Never claim code was executed or tests were run unless the submission includes verifiable execution evidence. Do not reward copied boilerplate without explaining what is missing. If a deliverable is impossible to judge from text alone, state that limitation.
Return strict JSON only with keys: score (integer 0-100), criterionScores (array of {{criterion, score, evidence, gap}}), strengths (array of strings), issues (array of strings), nextActions (array of strings), feedback (string), missingDeliverables (array of strings).
Task title: {row['title']}
Scenario: {sim.get('scenario','')}
Role: {sim.get('role','')}
Inputs/data: {sim.get('inputsOrData','')}
Dataset/artifact: {sim.get('datasetOrArtifactName','')}
Constraints: {json.dumps(sim.get('constraints',[]),ensure_ascii=False)}
Required deliverables: {json.dumps(sim.get('deliverables',[]),ensure_ascii=False)}
Acceptance criteria: {json.dumps(sim.get('acceptanceCriteria',[]),ensure_ascii=False)}
Rubric: {json.dumps(rubric,ensure_ascii=False)}
Student submission:
{submission}
"""
    try:
        review = generate_ai_json(prompt)
        try: score = max(0, min(100, int(review.get("score", 0))))
        except (TypeError, ValueError): score = 0
        try: threshold = max(60, min(100, int((plan.get("evaluation") or {}).get("masteryThreshold", 80))))
        except (TypeError, ValueError): threshold = 80
        passed = score >= threshold and not bool(review.get("missingDeliverables"))
        attempt = {"score": score, "passed": passed, "submittedAt": datetime.now(APP_TZ).isoformat(), "feedback": str(review.get("feedback") or ""), "criterionScores": review.get("criterionScores", [])[:12] if isinstance(review.get("criterionScores"), list) else [], "strengths": review.get("strengths", [])[:10] if isinstance(review.get("strengths"), list) else [], "issues": review.get("issues", [])[:10] if isinstance(review.get("issues"), list) else [], "nextActions": review.get("nextActions", [])[:10] if isinstance(review.get("nextActions"), list) else [], "missingDeliverables": review.get("missingDeliverables", [])[:10] if isinstance(review.get("missingDeliverables"), list) else []}
        sim["lastScore"] = score
        sim["passed"] = passed
        sim["attemptCount"] = int(sim.get("attemptCount", 0) or 0) + 1
        sim["lastAttempt"] = attempt
        history_attempts = sim.get("attempts", []) if isinstance(sim.get("attempts"), list) else []
        sim["attempts"] = (history_attempts + [attempt])[-5:]
        plan["professionalWorkSimulation"] = sim
        milestone_completed = bool(passed and plan.get("milestoneQuizPassed"))
        order_row = conn.execute("SELECT node_order FROM blueprint_nodes WHERE id=? AND user_id=?", (payload.node_id, uid)).fetchone()
        if milestone_completed and order_row:
            conn.execute("UPDATE blueprint_nodes SET is_completed=1 WHERE id=? AND user_id=?", (payload.node_id, uid))
            conn.execute("UPDATE blueprint_nodes SET is_unlocked=1 WHERE user_id=? AND node_order=?", (uid, int(order_row["node_order"])+1))
        conn.execute("UPDATE blueprint_nodes SET learning_plan=? WHERE id=? AND user_id=?", (json.dumps(plan, ensure_ascii=False), payload.node_id, uid))
        conn.execute("CREATE TABLE IF NOT EXISTS roadmap_history (id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,goal TEXT NOT NULL DEFAULT '',duration TEXT NOT NULL DEFAULT '',lang TEXT NOT NULL DEFAULT 'ar',roadmap_json TEXT NOT NULL DEFAULT '{}',node_count INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL)")
        hist = conn.execute("SELECT id, roadmap_json FROM roadmap_history WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
        if hist:
            try:
                snap = json.loads(hist["roadmap_json"] or "{}")
                snodes = snap.get("nodes", []) if isinstance(snap, dict) else []
                order = conn.execute("SELECT node_order FROM blueprint_nodes WHERE id=? AND user_id=?", (payload.node_id, uid)).fetchone()
                idx = int(order["node_order"])-1 if order else -1
                if 0 <= idx < len(snodes) and isinstance(snodes[idx], dict):
                    snodes[idx]["professionalWorkSimulation"] = sim
                    if isinstance(snodes[idx].get("learning_plan"), dict):
                        snodes[idx]["learning_plan"]["professionalWorkSimulation"] = sim
                        snodes[idx]["learning_plan"]["milestoneQuizPassed"] = bool(plan.get("milestoneQuizPassed"))
                    if milestone_completed: snodes[idx]["isCompleted"] = True
                    conn.execute("UPDATE roadmap_history SET roadmap_json=? WHERE id=? AND user_id=?", (json.dumps(snap, ensure_ascii=False), hist["id"], uid))
            except Exception:
                pass
        conn.commit(); conn.close()
        return {"success": True, "score": score, "passed": passed, "mastery_threshold": threshold, "milestone_completed": milestone_completed, "review": attempt}
    except HTTPException:
        conn.close()
        raise
    except Exception as exc:
        conn.close()
        traceback.print_exc()
        raise HTTPException(status_code=502, detail="Could not evaluate the submission right now. Your submitted work was not saved; retry shortly.") from exc

@app.post("/api/gps/daily-assessment-result")
def save_daily_roadmap_assessment(payload: DailyRoadmapAssessmentRequest, request: Request):
    uid = _require_user(request, payload.user_id)
    conn = get_db()
    row = conn.execute("SELECT id, learning_plan, is_unlocked, is_completed FROM blueprint_nodes WHERE id=? AND user_id=?", (payload.node_id, uid)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Roadmap node not found")
    if not row["is_unlocked"] and not row["is_completed"]:
        conn.close()
        raise HTTPException(status_code=403, detail="Complete the previous milestone first")
    try: plan = json.loads(row["learning_plan"] or "{}")
    except Exception: plan = {}
    days = plan.get("dailyLessons", []) if isinstance(plan, dict) else []
    di = int(payload.day_index)
    if not isinstance(days, list) or di < 0 or di >= len(days):
        conn.close()
        raise HTTPException(status_code=400, detail="Invalid daily lesson")
    if di > 0 and not bool(days[di-1].get("passed")):
        conn.close()
        raise HTTPException(status_code=403, detail="Pass the previous daily assessment first")
    day = days[di]
    questions = day.get("quizQuestions", []) if isinstance(day, dict) else []
    # Grade on the server using the stored answer key; do not trust a client-supplied score.
    answers = payload.answers[:len(questions)] if isinstance(payload.answers, list) else []
    correct = 0
    for qi, question in enumerate(questions):
        try: chosen = int(answers[qi].get("chosenIndex", -1)) if qi < len(answers) else -1
        except (TypeError, ValueError): chosen = -1
        try: correct_index = int(question.get("correctIndex", -2))
        except (TypeError, ValueError): correct_index = -2
        if chosen == correct_index: correct += 1
    score = round(correct / max(1, len(questions)) * 100)
    threshold = 80
    passed = bool(questions) and score >= threshold
    day["lastAttemptScore"] = score
    day["passed"] = passed
    day["attemptCount"] = int(day.get("attemptCount", 0) or 0) + 1
    day["lastAttemptAt"] = datetime.now(APP_TZ).isoformat()
    conn.execute("UPDATE blueprint_nodes SET learning_plan=? WHERE id=? AND user_id=?", (json.dumps(plan, ensure_ascii=False), payload.node_id, uid))
    # Mirror daily results into the most recent roadmap history snapshot without altering prior history records.
    conn.execute("CREATE TABLE IF NOT EXISTS roadmap_history (id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,goal TEXT NOT NULL DEFAULT '',duration TEXT NOT NULL DEFAULT '',lang TEXT NOT NULL DEFAULT 'ar',roadmap_json TEXT NOT NULL DEFAULT '{}',node_count INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL)")
    hist = conn.execute("SELECT id, roadmap_json FROM roadmap_history WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
    if hist:
        try:
            snap = json.loads(hist["roadmap_json"] or "{}")
            snodes = snap.get("nodes", []) if isinstance(snap, dict) else []
            order = conn.execute("SELECT node_order FROM blueprint_nodes WHERE id=? AND user_id=?", (payload.node_id, uid)).fetchone()
            idx = int(order["node_order"])-1 if order else -1
            if 0 <= idx < len(snodes) and isinstance(snodes[idx], dict):
                snodes[idx]["dailyLessons"] = days
                if isinstance(snodes[idx].get("learning_plan"), dict): snodes[idx]["learning_plan"]["dailyLessons"] = days
                conn.execute("UPDATE roadmap_history SET roadmap_json=? WHERE id=? AND user_id=?", (json.dumps(snap, ensure_ascii=False), hist["id"], uid))
        except Exception:
            pass
    conn.commit(); conn.close()
    return {"success": True, "score": score, "passed": passed, "correct": correct, "total": len(questions), "mastery_threshold": threshold}

class RoadmapProgressRequest(BaseModel):
    user_id: int
    node_id: int
    completed_activities: list[int] = []

@app.post("/api/gps/node-progress")
def update_roadmap_node_progress(payload: RoadmapProgressRequest, request: Request):
    uid = _require_user(request, payload.user_id)
    conn = get_db()
    row = conn.execute("SELECT id, learning_plan, progress_json, is_unlocked, is_completed FROM blueprint_nodes WHERE id=? AND user_id=?", (payload.node_id, uid)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Roadmap node not found")
    if not row["is_unlocked"] and not row["is_completed"]:
        conn.close()
        raise HTTPException(status_code=403, detail="Complete the previous milestone first")
    try:
        plan = json.loads(row["learning_plan"] or "{}")
    except Exception:
        plan = {}
    activity_count = len(plan.get("activities", [])) if isinstance(plan, dict) else 0
    cleaned = sorted({int(i) for i in payload.completed_activities if isinstance(i, int) and 0 <= i < activity_count})
    node_order_row = conn.execute("SELECT node_order FROM blueprint_nodes WHERE id=? AND user_id=?", (payload.node_id, uid)).fetchone()
    conn.execute("UPDATE blueprint_nodes SET progress_json=? WHERE id=? AND user_id=?", (json.dumps(cleaned), payload.node_id, uid))
    # Keep the latest History snapshot synchronized with activity completion, not just the live node.
    history_row = conn.execute("SELECT id, roadmap_json FROM roadmap_history WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
    if history_row and node_order_row:
        try:
            snapshot = json.loads(history_row["roadmap_json"] or "{}")
            snapshot_nodes = snapshot.get("nodes", []) if isinstance(snapshot, dict) else []
            idx = int(node_order_row["node_order"]) - 1
            if isinstance(snapshot_nodes, list) and 0 <= idx < len(snapshot_nodes) and isinstance(snapshot_nodes[idx], dict):
                snapshot_nodes[idx]["completedActivities"] = cleaned
                snapshot_nodes[idx]["activityProgressPercent"] = round(100 * len(cleaned) / activity_count) if activity_count else 0
                conn.execute("UPDATE roadmap_history SET roadmap_json=? WHERE id=? AND user_id=?", (json.dumps(snapshot, ensure_ascii=False), history_row["id"], uid))
        except Exception:
            pass
    conn.commit(); conn.close()
    return {"success": True, "completed_activities": cleaned, "activity_count": activity_count,
            "progress_percent": round(100 * len(cleaned) / activity_count) if activity_count else 0}

@app.get("/api/gps/roadmap-history")
def gps_roadmap_history(user_id: int, request: Request, limit: int = 30):
    _require_user(request, user_id)
    limit = max(1, min(int(limit), 100))
    conn = get_db()
    conn.execute("""CREATE TABLE IF NOT EXISTS roadmap_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        goal TEXT NOT NULL DEFAULT '',
        duration TEXT NOT NULL DEFAULT '',
        lang TEXT NOT NULL DEFAULT 'ar',
        roadmap_json TEXT NOT NULL DEFAULT '{}',
        node_count INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )""")
    rows = conn.execute(
        "SELECT id, goal, duration, lang, node_count, created_at FROM roadmap_history WHERE user_id=? ORDER BY id DESC LIMIT ?",
        (user_id, limit)
    ).fetchall()
    conn.close()
    return {"success": True, "roadmaps": [dict(r) for r in rows]}


@app.get("/api/gps/roadmap-history/{roadmap_id}")
def gps_roadmap_history_detail(roadmap_id: int, user_id: int, request: Request):
    _require_user(request, user_id)
    conn = get_db()
    conn.execute("""CREATE TABLE IF NOT EXISTS roadmap_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        goal TEXT NOT NULL DEFAULT '',
        duration TEXT NOT NULL DEFAULT '',
        lang TEXT NOT NULL DEFAULT 'ar',
        roadmap_json TEXT NOT NULL DEFAULT '{}',
        node_count INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )""")
    row = conn.execute(
        "SELECT goal, duration, lang, roadmap_json, node_count, created_at FROM roadmap_history WHERE id=? AND user_id=?",
        (roadmap_id, user_id)
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Roadmap not found")
    try:
        roadmap = json.loads(row["roadmap_json"] or "{}")
    except Exception:
        roadmap = {}
    return {"success": True, "goal": row["goal"], "duration": row["duration"], "lang": row["lang"],
            "roadmap": roadmap, "node_count": row["node_count"], "created_at": row["created_at"]}


# -----------------------------------------------------------------------------
# PDF -> AI-generated custom video
# -----------------------------------------------------------------------------
class VideoPerspectiveRequest(BaseModel):
    source_job_id: str
    perspective: int = 1
    lang: str = "ar"

@app.post("/api/generate-interactive-video")
async def generate_interactive_video(
    request: Request,
    pdfFile: UploadFile = File(...),
    lang: str = Form("ar"),
    perspective: int = Form(1),
    mode: str = Form("ai"),
):
    try:
        user_id = _current_user_id(request)
        if not user_id:
            raise HTTPException(status_code=401, detail="Login required")
        lang = "en" if str(lang).lower().startswith("en") else "ar"
        mode = str(mode or "ai").lower().strip()
        if mode not in {"fast", "ai", "youtube", "both"}:
            mode = "ai"
        pdf_bytes = await pdfFile.read()
        if not pdf_bytes:
            return JSONResponse(status_code=400, content={"error": "The PDF is empty."})
        filename = pdfFile.filename or "uploaded.pdf"
        source_hash = _make_video_source_hash(pdf_bytes, lang, int(perspective), mode)
        conn=get_db(); cur=conn.cursor()
        cached=cur.execute("SELECT * FROM video_jobs WHERE user_id=? AND source_hash=? AND status='completed' ORDER BY created_at DESC LIMIT 1",(user_id,source_hash)).fetchone()
        if cached:
            conn.close()
            return {"success":True,"jobId":cached["id"],"cached":True,"mode":(cached["mode"] if "mode" in cached.keys() else mode) or mode,"message":"Existing video/resource reused."}
        job_id=secrets.token_hex(8); job_dir=os.path.join(GENERATED_VIDEO_DIR,job_id); os.makedirs(job_dir,exist_ok=True)
        source_pdf=os.path.join(job_dir,"source.pdf")
        with open(source_pdf,"wb") as f: f.write(pdf_bytes)
        now=datetime.now(APP_TZ).isoformat()
        cur.execute("""INSERT INTO video_jobs (id,status,progress,stage,filename,perspective,source_pdf,created_at,updated_at,user_id,language,source_hash,engine,mode,youtube_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (job_id,"queued",0,f"في قائمة انتظار {mode}",filename,int(perspective),source_pdf,now,now,user_id,lang,source_hash,"s-tesseract",mode,"[]"))
        conn.commit(); conn.close()
        _audit(user_id,"video_queued",f"job={job_id};file={filename};lang={lang};mode={mode}")
        return {"success":True,"jobId":job_id,"mode":mode,"message":"Job queued. The server continues processing if you close the site."}
    except HTTPException: raise
    except Exception as exc:
        traceback.print_exc(); return JSONResponse(status_code=500,content={"error":str(exc)})

@app.post("/api/generate-perspective-video")
async def generate_perspective_video(payload: VideoPerspectiveRequest, request: Request):
    try:
        user_id=_current_user_id(request)
        if not user_id: raise HTTPException(status_code=401,detail="Login required")
        payload.lang="en" if str(payload.lang).lower().startswith("en") else "ar"
        source_job=video_job_get(payload.source_job_id)
        if not source_job: raise HTTPException(status_code=404,detail="Source video job not found")
        if source_job.get("user_id") and int(source_job["user_id"])!=user_id: raise HTTPException(status_code=403,detail="Not your video job")
        source_pdf=source_job.get("source_pdf","")
        if not source_pdf or not os.path.exists(source_pdf): raise HTTPException(status_code=404,detail="Source PDF is no longer available")
        with open(source_pdf,"rb") as f: pdf_bytes=f.read()
        filename=source_job.get("filename") or "uploaded.pdf"
        source_hash=_make_video_source_hash(pdf_bytes,payload.lang,int(payload.perspective),"ai")
        conn=get_db(); cur=conn.cursor()
        cached=cur.execute("SELECT * FROM video_jobs WHERE user_id=? AND source_hash=? AND status='completed' ORDER BY created_at DESC LIMIT 1",(user_id,source_hash)).fetchone()
        if cached and cached["video_url"]:
            conn.close(); return {"success":True,"jobId":cached["id"],"cached":True}
        job_id=secrets.token_hex(8); job_dir=os.path.join(GENERATED_VIDEO_DIR,job_id); os.makedirs(job_dir,exist_ok=True)
        copied_pdf=os.path.join(job_dir,"source.pdf"); open(copied_pdf,"wb").write(pdf_bytes)
        now=datetime.now(APP_TZ).isoformat()
        cur.execute("""INSERT INTO video_jobs (id,status,progress,stage,filename,perspective,source_pdf,created_at,updated_at,user_id,language,source_hash,engine,mode,youtube_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (job_id,"queued",0,"في قائمة انتظار الفيديو",filename,int(payload.perspective),copied_pdf,now,now,user_id,payload.lang,source_hash,"s-tesseract","ai","[]"))
        conn.commit(); conn.close(); _audit(user_id,"video_queued",f"job={job_id};perspective={payload.perspective}")
        return {"success":True,"jobId":job_id}
    except HTTPException: raise
    except Exception as exc:
        traceback.print_exc(); return JSONResponse(status_code=500,content={"error":str(exc)})

@app.get("/api/video-job/{job_id}")
def get_video_job(job_id: str, request: Request):
    row = video_job_get(job_id)
    if not row:
        raise HTTPException(status_code=404, detail="Video job not found")
    uid=_current_user_id(request)
    if uid and row.get("user_id") and int(row["user_id"])!=uid:
        raise HTTPException(status_code=403, detail="Not your video job")

    payload = {
        "success": True,
        "jobId": row["id"],
        "status": row["status"],
        "progress": row["progress"],
        "stage": row["stage"],
        "filename": row["filename"],
        "perspective": row["perspective"],
        "mode": (row["mode"] if "mode" in row.keys() else "ai") or "ai",
        "videoUrl": row["video_url"],
        "youtube": json.loads(row.get("youtube_json") or "[]") if row.get("youtube_json") else [],
        "error": row["error"],
    }
    if row["result_json"]:
        try:
            payload["data"] = json.loads(row["result_json"])
        except Exception:
            payload["data"] = {}
    return JSONResponse(content=payload)

@app.get("/api/video-history")
def video_history(request: Request, limit: int = 50):
    user_id = _current_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Login required")
    limit=max(1,min(int(limit),100))
    conn=get_db()
    rows=conn.execute("""SELECT id,filename,status,progress,stage,perspective,language,mode,video_url,result_json,youtube_json,error,created_at,updated_at FROM video_jobs WHERE user_id=? ORDER BY created_at DESC LIMIT ?""",(user_id,limit)).fetchall()
    conn.close()
    items=[]
    for row in rows:
        item=dict(row); item["has_data"]=bool(item.get("result_json")); item.pop("result_json",None)
        try: item["youtube"]=json.loads(item.get("youtube_json") or "[]")
        except Exception: item["youtube"]=[]
        item.pop("youtube_json",None); items.append(item)
    return {"success":True,"videos":items}

@app.post("/api/schedule/add")
def add_schedule_task(payload: TaskRequest, request: Request):
    _require_user(request, payload.user_id)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO schedule_items (user_id, title, target_date, target_time, is_completed, source, reminder_sent)
        VALUES (?, ?, ?, ?, 0, 'manual', 0)
    """, (payload.user_id, payload.title.strip(), payload.target_date, payload.target_time.strip()))
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return {"success": True, "id": new_id}

@app.get("/api/schedule/{user_id}/list")
def list_schedule_items(user_id: int, request: Request):
    _require_user(request, user_id)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM schedule_items WHERE user_id = ? ORDER BY target_date ASC, target_time ASC", (user_id,))
    items = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"success": True, "items": items}

@app.post("/api/schedule/delete")
def delete_schedule_item(payload: TaskDeleteRequest, request: Request):
    uid = _require_user(request)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM schedule_items WHERE id = ? AND user_id = ?", (payload.schedule_id, uid))
    conn.commit()
    conn.close()
    return {"success": True}

@app.post("/api/schedule/toggle")
def toggle_schedule_item(payload: TaskToggleRequest, request: Request):
    uid = _require_user(request)
    conn = get_db()
    cur = conn.cursor()
    if payload.is_completed:
        cur.execute("UPDATE schedule_items SET is_completed = 1 WHERE id = ? AND user_id = ?", (payload.schedule_id, uid))
    else:
        cur.execute("UPDATE schedule_items SET is_completed = 0, reminder_sent = 0 WHERE id = ? AND user_id = ?", (payload.schedule_id, uid))
    conn.commit()
    conn.close()
    return {"success": True}

class TelegramUserRequest(BaseModel):
    user_id: int

@app.get("/api/telegram/status/{user_id}")
def telegram_status(user_id: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT username, telegram_chat_id FROM users WHERE id=?", (user_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    return {
        "configured": telegram_configured(),
        "connected": bool(row["telegram_chat_id"]),
        "username": row["username"],
    }

@app.post("/api/telegram/link")
def telegram_link(payload: TelegramUserRequest):
    if not telegram_configured():
        return JSONResponse(status_code=400, content={"error": "TELEGRAM_BOT_TOKEN is not configured."})
    token = create_telegram_link_token(payload.user_id)
    bot_info = telegram_api("getMe").get("result", {})
    bot_username = bot_info.get("username", "")
    if not bot_username:
        return JSONResponse(status_code=500, content={"error": "Could not resolve Telegram bot username."})
    return {
        "success": True,
        "link": f"https://t.me/{bot_username}?start={token}",
        "expiresMinutes": 15,
    }

@app.post("/api/telegram/unlink")
def telegram_unlink(payload: TelegramUserRequest):
    conn = get_db()
    conn.execute("UPDATE users SET telegram_chat_id='' WHERE id=?", (payload.user_id,))
    conn.commit()
    conn.close()
    return {"success": True}

@app.post("/api/user/record-result")
def record_result(payload: ResultRequest, request: Request):
    _require_user(request, payload.user_id)
    conn = get_db()
    cur = conn.cursor()
    progression_blocked_by_work = False
    milestone_completed = False
    cur.execute("SELECT * FROM users WHERE id = ?", (payload.user_id,))
    user = cur.fetchone()
    if not user:
        conn.close()
        raise HTTPException(status_code=404, detail="User not found")
        
    today = date.today()
    last_active = datetime.strptime(user["last_active_date"], "%Y-%m-%d").date() if user["last_active_date"] else None
    
    new_streak = user["streak"]
    if payload.passed == 1:
        if last_active is None:
            new_streak = 1
        elif last_active == today:
            pass
        elif last_active == today - timedelta(days=1):
            new_streak += 1
        else:
            new_streak = 1
            
        if payload.node_id > 0:
            cur.execute("SELECT id, node_order, learning_plan FROM blueprint_nodes WHERE id = ? AND user_id = ?", (payload.node_id, payload.user_id))
            curr_node = cur.fetchone()
            if not curr_node:
                conn.close()
                raise HTTPException(status_code=404, detail="Roadmap node not found")
            try: node_plan = json.loads(curr_node["learning_plan"] or "{}")
            except Exception: node_plan = {}
            work_sim = node_plan.get("professionalWorkSimulation") if isinstance(node_plan, dict) else None
            work_sim_required = isinstance(work_sim, dict)
            work_sim_passed = bool(work_sim.get("passed")) if work_sim_required else True
            if work_sim_required and not work_sim_passed:
                progression_blocked_by_work = True
                # Remember the knowledge-test pass so the learner need not repeat it after passing the work simulation.
                node_plan["milestoneQuizPassed"] = True
                cur.execute("UPDATE blueprint_nodes SET learning_plan=? WHERE id=? AND user_id=?", (json.dumps(node_plan, ensure_ascii=False), payload.node_id, payload.user_id))
            if work_sim_passed:
                milestone_completed = True
                cur.execute("UPDATE blueprint_nodes SET is_completed = 1 WHERE id = ? AND user_id = ?", (payload.node_id, payload.user_id))
                cur.execute("UPDATE blueprint_nodes SET is_unlocked = 1 WHERE user_id = ? AND node_order = ?",
                            (payload.user_id, curr_node["node_order"] + 1))
                # Mirror quiz completion in the most recent History snapshot.
                history_row = cur.execute("SELECT id, roadmap_json FROM roadmap_history WHERE user_id=? ORDER BY id DESC LIMIT 1", (payload.user_id,)).fetchone()
                if history_row:
                    try:
                        snapshot = json.loads(history_row["roadmap_json"] or "{}")
                        snapshot_nodes = snapshot.get("nodes", []) if isinstance(snapshot, dict) else []
                        idx = int(curr_node["node_order"]) - 1
                        if isinstance(snapshot_nodes, list) and 0 <= idx < len(snapshot_nodes) and isinstance(snapshot_nodes[idx], dict):
                            snapshot_nodes[idx]["isCompleted"] = True
                            cur.execute("UPDATE roadmap_history SET roadmap_json=? WHERE id=? AND user_id=?",
                                        (json.dumps(snapshot, ensure_ascii=False), history_row["id"], payload.user_id))
                    except Exception:
                        pass
            
    cur.execute("""
        UPDATE users
        SET xp = xp + ?,
            streak = ?,
            last_active_date = ?,
            tests_taken = tests_taken + 1,
            tests_passed = tests_passed + ?
        WHERE id = ?
    """, (payload.earned_xp, new_streak, str(today), payload.passed, payload.user_id))
    
    conn.commit()
    conn.close()
    return {"success": True, "new_streak": new_streak, "milestone_completed": milestone_completed, "work_simulation_required": progression_blocked_by_work}
# -----------------------------------------------------------------------------
# AI Custom Video: PDF -> witty Egyptian-Arabic script -> CINEMATIC narrated MP4
# -----------------------------------------------------------------------------
# Free pipeline (no Veo needed):
#   Gemini (text)  -> 8-scene lesson plan in a fast, funny, sarcastic style
#   Gemini (image) -> cinematic background per scene (optional, auto-fallback)
#   PyMuPDF        -> the REAL PDF page shown as a floating card
#   edge-tts/gTTS  -> narration voice
#   Pillow+ffmpeg  -> Ken-Burns camera, animated text, vignette, letterbox,
#                     film grain, colour grade, ambient music bed, fade in/out
# Set VIDEO_ENGINE=veo to use paid Veo first (it falls back to this pipeline).
import io
import random
import shutil
from concurrent.futures import ThreadPoolExecutor

try:
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont
except Exception:  # pragma: no cover
    Image = ImageDraw = ImageEnhance = ImageFilter = ImageFont = None

try:
    import arabic_reshaper
    from bidi.algorithm import get_display as _bidi_display
except Exception:  # pragma: no cover
    arabic_reshaper = None
    _bidi_display = None

try:
    import pymupdf as _pymupdf
except Exception:  # pragma: no cover
    try:
        import fitz as _pymupdf
    except Exception:
        _pymupdf = None

VIDEO_ENGINE = os.environ.get("VIDEO_ENGINE", "cinematic").strip().lower()
CV_PIP_HINT = "pip install pillow pymupdf arabic-reshaper python-bidi edge-tts gTTS imageio-ffmpeg"
CV_W, CV_H, CV_FPS, CV_BAR = 640, 360, 15, 42
CV_AMBER = (251, 191, 36)
CV_ARABIC_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]")

CV_PALETTES = [
    {"a": (99, 102, 241), "b": (236, 72, 153), "bg1": (6, 10, 30), "bg2": (30, 20, 74)},
    {"a": (20, 184, 166), "b": (250, 204, 21), "bg1": (4, 20, 28), "bg2": (8, 47, 73)},
    {"a": (249, 115, 22), "b": (56, 189, 248), "bg1": (26, 10, 12), "bg2": (30, 27, 75)},
    {"a": (168, 85, 247), "b": (34, 211, 238), "bg1": (12, 8, 32), "bg2": (34, 10, 64)},
]
# ambient chord bed (Am - F - C - G) so the lesson has a musical thread
CV_CHORDS = [(110.0, 130.8, 164.8), (87.3, 110.0, 130.8), (130.8, 164.8, 196.0), (98.0, 123.5, 146.8)]

CV_CLIP_ENGINE = {}
CV_VEO_DISABLED_UNTIL = {"t": 0.0}
CV_IMG_DISABLED_UNTIL = {"t": 0.0}
CV_FFMPEG_CACHE = {"path": None}
CV_FONT_CACHE = {}


def cv_log(*a):
    print("[video]", *a, flush=True)


# ---------------------------------------------------------------- ffmpeg ---
def cv_ffmpeg() -> str:
    if CV_FFMPEG_CACHE["path"]:
        return CV_FFMPEG_CACHE["path"]
    env = os.environ.get("FFMPEG_PATH", "").strip()
    if env and os.path.exists(env):
        CV_FFMPEG_CACHE["path"] = env
        return env
    found = shutil.which("ffmpeg")
    if found:
        CV_FFMPEG_CACHE["path"] = found
        return found
    try:
        import imageio_ffmpeg

        CV_FFMPEG_CACHE["path"] = imageio_ffmpeg.get_ffmpeg_exe()
        return CV_FFMPEG_CACHE["path"]
    except Exception:
        pass
    raise RuntimeError("ffmpeg not found. Easiest fix:  pip install imageio-ffmpeg")


def cv_run(cmd, timeout=900):
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore", timeout=timeout
    )


def cv_duration(path: str) -> float:
    proc = cv_run([cv_ffmpeg(), "-hide_banner", "-i", path], timeout=60)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr or "")
    if not m:
        return 0.0
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def cv_concat_path(p: str) -> str:
    return os.path.abspath(p).replace("\\", "/").replace("'", "'\\''")


# ------------------------------------------------------------ text/fonts ---
def cv_has_arabic(s: str) -> bool:
    return bool(CV_ARABIC_RE.search(s or ""))


def cv_shape(s: str) -> str:
    if cv_has_arabic(s):
        if arabic_reshaper is None or _bidi_display is None:
            raise RuntimeError("Arabic text needs arabic-reshaper + python-bidi.  " + CV_PIP_HINT)
        return _bidi_display(arabic_reshaper.reshape(s))
    return s


CV_FONT_REGULAR = [
    "C:/Windows/Fonts/tahoma.ttf", "C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/segoeui.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/SFArabic.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans.ttf",
]
CV_FONT_BOLD = [
    "C:/Windows/Fonts/tahomabd.ttf", "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/segoeuib.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
]


def cv_font(size: int, bold: bool = False):
    key = (size, bold)
    if key in CV_FONT_CACHE:
        return CV_FONT_CACHE[key]
    cands = []
    envp = os.environ.get("VIDEO_FONT_BOLD_PATH" if bold else "VIDEO_FONT_PATH", "").strip()
    if envp:
        cands.append(envp)
    cands += CV_FONT_BOLD if bold else CV_FONT_REGULAR
    font = None
    for path in cands:
        if os.path.exists(path):
            try:
                try:  # BASIC layout: Arabic is already shaped by arabic_reshaper
                    font = ImageFont.truetype(path, size, layout_engine=ImageFont.Layout.BASIC)
                except AttributeError:
                    font = ImageFont.truetype(path, size, layout_engine=ImageFont.LAYOUT_BASIC)
                break
            except Exception:
                continue
    if font is None:
        cv_log("WARNING: no font with Arabic support found. Set VIDEO_FONT_PATH to a .ttf file.")
        font = ImageFont.load_default()
    CV_FONT_CACHE[key] = font
    return font


def cv_wrap(probe, text: str, font, max_width: int):
    words = (text or "").split()
    lines, cur = [], ""
    for w in words:
        cand = (cur + " " + w).strip()
        if not cur or probe.textlength(cv_shape(cand), font=font) <= max_width:
            cur = cand
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def cv_draw_line(d, line, font, y, left, right, rtl, fill):
    shaped = cv_shape(line)
    width = d.textlength(shaped, font=font)
    d.text((right - width if rtl else left, y), shaped, font=font, fill=fill)


def cv_shadow(img, blur=5, dy=3, opacity=0.78):
    """Soft drop-shadow behind an RGBA image (keeps same size)."""
    alpha = img.getchannel("A").point(lambda a: int(a * opacity))
    sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
    sh.putalpha(alpha)
    sh = sh.filter(ImageFilter.GaussianBlur(blur))
    moved = Image.new("RGBA", img.size, (0, 0, 0, 0))
    moved.paste(sh, (0, dy))
    return Image.alpha_composite(moved, img)


def cv_layer(img, x, y, kind, t0, fd, dx=0):
    return {"img": img, "x": int(x), "y": int(y), "kind": kind, "t0": float(t0), "fd": float(fd), "dx": dx}


def cv_layer_from_full(full, kind, t0, fd, dx=0):
    bbox = full.getchannel("A").getbbox()
    if not bbox:
        return None
    pad = 6
    x0, y0 = max(0, bbox[0] - pad), max(0, bbox[1] - pad)
    x1, y1 = min(full.width, bbox[2] + pad), min(full.height, bbox[3] + pad)
    return cv_layer(full.crop((x0, y0, x1, y1)), x0, y0, kind, t0, fd, dx)


# ------------------------------------------------------------ backgrounds ---
def cv_cover(img, w, h):
    scale = max(w / img.width, h / img.height)
    nw, nh = int(img.width * scale + 1), int(img.height * scale + 1)
    img = img.resize((nw, nh), Image.LANCZOS)
    left, top = (nw - w) // 2, (nh - h) // 2
    return img.crop((left, top, left + w, top + h))


def cv_make_background(scene_no, ai_img):
    BW, BH = 2560, 1440
    if ai_img is not None:
        bg = cv_cover(ai_img.convert("RGB"), BW, BH)
        bg = ImageEnhance.Brightness(bg).enhance(0.58)
        return ImageEnhance.Contrast(bg).enhance(1.1)
    pal = CV_PALETTES[(scene_no - 1) % len(CV_PALETTES)]
    grad = Image.linear_gradient("L").resize((BW, BH))
    base = Image.composite(
        Image.new("RGB", (BW, BH), pal["bg2"]), Image.new("RGB", (BW, BH), pal["bg1"]), grad
    ).convert("RGBA")
    rng = random.Random(scene_no * 7919 + 13)
    sw, sh = BW // 4, BH // 4
    glow = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for col in (pal["a"], pal["b"]):
        cx = rng.randint(int(sw * 0.15), int(sw * 0.85))
        cy = rng.randint(int(sh * 0.15), int(sh * 0.85))
        r = rng.randint(int(sh * 0.28), int(sh * 0.5))
        gd.ellipse([cx - r, cy - r, cx + r, cy + r], fill=col + (140,))
    glow = glow.filter(ImageFilter.GaussianBlur(38)).resize((BW, BH), Image.BICUBIC)
    base = Image.alpha_composite(base, glow)
    layer = Image.new("RGBA", (BW, BH), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for gx in range(0, BW, 160):
        d.line([(gx, 0), (gx, BH)], fill=(255, 255, 255, 12), width=2)
    for gy in range(0, BH, 160):
        d.line([(0, gy), (BW, gy)], fill=(255, 255, 255, 12), width=2)
    for _ in range(150):
        x, y = rng.randint(0, BW), rng.randint(0, BH)
        r, a = rng.choice((2, 3, 3, 4, 6)), rng.randint(50, 170)
        d.ellipse([x - r, y - r, x + r, y + r], fill=(255, 255, 255, a))
    layer = layer.filter(ImageFilter.GaussianBlur(1.2))
    return Image.alpha_composite(base, layer).convert("RGB")


def cv_ai_image(prompt: str, scene_no: int):
    """Cinematic AI background. Returns PIL image or None (never raises)."""
    if os.environ.get("VIDEO_AI_IMAGES", "0").strip().lower() in ("0", "false", "no", "off"):
        return None
    if not prompt or time.time() < CV_IMG_DISABLED_UNTIL["t"]:
        return None
    models = [m.strip() for m in os.environ.get(
        "VIDEO_IMAGE_MODELS", "gemini-3.1-flash-image,gemini-2.5-flash-image").split(",") if m.strip()]
    full = (
        prompt.strip() + "\n\nStyle: cinematic film still, dramatic lighting, rich colour grading, "
        "shallow depth of field, widescreen 16:9 composition, highly detailed, documentary look. "
        "Absolutely no text, letters, numbers, logos or watermarks."
    )
    quota_hits = 0
    for model in models:
        try:
            try:
                cfg = types.GenerateContentConfig(
                    response_modalities=["TEXT", "IMAGE"],
                    image_config=types.ImageConfig(aspect_ratio="16:9"),
                )
            except Exception:
                cfg = types.GenerateContentConfig(response_modalities=["TEXT", "IMAGE"])
            resp = ai.models.generate_content(model=model, contents=[full], config=cfg)
            for cand in (getattr(resp, "candidates", None) or []):
                for part in (getattr(getattr(cand, "content", None), "parts", None) or []):
                    inline = getattr(part, "inline_data", None)
                    if inline is not None and getattr(inline, "data", None):
                        return Image.open(io.BytesIO(inline.data)).convert("RGB")
        except Exception as exc:
            msg = str(exc)
            cv_log(f"scene {scene_no}: image model {model} failed: {msg[:160]}")
            up = msg.upper()
            if any(k in up for k in ("429", "RESOURCE_EXHAUSTED", "QUOTA", "PERMISSION", "403")):
                quota_hits += 1
    if quota_hits >= len(models):
        CV_IMG_DISABLED_UNTIL["t"] = time.time() + 600
        cv_log("image generation unavailable -> procedural cinematic backgrounds for 10 min")
    return None


# ------------------------------------------------------------- PDF page ---
def cv_first_page(scene: dict) -> int:
    pages = scene.get("sourcePages") or []
    if not isinstance(pages, list):
        pages = [pages]
    for p in pages:
        try:
            n = int(str(p).strip())
            if n >= 1:
                return n
        except Exception:
            continue
    return 0


def cv_pdf_page(pdf_path: str, page_no: int, max_w: int, max_h: int):
    if _pymupdf is None or not pdf_path or not os.path.exists(pdf_path):
        return None
    try:
        doc = _pymupdf.open(pdf_path)
        try:
            idx = max(0, min(int(page_no) - 1, len(doc) - 1))
            pix = doc[idx].get_pixmap(matrix=_pymupdf.Matrix(1.7, 1.7), alpha=False)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        finally:
            doc.close()
        img.thumbnail((max_w, max_h), Image.LANCZOS)
        return img
    except Exception:
        traceback.print_exc()
        return None


def cv_page_card(page_img, tilt):
    pad, m = 14, 46
    cw, ch = page_img.width + 2 * pad, page_img.height + 2 * pad
    canvas = Image.new("RGBA", (cw + 2 * m, ch + 2 * m), (0, 0, 0, 0))
    sh = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle([m + 6, m + 16, m + cw + 6, m + ch + 16], radius=18, fill=(0, 0, 0, 190))
    canvas = Image.alpha_composite(canvas, sh.filter(ImageFilter.GaussianBlur(16)))
    ImageDraw.Draw(canvas).rounded_rectangle([m, m, m + cw, m + ch], radius=16, fill=(255, 255, 255, 255))
    canvas.paste(page_img.convert("RGB"), (m + pad, m + pad))
    return canvas.rotate(tilt, resample=Image.BICUBIC, expand=True)


# --------------------------------------------------------------- layers ---
def cv_chrome(concept_title, scene_no, total_scenes, rtl):
    """Vignette + letterbox bars + header + progress: always on top."""
    W, H = CV_W, CV_H
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    vg = Image.radial_gradient("L").resize((W, H))
    alpha = vg.point(lambda v: int(max(0, v - 95) * 1.25))
    black = Image.new("RGBA", (W, H), (0, 0, 0, 255))
    black.putalpha(alpha)
    layer = Image.alpha_composite(layer, black)
    d = ImageDraw.Draw(layer)
    d.rectangle([0, 0, W, CV_BAR], fill=(0, 0, 0, 255))
    d.rectangle([0, H - CV_BAR, W, H], fill=(0, 0, 0, 255))

    margin = 72
    head_font, pill_font = cv_font(23, True), cv_font(21, True)
    pill_txt = f"{scene_no}/{total_scenes}"
    pill_w = int(d.textlength(pill_txt, font=pill_font)) + 34
    py0 = CV_BAR + 16
    px0 = margin if rtl else W - margin - pill_w
    d.rounded_rectangle([px0, py0, px0 + pill_w, py0 + 38], radius=19, fill=CV_AMBER + (255,))
    d.text((px0 + 17, py0 + 6), pill_txt, font=pill_font, fill=(15, 23, 42, 255))
    txt = (concept_title or "").strip()
    room = W - 2 * margin - pill_w - 30
    while len(txt) > 4 and d.textlength(cv_shape(txt), font=head_font) > room:
        txt = txt[:-1]
    if txt != (concept_title or "").strip():
        txt += "..."
    if rtl:
        cv_draw_line(d, txt, head_font, py0 + 5, margin + pill_w + 30, W - margin, True, (203, 213, 225, 235))
    else:
        cv_draw_line(d, txt, head_font, py0 + 5, margin, W - margin - pill_w - 30, False, (203, 213, 225, 235))

    # progress line + brand inside the bottom bar
    by = H - CV_BAR // 2
    track = [margin + 190, by - 3, W - margin - 190, by + 3]
    d.rounded_rectangle(track, radius=3, fill=(51, 65, 85, 255))
    span = track[2] - track[0]
    done = int(span * min(1.0, scene_no / max(1, total_scenes)))
    if done > 0:
        if rtl:
            d.rounded_rectangle([track[2] - done, track[1], track[2], track[3]], radius=3, fill=CV_AMBER + (255,))
        else:
            d.rounded_rectangle([track[0], track[1], track[0] + done, track[3]], radius=3, fill=CV_AMBER + (255,))
    d.text((margin, by - 11), "S-TESSERACT", font=cv_font(18, True), fill=(148, 163, 184, 255))
    return cv_layer(layer, 0, 0, "static", 0, 0)



def cv_scene_type(title: str, narration: str, points=None):
    """Lightweight deterministic visual classifier for Fast Video (no Gemini)."""
    text = f"{title} {narration} {' '.join(points or [])}".lower()
    if re.search(r"```|\b(def|class|import|for|while|return|function|code|python|c\+\+|javascript|sql)\b", text):
        return "code"
    if re.search(r"\bvs\b|versus|compare|comparison|difference|مقارنة|الفرق|بين", text):
        return "comparison"
    if re.search(r"timeline|history|year|century|تاريخ|سنة|قرن|تطور", text):
        return "timeline"
    if re.search(r"step|steps|process|procedure|flow|pipeline|algorithm|خطوة|خطوات|عملية|مراحل|تدفق", text):
        return "process"
    if re.search(r"=|equation|formula|calculate|derivative|integral|قانون|معادلة|احسب|تكامل|مشتق", text):
        return "equation"
    if re.search(r"example|example:|مثال|تطبيق|حل", text):
        return "example"
    if re.search(r"definition|means|defined|تعريف|يعني|هو أن", text):
        return "definition"
    if re.search(r"important|key|remember|مهم|الخلاصة|تذكر", text):
        return "key_point"
    return "concept"


def cv_visual_card(scene, scene_type, rtl):
    """Create a compact educational visual using Pillow only."""
    W, H = 560, 300
    im = Image.new("RGBA", (W, H), (8, 14, 35, 245))
    d = ImageDraw.Draw(im)
    accent = CV_AMBER + (255,)
    white = (245, 248, 252, 255)
    muted = (165, 178, 205, 255)
    title = str(scene.get("title") or "").strip()[:42]
    points = [str(x).strip() for x in (scene.get("onScreenPoints") or []) if str(x).strip()][:3]
    f_title = cv_font(30, True); f = cv_font(23); f_small = cv_font(18)
    d.rounded_rectangle([2,2,W-2,H-2], radius=22, outline=(255,255,255,28), width=2)
    d.text((W-34, 18) if rtl else (28,18), cv_shape(title), font=f_title, fill=white, anchor="ra" if rtl else "la")
    d.rounded_rectangle([28,66,W-28,70], radius=2, fill=accent)
    typelabel = {"concept":"CONCEPT","equation":"EQUATION","process":"PROCESS","comparison":"COMPARE","timeline":"TIMELINE","code":"CODE","example":"EXAMPLE","definition":"DEFINITION","key_point":"KEY POINT"}.get(scene_type,"CONCEPT")
    d.text((W-30, 88) if rtl else (30,88), typelabel, font=f_small, fill=accent, anchor="ra" if rtl else "la")
    # Central visual grammar
    if scene_type == "equation":
        eq = (points[0] if points else title)[:32]
        d.rounded_rectangle([70,125,W-70,205], radius=16, fill=(18,30,60,255), outline=(255,255,255,35))
        d.text((W/2,165), cv_shape(eq), font=cv_font(34, True), fill=white, anchor="mm")
        d.text((W/2,235), "variables  →  relation  →  result", font=f_small, fill=muted, anchor="mm")
    elif scene_type == "process":
        labels = points or ["Input","Process","Output"]
        n=min(3,len(labels)); bw=135
        for i in range(n):
            x=28+i*175
            d.rounded_rectangle([x,130,x+bw,205],radius=14,fill=(18,30,60,255),outline=(255,255,255,35))
            d.text((x+bw/2,168),cv_shape(labels[i][:15]),font=f_small,fill=white,anchor="mm")
            if i<n-1:
                d.line([x+bw+8,168,x+168,168],fill=accent,width=5)
                d.polygon([(x+168,168),(x+154,160),(x+154,176)],fill=accent)
        d.text((W/2,245),"STEP → STEP → STEP",font=f_small,fill=muted,anchor="mm")
    elif scene_type == "comparison":
        for i,label in enumerate((points+ ["Option A","Option B"])[:2]):
            x=30+i*260
            d.rounded_rectangle([x,125,x+230,215],radius=16,fill=(18,30,60,255),outline=accent if i==0 else (255,255,255,35))
            d.text((x+115,170),cv_shape(label[:18]),font=f_small,fill=white,anchor="mm")
        d.text((W/2,245),"WHAT CHANGES?",font=f_small,fill=muted,anchor="mm")
    elif scene_type == "timeline":
        d.line([55,175,W-55,175],fill=accent,width=5)
        for i,label in enumerate((points or ["Start","Change","Result"])[:3]):
            x=90+i*190
            d.ellipse([x-12,163,x+12,187],fill=accent)
            d.text((x,135),str(i+1),font=f_small,fill=white,anchor="mm")
            d.text((x,215),cv_shape(label[:14]),font=f_small,fill=muted,anchor="mm")
    elif scene_type == "code":
        lines=(points or ["input()","process()","return result"])[:3]
        for i,line in enumerate(lines):
            y=125+i*42
            d.text((45,y),f"{i+1:02d}",font=f_small,fill=accent)
            d.rounded_rectangle([85,y-5,W-35,y+28],radius=7,fill=(3,8,20,255))
            d.text((105,y+10),cv_shape(line[:30]),font=f_small,fill=white,anchor="lm")
    elif scene_type == "key_point":
        d.ellipse([175,120,385,330],fill=(18,30,60,255),outline=accent,width=5)
        d.text((280,185),"!",font=cv_font(70,True),fill=accent,anchor="mm")
        d.text((280,245),cv_shape((points[0] if points else "Remember this")[:24]),font=f_small,fill=white,anchor="mm")
    else:
        # Concept / definition / example: three connected knowledge nodes.
        labels=(points or ["Idea","Meaning","Example"])[:3]
        for i,label in enumerate(labels):
            x=105+i*175
            d.ellipse([x-38,135,x+38,211],fill=(18,30,60,255),outline=accent,width=3)
            d.text((x,173),str(i+1),font=cv_font(28,True),fill=accent,anchor="mm")
            d.text((x,235),cv_shape(label[:14]),font=f_small,fill=muted,anchor="mm")
            if i<2: d.line([x+40,173,x+135,173],fill=(120,140,175,255),width=3)
    return im

def cv_build_layers(concept_title, scene_no, total_scenes, title, points, punchline, card, rtl, t, visual=None):
    """Build all animated overlay layers. `t` = dict of timings."""
    W, H = CV_W, CV_H
    probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    margin = 72
    zone_top, zone_bottom = CV_BAR + 20, H - CV_BAR - 24
    layers = []

    text_left, text_right = margin, W - margin
    card_pos = None
    if card is not None:
        cw, ch = card.size
        cx = margin - 10 if rtl else W - margin - cw + 10
        cy = (zone_top + zone_bottom) // 2 - ch // 2 + 18
        card_pos = (cx, cy)
        if rtl:
            text_left, text_right = cx + cw - 20, W - margin
        else:
            text_left, text_right = margin, cx + 20
    else:
        if rtl:
            text_left = max(margin, text_right - 860)
        else:
            text_right = min(W - margin, text_left + 860)
    text_w = max(340, text_right - text_left)

    # ---- title
    tsize = 56
    for tsize in (56, 50, 44, 38):
        tfont = cv_font(tsize, True)
        t_lines = cv_wrap(probe, title, tfont, text_w)
        if len(t_lines) <= 2:
            break
    t_lines = t_lines[:3]
    t_lh = int(tsize * 1.3)
    full = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(full)
    y = zone_top + 66
    for ln in t_lines:
        cv_draw_line(d, ln, tfont, y, text_left, text_right, rtl, (248, 250, 252, 255))
        y += t_lh
    bw = 110
    bx = text_right - bw if rtl else text_left
    d.rounded_rectangle([bx, y + 6, bx + bw, y + 12], radius=3, fill=CV_AMBER + (255,))
    title_bottom = y + 12
    lay = cv_layer_from_full(cv_shadow(full), "title", t["title"], 0.55)
    if lay:
        layers.append(lay)

    # ---- keyword chips
    px, py, bar_gap = 20, 12, 18
    reserve = 80 if (punchline and card is None) else 0
    chosen = None
    for size in (32, 29, 26, 23):
        f, lh = cv_font(size), int(size * 1.38)
        laid, tot = [], 0
        for p in points:
            lines = cv_wrap(probe, p, f, text_w - 2 * px - bar_gap)
            laid.append(lines)
            tot += len(lines) * lh + 2 * py + 16
        chosen = (f, lh, laid)
        if title_bottom + 30 + tot <= zone_bottom - reserve:
            break
    by_ = title_bottom + 30
    if chosen:
        f, lh, laid = chosen
        for i, lines in enumerate(laid):
            widths = [probe.textlength(cv_shape(ln), font=f) for ln in lines]
            panel_w = int(max(widths) if widths else 0) + 2 * px + bar_gap
            panel_h = len(lines) * lh + 2 * py
            x1 = text_right if rtl else text_left + panel_w
            x0 = x1 - panel_w
            full = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            d = ImageDraw.Draw(full)
            d.rounded_rectangle([x0, by_, x1, by_ + panel_h], radius=14, fill=(8, 12, 32, 188), outline=(255, 255, 255, 34))
            if rtl:
                d.rounded_rectangle([x1 - 8, by_ + 9, x1 - 4, by_ + panel_h - 9], radius=2, fill=CV_AMBER + (255,))
                l_left, l_right = x0 + px, x1 - px - bar_gap
            else:
                d.rounded_rectangle([x0 + 4, by_ + 9, x0 + 8, by_ + panel_h - 9], radius=2, fill=CV_AMBER + (255,))
                l_left, l_right = x0 + px + bar_gap, x1 - px
            ty = by_ + py
            for ln in lines:
                cv_draw_line(d, ln, f, ty, l_left, l_right, rtl, (248, 250, 252, 255))
                ty += lh
            lay = cv_layer_from_full(full, "slide", t["points"][i] if i < len(t["points"]) else t["title"] + 1,
                                     0.45, dx=(40 if rtl else -40))
            if lay:
                layers.append(lay)
            by_ += panel_h + 16

    # ---- educational visual (Fast Video: varied forms, not PDF-only)
    if visual is not None:
        vx = margin - 10 if rtl else W - margin - visual.width + 10
        vy = zone_top + 18
        if card_pos is None:
            vx = (W - visual.width) // 2
            vy = zone_top + 20
        layers.append(cv_layer(visual, vx, vy, "card", t.get("visual", t["card"]), 0.55, dx=(-480 if rtl else 480)))

    # ---- page card
    if card is not None and card_pos is not None:
        layers.append(cv_layer(card, card_pos[0], card_pos[1], "card", t["card"], 0.6, dx=(-560 if rtl else 560)))

    # ---- punchline sticker
    if punchline:
        sf = cv_font(30, True)
        shaped = cv_shape(punchline)
        tw = probe.textlength(shaped, font=sf)
        sw_, sh_ = int(tw) + 46, 60
        canvas = Image.new("RGBA", (sw_ + 60, sh_ + 60), (0, 0, 0, 0))
        cd = ImageDraw.Draw(canvas)
        cd.rounded_rectangle([30, 30, 30 + sw_, 30 + sh_], radius=15, fill=CV_AMBER + (255,))
        cd.text((30 + (sw_ - tw) / 2, 30 + 10), shaped, font=sf, fill=(15, 23, 42, 255))
        canvas = cv_shadow(canvas, blur=6, dy=5)
        canvas = canvas.rotate(4 if rtl else -4, resample=Image.BICUBIC, expand=True)
        if card_pos is not None:
            sx = card_pos[0] + card.width // 2 - canvas.width // 2
            sy = card_pos[1] + card.height - canvas.height - 40
        else:
            sx = (text_left + text_right) // 2 - canvas.width // 2
            sy = min(by_ + 6, zone_bottom - canvas.height + 20)
        layers.append(cv_layer(canvas, sx, sy, "pop", t["punch"], 0.3))

    layers.append(cv_chrome(concept_title, scene_no, total_scenes, rtl))
    return layers


# ------------------------------------------------------------ narration ---
def cv_clean_speech(text: str) -> str:
    text = re.sub(r"[*_`#>|]+", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()


def cv_tts(text: str, lang: str, out_mp3: str) -> bool:
    # Natural storyteller pacing: slower than the old +8% setting, with
    # deliberate pauses created by punctuation. This avoids the
    # "reading/rapping" effect while keeping Fast Video lightweight.
    text = cv_clean_speech(text)
    if not text:
        return False
    is_en = (lang or "ar").lower().startswith("en")
    voice = os.environ.get("VIDEO_TTS_VOICE_EN" if is_en else "VIDEO_TTS_VOICE_AR", "").strip()
    if not voice:
        voice = "en-GB-RyanNeural" if is_en else "ar-EG-ShakirNeural"
    rate = os.environ.get("VIDEO_TTS_RATE", "-6%")
    pitch = os.environ.get("VIDEO_TTS_PITCH", "-1Hz")
    try:
        import edge_tts

        async def _go():
            await edge_tts.Communicate(text, voice, rate=rate, pitch=pitch).save(out_mp3)

        asyncio.run(_go())
        if os.path.exists(out_mp3) and os.path.getsize(out_mp3) > 1500:
            return True
    except Exception as exc:
        cv_log("edge-tts failed:", str(exc)[:160])
    try:
        from gtts import gTTS

        gTTS(text=text, lang="en" if is_en else "ar").save(out_mp3)
        if os.path.exists(out_mp3) and os.path.getsize(out_mp3) > 1500:
            return True
    except Exception as exc:
        cv_log("gTTS failed:", str(exc)[:160])
    raise RuntimeError("Narration voice could not be generated. Install edge-tts (recommended) or gTTS and make sure internet access is available.")


def cv_silent_audio(seconds: float, out_mp3: str):
    proc = cv_run([cv_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                   "-i", "anullsrc=r=44100:cl=mono", "-t", f"{seconds:.2f}", "-q:a", "9",
                   "-acodec", "libmp3lame", out_mp3], timeout=60)
    if proc.returncode != 0:
        raise RuntimeError("could not create silent audio: " + (proc.stderr or "")[-300:])


# ------------------------------------------------------------- rendering ---
def cv_scene_points(scene: dict):
    raw = scene.get("onScreenPoints")
    pts = [str(x).strip() for x in raw if str(x).strip()] if isinstance(raw, list) else []
    if not pts:
        for part in re.split(r"[.!?؟،;\n]+", str(scene.get("narration") or "")):
            part = part.strip()
            if len(part) >= 6:
                w = part.split()
                pts.append(" ".join(w[:9]) + ("..." if len(w) > 9 else ""))
            if len(pts) == 3:
                break
    return [p if len(p) <= 80 else p[:77] + "..." for p in pts[:3]]


def cv_build_cinematic(bg_path, layers, audio_path, out_path, total, lead, scene_no, workdir):
    ff = cv_ffmpeg()
    N = max(2, int(total * CV_FPS))
    cmd = [ff, "-y", "-hide_banner", "-loglevel", "error", "-i", bg_path]
    for i, L in enumerate(layers):
        png = os.path.join(workdir, f"L{i}.png")
        L["img"].save(png)
        cmd += ["-loop", "1", "-framerate", str(CV_FPS), "-t", f"{total:.3f}", "-i", png]
    a_idx = len(layers) + 1
    cmd += ["-i", audio_path]
    chord = CV_CHORDS[(scene_no - 1) % len(CV_CHORDS)]
    for fq in chord:
        cmd += ["-f", "lavfi", "-t", f"{total:.3f}", "-i", f"sine=frequency={fq}:sample_rate=44100"]

    variant = (scene_no - 1) % 4
    if variant == 0:
        z, x, y = f"1+0.13*on/{N}", "(iw-iw/zoom)*0.5", "(ih-ih/zoom)*0.5"
    elif variant == 1:
        z, x, y = f"1.13-0.13*on/{N}", "(iw-iw/zoom)*0.5", "(ih-ih/zoom)*0.5"
    elif variant == 2:
        z, x, y = "1.12", f"(iw-iw/zoom)*on/{N}", "(ih-ih/zoom)*0.5"
    else:
        z, x, y = "1.12", f"(iw-iw/zoom)*(1-on/{N})", "(ih-ih/zoom)*0.5"

    parts = [f"[0:v]zoompan=z='{z}':x='{x}':y='{y}':d={N}:s={CV_W}x{CV_H}:fps={CV_FPS},format=yuv420p[v0]"]
    for i, L in enumerate(layers):
        t0, fd, kind = L["t0"], L["fd"], L["kind"]
        lx, ly = L["x"], L["y"]
        fade = f",fade=t=in:st={t0:.3f}:d={fd:.3f}:alpha=1" if fd > 0 else ""
        parts.append(f"[{i + 1}:v]format=rgba{fade}[l{i}]")
        xe, ye = str(lx), str(ly)
        if kind == "title":
            ye = f"{ly}+26*pow(max(0,1-(t-{t0:.3f})/0.6),2)"
        elif kind == "slide":
            xe = f"{lx}+({L['dx']})*pow(max(0,1-(t-{t0:.3f})/0.5),2)"
        elif kind == "card":
            xe = f"{lx}+({L['dx']})*pow(max(0,1-(t-{t0:.3f})/0.9),3)"
            ye = f"{ly}+6*sin(1.4*t)"
        elif kind == "pop":
            ye = f"{ly}+14*pow(max(0,1-(t-{t0:.3f})/0.3),2)"
        parts.append(f"[v{i}][l{i}]overlay=x='{xe}':y='{ye}':format=auto[v{i + 1}]")
    last = f"v{len(layers)}"
    parts.append(
        f"[{last}]eq=contrast=1.05:saturation=1.08,noise=alls=6:allf=t,"
        f"fade=t=in:st=0:d=0.45,fade=t=out:st={max(0.5, total - 0.55):.3f}:d=0.55[vout]"
    )
    ms = int(lead * 1000)
    s0 = a_idx + 1
    parts.append(f"[{a_idx}:a]adelay={ms}|{ms},apad=whole_dur={total:.3f}[av]")
    parts.append(
        f"[{s0}:a][{s0 + 1}:a][{s0 + 2}:a]amix=inputs=3,lowpass=f=650,tremolo=f=0.18:d=0.5,volume=0.45,"
        f"afade=t=in:st=0:d=1.2,afade=t=out:st={max(0.5, total - 1.4):.3f}:d=1.4[pad]"
    )
    parts.append("[av][pad]amix=inputs=2:duration=longest,volume=2[aout]")

    cmd += ["-filter_complex", ";".join(parts), "-map", "[vout]", "-map", "[aout]",
            "-t", f"{total:.3f}", "-r", str(CV_FPS),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2",
            "-movflags", "+faststart", out_path]
    proc = cv_run(cmd, timeout=1500)
    if proc.returncode != 0 or not os.path.exists(out_path):
        raise RuntimeError("ffmpeg cinematic render failed: " + (proc.stderr or "")[-900:])


def cv_build_simple(bg_path, layers, audio_path, out_path, total, lead, workdir):
    """Safety net: one composed still + voice (used only if the cinematic graph fails)."""
    bg = Image.open(bg_path).convert("RGB").resize((CV_W, CV_H), Image.LANCZOS).convert("RGBA")
    for L in layers:
        bg.paste(L["img"], (L["x"], L["y"]), L["img"])
    frame = os.path.join(workdir, "simple.png")
    bg.convert("RGB").save(frame)
    ms = int(lead * 1000)
    cmd = [cv_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
           "-loop", "1", "-framerate", str(CV_FPS), "-i", frame, "-i", audio_path,
           "-af", f"adelay={ms}|{ms},apad=whole_dur={total:.3f}", "-t", f"{total:.3f}",
           "-vf", "format=yuv420p", "-c:v", "libx264", "-preset", "ultrafast", "-tune", "stillimage",
           "-threads", os.environ.get("VIDEO_FFMPEG_THREADS", "1"),
           "-crf", "27", "-c:a", "aac", "-b:a", "96k", "-ar", "44100", "-ac", "2",
           "-movflags", "+faststart", out_path]
    proc = cv_run(cmd, timeout=600)
    if proc.returncode != 0 or not os.path.exists(out_path):
        raise RuntimeError("ffmpeg simple render failed: " + (proc.stderr or "")[-600:])


def cv_render_scene_clip(scene, concept_title, lang, out_path, total_scenes, pdf_path):
    if Image is None:
        raise RuntimeError("Pillow is not installed.  " + CV_PIP_HINT)
    workdir = out_path + "_work"
    os.makedirs(workdir, exist_ok=True)
    try:
        scene_no = int(scene.get("sceneNumber") or 1)
        title = str(scene.get("title") or concept_title or "").strip()
        narration = str(scene.get("narration") or title).strip()
        points = cv_scene_points(scene)
        punch = str(scene.get("punchline") or "").strip()
        if len(punch) > 60:
            punch = punch[:57] + "..."
        rtl = cv_has_arabic(title + " " + " ".join(points))

        audio = os.path.join(workdir, "voice.mp3")
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_tts = pool.submit(cv_tts, narration, lang, audio)
            f_img = pool.submit(cv_ai_image, str(scene.get("visualPrompt") or ""), scene_no)
            ok, ai_img = f_tts.result(), f_img.result()

        if not ok or not os.path.exists(audio) or os.path.getsize(audio) <= 1500:
            raise RuntimeError(f"Scene {scene_no}: narration voice generation failed.")
        dur = cv_duration(audio)
        if dur <= 0.5:
            raise RuntimeError(f"Scene {scene_no}: generated narration has no usable duration.")

        lead, tail = 0.45, 0.9
        total = lead + dur + tail

        card = None
        page_no = cv_first_page(scene)
        if page_no:
            pg = cv_pdf_page(pdf_path, page_no, 350, 470)
            if pg is not None:
                card = cv_page_card(pg, 2.5 if rtl else -2.5)

        n = len(points)
        s0, e0 = lead + 1.3, lead + dur * 0.72
        step = max(0.9, (e0 - s0) / max(1, n))
        timing = {"title": lead - 0.1, "card": lead + 0.15,
                  "points": [s0 + i * step for i in range(n)], "punch": lead + dur * 0.55}
        layers = cv_build_layers(concept_title, scene_no, total_scenes, title, points, punch, card, rtl, timing)

        bg_path = os.path.join(workdir, "bg.jpg")
        cv_make_background(scene_no, ai_img).save(bg_path, quality=90)

        # On small/shared hosts, the full cinematic FFmpeg graph can consume a lot of
        # CPU/RAM and leave the job apparently stuck at 10% while scene 1 renders.
        # The safe renderer still produces a real MP4 with the narration, composed
        # educational visuals, PDF reference card (when available), and on-screen
        # points, but is dramatically lighter. Set VIDEO_RENDER_STYLE=cinematic only
        # on a host with enough resources.
        render_style = os.environ.get("VIDEO_RENDER_STYLE", "safe").strip().lower()
        if render_style == "cinematic":
            try:
                cv_build_cinematic(bg_path, layers, audio, out_path, total, lead, scene_no, workdir)
            except Exception as exc:
                cv_log(f"scene {scene_no}: cinematic render failed, using safe mode: {str(exc)[:300]}")
                cv_build_simple(bg_path, layers, audio, out_path, total, lead, workdir)
        else:
            cv_build_simple(bg_path, layers, audio, out_path, total, lead, workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ------------------------------------------------------------ Veo (paid) ---
def veo_generate_scene_clip(scene: dict, concept_title: str, lang: str, out_path: str):
    narration = str(scene.get("narration") or "").strip()
    visual_prompt = str(scene.get("visualPrompt") or "Educational motion graphics explaining the PDF concept").strip()
    language_note = "Narration in Egyptian Arabic." if lang != "en" else "Narration in clear English."
    prompt = f"""
Create a polished 8-second cinematic educational scene about: {concept_title}

Visual direction:
{visual_prompt}

Audio direction:
- One natural, energetic narrator. {language_note}
- Keep the teaching faithful to this narration:
{narration}

Production: documentary / motion-graphics look, purposeful camera movement, concept clarity first,
no logos, watermarks, copyrighted characters, or dense readable text.
"""
    operation = ai.models.generate_videos(
        model="veo-3.1-generate-preview",
        prompt=prompt,
        config=types.GenerateVideosConfig(
            number_of_videos=1,
            resolution=os.environ.get("VIDEO_RESOLUTION", "720p"),
            aspect_ratio=os.environ.get("VIDEO_ASPECT_RATIO", "16:9"),
        ),
    )
    poll_seconds = max(5, int(os.environ.get("VEO_POLL_SECONDS", "10")))
    max_wait = max(180, int(os.environ.get("VEO_MAX_WAIT_SECONDS", "900")))
    started = time.time()
    while not operation.done:
        if time.time() - started > max_wait:
            raise TimeoutError(f"Veo generation exceeded {max_wait} seconds")
        time.sleep(poll_seconds)
        operation = ai.operations.get(operation)
    if getattr(operation, "error", None):
        raise RuntimeError(str(operation.error))
    generated = operation.response.generated_videos[0]
    ai.files.download(file=generated.video, destination=out_path)


def generate_scene_clip(scene, concept_title, lang, out_path, total_scenes, pdf_path):
    """One scene -> one MP4.  Veo only if asked for; otherwise (or on any failure) cinematic."""
    if VIDEO_ENGINE == "veo" and time.time() > CV_VEO_DISABLED_UNTIL["t"]:
        try:
            veo_generate_scene_clip(scene, concept_title, lang, out_path)
            CV_CLIP_ENGINE[out_path] = "veo"
            return
        except Exception as exc:
            cv_log("Veo failed -> cinematic engine for this scene:", str(exc)[:200])
            up = str(exc).upper()
            if any(k in up for k in ("429", "RESOURCE_EXHAUSTED", "QUOTA", "PERMISSION", "403", "BILLING", "404")):
                CV_VEO_DISABLED_UNTIL["t"] = time.time() + 600
    cv_render_scene_clip(scene, concept_title, lang, out_path, total_scenes, pdf_path)
    CV_CLIP_ENGINE[out_path] = "cinematic"


def merge_video_clips(clip_paths: list[str], output_path: str):
    ff = cv_ffmpeg()
    list_path = output_path + ".txt"
    with open(list_path, "w", encoding="utf-8") as fh:
        for clip in clip_paths:
            fh.write(f"file '{cv_concat_path(clip)}'\n")
    mixed = len({CV_CLIP_ENGINE.get(p, "unknown") for p in clip_paths}) > 1
    try:
        if not mixed:
            proc = cv_run([ff, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
                           "-i", list_path, "-c", "copy", "-movflags", "+faststart", output_path], timeout=900)
            if proc.returncode == 0 and os.path.exists(output_path):
                return
        vf = (f"scale={CV_W}:{CV_H}:force_original_aspect_ratio=decrease,"
              f"pad={CV_W}:{CV_H}:(ow-iw)/2:(oh-ih)/2,fps={CV_FPS},format=yuv420p")
        proc2 = cv_run([ff, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
                        "-i", list_path, "-vf", vf, "-c:v", "libx264", "-preset", "ultrafast", "-crf", "27",
                        "-threads", os.environ.get("VIDEO_FFMPEG_THREADS", "1"),
                        "-c:a", "aac", "-b:a", "96k", "-ar", "44100", "-ac", "2",
                        "-movflags", "+faststart", output_path], timeout=1800)
        if proc2.returncode != 0:
            raise RuntimeError("ffmpeg merge failed: " + (proc2.stderr or "")[-1500:])
    finally:
        try:
            os.remove(list_path)
        except OSError:
            pass


def cv_report_dependencies():
    missing = []
    if Image is None:
        missing.append("pillow")
    if arabic_reshaper is None or _bidi_display is None:
        missing.append("arabic-reshaper + python-bidi")
    if _pymupdf is None:
        missing.append("pymupdf (to show the real PDF pages)")
    try:
        import edge_tts  # noqa: F401
    except Exception:
        try:
            import gtts  # noqa: F401
        except Exception:
            missing.append("edge-tts or gTTS (narration voice)")
    try:
        cv_ffmpeg()
    except Exception:
        missing.append("imageio-ffmpeg (or ffmpeg in PATH)")
    cv_log(f"engine={VIDEO_ENGINE}")
    if missing:
        cv_log("MISSING ->", ", ".join(missing))
        cv_log("Run:", CV_PIP_HINT)


cv_report_dependencies()


# -----------------------------------------------------------------------------
# Video job bookkeeping
# -----------------------------------------------------------------------------
def video_job_update(job_id: str, **fields):
    fields["updated_at"] = datetime.now(APP_TZ).isoformat()
    assignments = ", ".join(f"{key} = ?" for key in fields)
    values = list(fields.values()) + [job_id]
    conn = get_db()
    cur = conn.cursor()
    cur.execute(f"UPDATE video_jobs SET {assignments} WHERE id = ?", values)
    conn.commit()
    conn.close()

def video_job_get(job_id: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM video_jobs WHERE id = ?", (job_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


# -----------------------------------------------------------------------------
# Lesson plan: fast, funny, sarcastic Egyptian explainer style
# -----------------------------------------------------------------------------
def generate_custom_lesson_plan(pdf_bytes: bytes, filename: str, lang: str, perspective: int):
    perspective_name = {
        1: "الأساس العلمي والشرح المبدئي",
        2: "بنستخدمها في إيه في الحياة؟",
        3: "أفكار عالية وحل مسائل الامتحانات",
    }.get(perspective, "الأساس العلمي والشرح المبدئي")

    if str(lang).lower() != "en":
        voice_rules = """
LANGUAGE & VOICE (Arabic mode):
- Write like a very clear Egyptian teacher explaining to a university student, not like a textbook and not like a comedy script.
- Use simple Egyptian Arabic for the explanation, while keeping the PDF's exact scientific/technical terms when needed.
- If a technical term first appears, explain it in plain Arabic immediately, then optionally put the English term in parentheses.
- One idea per sentence. Prefer short sentences, natural pauses, and familiar words. Avoid long nested sentences, academic filler, and difficult formal Arabic.
- Explain the WHY and HOW, not just repeat the PDF. When the PDF contains an example, use that example. If it contains an equation, explain what each important symbol means in words before using it.
- Use a simple analogy only when it genuinely clarifies the PDF concept, and explicitly connect the analogy back to the real concept. Do not force jokes or random analogies.
- Speak directly to the student with calm, natural phrases such as: "خلينا نفهمها ببساطة", "الفكرة هنا", "طب ليه؟", "بمعنى أبسط".
- Do not imitate or name any real presenter, creator, or fictional character.
"""
    else:
        voice_rules = """
LANGUAGE & VOICE (English mode):
- Use clear, natural spoken English like an excellent teacher explaining a difficult university topic to a student.
- Prefer short sentences and familiar words. Explain technical terms in simple English before using them repeatedly.
- Explain the idea, the reason, and the meaning of important equations or examples instead of reading the PDF word-for-word.
- Use calm pacing, natural pauses, and direct address. Avoid forced jokes, sarcasm, and overly academic wording.
"""

    prompt = f"""
You are the script director of S-Tesseract, making a CINEMATIC, FAST, FUNNY, SARCASTIC explainer video.
The student uploaded a PDF named {filename!r}. Perspective: {perspective_name}
{voice_rules}
STYLE:
- Notebook-style teaching flow: start with the core idea, make it intuitive, then connect it to the PDF's technical definition, example, equation, or diagram.
- The student should understand the idea before hearing the difficult terminology.
- Do not read or paraphrase long PDF sentences mechanically. Rewrite them into natural spoken teaching language while preserving the source meaning.
- Use small transitions between ideas so the narration feels like one lesson, not eight disconnected summaries.
- Humor is optional and very light. Clarity is more important than entertainment.
- Never claim to be, quote, imitate, or name any real presenter or creator.

ACCURACY (non-negotiable):
- The PDF is the only source. Every teaching claim, definition, classification, equation or number must come from the PDF.
- Jokes never change the science. Do not invent facts. If the PDF is unclear, say so with a joke.

ARC (exactly 8 scenes):
1 = absurd/relatable hook question. 2-3 = build the core idea with an analogy. 4-6 = go deeper (rules, classification, equations from the PDF).
7 = exam trap / common mistake from the PDF. 8 = fast recap + final punchline.

PER SCENE:
- "title": catchy, max 6 words.
- "narration": 60 to 95 words of simple spoken teaching. Do not copy sentences from the PDF. Explain the idea in your own clear words while preserving the PDF meaning.
- Start each scene with the idea in simple language, then introduce the technical term or detail.
- Include a short concrete example when the PDF provides one. For equations, explain the variables in words.
- "onScreenPoints": 2 or 3 short keywords/facts, max 7 words each (must be true per the PDF).
- "punchline": optional short takeaway, max 8 words. It must summarize the lesson, not be a joke.
- "visualPrompt": ONE English sentence describing a cinematic visual metaphor for the scene (no text in the image).
- "sourcePages": PDF page numbers supporting the scene; put FIRST the page whose figure/table/equation best illustrates it; [] only if unknown.

Return STRICT JSON only:
{{
  "conceptTitle": "...",
  "perspectiveTitle": "{perspective_name}",
  "hook": "...",
  "lessonSummary": "...",
  "scenes": [
    {{"sceneNumber": 1, "title": "...", "narration": "...", "onScreenPoints": ["...", "..."],
      "punchline": "...", "visualPrompt": "...", "sourcePages": [1]}}
  ],
  "keyInsights": ["...", "...", "...", "..."],
  "engineeringCaseStudy": {{"title": "...", "problem": "...", "solution": "..."}},
  "examInsights": ["...", "...", "..."],
  "recap": ["...", "...", "..."],
  "quizQuestions": [
    {{"question":"...","options":["A","B","C","D"],"correctIndex":0,"explanation":"..."}},
    {{"question":"...","options":["A","B","C","D"],"correctIndex":1,"explanation":"..."}},
    {{"question":"...","options":["A","B","C","D"],"correctIndex":2,"explanation":"..."}}
  ]
}}
Rules: exactly 8 scenes, exactly 3 quiz questions; quiz answers must be derivable from the PDF.
The keyInsights/recap/engineeringCaseStudy/quiz texts follow the same language & tone (light humor is fine, accuracy first).
"""
    contents = [types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"), prompt]
    last = ""
    plan = {}
    for _ in range(2):
        plan = generate_ai_json(prompt, contents)
        scenes = plan.get("scenes", []) if isinstance(plan, dict) else []
        if isinstance(scenes, list) and len(scenes) >= 8:
            plan["scenes"] = scenes[:8]
            for i, sc in enumerate(plan["scenes"], start=1):
                sc["sceneNumber"] = i
                if not str(sc.get("narration") or "").strip():
                    sc["narration"] = str(sc.get("title") or plan.get("conceptTitle") or "")
            return plan
        last = f"got {len(scenes) if isinstance(scenes, list) else 0} scenes"
        cv_log("lesson plan retry:", last)
    raise ValueError(f"The AI lesson plan must contain 8 scenes ({last}).")


def _fast_story_narration(chunk: str, scene_no: int, total_scenes: int, lang: str) -> str:
    """Turn source text into easier spoken language without inventing facts.

    Fast Video is intentionally non-AI, so it cannot perform true semantic
    rewriting like NotebookLM. This function therefore removes common PDF
    formatting noise, breaks long sentences, and adds gentle teaching
    transitions without changing the source facts.
    """
    raw = re.sub(r"\s+", " ", chunk or "").strip()
    if not raw:
        return ""

    # Remove common document noise that sounds terrible when spoken.
    raw = re.sub(r"(?i)\b(page|figure|fig\.|table|chapter|section)\s*[-.:]?\s*\d+\b", "", raw)
    raw = re.sub(r"\[[0-9,;\s-]+\]", "", raw)
    raw = re.sub(r"\s+", " ", raw).strip()

    if str(lang).lower().startswith("en"):
        openers = [
            "Let's make this simple. ",
            "Here's the main idea. ",
            "Now let's break it down. ",
            "The important part is this. ",
        ]
        closer = " That's the idea to keep in mind."
    else:
        openers = [
            "خلينا نفهم الفكرة ببساطة. ",
            "بص على الفكرة الأساسية هنا. ",
            "تعالى نقسمها لخطوات بسيطة. ",
            "المهم هنا إننا نفهم السبب. ",
        ]
        closer = " دي الفكرة الأساسية اللي محتاج تفتكرها."

    # Break long runs into speech-sized sentences. We keep the original words
    # because Fast Video must remain source-faithful and must not hallucinate.
    sentences = [x.strip() for x in re.split(r"(?<=[.!?؟])\s+", raw) if x.strip()]
    if not sentences:
        sentences = [raw]
    spoken = []
    for sent in sentences:
        words = sent.split()
        if len(words) <= 22:
            spoken.append(sent)
            continue
        for i in range(0, len(words), 18):
            part = " ".join(words[i:i+18]).strip()
            if part:
                spoken.append(part + ("." if i + 18 < len(words) else ""))

    body = " ".join(spoken)
    body = re.sub(r"\s*[:;]\s*", ". ", body)
    body = re.sub(r"\s*[—–-]\s*", ". ", body)
    body = re.sub(r"\.{2,}", ".", body).strip()
    return openers[(scene_no - 1) % len(openers)] + body + closer


def _fast_video_plan(pdf_bytes: bytes, filename: str, lang: str, perspective: int):
    """Deterministic Fast Video plan: source-faithful, but narrated like a lesson."""
    text = _extract_pdf_text(pdf_bytes)
    if not text.strip():
        raise ValueError("Fast Video needs a text-based PDF. This PDF appears scanned/image-only; use AI Video or OCR first.")
    paragraphs = [re.sub(r"\s+", " ", x).strip() for x in re.split(r"\n\s*\n|(?<=[.!?])\s+", text) if x.strip()]
    paragraphs = [x for x in paragraphs if len(x) >= 20]
    if not paragraphs:
        raise ValueError("Could not extract enough readable text from this PDF for Fast Video.")
    chunks = []
    current = ""
    for para in paragraphs:
        if len(current) + len(para) > 520 and current:
            chunks.append(current.strip()); current = ""
        current += (" " if current else "") + para
    if current:
        chunks.append(current.strip())
    chunks = chunks[:8]

    title = (filename.rsplit(".",1)[0] if "." in filename else filename).strip() or "PDF Lesson"
    scenes = []
    for i, chunk in enumerate(chunks, 1):
        short = chunk[:500].strip()
        first_sentence = re.split(r"(?<=[.!?؟])\s+", short)[0].strip()
        spoken = _fast_story_narration(short, i, len(chunks), lang)
        title_words = re.findall(r"\S+", first_sentence)[:8]
        scene_title = " ".join(title_words).strip(" .,:؛،") or f"{title[:38]} — {i}"
        points = [x.strip()[:80] for x in re.split(r"[.;،:؛]", short) if x.strip()][:3]
        # The punchline is a source-derived takeaway, not a made-up fact.
        takeaway = first_sentence[:70].rstrip(" .،؛:")
        stype = cv_scene_type(scene_title, spoken, points)
        scenes.append({
            "sceneNumber": i,
            "title": scene_title,
            "narration": spoken,
            "onScreenPoints": points,
            "punchline": ("الخلاصة: " + takeaway) if lang != "en" else ("Key idea: " + takeaway),
            "visualPrompt": "",
            "visualType": stype,
            "sourcePages": []
        })
    return {
        "conceptTitle": title[:120],
        "perspectiveTitle": "Fast Video — source-based" if lang == "en" else "فيديو سريع — شرح قصصي من محتوى الـPDF",
        "hook": "شرح مباشر من الملف مع إيقاع قصصي وتوقفات صوتية — بدون توليد AI." if lang != "en" else "A source-faithful explanation with storyteller pacing — no AI script generation.",
        "lessonSummary": "Fast Video uses extracted PDF text, natural-paced TTS, PDF pages/simple graphics and FFmpeg." if lang == "en" else "Fast Video بيستخدم نص الـPDF مع TTS بإيقاع طبيعي وصفحات الـPDF وجرافيكس بسيطة وFFmpeg.",
        "scenes": scenes,
        "keyInsights": [s["narration"][:180] for s in scenes[:4]],
        "engineeringCaseStudy": {},
        "examInsights": [],
        "recap": [s["narration"][:180] for s in scenes[-3:]],
        "quizQuestions": [],
        "sourceFile": filename,
        "perspective": perspective,
    }


def cv_render_fast_scene_clip(scene, concept_title, lang, out_path, total_scenes, pdf_path):
    """Fast renderer: TTS + real PDF page/simple background + FFmpeg. Never calls Gemini/image generation."""
    if Image is None:
        raise RuntimeError("Pillow is not installed. " + CV_PIP_HINT)
    workdir = out_path + "_work"
    os.makedirs(workdir, exist_ok=True)
    try:
        scene_no = int(scene.get("sceneNumber") or 1)
        title = str(scene.get("title") or concept_title or "").strip()
        narration = str(scene.get("narration") or title).strip()
        points = cv_scene_points(scene)
        punch = str(scene.get("punchline") or "").strip()[:60]
        rtl = cv_has_arabic(title + " " + " ".join(points))
        audio = os.path.join(workdir, "voice.mp3")
        ok = cv_tts(narration, lang, audio)
        if not ok or not os.path.exists(audio) or os.path.getsize(audio) <= 1500:
            raise RuntimeError(f"Scene {scene_no}: narration voice generation failed.")
        dur = cv_duration(audio)
        if dur <= 0.5:
            raise RuntimeError(f"Scene {scene_no}: generated narration has no usable duration.")
        card = None
        # PDF is a reference, not the default visual. Show it only when explicitly mapped.
        page_no = cv_first_page(scene)
        if page_no and str(scene.get("visualType") or "") == "pdf_reference":
            pg = cv_pdf_page(pdf_path, page_no, 350, 470)
            if pg is not None:
                card = cv_page_card(pg, 2.5 if rtl else -2.5)
        scene_type = str(scene.get("visualType") or cv_scene_type(title, narration, points))
        visual = cv_visual_card(scene, scene_type, rtl)
        lead, tail = 0.35, 0.65
        total = lead + dur + tail
        timing = {"title": lead, "card": lead + 0.1, "visual": lead + 0.18, "points": [lead + 0.8 + i*0.7 for i in range(len(points))], "punch": lead + dur*0.55}
        layers = cv_build_layers(concept_title, scene_no, total_scenes, title, points, punch, card, rtl, timing, visual=visual)
        # No AI image. cv_make_background(None) is deterministic local rendering.
        bg = cv_make_background(scene_no, None)
        bg_path = os.path.join(workdir, "bg.jpg")
        bg.save(bg_path, quality=88)
        # Fast Video stays lightweight on small hosting plans.
        cv_build_simple(bg_path, layers, audio, out_path, total, lead, workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def run_fast_video_job(job_id: str, pdf_bytes: bytes, filename: str, lang: str, perspective: int):
    job_dir = os.path.join(GENERATED_VIDEO_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    source_pdf = os.path.join(job_dir, "source.pdf")
    try:
        with open(source_pdf, "wb") as f: f.write(pdf_bytes)
        video_job_update(job_id, status="processing", progress=3, stage="استخراج النص من الـPDF — بدون AI")
        plan = await asyncio.to_thread(_fast_video_plan, pdf_bytes, filename, lang, perspective)
        scenes = plan.get("scenes") or []
        with open(os.path.join(job_dir, "lesson_plan.json"), "w", encoding="utf-8") as f:
            json.dump(plan, f, ensure_ascii=False, indent=2)
        clips=[]
        for idx, scene in enumerate(scenes, 1):
            video_job_update(job_id, status="processing", progress=8 + int((idx-1)*84/max(1,len(scenes))), stage=f"Fast Video: صوت + مشهد {idx}/{len(scenes)}")
            clip=os.path.join(job_dir, f"scene_{idx:02d}.mp4")
            await asyncio.to_thread(cv_render_fast_scene_clip, scene, plan.get("conceptTitle",""), lang, clip, len(scenes), source_pdf)
            clips.append(clip)
        video_job_update(job_id, status="processing", progress=94, stage="Fast Video: المونتاج النهائي")
        final=os.path.join(job_dir,"lesson.mp4")
        await asyncio.to_thread(merge_video_clips, clips, final)
        video_job_update(job_id, status="completed", progress=100, stage="Fast Video جاهز", result_json=json.dumps(plan,ensure_ascii=False), video_url=f"/generated-videos/{job_id}/lesson.mp4", source_pdf=source_pdf, error="")
    except Exception as exc:
        traceback.print_exc(); video_job_update(job_id,status="failed",progress=0,stage="فشل Fast Video",error=str(exc))


def run_youtube_video_job(job_id: str, filename: str, lang: str, material_title: str = "", pdf_bytes: bytes = b""):
    title = material_title or filename.rsplit(".",1)[0] or "study topic"
    # YouTube mode intentionally uses search links rather than pretending we selected exact videos.
    # When possible, enrich the search topic from the PDF's first readable text.
    topic = title
    if pdf_bytes:
        try:
            extracted = _extract_pdf_text(pdf_bytes)
            first = re.sub(r"\s+", " ", extracted).strip()[:220]
            if first and len(first) > len(topic):
                topic = f"{title} {first[:140]}"
        except Exception:
            pass
    links = _youtube_results(topic, 6)
    result = {
        "conceptTitle": title[:120],
        "perspectiveTitle": "فيديوهات جاهزة من YouTube" if lang != "en" else "Ready YouTube videos",
        "hook": "هنجهزلك عمليات بحث مرتبطة بالمادة علشان تلاقي شروحات مناسبة على YouTube." if lang != "en" else "Relevant YouTube searches based on your material.",
        "lessonSummary": "دي روابط بحث على YouTube وليست فيديوهات مختارة يدويًا." if lang != "en" else "These are YouTube search resources, not manually selected exact videos.",
        "scenes": [], "keyInsights": [], "engineeringCaseStudy": {}, "examInsights": [], "recap": [], "quizQuestions": [],
        "youtube": links, "sourceFile": filename
    }
    video_job_update(job_id, status="completed", progress=100, stage="جاري تجهيز فيديوهات مناسبة...", result_json=json.dumps(result,ensure_ascii=False), youtube_json=json.dumps(links,ensure_ascii=False), video_url="", error="")


async def run_video_job_dispatch(job_id: str, pdf_bytes: bytes, filename: str, lang: str, perspective: int, mode: str):
    mode = str(mode or "ai").lower().strip()
    if mode == "fast":
        return await run_fast_video_job(job_id, pdf_bytes, filename, lang, perspective)
    if mode == "youtube":
        return await asyncio.to_thread(run_youtube_video_job, job_id, filename, lang, filename.rsplit(".",1)[0], pdf_bytes)
    if mode == "both":
        # Both = AI generated video + YouTube resources.
        await run_custom_video_job(job_id, pdf_bytes, filename, lang, perspective)
        row = video_job_get(job_id)
        if row and row.get("status") == "completed":
            links = _youtube_results(filename.rsplit(".",1)[0], 6)
            video_job_update(job_id, youtube_json=json.dumps(links,ensure_ascii=False))
            row = video_job_get(job_id)
            data = json.loads(row.get("result_json") or "{}")
            data["youtube"] = links
            video_job_update(job_id, result_json=json.dumps(data,ensure_ascii=False))
        return
    return await run_custom_video_job(job_id, pdf_bytes, filename, lang, perspective)


async def run_custom_video_job(job_id: str, pdf_bytes: bytes, filename: str, lang: str, perspective: int):
    job_dir = os.path.join(GENERATED_VIDEO_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    try:
        source_pdf = os.path.join(job_dir, "source.pdf")
        with open(source_pdf, "wb") as f:
            f.write(pdf_bytes)

        video_job_update(job_id, status="processing", progress=3, stage="تحليل الـPDF وكتابة السيناريو")

        plan = await asyncio.to_thread(generate_custom_lesson_plan, pdf_bytes, filename, lang, perspective)
        scenes = plan.get("scenes", []) if isinstance(plan, dict) else []
        if len(scenes) != 8:
            raise ValueError("The AI lesson plan must contain exactly 8 scenes.")

        with open(os.path.join(job_dir, "lesson_plan.json"), "w", encoding="utf-8") as f:
            json.dump(plan, f, ensure_ascii=False, indent=2)

        concept = plan.get("conceptTitle", "PDF lesson")
        clips = []
        for idx, scene in enumerate(scenes, start=1):
            base_progress = 10 + int((idx - 1) * 80 / 8)
            video_job_update(
                job_id, status="processing",
                progress=base_progress,
                stage=f"جاري تجهيز المشهد {idx}/8..."
            )
            clip_path = os.path.join(job_dir, f"scene_{idx:02d}.mp4")
            video_job_update(
                job_id, status="processing",
                progress=min(92, base_progress + 2),
                stage=f"جاري إنشاء صوت ورسومات المشهد {idx}/8..."
            )
            await asyncio.to_thread(generate_scene_clip, scene, concept, lang, clip_path, len(scenes), source_pdf)
            if not os.path.exists(clip_path) or os.path.getsize(clip_path) < 1000:
                raise RuntimeError(f"Scene {idx} was not rendered correctly.")
            clips.append(clip_path)
            video_job_update(
                job_id, status="processing",
                progress=min(92, base_progress + 10),
                stage=f"تم تجهيز المشهد {idx}/8 — الانتقال للمشهد التالي..."
            )

        video_job_update(job_id, status="processing", progress=93, stage="المونتاج النهائي")
        final_path = os.path.join(job_dir, "lesson.mp4")
        await asyncio.to_thread(merge_video_clips, clips, final_path)

        result = {
            "conceptTitle": plan.get("conceptTitle", ""),
            "perspectiveTitle": plan.get("perspectiveTitle", ""),
            "hook": plan.get("hook", ""),
            "lessonSummary": plan.get("lessonSummary", ""),
            "scenes": plan.get("scenes", []),
            "keyInsights": plan.get("keyInsights", []),
            "engineeringCaseStudy": plan.get("engineeringCaseStudy", {}),
            "examInsights": plan.get("examInsights", []),
            "recap": plan.get("recap", []),
            "quizQuestions": plan.get("quizQuestions", []),
            "sourceFile": filename,
            "perspective": perspective,
        }

        video_job_update(
            job_id, status="completed", progress=100, stage="الفيديو جاهز",
            result_json=json.dumps(result, ensure_ascii=False),
            video_url=f"/generated-videos/{job_id}/lesson.mp4",
            source_pdf=source_pdf, error=""
        )
    except Exception as exc:
        traceback.print_exc()
        video_job_update(job_id, status="failed", progress=0, stage="فشل إنشاء الفيديو", error=str(exc))


# -----------------------------------------------------------------------------
# Durable video worker: the browser can close without stopping the job.
# AI is used for the lesson script; the local cinematic pipeline renders scenes.
# -----------------------------------------------------------------------------
_video_worker_task = None

def _run_video_job_dispatch_in_worker_thread(job_id: str, pdf_bytes: bytes, filename: str, lang: str, perspective: int, mode: str):
    """Run the whole video coroutine outside FastAPI's main event-loop thread.

    Video rendering uses PIL/FFmpeg/TTS and other CPU/blocking operations.
    Keeping it on the ASGI event-loop thread can make Blitz's proxy see the
    application as unhealthy and return an HTML 503 while a job starts.
    """
    asyncio.run(run_video_job_dispatch(job_id, pdf_bytes, filename, lang, perspective, mode))


async def _video_worker_loop():
    while True:
        try:
            conn=get_db(); row=conn.execute("SELECT * FROM video_jobs WHERE status='queued' ORDER BY created_at ASC LIMIT 1").fetchone(); conn.close()
            if not row:
                await asyncio.sleep(2); continue
            try:
                with open(row["source_pdf"],"rb") as f: pdf_bytes=f.read()
                # IMPORTANT: never execute the heavy video pipeline on the
                # FastAPI/uvicorn event loop. This keeps /api/* responsive
                # while FFmpeg/PIL/TTS/Gemini work is running.
                await asyncio.to_thread(
                    _run_video_job_dispatch_in_worker_thread,
                    row["id"], pdf_bytes, row["filename"] or "uploaded.pdf",
                    row["language"] or "ar", int(row["perspective"] or 1),
                    (row["mode"] if "mode" in row.keys() else "ai") or "ai"
                )
                if row["user_id"]:
                    r=video_job_get(row["id"])
                    if r and r.get("status")=="completed":
                        _push_notification(int(row["user_id"]),"الفيديو جاهز 🎬",f"الفيديو الخاص بـ {row['filename'] or 'المادة'} أصبح جاهزًا.","video")
                        _audit(int(row["user_id"]),"video_completed",f"job={row['id']}")
            except Exception as exc:
                video_job_update(row["id"],status="failed",progress=0,stage="فشل إنشاء الفيديو",error=str(exc))
                if row["user_id"]:
                    _push_notification(int(row["user_id"]),"فشل إنشاء الفيديو",str(exc)[:500],"error")
        except Exception:
            traceback.print_exc()
            await asyncio.sleep(3)

@app.on_event("startup")
async def _start_s_tesseract_workers():
    global _video_worker_task
    if _video_worker_task is None or _video_worker_task.done():
        _video_worker_task=asyncio.create_task(_video_worker_loop())

# =============================================================================
# Unified Phase 2/3 Study Center
# =============================================================================

def _study_user_exists(user_id: int):
    conn = get_db()
    row = conn.execute("SELECT id FROM users WHERE id=?", (int(user_id),)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    return int(user_id)


def _current_user_id(request: Request) -> int | None:
    # Companion apps send "Authorization: Bearer <token>"; the website uses the cookie.
    token = companion_auth.bearer_token(request) or request.cookies.get(USER_COOKIE, "")
    if not token:
        return None
    conn = get_db()
    row = conn.execute("SELECT user_id,expires_at FROM user_sessions WHERE token_hash=?", (_hash_token(token),)).fetchone()
    conn.close()
    if not row:
        return None
    try:
        if datetime.fromisoformat(row["expires_at"]) <= datetime.now(APP_TZ):
            return None
    except Exception:
        return None
    return int(row["user_id"])

app.include_router(companion_auth.build_router(get_db, _hash_token, _current_user_id, APP_TZ, _audit))
# Put your built app files here (e.g. S-Tesseract-Companion-1.0.0.apk / .exe). Override with COMPANION_DOWNLOAD_DIR.
COMPANION_DOWNLOAD_DIR = os.environ.get("COMPANION_DOWNLOAD_DIR", os.path.join(BASE_DIR, "downloads"))
os.makedirs(COMPANION_DOWNLOAD_DIR, exist_ok=True)
app.include_router(companion_auth.build_downloads_router(COMPANION_DOWNLOAD_DIR))

# Exam Studio: uploads live OUTSIDE the publicly served /study-uploads folder.
EXAM_SOURCE_DIR = os.environ.get("EXAM_SOURCE_DIR", os.path.join(_default_db_dir, "exam_sources"))
exam_studio.migrate(get_db)
app.include_router(exam_studio.build_router(get_db, _current_user_id, generate_ai_json, ai, APP_TZ, EXAM_SOURCE_DIR))

def _require_user(request: Request, user_id: int | None = None) -> int:
    uid = _current_user_id(request)
    if not uid:
        raise HTTPException(status_code=401, detail="Login required")
    if user_id is not None and int(user_id) != int(uid):
        raise HTTPException(status_code=403, detail="Not your data")
    return uid

# Cross-discipline Learning Studio (safe remote code runner; work is user-owned and persisted).
import learning_studio
learning_studio.migrate(get_db)
app.include_router(learning_studio.build_router(get_db, _require_user, generate_ai_json, APP_TZ))

def _push_notification(user_id: int, title: str, message: str, kind: str = "info"):
    try:
        conn=get_db()
        conn.execute("INSERT INTO app_notifications(user_id,title,message,kind,created_at) VALUES(?,?,?,?,?)",
                     (int(user_id), title[:200], message[:2000], kind[:40], datetime.now(APP_TZ).isoformat()))
        conn.commit(); conn.close()
    except Exception:
        pass

def _make_video_source_hash(pdf_bytes: bytes, lang: str, perspective: int, mode: str = "ai") -> str:
    h=hashlib.sha256()
    safe_mode = str(mode or "ai").lower().strip()
    h.update(pdf_bytes); h.update(f"|{lang}|{perspective}|{safe_mode}|video-v3".encode())
    return h.hexdigest()

def _extract_pdf_text(pdf_bytes: bytes) -> str:
    try:
        import fitz
    except Exception as exc:
        raise RuntimeError("PyMuPDF is required for PDF text extraction.") from exc
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    chunks = []
    try:
        for page in doc:
            chunks.append(page.get_text("text") or "")
    finally:
        doc.close()
    text = "\n\n".join(chunks).strip()
    return text[:140000]

def _study_language_instruction(lang: str) -> str:
    return (
        "Respond entirely in Egyptian Arabic. Keep technical terms in English in parentheses when useful."
        if str(lang).lower() != "en"
        else "Respond entirely in clear English."
    )

def _safe_count(value, default=5, maximum=50):
    try:
        n = int(value)
    except Exception:
        n = default
    return max(1, min(maximum, n))

def _question_types(raw: str):
    allowed = {"mcq", "true_false", "short_answer", "word_problem", "multi_step"}
    vals = [x.strip() for x in str(raw or "").split(",") if x.strip() in allowed]
    return vals or ["mcq", "short_answer", "multi_step"]

def _youtube_results(title: str, count: int = 5):
    title = str(title or "study topic").strip()
    queries = [
        f"{title} lecture",
        f"{title} explained",
        f"{title} problems",
        f"{title} tutorial",
        f"{title} revision",
    ][:max(1, min(10, count))]
    return [
        {
            "title": q,
            "url": "https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(q),
            "provider": "YouTube"
        }
        for q in queries
    ]

def _insert_flashcards(user_id, material_id, cards):
    conn = get_db()
    now = datetime.now(APP_TZ)
    created = []
    for card in cards[:50]:
        front = str(card.get("front") or "").strip()
        back = str(card.get("back") or "").strip()
        if not front or not back:
            continue
        due = now.isoformat()
        cur = conn.execute(
            """INSERT INTO flashcards
               (user_id,material_id,front,back,due_at,ease,interval_days,repetitions,last_result)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (user_id, material_id, front[:400], back[:800], due, 2.5, 1, 0, "")
        )
        created.append(cur.lastrowid)
    conn.commit()
    conn.close()
    return created

def _insert_knowledge_nodes(user_id, material_id, nodes):
    conn = get_db()
    now = datetime.now(APP_TZ).isoformat()
    ids = []
    for node in nodes[:30]:
        title = str(node.get("title") or "").strip()
        if not title:
            continue
        cur = conn.execute(
            """INSERT INTO knowledge_nodes
               (user_id,material_id,title,prerequisite,relation,mastery,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (
                user_id, material_id, title[:200],
                str(node.get("prerequisite") or "")[:400],
                str(node.get("relation") or "depends_on")[:60],
                float(node.get("mastery") or 0),
                now
            )
        )
        ids.append(cur.lastrowid)
    conn.commit()
    conn.close()
    return ids

def _create_assessment(user_id, material_id, mode, lang, qtypes, questions):
    conn = get_db()
    now = datetime.now(APP_TZ).isoformat()
    cur = conn.execute(
        """INSERT INTO assessments
           (user_id,material_id,mode,question_types,question_count,language,status,created_at,max_score)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (user_id, material_id, mode, ",".join(qtypes), len(questions), lang, "ready", now, len(questions))
    )
    assessment_id = cur.lastrowid
    for idx, q in enumerate(questions, 1):
        conn.execute(
            """INSERT INTO assessment_questions
               (assessment_id,question_number,question_type,question_text,options_json,
                correct_answer,explanation,rubric_json,source_refs)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                assessment_id, idx,
                str(q.get("type") or "mcq"),
                str(q.get("question") or ""),
                json.dumps(q.get("options") or [], ensure_ascii=False),
                str(q.get("correctAnswer") if q.get("correctAnswer") is not None else ""),
                str(q.get("explanation") or ""),
                json.dumps(q.get("rubric") or {}, ensure_ascii=False),
                json.dumps(q.get("sourcePages") or [], ensure_ascii=False),
            )
        )
    conn.commit()
    conn.close()
    return int(assessment_id)

@app.post("/api/study/pack")
async def create_study_pack(
    user_id: int = Form(...),
    materialTitle: str = Form("Study Material"),
    videoMode: str = Form("generated"),
    videoLanguage: str = Form("ar"),
    assessmentMode: str = Form("understanding"),
    questionTypes: str = Form("mcq,short_answer,word_problem,multi_step"),
    questionCount: int = Form(5),
    pdfFile: UploadFile = File(...)
):
    """One-file workflow: upload PDF -> summary/notes/flashcards/knowledge graph
    -> understanding check or full exam -> YouTube suggestions -> optional video job."""
    _study_user_exists(user_id)
    pdf_bytes = await pdfFile.read()
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="PDF is empty.")
    filename = pdfFile.filename or "study.pdf"
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported.")

    try:
        text_content = await asyncio.to_thread(_extract_pdf_text, pdf_bytes)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    now = datetime.now(APP_TZ).isoformat()
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO study_materials
           (user_id,title,filename,source_type,text_content,created_at)
           VALUES(?,?,?,?,?,?)""",
        (user_id, materialTitle.strip()[:200] or filename, filename[:255], "pdf", text_content, now)
    )
    material_id = cur.lastrowid
    conn.commit()
    conn.close()

    lang = "en" if videoLanguage.lower() == "en" else "ar"
    mode = "full_exam" if str(assessmentMode).lower() in {"full_exam", "exam", "full"} else "understanding"
    qcount = _safe_count(questionCount, 20 if mode == "full_exam" else 5, 50)
    if mode == "full_exam":
        qcount = max(10, qcount)
    qtypes = _question_types(questionTypes)

    lang_instruction = _study_language_instruction(lang)
    pack_prompt = f"""
{lang_instruction}
You are S-Tesseract's source-grounded study architect.
The PDF below is the ONLY source for factual content.

Create:
1) a concise but useful summary
2) structured study notes
3) 12 flashcards
4) a prerequisite/knowledge graph
5) a teach-me explanation strategy
6) 5 common weaknesses/traps if supported by the source

Return STRICT JSON:
{{
 "summary":"...",
 "notes":[{{"title":"...","content":"..."}}],
 "flashcards":[{{"front":"...","back":"..."}}],
 "knowledgeNodes":[{{"title":"...","prerequisite":"...","relation":"depends_on"}}],
 "weaknessHints":["..."],
 "teachMe":"..."
}}
Never invent information absent from the PDF.
"""
    pack = generate_ai_json(pack_prompt, [
        types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
        pack_prompt
    ])
    summary = str(pack.get("summary") or "")
    conn = get_db()
    conn.execute("UPDATE study_materials SET summary=? WHERE id=?", (summary[:20000], material_id))
    conn.commit()
    conn.close()

    # Persist notes.
    conn = get_db()
    for note in (pack.get("notes") or [])[:20]:
        title = str(note.get("title") or "Study Note").strip()
        content = str(note.get("content") or "").strip()
        if title and content:
            conn.execute(
                """INSERT INTO study_notes(user_id,material_id,title,content,created_at,updated_at)
                   VALUES(?,?,?,?,?,?)""",
                (user_id, material_id, title[:200], content[:12000], now, now)
            )
    conn.commit()
    conn.close()

    flashcard_ids = _insert_flashcards(user_id, material_id, pack.get("flashcards") or [])
    node_ids = _insert_knowledge_nodes(user_id, material_id, pack.get("knowledgeNodes") or [])

    exam_prompt = f"""
{lang_instruction}
Create a {mode.replace('_',' ')} from the supplied PDF.
Question types allowed: {", ".join(qtypes)}
Exact number of questions: {qcount}
For every question include:
type, question, options (for MCQ/true_false), correctAnswer, explanation,
rubric, sourcePages.
For word_problem and multi_step, the rubric must contain objective checkpoints and points.
The student may answer those with text, image, or PDF.
The PDF is the only factual source. Return STRICT JSON:
{{"questions":[...]}}
"""
    exam_data = generate_ai_json(exam_prompt, [
        types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
        exam_prompt
    ])
    questions = exam_data.get("questions") or []
    if len(questions) < qcount:
        # Keep the platform usable if the model under-produces.
        qcount = len(questions)
    questions = questions[:qcount]
    assessment_id = _create_assessment(user_id, material_id, mode, lang, qtypes, questions)

    # Add initial weakness hints.
    conn = get_db()
    for hint in (pack.get("weaknessHints") or [])[:5]:
        topic = str(hint).strip()
        if topic:
            conn.execute(
                """INSERT INTO study_weaknesses(user_id,topic,score,evidence,recommended_action,updated_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(user_id,topic) DO UPDATE SET evidence=excluded.evidence,updated_at=excluded.updated_at""",
                (user_id, topic[:200], 0, "AI-identified topic/trap from source", "Review the source and practice related questions.", now)
            )
    conn.commit()
    conn.close()

    result = {
        "success": True,
        "materialId": material_id,
        "assessmentId": assessment_id,
        "assessmentMode": mode,
        "questionCount": len(questions),
        "summary": summary,
        "teachMe": pack.get("teachMe") or "",
        "flashcardIds": flashcard_ids,
        "knowledgeNodeIds": node_ids,
        "youtube": _youtube_results(materialTitle, 5) if videoMode.lower() in {"youtube", "both"} else [],
        "videoMode": videoMode,
        "videoLanguage": lang,
    }

    if videoMode.lower() in {"generated", "ai", "fast", "both"}:
        source_hash = _make_video_source_hash(pdf_bytes, lang, 1, "ai" if videoMode.lower() in {"generated","both"} else videoMode.lower())
        conn = get_db()
        cached = conn.execute("SELECT id FROM video_jobs WHERE user_id=? AND source_hash=? AND status='completed' ORDER BY created_at DESC LIMIT 1", (user_id, source_hash)).fetchone()
        if cached:
            job_id = cached["id"]
            conn.close()
        else:
            job_id = secrets.token_urlsafe(10)
            job_dir = os.path.join(GENERATED_VIDEO_DIR, job_id)
            os.makedirs(job_dir, exist_ok=True)
            source_pdf = os.path.join(job_dir, "source.pdf")
            with open(source_pdf, "wb") as f:
                f.write(pdf_bytes)
            conn.execute(
                """INSERT INTO video_jobs
                   (id,status,progress,stage,filename,perspective,source_pdf,created_at,updated_at,error,user_id,language,source_hash,engine,mode,youtube_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (job_id, "queued", 0, "Queued", filename, 1, source_pdf, now, now, "", user_id, lang, source_hash, "s-tesseract", "ai" if videoMode.lower() in {"generated","both"} else videoMode.lower(), "[]")
            )
            conn.commit()
            conn.close()
            _audit(user_id, "video_queued", f"job={job_id};source=study_pack")
        result["videoJobId"] = job_id

    return result

@app.get("/api/study/assessment/{assessment_id}")
def get_study_assessment(assessment_id: int, user_id: int):
    _study_user_exists(user_id)
    conn = get_db()
    a = conn.execute(
        "SELECT * FROM assessments WHERE id=? AND user_id=?", (assessment_id, user_id)
    ).fetchone()
    if not a:
        conn.close()
        raise HTTPException(status_code=404, detail="Assessment not found")
    rows = conn.execute(
        "SELECT * FROM assessment_questions WHERE assessment_id=? ORDER BY question_number",
        (assessment_id,)
    ).fetchall()
    conn.close()
    questions = []
    for r in rows:
        questions.append({
            "id": r["id"], "number": r["question_number"], "type": r["question_type"],
            "question": r["question_text"], "options": json.loads(r["options_json"] or "[]"),
            "rubric": json.loads(r["rubric_json"] or "{}"),
            "sourcePages": json.loads(r["source_refs"] or "[]")
        })
    return {"success": True, "assessment": dict(a), "questions": questions}

@app.post("/api/study/assessment/submit")
async def submit_study_answer(
    user_id: int = Form(...),
    assessmentId: int = Form(...),
    questionId: int = Form(...),
    answerText: str = Form(""),
    answerFile: UploadFile = File(None)
):
    _study_user_exists(user_id)
    conn = get_db()
    q = conn.execute(
        """SELECT q.*,a.user_id,a.language,a.mode FROM assessment_questions q
           JOIN assessments a ON a.id=q.assessment_id
           WHERE q.id=? AND q.assessment_id=? AND a.user_id=?""",
        (questionId, assessmentId, user_id)
    ).fetchone()
    if not q:
        conn.close()
        raise HTTPException(status_code=404, detail="Question not found")
    existing = conn.execute(
        "SELECT id FROM assessment_submissions WHERE assessment_id=? AND question_id=? AND user_id=?",
        (assessmentId, questionId, user_id)
    ).fetchone()
    if existing:
        conn.close()
        return {"success": True, "alreadySubmitted": True}

    file_path = ""
    file_bytes = b""
    mime = ""
    if answerFile and answerFile.filename:
        file_bytes = await answerFile.read()
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", answerFile.filename)[:120]
        file_path = os.path.join(STUDY_UPLOAD_DIR, f"answer_{assessmentId}_{questionId}_{secrets.token_hex(4)}_{safe_name}")
        with open(file_path, "wb") as f:
            f.write(file_bytes)
        mime = answerFile.content_type or "application/octet-stream"

    qtype = q["question_type"]
    correct = str(q["correct_answer"] or "").strip()
    answer = answerText.strip()
    score = 0.0
    max_score = 1.0
    feedback = ""
    if qtype in {"mcq", "true_false"}:
        norm = lambda x: re.sub(r"\s+", " ", str(x).strip().lower())
        score = 1.0 if norm(answer) == norm(correct) else 0.0
        feedback = str(q["explanation"] or ("Correct." if score else "Incorrect. Review the explanation."))
    else:
        prompt = f"""
{_study_language_instruction(q['language'])}
You are a strict but fair engineering teacher.
Question: {q['question_text']}
Expected answer: {correct}
Rubric: {q['rubric_json']}
Student text answer: {answer if answer else '[See attached answer file]'}
Grade using the rubric. Accept mathematically/scientifically valid alternate methods.
Do not award points for unsupported claims. Return STRICT JSON:
{{"score":0.0,"maxScore":1.0,"verdict":"...","feedback":"...","correctedSteps":["..."],"weakTopics":["..."]}}
"""
        contents = [prompt]
        if file_bytes and (mime.startswith("image/") or mime == "application/pdf"):
            contents.insert(0, types.Part.from_bytes(data=file_bytes, mime_type=mime))
        graded = generate_ai_json(prompt, contents)
        score = float(graded.get("score") or 0)
        max_score = float(graded.get("maxScore") or 1)
        if max_score <= 0:
            max_score = 1
        score = max(0, min(score, max_score))
        feedback = json.dumps(graded, ensure_ascii=False)

        conn2 = get_db()
        for topic in (graded.get("weakTopics") or []):
            topic = str(topic).strip()
            if topic:
                conn2.execute(
                    """INSERT INTO study_weaknesses(user_id,topic,score,evidence,recommended_action,updated_at)
                       VALUES(?,?,?,?,?,?)
                       ON CONFLICT(user_id,topic) DO UPDATE SET score=excluded.score,evidence=excluded.evidence,
                       recommended_action=excluded.recommended_action,updated_at=excluded.updated_at""",
                    (
                        user_id, topic[:200],
                        round((score / max_score) * 100, 1),
                        str(graded.get("feedback") or "")[:1000],
                        "Re-study the concept, then retry a similar problem.",
                        datetime.now(APP_TZ).isoformat()
                    )
                )
        conn2.commit()
        conn2.close()

    conn.execute(
        """INSERT INTO assessment_submissions
           (assessment_id,question_id,user_id,answer_text,answer_file,score,max_score,feedback,graded_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (assessmentId, questionId, user_id, answer[:12000], file_path, score, max_score, feedback[:16000], datetime.now(APP_TZ).isoformat())
    )
    conn.commit()
    # Update assessment progress.
    total = conn.execute("SELECT COUNT(*) n FROM assessment_questions WHERE assessment_id=?", (assessmentId,)).fetchone()["n"]
    done = conn.execute("SELECT COUNT(*) n FROM assessment_submissions WHERE assessment_id=?", (assessmentId,)).fetchone()["n"]
    if done >= total and total:
        agg = conn.execute(
            "SELECT COALESCE(SUM(score),0) s,COALESCE(SUM(max_score),0) m FROM assessment_submissions WHERE assessment_id=?",
            (assessmentId,)
        ).fetchone()
        conn.execute(
            "UPDATE assessments SET status='completed',score=?,max_score=?,completed_at=? WHERE id=?",
            (agg["s"], agg["m"], datetime.now(APP_TZ).isoformat(), assessmentId)
        )
        conn.execute(
            "UPDATE users SET xp=xp+? WHERE id=?",
            (int(round((agg["s"] / max(1, agg["m"])) * 100)), user_id)
        )
    conn.commit()
    conn.close()
    return {
        "success": True,
        "score": score, "maxScore": max_score, "feedback": feedback,
        "completed": done >= total if total else False
    }

@app.get("/api/study/review")
def study_review(user_id: int, limit: int = 20):
    _study_user_exists(user_id)
    limit = _safe_count(limit, 20, 50)
    conn = get_db()
    rows = conn.execute(
        """SELECT * FROM flashcards WHERE user_id=? AND due_at<=?
           ORDER BY due_at ASC LIMIT ?""",
        (user_id, datetime.now(APP_TZ).isoformat(), limit)
    ).fetchall()
    conn.close()
    return {"success": True, "cards": [dict(r) for r in rows]}

@app.post("/api/study/review/rate")
def rate_flashcard(user_id: int = Form(...), cardId: int = Form(...), result: str = Form("known")):
    _study_user_exists(user_id)
    result = "known" if result == "known" else "missed"
    conn = get_db()
    card = conn.execute("SELECT * FROM flashcards WHERE id=? AND user_id=?", (cardId,user_id)).fetchone()
    if not card:
        conn.close()
        raise HTTPException(status_code=404, detail="Flashcard not found")
    if result == "known":
        reps = int(card["repetitions"] or 0) + 1
        interval = min(60, max(1, int(round((card["interval_days"] or 1) * 2.2))))
        ease = min(3.0, float(card["ease"] or 2.5) + 0.05)
    else:
        reps = 0
        interval = 1
        ease = max(1.3, float(card["ease"] or 2.5) - 0.2)
    due = (datetime.now(APP_TZ) + timedelta(days=interval)).isoformat()
    conn.execute(
        "UPDATE flashcards SET due_at=?,ease=?,interval_days=?,repetitions=?,last_result=? WHERE id=? AND user_id=?",
        (due,ease,interval,reps,result,cardId,user_id)
    )
    conn.commit()
    conn.close()
    return {"success": True, "nextReview": due, "intervalDays": interval}

@app.get("/api/study/weaknesses")
def study_weaknesses(user_id: int):
    _study_user_exists(user_id)
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM study_weaknesses WHERE user_id=? ORDER BY score ASC,updated_at DESC",
        (user_id,)
    ).fetchall()
    conn.close()
    return {"success": True, "weaknesses": [dict(r) for r in rows]}

@app.get("/api/study/progress")
def study_progress(user_id: int):
    _study_user_exists(user_id)
    conn = get_db()
    materials = conn.execute("SELECT COUNT(*) n FROM study_materials WHERE user_id=?", (user_id,)).fetchone()["n"]
    exams = conn.execute("SELECT COUNT(*) n FROM assessments WHERE user_id=?", (user_id,)).fetchone()["n"]
    completed = conn.execute("SELECT COUNT(*) n FROM assessments WHERE user_id=? AND status='completed'", (user_id,)).fetchone()["n"]
    cards = conn.execute("SELECT COUNT(*) n FROM flashcards WHERE user_id=?", (user_id,)).fetchone()["n"]
    mastered = conn.execute("SELECT COUNT(*) n FROM flashcards WHERE user_id=? AND last_result='known'", (user_id,)).fetchone()["n"]
    conn.close()
    return {
        "success": True,
        "materials": materials, "assessments": exams, "completedAssessments": completed,
        "flashcards": cards, "masteredCards": mastered,
        "assessmentCompletion": round((completed / exams) * 100, 1) if exams else 0,
        "flashcardMastery": round((mastered / cards) * 100, 1) if cards else 0,
    }

@app.get("/api/study/search")
def search_material(user_id: int, materialId: int, q: str):
    _study_user_exists(user_id)
    q = q.strip()
    if not q:
        return {"success": True, "matches": []}
    conn = get_db()
    row = conn.execute(
        "SELECT text_content FROM study_materials WHERE id=? AND user_id=?",
        (materialId,user_id)
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Material not found")
    text = row["text_content"] or ""
    low = text.lower()
    needle = q.lower()
    matches = []
    start = 0
    while len(matches) < 20:
        idx = low.find(needle, start)
        if idx < 0:
            break
        matches.append({"snippet": text[max(0,idx-180):idx+len(q)+260].replace("\n"," "), "position": idx})
        start = idx + max(1, len(q))
    return {"success": True, "matches": matches}

@app.post("/api/study/tutor")
async def study_tutor(
    request: Request,
    user_id: int = Form(...),
    materialId: int = Form(0),
    question: str = Form(...),
    mode: str = Form("source"),
    strategy: str = Form("normal"),
    sessionId: int = Form(0)
):
    session_user=_current_user_id(request)
    if not session_user: raise HTTPException(status_code=401,detail="Login required")
    if int(session_user)!=int(user_id): raise HTTPException(status_code=403,detail="Not your tutor")
    _study_user_exists(user_id)
    question=question.strip()
    if not question: raise HTTPException(status_code=400,detail="Question is required")
    mode="source" if mode not in {"general","source"} else mode
    strategy=strategy if strategy in {"normal","simpler","analogy","step_by_step","diagnostic"} else "normal"
    conn=get_db()
    session=conn.execute("SELECT * FROM tutor_sessions WHERE id=? AND user_id=?",(int(sessionId),int(user_id))).fetchone() if sessionId else None
    if not session:
        now=datetime.now(APP_TZ).isoformat()
        cur=conn.cursor(); cur.execute("INSERT INTO tutor_sessions(user_id,material_id,mode,strategy,created_at) VALUES(?,?,?,?,?)",(user_id,materialId or None,mode,strategy,now)); session_id=cur.lastrowid; conn.commit()
    else: session_id=session["id"]
    history_rows=list(reversed(conn.execute("SELECT role,content FROM tutor_messages WHERE session_id=? ORDER BY id DESC LIMIT 12",(session_id,)).fetchall()))
    source_text=""
    if mode=="source":
        row=conn.execute("SELECT text_content,title FROM study_materials WHERE id=? AND user_id=?",(materialId,user_id)).fetchone()
        if not row: conn.close(); raise HTTPException(status_code=404,detail="Select a source material first.")
        source_text=row["text_content"][:100000]
    history_text=""
    if history_rows: history_text="\nCONVERSATION HISTORY:\n"+"\n".join(f"{r['role'].upper()}: {r['content'][:4000]}" for r in history_rows)
    prompt=f"""
{_study_language_instruction("ar")}
You are the S-Tesseract AI tutor.
Mode: {mode} (source means use ONLY the provided source; general means general knowledge is allowed).
Explanation strategy: {strategy}.
Student question: {question}
{history_text}
"""
    contents=[prompt]
    if source_text: prompt += "\nSOURCE MATERIAL:\n"+source_text; contents=[prompt]
    result=generate_ai_json(prompt,contents)
    now=datetime.now(APP_TZ).isoformat()
    conn.execute("INSERT INTO tutor_messages(session_id,user_id,material_id,role,content,created_at) VALUES(?,?,?,?,?,?)",(session_id,user_id,materialId or None,"user",question,now))
    conn.execute("INSERT INTO tutor_messages(session_id,user_id,material_id,role,content,created_at) VALUES(?,?,?,?,?,?)",(session_id,user_id,materialId or None,"assistant",json.dumps(result,ensure_ascii=False),datetime.now(APP_TZ).isoformat()))
    conn.commit(); conn.close()
    return {"success":True,"sessionId":session_id,"answer":result}

@app.get("/api/study/tutor-history")
def study_tutor_history(request: Request, user_id: int, limit: int = 30):
    session_user=_current_user_id(request)
    if not session_user: raise HTTPException(status_code=401,detail="Login required")
    if int(session_user)!=int(user_id): raise HTTPException(status_code=403,detail="Not your tutor history")
    limit=max(1,min(int(limit),100)); conn=get_db()
    rows=conn.execute("""SELECT s.id,s.material_id,s.mode,s.strategy,s.created_at,COALESCE(m.title,'General chat') material_title,
      (SELECT content FROM tutor_messages tm WHERE tm.session_id=s.id AND tm.role='user' ORDER BY tm.id ASC LIMIT 1) first_question,
      (SELECT COUNT(*) FROM tutor_messages tm2 WHERE tm2.session_id=s.id) message_count
      FROM tutor_sessions s LEFT JOIN study_materials m ON m.id=s.material_id WHERE s.user_id=? ORDER BY s.created_at DESC LIMIT ?""",(user_id,limit)).fetchall()
    conn.close(); return {"success":True,"sessions":[dict(r) for r in rows]}

@app.get("/api/study/tutor-history/{session_id}")
def study_tutor_history_detail(session_id: int, request: Request, user_id: int):
    session_user=_current_user_id(request)
    if not session_user or int(session_user)!=int(user_id): raise HTTPException(status_code=403,detail="Not your tutor history")
    conn=get_db(); session=conn.execute("SELECT * FROM tutor_sessions WHERE id=? AND user_id=?",(session_id,user_id)).fetchone()
    if not session: conn.close(); raise HTTPException(status_code=404,detail="Tutor session not found")
    messages=conn.execute("SELECT id,role,content,created_at FROM tutor_messages WHERE session_id=? ORDER BY id ASC",(session_id,)).fetchall(); conn.close()
    return {"success":True,"session":dict(session),"messages":[dict(r) for r in messages]}

@app.post("/api/study/goal-plan")
async def study_goal_plan(
    user_id: int = Form(...),
    goal: str = Form(...),
    duration: str = Form("1 month"),
    language: str = Form("ar")
):
    _study_user_exists(user_id)
    prompt = f"""
{_study_language_instruction(language)}
Create a realistic goal -> learning plan for: {goal!r}
Duration: {duration}
Return STRICT JSON:
{{
 "goal":"...",
 "milestones":[{{"title":"...","order":1,"skills":["..."],"prerequisites":["..."],"tasks":["..."],"check":"..."}}],
 "knowledgeGraph":[{{"concept":"...","prerequisite":"...","relation":"depends_on"}}],
 "dailyRoutine":["..."],
 "masteryChecks":["..."]
}}
Avoid invented prerequisites when uncertain; mark them as assumptions.
"""
    plan = generate_ai_json(prompt)
    return {"success": True, "plan": plan}

@app.get("/api/study/room-settings")
def study_room_get(user_id:int):
    _study_user_exists(user_id); conn=get_db()
    row=conn.execute("SELECT * FROM study_room_settings WHERE user_id=?",(user_id,)).fetchone(); conn.close()
    if not row:
        return {"success":True,"settings":{"focus_mode":1,"audio_type":"none","audio_volume":0.35,"quran_reciter":"1","quran_surah":1,"noise_type":"white"}}
    return {"success":True,"settings":dict(row)}

@app.post("/api/study/room-settings")
def study_room_save(payload:dict):
    user_id=_study_user_exists(int(payload.get("user_id",0))); now=datetime.now(APP_TZ).isoformat()
    audio=str(payload.get("audio_type") or "none"); allowed={"none","quran","white","brown","rain","ocean","forest","cafe"}
    if audio not in allowed: audio="none"
    volume=max(0,min(1,float(payload.get("audio_volume",0.35))))
    conn=get_db(); conn.execute("""INSERT INTO study_room_settings(user_id,focus_mode,audio_type,audio_volume,quran_reciter,quran_surah,noise_type,updated_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET focus_mode=excluded.focus_mode,audio_type=excluded.audio_type,audio_volume=excluded.audio_volume,quran_reciter=excluded.quran_reciter,quran_surah=excluded.quran_surah,noise_type=excluded.noise_type,updated_at=excluded.updated_at""",
                 (user_id,1 if payload.get("focus_mode",True) else 0,audio,volume,str(payload.get("quran_reciter") or "1"),max(1,min(114,int(payload.get("quran_surah",1)))),str(payload.get("noise_type") or "white"),now)); conn.commit(); conn.close()
    return {"success":True}

@app.get("/api/study/notifications")
def study_notifications(user_id: int):
    _study_user_exists(user_id)
    now = datetime.now(APP_TZ)
    conn = get_db()
    events = conn.execute(
        """SELECT id,title,target_date,target_time FROM schedule_items
           WHERE user_id=? AND is_completed=0
           ORDER BY target_date ASC,target_time ASC LIMIT 20""",
        (user_id,)
    ).fetchall()
    cards = conn.execute(
        """SELECT id,front,due_at FROM flashcards
           WHERE user_id=? AND due_at<=? ORDER BY due_at ASC LIMIT 10""",
        (user_id,now.isoformat())
    ).fetchall()
    stored = conn.execute("SELECT id,title,message,kind,is_read,created_at FROM app_notifications WHERE user_id=? AND is_read=0 ORDER BY id DESC LIMIT 20", (user_id,)).fetchall()
    conn.close()
    notifications = [{"type":r["kind"],"title":r["title"],"message":r["message"],"id":r["id"]} for r in stored]
    for e in events:
        try:
            dt = datetime.fromisoformat(f"{e['target_date']}T{e['target_time'] or '23:59'}").replace(tzinfo=APP_TZ)
            delta = dt - now
            if delta <= timedelta(hours=24):
                notifications.append({"type":"schedule","title":e["title"],"message":"Due within 24 hours","id":e["id"]})
        except Exception:
            pass
    for c in cards:
        notifications.append({"type":"review","title":"Smart review due","message":c["front"],"id":c["id"]})
    return {"success": True, "notifications": notifications[:20]}

@app.post("/api/study/notifications/read")
def study_notifications_read(payload:dict):
    user_id=_study_user_exists(int(payload.get("user_id",0))); ids=payload.get("ids") or []
    conn=get_db()
    if ids:
        conn.executemany("UPDATE app_notifications SET is_read=1 WHERE id=? AND user_id=?", [(int(i),user_id) for i in ids])
    else:
        conn.execute("UPDATE app_notifications SET is_read=1 WHERE user_id=?",(user_id,))
    conn.commit(); conn.close(); return {"success":True}

@app.get("/api/study/material-history")
def study_material_history(request: Request, limit: int = 50):
    user_id = _current_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Login required")
    limit=max(1,min(int(limit),100))
    conn=get_db()
    rows=conn.execute("""
        SELECT m.id,m.title,m.filename,m.source_type,m.created_at,
               (SELECT COUNT(*) FROM video_jobs v WHERE v.user_id=m.user_id AND v.filename=m.filename) AS video_count,
               (SELECT COUNT(*) FROM assessments a WHERE a.user_id=m.user_id AND a.material_id=m.id) AS assessment_count
        FROM study_materials m
        WHERE m.user_id=?
        ORDER BY m.created_at DESC LIMIT ?
    """,(user_id,limit)).fetchall()
    conn.close()
    return {"success":True,"materials":[dict(r) for r in rows]}

@app.get("/api/study/assessment-history")
def study_assessment_history(user_id: int):
    _study_user_exists(user_id)
    conn = get_db()
    rows = conn.execute(
        """SELECT id,mode,question_count,language,status,score,max_score,created_at,completed_at
           FROM assessments WHERE user_id=? ORDER BY id DESC LIMIT 30""",
        (user_id,)
    ).fetchall()
    conn.close()
    return {"success": True, "assessments": [dict(r) for r in rows]}

ADMIN_HTML = r"""<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>S-Tesseract Admin</title><script src="https://cdn.tailwindcss.com"></script></head><body class="bg-slate-950 text-slate-100 min-h-screen"><main class="max-w-7xl mx-auto p-4 md:p-8"><header class="flex flex-wrap gap-3 justify-between items-center mb-6"><div><h1 class="text-3xl font-black">S-Tesseract Admin</h1><p class="text-slate-400">إدارة المستخدمين، الجلسات، الفيديوهات، الأسئلة والنشاط.</p></div><button id="logout" class="px-4 py-2 rounded-xl bg-red-500/15 text-red-300">تسجيل خروج</button></header><section id="login" class="max-w-md mx-auto mt-16 bg-slate-900 border border-slate-800 rounded-3xl p-6"><h2 class="text-xl font-black mb-4">Admin Login</h2><input id="au" class="w-full p-3 mb-3 rounded-xl bg-slate-800" placeholder="Username"><input id="ap" type="password" class="w-full p-3 mb-3 rounded-xl bg-slate-800" placeholder="Password"><button id="loginBtn" class="w-full p-3 rounded-xl bg-amber-400 text-slate-950 font-black">دخول</button><p id="err" class="text-red-300 mt-3"></p></section><section id="dash" class="hidden space-y-6"><div id="cards" class="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-8 gap-3"></div><section class="bg-slate-900 border border-slate-800 rounded-3xl p-5"><h2 class="font-black mb-4">إضافة مستخدم</h2><div class="grid md:grid-cols-3 gap-2"><input id="newName" class="p-3 rounded-xl bg-slate-800" placeholder="اسم المستخدم"><input id="newField" class="p-3 rounded-xl bg-slate-800" placeholder="التخصص"><button id="addUser" class="p-3 rounded-xl bg-emerald-500 text-slate-950 font-black">+ إضافة</button></div><p id="addMsg" class="text-xs text-slate-400 mt-2"></p></section><div class="grid lg:grid-cols-2 gap-6"><section class="bg-slate-900 border border-slate-800 rounded-3xl p-5"><div class="flex gap-2 mb-4"><input id="search" class="flex-1 p-3 rounded-xl bg-slate-800" placeholder="ابحث بالاسم أو التخصص"><button id="refresh" class="px-4 rounded-xl bg-slate-800">تحديث</button></div><div class="overflow-auto"><table class="w-full text-sm"><thead><tr class="text-slate-400"><th class="p-2">المستخدم</th><th>الحالة</th><th>الأسئلة</th><th>فيديو</th><th>آخر نشاط</th></tr></thead><tbody id="users"></tbody></table></div></section><section class="bg-slate-900 border border-slate-800 rounded-3xl p-5"><h2 class="font-black mb-4">آخر النشاط</h2><div id="activity" class="space-y-2 max-h-[500px] overflow-auto"></div></section></div><section id="detail" class="hidden bg-slate-900 border border-slate-800 rounded-3xl p-5"></section></section></main><script>const $=id=>document.getElementById(id);async function api(u,o={}){const r=await fetch(u,o);const d=await r.json().catch(()=>({}));if(r.status===401)showLogin();if(!r.ok)throw new Error(d.detail||'Request failed');return d}function showLogin(){$('login').classList.remove('hidden');$('dash').classList.add('hidden')}function showDash(){$('login').classList.add('hidden');$('dash').classList.remove('hidden')}function esc(x){return String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}function fmt(x){return x??0}async function load(){try{const d=await api('/api/admin/overview');showDash();const m=d.metrics;const cards=[['المستخدمون',m.users],['أونلاين',m.online],['زوار 24س',m.visitors_24h],['رفع',m.uploads],['فيديوهات',m.videos],['اختبارات',m.assessments],['الأسئلة',m.questions],['الإجابات',m.submissions]];$('cards').innerHTML=cards.map(x=>`<div class="bg-slate-900 border border-slate-800 rounded-2xl p-4"><div class="text-xs text-slate-400">${x[0]}</div><div class="text-2xl font-black mt-1">${fmt(x[1])}</div></div>`).join('');await users();await activity()}catch(e){showLogin()}}async function users(){const q=encodeURIComponent($('search').value);const d=await api('/api/admin/users?q='+q);$('users').innerHTML=d.users.map(u=>`<tr class="border-t border-slate-800 hover:bg-slate-800/60 cursor-pointer" onclick="detail(${u.id})"><td class="p-2 font-bold">${esc(u.username)}<div class="text-[10px] text-slate-500">${esc(u.public_user_id||'')}</div></td><td><span class="px-2 py-1 rounded-lg text-[10px] ${u.account_status==='active'?'bg-emerald-500/15 text-emerald-300':u.account_status==='banned'?'bg-red-500/15 text-red-300':'bg-amber-500/15 text-amber-300'}">${esc(u.account_status||'active')}</span></td><td>${fmt(u.questions)}</td><td>${fmt(u.videos)}</td><td>${esc(u.last_seen_at||'-')}</td></tr>`).join('')}async function activity(){const d=await api('/api/admin/activity?limit=100');$('activity').innerHTML=d.activity.map(a=>`<div class="p-3 rounded-xl bg-slate-800/70"><b>${esc(a.username||'Anonymous')}</b> · ${esc(a.event_type)}<div class="text-xs text-slate-400 mt-1">${esc(a.details||'')}<br>${esc(a.created_at)}</div></div>`).join('')}async function detail(id){const d=await api('/api/admin/users/'+id);const u=d.user;$('detail').classList.remove('hidden');const status=u.account_status||'active';$('detail').innerHTML=`<div class="flex flex-wrap justify-between gap-3"><div><h2 class="text-xl font-black">${esc(u.username)}</h2><p class="text-xs text-slate-400">${esc(u.public_user_id||'')} · ${esc(u.field_of_study||'')}</p></div><div class="flex flex-wrap gap-2"><button onclick="act(${id},'kick')" class="px-3 py-2 rounded-xl bg-amber-500/15 text-amber-300">Kick الأجهزة</button><button onclick="act(${id},'ban')" class="px-3 py-2 rounded-xl bg-red-500/15 text-red-300">Ban</button><button onclick="act(${id},'unban')" class="px-3 py-2 rounded-xl bg-emerald-500/15 text-emerald-300">Unban</button><button onclick="act(${id},'disable')" class="px-3 py-2 rounded-xl bg-orange-500/15 text-orange-300">Disable</button><button onclick="act(${id},'enable')" class="px-3 py-2 rounded-xl bg-blue-500/15 text-blue-300">Enable</button></div></div><div class="mt-5 p-4 rounded-2xl bg-amber-500/10 border border-amber-500/20"><div class="flex flex-wrap items-center justify-between gap-3"><div><div class="font-black text-amber-200">إدارة النقاط (XP)</div><div class="text-xs text-slate-400 mt-1">أضف أو اخصم نقاط من حساب المستخدم. لن تنزل النقاط عن 0.</div></div><div class="flex flex-wrap items-center gap-2"><input id="xpDelta_${id}" type="number" step="1" placeholder="+ / - XP" class="w-32 px-3 py-2 rounded-xl bg-slate-950 border border-slate-700 text-white"/><input id="xpReason_${id}" type="text" maxlength="500" placeholder="السبب (اختياري)" class="w-52 px-3 py-2 rounded-xl bg-slate-950 border border-slate-700 text-white"/><button onclick="adjustXP(${id})" class="px-4 py-2 rounded-xl bg-amber-400 text-slate-950 font-black">تطبيق</button></div></div></div><div class="grid grid-cols-2 md:grid-cols-5 gap-3 mt-5">${[['الأسئلة',u.questions],['الاختبارات',u.assessments],['الإجابات',u.submissions],['الفيديوهات',u.videos],['XP',u.xp],['Streak',u.streak],['الدخول',u.login_count],['المهام',u.schedule_items],['Todos',u.todos],['الحالة',status]].map(x=>`<div class="bg-slate-800 rounded-xl p-3"><div class="text-xs text-slate-400">${x[0]}</div><b class="text-xl">${fmt(x[1])}</b></div>`).join('')}</div><h3 class="font-black mt-6 mb-2">سجل النشاط</h3><div class="space-y-2">${d.activity.map(a=>`<div class="text-sm border-b border-slate-800 py-2">${esc(a.event_type)} — ${esc(a.details||'')} <span class="text-slate-500">${esc(a.created_at)}</span></div>`).join('')}</div>`;$('detail').scrollIntoView({behavior:'smooth'})}async function adjustXP(id){const input=$('xpDelta_'+id), reason=$('xpReason_'+id);const delta=Number(input.value);if(!Number.isInteger(delta)||delta===0){alert('اكتب عدد نقاط صحيحًا، مثل +100 أو -50');return}if(!confirm((delta>0?'إضافة ':'خصم ')+Math.abs(delta)+' XP؟'))return;try{await api('/api/admin/users/'+id+'/xp',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({delta,reason:reason.value})});input.value='';reason.value='';await detail(id);await load()}catch(e){alert(e.message)}}async function act(id,action){let reason='';if(action==='ban')reason=prompt('سبب الحظر (اختياري):')||'';if(action==='delete'&&!confirm('حذف الحساب وبياناته؟'))return;try{await api('/api/admin/users/'+id+'/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,reason})});await load();await detail(id)}catch(e){alert(e.message)}}$('loginBtn').onclick=async()=>{try{await api('/api/admin/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:$('au').value,password:$('ap').value})});$('err').textContent='';load()}catch(e){$('err').textContent=e.message}};$('logout').onclick=async()=>{await api('/api/admin/logout',{method:'POST'});showLogin()};$('refresh').onclick=load;$('search').oninput=users;$('addUser').onclick=async()=>{try{const d=await api('/api/admin/users',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:$('newName').value,field_of_study:$('newField').value})});$('addMsg').textContent='تم إنشاء الحساب: '+d.public_user_id;$('newName').value='';$('newField').value='';load()}catch(e){$('addMsg').textContent=e.message}};load();setInterval(load,30000);</script></body></html>"""


# -----------------------------------------------------------------------------
# Private Admin Dashboard
# -----------------------------------------------------------------------------
@app.post("/api/admin/login")
def admin_login(payload: dict, request: Request):
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    if not secrets.compare_digest(username, ADMIN_USERNAME) or not _admin_password_ok(password):
        raise HTTPException(status_code=401, detail="Invalid admin credentials")
    token = secrets.token_urlsafe(48)
    now = datetime.now(APP_TZ)
    conn = get_db()
    conn.execute("INSERT INTO admin_sessions(token_hash,created_at,expires_at) VALUES(?,?,?)",
                 (_hash_token(token), now.isoformat(), (now+timedelta(days=ADMIN_SESSION_DAYS)).isoformat()))
    conn.commit(); conn.close()
    response = JSONResponse({"success": True})
    response.set_cookie(ADMIN_COOKIE, token, max_age=60*60*24*ADMIN_SESSION_DAYS, httponly=True, samesite="strict", secure=(request.url.scheme == "https"), path="/")
    return response

@app.post("/api/admin/logout")
def admin_logout(request: Request):
    token = request.cookies.get(ADMIN_COOKIE, "")
    conn = get_db(); conn.execute("DELETE FROM admin_sessions WHERE token_hash=?", (_hash_token(token),)); conn.commit(); conn.close()
    response = JSONResponse({"success": True}); response.delete_cookie(ADMIN_COOKIE); return response

@app.get("/api/admin/overview")
def admin_overview(request: Request):
    _admin_auth(request)
    now = datetime.now(APP_TZ)
    online_cutoff = (now - timedelta(minutes=5)).isoformat()
    day_cutoff = (now - timedelta(days=1)).isoformat()
    conn = get_db(); cur = conn.cursor()
    users = cur.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
    new_24h = cur.execute("SELECT COUNT(*) n FROM users WHERE created_at >= ?", (day_cutoff,)).fetchone()["n"]
    online = cur.execute("SELECT COUNT(*) n FROM users WHERE last_seen_at >= ?", (online_cutoff,)).fetchone()["n"]
    visitors_24h = cur.execute("SELECT COUNT(DISTINCT visitor_hash) n FROM site_visits WHERE last_seen_at >= ?", (day_cutoff,)).fetchone()["n"]
    tables = {}
    for name in ["schedule_items","focus_todos","blueprint_nodes","video_jobs","study_materials","assessments","assessment_questions","assessment_submissions"]:
        try: tables[name] = cur.execute(f"SELECT COUNT(*) n FROM {name}").fetchone()["n"]
        except sqlite3.OperationalError: tables[name] = 0
    question_types = {}
    try:
        rows=cur.execute("SELECT question_type,COUNT(*) n FROM assessment_questions GROUP BY question_type").fetchall(); question_types={r["question_type"]:r["n"] for r in rows}
    except sqlite3.OperationalError: pass
    jobs = {}
    try:
        rows=cur.execute("SELECT status,COUNT(*) n FROM video_jobs GROUP BY status").fetchall(); jobs={r["status"]:r["n"] for r in rows}
    except sqlite3.OperationalError: pass
    recent=cur.execute("SELECT id,username,field_of_study,public_user_id,account_status,created_at,last_login_at,last_seen_at,login_count,last_platform,xp,streak,tests_taken,tests_passed FROM users ORDER BY id DESC LIMIT 12").fetchall()
    conn.close()
    return {"success":True,"metrics":{"users":users,"new_24h":new_24h,"online":online,"visitors_24h":visitors_24h,"questions":tables.get("assessment_questions",0),"assessments":tables.get("assessments",0),"submissions":tables.get("assessment_submissions",0),"videos":tables.get("video_jobs",0),"uploads":tables.get("study_materials",0)},"question_types":question_types,"jobs":jobs,"recent_users":[dict(r) for r in recent]}

@app.get("/api/admin/users")
def admin_users(request: Request, q: str = "", limit: int = 100):
    _admin_auth(request); limit=max(1,min(limit,500)); conn=get_db(); cur=conn.cursor()
    like=f"%{q.strip()}%"
    rows=cur.execute("SELECT id,username,field_of_study,public_user_id,account_status,created_at,last_login_at,last_seen_at,login_count,last_platform,xp,streak,tests_taken,tests_passed FROM users WHERE username LIKE ? OR field_of_study LIKE ? ORDER BY id DESC LIMIT ?",(like,like,limit)).fetchall()
    out=[]
    for r in rows:
        d=dict(r); uid=d["id"]
        for key,sql in [("uploads","SELECT COUNT(*) n FROM study_materials WHERE user_id=?"),("videos","SELECT COUNT(*) n FROM video_jobs WHERE user_id=?"),("questions","SELECT COUNT(*) n FROM assessment_questions WHERE assessment_id IN (SELECT id FROM assessments WHERE user_id=?)"),("assessments","SELECT COUNT(*) n FROM assessments WHERE user_id=?"),("submissions","SELECT COUNT(*) n FROM assessment_submissions WHERE user_id=?")]:
            try:d[key]=cur.execute(sql,(uid,)).fetchone()["n"]
            except sqlite3.OperationalError:d[key]=0
        out.append(d)
    conn.close(); return {"success":True,"users":out}

@app.get("/api/admin/users/{user_id}")
def admin_user_detail(user_id:int, request:Request):
    _admin_auth(request); conn=get_db(); cur=conn.cursor(); u=cur.execute("SELECT * FROM users WHERE id=?",(user_id,)).fetchone()
    if not u: conn.close(); raise HTTPException(status_code=404,detail="User not found")
    def count(sql):
        try:return cur.execute(sql,(user_id,)).fetchone()[0]
        except sqlite3.OperationalError:return 0
    data=companion_auth.public_user(u); data.update({"uploads":count("SELECT COUNT(*) FROM study_materials WHERE user_id=?"),"videos":count("SELECT COUNT(*) FROM video_jobs WHERE user_id=?"),"assessments":count("SELECT COUNT(*) FROM assessments WHERE user_id=?"),"submissions":count("SELECT COUNT(*) FROM assessment_submissions WHERE user_id=?"),"schedule_items":count("SELECT COUNT(*) FROM schedule_items WHERE user_id=?"),"todos":count("SELECT COUNT(*) FROM focus_todos WHERE user_id=?")})
    try:
        data["questions"]=cur.execute("SELECT COUNT(*) FROM assessment_questions WHERE assessment_id IN (SELECT id FROM assessments WHERE user_id=?)",(user_id,)).fetchone()[0]
    except sqlite3.OperationalError:data["questions"]=0
    audits=cur.execute("SELECT event_type,details,created_at FROM admin_audit_log WHERE user_id=? ORDER BY id DESC LIMIT 50",(user_id,)).fetchall()
    conn.close(); return {"success":True,"user":data,"activity":[dict(x) for x in audits]}

@app.post("/api/admin/users/{user_id}/xp")
def admin_adjust_xp(user_id:int, payload:dict, request:Request):
    """Admin-only XP adjustment. Positive delta adds XP; negative delta removes XP.
    XP is clamped at zero and every manual change is permanently audited.
    """
    _admin_auth(request)
    try:
        delta = int(payload.get("delta", 0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="delta must be an integer")
    if delta == 0:
        raise HTTPException(status_code=400, detail="delta cannot be zero")
    reason = str(payload.get("reason") or "Admin adjustment").strip()[:500]
    conn = get_db(); cur = conn.cursor()
    user = cur.execute("SELECT id,username,xp FROM users WHERE id=?", (user_id,)).fetchone()
    if not user:
        conn.close(); raise HTTPException(status_code=404, detail="User not found")
    old_xp = int(user["xp"] or 0)
    new_xp = max(0, old_xp + delta)
    actual_delta = new_xp - old_xp
    now = datetime.now(APP_TZ).isoformat()
    cur.execute("UPDATE users SET xp=? WHERE id=?", (new_xp, user_id))
    cur.execute("INSERT INTO admin_xp_log(user_id,delta,old_xp,new_xp,reason,created_at) VALUES(?,?,?,?,?,?)",
                (user_id, actual_delta, old_xp, new_xp, reason, now))
    conn.commit(); conn.close()
    _audit(user_id, "admin_xp_adjust", f"delta={actual_delta}; old={old_xp}; new={new_xp}; reason={reason}")
    return {"success":True,"user_id":user_id,"old_xp":old_xp,"new_xp":new_xp,"delta":actual_delta}

@app.get("/api/admin/activity")
def admin_activity(request:Request, limit:int=100):
    _admin_auth(request); conn=get_db(); rows=conn.execute("SELECT a.created_at,a.event_type,a.details,a.user_id,u.username FROM admin_audit_log a LEFT JOIN users u ON u.id=a.user_id ORDER BY a.id DESC LIMIT ?",(max(1,min(limit,300)),)).fetchall(); conn.close(); return {"success":True,"activity":[dict(r) for r in rows]}

@app.post("/api/admin/users")
def admin_create_user(payload:dict, request:Request):
    _admin_auth(request); username=str(payload.get("username","")).strip(); field=str(payload.get("field_of_study") or payload.get("field") or "").strip()
    if not username or not field: raise HTTPException(status_code=400,detail="username and field_of_study are required")
    conn=get_db(); cur=conn.cursor()
    if cur.execute("SELECT id FROM users WHERE username=?",(username,)).fetchone(): conn.close(); raise HTTPException(status_code=409,detail="Username already exists")
    public_id="STX-"+secrets.token_hex(5).upper(); now=datetime.now(APP_TZ).isoformat()
    cur.execute("INSERT INTO users(username,field_of_study,created_at,public_user_id,account_status) VALUES(?,?,?,?, 'active')",(username,field,now,public_id)); uid=cur.lastrowid; conn.commit(); conn.close()
    _audit(uid,"admin_create_user",f"username={username}")
    return {"success":True,"user_id":uid,"public_user_id":public_id}

@app.post("/api/admin/users/{user_id}/action")
def admin_user_action(user_id:int,payload:dict,request:Request):
    _admin_auth(request); action=str(payload.get("action","")).lower(); reason=str(payload.get("reason") or "").strip()[:500]
    if action not in {"kick","ban","unban","disable","enable","delete"}: raise HTTPException(status_code=400,detail="Unknown action")
    conn=get_db(); cur=conn.cursor(); user=cur.execute("SELECT id,username FROM users WHERE id=?",(user_id,)).fetchone()
    if not user: conn.close(); raise HTTPException(status_code=404,detail="User not found")
    now=datetime.now(APP_TZ).isoformat()
    if action=="kick":
        cur.execute("INSERT INTO app_notifications(user_id,title,message,kind,is_read,created_at) VALUES(?,?,?,?,0,?)",(user_id,"تم تسجيل خروجك","قام المشرف بإنهاء جلساتك الحالية. سيتم إخراجك من البرنامج الآن.","admin",now))
        cur.execute("DELETE FROM user_sessions WHERE user_id=?",(user_id,))
    elif action=="ban":
        cur.execute("UPDATE users SET account_status='banned',ban_reason=?,banned_at=? WHERE id=?",(reason,now,user_id))
        cur.execute("INSERT INTO app_notifications(user_id,title,message,kind,is_read,created_at) VALUES(?,?,?,?,0,?)",(user_id,"تم حظر الحساب","تم حظر حسابك بواسطة الإدارة." + ((" السبب: "+reason) if reason else ""),"admin",now))
        cur.execute("DELETE FROM user_sessions WHERE user_id=?",(user_id,))
    elif action=="unban":
        cur.execute("UPDATE users SET account_status='active',ban_reason='',banned_at='' WHERE id=?",(user_id,))
    elif action=="disable":
        cur.execute("UPDATE users SET account_status='disabled' WHERE id=?",(user_id,))
        cur.execute("INSERT INTO app_notifications(user_id,title,message,kind,is_read,created_at) VALUES(?,?,?,?,0,?)",(user_id,"تم إيقاف الحساب","تم إيقاف حسابك بواسطة الإدارة.","admin",now))
        cur.execute("DELETE FROM user_sessions WHERE user_id=?",(user_id,))
    elif action=="enable":
        cur.execute("UPDATE users SET account_status='active' WHERE id=?",(user_id,))
    elif action=="delete":
        cur.execute("DELETE FROM user_sessions WHERE user_id=?",(user_id,)); cur.execute("DELETE FROM app_notifications WHERE user_id=?",(user_id,)); cur.execute("DELETE FROM users WHERE id=?",(user_id,))
    conn.commit(); conn.close(); _audit(user_id,f"admin_{action}",reason); return {"success":True,"action":action}

@app.get("/api/admin/notifications")
def admin_notifications(request:Request):
    _admin_auth(request); conn=get_db(); rows=conn.execute("SELECT id,title,message,kind,created_at FROM app_notifications ORDER BY id DESC LIMIT 100").fetchall(); conn.close(); return {"success":True,"notifications":[dict(r) for r in rows]}

@app.get("/api/health")
def api_health(request: Request):
    """Liveness plus the facts that explain 'I cannot log in': where the database lives and how many accounts it holds.
    Only counts and a folder label are exposed, never names, paths of files, or any secret."""
    info = {"ok": True, "service": "S-Tesseract", "status": "healthy"}
    try:
        db_dir = os.path.dirname(os.path.abspath(DB_FILE))
        conn = get_db()
        users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        with_password = conn.execute("SELECT COUNT(*) FROM users WHERE COALESCE(password_hash,'') != ''").fetchone()[0]
        conn.close()
        info["storage"] = {
            "database_location": "persistent disk (/data)" if db_dir == "/data" or db_dir.startswith("/data/") else "app folder (a new deploy can erase it unless this folder is a mounted persistent disk)",
            "accounts": users, "accounts_with_password": with_password,
        }
    except Exception as exc:
        info["storage"] = {"error": type(exc).__name__}
    info["session_cookie"] = {"request_seen_as_https": _request_is_https(request), "secure_flag_will_be_set": _request_is_https(request)}
    return info

@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    return ADMIN_HTML

# -----------------------------------------------------------------------------
# Frontend: S-Tesseract Workspace
# -----------------------------------------------------------------------------
LOGIN_PAGE = """<!DOCTYPE html>
<html lang="ar" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>تسجيل الدخول - S-Tesseract</title>
<style>
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#fffbeb;font-family:Tahoma,Arial,sans-serif;color:#0f172a;padding:16px}
.card{width:100%;max-width:420px;background:#fff;border:1px solid #fde68a;border-radius:20px;padding:24px;box-shadow:0 10px 30px rgba(0,0,0,.08)}
h1{margin:0 0 4px;font-size:22px}p.sub{margin:0 0 18px;color:#64748b;font-size:13px}
label{display:block;font-size:13px;font-weight:700;margin:12px 0 6px}
input[type=text],input[type=password]{width:100%;padding:12px;font-size:16px;border:1px solid #cbd5e1;border-radius:12px;background:#f8fafc}
.row{display:flex;align-items:center;gap:6px;margin-top:8px;font-size:13px;color:#475569}
button{width:100%;margin-top:18px;padding:14px;font-size:16px;font-weight:800;border:0;border-radius:12px;background:#fbbf24;color:#0f172a;cursor:pointer}
button:disabled{opacity:.6;cursor:wait}
#msg{margin-top:12px;font-size:14px;font-weight:700;line-height:1.7;text-align:center;min-height:1.5em}
.err{color:#dc2626}.ok{color:#059669}a{display:block;margin-top:14px;text-align:center;font-size:12px;color:#64748b}
</style></head><body><div class="card">
<h1>S-Tesseract</h1><p class="sub">سجّل الدخول بالاسم وكلمة المرور</p>
<label for="u">الاسم</label><input id="u" type="text" autocomplete="username" autocapitalize="off">
<label for="pw">كلمة المرور</label><input id="pw" type="password" autocomplete="current-password">
<div class="row"><input id="show" type="checkbox"><label for="show" style="margin:0;font-weight:400">إظهار كلمة المرور</label></div>
<label for="f">المجال (للحساب الجديد فقط)</label><input id="f" type="text" placeholder="مثال: computer">
<button id="go" type="button">دخول</button>
<div id="msg" role="alert" aria-live="assertive"></div>
<a href="/">الرجوع للصفحة الرئيسية</a>
</div>
<script>
(function () {
  var $ = function (id) { return document.getElementById(id); };
  function say(text, ok) { var m = $("msg"); m.textContent = text || ""; m.className = ok ? "ok" : "err"; }
  $("show").addEventListener("change", function () { $("pw").type = this.checked ? "text" : "password"; });
  async function login() {
    var name = $("u").value.trim(), pw = $("pw").value, field = $("f").value.trim();
    if (!name) { say("اكتب الاسم أولاً."); $("u").focus(); return; }
    var btn = $("go"), label = btn.textContent, ctrl = new AbortController(), timer = setTimeout(function () { ctrl.abort(); }, window.__AUTH_TIMEOUT_MS || 25000);
    btn.disabled = true; btn.textContent = "جاري الدخول..."; say("");
    try {
      var res = await fetch("/api/auth/register-or-login", { method: "POST", credentials: "same-origin", signal: ctrl.signal,
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username: name, field: field, password: pw }) });
      var data = {}; try { data = JSON.parse(await res.text()); } catch (e) { data = {}; }
      if (!res.ok || !data.id) {
        var d = typeof data.detail === "string" ? data.detail : "";
        say(res.status >= 500 ? "السيرفر فيه مشكلة دلوقتي (رمز " + res.status + "). جرّب تاني بعد دقيقة." : (d || "الاسم أو كلمة المرور غير صحيحة."));
        return;
      }
      localStorage.setItem("grit_user_id", String(data.id)); localStorage.setItem("grit_username", data.username || name);
      localStorage.setItem("grit_field", data.field_of_study || field);
      say("تم الدخول، جاري التحويل...", true); location.href = "/";
    } catch (e) {
      say(e && e.name === "AbortError" ? "السيرفر مش بيرد. لو الموقع كان نايم استنى دقيقة وجرّب تاني." : "مفيش اتصال بالسيرفر. اتأكد من الإنترنت وجرّب تاني.");
    } finally { clearTimeout(timer); btn.disabled = false; btn.textContent = label; }
  }
  $("go").addEventListener("click", login);
  ["u", "pw", "f"].forEach(function (id) { $(id).addEventListener("keydown", function (e) { if (e.key === "Enter") login(); }); });
})();
</script></body></html>"""


@app.get("/login", response_class=HTMLResponse)
async def simple_login_page():
    """A plain login page with no CDN, no overlays and no shared UI code: it works even if the main page's UI is broken."""
    resp = HTMLResponse(LOGIN_PAGE)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    return exam_studio.inject_ui("""<!DOCTYPE html>
<html lang="ar">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>S-Tesseract • Roadmap & Concept Mastery</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Cairo:wght@600;700;800;900&family=Plus+Jakarta+Sans:wght@600;700;800;900&display=swap" rel="stylesheet">
  <style>
    body {
      font-family: 'Cairo', 'Plus Jakarta Sans', system-ui, sans-serif;
      background: radial-gradient(circle at 10% 20%, #FFFBEB 0%, #F8FAFC 60%, #EEF2F6 100%);
    }
    .no-scrollbar::-webkit-scrollbar { display: none; }
    .no-scrollbar { -ms-overflow-style: none; scrollbar-width: none; }
    .banana-card {
      background: rgba(255, 255, 255, 0.95);
      backdrop-filter: blur(12px);
      border: 1px solid rgba(254, 240, 138, 0.6);
      box-shadow: 0 10px 25px -5px rgba(234, 179, 8, 0.08), 0 8px 10px -6px rgba(0, 0, 0, 0.03);
    }
    .video-frame-box {
      border: 2px solid rgba(250, 204, 21, 0.6);
      box-shadow: 0 20px 50px rgba(0, 0, 0, 0.35);
    }
    .calendar-shell {
      background: radial-gradient(circle at 15% 0%, rgba(59,130,246,.13), transparent 32%), #07101f;
    }
    .calendar-mini-day {
      aspect-ratio: 1 / 1;
      display: flex;
      align-items: center;
      justify-content: center;
      border-radius: .65rem;
      font-size: 9px;
      font-weight: 800;
      color: #94a3b8;
    }
    .calendar-mini-day.today { background: #2563eb; color: #fff; }
    .calendar-mini-day.has-event {
      color: #e2e8f0;
      box-shadow: inset 0 -2px 0 #60a5fa;
    }
  
/* ===== S-Tesseract mobile navigation fix ===== */
@media (max-width: 767px) {
  #mobileSidebar { width: min(86vw, 340px); }
  /* IMPORTANT: sidebar backdrop must stay hidden until the menu is opened. */
  #mobileSidebarBackdrop { display: none !important; opacity: 0; pointer-events: none; visibility: hidden; }
  body.mobile-sidebar-open #mobileSidebarBackdrop { display: block !important; opacity: 1; pointer-events: auto; visibility: visible; }
}
@media (min-width: 768px) {
  #mobileSidebar, #mobileSidebarBackdrop, #mobileMenuButton { display: none !important; }
}
#mobileSidebar { transition: transform 250ms ease; }
#mobileSidebarBackdrop { transition: opacity 200ms ease; }
/* Never show the mobile sidebar backdrop on page load. It must only appear when the sidebar is open. */
#mobileSidebarBackdrop {
  display: none !important;
  opacity: 0 !important;
  visibility: hidden !important;
  pointer-events: none !important;
  -webkit-backdrop-filter: none !important;
  backdrop-filter: none !important;
}
body.mobile-sidebar-open #mobileSidebarBackdrop {
  display: block !important;
  opacity: 1 !important;
  visibility: visible !important;
  pointer-events: auto !important;
  z-index: 9990 !important;
}
body:not(.mobile-sidebar-open) #mobileSidebarBackdrop {
  display: none !important;
  opacity: 0 !important;
  visibility: hidden !important;
  pointer-events: none !important;
}

/* =========================================================
   S-TESSERACT — Full Mobile Responsive Layer
   Keeps desktop layout intact and optimizes the existing UI
   for phones without changing application logic.
   ========================================================= */
/* Explicit overlay rules: do not rely only on Tailwind's .hidden on mobile. */
#authModal, #lockdownOverlay, #eventModal {
  position: fixed !important;
  inset: 0 !important;
  width: 100vw !important;
  min-height: 100vh !important;
  min-height: 100dvh !important;
}
/*
   IMPORTANT: overlays are CLOSED by default.
   This is intentionally stronger than Tailwind's .hidden because some
   mobile browsers can retain a stale computed style after viewport changes.
*/
#authModal, #lockdownOverlay, #eventModal {
  display: none !important;
  visibility: hidden !important;
  pointer-events: none !important;
  opacity: 0 !important;
}
#authModal.auth-visible {
  display: flex !important;
  visibility: visible !important;
  pointer-events: auto !important;
  opacity: 1 !important;
  z-index: 99990 !important;
  background: rgba(2, 6, 23, 0.70) !important;
  -webkit-backdrop-filter: none !important;
  backdrop-filter: none !important;
}
#lockdownOverlay.overlay-visible {
  display: flex !important;
  visibility: visible !important;
  pointer-events: auto !important;
  opacity: 1 !important;
  z-index: 99980 !important;
}
#eventModal.overlay-visible {
  display: flex !important;
  visibility: visible !important;
  pointer-events: auto !important;
  opacity: 1 !important;
  z-index: 99970 !important;
}
#authModal.hidden, #lockdownOverlay.hidden, #eventModal.hidden {
  display: none !important;
  pointer-events: none !important;
  visibility: hidden !important;
  opacity: 0 !important;
}
#authModal > div {
  position: relative !important;
  z-index: 99991 !important;
  pointer-events: auto !important;
  opacity: 1 !important;
  visibility: visible !important;
  display: block !important;
}
#lockdownOverlay {
  z-index: 99980 !important;
}
body.auth-open {
  overflow: hidden !important;
}
@media (max-width: 767px) {
  #authModal {
    padding: max(16px, env(safe-area-inset-top)) max(16px, env(safe-area-inset-right)) max(16px, env(safe-area-inset-bottom)) max(16px, env(safe-area-inset-left)) !important;
    align-items: center !important;
    justify-content: center !important;
  }
  #authModal > div {
    width: min(100%, 430px) !important;
    max-height: calc(100dvh - 32px) !important;
    overflow-y: auto !important;
    -webkit-overflow-scrolling: touch;
  }
}

@media (max-width: 767px) {
  html, body {
    width: 100%;
    max-width: 100%;
    overflow-x: hidden !important;
  }

  body {
    min-width: 0 !important;
  }

  #mainContentArea {
    width: 100% !important;
    min-width: 0 !important;
    max-width: 100vw !important;
    padding-left: 12px !important;
    padding-right: 12px !important;
    overflow-x: hidden !important;
  }

  main {
    width: 100% !important;
    min-width: 0 !important;
    max-width: 100vw !important;
    overflow-x: hidden !important;
  }

  /* Prevent long text, URLs and generated content from creating
     horizontal scrolling on small screens. */
  #mainContentArea *,
  #mobileSidebar * {
    max-width: 100%;
  }

  #mainContentArea img,
  #mainContentArea video,
  #mainContentArea canvas,
  #mainContentArea iframe {
    max-width: 100% !important;
    height: auto;
  }

  #mainContentArea pre,
  #mainContentArea code {
    max-width: 100%;
    overflow-x: auto;
  }

  /* Responsive cards / containers */
  #mainContentArea .banana-card,
  #mainContentArea .rounded-3xl,
  #mainContentArea .rounded-2xl {
    min-width: 0 !important;
  }

  /* Header */
  header {
    min-height: 64px;
    padding: 10px 12px !important;
    gap: 8px;
  }

  header h1,
  header h2,
  header p {
    min-width: 0;
  }

  header h1 {
    font-size: 1rem !important;
    line-height: 1.25rem !important;
  }

  header p {
    font-size: 10px !important;
    line-height: 1rem !important;
  }

  /* Buttons: make touch targets comfortable without making them huge. */
  #mainContentArea button,
  #mainContentArea input[type="button"],
  #mainContentArea input[type="submit"] {
    min-height: 42px;
  }

  /* Forms */
  #mainContentArea input,
  #mainContentArea select,
  #mainContentArea textarea {
    max-width: 100% !important;
    min-width: 0 !important;
    font-size: 16px !important; /* prevents iOS zoom */
  }

  #mainContentArea textarea {
    min-height: 110px;
  }

  /* Any fixed-width Tailwind controls become fluid on phones. */
  #mainContentArea [class*="w-["],
  #mainContentArea [class*="min-w-["] {
    max-width: 100% !important;
  }

  /* Tables / calendar-like content */
  #mainContentArea table {
    display: block;
    width: 100%;
    max-width: 100%;
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
  }

  /* Keep dense grids usable. */
  #mainContentArea .grid {
    min-width: 0;
  }

  /* Long flex rows should be allowed to wrap on mobile. */
  #mainContentArea .flex {
    min-width: 0;
  }

  /* Video area */
  #mainContentArea video {
    width: 100% !important;
    border-radius: 14px;
  }

  /* Calendar day cells / generated cards */
  #mainContentArea [class*="grid-cols-7"] {
    min-width: 0 !important;
  }

  /* Modal/dialog safety */
  [role="dialog"],
  .fixed.inset-0 {
    max-width: 100vw;
  }

  /* Mobile drawer */
  #mobileSidebar,
  #sidebar {
    width: min(88vw, 340px) !important;
    max-width: 340px !important;
  }

  /* Avoid accidental page scroll while drawer is open. */
  body.mobile-sidebar-open {
    overflow: hidden !important;
    touch-action: none;
  }

  /* Better spacing for stacked sections */
  #mainContentArea .space-y-6 > :not([hidden]) ~ :not([hidden]) {
    margin-top: 1rem;
  }
}

/* Very small phones */
@media (max-width: 380px) {
  #mainContentArea {
    padding-left: 8px !important;
    padding-right: 8px !important;
  }

  header {
    padding-left: 8px !important;
    padding-right: 8px !important;
  }

  #mainContentArea .text-3xl {
    font-size: 1.5rem !important;
    line-height: 2rem !important;
  }

  #mainContentArea .text-2xl {
    font-size: 1.25rem !important;
    line-height: 1.75rem !important;
  }
}


/* ===== FINAL MOBILE SIDEBAR STACK FIX =====
   Backdrop must be BELOW the sidebar. The previous high z-index on the
   backdrop made the entire sidebar untouchable on some phones. */
@media (max-width: 767px) {
  #mobileSidebarBackdrop {
    z-index: 10000 !important;
    display: none !important;
    opacity: 0 !important;
    visibility: hidden !important;
    pointer-events: none !important;
    -webkit-backdrop-filter: none !important;
    backdrop-filter: none !important;
  }

  body.mobile-sidebar-open #mobileSidebarBackdrop {
    display: block !important;
    opacity: 1 !important;
    visibility: visible !important;
    pointer-events: auto !important;
  }

  #mobileSidebar {
    z-index: 10001 !important;
    pointer-events: auto !important;
    visibility: visible;
  }

  #mobileSidebar > * {
    pointer-events: auto;
  }

  #mobileMenuButton {
    z-index: 10002 !important;
  }
}
</style>
</head>
<body class="text-slate-800 min-h-screen flex flex-row-reverse selection:bg-amber-400 selection:text-slate-900">

  <div id="authModal" class="fixed inset-0 bg-slate-950/70 z-50 hidden items-center justify-center p-4" aria-hidden="true">
    <div class="max-w-md w-full bg-white border border-amber-200 p-8 rounded-3xl shadow-2xl space-y-6">
      <div class="text-center space-y-1">
        <div class="w-16 h-16 bg-gradient-to-tr from-amber-400 to-yellow-300 text-slate-900 rounded-3xl flex items-center justify-center mx-auto text-3xl font-black shadow-lg shadow-amber-300/50 mb-2">
          ⚡
        </div>
        <h2 class="text-2xl font-black text-slate-900" data-t="auth_title">S-Tesseract</h2>
      </div>
      <div class="space-y-4">
        <div>
          <label class="block text-xs font-bold text-slate-700 mb-1.5 uppercase tracking-wider" data-t="label_username">الاسم</label>
          <input type="text" id="authUsername" class="w-full bg-slate-50 border border-slate-200 rounded-xl p-3 text-sm text-slate-800 focus:bg-white focus:outline-none focus:ring-2 focus:ring-amber-400 transition">
        </div>
        <div>
          <label class="block text-xs font-bold text-slate-700 mb-1.5 uppercase tracking-wider">كلمة المرور</label>
          <input type="password" id="authPassword" autocomplete="current-password" class="w-full bg-slate-50 border border-slate-200 rounded-xl p-3 text-sm text-slate-800 focus:bg-white focus:outline-none focus:ring-2 focus:ring-amber-400 transition">
        </div>
        <div>
          <label class="block text-xs font-bold text-slate-700 mb-1.5 uppercase tracking-wider" data-t="label_field">المجال</label>
          <input type="text" id="authField" class="w-full bg-slate-50 border border-slate-200 rounded-xl p-3 text-sm text-slate-800 focus:bg-white focus:outline-none focus:ring-2 focus:ring-amber-400 transition">
        </div>
        <button id="authBtn" type="button" onclick="handleAuthSubmit()" class="w-full py-3.5 bg-gradient-to-r from-amber-400 to-yellow-400 hover:from-amber-300 text-slate-900 font-black text-sm rounded-xl shadow-lg shadow-amber-200 transition" data-t="btn_enter">
          دخول ←
        </button>
        <p id="authError" role="alert" aria-live="assertive" class="hidden mt-3 text-sm font-bold text-red-600 text-center leading-relaxed"></p>
        <a href="/login" class="block mt-3 text-center text-xs font-bold text-slate-500 underline">مشكلة في الدخول؟ جرّب صفحة الدخول البسيطة</a>
      </div>
    </div>
  </div>

  <div id="pwSetupModal" class="fixed inset-0 bg-slate-950/70 z-50 hidden items-center justify-center p-4">
    <div class="max-w-md w-full bg-white border border-amber-200 p-8 rounded-3xl shadow-2xl space-y-4">
      <h2 class="text-xl font-black text-slate-900">اعمل كلمة مرور لحسابك</h2>
      <p class="text-sm text-slate-600">معرّفك الدائم: <b id="pwSetupId" dir="ltr" class="select-all"></b><br>هتستخدمه مع كلمة المرور لتسجيل الدخول من تطبيق Companion.</p>
      <input type="password" id="pwCurrentInput" autocomplete="current-password" placeholder="كلمة المرور الحالية" class="hidden w-full bg-slate-50 border border-slate-200 rounded-xl p-3 text-sm">
      <input type="password" id="pwSetupInput" autocomplete="new-password" placeholder="8 حروف على الأقل" class="w-full bg-slate-50 border border-slate-200 rounded-xl p-3 text-sm">
      <p id="pwSetupMsg" class="text-xs text-red-600"></p>
      <button type="button" onclick="submitPasswordSetup()" class="w-full py-3 bg-gradient-to-r from-amber-400 to-yellow-400 text-slate-900 font-black text-sm rounded-xl">حفظ كلمة المرور</button>
      <button type="button" id="pwSetupCancel" onclick="closePasswordModal()" class="hidden w-full py-2 text-xs font-bold text-slate-500">إلغاء</button>
    </div>
  </div>

  <div id="lockdownOverlay" class="fixed inset-0 bg-slate-950/95 backdrop-blur-xl z-50 hidden flex-col justify-between p-4 md:p-6 overflow-y-auto">
    <div class="max-w-2xl mx-auto w-full flex items-center justify-between border-b border-slate-800 pb-3">
      <div class="flex items-center gap-1.5 text-lg" id="quizLives">
        <span id="heart1">❤️</span>
        <span id="heart2">❤️</span>
        <span id="heart3">❤️</span>
      </div>
      <div id="comboBadge" class="bg-gradient-to-r from-amber-500 to-orange-500 px-3 py-1 rounded-full text-xs font-black tracking-wider text-slate-950 shadow-lg flex items-center gap-1">
        <span>🔥</span> <span id="comboText">1x STREAK</span>
      </div>
      <div class="flex items-center gap-2 bg-slate-900 px-3 py-1 rounded-xl border border-slate-800">
        <span class="text-xs">⏳</span>
        <span id="timerText" class="font-mono font-black text-amber-400 text-sm">60s</span>
      </div>
      <button onclick="abortExam()" class="text-xs text-slate-500 hover:text-slate-300 px-2 py-1 rounded-lg border border-slate-800" data-t="btn_abort">انسحاب</button>
    </div>

    <div class="max-w-2xl mx-auto w-full h-2 bg-slate-900 rounded-full overflow-hidden mt-3 p-0.5 border border-slate-800">
      <div id="timerBar" class="h-full bg-gradient-to-r from-amber-400 to-rose-500 rounded-full transition-all duration-1000" style="width: 100%;"></div>
    </div>

    <div class="max-w-2xl mx-auto w-full my-auto py-6 space-y-6 text-white text-center">
      <span id="questionStepTag" class="text-[11px] font-extrabold uppercase tracking-widest text-amber-400 bg-amber-400/10 px-3 py-1 rounded-full border border-amber-400/20">1</span>
      <h3 id="gameQuestionText" class="text-lg md:text-2xl font-black text-slate-100 leading-snug">...</h3>
      <div id="gameOptionsContainer" class="grid grid-cols-1 sm:grid-cols-2 gap-3.5 pt-2 text-right"></div>
      <div class="pt-2 text-right"><button id="quizHintButton" type="button" onclick="toggleCurrentQuizHint()" class="rounded-xl border border-sky-400/40 bg-sky-400/10 px-4 py-2 text-xs font-black text-sky-200">💡 تلميح يساعدك على التفكير</button><p id="quizHintText" class="hidden mt-3 rounded-xl border border-sky-400/20 bg-sky-400/10 p-3 text-sm leading-relaxed text-sky-100 text-right"></p></div>
      <div id="quizAnswerFeedback" class="hidden mt-3 rounded-xl border border-slate-700 bg-slate-900 p-3 text-sm text-right"></div>
    </div>

    <div class="max-w-2xl mx-auto w-full text-center py-2 text-[11px] font-bold text-slate-500 border-t border-slate-900" data-t="quiz_footer_hint">
      أجب بدقة لتأكيد إتمام المرحلة وفتح ما بعدها
    </div>
  </div>

  <div id="roadmapAssessmentReview" class="fixed inset-0 z-[120] hidden items-center justify-center bg-slate-950/80 p-3 md:p-6">
    <div class="w-full max-w-3xl max-h-[90vh] overflow-y-auto rounded-2xl bg-white p-5 text-slate-900 shadow-2xl">
      <div class="flex items-start justify-between gap-3"><div><h2 class="text-xl font-black">مراجعة الاختبار وخطة التحسين</h2><p id="roadmapAssessmentSummary" class="mt-2 text-sm"></p></div><button type="button" onclick="closeRoadmapAssessmentReview()" class="rounded-lg border px-3 py-2 font-bold">إغلاق</button></div>
      <p class="mt-3 text-xs text-slate-500">الإجابات والتفسيرات تظهر بعد التسليم فقط. استخدم نقاط التحسين لاختيار ما ستراجعه قبل المحاولة التالية.</p>
      <div id="roadmapAssessmentDetails" class="mt-4 space-y-2"></div>
      <button type="button" onclick="closeRoadmapAssessmentReview();switchTab('gps')" class="mt-3 w-full rounded-xl bg-slate-900 px-4 py-3 font-black text-amber-300">العودة إلى الـ Roadmap</button>
    </div>
  </div>
  <!-- Mobile sidebar -->
  <div id="mobileSidebarBackdrop" class="fixed inset-0 bg-slate-950/50 backdrop-blur-sm z-30 hidden md:hidden" onclick="closeMobileSidebar()"></div>

  <aside id="mobileSidebar" aria-hidden="true" class="fixed top-0 right-0 bottom-0 w-[86vw] max-w-[340px] bg-white border-l border-amber-200/60 p-5 shadow-2xl z-40 translate-x-full overflow-y-auto md:hidden">
    <div class="flex flex-col justify-between min-h-full">
      <div class="space-y-6">
        <div class="flex items-center justify-between gap-3">
          <div class="flex items-center gap-2.5">
            <div class="w-10 h-10 rounded-2xl bg-amber-400 text-slate-950 flex items-center justify-center font-black text-xl shadow-md shadow-amber-200">⚡</div>
            <div>
              <h1 class="font-black text-base leading-tight text-slate-900">S-Tesseract</h1>
              <p class="text-[10px] text-slate-400 font-bold">Study Platform</p>
            </div>
          </div>
          <button type="button" onclick="closeMobileSidebar()" class="w-10 h-10 rounded-xl bg-slate-100 hover:bg-slate-200 text-slate-700 font-black text-xl" aria-label="Close menu">×</button>
        </div>

        <nav class="space-y-1 text-xs font-bold text-right">
          <button onclick="switchTab('studyroom')" class="nav-item w-full flex items-center justify-end gap-3 px-3.5 py-3 rounded-xl text-slate-600 hover:bg-amber-50 hover:text-amber-700 transition" data-target="studyroom"><span data-t="nav_studyroom">غرفة المذاكرة</span><span class="text-base">🚪</span></button>
          <button onclick="switchTab('home')" class="nav-item w-full flex items-center justify-end gap-3 px-3.5 py-3 rounded-xl text-slate-600 hover:bg-amber-50 hover:text-amber-700 transition" data-target="home"><span data-t="nav_home">الرئيسية</span><span class="text-base">🏠</span></button>
          <button onclick="switchTab('video')" class="nav-item w-full flex items-center justify-end gap-3 px-3.5 py-3 rounded-xl text-slate-600 hover:bg-amber-50 hover:text-amber-700 transition" data-target="video"><span data-t="nav_video">الفيديو السينمائي</span><span class="text-base">🎬</span></button>
           <button onclick="switchTab('studycenter')" class="nav-item w-full flex items-center justify-end gap-3 px-3.5 py-3 rounded-xl text-slate-600 hover:bg-amber-50 hover:text-amber-700 transition" data-target="studycenter"><span>AI Study Center</span><span class="text-base">🧠</span></button>
          <button onclick="switchTab('history')" class="nav-item w-full flex items-center justify-end gap-3 px-3.5 py-3 rounded-xl text-slate-600 hover:bg-amber-50 hover:text-amber-700 transition" data-target="history"><span data-t="nav_history">السجل</span><span class="text-base">🕘</span></button>
          <button onclick="switchTab('gps')" class="nav-item w-full flex items-center justify-end gap-3 px-3.5 py-3 rounded-xl text-slate-600 hover:bg-amber-50 hover:text-amber-700 transition" data-target="gps"><span data-t="nav_gps">خارطة التعلم</span><span class="text-base">🗺️</span></button>
          <button onclick="switchTab('calendar')" class="nav-item w-full flex items-center justify-end gap-3 px-3.5 py-3 rounded-xl text-slate-600 hover:bg-amber-50 hover:text-amber-700 transition" data-target="calendar"><span data-t="nav_calendar">التقويم</span><span class="text-base">📅</span></button>
          <button onclick="switchTab('leaderboard')" class="nav-item w-full flex items-center justify-end gap-3 px-3.5 py-3 rounded-xl text-slate-600 hover:bg-amber-50 hover:text-amber-700 transition" data-target="leaderboard"><span data-t="nav_leaderboard">المتصدرون</span><span class="text-base">🏆</span></button>
          <button onclick="switchTab('account')" class="nav-item w-full flex items-center justify-end gap-3 px-3.5 py-3 rounded-xl text-slate-600 hover:bg-amber-50 hover:text-amber-700 transition" data-target="account"><span data-t="nav_account">الحساب</span><span class="text-base">👤</span></button>
        </nav>
      </div>

      <button onclick="toggleLanguage()" class="w-full mt-6 px-4 py-3 bg-amber-50 border border-amber-200 text-amber-900 font-black text-xs rounded-xl">
        <span id="mobileSidebarLangBtnText">English</span>
      </button>
    </div>
  </aside>

  <aside class="w-64 bg-white/90 backdrop-blur-lg border-l border-amber-200/60 flex flex-col justify-between p-5 hidden md:flex shrink-0 shadow-sm z-20">
    <div class="space-y-6">
      <div class="flex items-center gap-2.5">
        <div class="w-10 h-10 rounded-2xl bg-amber-400 text-slate-950 flex items-center justify-center font-black text-xl shadow-md shadow-amber-200">
          ⚡
        </div>
        <div>
          <h1 class="font-black text-base leading-tight text-slate-900">S-Tesseract</h1>
        </div>
      </div>

      <nav class="space-y-1 text-xs font-bold text-right">
        <button onclick="switchTab('studyroom')" class="nav-item w-full flex items-center justify-end gap-3 px-3.5 py-2.5 rounded-xl text-slate-600 hover:bg-amber-50 hover:text-amber-700 transition" data-target="studyroom">
          <span data-t="nav_studyroom">غرفة المذاكرة</span><span class="text-base">🚪</span>
        </button>
        <button onclick="switchTab('home')" class="nav-item w-full flex items-center justify-end gap-3 px-3.5 py-2.5 rounded-xl text-slate-600 hover:bg-amber-50 hover:text-amber-700 transition" data-target="home">
          <span data-t="nav_home">الرئيسية</span><span class="text-base">⚡</span>
        </button>
        <button onclick="switchTab('video')" class="nav-item w-full flex items-center justify-end gap-3 px-3.5 py-2.5 rounded-xl text-slate-600 hover:bg-amber-50 hover:text-amber-700 transition" data-target="video">
          <span data-t="nav_video">إتقان الفكرة & مشاكل الواقع</span><span class="text-base">🎬</span>
        </button>
        <button onclick="switchTab('studycenter')" class="nav-item w-full flex items-center justify-end gap-3 px-3.5 py-2.5 rounded-xl text-slate-600 hover:bg-amber-50 hover:text-amber-700 transition" data-target="studycenter">
          <span>AI Study Center</span><span class="text-base">🧠</span>
        </button>
        <button onclick="switchTab('history')" class="nav-item w-full flex items-center justify-end gap-3 px-3.5 py-2.5 rounded-xl text-slate-600 hover:bg-amber-50 hover:text-amber-700 transition" data-target="history">
          <span data-t="nav_history">السجل</span><span class="text-base">🕘</span>
        </button>
        <button onclick="switchTab('gps')" class="nav-item w-full flex items-center justify-end gap-3 px-3.5 py-2.5 rounded-xl text-slate-600 hover:bg-amber-50 hover:text-amber-700 transition" data-target="gps">
          <span data-t="nav_gps">خارطة التعلم الذكية (Roadmap)</span><span class="text-base">🧭</span>
        </button>
        <button onclick="switchTab('calendar')" class="nav-item w-full flex items-center justify-end gap-3 px-3.5 py-2.5 rounded-xl text-slate-600 hover:bg-amber-50 hover:text-amber-700 transition" data-target="calendar">
          <span data-t="nav_schedule">الجدول</span><span class="text-base">📅</span>
        </button>
        <button onclick="switchTab('leaderboard')" class="nav-item w-full flex items-center justify-end gap-3 px-3.5 py-2.5 rounded-xl text-slate-600 hover:bg-amber-50 hover:text-amber-700 transition" data-target="leaderboard">
          <span data-t="nav_ranks">المتصدرين</span><span class="text-base">🏆</span>
        </button>
        <button onclick="switchTab('account')" class="nav-item w-full flex items-center justify-end gap-3 px-3.5 py-2.5 rounded-xl text-slate-600 hover:bg-amber-50 hover:text-amber-700 transition" data-target="account">
          <span data-t="nav_profile">الملف الشخصي</span><span class="text-base">👤</span>
        </button>
      </nav>
    </div>

    <div class="pt-4 border-t border-slate-100 flex items-center justify-between">
      <span class="text-xs font-bold text-slate-500" data-t="label_lang">اللغة</span>
      <button onclick="toggleLanguage()" class="px-3 py-1.5 bg-amber-100 hover:bg-amber-200 text-amber-900 font-black text-xs rounded-xl transition">
        <span id="langBtnText">English</span>
      </button>
    </div>
  </aside>

  <div class="flex-1 flex flex-col min-h-screen overflow-hidden">
    <header class="bg-white/70 backdrop-blur-md border-b border-amber-200/50 px-6 py-4 flex items-center justify-between">
      <div class="flex items-center gap-3">
        <div class="w-10 h-10 rounded-2xl bg-amber-100 text-amber-800 flex items-center justify-center font-black text-lg">
          🎯
        </div>
        <div>
          <h2 id="topBarUser" class="text-sm font-extrabold text-slate-900">...</h2>
          <p id="topBarRoleModel" class="text-[11px] text-amber-700 font-bold">...</p>
        </div>
      </div>
      
      <div class="flex items-center gap-3">
        <div class="flex items-center gap-1.5 bg-amber-50 border border-amber-200/60 px-3.5 py-1.5 rounded-full shadow-sm">
          <span class="text-sm">🔥</span>
          <span id="uiUserStreak" class="text-xs font-black text-amber-700 font-mono">0</span>
        </div>
        <button onclick="toggleLanguage()" class="md:hidden px-3 py-1.5 bg-amber-100 text-amber-900 font-black text-xs rounded-xl">
          <span id="mobileLangBtnText">EN</span>
        </button>
        <button id="mobileMenuButton" type="button" onclick="toggleMobileSidebar()" class="md:hidden w-10 h-10 rounded-xl bg-amber-100 hover:bg-amber-200 text-amber-900 flex items-center justify-center text-xl font-black shadow-sm" aria-label="Open menu" aria-controls="mobileSidebar" aria-expanded="false">☰</button>
      </div>
    </header>

    <main id="mainContentArea" class="flex-1 overflow-y-auto p-6 md:p-8 no-scrollbar max-w-4xl mx-auto w-full pb-24 md:pb-8 text-right">

      <div id="tab-studyroom" class="tab-content hidden space-y-6">
        <div class="banana-card p-6 md:p-8 rounded-3xl space-y-6">
          
          <div class="flex justify-between items-center border-b border-amber-100 pb-4">
            <h2 class="text-lg font-black text-slate-900" data-t="studyroom_title">غرفة المذاكرة</h2>
            <button onclick="toggleFullscreenIsolation()" id="dndBtn" class="px-4 py-2 bg-slate-900 hover:bg-slate-800 text-amber-300 font-black text-xs rounded-xl shadow-md transition">
              <span id="dndBtnText" data-t="btn_isolation">وضع العزل والشاشة الكاملة</span>
            </button>
          </div>

          <div class="grid grid-cols-1 md:grid-cols-2 gap-6 items-center">
            
            <div class="p-6 bg-slate-950 rounded-3xl text-white text-center space-y-4 shadow-xl border border-slate-800">
              <div class="text-xs text-slate-400" data-t="timer_label">مؤقت التركيز</div>
              <div class="py-2">
                <span id="focusDisplayTime" class="font-mono text-5xl md:text-6xl font-black text-amber-300">25:00</span>
              </div>
              <div class="flex justify-center gap-3">
                <button onclick="startFocusTimer()" id="timerControlBtn" class="px-6 py-2.5 bg-amber-400 hover:bg-amber-300 text-slate-950 font-black text-xs rounded-xl shadow-lg transition" data-t="btn_start_timer">
                  بدء الجلسة ▶
                </button>
                <button onclick="resetFocusTimer()" class="px-4 py-2.5 bg-slate-800 hover:bg-slate-700 text-slate-300 font-bold text-xs rounded-xl transition" data-t="btn_reset_timer">
                  إعادة ضبط
                </button>
              </div>
              <div class="flex justify-center gap-2 pt-1 text-[10px] text-slate-400 font-bold">
                <button onclick="setTimerDuration(25)" class="px-2.5 py-1 bg-slate-900 rounded-lg hover:text-amber-400">25m</button>
                <button onclick="setTimerDuration(45)" class="px-2.5 py-1 bg-slate-900 rounded-lg hover:text-amber-400">45m</button>
                <button onclick="setTimerDuration(60)" class="px-2.5 py-1 bg-slate-900 rounded-lg hover:text-amber-400">60m</button>
              </div>
            </div>

            <div class="p-6 bg-amber-50/60 rounded-3xl border border-amber-200/60 space-y-4">
              <div class="flex flex-wrap justify-between items-center gap-3">
                <span class="text-xs font-black text-slate-900" data-t="todo_header">قائمة المهام</span>
                <span id="todoStats" class="text-[10px] font-mono font-bold bg-amber-200 text-amber-900 px-2 py-0.5 rounded-md">0</span>
              </div>
              <div class="flex gap-2">
                <input type="text" id="newTodoInput" class="flex-1 bg-white border border-slate-200 rounded-xl p-2.5 text-xs text-slate-800 focus:outline-none focus:ring-2 focus:ring-amber-400">
                <button onclick="addNewTodo()" class="px-4 py-2.5 bg-slate-900 hover:bg-slate-800 text-amber-300 font-black text-xs rounded-xl shadow-sm transition" data-t="btn_add">إضافة</button>
              </div>
              <div id="todoListContainer" class="space-y-2 max-h-48 overflow-y-auto no-scrollbar pt-1"></div>
            </div>

          </div>

          <div class="p-6 bg-gradient-to-br from-slate-950 to-slate-900 rounded-3xl text-white space-y-4 border border-indigo-400/30 shadow-2xl">
            <div class="flex flex-wrap justify-between items-center gap-3">
              <div><h3 class="text-base font-black text-indigo-300">🔕 Focus + 🎧 Study Audio</h3><p class="text-[11px] text-slate-400 mt-1">Focus Mode والصوت يعملان معًا.</p></div>
              <button id="focusModeToggle" onclick="toggleStudyFocus()" class="px-4 py-2 rounded-xl bg-emerald-500 text-slate-950 font-black text-xs">Focus ON</button>
            </div>
            <div class="grid grid-cols-2 md:grid-cols-4 gap-2">
              <button onclick="setStudyAudio('quran')" class="p-3 rounded-xl bg-slate-800 hover:bg-indigo-600 text-xs font-black">📖 قرآن</button>
              <button onclick="setStudyAudio('white')" class="p-3 rounded-xl bg-slate-800 hover:bg-indigo-600 text-xs font-black">🤍 White Noise</button>
              <button onclick="setStudyAudio('brown')" class="p-3 rounded-xl bg-slate-800 hover:bg-indigo-600 text-xs font-black">🤎 Brown Noise</button>
              <button onclick="setStudyAudio('rain')" class="p-3 rounded-xl bg-slate-800 hover:bg-indigo-600 text-xs font-black">🌧️ Rain</button>
              <button onclick="setStudyAudio('ocean')" class="p-3 rounded-xl bg-slate-800 hover:bg-indigo-600 text-xs font-black">🌊 Ocean</button>
              <button onclick="setStudyAudio('forest')" class="p-3 rounded-xl bg-slate-800 hover:bg-indigo-600 text-xs font-black">🌲 Forest</button>
              <button onclick="setStudyAudio('cafe')" class="p-3 rounded-xl bg-slate-800 hover:bg-indigo-600 text-xs font-black">☕ Café</button>
              <button onclick="setStudyAudio('none')" class="p-3 rounded-xl bg-slate-800 hover:bg-red-600 text-xs font-black">⏹️ إيقاف الصوت</button>
            </div>
            <div class="flex flex-wrap items-center gap-3 text-xs"><label>🔊 الصوت</label><input id="studyAudioVolume" type="range" min="0" max="100" value="35" oninput="setStudyVolume(this.value)" class="flex-1"><span id="studyAudioVolumeText">35%</span></div>
            <div id="quranControls" class="hidden grid md:grid-cols-2 gap-2">
              <select id="quranReciter" onchange="setQuranSetting()" class="bg-slate-800 rounded-xl p-2 text-xs"><option value="1">إبراهيم الأخضر</option><option value="7">عبد الباسط</option><option value="9">محمد صديق المنشاوي</option><option value="12">محمود خليل الحصري</option><option value="253">إسلام صبحي</option></select>
              <div class="md:col-span-2 grid grid-cols-[auto_1fr_auto] gap-2 items-center">
                <button type="button" onclick="changeQuranSurah(-1)" class="px-3 py-2 rounded-xl bg-slate-700 hover:bg-indigo-600 text-xs font-black" title="السورة السابقة">‹</button>
                <select id="quranSurah" onchange="setQuranSetting()" class="min-w-0 bg-slate-800 rounded-xl p-2 text-xs"></select>
                <button type="button" onclick="changeQuranSurah(1)" class="px-3 py-2 rounded-xl bg-slate-700 hover:bg-indigo-600 text-xs font-black" title="السورة التالية">›</button>
              </div>
              <div class="md:col-span-2 text-[10px] text-slate-400">114 سورة كاملة — اختر السورة أو استخدم السهمين للتنقل.</div>
            </div>
            <audio id="studyQuranAudio" class="w-full hidden" controls preload="none"></audio>
            <div id="studyAudioStatus" class="text-[10px] text-slate-400">اختر الصوت الذي يناسبك.</div>
          </div>

          <div class="p-6 bg-slate-950 rounded-3xl text-white space-y-5 border-2 border-amber-400/40 shadow-2xl">
            <h3 class="text-base font-black text-amber-300" data-t="evaluator_title">المصحح الذكي</h3>

            <div class="space-y-3.5">
              <div>
                <label class="block text-xs font-bold text-slate-300 mb-1.5" data-t="label_question">نص السؤال أو المسألة:</label>
                <textarea id="hwQuestionInput" rows="3" class="w-full bg-slate-900 border border-slate-800 rounded-2xl p-3 text-xs text-slate-100 focus:outline-none focus:border-amber-400"></textarea>
              </div>

              <div>
                <label class="block text-xs font-bold text-slate-300 mb-1.5" data-t="label_answer">الحل (اختياري للتقييم):</label>
                <textarea id="hwStudentAnswerInput" rows="3" class="w-full bg-slate-900 border border-slate-800 rounded-2xl p-3 text-xs text-slate-100 focus:outline-none focus:border-amber-400"></textarea>
              </div>

              <div class="flex flex-col sm:flex-row items-center gap-3">
                <div class="flex-1 w-full">
                  <input type="file" id="hwImageInput" accept="image/*" class="block w-full text-xs text-slate-400 file:mr-2 file:py-1.5 file:px-3 file:rounded-xl file:border-0 file:text-xs file:font-bold file:bg-slate-800 file:text-amber-300">
                </div>
                <button id="hwSubmitBtn" onclick="evaluateOrSolveHomework()" class="w-full sm:w-auto px-7 py-3 bg-gradient-to-r from-amber-400 to-yellow-400 hover:from-amber-300 text-slate-950 font-black text-xs rounded-xl shadow-lg transition" data-t="btn_evaluate">
                  إرسال للتقييم أو الحل
                </button>
              </div>
            </div>

            <div id="hwResultCard" class="hidden p-5 bg-slate-900 rounded-2xl border-2 border-amber-400/60 space-y-4">
              <div class="flex justify-between items-center border-b border-slate-800 pb-3">
                <h4 id="hwResultVerdict" class="text-base font-black text-white">...</h4>
                <div id="hwScoreBox" class="flex items-center gap-3">
                  <div class="text-center px-3 py-1 bg-slate-950 rounded-xl border border-slate-800">
                    <span id="hwScoreText" class="text-base font-black text-emerald-400 font-mono">0%</span>
                  </div>
                  <div class="text-center px-3 py-1 bg-slate-950 rounded-xl border border-slate-800">
                    <span id="hwXpText" class="text-base font-black text-amber-400 font-mono">+0 XP</span>
                  </div>
                </div>
              </div>

              <div class="space-y-1">
                <span class="text-xs font-bold text-amber-400" data-t="feedback_label">التقييم:</span>
                <p id="hwFeedbackText" class="text-xs text-slate-200 leading-relaxed font-medium bg-slate-950 p-3 rounded-xl border border-slate-800"></p>
              </div>

              <div class="space-y-1">
                <span id="hwSolutionLabel" class="text-xs font-bold text-emerald-400" data-t="solution_label">الحل النموذجي:</span>
                <div id="hwModelSolutionText" class="text-xs text-slate-200 leading-relaxed font-mono bg-slate-950 p-4 rounded-xl border border-emerald-500/30 whitespace-pre-line"></div>
              </div>
            </div>

          </div>

        </div>
      </div>

      <div id="tab-home" class="tab-content space-y-6">
        <div class="banana-card p-6 md:p-8 rounded-3xl relative overflow-hidden bg-gradient-to-r from-amber-300 via-amber-200 to-yellow-200 text-slate-900">
          <div class="max-w-xl space-y-3">
            <h2 class="text-2xl md:text-3xl font-black leading-tight" data-t="hero_title">
              منصة S-Tesseract للتعلم والتركيز
            </h2>
            <div class="flex flex-wrap gap-3 pt-2">
              <button onclick="switchTab('studyroom')" class="px-5 py-2.5 bg-slate-900 hover:bg-slate-800 text-amber-300 font-black text-xs rounded-xl shadow-md transition" data-t="btn_enter_room">
                غرفة المذاكرة ←
              </button>
            </div>
          </div>
        </div>

        <div class="grid grid-cols-1 sm:grid-cols-3 gap-4">
          <div class="banana-card p-5 rounded-2xl space-y-1">
            <span class="text-xs font-bold text-slate-400 uppercase tracking-wider block" data-t="metric_xp">إجمالي النقاط</span>
            <div class="flex items-baseline gap-1.5">
              <span id="uiUserXP" class="text-3xl font-black text-amber-600 font-mono">0</span>
              <span class="text-xs font-bold text-slate-400">XP</span>
            </div>
          </div>
          <div class="banana-card p-5 rounded-2xl space-y-1">
            <span class="text-xs font-bold text-slate-400 uppercase tracking-wider block" data-t="metric_gps">التقدم</span>
            <div class="flex items-baseline gap-1.5">
              <span id="uiGpsProgress" class="text-3xl font-black text-indigo-600 font-mono">0%</span>
            </div>
          </div>
          <div class="banana-card p-5 rounded-2xl space-y-1">
            <span class="text-xs font-bold text-slate-400 uppercase tracking-wider block" data-t="metric_accuracy">الدقة</span>
            <div class="flex items-baseline gap-1.5">
              <span id="uiHonestyRate" class="text-3xl font-black text-emerald-600 font-mono">100%</span>
            </div>
          </div>
        </div>
      </div>

      <div id="tab-video" class="tab-content hidden space-y-6">
        <div class="banana-card p-6 md:p-8 rounded-3xl space-y-6">
          <div class="border-b border-amber-100 pb-4 flex flex-col md:flex-row md:items-center justify-between gap-3">
            <div>
              <h2 class="text-lg font-black text-slate-900">استوديو الفيديو 🎬</h2>
              <p class="text-xs text-slate-500 font-medium mt-1">اختار طريقة الفيديو: سريع بدون Gemini، فيديو AI، مصادر YouTube، أو الاثنين.</p>
            </div>
            <span class="px-3 py-1.5 bg-slate-950 text-amber-300 font-black text-xs rounded-xl">VIDEO STUDIO</span>
          </div>

          <div class="p-5 bg-amber-50/60 border border-amber-200/60 rounded-2xl space-y-5">
            <input type="file" id="pdfVideoInput" accept="application/pdf"
                   class="block w-full text-xs text-slate-500 file:mr-2 file:py-2 file:px-3 file:rounded-xl file:border-0 file:text-xs file:font-bold file:bg-slate-900 file:text-amber-300">

            <div class="space-y-2">
              <div class="text-xs font-black text-slate-700">لغة الفيديو</div>
              <div class="grid grid-cols-2 gap-2">
                <button id="videoLangAr" type="button" onclick="selectVideoLanguage('ar')" class="video-lang-btn p-3 bg-indigo-600 text-white rounded-2xl font-black text-xs shadow-lg transition">🇪🇬 عربي</button>
                <button id="videoLangEn" type="button" onclick="selectVideoLanguage('en')" class="video-lang-btn p-3 bg-slate-900 text-slate-300 hover:bg-slate-800 rounded-2xl font-black text-xs transition">🇬🇧 English</button>
              </div>
            </div>

            <div class="p-5 rounded-3xl bg-slate-950 border border-slate-800 text-white space-y-4">
              <div class="flex items-center gap-3">
                <div class="w-10 h-10 rounded-2xl bg-amber-400 text-slate-950 flex items-center justify-center text-xl">🧭</div>
                <div>
                  <div class="font-black text-sm">اختار طريقة الشرح المناسبة ليك</div>
                  <div class="text-[11px] text-slate-400 mt-1">مش عارف تختار؟ شوف الفرق في ثانية.</div>
                </div>
              </div>
              <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
                <div class="rounded-2xl bg-slate-900 border border-slate-800 p-4"><div class="text-xl">⚡</div><div class="font-black text-xs mt-2">عايز شرح سريع</div><div class="text-[10px] text-slate-400 mt-1 leading-relaxed">شرح مباشر من الـPDF، مناسب للمراجعة السريعة.</div></div>
                <div class="rounded-2xl bg-slate-900 border border-slate-800 p-4"><div class="text-xl">🤖</div><div class="font-black text-xs mt-2">عايز شرح أذكى</div><div class="text-[10px] text-slate-400 mt-1 leading-relaxed">شرح منظم أعمق، مع أمثلة وربط بين الأفكار.</div></div>
                <div class="rounded-2xl bg-slate-900 border border-slate-800 p-4"><div class="text-xl">▶️</div><div class="font-black text-xs mt-2">عايز فيديوهات جاهزة</div><div class="text-[10px] text-slate-400 mt-1 leading-relaxed">روابط بحث YouTube تساعدك تلاقي شروحات مناسبة.</div></div>
                <div class="rounded-2xl bg-slate-900 border border-slate-800 p-4"><div class="text-xl">🔀</div><div class="font-black text-xs mt-2">عايز الاتنين</div><div class="text-[10px] text-slate-400 mt-1 leading-relaxed">شرح AI ومعاه مصادر فيديو من YouTube.</div></div>
              </div>
            </div>

            <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
              <button type="button" id="videoModeFast" onclick="selectVideoMode('fast')" class="video-mode-card text-right p-4 rounded-2xl border-2 border-emerald-400 bg-emerald-50 transition">
                <div class="text-2xl">⚡</div><div class="font-black text-sm text-slate-900 mt-1">شرح سريع</div>
                <div class="text-[10px] text-slate-500 mt-1">لو عايز تراجع بسرعة من محتوى ملفك.</div>
              </button>
              <button type="button" id="videoModeAi" onclick="selectVideoMode('ai')" class="video-mode-card text-right p-4 rounded-2xl border-2 border-slate-200 bg-white transition">
                <div class="text-2xl">🤖</div><div class="font-black text-sm text-slate-900 mt-1">شرح أذكى</div>
                <div class="text-[10px] text-slate-500 mt-1">شرح أعمق ومنظم بطريقة تعليمية.</div>
              </button>
              <button type="button" id="videoModeYoutube" onclick="selectVideoMode('youtube')" class="video-mode-card text-right p-4 rounded-2xl border-2 border-slate-200 bg-white transition">
                <div class="text-2xl">▶️</div><div class="font-black text-sm text-slate-900 mt-1">فيديوهات جاهزة</div>
                <div class="text-[10px] text-slate-500 mt-1">ابحث عن شروحات مناسبة على YouTube.</div>
              </button>
              <button type="button" id="videoModeBoth" onclick="selectVideoMode('both')" class="video-mode-card text-right p-4 rounded-2xl border-2 border-slate-200 bg-white transition">
                <div class="text-2xl">🔀</div><div class="font-black text-sm text-slate-900 mt-1">الاتنين</div>
                <div class="text-[10px] text-slate-500 mt-1">شرح أذكى + مصادر YouTube.</div>
              </button>
            </div>

            <div class="grid grid-cols-1 md:grid-cols-3 gap-2">
              <button id="persBtn0" type="button" onclick="selectVideoPerspective(1)" class="pers-tab p-3 bg-indigo-600 text-white rounded-2xl font-black text-xs shadow-lg transition text-center">1. الأساس العلمي والشرح</button>
              <button id="persBtn1" type="button" onclick="selectVideoPerspective(2)" class="pers-tab p-3 bg-slate-900 text-slate-300 hover:bg-slate-800 rounded-2xl font-black text-xs transition text-center">2. بنستخدمها في الحياة</button>
              <button id="persBtn2" type="button" onclick="selectVideoPerspective(3)" class="pers-tab p-3 bg-slate-900 text-slate-300 hover:bg-slate-800 rounded-2xl font-black text-xs transition text-center">3. الامتحان والتفكير العالي</button>
            </div>

            <button id="genVideoBtn" type="button" onclick="generateInteractiveVideo()" class="w-full py-3.5 bg-gradient-to-r from-indigo-600 via-purple-700 to-indigo-600 text-white font-black text-xs rounded-xl shadow-lg transition flex items-center justify-center gap-2">
              إنشاء الفيديو 🎬
            </button>

            <div id="videoJobBox" class="hidden p-4 bg-slate-950 rounded-2xl border border-slate-800 space-y-3 text-white">
              <div class="flex items-center justify-between gap-3"><span id="videoJobStage" class="text-xs font-black text-amber-300">جاري تجهيز الفيديو...</span><span id="videoJobPercent" class="text-xs font-mono text-slate-400">0%</span></div>
              <div class="w-full h-2 rounded-full bg-slate-800 overflow-hidden"><div id="videoJobProgress" class="h-full bg-indigo-500 transition-all" style="width:0%"></div></div>
              <p id="videoJobHint" class="text-[10px] text-slate-500">المعالجة تتم على السيرفر في الخلفية. يمكنك إغلاق الموقع.</p>
            </div>
          </div>

          <div id="videoPlayerBox" class="hidden space-y-5">
            <div class="video-frame-box bg-slate-950 rounded-3xl overflow-hidden text-white p-5 md:p-7 space-y-5">
              <div class="grid grid-cols-1 md:grid-cols-3 gap-3">
                <div class="p-3 rounded-2xl bg-slate-900 border border-slate-800"><div class="text-[9px] text-slate-500 font-black mb-1">الموضوع</div><div id="videoConceptTitle" class="text-xs font-black text-white leading-relaxed">...</div></div>
                <div class="p-3 rounded-2xl bg-slate-900 border border-slate-800"><div class="text-[9px] text-slate-500 font-black mb-1">النوع</div><div id="videoModeTitle" class="text-xs font-black text-amber-300 leading-relaxed">...</div></div>
                <div class="p-3 rounded-2xl bg-slate-900 border border-slate-800"><div class="text-[9px] text-slate-500 font-black mb-1">طريقة الشرح</div><div id="videoPerspectiveTitle" class="text-xs font-black text-amber-300 leading-relaxed">...</div></div>
              </div>
              <div id="videoElementWrap" class="relative w-full aspect-video bg-black rounded-2xl overflow-hidden border border-slate-800 shadow-2xl"><video id="generatedLessonVideo" class="w-full h-full object-contain bg-black" controls playsinline preload="metadata"></video></div>
              <div id="youtubeResultsBox" class="hidden bg-slate-900/90 rounded-2xl p-5 border border-slate-800 space-y-3"><div class="font-black text-xs text-amber-400">▶️ فيديوهات مقترحة من YouTube</div><div id="youtubeResultsContainer" class="grid grid-cols-1 md:grid-cols-2 gap-2"></div></div>
              <div class="grid grid-cols-1 lg:grid-cols-2 gap-4">
                <div class="bg-slate-900/90 rounded-2xl p-5 border border-slate-800 space-y-3"><div class="flex items-center gap-2 text-amber-400 font-bold text-xs"><span>💡</span><span>مفاتيح الفهم</span></div><div id="keyInsightsContainer" class="space-y-2 text-xs text-slate-200"></div></div>
                <div class="bg-gradient-to-r from-slate-900 to-indigo-950 rounded-2xl p-5 border border-indigo-500/30 space-y-3"><div class="flex items-center gap-2 text-amber-400 font-black text-xs"><span>🏗️</span><span>تطبيق واقعي</span></div><h4 id="csTitle" class="text-sm font-extrabold text-white">...</h4><p id="csProblem" class="text-xs text-slate-300 leading-relaxed">...</p><div class="pt-2 border-t border-slate-800"><span class="text-emerald-400 font-bold text-xs block mb-1">الحل الهندسي</span><p id="csSolution" class="text-xs text-slate-300 leading-relaxed">...</p></div></div>
              </div>
              <div class="bg-slate-900/90 rounded-2xl p-5 border border-slate-800 space-y-3"><div class="flex items-center gap-2 text-indigo-300 font-black text-xs"><span>🧠</span><span>الملخص</span></div><div id="videoRecapContainer" class="grid grid-cols-1 md:grid-cols-3 gap-2 text-xs text-slate-200"></div></div>
              <div class="pt-3 border-t border-slate-800 flex flex-col md:flex-row md:justify-between md:items-center gap-3"><p class="text-xs text-slate-400">بعد المشاهدة، اختبر فهمك.</p><button type="button" onclick="startVideoLockdownQuiz()" class="px-5 py-2.5 bg-gradient-to-r from-amber-400 to-yellow-400 text-slate-950 font-black text-xs rounded-xl shadow-md transition">دخول الكويز (+50 XP) ←</button></div>
            </div>
          </div>
        </div>
      </div>

      <div id="tab-learningstudio" class="tab-content hidden space-y-6">
        <div class="banana-card p-5 md:p-7 rounded-3xl space-y-5">
          <div class="flex flex-wrap justify-between items-start gap-3 border-b border-amber-100 pb-4">
            <div><h2 class="text-xl font-black text-slate-900">🧩 التطبيق العملي المرتبط بالمسار</h2><p class="text-xs text-slate-600 mt-2">مساحة واحدة للتطبيق في مجالات مختلفة: برمجة، رياضيات، علوم، لغات، هندسة، بحث، أعمال، وتصميم.</p></div><span class="rounded-xl bg-amber-100 px-3 py-2 text-xs font-black text-amber-900">Practice · Feedback · History</span>
          </div>
          <div class="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <section class="rounded-2xl border border-slate-200 bg-white p-4 space-y-3">
              <h3 class="font-black text-sm">1) أنشئ نشاطًا تعليميًا متخصصًا</h3>
              <label class="block text-xs font-bold">المجال</label><select id="lsDomain" onchange="lsToggleEditor()" class="w-full rounded-xl border p-3 text-sm"><option value="programming">البرمجة وعلوم الكمبيوتر</option><option value="mathematics">الرياضيات</option><option value="physics">الفيزياء والعلوم</option><option value="chemistry">الكيمياء</option><option value="engineering">الهندسة والهاردوير</option><option value="languages">اللغات</option><option value="medicine_biology">العلوم الحيوية / الطب للتعلّم</option><option value="law_economics">القانون والاقتصاد</option><option value="history_geography">التاريخ والجغرافيا</option><option value="business">الأعمال والإدارة</option><option value="arts_design">الفنون والتصميم</option><option value="other">مجال آخر</option></select>
              <label class="block text-xs font-bold">الموضوع المحدد</label><input id="lsTopic" class="w-full rounded-xl border p-3 text-sm" placeholder="مثال: قوانين نيوتن، SQL joins، present perfect...">
              <div class="grid grid-cols-2 gap-3"><div><label class="block text-xs font-bold mb-1">المستوى</label><select id="lsLevel" class="w-full rounded-xl border p-3 text-sm"><option value="beginner">مبتدئ</option><option value="intermediate">متوسط</option><option value="advanced">متقدم</option></select></div><div><label class="block text-xs font-bold mb-1">لغة النشاط</label><select id="lsLang" class="w-full rounded-xl border p-3 text-sm"><option value="ar">العربية</option><option value="en">English</option></select></div></div>
              <label class="block text-xs font-bold">هدفك من النشاط</label><input id="lsGoal" class="w-full rounded-xl border p-3 text-sm" value="أفهم المفهوم وأطبقه بنفسي">
              <button id="lsGenerateBtn" onclick="lsGenerateActivity()" class="w-full rounded-xl bg-slate-900 text-amber-300 font-black p-3 text-sm">إنشاء نشاط مناسب للمجال ✨</button>
              <p class="text-[11px] text-slate-500">المراجع ستظهر كاقتراحات بحث عند عدم توافر رابط موثوق. المحتوى المُولّد يحتاج مراجعة عند استخدامه في مجالات عالية المخاطر.</p>
            </section>
            <section class="rounded-2xl border border-slate-200 bg-white p-4 space-y-3">
              <h3 class="font-black text-sm">2) حلّ النشاط واحفظ شغلك</h3>
              <input id="lsWorkTitle" class="w-full rounded-xl border p-3 text-sm" placeholder="عنوان الحل أو المشروع">
              <label class="block text-xs font-bold">تعليمات / سؤال المهمة</label><textarea id="lsTask" rows="4" class="w-full rounded-xl border p-3 text-sm" placeholder="اكتب السؤال أو انسخ تعليمات النشاط هنا..."></textarea>
              <div id="lsAnswerWrap"><label class="block text-xs font-bold">إجابتك / الحل / الملاحظات</label><textarea id="lsAnswer" rows="8" class="w-full rounded-xl border p-3 text-sm" placeholder="اكتب خطوات الحل أو إجابتك أو تحليل الحالة هنا..."></textarea></div>
              <div id="lsCodeEditorWrap" class="space-y-3"><div class="flex flex-wrap items-center justify-between gap-2"><label class="text-xs font-bold">محرر الأكواد (لأنشطة البرمجة)</label><select id="lsCodeLanguage" class="rounded-lg border p-2 text-xs"><option value="python">Python</option><option value="cpp">C++</option><option value="javascript">JavaScript</option><option value="java">Java</option><option value="c">C</option></select></div>
              <textarea id="lsCode" spellcheck="false" autocapitalize="off" autocomplete="off" class="w-full min-h-[220px] rounded-xl border border-slate-700 bg-slate-950 text-emerald-200 p-4 font-mono text-sm leading-relaxed" placeholder="# اكتب الكود هنا...
print('Hello, S-TESSERACT')"></textarea>
              <label class="block text-xs font-bold">Input (اختياري)</label><textarea id="lsStdin" rows="2" class="w-full rounded-xl border p-3 font-mono text-xs" placeholder="بيانات الإدخال للبرنامج"></textarea>
              <label class="block text-xs font-bold">اختبارات تلقائية (JSON) — اختياري</label><textarea id="lsTestCases" rows="3" class="w-full rounded-xl border p-3 font-mono text-xs" placeholder='[{"input":"2\\n3\\n","expected":"5"}]'></textarea><button onclick="lsRunTests()" class="w-full rounded-xl bg-cyan-700 text-white font-black p-3 text-xs">🧪 تشغيل حالات الاختبار</button></div>
              <div class="grid grid-cols-1 sm:grid-cols-3 gap-2"><button id="lsRunCodeBtn" onclick="lsRunCode()" class="rounded-xl bg-emerald-600 text-white font-black p-3 text-xs">▶ تشغيل الكود</button><button onclick="lsGetFeedback()" class="rounded-xl bg-indigo-600 text-white font-black p-3 text-xs">🧠 راجع إجابتي بالـ AI</button><button onclick="lsSaveWork()" class="rounded-xl bg-amber-400 text-slate-950 font-black p-3 text-xs">💾 حفظ في History</button></div>
              <p class="text-[10px] leading-relaxed text-slate-500">تنبيه خصوصية: تشغيل الكود يرسل الكود إلى خدمة تشغيل خارجية معزولة (Piston) لتشغيله؛ لا تضع أسرارًا أو كلمات مرور داخل الكود. لا يتم تنفيذ كود الطالب مباشرةً على سيرفر الموقع.</p>
            </section>
          </div>
          <section id="lsActivityResult" class="hidden rounded-2xl border border-amber-200 bg-amber-50 p-4 space-y-3"></section>
          <section id="lsOutputPanel" class="hidden rounded-2xl border border-slate-700 bg-slate-950 text-white p-4 space-y-2"><div class="font-black text-sm">Output / Feedback</div><pre id="lsOutput" class="whitespace-pre-wrap break-words text-xs leading-relaxed"></pre></section>
          <section class="rounded-2xl border border-slate-200 bg-white p-4 space-y-3"><div class="flex items-center justify-between gap-2"><h3 class="font-black text-sm">آخر أعمالك</h3><button onclick="lsLoadHistory()" class="rounded-lg bg-slate-100 px-3 py-2 text-xs font-bold">تحديث السجل</button></div><div id="lsRecentHistory" class="space-y-2 text-xs"><p class="text-slate-500">افتح نشاطًا من الـ Roadmap للوصول إلى التطبيق العملي.</p></div></section>
        </div>
      </div>

      <div id="tab-gps" class="tab-content hidden space-y-6">
        <div class="banana-card p-6 md:p-8 rounded-3xl space-y-6">
          <div class="border-b border-amber-100 pb-3 flex justify-between items-center">
            <div>
              <h2 class="text-lg font-black text-slate-900" data-t="gps_title">خارطة التعلم الذكية (Roadmap Generator)</h2>
              <p class="text-xs text-slate-500 font-medium mt-1">حدد تخصصك والمدة، وسنقوم ببناء خطة مقسمة بأفضل الكورسات المجانية، مع اختبار شامل بعد كل مرحلة للتحقق من الفهم.</p>
            </div>
            <span class="text-2xl">🗺️</span>
          </div>

          <div class="grid grid-cols-1 sm:grid-cols-2 gap-4 bg-amber-50/60 p-5 rounded-2xl border border-amber-200/60">
            <div>
              <label class="block text-xs font-bold text-slate-700 mb-1.5" data-t="label_field_goal">التخصص أو المادة التي تريد دراستها:</label>
              <input type="text" id="gpsGoalInput" placeholder="مثلاً: Full-Stack Web Development, فيزياء ثانوية عامة, الذكاء الاصطناعي..." class="w-full bg-white border border-slate-200 rounded-xl p-3 text-xs text-slate-800 focus:outline-none focus:ring-2 focus:ring-amber-400">
            </div>
            <div>
              <label class="block text-xs font-bold text-slate-700 mb-1.5">المدة الزمنية المتاحة لديك:</label>
              <select id="gpsDurationInput" class="w-full bg-white border border-slate-200 rounded-xl p-3 text-xs text-slate-800 focus:outline-none focus:ring-2 focus:ring-amber-400 font-bold">
                <option value="أسبوعين مكثفين">أسبوعين (مكثف جداً)</option>
                <option value="شهر واحد" selected>شهر واحد (معدل متوازن)</option>
                <option value="3 أشهر">3 أشهر (دراسة عميقة وتفصيلية)</option>
                <option value="6 أشهر">6 أشهر (إتقان شامل من الصفر للاحتراف)</option>
              </select>
            </div>
            <div>
              <label class="block text-xs font-bold text-slate-700 mb-1.5">وقت المذاكرة في اليوم</label>
              <select id="gpsDailyTimeInput" class="w-full bg-white border border-slate-200 rounded-xl p-3 text-xs text-slate-800 font-bold">
                <option value="30">30 دقيقة</option><option value="60" selected>ساعة</option><option value="90">ساعة ونصف</option><option value="120">ساعتان</option><option value="180">3 ساعات</option><option value="240">4 ساعات أو أكثر</option>
              </select>
            </div>
            <div>
              <label class="block text-xs font-bold text-slate-700 mb-1.5">أيام المذاكرة في الأسبوع</label>
              <select id="gpsStudyDaysInput" class="w-full bg-white border border-slate-200 rounded-xl p-3 text-xs text-slate-800 font-bold">
                <option value="3">3 أيام</option><option value="4">4 أيام</option><option value="5" selected>5 أيام</option><option value="6">6 أيام</option><option value="7">كل يوم</option>
              </select>
            </div>
            <div class="sm:col-span-2">
              <button id="generateGpsBtn" onclick="generateGpsRoadmap()" class="w-full py-3.5 bg-slate-900 hover:bg-slate-800 text-amber-300 font-black text-xs rounded-xl shadow-md transition flex items-center justify-center gap-2">
                <span>إنشاء مسار مخصص بموارد وتطبيقات مناسبة</span> 🚀
              </button>
            </div>
          </div>

          <div class="space-y-4 pt-2">
            <h3 class="text-sm font-black text-slate-900 flex items-center gap-2">
              <span>📍</span> <span>محطات المسار المعتمدة:</span>
            </h3>
            <div id="gpsRoadmapContainer" class="space-y-4">
              </div>
          </div>
        </div>
      </div>

      <div id="tab-calendar" class="tab-content hidden space-y-4">
        <div class="calendar-shell rounded-[28px] overflow-hidden border border-slate-800 shadow-2xl bg-slate-950 text-white">
          <div class="px-4 md:px-6 py-4 border-b border-slate-800">
            <div class="flex flex-wrap items-center justify-between gap-3">
              <div class="flex items-center gap-2">
                <button onclick="openEventModal()" class="w-10 h-10 flex items-center justify-center rounded-xl bg-blue-500 hover:bg-blue-400 text-white font-black text-xl">+</button>
                <button onclick="calGoToday()" class="px-3.5 py-2.5 bg-slate-900 hover:bg-slate-800 border border-slate-700 text-slate-200 font-bold text-xs rounded-xl">اليوم</button>
                <button onclick="calShiftWeek(-1)" class="w-9 h-9 rounded-xl bg-slate-900 border border-slate-700 hover:bg-slate-800 font-black">‹</button>
                <button onclick="calShiftWeek(1)" class="w-9 h-9 rounded-xl bg-slate-900 border border-slate-700 hover:bg-slate-800 font-black">›</button>
              </div>
              <div class="text-center">
                <div class="text-[9px] font-black text-slate-500 uppercase tracking-widest">S-TESSERACT CALENDAR</div>
                <h3 id="calRangeLabel" class="text-sm md:text-base font-black text-white">...</h3>
              </div>
              <div id="telegramControls" class="flex items-center gap-2"></div>
            </div>
          </div>

          <div class="grid grid-cols-1 lg:grid-cols-[180px_minmax(0,1fr)_210px] gap-0">
            <aside class="border-b lg:border-b-0 lg:border-l border-slate-800 p-4 space-y-4 bg-[#09111f]">
              <div>
                <div id="calMiniMonthTitle" class="text-xs font-black text-white mb-2"></div>
                <div id="calMiniMonth" class="grid grid-cols-7 gap-1"></div>
              </div>
              <div class="p-3 rounded-2xl bg-slate-950 border border-slate-800">
                <div class="text-[9px] text-slate-500 font-black mb-2">NEXT REMINDER</div>
                <div id="calNextReminder" class="text-[10px] text-slate-200 font-bold leading-relaxed">—</div>
              </div>
              <div class="p-3 rounded-2xl bg-slate-950 border border-slate-800 text-[10px] text-slate-400 leading-relaxed">
                ابعت الموعد للبوت على Telegram، أو ابعت PDF فيه جدول، والمواعيد هتدخل هنا تلقائيًا.
              </div>
            </aside>

            <section class="min-w-0 bg-slate-950">
              <div class="overflow-x-auto no-scrollbar">
                <div class="min-w-[760px]">
                  <div id="calHeaderRow" class="flex flex-row-reverse border-b border-slate-800 bg-slate-950 sticky top-0 z-10"></div>
                  <div id="calAllDayRow" class="flex flex-row-reverse border-b border-slate-800 bg-slate-900/70 min-h-[2.3rem]"></div>
                  <div class="max-h-[520px] overflow-y-auto no-scrollbar">
                    <div id="calGridBody" class="flex flex-row-reverse relative"></div>
                  </div>
                </div>
              </div>
            </section>

            <aside class="border-t lg:border-t-0 lg:border-r border-slate-800 p-4 bg-[#09111f]">
              <div class="text-[9px] text-slate-500 font-black tracking-widest mb-3">AGENDA</div>
              <div id="calAgendaList" class="space-y-2"></div>
            </aside>
          </div>
        </div>
      </div>

      <div id="eventModal" class="fixed inset-0 bg-slate-950/60 backdrop-blur-sm z-50 hidden items-center justify-center p-4">
        <div class="max-w-sm w-full bg-white border border-amber-200 p-6 rounded-3xl shadow-2xl space-y-4">
          <div class="flex flex-wrap items-center justify-between gap-3">
            <h3 class="text-sm font-black text-slate-900" data-t="event_modal_title">إضافة حدث جديد</h3>
            <button onclick="closeEventModal()" class="w-7 h-7 flex items-center justify-center rounded-full hover:bg-slate-100 text-slate-500 font-bold">✕</button>
          </div>
          <div class="space-y-3">
            <input type="text" id="newTaskTitle" placeholder="عنوان الحدث" class="w-full bg-slate-50 border border-slate-200 rounded-xl p-3 text-xs text-slate-800 focus:outline-none focus:ring-2 focus:ring-amber-400">
            <div class="grid grid-cols-2 gap-2">
              <input type="date" id="newTaskDate" class="w-full bg-slate-50 border border-slate-200 rounded-xl p-3 text-xs text-slate-700 focus:outline-none focus:ring-2 focus:ring-amber-400">
              <input type="time" id="newTaskTime" class="w-full bg-slate-50 border border-slate-200 rounded-xl p-3 text-xs text-slate-700 focus:outline-none focus:ring-2 focus:ring-amber-400">
            </div>
            <button onclick="addNewScheduleTask()" class="w-full py-2.5 bg-slate-900 hover:bg-slate-800 text-amber-300 font-black text-xs rounded-xl shadow-sm transition" data-t="btn_add">إضافة</button>
          </div>
        </div>
      </div>

      <div id="tab-leaderboard" class="tab-content hidden space-y-6">
        <div class="banana-card p-6 rounded-3xl space-y-4">
          <h2 class="text-base font-black text-slate-900" data-t="nav_ranks">المتصدرين</h2>
          <div id="leaderboardList" class="space-y-2 pt-2"></div>
        </div>
      </div>

      <div id="tab-account" class="tab-content hidden space-y-6">
        <div class="banana-card p-6 rounded-3xl space-y-5">
          <div class="flex items-center gap-4">
            <div id="accountInitial" class="w-14 h-14 rounded-2xl bg-amber-400 text-slate-900 flex items-center justify-center text-xl font-black">?</div>
            <div>
              <h3 id="accountName" class="text-base font-black text-slate-900">...</h3>
              <p id="accountField" class="text-xs text-slate-500 font-medium">...</p>
            </div>
          </div>
          <div class="grid grid-cols-3 gap-2 text-center pt-3 border-t border-slate-100">
            <div class="p-3 bg-amber-50/60 rounded-2xl">
              <span class="text-[10px] font-bold text-slate-400 block" data-t="metric_xp">النقاط</span>
              <span id="accountXP" class="text-base font-black text-amber-600 font-mono">0</span>
            </div>
            <div class="p-3 bg-amber-50/60 rounded-2xl">
              <span class="text-[10px] font-bold text-slate-400 block" data-t="streak_label">الأيام</span>
              <span id="accountStreak" class="text-base font-black text-slate-800 font-mono">0</span>
            </div>
            <div class="p-3 bg-amber-50/60 rounded-2xl">
              <span class="text-[10px] font-bold text-slate-400 block" data-t="passed_label">المجتاز</span>
              <span id="accountTests" class="text-base font-black text-emerald-600 font-mono">0</span>
            </div>
          </div>
          <div class="p-4 bg-amber-50/60 rounded-2xl border border-amber-200 space-y-2">
            <span class="text-[10px] font-bold text-slate-500 block">معرّفك الدائم (Permanent ID) — استخدمه مع كلمة المرور في تطبيق Companion</span>
            <div class="flex items-center gap-2">
              <code id="accountPublicId" dir="ltr" class="flex-1 text-sm font-black text-slate-900 select-all">...</code>
              <button type="button" onclick="copyPublicId()" class="px-3 py-1.5 bg-amber-400 text-slate-900 font-black text-xs rounded-lg">نسخ</button>
            </div>
            <button type="button" onclick="openPasswordChange()" class="text-xs font-bold text-slate-600 underline">تغيير كلمة المرور</button>
          </div>
          <div class="p-4 bg-slate-50 rounded-2xl border border-slate-200 space-y-3">
            <span class="text-sm font-black text-slate-800 block">📲 حمّل تطبيق S-TESSERACT Companion</span>
            <div class="grid grid-cols-2 gap-2">
              <a id="dlWindows" href="#" class="hidden py-3 text-center bg-slate-900 text-white font-bold rounded-xl text-xs">🖥️ Windows<span id="dlWindowsInfo" class="block text-[10px] font-normal opacity-70"></span></a>
              <a id="dlAndroid" href="#" class="hidden py-3 text-center bg-emerald-600 text-white font-bold rounded-xl text-xs">🤖 Android (APK)<span id="dlAndroidInfo" class="block text-[10px] font-normal opacity-70"></span></a>
            </div>
            <p id="dlEmpty" class="text-xs text-slate-500">التطبيق لسه مش متاح للتنزيل. هيظهر هنا أول ما يتم رفعه.</p>
          </div>
          <button onclick="logout()" class="w-full py-3 bg-slate-100 text-slate-700 font-bold rounded-xl text-xs" data-t="btn_logout">تسجيل الخروج</button>
        </div>
      </div>


      <div id="tab-history" class="tab-content hidden space-y-6">
        <div class="banana-card p-5 md:p-7 rounded-3xl space-y-5">
          <div class="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 class="text-lg md:text-2xl font-black text-slate-900">🕘 <span data-t="nav_history">السجل / History</span></h2>
              <p class="text-xs text-slate-500 mt-1">كل موادك، فيديوهاتك، الاختبارات ومحادثات الـAI Tutor محفوظة هنا.</p>
            </div>
            <button onclick="loadHistoryPage()" class="px-3 py-2 rounded-xl bg-slate-900 text-amber-300 text-xs font-black">Refresh</button>
          </div>
          <div class="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <section class="p-4 rounded-2xl border bg-white space-y-3">
              <div class="flex items-center justify-between"><h3 class="font-black">📚 Materials</h3><span class="text-[10px] text-slate-400">PDF uploads</span></div>
              <div id="historyMaterialsBox" class="space-y-2 text-xs"></div>
            </section>
            <section class="p-4 rounded-2xl border bg-white space-y-3">
              <div class="flex items-center justify-between"><h3 class="font-black">🎬 Videos</h3><span class="text-[10px] text-slate-400">Video jobs</span></div>
              <div id="historyVideosBox" class="space-y-2 text-xs"></div>
            </section>
            <section class="p-4 rounded-2xl border bg-white space-y-3">
              <div class="flex items-center justify-between"><h3 class="font-black">📝 Assessments</h3><span class="text-[10px] text-slate-400">Exams & checks</span></div>
              <div id="historyAssessmentsBox" class="space-y-2 text-xs"></div>
            </section>
            <section class="p-4 rounded-2xl border bg-white space-y-3">
              <div class="flex items-center justify-between"><h3 class="font-black">🧭 Learning Roadmaps</h3><span class="text-[10px] text-slate-400">Generated paths</span></div>
              <div id="historyRoadmapsBox" class="space-y-2 text-xs"></div>
            </section>
            <section class="p-4 rounded-2xl border bg-white space-y-3">
              <div class="flex items-center justify-between"><h3 class="font-black">📝 Generated Exams</h3><span class="text-[10px] text-slate-400">Exam Studio</span></div>
              <div id="historyGeneratedExamsBox" class="space-y-2 text-xs"></div>
            </section>
            <section class="p-4 rounded-2xl border bg-white space-y-3">
              <div class="flex items-center justify-between"><h3 class="font-black">🤖 Tutor Chats</h3><span class="text-[10px] text-slate-400">AI conversations</span></div>
              <div id="historyTutorBox" class="space-y-2 text-xs"></div>
              <div id="historyTutorConversationBox" class="hidden max-h-80 overflow-auto space-y-2 border-t pt-3"></div>
            </section>
          </div>
        </div>
      </div>

      <div id="tab-studycenter" class="tab-content hidden space-y-5">
        <div class="banana-card p-5 md:p-7 rounded-3xl space-y-5">
          <div class="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 class="text-lg md:text-2xl font-black text-slate-900">🧠 AI Study Center</h2>
              <p class="text-xs text-slate-500 mt-1">PDF → notes → flashcards → exam → grading → weakness analysis → review</p>
            </div>
            <button onclick="loadStudyProgress()" class="px-3 py-2 rounded-xl bg-slate-900 text-amber-300 text-xs font-black">Refresh</button>
          </div>

          <div class="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <div class="p-4 rounded-2xl border border-amber-200 bg-amber-50/40 space-y-3">
              <h3 class="font-black text-slate-900">1. Build a Study Pack</h3>
              <input id="studyMaterialTitle" class="w-full p-3 rounded-xl border border-slate-200" placeholder="Material title">
              <input id="studyPdfFile" type="file" accept="application/pdf" class="w-full p-3 rounded-xl border border-slate-200 bg-white">
              <div class="grid grid-cols-2 gap-2">
                <select id="studyVideoMode" class="p-3 rounded-xl border border-slate-200">
                  <option value="generated">Generated Video</option>
                  <option value="youtube">YouTube</option>
                  <option value="both">Both</option>
                </select>
                <select id="studyVideoLanguage" class="p-3 rounded-xl border border-slate-200">
                  <option value="ar">🇪🇬 Arabic</option>
                  <option value="en">🇬🇧 English</option>
                </select>
              </div>
              <div class="grid grid-cols-2 gap-2">
                <select id="studyAssessmentMode" class="p-3 rounded-xl border border-slate-200">
                  <option value="understanding">Understanding Check</option>
                  <option value="full_exam">Full Exam</option>
                </select>
                <input id="studyQuestionCount" type="number" min="5" max="50" value="5" class="p-3 rounded-xl border border-slate-200">
              </div>
              <div class="grid grid-cols-2 sm:grid-cols-3 gap-2 text-xs">
                <label class="p-2 bg-white rounded-xl border"><input type="checkbox" class="study-qtype" value="mcq" checked> MCQ</label>
                <label class="p-2 bg-white rounded-xl border"><input type="checkbox" class="study-qtype" value="true_false" checked> True/False</label>
                <label class="p-2 bg-white rounded-xl border"><input type="checkbox" class="study-qtype" value="short_answer" checked> Short Answer</label>
                <label class="p-2 bg-white rounded-xl border"><input type="checkbox" class="study-qtype" value="word_problem" checked> Word Problem</label>
                <label class="p-2 bg-white rounded-xl border"><input type="checkbox" class="study-qtype" value="multi_step" checked> Multi-step</label>
              </div>
              <button onclick="buildStudyPack()" class="w-full py-3.5 rounded-xl bg-slate-950 text-amber-300 font-black">Create Everything 🚀</button>
              <div id="studyPackStatus" class="text-xs text-slate-500"></div>
            </div>

            <div class="p-4 rounded-2xl border border-slate-200 bg-white space-y-3">
              <h3 class="font-black text-slate-900">2. Progress & Weaknesses</h3>
              <div id="studyProgressBox" class="grid grid-cols-2 gap-2 text-center"></div>
              <div id="studyWeaknessBox" class="space-y-2 text-xs"></div>
              <div id="studyNotificationBox" class="space-y-2 text-xs"></div>
            </div>
          </div>

          <div class="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <div class="p-4 rounded-2xl border border-slate-200 bg-white space-y-3">
              <h3 class="font-black">3. Tutor</h3>
              <select id="tutorMode" class="w-full p-3 rounded-xl border"><option value="source">Source-based (PDF only)</option><option value="general">General AI</option></select>
              <select id="tutorStrategy" class="w-full p-3 rounded-xl border"><option value="normal">Normal</option><option value="simpler">Simpler</option><option value="analogy">Use an analogy</option><option value="step_by_step">Step by step</option><option value="diagnostic">I don't understand — diagnose me</option></select>
              <textarea id="tutorQuestion" rows="3" class="w-full p-3 rounded-xl border" placeholder="Ask the tutor..."></textarea>
              <div class="grid grid-cols-2 gap-2"><button onclick="askStudyTutor()" class="w-full py-3 rounded-xl bg-slate-900 text-white font-black">Ask Tutor</button><button onclick="startNewTutorChat()" class="w-full py-3 rounded-xl bg-slate-100 text-slate-700 font-black">New Chat</button></div>
              <div id="tutorSessionLabel" class="text-[10px] text-slate-400"></div>
              <pre id="tutorAnswer" class="whitespace-pre-wrap text-xs bg-slate-50 p-3 rounded-xl min-h-24"></pre>
            </div>

            <div class="p-4 rounded-2xl border border-slate-200 bg-white space-y-3">
              <h3 class="font-black">4. Search in Material</h3>
              <div class="flex gap-2">
                <input id="materialSearch" class="flex-1 p-3 rounded-xl border" placeholder="Search exact word/phrase">
                <button onclick="searchStudyMaterial()" class="px-4 rounded-xl bg-slate-900 text-white font-black">Search</button>
              </div>
              <div id="materialSearchResults" class="space-y-2 text-xs"></div>
              <h3 class="font-black pt-2">Goal → Learning Plan</h3>
              <div class="flex gap-2">
                <input id="studyGoal" class="flex-1 p-3 rounded-xl border" placeholder="e.g. Learn ML Engineering">
                <button onclick="generateStudyGoalPlan()" class="px-4 rounded-xl bg-amber-400 font-black">Plan</button>
              </div>
              <pre id="studyGoalPlan" class="whitespace-pre-wrap text-xs bg-slate-50 p-3 rounded-xl max-h-72 overflow-auto"></pre>
            </div>
          </div>

          <div class="p-4 rounded-2xl border border-slate-200 bg-white space-y-3">
            <div class="flex flex-wrap items-center justify-between gap-2">
              <h3 class="font-black">5. Assessment / Full Exam</h3>
              <button onclick="loadLatestAssessment()" class="px-3 py-2 rounded-xl bg-slate-900 text-amber-300 text-xs font-black">Open Latest</button>
            </div>
            <div id="assessmentArea" class="space-y-4"></div>
          </div>

          <div class="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <div class="p-4 rounded-2xl border border-slate-200 bg-white space-y-3"><div class="flex items-center justify-between gap-2"><h3 class="font-black">6. Video History</h3><button onclick="loadVideoHistory()" class="px-3 py-2 rounded-xl bg-slate-900 text-amber-300 text-xs font-black">Refresh</button></div><div id="videoHistoryBox" class="space-y-2 text-xs"></div></div>
            <div class="p-4 rounded-2xl border border-slate-200 bg-white space-y-3"><div class="flex items-center justify-between gap-2"><h3 class="font-black">7. Tutor Chat History</h3><button onclick="loadTutorHistory()" class="px-3 py-2 rounded-xl bg-slate-900 text-amber-300 text-xs font-black">Refresh</button></div><div id="tutorHistoryBox" class="space-y-2 text-xs"></div><div id="tutorConversationBox" class="hidden max-h-80 overflow-auto space-y-2 border-t pt-3"></div></div>
          </div>

          <div class="p-4 rounded-2xl border border-slate-200 bg-white space-y-3">
            <h3 class="font-black">6. Smart Review</h3>
            <div id="reviewArea" class="space-y-3"></div>
            <button onclick="loadSmartReview()" class="px-4 py-2 rounded-xl bg-amber-400 font-black text-xs">Load Review</button>
          </div>
        </div>
      </div>

    </main>
  </div>

  <script>
    // Recovery guard: mobile browsers can restore a stale DOM with the drawer
    // backdrop still active. Reset it on normal load and BFCache pageshow before
    // the app's main initialization so it cannot intercept every tap.
    (function () {
      function resetStaleMobileOverlay() {
        if (!document.body) return;
        document.body.classList.remove("mobile-sidebar-open", "overflow-hidden");
        const backdrop = document.getElementById("mobileSidebarBackdrop");
        const sidebar = document.getElementById("mobileSidebar");
        const button = document.getElementById("mobileMenuButton");
        if (backdrop) {
          backdrop.classList.add("hidden");
          backdrop.style.setProperty("display", "none", "important");
          backdrop.style.setProperty("visibility", "hidden", "important");
          backdrop.style.setProperty("opacity", "0", "important");
          backdrop.style.setProperty("pointer-events", "none", "important");
        }
        if (sidebar) {
          sidebar.classList.add("translate-x-full");
          sidebar.setAttribute("aria-hidden", "true");
        }
        if (button) button.setAttribute("aria-expanded", "false");
      }
      document.addEventListener("DOMContentLoaded", resetStaleMobileOverlay, { once: true });
      window.addEventListener("pageshow", resetStaleMobileOverlay);
    })();
  </script>
  <script>
    let currentLang = localStorage.getItem("grit_lang") || "ar";

    const translations = {
      ar: {
        auth_title: "S-Tesseract",
        label_username: "الاسم",
        label_field: "المجال أو التخصص",
        btn_enter: "دخول ←",
        btn_abort: "انسحاب",
        quiz_footer_hint: "أجب بدقة لتأكيد إتمام المرحلة وفتح ما بعدها",
        nav_studyroom: "غرفة المذاكرة",
        nav_home: "الرئيسية",
        nav_video: "إتقان الفكرة & مشاكل الواقع",
        nav_gps: "خارطة التعلم (Roadmap)",
        nav_schedule: "الجدول",
        nav_ranks: "المتصدرين",
        nav_profile: "الملف الشخصي",
        nav_history: "السجل / History",
        label_lang: "اللغة",
        studyroom_title: "غرفة المذاكرة",
        btn_isolation: "وضع العزل والشاشة الكاملة",
        timer_label: "مؤقت التركيز",
        btn_start_timer: "بدء الجلسة ▶",
        btn_reset_timer: "إعادة ضبط",
        todo_header: "قائمة المهام",
        btn_add: "إضافة",
        evaluator_title: "المصحح الذكي",
        label_question: "نص السؤال أو المسألة:",
        label_answer: "الحل (اختياري للتقييم):",
        btn_evaluate: "إرسال للتقييم أو الحل",
        feedback_label: "التقييم والملاحظات:",
        solution_label: "الحل النموذجي:",
        hero_title: "منصة S-Tesseract للتعلم والتركيز",
        btn_enter_room: "غرفة المذاكرة ←",
        metric_xp: "إجمالي النقاط",
        metric_gps: "التقدم",
        metric_accuracy: "الدقة",
        video_title: "إتقان الفكرة & مشاكل الواقع الهندسية",
        btn_generate_video: "إنشاء الفيديو السينمائي للـPDF",
        btn_quiz: "دخول كويز نمط الشيت الأصلي (+50 XP) ←",
        gps_title: "خارطة التعلم الذكية (Roadmap Generator)",
        label_field_goal: "التخصص أو المادة التي تريد دراستها:",
        streak_label: "الأيام",
        passed_label: "المجتاز",
        btn_logout: "تسجيل الخروج",
        yt_heading: "بحث إضافي حول هذه الفكرة:",
        yt_open_link: "فتح نتائج البحث في YouTube ↗",
        cal_today: "اليوم",
        event_modal_title: "إضافة حدث جديد"
      },
      en: {
        auth_title: "S-Tesseract",
        label_username: "Username",
        label_field: "Field or Track",
        btn_enter: "Enter →",
        btn_abort: "Abort",
        quiz_footer_hint: "Answer accurately to verify this milestone and unlock next",
        nav_studyroom: "Study Room",
        nav_home: "Home",
        nav_video: "Concept Mastery & Case Study",
        nav_gps: "Learning Roadmap",
        nav_schedule: "Schedule",
        nav_ranks: "Leaderboard",
        nav_profile: "Profile",
        nav_history: "History",
        label_lang: "Language",
        studyroom_title: "Study Room",
        btn_isolation: "Fullscreen Isolation Mode",
        timer_label: "Focus Timer",
        btn_start_timer: "Start Session ▶",
        btn_reset_timer: "Reset",
        todo_header: "Tasks",
        btn_add: "Add",
        evaluator_title: "Smart Evaluator",
        label_question: "Problem or Question:",
        label_answer: "Your Solution (Optional):",
        btn_evaluate: "Submit to Evaluate or Solve",
        feedback_label: "Evaluation & Feedback:",
        solution_label: "Model Solution:",
        hero_title: "S-Tesseract Learning & Focus Workspace",
        btn_enter_room: "Study Room ←",
        metric_xp: "Total XP",
        metric_gps: "Progress",
        metric_accuracy: "Accuracy",
        video_title: "Concept Mastery & Engineering Case Study",
        btn_generate_video: "Generate Cinematic PDF Lesson",
        btn_quiz: "Start Sheet-Style Quiz (+50 XP) ←",
        gps_title: "Smart Roadmap Generator",
        label_field_goal: "Topic or Field You Want to Learn:",
        streak_label: "Streak",
        passed_label: "Passed",
        btn_logout: "Log Out",
        yt_heading: "Additional Search on This Concept:",
        yt_open_link: "Open search results in YouTube ↗",
        cal_today: "Today",
        event_modal_title: "Add New Event"
      }
    };

    function applyLanguage(lang) {
      currentLang = lang;
      localStorage.setItem("grit_lang", lang);

      const dict = translations[lang];
      document.querySelectorAll("[data-t]").forEach(el => {
        const key = el.getAttribute("data-t");
        if (dict[key]) el.innerText = dict[key];
      });

      document.getElementById("langBtnText").innerText = lang === "ar" ? "English" : "العربية";
      document.getElementById("mobileLangBtnText").innerText = lang === "ar" ? "EN" : "AR";
      
      const content = document.getElementById("mainContentArea");
      if (lang === "en") {
        content.classList.remove("text-right");
        content.classList.add("text-left");
      } else {
        content.classList.remove("text-left");
        content.classList.add("text-right");
      }
    }

    function toggleLanguage() {
      applyLanguage(currentLang === "ar" ? "en" : "ar");
    }

    let currentUserId = null;
    let isExamActive = false;
    let currentQuizItems = [];
    let currentQuizReview = [];
    let currentQuizHintUsed = false;
    let currentQuestionIdx = 0;
    let livesRemaining = 3;
    let comboStreak = 0;
    let questionTimer = 15;
    let timerInterval = null;
    let activeBlueprintNodeId = 0;
    let currentCorrectAnswers = 0;
    let currentPassThreshold = 0; // 0 preserves legacy quiz behavior; roadmap assessments set a real threshold.

    let focusMinutes = 25;
    let focusSeconds = 0;
    let focusTimerInterval = null;
    let isFocusRunning = false;
    let isDNDActive = false;

    function setAuthModalVisible(visible) {
      const modal = document.getElementById("authModal");
      if (!modal) return;
      modal.classList.toggle("auth-visible", !!visible);
      modal.classList.toggle("hidden", !visible);
      modal.setAttribute("aria-hidden", visible ? "false" : "true");
      if (!visible) {
        modal.style.removeProperty("display");
        modal.style.removeProperty("visibility");
        modal.style.removeProperty("pointer-events");
        modal.style.removeProperty("opacity");
      }
      document.body.classList.toggle("auth-open", !!visible);
    }

    let sessionGuardInterval = null;
    let notificationPollInterval = null;
    const seenNotificationIds = new Set();
    let notificationReady = false;

    function showAppToast(title, message, kind='info') {
      let host = document.getElementById('stGlobalToastHost');
      if (!host) {
        host = document.createElement('div');
        host.id = 'stGlobalToastHost';
        host.className = 'fixed top-4 left-1/2 -translate-x-1/2 z-[99999] w-[min(92vw,420px)] space-y-2 pointer-events-none';
        document.body.appendChild(host);
      }
      const el = document.createElement('div');
      el.className = 'pointer-events-auto rounded-2xl border border-amber-300/40 bg-slate-950 text-white shadow-2xl px-4 py-3';
      el.innerHTML = `<div class="font-black text-sm">${escapeHtml(title)}</div><div class="text-xs text-slate-300 mt-1">${escapeHtml(message)}</div>`;
      host.appendChild(el);
      setTimeout(()=>{ el.style.opacity='0'; el.style.transform='translateY(-6px)'; el.style.transition='all .25s'; setTimeout(()=>el.remove(),300); }, 6500);
    }

    async function requestBrowserNotifications() {
      try {
        if ('Notification' in window && Notification.permission === 'default') await Notification.requestPermission();
      } catch(e) {}
    }

    function showBrowserNotification(title, message) {
      try {
        if ('Notification' in window && Notification.permission === 'granted') {
          new Notification(title, { body: message, icon: '/favicon.ico', tag: 'stesseract-'+Date.now() });
        }
      } catch(e) {}
    }

    async function forceSessionExit(message) {
      if (sessionGuardInterval) { clearInterval(sessionGuardInterval); sessionGuardInterval = null; }
      if (notificationPollInterval) { clearInterval(notificationPollInterval); notificationPollInterval = null; }
      currentUserId = null;
      localStorage.removeItem('grit_user_id');
      localStorage.removeItem('grit_username');
      localStorage.removeItem('grit_field');
      showAppToast('تم إنهاء الجلسة', message || 'تم تسجيل خروجك من البرنامج.', 'error');
      setAuthModalVisible(true);
    }

    function startSessionGuard() {
      if (sessionGuardInterval) clearInterval(sessionGuardInterval);
      sessionGuardInterval = setInterval(async () => {
        if (!currentUserId) return;
        try {
          const r = await fetch('/api/auth/session', { credentials:'same-origin', cache:'no-store' });
          if (r.ok) return;
          let d={}; try { d=await r.json(); } catch(e) {}
          if (r.status===401 || r.status===403) {
            await forceSessionExit(d.detail || 'تم تسجيل خروجك بواسطة الإدارة.');
          }
        } catch(e) {
          // Temporary network errors should not log the user out.
        }
      }, 2500);
    }

    async function pollAppNotifications() {
      if (!currentUserId) return;
      try {
        const r = await fetch(`/api/study/notifications?user_id=${encodeURIComponent(currentUserId)}`, { credentials:'same-origin', cache:'no-store' });
        if (!r.ok) return;
        const d = await r.json();
        const items = d.notifications || [];
        if (!notificationReady) {
          items.forEach(x => { if (x.id != null) seenNotificationIds.add(String(x.id)); });
          notificationReady = true;
          return;
        }
        for (const x of items) {
          if (x.id == null) continue;
          const id=String(x.id);
          if (seenNotificationIds.has(id)) continue;
          seenNotificationIds.add(id);
          showAppToast(x.title || 'إشعار', x.message || '');
          showBrowserNotification(x.title || 'S-Tesseract', x.message || '');
        }
      } catch(e) {}
    }

    function startNotificationPolling() {
      if (notificationPollInterval) clearInterval(notificationPollInterval);
      notificationReady = false;
      seenNotificationIds.clear();
      pollAppNotifications();
      notificationPollInterval = setInterval(pollAppNotifications, 4000);
    }

    async function restoreUserSession() {
      try {
        const res = await fetch("/api/auth/session", { credentials: "same-origin", cache: "no-store" });
        if (res.ok) {
          const user = await res.json();
          currentUserId = user.id;
          if (user.password_set === false) { showPasswordSetup(user.public_user_id); }
          localStorage.setItem("grit_user_id", String(user.id));
          localStorage.setItem("grit_username", user.username || "");
          localStorage.setItem("grit_field", user.field_of_study || "");
          setAuthModalVisible(false);
          await loadDashboardData();
          switchTab("home");
          refreshTelegramControls();
          startSessionGuard();
          startNotificationPolling();
          return true;
        }
      } catch (e) {
        console.warn("Session restore failed:", e);
      }

      // Cookie may be missing after a browser/proxy restore. Re-establish the
      // server-side session from the locally remembered account name instead of
      // forcing the user through the login screen on every refresh.
      const savedName = (localStorage.getItem("grit_username") || "").trim();
      const savedField = (localStorage.getItem("grit_field") || "").trim();
      if (savedName) {
        try {
          const reauth = await fetch("/api/auth/register-or-login", {
            method:"POST", headers:{"Content-Type":"application/json"},
            credentials:"same-origin", body:JSON.stringify({username:savedName,field:savedField})
          });
          const data = await reauth.json().catch(()=>({}));
          if (reauth.ok && data.id) {
            currentUserId=data.id;
            if (data.password_set === false) { showPasswordSetup(data.public_user_id); }
            localStorage.setItem("grit_user_id",String(data.id));
            localStorage.setItem("grit_username",data.username||savedName);
            localStorage.setItem("grit_field",data.field_of_study||savedField);
            setAuthModalVisible(false);
            await loadDashboardData();
            switchTab("home");
            refreshTelegramControls();
            startSessionGuard();
            startNotificationPolling();
            return true;
          }
        } catch(e) {
          console.warn("Automatic session restore failed:",e);
        }
      }

      // No account is required from the visitor. Create a browser-scoped guest
      // in the existing user/session tables so user-scoped features receive a
      // real database ID instead of null/0. Registered-account auth remains intact.
      try {
        const guestRes = await fetch("/api/auth/guest", {
          method: "POST", credentials: "same-origin", cache: "no-store"
        });
        const guest = await guestRes.json().catch(() => ({}));
        if (guestRes.ok && guest.id) {
          currentUserId = Number(guest.id);
          localStorage.setItem("grit_user_id", String(guest.id));
          localStorage.setItem("grit_username", guest.username || "Guest");
          localStorage.setItem("grit_field", guest.field_of_study || "General");
          setAuthModalVisible(false);
          await loadDashboardData();
          switchTab("home");
          refreshTelegramControls();
          startSessionGuard();
          startNotificationPolling();
          return true;
        }
        console.error("Guest session creation failed", guestRes.status, guest);
      } catch (guestError) {
        console.error("Guest session creation failed", guestError);
      }
      currentUserId = null;
      localStorage.removeItem("grit_user_id");
      setAuthModalVisible(true);
      return false;
    }

    window.addEventListener("DOMContentLoaded", async () => {
      applyLanguage(currentLang);
      // Browser notifications are optional; the in-app toast always works.
      requestBrowserNotifications();

      // HARD RESET MOBILE NAVIGATION ON EVERY PAGE LOAD.
      // This prevents a stale body class or browser-restored DOM state from
      // leaving the sidebar backdrop covering/blurring the whole page.
      document.body.classList.remove("mobile-sidebar-open", "overflow-hidden");
      const startupSidebar = document.getElementById("mobileSidebar");
      const startupBackdrop = document.getElementById("mobileSidebarBackdrop");
      const startupMenuButton = document.getElementById("mobileMenuButton");
      if (startupSidebar) {
        startupSidebar.classList.add("translate-x-full");
        startupSidebar.setAttribute("aria-hidden", "true");
      }
      if (startupBackdrop) {
        startupBackdrop.classList.add("hidden");
        startupBackdrop.style.setProperty("display", "none", "important");
        startupBackdrop.style.setProperty("visibility", "hidden", "important");
        startupBackdrop.style.setProperty("opacity", "0", "important");
        startupBackdrop.style.setProperty("pointer-events", "none", "important");
      }
      if (startupMenuButton) {
        startupMenuButton.setAttribute("aria-expanded", "false");
      }

      // Hard-reset all startup overlays. This prevents a stale mobile browser
      // state or a previous exam/modal state from leaving the whole page dimmed.
      setAuthModalVisible(false);
      const lockdown = document.getElementById("lockdownOverlay");
      if (lockdown) {
        lockdown.classList.add("hidden");
        lockdown.classList.remove("flex", "overlay-visible");
        lockdown.style.removeProperty("display");
        lockdown.style.removeProperty("visibility");
        lockdown.style.removeProperty("pointer-events");
        lockdown.style.removeProperty("opacity");
      }
      const eventModal = document.getElementById("eventModal");
      if (eventModal) {
        eventModal.classList.add("hidden");
        eventModal.classList.remove("flex", "overlay-visible");
        eventModal.style.removeProperty("display");
        eventModal.style.removeProperty("visibility");
        eventModal.style.removeProperty("pointer-events");
        eventModal.style.removeProperty("opacity");
      }

      // Canonical startup state: Home is always the first application tab.
      // Session restoration may populate data, but it must never choose the Roadmap.
      switchTab("home");
      await restoreUserSession();
      switchTab("home");
    });

    function showAuthError(msg) {
      const box = document.getElementById("authError");
      if (!box) return;
      box.textContent = msg || "";
      box.classList.toggle("hidden", !msg);
    }

    function authFailureMessage(status, data) {
      const detail = data && typeof data.detail === "string" ? data.detail : "";
      if (status === 401 || status === 403) return detail || "الاسم أو كلمة المرور غير صحيحة.";
      if (status === 429) return detail || "محاولات كتير. استنى شوية وجرّب تاني.";
      if (status === 400 || status === 422) return detail || "البيانات غير مكتملة أو كلمة المرور ضعيفة (8 حروف على الأقل).";
      if (status >= 500) return "السيرفر فيه مشكلة دلوقتي (رمز " + status + "). جرّب تاني بعد دقيقة.";
      return detail || ("فشل تسجيل الدخول (رمز " + status + ").");
    }

    async function handleAuthSubmit() {
      const btn = document.getElementById("authBtn");
      const u = document.getElementById("authUsername").value.trim();
      const f = document.getElementById("authField").value.trim();
      const pw = document.getElementById("authPassword").value;
      showAuthError("");
      if (!u) {
        showAuthError(currentLang === "ar" ? "اكتب الاسم أولاً." : "Please enter your name.");
        document.getElementById("authUsername").focus();
        return;
      }

      const originalLabel = btn ? btn.innerHTML : "";
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), window.__AUTH_TIMEOUT_MS || 25000);
      try {
        if (btn) { btn.disabled = true; btn.style.opacity = "0.65"; btn.textContent = currentLang === "ar" ? "جاري الدخول..." : "Signing in..."; }
        const res = await fetch("/api/auth/register-or-login", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "same-origin",
          signal: controller.signal,
          body: JSON.stringify({ username: u, field: f, password: pw })
        });
        let data = {};
        try { data = JSON.parse(await res.text()); } catch (_) { data = {}; }
        if (!res.ok || !data.id) {
          showAuthError(authFailureMessage(res.status, data));
          return;
        }
        currentUserId = data.id;
        localStorage.setItem("grit_user_id", String(data.id));
        localStorage.setItem("grit_username", data.username || u);
        localStorage.setItem("grit_field", data.field_of_study || f);
        document.getElementById("authPassword").value = "";
        setAuthModalVisible(false);
        if (data.password_set === false) { showPasswordSetup(data.public_user_id); }
        await loadDashboardData();
        switchTab("home");
        refreshTelegramControls();
        startSessionGuard();
        startNotificationPolling();
      } catch (e) {
        console.error(e);
        if (e && e.name === "AbortError") showAuthError("السيرفر مش بيرد. لو الموقع كان نايم استنى دقيقة وجرّب تاني.");
        else if (document.getElementById("authModal").classList.contains("auth-visible")) showAuthError("مفيش اتصال بالسيرفر. اتأكد من الإنترنت وجرّب تاني.");
        else alert((currentLang === "ar" ? "حصلت مشكلة بعد تسجيل الدخول: " : "Error after login: ") + e.message);
      } finally {
        clearTimeout(timer);
        if (btn) { btn.disabled = false; btn.style.opacity = "1"; btn.innerHTML = originalLabel; }
      }
    }

    function closePasswordModal() {
      const m = document.getElementById("pwSetupModal");
      m.classList.add("hidden"); m.classList.remove("flex");
    }

    function openPasswordChange() {
      showPasswordSetup(window.__publicUserId || "");
      document.getElementById("pwCurrentInput").classList.remove("hidden");
      document.getElementById("pwSetupCancel").classList.remove("hidden");
    }

    function copyPublicId() {
      const id = window.__publicUserId || "";
      if (id && navigator.clipboard) navigator.clipboard.writeText(id).catch(() => {});
    }

    function fmtMB(n) { return (n / 1048576).toFixed(1) + " MB"; }

    async function loadCompanionDownloads() {
      try {
        const res = await fetch("/api/companion/downloads");
        const d = (await res.json()).downloads || {};
        let any = false;
        for (const [plat, id, info] of [["windows", "dlWindows", "dlWindowsInfo"], ["android", "dlAndroid", "dlAndroidInfo"]]) {
          const el = document.getElementById(id);
          if (d[plat]) {
            el.href = "/downloads/" + encodeURIComponent(d[plat].file);
            el.classList.remove("hidden");
            document.getElementById(info).textContent = (d[plat].version ? "v" + d[plat].version + " · " : "") + fmtMB(d[plat].size);
            any = true;
          } else { el.classList.add("hidden"); }
        }
        document.getElementById("dlEmpty").classList.toggle("hidden", any);
      } catch (e) { console.warn("downloads list failed", e); }
    }

    function showPasswordSetup(publicId) {
      document.getElementById("pwSetupId").textContent = publicId || "";
      const m = document.getElementById("pwSetupModal");
      m.classList.remove("hidden"); m.classList.add("flex");
    }

    async function submitPasswordSetup() {
      const input = document.getElementById("pwSetupInput");
      const msg = document.getElementById("pwSetupMsg");
      msg.textContent = "";
      try {
        const res = await fetch("/api/auth/set-password", {
          method: "POST", headers: { "Content-Type": "application/json" },
          credentials: "same-origin", body: JSON.stringify({ new_password: input.value, current_password: document.getElementById("pwCurrentInput").value })
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : "فشل حفظ كلمة المرور");
        input.value = "";
        document.getElementById("pwCurrentInput").value = "";
        document.getElementById("pwCurrentInput").classList.add("hidden");
        document.getElementById("pwSetupCancel").classList.add("hidden");
        window.__passwordSet = true;
        closePasswordModal();
      } catch (e) { msg.textContent = e.message; }
    }

    async function logout() {
      try {
        await fetch("/api/auth/logout", { method: "POST", credentials: "same-origin" });
      } catch (e) {}
      currentUserId = null;
      localStorage.removeItem("grit_user_id");
      localStorage.removeItem("grit_username");
      localStorage.removeItem("grit_field");
      location.reload();
    }

    async function loadDashboardData() {
      if (!currentUserId) return;
      try {
        const res = await fetch("/api/user/" + currentUserId + "/dashboard", {credentials:"same-origin", cache:"no-store"});
        if (res.status === 404) {
          currentUserId = null;
          localStorage.removeItem("grit_user_id");
          setAuthModalVisible(true);
          return;
        }
        if (!res.ok) {
          throw new Error("Dashboard request failed: " + res.status);
        }
        const data = await res.json();
        const u = data.user;

        document.getElementById('topBarUser').innerText = u.username;
        document.getElementById('topBarRoleModel').innerText = u.role_model || u.field_of_study || "";
        document.getElementById('uiUserStreak').innerText = u.streak;
        document.getElementById('uiUserXP').innerText = u.xp;
        document.getElementById('uiGpsProgress').innerText = data.gps_progress + "%";
        document.getElementById('uiHonestyRate').innerText = data.honesty_ratio + "%";

        document.getElementById('accountName').innerText = u.username;
        document.getElementById('accountField').innerText = u.field_of_study || "";
        document.getElementById('accountInitial').innerText = u.username.charAt(0).toUpperCase();
        document.getElementById('accountXP').innerText = u.xp;
        document.getElementById('accountStreak').innerText = u.streak;
        document.getElementById('accountTests').innerText = u.tests_passed;
        document.getElementById('accountPublicId').innerText = u.public_user_id || "";
        window.__publicUserId = u.public_user_id || "";
        window.__passwordSet = u.password_set !== false;
        loadCompanionDownloads();

        renderTodos(data.todos || []);

        renderRoadmapCards(data.blueprint || []);

        const lbBox = document.getElementById('leaderboardList');
        lbBox.innerHTML = (data.leaderboard || []).map((item, idx) => `
          <div class="flex justify-between items-center p-3 rounded-2xl ${item.username === u.username ? 'bg-amber-100/70 border border-amber-300' : 'bg-white border border-slate-100'}">
            <span class="text-xs font-bold text-slate-800">${idx + 1}. ${item.username}</span>
            <span class="font-mono font-extrabold text-xs text-amber-800">${item.xp} XP</span>
          </div>
        `).join('');

      } catch (err) {
        console.error(err);
      }
    }

    async function loadLeaderboard(){
      const box=document.getElementById('leaderboardList'); if(!box||!currentUserId)return;
      try{const data=await readApiResponse(await fetch('/api/leaderboard',{cache:'no-store'})); const rows=data.leaderboard||[]; box.innerHTML=rows.map((item,idx)=>`<div class="flex justify-between items-center p-3 rounded-2xl ${item.username===localStorage.getItem('grit_username')?'bg-amber-100/70 border border-amber-300':'bg-white border border-slate-100'}"><span class="text-xs font-bold text-slate-800">${idx+1}. ${escapeHtml(item.username)}</span><span class="font-mono font-extrabold text-xs text-amber-800">${Number(item.xp)||0} XP</span></div>`).join('')||'<div class="p-4 rounded-xl bg-slate-50 text-slate-400 text-sm">No registered students yet.</div>';}catch(e){box.innerHTML=`<div class="text-rose-500 text-xs">${escapeHtml(e.message)}</div>`;}
    }

    function lsSetOutput(text) {
      const panel=document.getElementById('lsOutputPanel'); const out=document.getElementById('lsOutput');
      if(panel) panel.classList.remove('hidden'); if(out) out.textContent=String(text||'');
    }
    function lsToggleEditor(){
      const d=(document.getElementById('lsDomain')?.value||'other');
      const isCode=d==='programming';
      document.getElementById('lsCodeEditorWrap')?.classList.toggle('hidden',!isCode);
      document.getElementById('lsAnswerWrap')?.classList.toggle('hidden',isCode);
      document.getElementById('lsRunCodeBtn')?.classList.toggle('hidden',!isCode);
      if(isCode && lsCurrentRoadmapNodeId){
        const node=(window.__activeRoadmapNodes||[]).find(n=>Number(n.id)===Number(lsCurrentRoadmapNodeId));
        const plan=node?.learning_plan||{}; const inferred=String(plan.programmingLanguage||'')||inferRoadmapCodeLanguage(plan.trackGoal||'');
        if(['python','cpp','javascript','java','c'].includes(inferred)) document.getElementById('lsCodeLanguage').value=inferred;
      }
    }
    function lsPayload(type, content, result={}) {
      return {domain:document.getElementById('lsDomain')?.value||'general',activity_type:type,title:document.getElementById('lsWorkTitle')?.value.trim()||document.getElementById('lsTopic')?.value.trim()||'Study work',content:content||'',language:document.getElementById('lsCodeLanguage')?.value||'',result,roadmap_node_id:lsCurrentRoadmapNodeId};
    }
    async function lsGenerateActivity(){
      const topic=document.getElementById('lsTopic')?.value.trim(); if(!topic)return alert('اكتب الموضوع الأول.');
      const btn=document.getElementById('lsGenerateBtn'); const original=btn.innerText; btn.disabled=true; btn.innerText='جاري تصميم نشاط متخصص...';
      try{
        const res=await readApiResponse(await fetch('/api/learning-studio/design-activity',{method:'POST',headers:{'Content-Type':'application/json'},credentials:'same-origin',body:JSON.stringify({domain:document.getElementById('lsDomain').value,topic,level:document.getElementById('lsLevel').value,goal:document.getElementById('lsGoal').value,lang:document.getElementById('lsLang').value,code_language:document.getElementById('lsCodeLanguage').value,roadmap_node_id:lsCurrentRoadmapNodeId})}));
        const a=res.activity||{}; const box=document.getElementById('lsActivityResult'); box.classList.remove('hidden');
        const arr=(xs)=>Array.isArray(xs)?xs:[];
        box.innerHTML=`<div class="flex flex-wrap items-start justify-between gap-2"><div><span class="text-[10px] font-black uppercase text-amber-800">${escapeHtml(a.activityType||'Learning activity')}</span><h3 class="font-black text-lg">${escapeHtml(a.title||topic)}</h3></div><span class="rounded-lg bg-white px-3 py-2 text-xs font-black">إتقان ${Number(a.masteryThreshold)||80}%</span></div><p class="text-sm">${escapeHtml(a.objective||'')}</p><div><b class="text-xs">المتطلبات والأدوات</b><p class="text-xs mt-1">${escapeHtml(arr(a.prerequisites).join(' · '))}</p><p class="text-xs mt-1">${escapeHtml(arr(a.materialsOrTools).join(' · '))}</p></div><div><b class="text-xs">خطوات التنفيذ</b><ol class="list-decimal pr-5 mt-2 space-y-2">${arr(a.steps).map((x,i)=>`<li class="text-xs"><b>${escapeHtml(x.step||'')}</b><p class="mt-1">الدليل المطلوب: ${escapeHtml(x.expectedEvidence||'')}</p></li>`).join('')}</ol></div><div class="rounded-xl bg-white border p-3"><b class="text-xs">المخرج المطلوب</b><p class="text-xs mt-1">${escapeHtml(a.expectedOutput||'')}</p></div><div><b class="text-xs">معايير التقييم</b><ul class="list-disc pr-5 mt-2">${arr(a.rubric).map(x=>`<li class="text-xs mb-1">${escapeHtml(x.criterion||'')}: ${escapeHtml(x.observableEvidence||'')}</li>`).join('')}</ul></div><details class="rounded-xl bg-white border p-3"><summary class="cursor-pointer text-xs font-black">أسئلة تحقق ودليل الإجابة</summary><div class="mt-3 space-y-2">${arr(a.checkQuestions).map((q,i)=>`<div class="text-xs"><b>${i+1}. ${escapeHtml(q.question||'')}</b><p>${escapeHtml(arr(q.options).join(' / '))}</p><p class="text-emerald-800">الإجابة: ${escapeHtml(arr(q.options)[Number(q.correctIndex)]||'—')}</p><p>${escapeHtml(q.explanation||'')}</p></div>`).join('')}<p class="text-xs whitespace-pre-wrap mt-2">${escapeHtml(a.answerGuide||'')}</p></div></details><details class="rounded-xl bg-white border p-3"><summary class="cursor-pointer text-xs font-black">المراجع والأخطاء الشائعة والمراجعة</summary><div class="mt-2 space-y-2">${arr(a.resources).map(r=>`<div class="text-xs"><b>${escapeHtml(r.title||'Resource')}</b> · ${escapeHtml(r.provider||'')}<p>${escapeHtml(r.why||'')}</p><a class="text-blue-700 underline" target="_blank" rel="noopener" href="https://www.google.com/search?q=${encodeURIComponent(r.searchQuery||r.title||topic)}">ابحث عن المصدر</a></div>`).join('')}<p class="text-xs">الأخطاء الشائعة: ${escapeHtml(arr(a.commonMistakes).join(' · '))}</p><p class="text-xs">خطة المراجعة: ${escapeHtml(a.reviewPlan||'')}</p></div></details><button onclick="lsLoadWork(${Number(res.id)})" class="rounded-xl bg-slate-900 text-amber-300 px-4 py-2 text-xs font-black">فتح النشاط من السجل</button>`;
        document.getElementById('lsWorkTitle').value=a.title||topic; document.getElementById('lsTestCases').value=JSON.stringify(arr(a.testCases),null,2); document.getElementById('lsTask').value=`${a.objective||''}\\n\\n${arr(a.steps).map((x,i)=>`${i+1}. ${x.step||''}\\nالدليل المطلوب: ${x.expectedEvidence||''}`).join('\\n')}\\n\\nالمخرج: ${a.expectedOutput||''}`;
        lsLoadHistory();
      }catch(e){lsSetOutput('تعذر إنشاء النشاط: '+e.message)}finally{btn.disabled=false;btn.innerText=original;}
    }
    async function lsRunCode(){
      const code=document.getElementById('lsCode').value; if(!code.trim())return alert('اكتب الكود الأول.');
      lsSetOutput('جاري تشغيل الكود في بيئة معزولة...');
      try{const r=await readApiResponse(await fetch('/api/learning-studio/run-code',{method:'POST',headers:{'Content-Type':'application/json'},credentials:'same-origin',body:JSON.stringify({language:document.getElementById('lsCodeLanguage').value,code,stdin:document.getElementById('lsStdin').value})}));
        lsLastExecutionEvidence={kind:'single_run',language:r.language||document.getElementById('lsCodeLanguage').value,code,status:r.status,stdout:r.stdout||'',stderr:r.stderr||'',compile_output:r.compile_output||'',compile_stderr:r.compile_stderr||'',exit_code:r.exit_code};
        lsSetOutput(`Language: ${lsLastExecutionEvidence.language}\\nStatus: ${r.status}\\nExit code: ${r.exit_code}\\n\\nSTDOUT:\\n${r.stdout||'(empty)'}\\n\\nSTDERR:\\n${r.stderr||'(empty)'}\\n\\nCompile output:\\n${r.compile_output||'(none)'}\\n\\nCompile errors:\\n${r.compile_stderr||'(none)'}`);
        const result=lsLastExecutionEvidence;
        await fetch('/api/learning-studio/save',{method:'POST',headers:{'Content-Type':'application/json'},credentials:'same-origin',body:JSON.stringify({...lsPayload('code_run',code,result),result})}); lsLoadHistory();
      }catch(e){lsLastExecutionEvidence={kind:'single_run',status:'runner_error',error:e.message};lsSetOutput('لم يتم تشغيل الكود. '+e.message+'\\nتأكد من توفر خدمة Piston أو إعداد CODE_RUNNER_URL على الاستضافة.');}
    }
    async function lsRunTests(){
      let cases; try{cases=JSON.parse(document.getElementById('lsTestCases').value||'[]');}catch(e){return alert('صيغة JSON غير صحيحة في حالات الاختبار.');}
      if(!Array.isArray(cases)||!cases.length)return alert('أضف حالة اختبار واحدة على الأقل.');
      if(cases.length>10)return alert('الحد الأقصى 10 حالات اختبار في المرة الواحدة.');
      const code=document.getElementById('lsCode').value; if(!code.trim())return alert('اكتب الكود الأول.');
      lsSetOutput(`جاري تشغيل ${cases.length} حالات اختبار...`); const results=[];
      for(let i=0;i<cases.length;i++){
        try{const r=await readApiResponse(await fetch('/api/learning-studio/run-code',{method:'POST',headers:{'Content-Type':'application/json'},credentials:'same-origin',body:JSON.stringify({language:document.getElementById('lsCodeLanguage').value,code,stdin:String(cases[i].input??'')})})); const actual=String(r.stdout||'').trim(); const expected=String(cases[i].expected??'').trim(); results.push({case:i+1,passed:r.status==='success'&&actual===expected,expected,actual,stderr:r.stderr||'',exit_code:r.exit_code});}
        catch(e){results.push({case:i+1,passed:false,error:e.message});break;}
      }
      const passed=results.filter(x=>x.passed).length; lsLastExecutionEvidence={kind:'automated_tests',language:document.getElementById('lsCodeLanguage').value,code,passed,total:results.length,results}; lsSetOutput(`Test results: ${passed}/${results.length} passed\\n\\n`+results.map(x=>`Case ${x.case}: ${x.passed?'PASS':'FAIL'}\\nExpected: ${x.expected??'—'}\\nActual: ${x.actual??'—'}${x.error?'\\nError: '+x.error:''}${x.stderr?'\\nStderr: '+x.stderr:''}`).join('\\n\\n'));
      const content=document.getElementById('lsCode').value; try{await fetch('/api/learning-studio/save',{method:'POST',headers:{'Content-Type':'application/json'},credentials:'same-origin',body:JSON.stringify({...lsPayload('code_tests',content,lsLastExecutionEvidence),content})});lsLoadHistory();}catch(e){}
    }
    async function lsGetFeedback(){
      const task=document.getElementById('lsTask').value.trim(); const isCode=document.getElementById('lsDomain').value==='programming'; const answer=isCode?document.getElementById('lsCode').value:document.getElementById('lsAnswer').value;
      if(!task||!answer.trim())return alert('اكتب تعليمات المهمة وحلك الأول.'); lsSetOutput('الـ AI بيراجع الحل...');
      const usableEvidence=lsLastExecutionEvidence&&(!isCode||lsLastExecutionEvidence.code===answer)?lsLastExecutionEvidence:null; const taskWithEvidence=usableEvidence?`${task}\\n\\nنتائج التشغيل/الاختبارات الفعلية من بيئة التنفيذ (استخدمها كدليل، ولا تفترض نجاح اختبارات لم تُشغّل):\\n${JSON.stringify(usableEvidence).slice(0,12000)}`:task;
      try{const r=await readApiResponse(await fetch('/api/learning-studio/feedback',{method:'POST',headers:{'Content-Type':'application/json'},credentials:'same-origin',body:JSON.stringify({domain:document.getElementById('lsDomain').value,task:taskWithEvidence,answer,level:document.getElementById('lsLevel').value,lang:document.getElementById('lsLang').value})}));
        const f=r.feedback||{}; const arr=(xs)=>Array.isArray(xs)?xs:[]; const score=Number.isFinite(Number(f.score))?`${Math.max(0,Math.min(100,Number(f.score)))}/100`:'غير متاح'; lsSetOutput(`التقييم المبدئي: ${score}\\n${f.scoreRationale||'الدرجة إرشادية وتعتمد على المهمة والمعايير المتاحة.'}\\n\\n${f.overallAssessment||''}\\n\\nنقاط القوة:\\n${arr(f.strengths).map(x=>'• '+x).join('\\n')}\\n\\nنقاط تحتاج مراجعة:\\n${arr(f.issues).map(x=>'• '+(x.issue||'')+' ['+(x.severity||'ملاحظة')+']\\n  السبب: '+(x.why||'')+'\\n  تلميح: '+(x.hint||'')).join('\\n')}\\n\\nالخطوات التالية:\\n${arr(f.nextSteps).map(x=>'• '+x).join('\\n')}\\n\\nتصحيح مقترح (بعد المحاولة):\\n${f.suggestedCorrection||''}\\n\\nالثقة: ${f.confidence||'غير محددة'}`); lsLoadHistory();
      }catch(e){lsSetOutput('تعذر الحصول على feedback: '+e.message)}
    }
    async function lsSaveWork(){
      const isCode=document.getElementById('lsDomain').value==='programming'; const work=isCode?document.getElementById('lsCode').value:document.getElementById('lsAnswer').value; const task=document.getElementById('lsTask').value; const content=work; if(!task.trim()&&!work.trim())return alert('اكتب شيئًا تريد حفظه.');
      try{const r=await readApiResponse(await fetch('/api/learning-studio/save',{method:'POST',headers:{'Content-Type':'application/json'},credentials:'same-origin',body:JSON.stringify({...lsPayload('student_work',content,{task}),content,language:isCode?document.getElementById('lsCodeLanguage').value:''})})); lsSetOutput('تم حفظ عملك في الـ History. ID: '+r.id); lsLoadHistory();}catch(e){lsSetOutput('فشل الحفظ: '+e.message)}
    }
    async function lsLoadHistory(){
      const box=document.getElementById('lsRecentHistory'); if(!box||!currentUserId)return;
      try{const r=await readApiResponse(await fetch('/api/learning-studio/history?limit=12',{credentials:'same-origin',cache:'no-store'})); const items=r.items||[]; const html=items.map(x=>`<div class="rounded-xl border bg-slate-50 p-3"><div class="flex flex-wrap justify-between gap-2"><b>${escapeHtml(x.title||'Study work')}</b><span class="text-slate-400">${escapeHtml(x.activity_type||'')} · ${escapeHtml(x.created_at||'')}</span></div><p class="text-slate-600 mt-1">${escapeHtml(x.domain||'general')} · ${escapeHtml(x.language||'')}</p><p class="mt-1 text-slate-500">${escapeHtml(x.preview||'')}</p><button onclick="lsLoadWork(${Number(x.id)})" class="mt-2 rounded-lg bg-white border px-3 py-1.5 font-bold">فتح العمل</button></div>`).join('')||'<p class="text-slate-500">لا يوجد عمل محفوظ بعد.</p>'; box.innerHTML=html; const h=document.getElementById('historyLearningStudioBox'); if(h)h.innerHTML=html;
      }catch(e){box.innerHTML=`<p class="text-rose-600">${escapeHtml(e.message)}</p>`;}
    }
    async function lsLoadWork(id){
      try{const r=await readApiResponse(await fetch(`/api/learning-studio/work/${Number(id)}`,{credentials:'same-origin',cache:'no-store'})); const w=r.work||{}; lsCurrentRoadmapNodeId=w.roadmap_node_id?Number(w.roadmap_node_id):null; lsCurrentActivityIndex=null; lsLastExecutionEvidence=(w.result&&w.result.kind)?w.result:null; document.getElementById('lsDomain').value=w.domain||'other'; document.getElementById('lsWorkTitle').value=w.title||''; document.getElementById('lsCodeLanguage').value=w.language||'python'; const content=String(w.content||''); if((w.activity_type||'').includes('code')||(w.domain==='programming'&&w.activity_type!=='designed_activity')){document.getElementById('lsCode').value=content;document.getElementById('lsAnswer').value='';}else{document.getElementById('lsAnswer').value=content;document.getElementById('lsCode').value='';} document.getElementById('lsTask').value=(w.result&&w.result.task)||''; if(w.result&&w.result.feedback){lsSetOutput(JSON.stringify(w.result.feedback,null,2));}else if(w.result&&w.result.generated_activity){document.getElementById('lsActivityResult').classList.remove('hidden');document.getElementById('lsActivityResult').innerHTML=`<h3 class="font-black">${escapeHtml(w.title||'Saved activity')}</h3><pre class="whitespace-pre-wrap text-xs">${escapeHtml(JSON.stringify(w.result.generated_activity,null,2))}</pre>`;}else if(w.result&&Object.keys(w.result).length){lsSetOutput(JSON.stringify(w.result,null,2));} lsToggleEditor(); switchTab('learningstudio');}catch(e){alert('تعذر فتح العمل: '+e.message)}
    }
    let lsCurrentRoadmapNodeId = null;
    let lsCurrentActivityIndex = null;
    let lsLastExecutionEvidence = null;
    function inferRoadmapCodeLanguage(text){
      const t=String(text||'').toLowerCase();
      if(/c\\+\\+|\\bcpp\\b|c plus plus/.test(t)) return 'cpp';
      if(/\\bjavascript\\b|\\bnode\\.js\\b|\\bjs\\b/.test(t)) return 'javascript';
      if(/\\bjava\\b/.test(t)) return 'java';
      if(/\\bpython\\b/.test(t)) return 'python';
      if(/\\bc programming\\b|\\blanguage c\\b/.test(t)) return 'c';
      return '';
    }
    function openLearningStudioFromRoadmap(nodeId, activityIndex){
      const node=(window.__activeRoadmapNodes||[]).find(n=>Number(n.id)===Number(nodeId)); if(!node)return;
      const plan=node.learning_plan||{}; const a=(plan.activities||[])[Number(activityIndex)]||{};
      const discipline=String(plan.discipline||plan.activityType||'other').toLowerCase();
      const domain=discipline.includes('cod')||discipline.includes('software')?'programming':discipline.includes('math')?'mathematics':discipline.includes('phys')||discipline.includes('science')?'physics':discipline.includes('language')?'languages':discipline.includes('engineer')||discipline.includes('hardware')?'engineering':'other';
      lsCurrentRoadmapNodeId=Number(nodeId); lsCurrentActivityIndex=Number(activityIndex); lsLastExecutionEvidence=null;
      const goal=String(plan.trackGoal||document.getElementById('gpsGoalInput')?.value||'');
      document.getElementById('lsDomain').value=domain; document.getElementById('lsTopic').value=a.title||node.title||''; document.getElementById('lsWorkTitle').value=a.title||node.title||''; document.getElementById('lsTask').value=`المسار: ${goal}\\nالمرحلة: ${node.title||''}\\nالنشاط: ${a.title||''}\\n\\n${a.instructions||''}\\n\\nالمخرج المطلوب: ${a.deliverable||''}`;
      if(domain==='programming'){
        const inferred=String(plan.programmingLanguage||'')||inferRoadmapCodeLanguage(goal+' '+node.title+' '+a.title+' '+(a.instructions||''));
        if(inferred && ['python','cpp','javascript','java','c'].includes(inferred)) document.getElementById('lsCodeLanguage').value=inferred;
        else document.getElementById('lsCodeLanguage').value='python';
        document.getElementById('lsTestCases').value='';
      }
      lsToggleEditor(); switchTab('learningstudio');
    }

    function renderRoadmapCards(nodes) {
      window.__activeRoadmapNodes = Array.isArray(nodes) ? nodes : [];
      nodes = window.__activeRoadmapNodes;
      const roadmapBox = document.getElementById('gpsRoadmapContainer');
      if (!roadmapBox) return;
      if (!nodes || nodes.length === 0) {
        roadmapBox.innerHTML = `<div class="p-8 text-center text-slate-400 text-xs bg-slate-50 border border-dashed border-slate-200 rounded-2xl">لم يتم إنشاء مسار بعد. اكتب المجال والمدة بالأعلى واضغط "إنشاء المسار المخصص".</div>`;
        return;
      }
      roadmapBox.innerHTML = nodes.map(b => {
        const plan = b.learning_plan && typeof b.learning_plan === 'object' ? b.learning_plan : {};
        const activities = Array.isArray(plan.activities) ? plan.activities : [];
        const done = Array.isArray(b.progress_json) ? b.progress_json.map(Number) : [];
        const progressPct = activities.length ? Math.round(done.length / activities.length * 100) : 0;
        const isLocked = !b.is_unlocked && !b.is_completed;
        const resource = (Array.isArray(plan.resources) && plan.resources[0]) || {};
        const query = resource.searchQuery || b.course_link || b.course_title || b.title;
        const ytSearchUrl = `https://www.youtube.com/results?search_query=${encodeURIComponent(query)}`;
        const list = (arr, cls='') => (Array.isArray(arr) && arr.length) ? `<ul class="${cls}">${arr.map(x => `<li class="mb-1">• ${escapeHtml(typeof x === 'string' ? x : JSON.stringify(x))}</li>`).join('')}</ul>` : '<p class="text-xs text-slate-400">لم يحدد المحتوى تفاصيل إضافية لهذا الجزء.</p>';
        const activityHtml = activities.map((a, i) => {
          const checked = done.includes(i);
          return `<label class="flex items-start gap-2 rounded-xl border ${checked ? 'border-emerald-200 bg-emerald-50' : 'border-slate-200 bg-white'} p-3 cursor-pointer">
            <input type="checkbox" class="mt-1 accent-emerald-600" ${checked ? 'checked' : ''} ${isLocked ? 'disabled' : ''} onchange="saveRoadmapProgress(${Number(b.id)}, ${i}, this.checked)">
            <span class="flex-1"><span class="block text-xs font-black text-slate-800">${escapeHtml(a.title || `نشاط ${i+1}`)} <span class="text-[10px] font-normal text-slate-500">· ${Number(a.estimatedMinutes)||30} دقيقة</span></span>
            <span class="block text-xs text-slate-600 mt-1 whitespace-pre-line">${escapeHtml(a.instructions || '')}</span>
            ${a.deliverable ? `<span class="block text-[11px] text-amber-800 mt-1"><b>المخرج المطلوب:</b> ${escapeHtml(a.deliverable)}</span>` : ''}
            ${Array.isArray(a.tools) && a.tools.length ? `<span class="block text-[10px] text-slate-500 mt-1"><b>الأدوات:</b> ${escapeHtml(a.tools.join('، '))}</span>` : ''}<button type="button" onclick="openLearningStudioFromRoadmap(${Number(b.id)},${i})" class="mt-2 rounded-lg bg-indigo-50 text-indigo-800 border border-indigo-100 px-3 py-1.5 text-[10px] font-black">فتح مساحة التطبيق لهذا النشاط ↗</button></span></label>`;
        }).join('');
        const dailyLessons = Array.isArray(plan.dailyLessons) ? plan.dailyLessons : [];
        const dailyLessonsHtml = dailyLessons.map((day, di) => {
          const priorDay = di > 0 ? dailyLessons[di-1] : null;
          const lockedDay = !!isLocked || (!!priorDay && !priorDay.passed);
          const dayQs = Array.isArray(day.quizQuestions) ? day.quizQuestions : [];
          const v = day.video || {}, rd = day.reading || {}, pr = day.practice || {};
          const videoUrl = `https://www.youtube.com/results?search_query=${encodeURIComponent(v.searchQuery || day.title || b.title)}`;
          const readingUrl = `https://www.google.com/search?q=${encodeURIComponent((rd.provider ? 'site:'+rd.provider+' ' : '') + (rd.searchQuery || rd.title || day.title || b.title))}`;
          const state = day.passed ? `<span class="text-emerald-700 font-bold">✓ اجتزت (${Number(day.lastAttemptScore)||0}%)</span>` : (day.lastAttemptScore != null ? `<span class="text-amber-700 font-bold">آخر نتيجة: ${Number(day.lastAttemptScore)}% — راجع وأعد الاختبار</span>` : '<span class="text-slate-500">لم تختبر بعد</span>');
          return `<div class="rounded-xl border ${day.passed?'border-emerald-200 bg-emerald-50/40':'border-slate-200 bg-white'} p-3 space-y-2"><div class="flex items-center justify-between gap-2"><b class="text-xs">اليوم ${Number(day.dayNumber)||di+1}: ${escapeHtml(day.title||'درس اليوم')}</b><span class="text-[10px]">${Number(day.estimatedMinutes)||0} دقيقة</span></div><p class="text-xs text-slate-600">${escapeHtml(day.objective||'')}</p><div class="grid grid-cols-1 sm:grid-cols-2 gap-2"><a target="_blank" rel="noopener noreferrer" href="${videoUrl}" class="rounded-lg bg-red-50 text-red-800 p-2 text-xs font-bold">▶ ${escapeHtml(v.title||'فيديو اليوم')} · ${Number(v.estimatedMinutes)||0} د</a><a target="_blank" rel="noopener noreferrer" href="${readingUrl}" class="rounded-lg bg-blue-50 text-blue-800 p-2 text-xs font-bold">📖 ${escapeHtml(rd.title||'قراءة اليوم')} · ${Number(rd.estimatedMinutes)||0} د</a></div><div class="rounded-lg bg-slate-50 p-2 text-xs"><b>${escapeHtml(pr.title||'تطبيق اليوم')}</b><p class="mt-1">${escapeHtml(pr.instructions||'')}</p><p class="mt-1 text-amber-800">المطلوب: ${escapeHtml(pr.deliverable||'مخرج عملي')}</p><span class="text-slate-500">${Number(pr.estimatedMinutes)||0} دقيقة للتطبيق + ${Number(day.quizMinutes)||0} دقيقة للاختبار</span></div><div class="flex flex-wrap items-center justify-between gap-2 text-[10px]">${state}<span>${dayQs.length} أسئلة على محتوى اليوم</span></div>${dayQs.length ? `<button type="button" ${lockedDay?'disabled':''} onclick="launchDailyRoadmapQuiz(${Number(b.id)},${di})" class="w-full rounded-lg px-3 py-2 text-xs font-black ${lockedDay?'bg-slate-100 text-slate-400':'bg-indigo-600 text-white'}">${lockedDay?'🔒 اجتز اختبار اليوم السابق أولًا':(day.passed?'إعادة اختبار اليوم':'بدء اختبار اليوم')}</button>` : '<p class="text-xs text-amber-700">لم يتم إنشاء اختبار يومي صالح لهذا اليوم؛ أعد إنشاء المسار لضمان الاختبار.</p>'}</div>`;
        }).join('');
        const resourcesHtml = (Array.isArray(plan.resources) ? plan.resources : []).map(r => { const kind=String(r.resourceType||r.type||'video').toLowerCase(); const query=String(r.searchQuery||r.title||b.title||''); const isVideo=kind.includes('video')||kind.includes('lesson'); const provider=String(r.provider||''); const refQuery=(provider.includes('.')?'site:'+provider+' ':provider+' ')+query; const searchUrl=isVideo?`https://www.youtube.com/results?search_query=${encodeURIComponent(query)}`:`https://www.google.com/search?q=${encodeURIComponent(refQuery)}`; const kindLabel=isVideo?'فيديو / شرح':(kind.includes('practice')?'تطبيق عملي':(kind.includes('course')?'كورس':'مرجع مكتوب')); const mins=Number(r.estimatedMinutes)||0; return `<div class="rounded-xl border border-amber-200 bg-amber-50/70 p-3"><div class="flex flex-wrap items-center gap-2"><span class="text-[9px] rounded-full bg-white border border-amber-200 px-2 py-1 font-bold">${kindLabel}</span>${mins?`<span class="text-[9px] text-slate-500">${mins} دقيقة تقريبًا</span>`:''}</div><div class="font-black text-xs text-slate-800 mt-2">${escapeHtml(r.title || 'مورد تعليمي')}</div><div class="text-[10px] text-slate-500 mt-1">${escapeHtml(r.provider || 'مصدر تعليمي')}</div><p class="text-xs text-slate-600 mt-1">${escapeHtml(r.why || '')}</p><a class="inline-block mt-2 text-[11px] font-bold ${isVideo?'text-red-700':'text-blue-700'} underline" target="_blank" rel="noopener noreferrer" href="${searchUrl}">${isVideo?'افتح بحث الفيديو الموجّه ↗':'افتح بحث المرجع الموجّه ↗'}</a></div>`; }).join('');
        const evaluation = plan.evaluation || {};
        const quiz = Array.isArray(plan.quizQuestions) ? plan.quizQuestions : [];
        return `<article class="p-4 md:p-5 bg-white border-2 ${b.is_completed ? 'border-emerald-400 bg-emerald-50/20' : (b.is_unlocked ? 'border-amber-400 shadow-md' : 'border-slate-200 opacity-80')} rounded-2xl space-y-3 transition">
          <div class="flex flex-wrap justify-between items-center gap-2 border-b border-slate-100 pb-3"><div class="flex items-center gap-2"><span class="w-7 h-7 rounded-lg ${b.is_completed ? 'bg-emerald-500 text-white' : (b.is_unlocked ? 'bg-amber-400 text-slate-950' : 'bg-slate-200 text-slate-500')} flex items-center justify-center text-xs font-black">${b.is_completed ? '✓' : Number(b.node_order)||''}</span><div><h4 class="font-black text-slate-900 text-sm">${escapeHtml(b.title || '')}</h4><p class="text-[10px] text-slate-500">${escapeHtml(plan.activityType || 'Mixed Practice')} · ${escapeHtml(b.timeframe || 'مرحلة أساسية')}</p></div></div><span class="text-[10px] font-bold rounded-lg bg-slate-100 px-2 py-1">${progressPct}% من الأنشطة</span></div>
          <p class="text-xs text-slate-700 leading-relaxed">🎯 <b>هدف المرحلة:</b> ${escapeHtml(b.philosophy || '')}</p>
          ${plan.prerequisites && plan.prerequisites.length ? `<div class="text-xs"><b>المتطلبات السابقة:</b>${list(plan.prerequisites, 'mt-1 text-slate-600')}</div>` : ''}
          ${plan.coreConcepts && plan.coreConcepts.length ? `<div class="text-xs"><b>المفاهيم الأساسية:</b>${list(plan.coreConcepts, 'mt-1 text-slate-600')}</div>` : ''}
          <div class="h-2 rounded-full bg-slate-100 overflow-hidden"><div class="h-full bg-emerald-500 transition-all" style="width:${progressPct}%"></div></div>
          ${dailyLessons.length ? `<details open class="rounded-xl border border-indigo-200 bg-indigo-50/30 p-3"><summary class="cursor-pointer font-black text-xs text-indigo-900">📅 الخطة اليومية والاختبارات على محتوى كل يوم (${dailyLessons.filter(d=>d.passed).length}/${dailyLessons.length} أيام مجتازة)</summary><p class="text-[10px] text-slate-600 mt-2 mb-3">كل يوم يحدد فيديو وقراءة وتطبيقًا واختبارًا من نفس محتواه. اجتز اختبار اليوم قبل فتح اختبار اليوم التالي.</p><div class="space-y-3 mt-2">${dailyLessonsHtml}</div></details>` : ''}
          ${plan.professionalWorkSimulation ? `<details class="rounded-xl border border-emerald-200 p-3"><summary class="cursor-pointer font-black text-xs text-emerald-900">🧰 محاكاة مهمة عمل حقيقية</summary><div class="mt-3 space-y-2 text-xs"><p><b>السياق:</b> ${escapeHtml(plan.professionalWorkSimulation.scenario||'')}</p><p><b>المدخلات/البيانات:</b> ${escapeHtml(plan.professionalWorkSimulation.inputsOrData||'')}</p><p><b>اسم البيانات/الأثر المطلوب:</b> ${escapeHtml(plan.professionalWorkSimulation.datasetOrArtifactName||'')}</p>${plan.professionalWorkSimulation.sourceUrl && (String(plan.professionalWorkSimulation.sourceUrl).startsWith('https://') || String(plan.professionalWorkSimulation.sourceUrl).startsWith('http://')) ? `<p><a class="text-blue-700 underline" target="_blank" rel="noopener noreferrer" href="${escapeHtml(plan.professionalWorkSimulation.sourceUrl)}">فتح المصدر المحدد ↗</a></p>` : `<p><a class="text-blue-700 underline" target="_blank" rel="noopener noreferrer" href="https://www.google.com/search?q=${encodeURIComponent(plan.professionalWorkSimulation.sourceSearchQuery||plan.professionalWorkSimulation.datasetOrArtifactName||b.title)}">البحث عن البيانات/المشروع ↗</a></p>`}<p><b>الترخيص والوصول:</b> ${escapeHtml(plan.professionalWorkSimulation.licenseOrAccess||'')}</p><p><b>البديل عند عدم توفر المصدر:</b> ${escapeHtml(plan.professionalWorkSimulation.fallbackIfUnavailable||'')}</p>${list(plan.professionalWorkSimulation.constraints,'text-slate-600')}<p><b>التسليمات:</b></p>${list(plan.professionalWorkSimulation.deliverables,'text-slate-600')}<p><b>معايير القبول:</b></p>${list(plan.professionalWorkSimulation.acceptanceCriteria,'text-slate-600')}<p class="text-[10px] text-slate-500">مصدر البيانات: ${escapeHtml(plan.professionalWorkSimulation.dataProvenance||'غير محدد')} — المحاكاة ليست ضمانًا للتوظيف.</p><label class="block font-bold mt-3">سلّم حلك أو تقريرك أو ملخص الكود والاختبارات</label><textarea id="professionalSubmission-${Number(b.id)}" rows="7" maxlength="30000" class="w-full rounded-xl border border-emerald-200 p-3 text-xs" placeholder="اشرح قراراتك، أرفق النتائج المهمة، واكتب ما نفذته وما لم تستطع التحقق منه..."></textarea><button type="button" onclick="submitProfessionalSimulation(${Number(b.id)})" class="w-full rounded-xl bg-emerald-700 text-white font-black text-xs p-3">إرسال المهمة للتقييم المهني بالـ AI</button><div id="professionalResult-${Number(b.id)}" class="text-xs mt-2">${plan.professionalWorkSimulation.lastAttempt ? `آخر نتيجة: ${Number(plan.professionalWorkSimulation.lastScore)||0}% — ${plan.professionalWorkSimulation.passed?'اجتزت المهمة المهنية':'تحتاج إلى تحسين'}<p class="mt-1">${escapeHtml(plan.professionalWorkSimulation.lastAttempt.feedback||'')}</p>` : 'لم يتم تقييم تسليم مهني بعد.'}</div></div></details>` : ''}
          ${plan.finalJobReadinessAssessment ? `<details class="rounded-xl border border-violet-200 bg-violet-50/30 p-3"><summary class="cursor-pointer font-black text-xs text-violet-900">🏁 التقييم النهائي للاستعداد المهني</summary><div class="mt-3 space-y-2 text-xs"><p>${escapeHtml(plan.finalJobReadinessAssessment.task||'')}</p><p><b>حد الاجتياز:</b> ${Number(plan.finalJobReadinessAssessment.passThreshold)||80}%</p><p><b>المخرجات المطلوبة:</b></p>${list(plan.finalJobReadinessAssessment.deliverables,'text-slate-600')}<p><b>معايير التقييم:</b></p>${list((plan.finalJobReadinessAssessment.rubric||[]).map(r=>typeof r==='string'?r:`${r.criterion||'معيار'} (${Number(r.weight)||0}%)`),'text-slate-600')}<p class="text-[10px] text-slate-500">إنهاء المسار لا يضمن التوظيف؛ هذا التقييم يقيس أدلة عملية على المهارات.</p></div></details>` : ''}
          <details open class="rounded-xl border border-slate-200 p-3"><summary class="cursor-pointer font-black text-xs text-slate-800">🧩 أنشطة عملية قابلة للتأشير (${done.length}/${activities.length})</summary><div class="space-y-2 mt-3">${activityHtml || '<p class="text-xs text-slate-500">لا توجد أنشطة مفصلة لهذا المسار القديم. أنشئ مسارًا جديدًا للحصول على الأنشطة التفصيلية.</p>'}</div><p id="roadmapProgressMsg-${Number(b.id)}" class="text-[10px] text-emerald-700 mt-2"></p></details>
          <details class="rounded-xl border border-slate-200 p-3"><summary class="cursor-pointer font-black text-xs text-slate-800">📚 المراجع المقترحة</summary><p class="text-[10px] text-slate-500 mt-2">روابط البحث موجّهة إلى الموضوع والمزوّد؛ راجع نتيجة البحث نفسها لأن الموقع لا يدّعي التحقق المباشر من كل فيديو أو إصدار كتاب.</p><div class="space-y-2 mt-3">${resourcesHtml || `<a href="${ytSearchUrl}" target="_blank" rel="noopener" class="text-xs font-bold text-red-700 underline">ابحث عن مصدر مناسب ↗</a>`}</div></details>
          <details class="rounded-xl border border-slate-200 p-3"><summary class="cursor-pointer font-black text-xs text-slate-800">✅ التقييم ومعيار الإتقان</summary><div class="mt-3 text-xs text-slate-700"><p class="mb-2"><b>المعيار المطلوب:</b> ${Number(evaluation.masteryThreshold)||80}%</p>${list(evaluation.criteria, 'text-slate-600')}</div>${plan.miniProject ? `<div class="mt-3 rounded-lg bg-indigo-50 p-3 text-xs text-indigo-900"><b>المشروع/المخرج:</b> ${escapeHtml(plan.miniProject)}</div>` : ''}</details>
          ${quiz.length ? `<details class="rounded-xl border border-slate-200 p-3"><summary class="cursor-pointer font-black text-xs text-slate-800">🧠 أسئلة المراجعة (${quiz.length})</summary><div class="space-y-3 mt-3">${quiz.map((q,qi)=>`<div class="rounded-xl bg-slate-50 p-3"><p class="font-bold text-xs text-slate-800">${qi+1}. ${escapeHtml(q.question || '')}</p>${list(q.options, 'mt-2 text-xs text-slate-600')}${b.is_completed ? `<details class="mt-2"><summary class="cursor-pointer text-[11px] font-bold text-amber-800">عرض الإجابة والتفسير</summary><p class="mt-2 text-xs text-emerald-800">الإجابة: ${escapeHtml((q.options||[])[Number(q.correctIndex)||0] || 'غير محددة')}</p><p class="text-xs text-slate-600">${escapeHtml(q.explanation || '')}</p></details>` : '<p class="mt-2 text-[10px] text-slate-400">اجتز اختبار المرحلة لعرض الإجابة والتفسير.</p>'}</div>`).join('')}</div></details>` : ''}
          ${plan.commonMistakes && plan.commonMistakes.length ? `<details class="rounded-xl border border-slate-200 p-3"><summary class="cursor-pointer font-black text-xs text-slate-800">⚠️ أخطاء شائعة</summary>${list(plan.commonMistakes, 'mt-3 text-xs text-slate-600')}</details>` : ''}
          ${plan.reviewPlan ? `<div class="rounded-xl bg-blue-50 p-3 text-xs text-blue-900"><b>🔁 المراجعة:</b> ${escapeHtml(plan.reviewPlan)}</div>` : ''}
          ${plan.nextStep ? `<div class="rounded-xl bg-slate-50 p-3 text-xs text-slate-700"><b>الخطوة التالية:</b> ${escapeHtml(plan.nextStep)}</div>` : ''}
          <div class="pt-1">${b.is_completed ? '<div class="text-center py-2 bg-emerald-100 text-emerald-800 font-black text-xs rounded-xl">✓ اجتزت اختبار المرحلة</div>' : (b.is_unlocked ? `<button onclick='launchBlueprintChallengeById(${Number(b.id)})' class="w-full py-3 bg-gradient-to-r from-amber-400 to-yellow-400 text-slate-950 font-black text-xs rounded-xl">بدء اختبار التحقق من المرحلة (+50 XP) 🎯</button>` : '<div class="text-center py-2 bg-slate-100 text-slate-500 font-bold text-xs rounded-xl">🔒 أكمل اختبار المرحلة السابقة لفتح هذه المرحلة</div>')}</div>
        </article>`;
      }).join('');
    }

    async function saveRoadmapProgress(nodeId, activityIndex, checked) {
      const node = (window.__activeRoadmapNodes || []).find(n => Number(n.id) === Number(nodeId));
      if (!node || !currentUserId) return;
      const prior = Array.isArray(node.progress_json) ? node.progress_json.map(Number) : [];
      const updated = new Set(prior);
      if (checked) updated.add(Number(activityIndex)); else updated.delete(Number(activityIndex));
      const msg = document.getElementById(`roadmapProgressMsg-${nodeId}`);
      try {
        const res = await fetch('/api/gps/node-progress', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({user_id:Number(currentUserId), node_id:Number(nodeId), completed_activities:[...updated]})});
        const data = await readApiResponse(res);
        node.progress_json = data.completed_activities || [];
        renderRoadmapCards(window.__activeRoadmapNodes || []);
        const updatedMsg = document.getElementById(`roadmapProgressMsg-${nodeId}`);
        if (updatedMsg) updatedMsg.textContent = 'تم حفظ تقدمك في قاعدة البيانات.';
      } catch (e) {
        if (msg) msg.textContent = 'تعذر حفظ التقدم. حاول مرة أخرى.';
        console.error('Roadmap progress save failed', e);
      }
    }

    async function generateGpsRoadmap() {
      const g = document.getElementById('gpsGoalInput').value.trim();
      const d = document.getElementById('gpsDurationInput').value;
      if (!g) return alert(currentLang === 'ar' ? "يرجى كتابة التخصص أو المادة أولاً." : "Please enter your target goal.");

      const btn = document.getElementById('generateGpsBtn');
      btn.disabled = true;
      btn.innerHTML = `<span>جاري بناء مسار مناسب لوقتك واختيار مصادر وتطبيقات...</span> ⏳`;

      try {
        const res = await fetch("/api/gps/generate-roadmap", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ 
            user_id: parseInt(currentUserId), 
            goal: g, 
            duration: d,
            daily_minutes: parseInt(document.getElementById('gpsDailyTimeInput')?.value || '60', 10),
            study_days_per_week: parseInt(document.getElementById('gpsStudyDaysInput')?.value || '5', 10),
            lang: currentLang 
          })
        });
        const resJson = await res.json();
        if (res.ok && resJson.success) {
          loadDashboardData();
        } else {
          const errMsg = resJson.error || (currentLang === 'ar' ? "خطأ غير معروف" : "Unknown error");
          alert((currentLang === 'ar' ? "فشل إنشاء المسار: " : "Failed to generate roadmap: ") + errMsg);
          console.error("Roadmap generation error:", resJson);
        }
      } catch (e) {
        alert(currentLang === 'ar' ? "خطأ في الاتصال بالسيرفر." : "Connection error.");
        console.error(e);
      } finally {
        btn.disabled = false;
        btn.innerHTML = `<span>إنشاء مسار مخصص بموارد وتطبيقات مناسبة</span> 🚀`;
      }
    }

    async function submitProfessionalSimulation(nodeId) {
      const input=document.getElementById(`professionalSubmission-${Number(nodeId)}`);
      const output=document.getElementById(`professionalResult-${Number(nodeId)}`);
      const submission=(input?.value||'').trim();
      if(!submission) return alert('اكتب التسليم المطلوب قبل الإرسال.');
      if(submission.length>30000) return alert('التسليم أطول من الحد المسموح.');
      if(output) output.textContent='جاري تقييم المهمة وفق معايير القبول والـ rubric...';
      try {
        const res=await fetch('/api/gps/professional-assessment/submit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user_id:Number(currentUserId),node_id:Number(nodeId),submission})});
        const data=await readApiResponse(res);
        if(!res.ok||!data.success) throw new Error(data.detail||data.error||'تعذر تقييم المهمة');
        const review=data.review||{};
        const lines=[`الدرجة: ${data.score}% (المطلوب ${data.mastery_threshold}%)`,data.passed?'اجتزت المهمة المهنية.':'لم تصل لمعيار الإتقان بعد.',data.milestone_completed?'اجتزت اختبار المرحلة ومحاكاة العمل؛ تم فتح المرحلة التالية.':'',review.feedback||'',...(review.strengths||[]).map(x=>'نقطة قوة: '+x),...(review.issues||[]).map(x=>'تحتاج تحسين: '+x),...(review.nextActions||[]).map(x=>'الخطوة التالية: '+x),...(review.missingDeliverables||[]).map(x=>'مخرج ناقص: '+x)];
        if(output) output.textContent=lines.join('\\n');
        const node=(window.__activeRoadmapNodes||[]).find(n=>Number(n.id)===Number(nodeId));
        if(node&&node.learning_plan&&node.learning_plan.professionalWorkSimulation){Object.assign(node.learning_plan.professionalWorkSimulation,{lastScore:data.score,passed:!!data.passed,lastAttempt:review});}
        renderRoadmapCards(window.__activeRoadmapNodes||[]);
        loadDashboardData();
      } catch(e) { if(output) output.textContent='تعذر التقييم: '+e.message; }
    }

    let activeDailyAssessment = null;
    function launchDailyRoadmapQuiz(nodeId, dayIndex) {
      const node = (window.__activeRoadmapNodes || []).find(n => Number(n.id) === Number(nodeId));
      if (!node) return;
      const plan = node.learning_plan && typeof node.learning_plan === 'object' ? node.learning_plan : {};
      const days = Array.isArray(plan.dailyLessons) ? plan.dailyLessons : [];
      const day = days[Number(dayIndex)];
      if (!day || !Array.isArray(day.quizQuestions) || !day.quizQuestions.length) return alert('لا يوجد اختبار صالح لهذا اليوم.');
      if (Number(dayIndex) > 0 && !days[Number(dayIndex)-1]?.passed) return alert('اجتز اختبار اليوم السابق أولًا.');
      activeDailyAssessment = {nodeId:Number(nodeId), dayIndex:Number(dayIndex), dayNumber:Number(day.dayNumber)||Number(dayIndex)+1};
      activeBlueprintNodeId = 0;
      const items = day.quizQuestions.map(q => ({...q, question:q.question, options:q.options, correctIndex:Number(q.correctIndex)||0, hint:q.hint||'فكّر في محتوى اليوم وحدد المفهوم الذي يختبره السؤال.'}));
      startGamifiedQuiz(items, 10, 0.8);
    }

    function launchBlueprintChallengeById(nodeId) {
      const node = (window.__activeRoadmapNodes || []).find(n => Number(n.id) === Number(nodeId));
      if (node) launchBlueprintChallenge(node);
    }

    function launchBlueprintChallenge(node) {
      activeBlueprintNodeId = node.id;
      const plan = node.learning_plan && typeof node.learning_plan === 'object' ? node.learning_plan : {};
      const questions = Array.isArray(plan.quizQuestions) ? plan.quizQuestions.filter(q => q && q.question && Array.isArray(q.options) && q.options.length >= 2) : [];
      const items = questions.length ? questions.map(q => ({...q, question:q.question, options:q.options, correctIndex:Number(q.correctIndex)||0, hint:q.hint||'حدد المفهوم الذي يختبره السؤال واستبعد الاختيارات غير المتوافقة.'})) : [{
        question: node.challenge_question,
        options: node.challenge_options,
        correctIndex: node.correct_index
      }];
      const threshold = Number((plan.evaluation || {}).masteryThreshold || 80) / 100;
      startGamifiedQuiz(items, 50, threshold);
    }

    async function evaluateOrSolveHomework() {
      const q = document.getElementById('hwQuestionInput').value.trim();
      const ans = document.getElementById('hwStudentAnswerInput').value.trim();
      const img = document.getElementById('hwImageInput').files[0];
      const btn = document.getElementById('hwSubmitBtn');
      const card = document.getElementById('hwResultCard');

      if (!q) return alert(currentLang === 'ar' ? "يرجى كتابة السؤال." : "Please enter the question.");

      btn.disabled = true;

      const fd = new FormData();
      fd.append("user_id", currentUserId);
      fd.append("questionText", q);
      fd.append("studentSolutionText", ans);
      fd.append("lang", activeVideoLanguage);
      if (img) fd.append("solutionImage", img);

      try {
        const res = await fetch("/api/homework/smart-evaluate", { method: "POST", body: fd });
        const d = await res.json();
        if (d.success && d.result) {
          const r = d.result;
          card.classList.remove('hidden');

          const scoreBox = document.getElementById('hwScoreBox');
          document.getElementById('hwResultVerdict').innerText = r.verdict;
          document.getElementById('hwFeedbackText').innerText = r.detailed_feedback;
          document.getElementById('hwModelSolutionText').innerText = r.model_solution;

          if (r.mode === "CORRECTION") {
            scoreBox.classList.remove('hidden');
            document.getElementById('hwScoreText').innerText = `${r.score}%`;
            document.getElementById('hwXpText').innerText = `+${r.earned_xp} XP`;
            if (r.earned_xp > 0) loadDashboardData();
          } else {
            scoreBox.classList.add('hidden');
          }
        } else {
          alert((currentLang === 'ar' ? "خطأ: " : "Error: ") + (d.error || "Unknown error"));
        }
      } catch (e) {
        alert("Error during evaluation.");
      } finally {
        btn.disabled = false;
      }
    }

    function setTimerDuration(mins) {
      if (isFocusRunning) return;
      focusMinutes = mins;
      focusSeconds = 0;
      updateFocusDisplay();
    }

    function updateFocusDisplay() {
      const m = String(focusMinutes).padStart(2, '0');
      const s = String(focusSeconds).padStart(2, '0');
      document.getElementById('focusDisplayTime').innerText = `${m}:${s}`;
    }

    function startFocusTimer() {
      if (isFocusRunning) {
        clearInterval(focusTimerInterval);
        isFocusRunning = false;
        document.getElementById('timerControlBtn').innerText = currentLang === 'ar' ? "استئناف ▶" : "Resume ▶";
      } else {
        isFocusRunning = true;
        document.getElementById('timerControlBtn').innerText = currentLang === 'ar' ? "إيقاف ⏸" : "Pause ⏸";

        focusTimerInterval = setInterval(() => {
          if (focusSeconds === 0) {
            if (focusMinutes === 0) {
              clearInterval(focusTimerInterval);
              isFocusRunning = false;
              alert(currentLang === 'ar' ? "تمت الجلسة بنجاح!" : "Focus session completed!");
              return;
            }
            focusMinutes--;
            focusSeconds = 59;
          } else {
            focusSeconds--;
          }
          updateFocusDisplay();
        }, 1000);
      }
    }

    function resetFocusTimer() {
      clearInterval(focusTimerInterval);
      isFocusRunning = false;
      focusMinutes = 25;
      focusSeconds = 0;
      updateFocusDisplay();
      document.getElementById('timerControlBtn').innerText = currentLang === 'ar' ? "بدء الجلسة ▶" : "Start ▶";
    }

    function toggleFullscreenIsolation() {
      if (!isDNDActive) {
        if (document.documentElement.requestFullscreen) {
          document.documentElement.requestFullscreen();
        }
        isDNDActive = true;
        document.getElementById('dndBtn').classList.replace('bg-slate-900', 'bg-rose-600');
      } else {
        if (document.exitFullscreen) {
          document.exitFullscreen();
        }
        isDNDActive = false;
        document.getElementById('dndBtn').classList.replace('bg-rose-600', 'bg-slate-900');
      }
    }

    function renderTodos(todos) {
      const container = document.getElementById('todoListContainer');
      const doneCount = todos.filter(t => t.is_done).length;
      document.getElementById('todoStats').innerText = `${doneCount}/${todos.length}`;

      container.innerHTML = todos.map(t => `
        <div class="flex items-center justify-between p-2.5 bg-white rounded-xl border border-amber-100 text-xs">
          <label class="flex items-center gap-2.5 cursor-pointer flex-1 justify-end">
            <span class="${t.is_done ? 'line-through text-slate-400' : 'text-slate-800 font-semibold'}">${t.task}</span>
            <input type="checkbox" onchange="toggleTodoItem(${t.id}, this.checked)" ${t.is_done ? 'checked' : ''} class="w-4 h-4 accent-amber-500 rounded">
          </label>
        </div>
      `).join('');
    }

    async function addNewTodo() {
      const task = document.getElementById('newTodoInput').value.trim();
      if (!task) return;
      await fetch("/api/todos/add", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: parseInt(currentUserId), task: task })
      });
      document.getElementById('newTodoInput').value = "";
      loadDashboardData();
    }

    async function toggleTodoItem(id, checked) {
      await fetch("/api/todos/toggle", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ todo_id: id, is_done: checked ? 1 : 0 })
      });
      loadDashboardData();
    }

    function startGamifiedQuiz(items, rewardXP = 50, passThreshold = 0) {
      currentQuizItems = Array.isArray(items) ? items.map(q => ({...q})) : [];
      currentQuizReview = [];
      currentQuizHintUsed = false;
      currentQuestionIdx = 0;
      currentCorrectAnswers = 0;
      currentPassThreshold = Math.max(0, Math.min(1, Number(passThreshold) || 0));
      livesRemaining = 3;
      comboStreak = 0;

      updateLivesDisplay();
      document.getElementById('comboText').innerText = `1x`;
      document.getElementById('lockdownOverlay').classList.remove('hidden');
      document.getElementById('lockdownOverlay').classList.add('flex', 'overlay-visible');
      isExamActive = true;

      renderCurrentGameQuestion();
    }

    function updateLivesDisplay() {
      document.getElementById('heart1').innerText = livesRemaining >= 1 ? '❤️' : '💔';
      document.getElementById('heart2').innerText = livesRemaining >= 2 ? '❤️' : '💔';
      document.getElementById('heart3').innerText = livesRemaining >= 3 ? '❤️' : '💔';
    }

    function renderCurrentGameQuestion() {
      if (currentQuestionIdx >= currentQuizItems.length) {
        finishGamifiedExam(true);
        return;
      }

      const q = currentQuizItems[currentQuestionIdx];
      document.getElementById('questionStepTag').innerText = `${currentQuestionIdx + 1} / ${currentQuizItems.length}`;
      document.getElementById('gameQuestionText').innerText = q.question;
      const hintText=document.getElementById('quizHintText'); if(hintText){hintText.textContent=q.hint||'فكّر في المفهوم الذي يختبره السؤال، ثم استبعد الاختيارات التي لا تفسّر الحالة.';hintText.classList.add('hidden');}
      const hintButton=document.getElementById('quizHintButton'); if(hintButton){hintButton.disabled=false;hintButton.classList.remove('opacity-50');hintButton.textContent='💡 تلميح يساعدك على التفكير';}
      const feedback=document.getElementById('quizAnswerFeedback'); if(feedback){feedback.classList.add('hidden');feedback.textContent='';}

      const colorSchemes = [
        { bg: "from-blue-600 to-cyan-600", symbol: "▲" },
        { bg: "from-emerald-600 to-teal-600", symbol: "◆" },
        { bg: "from-amber-500 to-yellow-500 text-slate-950", symbol: "■" },
        { bg: "from-violet-600 to-fuchsia-600", symbol: "●" }
      ];

      const optContainer = document.getElementById('gameOptionsContainer');
      optContainer.innerHTML = q.options.map((opt, i) => `
        <button onclick="handleOptionChoice(${i})" class="flex items-center gap-3.5 p-4 rounded-2xl bg-gradient-to-r ${colorSchemes[i].bg} text-white font-black text-sm shadow-lg transform hover:-translate-y-1 transition active:scale-95 text-right">
          <span class="w-8 h-8 rounded-xl bg-black/20 flex items-center justify-center text-sm font-black shrink-0">${colorSchemes[i].symbol}</span>
          <span class="flex-1">${opt}</span>
        </button>
      `).join('');

      questionTimer = 60;
      clearInterval(timerInterval);
      updateTimerUI();

      timerInterval = setInterval(() => {
        questionTimer--;
        updateTimerUI();
        if (questionTimer <= 0) {
          clearInterval(timerInterval);
          handleTimeOut();
        }
      }, 1000);
    }

    function toggleCurrentQuizHint() {
      const q=currentQuizItems[currentQuestionIdx]; if(!q) return;
      const box=document.getElementById('quizHintText'); const btn=document.getElementById('quizHintButton');
      if(!box||!btn) return;
      box.classList.toggle('hidden');
      if(!box.classList.contains('hidden')) { btn.textContent='💡 تم عرض التلميح'; btn.disabled=true; btn.classList.add('opacity-50'); q.hintUsed=true; }
    }

    function updateTimerUI() {
      document.getElementById('timerText').innerText = `${questionTimer}s`;
      const pct = (questionTimer / 60) * 100;
      document.getElementById('timerBar').style.width = `${pct}%`;
    }

    function handleOptionChoice(chosenIdx) {
      clearInterval(timerInterval);
      const q = currentQuizItems[currentQuestionIdx];
      const correct = chosenIdx === Number(q.correctIndex);
      currentQuizReview.push({question:q.question, options:q.options||[], chosenIndex:chosenIdx, correctIndex:Number(q.correctIndex)||0, correct, hintUsed:!!q.hintUsed, explanation:q.explanation||'', solutionSteps:Array.isArray(q.solutionSteps)?q.solutionSteps:[], remediation:q.remediation||'', conceptTested:q.conceptTested||''});
      if (correct) { currentCorrectAnswers++; comboStreak++; document.getElementById('comboText').innerText = `${comboStreak + 1}x`; }
      else { comboStreak = 0; document.getElementById('comboText').innerText = `1x`; }
      const feedback=document.getElementById('quizAnswerFeedback');
      if(feedback){feedback.classList.remove('hidden');feedback.innerHTML=`<b class="${correct?'text-emerald-300':'text-amber-300'}">${correct?'إجابة صحيحة':'تم تسجيل إجابتك'}</b><p class="mt-1 text-slate-300 text-xs">${correct?'أكمل بنفس التركيز.':'مش مشكلة؛ هنراجع السبب بعد نهاية الاختبار.'}</p>`;}
      document.querySelectorAll('#gameOptionsContainer button').forEach((b,i)=>{b.disabled=true;b.classList.add('opacity-70');if(i===Number(q.correctIndex))b.classList.add('ring-2','ring-emerald-300');});
      setTimeout(()=>{currentQuestionIdx++;renderCurrentGameQuestion();},850);
    }

    function handleTimeOut() {
      clearInterval(timerInterval);
      const q=currentQuizItems[currentQuestionIdx];
      if(q) currentQuizReview.push({question:q.question,options:q.options||[],chosenIndex:-1,correctIndex:Number(q.correctIndex)||0,correct:false,timeout:true,hintUsed:!!q.hintUsed,explanation:q.explanation||'',solutionSteps:Array.isArray(q.solutionSteps)?q.solutionSteps:[],remediation:q.remediation||'',conceptTested:q.conceptTested||''});
      currentQuestionIdx++;
      renderCurrentGameQuestion();
    }

    async function finishGamifiedExam(passed) {
      clearInterval(timerInterval);
      if (currentPassThreshold > 0 && currentQuizItems.length > 0) {
        passed = (currentCorrectAnswers / currentQuizItems.length) >= currentPassThreshold;
      }
      const roadmapAssessment = currentPassThreshold > 0;
      abortExam();
      if (roadmapAssessment) {
        const scorePct = Math.round(currentCorrectAnswers / Math.max(1, currentQuizItems.length) * 100);
        const summary=(currentLang === 'ar' ? `نتيجتك: ${currentCorrectAnswers}/${currentQuizItems.length} (${scorePct}%). ` : `Score: ${currentCorrectAnswers}/${currentQuizItems.length} (${scorePct}%). `) + (passed ? (currentLang === 'ar' ? 'اجتزت المرحلة!' : 'Milestone passed!') : (currentLang === 'ar' ? 'لم تصل لمعيار الإتقان بعد؛ راجع نقاط التحسين ثم أعد المحاولة.' : 'Mastery threshold not reached; review improvement points and retry.'));
        const reviewHtml=currentQuizReview.map((r,i)=>`<div class="rounded-xl border ${r.correct?'border-emerald-200':'border-amber-200'} p-3 mb-2"><b>${i+1}. ${escapeHtml(r.question)}</b><p class="mt-1 text-xs">إجابتك: ${r.chosenIndex<0?'لم تتم الإجابة':escapeHtml((r.options||[])[r.chosenIndex]||'')}</p><p class="text-xs text-emerald-800 mt-1">الإجابة الصحيحة: ${escapeHtml((r.options||[])[r.correctIndex]||'—')}</p><p class="text-xs mt-2">${escapeHtml(r.explanation||'')}</p>${(r.solutionSteps||[]).length?`<ol class="list-decimal pr-5 mt-2 text-xs">${r.solutionSteps.map(st=>`<li>${escapeHtml(st)}</li>`).join('')}</ol>`:''}<p class="text-xs text-blue-800 mt-2">الخطوة المقترحة: ${escapeHtml(r.remediation||'راجع المفهوم المرتبط بالسؤال.')}</p></div>`).join('');
        const reviewModal=document.getElementById('roadmapAssessmentReview');
        if(reviewModal){document.getElementById('roadmapAssessmentSummary').textContent=summary;document.getElementById('roadmapAssessmentDetails').innerHTML=reviewHtml;reviewModal.classList.remove('hidden');reviewModal.classList.add('flex');} else { alert(summary); }
      }
      if (activeDailyAssessment) {
        const daily = {...activeDailyAssessment};
        activeDailyAssessment = null;
        try {
          const saveRes = await fetch('/api/gps/daily-assessment-result', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({user_id:Number(currentUserId), node_id:daily.nodeId, day_index:daily.dayIndex, score:Math.round(currentCorrectAnswers/Math.max(1,currentQuizItems.length)*100), passed:passed, answers:currentQuizReview.map(r=>({chosenIndex:r.chosenIndex, correct:!!r.correct, hintUsed:!!r.hintUsed}))})});
          const saveData = await readApiResponse(saveRes);
          if (!saveRes.ok || !saveData.success) throw new Error(saveData.detail || 'Could not save daily result');
          const node = (window.__activeRoadmapNodes||[]).find(n=>Number(n.id)===daily.nodeId);
          if (node && node.learning_plan && Array.isArray(node.learning_plan.dailyLessons)) {
            node.learning_plan.dailyLessons[daily.dayIndex].lastAttemptScore = saveData.score;
            node.learning_plan.dailyLessons[daily.dayIndex].passed = !!saveData.passed;
          }
          const reviewModal=document.getElementById('roadmapAssessmentReview');
          if(reviewModal){const summary=document.getElementById('roadmapAssessmentSummary');if(summary)summary.textContent=`اختبار اليوم ${daily.dayNumber}: ${saveData.score}% — ${saveData.passed?'اجتزت اختبار اليوم':'راجع المحتوى ثم أعد المحاولة'}`;}
          renderRoadmapCards(window.__activeRoadmapNodes||[]);
        } catch (e) { console.error('Daily assessment persistence failed',e); alert('لم نتمكن من حفظ نتيجة اختبار اليوم. حاول مرة أخرى.'); }
        loadDashboardData();
        return;
      }
      const earnedXP = passed ? 50 : 0;
      const resultResponse = await fetch("/api/user/record-result", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ 
          user_id: parseInt(currentUserId), 
          earned_xp: earnedXP, 
          passed: passed ? 1 : 0,
          node_id: activeBlueprintNodeId 
        })
      });
      let progressionResult = {};
      try { progressionResult = await readApiResponse(resultResponse); } catch (_) {}
      if (passed && progressionResult.work_simulation_required) {
        const modalSummary=document.getElementById('roadmapAssessmentSummary');
        if(modalSummary) modalSummary.textContent += ' اجتزت اختبار الفهم، لكن لن تُفتح المرحلة التالية قبل اجتياز محاكاة العمل وتسليم مخرجاتها.';
      }
      activeBlueprintNodeId = 0;
      loadDashboardData();
    }

    function closeRoadmapAssessmentReview(){const el=document.getElementById('roadmapAssessmentReview');if(el){el.classList.add('hidden');el.classList.remove('flex');}}

    function abortExam() {
      clearInterval(timerInterval);
      isExamActive = false;
      document.getElementById('lockdownOverlay').classList.add('hidden');
      document.getElementById('lockdownOverlay').classList.remove('flex', 'overlay-visible');
    }

    // ---------------------------------------------------------
    // Study Room: Focus + audio mixer
    // ---------------------------------------------------------
    let studyFocusEnabled = true;
    let studyAudioType = 'none';
    let noiseCtx = null, noiseSource = null, noiseGain = null;
    const quranBase = 'https://server6.mp3quran.net/akdr/';
    const QURAN_SURAHS = [
      'الفاتحة','البقرة','آل عمران','النساء','المائدة','الأنعام','الأعراف','الأنفال','التوبة','يونس','هود','يوسف','الرعد','إبراهيم','الحجر','النحل','الإسراء','الكهف','مريم','طه','الأنبياء','الحج','المؤمنون','النور','الفرقان','الشعراء','النمل','القصص','العنكبوت','الروم','لقمان','السجدة','الأحزاب','سبأ','فاطر','يس','الصافات','ص','الزمر','غافر','فصلت','الشورى','الزخرف','الدخان','الجاثية','الأحقاف','محمد','الفتح','الحجرات','ق','الذاريات','الطور','النجم','القمر','الرحمن','الواقعة','الحديد','المجادلة','الحشر','الممتحنة','الصف','الجمعة','المنافقون','التغابن','الطلاق','التحريم','الملك','القلم','الحاقة','المعارج','نوح','الجن','المزمل','المدثر','القيامة','الإنسان','المرسلات','النبأ','النازعات','عبس','التكوير','الانفطار','المطففين','الانشقاق','البروج','الطارق','الأعلى','الغاشية','الفجر','البلد','الشمس','الليل','الضحى','الشرح','التين','العلق','القدر','البينة','الزلزلة','العاديات','القارعة','التكاثر','العصر','الهمزة','الفيل','قريش','الماعون','الكوثر','الكافرون','النصر','المسد','الإخلاص','الفلق','الناس'
    ];
    function initQuranSurahs(){
      const select=document.getElementById('quranSurah');
      if(!select || select.options.length) return;
      select.innerHTML=QURAN_SURAHS.map((name,i)=>`<option value="${i+1}">${i+1}. سورة ${name}</option>`).join('');
    }
    function changeQuranSurah(delta){
      initQuranSurahs();
      const select=document.getElementById('quranSurah');
      if(!select) return;
      const next=Math.max(1,Math.min(114,Number(select.value||1)+delta));
      select.value=String(next);
      setQuranSetting();
    }
    function studyRoomUserId(){ return parseInt(currentUserId||0); }
    async function loadStudyRoomSettings(){
      initQuranSurahs();
      if(!studyRoomUserId()) return;
      try{const r=await fetch(`/api/study/room-settings?user_id=${studyRoomUserId()}`);const d=await r.json();const s=d.settings||{};studyFocusEnabled=!!s.focus_mode;studyAudioType=s.audio_type||'none';document.getElementById('studyAudioVolume').value=Math.round((s.audio_volume||.35)*100);document.getElementById('studyAudioVolumeText').innerText=document.getElementById('studyAudioVolume').value+'%';document.getElementById('focusModeToggle').innerText=studyFocusEnabled?'Focus ON':'Focus OFF';if(s.quran_reciter)document.getElementById('quranReciter').value=s.quran_reciter;if(s.quran_surah)document.getElementById('quranSurah').value=s.quran_surah;applyStudyAudio(false)}catch(e){console.warn(e)}}
    async function saveStudyRoomSettings(){if(!studyRoomUserId())return;await fetch('/api/study/room-settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user_id:studyRoomUserId(),focus_mode:studyFocusEnabled,audio_type:studyAudioType,audio_volume:Number(document.getElementById('studyAudioVolume').value)/100,quran_reciter:document.getElementById('quranReciter').value,quran_surah:Number(document.getElementById('quranSurah').value),noise_type:studyAudioType})})}
    function toggleStudyFocus(){studyFocusEnabled=!studyFocusEnabled;document.getElementById('focusModeToggle').innerText=studyFocusEnabled?'Focus ON':'Focus OFF';document.getElementById('focusModeToggle').className=studyFocusEnabled?'px-4 py-2 rounded-xl bg-emerald-500 text-slate-950 font-black text-xs':'px-4 py-2 rounded-xl bg-slate-700 text-white font-black text-xs';saveStudyRoomSettings()}
    function setStudyVolume(v){document.getElementById('studyAudioVolumeText').innerText=v+'%';if(noiseGain)noiseGain.gain.value=Number(v)/100;const a=document.getElementById('studyQuranAudio');if(a)a.volume=Number(v)/100;saveStudyRoomSettings()}
    function stopNoise(){if(noiseSource){try{noiseSource.stop()}catch(e){}noiseSource=null}if(noiseCtx){try{noiseCtx.close()}catch(e){}noiseCtx=null}noiseGain=null}
    function startNoise(kind){stopNoise();noiseCtx=new (window.AudioContext||window.webkitAudioContext)();noiseSource=noiseCtx.createBufferSource();const seconds=2,sr=noiseCtx.sampleRate,buf=noiseCtx.createBuffer(1,sr*seconds,sr),data=buf.getChannelData(0);let last=0;for(let i=0;i<data.length;i++){const white=Math.random()*2-1;if(kind==='brown'){last=(last+0.02*white);data[i]=last*3}else if(kind==='rain'){const hit=Math.random()<0.018?Math.random()*1.5:0;data[i]=(white*0.06)+hit}else if(kind==='ocean'){data[i]=Math.sin(i/sr*1.3)*0.08+white*0.03}else if(kind==='forest'){data[i]=white*0.035+Math.sin(i/sr*2.1)*0.02}else if(kind==='cafe'){data[i]=white*0.05+Math.sin(i/sr*55)*0.01}else data[i]=white*0.12}noiseSource.buffer=buf;noiseSource.loop=true;noiseGain=noiseCtx.createGain();noiseGain.gain.value=Number(document.getElementById('studyAudioVolume').value)/100;noiseSource.connect(noiseGain).connect(noiseCtx.destination);noiseSource.start()}
    function quranUrl(reciter,surah){const servers={1:'https://server6.mp3quran.net/akdr/',7:'https://server7.mp3quran.net/basit/',9:'https://server7.mp3quran.net/minsh/',12:'https://server7.mp3quran.net/husr/',253:'https://server14.mp3quran.net/islam/Rewayat-Hafs-A-n-Assem/'};const base=servers[String(reciter)]||servers['1'];return base+String(surah).padStart(3,'0')+'.mp3'}
    function setQuranSetting(){if(studyAudioType==='quran')applyStudyAudio(true);saveStudyRoomSettings()}
    function setStudyAudio(type){studyAudioType=type;applyStudyAudio(true);saveStudyRoomSettings()}
    function applyStudyAudio(playNow=true){const q=document.getElementById('studyQuranAudio'), qc=document.getElementById('quranControls'), st=document.getElementById('studyAudioStatus');stopNoise();q.pause();q.classList.add('hidden');qc.classList.add('hidden');if(studyAudioType==='none'){st.innerText='الصوت متوقف.';return}if(studyAudioType==='quran'){qc.classList.remove('hidden');q.classList.remove('hidden');q.src=quranUrl(document.getElementById('quranReciter').value,document.getElementById('quranSurah').value);q.volume=Number(document.getElementById('studyAudioVolume').value)/100;st.innerText='القرآن يعمل بشكل مستقل عن Focus Mode.';if(playNow)q.play().catch(()=>{})}else{startNoise(studyAudioType);st.innerText='Study Audio يعمل مع Focus Mode.'}}
    // Browser-level notification blocking is intentionally not faked: Focus Mode controls S-Tesseract notifications.
    // The native Windows/Android uploader can later request OS Do-Not-Disturb permissions.
    initQuranSurahs();
    setTimeout(loadStudyRoomSettings,1200);

    // ---------------------------------------------------------
    // Video Studio — Fast / AI / YouTube / Both
    // ---------------------------------------------------------
    let videoPerspectiveJobs = {};
    let activeVideoSourceJobId = null;
    let activeVideoLanguage = 'ar';
    let activeVideoMode = 'fast';
    let videoGenerationPoller = null;
    let activeVideoData = {};
    let activePerspectiveIndex = 1;

    function setVideoLanguageState(lang) {
      activeVideoLanguage = lang === 'en' ? 'en' : 'ar';
      const ar=document.getElementById('videoLangAr'), en=document.getElementById('videoLangEn');
      if (!ar || !en) return;
      ar.className = activeVideoLanguage==='ar' ? 'video-lang-btn p-3 bg-indigo-600 text-white rounded-2xl font-black text-xs shadow-lg transition' : 'video-lang-btn p-3 bg-slate-900 text-slate-300 rounded-2xl font-black text-xs transition';
      en.className = activeVideoLanguage==='en' ? 'video-lang-btn p-3 bg-indigo-600 text-white rounded-2xl font-black text-xs shadow-lg transition' : 'video-lang-btn p-3 bg-slate-900 text-slate-300 rounded-2xl font-black text-xs transition';
    }
    function selectVideoLanguage(lang){ setVideoLanguageState(lang); }

    function selectVideoMode(mode){
      activeVideoMode = ['fast','ai','youtube','both'].includes(mode) ? mode : 'fast';
      const names=['fast','ai','youtube','both'];
      names.forEach(n=>{
        const el=document.getElementById('videoMode'+n.charAt(0).toUpperCase()+n.slice(1));
        if(!el) return;
        el.className = 'video-mode-card text-right p-4 rounded-2xl border-2 transition ' + (n===activeVideoMode ? 'border-emerald-400 bg-emerald-50' : 'border-slate-200 bg-white');
      });
      const stage=document.getElementById('videoJobStage');
      if(stage) stage.innerText = ({fast:'عايز شرح سريع',ai:'عايز شرح أذكى',youtube:'عايز فيديوهات جاهزة',both:'عايز الاتنين'})[activeVideoMode];
    }

    function setVideoTabState(activeIndex){
      for(let i=1;i<=3;i++){
        const el=document.getElementById(`persBtn${i-1}`); if(!el) continue;
        el.className=i===activeIndex ? 'pers-tab p-3 bg-indigo-600 text-white rounded-2xl font-black text-xs shadow-lg transition text-center' : 'pers-tab p-3 bg-slate-900 text-slate-300 rounded-2xl font-black text-xs transition text-center';
      }
    }
    function selectVideoPerspective(perspective){ activePerspectiveIndex=Number(perspective)||1; setVideoTabState(activePerspectiveIndex); }

    async function readApiResponse(res){
      const ct=res.headers.get('content-type')||'';
      if(ct.includes('application/json')){
        const data=await res.json();
        if(!res.ok) throw new Error(data.error||data.detail||`HTTP ${res.status}`);
        return data;
      }
      const text=await res.text();
      if(!res.ok) throw new Error(`HTTP ${res.status}: ${text.slice(0,300)}`);
      try{return JSON.parse(text)}catch{return {success:true,text}}
    }

    function friendlyVideoStage(stage){
      const s=String(stage||'');
      if(activeVideoMode==='fast'){
        if(s.includes('جاهز')) return 'الشرح السريع جاهز ✅';
        if(s.includes('المونتاج')) return 'جاري إنهاء الشرح السريع...';
        if(s.includes('مشهد')) return 'جاري إعداد الشرح السريع...';
        if(s.includes('استخراج') || s.includes('PDF')) return 'جاري قراءة المادة وتجهيز الشرح السريع...';
        return 'جاري إعداد الشرح السريع...';
      }
      if(activeVideoMode==='youtube'){
        if(s.includes('جاهزة') || s.includes('جاهز')) return 'فيديوهات YouTube جاهزة ✅';
        return 'جاري تجهيز فيديوهات مناسبة...';
      }
      if(activeVideoMode==='both'){
        if(s.includes('جاهز')) return 'الشرح والفيديوهات جاهزين ✅';
        if(s.includes('YouTube')) return 'جاري تجهيز فيديوهات مناسبة...';
        return 'جاري إعداد الشرح الذكي...';
      }
      if(s.includes('جاهز')) return 'الشرح الذكي جاهز ✅';
      return 'جاري إعداد الشرح الذكي...';
    }

    function showVideoJobProgress(percent,stage){
      const box=document.getElementById('videoJobBox'),bar=document.getElementById('videoJobProgress'),label=document.getElementById('videoJobPercent'),st=document.getElementById('videoJobStage');
      if(!box||!bar||!label||!st)return;
      box.classList.remove('hidden'); const p=Math.max(0,Math.min(100,Number(percent)||0)); bar.style.width=p+'%'; label.innerText=Math.round(p)+'%'; st.innerText=friendlyVideoStage(stage);
    }

    function renderYoutubeResults(links){
      const box=document.getElementById('youtubeResultsBox'), c=document.getElementById('youtubeResultsContainer');
      if(!box||!c)return;
      if(!Array.isArray(links)||!links.length){box.classList.add('hidden');c.innerHTML='';return;}
      box.classList.remove('hidden');
      c.innerHTML=links.map(x=>`<a href="${escapeHtml(x.url)}" target="_blank" rel="noopener noreferrer" class="block p-3 rounded-xl bg-slate-950 border border-slate-800 hover:border-amber-400 transition"><div class="text-xs font-bold text-white">▶️ ${escapeHtml(x.title||'YouTube')}</div><div class="text-[10px] text-slate-500 mt-1">فتح بحث الفيديوهات على YouTube ↗</div></a>`).join('');
    }

    async function generateInteractiveVideo(){
      const file=document.getElementById('pdfVideoInput')?.files?.[0];
      if(!file) return alert(currentLang==='ar'?'اختر ملف PDF أولاً.':'Select a PDF file first.');
      const btn=document.getElementById('genVideoBtn'); if(btn) btn.disabled=true;
      showVideoJobProgress(1,'جاري تجهيز الشرح...');
      const fd=new FormData(); fd.append('pdfFile',file); fd.append('lang',activeVideoLanguage); fd.append('perspective',String(activePerspectiveIndex)); fd.append('mode',activeVideoMode);
      let jobStarted=false;
      try{
        const d=await readApiResponse(await fetch('/api/generate-interactive-video',{method:'POST',body:fd,credentials:'same-origin'}));
        jobStarted=true; activeVideoSourceJobId=d.jobId;
        showVideoJobProgress(2, activeVideoMode==='fast'?'جاري إعداد الشرح السريع...':activeVideoMode==='youtube'?'جاري تجهيز فيديوهات مناسبة...':'جاري إعداد الشرح الذكي...');
        await pollVideoJob(d.jobId,activePerspectiveIndex);
      }catch(e){
        console.error(e);
        showVideoJobProgress(0,jobStarted?'تعذر الاتصال بالسيرفر مؤقتًا — المهمة قد تكون ما زالت تعمل':'فشل بدء المهمة');
        alert(jobStarted ? 'السيرفر لم يستجب مؤقتًا (502/503/504). المهمة قد تكون ما زالت تعمل، سنحاول المتابعة تلقائيًا.' : 'فشل بدء الفيديو: '+e.message);
      }finally{if(btn){btn.disabled=false;btn.innerText='إنشاء الفيديو 🎬';}}
    }

    async function pollVideoJob(jobId,perspective){
      if(videoGenerationPoller){clearTimeout(videoGenerationPoller);videoGenerationPoller=null;}
      let transientFailures=0, lastProgress=2, lastStage='جاري إنشاء الفيديو...'; const maxTransientFailures=30;
      return new Promise((resolve,reject)=>{
        const check=async()=>{
          try{
            const res=await fetch(`/api/video-job/${encodeURIComponent(jobId)}`,{cache:'no-store',credentials:'same-origin'});
            if([502,503,504].includes(res.status)){
              transientFailures++; showVideoJobProgress(lastProgress,`السيرفر مشغول — إعادة الاتصال ${transientFailures}/${maxTransientFailures}`);
              if(transientFailures>maxTransientFailures){reject(new Error(`HTTP ${res.status}: server unavailable`));return;}
              videoGenerationPoller=setTimeout(check,Math.min(12000,2500+transientFailures*500)); return;
            }
            if(res.status===404){showVideoJobProgress(lastProgress,'المهمة لسه بتتسجل على السيرفر... إعادة المحاولة');videoGenerationPoller=setTimeout(check,3000);return;}
            const d=await readApiResponse(res); transientFailures=0; lastProgress=Number(d.progress)||lastProgress; lastStage=d.stage||lastStage;
            showVideoJobProgress(lastProgress,lastStage);
            if(d.status==='completed'){videoGenerationPoller=null;renderConceptTheater(d.data||{},d.videoUrl||'',perspective,d.mode||activeVideoMode,d.youtube||[]);resolve(d);return;}
            if(d.status==='failed'){videoGenerationPoller=null;reject(new Error(d.error||'Video generation failed.'));return;}
            videoGenerationPoller=setTimeout(check,4000);
          }catch(e){
            transientFailures++; console.warn('Video job polling failed:',e); showVideoJobProgress(lastProgress,`إعادة الاتصال بالسيرفر ${transientFailures}/${maxTransientFailures}`);
            if(transientFailures>maxTransientFailures){reject(e);return;} videoGenerationPoller=setTimeout(check,Math.min(12000,2500+transientFailures*500));
          }
        }; check();
      });
    }

    function escapeHtml(value){return String(value??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));}

    function renderConceptTheater(data,videoUrl,perspective,mode,youtube){
      activeVideoData=data||{}; activePerspectiveIndex=perspective||1;
      document.getElementById('videoPlayerBox')?.classList.remove('hidden');
      document.getElementById('videoConceptTitle').innerText=activeVideoData.conceptTitle||'...';
      document.getElementById('videoModeTitle').innerText=({fast:'شرح سريع',ai:'شرح أذكى',youtube:'فيديوهات جاهزة',both:'الاتنين'})[mode]||mode||'...';
      document.getElementById('videoPerspectiveTitle').innerText=activeVideoData.perspectiveTitle||({1:'الأساس العلمي والشرح',2:'بنستخدمها في الحياة',3:'الامتحان والتفكير العالي'}[Number(perspective)]||'طريقة الشرح');
      const video=document.getElementById('generatedLessonVideo'), wrap=document.getElementById('videoElementWrap');
      if(videoUrl){wrap?.classList.remove('hidden');video.src=videoUrl;video.load();}else{video?.removeAttribute('src');wrap?.classList.add('hidden');}
      renderYoutubeResults(youtube?.length?youtube:(activeVideoData.youtube||[]));
      const insights=document.getElementById('keyInsightsContainer'); insights.innerHTML=(activeVideoData.keyInsights||[]).map((x,i)=>`<div class="flex items-start gap-2.5 p-3 bg-slate-950 rounded-xl border border-slate-800"><span class="w-5 h-5 rounded-full bg-amber-400 text-slate-950 font-black text-xs flex items-center justify-center shrink-0">${i+1}</span><p class="leading-relaxed font-medium">${escapeHtml(x)}</p></div>`).join('');
      const recap=document.getElementById('videoRecapContainer'); recap.innerHTML=(activeVideoData.recap||[]).map(x=>`<div class="p-3 rounded-xl bg-slate-950 border border-slate-800 leading-relaxed">${escapeHtml(x)}</div>`).join('');
      const cs=activeVideoData.engineeringCaseStudy||{}; document.getElementById('csTitle').innerText=cs.title||'تطبيق واقعي'; document.getElementById('csProblem').innerText=cs.problem||''; document.getElementById('csSolution').innerText=cs.solution||'';
      showVideoJobProgress(100,'الفيديو جاهز ✅');
    }

    function startVideoLockdownQuiz(){
      if(!activeVideoData || !activeVideoData.quizQuestions || !activeVideoData.quizQuestions.length){return alert(currentLang==='ar'?'لا يوجد كويز متاح لهذا الفيديو.':'No quiz is available for this video.');}
      activeBlueprintNodeId=0; startGamifiedQuiz(activeVideoData.quizQuestions,50);
    }
    setTimeout(()=>{setVideoLanguageState('ar');selectVideoMode('fast');setVideoTabState(1)},0);


    // ---------------------------------------------------------
    // Calendar + Telegram
    // ---------------------------------------------------------
    const CAL_START_HOUR = 6;
    const CAL_END_HOUR = 23;
    const CAL_ROW_HEIGHT = 54;
    const AR_DAY_NAMES = ["الأحد", "الإثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت"];
    const EN_DAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
    const AR_MONTH_NAMES = ["يناير","فبراير","مارس","أبريل","مايو","يونيو","يوليو","أغسطس","سبتمبر","أكتوبر","نوفمبر","ديسمبر"];
    const EN_MONTH_NAMES = ["January","February","March","April","May","June","July","August","September","October","November","December"];

    let calWeekStart = getWeekStart(new Date());
    let calAllEvents = [];

    function getWeekStart(d) {
      const nd = new Date(d);
      nd.setHours(0, 0, 0, 0);
      nd.setDate(nd.getDate() - nd.getDay());
      return nd;
    }

    function fmtDateKey(d) {
      const y = d.getFullYear();
      const m = String(d.getMonth() + 1).padStart(2, '0');
      const day = String(d.getDate()).padStart(2, '0');
      return `${y}-${m}-${day}`;
    }

    function calShiftWeek(dir) {
      calWeekStart.setDate(calWeekStart.getDate() + dir * 7);
      calWeekStart = new Date(calWeekStart);
      renderWeekCalendar();
    }

    async function fetchScheduleEvents() {
      if (!currentUserId) return;
      try {
        const res = await fetch(`/api/schedule/${currentUserId}/list`);
        const d = await res.json();
        calAllEvents = d.items || [];
      } catch (e) {
        console.error(e);
        calAllEvents = [];
      }
    }

    function calGoToday() {
      calWeekStart = getWeekStart(new Date());
      renderWeekCalendar();
    }

    function calRangeLabelText() {
      const last = new Date(calWeekStart);
      last.setDate(last.getDate() + 6);
      const names = currentLang === "ar" ? AR_MONTH_NAMES : EN_MONTH_NAMES;
      if (calWeekStart.getMonth() === last.getMonth()) {
        return `${names[calWeekStart.getMonth()]} ${calWeekStart.getFullYear()}`;
      }
      return `${names[calWeekStart.getMonth()]} – ${names[last.getMonth()]} ${last.getFullYear()}`;
    }

    function eventSourceMeta(source) {
      if (source === "telegram_pdf") return { icon: "📄", label: "PDF" };
      if (source === "telegram") return { icon: "✈️", label: "TG" };
      return { icon: "✚", label: "Manual" };
    }

    function escapeHtmlCalendar(value) {
      return String(value ?? "").replace(/[&<>"']/g, ch => ({
        "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"
      }[ch]));
    }

    function calEventChip(e) {
      const meta = eventSourceMeta(e.source);
      return `
        <div onclick="handleEventClick(${e.id})"
             class="px-2 py-1.5 rounded-lg text-[10px] font-bold cursor-pointer truncate border border-white/5
             ${e.is_completed ? "bg-emerald-500/20 text-emerald-200 line-through" : "bg-blue-500/20 text-blue-100"}"
             title="${escapeHtmlCalendar(e.title)}">
          ${meta.icon} ${escapeHtmlCalendar(e.title)}
        </div>
      `;
    }

    function renderMiniMonth() {
      const box = document.getElementById("calMiniMonth");
      const title = document.getElementById("calMiniMonthTitle");
      if (!box || !title) return;

      const anchor = new Date(calWeekStart);
      const monthNames = currentLang === "ar" ? AR_MONTH_NAMES : EN_MONTH_NAMES;
      title.innerText = `${monthNames[anchor.getMonth()]} ${anchor.getFullYear()}`;

      const first = new Date(anchor.getFullYear(), anchor.getMonth(), 1);
      const offset = first.getDay();
      const daysInMonth = new Date(anchor.getFullYear(), anchor.getMonth() + 1, 0).getDate();
      const prevDays = new Date(anchor.getFullYear(), anchor.getMonth(), 0).getDate();
      const todayKey = fmtDateKey(new Date());

      const names = currentLang === "ar"
        ? ["ح","ن","ث","ر","خ","ج","س"]
        : ["S","M","T","W","T","F","S"];

      let html = names.map(n =>
        `<div class="text-[8px] text-slate-600 font-black text-center py-1">${n}</div>`
      ).join("");

      for (let i = 0; i < 42; i++) {
        const dayNum = i - offset + 1;
        let dateObj;
        let label;
        if (dayNum < 1) {
          const d = prevDays + dayNum;
          dateObj = new Date(anchor.getFullYear(), anchor.getMonth() - 1, d);
          label = d;
        } else if (dayNum > daysInMonth) {
          const d = dayNum - daysInMonth;
          dateObj = new Date(anchor.getFullYear(), anchor.getMonth() + 1, d);
          label = d;
        } else {
          dateObj = new Date(anchor.getFullYear(), anchor.getMonth(), dayNum);
          label = dayNum;
        }

        const key = fmtDateKey(dateObj);
        const hasEvent = calAllEvents.some(e => e.target_date === key);
        let cls = "calendar-mini-day cursor-pointer hover:bg-slate-800";
        if (key === todayKey) cls += " today";
        if (hasEvent) cls += " has-event";

        html += `<div class="${cls}" onclick="calJumpToDate('${key}')">${label}</div>`;
      }
      box.innerHTML = html;
    }

    function calJumpToDate(key) {
      calWeekStart = getWeekStart(new Date(`${key}T00:00:00`));
      renderWeekCalendar();
    }

    function renderAgenda() {
      const box = document.getElementById("calAgendaList");
      const nextBox = document.getElementById("calNextReminder");
      if (!box) return;

      const upcoming = [...calAllEvents]
        .filter(e => e.target_date && !e.is_completed)
        .sort((a,b) => (`${a.target_date} ${a.target_time || "99:99"}`).localeCompare(`${b.target_date} ${b.target_time || "99:99"}`));

      if (!upcoming.length) {
        box.innerHTML = `<div class="text-center py-8 text-xs text-slate-500">No upcoming events</div>`;
        if (nextBox) nextBox.innerText = "—";
        return;
      }

      box.innerHTML = upcoming.slice(0, 10).map(e => {
        const meta = eventSourceMeta(e.source);
        const today = e.target_date === fmtDateKey(new Date());
        return `
          <div onclick="handleEventClick(${e.id})" class="p-3 rounded-xl bg-slate-900 border border-slate-800 hover:border-slate-700 cursor-pointer">
            <div class="flex justify-between gap-2 mb-1">
              <span class="text-[9px] font-black ${today ? "text-blue-300" : "text-slate-500"}">${today ? "TODAY" : e.target_date}</span>
              <span class="text-[9px] text-slate-500">${meta.icon} ${meta.label}</span>
            </div>
            <div class="text-xs font-black text-white leading-relaxed">${escapeHtmlCalendar(e.title)}</div>
            <div class="text-[10px] text-slate-400 mt-1">${e.target_time || "All day"}</div>
          </div>
        `;
      }).join("");

      const timed = upcoming.find(e => e.target_time);
      if (nextBox) {
        nextBox.innerText = timed
          ? `${timed.title} • ${timed.target_date} ${timed.target_time}`
          : "No timed reminders";
      }
    }

    function renderWeekCalendar() {
      const range = document.getElementById("calRangeLabel");
      if (range) range.innerText = calRangeLabelText();
      renderMiniMonth();
      renderAgenda();

      const dayNames = currentLang === "ar" ? AR_DAY_NAMES : EN_DAY_NAMES;
      const todayKey = fmtDateKey(new Date());
      const days = [...Array(7)].map((_, i) => {
        const d = new Date(calWeekStart);
        d.setDate(d.getDate() + i);
        return d;
      });

      const header = document.getElementById("calHeaderRow");
      if (!header) return;

      let headerHtml = `<div class="w-14 shrink-0 bg-slate-950"></div>`;
      days.forEach(d => {
        const isToday = fmtDateKey(d) === todayKey;
        headerHtml += `
          <div class="flex-1 min-w-[106px] text-center py-3 border-r border-slate-800 bg-slate-950">
            <div class="text-[9px] font-black text-slate-500 uppercase">${dayNames[d.getDay()]}</div>
            <div class="mx-auto mt-1 w-8 h-8 flex items-center justify-center rounded-full text-sm font-black ${isToday ? "bg-blue-500 text-white shadow-lg shadow-blue-500/20" : "text-slate-200"}">${d.getDate()}</div>
          </div>`;
      });
      header.innerHTML = headerHtml;

      const allDayRow = document.getElementById("calAllDayRow");
      if (allDayRow) {
        let allDayHtml = `<div class="w-14 shrink-0 flex items-center justify-center text-[8px] text-slate-600 font-black">ALL</div>`;
        days.forEach(d => {
          const key = fmtDateKey(d);
          const events = calAllEvents.filter(e => e.target_date === key && !e.target_time);
          allDayHtml += `<div class="flex-1 min-w-[106px] border-r border-slate-800 p-1 space-y-1">${events.map(calEventChip).join("")}</div>`;
        });
        allDayRow.innerHTML = allDayHtml;
      }

      const gridBody = document.getElementById("calGridBody");
      if (!gridBody) return;

      const totalHours = CAL_END_HOUR - CAL_START_HOUR + 1;
      const gridHeight = totalHours * CAL_ROW_HEIGHT;

      let bodyHtml = `<div class="w-14 shrink-0 relative bg-slate-950" style="height:${gridHeight}px;">`;
      for (let h = CAL_START_HOUR; h <= CAL_END_HOUR; h++) {
        const top = (h - CAL_START_HOUR) * CAL_ROW_HEIGHT;
        const label = currentLang === "ar"
          ? `${h > 12 ? h - 12 : (h || 12)} ${h < 12 ? "ص" : "م"}`
          : `${h > 12 ? h - 12 : (h || 12)} ${h < 12 ? "AM" : "PM"}`;
        bodyHtml += `<div class="absolute right-1 text-[9px] text-slate-500 font-bold" style="top:${top - 6}px">${label}</div>`;
      }
      bodyHtml += `</div>`;

      const now = new Date();
      const nowMinutes = now.getHours() * 60 + now.getMinutes();

      days.forEach(d => {
        const key = fmtDateKey(d);
        const isToday = key === todayKey;
        const events = calAllEvents.filter(e => e.target_date === key && e.target_time);
        let col = `<div class="flex-1 min-w-[106px] relative border-r border-slate-800 bg-slate-950" style="height:${gridHeight}px;">`;

        for (let h = CAL_START_HOUR; h <= CAL_END_HOUR; h++) {
          const top = (h - CAL_START_HOUR) * CAL_ROW_HEIGHT;
          col += `<div class="absolute left-0 right-0 border-t border-slate-900" style="top:${top}px;"></div>`;
        }

        events.forEach(e => {
          const parts = String(e.target_time).split(":").map(Number);
          const hh = parts[0] || 0;
          const mm = parts[1] || 0;
          const top = ((hh - CAL_START_HOUR) + mm / 60) * CAL_ROW_HEIGHT;
          if (top < -20 || top > gridHeight) return;
          const meta = eventSourceMeta(e.source);
          col += `
            <div onclick="handleEventClick(${e.id})"
                 class="absolute right-1 left-1 rounded-xl px-2 py-1.5 text-[10px] font-bold cursor-pointer overflow-hidden shadow-lg border
                 ${e.is_completed ? "bg-emerald-500/20 text-emerald-200 border-emerald-400/20 line-through" : "bg-blue-500/90 text-white border-blue-300/20"}"
                 style="top:${top}px; height:${CAL_ROW_HEIGHT - 6}px;"
                 title="${escapeHtmlCalendar(e.title)}">
              <div class="flex items-center gap-1"><span>${meta.icon}</span><span class="font-black">${e.target_time}</span></div>
              <div class="truncate mt-0.5">${escapeHtmlCalendar(e.title)}</div>
            </div>`;
        });

        if (isToday && nowMinutes >= CAL_START_HOUR * 60 && nowMinutes <= (CAL_END_HOUR + 1) * 60) {
          const lineTop = ((nowMinutes / 60) - CAL_START_HOUR) * CAL_ROW_HEIGHT;
          col += `<div class="absolute left-0 right-0 border-t-2 border-rose-500 z-10" style="top:${lineTop}px"><span class="absolute -right-0.5 -top-1 w-2 h-2 rounded-full bg-rose-500"></span></div>`;
        }

        col += `</div>`;
        bodyHtml += col;
      });

      gridBody.innerHTML = bodyHtml;
      gridBody.style.minHeight = `${gridHeight}px`;
    }

    function handleEventClick(id) {
      const event = calAllEvents.find(e => e.id === id);
      if (!event) return;
      const ok = confirm(
        `${event.title}\\n${event.target_date} ${event.target_time || ""}\\n\\n` +
        (event.is_completed ? "OK = إلغاء الإتمام" : "OK = تحديد كمكتمل")
      );
      if (ok) {
        toggleScheduleItem(id, event.is_completed ? 0 : 1);
      }
    }

    async function toggleScheduleItem(id, isCompleted) {
      await fetch("/api/schedule/toggle", {
        method: "POST",
        headers: {"Content-Type":"application/json"},
        body: JSON.stringify({schedule_id:id, is_completed:isCompleted})
      });
      await fetchScheduleEvents();
      renderWeekCalendar();
    }

    function openEventModal(prefillDate) {
      document.getElementById("newTaskTitle").value = "";
      document.getElementById("newTaskDate").value = prefillDate || fmtDateKey(new Date());
      document.getElementById("newTaskTime").value = "";
      document.getElementById("eventModal").classList.remove("hidden");
      document.getElementById("eventModal").classList.add("flex", "overlay-visible");
    }

    function closeEventModal() {
      document.getElementById("eventModal").classList.add("hidden");
      document.getElementById("eventModal").classList.remove("flex", "overlay-visible");
    }

    async function addNewScheduleTask() {
      const title = document.getElementById("newTaskTitle").value.trim();
      const d = document.getElementById("newTaskDate").value;
      const tm = document.getElementById("newTaskTime").value;
      if (!title || !d) return alert("اكتب عنوان الحدث والتاريخ.");

      const res = await fetch("/api/schedule/add", {
        method: "POST",
        headers: {"Content-Type":"application/json"},
        body: JSON.stringify({
          user_id: parseInt(currentUserId),
          title,
          target_date: d,
          target_time: tm
        })
      });
      if (!res.ok) return alert("حصل خطأ أثناء إضافة الموعد.");
      closeEventModal();
      await fetchScheduleEvents();
      renderWeekCalendar();
    }

    async function refreshTelegramControls() {
      const box = document.getElementById("telegramControls");
      if (!box || !currentUserId) return;

      try {
        const res = await fetch(`/api/telegram/status/${currentUserId}`);
        const st = await res.json();

        if (!st.configured) {
          box.innerHTML = `<span class="px-3 py-1.5 rounded-xl bg-slate-900 text-[10px] font-black text-slate-400">Telegram غير مفعّل</span>`;
          return;
        }

        if (st.connected) {
          box.innerHTML = `
            <span class="px-3 py-1.5 rounded-xl bg-emerald-500/10 border border-emerald-400/20 text-[10px] font-black text-emerald-300">✈️ Telegram متصل</span>
            <button onclick="unlinkTelegram()" class="px-3 py-1.5 rounded-xl bg-slate-900 border border-slate-700 text-[10px] font-black text-slate-300">فصل</button>
          `;
        } else {
          box.innerHTML = `
            <button onclick="linkTelegram()" class="px-3 py-1.5 rounded-xl bg-blue-500 hover:bg-blue-400 text-white text-[10px] font-black shadow-lg">ربط Telegram ✈️</button>
          `;
        }
      } catch (e) {
        console.error(e);
      }
    }

    async function linkTelegram() {
      try {
        const res = await fetch("/api/telegram/link", {
          method: "POST",
          headers: {"Content-Type":"application/json"},
          body: JSON.stringify({user_id:parseInt(currentUserId)})
        });
        const d = await res.json();
        if (!res.ok || !d.success) {
          throw new Error(d.error || "Telegram is not configured.");
        }
        window.open(d.link, "_blank");
        await refreshTelegramControls();
      } catch (e) {
        alert(e.message);
      }
    }

    async function unlinkTelegram() {
      await fetch("/api/telegram/unlink", {
        method: "POST",
        headers: {"Content-Type":"application/json"},
        body: JSON.stringify({user_id:parseInt(currentUserId)})
      });
      refreshTelegramControls();
    }

    function openMobileSidebar() {
      const sb = document.getElementById("mobileSidebar");
      const bd = document.getElementById("mobileSidebarBackdrop");
      const btn = document.getElementById("mobileMenuButton");
      if (!sb || !bd) return;
      sb.style.setProperty("z-index", "10001", "important");
      bd.style.setProperty("z-index", "10000", "important");

      sb.classList.remove("translate-x-full");
      bd.classList.remove("hidden");
      bd.style.removeProperty("display");
      bd.style.removeProperty("visibility");
      bd.style.removeProperty("opacity");
      bd.style.removeProperty("pointer-events");
      document.body.classList.add("overflow-hidden", "mobile-sidebar-open");
      sb.setAttribute("aria-hidden", "false");
      if (btn) btn.setAttribute("aria-expanded", "true");
    }

    function closeMobileSidebar() {
      const sb = document.getElementById("mobileSidebar");
      const bd = document.getElementById("mobileSidebarBackdrop");
      const btn = document.getElementById("mobileMenuButton");
      if (!sb || !bd) return;
      sb.style.setProperty("z-index", "10001", "important");
      bd.style.setProperty("z-index", "10000", "important");
      sb.classList.add("translate-x-full");
      bd.classList.add("hidden");
      bd.style.setProperty("display", "none", "important");
      bd.style.setProperty("visibility", "hidden", "important");
      bd.style.setProperty("opacity", "0", "important");
      bd.style.setProperty("pointer-events", "none", "important");
      document.body.classList.remove("overflow-hidden", "mobile-sidebar-open");
      sb.setAttribute("aria-hidden", "true");
      if (btn) btn.setAttribute("aria-expanded", "false");
    }

    function toggleMobileSidebar() {
      const sb = document.getElementById("mobileSidebar");
      if (!sb) return;
      if (sb.classList.contains("translate-x-full")) {
        openMobileSidebar();
      } else {
        closeMobileSidebar();
      }
    }

    document.addEventListener("keydown", function(event) {
      if (event.key === "Escape") closeMobileSidebar();
    });

    window.addEventListener("resize", function() {
      if (window.innerWidth >= 768) closeMobileSidebar();
    });


    let activeStudyMaterialId = 0;
    let activeAssessmentId = 0;

    function studyUserId() {
      return parseInt(currentUserId || "0");
    }

    let activeTutorSessionId = 0;

    function startNewTutorChat(){
      activeTutorSessionId=0; const q=document.getElementById("tutorQuestion"),a=document.getElementById("tutorAnswer"),l=document.getElementById("tutorSessionLabel"),c=document.getElementById("tutorConversationBox");
      if(q)q.value=""; if(a)a.innerText=""; if(l)l.innerText="New chat"; if(c)c.classList.add("hidden");
    }
    async function loadTutorHistory(){
      const box=document.getElementById("tutorHistoryBox"); if(!box||!studyUserId())return;
      try{const d=await readApiResponse(await fetch(`/api/study/tutor-history?user_id=${studyUserId()}&limit=30`)); box.innerHTML=(d.sessions||[]).map(s=>`<button type="button" onclick="openTutorHistory(${Number(s.id)})" class="w-full text-left p-3 rounded-xl border bg-slate-50 hover:bg-amber-50"><div class="font-black">${escapeHtml(s.first_question||s.material_title||'Chat')}</div><div class="text-slate-400 mt-1">${escapeHtml(s.material_title||'General chat')} · ${Number(s.message_count)||0} messages · ${escapeHtml(s.created_at||'')}</div></button>`).join("")||'<div class="text-slate-400">No saved tutor chats yet.</div>';}catch(e){box.innerHTML=`<div class="text-rose-500">${escapeHtml(e.message)}</div>`;}
    }
    async function openTutorHistory(id){
      const box=document.getElementById("tutorConversationBox"); if(!box)return;
      try{const d=await readApiResponse(await fetch(`/api/study/tutor-history/${Number(id)}?user_id=${studyUserId()}`)); activeTutorSessionId=Number(id); const l=document.getElementById("tutorSessionLabel"); if(l)l.innerText=`Saved chat #${id}`; box.classList.remove("hidden"); box.innerHTML=(d.messages||[]).map(m=>`<div class="p-3 rounded-xl ${m.role==='user'?'bg-amber-50 border border-amber-100':'bg-slate-50 border border-slate-200'}"><b>${m.role==='user'?'You':'AI Tutor'}</b><div class="mt-1 whitespace-pre-wrap">${escapeHtml(m.content||'')}</div></div>`).join(""); box.scrollTop=box.scrollHeight;}catch(e){box.classList.remove("hidden");box.innerHTML=`<div class="text-rose-500">${escapeHtml(e.message)}</div>`;}
    }
    async function loadVideoHistory(){
      const box=document.getElementById("videoHistoryBox"); if(!box||!studyUserId())return;
      try{const d=await readApiResponse(await fetch('/api/video-history?limit=50')); box.innerHTML=(d.videos||[]).map(v=>{const state=v.status==='completed'?'جاهز':v.status==='failed'?'فشل':v.status==='processing'?'جاري التنفيذ':'في الانتظار'; return `<div class="p-3 rounded-xl border bg-slate-50 space-y-2"><div class="flex justify-between gap-2"><b>${escapeHtml(v.filename||'Video')}</b><span>${state} ${Number(v.progress)||0}%</span></div><div class="text-slate-400">${escapeHtml(v.mode||'ai')} · ${escapeHtml(v.language||'ar')} · ${escapeHtml(v.created_at||'')}</div>${v.error?`<div class="text-rose-500">${escapeHtml(v.error)}</div>`:''}${v.status==='completed'?`<button type="button" onclick="openVideoHistory('${escapeHtml(v.id)}')" class="px-3 py-2 rounded-lg bg-slate-900 text-amber-300 font-black">Open Video</button>`:''}</div>`;}).join("")||'<div class="text-slate-400">No video history yet.</div>';}catch(e){box.innerHTML=`<div class="text-rose-500">${escapeHtml(e.message)}</div>`;}
    }
    async function openVideoHistory(id){try{const d=await readApiResponse(await fetch(`/api/video-job/${encodeURIComponent(id)}`,{cache:'no-store',credentials:'same-origin'}));if(d.status!=='completed')return alert(d.error||'Video is not ready yet.');switchTab('video');renderConceptTheater(d.data||{},d.videoUrl||'',Number(d.perspective)||1,d.mode||'ai',d.youtube||[]);}catch(e){alert(e.message);}}

    async function buildStudyPack() {
      const file = document.getElementById("studyPdfFile")?.files?.[0];
      if (!file) return alert("Select a PDF first.");
      const status = document.getElementById("studyPackStatus");
      status.innerText = "Uploading PDF and building the study pack...";
      const fd = new FormData();
      fd.append("user_id", studyUserId());
      fd.append("materialTitle", document.getElementById("studyMaterialTitle").value || file.name);
      fd.append("videoMode", document.getElementById("studyVideoMode").value);
      fd.append("videoLanguage", document.getElementById("studyVideoLanguage").value);
      fd.append("assessmentMode", document.getElementById("studyAssessmentMode").value);
      fd.append("questionCount", document.getElementById("studyQuestionCount").value);
      fd.append("questionTypes", [...document.querySelectorAll(".study-qtype:checked")].map(x => x.value).join(","));
      fd.append("pdfFile", file);
      try {
        const res = await fetch("/api/study/pack", {method:"POST", body:fd});
        const d = await res.json();
        if (!res.ok) throw new Error(d.detail || d.error || "Study pack failed");
        activeStudyMaterialId = d.materialId;
        activeAssessmentId = d.assessmentId;
        status.innerHTML = `Done. <button type="button" onclick="openCreatedAssessment(${Number(d.assessmentId)})" class="underline font-black text-blue-700">Assessment #${Number(d.assessmentId)}</button> created.`;
        await loadStudyProgress();
        await renderAssessment(d.assessmentId);
        if (d.youtube?.length) {
          status.innerText += " YouTube options are ready.";
        }
      } catch (e) {
        status.innerText = "Error: " + e.message;
        alert(e.message);
      }
    }

    async function loadStudyProgress() {
      if (!studyUserId()) return;
      const [p,w,n] = await Promise.all([
        fetch(`/api/study/progress?user_id=${studyUserId()}`).then(r=>r.json()),
        fetch(`/api/study/weaknesses?user_id=${studyUserId()}`).then(r=>r.json()),
        fetch(`/api/study/notifications?user_id=${studyUserId()}`).then(r=>r.json())
      ]);
      document.getElementById("studyProgressBox").innerHTML = `
        <div class="p-3 rounded-xl bg-amber-50"><b>${p.materials||0}</b><br>Materials</div>
        <div class="p-3 rounded-xl bg-amber-50"><b>${p.assessments||0}</b><br>Assessments</div>
        <div class="p-3 rounded-xl bg-amber-50"><b>${p.completedAssessments||0}</b><br>Completed</div>
        <div class="p-3 rounded-xl bg-amber-50"><b>${p.flashcardMastery||0}%</b><br>Card mastery</div>`;
      document.getElementById("studyWeaknessBox").innerHTML = (w.weaknesses||[]).slice(0,8).map(x =>
        `<div class="p-2 rounded-xl bg-rose-50 border border-rose-100"><b>${escapeHtml(x.topic)}</b><br>${escapeHtml(x.recommended_action||"Review and retry.")}</div>`
      ).join("") || '<div class="text-slate-400">No weakness data yet.</div>';
      document.getElementById("studyNotificationBox").innerHTML = (n.notifications||[]).slice(0,8).map(x =>
        `<div class="p-2 rounded-xl bg-blue-50 border border-blue-100">🔔 ${escapeHtml(x.title)} — ${escapeHtml(x.message)}</div>`
      ).join("") || '<div class="text-slate-400">No urgent notifications.</div>';
    }

    async function renderAssessment(id) {
      activeAssessmentId = id;
      const res = await fetch(`/api/study/assessment/${id}?user_id=${studyUserId()}`);
      const d = await res.json();
      if (!res.ok) return alert(d.detail || "Assessment unavailable");
      const box = document.getElementById("assessmentArea");
      box.innerHTML = `<div class="text-xs font-bold text-slate-500">${escapeHtml(d.assessment.mode)} • ${d.questions.length} questions</div>` +
        d.questions.map(q => {
          const opts = (q.options||[]).map(o => `<option>${escapeHtml(o)}</option>`).join("");
          return `<div class="p-4 rounded-2xl bg-slate-50 border border-slate-200 space-y-2">
            <div class="font-black text-sm">Q${q.number} • ${escapeHtml(q.type)}</div>
            <div class="text-sm font-bold">${escapeHtml(q.question)}</div>
            ${opts ? `<select id="ans_${q.id}" class="w-full p-3 rounded-xl border"><option value="">Choose...</option>${opts}</select>` :
              `<textarea id="ans_${q.id}" rows="3" class="w-full p-3 rounded-xl border" placeholder="Your answer"></textarea>`}
            <input id="file_${q.id}" type="file" accept="image/*,application/pdf" class="w-full text-xs">
            <button onclick="submitStudyAnswer(${q.id})" class="px-4 py-2 rounded-xl bg-slate-900 text-amber-300 font-black text-xs">Submit & Grade</button>
            <div id="feedback_${q.id}" class="text-xs whitespace-pre-wrap"></div>
          </div>`;
        }).join("");
    }

    async function submitStudyAnswer(qid) {
      const fd = new FormData();
      fd.append("user_id", studyUserId());
      fd.append("assessmentId", activeAssessmentId);
      fd.append("questionId", qid);
      const ans = document.getElementById(`ans_${qid}`);
      if (ans) fd.append("answerText", ans.value || "");
      const file = document.getElementById(`file_${qid}`)?.files?.[0];
      if (file) fd.append("answerFile", file);
      const out = document.getElementById(`feedback_${qid}`);
      out.innerText = "Grading...";
      try {
        const res = await fetch("/api/study/assessment/submit", {method:"POST", body:fd});
        const d = await res.json();
        if (!res.ok) throw new Error(d.detail || d.error || "Grading failed");
        out.innerText = `Score: ${d.score}/${d.maxScore}\\n${d.feedback}`;
        await loadStudyProgress();
      } catch (e) { out.innerText = "Error: " + e.message; }
    }

    async function openCreatedAssessment(id) {
      const assessmentId = Number(id);
      if (!Number.isFinite(assessmentId) || assessmentId <= 0) return;
      await renderAssessment(assessmentId);
      document.getElementById("assessmentArea")?.scrollIntoView({behavior:"smooth", block:"start"});
    }

    async function loadLatestAssessment() {
      const res = await fetch(`/api/study/assessment-history?user_id=${studyUserId()}`);
      const d = await res.json();
      if (d.assessments?.length) await renderAssessment(d.assessments[0].id);
      else alert("No assessment yet. Build a study pack first.");
    }

    async function loadSmartReview() {
      const res = await fetch(`/api/study/review?user_id=${studyUserId()}&limit=20`);
      const d = await res.json();
      const box = document.getElementById("reviewArea");
      box.innerHTML = (d.cards||[]).map(c => `
        <div class="p-4 rounded-2xl border bg-amber-50 space-y-2">
          <div class="font-black">${escapeHtml(c.front)}</div>
          <details><summary class="text-xs font-bold cursor-pointer">Reveal answer</summary><div class="pt-2 text-sm">${escapeHtml(c.back)}</div></details>
          <div class="flex gap-2"><button onclick="rateReview(${c.id},'known')" class="px-3 py-2 rounded-xl bg-emerald-100 text-emerald-800 font-black text-xs">✓ Know</button>
          <button onclick="rateReview(${c.id},'missed')" class="px-3 py-2 rounded-xl bg-rose-100 text-rose-800 font-black text-xs">× Missed</button></div>
        </div>`).join("") || '<div class="text-slate-400 text-sm">No cards due right now.</div>';
    }

    async function rateReview(id, result) {
      const fd = new FormData();
      fd.append("user_id", studyUserId()); fd.append("cardId", id); fd.append("result", result);
      await fetch("/api/study/review/rate", {method:"POST", body:fd});
      loadSmartReview(); loadStudyProgress();
    }

    async function searchStudyMaterial() {
      if (!activeStudyMaterialId) return alert("Build a study pack first.");
      const q = document.getElementById("materialSearch").value.trim();
      if (!q) return;
      const d = await fetch(`/api/study/search?user_id=${studyUserId()}&materialId=${activeStudyMaterialId}&q=${encodeURIComponent(q)}`).then(r=>r.json());
      document.getElementById("materialSearchResults").innerHTML = (d.matches||[]).map(m =>
        `<div class="p-3 rounded-xl bg-slate-50 border">${escapeHtml(m.snippet)}</div>`
      ).join("") || '<div class="text-slate-400">No matches.</div>';
    }

    async function askStudyTutor() {
      const q = document.getElementById("tutorQuestion").value.trim();
      if (!q) return;
      const fd = new FormData();
      fd.append("user_id", studyUserId());
      fd.append("materialId", activeStudyMaterialId);
      fd.append("question", q);
      fd.append("mode", document.getElementById("tutorMode").value);
      fd.append("strategy", document.getElementById("tutorStrategy").value);
      fd.append("sessionId", activeTutorSessionId || 0);
      const out = document.getElementById("tutorAnswer"); out.innerText="Thinking...";
      try{const d=await readApiResponse(await fetch("/api/study/tutor",{method:"POST",body:fd})); activeTutorSessionId=Number(d.sessionId||0); const l=document.getElementById("tutorSessionLabel"); if(l)l.innerText=`Saved chat #${activeTutorSessionId}`; out.innerText=JSON.stringify(d.answer,null,2); loadTutorHistory();}catch(e){out.innerText="Error: "+e.message;}
    }

    async function generateStudyGoalPlan() {
      const goal = document.getElementById("studyGoal").value.trim();
      if (!goal) return;
      const fd = new FormData();
      fd.append("user_id", studyUserId()); fd.append("goal", goal); fd.append("duration", "1 month"); fd.append("language", currentLang);
      const res = await fetch("/api/study/goal-plan", {method:"POST", body:fd});
      const d = await res.json();
      document.getElementById("studyGoalPlan").innerText = JSON.stringify(d.plan || d, null, 2);
    }

    async function loadHistoryPage(){
      if(!studyUserId()) return;
      const setBox=(id,html)=>{const el=document.getElementById(id); if(el) el.innerHTML=html;};
      try{
        const [m,v,a,t,r,ex,lab]=await Promise.all([
          readApiResponse(await fetch(`/api/study/material-history?limit=50`,{credentials:'same-origin',cache:'no-store'})),
          readApiResponse(await fetch(`/api/video-history?limit=50`,{credentials:'same-origin',cache:'no-store'})),
          readApiResponse(await fetch(`/api/study/assessment-history?user_id=${studyUserId()}`,{credentials:'same-origin',cache:'no-store'})),
          readApiResponse(await fetch(`/api/study/tutor-history?user_id=${studyUserId()}&limit=30`,{credentials:'same-origin',cache:'no-store'})),
          readApiResponse(await fetch(`/api/gps/roadmap-history?user_id=${studyUserId()}&limit=30`,{credentials:'same-origin',cache:'no-store'})),
          readApiResponse(await fetch(`/api/exams`,{credentials:'same-origin',cache:'no-store'})),
          readApiResponse(await fetch(`/api/learning-studio/history?limit=30`,{credentials:'same-origin',cache:'no-store'}))
        ]);
        setBox('historyMaterialsBox',(m.materials||[]).map(x=>`<div class="p-3 rounded-xl border bg-slate-50"><div class="font-black">${escapeHtml(x.title||x.filename||'Material')}</div><div class="text-slate-400 mt-1">${escapeHtml(x.filename||'')} · ${escapeHtml(x.created_at||'')}</div><div class="text-slate-500 mt-1">Videos: ${Number(x.video_count)||0} · Assessments: ${Number(x.assessment_count)||0}</div></div>`).join('')||'<div class="text-slate-400">No materials yet.</div>');
        setBox('historyVideosBox',(v.videos||[]).map(x=>`<div class="p-3 rounded-xl border bg-slate-50"><div class="flex justify-between gap-2"><b>${escapeHtml(x.filename||'Video')}</b><span>${Number(x.progress)||0}%</span></div><div class="text-slate-400 mt-1">${escapeHtml(x.mode||'ai')} · ${escapeHtml(x.status||'')} · ${escapeHtml(x.created_at||'')}</div>${x.status==='completed'?`<button type="button" onclick="openVideoHistory('${escapeHtml(x.id)}')" class="mt-2 px-3 py-2 rounded-lg bg-slate-900 text-amber-300 font-black">Open Video</button>`:''}</div>`).join('')||'<div class="text-slate-400">No videos yet.</div>');
        setBox('historyAssessmentsBox',(a.assessments||[]).map(x=>`<div class="p-3 rounded-xl border bg-slate-50"><div class="font-black">Assessment #${Number(x.id)}</div><div class="text-slate-400 mt-1">${escapeHtml(x.mode||'')} · ${Number(x.question_count)||0} questions · ${escapeHtml(x.created_at||'')}</div><div class="mt-1">Status: ${escapeHtml(x.status||'')} · Score: ${x.score==null?'—':Number(x.score)}/${x.max_score==null?'—':Number(x.max_score)}</div><button type="button" onclick="openCreatedAssessment(${Number(x.id)})" class="mt-2 px-3 py-2 rounded-lg bg-slate-900 text-amber-300 font-black">Open</button></div>`).join('')||'<div class="text-slate-400">No assessments yet.</div>');
        setBox('historyRoadmapsBox',(r.roadmaps||[]).map(x=>`<div class="p-3 rounded-xl border bg-slate-50"><div class="font-black">${escapeHtml(x.goal||'Learning roadmap')}</div><div class="text-slate-400 mt-1">${escapeHtml(x.duration||'')} · ${Number(x.node_count)||0} stages · ${escapeHtml(x.created_at||'')}</div><button type="button" onclick="openRoadmapHistory(${Number(x.id)})" class="mt-2 px-3 py-2 rounded-lg bg-slate-900 text-amber-300 font-black">View roadmap</button></div>`).join('')||'<div class="text-slate-400">No saved roadmaps yet.</div>');
        setBox('historyGeneratedExamsBox',(ex.exams||[]).map(x=>`<div class="p-3 rounded-xl border bg-slate-50"><div class="font-black">${escapeHtml(x.title||('Exam #'+x.id))}</div><div class="text-slate-400 mt-1">${escapeHtml(x.status||'')} · ${Number(x.n_questions)||0} questions · ${escapeHtml(x.created_at||'')}</div>${x.status==='done'?`<div class="flex flex-wrap gap-2 mt-2"><a target="_blank" href="/exam/${Number(x.id)}/paper" class="px-3 py-2 rounded-lg bg-slate-900 text-amber-300 font-black">Exam paper</a><a target="_blank" href="/exam/${Number(x.id)}/key" class="px-3 py-2 rounded-lg bg-emerald-600 text-white font-black">Answer key</a></div>`:''}</div>`).join('')||'<div class="text-slate-400">No generated exams yet.</div>');
        setBox('historyTutorBox',(t.sessions||[]).map(x=>`<button type="button" onclick="openHistoryTutor(${Number(x.id)})" class="w-full text-right p-3 rounded-xl border bg-slate-50 hover:bg-amber-50"><div class="font-black">${escapeHtml(x.first_question||x.material_title||'Chat')}</div><div class="text-slate-400 mt-1">${escapeHtml(x.material_title||'General chat')} · ${Number(x.message_count)||0} messages · ${escapeHtml(x.created_at||'')}</div></button>`).join('')||'<div class="text-slate-400">No saved tutor chats yet.</div>');
        setBox('historyLearningStudioBox',(lab.items||[]).map(x=>`<div class="p-3 rounded-xl border bg-slate-50"><div class="font-black">${escapeHtml(x.title||'Study work')}</div><div class="text-slate-400 mt-1">${escapeHtml(x.activity_type||'')} · ${escapeHtml(x.domain||'general')} · ${escapeHtml(x.created_at||'')}</div><button type="button" onclick="lsLoadWork(${Number(x.id)})" class="mt-2 px-3 py-2 rounded-lg bg-slate-900 text-amber-300 font-black">Open work</button></div>`).join('')||'<div class="text-slate-400">No Learning Studio work yet.</div>');
      }catch(e){
        const msg=`<div class="text-rose-500">${escapeHtml(e.message||'History failed to load')}</div>`;
        setBox('historyMaterialsBox',msg); setBox('historyVideosBox',msg); setBox('historyAssessmentsBox',msg); setBox('historyTutorBox',msg); setBox('historyRoadmapsBox',msg); setBox('historyGeneratedExamsBox',msg); setBox('historyLearningStudioBox',msg);
      }
    }

    async function openRoadmapHistory(id){
      const box=document.getElementById('historyRoadmapsBox'); if(!box) return;
      try{
        const d=await readApiResponse(await fetch(`/api/gps/roadmap-history/${Number(id)}?user_id=${studyUserId()}`,{credentials:'same-origin',cache:'no-store'}));
        const nodes=(d.roadmap&&Array.isArray(d.roadmap.nodes))?d.roadmap.nodes:[];
        const renderList=(arr)=>Array.isArray(arr)&&arr.length?`<ul class="mt-2 space-y-1">${arr.map(x=>`<li class="text-xs text-slate-600">• ${escapeHtml(typeof x==='string'?x:JSON.stringify(x))}</li>`).join('')}</ul>`:'<p class="text-xs text-slate-400 mt-1">No details saved.</p>';
        const content=nodes.map((n,i)=>{
          const acts=Array.isArray(n.activities)?n.activities:[];
          const done=Array.isArray(n.completedActivities)?n.completedActivities:[];
          const pct=Number(n.activityProgressPercent)||0;
          const resources=Array.isArray(n.resources)?n.resources:[];
          const quiz=Array.isArray(n.quizQuestions)?n.quizQuestions:[];
          return `<article class="p-3 rounded-xl border bg-white mt-2 space-y-3"><div><b>${i+1}. ${escapeHtml(n.title||'Stage')}</b><div class="text-slate-500 mt-1">${escapeHtml(n.timeframe||'')} · ${escapeHtml(n.activityType||'')}</div><div class="mt-1 text-sm">${escapeHtml(n.objective||'')}</div></div>
          <div><b class="text-xs">Core concepts</b>${renderList(n.coreConcepts)}</div>
          <details><summary class="cursor-pointer text-xs font-bold">Activities (${done.length}/${acts.length} complete)</summary><div class="mt-2 space-y-2">${acts.map((a,ai)=>`<div class="rounded-lg bg-slate-50 p-2"><b class="text-xs">${done.includes(ai)?'✓ ':''}${escapeHtml(a.title||`Activity ${ai+1}`)}</b><p class="text-xs mt-1">${escapeHtml(a.instructions||'')}</p><p class="text-xs text-slate-500 mt-1">Deliverable: ${escapeHtml(a.deliverable||'—')} · ${Number(a.estimatedMinutes)||30} min</p></div>`).join('')||'<p class="text-xs text-slate-400">No activities saved.</p>'}</div><div class="h-2 rounded bg-slate-100 mt-2"><div class="h-2 rounded bg-emerald-500" style="width:${Math.max(0,Math.min(100,pct))}%"></div></div></details>
          <details><summary class="cursor-pointer text-xs font-bold">Resources (${resources.length})</summary><div class="mt-2 space-y-2">${resources.map(r=>`<div class="rounded-lg bg-amber-50 p-2"><b class="text-xs">${escapeHtml(r.title||'Resource')}</b><p class="text-xs text-slate-500">${escapeHtml(r.provider||'')} · ${escapeHtml(r.why||'')}</p><a target="_blank" rel="noopener" class="text-xs text-red-700 underline" href="https://www.youtube.com/results?search_query=${encodeURIComponent(r.searchQuery||r.title||n.title||'')}">Search resource ↗</a></div>`).join('')||'<p class="text-xs text-slate-400">No resources saved.</p>'}</div></details>
          <details><summary class="cursor-pointer text-xs font-bold">Evaluation & quiz (${quiz.length} questions)</summary><div class="mt-2 text-xs">Mastery threshold: ${Number((n.evaluation||{}).masteryThreshold)||80}%</div>${renderList((n.evaluation||{}).criteria)}${quiz.map((q,qi)=>`<div class="mt-2 rounded-lg bg-slate-50 p-2"><b class="text-xs">${qi+1}. ${escapeHtml(q.question||'')}</b>${renderList(q.options)}<p class="text-xs text-emerald-800 mt-1">Answer: ${escapeHtml((q.options||[])[Number(q.correctIndex)||0]||'—')}</p><p class="text-xs text-slate-600 mt-1">${escapeHtml(q.explanation||'')}</p></div>`).join('')}</details>
          ${n.miniProject?`<div class="rounded-lg bg-indigo-50 p-2 text-xs"><b>Deliverable / mini-project:</b> ${escapeHtml(n.miniProject)}</div>`:''}${n.commonMistakes&&n.commonMistakes.length?`<div><b class="text-xs">Common mistakes</b>${renderList(n.commonMistakes)}</div>`:''}${n.reviewPlan?`<p class="text-xs bg-blue-50 p-2 rounded-lg"><b>Review:</b> ${escapeHtml(n.reviewPlan)}</p>`:''}${n.nextStep?`<p class="text-xs bg-slate-50 p-2 rounded-lg"><b>Next step:</b> ${escapeHtml(n.nextStep)}</p>`:''}</article>`;
        }).join('');
        box.innerHTML=`<div class="p-3 rounded-xl border bg-amber-50"><b>${escapeHtml(d.goal||'Learning roadmap')}</b><div class="text-slate-500 mt-1">${escapeHtml((d.roadmap||{}).trackSummary||'')}</div><div class="text-xs text-slate-500 mt-1">${escapeHtml(d.duration||'')} · ${nodes.length} stages · ${escapeHtml(d.created_at||'')}</div>${content}<button type="button" onclick="loadHistoryPage()" class="mt-3 px-3 py-2 rounded-lg bg-slate-900 text-amber-300 font-black">Back to history</button></div>`;
      }catch(e){box.innerHTML=`<div class="text-rose-500">${escapeHtml(e.message||'Could not open roadmap')}</div>`;}
    }

    async function openHistoryTutor(id){
      const box=document.getElementById('historyTutorConversationBox'); if(!box) return;
      try{const d=await readApiResponse(await fetch(`/api/study/tutor-history/${Number(id)}?user_id=${studyUserId()}`,{credentials:'same-origin'})); box.classList.remove('hidden'); box.innerHTML=(d.messages||[]).map(m=>`<div class="p-3 rounded-xl ${m.role==='user'?'bg-amber-50 border border-amber-100':'bg-slate-50 border border-slate-200'}"><b>${m.role==='user'?'You':'AI Tutor'}</b><div class="mt-1 whitespace-pre-wrap">${escapeHtml(m.content||'')}</div></div>`).join(''); box.scrollTop=box.scrollHeight;}catch(e){box.classList.remove('hidden');box.innerHTML=`<div class="text-rose-500">${escapeHtml(e.message)}</div>`;}
    }

    function switchTab(t) {
      const allowed = new Set(["studyroom","home","video","studycenter","exams","history","gps","learningstudio","calendar","leaderboard","account"]);
      // Keep navigation side effects isolated: a missing mobile helper or a
      // stale inline handler must never leave every application panel hidden.
      if (!allowed.has(t)) t = "home";
      if (window.innerWidth < 768 && typeof closeMobileSidebar === "function") {
        try { closeMobileSidebar(); } catch (navError) { console.warn("Mobile navigation reset failed", navError); }
      }
      const panels = Array.from(document.querySelectorAll(".tab-content"));
      panels.forEach(e => e.classList.add("hidden"));
      const target = document.getElementById("tab-" + t) || document.getElementById("tab-home");
      if (target) {
        target.classList.remove("hidden");
        target.removeAttribute("aria-hidden");
        target.style.removeProperty("display");
      } else {
        const home = document.getElementById("tab-home");
        if (home) { home.classList.remove("hidden"); home.style.removeProperty("display"); }
      }

      document.querySelectorAll(".nav-item").forEach(b => {
        const active = b.dataset.target === t;
        b.classList.toggle("bg-amber-100", active);
        b.classList.toggle("text-amber-900", active);
        b.classList.toggle("text-slate-600", !active);
      });

      if (t === "calendar") {
        fetchScheduleEvents().then(renderWeekCalendar);
        refreshTelegramControls();
      }
      if (t === "exams" && window.exLoadAll) {
        window.exLoadAll();
      }
      if (t === "studycenter") {
        loadStudyProgress();
        loadSmartReview();
        loadVideoHistory();
        loadTutorHistory();
      }
      if (t === "history") {
        loadHistoryPage();
      }
      if (t === "learningstudio") {
        lsToggleEditor();
        lsLoadHistory();
      }
      if (t === "leaderboard") {
        loadLeaderboard();
      }
    }
  </script>
</body>
</html>""")
if __name__ == "__main__":
    import uvicorn
    import os

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080"))
    )
