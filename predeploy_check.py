"""Static pre-deployment checks for S-TESSERACT. Does not contact production or mutate a database."""
from pathlib import Path
import ast
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = ["main.py", "companion_auth.py", "exam_studio.py", "learning_studio.py", "requirements.txt"]
missing = [name for name in REQUIRED if not (ROOT / name).is_file()]
if missing:
    print("FAIL missing required files:", ", ".join(missing)); sys.exit(1)
for name in ["main.py", "companion_auth.py", "exam_studio.py", "learning_studio.py"]:
    path = ROOT / name
    try:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        print(f"FAIL Python syntax {name}:{exc.lineno}: {exc.msg}"); sys.exit(1)
print("PASS Python syntax for required modules")
main_text = (ROOT / "main.py").read_text(encoding="utf-8")
for marker in ["dailyLessons", "professionalWorkSimulation", "finalJobReadinessAssessment", "/api/gps/daily-assessment-result", "/api/gps/professional-assessment/submit", "/api/gps/generate-roadmap"]:
    if marker not in main_text:
        print("FAIL missing roadmap feature marker:", marker); sys.exit(1)
print("PASS roadmap daily assessment / professional simulation / capstone markers")
node = shutil.which("node")
blocks = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", main_text, flags=re.S | re.I)
if node:
    checked = 0
    for i, block in enumerate(blocks):
        if not block.strip(): continue
        tmp = ROOT / f".predeploy_script_{i}.js"
        try:
            tmp.write_text(block, encoding="utf-8")
            result = subprocess.run([node, "--check", str(tmp)], capture_output=True, text=True)
            if result.returncode:
                print(f"FAIL JavaScript syntax in script block {i}:\n{result.stderr}"); sys.exit(1)
            checked += 1
        finally:
            tmp.unlink(missing_ok=True)
    print(f"PASS JavaScript syntax ({checked} non-empty inline script blocks)")
else:
    print("WARN Node.js not installed; inline JavaScript syntax was not checked")
# The checks above read the SOURCE of main.py. The browser receives something different: the page is a normal (non-raw) Python
# string, so a JavaScript line written as  join('\n')  silently becomes a real line break and the whole script dies with
# "Invalid or unexpected token" (blank page). So also check the page exactly as the server sends it.
import asyncio, inspect, os, tempfile
_tmp = tempfile.TemporaryDirectory(prefix="stess_predeploy_")
os.environ.setdefault("STESSERACT_DB_FILE", str(Path(_tmp.name) / "predeploy.db"))
os.environ.setdefault("GENERATED_VIDEO_DIR", str(Path(_tmp.name) / "videos"))
os.environ.setdefault("STUDY_UPLOAD_DIR", str(Path(_tmp.name) / "uploads"))
os.environ.setdefault("GOOGLE_API_KEY", "predeploy-check-key")
sys.path.insert(0, str(ROOT))
try:
    import main as _main
    _page = _main.serve_ui()
    if inspect.isawaitable(_page):
        _page = asyncio.run(_page)
    _served = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", str(_page), flags=re.S | re.I)
    if node:
        for i, block in enumerate(_served):
            if not block.strip():
                continue
            tmp_js = Path(_tmp.name) / f"served_{i}.js"
            tmp_js.write_text(block, encoding="utf-8")
            result = subprocess.run([node, "--check", str(tmp_js)], capture_output=True, text=True)
            if result.returncode:
                print(f"FAIL JavaScript syntax in the page AS SERVED (script block {i}). The site would load as a blank page.\n{result.stderr[:900]}"); sys.exit(1)
        _login_scripts = re.findall(r"<script[^>]*>(.*?)</script>", getattr(_main, "LOGIN_PAGE", ""), flags=re.S | re.I)
        for i, block in enumerate(_login_scripts):
            tmp_js = Path(_tmp.name) / f"login_{i}.js"
            tmp_js.write_text(block, encoding="utf-8")
            result = subprocess.run([node, "--check", str(tmp_js)], capture_output=True, text=True)
            if result.returncode:
                print(f"FAIL JavaScript syntax in the /login page.\n{result.stderr[:900]}"); sys.exit(1)
        print(f"PASS JavaScript syntax of the page as served to the browser ({len(_served)} inline script blocks) and of /login ({len(_login_scripts)})")
    else:
        print("WARN Node.js not installed; the served page was not syntax-checked")
except SystemExit:
    raise
except Exception as exc:
    print(f"FAIL could not build the served page: {type(exc).__name__}: {exc}"); sys.exit(1)
finally:
    _tmp.cleanup()
print("PASS static pre-deploy checks; live AI generation and production-host behavior still require runtime verification")
