"""
Smart Path ("خريطة التعلّم الذكية") for S-Tesseract.  Builds on learning_lab.py.

Structure   field -> stages -> lessons (blueprint_nodes) -> skills. Every lesson has resources, a challenge,
            and Learning Lab activities; the last lesson of each stage suggests a practical project.
Projects    a new activity type ("project") with a rubric, graded like written work: credit needs evidence
            that really appears in what the student submitted (links are never opened).
Skills      every generated activity targets ONE skill of its lesson (the weakest / not-yet-measured one),
            and every attempt updates that skill's mastery.
Adaptation  runs when a lesson is completed (and on demand), based on real results:
              - weak skill (< 60%)        -> an optional remedial lesson is inserted right after the lesson;
              - flawless + >= 90% mastery -> the next lesson becomes "fast track" (one hard activity completes it);
              - 3 failed tries, < 40%     -> the lesson's suggested difficulty drops to easy.
            Every change is logged and shown to the student. The AI writes lesson text; it never decides marks.
"""
import json
import re
import threading
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

import learning_lab as LL
from learning_lab import HOOKS, ActivityType, _gen, _s, _str_list, register, rubric_grade

LEVELS = {"beginner": "مبتدئ", "intermediate": "متوسط", "advanced": "متقدم"}
WEAK = 0.6
STRUGGLE = 0.4
MAX_LESSONS = 16
MAX_REMEDIAL = 4
PATH_DAILY_LIMIT = 5
STALE_MINUTES = 10
RES_TYPES = ("video", "article", "docs", "practice", "book")
DEFAULT_OPTIONS = ["صحيح", "خطأ", "غير متأكد", "يحتاج مراجعة"]


# =============================================================================
# Schema
# =============================================================================
def migrate(get_db):
    conn = get_db()
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(blueprint_nodes)").fetchall()}
        for name, ddl in [("stage_title", "TEXT DEFAULT ''"), ("stage_order", "INTEGER DEFAULT 0"), ("skills", "TEXT DEFAULT '[]'"),
                          ("resources", "TEXT DEFAULT '[]'"), ("kind", "TEXT DEFAULT 'core'"), ("is_optional", "INTEGER DEFAULT 0"),
                          ("pass_required", "INTEGER DEFAULT 0"), ("level_hint", "TEXT DEFAULT ''"), ("stage_project", "TEXT DEFAULT ''")]:
            if name not in cols:
                conn.execute(f"ALTER TABLE blueprint_nodes ADD COLUMN {name} {ddl}")
        lab_cols = {r[1] for r in conn.execute("PRAGMA table_info(lab_activities)").fetchall()}
        if lab_cols and "skill" not in lab_cols:
            conn.execute("ALTER TABLE lab_activities ADD COLUMN skill TEXT DEFAULT ''")
        conn.execute("""CREATE TABLE IF NOT EXISTS learning_paths (
            user_id INTEGER PRIMARY KEY, goal TEXT DEFAULT '', duration TEXT DEFAULT '', level TEXT DEFAULT 'beginner',
            known TEXT DEFAULT '', summary TEXT DEFAULT '', status TEXT DEFAULT 'ready', error TEXT DEFAULT '',
            created_at TEXT DEFAULT '', updated_at TEXT DEFAULT '')""")
        conn.execute("""CREATE TABLE IF NOT EXISTS skill_progress (
            user_id INTEGER NOT NULL, node_id INTEGER NOT NULL, skill TEXT NOT NULL, attempts INTEGER DEFAULT 0,
            mastery REAL DEFAULT 0, updated_at TEXT DEFAULT '', PRIMARY KEY (user_id, node_id, skill))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS path_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, node_id INTEGER DEFAULT 0,
            kind TEXT NOT NULL, skill TEXT DEFAULT '', detail TEXT DEFAULT '', created_at TEXT NOT NULL)""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_path_changes_user ON path_changes(user_id)")
        conn.execute("UPDATE learning_paths SET status='failed', error='السيرفر اتعمله إعادة تشغيل أثناء البناء.' WHERE status='generating'")
        conn.commit()
    finally:
        conn.close()


# =============================================================================
# Project activity type (rubric-graded practical work)
# =============================================================================
@register
class ProjectActivity(ActivityType):
    key = "project"
    label = "مشروع عملي (بمعايير تقييم)"
    icon = "🏁"
    description = "مشروع صغير بمهام وتسليمات ومعايير تقييم واضحة."
    pass_mark = 0.7

    def generate(self, ctx):
        prompt = f"""You are an instructor designing ONE small practical project for this lesson. It must be doable in 1-3 hours
using only free tools, and the student must be able to PASTE the result as text (code, calculations, a written report, a design
description with decisions). Links cannot be opened by the grader.
{LL._lesson_block(ctx)}
- scenario: a realistic situation in 2-4 sentences. deliverables: 2-5 concrete things to submit. constraints: 0-4 limits.
- tasks: 3-6 ordered tasks (title + short detail) that lead the student through the project.
- criteria: 3-6 rubric criteria checkable from the submitted text; each has name, description, points (1-5).
- model_answer: a concise description of an excellent submission (for the grader only).
- Write everything in Arabic (keep technical terms as taught).
Return STRICT JSON: {{"title":"...","scenario":"...","deliverables":["..."],"constraints":["..."],
"tasks":[{{"title":"...","detail":"..."}}],"criteria":[{{"name":"...","description":"...","points":2}}],"model_answer":"...",
"misconceptions":["..."],"hints":["...","..."],"key_points":["..."],"study_keywords":["..."]}}"""
        raw = _gen(ctx, prompt)
        crit = []
        for c in (raw.get("criteria") or [])[:6]:
            if isinstance(c, dict) and _s(c.get("name")):
                try:
                    pts = max(1.0, min(5.0, round(float(c.get("points", 1)) * 2) / 2))
                except (TypeError, ValueError):
                    pts = 1.0
                crit.append({"id": f"c{len(crit) + 1}", "name": _s(c.get("name"), 200), "description": _s(c.get("description"), 500), "points": pts})
        tasks = [{"title": _s(t.get("title"), 160), "detail": _s(t.get("detail"), 500)} for t in (raw.get("tasks") or [])[:6]
                 if isinstance(t, dict) and _s(t.get("title"))]
        deliverables = _str_list(raw.get("deliverables"), 5, 300)
        if len(crit) < 3 or len(_s(raw.get("scenario"))) < 30 or not deliverables or len(tasks) < 2:
            raise ValueError("incomplete project")
        spec = {"scenario": _s(raw.get("scenario"), 1500), "deliverables": deliverables, "constraints": _str_list(raw.get("constraints"), 4, 300),
                "tasks": tasks, "criteria": crit, "max_points": sum(c["points"] for c in crit),
                "model_answer": _s(raw.get("model_answer"), 4000), "misconceptions": _str_list(raw.get("misconceptions"), 6),
                "hints": _str_list(raw.get("hints"), 3, 600), "key_points": _str_list(raw.get("key_points"), 6),
                "keywords": _str_list(raw.get("study_keywords"), 8, 80)}
        return _s(raw.get("title"), 120) or "مشروع عملي", spec

    def public(self, spec):
        return {"scenario": spec["scenario"], "deliverables": spec["deliverables"], "constraints": spec["constraints"], "tasks": spec["tasks"],
                "criteria": spec["criteria"], "max_points": spec["max_points"], "hints": spec["hints"],
                "key_points": spec["key_points"], "keywords": spec["keywords"]}

    def grade(self, ctx, spec, sub):
        answer = _s(sub.get("answer"), 8000)
        if len(answer) < 150:
            raise HTTPException(status_code=400, detail="التسليم قصير. الصق شغلك كامل (150 حرف على الأقل): الكود أو الحل أو الشرح وقراراتك.")
        task = spec["scenario"] + "\nDeliverables: " + "; ".join(spec["deliverables"])
        extra = "The student may mention links; you cannot open links, so give NO credit for claims that exist only behind a link."
        res = rubric_grade(ctx, spec, answer, task, extra)
        res["details"]["links_note"] = "التصحيح اعتمد على النص اللي لصقته فقط، اللينكات مش بتتفتح."
        return res

    def reveal(self, spec):
        return None


# =============================================================================
# Path generation
# =============================================================================
def build_path_prompt(req) -> str:
    return f"""You are a learning-roadmap architect. Design a complete roadmap for the goal below.
Goal: "{req['goal']}"
Time available: "{req['duration']}"
Student level: {req['level']} ({LEVELS.get(req['level'], '')}). {("Already known (skip or only review): " + req['known']) if req['known'] else ""}
Rules:
- 3 to 4 STAGES in a sensible order; each stage has 2 to 4 LESSONS (at most {MAX_LESSONS} lessons in total). Lessons must fit the time available.
- Each lesson: title, objective (what the student can do afterwards), timeframe, 3 to 5 SKILLS (short, specific, checkable skills),
  one recommended FREE course (courseTitle, courseProvider, courseSearchQuery to find it on YouTube/Google), up to 4 resources of different
  types (video, article, docs, practice, book) each with a title and a precise search_query (NEVER invent URLs), and one multiple-choice
  challenge question (4 options, correctIndex) testing the lesson's core idea.
- Each stage also has one practical PROJECT idea (title + brief) that combines the stage's skills.
- Adapt to the level: a beginner gets foundations first; an advanced student skips basics and goes deeper.
- Write everything in Arabic (keep technical terms in English where usual).
Return STRICT JSON:
{{"summary":"...","stages":[{{"title":"المرحلة الأولى: ...","summary":"...","project":{{"title":"...","brief":"..."}},
"lessons":[{{"title":"...","objective":"...","timeframe":"...","skills":["..."],"courseTitle":"...","courseProvider":"...","courseSearchQuery":"...",
"resources":[{{"type":"video","title":"...","search_query":"..."}}],
"challenge":{{"question":"...","options":["...","...","...","..."],"correctIndex":0}}}}]}}]}}"""


def _challenge(c, title):
    q = _s((c or {}).get("question"), 600) if isinstance(c, dict) else ""
    opts = [_s(o, 300) for o in ((c or {}).get("options") or []) if _s(o)] if isinstance(c, dict) else []
    idx = (c or {}).get("correctIndex") if isinstance(c, dict) else 0
    if not q:
        q = f"سؤال مراجعة عامة حول: {title}"
    if len(opts) < 2:
        opts, idx = list(DEFAULT_OPTIONS), 0
    if not isinstance(idx, int) or not 0 <= idx < len(opts):
        idx = 0
    return q, opts[:6], idx


def _resources(raw, fallback_query):
    out = []
    for r in (raw or [])[:4]:
        if isinstance(r, dict) and _s(r.get("search_query")):
            t = _s(r.get("type"), 20).lower()
            out.append({"type": t if t in RES_TYPES else "article", "title": _s(r.get("title"), 160) or _s(r.get("search_query"), 160),
                        "search_query": _s(r.get("search_query"), 160)})
    if not out and fallback_query:
        out.append({"type": "video", "title": fallback_query[:160], "search_query": fallback_query[:160]})
    return out


def normalize_path(raw: dict) -> dict:
    stages_raw = raw.get("stages") if isinstance(raw, dict) else None
    if not isinstance(stages_raw, list) or not stages_raw:
        raise ValueError("no stages")
    stages, total = [], 0
    for si, st in enumerate(stages_raw[:5], 1):
        if not isinstance(st, dict):
            continue
        lessons = []
        for li, ls in enumerate((st.get("lessons") or [])[:5], 1):
            if not isinstance(ls, dict) or total >= MAX_LESSONS:
                continue
            title = _s(ls.get("title"), 200) or f"الدرس {li}"
            skills = [x for x in _str_list(ls.get("skills"), 5, 80)]
            if not skills:
                skills = [title[:80]]
            q, opts, idx = _challenge(ls.get("challenge"), title)
            query = _s(ls.get("courseSearchQuery"), 160) or title
            lessons.append({"title": title, "objective": _s(ls.get("objective"), 600) or title, "timeframe": _s(ls.get("timeframe"), 80),
                            "skills": skills, "course_title": _s(ls.get("courseTitle"), 200) or "بحث مفتوح حسب الموضوع",
                            "course_provider": _s(ls.get("courseProvider"), 100) or "Free Online", "course_query": query,
                            "resources": _resources(ls.get("resources"), query), "q": q, "options": opts, "idx": idx})
            total += 1
        if lessons:
            proj = st.get("project") if isinstance(st.get("project"), dict) else {}
            stages.append({"title": _s(st.get("title"), 200) or f"المرحلة {si}", "summary": _s(st.get("summary"), 500),
                           "project": {"title": _s(proj.get("title"), 160), "brief": _s(proj.get("brief"), 600)} if _s(proj.get("title")) else None,
                           "lessons": lessons})
    if not stages:
        raise ValueError("no lessons")
    return {"summary": _s(raw.get("summary"), 800), "stages": stages}


def insert_path(conn, uid, norm, now):
    """Replace the user's whole roadmap (and everything attached to it) with the new path, atomically."""
    for table in ("lab_attempts", "lab_activities", "skill_progress"):
        conn.execute(f"DELETE FROM {table} WHERE user_id=?", (uid,))
    # keep 'started' rows: they are the daily-limit counter and must survive a replacement
    conn.execute("DELETE FROM path_changes WHERE user_id=? AND kind NOT IN ('started','generated')", (uid,))
    conn.execute("DELETE FROM blueprint_nodes WHERE user_id=?", (uid,))
    order = 0
    for si, st in enumerate(norm["stages"], 1):
        for li, ls in enumerate(st["lessons"]):
            order += 1
            last = li == len(st["lessons"]) - 1
            conn.execute(
                "INSERT INTO blueprint_nodes (user_id,node_order,title,philosophy,timeframe,course_title,course_provider,course_link,"
                "challenge_question,challenge_options,correct_index,is_unlocked,is_completed,stage_title,stage_order,skills,resources,kind,is_optional,stage_project) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?,0,?)",
                (uid, order, ls["title"], ls["objective"], ls["timeframe"], ls["course_title"], ls["course_provider"], ls["course_query"],
                 ls["q"], json.dumps(ls["options"], ensure_ascii=False), ls["idx"], 1 if order == 1 else 0, st["title"], si,
                 json.dumps(ls["skills"], ensure_ascii=False), json.dumps(ls["resources"], ensure_ascii=False), "core",
                 json.dumps(st["project"], ensure_ascii=False) if (last and st["project"]) else ""))
    return order


# =============================================================================
# Skills + adaptation
# =============================================================================
_adapting = set()
_adapt_lock = threading.Lock()


def update_skill(conn, uid, node_id, skill, now):
    rows = conn.execute("SELECT t.score FROM lab_attempts t JOIN lab_activities a ON a.id=t.activity_id "
                        "WHERE a.user_id=? AND a.node_id=? AND a.skill=? ORDER BY t.id DESC", (uid, node_id, skill)).fetchall()
    if not rows:
        return None
    last3 = [r[0] for r in rows[:3]]
    mastery = sum(last3) / len(last3)
    conn.execute("INSERT INTO skill_progress(user_id,node_id,skill,attempts,mastery,updated_at) VALUES(?,?,?,?,?,?) "
                 "ON CONFLICT(user_id,node_id,skill) DO UPDATE SET attempts=excluded.attempts, mastery=excluded.mastery, updated_at=excluded.updated_at",
                 (uid, node_id, skill, len(rows), mastery, now))
    return mastery


def _log(conn, uid, node_id, kind, skill, detail, now):
    conn.execute("INSERT INTO path_changes(user_id,node_id,kind,skill,detail,created_at) VALUES(?,?,?,?,?,?)", (uid, node_id, kind, skill, detail, now))


def _remedial_content(gen_json, node, skill, tags):
    prompt = f"""A student struggled with ONE skill. Write a short optional REMEDIAL lesson for it.
Main lesson: {node['title']}\nWeak skill: {skill}\nMistakes seen: {tags}
Return STRICT JSON in Arabic: {{"title":"مراجعة: ...","objective":"...","timeframe":"يوم أو يومين","skills":["{skill}"],
"courseTitle":"...","courseProvider":"...","courseSearchQuery":"...","resources":[{{"type":"video","title":"...","search_query":"..."}}],
"challenge":{{"question":"...","options":["...","...","...","..."],"correctIndex":0}}}}"""
    try:
        norm = normalize_path({"stages": [{"title": "x", "lessons": [gen_json(prompt)]}]})
        ls = norm["stages"][0]["lessons"][0]
        ls["skills"] = [skill]
        return ls
    except Exception:
        q = f"{skill} شرح مبسط"
        return {"title": f"مراجعة: {skill}", "objective": f"تثبيت مهارة «{skill}» قبل ما تكمّل.", "timeframe": "يوم أو يومين", "skills": [skill],
                "course_title": "بحث مفتوح حسب الموضوع", "course_provider": "Free Online", "course_query": q,
                "resources": _resources([], q), "q": f"سؤال مراجعة عامة حول: {skill}", "options": list(DEFAULT_OPTIONS), "idx": 0}


def adapt_node(get_db, gen_json, tz, uid, node_id):
    """Look at a COMPLETED lesson's real results and adjust the path. Idempotent per lesson."""
    key = (uid, node_id)
    with _adapt_lock:
        if key in _adapting:
            return []
        _adapting.add(key)
    changes = []
    try:
        conn = get_db()
        now = datetime.now(tz).isoformat()
        node = conn.execute("SELECT * FROM blueprint_nodes WHERE id=? AND user_id=?", (node_id, uid)).fetchone()
        if not node or not node["is_completed"] or node["kind"] == "remedial":
            conn.close()
            return []
        if conn.execute("SELECT 1 FROM path_changes WHERE user_id=? AND node_id=? AND kind='checked'", (uid, node_id)).fetchone():
            conn.close()
            return []
        stats = conn.execute("SELECT COUNT(CASE WHEN t.passed=0 THEN 1 END) AS failed, COUNT(t.id) AS n FROM lab_attempts t WHERE t.user_id=? AND t.node_id=?",
                             (uid, node_id)).fetchone()
        sk = conn.execute("SELECT skill, mastery FROM skill_progress WHERE user_id=? AND node_id=?", (uid, node_id)).fetchall()
        weak = sorted([(r["mastery"], r["skill"]) for r in sk if r["mastery"] < WEAK])
        avg = sum(r["mastery"] for r in sk) / len(sk) if sk else 0.0
        remedial_count = conn.execute("SELECT COUNT(*) FROM blueprint_nodes WHERE user_id=? AND kind='remedial'", (uid,)).fetchone()[0]
        if weak and remedial_count < MAX_REMEDIAL:
            mastery, skill = weak[0]
            tags = []
            for r in conn.execute("SELECT feedback_json FROM lab_attempts WHERE user_id=? AND node_id=? AND passed=0 ORDER BY id DESC LIMIT 6", (uid, node_id)):
                tags += json.loads(r["feedback_json"] or "{}").get("tags", [])
            conn.close()
            content = _remedial_content(gen_json, node, skill, list(dict.fromkeys(tags))[:5])        # AI call: no DB handle held
            conn = get_db()
            conn.execute("UPDATE blueprint_nodes SET node_order=node_order+1 WHERE user_id=? AND node_order>?", (uid, node["node_order"]))
            conn.execute(
                "INSERT INTO blueprint_nodes (user_id,node_order,title,philosophy,timeframe,course_title,course_provider,course_link,challenge_question,"
                "challenge_options,correct_index,is_unlocked,is_completed,stage_title,stage_order,skills,resources,kind,is_optional,stage_project) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,1,0,?,?,?,?,'remedial',1,'')",
                (uid, node["node_order"] + 1, content["title"], content["objective"], content["timeframe"], content["course_title"],
                 content["course_provider"], content["course_query"], content["q"], json.dumps(content["options"], ensure_ascii=False), content["idx"],
                 node["stage_title"], node["stage_order"], json.dumps([skill], ensure_ascii=False), json.dumps(content["resources"], ensure_ascii=False)))
            detail = f"مهارة «{skill}» وصلت {round(mastery * 100)}%، فأضفنا درس مراجعة اختياري بعد «{node['title']}»."
            _log(conn, uid, node_id, "remedial", skill, detail, now)
            changes.append({"kind": "remedial", "detail": detail})
        elif not weak and stats["failed"] == 0 and stats["n"] >= 2 and avg >= 0.9:
            nxt = conn.execute("SELECT * FROM blueprint_nodes WHERE user_id=? AND node_order=?", (uid, node["node_order"] + 1)).fetchone()
            if nxt and not nxt["is_completed"] and not nxt["is_optional"]:
                conn.execute("UPDATE blueprint_nodes SET pass_required=1, level_hint='hard' WHERE id=?", (nxt["id"],))
                detail = f"أداؤك في «{node['title']}» ممتاز بدون أي غلطة، فالدرس الجاي «{nxt['title']}» بقى مسار سريع: نشاط صعب واحد ناجح يكمّله."
                _log(conn, uid, node_id, "fast_track", "", detail, now)
                changes.append({"kind": "fast_track", "detail": detail})
        _log(conn, uid, node_id, "checked", "", "", now)
        conn.commit()
        conn.close()
    except Exception as exc:
        print(f"[smart_path] adapt failed: {type(exc).__name__}: {exc}")
    finally:
        with _adapt_lock:
            _adapting.discard(key)
    return changes


# =============================================================================
# Router
# =============================================================================
class PathRequest(BaseModel):
    goal: str
    duration: str = "شهر واحد"
    level: str = "beginner"
    known: str = ""
    replace: bool = False


def build_router(get_db, current_user_id, gen_json, tz) -> APIRouter:
    router = APIRouter()

    def now():
        return datetime.now(tz).isoformat()

    def uid_or_401(request):
        uid = current_user_id(request)
        if not uid:
            raise HTTPException(status_code=401, detail="سجّل الدخول الأول.")
        return uid

    # ---- hooks into the Learning Lab ----------------------------------------
    def ctx_hook(conn, uid, node, ctx):
        keys = node.keys()
        skills = json.loads(node["skills"] or "[]") if "skills" in keys else []
        path = conn.execute("SELECT level FROM learning_paths WHERE user_id=?", (uid,)).fetchone()
        if path:
            ctx["level"] = LEVELS.get(path["level"], "")
        if not skills:
            return
        mastery = {r["skill"]: r["mastery"] for r in conn.execute("SELECT skill, mastery FROM skill_progress WHERE user_id=? AND node_id=?", (uid, node["id"]))}
        target = next((s for s in skills if s not in mastery), None) or min(skills, key=lambda s: mastery[s])
        ctx["skills"], ctx["target_skill"] = skills, target

    def attempt_hook(info):
        out, uid, node_id = {}, info["uid"], info["node_id"]
        skill = info["spec"].get("skill") or ""
        conn = get_db()
        try:
            if skill:
                m = update_skill(conn, uid, node_id, skill, now())
                if m is not None:
                    out["skill"] = {"name": skill, "mastery": round(m, 3)}
            if not info["passed"] and info["attempts"] >= 3 and info["score"] < STRUGGLE:
                n = conn.execute("SELECT level_hint FROM blueprint_nodes WHERE id=?", (node_id,)).fetchone()
                if n and n["level_hint"] != "easy":
                    conn.execute("UPDATE blueprint_nodes SET level_hint='easy' WHERE id=?", (node_id,))
                    detail = "جرّبت أكتر من مرة بدون تقدم، فبقى الافتراضي لأنشطة الدرس ده «سهل». راجع التلميحات والملخص."
                    _log(conn, uid, node_id, "easier", skill, detail, now())
                    out["note"] = detail
            conn.commit()
        finally:
            conn.close()
        if info["just_completed"]:
            threading.Thread(target=adapt_node, args=(get_db, gen_json, tz, uid, node_id), daemon=True).start()
            out["adapting"] = True
        return out

    HOOKS["context"].append(ctx_hook)
    HOOKS["after_attempt"].append(attempt_hook)

    # ---- generation ----------------------------------------------------------
    def run_path(uid, req):
        try:
            norm = normalize_path(gen_json(build_path_prompt(req)))
            conn = get_db()
            n = insert_path(conn, uid, norm, now())
            conn.execute("UPDATE learning_paths SET status='ready', error='', summary=?, updated_at=? WHERE user_id=?", (norm["summary"], now(), uid))
            _log(conn, uid, 0, "generated", "", f"{len(norm['stages'])} مراحل · {n} دروس", now())
            conn.commit()
            conn.close()
        except Exception as exc:
            print(f"[smart_path] path generation failed: {type(exc).__name__}: {exc}")
            conn = get_db()
            conn.execute("UPDATE learning_paths SET status='failed', error=?, updated_at=? WHERE user_id=?",
                         ("فشل بناء المسار. جرّب تاني أو اكتب هدفك بشكل أوضح.", now(), uid))
            conn.commit()
            conn.close()

    @router.post("/api/path/generate")
    def generate(payload: PathRequest, request: Request):
        uid = uid_or_401(request)
        goal = payload.goal.strip()
        if not 3 <= len(goal) <= 200:
            raise HTTPException(status_code=400, detail="اكتب هدفك (من 3 لـ 200 حرف).")
        level = payload.level if payload.level in LEVELS else "beginner"
        conn = get_db()
        try:
            cutoff = (datetime.now(tz) - timedelta(minutes=STALE_MINUTES)).isoformat()
            conn.execute("UPDATE learning_paths SET status='failed', error='البناء أخد وقت طويل وتوقف.' WHERE user_id=? AND status='generating' AND updated_at<?", (uid, cutoff))
            cur = conn.execute("SELECT status FROM learning_paths WHERE user_id=?", (uid,)).fetchone()
            if cur and cur["status"] == "generating":
                raise HTTPException(status_code=429, detail="في مسار لسه بيتبني. استنى لما يخلص.")
            if conn.execute("SELECT COUNT(*) FROM blueprint_nodes WHERE user_id=?", (uid,)).fetchone()[0] and not payload.replace:
                raise HTTPException(status_code=409, detail="عندك مسار حالي. بناء مسار جديد هيمسحه ومعاه كل أنشطتك ونتايجك.")
            today = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            if conn.execute("SELECT COUNT(*) FROM path_changes WHERE user_id=? AND kind='started' AND created_at>=?", (uid, today)).fetchone()[0] >= PATH_DAILY_LIMIT:
                raise HTTPException(status_code=429, detail=f"وصلت للحد اليومي ({PATH_DAILY_LIMIT} مسارات). جرّب بكرة.")
            req = {"goal": goal, "duration": _s(payload.duration, 60) or "شهر واحد", "level": level, "known": _s(payload.known, 300)}
            conn.execute("INSERT INTO learning_paths(user_id,goal,duration,level,known,status,created_at,updated_at) VALUES(?,?,?,?,?,'generating',?,?) "
                         "ON CONFLICT(user_id) DO UPDATE SET goal=excluded.goal, duration=excluded.duration, level=excluded.level, known=excluded.known, "
                         "status='generating', error='', updated_at=excluded.updated_at", (uid, goal, req["duration"], level, req["known"], now(), now()))
            _log(conn, uid, 0, "started", "", goal, now())
            conn.commit()
        finally:
            conn.close()
        threading.Thread(target=run_path, args=(uid, req), daemon=True).start()
        return {"success": True}

    # ---- overview ------------------------------------------------------------
    @router.get("/api/path/overview")
    def overview(request: Request):
        uid = uid_or_401(request)
        conn = get_db()
        try:
            cutoff = (datetime.now(tz) - timedelta(minutes=STALE_MINUTES)).isoformat()
            conn.execute("UPDATE learning_paths SET status='failed', error='البناء أخد وقت طويل وتوقف.' WHERE user_id=? AND status='generating' AND updated_at<?", (uid, cutoff))
            conn.commit()
            path = conn.execute("SELECT * FROM learning_paths WHERE user_id=?", (uid,)).fetchone()
            nodes = conn.execute("SELECT * FROM blueprint_nodes WHERE user_id=? ORDER BY node_order", (uid,)).fetchall()
            acts = conn.execute("SELECT a.node_id, a.type, a.status, COALESCE(MAX(t.score),0) AS best, COALESCE(MAX(t.passed),0) AS passed "
                                "FROM lab_activities a LEFT JOIN lab_attempts t ON t.activity_id=a.id WHERE a.user_id=? GROUP BY a.id", (uid,)).fetchall()
            skills = {}
            for r in conn.execute("SELECT node_id, skill, mastery, attempts FROM skill_progress WHERE user_id=?", (uid,)):
                skills[(r["node_id"], r["skill"])] = (r["mastery"], r["attempts"])
            changes = [dict(r) for r in conn.execute("SELECT kind, detail, created_at FROM path_changes WHERE user_id=? AND kind IN ('remedial','fast_track','easier') ORDER BY id DESC LIMIT 8", (uid,))]
        finally:
            conn.close()
        by_node = {}
        for a in acts:
            by_node.setdefault(a["node_id"], []).append(a)
        stages, cur_key, weakest, current = [], None, None, None
        for n in nodes:
            lessons = by_node.get(n["id"], [])
            ready = [a for a in lessons if a["status"] == "ready"]
            mastery = sum(a["best"] for a in ready) / len(ready) if ready else None
            proj = [a for a in lessons if a["type"] == "project" and a["status"] == "ready"]
            sk_list = json.loads(n["skills"] or "[]")
            sk = [{"name": s, "mastery": round(skills[(n["id"], s)][0], 3) if (n["id"], s) in skills else None} for s in sk_list]
            status = "done" if n["is_completed"] else "open" if n["is_unlocked"] else "locked"
            try:
                project = json.loads(n["stage_project"]) if n["stage_project"] else None
            except ValueError:
                project = None
            lesson = {"id": n["id"], "order": n["node_order"], "title": n["title"], "objective": n["philosophy"], "timeframe": n["timeframe"],
                      "status": status, "kind": n["kind"], "optional": bool(n["is_optional"]), "fast_track": bool(n["pass_required"]) and not n["is_completed"],
                      "level_hint": n["level_hint"], "mastery": None if mastery is None else round(mastery, 3), "skills": sk,
                      "resources": json.loads(n["resources"] or "[]"), "course": {"title": n["course_title"], "provider": n["course_provider"], "query": n["course_link"]},
                      "project": ({"title": project["title"], "brief": project["brief"], "best": max((a["best"] for a in proj), default=None),
                                   "passed": any(a["passed"] for a in proj), "has_activity": bool(proj)} if project else None)}
            key = (n["stage_order"], n["stage_title"])
            if key != cur_key:
                stages.append({"title": n["stage_title"] or "المسار الأساسي", "order": n["stage_order"], "lessons": []})
                cur_key = key
            stages[-1]["lessons"].append(lesson)
            if status == "open" and current is None and not n["is_optional"]:
                current = lesson
            for s in sk:
                if s["mastery"] is not None and (weakest is None or s["mastery"] < weakest[0]):
                    weakest = (s["mastery"], s["name"], n["title"])
        total = [l for st in stages for l in st["lessons"] if not l["optional"]]
        done = sum(1 for l in total if l["status"] == "done")
        if current:
            msg = f"كمّل: «{current['title']}»."
            if weakest and weakest[0] < WEAK:
                msg += f" ركّز على مهارة «{weakest[1]}» ({round(weakest[0] * 100)}%)."
            rec = {"lesson_id": current["id"], "message": msg}
        else:
            rec = {"lesson_id": None, "message": "🎉 خلّصت كل دروس المسار!" if total and done == len(total) else ""}
        return {"path": dict(path) if path else None, "stages": stages, "progress": {"done": done, "total": len(total)},
                "recommendation": rec, "changes": changes, "levels": LEVELS}

    @router.post("/api/path/adapt")
    def adapt(request: Request):
        uid = uid_or_401(request)
        conn = get_db()
        ids = [r["id"] for r in conn.execute("SELECT id FROM blueprint_nodes WHERE user_id=? AND is_completed=1 AND kind!='remedial' ORDER BY node_order", (uid,))]
        conn.close()

        def run():
            for nid in ids:
                adapt_node(get_db, gen_json, tz, uid, nid)
        threading.Thread(target=run, daemon=True).start()
        return {"started": True, "lessons": len(ids)}

    return router


# =============================================================================
# Browser UI
# =============================================================================
PANEL_HTML = """
          <div id="smartPathPanel" class="rounded-3xl border border-amber-200 bg-white p-4 md:p-5 space-y-4">
            <div class="flex items-center justify-between gap-2">
              <h3 class="text-sm md:text-base font-black text-slate-900">🧭 المسار الذكي <span class="text-[11px] font-bold text-slate-500">(مراحل · دروس · مهارات · مشاريع)</span></h3>
              <button type="button" onclick="spLoad()" class="px-3 py-1.5 rounded-lg bg-slate-900 text-amber-300 text-xs font-black">تحديث</button>
            </div>
            <div id="spBody" class="space-y-3 text-xs text-slate-500">جاري التحميل...</div>
          </div>
"""

SP_JS = r"""
<script>
(function(){
  var SP = {data:null, timer:null, lessons:{}};
  function U(){ return window.LAB_UI; }
  function esc(s){ return U().esc(s); }
  function $(id){ return document.getElementById(id); }
  var SEARCH = {video:"https://www.youtube.com/results?search_query=", article:"https://www.google.com/search?q=", docs:"https://www.google.com/search?q=",
                practice:"https://www.google.com/search?q=", book:"https://www.google.com/search?q="};
  var ICON = {video:"🎬", article:"📰", docs:"📘", practice:"🛠️", book:"📚"};
  function link(type, title, q){ return '<a href="'+(SEARCH[type]||SEARCH.article)+encodeURIComponent(q)+'" target="_blank" rel="noopener noreferrer" class="px-2 py-1 bg-slate-100 rounded-lg hover:bg-amber-100">'+(ICON[type]||"🔗")+" "+esc(title)+'</a>'; }
  function bar(m){
    if(m == null) return '<span class="text-slate-400">لسه</span>';
    var p = Math.round(m*100), c = m >= 0.8 ? "bg-emerald-500" : m >= 0.6 ? "bg-amber-400" : "bg-red-400";
    return '<span class="inline-block w-12 h-1.5 bg-slate-200 rounded-full align-middle overflow-hidden"><span class="block h-full '+c+'" style="width:'+p+'%"></span></span> <b>'+p+'%</b>';
  }
  var STAT = {done:"✅", open:"▶️", locked:"🔒"};

  window.spLoad = async function(){
    clearTimeout(SP.timer);
    var body = $("spBody"); if(!body || !window.LAB_UI) return;
    var d; try { d = await U().api("/api/path/overview"); } catch(e) { body.textContent = e.message; return; }
    SP.data = d; render(d);
    var gen = d.path && d.path.status === "generating";
    if(gen && $("smartPathPanel").offsetParent !== null) SP.timer = setTimeout(window.spLoad, 4000);
  };
  window.addEventListener("lab:closed", function(){ if($("smartPathPanel") && $("smartPathPanel").offsetParent !== null) window.spLoad(); });

  function form(hasPath){
    return '<div class="grid grid-cols-2 gap-2">'
      + '<input id="spGoal" class="col-span-2 bg-slate-50 border border-slate-200 rounded-xl p-2.5 text-xs text-slate-800" placeholder="هدفك: مثلًا Python للمبتدئين، أو فيزياء ثانوية عامة" value="'+esc(hasPath && SP.data.path ? SP.data.path.goal : "")+'">'
      + '<select id="spDur" class="bg-slate-50 border border-slate-200 rounded-xl p-2.5 text-xs text-slate-800"><option>أسبوعين</option><option selected>شهر واحد</option><option>3 أشهر</option><option>6 أشهر</option></select>'
      + '<select id="spLevel" class="bg-slate-50 border border-slate-200 rounded-xl p-2.5 text-xs text-slate-800"><option value="beginner">مستواي: مبتدئ</option><option value="intermediate">متوسط</option><option value="advanced">متقدم</option></select>'
      + '<input id="spKnown" class="col-span-2 bg-slate-50 border border-slate-200 rounded-xl p-2.5 text-xs text-slate-800" placeholder="اللي أعرفه بالفعل (اختياري) عشان نتخطاه">'
      + '<button type="button" onclick="spGenerate()" class="col-span-2 py-3 bg-gradient-to-r from-amber-400 to-yellow-400 text-slate-900 font-black text-xs rounded-xl">🧭 ابنِ المسار الذكي</button>'
      + '<p id="spMsg" class="col-span-2 text-xs text-slate-500"></p></div>';
  }

  function render(d){
    var body = $("spBody"), p = d.path, h = "";
    if(p && p.status === "generating"){ body.innerHTML = '<p class="text-amber-700 font-bold">⏳ بنبني مسارك (مراحل ودروس ومهارات). ممكن ياخد دقيقة أو اتنين، تقدر تسيب الصفحة.</p>'; return; }
    if(p && p.status === "failed" && p.error) h += '<p class="text-red-600">'+esc(p.error)+'</p>';
    if(!d.stages.length){ body.innerHTML = h + '<p>مفيش مسار ذكي لسه. اكتب هدفك ومستواك وهنبنيه لك.</p>' + form(false); return; }
    SP.lessons = {};
    h += '<div class="p-3 bg-amber-50 border border-amber-200 rounded-2xl space-y-1.5 text-slate-700">'
      + (p && p.goal ? '<div class="flex justify-between font-black text-slate-900"><span>🎯 '+esc(p.goal)+'</span><span>'+d.progress.done+' / '+d.progress.total+' درس</span></div>' : '')
      + (p && p.summary ? '<p>'+esc(p.summary)+'</p>' : '')
      + '<div class="h-2 bg-amber-100 rounded-full overflow-hidden"><div class="h-full bg-amber-400" style="width:'+(d.progress.total ? Math.round(100*d.progress.done/d.progress.total) : 0)+'%"></div></div>'
      + (d.recommendation.message ? '<p class="font-bold text-slate-900">👉 '+esc(d.recommendation.message)+'</p>' : '')+'</div>';
    d.stages.forEach(function(st, si){
      var done = st.lessons.filter(function(l){return l.status==="done";}).length;
      var openIt = st.lessons.some(function(l){ return l.status === "open" && !l.optional; }) || d.stages.length === 1;
      h += '<details class="border border-slate-200 rounded-2xl" '+(openIt?"open":"")+'><summary class="cursor-pointer p-3 font-black text-slate-900 flex justify-between"><span>'+esc(st.title)+'</span><span class="text-slate-500">'+done+'/'+st.lessons.length+'</span></summary><div class="p-2 space-y-2">';
      st.lessons.forEach(function(l){
        SP.lessons[l.id] = l;
        var badges = (l.kind==="remedial" ? '<span class="px-1.5 py-0.5 bg-blue-100 text-blue-800 rounded-md">مراجعة</span> ' : '')
          + (l.optional ? '<span class="px-1.5 py-0.5 bg-slate-100 rounded-md">اختياري</span> ' : '')
          + (l.fast_track ? '<span class="px-1.5 py-0.5 bg-emerald-100 text-emerald-800 rounded-md">⚡ مسار سريع</span> ' : '');
        var skills = l.skills.map(function(s){ return '<div class="flex justify-between gap-2"><span>• '+esc(s.name)+'</span><span class="whitespace-nowrap">'+bar(s.mastery)+'</span></div>'; }).join("");
        var res = (l.course && l.course.query ? link("video", l.course.title || "الكورس", l.course.query) : "") + l.resources.map(function(r){ return link(r.type, r.title, r.search_query); }).join("");
        var proj = l.project ? '<div class="p-2 bg-amber-50 border border-amber-200 rounded-xl space-y-1"><b>🏁 مشروع المرحلة: '+esc(l.project.title)+'</b><p>'+esc(l.project.brief)+'</p>'
          + (l.project.passed ? '<p class="text-emerald-700 font-bold">✅ اجتزت المشروع</p>' : '')
          + '<button type="button" '+(l.status==="locked"?"disabled":"")+' onclick="spProject('+l.id+')" class="px-3 py-1.5 bg-slate-900 text-amber-300 font-black rounded-lg disabled:opacity-40">'+(l.project.has_activity?'افتح المشروع':'ولّد المشروع')+'</button></div>' : "";
        h += '<div class="p-3 rounded-2xl border '+(l.status==="open"?"border-amber-300 bg-white":"border-slate-200 bg-slate-50")+' space-y-2 text-slate-600">'
          + '<div class="flex items-start justify-between gap-2"><div><b class="text-slate-900">'+STAT[l.status]+' '+esc(l.title)+'</b> '+badges+'<p>'+esc(l.objective)+'</p>'
          + (l.timeframe ? '<p class="text-slate-400">'+esc(l.timeframe)+'</p>' : '')+'</div>'
          + '<div class="text-center whitespace-nowrap"><div>إتقان</div>'+bar(l.mastery)+'</div></div>'
          + '<div class="space-y-0.5">'+skills+'</div><div class="flex flex-wrap gap-1.5">'+res+'</div>' + proj
          + '<button type="button" '+(l.status==="locked"?"disabled":"")+' onclick="spOpen('+l.id+')" class="w-full py-2 bg-slate-900 text-amber-300 font-black rounded-xl disabled:opacity-40">🧪 أنشطة الدرس</button></div>';
      });
      h += '</div></details>';
    });
    if(d.changes.length){
      h += '<div class="p-3 bg-blue-50 border border-blue-200 rounded-2xl space-y-1 text-slate-700"><b>🔄 تعديلات المسار بناءً على نتايجك</b>'
        + d.changes.map(function(c){ return '<p>• '+esc(c.detail)+'</p>'; }).join("")+'</div>';
    }
    h += '<div class="flex gap-2"><button type="button" onclick="spAdapt()" class="flex-1 py-2.5 bg-blue-600 text-white font-black rounded-xl">🔄 راجع مساري الآن</button></div><p id="spMsg" class="text-slate-500"></p>';
    h += '<details class="border border-slate-200 rounded-2xl"><summary class="cursor-pointer p-3 font-black text-slate-900">مسار جديد (بيمسح الحالي)</summary><div class="p-3">'+form(true)+'</div></details>';
    body.innerHTML = h;
  }

  window.spOpen = function(id){ var l = SP.lessons[id]; if(l) window.labOpen(id, l.title); };
  window.spProject = async function(id){
    var l = SP.lessons[id], msg = $("spMsg");
    try {
      if(!l.project.has_activity){
        await U().post("/api/lab/nodes/"+id+"/activities", {type:"project", difficulty:"medium", focus:"مشروع المرحلة: "+l.project.title+" - "+l.project.brief});
      }
      window.labOpen(id, l.title);
    } catch(e) { if(msg) msg.textContent = e.message; }
  };
  window.spAdapt = async function(){
    var msg = $("spMsg");
    try { var d = await U().post("/api/path/adapt", {}); msg.textContent = "بنراجع "+d.lessons+" درس مكتمل..."; setTimeout(window.spLoad, 5000); setTimeout(window.spLoad, 12000); }
    catch(e) { msg.textContent = e.message; }
  };
  window.spGenerate = async function(replace){
    var msg = $("spMsg");
    var body = {goal:$("spGoal").value, duration:$("spDur").value, level:$("spLevel").value, known:$("spKnown").value, replace: replace === true};
    try { await U().post("/api/path/generate", body); window.spLoad(); }
    catch(e) {
      if(/عندك مسار حالي/.test(e.message)){ if(confirm(e.message+"\nمتأكد؟")) return window.spGenerate(true); }
      else msg.textContent = e.message;
    }
  };

  /* ----- "project" activity renderer (extends the Learning Lab) ----- */
  var R = window.LAB_RENDERERS; if(!R) return;
  R.project = function(a, box, out){
    var s = a.spec, L = window.LAB_UI;
    function list(t, arr){ return arr && arr.length ? '<div class="text-xs"><b>'+t+'</b><ul class="list-disc ps-5">'+arr.map(function(x){return "<li>"+L.esc(x)+"</li>";}).join("")+'</ul></div>' : ""; }
    box.innerHTML = '<p class="text-sm whitespace-pre-wrap">'+L.esc(s.scenario)+'</p>' + list("📦 المطلوب تسليمه", s.deliverables) + list("⛓️ القيود", s.constraints)
      + '<div class="p-3 bg-slate-50 border border-slate-200 rounded-2xl text-xs space-y-1"><b>✅ المهام (علّم اللي خلّصته)</b>'
      + s.tasks.map(function(t){ return '<label class="flex gap-2 items-start"><input type="checkbox" class="mt-0.5"><span><b>'+L.esc(t.title)+'</b> '+L.esc(t.detail)+'</span></label>'; }).join("")+'</div>'
      + '<div class="p-3 bg-slate-50 border border-slate-200 rounded-2xl text-xs space-y-1"><b>معايير التقييم ('+s.max_points+' درجة)</b>'
      + s.criteria.map(function(c){ return '<p>• <b>'+L.esc(c.name)+'</b> ('+c.points+'): '+L.esc(c.description)+'</p>'; }).join("")+'</div>'
      + '<p class="text-[11px] text-slate-500">الصق شغلك نفسه (الكود/الحل/الشرح وقراراتك). اللينكات مش بتتفتح، والتصحيح بيعتمد على النص اللي بتلصقه.</p>'
      + '<textarea id="labAns" rows="12" class="w-full bg-slate-50 border border-slate-200 rounded-2xl p-3 text-sm" placeholder="تسليمك..."></textarea>'
      + L.hints(a) + '<button type="button" id="labSend" class="w-full py-3 bg-gradient-to-r from-amber-400 to-yellow-400 font-black text-xs rounded-xl">📨 سلّم المشروع</button>';
    L.bindHints(a);
    document.getElementById("labSend").onclick = async function(){
      out.innerHTML = '<p class="text-xs text-slate-500">⏳ بيتصحح...</p>';
      var d = await L.submit(a.id, {answer: document.getElementById("labAns").value}, out, this); if(!d) return;
      var rows = d.details.criteria.map(function(c){
        return '<div class="p-2.5 rounded-xl border '+(c.points>=c.max?'bg-emerald-50 border-emerald-200':c.points>0?'bg-amber-50 border-amber-200':'bg-red-50 border-red-200')+' text-xs space-y-0.5">'
          + '<div class="flex justify-between font-black"><span>'+L.esc(c.name)+'</span><span>'+c.points+' / '+c.max+'</span></div>'
          + (c.evidence?'<p class="text-slate-600">«'+L.esc(c.evidence)+'»</p>':"")+(c.comment?'<p>'+L.esc(c.comment)+'</p>':"")+'</div>'; }).join("");
      out.innerHTML = L.scoreBadge(d)+'<div class="space-y-1.5">'+rows+'</div><p class="text-[11px] text-slate-500">'+L.esc(d.details.links_note||"")+'</p>'+L.aiBox(d.feedback)
        + (d.model_answer ? '<details class="text-xs"><summary class="font-black cursor-pointer">وصف تسليم ممتاز</summary><p class="p-2 whitespace-pre-wrap">'+L.esc(d.model_answer)+'</p></details>' : "")
        + (d.path && d.path.note ? '<p class="p-2 bg-blue-50 rounded-xl text-xs">'+L.esc(d.path.note)+'</p>' : "") + L.progressBox(d);
    };
  };
  window.spLoad();
})();
</script>
"""


def inject_ui(page: str) -> str:
    m = re.search(r'<div class="space-y-4 pt-2">\s*<h3[^>]*>\s*<span>📍</span>', page)
    if m:
        page = page[:m.start()] + PANEL_HTML + "\n          " + page[m.start():]
    else:
        print("[smart_path] WARNING: roadmap tab anchor not found")
    hook = '      if (t === "history") {'
    if hook in page:
        page = page.replace(hook, '      if (t === "gps" && window.spLoad) { window.spLoad(); }\n' + hook, 1)
    else:
        print("[smart_path] WARNING: switchTab hook anchor not found")
    if "</body>" in page:
        page = page.replace("</body>", SP_JS + "\n</body>", 1)
    return page
