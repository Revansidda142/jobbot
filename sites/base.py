class Adapter:
    """One job site. Selectors live in the adapter so a site redesign only means editing one file."""
    name = ""
    supports_resume_upload = False  # True if each application can carry its own resume file

    def ensure_logged_in(self, page) -> bool:
        raise NotImplementedError

    def collect(self, page, cfg) -> list:
        """Search all keywords; return Job objects (last N hours)."""
        raise NotImplementedError

    def get_description(self, page, job) -> str:
        raise NotImplementedError

    def apply(self, page, job, pdf_path, cfg, submit: bool):
        """Return (status, detail). status: applied | dry_run_ok | needs_answers | external |
        no_apply_button | already_applied | unconfirmed | error"""
        raise NotImplementedError

    def verify(self, page, job) -> str:
        """Re-open the job later and say whether the application went through.
        Returns 'applied' | 'not_applied' | 'unknown'. Sites that cannot tell keep the default."""
        return "unknown"
