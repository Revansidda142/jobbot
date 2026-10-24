"""Read-only local web UI: python main.py --ui  ->  http://127.0.0.1:8765

Shows which jobs were applied / need you / were skipped, and every profile's JD, config and resume.
Standard library only. It never writes anything and never starts the bot.
Everything shown from the job sites is untrusted text, so every value is HTML-escaped.
"""
import html
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

from common import list_profiles, load_profile_config, profile_dir, root

HOST = "127.0.0.1"

# (section title, statuses). Order = order on the page.
BUCKETS = [
    ("Applied", ["applied", "already_applied"]),
    ("Ready: dry-run OK (the bot would apply in live mode)", ["dry_run_ok"]),
    ("You need to apply by hand", ["external", "no_apply_button", "needs_answers", "unconfirmed"]),
    ("Skipped", ["skipped_score", "skipped_filter", "skipped_tailor_failed"]),
    ("Errors", ["error"]),
]
HAND_HELP = {"external": "only 'apply on company site'", "no_apply_button": "no Apply button found",
             "needs_answers": "form has questions the bot has no answer for",
             "unconfirmed": "clicked submit, no confirmation seen: check the site"}

CSS = """body{font:14px system-ui,sans-serif;margin:0;color:#222}header{background:#1f2937;padding:10px 20px}
header a{color:#fff;margin-right:18px;text-decoration:none}main{padding:16px 20px}
table{border-collapse:collapse;width:100%;margin:8px 0 22px}th,td{border-bottom:1px solid #ddd;padding:5px 8px;text-align:left;vertical-align:top}
th{background:#f3f4f6}h2{margin:22px 0 4px}pre{background:#f6f8fa;padding:10px;overflow:auto;max-height:420px;white-space:pre-wrap}
.muted{color:#777}.pill{background:#e5e7eb;border-radius:9px;padding:1px 8px}form{margin:8px 0}select,input,button{margin-right:8px}
details{margin:6px 0}"""


def esc(v):
    return html.escape("" if v is None else str(v), quote=True)


def safe_url(u):
    """Only http(s) links are clickable (a 'javascript:' URL from a job site must never become a link)."""
    return u if str(u).lower().startswith(("http://", "https://")) else ""


def link(href, text):
    return f'<a href="{esc(href)}">{esc(text)}</a>'


def page(title, body, db="dry"):
    nav = "".join(link(f"{p}?{urlencode({'db': db})}", t) for p, t in
                  (("/", "Jobs"), ("/profiles", "Profiles"), ("/config", "Config")))
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{esc(title)} - jobbot</title>"
            f"<style>{CSS}</style></head><body><header>{nav}</header><main><h1>{esc(title)}</h1>{body}</main></body></html>")


# ---------------- data access (read-only) ----------------
def db_file(which):
    return root() / "data" / ("jobs.db" if which == "live" else "jobs_dryrun.db")


def connect_ro(which):
    path = db_file(which)
    if not path.exists():
        return None
    con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def fetch_jobs(which, profile=None, platform=None, days=None):
    con = connect_ro(which)
    if con is None:
        return None
    sql, args = "SELECT * FROM jobs WHERE 1=1", []
    if profile:
        sql += " AND profile=?"
        args.append(profile)
    if platform:
        sql += " AND platform=?"
        args.append(platform)
    if days:
        sql += " AND date(updated) >= date('now','localtime',?)"
        args.append(f"-{int(days)} days")
    try:
        return con.execute(sql + " ORDER BY updated DESC", args).fetchall()
    finally:
        con.close()


def under(path, base):
    """True if path (resolved) is inside base."""
    try:
        Path(path).resolve().relative_to(Path(base).resolve())
        return True
    except (ValueError, OSError):
        return False


def file_link(path, text):
    return link("/file?" + urlencode({"path": str(path)}), text)


# ---------------- pages ----------------
def jobs_page(qs):
    which = "live" if qs.get("db") == "live" else "dry"
    profile, platform = qs.get("profile") or None, qs.get("platform") or None
    days = int(qs["days"]) if qs.get("days", "").isdigit() else None
    rows = fetch_jobs(which, profile, platform, days)

    profiles = list_profiles()
    form = ("<form method='get'>"
            f"<select name='db'><option value='dry'{' selected' if which == 'dry' else ''}>dry-run DB</option>"
            f"<option value='live'{' selected' if which == 'live' else ''}>live DB</option></select>"
            "<select name='profile'><option value=''>all profiles</option>"
            + "".join(f"<option{' selected' if p == profile else ''}>{esc(p)}</option>" for p in profiles) + "</select>"
            "<select name='platform'><option value=''>all platforms</option>"
            + "".join(f"<option{' selected' if p == platform else ''}>{p}</option>" for p in ("linkedin", "naukri"))
            + f"</select><input name='days' size='3' placeholder='days' value='{esc(days or '')}'>"
            "<button>Filter</button></form>")
    if rows is None:
        return page("Jobs", form + f"<p class='muted'>No history yet ({esc(db_file(which).name)} does not exist).</p>", which)

    known = {s for _, ss in BUCKETS for s in ss}
    sections = [(t, [r for r in rows if r["status"] in ss]) for t, ss in BUCKETS]
    other = [r for r in rows if r["status"] not in known]
    if other:
        sections.append(("Other", other))
    body = form + f"<p class='muted'>{len(rows)} jobs in {esc(db_file(which).name)}</p>"
    for title, items in sections:
        table = job_table(items, which)
        count = f"<span class='pill'>{len(items)}</span>"
        if title == "Skipped":
            body += f"<details><summary><b>{esc(title)}</b> {count}</summary>{table}</details>"
        else:
            body += f"<h2>{esc(title)} {count}</h2>{table}"
    return page("Jobs", body, which)


def job_table(items, which):
    if not items:
        return "<p class='muted'>none</p>"
    out = ["<table><tr><th>Updated</th><th>Profile</th><th>Platform</th><th>Company</th><th>Title</th>"
           "<th>Score</th><th>Status</th><th>Tailored</th><th>Job page</th></tr>"]
    for r in items:
        detail = "/job?" + urlencode({"db": which, "platform": r["platform"], "id": r["job_id"], "profile": r["profile"]})
        url = safe_url(r["url"])
        note = HAND_HELP.get(r["status"], "")
        out.append(
            f"<tr><td>{esc((r['updated'] or '')[:16].replace('T', ' '))}</td><td>{esc(r['profile'])}</td>"
            f"<td>{esc(r['platform'])}</td><td>{esc(r['company'])}</td><td>{link(detail, r['title'] or '(no title)')}</td>"
            f"<td>{esc(r['score'])}</td><td title='{esc(r['reason'])}'>{esc(r['status'])}"
            f"{'<br><span class=muted>' + esc(note) + '</span>' if note else ''}</td>"
            f"<td>{'yes' if r['tailored'] else 'no'}</td>"
            f"<td>{link(url, 'open') if url else ''}</td></tr>")
    return "".join(out) + "</table>"


def job_page(qs):
    which = "live" if qs.get("db") == "live" else "dry"
    con = connect_ro(which)
    row = None
    if con is not None:
        row = con.execute("SELECT * FROM jobs WHERE platform=? AND job_id=? AND profile=?",
                          (qs.get("platform", ""), qs.get("id", ""), qs.get("profile", ""))).fetchone()
        con.close()
    if row is None:
        return None
    body = "<table>" + "".join(f"<tr><th>{esc(k)}</th><td>{esc(row[k])}</td></tr>" for k in row.keys()
                               if k not in ("job_folder", "resume_pdf")) + "</table>"
    url = safe_url(row["url"])
    if url:
        body += f"<p>{link(url, 'Open the job page')}</p>"
    folder = Path(row["job_folder"]) if row["job_folder"] else None
    if folder and under(folder, root() / "output") and folder.is_dir():
        body += f"<h2>Tailored resume folder</h2><p class='muted'>{esc(folder)}</p><ul>"
        for pdf in sorted(folder.glob("*.pdf")):
            if pdf.name != "resume.pdf":
                body += f"<li>{file_link(pdf, pdf.name)}</li>"
        body += "</ul>"
        for name, label in (("job.txt", "Job description"), ("meta.json", "meta.json"),
                            ("changes.diff", "Resume changes (master to tailored)"), ("FAILED.txt", "FAILED.txt")):
            f = folder / name
            if f.exists():
                body += f"<details><summary>{esc(label)}</summary><pre>{esc(f.read_text(encoding='utf-8', errors='replace'))}</pre></details>"
    elif row["resume_pdf"]:
        body += f"<p>Resume used: {esc(row['resume_pdf'])} (no per-job folder)</p>"
    return page(row["title"] or "Job", body, which)


def read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def status_counts(which, profile):
    rows = fetch_jobs(which, profile) or []
    c = {k: 0 for k in ("applied", "dry_run_ok", "by_hand", "skipped", "error")}
    hand = set(BUCKETS[2][1])
    skipped = set(BUCKETS[3][1])
    for r in rows:
        s = r["status"]
        key = "applied" if s in ("applied", "already_applied") else "dry_run_ok" if s == "dry_run_ok" else \
            "by_hand" if s in hand else "skipped" if s in skipped else "error" if s == "error" else None
        if key:
            c[key] += 1
    return c


def profiles_page(qs):
    which = "live" if qs.get("db") == "live" else "dry"
    names = list_profiles()
    if not names:
        return page("Profiles", "<p class='muted'>No profiles. Create one: python main.py --new-profile NAME</p>", which)
    out = ["<table><tr><th>Profile</th><th>resume.tex</th><th>Applied</th><th>Dry-run OK</th><th>Apply by hand</th>"
           f"<th>Skipped</th><th>Errors</th></tr>"]
    for n in names:
        c = status_counts(which, n)
        has = (profile_dir(n) / "resume.tex").exists()
        out.append(f"<tr><td>{link('/profile/' + quote(n) + '?' + urlencode({'db': which}), n)}</td>"
                   f"<td>{'ok' if has else '<b>MISSING</b>'}</td><td>{c['applied']}</td><td>{c['dry_run_ok']}</td>"
                   f"<td>{c['by_hand']}</td><td>{c['skipped']}</td><td>{c['error']}</td></tr>")
    return page("Profiles", f"<p class='muted'>Counts from the {'live' if which == 'live' else 'dry-run'} database.</p>"
                + "".join(out) + "</table>", which)


def profile_page(name, qs):
    which = "live" if qs.get("db") == "live" else "dry"
    if name not in list_profiles():
        return None
    pdir = profile_dir(name)
    body = f"<p>{link('/?' + urlencode({'db': which, 'profile': name}), 'Show jobs for this profile')}</p>"
    try:
        cfg = load_profile_config(name)
        s, f = cfg["search"], cfg.get("filters", {})
        rows = [("keywords", ", ".join(s.get("keywords") or [])), ("location", s.get("location")),
                ("geo_id", s.get("geo_id")), ("max_age_hours", s.get("max_age_hours")), ("pages", s.get("pages")),
                ("min_score", cfg["scoring"].get("min_score")), ("scoring notes", cfg["scoring"].get("notes")),
                ("title must contain any of", ", ".join(f.get("title_include_any") or [])),
                ("title must not contain", ", ".join(f.get("title_exclude") or [])),
                ("company blacklist", ", ".join(f.get("company_blacklist") or [])),
                ("screening answers", len(cfg.get("answers") or [])),
                ("output file name", cfg["resume"].get("output_name")),
                ("protected strings", ", ".join(cfg["resume"].get("protected_strings") or [])),
                ("on tailor failure", cfg["resume"].get("on_tailor_failure")),
                ("max applications per run / day", f"{cfg['limits'].get('max_applications_per_run')} / "
                                                   f"{cfg['limits'].get('max_applications_per_day')}")]
        body += "<h2>Merged settings (config.yaml + profile.yaml)</h2><table>" + "".join(
            f"<tr><th>{esc(k)}</th><td>{esc(v)}</td></tr>" for k, v in rows) + "</table>"
        answers = cfg.get("answers") or []
        if answers:
            body += "<h2>Screening answers</h2><table><tr><th>question matches</th><th>value</th></tr>" + "".join(
                f"<tr><td>{esc(a.get('match'))}</td><td>{esc(a.get('value'))}</td></tr>" for a in answers) + "</table>"
    except Exception as e:  # noqa: BLE001  (e.g. broken YAML: show the problem instead of a crash)
        body += f"<p><b>Could not load this profile's config:</b> {esc(e)}</p>"

    jd = read_text(pdir / "target_jd.md")
    body += "<h2>Target role (target_jd.md)</h2>" + (f"<pre>{esc(jd)}</pre>" if jd else "<p class='muted'>none</p>")
    yml = read_text(pdir / "profile.yaml")
    body += "<h2>profile.yaml</h2>" + (f"<pre>{esc(yml)}</pre>" if yml else "<p class='muted'>none</p>")
    tex = read_text(pdir / "resume.tex")
    body += "<h2>Master resume</h2>"
    if tex is None:
        body += "<p><b>resume.tex is missing</b></p>"
    else:
        body += f"<details><summary>resume.tex</summary><pre>{esc(tex)}</pre></details>"
    for pdf in sorted((root() / "output" / name / "_master").glob("*.pdf")):
        body += f"<p>{file_link(pdf, 'Master PDF: ' + pdf.name)}</p>"
    return page(f"Profile: {name}", body, which)


def config_page(qs):
    which = "live" if qs.get("db") == "live" else "dry"
    text = read_text(root() / "config.yaml")
    if text is None:
        return page("Config", "<p class='muted'>config.yaml not found</p>", which)
    return page("Config", f"<p class='muted'>Shared settings (config.yaml). Profile files override parts of this.</p>"
                          f"<pre>{esc(text)}</pre>", which)


# ---------------- server ----------------
TEXT_SUFFIXES = {".txt", ".json", ".diff", ".tex", ".md", ".yaml"}


class Handler(BaseHTTPRequestHandler):
    server_version = "jobbot-ui"

    def log_message(self, *args):  # keep the terminal quiet
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8", html_page=True):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        if html_page:
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def _not_found(self, msg="Not found"):
        self._send(404, page("Not found", f"<p>{esc(msg)}</p>"))

    def do_GET(self):
        url = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path == "/":
                out = jobs_page(qs)
            elif url.path == "/job":
                out = job_page(qs)
            elif url.path == "/profiles":
                out = profiles_page(qs)
            elif url.path.startswith("/profile/"):
                out = profile_page(unquote(url.path[len("/profile/"):]), qs)
            elif url.path == "/config":
                out = config_page(qs)
            elif url.path == "/file":
                return self._file(qs.get("path", ""))
            else:
                return self._not_found()
            if out is None:
                return self._not_found()
            self._send(200, out)
        except Exception as e:  # noqa: BLE001
            self._send(500, page("Error", f"<p>{esc(e)}</p>"))

    def _file(self, raw):
        path = Path(raw)
        path = path if path.is_absolute() else root() / path
        allowed = under(path, root() / "output") or under(path, root() / "resume")
        if not allowed:
            return self._send(403, "forbidden", "text/plain; charset=utf-8", html_page=False)
        suffix = path.suffix.lower()
        if suffix != ".pdf" and suffix not in TEXT_SUFFIXES or not path.is_file():
            return self._send(404, "not found", "text/plain; charset=utf-8", html_page=False)
        ctype = "application/pdf" if suffix == ".pdf" else "text/plain; charset=utf-8"
        self._send(200, path.read_bytes(), ctype, html_page=False)

    def _method_not_allowed(self):
        self._send(405, "read-only UI: only GET is supported", "text/plain; charset=utf-8", html_page=False)

    do_POST = do_PUT = do_DELETE = do_PATCH = _method_not_allowed


def make_server(port=8765):
    return ThreadingHTTPServer((HOST, port), Handler)


def serve(port=8765):
    server = make_server(port)
    print(f"jobbot UI: http://{HOST}:{server.server_address[1]}  (read-only; Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0
