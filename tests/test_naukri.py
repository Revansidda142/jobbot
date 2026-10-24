"""Naukri apply logic against a fake Playwright page (no browser, no network, nothing is ever submitted)."""
import os
import re
import sys
import unittest
from unittest import mock

from playwright.sync_api import Error as PWError

from common import Job
from sites.naukri import Naukri, _ask_user, _goto

URL = "https://www.naukri.com/job-listings-java-developer-acme-bengaluru-0-to-2-years-0910"


class FakeLocator:
    def __init__(self, n=0, visible=False, on_click=None):
        self.n, self.visible, self.on_click = n, visible, on_click

    def count(self):
        return self.n

    @property
    def first(self):
        return self

    def click(self):
        if self.on_click:
            self.on_click()

    def is_visible(self):
        return self.visible


class FakePage:
    """scenario: applies | silent | questionnaire | already | external | nobutton | late (label appears after ~4s)"""

    def __init__(self, scenario):
        self.scenario, self.url = scenario, URL
        self.applied = scenario == "already"
        self.clicks, self.waited, self.selectors = 0, 0, []

    def goto(self, url, **kw):
        self.url = url

    def wait_for_timeout(self, ms):
        self.waited += ms
        if self.scenario == "late" and self.clicks and self.waited >= 4000:
            self.applied = True
        if self.scenario == "answered" and self.clicks and self.waited >= 30000:   # the user answers after ~30s
            self.applied = True

    def _click(self):
        self.clicks += 1
        if self.scenario == "applies":
            self.applied = True

    def get_by_text(self, rx):
        return FakeLocator(1 if self.applied and rx.search("Applied") else 0)

    def locator(self, sel):
        self.selectors.append(sel)
        if sel == "#apply-button":
            has_btn = self.scenario in ("applies", "silent", "questionnaire", "late", "answered")
            return FakeLocator(1 if has_btn else 0, on_click=self._click)
        return FakeLocator(1, visible=self.scenario in ("questionnaire", "answered") and not self.applied)

    def get_by_role(self, role, name):
        if name.search("apply on company site"):
            return FakeLocator(1 if self.scenario == "external" else 0)
        return FakeLocator(0)


def apply(scenario, submit=True, wait=None, ask=None):
    """ask=None: unattended (no terminal). ask=fn(page) -> outcome: interactive, fn plays the user."""
    page = FakePage(scenario)
    job = Job("naukri", "1", "Java Developer", "Acme", "Bengaluru", URL, "1 day ago", 1440)
    cfg = {} if wait is None else {"naukri": {"questionnaire_wait_seconds": wait}}
    asked = []

    def fake_ask(job_, timeout, poll):
        asked.append(timeout)
        return ask(page)

    with mock.patch("sites.naukri._interactive", return_value=ask is not None), \
            mock.patch("sites.naukri._ask_user", fake_ask):
        status, _ = Naukri().apply(page, job, None, cfg, submit=submit)
    page.asked = asked
    return status, page


class NaukriApplyTest(unittest.TestCase):
    def test_dry_run_never_clicks(self):
        status, page = apply("applies", submit=False)
        self.assertEqual((status, page.clicks), ("dry_run_ok", 0))

    def test_applied_label_after_click_means_applied(self):
        status, page = apply("applies")
        self.assertEqual((status, page.clicks), ("applied", 1))

    def test_label_that_shows_up_a_few_seconds_late_is_still_applied(self):
        self.assertEqual(apply("late")[0], "applied")

    def test_no_label_and_no_questionnaire_is_unconfirmed(self):
        status, page = apply("silent")
        self.assertEqual((status, page.clicks), ("unconfirmed", 1))

    def test_real_questionnaire_needs_answers(self):
        self.assertEqual(apply("questionnaire")[0], "needs_answers")

    def test_questionnaire_you_answer_in_time_becomes_applied(self):
        status, page = apply("answered", wait=60)
        self.assertEqual((status, page.clicks), ("applied", 1))

    def test_questionnaire_not_answered_within_the_wait_is_needs_answers(self):
        status, page = apply("answered", wait=15)            # the user would answer after 30s
        self.assertEqual(status, "needs_answers")
        self.assertLess(page.waited, 8000 + 16000)           # it did not wait longer than configured

    def test_zero_wait_does_not_wait_for_the_questionnaire(self):
        status, page = apply("answered", wait=0)
        self.assertEqual(status, "needs_answers")
        self.assertLessEqual(page.waited, 8000)

    def test_prompt_enter_then_label_is_applied(self):
        def user(page):
            page.applied = True                              # you answered; Naukri submits
            return "done"
        status, page = apply("questionnaire", ask=user)
        self.assertEqual((status, page.asked), ("applied", [120]))

    def test_prompt_enter_but_no_label_is_needs_answers(self):
        self.assertEqual(apply("questionnaire", ask=lambda page: "done")[0], "needs_answers")

    def test_prompt_skip_is_needs_answers_without_waiting(self):
        status, page = apply("questionnaire", ask=lambda page: "skip")
        self.assertEqual(status, "needs_answers")
        self.assertLessEqual(page.waited, 8000)              # only the post-click label wait

    def test_prompt_timeout_is_needs_answers(self):
        self.assertEqual(apply("questionnaire", wait=30, ask=lambda page: "timeout")[0], "needs_answers")

    def test_prompt_sees_the_label_appear_on_its_own(self):
        status, _ = apply("questionnaire", ask=lambda page: "label")
        self.assertEqual(status, "applied")

    def test_no_prompt_when_wait_is_zero_or_unattended(self):
        status, page = apply("questionnaire", wait=0, ask=lambda page: self.fail("must not prompt"))
        self.assertEqual(status, "needs_answers")
        self.assertEqual(apply("answered", wait=60)[1].asked, [])      # unattended: polls instead, never asks

    def test_questionnaire_check_ignores_the_page_header_drawer(self):
        _, page = apply("silent")
        selectors = " ".join(s for s in page.selectors if s != "#apply-button")
        self.assertNotIn("Drawer", selectors)
        self.assertNotIn("drawer", selectors.lower())

    def test_other_outcomes(self):
        status, page = apply("already")
        self.assertEqual((status, page.clicks), ("already_applied", 0))
        self.assertEqual(apply("external")[0], "external")
        self.assertEqual(apply("nobutton")[0], "no_apply_button")


class NaukriVerifyTest(unittest.TestCase):
    def verify(self, scenario):
        page = FakePage(scenario)
        job = Job("naukri", "1", "Java Developer", "Acme", "Bengaluru", URL, "1 day ago", 1440)
        return Naukri().verify(page, job), page

    def test_applied_label_means_applied(self):
        self.assertEqual(self.verify("already")[0], "applied")

    def test_visible_apply_button_means_not_applied_and_nothing_is_clicked(self):
        result, page = self.verify("questionnaire")
        self.assertEqual((result, page.clicks), ("not_applied", 0))

    def test_no_label_and_no_button_is_unknown(self):
        self.assertEqual(self.verify("nobutton")[0], "unknown")


class AskUserTest(unittest.TestCase):
    """The real prompt, reading from a pipe that plays the terminal."""
    def ask(self, typed, poll=lambda: False, timeout=0.3):
        r, w = os.pipe()
        if typed is not None:
            os.write(w, typed.encode())
        job = Job("naukri", "1", "Java Developer", "Acme", "", URL, "", 0)
        try:
            with os.fdopen(r) as fake_stdin, mock.patch.object(sys, "stdin", fake_stdin), \
                    mock.patch("builtins.print"):
                return _ask_user(job, timeout, poll, slice_s=0.05)
        finally:
            os.close(w)

    def test_enter_is_done(self):
        self.assertEqual(self.ask("\n"), "done")
        self.assertEqual(self.ask("y\n"), "done")

    def test_s_or_n_is_skip(self):
        self.assertEqual(self.ask("s\n"), "skip")
        self.assertEqual(self.ask("N\n"), "skip")

    def test_nothing_typed_times_out(self):
        self.assertEqual(self.ask(None), "timeout")

    def test_label_appearing_ends_the_wait(self):
        self.assertEqual(self.ask(None, poll=lambda: True, timeout=5), "label")


class GotoRetryTest(unittest.TestCase):
    class Page:
        def __init__(self, errors):
            self.errors, self.calls, self.url = list(errors), 0, ""

        def goto(self, url, **kw):
            self.calls += 1
            if self.errors:
                raise PWError(self.errors.pop(0))
            self.url = url

        def wait_for_timeout(self, ms):
            pass

    def test_retries_once_when_interrupted_by_the_apply_redirect(self):
        page = self.Page(["Page.goto: Navigation to X is interrupted by another navigation to saveApply"])
        _goto(page, "http://x")
        self.assertEqual((page.calls, page.url), (2, "http://x"))

    def test_other_errors_and_repeated_interruptions_are_raised(self):
        with self.assertRaises(PWError):
            _goto(self.Page(["net::ERR_FAILED"]), "http://x")
        with self.assertRaises(PWError):
            _goto(self.Page(["interrupted by another navigation"] * 2), "http://x")


if __name__ == "__main__":
    unittest.main()
