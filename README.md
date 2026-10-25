# Axiom Learn — New AI learning platform

Separate project, not connected to S-TESSERACT or its database.

## Features in this starter
- Interactive 3D-inspired dark UI with Three.js decorative particle field (CDN, internet required)
- Email/password registration and login
- Guest mode with generated Player-style name and editable saved display name
- Create exams from a topic and pasted notes
- AI question generation and AI-assisted grading through an OpenAI-compatible Chat Completions API
- Demo fallback clearly labeled when no API key is configured
- Export exam and answer key as separate PDF files
- SQLite database `axiom_learn.db` created only inside this project folder

## Windows PowerShell quick start
```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
# Edit .env and set a long random SECRET_KEY; set AI_API_KEY to enable API calls.
uvicorn app.main:app --reload
```
Open http://127.0.0.1:8000

## Important
This is a runnable foundation, not a production-hardened service. Before public deployment, add HTTPS-only cookies, rate limiting, password reset/email verification, migrations, backups, and security review. AI-generated questions and grading require human review before high-stakes use. The no-key fallback is demo behavior, not AI.
