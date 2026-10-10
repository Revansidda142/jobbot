"""SQLite record of every job the bot has looked at, so each job is handled once."""
import sqlite3
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    platform TEXT, job_id TEXT, title TEXT, company TEXT, url TEXT,
    posted_text TEXT, age_minutes INTEGER, status TEXT, score INTEGER,
    reason TEXT, pdf_path TEXT, attempts INTEGER DEFAULT 0,
    first_seen TEXT, updated TEXT,
    PRIMARY KEY (platform, job_id)
)"""


class DB:
    def __init__(self, path):
        self.conn = sqlite3.connect(str(path))
        self.conn.execute(SCHEMA)
        self.conn.commit()

    def seen(self, platform, job_id):
        """True if the job was already handled. Jobs that errored get up to 2 retries."""
        row = self.conn.execute(
            "SELECT status, attempts FROM jobs WHERE platform=? AND job_id=?", (platform, job_id)
        ).fetchone()
        if row is None:
            return False
        status, attempts = row
        return not (status == "error" and attempts < 2)

    def record(self, job, status, score=None, reason="", pdf_path=""):
        now = datetime.now().isoformat(timespec="seconds")
        self.conn.execute(
            """INSERT INTO jobs (platform, job_id, title, company, url, posted_text, age_minutes,
                                 status, score, reason, pdf_path, attempts, first_seen, updated)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?,?)
               ON CONFLICT(platform, job_id) DO UPDATE SET
                 status=excluded.status, score=excluded.score, reason=excluded.reason,
                 pdf_path=excluded.pdf_path, attempts=attempts+1, updated=excluded.updated""",
            (job.platform, job.job_id, job.title, job.company, job.url, job.posted_text,
             job.age_minutes, status, score, reason, pdf_path, now, now),
        )
        self.conn.commit()

    def applied_today(self):
        return self.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status='applied' AND date(updated)=date('now','localtime')"
        ).fetchone()[0]
