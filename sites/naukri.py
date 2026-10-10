"""Naukri adapter. NOTE: Naukri's Apply button uses the resume saved on your Naukri profile, so a per-job
tailored PDF cannot be attached (supports_resume_upload = False)."""
import math
import re

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


class Naukri(Adapter):
    name = "naukri"
    supports_resume_upload = False

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
        page.goto(job.url, wait_until="domcontentloaded")
        _check_blocked(page)
        page.wait_for_timeout(2500)
        try:
            return page.locator("[class*='job-desc'], [class*='JDC'], .dang-inner-html").first.inner_text(timeout=8000)
        except PWTimeout:
            return page.inner_text("main", timeout=5000)

    def apply(self, page, job, pdf_path, cfg, submit):
        if job.url not in page.url:
            page.goto(job.url, wait_until="domcontentloaded")
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
        page.wait_for_timeout(3500)
        _check_blocked(page)
        if page.get_by_text(re.compile(r"successfully applied|applied successfully", re.I)).count():
            return "applied", "submitted with profile resume"
        if page.locator("[class*='chatbot'], [class*='Drawer'], [class*='questionnaire']").count():
            return "needs_answers", "Recruiter questionnaire appeared; left for you to do by hand"
        return "unconfirmed", "Clicked Apply; no confirmation text seen"
