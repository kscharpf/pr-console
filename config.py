"""The per-repo config of the PR console: one TOML file (see README.md, "Config").

load() checks the whole file once and returns a plain dict; a bad file raises ConfigError with a
message that names the key. expand() turns a command template into an argument list. It
substitutes each placeholder within one argument and never goes through a shell; the caller
validates every value (PR numbers, branch names) before it gets here.
"""

import re
import tomllib
from pathlib import Path
from typing import Literal, TypedDict

HERE = Path(__file__).resolve().parent

# A command template: arguments, each a string or a nested array of strings (see _template).
Template = list[str | list[str]]
# A placeholder value; expand() gets them already validated.
Value = str | int | bool | list[int]


class Conventions(TypedDict, total=False):
    train_branch_prefix: str
    train_title_prefix: str
    verify_workflow: str
    maintainer_label: str
    worktrees_dir: str
    reverify_lock_dir: str
    worktree_groups: dict[str, str]


class Coordinator(TypedDict):
    prompt_append: str
    description: str


class Config(TypedDict):
    """The checked config load() returns."""
    name: str
    repo: str
    gh_repo: str
    commands: dict[str, Template]
    flags: dict[str, str]
    conventions: Conventions
    verdicts: dict[str, str]
    coordinator: Coordinator


class PublicCoordinator(TypedDict):
    description: str


class PublicConfig(TypedDict):
    """GET /api/config."""
    name: str
    repo: str
    gh_repo: str
    conventions: Conventions
    verdicts: dict[str, str]
    commands: dict[str, Template]
    flags: dict[str, str]
    actions: list[str]
    coordinator: PublicCoordinator


class ConfigError(ValueError):
    pass


# Placeholder kinds. A scalar may sit inside an argument ("--pr={pr}"); a list or a flag must be
# the whole argument. A list expands to one argument per item; a nested array in a template
# repeats once per item of the list placeholder inside it (["--without", "{without}"]). A flag
# expands to the configured flag when it is set, else to nothing.
SCALARS = {"pr", "train", "branch"}
LISTS = {"prs", "without"}
FLAGS = {"dry_run": "dry_run_flag", "apply": "apply_flag"}

# command name -> (placeholders it must use, placeholders it may use)
COMMANDS = {
    "verdict": ({"pr"}, set()),          # verdict of an ordinary PR
    "train_verdict": ({"pr"}, set()),    # verdict of a train PR
    "reverify": ({"pr"}, set()),
    "reverify_cancel": ({"pr"}, set()),
    "reverify_status": ({"pr"}, set()),
    "train": ({"prs"}, {"dry_run"}),
    "rebuild": ({"train", "without"}, {"dry_run"}),
    "cleanup": ({"branch", "apply"}, set()),
}
# Actions of the UI and the script command each needs; merge and close need only gh.
ACTION_COMMANDS = {"reverify": "reverify", "reverify_cancel": "reverify_cancel", "train": "train",
                   "rebuild": "rebuild", "cleanup": "cleanup"}

TOP = {"name", "repo", "gh_repo", "commands", "conventions", "verdicts", "coordinator"}
CONVENTIONS = {"train_branch_prefix", "train_title_prefix", "verify_workflow", "maintainer_label",
               "worktrees_dir", "worktree_groups", "reverify_lock_dir"}
VERDICTS = {"ready_for_train", "ready", "not_ready"}
COORDINATOR = {"prompt_append", "description"}
PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
GH_REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def _str(v: object, where: str, empty: bool = False) -> str:
    if not isinstance(v, str) or (not empty and not v):
        raise ConfigError(f"{where} must be a{'' if empty else ' non-empty'} string")
    return v


def _table(v: object, where: str, keys: set[str]) -> dict[str, object]:
    if v is None:
        return {}
    if not isinstance(v, dict):
        raise ConfigError(f"[{where}] must be a table")
    unknown = sorted(set(v) - keys)
    if unknown:
        key = unknown[0] if where == "top level" else f"{where}.{unknown[0]}"
        raise ConfigError(f"unknown key {key} (known: {', '.join(sorted(keys))})")
    return v


def _template(name: str, t: object, flags: dict[str, str]) -> Template:
    where = f"commands.{name}"
    if not isinstance(t, list) or not t:
        raise ConfigError(f"{where} must be a non-empty array")
    required, optional = COMMANDS[name]
    allowed = required | optional
    used: set[str] = set()
    out: Template = []

    def arg(x: object) -> list[str]:
        a = _str(x, where, empty=True)
        names: list[str] = PLACEHOLDER_RE.findall(a)
        for p in names:
            if p not in allowed:
                raise ConfigError(f"{where}: unknown placeholder {{{p}}} (allowed: "
                                  f"{', '.join('{' + x + '}' for x in sorted(allowed))})")
            if (p in LISTS or p in FLAGS) and a != "{" + p + "}":
                raise ConfigError(f"{where}: {{{p}}} must be a whole argument")
            if p in FLAGS and not flags.get(FLAGS[p]):
                raise ConfigError(f"{where} uses {{{p}}} but commands.{FLAGS[p]} is not set")
            used.add(p)
        return names

    for i, a in enumerate(t):
        if isinstance(a, list):
            if i == 0 or not a:
                raise ConfigError(f"{where}: a nested array must be non-empty and not first")
            lists = [p for x in a for p in arg(x) if p in LISTS]
            if len(lists) != 1:
                raise ConfigError(f"{where}: a nested array needs exactly one list placeholder")
            if any(isinstance(x, list) for x in a):
                raise ConfigError(f"{where}: arrays nest one level only")
            out.append([str(x) for x in a])
        else:
            names = arg(a)
            if i == 0 and names:
                raise ConfigError(f"{where}: the program (first element) takes no placeholder")
            out.append(str(a))
    missing = sorted(required - used)
    if missing:
        raise ConfigError(f"{where} must use {{{missing[0]}}}")
    return out


def load(path: str | Path | None) -> Config:
    """Read and check the config file; ConfigError on any problem."""
    if not path:
        raise ConfigError("no config: pass --config <file> or set PR_CONSOLE_CONFIG (see examples/)")
    p = Path(path).expanduser()
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except OSError as e:
        raise ConfigError(f"cannot read config {p}: {e.strerror or e}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"config {p} is not valid TOML: {e}") from e
    try:
        return _check(raw)
    except ConfigError as e:
        raise ConfigError(f"config {p}: {e}") from e


def resolve_repo(value: str) -> Path:
    """The checkout path: `~` expanded, a relative path taken against the console's own
    directory (never the process cwd or the config file's directory), then made absolute."""
    p = Path(value).expanduser()
    return (p if p.is_absolute() else HERE / p).resolve()


def check_repo_dir(cfg: Config) -> None:
    """ConfigError unless the config's repo is a directory; the message names the resolved path."""
    if not Path(cfg["repo"]).is_dir():
        raise ConfigError(f"config repo {cfg['repo']} is not a directory")


def _check(raw: dict[str, object]) -> Config:
    _table(raw, "top level", TOP)
    repo = resolve_repo(_str(raw.get("repo"), "repo"))
    gh_repo = _str(raw.get("gh_repo"), "gh_repo")
    if not GH_REPO_RE.fullmatch(gh_repo):
        raise ConfigError(f"gh_repo must be OWNER/NAME, not {gh_repo!r}")
    name = _str(raw.get("name", gh_repo.split("/", 1)[1]), "name")

    cmds = dict(_table(raw.get("commands"), "commands", set(COMMANDS) | set(FLAGS.values())))
    flags = {k: _str(cmds.pop(k), f"commands.{k}") for k in FLAGS.values() if k in cmds}
    commands = {k: _template(k, v, flags) for k, v in cmds.items()}

    raw_conv = _table(raw.get("conventions"), "conventions", CONVENTIONS)
    raw_groups = raw_conv.get("worktree_groups", {})
    if not isinstance(raw_groups, dict) or not all(isinstance(v, str) and v for v in raw_groups.values()):
        raise ConfigError("conventions.worktree_groups must map group names to path prefixes")
    groups: dict[str, str] = dict(raw_groups)  # TOML keys are strings; the values were checked above
    if groups and "worktrees_dir" not in raw_conv:
        raise ConfigError("conventions.worktree_groups needs conventions.worktrees_dir")
    strs = {k: _str(v, f"conventions.{k}") for k, v in raw_conv.items() if k != "worktree_groups"}
    conv = _conventions(strs, groups)

    raw_verdicts = _table(raw.get("verdicts"), "verdicts", VERDICTS)
    verdicts = {k: _str(v, f"verdicts.{k}") for k, v in raw_verdicts.items()}
    if ({"verdict", "train_verdict"} & set(commands)) and set(verdicts) != VERDICTS:
        raise ConfigError("the verdict commands need [verdicts] with " + ", ".join(sorted(VERDICTS)))

    raw_coord = _table(raw.get("coordinator"), "coordinator", COORDINATOR)
    coord = {k: _str(v, f"coordinator.{k}", empty=True) for k, v in raw_coord.items()}

    return {"name": name, "repo": str(repo), "gh_repo": gh_repo, "commands": commands, "flags": flags,
            "conventions": conv, "verdicts": verdicts,
            "coordinator": {"prompt_append": coord.get("prompt_append", ""),
                            "description": coord.get("description", "")}}


ConventionKey = Literal["train_branch_prefix", "train_title_prefix", "verify_workflow",
                        "maintainer_label", "worktrees_dir", "reverify_lock_dir"]


def _conventions(strs: dict[str, str], groups: dict[str, str]) -> Conventions:
    """The [conventions] table, keys in the file's order, with worktrees_dir stripped of slashes."""
    conv: Conventions = {}
    for k, v in strs.items():
        if k == "train_branch_prefix":
            conv["train_branch_prefix"] = v
        elif k == "train_title_prefix":
            conv["train_title_prefix"] = v
        elif k == "verify_workflow":
            conv["verify_workflow"] = v
        elif k == "maintainer_label":
            conv["maintainer_label"] = v
        elif k == "worktrees_dir":
            conv["worktrees_dir"] = v
        elif k == "reverify_lock_dir":
            conv["reverify_lock_dir"] = v
    conv["worktree_groups"] = groups
    if "worktrees_dir" in conv:
        conv["worktrees_dir"] = conv["worktrees_dir"].strip("/")
    return conv


def enabled_actions(cfg: Config) -> list[str]:
    """The /api/action kinds this config allows, in a fixed order."""
    out: list[str] = []
    for kind in ("reverify", "reverify_cancel", "merge", "close", "label", "train", "rebuild", "cleanup"):
        if kind in ACTION_COMMANDS and ACTION_COMMANDS[kind] not in cfg["commands"]:
            continue
        if kind == "label" and not cfg["conventions"].get("maintainer_label"):
            continue
        out.append(kind)
    return out


def expand(cfg: Config, name: str, **values: Value) -> list[str]:
    """The argument list of command `name` with `values` substituted.

    `values` are already validated: scalars are str or int, lists are lists, flags are bools.
    A relative program path (one with a `/`) resolves against the repo; a bare name is looked
    up on PATH.
    """
    t = cfg["commands"].get(name)
    if t is None:
        raise KeyError(name)

    def items(v: Value) -> list[int]:
        if not isinstance(v, list):
            raise TypeError(f"commands.{name}: a list placeholder needs a list")
        return v

    def one(a: str, item: int | None = None) -> list[str]:
        whole = PLACEHOLDER_RE.fullmatch(a)
        if whole and whole.group(1) in FLAGS:
            return [cfg["flags"][FLAGS[whole.group(1)]]] if values.get(whole.group(1)) else []
        if whole and whole.group(1) in LISTS:
            return [str(item)] if item is not None else [str(x) for x in items(values[whole.group(1)])]
        return [PLACEHOLDER_RE.sub(lambda m: str(values[m.group(1)]), a)]

    argv: list[str] = []
    for a in t:
        if isinstance(a, list):
            lst = next(p for x in a for p in PLACEHOLDER_RE.findall(x) if p in LISTS)
            for item in items(values[lst]):
                for x in a:
                    argv += one(x, item if PLACEHOLDER_RE.fullmatch(x) and x == "{" + lst + "}" else None)
        else:
            argv += one(a)
    prog = argv[0]
    if "/" in prog and not Path(prog).expanduser().is_absolute():
        argv[0] = str(Path(cfg["repo"]) / prog)
    else:
        argv[0] = str(Path(prog).expanduser()) if prog.startswith("~") else prog
    return argv


def public(cfg: Config) -> PublicConfig:
    """What GET /api/config shows the page: no prompt text, nothing secret."""
    return {"name": cfg["name"], "repo": cfg["repo"], "gh_repo": cfg["gh_repo"],
            "conventions": cfg["conventions"], "verdicts": cfg["verdicts"],
            "commands": cfg["commands"], "flags": cfg["flags"], "actions": enabled_actions(cfg),
            "coordinator": {"description": cfg["coordinator"]["description"]}}
