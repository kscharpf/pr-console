# PR console

Built to manage the multi-agent workflow of the [Aldis](https://github.com/kscharpf/aldis) project: one maintainer, several Claude Code coordinator sessions, and the pull requests, merge trains and agent worktrees they produce. Aldis is still its only user; `examples/aldis.toml` is its config. Everything specific to Aldis lives in that file, so another repo with similar scripts can use the console by writing its own config.

A local web app to manage the pull requests of one GitHub repo from one place, and to run Claude Code chat sessions next to them. The server is Python 3 standard library only, plus the Claude Agent SDK (`claude-agent-sdk`, pinned in `requirements.txt`) for the Chats view. The page is React, Mantine and TypeScript (strict), built with Vite into `web/dist/`; everything is bundled, with no CDN at runtime.

## Setup

Python 3.11 or later (the config is read with `tomllib`), in a virtualenv for the SDK:

    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt -r requirements-dev.txt   # dev: mypy and ruff, for scripts/check

Node 20 or later. The versions are pinned in `web/package-lock.json`.

    cd web
    npm ci
    npm run build        # tsc -b, then vite build into web/dist/

## Run

    .venv/bin/python -I server.py --config examples/aldis.toml [--port 8765] [--chats-dir ./chats]

The config may also come from `PR_CONSOLE_CONFIG`; there is no default. The server stops at once with a message when the file is missing, is not valid TOML, has an unknown key or placeholder, or names a `repo` that is not a directory.

Run with a Python without the SDK, the server works as before and the Chats view says that chats are off. Chats need the `claude` CLI to be logged in (the SDK runs its bundled CLI).

It listens on 127.0.0.1 only and prints `open http://127.0.0.1:8765/?token=...`. Open that URL. The page keeps the token in `sessionStorage` for the tab, removes it from the address bar, and sends it in an `X-Token` header. The query token is accepted only on the first load of `/` and on the chat event streams (`EventSource` cannot send headers); the request log never contains a query. The page and every `/api` request need the token. The built assets under `/assets/` need none (they hold no secret); the server still checks their `Host` header and refuses any path outside `web/dist/assets/`. A reload of `/` without the token gets a 403 page whose `assets/unlock.js` re-enters with the token the tab kept; a new tab needs the printed URL. If `web/dist/index.html` is missing, the server falls back to the old page in `static/` if there is one, else answers 503 for the page. The CSP is `default-src 'self'`, so the UI uses no inline scripts or styles (Mantine's `AppShell`, modal scroll lock and colour-scheme transition guard inject `<style>` tags and are not used). The server also checks the `Host` header (against DNS rebinding) and, for POST, `Content-Type: application/json`. You can also fix the token with `PR_CONSOLE_TOKEN`. `gh` must be logged in.

Checks: `scripts/check` runs ruff (`ruff check` and `ruff format --check`, config in `pyproject.toml`), shellcheck (`-o all`) on `scripts/check`, mypy (strict, config in `pyproject.toml`), the unittest suite, `tsc -b`, ESLint (`npm run lint`: typescript-eslint strict and stylistic, type-aware, plus the React hooks rules; config in `web/eslint.config.js`), `npm run build` and Playwright, stopping at the first failure; `scripts/check --quick` runs ruff, shellcheck, mypy, unittest and `tsc -b`. shellcheck is not pinned by the repo: install it with your package manager. `.github/workflows/check.yml` runs the same on push and pull request.

Tests (no network):

    .venv/bin/python -m unittest discover -s tests   # config, parsers, actions, asset paths, chat validation, event buffer
    cd web && npx playwright install chromium && npm test

The Playwright tests (`web/tests/`) run the real `server.py` request handler against the built `web/dist`, with GitHub, git and the scripts replaced by fixtures (`web/tests/fake_server.py`) and the config `tests/fixtures/console.toml`; run `npm run build` first. One test starts a second server with `tests/fixtures/minimal.toml` to check that the actions it leaves out have no button and are refused. The chat tests run the real `ChatManager` and the real SDK client over a fake CLI transport (`web/tests/fake_sdk.py`), so no model is called and nothing touches a real checkout; one test restarts a second server to check that past chats come back. They click every action, check the confirmation modal names the command, that confirming sends the expected `/api/action` body and cancelling sends nothing, that a reload keeps working, and that the page logs no CSP violation.

## Security

The console runs commands in your checkout and starts Claude Code sessions that act on your behalf. Run it only on your own machine.

- It listens on 127.0.0.1 only. Every page and API request needs the token it prints at startup. The server also checks the `Host` header, against DNS rebinding.
- Keep the token private: anyone with it can merge, close and rebuild PRs, and talk to your sessions. Set `PR_CONSOLE_TOKEN` only to a long random value.
- Actions run fixed argument lists from the config, never a shell, and PR numbers and branches are validated first.
- Chats run under your Claude Code settings and the permission mode you pick (`auto` by default). The console approves nothing itself: every permission request comes to you.
- `actions.jsonl` and `chats/` hold your action history and chat transcripts. They are gitignored; don't commit them.

## Config

One TOML file per repo, read with `tomllib`. `examples/aldis.toml` is the config for the Aldis repo and shows every key.

| Key | Meaning |
|---|---|
| `name` | Shown in the confirmation ("Runs in the <name> checkout"); default: the name part of `gh_repo`. |
| `repo` | The local checkout. Required. `~` is expanded; a relative path is taken against the console's own directory (the one holding `server.py`), never the current directory or the config file's, so `../aldis` names a clone beside `pr-console/`. The result is made absolute, and the error for a missing directory names that path. |
| `gh_repo` | `OWNER/NAME` on GitHub. Required. |
| `[commands]` | Each command as an argument list with placeholders, run in `repo`. A relative program with a `/` resolves against `repo`; a bare name is looked up on `PATH`. |
| `commands.dry_run_flag`, `commands.apply_flag` | What `{dry_run}` and `{apply}` expand to when set. |
| `[conventions]` | `train_branch_prefix` (marks train PRs), `train_title_prefix` (titles that list constituents), `verify_workflow` (the workflow whose newest run a train shows), `maintainer_label`, `worktrees_dir` (relative to the checkout; paths below it are shortened), `reverify_lock_dir` (under the git common dir), `[conventions.worktree_groups]` (Sessions group name → path prefix below `worktrees_dir`). All optional; a missing one turns its feature off. |
| `[verdicts]` | `ready_for_train`, `ready`, `not_ready`: the strings the verdict commands print, which the table colours (green, teal, red) and the train builder picks by. Required with `verdict` or `train_verdict`. |
| `[coordinator]` | `prompt_append` (appended to Claude Code's system prompt for every chat) and `description` (shown on the launch form). Optional. |

Commands and their placeholders:

| Command | Must use | May use |
|---|---|---|
| `verdict`, `train_verdict` | `{pr}` | |
| `reverify`, `reverify_cancel`, `reverify_status` | `{pr}` | |
| `train` | `{prs}` | `{dry_run}` |
| `rebuild` | `{train}`, `{without}` | `{dry_run}` |
| `cleanup` | `{branch}`, `{apply}` | |

A scalar (`{pr}`, `{train}`, `{branch}`) may sit inside an argument (`"--pr={pr}"`); a list (`{prs}`, `{without}`) or a flag (`{dry_run}`, `{apply}`) must be a whole argument. A list expands to one argument per item; a nested array repeats once per item of the list inside it, so `["--without", "{without}"]` gives `--without 1 --without 2`. Substitution is per argument and never goes through a shell, and every value is validated first (PR numbers open, branch names as below). The verdict commands must print a line `#<n>: <verdict>`, with `<not_ready>: reason; reason` for a PR that is not ready.

The page reads the public part of the config (no prompt) from `GET /api/config`, which needs the token.

## Views

- **All PRs** comes from `gh pr list --json ...`. The verdict comes from the `verdict` command, or from `train_verdict` for train PRs (branch starts with `train_branch_prefix`). These calls run 4 at a time. Results are cached per head SHA and refreshed every 180 s or when you press Refresh. Train PRs also show their constituents (from the body's `- #<n>` lines, else the `Merge pull request #<n>` commits, else the `#<n>` after the first comma of a title that starts with `train_title_prefix`) and the newest run of `gh run list --workflow <verify_workflow> --branch <b> -L 1`. An open train PR is shown in a banner.
- **Sessions** shows `git worktree list --porcelain` and `git -C <wt> status --porcelain` for each worktree. Worktrees are grouped by Claude agent lock (`claude agent agent-<id> (pid N start ...)`, and whether that pid is alive), by the `worktree_groups` of the config, or as unlocked. It also lists the reverify locks in `<git-common-dir>/<reverify_lock_dir>/<n>.lock` (symlinks to `<pid>|<start>`), each with the `reverify_status` command, and that status for every open train PR. A worktree is marked "ready to clean up" when it is clean and unlocked and its branch has a merged PR and no open one.
- **Chats** runs Claude Code sessions (`ClaudeSDKClient`, one `claude` process per connected session, at most 8). See below.
- **History** lists the last 100 entries of `actions.jsonl`.

Every destructive action asks in an in-page modal (never `confirm()`), which shows the exact command. The PR list refreshes every 15 s; rows are keyed by PR number, so a refresh does not swallow clicks. Colour scheme follows the system; the header toggle overrides it.

## Actions (all run in the configured checkout, in the background, one per PR at a time; output streams into the log panel and goes to `actions.jsonl`)

| Button | Command (config key) | Confirmation |
|---|---|---|
| Dry run (train) | `train` with `{dry_run}` set | none |
| Build train | `train` | yes; warns if a train is open |
| Rebuild dry run / Rebuild | `rebuild`, with or without `{dry_run}` | yes for a real rebuild |
| reverify (train only) | `reverify` | yes |
| cancel+reverify (train only) | `reverify_cancel` | yes |
| merge (train only) | `gh pr merge <n> --merge` | yes |
| close | `gh pr close <n> [--comment ...]` | yes |
| ± label | `gh pr edit <n> --add-label/--remove-label <maintainer_label>` | yes |
| cleanup… | `cleanup` without `{apply}`, then with it | the dry run first, then two confirmations |

A command the config leaves out has no button, and the server refuses its POST; so does the label button without `maintainer_label`, and a Dry run button whose command has no `{dry_run}`. Merge and close need only `gh`.

Every PR number must be digits and open at that moment (a fresh `gh pr list`). A branch must match `[A-Za-z0-9._/-]+`, must not start with `-` or `/`, and must not contain `..`. Commands are run from argument lists, never through a shell.

## Chats

- **New chat**: every chat is a coordinator session in the main checkout (the server ignores any other directory). Write the first prompt (at most 16 000 characters), and optionally pick a model (`opus` by default, `sonnet`, `haiku`, or the CLI default) and a permission mode: `auto` (the default; the classifier approves routine actions and asks about or blocks risky ones), `default` (asks for each tool the settings do not allow) or `plan` (read-only, asks before it leaves plan mode). `acceptEdits`, `dontAsk` and `bypassPermissions` are refused. A session can also be resumed by its ID: a console chat with its Resume button or by sending to it, another session with its ID and directory.
- **List**: status (starting; running; working in background, when the turn ended with a background task still running; needs your input or done: ready for PR, when the agent reported one through the console's `set_chat_status` tool, with its summary on hover; turn ended, when it reported neither; waiting for permission; idle; ended; error), directory, branch, the open PR on that branch (the link opens the PR row), last activity.
- **Delete**: the Delete button (only a chat that is not connected can be deleted; a chat in `error` status may still be connected and must be ended first, and the server refuses a connected one with 400, as it does a malformed ID) asks in a modal that names `POST /api/chats/<id>/delete`, then drops the chat from `chats/registry.json` and removes its `chats/<id>.jsonl`. Claude Code's own saved transcript is not touched, so the session can be adopted again by its ID.
- **Chat**: the assistant's text streams as it is written; tool calls show their name and input, with the result folded. Markdown is rendered into React elements by `web/src/Markdown.tsx`, so raw HTML stays text and only `http(s)`/`mailto` links are kept. Interrupt stops the running turn; End session closes the `claude` process (send again, or Resume, to continue it).
- **Permissions**: every `can_use_tool` request opens an in-page modal with the tool and its full input; Deny has the focus; requests from all chats queue and show one at a time. The console never approves anything by itself; a request still open when a session ends is denied. Tools that your Claude Code settings already allow do not ask (the CLI decides that before the console is asked). Hook blocks (`include_hook_events`) and errors, with the CLI's last stderr lines, show in the chat.
- **Live updates**: `GET /api/chats/<id>/events` is a server-sent event stream with an id per event; a reconnect replays from `Last-Event-ID` (or `?after=`). The page keeps one stream open, for the chat on screen, and keeps every chat's events, so switching back resumes after the last event it has.
- **History**: `chats/registry.json` holds each chat's ID, directory, created time, title, model, mode and the SDK session ID to resume; `chats/<id>.jsonl` is the log of its events (all but the partial-text deltas). After a restart the chats are listed as ended and their transcripts come from that log; a session adopted by ID starts from Claude Code's saved transcript (`get_session_messages`).

## License

MIT; see [LICENSE](LICENSE).
