import contextlib
import io
import unittest
from pathlib import Path

import main
from common import ProfileError, deep_merge, list_profiles, load_profile_config, resolve_profile
from llm_client import score_job
from tests.helpers import HomeTestCase
from tests.test_tailor_and_check import make_job


def run_main(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class SelectionTest(HomeTestCase):
    profiles = ("sales", "ops")

    def test_resolve(self):
        self.assertEqual(list_profiles(), ["ops", "sales"])
        self.assertEqual(resolve_profile("ops"), "ops")

    def test_unknown_profile_lists_options(self):
        with self.assertRaisesRegex(ProfileError, r"unknown profile 'nope'. Available: ops, sales"):
            resolve_profile("nope")
        code, _, err = run_main("--profile", "nope", "--force")
        self.assertEqual(code, 2)
        self.assertIn("ops, sales", err)

    def test_missing_profile_with_several_available_is_an_error(self):
        code, _, err = run_main("--force")
        self.assertEqual(code, 2)
        self.assertIn("--profile", err)
        self.assertIn("ops, sales", err)

    def test_list_profiles_flags_missing_resume(self):
        self.add_profile("broken", with_tex=False)
        code, out, _ = run_main("--list-profiles")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertIn("sales", lines)
        self.assertIn("ops", lines)
        self.assertTrue(any(l.startswith("broken") and "MISSING resume.tex" in l for l in lines))


class SingleProfileTest(HomeTestCase):
    profiles = ("only",)

    def test_single_profile_is_used_automatically(self):
        self.assertEqual(resolve_profile(None), "only")
        self.assertEqual(load_profile_config(resolve_profile(None))["profile"], "only")


class MergeTest(HomeTestCase):
    def test_deep_merge(self):
        self.assertEqual(deep_merge({"a": {"x": 1, "y": 2}, "l": [1]}, {"a": {"y": 3}, "l": [9]}),
                         {"a": {"x": 1, "y": 3}, "l": [9]})

    def test_profile_overrides_config(self):
        self.add_profile("sales", scoring={"min_score": 55}, limits={"max_applications_per_run": 3},
                         answers=[{"match": "notice", "value": "30"}], llm={"cmd": "evil"}, platforms=["x"])
        (self.home / "resume" / "sales" / "target_jd.md").write_text("Inside sales, SaaS\n", encoding="utf-8")
        cfg = load_profile_config("sales")
        self.assertEqual(cfg["profile"], "sales")
        self.assertEqual(cfg["scoring"]["min_score"], 55)
        self.assertEqual(cfg["scoring"]["max_description_chars"], 6000)       # shared value kept
        self.assertEqual(cfg["limits"]["max_applications_per_run"], 3)
        self.assertEqual(cfg["limits"]["max_applications_per_day"], 40)       # default limit kept
        self.assertEqual(cfg["answers"], [{"match": "notice", "value": "30"}])
        self.assertEqual(cfg["search"]["keywords"], ["sales coordinator"])
        self.assertEqual(cfg["resume"]["output_name"], "Test_Resume.pdf")
        self.assertIn("compile_cmd", cfg["resume"])                           # shared toolchain kept
        self.assertEqual(cfg["target_jd"], "Inside sales, SaaS")
        self.assertNotEqual(cfg["llm"]["cmd"], "evil")                        # not an allowed profile key
        self.assertEqual(cfg["platforms"], ["linkedin", "naukri"])

    def test_target_jd_is_in_the_scoring_prompt(self):
        (self.home / "resume" / "default" / "target_jd.md").write_text("Junior B2B inside sales", encoding="utf-8")
        cfg = load_profile_config("default")
        score_job(cfg, "RESUME", make_job())
        stdin = Path(self.log + ".stdin").read_text(encoding="utf-8")
        self.assertIn("<profile_target>\nJunior B2B inside sales\n</profile_target>", stdin)


class NewAndCheckProfileTest(HomeTestCase):
    def test_new_profile_scaffold(self):
        code, out, _ = run_main("--new-profile", "sales_coordinator")
        self.assertEqual(code, 0)
        pdir = self.home / "resume" / "sales_coordinator"
        self.assertTrue((pdir / "profile.yaml").exists())
        self.assertTrue((pdir / "target_jd.md").exists())
        self.assertEqual((pdir / "resume.cls").read_bytes(), (self.home / "resume" / "default" / "resume.cls").read_bytes())
        self.assertFalse((pdir / "resume.tex").exists())
        cfg = load_profile_config("sales_coordinator")
        self.assertEqual(cfg["answers"], [])
        self.assertEqual(cfg["search"]["keywords"], [])

    def test_new_profile_refuses_existing_and_bad_names(self):
        code, _, err = run_main("--new-profile", "default")
        self.assertEqual(code, 2)
        self.assertIn("already exists", err)
        self.assertEqual(run_main("--new-profile", "../evil")[0], 2)
        self.assertFalse((self.home / "evil").exists())

    def test_run_refuses_profile_without_keywords(self):
        run_main("--new-profile", "fresh")
        shutil_tex = self.home / "resume" / "default" / "resume.tex"
        (self.home / "resume" / "fresh" / "resume.tex").write_text(shutil_tex.read_text(encoding="utf-8"), encoding="utf-8")
        code, _, err = run_main("--profile", "fresh", "--force")
        self.assertEqual(code, 2)
        self.assertIn("keywords", err)

    def test_check_profile_ok_makes_no_llm_call(self):
        code, out, _ = run_main("--check-profile", "default")
        self.assertEqual(code, 0, out)
        self.assertIn("Compiles OK", out)
        self.assertIn("Protected strings: 1/1 found", out)
        self.assertIn("keywords: ['sales coordinator']", out)
        self.assertIn("min_score: 70", out)
        self.assertIn("answers: 0", out)
        self.assertFalse(self.llm_called())

    def test_check_profile_reports_missing_protected_string(self):
        self.add_profile("p2", resume={"protected_strings": ["Shradha Biradar", "Nobody Here"]})
        code, out, _ = run_main("--check-profile", "p2")
        self.assertEqual(code, 1)
        self.assertIn("MISSING protected string in resume.tex: 'Nobody Here'", out)

    def test_check_unknown_profile(self):
        code, _, err = run_main("--check-profile", "zzz")
        self.assertEqual(code, 2)
        self.assertIn("unknown profile", err)


if __name__ == "__main__":
    unittest.main()
