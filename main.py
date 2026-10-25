import os, io, json, secrets
from pathlib import Path
from fastapi import FastAPI, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import select
from passlib.context import CryptContext
from dotenv import load_dotenv
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from .database import Base, engine, get_db
from .models import User, GuestProfile, Exam, ExamAttempt
from .ai_service import generate_questions, grade_answers
load_dotenv()
ROOT=Path(__file__).resolve().parent
app=FastAPI(title=os.getenv("APP_NAME","Axiom Learn"))
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SECRET_KEY","dev-only-change-me"), same_site="lax", https_only=False)
app.mount("/static",StaticFiles(directory=str(ROOT/"static")),name="static")
templates=Jinja2Templates(directory=str(ROOT/"templates")); Base.metadata.create_all(bind=engine)
pwd=CryptContext(schemes=["bcrypt"],deprecated="auto")
def identity(request,db):
    if request.session.get("user_id"):
        u=db.get(User,int(request.session["user_id"]))
        if u:return {"kind":"user","id":u.id,"name":u.display_name}
    if request.session.get("guest_id"):
        g=db.get(GuestProfile,request.session["guest_id"])
        if g:return {"kind":"guest","id":g.id,"name":g.display_name}
    return None
def owned(db,eid,who):
    e=db.get(Exam,eid)
    if not e or (who["kind"]=="user" and e.user_id!=who["id"]) or (who["kind"]=="guest" and e.guest_id!=who["id"]): raise HTTPException(404,"Exam not found")
    return e
@app.get("/",response_class=HTMLResponse)
def home(request:Request,db:Session=Depends(get_db)):
    who=identity(request,db)
    if not who:return templates.TemplateResponse("auth.html",{"request":request,"error":None})
    col=Exam.user_id if who["kind"]=="user" else Exam.guest_id
    exams=db.scalars(select(Exam).where(col==who["id"]).order_by(Exam.created_at.desc())).all()
    return templates.TemplateResponse("dashboard.html",{"request":request,"identity":who,"exams":exams})
@app.post("/register")
def register(request:Request,email:str=Form(...),display_name:str=Form(...),password:str=Form(...),db:Session=Depends(get_db)):
    email=email.strip().lower(); name=display_name.strip()
    if not 2<=len(name)<=80 or len(password)<8:return templates.TemplateResponse("auth.html",{"request":request,"error":"Name must be 2–80 characters and password at least 8 characters."},status_code=400)
    if db.scalar(select(User).where(User.email==email)):return templates.TemplateResponse("auth.html",{"request":request,"error":"This email already has an account."},status_code=400)
    u=User(email=email,password_hash=pwd.hash(password),display_name=name);db.add(u);db.commit();db.refresh(u);request.session.clear();request.session["user_id"]=u.id
    return RedirectResponse("/",303)
@app.post("/login")
def login(request:Request,email:str=Form(...),password:str=Form(...),db:Session=Depends(get_db)):
    u=db.scalar(select(User).where(User.email==email.strip().lower()))
    if not u or not pwd.verify(password,u.password_hash):return templates.TemplateResponse("auth.html",{"request":request,"error":"Email or password is incorrect."},status_code=401)
    request.session.clear();request.session["user_id"]=u.id;return RedirectResponse("/",303)
@app.post("/guest")
def guest(request:Request,db:Session=Depends(get_db)):
    gid=secrets.token_urlsafe(24);g=GuestProfile(id=gid,display_name=f"Player{secrets.randbelow(900000)+100000}");db.add(g);db.commit();request.session.clear();request.session["guest_id"]=gid;return RedirectResponse("/",303)
@app.post("/rename")
def rename(request:Request,display_name:str=Form(...),db:Session=Depends(get_db)):
    who=identity(request,db);name=display_name.strip()
    if not who:return RedirectResponse("/",303)
    if not 2<=len(name)<=80:raise HTTPException(400,"Name must be 2–80 characters")
    obj=db.get(GuestProfile,who["id"]) if who["kind"]=="guest" else db.get(User,who["id"]);obj.display_name=name;db.commit();return RedirectResponse("/",303)
@app.post("/logout")
def logout(request:Request):request.session.clear();return RedirectResponse("/",303)
@app.post("/exams")
async def create_exam(request:Request,title:str=Form(...),subject:str=Form(...),source_text:str=Form(""),count:int=Form(5),difficulty:str=Form("medium"),db:Session=Depends(get_db)):
    who=identity(request,db)
    if not who:return RedirectResponse("/",303)
    count=max(1,min(20,count));title=title.strip()[:200];subject=subject.strip()[:200]
    if not title or not subject:raise HTTPException(400,"Title and subject are required")
    qs,mode=await generate_questions(subject,source_text,count,difficulty)
    e=Exam(title=title,subject=subject,questions_json=json.dumps(qs,ensure_ascii=False),user_id=who["id"] if who["kind"]=="user" else None,guest_id=who["id"] if who["kind"]=="guest" else None);db.add(e);db.commit();db.refresh(e)
    return RedirectResponse(f"/exams/{e.id}?mode={mode}",303)
@app.get("/exams/{eid}",response_class=HTMLResponse)
def take_exam(request:Request,eid:int,db:Session=Depends(get_db)):
    who=identity(request,db)
    if not who:return RedirectResponse("/",303)
    e=owned(db,eid,who);return templates.TemplateResponse("exam.html",{"request":request,"identity":who,"exam":e,"questions":json.loads(e.questions_json),"mode":request.query_params.get("mode","saved")})
@app.post("/exams/{eid}/submit")
async def submit(request:Request,eid:int,db:Session=Depends(get_db)):
    who=identity(request,db)
    if not who:return RedirectResponse("/",303)
    e=owned(db,eid,who);qs=json.loads(e.questions_json);form=await request.form();answers={str(i):str(form.get(f"q_{i}","")) for i in range(len(qs))};grades,mode=await grade_answers(qs,answers)
    score=sum(max(0,min(int(q.get("points",1)),int(g.get("points_awarded",0)))) for q,g in zip(qs,grades));total=sum(int(q.get("points",1)) for q in qs)
    db.add(ExamAttempt(exam_id=e.id,answers_json=json.dumps(answers,ensure_ascii=False),score=score,feedback_json=json.dumps(grades,ensure_ascii=False)));db.commit()
    return templates.TemplateResponse("results.html",{"request":request,"identity":who,"exam":e,"questions":qs,"answers":answers,"grading":grades,"score":score,"total":total,"mode":mode})
def pdf_response(e,answer_key=False):
    qs=json.loads(e.questions_json);buf=io.BytesIO();c=canvas.Canvas(buf,pagesize=A4);_,height=A4;y=height-50;c.setFont("Helvetica-Bold",15);c.drawString(40,y,(e.title+(" — Answer Key" if answer_key else ""))[:90]);y-=30;c.setFont("Helvetica",10)
    for i,q in enumerate(qs,1):
        lines=[f"{i}. {q.get('answer','')}" , f"Explanation: {q.get('explanation','')}" ] if answer_key else [f"{i}. {q.get('question','')}"]+[f"   {chr(65+j)}. {o}" for j,o in enumerate(q.get("options",[]))]
        for line in lines:
            if y<50:c.showPage();y=height-50;c.setFont("Helvetica",10)
            c.drawString(40,y,line[:115]);y-=15
        y-=8
    c.save();buf.seek(0);name=f"answers-{e.id}.pdf" if answer_key else f"exam-{e.id}.pdf"
    return StreamingResponse(buf,media_type="application/pdf",headers={"Content-Disposition":f'attachment; filename="{name}"'})
@app.get("/exams/{eid}/pdf")
def export_exam(request:Request,eid:int,db:Session=Depends(get_db)):
    who=identity(request,db)
    if not who:return RedirectResponse("/",303)
    return pdf_response(owned(db,eid,who))
@app.get("/exams/{eid}/answers.pdf")
def export_answers(request:Request,eid:int,db:Session=Depends(get_db)):
    who=identity(request,db)
    if not who:return RedirectResponse("/",303)
    return pdf_response(owned(db,eid,who),True)
