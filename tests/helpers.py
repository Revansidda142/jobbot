"""Test helpers: a fake `cursor-agent`, a compile command, and a temp project root."""
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from common import deep_merge

CODE_DIR = Path(__file__).resolve().parent.parent

# Behaviour is chosen by the FAKE_MODE environment variable.
FAKE_AGENT = r"""#!/usr/bin/env bash
# Fake cursor-agent. Records how it was called, then answers like the real tool.
echo "$@" > "$FAKE_LOG.args"
pwd > "$FAKE_LOG.cwd"
ls -A | wc -l > "$FAKE_LOG.cwdfiles"
cat > "$FAKE_LOG.stdin"
case "${FAKE_MODE:-ok}" in
  fail)  echo "boom: something broke" >&2; exit 3 ;;
  auth)  echo "Error: Not logged in. Please log in." >&2; exit 1 ;;
  empty) exit 0 ;;
  sleep) sleep 5; echo OK ;;
  notex) echo "sorry, here is no tex"; exit 0 ;;
  ansi)  printf '\033[1mOK\033[0m\n'; exit 0 ;;
esac
if grep -q "tailor a LaTeX resume" "$FAKE_LOG.stdin"; then
  echo "Sure, here you go:"
  echo "<tex>"
  sed -n '/<master_resume>/,/<\/master_resume>/p' "$FAKE_LOG.stdin" | sed '1d;$d' | sed 's/^\\end{document}/% tailored\n\\end{document}/'
  echo "</tex>"
elif grep -q "screening a job" "$FAKE_LOG.stdin"; then
  printf 'Here is my verdict:\n```json\n{"score": 83, "reason": "good fit"}\n```\nthanks\n'
elif grep -q '"score": 42' "$FAKE_LOG.stdin"; then
  echo '{"score": 42, "reason": "check"}'
else
  echo OK
fi
"""

# Fake LaTeX compiler: writes a valid 1-page resume.pdf. Used only when tectonic is not on PATH.
FAKE_COMPILE = ("from pypdf import PdfWriter; w = PdfWriter(); w.add_blank_page(612, 792); "
                "w.write('resume.pdf')")


def compile_cmd():
    if shutil.which("tectonic"):
        return ["tectonic", "resume.tex"]
    return [sys.executable, "-c", FAKE_COMPILE]


def make_agent(tmp):
    """Create the fake cursor-agent in tmp; returns (script_path, log_prefix)."""
    script = Path(tmp) / "cursor-agent"
    script.write_text(FAKE_AGENT, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script), str(Path(tmp) / "fake")


def make_cfg(agent, **llm):
    """Minimal in-memory config for llm_client tests."""
    return {
        "llm": {"provider": "cursor", "cmd": agent, "model": None, "timeout_seconds": 60, **llm},
        "search": {"keywords": ["sales coordinator"]},
        "scoring": {"min_score": 70, "max_description_chars": 6000, "notes": ""},
    }


PROFILE_DEFAULTS = {
    "search": {"keywords": ["sales coordinator"], "location": "Bengaluru", "max_age_hours": 24, "pages": 1},
    "filters": {"title_include_any": [], "title_exclude": [], "company_blacklist": []},
    "scoring": {"min_score": 70, "notes": ""},
    "answers": [],
    "resume": {"output_name": "Test_Resume.pdf", "on_tailor_failure": "use_master",
               "protected_strings": ["Shradha Biradar"]},
}


class HomeTestCase(unittest.TestCase):
    """Runs every test under a temp JOBBOT_HOME with the fake cursor-agent. Real data/, output/ are never touched."""

    profiles = ("default",)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jobbot_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        (self.home / "resume").mkdir(parents=True)
        agent, self.log = make_agent(self.tmp)
        cfg = {"platforms": ["linkedin", "naukri"], "mode": "dry-run", "active_hours": [0, 24],
               "scoring": {"min_score": 70, "max_description_chars": 6000},
               "limits": {"max_applications_per_run": 15, "max_applications_per_day": 40, "delay_seconds": [0, 0]},
               "resume": {"tex_file": "resume.tex", "compile_cmd": compile_cmd()},
               "llm": {"provider": "cursor", "cmd": agent, "model": None, "timeout_seconds": 60}}
        (self.home / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
        for name in self.profiles:
            self.add_profile(name)
        env = mock.patch.dict(os.environ, {"JOBBOT_HOME": str(self.home), "FAKE_LOG": self.log, "FAKE_MODE": "ok"})
        env.start()
        self.addCleanup(env.stop)
        quiet = mock.patch("main.setup_logging")  # no log files / handlers during tests
        quiet.start()
        self.addCleanup(quiet.stop)

    def add_profile(self, name, with_tex=True, **override):
        pdir = self.home / "resume" / name
        pdir.mkdir(parents=True)
        if with_tex:
            shutil.copy(CODE_DIR / "resume" / "default" / "resume.tex", pdir / "resume.tex")
        shutil.copy(CODE_DIR / "resume" / "default" / "resume.cls", pdir / "resume.cls")
        (pdir / "profile.yaml").write_text(yaml.safe_dump(deep_merge(PROFILE_DEFAULTS, override)), encoding="utf-8")
        return pdir

    def mode(self, m):
        return mock.patch.dict(os.environ, {"FAKE_MODE": m})

    def llm_called(self):
        return Path(self.log + ".args").exists()

    def forget_llm_call(self):
        for ext in (".args", ".stdin", ".cwd", ".cwdfiles"):
            Path(self.log + ext).unlink(missing_ok=True)
