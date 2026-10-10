import contextlib
import io
import json
import os
import unittest
from pathlib import Path

import main
import tailor
from common import Job, load_profile_config
from tests.helpers import HomeTestCase


def make_job(job_id="42", platform="linkedin", company="Acme Corp", age=60):
    return Job(platform, job_id, "Sales Coordinator", company, "Bengaluru", "http://x/" + job_id,
               "1 hour ago", age, "Coordinate sales. Great team.")


class TailorTest(HomeTestCase):
    def setUp(self):
        super().setUp()
        self.cfg = load_profile_config("default")
        self.job = make_job()
        self.folder = self.home / "output" / "default" / "linkedin_acme-corp_42"

    def test_output_folder_layout(self):
        pdf = tailor.tailor_resume(self.cfg, self.job, score=83, reason="good fit")
        self.assertEqual(pdf, self.folder / "Test_Resume.pdf")
        for f in ("resume.tex", "resume.cls", "Test_Resume.pdf", "job.txt", "meta.json", "changes.diff"):
            self.assertTrue((self.folder / f).exists(), f)
        self.assertFalse((self.folder / "FAILED.txt").exists())
        job_txt = (self.folder / "job.txt").read_text(encoding="utf-8")
        for part in ("Sales Coordinator", "Acme Corp", "Bengaluru", "http://x/42", "1 hour ago", "Great team."):
            self.assertIn(part, job_txt)
        meta = json.loads((self.folder / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual((meta["profile"], meta["score"], meta["reason"], meta["attempts"]), ("default", 83, "good fit", 1))
        self.assertEqual(meta["validation"], {"ok": True, "errors": []})
        self.assertRegex(meta["created_at"], r"^\d{4}-\d\d-\d\dT")
        diff = (self.folder / "changes.diff").read_text(encoding="utf-8")
        self.assertIn("--- master/resume.tex", diff)
        self.assertIn("+% tailored", diff)

    def test_folder_is_per_profile(self):
        self.add_profile("other")
        other = load_profile_config("other")
        self.assertNotEqual(tailor.job_folder(other, self.job), tailor.job_folder(self.cfg, self.job))
        self.assertEqual(tailor.job_folder(other, self.job).parent.name, "other")

    def test_reuse_without_llm_call(self):
        first = tailor.tailor_resume(self.cfg, self.job)
        self.forget_llm_call()
        with self.mode("fail"):  # would fail if the LLM were called
            again = tailor.tailor_resume(self.cfg, self.job)
        self.assertEqual(again, first)
        self.assertFalse(self.llm_called())

    def test_retailor_regenerates_and_keeps_previous_tex(self):
        tailor.tailor_resume(self.cfg, self.job)
        old = (self.folder / "resume.tex").read_text(encoding="utf-8")
        self.forget_llm_call()
        pdf = tailor.tailor_resume(self.cfg, self.job, retailor=True)
        self.assertTrue(self.llm_called())
        self.assertIsNotNone(pdf)
        self.assertEqual((self.folder / "resume.prev.tex").read_text(encoding="utf-8"), old)

    def test_failed_twice_writes_failed_txt_and_is_not_reused(self):
        with self.mode("notex"):
            self.assertIsNone(tailor.tailor_resume(self.cfg, self.job))
        self.assertTrue((self.folder / "FAILED.txt").exists())
        meta = json.loads((self.folder / "meta.json").read_text(encoding="utf-8"))
        self.assertFalse(meta["validation"]["ok"])
        self.assertEqual(meta["attempts"], 2)
        # next run tries again (no reuse of a failed folder) and clears FAILED.txt on success
        self.assertIsNotNone(tailor.tailor_resume(self.cfg, self.job))
        self.assertFalse((self.folder / "FAILED.txt").exists())

    def test_llm_errors_are_not_fatal(self):
        for m in ("fail", "auth", "empty"):
            with self.mode(m):
                self.assertIsNone(tailor.tailor_resume(self.cfg, self.job, retailor=True))

    def test_master_pdf_cached_per_profile(self):
        pdf = tailor.master_pdf(self.cfg)
        self.assertEqual(pdf, self.home / "output" / "default" / "_master" / "Test_Resume.pdf")
        mtime = pdf.stat().st_mtime_ns
        self.assertEqual(tailor.master_pdf(self.cfg).stat().st_mtime_ns, mtime)  # cached
        self.assertNotEqual(tailor.master_pdf(self.cfg, force=True).stat().st_mtime_ns, mtime)

    def test_target_jd_is_in_the_tailoring_prompt(self):
        (self.home / "resume" / "default" / "target_jd.md").write_text("Junior B2B inside sales in SaaS", encoding="utf-8")
        cfg = load_profile_config("default")
        tailor.tailor_resume(cfg, self.job)
        stdin = Path(self.log + ".stdin").read_text(encoding="utf-8")
        self.assertIn("<profile_target>\nJunior B2B inside sales in SaaS\n</profile_target>", stdin)


class LLMCheckTest(HomeTestCase):
    def run_check(self, m):
        buf = io.StringIO()
        with self.mode(m), contextlib.redirect_stdout(buf):
            code = main.main(["--llm-check"])
        return code, buf.getvalue()

    def test_llm_check_ok(self):
        code, out = self.run_check("ok")
        self.assertEqual(code, 0)
        self.assertIn("Provider: cursor", out)
        self.assertIn("-p --trust --mode ask --output-format text", out)
        self.assertIn("PASSED", out)

    def test_llm_check_reports_failure(self):
        code, out = self.run_check("auth")
        self.assertEqual(code, 1)
        self.assertIn("cursor-agent login", out)
        self.assertIn("FAILED", out)


if __name__ == "__main__":
    unittest.main()
