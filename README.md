# jobbot

Setup (inside WSL, in ~/jobbot, with the venv active):
    pip install -r requirements.txt
    playwright install --with-deps chromium

Files you provide: resume/resume.tex and resume/resume.cls (already copied).

First use:
    python login.py                      # log in to LinkedIn + Naukri once, by hand
    python main.py --tailor-test jd.txt  # test Claude + tectonic on a pasted job description (no browser)
    python main.py --force               # dry-run: nothing is submitted
    python main.py --force --mode live   # real applications

Everything you will want to change is in config.yaml.
Reports: logs/run_*.md   Tailored resumes: output/<job>/   History: data/*.db
