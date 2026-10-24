import hashlib
import http.client
import threading
import unittest
import urllib.error
import urllib.request
from urllib.parse import urlencode

import ui
from common import Job
from db import DB
from tests.helpers import HomeTestCase


def job(job_id, company, title="Site Reliability Engineer", platform="linkedin", url=None):
    return Job(platform, job_id, title, company, "Bengaluru", url or f"https://example.com/jobs/{job_id}",
               "1 hour ago", 60, "d")


class UITest(HomeTestCase):
    profiles = ("default", "other")

    def setUp(self):
        super().setUp()
        self.server = ui.make_server(0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.dbfile = self.home / "data" / "jobs_dryrun.db"

    def get(self, path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}") as r:
                return r.status, r.headers, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.headers, e.read().decode("utf-8", "replace")

    def seed(self):
        (self.home / "data").mkdir(exist_ok=True)
        db = DB(self.dbfile)
        db.record(job("1", "AppliedCo"), "applied", 90, "r1", profile="default")
        db.record(job("2", "ReadyCo"), "dry_run_ok", 80, "r2", profile="default")
        db.record(job("3", "HandCo", url="https://naukri.example/job-3", platform="naukri"), "external", 72, "r3",
                  profile="default")
        db.record(job("4", "SkipCo", title="<script>alert(1)</script>"), "skipped_score", 10, "r4", profile="other")
        db.record(job("5", "ErrCo"), "error", None, "boom", profile="other")
        db.record(job("6", "EvilUrlCo", url="javascript:alert(1)"), "external", 60, "r6", profile="other")
        db.conn.close()

    # ---- jobs page ----
    def test_jobs_grouped_into_sections(self):
        self.seed()
        status, headers, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        marks = ["<h2>Applied", "<h2>Ready", "<h2>You need to apply by hand", "<summary><b>Skipped", "<h2>Errors"]
        companies = ["AppliedCo", "ReadyCo", "HandCo", "SkipCo", "ErrCo"]
        pos = [body.index(m) for m in marks] + [len(body)]
        for i, c in enumerate(companies):
            self.assertTrue(pos[i] < body.index(c) < pos[i + 1], f"{c} is not in section {marks[i]}")

    def test_external_job_is_listed_with_its_link(self):
        self.seed()
        body = self.get("/")[2]
        hand = body[body.index("<h2>You need to apply by hand"):body.index("<summary><b>Skipped")]
        self.assertIn("HandCo", hand)
        self.assertIn('href="https://naukri.example/job-3"', hand)
        self.assertIn("apply on company site", hand)

    def test_filters(self):
        self.seed()
        body = self.get("/?profile=other")[2]
        self.assertIn("SkipCo", body)
        self.assertNotIn("AppliedCo", body)
        body = self.get("/?platform=naukri")[2]
        self.assertIn("HandCo", body)
        self.assertNotIn("ReadyCo", body)
        self.assertIn("ReadyCo", self.get("/?days=7")[2])

    def test_untrusted_text_is_escaped(self):
        self.seed()
        body = self.get("/")[2]
        self.assertNotIn("<script>alert(1)</script>", body)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", body)
        self.assertNotIn("javascript:", body.replace("alert(1)", ""))  # a javascript: URL never becomes a link

    def test_missing_db_shows_empty_state(self):
        status, _, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("No history yet", body)
        self.assertIn("No history yet", self.get("/?db=live")[2])

    def test_browsing_never_changes_the_db(self):
        self.seed()
        before = hashlib.sha256(self.dbfile.read_bytes()).hexdigest()
        mtime = self.dbfile.stat().st_mtime_ns
        for p in ("/", "/?profile=other", "/profiles", "/profile/default", "/config", "/?db=live"):
            self.get(p)
        self.assertEqual(hashlib.sha256(self.dbfile.read_bytes()).hexdigest(), before)
        self.assertEqual(self.dbfile.stat().st_mtime_ns, mtime)
        self.assertFalse((self.home / "data" / "jobs.db").exists())   # a read-only open never creates a DB

    # ---- job detail + files ----
    def test_job_detail_and_file_route(self):
        folder = self.home / "output" / "default" / "linkedin_acme_9"
        folder.mkdir(parents=True)
        (folder / "job.txt").write_text("Title: SRE\nWe need Kubernetes", encoding="utf-8")
        (folder / "meta.json").write_text('{"score": 77}', encoding="utf-8")
        (folder / "changes.diff").write_text("+% tailored", encoding="utf-8")
        (folder / "My_Resume.pdf").write_bytes(b"%PDF-1.4 fake")
        (self.home / "data").mkdir()
        db = DB(self.dbfile)
        db.record(job("9", "Acme"), "dry_run_ok", 77, "good", profile="default", resume_pdf=folder / "My_Resume.pdf",
                  job_folder=folder, tailored=1)
        db.conn.close()
        q = urlencode({"db": "dry", "platform": "linkedin", "id": "9", "profile": "default"})
        status, _, body = self.get("/job?" + q)
        self.assertEqual(status, 200)
        for text in ("We need Kubernetes", "+% tailored", "score", "My_Resume.pdf"):
            self.assertIn(text, body)
        pdf_status, headers, pdf_body = self.get("/file?" + urlencode({"path": str(folder / "My_Resume.pdf")}))
        self.assertEqual((pdf_status, headers["Content-Type"]), (200, "application/pdf"))
        self.assertTrue(pdf_body.startswith("%PDF"))
        self.assertEqual(self.get("/job?db=dry&platform=linkedin&id=nope&profile=default")[0], 404)

    def test_file_route_blocks_traversal_and_other_files(self):
        (self.home / "output").mkdir()
        (self.home / "secret.txt").write_text("secret", encoding="utf-8")
        for path in ("../secret.txt", "output/../secret.txt", "/etc/passwd", str(self.home / "config.yaml"),
                     str(self.home / "browser_profile" / "Cookies")):
            status, _, body = self.get("/file?" + urlencode({"path": path}))
            self.assertIn(status, (403, 404), path)
            self.assertNotIn("secret", body)
        (self.home / "output" / "run.sh").write_text("echo hi", encoding="utf-8")   # inside output/, wrong type
        self.assertEqual(self.get("/file?" + urlencode({"path": "output/run.sh"}))[0], 404)

    # ---- profiles / config ----
    def test_profiles_page_lists_profiles_and_flags_missing_resume(self):
        self.add_profile("broken", with_tex=False)
        self.seed()
        body = self.get("/profiles")[2]
        for n in ("default", "other", "broken"):
            self.assertIn(f"/profile/{n}", body)
        broken_row = body[body.index("/profile/broken"):]
        self.assertIn("MISSING", broken_row[:200])

    def test_profile_page_shows_jd_config_and_resume(self):
        (self.home / "resume" / "default" / "target_jd.md").write_text("Mid-level SRE, Kubernetes <b>only</b>", encoding="utf-8")
        status, _, body = self.get("/profile/default")
        self.assertEqual(status, 200)
        self.assertIn("Mid-level SRE, Kubernetes &lt;b&gt;only&lt;/b&gt;", body)       # JD, escaped
        self.assertIn("sales coordinator", body)                                        # merged keywords
        self.assertIn("Bengaluru", body)
        self.assertIn("min_score", body)                                                # raw profile.yaml
        self.assertIn("Test_Resume.pdf", body)                                          # output name
        self.assertIn("\\documentclass", body)                                          # master resume.tex
        self.assertEqual(self.get("/profile/nope")[0], 404)
        self.assertEqual(self.get("/profile/..%2F..%2Fetc")[0], 404)

    def test_broken_profile_yaml_does_not_crash_the_page(self):
        (self.home / "resume" / "other" / "profile.yaml").write_text("search: [unclosed", encoding="utf-8")
        status, _, body = self.get("/profile/other")
        self.assertEqual(status, 200)
        self.assertIn("Could not load", body)

    def test_config_page(self):
        status, _, body = self.get("/config")
        self.assertEqual(status, 200)
        self.assertIn("platforms", body)
        self.assertIn("cursor-agent", body)

    # ---- misc ----
    def test_only_get_is_allowed(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("POST", "/", body="x")
        self.assertEqual(conn.getresponse().status, 405)
        conn.close()
        self.assertEqual(self.get("/nothing")[0], 404)


if __name__ == "__main__":
    unittest.main()
