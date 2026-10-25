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
      const allowed = new Set(["studyroom","home","video","studycenter","history","gps","learningstudio","calendar","leaderboard","account"]);
      if (!allowed.has(t)) t = "home";
      if (window.innerWidth < 768) closeMobileSidebar();
      document.querySelectorAll(".tab-content").forEach(e => e.classList.add("hidden"));
      const target = document.getElementById("tab-" + t) || document.getElementById("tab-home");
      if (target) target.classList.remove("hidden");

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
