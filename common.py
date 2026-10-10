"""Shared helpers: config, Job model, 'posted X ago' parsing, delays, logging."""
import logging
import os
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

CODE_DIR = Path(__file__).resolve().parent
log = logging.getLogger("jobbot")


def root():
    """Project root: $JOBBOT_HOME if set (tests use a temp dir), else the folder containing the code."""
    return Path(os.environ.get("JOBBOT_HOME") or CODE_DIR).resolve()


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
    path = Path(path) if path else root() / "config.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ---------------- profiles ----------------
class ProfileError(Exception):
    """Unknown/missing/invalid profile; the message is shown to the user as-is."""


# keys a profile.yaml may override (everything else stays in config.yaml)
PROFILE_KEYS = ("search", "filters", "scoring", "answers", "resume", "limits")


def profiles_dir():
    return root() / "resume"


def profile_dir(name):
    return profiles_dir() / name


def list_profiles():
    """Names of all profile folders (sub-folders of resume/), sorted."""
    base = profiles_dir()
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir() and not p.name.startswith((".", "_")))


def resolve_profile(name=None):
    """Validate/choose the profile name. One profile only -> used automatically."""
    names = list_profiles()
    if name is None:
        if len(names) == 1:
            return names[0]
        raise ProfileError("no profile selected. Use --profile <name>. Available: "
                           + (", ".join(names) or "(none - create one with --new-profile <name>)"))
    if name not in names:
        raise ProfileError(f"unknown profile {name!r}. Available: " + (", ".join(names) or "(none)"))
    return name


def deep_merge(base, over):
    """Dicts merge key by key; anything else (lists, scalars) in `over` replaces `base`."""
    out = dict(base)
    for k, v in over.items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_profile_config(name):
    """config.yaml deep-merged with resume/<name>/profile.yaml. Adds cfg['profile'] and cfg['target_jd']."""
    name = resolve_profile(name)
    pdir = profile_dir(name)
    cfg = load_config()
    yml = pdir / "profile.yaml"
    over = (yaml.safe_load(yml.read_text(encoding="utf-8")) or {}) if yml.exists() else {}
    for k in over:
        if k not in PROFILE_KEYS:
            log.warning("profile %s: ignoring %r (not allowed in profile.yaml; allowed: %s)", name, k, ", ".join(PROFILE_KEYS))
    cfg = deep_merge(cfg, {k: v for k, v in over.items() if k in PROFILE_KEYS})
    jd = pdir / "target_jd.md"
    cfg["profile"] = name
    cfg["target_jd"] = jd.read_text(encoding="utf-8").strip() if jd.exists() else ""
    return cfg


def profile_master_tex(cfg):
    return profile_dir(cfg["profile"]) / cfg["resume"].get("tex_file", "resume.tex")


def setup_logging():
    (root() / "logs").mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(root() / "logs" / "jobbot.log", encoding="utf-8")],
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
