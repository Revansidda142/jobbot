# jobbot

Setup (inside WSL, in ~/jobbot, with the venv active):

    pip install -r requirements.txt
    playwright install --with-deps chromium

LLM backend (Cursor CLI, used for scoring and resume tailoring):

    curl https://cursor.com/install -fsS | bash
    cursor-agent login
    cursor-agent status                  # confirm you are logged in
    python main.py --llm-check           # tiny real call; prints provider, exact command, raw reply, parse result

## Layout

    config.yaml                  shared settings: mode, active_hours, platforms, llm, compile_cmd, default limits
    resume/<profile>/
        resume.tex, resume.cls (+ any .sty)   the master resume of this profile
        profile.yaml                          job targeting, deep-merged over config.yaml
        target_jd.md                          optional: the ideal role, in plain words
    output/<profile>/<platform>_<company-slug>_<job_id>/
        resume.tex, resume.cls, <output_name>.pdf, job.txt, meta.json, changes.diff
        (FAILED.txt if tailoring failed twice; resume.prev.tex after --retailor)
    output/<profile>/_master/    cached PDF of the untouched master (fallback)
    data/jobs.db                 live applications (source of truth)      data/jobs_dryrun.db   dry-run
    data/applications.csv        append-only log of submitted applications (you fill in notes/outcome by hand)
    data/applications_dryrun.csv same, for dry-run (status dry_run_ok)
    logs/                        run reports and logs

`profile.yaml` may override: `search`, `filters`, `scoring`, `answers`, `resume`
(`output_name`, `protected_strings`, `on_tailor_failure`) and `limits`. Dicts merge key by key; lists replace.
`target_jd.md` is sent (inside `<profile_target>` tags) in both the scoring and the tailoring prompt.

## Commands

    python main.py --list-profiles                 valid profile names (flags profiles missing resume.tex)
    python main.py --new-profile NAME              scaffold resume/NAME/ (refuses if it exists)
    python main.py --check-profile NAME            compile the resume, check protected strings, show merged settings
    python main.py --profile NAME --force          dry-run: nothing is submitted (--profile optional if only one exists)
    python main.py --profile NAME --force --mode live    real applications
    python main.py --profile NAME --retailor       regenerate tailored resumes even if output/ already has them
    python main.py --history [--profile NAME] [--days N] [--mode live]   table from the DB (dry-run DB unless --mode live)
    python main.py --profile NAME --tailor-test jd.txt   score + tailor a pasted job description, no browser
    python main.py --profile NAME --verify         re-open unconfirmed/needs_answers jobs (jobs.db); mark the ones that went through as applied
    python main.py --llm-check                     test the LLM backend
    python login.py                                log in to LinkedIn + Naukri once, by hand
    python -m unittest                             tests (temp JOBBOT_HOME, fake cursor-agent, no browser)

`run.sh` passes all arguments through, e.g. `./run.sh --profile sales_coordinator`.
Set `JOBBOT_HOME=/some/dir` to use another project root (config.yaml, resume/, data/, output/, browser_profile/).

## Making runs faster

- `filters.max_experience_years: 3` (profile.yaml): skips jobs whose Naukri URL or title asks for more years
  (e.g. `...-4-to-7-years-...`, `7+ YoE`) before any page load or LLM call. Jobs without that info are not skipped.
- `limits.max_jobs_per_run: 30` (profile.yaml or config.yaml): stop after looking at (scoring) that many jobs, newest
  first. Unprocessed jobs are picked up by the next run. `max_applications_per_run` only counts successful applies.
- `llm.score_model: <name>` (config.yaml, optional): use another model for scoring only. A tiny call takes ~13-16s on
  any model (mostly cursor-agent startup), so the gain is small.
- Fewer `search.keywords` and `search.pages: 1` mean fewer jobs collected.

## UI (read-only)

    python main.py --ui [--port 8765]      then open http://127.0.0.1:8765

Jobs page: Applied / Ready (dry-run OK) / You need to apply by hand (external, no Apply button, needs answers,
unconfirmed, each with a link to the job) / Skipped / Errors, filterable by profile, platform and days; switch between
the dry-run and live database at the top. Click a job for its description, `meta.json`, the resume diff and the tailored
PDF. Profiles page: every profile with its target JD, merged settings, screening answers, `profile.yaml` and master resume.
Config page: the shared `config.yaml`. The UI only listens on 127.0.0.1, only reads (never writes, never starts the bot)
and escapes all job-site text. Stop it with Ctrl+C.

## Behaviour notes

- Dry-run is the default mode. Jobs are processed newest first across all platforms.
- Tailored resumes are reused: if `output/<profile>/<job folder>/` has a valid 1-page PDF for the same profile + job,
  no LLM call is made. After editing your master resume, run with `--retailor`.
- Double-apply protection: a job with outcome applied / already_applied / unconfirmed / external / no_apply_button
  is never touched again by ANY profile. skipped_*, needs_answers, error and dry_run_ok only affect their own profile.
- `applications.csv` is only ever appended to (header written once). Rows are added only for status `applied`
  (dry-run: `dry_run_ok` rows go to `applications_dryrun.csv`). Columns: date_time, profile, platform, company,
  title, location, job_url, posted_text, score, status, tailored, resume_pdf, job_folder, notes, outcome.
- Naukri: after clicking Apply the bot waits up to ~8s for the page to show "Applied" (then status `applied`).
  `unconfirmed` = clicked but no label seen (check the job on Naukri); `needs_answers` = a visible recruiter
  questionnaire. `unconfirmed` counts toward `max_applications_per_run` and `max_applications_per_day`.
  At the end of every live run the bot re-opens each `unconfirmed`/`needs_answers` job of that run; if the page now shows
  "Applied" the row becomes `applied` (and goes into applications.csv). `--verify` does the same for older rows.
  If a questionnaire pops up (live run, interactive terminal) the bot prints a prompt: answer it in the browser window,
  then press Enter in the terminal (or `s` + Enter to skip). It then re-checks for "Applied" and moves on at once; it
  also moves on by itself if the page shows "Applied". `naukri.questionnaire_wait_seconds` (config.yaml, default 120,
  0 = never wait) is only the longest it waits for you. Under cron / `run.sh` (no terminal) it cannot prompt and just
  polls that long for the label.
  Rows whose page still shows Apply stay as they are: apply to those by hand.
- Old databases are migrated automatically on open (table rebuilt; existing rows get profile `default`).

## LinkedIn shows the wrong location (e.g. Bidar instead of Bengaluru)

LinkedIn sometimes ignores the text `search.location` and uses a location guessed from your IP/account.
1. Try a fuller text in profile.yaml: `location: "Bengaluru, Karnataka, India"`.
2. If it still shows the wrong place, open linkedin.com/jobs in the bot browser (`python login.py`), pick the
   location from the dropdown by hand, copy the number after `geoId=` from the address bar, and set
   `search.geo_id: <number>` in profile.yaml. The bot then adds `&geoId=` to every search URL.

## LLM config (config.yaml)

    llm:
      provider: cursor        # cursor | claude
      cmd: cursor-agent
      model: null             # names from `cursor-agent models`; null = Cursor default
      timeout_seconds: 300

The cursor provider runs `cursor-agent -p --trust --mode ask --output-format text` (read-only, prompt on stdin) in an
empty temporary directory, so the model cannot see your files. All LLM calls live in `llm_client.py`.

## Create a second profile (e.g. sales_coordinator)

    python main.py --new-profile sales_coordinator
    cp /path/to/your/resume.tex resume/sales_coordinator/resume.tex     # copy .sty files too if you have any
    $EDITOR resume/sales_coordinator/profile.yaml                       # keywords, location, filters, min_score,
                                                                        # output_name, protected_strings, answers
    $EDITOR resume/sales_coordinator/target_jd.md                       # the role you are aiming for
    python main.py --check-profile sales_coordinator                    # must say Result: OK
    python main.py --profile sales_coordinator --force                  # dry-run first
