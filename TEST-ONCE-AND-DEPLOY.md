# S-TESSERACT — Integrated Test Build

This package consolidates the current integrated Roadmap, daily mastery checks, professional-work simulation, code editor/compiler integration, exam section, account/session code, and mobile stale-overlay recovery into one source package for a single round of user testing.

## Data safety
- This ZIP intentionally **does not contain** `stesseract_core.db`, `.env`, uploaded study files, generated videos, or other user data.
- Keep the existing production database and persistent storage exactly where they are. Do not delete or replace them during deployment.
- Keep API keys and admin credentials in the host's environment/secrets settings; never put them in source files.
- This source package cannot itself update the user's hosted site. It must be deployed to the existing app by the owner.

## Before deploying
1. Keep the current live version and persistent database untouched until the new build is checked.
2. Install dependencies from `requirements.txt` in the hosting environment.
3. Run `python scripts/predeploy_check.py`.
4. Run `python scripts/smoke_test.py` in an environment with the listed dependencies and configured environment variables.
5. Confirm the host points `STESSERACT_DB_FILE`, uploads, and generated video directories to persistent storage.

## One complete test pass
Use a test account first and verify:
- Login, permanent user ID, session persistence after refresh, logout, and existing account data.
- Home and every navigation item on desktop and mobile; open/close the mobile sidebar and confirm taps still work after refreshing and returning to the page.
- Roadmap creation with goal, current level, time budget, and daily availability.
- Daily lesson time totals, resource links/searches, daily quiz, hints, feedback, saved result, and next-day unlock rules.
- Professional-work simulation submission, AI feedback, rubric, milestone progress, and final readiness assessment.
- Roadmap history and saved learning work.
- Programming activity opens the relevant language editor, runs supported code/tests, and reports errors honestly.
- Exams: source material upload/selection, paper and electronic mode, answer explanations, separate exam and answer-key PDF exports.
- Existing video/AI Center, calendar, reminders, leaderboard, account and admin features that are enabled in the current environment.
- Restart the app and verify database-backed records remain present.

## Known verification boundary
Static Python/JavaScript syntax checks pass. In this build, an isolated smoke test with a temporary SDK stub confirmed app import and 88 registered routes; the homepage returned HTTP 200 and protected history/exam routes correctly required login. AI provider calls and a real graphical mobile browser session still need verification in the actual hosting environment with valid provider credentials.
