# AGENTS.md

Rules for coding agents working on the PR console. Read [README.md](README.md) for setup, config, views and tests; this file holds only what you must or must not do.

## Purpose

Built for the Aldis workflow; everything specific to Aldis lives only in `examples/aldis.toml`. Never hard-code a repo, a path or an Aldis convention in the code.

## The check

Run `scripts/check --quick` after each edit and `scripts/check` before each commit. Fix everything it reports.

## Security rules

Keep these. Do not weaken any of them without the maintainer's approval.

- The server binds to localhost only.
- The token is required on every page and API request. It is taken from the query string only on the first page load and on the SSE endpoint.
- Keep the `Host` header check.
- CSP stays `default-src 'self'`: no inline scripts or styles, and no CDN.
- Commands are argument lists, never a shell.
- Inputs are validated before placeholder substitution.
- No permission mode that bypasses permissions.
- The console never auto-approves a permission request.

## UI

- In-page modals only; never `confirm`, `alert` or `prompt`.
- Every destructive action gets a modal that names the exact command.

## Tests

- New behaviour gets a Playwright or unittest case, run against the fakes (`web/tests/fake_server.py`, `web/tests/fake_sdk.py`).
- Tests never call the real GitHub, the real model, or a real managed repo.

## Files

Never commit `actions.jsonl`, `chats/`, `.venv`, `web/dist` or `node_modules`.

## Dependencies

Pin every dependency exactly. A new runtime dependency needs the maintainer's approval.

## Commits

Use conventional commit messages (`type(scope): description`).
