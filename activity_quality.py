"""
Evidence helpers for the Learning Studio (S-TESSERACT).

Why this exists
  * AI-written test cases can be wrong. So before a student ever sees them, the AI's REFERENCE SOLUTION is executed by the
    isolated runner on every test, and only the tests whose expected output the reference really produces are kept.
  * A score should come from running the student's code, not from a model's impression of it.
  * A model can state a score that disagrees with its own rubric. The score is recomputed from the rubric points.
  * Quiz options are de-duplicated and shuffled with a stable seed (models love putting the right answer first).

Nothing here runs code itself: it receives an `execute(language, code, stdin) -> dict` callable (the Piston-compatible
runner) so the module is easy to test and cannot become an exec/eval hole.
"""
import math
import random
import re
from concurrent.futures import ThreadPoolExecutor

MAX_TESTS = 10
VISIBLE_TESTS = 3
MIN_VERIFIED = 3
_ALL_ABOVE = re.compile(r"(كل ما سبق|جميع ما سبق|جميع الإجابات|كل الإجابات|لا شيء مما سبق|لا شيء مما ذكر|all of the above|none of the above|\bboth\b)", re.I)


# ----------------------------------------------------------------------------
# Output comparison
# ----------------------------------------------------------------------------
def _lines(s):
    text = str(s if s is not None else "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    while lines and not lines[0]:
        lines.pop(0)
    return lines


def _token_equal(a, b):
    if a == b:
        return True
    try:
        x, y = float(a), float(b)
    except ValueError:
        return False
    return math.isfinite(x) and math.isfinite(y) and abs(x - y) <= 1e-6 * max(1.0, abs(x), abs(y))


def outputs_match(actual, expected) -> bool:
    """Line by line; spacing inside a line and trailing blank lines do not matter; numbers match within 1e-6."""
    a, e = _lines(actual), _lines(expected)
    if len(a) != len(e):
        return False
    for la, le in zip(a, e):
        ta, te = la.split(" "), le.split(" ")
        if len(ta) != len(te) or not all(_token_equal(x, y) for x, y in zip(ta, te)):
            return False
    return True


# ----------------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------------
def parse_tests(raw, limit=MAX_TESTS) -> list:
    out = []
    for t in (raw or [])[:limit] if isinstance(raw, list) else []:
        if not isinstance(t, dict):
            continue
        exp = t.get("expected")
        if exp is None or str(exp).strip() == "":
            continue
        out.append({"input": str(t.get("input") if t.get("input") is not None else "")[:2000], "expected": str(exp)[:2000],
                    "why": str(t.get("why") or "")[:300]})
    return out


def _run_one(execute, language, code, stdin):
    """-> dict(ok, stdout, error, infra) ; infra=True means the RUNNER failed (not the code)."""
    try:
        r = execute(language, code, stdin)
    except Exception as exc:
        return {"ok": False, "stdout": "", "error": f"runner: {str(exc)[:200]}", "infra": True}
    err = ""
    if r.get("status") != "success":
        err = (str(r.get("compile_stderr") or "") + " " + str(r.get("stderr") or "")).strip()[:400] or f"exit code {r.get('exit_code')}"
        if r.get("signal"):
            err = f"{r.get('signal')} (time/memory limit?) " + err
    return {"ok": not err, "stdout": str(r.get("stdout") or ""), "error": err, "infra": False}


def _run_many(execute, language, code, tests, workers=3):
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda t: _run_one(execute, language, code, t["input"]), tests))


def verify_tests(execute, reference, tests, language="python") -> dict:
    """Run the AI's reference solution on every AI-written test; keep only tests the reference reproduces."""
    if not reference or not tests:
        return {"status": "no_reference", "kept": [], "dropped": [], "total": len(tests)}
    runs = _run_many(execute, language, reference, tests)
    if runs and all(r["infra"] for r in runs):
        return {"status": "runner_unavailable", "kept": [], "dropped": [], "total": len(tests), "detail": runs[0]["error"]}
    kept, dropped = [], []
    for i, (t, r) in enumerate(zip(tests, runs), 1):
        if r["infra"]:
            dropped.append({"n": i, "reason": "runner error"})
        elif not r["ok"]:
            dropped.append({"n": i, "reason": "reference solution crashed: " + r["error"][:120]})
        elif outputs_match(r["stdout"], t["expected"]):
            kept.append(t)
        else:
            dropped.append({"n": i, "reason": f"reference printed {r['stdout'].strip()[:80]!r} but expected {t['expected'].strip()[:80]!r}"})
    need = max(MIN_VERIFIED, math.ceil(0.6 * len(tests)))
    return {"status": "verified" if len(kept) >= need else "failed", "kept": kept, "dropped": dropped, "total": len(tests)}


def retry_note(verification) -> str:
    lines = "\n".join(f"- test {d['n']}: {d['reason']}" for d in verification.get("dropped", [])[:6])
    return ("\n\nVERIFICATION FAILED: your reference solution was executed on your test cases and disagreed with your expected outputs:\n" + lines +
            "\nReturn the COMPLETE corrected JSON again. Make the referenceSolution a full program that reads stdin and prints stdout, "
            "and make every `expected` EXACTLY what that program prints for that `input`. Keep the inputs small.")


def run_student_tests(execute, code, tests, language="python") -> list:
    """Grade by running. -> list of dict(passed, actual, error, infra) aligned with tests."""
    out = []
    for t, r in zip(tests, _run_many(execute, language, code, tests)):
        passed = r["ok"] and outputs_match(r["stdout"], t["expected"])
        out.append({"passed": bool(passed), "actual": r["stdout"][:600], "error": r["error"], "infra": r["infra"]})
    return out


# ----------------------------------------------------------------------------
# Quiz questions
# ----------------------------------------------------------------------------
def clean_check_questions(questions, seed="") -> list:
    out = []
    for qi, q in enumerate(questions if isinstance(questions, list) else []):
        if not isinstance(q, dict) or not isinstance(q.get("options"), list):
            continue
        try:
            idx = int(q.get("correctIndex", 0))
        except (TypeError, ValueError):
            idx = 0
        options, mapping, seen = [], {}, set()
        for k, o in enumerate(q["options"]):
            text = str(o).strip()
            key = re.sub(r"\s+", " ", text.lower())
            if text and key not in seen:
                seen.add(key)
                mapping[k] = len(options)
                options.append(text)
        if len(options) < 3 or idx not in mapping:
            continue
        idx = mapping[idx]
        if not any(_ALL_ABOVE.search(o) for o in options):
            order = list(range(len(options)))
            random.Random(f"{seed}:{qi}").shuffle(order)
            options, idx = [options[i] for i in order], order.index(idx)
        out.append(dict(q, options=options, correctIndex=idx))
    return out


# ----------------------------------------------------------------------------
# Scores
# ----------------------------------------------------------------------------
def _num(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def reconcile_score(feedback, tests=None) -> dict:
    """tests: None, or {"score": 0-100, "passed": n, "total": n} computed by the server by RUNNING the student's code.
    Returns a dict whose `score` can be trusted as far as `scoreSource` says."""
    fb = dict(feedback) if isinstance(feedback, dict) else {"overallAssessment": str(feedback or "")[:3000]}
    stated = _num(fb.get("score"))
    if tests:
        fb["aiScore"] = None if stated is None else int(max(0, min(100, round(stated))))
        fb["score"] = int(tests["score"])
        fb["scoreSource"] = "tests"
        fb["scoreRationale"] = f"محسوبة من تشغيل كودك فعليًا على السيرفر: نجح {tests['passed']} من {tests['total']} حالة اختبار."
        return fb
    earned = possible = 0.0
    for row in fb.get("rubric") if isinstance(fb.get("rubric"), list) else []:
        if not isinstance(row, dict):
            continue
        p, e = _num(row.get("pointsPossible")), _num(row.get("pointsEarned"))
        if p and p > 0 and e is not None:
            possible += p
            earned += max(0.0, min(e, p))
    if possible > 0:
        computed = int(round(100 * earned / possible))
        if stated is None or abs(stated - computed) > 10:
            fb["aiScore"] = None if stated is None else int(max(0, min(100, round(stated))))
            fb["score"], fb["scoreAdjusted"] = computed, True
            fb["scoreRationale"] = (str(fb.get("scoreRationale") or "") + " (الدرجة اتعدّلت لتطابق مجموع نقاط المعايير.)").strip()
        else:
            fb["score"] = int(max(0, min(100, round(stated))))
        fb["scoreSource"] = "rubric"
    else:
        fb["score"] = None if stated is None else int(max(0, min(100, round(stated))))
        fb["scoreSource"] = "ai_estimate"
        fb.setdefault("scoreRationale", "تقدير من الذكاء الاصطناعي بدون معايير قابلة للحساب.")
    return fb
