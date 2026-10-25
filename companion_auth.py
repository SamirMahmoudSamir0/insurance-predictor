"""
Password + Permanent-User-ID authentication for S-Tesseract.

Used by both the website (register-or-login / set-password) and the
Companion apps (Windows / Android) via /api/companion/auth/*.

Design notes
------------
* Identity  = users.public_user_id  ("STX-XXXXXXXXXX", already stored, never changes).
* Secret    = password. Stored only as a salted PBKDF2-HMAC-SHA256 hash (stdlib,
              no new dependency). The plaintext is never logged or stored.
* Sessions  = the existing `user_sessions` table (token hash only in the DB).
              The app receives a random bearer token once, at login, and keeps it
              in the OS secure store. It never keeps the password.
* The public ID is NOT a secret (it is shown on the leaderboard); only the
  password proves ownership. Failed-login lockout protects against guessing.
"""
import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
import os
from pydantic import BaseModel

PBKDF2_ITERATIONS = 600_000
PASSWORD_MIN = 8
PASSWORD_MAX = 128
MAX_FAILED_ATTEMPTS = 5
LOCK_MINUTES = 15
COMPANION_SESSION_DAYS = 90

PUBLIC_ID_RE = re.compile(r"^STX-[0-9A-F]{10}$")
SECRET_COLUMNS = {"password_hash", "password_set_at", "failed_login_count", "locked_until"}
_COMMON = {"12345678", "123456789", "1234567890", "password", "password1", "qwertyui", "11111111", "00000000"}


# ----------------------------------------------------------------------------
# Hashing helpers
# ----------------------------------------------------------------------------
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


# Used to burn the same CPU time when the ID does not exist (no timing oracle).
_DUMMY_HASH = hash_password("not-a-real-password")


def validate_new_password(password: str, *, username: str = "", public_id: str = "") -> str:
    """Return an error message, or '' if the password is acceptable."""
    if len(password) < PASSWORD_MIN:
        return f"كلمة المرور لازم تكون {PASSWORD_MIN} حروف على الأقل."
    if len(password) > PASSWORD_MAX:
        return "كلمة المرور طويلة جدًا."
    low = password.lower()
    if low in _COMMON or (username and low == username.lower()) or (public_id and low == public_id.lower()):
        return "كلمة المرور سهلة التخمين. اختار كلمة مرور أقوى."
    return ""


def public_user(row) -> dict:
    """User row -> dict that is safe to send to a client (no secrets)."""
    d = dict(row)
    for key in SECRET_COLUMNS:
        d.pop(key, None)
    d["password_set"] = bool(dict(row).get("password_hash"))
    return d


def bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if header[:7].lower() == "bearer ":
        return header[7:].strip()
    return ""


# ----------------------------------------------------------------------------
# Schema migration (idempotent, additive: existing accounts are untouched)
# ----------------------------------------------------------------------------
def migrate(get_db) -> None:
    conn = get_db()
    try:
        ucols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        for name, ddl in [
            ("password_hash", "TEXT DEFAULT ''"),
            ("password_set_at", "TEXT DEFAULT ''"),
            ("failed_login_count", "INTEGER DEFAULT 0"),
            ("locked_until", "TEXT DEFAULT ''"),
        ]:
            if name not in ucols:
                conn.execute(f"ALTER TABLE users ADD COLUMN {name} {ddl}")
        scols = {r[1] for r in conn.execute("PRAGMA table_info(user_sessions)").fetchall()}
        for name, ddl in [
            ("client", "TEXT DEFAULT 'web'"),
            ("device_name", "TEXT DEFAULT ''"),
            ("last_used_at", "TEXT DEFAULT ''"),
        ]:
            if name not in scols:
                conn.execute(f"ALTER TABLE user_sessions ADD COLUMN {name} {ddl}")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_public_id ON users(public_user_id) WHERE public_user_id != ''")
        conn.commit()
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# Shared credential check with lockout (used by website AND app login)
# ----------------------------------------------------------------------------
def check_password_with_lockout(conn, user, password: str, tz, audit=None) -> None:
    """Raise HTTPException unless `password` is correct for `user` (a sqlite Row)."""
    now = datetime.now(tz)
    locked_until = (user["locked_until"] or "") if "locked_until" in user.keys() else ""
    if locked_until:
        try:
            until = datetime.fromisoformat(locked_until)
            if until > now:
                minutes = max(1, int((until - now).total_seconds() // 60) + 1)
                raise HTTPException(status_code=429, detail={"code": "locked", "minutes": minutes})
        except ValueError:
            pass
    if verify_password(password, user["password_hash"] or ""):
        if (user["failed_login_count"] or 0) or locked_until:
            conn.execute("UPDATE users SET failed_login_count=0, locked_until='' WHERE id=?", (user["id"],))
            conn.commit()
        return
    fails = (user["failed_login_count"] or 0) + 1
    locked_now = fails >= MAX_FAILED_ATTEMPTS
    if locked_now:
        conn.execute("UPDATE users SET failed_login_count=0, locked_until=? WHERE id=?",
                     ((now + timedelta(minutes=LOCK_MINUTES)).isoformat(), user["id"]))
    else:
        conn.execute("UPDATE users SET failed_login_count=? WHERE id=?", (fails, user["id"]))
    conn.commit()
    # The audit log opens its OWN database connection. It must run only AFTER the commit above: while this connection still holds
    # the write lock, the second connection would wait for it (30 s) and freeze every other request.
    if locked_now and audit:
        audit(user["id"], "login_locked", f"after {MAX_FAILED_ATTEMPTS} failed attempts")
    raise HTTPException(status_code=401, detail={"code": "invalid_credentials"})


# ----------------------------------------------------------------------------
# Router
# ----------------------------------------------------------------------------
class CompanionLogin(BaseModel):
    public_user_id: str
    password: str
    device_name: str = ""
    platform: str = ""


class SetPassword(BaseModel):
    new_password: str
    current_password: str = ""


def build_router(get_db, hash_token, current_user_id, tz, audit) -> APIRouter:
    router = APIRouter()

    @router.post("/api/companion/auth/login")
    def companion_login(payload: CompanionLogin, request: Request):
        public_id = payload.public_user_id.strip().upper()
        password = payload.password
        if len(password) > PASSWORD_MAX or not PUBLIC_ID_RE.match(public_id):
            verify_password(password[:PASSWORD_MAX], _DUMMY_HASH)
            raise HTTPException(status_code=401, detail={"code": "invalid_credentials"})

        conn = get_db()
        try:
            user = conn.execute("SELECT * FROM users WHERE public_user_id=?", (public_id,)).fetchone()
            if not user:
                verify_password(password, _DUMMY_HASH)  # same cost as a real check
                raise HTTPException(status_code=401, detail={"code": "invalid_credentials"})
            if not user["password_hash"]:
                raise HTTPException(status_code=403, detail={"code": "password_not_set"})

            check_password_with_lockout(conn, user, password, tz, audit)

            status = (user["account_status"] or "active").lower()
            if status != "active":
                raise HTTPException(status_code=403, detail={"code": "account_blocked", "message": user["ban_reason"] or ""})

            now = datetime.now(tz)
            token = secrets.token_urlsafe(32)
            expires = (now + timedelta(days=COMPANION_SESSION_DAYS)).isoformat()
            platform = (payload.platform or "app")[:20]
            device = (payload.device_name or "")[:80]
            conn.execute(
                "INSERT INTO user_sessions(token_hash,user_id,created_at,expires_at,client,device_name,last_used_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (hash_token(token), user["id"], now.isoformat(), expires, "companion", device, now.isoformat()),
            )
            conn.execute(
                "UPDATE users SET last_login_at=?, login_count=COALESCE(login_count,0)+1, last_seen_at=?, last_platform=? WHERE id=?",
                (now.isoformat(), now.isoformat(), platform, user["id"]),
            )
            conn.commit()
            user = conn.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
        finally:
            conn.close()
        audit(user["id"], "companion_login", f"platform={platform} device={device}")
        return {"token": token, "token_type": "Bearer", "expires_at": expires, "user": public_user(user)}

    @router.get("/api/companion/auth/me")
    def companion_me(request: Request):
        uid = current_user_id(request)
        if not uid:
            raise HTTPException(status_code=401, detail={"code": "unauthorized"})
        token = bearer_token(request)
        conn = get_db()
        try:
            user = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            if not user or (user["account_status"] or "active").lower() != "active":
                raise HTTPException(status_code=401, detail={"code": "unauthorized"})
            if token:  # sliding expiry for app sessions
                now = datetime.now(tz)
                conn.execute("UPDATE user_sessions SET expires_at=?, last_used_at=? WHERE token_hash=?",
                             ((now + timedelta(days=COMPANION_SESSION_DAYS)).isoformat(), now.isoformat(), hash_token(token)))
                conn.commit()
        finally:
            conn.close()
        return {"user": public_user(user)}

    @router.post("/api/companion/auth/logout")
    def companion_logout(request: Request):
        token = bearer_token(request)
        if token:
            conn = get_db()
            conn.execute("DELETE FROM user_sessions WHERE token_hash=?", (hash_token(token),))
            conn.commit()
            conn.close()
        return {"success": True}

    @router.post("/api/auth/set-password")
    def set_password(payload: SetPassword, request: Request):
        """Create or change the password. Requires an already-authenticated session."""
        uid = current_user_id(request)
        if not uid:
            raise HTTPException(status_code=401, detail="Login required")
        conn = get_db()
        try:
            user = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            if not user:
                raise HTTPException(status_code=401, detail="Login required")
            if user["password_hash"]:
                if not payload.current_password:
                    raise HTTPException(status_code=400, detail="اكتب كلمة المرور الحالية.")
                check_password_with_lockout(conn, user, payload.current_password, tz, audit)
            err = validate_new_password(payload.new_password, username=user["username"], public_id=user["public_user_id"])
            if err:
                raise HTTPException(status_code=400, detail=err)
            now = datetime.now(tz).isoformat()
            conn.execute("UPDATE users SET password_hash=?, password_set_at=?, failed_login_count=0, locked_until='' WHERE id=?",
                         (hash_password(payload.new_password), now, uid))
            # Sign every other device out; keep the one making this request.
            keep = request.cookies.get("st_user_session", "") or bearer_token(request)
            if keep:
                conn.execute("DELETE FROM user_sessions WHERE user_id=? AND token_hash!=?", (uid, hash_token(keep)))
            else:
                conn.execute("DELETE FROM user_sessions WHERE user_id=?", (uid,))
            conn.commit()
        finally:
            conn.close()
        audit(uid, "password_set", "")
        return {"success": True, "public_user_id": user["public_user_id"]}

    return router


# ----------------------------------------------------------------------------
# App downloads (files are placed by the site owner in COMPANION_DOWNLOAD_DIR)
# ----------------------------------------------------------------------------
_DOWNLOAD_TYPES = {".apk": "android", ".exe": "windows", ".msix": "windows", ".msi": "windows", ".zip": "windows"}
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._\-]{1,120}$")


def _scan_downloads(directory: str) -> list:
    items = []
    if not os.path.isdir(directory):
        return items
    for name in os.listdir(directory):
        ext = os.path.splitext(name)[1].lower()
        path = os.path.join(directory, name)
        if ext in _DOWNLOAD_TYPES and _SAFE_NAME.match(name) and os.path.isfile(path):
            st = os.stat(path)
            m = re.search(r"(\d+\.\d+(?:\.\d+)*)", name)
            items.append({"platform": _DOWNLOAD_TYPES[ext], "file": name, "size": st.st_size,
                          "version": m.group(1) if m else "", "mtime": st.st_mtime})
    return items


def build_downloads_router(download_dir: str) -> APIRouter:
    router = APIRouter()

    @router.get("/api/companion/downloads")
    def list_downloads():
        newest = {}
        for it in _scan_downloads(download_dir):
            if it["platform"] not in newest or it["mtime"] > newest[it["platform"]]["mtime"]:
                newest[it["platform"]] = it
        return {"downloads": {k: {kk: vv for kk, vv in v.items() if kk != "mtime"} for k, v in newest.items()}}

    @router.get("/downloads/{filename}")
    def get_download(filename: str):
        ext = os.path.splitext(filename)[1].lower()
        path = os.path.join(download_dir, filename)
        if ext not in _DOWNLOAD_TYPES or not _SAFE_NAME.match(filename) or not os.path.isfile(path):
            raise HTTPException(status_code=404, detail="File not found")
        return FileResponse(path, filename=filename, media_type="application/octet-stream")

    return router
