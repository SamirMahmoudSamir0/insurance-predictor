"""
Server-side quality gate for AI-written roadmaps (S-TESSERACT).

The generator prompt already asks the model for a plan that fits the student's time. A prompt is a request, not a
guarantee, so this module checks the answer BEFORE it is saved, and fixes what can be fixed deterministically:

  time budget   minutes of activities + resources are fitted to what the student can really study
                (duration x study days per week x minutes per day), per milestone.
  courses       bootcamps and courses whose own name claims more time than the whole plan ("100 Days of Code",
                "60 hours") are replaced by a targeted search for the milestone's topic.
  duplicates    two milestones on the same topic, or more than one "introduction/setup" milestone, or milestones that
                teach the same concepts -> the model is asked ONCE to repair them; whatever remains is flagged.
  quiz fairness duplicate/empty options are removed, and options are shuffled with a stable seed so the right answer
                is not always in the first position ("all of the above" style questions keep their order).

Nothing here imports a web framework or touches the database, so it is easy to test.
"""
import copy
import random
import re

NODES_EXPECTED = 4
RESOURCE_SHARE = 0.40        # resources may use at most this share of a milestone's minutes
ACTIVITY_SHARE_MIN = 0.50    # activities always get at least this share
TOLERANCE = 1.10

_INTRO_RE = re.compile(r"(مقدم|مدخل|تمهيد|تثبيت|إعداد بيئة|اعداد بيئة|نظرة عامة|introduction|\bintro\b|getting started|overview|\bsetup\b|install)", re.I)
_STOP = set("و في من على إلى الى عن مع the a an of to and for in with".split())
_BOOTCAMP = re.compile(r"(boot\s?camp|بوتكامب|masterclass|zero to hero)", re.I)
_LENGTH = re.compile(r"(\d+)\s*[- ]?\s*(days?|يوم|أيام|ايام|hours?|hrs?|ساعة|ساعات|weeks?|أسبوع|اسبوع)", re.I)
_ALL_ABOVE = re.compile(r"(كل ما سبق|جميع ما سبق|جميع الإجابات|كل الإجابات|لا شيء مما سبق|لا شيء مما ذكر|all of the above|none of the above|\bboth\b)", re.I)


# ----------------------------------------------------------------------------
# Time
# ----------------------------------------------------------------------------
def duration_days(text) -> int:
    """'أسبوعين' -> 14, 'شهر' -> 30, '3 أشهر' -> 90 ... (default 30)."""
    t = str(text or "").lower()
    n = re.search(r"(\d+)", t)
    num = int(n.group(1)) if n else None
    if re.search(r"(أسبوعين|اسبوعين)", t):
        return 14
    if re.search(r"(أسبوع|اسبوع|week)", t):
        return 7 * (num or 1)
    if re.search(r"(يوم|أيام|ايام|day)", t):
        return num or 1
    if re.search(r"(شهرين)", t):
        return 60
    if re.search(r"(شهر|أشهر|اشهر|month)", t):
        return 30 * (num or 1)
    return 30


def build_budget(duration, daily_minutes, days_per_week, n_nodes=NODES_EXPECTED) -> dict:
    days = duration_days(duration)
    daily = max(15, min(480, int(daily_minutes or 60)))
    dpw = max(1, min(7, int(days_per_week or 5)))
    total = max(30, int(max(1.0, days * dpw / 7.0) * daily))
    return {"days": days, "dailyMinutes": daily, "daysPerWeek": dpw, "totalMinutes": total, "perNodeMinutes": max(20, total // n_nodes)}


def _minutes(v, default):
    try:
        return max(1, min(100000, int(float(v))))
    except (TypeError, ValueError):
        return default


def _r5(x):
    return max(5, int(round(x / 5.0)) * 5)


# ----------------------------------------------------------------------------
# Courses
# ----------------------------------------------------------------------------
def course_fits(title, days) -> bool:
    """False for bootcamps and for courses whose own name claims more time than the whole plan."""
    title = str(title or "")
    if _BOOTCAMP.search(title):
        return False
    for n, unit in _LENGTH.findall(title):
        n, u = int(n), unit.lower()
        if u.startswith(("day", "يوم", "أيام", "ايام")) and n > days:
            return False
        if u.startswith(("week", "أسبوع", "اسبوع")) and n * 7 > days:
            return False
        if u.startswith(("hour", "hr", "ساع")) and n > 15:
            return False
    return True


# ----------------------------------------------------------------------------
# Duplicates
# ----------------------------------------------------------------------------
def _norm_word(w):
    w = w.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا").replace("ة", "ه")
    if len(w) > 3 and w.startswith("و"):
        w = w[1:]
    if len(w) > 4 and w.startswith("ال"):
        w = w[2:]
    return w


def _tokens(t):
    return {_norm_word(w) for w in re.findall(r"[\w\u0600-\u06FF]+", str(t or "").lower()) if w not in _STOP and len(w) > 1}


def _similar(a, b, jac=0.6, contain=0.8) -> bool:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    inter = len(ta & tb)
    return inter / len(ta | tb) >= jac or (min(len(ta), len(tb)) >= 2 and inter / min(len(ta), len(tb)) >= contain)


def find_issues(nodes, expected=NODES_EXPECTED) -> list:
    """Problems the MODEL should repair. Each issue: {"nodes": [1-based indexes], "text": english sentence}."""
    issues = []
    if not isinstance(nodes, list) or not nodes:
        return [{"nodes": [], "text": "No milestones were returned."}]
    nodes = [n for n in nodes if isinstance(n, dict)]
    if len(nodes) != expected:
        issues.append({"nodes": [], "text": f"Exactly {expected} milestones are required but {len(nodes)} were returned."})
    intro_idx = [i for i, n in enumerate(nodes, 1) if _INTRO_RE.search(str(n.get("title") or ""))]
    if len(intro_idx) > 1:
        issues.append({"nodes": intro_idx[1:], "text": f"Milestones {intro_idx} are all introductions/setup. Only the FIRST may be an introduction; every other milestone must teach new skills."})
    for i in range(len(nodes)):
        for j in range(i + 1, len(nodes)):
            if _similar(nodes[i].get("title"), nodes[j].get("title")):
                issues.append({"nodes": [i + 1, j + 1], "text": f"Milestones {i + 1} and {j + 1} cover the same topic ({nodes[i].get('title')!r} / {nodes[j].get('title')!r})."})
                continue
            ci, cj = _tokens(" ".join(map(str, nodes[i].get("coreConcepts") or []))), _tokens(" ".join(map(str, nodes[j].get("coreConcepts") or [])))
            if len(ci) >= 3 and len(cj) >= 3 and len(ci & cj) / len(ci | cj) >= 0.6:
                issues.append({"nodes": [i + 1, j + 1], "text": f"Milestones {i + 1} and {j + 1} teach almost the same core concepts."})
    return issues


def repair_note(issues) -> str:
    lines = "\n".join(f"- {x['text']}" for x in issues)
    return ("\n\nQUALITY CHECK FAILED on your previous answer:\n" + lines +
            "\nReturn the COMPLETE corrected JSON again (same schema). Each milestone must introduce NEW concepts that build on the previous ones; "
            "never repeat an earlier milestone's topic; only the first milestone may be an introduction.")


# ----------------------------------------------------------------------------
# Cleaning
# ----------------------------------------------------------------------------
def _tidy_question(options, idx):
    """Distinct, non-empty options; returns (options, idx) or None when the question is unusable."""
    clean, seen, mapping = [], set(), {}
    for k, o in enumerate(options or []):
        t = str(o).strip()
        key = re.sub(r"\s+", " ", t.lower())
        if not t or key in seen:
            continue
        seen.add(key)
        mapping[k] = len(clean)
        clean.append(t)
    if len(clean) < 3 or idx not in mapping:
        return None
    return clean, mapping[idx]


def _shuffle(options, idx, rng):
    if any(_ALL_ABOVE.search(o) for o in options):
        return options, idx
    order = list(range(len(options)))
    rng.shuffle(order)
    return [options[i] for i in order], order.index(idx)


def _fit_minutes(node, per_node, days, notes):
    resources = [dict(r) for r in (node.get("resources") or []) if isinstance(r, dict)][:5]
    title = str(node.get("title") or "")
    cap_each = max(10, int(per_node * 0.30))
    changed = False
    for r in resources:
        if not course_fits(r.get("title"), days):
            q = f"{title} شرح مبسط"
            r.update({"title": f"ابحث عن شرح: {title}"[:200], "provider": "YouTube", "resourceType": "video", "searchQuery": q,
                      "why": "بحث مباشر عن موضوع المرحلة"})
            notes.append("اتشال كورس طويل ما يناسبش مدة الخطة، وحل محله بحث مناسب للدرس.")
        m = _minutes(r.get("estimatedMinutes"), 15)
        if m > cap_each:
            r["estimatedMinutes"], changed = cap_each, True
        else:
            r["estimatedMinutes"] = m
    while len(resources) > 1 and sum(r["estimatedMinutes"] for r in resources) > per_node * RESOURCE_SHARE:
        resources.pop()
        changed = True
    res_total = sum(r["estimatedMinutes"] for r in resources)

    allowed = max(per_node - res_total, int(per_node * ACTIVITY_SHARE_MIN))
    acts = [dict(a) for a in (node.get("activities") or []) if isinstance(a, dict)][:8]
    for a in acts:
        a["estimatedMinutes"] = _minutes(a.get("estimatedMinutes"), 30)
    total = sum(a["estimatedMinutes"] for a in acts)
    if acts and total > allowed * TOLERANCE:
        while len(acts) > 2 and (sum(a["estimatedMinutes"] for a in acts) > allowed * TOLERANCE or len(acts) * 10 > allowed):
            acts.pop()
        total = sum(a["estimatedMinutes"] for a in acts)
        if total > allowed:
            f = allowed / total
            for a in acts:
                a["estimatedMinutes"] = max(10, _r5(a["estimatedMinutes"] * f))
        changed = True
    planned = sum(a["estimatedMinutes"] for a in acts) + res_total
    node["activities"], node["resources"] = acts, resources
    node["timeBudget"] = {"availableMinutes": per_node, "plannedMinutes": planned, "trimmed": changed or planned > per_node * TOLERANCE}
    if planned > per_node * TOLERANCE:
        node["timeBudget"]["note"] = "الخطة الأدنى أكبر من وقتك المتاح لهذه المرحلة. زوّد وقت المذاكرة أو المدة."
    if changed:
        notes.append(f"اتعدّلت أوقات الأنشطة والمراجع لتناسب وقتك ({planned} من {per_node} دقيقة).")


def clean_nodes(nodes, budget, seed="") -> list:
    out = []
    for ni, raw in enumerate(nodes or [], 1):
        if not isinstance(raw, dict):
            continue
        node = copy.deepcopy(raw)
        notes = []
        _fit_minutes(node, budget["perNodeMinutes"], budget["days"], notes)
        # quiz questions
        questions = []
        for qi, q in enumerate(node.get("quizQuestions") or []):
            if not isinstance(q, dict) or not isinstance(q.get("options"), list):
                continue
            try:
                idx = int(q.get("correctIndex", 0))
            except (TypeError, ValueError):
                idx = 0
            tidy = _tidy_question(q["options"], idx)
            if not tidy:
                continue
            options, idx = _shuffle(*tidy, random.Random(f"{seed}:{ni}:{qi}"))
            questions.append(dict(q, options=options, correctIndex=idx))
        node["quizQuestions"] = questions
        # the milestone challenge (the question that unlocks the next milestone)
        opts = node.get("challengeOptions")
        if isinstance(opts, list):
            try:
                idx = int(node.get("correctIndex", 0))
            except (TypeError, ValueError):
                idx = 0
            tidy = _tidy_question(opts, idx)
            if tidy:
                node["challengeOptions"], node["correctIndex"] = _shuffle(*tidy, random.Random(f"{seed}:{ni}:c"))
            else:
                node.pop("challengeOptions", None)
                node.pop("correctIndex", None)
        node["qualityNotes"] = list(dict.fromkeys(notes))
        out.append(node)
    return out


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------
def improve(res_data, req, prompt, gen_json):
    """res_data: the model's parsed answer.  req: object with duration / daily_minutes / study_days_per_week / goal.
    gen_json(prompt) is called at most ONCE more, only when real problems were found."""
    budget = build_budget(getattr(req, "duration", "شهر"), getattr(req, "daily_minutes", 60), getattr(req, "study_days_per_week", 5))
    report = {"issues": [], "repairedByModel": False, "remainingIssues": [], "budget": budget}
    if not isinstance(res_data, dict):
        return res_data, report
    issues = find_issues(res_data.get("nodes"))
    report["issues"] = [x["text"] for x in issues]
    if issues:
        try:
            second = gen_json(prompt + repair_note(issues))
            if isinstance(second, dict) and isinstance(second.get("nodes"), list) and second["nodes"]:
                again = find_issues(second["nodes"])
                if len(again) < len(issues):
                    res_data, issues, report["repairedByModel"] = second, again, True
        except Exception as exc:  # the first answer is still usable
            report["repairError"] = str(exc)[:200]
    report["remainingIssues"] = [x["text"] for x in issues]
    seed = str(getattr(req, "goal", "")) + str(getattr(req, "user_id", ""))
    nodes = clean_nodes(res_data.get("nodes"), budget, seed)
    for issue in issues:                                      # whatever the model could not fix is shown to the student
        for n in issue["nodes"]:
            if 1 <= n <= len(nodes):
                note = "الذكاء الاصطناعي كرّر موضوع مرحلة سابقة هنا. اطلب مسار جديد لو حابب."
                nodes[n - 1]["qualityNotes"] = list(dict.fromkeys(nodes[n - 1].get("qualityNotes", []) + [note]))
    res_data["nodes"] = nodes
    res_data["qualityReport"] = report
    return res_data, report
