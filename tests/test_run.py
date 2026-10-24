"""End-to-end runs of main.main() with the browser and site adapters mocked (no network, nothing is submitted)."""
import contextlib
import csv
import io
import unittest
from unittest import mock

import main
from common import Job
from db import DB
from tests.helpers import HomeTestCase


class FakeAdapter:
    """Stands in for LinkedIn/Naukri. Records every apply() call into a shared list."""
    supports_resume_upload = True

    def __init__(self, name, jobs, calls, status="dry_run_ok"):
        self.name, self.jobs, self.calls, self.status = name, jobs, calls, status

    def ensure_logged_in(self, page):
        return True

    def collect(self, page, cfg):
        return list(self.jobs)

    def get_description(self, page, job):
        return f"Description of {job.title}"

    verify_result = "unknown"          # what verify() answers; tests patch this class attribute
    verified = []

    def verify(self, page, job):
        FakeAdapter.verified.append(job.job_id)
        return FakeAdapter.verify_result

    def apply(self, page, job, pdf_path, cfg, submit):
        self.calls.append({"platform": job.platform, "job_id": job.job_id, "submit": submit, "pdf": pdf_path})
        return self.status, "fake"


def mkjob(platform, job_id, age, company="Acme"):
    return Job(platform, job_id, "Sales Coordinator", company, "Bengaluru", f"http://{platform}/{job_id}",
               f"{age} minutes ago", age)


class RunTest(HomeTestCase):
    profiles = ("sales", "ops")

    def run_bot(self, *argv, jobs=None, status="dry_run_ok"):
        """jobs: {platform: [Job]}. Returns the list of apply() calls."""
        calls = []
        adapters = {p: FakeAdapter(p, js, calls, status) for p, js in (jobs or {}).items()}
        browser = contextlib.contextmanager(lambda headless: (yield object()))
        with mock.patch("main.browser_session", browser), mock.patch("main.get_adapters", lambda cfg: adapters), \
                mock.patch("main.human_pause"), mock.patch("main.human_pause_short"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main.main(list(argv)), 0)
        return calls

    def rows(self, name):
        with open(self.home / "data" / name, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def test_newest_first_across_platforms(self):
        calls = self.run_bot("--profile", "sales", "--force", jobs={
            "linkedin": [mkjob("linkedin", "L120", 120), mkjob("linkedin", "L5", 5)],
            "naukri": [mkjob("naukri", "N30", 30), mkjob("naukri", "N0", 0)]})
        self.assertEqual([c["job_id"] for c in calls], ["N0", "L5", "N30", "L120"])

    def test_platforms_flag_overrides_config(self):
        seen = {}
        with mock.patch("main.run_bot", side_effect=lambda cfg, args, mode: seen.update(p=cfg["platforms"]) or 0):
            main.main(["--profile", "sales", "--force", "--platforms", "linkedin"])
        self.assertEqual(seen["p"], ["linkedin"])

    def test_dry_run_is_default_submits_nothing_and_writes_dryrun_csv(self):
        calls = self.run_bot("--profile", "sales", "--force", jobs={"linkedin": [mkjob("linkedin", "1", 5)]})
        self.assertEqual([c["submit"] for c in calls], [False])
        self.assertFalse((self.home / "data" / "applications.csv").exists())
        (row,) = self.rows("applications_dryrun.csv")
        self.assertEqual((row["profile"], row["status"], row["tailored"], row["company"]), ("sales", "dry_run_ok", "1", "Acme"))
        self.assertTrue((self.home / "data" / "jobs_dryrun.db").exists())
        self.assertFalse((self.home / "data" / "jobs.db").exists())

    def test_live_run_records_folder_pdf_and_csv(self):
        calls = self.run_bot("--profile", "sales", "--mode", "live", "--force", status="applied",
                             jobs={"linkedin": [mkjob("linkedin", "7", 5, "Big Co")]})
        self.assertEqual([c["submit"] for c in calls], [True])  # the FAKE adapter; nothing real is submitted
        folder = self.home / "output" / "sales" / "linkedin_big-co_7"
        self.assertTrue((folder / "Test_Resume.pdf").exists())
        (row,) = self.rows("applications.csv")
        self.assertEqual(row["job_folder"], str(folder))
        self.assertEqual(row["resume_pdf"], str(folder / "Test_Resume.pdf"))
        self.assertEqual((row["notes"], row["outcome"]), ("", ""))
        db = DB(self.home / "data" / "jobs.db")
        (rec,) = db.conn.execute("SELECT profile, status, tailored, job_folder FROM jobs").fetchall()
        self.assertEqual(rec, ("sales", "applied", 1, str(folder)))

    def test_applied_job_is_never_applied_again_by_another_profile(self):
        job = mkjob("linkedin", "1", 5)
        live = ("--mode", "live", "--force")
        self.assertEqual(len(self.run_bot("--profile", "sales", *live, status="applied", jobs={"linkedin": [job]})), 1)
        again = self.run_bot("--profile", "ops", *live, status="applied", jobs={"linkedin": [job]})
        self.assertEqual(again, [])
        self.assertEqual([p.name for p in (self.home / "output" / "ops").iterdir()], ["_master"])  # no job folder
        self.assertEqual(len(self.rows("applications.csv")), 1)

    def test_skipped_score_in_one_profile_does_not_block_the_other(self):
        self.add_profile("picky", scoring={"min_score": 99})               # fake LLM scores 83
        job = mkjob("linkedin", "1", 5)
        self.assertEqual(self.run_bot("--profile", "picky", "--force", jobs={"linkedin": [job]}), [])
        calls = self.run_bot("--profile", "sales", "--force", jobs={"linkedin": [job]})
        self.assertEqual(len(calls), 1)

    def test_csv_is_append_only_across_runs(self):
        live = ("--profile", "sales", "--mode", "live", "--force")
        self.run_bot(*live, status="applied", jobs={"linkedin": [mkjob("linkedin", "1", 5)]})
        path = self.home / "data" / "applications.csv"
        first = path.read_bytes()
        self.run_bot(*live, status="applied", jobs={"linkedin": [mkjob("linkedin", "2", 5)]})
        second = path.read_bytes()
        self.assertTrue(second.startswith(first))
        self.assertEqual(second.count(b"date_time,profile"), 1)
        self.assertEqual([r["job_url"] for r in self.rows("applications.csv")], ["http://linkedin/1", "http://linkedin/2"])

    def test_non_applied_statuses_are_not_written_to_csv(self):
        self.run_bot("--profile", "sales", "--mode", "live", "--force", status="needs_answers",
                     jobs={"linkedin": [mkjob("linkedin", "1", 5)]})
        self.assertFalse((self.home / "data" / "applications.csv").exists())

    def test_tailor_failure_falls_back_to_master_pdf(self):
        # scoring works, tailoring yields nothing: the profile's cached master PDF is used
        with mock.patch("main.tailor_resume", return_value=None):
            calls = self.run_bot("--profile", "sales", "--force", jobs={"linkedin": [mkjob("linkedin", "1", 5)]})
        self.assertEqual(calls[0]["pdf"], self.home / "output" / "sales" / "_master" / "Test_Resume.pdf")
        (row,) = self.rows("applications_dryrun.csv")
        self.assertEqual(row["tailored"], "0")

    def test_history_command(self):
        self.run_bot("--profile", "sales", "--force", jobs={"linkedin": [mkjob("linkedin", "1", 5, "Zeta Corp")]})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main.main(["--history", "--profile", "sales", "--days", "7"]), 0)
        self.assertIn("Zeta Corp", out.getvalue())
        self.assertIn("dry_run_ok", out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main.main(["--history", "--profile", "ops"])
        self.assertNotIn("Zeta Corp", out.getvalue())


if __name__ == "__main__":
    unittest.main()


class LinkedInParsingTest(unittest.TestCase):
    def test_parse_card_text(self):
        from sites.linkedin import parse_card_text
        text = ("Member Technical Staff II (Full-stack) (Verified job)\nMember Technical Staff II (Full-stack) \n\n"
                "Nielsen\n\nBengaluru East\n\nActively reviewing applicants\n\nViewed\n\n · \n\nBe an early applicant"
                "\n\n · \n\nPosted 51 minutes ago\n51 minutes ago\n\n · \n\nEasy Apply")
        self.assertEqual(parse_card_text(text), {"title": "Member Technical Staff II (Full-stack)", "company": "Nielsen",
                                                 "location": "Bengaluru East", "posted": "Posted 51 minutes ago"})
        from common import parse_age_minutes
        self.assertEqual(parse_age_minutes("Posted 51 minutes ago"), 51)
        plain = parse_card_text("Sr SRE \n\nAcme\n\nBengaluru (On-site)\n\nPosted 3 hours ago\nEasy Apply")
        self.assertEqual((plain["title"], plain["company"], plain["location"]), ("Sr SRE", "Acme", "Bengaluru (On-site)"))

    def test_search_url(self):
        from sites.linkedin import search_url
        s = {"location": "Bengaluru", "geo_id": 105214831, "max_age_hours": 24}
        self.assertEqual(search_url(s, "site reliability engineer", 1),
                         "https://www.linkedin.com/jobs/search-results/?keywords=site+reliability+engineer"
                         "&geoId=105214831&f_TPR=r86400&sortBy=DD&start=25&f_AL=true")
        self.assertIn("location=Bengaluru", search_url({"location": "Bengaluru", "max_age_hours": 1}, "x", 0, False))


class FilterAndLimitTest(HomeTestCase):
    profiles = ("sales",)

    def cfg(self, **filters):
        return {"filters": filters}

    def test_experience_filter_reads_url_and_title(self):
        from main import min_years_required, passes_filters
        mk = lambda url="", title="Java Developer": Job("naukri", "1", title, "Acme", "Bengaluru", url, "", 0)
        cfg = self.cfg(max_experience_years=3)
        base = "https://www.naukri.com/job-listings-java-developer-acme-bengaluru-"
        self.assertEqual(min_years_required(mk(base + "4-to-7-years-0910")), 4)
        self.assertFalse(passes_filters(mk(base + "4-to-7-years-0910"), cfg)[0])
        self.assertIn("4+ years", passes_filters(mk(base + "4-to-7-years-0910"), cfg)[1])
        self.assertTrue(passes_filters(mk(base + "0-to-3-years-0910"), cfg)[0])
        self.assertTrue(passes_filters(mk(base + "3-to-5-years-0910"), cfg)[0])     # needs 3, cap is 3
        self.assertFalse(passes_filters(mk(base + "10-to-15-years-0910"), cfg)[0])
        self.assertFalse(passes_filters(mk("", "7+ YoE - Cloud Platform Engineering"), cfg)[0])
        self.assertFalse(passes_filters(mk("", "Developer (5-8 years)"), cfg)[0])
        self.assertTrue(passes_filters(mk("", "Python 3 Developer"), cfg)[0])        # no experience info: not skipped
        self.assertTrue(passes_filters(mk(base + "9-to-12-years-0910"), self.cfg())[0])  # filter not configured

    def test_experience_filter_skips_before_any_llm_call(self):
        self.add_profile("fresher", filters={"title_include_any": [], "title_exclude": [], "company_blacklist": [],
                                             "max_experience_years": 3})
        jobs = [Job("naukri", "A", "Java Developer", "Acme", "Bengaluru",
                    "https://www.naukri.com/job-x-acme-bengaluru-6-to-9-years-0910", "1 hour ago", 60),
                Job("naukri", "B", "Java Developer", "Beta", "Bengaluru",
                    "https://www.naukri.com/job-x-beta-bengaluru-0-to-2-years-0911", "2 hours ago", 120)]
        calls = RunTest.run_bot(self, "--profile", "fresher", "--force", jobs={"naukri": jobs})
        self.assertEqual([c["job_id"] for c in calls], ["B"])
        rows = dict((r[0], r[1]) for r in DB(self.home / "data" / "jobs_dryrun.db").conn.execute(
            "SELECT job_id, status FROM jobs"))
        self.assertEqual(rows, {"A": "skipped_filter", "B": "dry_run_ok"})

    def test_max_jobs_per_run_caps_the_jobs_looked_at(self):
        self.add_profile("capped", limits={"max_jobs_per_run": 2})
        jobs = [mkjob("linkedin", str(i), i) for i in range(1, 6)]
        calls = RunTest.run_bot(self, "--profile", "capped", "--force", jobs={"linkedin": jobs})
        self.assertEqual([c["job_id"] for c in calls], ["1", "2"])                   # newest two only
        ids = {r[0] for r in DB(self.home / "data" / "jobs_dryrun.db").conn.execute("SELECT job_id FROM jobs")}
        self.assertEqual(ids, {"1", "2"})                                            # others untouched: picked up next run

    def test_no_job_cap_by_default(self):
        jobs = [mkjob("linkedin", str(i), i) for i in range(1, 6)]
        self.assertEqual(len(RunTest.run_bot(self, "--profile", "sales", "--force", jobs={"linkedin": jobs})), 5)


class UnconfirmedCountsTowardLimitsTest(HomeTestCase):
    profiles = ("sales",)

    def test_unconfirmed_counts_toward_the_run_cap_and_the_daily_count(self):
        self.add_profile("capped", limits={"max_applications_per_run": 2})
        jobs = [mkjob("linkedin", str(i), i) for i in range(1, 6)]
        calls = RunTest.run_bot(self, "--profile", "capped", "--mode", "live", "--force", status="unconfirmed",
                                jobs={"linkedin": jobs})
        self.assertEqual(len(calls), 2)                     # stopped at the cap although nothing was 'applied'
        db = DB(self.home / "data" / "jobs.db")
        self.assertEqual(db.applied_today(), 2)
        self.assertFalse((self.home / "data" / "applications.csv").exists())   # CSV stays 'applied' only


class VerifyPassTest(HomeTestCase):
    profiles = ("sales",)

    def live_run(self, status, result, jobs=3, mode="live"):
        FakeAdapter.verified = []
        with mock.patch.object(FakeAdapter, "verify_result", result):
            RunTest.run_bot(self, "--profile", "sales", "--mode", mode, "--force", status=status,
                            jobs={"linkedin": [mkjob("linkedin", str(i), i) for i in range(1, jobs + 1)]})
        return DB(self.home / "data" / ("jobs.db" if mode == "live" else "jobs_dryrun.db"))

    def statuses(self, db):
        return sorted(r[0] for r in db.conn.execute("SELECT status FROM jobs"))

    def test_unconfirmed_jobs_found_applied_on_recheck_are_promoted_and_written_to_csv(self):
        db = self.live_run("unconfirmed", "applied")
        self.assertEqual(self.statuses(db), ["applied"] * 3)
        self.assertEqual(len(RunTest.rows(self, "applications.csv")), 3)
        reason = db.conn.execute("SELECT reason FROM jobs LIMIT 1").fetchone()[0]
        self.assertIn("verified on re-check", reason)

    def test_needs_answers_found_applied_is_promoted_too(self):
        self.assertEqual(self.statuses(self.live_run("needs_answers", "applied", jobs=1)), ["applied"])

    def test_not_applied_and_unknown_keep_their_status_and_write_no_csv(self):
        for result in ("not_applied", "unknown"):
            db = self.live_run("needs_answers", result, jobs=1)
            self.assertEqual(self.statuses(db), ["needs_answers"])
        self.assertFalse((self.home / "data" / "applications.csv").exists())

    def test_dry_run_never_verifies(self):
        self.live_run("dry_run_ok", "applied", mode="dry-run")
        self.assertEqual(FakeAdapter.verified, [])

    def test_applied_jobs_are_not_rechecked(self):
        self.live_run("applied", "unknown")
        self.assertEqual(FakeAdapter.verified, [])

    def test_verify_command_fixes_old_rows_and_keeps_original_timestamp(self):
        db = self.live_run("unconfirmed", "unknown", jobs=2)           # two unconfirmed rows
        db.conn.execute("UPDATE jobs SET updated='2026-01-02T03:04:05'")
        db.conn.commit()
        FakeAdapter.verified = []
        adapters = {"linkedin": FakeAdapter("linkedin", [], [])}
        browser = contextlib.contextmanager(lambda headless: (yield object()))
        with mock.patch.object(FakeAdapter, "verify_result", "applied"), mock.patch("main.browser_session", browser), \
                mock.patch("main.get_adapters", lambda cfg: adapters), mock.patch("main.human_pause_short"), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main.main(["--profile", "sales", "--verify"]), 0)
        self.assertIn("2 now 'applied'", out.getvalue())
        self.assertEqual(sorted(FakeAdapter.verified), ["1", "2"])
        db = DB(self.home / "data" / "jobs.db")
        self.assertEqual(self.statuses(db), ["applied", "applied"])
        self.assertEqual(db.applied_today(), 0)                          # old applications are not today's
        self.assertTrue(all(r["date_time"].startswith("2026-01-02 03:04:05")
                            for r in RunTest.rows(self, "applications.csv")))
