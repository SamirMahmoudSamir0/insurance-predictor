# S-TESSERACT — Learning Studio update

This source package preserves the existing S-TESSERACT app and adds a cross-discipline Learning Studio. It does not include production databases, uploaded files, generated videos, API keys, or `.env` secrets.

## Added in this update

- **Learning Studio tab** for programming, mathematics, physics/science, chemistry, engineering, languages, biology/medicine for education, law/economics, history/geography, business, arts/design, and other topics.
- **AI activity designer** that creates a specific objective, prerequisites, step-by-step activity, expected evidence/output, questions, rubric, common mistakes, review plan, resource search queries, and coding test cases when appropriate.
- **Programming editor and compiler/runner** with Python, C, C++, JavaScript and Java selection, stdin, output/error display, and user-supplied JSON test cases. When a roadmap activity is opened, its programming language is inferred from the selected learning goal and applied to both the editor and AI-generated activity instructions.
- **Isolated code execution** delegated to a Piston-compatible runner; the web server does not execute submitted code using local `exec`, `eval`, or shell commands. By default the code is sent to the public Piston endpoint configured in `learning_studio.py`; set `CODE_RUNNER_URL` to a trusted isolated service to change it. Do not submit secrets or proprietary code.
- **AI formative feedback with a provisional 0–100 score**, score rationale, strengths, issue severity, hints, next steps and rubric evidence. The latest actual run or automated-test results are included as evidence for code feedback; unexecuted code is never described as tested. The score is guidance, not a guaranteed authoritative grade.
- **Project-connected roadmaps**: roadmap generation connects each milestone's concepts to a growing project or discipline-appropriate artifact, with implementation tasks, deliverables, test/debug checkpoints and observable success criteria.
- **Saved work and Learning Studio history** stored per authenticated user in an additive SQLite table. Generated roadmaps' activities can be opened directly in the Studio, carry their roadmap node ID into saved submissions, and retain the selected language.
- **History integration** shows saved Learning Studio work alongside roadmaps, exams, videos and other history.
- **Electronic exams** alongside the existing printable exam paper, answer sheet, and model-answer PDF pages. Each generated exam has a timed online attempt, responsive question UI, automatic marking for multiple-choice and true/false questions, persistent per-user attempt history, and a score summary. Written/essay/problem answers are stored and their marks are clearly reported as pending review rather than being assigned an invented score.

## Deployment safety

1. Back up the current source, SQLite database, user uploads and generated videos.
2. Replace the matching source files from this package while retaining the production database and environment variables.
3. Keep `learning_studio.py` beside `main.py`; it is a required new module.
4. Install `requirements.txt` in the target environment and restart the app. No new Python package is required by this module.
5. Verify login, dashboard, roadmap, activity completion, quizzes, exams, Learning Studio, code execution, and History on the actual host.

Database migration is additive: it creates `learning_lab_submissions` and an index; it does not intentionally delete existing tables or user data. Always keep a backup before first startup.

## Current limits

- The code editor is a lightweight browser editor (monospace text area), not Monaco/VS Code-level syntax highlighting or language-server tooling.
- The online exam timer is currently enforced in the browser UI; this is intended for self-study, not secure proctored/high-stakes exams. Objective questions are auto-graded; written answers are saved but not automatically awarded marks in this update.
- Code execution depends on availability and policies of the configured Piston-compatible service. The web app cannot guarantee the external runner's uptime.
- AI-created learning content can be wrong and needs appropriate verification, especially for high-stakes topics. The activity designer does not by itself replace domain-specific simulation software, human grading, professional advice, or trusted source verification.
- This package has been checked for Python syntax and embedded JavaScript syntax. The external runner, live Gemini generation, existing production database, and hosting environment must still be tested on deployment.
