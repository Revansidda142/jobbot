"""SQLite record of every job the bot looked at (source of truth) + append-only CSV of applications."""
import csv
import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    platform TEXT, job_id TEXT, profile TEXT DEFAULT 'default',
    title TEXT, company TEXT, url TEXT,
    posted_text TEXT, age_minutes INTEGER, status TEXT, score INTEGER,
    reason TEXT, resume_pdf TEXT, job_folder TEXT, tailored INTEGER DEFAULT 0,
    attempts INTEGER DEFAULT 0, first_seen TEXT, updated TEXT,
    PRIMARY KEY (platform, job_id, profile)
)"""

# Outcomes that describe the JOB, not the profile: once one of these exists, no profile may touch the job again.
BLOCKS_ALL_PROFILES = {"applied", "already_applied", "unconfirmed", "external", "no_apply_button"}

CSV_COLUMNS = ["date_time", "profile", "platform", "company", "title", "location", "job_url", "posted_text",
               "score", "status", "tailored", "resume_pdf", "job_folder", "notes", "outcome"]


class DB:
    def __init__(self, path):
        self.conn = sqlite3.connect(str(path))
        self._migrate()
        self.conn.execute(SCHEMA)
        self.conn.commit()

    def _migrate(self):
        """Old databases (key = platform, job_id; column pdf_path) are rebuilt; their rows become profile 'default'."""
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(jobs)")]
        if not cols or "profile" in cols:
            return
        with self.conn:
            self.conn.execute("ALTER TABLE jobs RENAME TO jobs_old")
            self.conn.execute(SCHEMA)
            self.conn.execute(
                """INSERT INTO jobs (platform, job_id, profile, title, company, url, posted_text, age_minutes,
                                     status, score, reason, resume_pdf, job_folder, tailored, attempts,
                                     first_seen, updated)
                   SELECT platform, job_id, 'default', title, company, url, posted_text, age_minutes,
                          status, score, reason, pdf_path, '', 0, attempts, first_seen, updated
                   FROM jobs_old""")
            self.conn.execute("DROP TABLE jobs_old")

    def seen(self, platform, job_id, profile):
        """True if this profile should skip the job.
        Job-level outcomes (applied, ...) block every profile; the rest only block the profile that got them.
        Jobs that errored get up to 2 retries."""
        rows = self.conn.execute("SELECT profile, status, attempts FROM jobs WHERE platform=? AND job_id=?",
                                 (platform, job_id)).fetchall()
        for p, status, attempts in rows:
            if status in BLOCKS_ALL_PROFILES:
                return True
            if p == profile and not (status == "error" and attempts < 2):
                return True
        return False

    def record(self, job, status, score=None, reason="", profile="default", resume_pdf="", job_folder="", tailored=0):
        now = datetime.now().isoformat(timespec="seconds")
        self.conn.execute(
            """INSERT INTO jobs (platform, job_id, profile, title, company, url, posted_text, age_minutes,
                                 status, score, reason, resume_pdf, job_folder, tailored, attempts, first_seen, updated)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)
               ON CONFLICT(platform, job_id, profile) DO UPDATE SET
                 status=excluded.status, score=excluded.score, reason=excluded.reason,
                 resume_pdf=excluded.resume_pdf, job_folder=excluded.job_folder, tailored=excluded.tailored,
                 attempts=attempts+1, updated=excluded.updated""",
            (job.platform, job.job_id, profile, job.title, job.company, job.url, job.posted_text,
             job.age_minutes, status, score, reason, str(resume_pdf or ""), str(job_folder or ""),
             int(bool(tailored)), now, now),
        )
        self.conn.commit()

    def applied_today(self):
        return self.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status='applied' AND date(updated)=date('now','localtime')"
        ).fetchone()[0]

    def history(self, profile=None, days=None):
        """Rows (newest first) for the --history table."""
        sql = ("SELECT updated, profile, platform, company, title, score, status, tailored, resume_pdf "
               "FROM jobs WHERE 1=1")
        args = []
        if profile:
            sql += " AND profile=?"
            args.append(profile)
        if days:
            sql += " AND date(updated) >= date('now','localtime',?)"
            args.append(f"-{int(days)} days")
        return self.conn.execute(sql + " ORDER BY updated DESC", args).fetchall()


def append_application_csv(path, job, profile, status, score, tailored, resume_pdf, job_folder):
    """Append ONE row. Creates the file with a header if missing; never rewrites or truncates it.
    `notes` and `outcome` are left empty for you to fill in by hand."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    need_header = not path.exists() or path.stat().st_size == 0
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if need_header:
            w.writerow(CSV_COLUMNS)
        w.writerow([datetime.now().isoformat(sep=" ", timespec="seconds"), profile, job.platform, job.company,
                    job.title, job.location, job.url, job.posted_text, score, status, int(bool(tailored)),
                    str(resume_pdf or ""), str(job_folder or ""), "", ""])
