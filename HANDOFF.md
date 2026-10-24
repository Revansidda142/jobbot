# jobbot handoff (living document; update as work continues)

Owner: Revan. Python 3.14 venv (`.venv`), WSL. Read this first, then README.md.

## What it is
Job-application bot: collects jobs (LinkedIn, Naukri) with Playwright, scores them with an LLM (`cursor-agent`),
optionally tailors a LaTeX resume (tectonic), applies. Default mode is dry-run.

## Architecture
- `main.py` CLI/run loop, filters, limits. `common.py` profiles/config merge. `db.py` SQLite + CSV logs.
- `llm_client.py` only LLM module (`cursor-agent -p --trust --mode ask --output-format text`, prompt on stdin).
- `tailor.py` per-job resume tailoring -> `output/<profile>/<platform>_<company>_<id>/`.
- `sites/linkedin.py` (new `/jobs/search-results/` layout, `geoId` pins location), `sites/naukri.py`.
- `ui.py` read-only local web UI (`python main.py --ui`, 127.0.0.1:8765).
- Profiles: `resume/<name>/{resume.tex,resume.cls,profile.yaml,target_jd.md}`; profile.yaml deep-merges over config.yaml
  (keys: search, filters, scoring, answers, resume, limits).
- Profiles now: `sre_devops` (Revansidda R, LinkedIn), `shradha_profile_technical_java` (Shradha, Naukri, 0-3 yrs), `default`.
- DB `data/jobs.db` (live) / `data/jobs_dryrun.db`; PK (platform, job_id, profile). Statuses: applied, already_applied,
  unconfirmed, external, no_apply_button (these block ALL profiles), needs_answers, error, skipped_filter, skipped_score, dry_run_ok.
- Logs: `data/applications.csv` (live, append-only), `applications_dryrun.csv`.
- `run.sh` sends output to `logs/cron.log` (prints nothing). For manual runs use `.venv/bin/python main.py ...`.

## Commands
    .venv/bin/python -m unittest                         # 88 tests, no browser, never submits
    .venv/bin/python main.py --llm-check
    .venv/bin/python main.py --check-profile NAME
    .venv/bin/python main.py --profile NAME --platforms naukri --mode dry-run --force
    .venv/bin/python main.py --profile NAME --platforms naukri --mode live --force
    .venv/bin/python main.py --history --profile NAME --days 1 [--mode live]

## Key behaviours / facts
- Questionnaires: in an interactive terminal the bot prompts (Enter = answered, `s` = skip); it also advances by itself when the page shows 'Applied'; `naukri.questionnaire_wait_seconds` (default 120) is the max wait. Without a terminal (cron) it only polls for the label. Then the verify pass re-checks `needs_answers`. (The 1-job cap was exceeded in the 01:38 test only because the user answered one manually: not a bug.)
- Verify pass: at the end of each live run, uncertain rows (unconfirmed/needs_answers) are re-opened (`Adapter.verify`,
  Naukri: 'Applied' label -> applied, Apply button -> not_applied, else unknown). `main.py --profile NAME --verify`
  does it for old rows. Promotion keeps the original `updated` timestamp (so daily caps are not inflated).
- Naukri applies with the resume saved in the Naukri account: NO per-job tailoring (`supports_resume_upload=False`).
  Tailored PDFs exist only for LinkedIn.
- One browser profile (`browser_profile/`) is shared by all jobbot profiles; the logged-in site account is not tied to a
  profile. Swap folders (`browser_profile_revan/`, `browser_profile_shradha/`) to switch accounts.
- Naukri apply: after click, waits ~8s for "Applied" label -> `applied`; visible recruiter questionnaire ->
  `needs_answers`; otherwise `unconfirmed`. `unconfirmed` counts toward run/day caps.
- Speed: `filters.max_experience_years` skips before LLM; `limits.max_jobs_per_run` caps scoring. Each scored job = 1 LLM call (~15s).

## History (condensed)
1. Backend swapped from `claude -p` to cursor-agent. 2. Multi-profile support, per-job output folders, DB migration.
3. LinkedIn location + layout fixes. 4. Read-only UI. 5. Speed-ups. 6. Live Naukri run for Shradha misrecorded results
   (said unconfirmed/needs_answers though pages showed "Applied"); fixed detection, corrected 6 rows
   (backup `data/jobs.db.bak-20261011-004245`) and appended them to applications.csv.
7. Dry-run on Naukri after fixes OK; unit suite 88 OK.

## Current state
- Shradha `min_score` 55, `limits.max_applications_per_run` 5.
- Live 1-job test (01:04): Infosys FSD `070826009036` -> `needs_answers`; page re-probed afterwards still shows Apply and no 'Applied' label, so nothing was submitted: a real recruiter questionnaire (correct classification, not a false positive). Infosys postings often have questionnaires; apply by hand.
- Still unverified live: the `applied` path (needs a job with no questionnaire). Candidate to retry: `290726009237` (unconfirmed, still showed Apply) -> needs a DB reset to retry.
- User must confirm the logged-in Naukri account is Shradha's (check "Applied jobs").

- Shradha test settings restored (max_age_hours 72, max_applications_per_run 5).

## Open / optional ideas (not built)
- Once-per-company-and-title rule (Epam/Infosys repost per city -> duplicate applies).
- Per-profile browser folders (`browser_profile/<profile>/`, `login.py --profile`).
- `max_experience_years` filter for `sre_devops`. Reuse of tailored resume ignores master edits (use `--retailor`).
- Push to a PRIVATE GitHub repo (Revansidda142/jobbot); never commit data/, browser profiles, jobbot.zip.
