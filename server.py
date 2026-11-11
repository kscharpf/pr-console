"""PR console: a local web app for managing a repo's pull requests. Run: python3 -I server.py --config <file>"""

import argparse
import concurrent.futures
import contextlib
import hmac
import http.server
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, NotRequired, TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as K
import parsers as P

if TYPE_CHECKING:
    import chats as C

HERE = Path(__file__).resolve().parent


class ServerConfig(TypedDict):
    repo: str
    gh_repo: str
    port: int
    concurrency: int
    refresh_seconds: int
    log_file: str


CFG: K.Config  # the checked per-repo config (config.load), set by main() or use_config()
CONFIG: ServerConfig = {
    "repo": "",
    "gh_repo": "",
    "port": 8765,
    "concurrency": 4,
    "refresh_seconds": 180,
    "log_file": str(HERE / "actions.jsonl"),
}
TOKEN = ""
ENV = dict(os.environ, NO_COLOR="1", GH_FORCE_TTY="0", GH_NO_UPDATE_NOTIFIER="1", GH_PROMPT_DISABLED="1")


def run(args: list[str], timeout: float = 300) -> tuple[int, str, str]:
    """Run a command (argument list, never a shell) in the managed repo's checkout."""
    try:
        # check=False: the caller reads the exit code
        p = subprocess.run(
            args, cwd=CONFIG["repo"], env=ENV, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return 255, "", str(e)
    return p.returncode, p.stdout, p.stderr


def gh_json(args: list[str], timeout: float = 120) -> Any:
    """gh's JSON output. Its shape is gh's; each caller annotates the result with the shape it asked for."""
    rc, out, err = run(["gh", *args, "--repo", CONFIG["gh_repo"]], timeout)
    if rc != 0:
        raise RuntimeError(err.strip() or f"gh exited {rc}")
    return json.loads(out or "null")


def use_config(cfg: K.Config) -> None:
    """Install a checked config (config.load)."""
    global CFG  # noqa: PLW0603  the installed config is process state, read by every request handler
    CFG = cfg
    CONFIG["repo"] = cfg["repo"]
    CONFIG["gh_repo"] = cfg["gh_repo"]


def command(name: str, **values: K.Value) -> list[str] | None:
    """The argv of a configured command; None when the config leaves it out."""
    return K.expand(CFG, name, **values) if name in CFG["commands"] else None


def conv(key: K.ConventionKey) -> str | None:
    return CFG["conventions"].get(key)


# ---------------------------------------------------------------- PR state cache


class GhLabel(TypedDict):
    name: str


class GhPr(TypedDict):
    """One PR of `gh pr list --json` with the fields refresh() asks for."""

    number: int
    title: str
    headRefName: str
    headRefOid: str
    baseRefName: str
    author: NotRequired[P.GhAuthor | None]
    createdAt: str
    labels: NotRequired[list[GhLabel] | None]
    reviews: NotRequired[list[P.GhReview] | None]
    comments: NotRequired[list[P.GhComment] | None]
    isDraft: NotRequired[bool]
    body: NotRequired[str | None]


class GhRun(TypedDict, total=False):
    """`gh run list --json databaseId,status,conclusion,url,headSha`."""

    databaseId: int
    status: str
    conclusion: str
    url: str
    headSha: str


class RunError(TypedDict):
    error: str


class PrRow(TypedDict):
    """A row of GET /api/prs."""

    number: int
    title: str
    branch: str
    scope: str
    author: str
    head: str
    base: str
    created_at: str
    draft: bool
    labels: list[str]
    review: P.ReviewState
    is_train: bool
    constituents: list[int]
    verify_run: GhRun | RunError | None
    body: NotRequired[str]  # dropped once the summary is made
    status: NotRequired[P.Verdict]
    summary: NotRequired[str]
    url: NotRequired[str]


class CacheData(TypedDict):
    """GET /api/prs."""

    prs: list[PrRow]
    fetched_at: float | None
    error: str | None
    refreshing: bool


class VerdictEntry(TypedDict):
    head: str
    at: float
    verdict: P.Verdict


JobKey = int | str  # a PR number, "train" or "branch:<name>": one running action per key


class Cache:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.data: CacheData = {"prs": [], "fetched_at": None, "error": None, "refreshing": False}
        self.rerun = False
        self.status: dict[JobKey, VerdictEntry] = {}  # pr -> verdict at a head

    def open_numbers(self) -> set[int]:
        """Fresh list of open PR numbers, for validation of actions."""
        prs: list[GhPr] = gh_json(["pr", "list", "--state", "open", "--limit", "300", "--json", "number"])
        return {p["number"] for p in prs}

    def refresh(self, force: bool = False) -> None:
        """Refetch the PR list. `force` (the Refresh button) re-runs every verdict, and a forced
        request made while a refresh runs is queued to run right after it."""
        with self.lock:
            if self.data["refreshing"]:
                self.rerun = self.rerun or force
                return
            self.data["refreshing"] = True
            if force:
                self.status.clear()
        try:
            fields = (
                "number,title,headRefName,headRefOid,baseRefName,author,createdAt,labels,reviews,comments,isDraft,body"
            )
            prs: list[GhPr] = gh_json(["pr", "list", "--state", "open", "--limit", "300", "--json", fields])
            rows = [self._row(p) for p in prs]
            with concurrent.futures.ThreadPoolExecutor(CONFIG["concurrency"]) as ex:
                list(ex.map(self._enrich, rows))
            rows.sort(key=lambda r: -r["number"])
            with self.lock:
                self.data["prs"] = rows
                self.data["fetched_at"] = time.time()
                self.data["error"] = None
        except Exception as e:  # noqa: BLE001 - surfaced to the UI
            with self.lock:
                self.data["error"] = str(e)
        finally:
            with self.lock:
                self.data["refreshing"] = False
                again, self.rerun = self.rerun, False
            if again:
                self.refresh(force=True)

    def _row(self, p: GhPr) -> PrRow:
        branch = p["headRefName"]
        prefix = conv("train_branch_prefix")
        return {
            "number": p["number"],
            "title": p["title"],
            "branch": branch,
            "scope": P.scope_of(branch),
            "author": (p.get("author") or {}).get("login", ""),
            "head": p["headRefOid"],
            "base": p["baseRefName"],
            "created_at": p["createdAt"],
            "draft": p.get("isDraft", False),
            "labels": [lb["name"] for lb in p.get("labels") or []],
            "review": P.review_state(p.get("reviews"), p.get("comments")),
            "is_train": bool(prefix) and branch.startswith(prefix or ""),
            "constituents": [],
            "verify_run": None,
            "body": p.get("body") or "",
        }

    def _enrich(self, r: PrRow) -> None:
        n = r["number"]
        args = command("train_verdict" if r["is_train"] else "verdict", pr=n)
        cached = self.status.get(n)
        v: P.Verdict | None
        if args is None:
            v = None
        elif cached and cached["head"] == r["head"] and time.time() - cached["at"] < CONFIG["refresh_seconds"]:
            v = cached["verdict"]
        else:
            fresh = self._verdict(args, n)
            self.status[n] = {"head": r["head"], "at": time.time(), "verdict": fresh}
            v = fresh
        if v is not None:
            r["status"] = v
        if r["is_train"]:
            msgs: list[str] = []
            # The commits are the second source of constituents; without them the body and title still serve.
            with contextlib.suppress(Exception):
                commits: list[dict[str, str]] = gh_json(["pr", "view", str(n), "--json", "commits"])["commits"]
                msgs = [c.get("messageHeadline", "") + "\n" + c.get("messageBody", "") for c in commits]
            r["constituents"] = P.train_constituents(
                r["title"], r.get("body", ""), msgs, conv("train_title_prefix") or ""
            )
            workflow = conv("verify_workflow")
            try:
                runs: list[GhRun] = (
                    gh_json(
                        [
                            "run",
                            "list",
                            "--workflow",
                            workflow,
                            "--branch",
                            r["branch"],
                            "-L",
                            "1",
                            "--json",
                            "databaseId,status,conclusion,url,headSha",
                        ]
                    )
                    if workflow
                    else []
                )
                r["verify_run"] = runs[0] if runs else None
            except Exception as e:  # noqa: BLE001
                r["verify_run"] = {"error": str(e)}
        r["summary"] = P.pr_summary(r.get("body", ""))
        r["url"] = f"https://github.com/{CONFIG['gh_repo']}/pull/{n}"
        r.pop("body", None)

    @staticmethod
    def _verdict(args: list[str], n: int) -> P.Verdict:
        rc, err = 0, ""
        for attempt in range(3):  # concurrent pr-status calls race on `git fetch`'s ref lock
            rc, out, err = run(args, timeout=240)
            v = P.parse_verdict(out, n, CFG["verdicts"])
            if v["verdict"] != "UNKNOWN" or "cannot lock ref" not in err:
                break
            time.sleep(2 + 3 * attempt)
        if v["verdict"] == "UNKNOWN":
            v["line"] = (err.strip().splitlines() or [f"pr-status exited {rc}"])[-1]
        return v

    def snapshot(self) -> CacheData:
        with self.lock:
            copy: CacheData = json.loads(json.dumps(self.data))
            return copy


CACHE = Cache()


def refresher() -> None:
    while True:  # periodic refresh
        CACHE.refresh()
        time.sleep(CONFIG["refresh_seconds"])


# ---------------------------------------------------------------- sessions


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (ProcessLookupError, OverflowError):
        return False
    return True


class ClaudeLock(P.LockReason):
    alive: bool


class SessionWorktree(P.Worktree):
    dirty: bool | None
    open_pr: int | None
    merged_pr: int | None
    claude_lock: ClaudeLock | None
    is_main: bool
    ready_to_clean: bool


class LockView(TypedDict):
    pr: int
    holder: P.ReverifyLock | None
    alive: bool
    status: str


class TrainStatus(TypedDict):
    pr: int
    exit: int
    status: str


class SessionsView(TypedDict):
    """GET /api/sessions."""

    groups: dict[str, list[SessionWorktree]]
    reverify_locks: list[LockView]
    reverify_state_dir: str
    train_reverify_status: list[TrainStatus]


def sessions() -> SessionsView:
    rc, out, err = run(["git", "worktree", "list", "--porcelain"])
    if rc != 0:
        raise RuntimeError(err)
    wts = P.parse_porcelain(out)
    try:
        merged: list[GhPr] = gh_json(
            ["pr", "list", "--state", "merged", "--limit", "300", "--json", "number,headRefName"]
        )
    except Exception:  # noqa: BLE001
        merged = []
    merged_by_branch = {m["headRefName"]: m["number"] for m in merged}
    open_by_branch = {r["branch"]: r["number"] for r in CACHE.snapshot()["prs"]}
    main = str(Path(CONFIG["repo"]).resolve())
    wt_dir = conv("worktrees_dir")
    wt_groups = CFG["conventions"].get("worktree_groups", {})
    groups: dict[str, list[SessionWorktree]] = {}
    for wt in wts:
        rc, st, _ = run(["git", "-C", wt["path"], "status", "--porcelain"], timeout=60)
        dirty = None if rc != 0 else bool(st.strip())
        open_pr = open_by_branch.get(wt["branch"])
        merged_pr = merged_by_branch.get(wt["branch"])
        reason = P.parse_lock_reason(wt["lock_reason"]) if wt["locked"] else None
        lk: ClaudeLock | None = {**reason, "alive": pid_alive(reason["pid"])} if reason else None
        is_main = str(Path(wt["path"]).resolve()) == main
        w: SessionWorktree = {
            **wt,
            "dirty": dirty,
            "open_pr": open_pr,
            "merged_pr": merged_pr,
            "claude_lock": lk,
            "is_main": is_main,
            "ready_to_clean": bool(not is_main and dirty is False and not wt["locked"] and merged_pr and not open_pr),
        }
        if w["is_main"]:
            key = "main checkout"
        elif lk:
            key = f"{lk['agent']} (pid {lk['pid']}, {'alive' if lk['alive'] else 'gone'})"
        else:
            key = next(
                (g for g, prefix in wt_groups.items() if f"/{wt_dir}/{prefix}" in w["path"]), "unlocked worktrees"
            )
        groups.setdefault(key, []).append(w)
    rc, common, _ = run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"])
    locks: list[LockView] = []
    lock_dir = conv("reverify_lock_dir")
    state_dir = Path(common.strip()) / lock_dir if lock_dir else None
    if rc == 0 and state_dir is not None and state_dir.is_dir():
        for f in sorted(state_dir.glob("*.lock")):
            n = f.name[:-5]
            if not n.isdigit():
                continue
            try:
                holder = P.parse_reverify_lock(str(f.readlink()))
            except OSError:
                holder = None
            argv = command("reverify_status", pr=int(n))
            st = run(argv, timeout=30)[1] if argv else ""
            locks.append(
                {
                    "pr": int(n),
                    "holder": holder,
                    "alive": bool(holder and pid_alive(holder["pid"])),
                    "status": st.strip(),
                }
            )
    train_status: list[TrainStatus] = []
    for r in CACHE.snapshot()["prs"]:
        argv = command("reverify_status", pr=r["number"]) if r["is_train"] else None
        if argv:
            rc2, st, _ = run(argv, timeout=30)
            train_status.append({"pr": r["number"], "exit": rc2, "status": st.strip()})
    return {
        "groups": groups,
        "reverify_locks": locks,
        "reverify_state_dir": str(state_dir or ""),
        "train_reverify_status": train_status,
    }


# ---------------------------------------------------------------- actions


class Job(TypedDict):
    id: str
    key: JobKey
    argv: list[str]
    started: float
    output: list[str]
    exit: int | None
    ended: float | None


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()
BUSY: set[JobKey] = set()


def log_action(entry: Mapping[str, object]) -> None:
    with Path(CONFIG["log_file"]).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def _configured(argv: list[str] | None) -> list[str]:
    """enabled_actions() already refused a command the config leaves out."""
    if argv is None:
        raise P.ValidationError("this action is not configured for this repo")
    return argv


def _items(v: object, what: str) -> list[object]:
    if not v:
        return []
    if not isinstance(v, list):
        raise P.ValidationError(f"{what} must be a list")
    return v


def build_action(body: dict[str, object]) -> tuple[JobKey, list[str]]:
    """Validate a request and return (key, argv). Raises ValidationError."""
    kind = body.get("kind")
    if (kind in K.ACTION_COMMANDS or kind == "label") and kind not in K.enabled_actions(CFG):
        raise P.ValidationError(f"action {kind!r} is not configured for this repo")
    open_nums = CACHE.open_numbers()

    def pr(v: object) -> int:
        return P.validate_pr(v, open_nums)

    if kind in ("reverify", "reverify_cancel", "merge"):
        n = pr(body.get("pr"))
        row = next((r for r in CACHE.snapshot()["prs"] if r["number"] == n), None)
        if not row or not row["is_train"]:
            raise P.ValidationError("only train PRs are reverified or merged here")
        if kind == "merge":
            return n, ["gh", "pr", "merge", str(n), "--merge", "--repo", CONFIG["gh_repo"]]
        return n, _configured(command(str(kind), pr=n))
    if kind == "close":
        n = pr(body.get("pr"))
        argv = ["gh", "pr", "close", str(n), "--repo", CONFIG["gh_repo"]]
        if body.get("comment"):
            argv += ["--comment", str(body["comment"])[:4000]]
        return n, argv
    if kind == "label":
        n = pr(body.get("pr"))
        flag = "--add-label" if body.get("add") else "--remove-label"
        return n, ["gh", "pr", "edit", str(n), flag, conv("maintainer_label") or "", "--repo", CONFIG["gh_repo"]]
    if kind == "train":
        nums = [pr(v) for v in _items(body.get("prs"), "prs")]
        if not nums or len(set(nums)) != len(nums):
            raise P.ValidationError("give distinct PRs")
        return "train", _configured(command("train", prs=nums, dry_run=dry_run(body, "train")))
    if kind == "rebuild":
        t = pr(body.get("train"))
        without = [pr(v) for v in _items(body.get("without"), "without")]
        if not without:
            raise P.ValidationError("--without needs at least one PR")
        return "train", _configured(command("rebuild", train=t, without=without, dry_run=dry_run(body, "rebuild")))
    if kind == "cleanup":
        b = P.validate_branch(body.get("branch", ""))
        return "branch:" + b, _configured(command("cleanup", branch=b, apply=bool(body.get("apply"))))
    raise P.ValidationError(f"unknown action {kind!r}")


def dry_run(body: dict[str, object], name: str) -> bool:
    """Whether a dry run is asked for; refused when the command has no {dry_run}."""
    if not body.get("dry_run"):
        return False
    if "{dry_run}" not in CFG["commands"][name]:
        raise P.ValidationError(f"commands.{name} has no dry run in this config")
    return True


def start_job(key: JobKey, argv: list[str]) -> Job:
    with JOBS_LOCK:
        if key in BUSY:
            raise P.ValidationError(f"an action for {key} is already running")
        BUSY.add(key)
        jid = secrets.token_hex(6)
        job: Job = {
            "id": jid,
            "key": key,
            "argv": argv,
            "started": time.time(),
            "output": [],
            "exit": None,
            "ended": None,
        }
        JOBS[jid] = job

    def worker() -> None:
        try:
            p = subprocess.Popen(
                argv,
                cwd=CONFIG["repo"],
                env=ENV,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
            )
            for line in p.stdout or ():
                job["output"].append(line.rstrip("\n"))
            job["exit"] = p.wait()
        except OSError as e:
            job["output"].append(str(e))
            job["exit"] = 255
        job["ended"] = time.time()
        with JOBS_LOCK:
            BUSY.discard(key)
        log_action(job_record(job) | {"output_tail": job["output"][-40:]})
        CACHE.status.pop(key, None)
        threading.Thread(target=CACHE.refresh, daemon=True).start()

    threading.Thread(target=worker, daemon=True).start()
    return job


def job_record(job: Job) -> dict[str, object]:
    """What actions.jsonl keeps of a job, before its output tail."""
    return {
        "id": job["id"],
        "key": job["key"],
        "argv": job["argv"],
        "started": job["started"],
        "ended": job["ended"],
        "exit": job["exit"],
    }


# ---------------------------------------------------------------- HTTP

DIST = HERE / "web" / "dist"
LEGACY = HERE / "static"
LEGACY_ASSETS = ("/app.js", "/app.css")
CONTENT_TYPES = {
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".css": "text/css",
    ".map": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
}
# The 403 page for `/` without a token: assets/unlock.js re-enters with this tab's stored token.
UNLOCK_PAGE = (
    b'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>PR console</title></head>'
    b'<body><p id="msg">Missing token. Open the URL that server.py printed.</p>'
    b'<script src="/assets/unlock.js"></script></body></html>'
)


def ui_dir() -> Path | None:
    """The built UI in web/dist, else the old page in static/, else None."""
    for d in (DIST, LEGACY):
        if (d / "index.html").is_file():
            return d
    return None


def static_asset(ui: Path | None, path: str) -> tuple[Path, str] | None:
    """(file, content type) for a token-free asset path, or None. Rejects path traversal."""
    if ui is None:
        return None
    if ui == LEGACY:
        if path in LEGACY_ASSETS and (LEGACY / path[1:]).is_file():
            return LEGACY / path[1:], CONTENT_TYPES[Path(path).suffix]
        return None
    if not path.startswith("/assets/"):
        return None
    name = urllib.parse.unquote(path[len("/assets/") :])
    if not name or "\\" in name or "\0" in name or any(part in ("", ".", "..") for part in name.split("/")):
        return None
    root = (DIST / "assets").resolve()
    f = (root / name).resolve()
    ctype = CONTENT_TYPES.get(f.suffix)
    if ctype is None or not f.is_relative_to(root) or not f.is_file():
        return None
    return f, ctype


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "pr-console"

    # Ignores fmt and args so that the query string (it may hold the token) is never logged.
    def log_message(self, fmt: str, *args: object) -> None:  # noqa: ARG002  the signature is the base class's
        sys.stderr.write(f"{self.command} {urllib.parse.urlsplit(self.path).path}\n")

    def _send(self, code: int, obj: object, ctype: str = "application/json") -> None:
        data = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self) -> bool:
        host = self.headers.get("Host", "")
        return host in (f"127.0.0.1:{CONFIG['port']}", f"localhost:{CONFIG['port']}")  # DNS rebinding guard

    def _authorised(self) -> bool:
        if not self._host_ok():
            return False
        url = urllib.parse.urlsplit(self.path)
        given = self.headers.get("X-Token") or ""
        # The query may carry the token only on the first load of the page and for the event
        # stream (EventSource cannot send headers). log_message never logs the query.
        if not given and (url.path in ("/", "/index.html") or EVENTS_RE.match(url.path)):
            given = (urllib.parse.parse_qs(url.query).get("token") or [""])[0]
        return hmac.compare_digest(given.encode(), TOKEN.encode())

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        ui = ui_dir()
        if self._host_ok():
            # Static, not secret: the browser fetches these without the token.
            asset = static_asset(ui, path)
            if asset is not None:
                return self._send(200, asset[0].read_bytes(), asset[1])
            if path.startswith("/assets/") or path in LEGACY_ASSETS:
                return self._send(404, {"error": "not found"})
        if not self._authorised():
            if self._host_ok() and path in ("/", "/index.html"):
                return self._send(403, UNLOCK_PAGE, "text/html; charset=utf-8")
            return self._send(403, {"error": "missing or wrong token"})
        if path in ("/", "/index.html"):
            if ui is None:
                return self._send(
                    503, b"No UI built: run `npm ci && npm run build` in web/.", "text/plain; charset=utf-8"
                )
            return self._send(200, (ui / "index.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/api/config":
            return self._send(200, K.public(CFG))
        if path == "/api/prs":
            return self._send(200, CACHE.snapshot())
        if path == "/api/sessions":
            try:
                return self._send(200, sessions())
            except Exception as e:  # noqa: BLE001
                return self._send(500, {"error": str(e)})
        if path == "/api/jobs":
            return self._send(200, [{k: v for k, v in j.items() if k != "output"} for j in JOBS.values()])
        if path.startswith("/api/jobs/"):
            j = JOBS.get(path.rsplit("/", 1)[1])
            return self._send(200, j) if j else self._send(404, {"error": "no such job"})
        if path.startswith("/api/chats"):
            return self._chats_get(path)
        if path == "/api/history":
            try:
                lines = Path(CONFIG["log_file"]).read_text(encoding="utf-8").splitlines()[-100:]
            except OSError:
                lines = []
            return self._send(200, [json.loads(x) for x in lines])
        return self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if not self._authorised() or self.headers.get("Content-Type", "") != "application/json":
            return self._send(403, {"error": "missing or wrong token"})
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return self._send(400, {"error": "bad Content-Length"})
        if length > MAX_BODY:
            self.close_connection = True
            if length <= 16 * MAX_BODY:  # read it, so the client sees the answer and not a reset
                self.rfile.read(length)
            return self._send(413, {"error": "request too large"})
        try:
            body: object = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad json"})
        if not isinstance(body, dict):
            return self._send(400, {"error": "the body must be a JSON object"})
        if path.startswith("/api/chats"):
            return self._chats_post(path, body)
        if path == "/api/refresh":
            threading.Thread(target=CACHE.refresh, kwargs={"force": True}, daemon=True).start()
            return self._send(202, {"ok": True})
        if path == "/api/action":
            try:
                key, argv = build_action(body)
                job = start_job(key, argv)
            except P.ValidationError as e:
                return self._send(400, {"error": str(e)})
            except RuntimeError as e:
                return self._send(502, {"error": str(e)})
            return self._send(202, {"id": job["id"], "argv": argv})
        return self._send(404, {"error": "not found"})

    def _chats_get(self, path: str) -> None:
        import chats as C

        if CHATS is None:
            return self._send(503, {"error": CHATS_ERROR})
        try:
            if path == "/api/chats":
                return self._send(200, chat_rows(CHATS))
            if path == "/api/chats/dirs":
                return self._send(200, chat_dirs())
            m = EVENTS_RE.match(path)
            if m:
                return self._stream(CHATS.get(m.group(1)))
        except C.ChatError as e:
            return self._send(400, {"error": str(e)})
        return self._send(404, {"error": "not found"})

    def _stream(self, chat: "C.Chat") -> None:
        """Server-sent events: replay what follows Last-Event-ID (or ?after=), then follow live."""
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        raw = self.headers.get("Last-Event-ID") or (q.get("after") or ["0"])[0]
        last = int(raw) if raw.isdigit() else 0
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(b"retry: 1000\n\n")
            self.wfile.flush()
            while (
                not STOPPING.is_set() and not chat.deleted
            ):  # until the client goes away, the server stops or the chat is deleted
                evs = chat.buffer.after(last, timeout=15)
                if not evs:
                    self.wfile.write(b": ping\n\n")
                for ev in evs:
                    self.wfile.write(f"id: {ev['id']}\ndata: {json.dumps(ev)}\n\n".encode())
                    last = ev["id"]
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _chats_post(self, path: str, body: dict[str, object]) -> None:
        import chats as C

        if CHATS is None:
            return self._send(503, {"error": CHATS_ERROR})
        try:
            if path == "/api/chats":
                dirs = chat_dirs()
                main = next((d["path"] for d in dirs if d.get("is_main")), None)
                if main is None:
                    return self._send(500, {"error": "no main checkout in git worktree list"})
                cwd = C.validate_cwd(main, dirs)  # a coordinator works from the main checkout
                prompt = C.validate_prompt(body.get("prompt"))
                raw_model = "opus" if body.get("model") is None else body.get("model")
                model = C.validate_choice(raw_model, C.MODELS, "model")
                mode = C.validate_choice(body.get("permission_mode") or "auto", C.PERMISSION_MODES, "permission mode")
                return self._send(201, CHATS.launch(cwd, prompt, model, mode))
            if path == "/api/chats/resume":
                cid = C.validate_session_id(body.get("id"))
                given_cwd = C.validate_cwd(body["cwd"], chat_dirs()) if body.get("cwd") else None
                first = C.validate_prompt(body["prompt"]) if body.get("prompt") else None
                return self._send(200, CHATS.resume(cid, given_cwd, first))
            d = CHAT_DELETE_RE.match(path)
            if d:  # any id shape: a malformed one is a 400 from validation, not a 404
                CHATS.delete(d.group(1))
                return self._send(200, {"ok": True})
            m = CHAT_POST_RE.match(path)
            if m:
                cid, verb = m.groups()
                if verb == "send":
                    return self._send(200, CHATS.send(cid, C.validate_prompt(body.get("text"))))
                if verb == "interrupt":
                    CHATS.interrupt(cid)
                elif verb == "end":
                    CHATS.end(cid)
                else:
                    CHATS.decide(cid, body.get("req_id"), body.get("allow"))
                return self._send(200, {"ok": True})
        except C.ChatError as e:
            return self._send(400, {"error": str(e)})
        return self._send(404, {"error": "not found"})


# ---------------------------------------------------------------- chats

MAX_BODY = 65536
EVENTS_RE = re.compile(r"^/api/chats/([0-9a-f-]{36})/events$")
CHAT_POST_RE = re.compile(r"^/api/chats/([0-9a-f-]{36})/(send|interrupt|end|permission)$")
CHAT_DELETE_RE = re.compile(r"^/api/chats/([^/]+)/delete$")
CHATS: "C.ChatManager | None" = None  # set by main() when claude-agent-sdk is installed
CHATS_ERROR = "chats are off: install claude-agent-sdk in .venv (see README.md)"
BRANCH_CACHE: dict[str, tuple[float, str]] = {}  # cwd -> (time, branch)


class ChatRow(TypedDict):
    """A row of GET /api/chats: the chat, with the branch of its directory and that branch's PR."""

    id: str
    cwd: str
    created: float
    title: str
    model: str
    permission_mode: str
    resume_id: str | None
    status: str
    status_note: str
    connected: bool
    last_activity: float
    pending: list["C.PendingPermission"]
    last_event_id: int
    branch: str
    open_pr: int | None


def chat_dirs() -> "list[C.ChatDir]":
    import chats as C

    return C.worktree_dirs(CONFIG["repo"])


def branch_of(cwd: str) -> str:
    hit = BRANCH_CACHE.get(cwd)
    if hit and time.time() - hit[0] < 15:
        return hit[1]
    rc, out, _ = run(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"], timeout=10)
    b = out.strip() if rc == 0 else ""
    BRANCH_CACHE[cwd] = (time.time(), b)
    return b


def chat_rows(chats: "C.ChatManager") -> list[ChatRow]:
    open_by_branch = {r["branch"]: r["number"] for r in CACHE.snapshot()["prs"]}
    rows: list[ChatRow] = []
    for r in chats.list():
        branch = branch_of(r["cwd"])
        rows.append(
            {**r, "branch": branch, "open_pr": open_by_branch.get(branch) if branch not in ("", "HEAD") else None}
        )
    return rows


STOPPING = threading.Event()


def start_chats(
    state_dir: str, client_factory: "C.ClientFactory | None" = None, prompt_append: str = ""
) -> "C.ChatManager | None":
    """Start the chat manager, or leave chats off when the SDK is not installed."""
    global CHATS  # noqa: PLW0603  one chat manager per process, read by every request handler
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        print(CHATS_ERROR, flush=True)
        return None
    import chats as C

    CHATS = C.ChatManager(state_dir, client_factory, prompt_append)
    return CHATS


def main() -> None:
    global TOKEN  # noqa: PLW0603  the token is fixed once at startup, read by every request handler
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--config",
        default=os.environ.get("PR_CONSOLE_CONFIG"),
        help="the per-repo TOML config (else $PR_CONSOLE_CONFIG); see examples/",
    )
    ap.add_argument("--port", type=int, default=CONFIG["port"])
    ap.add_argument("--chats-dir", default=str(HERE / "chats"))
    a = ap.parse_args()
    try:
        cfg = K.load(a.config)
    except K.ConfigError as e:
        sys.exit(f"pr-console: {e}")
    try:
        K.check_repo_dir(cfg)
    except K.ConfigError as e:
        sys.exit(f"pr-console: {e}")
    use_config(cfg)
    CONFIG["port"] = a.port
    TOKEN = os.environ.get("PR_CONSOLE_TOKEN") or secrets.token_urlsafe(24)
    start_chats(a.chats_dir, prompt_append=cfg["coordinator"]["prompt_append"])
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", CONFIG["port"]), Handler)
    threading.Thread(target=refresher, daemon=True).start()
    print(f"PR console for {CONFIG['repo']} ({CONFIG['gh_repo']})", flush=True)
    print(f"open http://127.0.0.1:{CONFIG['port']}/?token={TOKEN}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        STOPPING.set()
        if CHATS is not None:
            CHATS.shutdown()


if __name__ == "__main__":
    main()
