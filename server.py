"""PR console: a local web app for managing a repo's pull requests. Run: python3 -I server.py --config <file>"""

import argparse
import concurrent.futures
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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as K  # noqa: E402
import parsers as P  # noqa: E402

HERE = Path(__file__).resolve().parent
CFG = None  # the checked per-repo config (config.load), set by main() or use_config()
CONFIG = {
    "repo": "",
    "gh_repo": "",
    "port": 8765,
    "concurrency": 4,
    "refresh_seconds": 180,
    "log_file": str(HERE / "actions.jsonl"),
}
TOKEN = ""
ENV = dict(os.environ, NO_COLOR="1", GH_FORCE_TTY="0", GH_NO_UPDATE_NOTIFIER="1", GH_PROMPT_DISABLED="1")


def run(args, timeout=300):
    """Run a command (argument list, never a shell) in the managed repo's checkout."""
    try:
        p = subprocess.run(args, cwd=CONFIG["repo"], env=ENV, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return 255, "", str(e)


def gh_json(args, timeout=120):
    rc, out, err = run(["gh", *args, "--repo", CONFIG["gh_repo"]], timeout)
    if rc != 0:
        raise RuntimeError(err.strip() or f"gh exited {rc}")
    return json.loads(out or "null")


def use_config(cfg):
    """Install a checked config (config.load)."""
    global CFG
    CFG = cfg
    CONFIG.update(repo=cfg["repo"], gh_repo=cfg["gh_repo"])


def command(name, **values):
    """The argv of a configured command; None when the config leaves it out."""
    return K.expand(CFG, name, **values) if name in CFG["commands"] else None


def conv(key, default=None):
    return CFG["conventions"].get(key, default)


# ---------------------------------------------------------------- PR state cache

class Cache:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = {"prs": [], "fetched_at": None, "error": None, "refreshing": False}
        self.status = {}  # pr -> {head, verdict...}

    def open_numbers(self):
        """Fresh list of open PR numbers, for validation of actions."""
        prs = gh_json(["pr", "list", "--state", "open", "--limit", "300", "--json", "number"])
        return {p["number"] for p in prs}

    def refresh(self):
        with self.lock:
            if self.data["refreshing"]:
                return
            self.data["refreshing"] = True
        try:
            prs = gh_json(["pr", "list", "--state", "open", "--limit", "300", "--json",
                           "number,title,headRefName,headRefOid,baseRefName,author,createdAt,"
                           "labels,reviews,comments,isDraft,body"])
            rows = [self._row(p) for p in prs]
            with concurrent.futures.ThreadPoolExecutor(CONFIG["concurrency"]) as ex:
                list(ex.map(self._enrich, rows))
            rows.sort(key=lambda r: -r["number"])
            with self.lock:
                self.data.update(prs=rows, fetched_at=time.time(), error=None)
        except Exception as e:  # noqa: BLE001 - surfaced to the UI
            with self.lock:
                self.data["error"] = str(e)
        finally:
            with self.lock:
                self.data["refreshing"] = False

    def _row(self, p):
        branch = p["headRefName"]
        return {
            "number": p["number"], "title": p["title"], "branch": branch,
            "scope": P.scope_of(branch), "author": (p.get("author") or {}).get("login", ""),
            "head": p["headRefOid"], "base": p["baseRefName"], "created_at": p["createdAt"],
            "draft": p.get("isDraft", False),
            "labels": [lb["name"] for lb in p.get("labels") or []],
            "review": P.review_state(p.get("reviews"), p.get("comments")),
            "is_train": bool(conv("train_branch_prefix")) and branch.startswith(conv("train_branch_prefix")),
            "constituents": [], "verify_run": None, "body": p.get("body") or "",
        }

    def _enrich(self, r):
        n = r["number"]
        args = command("train_verdict" if r["is_train"] else "verdict", pr=n)
        cached = self.status.get(n)
        if args is None:
            v = None
        elif cached and cached["head"] == r["head"] and time.time() - cached["at"] < CONFIG["refresh_seconds"]:
            v = cached["verdict"]
        else:
            for attempt in range(3):  # concurrent pr-status calls race on `git fetch`'s ref lock
                rc, out, err = run(args, timeout=240)
                v = P.parse_verdict(out, n, CFG["verdicts"])
                if v["verdict"] != "UNKNOWN" or "cannot lock ref" not in err:
                    break
                time.sleep(2 + 3 * attempt)
            if v["verdict"] == "UNKNOWN":
                v["line"] = (err.strip().splitlines() or [f"pr-status exited {rc}"])[-1]
            self.status[n] = {"head": r["head"], "at": time.time(), "verdict": v}
        if v is not None:
            r["status"] = v
        if r["is_train"]:
            msgs = []
            try:
                commits = gh_json(["pr", "view", str(n), "--json", "commits"])["commits"]
                msgs = [c.get("messageHeadline", "") + "\n" + c.get("messageBody", "") for c in commits]
            except Exception:  # noqa: BLE001
                pass
            r["constituents"] = P.train_constituents(r["title"], r["body"], msgs,
                                                     conv("train_title_prefix", ""))
            try:
                runs = gh_json(["run", "list", "--workflow", conv("verify_workflow"), "--branch", r["branch"],
                                "-L", "1", "--json", "databaseId,status,conclusion,url,headSha"]) \
                    if conv("verify_workflow") else []
                r["verify_run"] = runs[0] if runs else None
            except Exception as e:  # noqa: BLE001
                r["verify_run"] = {"error": str(e)}
        r["summary"] = P.pr_summary(r["body"])
        r["url"] = f"https://github.com/{CONFIG['gh_repo']}/pull/{n}"
        r.pop("body", None)

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.data))


CACHE = Cache()


def refresher():
    while True:  # periodic refresh
        CACHE.refresh()
        time.sleep(CONFIG["refresh_seconds"])


# ---------------------------------------------------------------- sessions

def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (ProcessLookupError, OverflowError):
        return False


def sessions():
    rc, out, err = run(["git", "worktree", "list", "--porcelain"])
    if rc != 0:
        raise RuntimeError(err)
    wts = P.parse_porcelain(out)
    try:
        merged = gh_json(["pr", "list", "--state", "merged", "--limit", "300", "--json", "number,headRefName"])
    except Exception:  # noqa: BLE001
        merged = []
    merged_by_branch = {m["headRefName"]: m["number"] for m in merged}
    open_by_branch = {r["branch"]: r["number"] for r in CACHE.snapshot()["prs"]}
    main = str(Path(CONFIG["repo"]).resolve())
    wt_dir = conv("worktrees_dir")
    wt_groups = conv("worktree_groups", {})
    groups = {}
    for w in wts:
        rc, st, _ = run(["git", "-C", w["path"], "status", "--porcelain"], timeout=60)
        w["dirty"] = None if rc != 0 else bool(st.strip())
        w["open_pr"] = open_by_branch.get(w["branch"])
        w["merged_pr"] = merged_by_branch.get(w["branch"])
        lk = P.parse_lock_reason(w["lock_reason"]) if w["locked"] else None
        if lk:
            lk["alive"] = pid_alive(lk["pid"])
        w["claude_lock"] = lk
        w["is_main"] = str(Path(w["path"]).resolve()) == main
        w["ready_to_clean"] = bool(not w["is_main"] and w["dirty"] is False and not w["locked"]
                                   and w["merged_pr"] and not w["open_pr"])
        if w["is_main"]:
            key = "main checkout"
        elif lk:
            key = f"{lk['agent']} (pid {lk['pid']}, {'alive' if lk['alive'] else 'gone'})"
        else:
            key = next((g for g, prefix in wt_groups.items() if f"/{wt_dir}/{prefix}" in w["path"]),
                       "unlocked worktrees")
        groups.setdefault(key, []).append(w)
    rc, common, _ = run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"])
    locks = []
    lock_dir = conv("reverify_lock_dir")
    state_dir = Path(common.strip()) / lock_dir if lock_dir else None
    if rc == 0 and state_dir is not None and state_dir.is_dir():
        for f in sorted(state_dir.glob("*.lock")):
            n = f.name[:-5]
            if not n.isdigit():
                continue
            try:
                holder = P.parse_reverify_lock(os.readlink(f))
            except OSError:
                holder = None
            argv = command("reverify_status", pr=int(n))
            st = run(argv, timeout=30)[1] if argv else ""
            locks.append({"pr": int(n), "holder": holder,
                          "alive": bool(holder and pid_alive(holder["pid"])),
                          "status": st.strip()})
    train_status = []
    for r in CACHE.snapshot()["prs"]:
        argv = command("reverify_status", pr=r["number"]) if r["is_train"] else None
        if argv:
            rc2, st, _ = run(argv, timeout=30)
            train_status.append({"pr": r["number"], "exit": rc2, "status": st.strip()})
    return {"groups": groups, "reverify_locks": locks, "reverify_state_dir": str(state_dir or ""),
            "train_reverify_status": train_status}


# ---------------------------------------------------------------- actions

JOBS, JOBS_LOCK, BUSY = {}, threading.Lock(), set()


def log_action(entry):
    with open(CONFIG["log_file"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def build_action(body):
    """Validate a request and return (key, argv, description). Raises ValidationError."""
    kind = body.get("kind")
    if kind in K.ACTION_COMMANDS or kind == "label":
        if kind not in K.enabled_actions(CFG):
            raise P.ValidationError(f"action {kind!r} is not configured for this repo")
    open_nums = CACHE.open_numbers()
    pr = lambda v: P.validate_pr(v, open_nums)  # noqa: E731
    if kind in ("reverify", "reverify_cancel", "merge"):
        n = pr(body.get("pr"))
        row = next((r for r in CACHE.snapshot()["prs"] if r["number"] == n), None)
        if not row or not row["is_train"]:
            raise P.ValidationError("only train PRs are reverified or merged here")
        if kind == "merge":
            return n, ["gh", "pr", "merge", str(n), "--merge", "--repo", CONFIG["gh_repo"]]
        return n, command(kind, pr=n)
    if kind == "close":
        n = pr(body.get("pr"))
        argv = ["gh", "pr", "close", str(n), "--repo", CONFIG["gh_repo"]]
        if body.get("comment"):
            argv += ["--comment", str(body["comment"])[:4000]]
        return n, argv
    if kind == "label":
        n = pr(body.get("pr"))
        flag = "--add-label" if body.get("add") else "--remove-label"
        return n, ["gh", "pr", "edit", str(n), flag, conv("maintainer_label"), "--repo", CONFIG["gh_repo"]]
    if kind == "train":
        nums = [pr(v) for v in body.get("prs") or []]
        if not nums or len(set(nums)) != len(nums):
            raise P.ValidationError("give distinct PRs")
        return "train", command("train", prs=nums, dry_run=dry_run(body, "train"))
    if kind == "rebuild":
        t = pr(body.get("train"))
        without = [pr(v) for v in body.get("without") or []]
        if not without:
            raise P.ValidationError("--without needs at least one PR")
        return "train", command("rebuild", train=t, without=without, dry_run=dry_run(body, "rebuild"))
    if kind == "cleanup":
        b = P.validate_branch(body.get("branch", ""))
        return "branch:" + b, command("cleanup", branch=b, apply=bool(body.get("apply")))
    raise P.ValidationError(f"unknown action {kind!r}")


def dry_run(body, name):
    """Whether a dry run is asked for; refused when the command has no {dry_run}."""
    if not body.get("dry_run"):
        return False
    if "{dry_run}" not in CFG["commands"][name]:
        raise P.ValidationError(f"commands.{name} has no dry run in this config")
    return True


def start_job(key, argv):
    with JOBS_LOCK:
        if key in BUSY:
            raise P.ValidationError(f"an action for {key} is already running")
        BUSY.add(key)
        jid = secrets.token_hex(6)
        job = {"id": jid, "key": key, "argv": argv, "started": time.time(), "output": [],
               "exit": None, "ended": None}
        JOBS[jid] = job

    def worker():
        try:
            p = subprocess.Popen(argv, cwd=CONFIG["repo"], env=ENV, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True)
            for line in p.stdout:
                job["output"].append(line.rstrip("\n"))
            job["exit"] = p.wait()
        except OSError as e:
            job["output"].append(str(e))
            job["exit"] = 255
        job["ended"] = time.time()
        with JOBS_LOCK:
            BUSY.discard(key)
        log_action({k: job[k] for k in ("id", "key", "argv", "started", "ended", "exit")}
                   | {"output_tail": job["output"][-40:]})
        CACHE.status.pop(key, None)
        threading.Thread(target=CACHE.refresh, daemon=True).start()

    threading.Thread(target=worker, daemon=True).start()
    return job


# ---------------------------------------------------------------- HTTP

DIST = HERE / "web" / "dist"
LEGACY = HERE / "static"
LEGACY_ASSETS = ("/app.js", "/app.css")
CONTENT_TYPES = {".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css",
                 ".map": "application/json", ".svg": "image/svg+xml", ".png": "image/png",
                 ".ico": "image/x-icon", ".woff2": "font/woff2", ".woff": "font/woff"}
# The 403 page for `/` without a token: assets/unlock.js re-enters with this tab's stored token.
UNLOCK_PAGE = (b'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>PR console</title></head>'
               b'<body><p id="msg">Missing token. Open the URL that server.py printed.</p>'
               b'<script src="/assets/unlock.js"></script></body></html>')


def ui_dir():
    """The built UI in web/dist, else the old page in static/, else None."""
    for d in (DIST, LEGACY):
        if (d / "index.html").is_file():
            return d
    return None


def static_asset(ui, path):
    """(file, content type) for a token-free asset path, or None. Rejects path traversal."""
    if ui is None:
        return None
    if ui == LEGACY:
        if path in LEGACY_ASSETS and (LEGACY / path[1:]).is_file():
            return LEGACY / path[1:], CONTENT_TYPES[Path(path).suffix]
        return None
    if not path.startswith("/assets/"):
        return None
    name = urllib.parse.unquote(path[len("/assets/"):])
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

    def log_message(self, fmt, *args):  # never log the query string (it may hold the token)
        sys.stderr.write(f"{self.command} {urllib.parse.urlsplit(self.path).path}\n")

    def _send(self, code, obj, ctype="application/json"):
        data = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self):
        host = self.headers.get("Host", "")
        return host in (f"127.0.0.1:{CONFIG['port']}", f"localhost:{CONFIG['port']}")  # DNS rebinding guard

    def _authorised(self):
        if not self._host_ok():
            return False
        url = urllib.parse.urlsplit(self.path)
        given = self.headers.get("X-Token") or ""
        # The query may carry the token only on the first load of the page and for the event
        # stream (EventSource cannot send headers). log_message never logs the query.
        if not given and (url.path in ("/", "/index.html") or EVENTS_RE.match(url.path)):
            given = (urllib.parse.parse_qs(url.query).get("token") or [""])[0]
        return hmac.compare_digest(given.encode(), TOKEN.encode())

    def do_GET(self):
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
                return self._send(503, b"No UI built: run `npm ci && npm run build` in web/.", "text/plain; charset=utf-8")
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

    def do_POST(self):
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
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad json"})
        if not isinstance(body, dict):
            return self._send(400, {"error": "the body must be a JSON object"})
        if path.startswith("/api/chats"):
            return self._chats_post(path, body)
        if path == "/api/refresh":
            threading.Thread(target=CACHE.refresh, daemon=True).start()
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


# ---------------------------------------------------------------- chats

MAX_BODY = 65536
EVENTS_RE = re.compile(r"^/api/chats/([0-9a-f-]{36})/events$")
CHAT_POST_RE = re.compile(r"^/api/chats/([0-9a-f-]{36})/(send|interrupt|end|permission)$")
CHATS = None          # chats.ChatManager, set by main() when claude-agent-sdk is installed
CHATS_ERROR = "chats are off: install claude-agent-sdk in .venv (see README.md)"
BRANCH_CACHE = {}     # cwd -> (time, branch)


def chat_dirs():
    import chats as C
    return C.worktree_dirs(CONFIG["repo"])


def branch_of(cwd):
    hit = BRANCH_CACHE.get(cwd)
    if hit and time.time() - hit[0] < 15:
        return hit[1]
    rc, out, _ = run(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"], timeout=10)
    b = out.strip() if rc == 0 else ""
    BRANCH_CACHE[cwd] = (time.time(), b)
    return b


def chat_rows():
    open_by_branch = {r["branch"]: r["number"] for r in CACHE.snapshot()["prs"]}
    rows = CHATS.list()
    for r in rows:
        r["branch"] = branch_of(r["cwd"])
        r["open_pr"] = open_by_branch.get(r["branch"]) if r["branch"] not in ("", "HEAD") else None
    return rows


def _chats_get(self, path):
    import chats as C
    if CHATS is None:
        return self._send(503, {"error": CHATS_ERROR})
    try:
        if path == "/api/chats":
            return self._send(200, chat_rows())
        if path == "/api/chats/dirs":
            return self._send(200, chat_dirs())
        m = EVENTS_RE.match(path)
        if m:
            return self._stream(CHATS.get(m.group(1)))
    except C.ChatError as e:
        return self._send(400, {"error": str(e)})
    return self._send(404, {"error": "not found"})


def _stream(self, chat):
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
        while not STOPPING.is_set():  # bound: until the client goes away or the server stops
            evs = chat.buffer.after(last, timeout=15)
            if not evs:
                self.wfile.write(b": ping\n\n")
            for ev in evs:
                self.wfile.write(f"id: {ev['id']}\ndata: {json.dumps(ev)}\n\n".encode())
                last = ev["id"]
            self.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass


def _chats_post(self, path, body):
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
            model = C.validate_choice(body.get("model"), C.MODELS, "model")
            mode = C.validate_choice(body.get("permission_mode") or "auto", C.PERMISSION_MODES,
                                     "permission mode")
            return self._send(201, CHATS.launch(cwd, prompt, model, mode))
        if path == "/api/chats/resume":
            cid = C.validate_session_id(body.get("id"))
            cwd = C.validate_cwd(body["cwd"], chat_dirs()) if body.get("cwd") else None
            prompt = C.validate_prompt(body["prompt"]) if body.get("prompt") else None
            return self._send(200, CHATS.resume(cid, cwd, prompt))
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


Handler._chats_get = _chats_get
Handler._chats_post = _chats_post
Handler._stream = _stream
STOPPING = threading.Event()


def start_chats(state_dir, client_factory=None, prompt_append=""):
    """Start the chat manager, or leave chats off when the SDK is not installed."""
    global CHATS
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        print(CHATS_ERROR, flush=True)
        return None
    import chats as C
    CHATS = C.ChatManager(state_dir, client_factory, prompt_append)
    return CHATS


def main():
    global TOKEN
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.environ.get("PR_CONSOLE_CONFIG"),
                    help="the per-repo TOML config (else $PR_CONSOLE_CONFIG); see examples/")
    ap.add_argument("--port", type=int, default=CONFIG["port"])
    ap.add_argument("--chats-dir", default=str(HERE / "chats"))
    a = ap.parse_args()
    try:
        cfg = K.load(a.config)
    except K.ConfigError as e:
        sys.exit(f"pr-console: {e}")
    if not Path(cfg["repo"]).is_dir():
        sys.exit(f"pr-console: config repo {cfg['repo']} is not a directory")
    use_config(cfg)
    CONFIG.update(port=a.port)
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
