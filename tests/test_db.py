import csv
import sqlite3
import unittest
from pathlib import Path
import tempfile
import shutil

from common import Job
from db import CSV_COLUMNS, DB, append_application_csv

OLD_SCHEMA = """CREATE TABLE jobs (
    platform TEXT, job_id TEXT, title TEXT, company TEXT, url TEXT,
    posted_text TEXT, age_minutes INTEGER, status TEXT, score INTEGER,
    reason TEXT, pdf_path TEXT, attempts INTEGER DEFAULT 0,
    first_seen TEXT, updated TEXT, PRIMARY KEY (platform, job_id))"""


def job(job_id="1", platform="linkedin"):
    return Job(platform, job_id, "Sales Coordinator", "Acme", "Bengaluru", "http://x", "1 hour ago", 60, "d")


class TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jobbot_db_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)


class MigrationTest(TmpCase):
    def test_old_schema_is_rebuilt(self):
        path = self.tmp / "old.db"
        con = sqlite3.connect(path)
        con.execute(OLD_SCHEMA)
        ins = "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        con.execute(ins, ("linkedin", "1", "T", "Acme", "u", "p", 5, "applied", 90, "r", "/old/a.pdf", 1, "t", "t"))
        con.execute(ins, ("naukri", "2", "T", "Beta", "u", "p", 5, "skipped_score", 10, "r", "", 1, "t", "t"))
        con.commit()
        con.close()

        db = DB(path)
        cols = [r[1] for r in db.conn.execute("PRAGMA table_info(jobs)")]
        for c in ("profile", "resume_pdf", "job_folder", "tailored"):
            self.assertIn(c, cols)
        rows = db.conn.execute("SELECT platform, job_id, profile, status, score, resume_pdf, tailored "
                               "FROM jobs ORDER BY job_id").fetchall()
        self.assertEqual(rows, [("linkedin", "1", "default", "applied", 90, "/old/a.pdf", 0),
                                ("naukri", "2", "default", "skipped_score", 10, "", 0)])
        self.assertEqual([r[1] for r in db.conn.execute("PRAGMA table_info(jobs)") if r[5]],  # primary key columns
                         ["platform", "job_id", "profile"])
        # new key: the same job may have a row per profile
        db.record(job("2", "naukri"), "dry_run_ok", 80, "r", profile="other")
        self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 3)
        # reopening a migrated DB is a no-op
        self.assertEqual(DB(path).conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 3)

    def test_fresh_db_has_new_schema(self):
        db = DB(self.tmp / "new.db")
        self.assertIn("job_folder", [r[1] for r in db.conn.execute("PRAGMA table_info(jobs)")])


class DedupTest(TmpCase):
    def setUp(self):
        super().setUp()
        self.db = DB(self.tmp / "d.db")

    def test_applied_blocks_every_profile(self):
        self.db.record(job(), "applied", 90, profile="a")
        self.assertTrue(self.db.seen("linkedin", "1", "a"))
        self.assertTrue(self.db.seen("linkedin", "1", "b"))
        self.assertFalse(self.db.seen("linkedin", "2", "b"))      # other job unaffected
        self.assertFalse(self.db.seen("naukri", "1", "b"))        # other platform unaffected

    def test_all_job_level_outcomes_block_every_profile(self):
        for n, st in enumerate(["already_applied", "unconfirmed", "external", "no_apply_button"]):
            self.db.record(job(str(n)), st, profile="a")
            self.assertTrue(self.db.seen("linkedin", str(n), "b"), st)

    def test_profile_level_outcomes_do_not_block_other_profiles(self):
        for n, st in enumerate(["skipped_filter", "skipped_score", "skipped_tailor_failed", "needs_answers", "dry_run_ok"]):
            self.db.record(job(str(n)), st, profile="a")
            self.assertTrue(self.db.seen("linkedin", str(n), "a"), st)       # still blocks its own profile
            self.assertFalse(self.db.seen("linkedin", str(n), "b"), st)      # but not the other one

    def test_error_retry_rule(self):
        self.db.record(job(), "error", profile="a")
        self.assertFalse(self.db.seen("linkedin", "1", "a"))      # 1 attempt -> retry
        self.db.record(job(), "error", profile="a")
        self.assertTrue(self.db.seen("linkedin", "1", "a"))       # 2 attempts -> give up
        self.assertFalse(self.db.seen("linkedin", "1", "b"))

    def test_history_filters(self):
        self.db.record(job("1"), "skipped_score", 10, profile="a")
        self.db.record(job("2"), "dry_run_ok", 80, profile="b", resume_pdf="/x.pdf", tailored=1)
        self.assertEqual(len(self.db.history()), 2)
        rows = self.db.history(profile="b")
        self.assertEqual([(r[1], r[6], r[7]) for r in rows], [("b", "dry_run_ok", 1)])
        self.assertEqual(len(self.db.history(days=1)), 2)


class CsvTest(TmpCase):
    def test_append_only_header_once(self):
        path = self.tmp / "data" / "applications.csv"
        append_application_csv(path, job("1"), "default", "applied", 90, True, "/p.pdf", "/folder")
        first = path.read_text(encoding="utf-8")
        append_application_csv(path, job("2"), "sales", "applied", 85, False, "/m.pdf", "")
        text = path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(first))                       # nothing before was rewritten
        rows = list(csv.reader(text.splitlines()))
        self.assertEqual(rows[0], CSV_COLUMNS)
        self.assertEqual(sum(1 for r in rows if r == CSV_COLUMNS), 1)  # header exactly once
        self.assertEqual(len(rows), 3)
        d = dict(zip(CSV_COLUMNS, rows[1]))
        self.assertEqual((d["profile"], d["platform"], d["company"], d["title"], d["location"], d["job_url"],
                          d["posted_text"], d["score"], d["status"], d["tailored"], d["resume_pdf"], d["job_folder"]),
                         ("default", "linkedin", "Acme", "Sales Coordinator", "Bengaluru", "http://x", "1 hour ago",
                          "90", "applied", "1", "/p.pdf", "/folder"))
        self.assertEqual((d["notes"], d["outcome"]), ("", ""))
        self.assertEqual(dict(zip(CSV_COLUMNS, rows[2]))["tailored"], "0")

    def test_existing_hand_edited_file_is_preserved(self):
        path = self.tmp / "applications.csv"
        edited = ",".join(CSV_COLUMNS) + "\r\n2026-01-01 10:00:00,default,linkedin,X,Y,,,,80,applied,1,,,my note,interview\r\n"
        path.write_text(edited, encoding="utf-8", newline="")
        append_application_csv(path, job("9"), "default", "applied", 70, 1, "", "")
        self.assertTrue(path.read_bytes().startswith(edited.encode()))
        self.assertEqual(len(list(csv.reader(path.read_text(encoding="utf-8").splitlines()))), 3)


if __name__ == "__main__":
    unittest.main()
