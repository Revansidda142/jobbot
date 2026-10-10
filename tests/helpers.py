"""Test helpers: a fake `cursor-agent`, a compile command, and a temp project root."""
import shutil
import stat
import sys
import tempfile
from pathlib import Path

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


def make_root():
    """Temp project root holding a copy of the resume files; nothing real is ever written to."""
    root = Path(tempfile.mkdtemp(prefix="jobbot_test_"))
    (root / "resume").mkdir()
    for f in ("resume.tex", "resume.cls"):
        shutil.copy(CODE_DIR / "resume" / f, root / "resume" / f)
    return root


def make_cfg(agent, **llm):
    return {
        "llm": {"provider": "cursor", "cmd": agent, "model": None, "timeout_seconds": 60, **llm},
        "search": {"keywords": ["sales coordinator"]},
        "scoring": {"min_score": 70, "max_description_chars": 6000, "notes": ""},
        "resume": {"master_dir": "resume", "tex_file": "resume.tex", "compile_cmd": compile_cmd(),
                   "output_name": "Test_Resume.pdf", "protected_strings": ["Shradha Biradar"],
                   "on_tailor_failure": "use_master"},
    }
