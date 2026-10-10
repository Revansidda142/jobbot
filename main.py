"""Job bot runner: collect -> filter -> newest first -> score -> tailor -> apply -> report."""
import argparse
import sys
from datetime import datetime

from common import ROOT, BlockedError, Job, human_pause, load_config, log, parse_age_minutes, setup_logging
from claude_client import score_job
from db import DB
from tailor import master_pdf, tailor_resume


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


def write_report(rows, mode, started):
    path = ROOT / "logs" / f"run_{started:%Y%m%d_%H%M%S}.md"
    lines = [f"# Run {started:%Y-%m-%d %H:%M} ({mode})", "",
             "| Posted | Platform | Company | Title | Score | Result | Detail |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append("| {posted} | {platform} | {company} | {title} | {score} | {status} | {detail} |".format(
            **{k: str(v).replace("|", "/") for k, v in r.items()}))
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def tailor_test(cfg, jd_file):
    """python main.py --tailor-test jd.txt  -> score + tailor against a pasted job description (no browser)."""
    desc = open(jd_file, encoding="utf-8").read()
    job = Job("test", "jd", "Test role", "TestCo", "Bengaluru", "", "", 0, desc)
    master_tex = (ROOT / cfg["resume"]["master_dir"] / cfg["resume"]["tex_file"]).read_text(encoding="utf-8")
    print("Master resume compiles:", master_pdf(cfg))
    print("Score:", score_job(cfg, master_tex, job))
    pdf = tailor_resume(cfg, job)
    print("Tailored PDF:", pdf if pdf else "FAILED - see the newest folder in output/ (FAILED.txt)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["dry-run", "live"], help="override config mode")
    ap.add_argument("--force", action="store_true", help="ignore active_hours")
    ap.add_argument("--headless", action="store_true", help="hide the browser (not recommended)")
    ap.add_argument("--tailor-test", metavar="JD_FILE", help="test scoring+tailoring on a text file, no browser")
    args = ap.parse_args()

    setup_logging()
    cfg = load_config()
    if args.tailor_test:
        return tailor_test(cfg, args.tailor_test)

    mode = args.mode or cfg.get("mode", "dry-run")
    started = datetime.now()
    lo, hi = cfg["active_hours"]
    if not (lo <= started.hour < hi) and not args.force:
        log.info("Outside active hours (%s-%s); exiting.", lo, hi)
        return
    log.info("=== run start, mode=%s ===", mode)

    (ROOT / "data").mkdir(exist_ok=True)
    db = DB(ROOT / "data" / ("jobs.db" if mode == "live" else "jobs_dryrun.db"))
    master_tex = (ROOT / cfg["resume"]["master_dir"] / cfg["resume"]["tex_file"]).read_text(encoding="utf-8")
    fallback_pdf = master_pdf(cfg)  # also proves tectonic + the .cls file work before we browse

    from playwright.sync_api import sync_playwright
    rows = []
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(ROOT / "browser_profile"), headless=args.headless,
                                                   viewport={"width": 1366, "height": 900})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
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

        # 2) drop jobs already handled, apply hard filters
        fresh = []
        for j in jobs:
            if db.seen(j.platform, j.job_id):
                continue
            ok, why = passes_filters(j, cfg)
            if not ok:
                db.record(j, "skipped_filter", reason=why)
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
                    db.record(job, "skipped_score", score, reason)
                    row.update(status="skipped_score", detail=reason)
                    rows.append(row)
                    continue

                pdf = None
                if ad.supports_resume_upload:
                    pdf = tailor_resume(cfg, job)
                    if pdf is None and cfg["resume"].get("on_tailor_failure") == "use_master":
                        pdf = fallback_pdf
                        reason += " | tailoring failed, used master resume"
                    elif pdf is None:
                        db.record(job, "skipped_tailor_failed", score, reason)
                        row.update(status="skipped_tailor_failed")
                        rows.append(row)
                        continue

                status, detail = ad.apply(page, job, pdf, cfg, submit=(mode == "live"))
                db.record(job, status, score, f"{reason} | {detail}", str(pdf or ""))
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
                shot = ROOT / "logs" / f"error_{job.platform}_{job.job_id}.png"
                try:
                    page.screenshot(path=str(shot))
                except Exception:  # noqa: BLE001
                    pass
                db.record(job, "error", reason=str(e)[:300])
                row.update(status="error", detail=str(e)[:150])
            rows.append(row)
            human_pause(cfg)
        ctx.close()

    report = write_report(rows, mode, started)
    log.info("=== run done: %d processed, report: %s ===", len(rows), report)


if __name__ == "__main__":
    sys.exit(main())
