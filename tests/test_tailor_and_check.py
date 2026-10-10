import contextlib
import io
import os
import shutil
import tempfile
import unittest
from unittest import mock

import main
import tailor
from common import Job
from tests.helpers import make_agent, make_cfg, make_root


class TailorAndCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jobbot_agent_")
        self.root = make_root()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.agent, self.log = make_agent(self.tmp)
        self.cfg = make_cfg(self.agent)
        self.job = Job("linkedin", "42", "Sales Coordinator", "Acme Corp", "Bengaluru", "http://x", "1 hour ago",
                       60, "Coordinate sales.")
        # never write into the real output/ folder
        p = mock.patch.object(tailor, "ROOT", self.root)
        p.start()
        self.addCleanup(p.stop)

    def mode(self, m):
        return mock.patch.dict(os.environ, {"FAKE_MODE": m, "FAKE_LOG": self.log})

    def test_tailor_resume_end_to_end(self):
        with self.mode("ok"):
            pdf = tailor.tailor_resume(self.cfg, self.job)
        self.assertIsNotNone(pdf)
        self.assertTrue(pdf.exists())
        self.assertEqual(pdf.name, "Test_Resume.pdf")
        self.assertIn("% tailored", (pdf.parent / "resume.tex").read_text(encoding="utf-8"))
        self.assertTrue(str(pdf).startswith(str(self.root / "output")))

    def test_tailor_failure_writes_failed_txt(self):
        with self.mode("notex"):
            self.assertIsNone(tailor.tailor_resume(self.cfg, self.job))
        failed = list((self.root / "output").glob("*/FAILED.txt"))
        self.assertEqual(len(failed), 1)
        self.assertIn("<tex>", failed[0].read_text(encoding="utf-8"))

    def test_tailor_llm_error_is_not_fatal(self):
        for m in ("fail", "auth", "empty"):
            with self.mode(m):
                self.assertIsNone(tailor.tailor_resume(self.cfg, self.job))

    def run_check(self, m):
        buf = io.StringIO()
        with self.mode(m), contextlib.redirect_stdout(buf):
            code = main.llm_check(self.cfg)
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
