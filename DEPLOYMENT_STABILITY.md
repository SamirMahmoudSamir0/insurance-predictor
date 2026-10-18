# S-TESSERACT Roadmap update — deployment safety

This package contains source code only. It intentionally excludes the production SQLite database, user uploads, generated videos, API keys, and actual `.env` files.

## Important

- Do not overwrite or delete the production database or persistent disk when deploying this source.
- Back up source + database + uploads + generated videos before deployment.
- Keep `STESSERACT_DB_FILE`, `GENERATED_VIDEO_DIR`, `STUDY_UPLOAD_DIR`, and the existing environment secrets unchanged unless you intentionally migrate them.
- The new daily assessment state is stored additively in the existing `blueprint_nodes.learning_plan` JSON and the latest roadmap-history snapshot. No new database table is required for this feature.
- The roadmap generator replaces the active roadmap rows for the user as the previous implementation already did; older roadmap history rows remain. Do not run a new generation on an account whose active roadmap must be retained unless you have backed it up.
- Run `python scripts/predeploy_check.py` before upload. Run `python scripts/smoke_test.py` in an environment with dependencies installed. Test AI generation, daily quiz pass/fail, professional simulation grading, and mobile/desktop rendering on staging before production deployment.
- This package has not been deployed to or tested against the live hosting service from this environment. Do not treat local checks as a production smoke test.
