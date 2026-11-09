import unittest
from unittest import mock

import parsers as P
import server
from tests import TEST_CONFIG, row

V = TEST_CONFIG["verdicts"]

PR_STATUS = """== #351 OPEN abc1234 -> main, behind 0
required contexts:
  verify: SUCCESS
latest review:
  ...
#351: NOT READY: review unanswered; label needs-maintainer (waits on a decision)
"""

PORCELAIN = """worktree /r
HEAD d5b129ca478f8792789477719c5f3f65f6181544
branch refs/heads/main

worktree /r/.claude/worktrees/agent-a494
HEAD cf7ae0133c18ac573560d080340f1a95c4e6e25e
branch refs/heads/process/review-round-cap
locked claude agent agent-a494d0f45342dc2a0 (pid 18687 start Sat Nov  7 12:25:04 2026)

worktree /r/.claude/worktrees/x
HEAD 0000000000000000000000000000000000000000
detached
locked
"""


class Verdicts(unittest.TestCase):
    def test_not_ready(self) -> None:
        v = P.parse_verdict(PR_STATUS, 351, V)
        self.assertEqual(v["verdict"], "NOT READY")
        self.assertEqual(v["reasons"], ["review unanswered", "label needs-maintainer (waits on a decision)"])

    def test_ready_variants(self) -> None:
        self.assertEqual(P.parse_verdict("#7: READY\n", 7, V)["verdict"], "READY")
        self.assertEqual(P.parse_verdict("x\n#7: READY FOR TRAIN\n", 7, V)["verdict"], "READY FOR TRAIN")

    def test_configured_strings(self) -> None:
        other = {"ready": "OK", "ready_for_train": "GO", "not_ready": "BLOCKED"}
        self.assertEqual(P.parse_verdict("#7: GO\n", 7, other)["verdict"], "GO")
        v = P.parse_verdict("#7: BLOCKED: a; b\n", 7, other)
        self.assertEqual((v["verdict"], v["reasons"]), ("BLOCKED", ["a", "b"]))
        self.assertEqual(P.parse_verdict("#7: READY\n", 7, other)["verdict"], "UNKNOWN")

    def test_other_pr_or_missing(self) -> None:
        self.assertEqual(P.parse_verdict("#70: READY\n", 7, V)["verdict"], "UNKNOWN")
        self.assertEqual(P.parse_verdict("", 7, V)["verdict"], "UNKNOWN")


class Worktrees(unittest.TestCase):
    def test_porcelain(self) -> None:
        w = P.parse_porcelain(PORCELAIN)
        self.assertEqual(len(w), 3)
        self.assertEqual(w[0]["branch"], "main")
        self.assertTrue(w[1]["locked"])
        self.assertEqual(w[1]["branch"], "process/review-round-cap")
        self.assertTrue(w[2]["detached"] and w[2]["locked"])
        self.assertEqual(w[2]["lock_reason"], "")

    def test_lock_reason(self) -> None:
        lk = P.parse_lock_reason("claude agent agent-a494d0f45342dc2a0 (pid 18687 start Sat Nov  7 12:25:04 2026)")
        self.assertEqual(lk, {"agent": "agent-a494d0f45342dc2a0", "pid": 18687, "start": "Sat Nov  7 12:25:04 2026"})
        self.assertEqual(P.parse_lock_reason("claude agent agent-ab12 (pid 5)"),
                         {"agent": "agent-ab12", "pid": 5, "start": ""})
        self.assertIsNone(P.parse_lock_reason("manual hold"))

    def test_reverify_lock(self) -> None:
        self.assertEqual(P.parse_reverify_lock("123|Sat Nov 8"), {"pid": 123, "start": "Sat Nov 8"})
        self.assertIsNone(P.parse_reverify_lock("garbage"))


class Trains(unittest.TestCase):
    def test_body(self) -> None:
        body = "## Summary\n\nMerge train...\n\n- #351 [a](u)\n- #349 [b](u)\n\n## Evidence\n- #1 no"
        self.assertEqual(P.train_constituents(body=body)[:2], [351, 349])

    def test_commits(self) -> None:
        msgs = ["Merge pull request #357 from example/a2/x\n\ndocs(a2): y", "fix", "Merge pull request #12 from o/b"]
        self.assertEqual(P.train_constituents(commit_messages=msgs), [357, 12])

    def test_title(self) -> None:
        self.assertEqual(P.train_constituents(title="chore(train): 2026-11-08-3, #357, #356",
                                                title_prefix="chore(train):"), [357, 356])
        self.assertEqual(P.train_constituents(title="feat: #3", title_prefix="chore(train):"), [])
        self.assertEqual(P.train_constituents(title="chore(train): x, #357"), [])  # no prefix configured


class Review(unittest.TestCase):
    def test_answered(self) -> None:
        rv: list[P.GhReview] = [{"state": "COMMENTED", "submittedAt": "2026-11-01T00:00:00Z", "author": {"login": "r"}}]
        c: list[P.GhComment] = [{"createdAt": "2026-11-02T00:00:00Z", "body": "fixed\n\n" + P.MARKER + "\n"}]
        self.assertTrue(P.review_state(rv, c)["answered"])
        old: list[P.GhComment] = [{"createdAt": "2026-10-01T00:00:00Z", "body": P.MARKER}]
        self.assertFalse(P.review_state(rv, old)["answered"])
        self.assertEqual(P.review_state([], [])["state"], "NONE")


class Validation(unittest.TestCase):
    def test_pr(self) -> None:
        self.assertEqual(P.validate_pr("351", {351}), 351)
        for bad in ("35a", "-1", "1;rm", "", "3 5", "9999999"):
            with self.assertRaises(P.ValidationError):
                P.validate_pr(bad, {351})
        with self.assertRaises(P.ValidationError):
            P.validate_pr("352", {351})

    def test_branch(self) -> None:
        self.assertEqual(P.validate_branch("m4/link-receiver_helpers.v2"), "m4/link-receiver_helpers.v2")
        for bad in ("--apply", "a b", "a;b", "a/../b", "/abs", "x$(y)", ""):
            with self.assertRaises(P.ValidationError):
                P.validate_branch(bad)


class Actions(unittest.TestCase):
    """build_action with gh stubbed: no network, nothing runs."""

    def setUp(self) -> None:
        server.use_config(TEST_CONFIG)
        self.p = mock.patch.object(server.CACHE, "open_numbers", return_value={350, 351, 365})
        self.p.start()
        server.CACHE.data["prs"] = [row(365, True), row(351, False)]

    def tearDown(self) -> None:
        self.p.stop()

    def test_train_dry_run(self) -> None:
        _, argv = server.build_action({"kind": "train", "prs": [351, "350"], "dry_run": True})
        self.assertEqual(argv[1:], ["--dry-run", "351", "350"])
        self.assertEqual(argv[0], "/nonexistent/scripts/train")

    def test_merge_only_trains_and_merge_commit(self) -> None:
        _, argv = server.build_action({"kind": "merge", "pr": 365})
        self.assertIn("--merge", argv)
        self.assertNotIn("--squash", argv)
        with self.assertRaises(P.ValidationError):
            server.build_action({"kind": "merge", "pr": 351})

    def test_rejects_closed_pr_and_bad_kind(self) -> None:
        with self.assertRaises(P.ValidationError):
            server.build_action({"kind": "close", "pr": 1})
        with self.assertRaises(P.ValidationError):
            server.build_action({"kind": "rm"})

    def test_rebuild_and_cleanup(self) -> None:
        _, argv = server.build_action({"kind": "rebuild", "train": 365, "without": ["351"], "dry_run": True})
        self.assertEqual(argv[1:], ["--dry-run", "--rebuild", "365", "--without", "351"])
        _, argv = server.build_action({"kind": "cleanup", "branch": "m4/x"})
        self.assertEqual(argv[1:], ["m4/x"])
        with self.assertRaises(P.ValidationError):
            server.build_action({"kind": "cleanup", "branch": "--apply"})

    def test_one_action_per_pr(self) -> None:
        server.BUSY.add(351)
        try:
            with self.assertRaises(P.ValidationError):
                server.start_job(351, ["true"])
        finally:
            server.BUSY.discard(351)


if __name__ == "__main__":
    unittest.main()


class PrSummary(unittest.TestCase):
    def test_summary_section(self) -> None:
        body = "## Summary\nDoes X.\n\nAnd Y.\n\n## Evidence\nruns"
        self.assertEqual(P.pr_summary(body), "Does X.\n\nAnd Y.")

    def test_no_section_uses_first_paragraph_and_cuts(self) -> None:
        self.assertEqual(P.pr_summary("first\n\nsecond"), "first")
        self.assertTrue(P.pr_summary("a" * 900).endswith("…"))
        self.assertEqual(P.pr_summary(None), "")
