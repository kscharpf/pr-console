"""Pure parsers and validators for PR console. No I/O here, so the tests need no network."""

import re
from collections.abc import Collection, Iterable
from typing import TypedDict

MARKER = "🤖 Generated with [Claude Code](https://claude.com/claude-code)"
BRANCH_RE = re.compile(r"[A-Za-z0-9._/-]+")
PR_RE = re.compile(r"[0-9]{1,6}")


class ValidationError(ValueError):
    pass


class Verdict(TypedDict):
    verdict: str
    reasons: list[str]
    line: str


class Worktree(TypedDict):
    """One entry of `git worktree list --porcelain`."""
    path: str
    head: str
    branch: str
    detached: bool
    bare: bool
    locked: bool
    lock_reason: str
    prunable: bool


class LockReason(TypedDict):
    agent: str
    pid: int
    start: str


class ReverifyLock(TypedDict):
    pid: int
    start: str


class GhAuthor(TypedDict, total=False):
    login: str


class GhReview(TypedDict, total=False):
    """A review in `gh pr list --json reviews`; only the keys read here."""
    state: str
    submittedAt: str
    author: GhAuthor | None


class GhComment(TypedDict, total=False):
    """A comment in `gh pr list --json comments`; only the keys read here."""
    createdAt: str
    body: str | None


class ReviewState(TypedDict):
    state: str
    author: str
    at: str
    answered: bool | None


def validate_pr(value: object, open_numbers: Collection[int]) -> int:
    """Return the PR number as int if it is digits only and open in the repo."""
    s = str(value).strip()
    if not PR_RE.fullmatch(s):
        raise ValidationError(f"not a PR number: {s!r}")
    n = int(s)
    if n not in open_numbers:
        raise ValidationError(f"PR #{n} is not open")
    return n


def validate_branch(value: object) -> str:
    s = str(value)
    if not BRANCH_RE.fullmatch(s) or s.startswith(("-", "/")) or ".." in s:
        raise ValidationError(f"invalid branch name: {s!r}")
    return s


def scope_of(branch: str) -> str:
    """The branch prefix, e.g. 'a2/' for 'a2/foo'; '' when there is none."""
    return branch.split("/", 1)[0] + "/" if "/" in branch else ""


def parse_verdict(output: str, n: int, verdicts: dict[str, str]) -> Verdict:
    """Find the `#<n>: <verdict>` line of the verdict command.

    `verdicts` is the config's [verdicts] table: the line's rest is exactly `ready` or
    `ready_for_train`, or `not_ready` followed by `: reason; reason`. Returns {"verdict": that
    string or "UNKNOWN", "reasons": [...], "line"}.
    """
    prefix = f"#{n}: "
    ready = (verdicts["ready"], verdicts["ready_for_train"])
    not_ready = verdicts["not_ready"]
    for line in reversed(output.splitlines()):
        if not line.startswith(prefix):
            continue
        rest = line[len(prefix):].strip()
        if rest in ready:
            return {"verdict": rest, "reasons": [], "line": line}
        if rest.startswith(not_ready + ":"):
            reasons = [r.strip() for r in rest[len(not_ready) + 1:].split(";") if r.strip()]
            return {"verdict": not_ready, "reasons": reasons, "line": line}
    return {"verdict": "UNKNOWN", "reasons": [], "line": ""}


def parse_porcelain(text: str) -> list[Worktree]:
    """Parse `git worktree list --porcelain` into a list of dicts."""
    out: list[Worktree] = []
    cur: Worktree | None = None
    for line in text.splitlines():
        if not line.strip():
            if cur:
                out.append(cur)
            cur = None
            continue
        key, _, val = line.partition(" ")
        if key == "worktree":
            if cur:
                out.append(cur)
            cur = {"path": val, "head": "", "branch": "", "detached": False, "bare": False,
                   "locked": False, "lock_reason": "", "prunable": False}
        elif cur is None:
            continue
        elif key == "HEAD":
            cur["head"] = val
        elif key == "branch":
            cur["branch"] = val.removeprefix("refs/heads/")
        elif key == "detached":
            cur["detached"] = True
        elif key == "bare":
            cur["bare"] = True
        elif key == "locked":
            cur["locked"] = True
            cur["lock_reason"] = val
        elif key == "prunable":
            cur["prunable"] = True
    if cur:
        out.append(cur)
    return out


LOCK_RE = re.compile(r"^claude agent (agent-[A-Za-z0-9]+) \(pid (\d+)(?: start ([^)]*))?\)$")


def parse_lock_reason(reason: str) -> LockReason | None:
    """Parse a Claude Code lock reason; None when it is not one."""
    m = LOCK_RE.match(reason.strip())
    if not m:
        return None
    return {"agent": m.group(1), "pid": int(m.group(2)), "start": m.group(3) or ""}


def parse_reverify_lock(target: str) -> ReverifyLock | None:
    """A reverify lock symlink target `<pid>|<start>`."""
    pid, sep, start = target.partition("|")
    if not sep or not pid.isdigit():
        return None
    return {"pid": int(pid), "start": start}


_BODY_RE = re.compile(r"^- #(\d+)\b", re.M)
_MERGE_RE = re.compile(r"^Merge pull request #(\d+) from ", re.M)


def train_constituents(title: str = "", body: str | None = "", commit_messages: Iterable[str] = (),
                       title_prefix: str = "") -> list[int]:
    """Constituent PRs of a train, in order: the body's `- #<n>` lines, else the merge
    commits, else the `#<n>` after the first comma of a title that starts with `title_prefix`."""
    nums = [int(x) for x in _BODY_RE.findall(body or "")]
    if nums:
        return nums
    for msg in commit_messages:
        nums += [int(x) for x in _MERGE_RE.findall(msg)]
    if nums:
        return nums
    if title_prefix and title.startswith(title_prefix) and "," in title:
        return [int(x) for x in re.findall(r"#(\d+)", title.split(",", 1)[1])]
    return []


def review_state(reviews: list[GhReview] | None, comments: list[GhComment] | None) -> ReviewState:
    """Latest submitted review and whether a later PR comment ends in the marker."""
    subs = [r for r in reviews or [] if r.get("submittedAt") and r.get("state") != "PENDING"]
    if not subs:
        return {"state": "NONE", "author": "", "at": "", "answered": None}
    last = max(subs, key=lambda r: r["submittedAt"])
    answered = False
    for c in comments or []:
        if c.get("createdAt", "") > last["submittedAt"]:
            lines = [ln for ln in (c.get("body") or "").splitlines() if ln.strip()]
            if lines and lines[-1].strip() == MARKER:
                answered = True
    return {"state": last.get("state", ""), "author": (last.get("author") or {}).get("login", ""),
            "at": last["submittedAt"], "answered": answered}


def pr_summary(body: str | None, limit: int = 700) -> str:
    """The text of the body's `## Summary` section (else its first paragraph), cut to `limit`."""
    body = (body or "").replace("\r\n", "\n")
    m = re.search(r"^##+\s*Summary\s*\n(.*?)(?=^##+\s|\Z)", body, re.S | re.M | re.I)
    text = (m.group(1) if m else body.split("\n\n", 1)[0]).strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"
