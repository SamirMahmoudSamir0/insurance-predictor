import os, json, httpx
from dotenv import load_dotenv
load_dotenv()
def extract_json(text):
    text=text.strip()
    if text.startswith("```"):
        text=text.split("\n",1)[-1]
        if text.endswith("```"): text=text[:-3]
    a,b=text.find("["),text.rfind("]")
    return json.loads(text[a:b+1] if a>=0 and b>a else text)
async def chat(prompt, system):
    key=os.getenv("AI_API_KEY", "").strip()
    if not key: return None
    base=os.getenv("AI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model=os.getenv("AI_MODEL", "gpt-4o-mini")
    async with httpx.AsyncClient(timeout=90) as client:
        r=await client.post(base+"/chat/completions",headers={"Authorization":"Bearer "+key},json={"model":model,"temperature":0.2,"messages":[{"role":"system","content":system},{"role":"user","content":prompt}]})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
async def generate_questions(subject, notes, count, difficulty):
    raw=await chat(f"Create {count} original questions about {subject}, difficulty {difficulty}. Use material as primary source. Return only JSON array; each item: type ('mcq' or 'short'), question, options array, answer string, explanation string, points integer.\nMaterial:\n{notes[:18000]}", "You create rigorous educational exams. Output valid JSON only.")
    if raw:
        return extract_json(raw), "api"
    seed=[line.strip() for line in notes.splitlines() if line.strip()] or [f"the core concepts of {subject}"]
    return ([{"type":"short","question":f"Explain or apply this idea from {subject}: {seed[i%len(seed)][:250]}","options":[],"answer":f"A correct answer accurately explains the relevant concept in {subject}, with reasoning and an example where appropriate.","explanation":"Demo rubric. Configure AI_API_KEY for AI-generated content.","points":1} for i in range(count)],"demo")
async def grade_answers(questions, answers):
    raw=await chat("Grade the student's answers. Return only JSON array of objects {index, correct, points_awarded, feedback}. Be fair; points_awarded cannot exceed question points.\n"+json.dumps({"questions":questions,"answers":answers},ensure_ascii=False),"You are a careful educational grader. Output valid JSON only.")
    if raw: return extract_json(raw),"api"
    out=[]
    for i,q in enumerate(questions):
        given=str(answers.get(str(i),"")).strip(); expected=str(q.get("answer",""))
        correct=bool(given) and given.casefold()==expected.casefold()
        out.append({"index":i,"correct":correct,"points_awarded":q.get("points",1) if correct else 0,"feedback":"Correct." if correct else "Demo grading uses exact matching; review the expected answer."})
    return out,"demo"
