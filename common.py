"""Shared helpers: config, Job model, 'posted X ago' parsing, delays, logging."""
import logging
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("jobbot")


class BlockedError(Exception):
    """Raised when a site shows a login wall, captcha or security checkpoint."""


@dataclass
class Job:
    platform: str
    job_id: str
    title: str
    company: str
    location: str
    url: str
    posted_text: str
    age_minutes: int
    description: str = ""


def load_config(path=None):
    path = Path(path) if path else ROOT / "config.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def setup_logging():
    (ROOT / "logs").mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(ROOT / "logs" / "jobbot.log", encoding="utf-8")],
    )


_UNIT_MINUTES = {"second": 0, "sec": 0, "minute": 1, "min": 1, "hour": 60, "hr": 60,
                 "day": 1440, "week": 10080, "month": 43200}
UNKNOWN_AGE = 10 ** 6


def parse_age_minutes(text):
    """'3 minutes ago' -> 3, 'Reposted 2 hours ago' -> 120, 'Just now' -> 0, '30+ days ago' -> 43200."""
    t = (text or "").lower().strip()
    if not t:
        return UNKNOWN_AGE
    if re.search(r"\bjust now\b|moments? ago|few seconds", t):
        return 0
    m = re.search(r"(\d+)\+?\s*(second|sec|minute|min|hour|hr|day|week|month)s?\b", t)
    if m:
        return int(m.group(1)) * _UNIT_MINUTES[m.group(2)]
    if "few hour" in t:
        return 180
    if "today" in t:
        return 720
    if "yesterday" in t:
        return 1440
    return UNKNOWN_AGE


def human_pause(cfg, factor=1.0):
    lo, hi = cfg["limits"]["delay_seconds"]
    time.sleep(random.uniform(lo, hi) * factor)


def slugify(text, maxlen=40):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:maxlen] or "x"
