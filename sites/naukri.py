"""Naukri adapter. NOTE: Naukri's Apply button uses the resume saved on your Naukri profile, so a per-job
tailored PDF cannot be attached (supports_resume_upload = False)."""
import math
import re
import select
import sys
import time

from playwright.sync_api import Error as PWError
from playwright.sync_api import TimeoutError as PWTimeout

from common import BlockedError, Job, log, parse_age_minutes, slugify
from sites.base import Adapter

CARD_JS = """
() => {
  const cards = document.querySelectorAll('div.srp-jobtuple-wrapper, div.cust-job-tuple, article.jobTuple');
  return Array.from(cards).map(c => {
    const q = s => { const e = c.querySelector(s); return e ? e : null; };
    const a = q('a.title, a[class*="title"]');
    const comp = q('a.comp-name, a[class*="comp-name"], [class*="subTitle"]');
    const loc = q('.locWdth, [class*="locWdth"], [class*="loc"]');
    const day = q('span.job-post-day, [class*="job-post-day"], [class*="post-day"]');
    return {
      id: c.getAttribute('data-job-id') || '',
      url: a ? a.href : '',
      title: a ? a.innerText.trim() : '',
      company: comp ? comp.innerText.trim() : '',
      location: loc ? loc.innerText.trim() : '',
      posted: day ? day.innerText.trim() : '',
    };
  });
}
"""


def _check_blocked(page):
    if any(x in page.url for x in ("nlogin", "captcha", "/login")):
        raise BlockedError(f"Naukri shows a login/captcha page: {page.url}")


def _goto(page, url):
    """page.goto that survives Naukri's delayed apply redirect ('interrupted by another navigation')."""
    for attempt in (1, 2):
        try:
            page.goto(url, wait_until="domcontentloaded")
            return
        except PWError as e:
            if attempt == 2 or "interrupted by another navigation" not in str(e):
                raise
            page.wait_for_timeout(2500)


def _applied(page):
    """After applying, Naukri turns the button into an 'Applied' label (it shows no 'successfully applied' text)."""
    try:
        return page.get_by_text(re.compile(r"^Applied$|successfully applied|applied successfully", re.I)).count() > 0
    except PWError:      # page is mid-navigation (the saveApply redirect): ask again shortly
        return False


def _questionnaire_open(page):
    """A real recruiter questionnaire/chatbot, visible. (The page header always has 'Drawer' classes, so not those.)"""
    try:
        return page.locator("[class*='chatbot'], [class*='questionnaire']").first.is_visible()
    except PWError:
        return False


def _interactive():
    """True when a person can see and answer a prompt (not cron / run.sh, which redirect output to a log)."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def _ask_user(job, timeout, poll, slice_s=2.0):
    """Tell the user a questionnaire is open and wait for the first of: Enter/y ('done'), s/n ('skip'),
    poll() turning true ('label': the page shows Applied by itself) or the timeout ('timeout')."""
    print(f"\n>>> Questionnaire open for: {job.title} @ {job.company}\n"
          f"    Answer it in the browser window, then press Enter here (or type s + Enter to skip). "
          f"Waiting up to {int(timeout)}s...", flush=True)
    deadline = time.monotonic() + timeout
    while True:
        ready, _, _ = select.select([sys.stdin], [], [], slice_s)
        if ready:
            line = sys.stdin.readline().strip().lower()
            return "skip" if line in ("s", "n", "skip", "no") else "done"
        if poll():
            return "label"
        if time.monotonic() >= deadline:
            return "timeout"


class Naukri(Adapter):
    name = "naukri"
    supports_resume_upload = False

    def verify(self, page, job):
        """Reopen the job page: an 'Applied' label means it went through; a visible Apply button means it did not."""
        _goto(page, job.url)
        page.wait_for_timeout(3000)
        _check_blocked(page)
        if _applied(page):
            return "applied"
        btn = page.locator("#apply-button")
        if btn.count() or page.get_by_role("button", name=re.compile(r"^Apply$", re.I)).count():
            return "not_applied"
        return "unknown"

    def ensure_logged_in(self, page):
        page.goto("https://www.naukri.com/mnjuser/homepage", wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        return not any(x in page.url for x in ("nlogin", "login"))

    def collect(self, page, cfg):
        s = cfg["search"]
        days = max(1, math.ceil(int(s["max_age_hours"]) / 24))
        found = {}
        for kw in s["keywords"]:
            for p in range(1, s.get("pages", 2) + 1):
                base = f"https://www.naukri.com/{slugify(kw, 80)}-jobs-in-{slugify(s['location'], 40)}"
                url = f"{base}{'' if p == 1 else f'-{p}'}?jobAge={days}"
                page.goto(url, wait_until="domcontentloaded")
                _check_blocked(page)
                page.wait_for_timeout(2500)
                rows = page.evaluate(CARD_JS)
                if not rows:
                    log.info("naukri: no result cards for %r page %d", kw, p)
                    break
                for row in rows:
                    if not row["url"] or not row["title"]:
                        continue
                    jid = row["id"] or (re.search(r"(\d{8,})", row["url"]) or [None, slugify(row["url"], 30)])[1]
                    job = Job("naukri", str(jid), row["title"].split("\n")[0].strip(),
                              row["company"].split("\n")[0].strip(), row["location"].split("\n")[0].strip(),
                              row["url"], row["posted"], parse_age_minutes(row["posted"]))
                    if job.job_id not in found or job.age_minutes < found[job.job_id].age_minutes:
                        found[job.job_id] = job
        log.info("naukri: collected %d jobs", len(found))
        return list(found.values())

    def get_description(self, page, job):
        _goto(page, job.url)
        _check_blocked(page)
        page.wait_for_timeout(2500)
        try:
            return page.locator("[class*='job-desc'], [class*='JDC'], .dang-inner-html").first.inner_text(timeout=8000)
        except PWTimeout:
            return page.inner_text("main", timeout=5000)

    def apply(self, page, job, pdf_path, cfg, submit):
        if job.url not in page.url:
            _goto(page, job.url)
        _check_blocked(page)
        if page.get_by_text(re.compile(r"^Applied$", re.I)).count():
            return "already_applied", "Naukri shows this job as already applied"
        btn = page.locator("#apply-button")
        if not btn.count():
            btn = page.get_by_role("button", name=re.compile(r"^Apply$", re.I))
        if not btn.count():
            if page.get_by_role("button", name=re.compile(r"apply on company site", re.I)).count():
                return "external", "Only 'Apply on company site' is offered"
            return "no_apply_button", "No Apply button found"
        if not submit:
            return "dry_run_ok", "Apply button found (not clicked in dry-run)"
        btn.first.click()
        for _ in range(4):                       # give Naukri up to ~8s to flip the button to 'Applied'
            page.wait_for_timeout(2000)
            _check_blocked(page)
            if _applied(page):
                return "applied", "submitted with profile resume"
        if _questionnaire_open(page):
            wait = int((cfg.get("naukri") or {}).get("questionnaire_wait_seconds", 120))
            if wait > 0 and _interactive():
                outcome = _ask_user(job, wait, lambda: _applied(page))
                if outcome == "label":
                    return "applied", "questionnaire answered by hand, submitted"
                if outcome == "done":                      # you say you answered: give Naukri a moment to confirm
                    for _ in range(4):
                        page.wait_for_timeout(2000)
                        _check_blocked(page)
                        if _applied(page):
                            return "applied", "questionnaire answered by hand, submitted"
            elif wait > 0:                                 # unattended (cron): just poll for the label
                log.info("Recruiter questionnaire open; polling up to %ds for it to be answered...", wait)
                for _ in range(wait // 3):
                    page.wait_for_timeout(3000)
                    _check_blocked(page)
                    if _applied(page):
                        return "applied", "questionnaire answered by hand, submitted"
            return "needs_answers", "Recruiter questionnaire appeared; left for you to do by hand"
        return "unconfirmed", "Clicked Apply; no 'Applied' label seen: check the job on Naukri"
