"""Login must never freeze the site.  Run:  pip install pytest httpx   then   pytest -q test_login_stability.py

History: the 5th wrong password used to take 30 seconds and block the whole database, because the lock-out code wrote the audit log
through a second connection while its own write was still uncommitted.  The site looked dead ("nothing happens" on the login button)."""
import importlib, os, sys, tempfile, time
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
LIMIT = 5.0          # a normal attempt takes ~0.4 s (password hashing); the bug took 30 s


@pytest.fixture()
def client(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("STESSERACT_DB_FILE", os.path.join(tmp, "t.db"))
    monkeypatch.setenv("STUDY_UPLOAD_DIR", os.path.join(tmp, "up"))
    monkeypatch.setenv("GENERATED_VIDEO_DIR", os.path.join(tmp, "vid"))
    monkeypatch.setenv("GOOGLE_API_KEY", "dummy")
    for m in ("main", "companion_auth", "exam_studio", "learning_studio"):
        sys.modules.pop(m, None)
    main = importlib.import_module("main")
    from fastapi.testclient import TestClient
    return TestClient(main.app)


def timed(fn):
    t = time.time(); r = fn(); return r, time.time() - t


def test_site_login_lockout_never_stalls(client):
    client.post("/api/auth/register-or-login", json={"username": "stab", "field": "x", "password": "Right-Pass-1"})
    from fastapi.testclient import TestClient
    fresh = TestClient(client.app)
    codes = []
    for i in range(8):
        r, dt = timed(lambda: fresh.post("/api/auth/register-or-login", json={"username": "stab", "field": "x", "password": f"wrong-pass-{i}"}))
        codes.append(r.status_code)
        assert dt < LIMIT, f"attempt {i + 1} took {dt:.1f}s - the database was blocked"
    assert codes[:5] == [401, 401, 401, 401, 401] and codes[5:] == [429, 429, 429]          # locked after 5, and says so
    r, dt = timed(lambda: fresh.get("/api/health"))
    assert r.status_code == 200 and dt < 1.0
    assert "محاولات كتير" in fresh.post("/api/auth/register-or-login", json={"username": "stab", "field": "x", "password": "Right-Pass-1"}).json()["detail"]


def test_the_lock_event_is_really_written_to_the_audit_log(client):
    import sqlite3, main
    client.post("/api/auth/register-or-login", json={"username": "aud", "field": "x", "password": "Right-Pass-1"})
    for i in range(5):
        client.post("/api/auth/register-or-login", json={"username": "aud", "field": "x", "password": f"bad-pass-{i}"})
    cx = sqlite3.connect(main.DB_FILE)
    assert cx.execute("SELECT COUNT(*) FROM admin_audit_log WHERE event_type='login_locked'").fetchone()[0] == 1   # it used to be lost (lock timeout)


def test_app_login_lockout_never_stalls(client):
    import sqlite3, main
    client.post("/api/auth/register-or-login", json={"username": "app1", "field": "x", "password": "Right-Pass-1"})
    pid = sqlite3.connect(main.DB_FILE).execute("SELECT public_user_id FROM users WHERE username='app1'").fetchone()[0]
    for i in range(7):
        r, dt = timed(lambda: client.post("/api/companion/auth/login", json={"public_user_id": pid, "password": f"nope-nope-{i}"}))
        assert dt < LIMIT, f"companion attempt {i + 1} took {dt:.1f}s"
    assert r.status_code == 429


def test_login_page_and_modal_show_errors_without_alert(client):
    html = client.get("/").text
    assert 'id="authError"' in html and "showAuthError" in html and 'href="/login"' in html
    page = client.get("/login")
    assert page.status_code == 200 and "no-store" in page.headers["cache-control"] and 'id="go"' in page.text and "/api/auth/register-or-login" in page.text
    assert "cdn.tailwindcss.com" not in page.text and "<script src=" not in page.text          # nothing external that could fail to load
