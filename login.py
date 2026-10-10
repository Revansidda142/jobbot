"""One-time (and whenever a session expires): log in by hand in the bot's own browser profile."""
from playwright.sync_api import sync_playwright

from common import root

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(str(root() / "browser_profile"), headless=False,
                                               viewport={"width": 1366, "height": 900})
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto("https://www.linkedin.com/login")
    naukri = ctx.new_page()
    naukri.goto("https://www.naukri.com/nlogin/login")
    input("\nLog in to LinkedIn (tab 1) and Naukri (tab 2) in the browser window, "
          "then come back here and press Enter... ")
    ctx.close()
print("Saved. You can now run: python main.py")
