"""Claude Code chat sessions for the PR console, on the Claude Agent SDK (claude-agent-sdk).

The SDK is async. One asyncio loop runs in a daemon thread (ChatManager); the threaded HTTP
server calls into it with asyncio.run_coroutine_threadsafe. Each chat appends its events to an
EventBuffer (thread-safe, with increasing ids) that the server streams as server-sent events, and
to a JSON-lines log under chats/, next to a registry file, so past chats survive a restart.

Tool permissions: every request the CLI sends to can_use_tool waits for a decision from the page.
Nothing is approved here; a request that is never answered is denied when the chat ends.
"""

import asyncio
import json
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path

SESSION_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
REQ_RE = re.compile(r"^[0-9a-f]{16}$")
MAX_PROMPT = 16_000          # characters

MAX_LIVE = 8                 # connected sessions at once
MAX_TOOL_TEXT = 20_000       # characters of a tool result kept
BUFFER_COMPACT = 4000        # events in memory before superseded deltas are dropped
# Modes that still ask before acting. Never "bypassPermissions", "acceptEdits", "auto" or "dontAsk".
PERMISSION_MODES = ("auto", "default", "plan")  # auto first: the maintainer's terminal default
MODELS = ("", "opus", "sonnet", "haiku")  # "" = the CLI's default
STATUSES = ("starting", "running", "waiting", "permission", "idle", "ended", "error")


class ChatError(Exception):
    """A request the console refuses (HTTP 400)."""


# ---------------------------------------------------------------- validation

def validate_session_id(v):
    if not isinstance(v, str) or not SESSION_RE.match(v):
        raise ChatError("session id must be a lower-case UUID")
    return v


def validate_prompt(v):
    if not isinstance(v, str) or not v.strip():
        raise ChatError("write a prompt")
    if len(v) > MAX_PROMPT:
        raise ChatError(f"prompt is longer than {MAX_PROMPT} characters")
    return v


def validate_choice(v, allowed, what):
    v = "" if v is None else v
    if v not in allowed:
        raise ChatError(f"unknown {what} {v!r}")
    return v


def worktree_dirs(repo):
    """[{path, branch, is_main}] of `git worktree list --porcelain` in repo (bare and prunable ones left out)."""
    import parsers as P
    try:
        p = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=repo, capture_output=True,
                           text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ChatError(f"git worktree list failed: {e}") from e
    if p.returncode != 0:
        raise ChatError(f"git worktree list failed: {p.stderr.strip()}")
    main = str(Path(repo).resolve())
    return [{"path": w["path"], "branch": w["branch"], "is_main": str(Path(w["path"]).resolve()) == main}
            for w in P.parse_porcelain(p.stdout) if not w["bare"] and not w["prunable"]]


def validate_cwd(v, dirs):
    """v must be exactly one of the listed directories (compared after resolving symlinks)."""
    if not isinstance(v, str) or not v or "\0" in v:
        raise ChatError("choose a working directory")
    want = os.path.realpath(v)
    for d in dirs:
        if os.path.realpath(d["path"]) == want and os.path.isdir(want):
            return d["path"]
    raise ChatError("the working directory must be the main checkout or a worktree from `git worktree list`")


# ---------------------------------------------------------------- event buffer

class EventBuffer:
    """Events of one chat with increasing ids, for replay after a reconnect.

    `delta` events (partial text) are not persisted and are dropped once their text is
    superseded by the full message, so a long chat does not grow without bound in memory.
    """

    def __init__(self, log_path=None, events=()):
        self.cond = threading.Condition()
        self.events = list(events)
        self.next_id = (self.events[-1]["id"] + 1) if self.events else 1
        self.log_path = log_path

    def append(self, kind, **fields):
        with self.cond:
            ev = {"id": self.next_id, "t": time.time(), "kind": kind, **fields}
            self.next_id += 1
            self.events.append(ev)
            if kind != "delta" and self.log_path is not None:
                with open(self.log_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(ev) + "\n")
            if len(self.events) > BUFFER_COMPACT:
                self._compact()
            self.cond.notify_all()
            return ev

    def _compact(self):
        last_full = max((e["id"] for e in self.events if e["kind"] == "text"), default=0)
        self.events = [e for e in self.events if e["kind"] != "delta" or e["id"] > last_full]

    def after(self, last_id, timeout=None):
        """Events with id > last_id; waits up to `timeout` seconds for one if there are none."""
        with self.cond:
            if timeout and not (self.events and self.events[-1]["id"] > last_id):
                self.cond.wait(timeout)
            return [e for e in self.events if e["id"] > last_id]

    @classmethod
    def load(cls, log_path):
        events = []
        try:
            for line in Path(log_path).read_text(encoding="utf-8").splitlines():
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue  # a torn last line after a crash
        except OSError:
            pass
        return cls(log_path, events)


def transcript_events(session_id, cwd):
    """Events rebuilt from Claude Code's saved transcript, for a session the console has no log of."""
    try:
        from claude_agent_sdk import get_session_messages
        msgs = get_session_messages(session_id, directory=cwd)
    except Exception:  # noqa: BLE001 - no transcript, or an unreadable one
        return []
    out, n = [], 1
    for m in msgs:
        content = (m.message or {}).get("content") if isinstance(m.message, dict) else None
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else (content or [])
        for b in blocks:
            if not isinstance(b, dict):
                continue
            ev = None
            if b.get("type") == "text":
                ev = {"kind": "user" if m.type == "user" else "text", "text": b.get("text", "")}
            elif b.get("type") == "tool_use":
                ev = {"kind": "tool_use", "tool_use_id": b.get("id", ""), "name": b.get("name", ""),
                      "input": b.get("input") or {}}
            elif b.get("type") == "tool_result":
                ev = {"kind": "tool_result", "tool_use_id": b.get("tool_use_id", ""),
                      "content": tool_text(b.get("content")), "is_error": bool(b.get("is_error"))}
            if ev:
                out.append({"id": n, "t": 0, **ev})
                n += 1
    return out


def tool_text(content):
    if content is None:
        return ""
    if isinstance(content, str):
        s = content
    elif isinstance(content, list):
        s = "\n".join(c.get("text", "") if isinstance(c, dict) and c.get("type") == "text"
                      else json.dumps(c) for c in content)
    else:
        s = json.dumps(content)
    return s if len(s) <= MAX_TOOL_TEXT else s[:MAX_TOOL_TEXT] + f"\n… ({len(s) - MAX_TOOL_TEXT} more characters)"


# ---------------------------------------------------------------- chats

class Chat:
    def __init__(self, meta, buffer):
        self.meta = meta                # registry entry: id, cwd, created, title, model, permission_mode, resume_id
        self.buffer = buffer
        self.status = "ended"
        self.pending = {}               # req_id -> (future, public payload); touched on the loop thread only
        self.pending_public = []        # copy for HTTP threads
        self.inbox = None               # asyncio.Queue of commands while live
        self.task = None
        self.last_activity = buffer.events[-1]["t"] if buffer.events else meta["created"]

    def set_status(self, s):
        if s != self.status:
            self.status = s
            self.emit("status", status=s)

    def emit(self, kind, **fields):
        ev = self.buffer.append(kind, **fields)
        self.last_activity = ev["t"]
        return ev

    def public(self):
        return {**self.meta, "status": self.status, "last_activity": self.last_activity,
                "pending": list(self.pending_public), "last_event_id": self.buffer.next_id - 1}


class ChatManager:
    def __init__(self, state_dir, client_factory=None, prompt_append=""):
        # Every console chat is the repo's coordinator: the config's [coordinator] prompt_append
        # names the role and where the managed repo keeps its rules.
        self.prompt_append = prompt_append
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.registry_path = self.dir / "registry.json"
        self.lock = threading.Lock()
        self.chats = {}
        self.client_factory = client_factory or default_client
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True, name="chats-loop").start()
        for meta in self._read_registry():
            if SESSION_RE.match(meta.get("id", "")):
                buf = EventBuffer.load(self._log(meta["id"]))
                if not buf.events:
                    buf = EventBuffer(self._log(meta["id"]), transcript_events(meta.get("resume_id") or meta["id"], meta["cwd"]))
                self.chats[meta["id"]] = Chat(meta, buf)

    # -- registry

    def _log(self, cid):
        return self.dir / f"{cid}.jsonl"

    def _read_registry(self):
        try:
            data = json.loads(self.registry_path.read_text(encoding="utf-8"))
            return [m for m in data if isinstance(m, dict)]
        except (OSError, ValueError):
            return []

    def _write_registry(self):
        tmp = self.registry_path.with_suffix(".tmp")
        tmp.write_text(json.dumps([c.meta for c in self.chats.values()], indent=1), encoding="utf-8")
        os.replace(tmp, self.registry_path)

    # -- calls from HTTP threads

    def _call(self, coro, timeout=30):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def get(self, cid):
        validate_session_id(cid)
        c = self.chats.get(cid)
        if c is None:
            raise ChatError("no such chat")
        return c

    def list(self):
        with self.lock:
            return sorted((c.public() for c in self.chats.values()), key=lambda m: -m["created"])

    def _live_count(self):
        return sum(1 for c in self.chats.values() if c.task is not None and not c.task.done())

    def launch(self, cwd, prompt, model="", permission_mode="auto"):
        with self.lock:
            if self._live_count() >= MAX_LIVE:
                raise ChatError(f"{MAX_LIVE} sessions are connected; end one first")
            cid = str(uuid.uuid4())
            meta = {"id": cid, "cwd": cwd, "created": time.time(), "title": title_of(prompt),
                    "model": model, "permission_mode": permission_mode, "resume_id": None}
            chat = Chat(meta, EventBuffer(self._log(cid)))
            self.chats[cid] = chat
            self._write_registry()
        self._call(self._start(chat, prompt, resume=None))
        return chat.public()

    def resume(self, cid, cwd=None, prompt=None):
        """Resume a console chat, or adopt a session started elsewhere (then cwd is required)."""
        validate_session_id(cid)
        with self.lock:
            chat = self.chats.get(cid)
            if chat is None:
                if cwd is None:
                    raise ChatError("give the working directory of a session the console did not start")
                meta = {"id": cid, "cwd": cwd, "created": time.time(), "title": f"resumed {cid[:8]}",
                        "model": "", "permission_mode": "auto", "resume_id": cid}
                chat = Chat(meta, EventBuffer(self._log(cid), transcript_events(cid, cwd)))
                for ev in chat.buffer.events:  # persist the transcript as this chat's log
                    with open(self._log(cid), "a", encoding="utf-8") as fh:
                        fh.write(json.dumps(ev) + "\n")
                self.chats[cid] = chat
                self._write_registry()
            if chat.task is not None and not chat.task.done():
                raise ChatError("this chat is already connected")
            if self._live_count() >= MAX_LIVE:
                raise ChatError(f"{MAX_LIVE} sessions are connected; end one first")
        self._call(self._start(chat, prompt, resume=chat.meta.get("resume_id") or cid))
        return chat.public()

    def send(self, cid, text):
        chat = self.get(cid)
        if chat.task is None or chat.task.done():
            return self.resume(cid, prompt=text)
        self._call(self._command(chat, ("query", text)))
        return chat.public()

    def interrupt(self, cid):
        chat = self.get(cid)
        if chat.task is None or chat.task.done():
            raise ChatError("this chat is not connected")
        self._call(self._command(chat, ("interrupt", None)))

    def end(self, cid):
        chat = self.get(cid)
        if chat.task is not None and not chat.task.done():
            self._call(self._command(chat, ("end", None)))

    def decide(self, cid, req_id, allow):
        chat = self.get(cid)
        if not isinstance(req_id, str) or not REQ_RE.match(req_id):
            raise ChatError("bad permission request id")
        if not isinstance(allow, bool):
            raise ChatError("allow must be true or false")
        self._call(self._decide(chat, req_id, allow))

    def shutdown(self):
        async def stop_all():
            for c in self.chats.values():
                if c.task is not None and not c.task.done():
                    await c.inbox.put(("end", None))
            tasks = [c.task for c in self.chats.values() if c.task is not None]
            if tasks:
                await asyncio.wait(tasks, timeout=10)
        try:
            self._call(stop_all(), timeout=15)
        except Exception:  # noqa: BLE001
            pass

    # -- on the loop

    async def _command(self, chat, cmd):
        if chat.inbox is None or chat.task is None or chat.task.done():
            raise ChatError("this chat is not connected")
        await chat.inbox.put(cmd)

    async def _decide(self, chat, req_id, allow):
        entry = chat.pending.get(req_id)
        if entry is None:
            raise ChatError("that permission request is no longer open")
        fut, _ = entry
        if not fut.done():
            fut.set_result(allow)

    async def _start(self, chat, prompt, resume):
        chat.inbox = asyncio.Queue()
        if prompt is not None:
            chat.inbox.put_nowait(("query", prompt))
        chat.set_status("starting")
        chat.task = asyncio.create_task(self._run(chat, resume))

    async def _run(self, chat, resume):
        from claude_agent_sdk import ClaudeAgentOptions, PermissionResultAllow, PermissionResultDeny

        stderr_tail = []

        async def can_use_tool(tool_name, tool_input, ctx):
            req_id = os.urandom(8).hex()
            fut = asyncio.get_running_loop().create_future()
            payload = {"req_id": req_id, "chat": chat.meta["id"], "tool": tool_name, "input": tool_input,
                       "tool_use_id": ctx.tool_use_id, "title": ctx.title, "description": ctx.description,
                       "decision_reason": ctx.decision_reason, "blocked_path": ctx.blocked_path}
            chat.pending[req_id] = (fut, payload)
            chat.pending_public = [p for _, p in chat.pending.values()]
            chat.emit("permission", **{k: v for k, v in payload.items() if k != "chat"})
            chat.set_status("permission")
            allow = False
            try:
                allow = await fut
            finally:
                chat.pending.pop(req_id, None)
                chat.pending_public = [p for _, p in chat.pending.values()]
                chat.emit("permission_decided", req_id=req_id, allow=allow)
                if chat.status == "permission" and not chat.pending:
                    chat.set_status("running")
            if allow:
                return PermissionResultAllow()
            return PermissionResultDeny(message="The maintainer denied this in the PR console.")

        def on_stderr(line):
            stderr_tail.append(line)
            del stderr_tail[:-40]

        options = ClaudeAgentOptions(
            # Claude Code's own system prompt (None would send an empty one), plus the role.
            system_prompt={"type": "preset", "preset": "claude_code",
                           **({"append": self.prompt_append} if self.prompt_append else {})},
            cwd=chat.meta["cwd"],
            permission_mode=chat.meta.get("permission_mode") or "auto",
            model=chat.meta.get("model") or None,
            can_use_tool=can_use_tool,
            include_partial_messages=True,
            include_hook_events=True,
            stderr=on_stderr,
            **({"resume": resume} if resume else {"session_id": chat.meta["id"]}),
        )
        client = self.client_factory(options)
        reader = None
        try:
            await client.connect()
            chat.set_status("idle")
            reader = asyncio.create_task(self._read(chat, client))
            while True:  # one command at a time until "end" or the reader stops
                get = asyncio.create_task(chat.inbox.get())
                done, _ = await asyncio.wait({get, reader}, return_when=asyncio.FIRST_COMPLETED)
                if reader in done:
                    get.cancel()
                    reader.result()  # raise its error, if any
                    break
                kind, arg = get.result()
                if kind == "query":
                    chat.emit("user", text=arg)
                    chat.set_status("running")
                    await client.query(arg)
                elif kind == "interrupt":
                    chat.emit("info", text="interrupt sent")
                    await client.interrupt()
                elif kind == "end":
                    break
            chat.set_status("ended")
        except Exception as e:  # noqa: BLE001 - shown in the chat
            msg = f"{type(e).__name__}: {e}"
            if stderr_tail:
                msg += "\n" + "\n".join(stderr_tail[-20:])
            chat.emit("error", message=msg)
            chat.set_status("error")
        finally:
            for fut, _ in list(chat.pending.values()):
                if not fut.done():
                    fut.set_result(False)  # deny what is still open
            if reader is not None and not reader.done():
                reader.cancel()
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    async def _read(self, chat, client):
        from claude_agent_sdk import (AssistantMessage, ResultMessage, StreamEvent, SystemMessage,
                                      TextBlock, ToolResultBlock, ToolUseBlock, UserMessage)
        async for m in client.receive_messages():
            if isinstance(m, StreamEvent):
                ev = m.event or {}
                d = ev.get("delta") or {}
                if ev.get("type") == "content_block_delta" and d.get("type") == "text_delta":
                    chat.emit("delta", text=d.get("text", ""))
            elif isinstance(m, AssistantMessage):
                if m.error:
                    chat.emit("error", message=f"assistant error: {m.error}")
                for b in m.content:
                    if isinstance(b, TextBlock):
                        chat.emit("text", text=b.text, sub=bool(m.parent_tool_use_id))
                    elif isinstance(b, ToolUseBlock):
                        chat.emit("tool_use", tool_use_id=b.id, name=b.name, input=b.input,
                                  sub=bool(m.parent_tool_use_id))
            elif isinstance(m, UserMessage):
                if isinstance(m.content, list):
                    for b in m.content:
                        if isinstance(b, ToolResultBlock):
                            chat.emit("tool_result", tool_use_id=b.tool_use_id, content=tool_text(b.content),
                                      is_error=bool(b.is_error))
            elif isinstance(m, ResultMessage):
                if m.session_id and m.session_id != chat.meta.get("resume_id"):
                    with self.lock:
                        chat.meta["resume_id"] = m.session_id
                        self._write_registry()
                chat.emit("result", subtype=m.subtype, is_error=m.is_error, cost=m.total_cost_usd,
                          duration_ms=m.duration_ms, num_turns=m.num_turns, terminal_reason=m.terminal_reason,
                          errors=m.errors or [], text=m.result if m.is_error else None)
                interrupted = (m.terminal_reason or "").startswith("aborted") or m.subtype == "error_during_execution"
                chat.set_status("idle" if interrupted else "error" if m.is_error else "waiting")
            elif isinstance(m, SystemMessage):
                data = m.data or {}
                if m.subtype == "init":
                    chat.emit("system", text=f"session {data.get('session_id', '')} · model {data.get('model', '')}"
                                             f" · mode {data.get('permissionMode', '')}")
                elif m.subtype == "hook_response":
                    outcome = data.get("outcome")
                    if outcome not in (None, "success") or data.get("exit_code") not in (None, 0):
                        chat.emit("hook", event=data.get("hook_event") or data.get("hook_event_name", ""),
                                  name=data.get("hook_name", ""), outcome=outcome, exit_code=data.get("exit_code"),
                                  output=tool_text(data.get("stderr") or data.get("output") or data.get("stdout")))


def title_of(prompt):
    line = " ".join(prompt.split())
    return line if len(line) <= 80 else line[:79] + "…"


def default_client(options):
    from claude_agent_sdk import ClaudeSDKClient
    return ClaudeSDKClient(options)
