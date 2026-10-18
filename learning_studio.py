"""Cross-discipline learning workspace for S-TESSERACT.

Code execution is delegated to an isolated Piston-compatible runner. Do not replace
this with exec/eval or local shell execution of student code.
"""
from __future__ import annotations
import json, os, urllib.request, urllib.error
from datetime import datetime
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel, Field

RUNNER_URL = os.environ.get("CODE_RUNNER_URL", "https://emkc.org/api/v2/piston/execute").strip()
MAX_CODE_CHARS = 20000
MAX_WORK_CHARS = 30000

class WorkPayload(BaseModel):
    domain: str = Field(default="general", max_length=80)
    activity_type: str = Field(default="practice", max_length=80)
    title: str = Field(default="Study work", max_length=240)
    content: str = Field(default="", max_length=MAX_WORK_CHARS)
    language: str = Field(default="", max_length=30)
    result: dict = Field(default_factory=dict)
    roadmap_node_id: int | None = None

class CodePayload(BaseModel):
    language: str = Field(default="python", max_length=30)
    code: str = Field(min_length=1, max_length=MAX_CODE_CHARS)
    stdin: str = Field(default="", max_length=4000)

class ActivityPayload(BaseModel):
    domain: str = Field(min_length=1, max_length=100)
    topic: str = Field(min_length=1, max_length=300)
    level: str = Field(default="beginner", max_length=80)
    goal: str = Field(default="understand and apply", max_length=300)
    lang: str = Field(default="ar", max_length=8)
    code_language: str = Field(default="python", max_length=30)
    roadmap_node_id: int | None = None

class FeedbackPayload(BaseModel):
    domain: str = Field(default="general", max_length=100)
    task: str = Field(min_length=1, max_length=3000)
    answer: str = Field(min_length=1, max_length=MAX_WORK_CHARS)
    level: str = Field(default="beginner", max_length=80)
    lang: str = Field(default="ar", max_length=8)


def migrate(get_db):
    conn = get_db()
    conn.execute("""CREATE TABLE IF NOT EXISTS learning_lab_submissions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        domain TEXT NOT NULL DEFAULT 'general',
        activity_type TEXT NOT NULL DEFAULT 'practice',
        title TEXT NOT NULL DEFAULT 'Study work',
        content TEXT NOT NULL DEFAULT '',
        language TEXT NOT NULL DEFAULT '',
        result_json TEXT NOT NULL DEFAULT '{}',
        roadmap_node_id INTEGER,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_learning_lab_user_time ON learning_lab_submissions(user_id, created_at DESC)")
    conn.commit(); conn.close()


def build_router(get_db, require_user, generate_ai_json, timezone):
    router = APIRouter()
    def now(): return datetime.now(timezone).isoformat()

    @router.get("/api/learning-studio/history")
    def history(request: Request, limit: int = 30):
        uid = require_user(request)
        limit = max(1, min(int(limit), 100))
        conn = get_db()
        rows = conn.execute("SELECT id,domain,activity_type,title,language,result_json,roadmap_node_id,created_at,updated_at,substr(content,1,500) AS preview FROM learning_lab_submissions WHERE user_id=? ORDER BY id DESC LIMIT ?", (uid,limit)).fetchall()
        conn.close()
        items=[]
        for row in rows:
            d=dict(row)
            try: d["result"] = json.loads(d.pop("result_json") or "{}")
            except Exception: d["result"] = {}
            items.append(d)
        return {"success":True,"items":items}

    @router.get("/api/learning-studio/work/{work_id}")
    def get_work(work_id: int, request: Request):
        uid = require_user(request)
        conn=get_db(); row=conn.execute("SELECT * FROM learning_lab_submissions WHERE id=? AND user_id=?",(work_id,uid)).fetchone(); conn.close()
        if not row: raise HTTPException(status_code=404,detail="Saved work not found")
        d=dict(row)
        try: d["result"] = json.loads(d.pop("result_json") or "{}")
        except Exception: d["result"]={}
        return {"success":True,"work":d}

    @router.post("/api/learning-studio/save")
    def save_work(payload: WorkPayload, request: Request):
        uid=require_user(request)
        content=payload.content.strip()
        if not content: raise HTTPException(status_code=400,detail="Nothing to save yet")
        timestamp=now(); conn=get_db()
        cur=conn.execute("INSERT INTO learning_lab_submissions(user_id,domain,activity_type,title,content,language,result_json,roadmap_node_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",(uid,payload.domain.strip(),payload.activity_type.strip(),payload.title.strip() or "Study work",content,payload.language.strip(),json.dumps(payload.result,ensure_ascii=False)[:50000],payload.roadmap_node_id,timestamp,timestamp))
        work_id=cur.lastrowid; conn.commit(); conn.close()
        return {"success":True,"id":work_id,"created_at":timestamp}

    @router.post("/api/learning-studio/run-code")
    async def run_code(payload: CodePayload, request: Request):
        require_user(request)
        lang=payload.language.lower().strip()
        languages={"python":("python","3.10.0"),"javascript":("javascript","18.15.0"),"cpp":("cpp","10.2.0"),"c++":("cpp","10.2.0"),"java":("java","15.0.2"),"c":("c","10.2.0")}
        if lang not in languages: raise HTTPException(status_code=400,detail="Unsupported language")
        if not RUNNER_URL: raise HTTPException(status_code=503,detail="Code runner is not configured. Set CODE_RUNNER_URL to a trusted isolated Piston-compatible service.")
        runner_lang,version=languages[lang]
        filename=("Main.java" if runner_lang=="java" else "main.py" if runner_lang=="python" else "main.js" if runner_lang=="javascript" else "main.cpp" if runner_lang=="cpp" else "main.c")
        body={"language":runner_lang,"version":version,"files":[{"name":filename,"content":payload.code}],"stdin":payload.stdin,"args":[],"compile_timeout":10000,"run_timeout":5000,"compile_memory_limit":100000000,"run_memory_limit":100000000}
        req=urllib.request.Request(RUNNER_URL,data=json.dumps(body).encode(),headers={"Content-Type":"application/json","Accept":"application/json"},method="POST")
        import asyncio
        def call_runner():
            try:
                with urllib.request.urlopen(req,timeout=18) as resp:
                    return json.loads(resp.read().decode("utf-8","replace"))
            except urllib.error.HTTPError as e:
                detail=e.read(1000).decode("utf-8","replace")
                raise RuntimeError(f"Runner returned HTTP {e.code}: {detail}")
        try:
            result=await asyncio.to_thread(call_runner)
        except Exception as e:
            raise HTTPException(status_code=502,detail=f"Code runner unavailable: {str(e)[:400]}")
        compile_result=result.get("compile") or {}
        run_result=result.get("run") or {}
        compile_code=compile_result.get("code", 0)
        run_code=run_result.get("code", 1)
        compile_failed=(compile_code not in (0, None)) or bool(compile_result.get("stderr")) and not run_result
        status="success" if run_code == 0 and not run_result.get("signal") and not compile_failed else "error"
        return {"success":True,"language":runner_lang,"stdout":str(run_result.get("stdout") or "")[:12000],"stderr":str(run_result.get("stderr") or "")[:12000],"compile_output":str(compile_result.get("output") or "")[:12000],"compile_stderr":str(compile_result.get("stderr") or "")[:12000],"compile_exit_code":compile_code,"exit_code":run_code,"signal":run_result.get("signal"),"status":status}

    @router.post("/api/learning-studio/design-activity")
    def design_activity(payload: ActivityPayload, request: Request):
        uid=require_user(request)
        language_instruction="Write the result in English." if payload.lang=="en" else "اكتب بالعربية الواضحة، مع المصطلحات العلمية المهمة بالإنجليزية عند الحاجة."
        prompt=f"""You are a cross-disciplinary learning designer. {language_instruction}
Create ONE concrete, safe, achievable interactive learning activity tailored to this domain, not a generic study checklist.
Domain: {payload.domain}\nTopic: {payload.topic}\nLevel: {payload.level}\nGoal: {payload.goal}
When Domain is programming, target language is {payload.code_language}. Any code task, examples, instructions and test cases must specifically match that language. Never silently switch languages.
Select a suitable format from: coding_lab, problem_solver, language_practice, experiment_or_simulation, reading_and_retrieval, case_study, design_project, creative_practice, mixed_practice.
Do not fabricate direct URLs, citations, or claim an experiment was physically performed. If external tools are needed, name them and provide search queries rather than made-up links. Give a solution/checking guide separately so a learner can try first. For code, include explicit test cases but do not claim the code was executed. For maths/science, include units and reasoning checkpoints. For law/medicine/finance, frame as education not professional advice.
Return strict JSON with: title, activityType, objective, prerequisites (array), materialsOrTools (array), steps (array of objects with step and expectedEvidence), expectedOutput, checkQuestions (array of objects question/options/correctIndex/explanation), rubric (array of objects criterion/observableEvidence), masteryThreshold (60-100), commonMistakes (array), reviewPlan, resources (array of objects title/provider/searchQuery/why), testCases (array of objects input/expected/why; include only for coding tasks, otherwise empty), answerGuide. Keep it specific to the requested topic."""
        try: data=generate_ai_json(prompt)
        except Exception as e: raise HTTPException(status_code=502,detail=f"Could not generate activity: {str(e)[:300]}")
        if not isinstance(data,dict) or not data.get("title") or not isinstance(data.get("steps"),list):
            raise HTTPException(status_code=502,detail="The AI response did not contain a valid activity. Please retry.")
        timestamp=now(); conn=get_db()
        cur=conn.execute("INSERT INTO learning_lab_submissions(user_id,domain,activity_type,title,content,language,result_json,roadmap_node_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",(uid,payload.domain,"designed_activity",str(data["title"])[:240],json.dumps(data,ensure_ascii=False),(payload.code_language if payload.domain == "programming" else payload.lang),json.dumps({"generated_activity":data,"language":payload.code_language if payload.domain == "programming" else payload.lang},ensure_ascii=False),payload.roadmap_node_id,timestamp,timestamp))
        work_id=cur.lastrowid; conn.commit(); conn.close()
        return {"success":True,"id":work_id,"activity":data,"created_at":timestamp}

    @router.post("/api/learning-studio/feedback")
    def feedback(payload: FeedbackPayload, request: Request):
        uid=require_user(request)
        lang_note="Respond in English." if payload.lang=="en" else "أجب بالعربية الواضحة."
        prompt=f"""You are a careful tutor giving formative feedback, not an unquestionable grader. {lang_note}
Discipline: {payload.domain}\nStudent level: {payload.level}\nTask: {payload.task[:3000]}\nStudent's work: {payload.answer[:MAX_WORK_CHARS]}
Evaluate only what can be supported from the task and answer. State uncertainty when needed. Do not praise falsely. For mathematics, inspect steps and identify the first apparent error; for code, reason about logic but never claim runtime testing; for languages, separate meaning, grammar and style; for theory, distinguish factual correctness from argument quality. Give hints before a full solution and mention what requires a human/source check.
Return JSON: score (integer 0-100), scoreRationale (short explanation of how the score was derived), overallAssessment, strengths (array), issues (array of objects issue/why/hint/severity), nextSteps (array), rubric (array of objects criterion/level/evidence/pointsEarned/pointsPossible), suggestedCorrection, confidence (low/medium/high). Score fairly against the task and level; do not invent missing requirements. If the task does not provide enough criteria for a reliable numerical score, give a provisional score and explicitly state the limitation in scoreRationale. For code, assess correctness, logic, edge cases, readability, and whether actual test results were supplied. Never claim code execution unless the app's runner actually executed it; use provided test results as evidence only."""
        try: result=generate_ai_json(prompt)
        except Exception as e: raise HTTPException(status_code=502,detail=f"Feedback failed: {str(e)[:300]}")
        ts=now(); conn=get_db()
        cur=conn.execute("INSERT INTO learning_lab_submissions(user_id,domain,activity_type,title,content,language,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",(uid,payload.domain,"feedback",f"Feedback: {payload.task[:180]}",payload.answer,payload.lang,json.dumps({"task":payload.task,"feedback":result},ensure_ascii=False),ts,ts))
        work_id=cur.lastrowid; conn.commit(); conn.close()
        return {"success":True,"id":work_id,"feedback":result,"created_at":ts}
    return router
