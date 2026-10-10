"""Job bot runner: collect -> filter -> newest first -> score -> tailor -> apply -> report."""
import argparse
import contextlib
import re
import shutil
import sys
from datetime import datetime

from common import (BlockedError, Job, ProfileError, human_pause, list_profiles, load_config, load_profile_config,
                    log, profile_dir, profile_master_tex, resolve_profile, root, setup_logging)
from db import DB, append_application_csv
from llm_client import LLMError, build_command, llm_settings, parse_score, run_llm, score_job
from tailor import job_folder, master_pdf, tailor_resume


def passes_filters(job, cfg):
    f = cfg["filters"]
    t = job.title.lower()
    if f.get("title_include_any") and not any(k.lower() in t for k in f["title_include_any"]):
        return False, "title does not contain any include-keyword"
    if any(k.lower() in t for k in f.get("title_exclude", [])):
        return False, "title contains an excluded word"
    if any(b.lower() in job.company.lower() for b in f.get("company_blacklist", []) if b):
        return False, "company is blacklisted"
    return True, ""


def get_adapters(cfg):
    from sites.linkedin import LinkedIn
    from sites.naukri import Naukri
    table = {"linkedin": LinkedIn, "naukri": Naukri}
    return {n: table[n]() for n in cfg["platforms"] if n in table}


def write_report(rows, mode, started, profile):
    path = root() / "logs" / f"run_{started:%Y%m%d_%H%M%S}_{profile}.md"
    path.parent.mkdir(exist_ok=True)
    lines = [f"# Run {started:%Y-%m-%d %H:%M} ({mode}, profile {profile})", "",
             "| Posted | Platform | Company | Title | Score | Result | Detail |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append("| {posted} | {platform} | {company} | {title} | {score} | {status} | {detail} |".format(
            **{k: str(v).replace("|", "/") for k, v in r.items()}))
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def tailor_test(cfg, jd_file):
    """python main.py --tailor-test jd.txt [--profile P] -> score + tailor against a pasted job description (no browser)."""
    desc = open(jd_file, encoding="utf-8").read()
    job = Job("test", "jd", "Test role", "TestCo", "Bengaluru", "", "", 0, desc)
    master_tex = profile_master_tex(cfg).read_text(encoding="utf-8")
    print("Profile:", cfg["profile"])
    print("Master resume compiles:", master_pdf(cfg, force=True))
    score, reason = score_job(cfg, master_tex, job)
    print("Score:", (score, reason))
    pdf = tailor_resume(cfg, job, retailor=True, score=score, reason=reason)
    print("Tailored PDF:", pdf if pdf else f"FAILED - see {job_folder(cfg, job)}/FAILED.txt")


# ---------------- profile commands ----------------
PROFILE_TEMPLATE = """# Profile '{name}'. Deep-merged over config.yaml. Allowed keys: search, filters, scoring, answers, resume, limits.
search:
  keywords: []              # e.g. ["sales coordinator", "inside sales"]  (one search per keyword)
  location: ""              # e.g. Bengaluru
  max_age_hours: 24
  pages: 2
filters:
  title_include_any: []     # e.g. [sales, coordinator]   (empty = no include filter)
  title_exclude: []         # e.g. [intern, director]
  company_blacklist: []
scoring:
  min_score: 70
  notes: ""                 # e.g. "Prefer junior roles; no field sales"
answers: []                 # LinkedIn screening answers, e.g. - {{match: "notice period", value: "30 days"}}
resume:
  output_name: Your_Name_Resume.pdf   # file name recruiters see
  on_tailor_failure: use_master       # use_master | skip
  protected_strings: []     # text that must survive tailoring unchanged, e.g. name, email, phone, employers
# limits:                   # optional overrides of config.yaml
#   max_applications_per_run: 10
"""

TARGET_TEMPLATE = """# Target role for profile '{name}'
Describe the ideal role in plain words: job titles, seniority, industry, location, what to avoid.
The scoring and tailoring prompts include this text as "what this profile is aiming for".
"""


def list_profiles_cmd():
    names = list_profiles()
    if not names:
        print("No profiles. Create one with: python main.py --new-profile <name>")
    for n in names:
        print(n + ("" if (profile_dir(n) / "resume.tex").exists() else "   (MISSING resume.tex)"))
    return 0


def new_profile(name):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise ProfileError("profile name may only contain letters, digits, '_' and '-'")
    pdir = profile_dir(name)
    if pdir.exists():
        raise ProfileError(f"profile {name!r} already exists: {pdir}")
    pdir.mkdir(parents=True)
    (pdir / "profile.yaml").write_text(PROFILE_TEMPLATE.format(name=name), encoding="utf-8")
    (pdir / "target_jd.md").write_text(TARGET_TEMPLATE.format(name=name), encoding="utf-8")
    src = profile_dir("default")
    for f in (list(src.glob("*.cls")) + list(src.glob("*.sty"))) if src.is_dir() else []:
        shutil.copy(f, pdir / f.name)
    print(f"Created {pdir}/ with profile.yaml and target_jd.md (class files copied from 'default').")
    print(f"Next: put your LaTeX resume at {pdir}/resume.tex, edit profile.yaml, then run:")
    print(f"  python main.py --check-profile {name}")
    return 0


def check_profile(name):
    cfg = load_profile_config(name)
    tex_path = profile_master_tex(cfg)
    if not tex_path.exists():
        raise ProfileError(f"{tex_path} is missing")
    ok = True
    try:
        print("Compiles OK:", master_pdf(cfg, force=True))
    except RuntimeError as e:
        ok = False
        print("COMPILE FAILED:", e)
    tex = tex_path.read_text(encoding="utf-8")
    protected = cfg["resume"].get("protected_strings") or []
    missing = [p for p in protected if p and p not in tex]
    for p in missing:
        print(f"MISSING protected string in resume.tex: {p!r}")
    ok = ok and not missing
    print(f"Protected strings: {len(protected) - len(missing)}/{len(protected)} found")
    s = cfg["search"]
    print(f"Profile: {name}\n  keywords: {s.get('keywords')}\n  location: {s.get('location')}\n"
          f"  min_score: {cfg['scoring']['min_score']}\n  answers: {len(cfg.get('answers') or [])}\n"
          f"  target_jd.md: {'yes' if cfg['target_jd'] else 'no'}")
    if not s.get("keywords"):
        print("WARNING: search.keywords is empty - the bot cannot run with this profile yet.")
    print("Result:", "OK" if ok else "PROBLEMS FOUND")
    return 0 if ok else 1


def history_cmd(cfg, args, mode):
    dbfile = root() / "data" / ("jobs.db" if mode == "live" else "jobs_dryrun.db")
    if not dbfile.exists():
        print(f"No history yet ({dbfile.name}).")
        return 0
    rows = DB(dbfile).history(args.profile, args.days)
    print(f"{dbfile.name}: {len(rows)} rows" + (f", profile {args.profile}" if args.profile else "")
          + (f", last {args.days} days" if args.days else "") + "  (use --mode live for jobs.db)")
    fmt = "{:<16} {:<12} {:<9} {:<20} {:<28} {:>5} {:<22} {}"
    print(fmt.format("updated", "profile", "platform", "company", "title", "score", "status", "T"))
    for upd, prof, plat, comp, title, score, status, tailored, _pdf in rows:
        print(fmt.format(upd[:16].replace("T", " "), prof[:12], plat[:9], (comp or "")[:20], (title or "")[:28],
                         "" if score is None else score, status, tailored))
    return 0


def llm_check(cfg):
    """python main.py --llm-check -> tiny real call to the configured LLM; no browser, no site access."""
    s = llm_settings(cfg)
    print("Provider:", s["provider"])
    print("Command :", " ".join(build_command(cfg)), "(prompt sent on stdin)" if s["provider"] == "cursor" else "")
    ok = True
    tests = [("Reply with exactly: OK", "", lambda r: "OK" in r),
             ('Reply with ONLY this JSON, no prose: {"score": 42, "reason": "check"}', "",
              lambda r: parse_score(r)[0] == 42)]
    for prompt, payload, check in tests:
        print("\nPrompt  :", prompt)
        try:
            reply = run_llm(cfg, prompt, payload)
        except LLMError as e:
            print("ERROR   :", e)
            ok = False
            continue
        try:
            parsed = bool(check(reply))
        except Exception:  # noqa: BLE001
            parsed = False
        print("Reply   :", reply)
        print("Parsed  :", "yes" if parsed else "NO")
        ok = ok and parsed
    print("\nLLM check:", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


@contextlib.contextmanager
def browser_session(headless):
    """Open the persistent browser; yields the first page. (Tests replace this function.)"""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(root() / "browser_profile"), headless=headless,
                                                   viewport={"width": 1366, "height": 900})
        try:
            yield ctx.pages[0] if ctx.pages else ctx.new_page()
        finally:
            ctx.close()


def run_bot(cfg, args, mode):
    profile = cfg["profile"]
    started = datetime.now()
    log.info("=== run start, profile=%s, mode=%s ===", profile, mode)

    (root() / "data").mkdir(exist_ok=True)
    db = DB(root() / "data" / ("jobs.db" if mode == "live" else "jobs_dryrun.db"))
    master_tex = profile_master_tex(cfg).read_text(encoding="utf-8")
    fallback_pdf = master_pdf(cfg)  # also proves the LaTeX toolchain + .cls file work before we browse

    rows = []
    with browser_session(args.headless) as page:
        adapters = get_adapters(cfg)

        # 1) collect from every platform
        jobs = []
        for name, ad in list(adapters.items()):
            try:
                if not ad.ensure_logged_in(page):
                    log.warning("%s: not logged in - run `python login.py`. Skipping this platform.", name)
                    del adapters[name]
                    continue
                jobs += ad.collect(page, cfg)
            except BlockedError as e:
                log.warning("%s blocked: %s", name, e)
                del adapters[name]
            except Exception as e:  # noqa: BLE001
                log.exception("%s: collect failed: %s", name, e)
                del adapters[name]

        # 2) drop jobs already handled (applied jobs block every profile), apply hard filters
        fresh = []
        for j in jobs:
            if db.seen(j.platform, j.job_id, profile):
                continue
            ok, why = passes_filters(j, cfg)
            if not ok:
                db.record(j, "skipped_filter", reason=why, profile=profile)
                continue
            fresh.append(j)

        # 3) NEWEST FIRST across all platforms ("just now" before "2 hours ago")
        fresh.sort(key=lambda j: j.age_minutes)
        log.info("%d new jobs to look at (of %d collected)", len(fresh), len(jobs))

        # 4) score -> tailor -> apply
        lim = cfg["limits"]
        done_run, blocked = 0, set()
        for job in fresh:
            if done_run >= lim["max_applications_per_run"] or db.applied_today() >= lim["max_applications_per_day"]:
                log.info("Limit reached; stopping.")
                break
            if job.platform in blocked or job.platform not in adapters:
                continue
            ad = adapters[job.platform]
            row = {"posted": job.posted_text, "platform": job.platform, "company": job.company,
                   "title": job.title, "score": "", "status": "", "detail": ""}
            try:
                job.description = ad.get_description(page, job)
                score, reason = score_job(cfg, master_tex, job)
                row["score"] = score
                if score < cfg["scoring"]["min_score"]:
                    db.record(job, "skipped_score", score, reason, profile=profile)
                    row.update(status="skipped_score", detail=reason)
                    rows.append(row)
                    continue

                pdf, folder, tailored = None, "", 0
                if ad.supports_resume_upload:
                    pdf = tailor_resume(cfg, job, retailor=args.retailor, score=score, reason=reason)
                    folder = job_folder(cfg, job)
                    if pdf is not None:
                        tailored = 1
                    elif cfg["resume"].get("on_tailor_failure") == "use_master":
                        pdf = fallback_pdf
                        reason += " | tailoring failed, used master resume"
                    else:
                        db.record(job, "skipped_tailor_failed", score, reason, profile=profile, job_folder=folder)
                        row.update(status="skipped_tailor_failed")
                        rows.append(row)
                        continue

                status, detail = ad.apply(page, job, pdf, cfg, submit=(mode == "live"))
                db.record(job, status, score, f"{reason} | {detail}", profile=profile,
                          resume_pdf=pdf or "", job_folder=folder, tailored=tailored)
                if status == "applied":
                    append_application_csv(root() / "data" / "applications.csv", job, profile, status, score,
                                           tailored, pdf, folder)
                elif status == "dry_run_ok":
                    append_application_csv(root() / "data" / "applications_dryrun.csv", job, profile, status, score,
                                           tailored, pdf, folder)
                row.update(status=status, detail=f"{detail} {('PDF: ' + pdf.name) if pdf else ''}".strip())
                if status in ("applied", "dry_run_ok"):
                    done_run += 1
                log.info("[%s] %s @ %s (%s) -> %s", job.platform, job.title, job.company, job.posted_text, status)
            except BlockedError as e:
                log.warning("%s blocked mid-run: %s", job.platform, e)
                blocked.add(job.platform)
                row.update(status="blocked", detail=str(e))
            except Exception as e:  # noqa: BLE001
                log.exception("error on %s: %s", job.url, e)
                shot = root() / "logs" / f"error_{job.platform}_{job.job_id}.png"
                try:
                    page.screenshot(path=str(shot))
                except Exception:  # noqa: BLE001
                    pass
                db.record(job, "error", reason=str(e)[:300], profile=profile)
                row.update(status="error", detail=str(e)[:150])
            rows.append(row)
            human_pause(cfg)

    report = write_report(rows, mode, started, profile)
    log.info("=== run done: %d processed, report: %s ===", len(rows), report)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", help="profile name (folder under resume/); optional if only one exists")
    ap.add_argument("--list-profiles", action="store_true", help="print the valid profile names")
    ap.add_argument("--new-profile", metavar="NAME", help="scaffold resume/NAME/")
    ap.add_argument("--check-profile", metavar="NAME", help="compile the profile's resume and check protected strings")
    ap.add_argument("--history", action="store_true", help="print recent jobs from the DB (use with --profile/--days)")
    ap.add_argument("--days", type=int, help="with --history: only the last N days")
    ap.add_argument("--retailor", action="store_true", help="regenerate tailored resumes even if a folder exists")
    ap.add_argument("--platforms", help="comma-separated override of config platforms, e.g. linkedin")
    ap.add_argument("--mode", choices=["dry-run", "live"], help="override config mode")
    ap.add_argument("--force", action="store_true", help="ignore active_hours")
    ap.add_argument("--headless", action="store_true", help="hide the browser (not recommended)")
    ap.add_argument("--tailor-test", metavar="JD_FILE", help="test scoring+tailoring on a text file, no browser")
    ap.add_argument("--llm-check", action="store_true", help="test the configured LLM backend, no browser")
    args = ap.parse_args(argv)

    setup_logging()
    try:
        if args.list_profiles:
            return list_profiles_cmd()
        if args.new_profile:
            return new_profile(args.new_profile)
        if args.check_profile:
            return check_profile(args.check_profile)
        if args.llm_check:
            return llm_check(load_config())
        if args.history:
            cfg = load_config()
            return history_cmd(cfg, args, args.mode or cfg.get("mode", "dry-run"))

        cfg = load_profile_config(resolve_profile(args.profile))
        if args.platforms:
            cfg["platforms"] = [p.strip() for p in args.platforms.split(",") if p.strip()]
        if args.tailor_test:
            return tailor_test(cfg, args.tailor_test)
        if not cfg["search"].get("keywords"):
            raise ProfileError(f"profile {cfg['profile']!r} has no search.keywords in {profile_dir(cfg['profile'])}/profile.yaml")
        if not profile_master_tex(cfg).exists():
            raise ProfileError(f"{profile_master_tex(cfg)} is missing")
    except ProfileError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    mode = args.mode or cfg.get("mode", "dry-run")
    lo, hi = cfg["active_hours"]
    if not (lo <= datetime.now().hour < hi) and not args.force:
        log.info("Outside active hours (%s-%s); exiting.", lo, hi)
        return 0
    return run_bot(cfg, args, mode)


if __name__ == "__main__":
    sys.exit(main())
