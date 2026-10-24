"""LinkedIn adapter (Easy Apply). Prefers role/text locators over CSS classes, which change often."""
import re
from urllib.parse import quote_plus

from playwright.sync_api import TimeoutError as PWTimeout

from common import BlockedError, Job, log, parse_age_minutes
from sites.base import Adapter

CARD_JS = """
els => els.map(li => {
  const q = s => { const e = li.querySelector(s); return e ? e.innerText.trim() : ''; };
  return {
    id: li.getAttribute('data-occludable-job-id') || '',
    title: q('a.job-card-list__title, a.job-card-container__link, a[href*="/jobs/view/"]'),
    company: q('.artdeco-entity-lockup__subtitle, .job-card-container__primary-description'),
    location: q('.artdeco-entity-lockup__caption, .job-card-container__metadata-item'),
    posted: q('time'),
  };
})
"""

SCROLL_JS = """
async () => {
  const el = document.querySelector('.jobs-search-results-list') || document.scrollingElement;
  for (let i = 0; i < 8; i++) { el.scrollBy(0, 900); await new Promise(r => setTimeout(r, 450)); }
}
"""


# Current LinkedIn layout: every job card is a div with componentkey="job-card-component-ref-<jobId>"
# (each card appears twice in the DOM, nested; duplicates are merged by job id in collect()).
NEW_CARD = '[componentkey^="job-card-component-ref-"]'
NEW_CARD_JS = "els => els.map(e => ({key: e.getAttribute('componentkey'), text: e.innerText}))"


def parse_card_text(text):
    """Card text looks like: 'Title (Verified job)', 'Title', 'Company', 'Location', ..., 'Posted 51 minutes ago', ..."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip() and ln.strip() != "\u00b7"]
    if not lines:
        return {"title": "", "company": "", "location": "", "posted": ""}
    title = re.sub(r"\s*\((Verified job|Promoted)\)\s*$", "", lines[0]).strip()
    rest = lines[1:]
    if rest and rest[0].strip() == title:
        rest = rest[1:]
    posted = next((ln for ln in lines if re.search(r"\b(ago|just now)\b", ln, re.I)), "")
    return {"title": title, "company": rest[0] if rest else "", "location": rest[1] if len(rest) > 1 else "",
            "posted": posted}


def search_url(s, keyword, page_no, easy_apply=True):
    """Search URL for the current layout. geoId (search.geo_id) pins the location; plain text is often ignored."""
    url = (f"https://www.linkedin.com/jobs/search-results/?keywords={quote_plus(keyword)}"
           + (f"&geoId={s['geo_id']}" if s.get("geo_id") else f"&location={quote_plus(s['location'])}")
           + f"&f_TPR=r{int(s['max_age_hours']) * 3600}&sortBy=DD&start={page_no * 25}")
    return url + ("&f_AL=true" if easy_apply else "")


def _check_blocked(page):
    u = page.url
    if any(x in u for x in ("checkpoint", "authwall", "/login", "captcha", "uas/")):
        raise BlockedError(f"LinkedIn shows a login/security page: {u}")


class LinkedIn(Adapter):
    name = "linkedin"
    supports_resume_upload = True

    def ensure_logged_in(self, page):
        page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        return not any(x in page.url for x in ("login", "authwall", "checkpoint", "uas/"))

    def collect(self, page, cfg):
        s = cfg["search"]
        found = {}
        for kw in s["keywords"]:
            for p in range(s.get("pages", 2)):
                url = search_url(s, kw, p, easy_apply=cfg.get("linkedin", {}).get("easy_apply_only", True))
                page.goto(url, wait_until="domcontentloaded")
                _check_blocked(page)
                try:
                    page.wait_for_selector(f"{NEW_CARD}, li[data-occludable-job-id]", timeout=15000)
                except PWTimeout:
                    log.info("linkedin: no result cards for %r page %d", kw, p + 1)
                    break
                page.evaluate(SCROLL_JS)
                if page.locator(NEW_CARD).count():   # current layout (/jobs/search-results/)
                    raw = page.eval_on_selector_all(NEW_CARD, NEW_CARD_JS)
                    rows = [dict(parse_card_text(r["text"]), id=r["key"].rsplit("-", 1)[-1]) for r in raw]
                else:                                # previous layout
                    rows = page.eval_on_selector_all("li[data-occludable-job-id]", CARD_JS)
                for row in rows:
                    if not row["id"] or not row["title"]:
                        continue
                    job = Job("linkedin", row["id"], row["title"].split("\n")[0].strip(),
                              row["company"].split("\n")[0].strip(), row["location"].split("\n")[0].strip(),
                              f"https://www.linkedin.com/jobs/view/{row['id']}/", row["posted"],
                              parse_age_minutes(row["posted"]))
                    if job.job_id not in found or job.age_minutes < found[job.job_id].age_minutes:
                        found[job.job_id] = job
        log.info("linkedin: collected %d jobs", len(found))
        return list(found.values())

    def get_description(self, page, job):
        page.goto(job.url, wait_until="domcontentloaded")
        _check_blocked(page)
        page.wait_for_timeout(2000)
        try:
            return page.locator("#job-details, .jobs-description__content, .jobs-box__html-content").first.inner_text(timeout=8000)
        except PWTimeout:
            return page.inner_text("main", timeout=5000)

    # ---- applying -------------------------------------------------------------------------
    def apply(self, page, job, pdf_path, cfg, submit):
        if job.url not in page.url:
            page.goto(job.url, wait_until="domcontentloaded")
        _check_blocked(page)
        btn = page.get_by_role("button", name=re.compile(r"^Easy Apply", re.I)).first
        try:
            btn.wait_for(state="visible", timeout=8000)
        except PWTimeout:
            if page.get_by_text(re.compile(r"^Applied", re.I)).count():
                return "already_applied", "LinkedIn shows this job as already applied"
            return "no_apply_button", "No Easy Apply button (external apply or closed)"
        btn.click()
        dialog = page.locator("div.jobs-easy-apply-modal, div[role='dialog']").first
        try:
            dialog.wait_for(state="visible", timeout=8000)
        except PWTimeout:
            return "error", "Easy Apply dialog did not open"

        uploaded = False
        answers = cfg.get("answers") or []
        for _ in range(14):
            page.wait_for_timeout(900)
            if pdf_path and not uploaded:
                uploaded = self._upload(dialog, pdf_path)
            self._fill_answers(dialog, answers)

            submit_btn = dialog.get_by_role("button", name=re.compile(r"Submit application", re.I))
            if submit_btn.count():
                if not submit:
                    self._discard(page)
                    return "dry_run_ok", "Reached the final step; discarded (dry-run)"
                submit_btn.first.click()
                page.wait_for_timeout(2500)
                ok = page.get_by_text(re.compile(r"application (was )?sent|application submitted", re.I)).count() > 0
                self._close_done(page)
                return ("applied", "submitted") if ok else ("unconfirmed", "Clicked submit; no confirmation text seen")

            nxt = dialog.get_by_role("button", name=re.compile(r"^(Next|Continue|Review)", re.I))
            if not nxt.count():
                self._discard(page)
                return "error", "No Next/Review/Submit button found in the dialog"
            nxt.first.click()
            page.wait_for_timeout(1200)
            if dialog.locator(".artdeco-inline-feedback--error").count():
                self._discard(page)
                return "needs_answers", "Form has required questions the bot has no answer for (see config 'answers')"
        self._discard(page)
        return "error", "Gave up after too many steps"

    def _upload(self, dialog, pdf_path):
        inputs = dialog.locator("input[type='file']")
        for i in range(inputs.count()):
            el = inputs.nth(i)
            hint = " ".join(filter(None, [el.get_attribute("id"), el.get_attribute("name"), el.get_attribute("aria-label")])).lower()
            if "cover" in hint:
                continue
            el.set_input_files(str(pdf_path))
            log.info("linkedin: uploaded %s", pdf_path)
            return True
        return False

    def _fill_answers(self, dialog, answers):
        """Fill screening questions only when a config rule matches the question text. Never guesses."""
        rules = [(re.compile(a["match"], re.I), str(a["value"])) for a in answers if str(a.get("value", "")).strip()]
        if not rules:
            return
        groups = dialog.locator("div.fb-dash-form-element, div.jobs-easy-apply-form-element, fieldset")
        for i in range(groups.count()):
            g = groups.nth(i)
            try:
                text = g.inner_text(timeout=1500)
                label = next((ln for ln in text.splitlines() if ln.strip()), "")
                value = next((v for rx, v in rules if rx.search(label)), None)
                if value is None:
                    continue
                inp = g.locator("input[type='text'], input[type='number'], input:not([type]), textarea")
                sel = g.locator("select")
                if inp.count():
                    if not inp.first.input_value():
                        inp.first.fill(value)
                elif sel.count():
                    sel.first.select_option(label=value)
                else:
                    g.get_by_label(re.compile(rf"^{re.escape(value)}$", re.I)).first.check(force=True)
            except Exception as e:  # noqa: BLE001
                log.debug("linkedin: could not fill a question: %s", e)

    def _discard(self, page):
        try:
            page.get_by_role("button", name=re.compile(r"^Dismiss", re.I)).first.click(timeout=3000)
            page.wait_for_timeout(600)
            page.get_by_role("button", name=re.compile(r"^Discard", re.I)).first.click(timeout=3000)
        except Exception:  # noqa: BLE001
            page.keyboard.press("Escape")

    def _close_done(self, page):
        try:
            page.get_by_role("button", name=re.compile(r"^(Done|Dismiss)", re.I)).first.click(timeout=3000)
        except Exception:  # noqa: BLE001
            page.keyboard.press("Escape")
