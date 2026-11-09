"""The real server.py Handler with GitHub, git and the scripts replaced by fixtures.

Run: ../.venv/bin/python -I tests/fake_server.py --port 8798 [--config <file>] (default tests/fixtures/console.toml). Token: "test-token". Nothing leaves the machine:
actions only record a finished fake job.
"""

import argparse
import http.server
import secrets
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import server  # noqa: E402

NOW = time.time()


def pr(n, title, verdict, reasons=(), labels=(), train=False, review="APPROVED", answered=True):
    branch = f"train/2026-11-08-{n}" if train else f"m3/wp-{n}"
    return {
        "number": n, "title": title, "branch": branch, "scope": branch.split("/")[0] + "/",
        "author": "octo", "head": f"{n:07d}abcdef", "base": "main",
        "created_at": "2026-11-07T10:00:00Z", "draft": False, "labels": list(labels),
        "review": {"state": review, "author": "reviewer", "at": "2026-11-07T11:00:00Z", "answered": answered},
        "is_train": train, "constituents": [102, 101] if train else [],
        "summary": f"Summary of #{n}.", "url": f"https://github.com/example/none/pull/{n}",
        "verify_run": ({"databaseId": 7, "status": "completed", "conclusion": "success",
                        "url": "https://example.invalid/run/7", "headSha": "abc"} if train else None),
        "status": {"verdict": verdict, "reasons": list(reasons), "line": f"#{n}: {verdict}"},
    }


PRS = [
    pr(200, "chore(train): 2026-11-08-200, #102, #101", "READY", train=True),
    pr(103, "feat(m3/wp-3c): not ready yet", "NOT READY", reasons=["no review", "verify missing"],
       review="CHANGES_REQUESTED", answered=False),
    pr(102, "feat(m3/wp-3b): second", "READY FOR TRAIN", labels=["needs-maintainer"]),
    pr(101, "feat(m3/wp-3a): first", "READY FOR TRAIN"),
]

SESSIONS = {
    "groups": {
        "main checkout": [{
            "path": "/repo", "head": "aaa", "branch": "main", "detached": False, "bare": False,
            "locked": False, "lock_reason": "", "prunable": False, "dirty": False, "open_pr": None,
            "merged_pr": None, "claude_lock": None, "is_main": True, "ready_to_clean": False}],
        "unlocked worktrees": [{
            "path": "/repo/.claude/worktrees/agent-1", "head": "bbb", "branch": "m3/wp-done",
            "detached": False, "bare": False, "locked": False, "lock_reason": "", "prunable": False,
            "dirty": False, "open_pr": None, "merged_pr": 99, "claude_lock": None, "is_main": False,
            "ready_to_clean": True}],
    },
    "reverify_locks": [],
    "reverify_state_dir": "/repo/.git/reverify",
    "train_reverify_status": [{"pr": 200, "exit": 0, "status": "#200: up to date"}],
}


def fake_start_job(key, argv):
    with server.JOBS_LOCK:
        jid = secrets.token_hex(6)
        out = [f"fake run of {' '.join(argv)}"]
        if "cleanup-merged" in argv[0] and "--apply" not in argv:
            out.append("would remove worktree /repo/.claude/worktrees/agent-1 and branch m3/wp-done")
        job = {"id": jid, "key": key, "argv": argv, "started": time.time(), "output": out,
               "exit": 0, "ended": time.time()}
        server.JOBS[jid] = job
    server.log_action({k: job[k] for k in ("id", "key", "argv", "started", "ended", "exit")}
                      | {"output_tail": out})
    return job


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8798)
    ap.add_argument("--chats-dir", default=None, help="keep chats here (to test a restart)")
    ap.add_argument("--config", default=str(Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "console.toml"))
    a = ap.parse_args()
    import config
    cfg = config.load(a.config)
    server.use_config(cfg)
    log = Path(tempfile.mkdtemp(prefix="pr-console-test-")) / "actions.jsonl"
    server.CONFIG.update(port=a.port, log_file=str(log))
    server.TOKEN = "test-token"
    server.CACHE.data.update(prs=PRS, fetched_at=NOW, error=None, refreshing=False)
    server.CACHE.open_numbers = lambda: {p["number"] for p in PRS}
    server.CACHE.refresh = lambda: None
    server.sessions = lambda: SESSIONS
    server.start_job = fake_start_job
    # Chats: the real ChatManager and SDK client over a fake CLI transport, in scratch directories.
    import fake_sdk
    work = Path(a.chats_dir or tempfile.mkdtemp(prefix="pr-console-chats-"))
    dirs = []
    for name, branch, is_main in (("main", "main", True), ("wt-102", "m3/wp-102", False)):
        (work / "dirs" / name).mkdir(parents=True, exist_ok=True)
        dirs.append({"path": str(work / "dirs" / name), "branch": branch, "is_main": is_main})
    server.chat_dirs = lambda: dirs
    server.branch_of = lambda cwd: next((d["branch"] for d in dirs if d["path"] == cwd), "")
    server.start_chats(str(work / "state"), fake_sdk.make_client, cfg["coordinator"]["prompt_append"])
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", a.port), server.Handler)
    print(f"fake PR console on {a.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
