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
                mock.patch("main.human_pause"), contextlib.redirect_stdout(io.StringIO()):
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
