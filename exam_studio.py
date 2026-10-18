"""
Exam Studio ("الاختبارات") for S-Tesseract.

The student uploads past exams / solved examples / lecture explanations (PDF or images).
Gemini reads the files natively (layout, tables, formulas, handwriting), learns the
instructor's exam style and solution method, and writes a NEW exam in that style.
The result is printable:  exam paper, bubble answer sheet, and a model-answer key.

Everything is per-user (all endpoints require the logged-in session).
"""
import html
import json
import os
import re
import threading
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from google.genai import types
from pydantic import BaseModel, Field

try:  # optional: validates PDFs and counts pages
    import fitz  # PyMuPDF
except Exception:  # pragma: no cover
    fitz = None

# ----------------------------------------------------------------------------
# Limits (override with env vars)
# ----------------------------------------------------------------------------
MAX_FILE_BYTES = int(os.environ.get("EXAM_MAX_FILE_MB", "30")) * 1024 * 1024
MAX_FILES_PER_UPLOAD = 10
MAX_SOURCES_PER_USER = int(os.environ.get("EXAM_MAX_SOURCES", "60"))
MAX_SOURCES_PER_EXAM = 8
MAX_TOTAL_BYTES_PER_EXAM = 80 * 1024 * 1024
INLINE_LIMIT_BYTES = 14 * 1024 * 1024        # above this total, files go through the Gemini Files API
DAILY_EXAM_LIMIT = int(os.environ.get("EXAM_DAILY_LIMIT", "10"))
MAX_QUESTIONS = 60
STALE_MINUTES = 20

# Strongest models first; main.generate_ai_json falls through to the next one if a model is unavailable.
EXAM_MODELS = [m.strip() for m in os.environ.get(
    "EXAM_MODELS", "gemini-3.8-pro,gemini-2.5-pro,gemini-3.8-flash,gemini-3.7-flash,gemini-2.5-flash"
).split(",") if m.strip()]

KINDS = {
    "past_exam": "Past exam and/or its solution (shows exactly how the instructor writes questions and marks)",
    "explanation": "Lecture / explanation / notes (the material to be examined)",
    "other": "Other supporting material",
}
QTYPES = ["mcq", "true_false", "fill_blank", "short_answer", "problem", "essay"]
AR_LETTERS = ["أ", "ب", "ج", "د", "هـ", "و"]
EN_LETTERS = ["A", "B", "C", "D", "E", "F"]
_ALLOWED_EXT = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg", ".webp": "image/webp"}


# ----------------------------------------------------------------------------
# Schema
# ----------------------------------------------------------------------------
def migrate(get_db) -> None:
    conn = get_db()
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS exam_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            filename TEXT NOT NULL, kind TEXT DEFAULT 'explanation', path TEXT NOT NULL,
            mime TEXT DEFAULT '', size INTEGER DEFAULT 0, pages INTEGER DEFAULT 0, created_at TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS exams (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            title TEXT DEFAULT '', status TEXT DEFAULT 'queued', error TEXT DEFAULT '',
            spec_json TEXT DEFAULT '{}', source_ids TEXT DEFAULT '[]', exam_json TEXT DEFAULT '',
            warnings TEXT DEFAULT '[]', created_at TEXT NOT NULL, finished_at TEXT DEFAULT '')""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_exam_sources_user ON exam_sources(user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_exams_user ON exams(user_id)")
        conn.execute("""CREATE TABLE IF NOT EXISTS exam_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, exam_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            answers_json TEXT NOT NULL DEFAULT '{}', score REAL NOT NULL DEFAULT 0,
            auto_max_score REAL NOT NULL DEFAULT 0, pending_marks REAL NOT NULL DEFAULT 0,
            total_marks REAL NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'submitted',
            started_at TEXT NOT NULL, submitted_at TEXT NOT NULL,
            FOREIGN KEY(exam_id) REFERENCES exams(id) ON DELETE CASCADE)""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_exam_attempts_user ON exam_attempts(user_id, exam_id)")
        # A restart kills background threads; never leave jobs spinning forever.
        conn.execute("UPDATE exams SET status='failed', error='السيرفر اتعمله إعادة تشغيل أثناء التوليد. جرّب تاني.' "
                     "WHERE status IN ('queued','generating')")
        conn.commit()
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# Prompt
# ----------------------------------------------------------------------------
def build_prompt(spec: dict, n_files: int) -> str:
    counts = {k: v for k, v in (spec.get("type_counts") or {}).items() if v}
    if counts:
        mix = "Use EXACTLY this number of questions per type: " + ", ".join(f"{k}={v}" for k, v in counts.items()) + "."
    else:
        mix = ("Choose the mix of question types yourself so it matches the past exams in the files "
               "(if there are no past exams, use a realistic university-style mix).")
    diff = {
        "like_sources": "the same difficulty as the past exams in the files",
        "easy": "easier than a typical final exam", "medium": "medium difficulty",
        "hard": "harder than a typical final exam, with multi-step reasoning",
    }.get(spec.get("difficulty"), "the same difficulty as the past exams in the files")
    lang = {"ar": "Arabic", "en": "English"}.get(spec.get("language"))
    lang_rule = (f"Write the whole exam in {lang}." if lang else
                 "Write in the same language as the files (if they mix Arabic and English technical terms, mix them the same way).")
    marks_rule = (f"The question marks must add up to exactly {spec['total_marks']}." if spec.get("total_marks")
                  else "Choose sensible marks per question; they should add up to a round total.")
    notes = (spec.get("notes") or "").strip()
    return f"""
You are the course instructor (the "doctor") who wrote and taught the attached material, and you are now writing
the exam for your own students. {n_files} file(s) are attached; each is labelled with its role just before it.

STEP 1 - READ EVERYTHING. Read every page of every file completely, including tables, formulas, diagrams,
figures, scanned pages and handwriting. Do not skim.

STEP 2 - LEARN THE INSTRUCTOR'S STYLE. From the past exams / solved examples work out, and then imitate:
the wording of questions, the types of questions, how marks are distributed, how long answers are expected to be,
the notation, units and terminology, the typical traps, and how a model solution is laid out step by step.
Your model answers must follow the instructor's own solution method, not a different method.

STEP 3 - SCOPE. Examine only what the files teach. Do not add topics that are not in the files.
Write NEW questions: never copy a question from the files; you may reuse a structure with different data.

STEP 4 - WRITE THE EXAM.
- Number of questions: EXACTLY {spec['num_questions']}.
- {mix}
- Difficulty: {diff}.
- Duration: {spec['duration_minutes']} minutes - the amount of work must fit that time.
- {marks_rule}
- {lang_rule}
- Math: write formulas in LaTeX between single dollar signs, e.g. $x^2 + 1$. Plain text otherwise.
- Allowed question types: {", ".join(QTYPES)}.
- mcq: 3 to 5 options, exactly one correct, distractors based on mistakes students really make in this material.
- true_false: a clear statement that is definitely true or definitely false.
- fill_blank: put "......" where the answer goes.
{("- Extra instructions from the student: " + notes) if notes else ""}

STEP 5 - VERIFY. Recompute every numeric answer before you output it. Make sure every question is answerable
from the files and that no two questions give away each other's answers.

Return STRICT JSON only, with exactly this shape:
{{
 "analysis": {{"style": "short description of the instructor's exam style", "topics": ["..."], "difficulty": "..."}},
 "exam": {{
  "title": "...", "subject": "...", "instructions": ["..."],
  "sections": [
   {{"title": "...", "instructions": "...", "questions": [
     {{"type": "mcq|true_false|fill_blank|short_answer|problem|essay",
      "text": "the question",
      "options": ["..."],
      "marks": 2,
      "answer": "mcq: the letter A/B/C/D; true_false: \\"true\\" or \\"false\\"; others: the final model answer",
      "solution_steps": ["step 1 ...", "step 2 ..."],
      "rubric": [{{"step": "what earns marks", "marks": 1}}],
      "source": "FILE 2, page 5"}}
   ]}}
  ]
 }}
}}
"options" is only for mcq. Every question needs "answer", "solution_steps" and "rubric".
""".strip()


# ----------------------------------------------------------------------------
# Normalising the model's JSON (never trust it: coerce, clamp, escape later)
# ----------------------------------------------------------------------------
def _s(v, limit=4000) -> str:
    return ("" if v is None else str(v)).strip()[:limit]


def _marks(v, default=1.0) -> float:
    try:
        m = float(v)
    except (TypeError, ValueError):
        m = default
    m = max(0.5, min(100.0, m))
    return round(m * 2) / 2


def _is_arabic(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    return sum(1 for c in letters if "\u0600" <= c <= "\u06ff") / len(letters) > 0.3


_TRUE = {"true", "t", "yes", "correct", "صح", "صحيح", "صحيحة", "√", "✓"}
_FALSE = {"false", "f", "no", "incorrect", "خطأ", "خطا", "غلط", "خاطئة", "خاطئ", "×", "✗"}


def _mcq_index(answer, options: list):
    a = _s(answer, 300)
    if not a or not options:
        return None
    if a.isdigit() and 1 <= int(a) <= len(options):
        return int(a) - 1
    head = re.match(r"^\(?\s*([A-Fa-fأبجدهوـ]{1,2})\s*[\)\.\-:]?(\s|$)", a)
    if head:
        h = head.group(1).replace("ـ", "")
        for i, (en, ar) in enumerate(zip(EN_LETTERS, AR_LETTERS)):
            if i < len(options) and (h.upper() == en or h == ar.replace("ـ", "")):
                return i
    for i, o in enumerate(options):
        if a.strip().lower() == o.strip().lower():
            return i
    return None


def normalize_exam(raw: dict, spec: dict):
    """Return (exam_dict, warnings). Raises ValueError if there is nothing usable."""
    warnings = []
    exam = raw.get("exam") if isinstance(raw, dict) else None
    if not isinstance(exam, dict):
        raise ValueError("Gemini did not return an exam.")
    sections_raw = exam.get("sections")
    if not isinstance(sections_raw, list) or not sections_raw:
        flat = exam.get("questions")
        sections_raw = [{"title": "", "instructions": "", "questions": flat}] if isinstance(flat, list) else []

    all_text = " ".join(_s(q.get("text"), 500) for sec in sections_raw if isinstance(sec, dict)
                        for q in (sec.get("questions") or []) if isinstance(q, dict))
    lang = {"ar": "ar", "en": "en"}.get(spec.get("language")) or ("ar" if _is_arabic(all_text) else "en")

    sections, qid = [], 0
    for sec in sections_raw:
        if not isinstance(sec, dict):
            continue
        questions = []
        for q in sec.get("questions") or []:
            if not isinstance(q, dict) or not _s(q.get("text")):
                continue
            qtype = _s(q.get("type"), 30).lower().replace("-", "_").replace(" ", "_")
            if qtype not in QTYPES:
                qtype = "short_answer"
            options = [_s(o, 600) for o in (q.get("options") or []) if _s(o)] if qtype == "mcq" else []
            options = options[:6]
            if qtype == "mcq" and len(options) < 2:
                qtype = "short_answer"
                options = []
            answer = _s(q.get("answer"), 3000)
            answer_index = None
            if qtype == "mcq":
                answer_index = _mcq_index(q.get("answer"), options)
                if answer_index is None:
                    warnings.append(f"سؤال {qid + 1}: الإجابة الصحيحة مش واضحة، راجعها.")
                else:
                    answer = options[answer_index]
            elif qtype == "true_false":
                low = answer.lower()
                answer = "true" if low in _TRUE else "false" if low in _FALSE else ""
                if not answer:
                    warnings.append(f"سؤال {qid + 1}: إجابة صح/خطأ مش واضحة، راجعها.")
            steps = [_s(x, 1500) for x in (q.get("solution_steps") or []) if _s(x)][:15]
            rubric = []
            for r in (q.get("rubric") or [])[:12]:
                if isinstance(r, dict) and _s(r.get("step")):
                    rubric.append({"step": _s(r.get("step"), 600), "marks": _marks(r.get("marks"), 0.5)})
                elif isinstance(r, str) and r.strip():
                    rubric.append({"step": r.strip()[:600], "marks": 0.5})
            qid += 1
            questions.append({"id": qid, "type": qtype, "text": _s(q.get("text")), "options": options,
                              "marks": _marks(q.get("marks")), "answer": answer, "answer_index": answer_index,
                              "solution_steps": steps, "rubric": rubric, "source": _s(q.get("source"), 200)})
        if questions:
            sections.append({"title": _s(sec.get("title"), 200), "instructions": _s(sec.get("instructions"), 800),
                             "questions": questions})
    if not sections:
        raise ValueError("Gemini returned an exam with no usable questions.")

    # honour the requested count exactly (drop extras, warn when short)
    expected = spec["num_questions"]
    flat_total = sum(len(s["questions"]) for s in sections)
    if flat_total > expected:
        keep = expected
        for s in sections:
            s["questions"] = s["questions"][:keep]
            keep = max(0, keep - len(s["questions"]))
        sections = [s for s in sections if s["questions"]]
        n = 0
        for s in sections:
            for q in s["questions"]:
                n += 1
                q["id"] = n
        flat_total = n
    if flat_total < expected:
        warnings.append(f"Gemini كتب {flat_total} سؤال بس من {expected} مطلوبين. ولّد تاني لو عايز العدد كامل.")

    total = sum(q["marks"] for s in sections for q in s["questions"])
    if spec.get("total_marks") and abs(total - spec["total_marks"]) > 0.01:
        warnings.append(f"مجموع الدرجات {total:g} بدل {spec['total_marks']} المطلوبة.")

    instr = [_s(i, 400) for i in (exam.get("instructions") or []) if _s(i)][:10]
    analysis = raw.get("analysis") if isinstance(raw.get("analysis"), dict) else {}
    return {
        "title": _s(spec.get("title") or exam.get("title"), 200) or "Exam",
        "subject": _s(exam.get("subject"), 200),
        "lang": lang,
        "duration_minutes": spec["duration_minutes"],
        "total_marks": total,
        "instructions": instr,
        "sections": sections,
        "analysis": {"style": _s(analysis.get("style"), 1500),
                     "topics": [_s(t, 200) for t in (analysis.get("topics") or [])[:30]],
                     "difficulty": _s(analysis.get("difficulty"), 200)},
    }, warnings


# ----------------------------------------------------------------------------
# Printable pages
# ----------------------------------------------------------------------------
LABELS = {
    "ar": {"dir": "rtl", "name": "الاسم", "id": "رقم الجلوس / الكود", "date": "التاريخ", "time": "الزمن",
           "minutes": "دقيقة", "marks": "الدرجة", "mark": "درجة", "instr": "تعليمات", "tf": ("صح", "خطأ"),
           "paper": "ورقة الامتحان", "sheet": "ورقة الإجابة", "key": "نموذج الإجابة", "print": "🖨️ طباعة / حفظ PDF",
           "fill": "ظلّل الدائرة المناسبة بالكامل. للأسئلة المقالية اكتب إجابتك في المساحة المخصصة.",
           "written": "الأسئلة المقالية", "q": "سؤال", "answer": "الإجابة", "steps": "خطوات الحل",
           "rubric": "توزيع الدرجات", "grade": "جدول التصحيح", "got": "الدرجة المستحقة", "max": "الدرجة", "total": "المجموع",
           "src": "المصدر", "no": "رقم", "tfq": "صح أم خطأ", "style": "أسلوب الدكتور اللي اتحاكى"},
    "en": {"dir": "ltr", "name": "Name", "id": "Seat No. / ID", "date": "Date", "time": "Time",
           "minutes": "minutes", "marks": "Marks", "mark": "mark(s)", "instr": "Instructions", "tf": ("True", "False"),
           "paper": "Exam Paper", "sheet": "Answer Sheet", "key": "Model Answer", "print": "🖨️ Print / Save PDF",
           "fill": "Fill the correct bubble completely. For written questions use the space provided.",
           "written": "Written questions", "q": "Question", "answer": "Answer", "steps": "Solution steps",
           "rubric": "Mark scheme", "grade": "Grading table", "got": "Awarded", "max": "Max", "total": "Total",
           "src": "Source", "no": "No.", "tfq": "True / False", "style": "Instructor style imitated"},
}

_PAGE_CSS = """
*{box-sizing:border-box}body{margin:0;background:#e9e9ee;font-family:'Cairo','Segoe UI',Tahoma,Arial,sans-serif;color:#111;line-height:1.6}
.toolbar{position:sticky;top:0;z-index:5;background:#111;color:#fff;padding:10px 16px;display:flex;gap:10px;align-items:center;justify-content:space-between}
.toolbar button,.toolbar a{background:#f5b800;color:#111;border:0;border-radius:8px;padding:8px 14px;font-weight:800;cursor:pointer;text-decoration:none;font-size:14px;font-family:inherit}
.page{background:#fff;width:210mm;min-height:297mm;margin:14px auto;padding:14mm;box-shadow:0 2px 12px rgba(0,0,0,.2)}
h1{margin:0;font-size:22px;text-align:center}h2{font-size:16px;margin:18px 0 4px;border-bottom:2px solid #111;padding-bottom:2px}
.sub{text-align:center;color:#444;font-size:13px;margin:2px 0 10px}
.meta{display:grid;grid-template-columns:1fr 1fr;gap:6px 18px;font-size:13px;margin:10px 0;border:1.5px solid #111;padding:8px 10px;border-radius:6px}
.fill{border-bottom:1px solid #111;display:inline-block;min-width:150px;height:1.1em}
.instr{font-size:12.5px;margin:8px 0;padding:6px 10px;background:#f5f5f5;border-radius:6px}.instr ul{margin:2px 0;padding-inline-start:20px}
.q{margin:10px 0 14px;break-inside:avoid;font-size:14px}.qh{display:flex;gap:6px}.qn{font-weight:800;min-width:1.8em}
.qt{flex:1}.mk{font-size:12px;color:#333;white-space:nowrap;font-weight:700}
.opts{margin:4px 0 0;padding-inline-start:2.2em}.opts div{margin:1px 0}.tf{margin:4px 2.2em;display:flex;gap:24px}
.box{display:inline-block;width:14px;height:14px;border:1.5px solid #111;border-radius:3px;vertical-align:-2px;margin-inline-end:6px}
.lines{margin:6px 0 0 0;height:calc(var(--n)*1.9em);background:repeating-linear-gradient(to bottom,transparent 0,transparent calc(1.9em - 1px),#999 calc(1.9em - 1px),#999 1.9em)}
.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:4px 28px;margin:8px 0}
.row{display:flex;align-items:center;gap:8px;font-size:13px;padding:2px 0;border-bottom:1px dotted #bbb}.row b{min-width:2.2em}
.bub{display:inline-flex;align-items:center;justify-content:center;width:22px;height:22px;border:1.5px solid #111;border-radius:50%;font-size:11px;font-weight:700}
.ans{background:#f1fbf3;border:1px solid #b9e3c2;border-radius:6px;padding:6px 10px;margin:6px 0;font-size:13px}
ol.steps{margin:4px 0;padding-inline-start:22px;font-size:13px}
table{border-collapse:collapse;width:100%;font-size:12.5px;margin:6px 0}th,td{border:1px solid #444;padding:3px 6px;text-align:center}
.small{font-size:11.5px;color:#555}
@page{size:A4;margin:12mm}
@media print{body{background:#fff}.toolbar{display:none}.page{box-shadow:none;margin:0;width:auto;min-height:0;padding:0}}
"""

_KATEX = r"""
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/KaTeX/0.16.9/katex.min.css">
<script defer src="https://cdnjs.cloudflare.com/ajax/libs/KaTeX/0.16.9/katex.min.js"></script>
<script defer src="https://cdnjs.cloudflare.com/ajax/libs/KaTeX/0.16.9/contrib/auto-render.min.js"
 onload="renderMathInElement(document.body,{throwOnError:false,delimiters:[{left:'$$',right:'$$',display:true},{left:'$',right:'$',display:false},{left:'\\(',right:'\\)',display:false},{left:'\\[',right:'\\]',display:true}]})"></script>
"""


def _e(text) -> str:
    return html.escape(str(text or ""), quote=True).replace("\n", "<br>")


def _fmt(n: float) -> str:
    return f"{n:g}"


def _letters(lang):
    return AR_LETTERS if lang == "ar" else EN_LETTERS


def _lines_for(q) -> int:
    base = {"short_answer": 3, "problem": 8, "essay": 12}.get(q["type"], 0)
    if not base:
        return 0
    return min(18, max(base, int(q["marks"] * 2))) if q["type"] != "short_answer" else min(8, max(3, int(q["marks"] * 1.5)))


def _page_shell(title: str, body: str, lang: str, L: dict) -> str:
    return (f'<!doctype html><html lang="{lang}" dir="{L["dir"]}"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{_e(title)}</title>'
            '<link rel="preconnect" href="https://fonts.googleapis.com">'
            '<link href="https://fonts.googleapis.com/css2?family=Cairo:wght@400;700;800&display=swap" rel="stylesheet">'
            f'<style>{_PAGE_CSS}</style>{_KATEX}</head><body>'
            f'<div class="toolbar"><span>{_e(title)}</span><button onclick="window.print()">{L["print"]}</button></div>'
            f'{body}</body></html>')


def _header(ex: dict, L: dict, kind: str) -> str:
    return (f'<h1>{_e(ex["title"])}</h1>'
            f'<div class="sub">{_e(ex["subject"])}{" - " if ex["subject"] else ""}{L[kind]}</div>'
            f'<div class="meta"><div>{L["name"]}: <span class="fill"></span></div>'
            f'<div>{L["id"]}: <span class="fill"></span></div>'
            f'<div>{L["date"]}: <span class="fill" style="min-width:110px"></span></div>'
            f'<div>{L["time"]}: {ex["duration_minutes"]} {L["minutes"]} &nbsp;|&nbsp; {L["marks"]}: {_fmt(ex["total_marks"])}</div></div>')


def render_paper(ex: dict, space: bool = True) -> str:
    L = LABELS[ex["lang"]]
    letters = _letters(ex["lang"])
    parts = [f'<div class="page">{_header(ex, L, "paper")}']
    if ex["instructions"]:
        parts.append(f'<div class="instr"><b>{L["instr"]}</b><ul>' + "".join(f"<li>{_e(i)}</li>" for i in ex["instructions"]) + "</ul></div>")
    for sec in ex["sections"]:
        if sec["title"]:
            parts.append(f'<h2>{_e(sec["title"])}</h2>')
        if sec["instructions"]:
            parts.append(f'<div class="small">{_e(sec["instructions"])}</div>')
        for q in sec["questions"]:
            parts.append(f'<div class="q"><div class="qh"><span class="qn">{q["id"]}.</span><span class="qt">{_e(q["text"])}</span>'
                         f'<span class="mk">[{_fmt(q["marks"])}]</span></div>')
            if q["type"] == "mcq":
                parts.append('<div class="opts">' + "".join(
                    f'<div><b>{letters[i]})</b> {_e(o)}</div>' for i, o in enumerate(q["options"])) + "</div>")
            elif q["type"] == "true_false":
                parts.append(f'<div class="tf"><span><span class="box"></span>{L["tf"][0]}</span><span><span class="box"></span>{L["tf"][1]}</span></div>')
            elif space and _lines_for(q):
                parts.append(f'<div class="lines" style="--n:{_lines_for(q)}"></div>')
            parts.append("</div>")
    parts.append("</div>")
    return _page_shell(f'{ex["title"]} - {L["paper"]}', "".join(parts), ex["lang"], L)


def render_sheet(ex: dict) -> str:
    L = LABELS[ex["lang"]]
    letters = _letters(ex["lang"])
    objective, written = [], []
    for sec in ex["sections"]:
        for q in sec["questions"]:
            (objective if q["type"] in ("mcq", "true_false") else written).append(q)
    parts = [f'<div class="page">{_header(ex, L, "sheet")}<div class="instr">{L["fill"]}</div>']
    if objective:
        parts.append('<div class="grid">')
        for q in objective:
            labels = letters[:len(q["options"])] if q["type"] == "mcq" else list(L["tf"])
            bubbles = "".join(f'<span class="bub" style="{"width:auto;padding:0 6px;border-radius:12px" if q["type"] == "true_false" else ""}">{_e(x)}</span>' for x in labels)
            parts.append(f'<div class="row"><b>{q["id"]}.</b>{bubbles}</div>')
        parts.append("</div>")
    if written:
        parts.append(f'<h2>{L["written"]}</h2>')
        for q in written:
            n = _lines_for(q) or 4
            parts.append(f'<div class="q"><div class="qh"><span class="qn">{q["id"]}.</span><span class="mk">[{_fmt(q["marks"])}]</span></div>'
                         f'<div class="lines" style="--n:{n}"></div></div>')
    parts.append("</div>")
    return _page_shell(f'{ex["title"]} - {L["sheet"]}', "".join(parts), ex["lang"], L)


def render_key(ex: dict) -> str:
    L = LABELS[ex["lang"]]
    letters = _letters(ex["lang"])
    parts = [f'<div class="page">{_header(ex, L, "key")}']
    if ex["analysis"].get("style"):
        parts.append(f'<div class="instr"><b>{L["style"]}:</b> {_e(ex["analysis"]["style"])}</div>')
    for sec in ex["sections"]:
        if sec["title"]:
            parts.append(f'<h2>{_e(sec["title"])}</h2>')
        for q in sec["questions"]:
            if q["type"] == "mcq":
                ans = (f'{letters[q["answer_index"]]}) ' if q["answer_index"] is not None else "") + _e(q["answer"])
            elif q["type"] == "true_false":
                ans = L["tf"][0] if q["answer"] == "true" else L["tf"][1] if q["answer"] == "false" else "?"
            else:
                ans = _e(q["answer"])
            parts.append(f'<div class="q"><div class="qh"><span class="qn">{q["id"]}.</span><span class="qt">{_e(q["text"])}</span>'
                         f'<span class="mk">[{_fmt(q["marks"])}]</span></div>')
            if q["type"] == "mcq":
                parts.append('<div class="opts">' + "".join(
                    f'<div><b>{letters[i]})</b> {_e(o)}</div>' for i, o in enumerate(q["options"])) + "</div>")
            parts.append(f'<div class="ans"><b>{L["answer"]}:</b> {ans}')
            if q["solution_steps"]:
                parts.append(f'<div style="margin-top:4px"><b>{L["steps"]}:</b></div><ol class="steps">' +
                             "".join(f"<li>{_e(s)}</li>" for s in q["solution_steps"]) + "</ol>")
            if q["rubric"]:
                parts.append(f'<table><tr><th>{L["rubric"]}</th><th>{L["mark"]}</th></tr>' +
                             "".join(f'<tr><td style="text-align:start">{_e(r["step"])}</td><td>{_fmt(r["marks"])}</td></tr>' for r in q["rubric"]) + "</table>")
            if q["source"]:
                parts.append(f'<div class="small">{L["src"]}: {_e(q["source"])}</div>')
            parts.append("</div></div>")
    # grading table
    rows = "".join(f'<tr><td>{q["id"]}</td><td>{_fmt(q["marks"])}</td><td></td></tr>' for s in ex["sections"] for q in s["questions"])
    parts.append(f'<h2>{L["grade"]}</h2><table><tr><th>{L["no"]}</th><th>{L["max"]}</th><th>{L["got"]}</th></tr>{rows}'
                 f'<tr><th>{L["total"]}</th><th>{_fmt(ex["total_marks"])}</th><th></th></tr></table></div>')
    return _page_shell(f'{ex["title"]} - {L["key"]}', "".join(parts), ex["lang"], L)


# ----------------------------------------------------------------------------
# Router
# ----------------------------------------------------------------------------
class OnlineExamSubmission(BaseModel):
    answers: dict[str, str] = Field(default_factory=dict)


class GenerateExam(BaseModel):
    title: str = ""
    source_ids: list[int]
    num_questions: int = 20
    type_counts: dict[str, int] = Field(default_factory=dict)
    duration_minutes: int = 60
    total_marks: int = 0
    difficulty: str = "like_sources"
    language: str = "auto"
    notes: str = ""


def _magic_ok(ext: str, head: bytes) -> bool:
    if ext == ".pdf":
        return head.startswith(b"%PDF")
    if ext == ".png":
        return head.startswith(b"\x89PNG")
    if ext in (".jpg", ".jpeg"):
        return head.startswith(b"\xff\xd8")
    if ext == ".webp":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    return False


def build_router(get_db, current_user_id, gen_json, ai_client, tz, source_dir: str) -> APIRouter:
    router = APIRouter()
    os.makedirs(source_dir, exist_ok=True)

    def uid_or_401(request: Request) -> int:
        uid = current_user_id(request)
        if not uid:
            raise HTTPException(status_code=401, detail="سجّل الدخول الأول.")
        return uid

    def now_iso():
        return datetime.now(tz).isoformat()

    # ---- sources ------------------------------------------------------------
    def src_row(r):
        return {"id": r["id"], "filename": r["filename"], "kind": r["kind"], "size": r["size"],
                "pages": r["pages"], "created_at": r["created_at"]}

    @router.get("/api/exams/sources")
    def list_sources(request: Request):
        uid = uid_or_401(request)
        conn = get_db()
        rows = conn.execute("SELECT * FROM exam_sources WHERE user_id=? ORDER BY id DESC", (uid,)).fetchall()
        conn.close()
        return {"sources": [src_row(r) for r in rows]}

    @router.post("/api/exams/sources")
    async def upload_sources(request: Request, kind: str = Form("explanation"), files: list[UploadFile] = File(...)):
        uid = uid_or_401(request)
        if kind not in KINDS:
            kind = "explanation"
        if not files or len(files) > MAX_FILES_PER_UPLOAD:
            raise HTTPException(status_code=400, detail=f"ارفع من 1 لـ {MAX_FILES_PER_UPLOAD} ملفات في المرة.")
        conn = get_db()
        have = conn.execute("SELECT COUNT(*) FROM exam_sources WHERE user_id=?", (uid,)).fetchone()[0]
        conn.close()
        if have + len(files) > MAX_SOURCES_PER_USER:
            raise HTTPException(status_code=400, detail=f"الحد الأقصى {MAX_SOURCES_PER_USER} ملف. امسح ملفات قديمة الأول.")
        user_dir = os.path.join(source_dir, str(uid))
        os.makedirs(user_dir, exist_ok=True)
        saved = []
        for f in files:
            name = os.path.basename(f.filename or "file")[:150]
            ext = os.path.splitext(name)[1].lower()
            if ext not in _ALLOWED_EXT:
                raise HTTPException(status_code=400, detail=f"{name}: النوع ده مش مدعوم. ارفع PDF أو صورة (png/jpg/webp).")
            data = await f.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                raise HTTPException(status_code=400, detail=f"{name}: أكبر من {MAX_FILE_BYTES // 1048576} ميجا.")
            if not data or not _magic_ok(ext, data[:16]):
                raise HTTPException(status_code=400, detail=f"{name}: الملف تالف أو مش من النوع اللي اسمه بيقوله.")
            pages = 1
            if ext == ".pdf" and fitz is not None:
                try:
                    with fitz.open(stream=data, filetype="pdf") as doc:
                        if doc.needs_pass:
                            raise ValueError("encrypted")
                        pages = doc.page_count
                except Exception:
                    raise HTTPException(status_code=400, detail=f"{name}: الـ PDF تالف أو محمي بباسورد.")
                if pages > 900:
                    raise HTTPException(status_code=400, detail=f"{name}: أكتر من 900 صفحة.")
            path = os.path.join(user_dir, uuid.uuid4().hex + ext)
            with open(path, "wb") as out:
                out.write(data)
            saved.append((name, path, _ALLOWED_EXT[ext], len(data), pages))
        conn = get_db()
        for name, path, mime, size, pages in saved:
            conn.execute("INSERT INTO exam_sources(user_id,filename,kind,path,mime,size,pages,created_at) VALUES(?,?,?,?,?,?,?,?)",
                         (uid, name, kind, path, mime, size, pages, now_iso()))
        conn.commit()
        conn.close()
        return {"success": True, "count": len(saved)}

    @router.delete("/api/exams/sources/{sid}")
    def delete_source(sid: int, request: Request):
        uid = uid_or_401(request)
        conn = get_db()
        row = conn.execute("SELECT * FROM exam_sources WHERE id=? AND user_id=?", (sid, uid)).fetchone()
        if not row:
            conn.close()
            raise HTTPException(status_code=404, detail="الملف مش موجود.")
        conn.execute("DELETE FROM exam_sources WHERE id=?", (sid,))
        conn.commit()
        conn.close()
        try:
            os.remove(row["path"])
        except OSError:
            pass
        return {"success": True}

    # ---- generation -----------------------------------------------------------
    def _file_parts(sources):
        total = sum(s["size"] for s in sources)
        use_files_api = total > INLINE_LIMIT_BYTES
        parts = []
        for i, s in enumerate(sources, 1):
            role = KINDS.get(s["kind"], KINDS["other"]).split(" (")[0]
            parts.append(f'FILE {i}: "{s["filename"]}" - role: {role}')
            if use_files_api:
                up = ai_client.files.upload(file=s["path"], config=types.UploadFileConfig(mime_type=s["mime"]))
                for _ in range(60):  # wait until Google finished processing the file
                    state = getattr(getattr(up, "state", None), "name", "ACTIVE")
                    if state == "ACTIVE":
                        break
                    if state == "FAILED":
                        raise RuntimeError(f"Gemini could not process {s['filename']}.")
                    import time
                    time.sleep(2)
                    up = ai_client.files.get(name=up.name)
                parts.append(up)
            else:
                with open(s["path"], "rb") as fh:
                    parts.append(types.Part.from_bytes(data=fh.read(), mime_type=s["mime"]))
        return parts

    def _run(exam_id: int, spec: dict, sources: list):
        conn = get_db()
        conn.execute("UPDATE exams SET status='generating' WHERE id=?", (exam_id,))
        conn.commit()
        conn.close()
        try:
            prompt = build_prompt(spec, len(sources))
            contents = _file_parts(sources) + [prompt]
            raw = gen_json(prompt, contents, models=EXAM_MODELS)
            exam, warnings = normalize_exam(raw, spec)
            conn = get_db()
            conn.execute("UPDATE exams SET status='done', exam_json=?, warnings=?, title=?, finished_at=?, error='' WHERE id=?",
                         (json.dumps(exam, ensure_ascii=False), json.dumps(warnings, ensure_ascii=False), exam["title"], now_iso(), exam_id))
            conn.commit()
            conn.close()
        except Exception as exc:
            print(f"[exam_studio] generation {exam_id} failed: {exc}")
            msg = "فشل توليد الامتحان. جرّب تاني بعد شوية، أو قلل عدد الملفات."
            conn = get_db()
            conn.execute("UPDATE exams SET status='failed', error=?, finished_at=? WHERE id=?", (msg, now_iso(), exam_id))
            conn.commit()
            conn.close()

    def _expire_stale(conn, uid):
        cutoff = (datetime.now(tz) - timedelta(minutes=STALE_MINUTES)).isoformat()
        conn.execute("UPDATE exams SET status='failed', error='التوليد أخد وقت طويل وتوقف. جرّب تاني.' "
                     "WHERE user_id=? AND status IN ('queued','generating') AND created_at<?", (uid, cutoff))
        conn.commit()

    @router.post("/api/exams/generate")
    def generate(payload: GenerateExam, request: Request):
        uid = uid_or_401(request)
        counts = {k: max(0, min(MAX_QUESTIONS, int(v))) for k, v in payload.type_counts.items() if k in QTYPES}
        counts = {k: v for k, v in counts.items() if v}
        n = sum(counts.values()) if counts else payload.num_questions
        if not 1 <= n <= MAX_QUESTIONS:
            raise HTTPException(status_code=400, detail=f"عدد الأسئلة لازم يبقى من 1 لـ {MAX_QUESTIONS}.")
        ids = list(dict.fromkeys(payload.source_ids))
        if not ids or len(ids) > MAX_SOURCES_PER_EXAM:
            raise HTTPException(status_code=400, detail=f"اختار من 1 لـ {MAX_SOURCES_PER_EXAM} ملفات.")
        conn = get_db()
        _expire_stale(conn, uid)
        rows = conn.execute(f"SELECT * FROM exam_sources WHERE user_id=? AND id IN ({','.join('?' * len(ids))})", (uid, *ids)).fetchall()
        if len(rows) != len(ids):
            conn.close()
            raise HTTPException(status_code=400, detail="في ملف مش بتاعك أو اتمسح.")
        if sum(r["size"] for r in rows) > MAX_TOTAL_BYTES_PER_EXAM:
            conn.close()
            raise HTTPException(status_code=400, detail="حجم الملفات كبير. قلل عدد الملفات.")
        if conn.execute("SELECT COUNT(*) FROM exams WHERE user_id=? AND status IN ('queued','generating')", (uid,)).fetchone()[0]:
            conn.close()
            raise HTTPException(status_code=429, detail="في امتحان لسه بيتولّد. استنى لما يخلص.")
        today = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        if conn.execute("SELECT COUNT(*) FROM exams WHERE user_id=? AND created_at>=?", (uid, today)).fetchone()[0] >= DAILY_EXAM_LIMIT:
            conn.close()
            raise HTTPException(status_code=429, detail=f"وصلت للحد اليومي ({DAILY_EXAM_LIMIT} امتحانات). جرّب بكرة.")
        spec = {"title": payload.title.strip()[:200], "num_questions": n, "type_counts": counts,
                "duration_minutes": max(5, min(600, payload.duration_minutes)),
                "total_marks": max(0, min(1000, payload.total_marks)),
                "difficulty": payload.difficulty if payload.difficulty in ("like_sources", "easy", "medium", "hard") else "like_sources",
                "language": payload.language if payload.language in ("auto", "ar", "en") else "auto",
                "notes": payload.notes.strip()[:1000]}
        cur = conn.execute("INSERT INTO exams(user_id,title,status,spec_json,source_ids,created_at) VALUES(?,?,?,?,?,?)",
                           (uid, spec["title"] or "Exam", "queued", json.dumps(spec, ensure_ascii=False), json.dumps(ids), now_iso()))
        exam_id = cur.lastrowid
        conn.commit()
        sources = [{"filename": r["filename"], "kind": r["kind"], "path": r["path"], "mime": r["mime"], "size": r["size"]}
                   for r in sorted(rows, key=lambda r: ids.index(r["id"]))]
        conn.close()
        threading.Thread(target=_run, args=(exam_id, spec, sources), daemon=True).start()
        return {"success": True, "id": exam_id}

    def _owned_exam(eid: int, uid: int):
        conn = get_db()
        _expire_stale(conn, uid)
        row = conn.execute("SELECT * FROM exams WHERE id=? AND user_id=?", (eid, uid)).fetchone()
        conn.close()
        if not row:
            raise HTTPException(status_code=404, detail="الامتحان مش موجود.")
        return row

    def exam_summary(r):
        info = {"id": r["id"], "title": r["title"], "status": r["status"], "error": r["error"],
                "created_at": r["created_at"], "warnings": json.loads(r["warnings"] or "[]")}
        if r["status"] == "done" and r["exam_json"]:
            ex = json.loads(r["exam_json"])
            info.update(n_questions=sum(len(s["questions"]) for s in ex["sections"]),
                        total_marks=ex["total_marks"], duration_minutes=ex["duration_minutes"])
        return info

    @router.get("/api/exams")
    def list_exams(request: Request):
        uid = uid_or_401(request)
        conn = get_db()
        _expire_stale(conn, uid)
        rows = conn.execute("SELECT * FROM exams WHERE user_id=? ORDER BY id DESC LIMIT 50", (uid,)).fetchall()
        conn.close()
        return {"exams": [exam_summary(r) for r in rows]}

    @router.get("/api/exams/{eid}")
    def get_exam(eid: int, request: Request):
        row = _owned_exam(eid, uid_or_401(request))
        out = exam_summary(row)
        if row["status"] == "done":
            out["exam"] = json.loads(row["exam_json"])
        return out

    @router.delete("/api/exams/{eid}")
    def delete_exam(eid: int, request: Request):
        uid = uid_or_401(request)
        conn = get_db()
        conn.execute("DELETE FROM exam_attempts WHERE exam_id=? AND user_id=?", (eid, uid))
        conn.execute("DELETE FROM exams WHERE id=? AND user_id=?", (eid, uid))
        conn.commit()
        conn.close()
        return {"success": True}

    def _page(eid: int, request: Request, renderer):
        row = _owned_exam(eid, uid_or_401(request))
        if row["status"] != "done":
            raise HTTPException(status_code=409, detail="الامتحان لسه مخلصش.")
        resp = HTMLResponse(renderer(json.loads(row["exam_json"])))
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @router.get("/exam/{eid}/online", response_class=HTMLResponse)
    def online_exam(eid: int, request: Request):
        row = _owned_exam(eid, uid_or_401(request))
        if row["status"] != "done" or not row["exam_json"]:
            raise HTTPException(status_code=409, detail="الامتحان لسه مخلصش.")
        ex = json.loads(row["exam_json"])
        L = LABELS[ex["lang"]]
        questions = []
        parts = [f'<main class="page"><h1>{_e(ex["title"])}</h1><div class="sub">{_e(ex["subject"])} · اختبار إلكتروني</div>',
                 f'<div class="meta"><div>الزمن: <b id="countdown">{int(ex["duration_minutes"])}:00</b></div><div>الدرجة الكلية: {_fmt(ex["total_marks"])}</div></div>',
                 '<div class="instr">اقرأ السؤال كويس، جاوب على الأسئلة، واضغط «تسليم الاختبار». الاختيار من متعدد وصح/خطأ بيتصححوا تلقائيًا، والإجابات الكتابية تحتاج مراجعة.</div>',
                 '<form id="onlineExamForm">']
        qindex = 0
        for sec in ex["sections"]:
            if sec["title"]: parts.append(f'<h2>{_e(sec["title"])}</h2>')
            if sec["instructions"]: parts.append(f'<p class="small">{_e(sec["instructions"])}</p>')
            for q in sec["questions"]:
                qindex += 1
                questions.append({"id": q["id"], "type": q["type"], "marks": q["marks"], "options": q.get("options", [])})
                parts.append(f'<section class="q"><div class="qh"><span class="qn">{qindex}.</span><span class="qt">{_e(q["text"])}</span><span class="mk">[{_fmt(q["marks"])}]</span></div>')
                qid = str(q["id"])
                if q["type"] == "mcq":
                    parts.append('<div class="opts">')
                    for oi, opt in enumerate(q["options"]):
                        parts.append(f'<label class="answerOption"><input type="radio" name="q_{qid}" value="{oi}"> {_e(opt)}</label>')
                    parts.append('</div>')
                elif q["type"] == "true_false":
                    true_label, false_label = L["tf"]
                    parts.append(f'<div class="opts"><label class="answerOption"><input type="radio" name="q_{qid}" value="true"> {_e(true_label)}</label><label class="answerOption"><input type="radio" name="q_{qid}" value="false"> {_e(false_label)}</label></div>')
                else:
                    parts.append(f'<textarea name="q_{qid}" rows="3" placeholder="اكتب إجابتك هنا" class="writtenAnswer"></textarea>')
                parts.append('</section>')
        parts.append('<button id="submitOnlineExam" type="submit">تسليم الاختبار</button></form><div id="onlineExamResult" role="status"></div></main>')
        payload = json.dumps({"id": eid, "duration": int(ex["duration_minutes"]), "questions": questions}, ensure_ascii=False).replace("</", r"<\/")
        script = f"""<style>.page{{max-width:900px;width:calc(100% - 24px);min-height:0;margin:20px auto;padding:clamp(16px,4vw,36px);border-radius:18px}}.answerOption{{display:block;padding:10px;margin:7px 0;border:1px solid #ddd;border-radius:10px;cursor:pointer}}.answerOption input{{margin-inline-end:8px}}.writtenAnswer{{width:100%;padding:12px;border:1px solid #bbb;border-radius:10px;font:inherit;margin-top:10px}}#submitOnlineExam{{position:sticky;bottom:12px;background:#111;color:#ffce3a;border:0;padding:14px 20px;border-radius:12px;font-weight:800;cursor:pointer;width:100%}}#onlineExamResult{{margin-top:16px;font-weight:800}}.q{{padding:14px 0;border-bottom:1px solid #ddd}}.toolbar{{position:sticky}}@media(max-width:600px){{.meta{{grid-template-columns:1fr}}}}</style>
<script>
(function(){{const data={payload};let left=data.duration*60;let submitted=false;const timer=document.getElementById('countdown');const form=document.getElementById('onlineExamForm');const result=document.getElementById('onlineExamResult');const btn=document.getElementById('submitOnlineExam');
function tick(){{if(submitted)return;left=Math.max(0,left-1);timer.textContent=Math.floor(left/60)+':'+String(left%60).padStart(2,'0');if(left<=0)submit(true);}}const interval=setInterval(tick,1000);
async function submit(auto){{if(submitted)return;if(!auto&&!confirm('متأكد إنك عايز تسلّم الاختبار؟'))return;submitted=true;clearInterval(interval);btn.disabled=true;btn.textContent='جاري تسليم الاختبار...';const answers={{}};data.questions.forEach(q=>{{const field=form.elements['q_'+q.id];if(!field)return;if(field.length!==undefined && !field.tagName){{const chosen=Array.from(field).find(x=>x.checked);if(chosen)answers[String(q.id)]=chosen.value;}}else if(field.type==='radio'){{if(field.checked)answers[String(q.id)]=field.value;}}else answers[String(q.id)]=field.value||'';}});try{{const r=await fetch('/api/exams/'+data.id+'/submit',{{method:'POST',credentials:'same-origin',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{answers}})}});const d=await r.json();if(!r.ok)throw new Error(d.detail||'تعذر تسليم الاختبار');form.querySelectorAll('input,textarea,button').forEach(x=>x.disabled=true);result.innerHTML='تم تسليم الاختبار بنجاح. الدرجة المصححة تلقائيًا: '+d.score+' / '+d.auto_max_score+(d.pending_marks>0?' · أسئلة كتابية تنتظر المراجعة: '+d.pending_marks+' درجة':'')+' · إجمالي الامتحان: '+d.total_marks+' درجة';btn.textContent='تم التسليم';}}catch(e){{submitted=false;btn.disabled=false;btn.textContent='إعادة محاولة التسليم';result.textContent=e.message;}}}}
form.addEventListener('submit',e=>{{e.preventDefault();submit(false);}});
}})();
</script>"""
        body = ''.join(parts) + script
        resp = HTMLResponse(_page_shell(f'{ex["title"]} - اختبار إلكتروني', body, ex["lang"], L))
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @router.post("/api/exams/{eid}/submit")
    def submit_online_exam(eid: int, payload: OnlineExamSubmission, request: Request):
        uid = uid_or_401(request)
        row = _owned_exam(eid, uid)
        if row["status"] != "done" or not row["exam_json"]:
            raise HTTPException(status_code=409, detail="الامتحان لسه مش جاهز.")
        ex = json.loads(row["exam_json"])
        supplied = payload.answers or {}
        saved_answers = {}
        score = 0.0
        auto_max = 0.0
        pending = 0.0
        for sec in ex["sections"]:
            for q in sec["questions"]:
                qid = str(q["id"])
                answer = str(supplied.get(qid, ""))[:12000].strip()
                saved_answers[qid] = answer
                if q["type"] == "mcq":
                    auto_max += float(q["marks"])
                    try: selected = int(answer)
                    except (ValueError, TypeError): selected = -1
                    if q.get("answer_index") is not None and selected == int(q["answer_index"]): score += float(q["marks"])
                elif q["type"] == "true_false":
                    auto_max += float(q["marks"])
                    expected = str(q.get("answer", "")).lower()
                    if answer.lower() == expected and expected in ("true", "false"): score += float(q["marks"])
                else:
                    pending += float(q["marks"])
        now = now_iso()
        conn = get_db()
        cur = conn.execute("INSERT INTO exam_attempts(exam_id,user_id,answers_json,score,auto_max_score,pending_marks,total_marks,status,started_at,submitted_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (eid, uid, json.dumps(saved_answers, ensure_ascii=False), score, auto_max, pending, float(ex["total_marks"]), "submitted", now, now))
        attempt_id = cur.lastrowid
        conn.commit()
        conn.close()
        return {"success": True, "attempt_id": attempt_id, "score": round(score, 2), "auto_max_score": round(auto_max, 2), "pending_marks": round(pending, 2), "total_marks": round(float(ex["total_marks"]), 2)}

    @router.get("/api/exams/{eid}/attempts")
    def list_online_attempts(eid: int, request: Request):
        uid = uid_or_401(request)
        _owned_exam(eid, uid)
        conn = get_db()
        rows = conn.execute("SELECT id,score,auto_max_score,pending_marks,total_marks,status,submitted_at FROM exam_attempts WHERE exam_id=? AND user_id=? ORDER BY id DESC LIMIT 20", (eid, uid)).fetchall()
        conn.close()
        return {"attempts": [dict(r) for r in rows]}

    @router.get("/exam/{eid}/paper", response_class=HTMLResponse)
    def paper(eid: int, request: Request, space: int = 1):
        return _page(eid, request, lambda ex: render_paper(ex, bool(space)))

    @router.get("/exam/{eid}/sheet", response_class=HTMLResponse)
    def sheet(eid: int, request: Request):
        return _page(eid, request, render_sheet)

    @router.get("/exam/{eid}/key", response_class=HTMLResponse)
    def key(eid: int, request: Request):
        return _page(eid, request, render_key)

    return router


# ----------------------------------------------------------------------------
# Website UI (injected into the main page by inject_ui)
# ----------------------------------------------------------------------------
TAB_HTML = """
      <div id="tab-exams" class="tab-content hidden space-y-6">
        <div class="banana-card p-5 md:p-7 rounded-3xl space-y-3">
          <h2 class="text-lg md:text-2xl font-black text-slate-900">📝 الاختبارات</h2>
          <p class="text-xs text-slate-500">ارفع امتحانات سابقة وحلولها وشرح الدكتور (PDF أو صور). Gemini بيقرأ الملفات ويفهم أسلوب الدكتور ويولّد امتحان جديد. تقدر تحلّه إلكترونيًا بمؤقّت وتصحيح تلقائي للأسئلة الموضوعية، أو تطبع ورقة الامتحان والإجابة ونموذج الحل PDF.</p>
        </div>

        <div class="banana-card p-5 md:p-6 rounded-3xl space-y-4">
          <h3 class="font-black text-slate-900">1) ملفاتك</h3>
          <div class="flex flex-wrap items-center gap-2">
            <select id="exKind" class="bg-slate-50 border border-slate-200 rounded-xl p-2.5 text-xs">
              <option value="past_exam">امتحان سابق / حل نموذجي</option>
              <option value="explanation" selected>شرح / محاضرة</option>
              <option value="other">ملف آخر</option>
            </select>
            <input id="exFiles" type="file" multiple accept=".pdf,.png,.jpg,.jpeg,.webp" class="text-xs flex-1 min-w-[180px]">
            <button type="button" onclick="exUpload()" class="px-4 py-2.5 bg-slate-900 text-amber-300 font-black text-xs rounded-xl">رفع</button>
          </div>
          <p id="exUploadMsg" class="text-xs text-slate-500"></p>
          <div id="exSources" class="space-y-2"></div>
        </div>

        <div class="banana-card p-5 md:p-6 rounded-3xl space-y-4">
          <h3 class="font-black text-slate-900">2) إعداد الامتحان</h3>
          <div class="grid grid-cols-2 md:grid-cols-4 gap-3 text-xs">
            <label class="col-span-2 md:col-span-4 space-y-1"><span class="font-bold text-slate-600">اسم الامتحان (اختياري)</span>
              <input id="exTitle" class="w-full bg-slate-50 border border-slate-200 rounded-xl p-2.5" placeholder="مثال: Midterm - الفيزياء"></label>
            <label class="space-y-1"><span class="font-bold text-slate-600">عدد الأسئلة</span>
              <input id="exNum" type="number" min="1" max="60" value="20" class="w-full bg-slate-50 border border-slate-200 rounded-xl p-2.5"></label>
            <label class="space-y-1"><span class="font-bold text-slate-600">الزمن (دقيقة)</span>
              <input id="exDur" type="number" min="5" max="600" value="60" class="w-full bg-slate-50 border border-slate-200 rounded-xl p-2.5"></label>
            <label class="space-y-1"><span class="font-bold text-slate-600">مجموع الدرجات (0 = تلقائي)</span>
              <input id="exMarks" type="number" min="0" max="1000" value="0" class="w-full bg-slate-50 border border-slate-200 rounded-xl p-2.5"></label>
            <label class="space-y-1"><span class="font-bold text-slate-600">الصعوبة</span>
              <select id="exDiff" class="w-full bg-slate-50 border border-slate-200 rounded-xl p-2.5">
                <option value="like_sources">زي الامتحانات القديمة</option><option value="easy">أسهل</option>
                <option value="medium">متوسط</option><option value="hard">أصعب</option></select></label>
            <label class="space-y-1"><span class="font-bold text-slate-600">اللغة</span>
              <select id="exLang" class="w-full bg-slate-50 border border-slate-200 rounded-xl p-2.5">
                <option value="auto">زي الملفات</option><option value="ar">عربي</option><option value="en">English</option></select></label>
          </div>
          <details class="text-xs">
            <summary class="cursor-pointer font-bold text-slate-600">تحديد عدد كل نوع من الأسئلة (اختياري، لو كتبت أرقام هنا بتبقى هي العدد الكلي)</summary>
            <div class="grid grid-cols-3 md:grid-cols-6 gap-2 mt-2">
              <label>اختيار من متعدد<input id="exT_mcq" type="number" min="0" max="60" value="0" class="w-full bg-slate-50 border border-slate-200 rounded-lg p-2"></label>
              <label>صح / خطأ<input id="exT_true_false" type="number" min="0" max="60" value="0" class="w-full bg-slate-50 border border-slate-200 rounded-lg p-2"></label>
              <label>أكمل<input id="exT_fill_blank" type="number" min="0" max="60" value="0" class="w-full bg-slate-50 border border-slate-200 rounded-lg p-2"></label>
              <label>إجابة قصيرة<input id="exT_short_answer" type="number" min="0" max="60" value="0" class="w-full bg-slate-50 border border-slate-200 rounded-lg p-2"></label>
              <label>مسائل<input id="exT_problem" type="number" min="0" max="60" value="0" class="w-full bg-slate-50 border border-slate-200 rounded-lg p-2"></label>
              <label>مقالي<input id="exT_essay" type="number" min="0" max="60" value="0" class="w-full bg-slate-50 border border-slate-200 rounded-lg p-2"></label>
            </div>
          </details>
          <label class="block text-xs space-y-1"><span class="font-bold text-slate-600">ملاحظات للدكتور (اختياري)</span>
            <textarea id="exNotes" rows="2" class="w-full bg-slate-50 border border-slate-200 rounded-xl p-2.5" placeholder="مثال: ركّز على الفصل 3 و4، ومن غير أسئلة نظرية طويلة"></textarea></label>
          <button type="button" id="exGenBtn" onclick="exGenerate()" class="w-full py-3 bg-gradient-to-r from-amber-400 to-yellow-400 text-slate-900 font-black text-sm rounded-xl">✨ ولّد الامتحان</button>
          <p id="exGenMsg" class="text-xs text-slate-500"></p>
        </div>

        <div class="banana-card p-5 md:p-6 rounded-3xl space-y-3">
          <div class="flex items-center justify-between"><h3 class="font-black text-slate-900">3) امتحاناتك</h3>
            <button type="button" onclick="exLoadExams()" class="px-3 py-1.5 rounded-lg bg-slate-900 text-amber-300 text-xs font-black">تحديث</button></div>
          <div id="exList" class="space-y-3"></div>
        </div>
      </div>
"""

TAB_JS = """
<script>
(function(){
  var EX = {sources: [], selected: {}, timer: null};
  function esc(s){return String(s==null?"":s).replace(/[&<>"']/g,function(c){return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];});}
  async function api(url, opts){
    var r = await fetch(url, Object.assign({credentials:"same-origin"}, opts||{}));
    var d = {}; try { d = await r.json(); } catch(e) {}
    if(!r.ok){ var det = d.detail; throw new Error(typeof det==="string" ? det : "HTTP "+r.status); }
    return d;
  }
  function mb(n){ return (n/1048576).toFixed(1)+" MB"; }
  var KIND = {past_exam:"امتحان/حل", explanation:"شرح", other:"آخر"};

  window.exLoadAll = async function(){ await window.exLoadSources(); await window.exLoadExams(); };

  window.exLoadSources = async function(){
    try { EX.sources = (await api("/api/exams/sources")).sources; } catch(e) { return; }
    var box = document.getElementById("exSources");
    if(!box) return;
    if(!EX.sources.length){ box.innerHTML = '<p class="text-xs text-slate-400">لسه مفيش ملفات. ارفع أول ملف.</p>'; return; }
    box.innerHTML = EX.sources.map(function(s){
      var on = EX.selected[s.id] ? "checked" : "";
      return '<div class="flex items-center gap-2 p-2.5 bg-white border border-slate-200 rounded-xl text-xs">'
        + '<input type="checkbox" '+on+' onchange="exToggle('+s.id+', this.checked)">'
        + '<span class="flex-1 truncate font-bold">'+esc(s.filename)+'</span>'
        + '<span class="px-2 py-0.5 rounded-lg bg-amber-100 text-amber-800 font-bold">'+(KIND[s.kind]||"")+'</span>'
        + '<span class="text-slate-400">'+s.pages+' ص · '+mb(s.size)+'</span>'
        + '<button type="button" onclick="exDeleteSource('+s.id+')" class="text-red-500 font-black">✕</button></div>';
    }).join("");
  };
  window.exToggle = function(id, on){ if(on) EX.selected[id] = true; else delete EX.selected[id]; };

  window.exUpload = async function(){
    var input = document.getElementById("exFiles"), msg = document.getElementById("exUploadMsg");
    if(!input.files.length){ msg.textContent = "اختار ملفات الأول."; return; }
    var fd = new FormData();
    fd.append("kind", document.getElementById("exKind").value);
    for(var i=0;i<input.files.length;i++) fd.append("files", input.files[i]);
    msg.textContent = "جاري الرفع...";
    try {
      var d = await api("/api/exams/sources", {method:"POST", body: fd});
      msg.textContent = "تم رفع "+d.count+" ملف.";
      input.value = "";
      await window.exLoadSources();
    } catch(e) { msg.textContent = e.message; }
  };
  window.exDeleteSource = async function(id){
    if(!confirm("تمسح الملف ده؟")) return;
    try { await api("/api/exams/sources/"+id, {method:"DELETE"}); delete EX.selected[id]; await window.exLoadSources(); } catch(e) { alert(e.message); }
  };

  function num(id){ var v = parseInt(document.getElementById(id).value, 10); return isNaN(v) ? 0 : v; }
  window.exGenerate = async function(){
    var msg = document.getElementById("exGenMsg"), btn = document.getElementById("exGenBtn");
    var ids = Object.keys(EX.selected).map(Number);
    if(!ids.length){ msg.textContent = "علّم على ملف واحد على الأقل من ملفاتك."; return; }
    var counts = {};
    ["mcq","true_false","fill_blank","short_answer","problem","essay"].forEach(function(t){ var v = num("exT_"+t); if(v>0) counts[t] = v; });
    var body = {title: document.getElementById("exTitle").value, source_ids: ids, num_questions: num("exNum") || 20,
      type_counts: counts, duration_minutes: num("exDur") || 60, total_marks: num("exMarks"),
      difficulty: document.getElementById("exDiff").value, language: document.getElementById("exLang").value,
      notes: document.getElementById("exNotes").value};
    btn.disabled = true; msg.textContent = "بنبدأ التوليد...";
    try {
      await api("/api/exams/generate", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)});
      msg.textContent = "بدأ التوليد. ممكن ياخد من دقيقة لـ 3 دقايق، تقدر تسيب الصفحة.";
      await window.exLoadExams();
    } catch(e) { msg.textContent = e.message; }
    btn.disabled = false;
  };

  window.exLoadExams = async function(){
    var list; try { list = (await api("/api/exams")).exams; } catch(e) { return; }
    var box = document.getElementById("exList");
    if(!box) return;
    var busy = list.some(function(x){ return x.status==="queued" || x.status==="generating"; });
    clearTimeout(EX.timer);
    if(busy) EX.timer = setTimeout(window.exLoadExams, 4000);
    if(!list.length){ box.innerHTML = '<p class="text-xs text-slate-400">لسه مفيش امتحانات.</p>'; return; }
    box.innerHTML = list.map(function(x){
      var head = '<div class="flex items-center justify-between gap-2"><b class="text-sm">'+esc(x.title||"Exam")+'</b>'
        + '<button type="button" onclick="exDeleteExam('+x.id+')" class="text-red-500 font-black text-xs">مسح</button></div>';
      if(x.status==="queued" || x.status==="generating")
        return '<div class="p-3 bg-white border border-amber-200 rounded-2xl space-y-1 text-xs">'+head+'<p class="text-amber-700 font-bold">⏳ بيتولّد دلوقتي... (لحد 3 دقايق)</p></div>';
      if(x.status==="failed")
        return '<div class="p-3 bg-white border border-red-200 rounded-2xl space-y-1 text-xs">'+head+'<p class="text-red-600">'+esc(x.error)+'</p></div>';
      var warn = (x.warnings||[]).length ? '<ul class="text-[11px] text-amber-700 list-disc ps-5">'+x.warnings.map(function(w){return "<li>"+esc(w)+"</li>";}).join("")+'</ul>' : "";
      var a = 'target="_blank" class="px-3 py-2 rounded-xl text-xs font-black ';
      return '<div class="p-3 bg-white border border-slate-200 rounded-2xl space-y-2 text-xs">'+head
        + '<p class="text-slate-500">'+x.n_questions+' سؤال · '+x.total_marks+' درجة · '+x.duration_minutes+' دقيقة</p>'+warn
        + '<div class="flex flex-wrap gap-2">'
        + '<a href="/exam/'+x.id+'/online" '+a+'bg-indigo-600 text-white">💻 اختبار إلكتروني</a>'
        + '<a href="/exam/'+x.id+'/paper" '+a+'bg-slate-900 text-amber-300">📄 ورقة الامتحان</a>'
        + '<a href="/exam/'+x.id+'/sheet" '+a+'bg-amber-400 text-slate-900">✍️ ورقة الإجابة</a>'
        + '<a href="/exam/'+x.id+'/key" '+a+'bg-emerald-600 text-white">✅ نموذج الإجابة</a>'
        + '<button type="button" onclick="exShowAttempts('+x.id+')" class="px-3 py-2 rounded-xl text-xs font-black bg-slate-100 text-slate-800">📊 محاولاتي</button></div><div id="exAttempts_'+x.id+'" class="text-xs text-slate-500"></div></div>';
    }).join("");
  };
  window.exShowAttempts = async function(id){
    var box=document.getElementById("exAttempts_"+id); if(!box)return; box.textContent="جاري تحميل المحاولات...";
    try { var d=await api("/api/exams/"+id+"/attempts"); if(!d.attempts.length){box.textContent="لسه مفيش محاولات إلكترونية.";return;}
      box.innerHTML="<b>المحاولات السابقة:</b> "+d.attempts.map(function(a){return "<div>"+esc(a.submitted_at)+" — "+a.score+" / "+a.auto_max_score+" درجة مصححة تلقائيًا"+(a.pending_marks>0?" · "+a.pending_marks+" درجة تحتاج مراجعة":"")+"</div>";}).join("");
    } catch(e){box.textContent=e.message;}
  };
  window.exDeleteExam = async function(id){
    if(!confirm("تمسح الامتحان ده؟")) return;
    try { await api("/api/exams/"+id, {method:"DELETE"}); await window.exLoadExams(); } catch(e) { alert(e.message); }
  };
})();
</script>
"""


def inject_ui(page: str) -> str:
    """Add the Exams nav buttons, tab and script to the main page (soft-fails if the page changed)."""
    def nav(m):
        # Preserve AI Study Center and add Exams as a separate navigation item.
        original = m.group(0)
        exams_button = original.replace("switchTab('studycenter')", "switchTab('exams')").replace('data-target="studycenter"', 'data-target="exams"')
        exams_button = exams_button.replace("AI Study Center", "الاختبارات").replace("🧠", "📝")
        return original + "\n" + exams_button

    page, n = re.subn(r"(<button[^>]*data-target=\"studycenter\"[^>]*>.*?</button>)", nav, page, flags=re.S)
    if n == 0:
        print("[exam_studio] WARNING: nav anchor not found")
    marker = '<div id="tab-history"'
    if marker in page:
        page = page.replace(marker, TAB_HTML + "\n      " + marker, 1)
    else:
        print("[exam_studio] WARNING: tab anchor not found")
    # main.py owns the switchTab allow-list and exams-loading hook. Keep injection
    # safe to call even when the page already contains those changes.
    if 'const allowed = new Set(' in page and '"exams"' not in page[page.find('const allowed = new Set('):page.find('const allowed = new Set(')+260]:
        page = re.sub(r'(const allowed = new Set\(\[[^]]*)"studycenter"', r'\1"studycenter","exams"', page, count=1)
    if 'if (t === "exams" && window.exLoadAll)' not in page:
        hook = '      if (t === "history") {'
        if hook in page:
            page = page.replace(hook, '      if (t === "exams" && window.exLoadAll) { window.exLoadAll(); }\n' + hook, 1)
    if "</body>" in page:
        page = page.replace("</body>", TAB_JS + "\n</body>", 1)
    return page
