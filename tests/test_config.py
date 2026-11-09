import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config as K
import parsers as P
import server
from tests import FIXTURES, TEST_CONFIG

GOOD = '''
repo = "/r"
gh_repo = "o/n"
[commands]
dry_run_flag = "--dry-run"
train = ["scripts/train", "{dry_run}", "{prs}"]
'''


def load_text(text):
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
        f.write(text)
    try:
        return K.load(f.name)
    finally:
        Path(f.name).unlink()


class Loading(unittest.TestCase):
    def test_examples_load(self):
        examples = sorted((Path(__file__).resolve().parents[1] / "examples").glob("*.toml"))
        self.assertTrue(examples)
        for f in examples:
            cfg = K.load(f)
            self.assertFalse(cfg["repo"].startswith("~"), f)  # ~ expanded
            self.assertEqual(K.enabled_actions(cfg), ["reverify", "reverify_cancel", "merge", "close", "label",
                                                      "train", "rebuild", "cleanup"])
        self.assertEqual(TEST_CONFIG["gh_repo"], "example/none")

    def test_tilde_expanded(self):
        self.assertEqual(load_text(GOOD.replace('"/r"', '"~/r"'))["repo"], str(Path.home() / "r"))

    def test_name_defaults_to_repo_part(self):
        self.assertEqual(load_text(GOOD)["name"], "n")

    def test_missing_file_and_none(self):
        with self.assertRaisesRegex(K.ConfigError, "cannot read config"):
            K.load(FIXTURES / "missing.toml")
        with self.assertRaisesRegex(K.ConfigError, "--config"):
            K.load(None)

    def test_bad_toml(self):
        with self.assertRaisesRegex(K.ConfigError, "not valid TOML"):
            load_text("repo = ")

    def test_bad_keys(self):
        cases = {
            "colour = 1\n" + GOOD: "unknown key colour",
            GOOD + "reverfy = ['x', '{pr}']\n": "unknown key commands.reverfy",
            GOOD + "[conventions]\nlabel = 'x'\n": "unknown key conventions.label",
            'gh_repo = "o/n"\n': "repo must be",
            'repo = "/r"\ngh_repo = "o n"\n': "gh_repo must be OWNER/NAME",
            GOOD.replace('"{prs}"', '"x"'): "must use {prs}",
            GOOD.replace('dry_run_flag = "--dry-run"\n', ""): "dry_run_flag is not set",
            GOOD.replace('"scripts/train"', '"{prs}"'): "program",
            GOOD + "verdict = ['s', '{pr}']\n": r"need \[verdicts\]",
            GOOD.replace('"{prs}"', '"--prs={prs}"'): "whole argument",
        }
        for text, msg in cases.items():
            with self.assertRaisesRegex(K.ConfigError, msg, msg=text):
                load_text(text)

    def test_unknown_placeholder(self):
        with self.assertRaisesRegex(K.ConfigError, r"unknown placeholder \{branch\}"):
            load_text(GOOD.replace('"{prs}"', '"{prs}", "{branch}"'))
        with self.assertRaisesRegex(K.ConfigError, r"unknown placeholder \{shell\}"):
            load_text(GOOD.replace('"{prs}"', '"{prs}", "$({shell})"'))


class Expansion(unittest.TestCase):
    def test_list_and_nested_list(self):
        self.assertEqual(K.expand(TEST_CONFIG, "train", prs=[3, 1], dry_run=False),
                         ["/nonexistent/scripts/train", "3", "1"])
        self.assertEqual(K.expand(TEST_CONFIG, "rebuild", train=9, without=[3, 1], dry_run=True),
                         ["/nonexistent/scripts/train", "--dry-run", "--rebuild", "9",
                          "--without", "3", "--without", "1"])

    def test_scalar_inside_an_argument_and_program_lookup(self):
        cfg = load_text('repo = "/r"\ngh_repo = "o/n"\n[commands]\nreverify = ["gh-x", "--pr={pr}"]\n')
        self.assertEqual(K.expand(cfg, "reverify", pr=5), ["gh-x", "--pr=5"])

    def test_values_stay_one_argument(self):
        argv = K.expand(TEST_CONFIG, "cleanup", branch="a b;$(rm -rf /)", apply=False)
        self.assertEqual(argv[1:], ["a b;$(rm -rf /)"])  # one argv entry, never a shell

    def test_injection_is_still_rejected_by_validation(self):
        server.use_config(TEST_CONFIG)
        with mock.patch.object(server.CACHE, "open_numbers", return_value={7}):
            for b in ("--apply", "-x", "a/../b"):
                with self.assertRaises(P.ValidationError):
                    server.build_action({"kind": "cleanup", "branch": b})
            with self.assertRaises(P.ValidationError):
                server.build_action({"kind": "train", "prs": ["--apply"]})


class Disabled(unittest.TestCase):
    def setUp(self):
        server.use_config(K.load(FIXTURES / "minimal.toml"))
        self.p = mock.patch.object(server.CACHE, "open_numbers", return_value={7, 9})
        self.p.start()
        server.CACHE.data["prs"] = [{"number": 9, "is_train": True}, {"number": 7, "is_train": False}]

    def tearDown(self):
        self.p.stop()
        server.use_config(TEST_CONFIG)

    def test_actions_left_out_are_refused(self):
        self.assertEqual(K.enabled_actions(server.CFG), ["merge", "close", "train", "rebuild", "cleanup"])
        for body in ({"kind": "reverify", "pr": 9}, {"kind": "reverify_cancel", "pr": 9},
                     {"kind": "label", "pr": 7, "add": True}):
            with self.assertRaisesRegex(P.ValidationError, "not configured"):
                server.build_action(body)

    def test_dry_run_refused_without_placeholder(self):
        with self.assertRaisesRegex(P.ValidationError, "no dry run"):
            server.build_action({"kind": "train", "prs": [7], "dry_run": True})
        self.assertEqual(server.build_action({"kind": "train", "prs": [7]})[1][1:], ["7"])

    def test_public_config_has_no_prompt(self):
        pub = K.public(TEST_CONFIG)
        self.assertNotIn("prompt_append", pub["coordinator"])
        self.assertIn("reverify", pub["actions"])


if __name__ == "__main__":
    unittest.main()
