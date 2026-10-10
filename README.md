# jobbot

Setup (inside WSL, in ~/jobbot, with the venv active):
    pip install -r requirements.txt
    playwright install --with-deps chromium

Files you provide: resume/resume.tex and resume/resume.cls (already copied).

LLM backend (Cursor CLI, used for scoring and resume tailoring):
    curl https://cursor.com/install -fsS | bash
    cursor-agent login
    cursor-agent status                  # confirm you are logged in
    python main.py --llm-check           # tiny real call; prints provider, exact command, raw reply, parse result

Config (config.yaml):
    llm:
      provider: cursor        # cursor | claude
      cmd: cursor-agent
      model: null             # names from `cursor-agent models`; null = Cursor default
      timeout_seconds: 300
The cursor provider runs `cursor-agent -p --trust --mode ask --output-format text` (read-only, prompt on stdin)
in an empty temporary directory, so the model cannot see your files. An old `claude:` section with no `llm:`
section still works (provider claude). All LLM calls live in llm_client.py.

First use:
    python login.py                      # log in to LinkedIn + Naukri once, by hand
    python main.py --tailor-test jd.txt  # test the LLM + tectonic on a pasted job description (no browser)
    python main.py --force               # dry-run: nothing is submitted
    python main.py --force --mode live   # real applications

Everything you will want to change is in config.yaml.
Reports: logs/run_*.md   Tailored resumes: output/<job>/   History: data/*.db

Tests: python -m unittest
