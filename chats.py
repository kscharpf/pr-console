"""Claude Code chat sessions for the PR console, on the Claude Agent SDK (claude-agent-sdk).

The SDK is async. One asyncio loop runs in a daemon thread (ChatManager); the threaded HTTP
server calls into it with asyncio.run_coroutine_threadsafe. Each chat appends its events to an
EventBuffer (thread-safe, with increasing ids) that the server streams as server-sent events, and
to a JSON-lines log under chats/, next to a registry file, so past chats survive a restart.

Tool permissions: every request the CLI sends to can_use_tool waits for a decision from the page.
Nothing is approved here; a request that is never answered is denied when the chat ends.
"""

import asyncio
import contextlib
import json
import os
import re
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypedDict, TypeVar, cast

if TYPE_CHECKING:  # the SDK is optional at run time (chats are off without it)
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
    from claude_agent_sdk.types import (
        PermissionMode,
        PermissionResultAllow,
        PermissionResultDeny,
        SystemPromptPreset,
        ToolPermissionContext,
    )

T = TypeVar("T")
# One event of a chat: {"id", "t", "kind", ...fields}. The fields carry JSON from the CLI (tool
# input, hook output), which the SDK itself types as dict[str, Any]; events go to the page as JSON.
Event = dict[str, Any]
Command = tuple[Literal["query", "interrupt", "end"], "str | None"]
ClientFactory = Callable[["ClaudeAgentOptions"], "ClaudeSDKClient"]

SESSION_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
REQ_RE = re.compile(r"^[0-9a-f]{16}$")
MAX_PROMPT = 16_000  # characters

MAX_LIVE = 8  # connected sessions at once
MAX_TOOL_TEXT = 20_000  # characters of a tool result kept
BUFFER_COMPACT = 4000  # events in memory before superseded deltas are dropped
# Modes that still ask before acting. Never "bypassPermissions", "acceptEdits", "auto" or "dontAsk".
PERMISSION_MODES = ("auto", "default", "plan")  # auto first: the maintainer's terminal default
MODELS = ("", "opus", "sonnet", "haiku")  # "" = the CLI's default
STATUSES = ("starting", "running", "waiting", "permission", "idle", "ended", "error")


class ChatError(Exception):
    """A request the console refuses (HTTP 400)."""


class ChatMeta(TypedDict):
    """A registry entry (chats/registry.json)."""

    id: str
    cwd: str
    created: float
    title: str
    model: str
    permission_mode: str
    resume_id: str | None


class ChatDir(TypedDict):
    path: str
    branch: str
    is_main: bool


class PendingPermission(TypedDict):
    """A can_use_tool request waiting for the page."""

    req_id: str
    chat: str
    tool: str
    input: dict[str, Any]
    tool_use_id: str | None
    title: str | None
    description: str | None
    decision_reason: str | None
    blocked_path: str | None


class ChatPublic(TypedDict):
    """A chat as GET /api/chats lists it; the server adds branch and open_pr."""

    id: str
    cwd: str
    created: float
    title: str
    model: str
    permission_mode: str
    resume_id: str | None
    status: str
    last_activity: float
    pending: list[PendingPermission]
    last_event_id: int


# ---------------------------------------------------------------- validation


def validate_session_id(v: object) -> str:
    if not isinstance(v, str) or not SESSION_RE.match(v):
        raise ChatError("session id must be a lower-case UUID")
    return v


def validate_prompt(v: object) -> str:
    if not isinstance(v, str) or not v.strip():
        raise ChatError("write a prompt")
    if len(v) > MAX_PROMPT:
        raise ChatError(f"prompt is longer than {MAX_PROMPT} characters")
    return v


def validate_choice(v: object, allowed: tuple[str, ...], what: str) -> str:
    v = "" if v is None else v
    if not isinstance(v, str) or v not in allowed:
        raise ChatError(f"unknown {what} {v!r}")
    return v


def worktree_dirs(repo: str) -> list[ChatDir]:
    """[{path, branch, is_main}] of `git worktree list --porcelain` in repo (bare and prunable ones left out)."""
    import parsers as P

    try:
        # check=False: a non-zero exit is turned into a ChatError below
        p = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ChatError(f"git worktree list failed: {e}") from e
    if p.returncode != 0:
        raise ChatError(f"git worktree list failed: {p.stderr.strip()}")
    main = str(Path(repo).resolve())
    return [
        {"path": w["path"], "branch": w["branch"], "is_main": str(Path(w["path"]).resolve()) == main}
        for w in P.parse_porcelain(p.stdout)
        if not w["bare"] and not w["prunable"]
    ]


def validate_cwd(v: object, dirs: list[ChatDir]) -> str:
    """v must be exactly one of the listed directories (compared after resolving symlinks)."""
    if not isinstance(v, str) or not v or "\0" in v:
        raise ChatError("choose a working directory")
    want = os.path.realpath(v)
    for d in dirs:
        if os.path.realpath(d["path"]) == want and Path(want).is_dir():
            return d["path"]
    raise ChatError("the working directory must be the main checkout or a worktree from `git worktree list`")


# ---------------------------------------------------------------- event buffer


class EventBuffer:
    """Events of one chat with increasing ids, for replay after a reconnect.

    `delta` events (partial text) are not persisted and are dropped once their text is
    superseded by the full message, so a long chat does not grow without bound in memory.
    """

    def __init__(self, log_path: Path | None = None, events: list[Event] | tuple[()] = ()) -> None:
        self.cond = threading.Condition()
        self.events: list[Event] = list(events)
        self.next_id: int = (self.events[-1]["id"] + 1) if self.events else 1
        self.log_path = log_path

    def append(self, kind: str, **fields: object) -> Event:
        with self.cond:
            ev = {"id": self.next_id, "t": time.time(), "kind": kind, **fields}
            self.next_id += 1
            self.events.append(ev)
            if kind != "delta" and self.log_path is not None:
                with self.log_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(ev) + "\n")
            if len(self.events) > BUFFER_COMPACT:
                self._compact()
            self.cond.notify_all()
            return ev

    def _compact(self) -> None:
        last_full = max((e["id"] for e in self.events if e["kind"] == "text"), default=0)
        self.events = [e for e in self.events if e["kind"] != "delta" or e["id"] > last_full]

    def after(self, last_id: int, timeout: float | None = None) -> list[Event]:
        """Events with id > last_id; waits up to `timeout` seconds for one if there are none."""
        with self.cond:
            if timeout and not (self.events and self.events[-1]["id"] > last_id):
                self.cond.wait(timeout)
            return [e for e in self.events if e["id"] > last_id]

    @classmethod
    def load(cls, log_path: Path) -> "EventBuffer":
        events: list[Event] = []
        try:
            for line in Path(log_path).read_text(encoding="utf-8").splitlines():
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue  # a torn last line after a crash
        except OSError:
            pass
        return cls(log_path, events)


def transcript_events(session_id: str, cwd: str) -> list[Event]:
    """Events rebuilt from Claude Code's saved transcript, for a session the console has no log of."""
    try:
        from claude_agent_sdk import get_session_messages

        msgs = get_session_messages(session_id, directory=cwd)
    except Exception:  # noqa: BLE001 - no transcript, or an unreadable one
        return []
    out: list[Event] = []
    n = 1
    for m in msgs:
        content = (m.message or {}).get("content") if isinstance(m.message, dict) else None
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else (content or [])
        for b in blocks:
            if not isinstance(b, dict):
                continue
            ev: Event | None = None
            if b.get("type") == "text":
                ev = {"kind": "user" if m.type == "user" else "text", "text": b.get("text", "")}
            elif b.get("type") == "tool_use":
                ev = {
                    "kind": "tool_use",
                    "tool_use_id": b.get("id", ""),
                    "name": b.get("name", ""),
                    "input": b.get("input") or {},
                }
            elif b.get("type") == "tool_result":
                ev = {
                    "kind": "tool_result",
                    "tool_use_id": b.get("tool_use_id", ""),
                    "content": tool_text(b.get("content")),
                    "is_error": bool(b.get("is_error")),
                }
            if ev:
                out.append({"id": n, "t": 0, **ev})
                n += 1
    return out


def tool_text(content: object) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        s = content
    elif isinstance(content, list):
        s = "\n".join(
            c.get("text", "") if isinstance(c, dict) and c.get("type") == "text" else json.dumps(c) for c in content
        )
    else:
        s = json.dumps(content)
    return s if len(s) <= MAX_TOOL_TEXT else s[:MAX_TOOL_TEXT] + f"\n… ({len(s) - MAX_TOOL_TEXT} more characters)"


# ---------------------------------------------------------------- chats


class Chat:
    def __init__(self, meta: ChatMeta, buffer: EventBuffer) -> None:
        self.meta = meta
        self.buffer = buffer
        self.status = "ended"
        # req_id -> (future, public payload); touched on the loop thread only
        self.pending: dict[str, tuple[asyncio.Future[bool], PendingPermission]] = {}
        self.pending_public: list[PendingPermission] = []  # copy for HTTP threads
        self.inbox: asyncio.Queue[Command] | None = None  # commands while live
        self.task: asyncio.Task[None] | None = None
        self.last_activity: float = buffer.events[-1]["t"] if buffer.events else meta["created"]

    def set_status(self, s: str) -> None:
        if s != self.status:
            self.status = s
            self.emit("status", status=s)

    def emit(self, kind: str, **fields: object) -> Event:
        ev = self.buffer.append(kind, **fields)
        self.last_activity = ev["t"]
        return ev

    def public(self) -> ChatPublic:
        return {
            **self.meta,
            "status": self.status,
            "last_activity": self.last_activity,
            "pending": list(self.pending_public),
            "last_event_id": self.buffer.next_id - 1,
        }


class ChatManager:
    def __init__(
        self, state_dir: str | Path, client_factory: ClientFactory | None = None, prompt_append: str = ""
    ) -> None:
        # Every console chat is the repo's coordinator: the config's [coordinator] prompt_append
        # names the role and where the managed repo keeps its rules.
        self.prompt_append = prompt_append
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.registry_path = self.dir / "registry.json"
        self.lock = threading.Lock()
        self.chats: dict[str, Chat] = {}
        self.client_factory = client_factory or default_client
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True, name="chats-loop").start()
        for meta in self._read_registry():
            if isinstance(meta.get("id"), str) and SESSION_RE.match(meta["id"]):
                buf = EventBuffer.load(self._log(meta["id"]))
                if not buf.events:
                    buf = EventBuffer(
                        self._log(meta["id"]), transcript_events(meta.get("resume_id") or meta["id"], meta["cwd"])
                    )
                self.chats[meta["id"]] = Chat(meta, buf)

    # -- registry

    def _log(self, cid: str) -> Path:
        return self.dir / f"{cid}.jsonl"

    def _read_registry(self) -> list[ChatMeta]:
        try:
            data = json.loads(self.registry_path.read_text(encoding="utf-8"))
            # The console wrote this file; entries are trusted to have ChatMeta's keys.
            return cast("list[ChatMeta]", [m for m in data if isinstance(m, dict)])
        except (OSError, ValueError):
            return []

    def _write_registry(self) -> None:
        tmp = self.registry_path.with_suffix(".tmp")
        tmp.write_text(json.dumps([c.meta for c in self.chats.values()], indent=1), encoding="utf-8")
        tmp.replace(self.registry_path)

    # -- calls from HTTP threads

    def _call(self, coro: Coroutine[Any, Any, T], timeout: float = 30) -> T:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def get(self, cid: str) -> Chat:
        validate_session_id(cid)
        c = self.chats.get(cid)
        if c is None:
            raise ChatError("no such chat")
        return c

    def list(self) -> list[ChatPublic]:
        with self.lock:
            return sorted((c.public() for c in self.chats.values()), key=lambda m: -m["created"])

    def _live_count(self) -> int:
        return sum(1 for c in self.chats.values() if c.task is not None and not c.task.done())

    def launch(self, cwd: str, prompt: str, model: str = "", permission_mode: str = "auto") -> ChatPublic:
        with self.lock:
            if self._live_count() >= MAX_LIVE:
                raise ChatError(f"{MAX_LIVE} sessions are connected; end one first")
            cid = str(uuid.uuid4())
            meta: ChatMeta = {
                "id": cid,
                "cwd": cwd,
                "created": time.time(),
                "title": title_of(prompt),
                "model": model,
                "permission_mode": permission_mode,
                "resume_id": None,
            }
            chat = Chat(meta, EventBuffer(self._log(cid)))
            self.chats[cid] = chat
            self._write_registry()
        self._call(self._start(chat, prompt, resume=None))
        return chat.public()

    def resume(self, cid: str, cwd: str | None = None, prompt: str | None = None) -> ChatPublic:
        """Resume a console chat, or adopt a session started elsewhere (then cwd is required)."""
        validate_session_id(cid)
        with self.lock:
            chat = self.chats.get(cid)
            if chat is None:
                if cwd is None:
                    raise ChatError("give the working directory of a session the console did not start")
                meta = ChatMeta(
                    id=cid,
                    cwd=cwd,
                    created=time.time(),
                    title=f"resumed {cid[:8]}",
                    model="",
                    permission_mode="auto",
                    resume_id=cid,
                )
                chat = Chat(meta, EventBuffer(self._log(cid), transcript_events(cid, cwd)))
                for ev in chat.buffer.events:  # persist the transcript as this chat's log
                    with self._log(cid).open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(ev) + "\n")
                self.chats[cid] = chat
                self._write_registry()
            if chat.task is not None and not chat.task.done():
                raise ChatError("this chat is already connected")
            if self._live_count() >= MAX_LIVE:
                raise ChatError(f"{MAX_LIVE} sessions are connected; end one first")
        self._call(self._start(chat, prompt, resume=chat.meta.get("resume_id") or cid))
        return chat.public()

    def send(self, cid: str, text: str) -> ChatPublic:
        chat = self.get(cid)
        if chat.task is None or chat.task.done():
            return self.resume(cid, prompt=text)
        self._call(self._command(chat, ("query", text)))
        return chat.public()

    def interrupt(self, cid: str) -> None:
        chat = self.get(cid)
        if chat.task is None or chat.task.done():
            raise ChatError("this chat is not connected")
        self._call(self._command(chat, ("interrupt", None)))

    def end(self, cid: str) -> None:
        chat = self.get(cid)
        if chat.task is not None and not chat.task.done():
            self._call(self._command(chat, ("end", None)))

    def decide(self, cid: str, req_id: object, allow: object) -> None:
        chat = self.get(cid)
        if not isinstance(req_id, str) or not REQ_RE.match(req_id):
            raise ChatError("bad permission request id")
        if not isinstance(allow, bool):
            raise ChatError("allow must be true or false")
        self._call(self._decide(chat, req_id, allow))

    def shutdown(self) -> None:
        async def stop_all() -> None:
            for c in self.chats.values():
                if c.task is not None and not c.task.done() and c.inbox is not None:
                    await c.inbox.put(("end", None))
            tasks = [c.task for c in self.chats.values() if c.task is not None]
            if tasks:
                await asyncio.wait(tasks, timeout=10)

        # Best effort at exit: a session that does not stop in time dies with the process.
        with contextlib.suppress(Exception):
            self._call(stop_all(), timeout=15)

    # -- on the loop

    async def _command(self, chat: Chat, cmd: Command) -> None:
        if chat.inbox is None or chat.task is None or chat.task.done():
            raise ChatError("this chat is not connected")
        await chat.inbox.put(cmd)

    async def _decide(self, chat: Chat, req_id: str, allow: bool) -> None:
        entry = chat.pending.get(req_id)
        if entry is None:
            raise ChatError("that permission request is no longer open")
        fut, _ = entry
        if not fut.done():
            fut.set_result(allow)

    async def _start(self, chat: Chat, prompt: str | None, resume: str | None) -> None:
        inbox: asyncio.Queue[Command] = asyncio.Queue()
        chat.inbox = inbox
        if prompt is not None:
            inbox.put_nowait(("query", prompt))
        chat.set_status("starting")
        chat.task = asyncio.create_task(self._run(chat, resume))

    async def _run(self, chat: Chat, resume: str | None) -> None:
        from claude_agent_sdk import ClaudeAgentOptions, PermissionResultAllow, PermissionResultDeny

        stderr_tail: list[str] = []
        inbox = chat.inbox
        assert inbox is not None  # noqa: S101  narrows the type; _start sets it before it starts this task

        async def can_use_tool(
            tool_name: str, tool_input: dict[str, Any], ctx: "ToolPermissionContext"
        ) -> "PermissionResultAllow | PermissionResultDeny":
            req_id = os.urandom(8).hex()
            fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
            payload: PendingPermission = {
                "req_id": req_id,
                "chat": chat.meta["id"],
                "tool": tool_name,
                "input": tool_input,
                "tool_use_id": ctx.tool_use_id,
                "title": ctx.title,
                "description": ctx.description,
                "decision_reason": ctx.decision_reason,
                "blocked_path": ctx.blocked_path,
            }
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

        def on_stderr(line: str) -> None:
            stderr_tail.append(line)
            del stderr_tail[:-40]

        # Claude Code's own system prompt (None would send an empty one), plus the role.
        system_prompt: SystemPromptPreset = {"type": "preset", "preset": "claude_code"}
        if self.prompt_append:
            system_prompt["append"] = self.prompt_append
        options = ClaudeAgentOptions(
            system_prompt=system_prompt,
            cwd=chat.meta["cwd"],
            permission_mode=permission_mode(chat.meta.get("permission_mode")),
            model=chat.meta.get("model") or None,
            can_use_tool=can_use_tool,
            include_partial_messages=True,
            include_hook_events=True,
            stderr=on_stderr,
            resume=resume or None,
            session_id=None if resume else chat.meta["id"],
        )
        client = self.client_factory(options)
        reader: asyncio.Task[None] | None = None
        try:
            await client.connect()
            chat.set_status("idle")
            reader = asyncio.create_task(self._read(chat, client))
            while True:  # one command at a time until "end" or the reader stops
                get = asyncio.create_task(inbox.get())
                done, _ = await asyncio.wait({get, reader}, return_when=asyncio.FIRST_COMPLETED)
                if reader in done:
                    get.cancel()
                    reader.result()  # raise its error, if any
                    break
                kind, arg = get.result()
                if kind == "query" and arg is not None:
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
            # The session is over either way; its error, if any, was shown above.
            with contextlib.suppress(Exception):
                await client.disconnect()

    async def _read(self, chat: Chat, client: "ClaudeSDKClient") -> None:
        from claude_agent_sdk import (
            AssistantMessage,
            ResultMessage,
            StreamEvent,
            SystemMessage,
            TextBlock,
            ToolResultBlock,
            ToolUseBlock,
            UserMessage,
        )

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
                        chat.emit(
                            "tool_use", tool_use_id=b.id, name=b.name, input=b.input, sub=bool(m.parent_tool_use_id)
                        )
            elif isinstance(m, UserMessage):
                if isinstance(m.content, list):
                    for b in m.content:
                        if isinstance(b, ToolResultBlock):
                            chat.emit(
                                "tool_result",
                                tool_use_id=b.tool_use_id,
                                content=tool_text(b.content),
                                is_error=bool(b.is_error),
                            )
            elif isinstance(m, ResultMessage):
                if m.session_id and m.session_id != chat.meta.get("resume_id"):
                    with self.lock:
                        chat.meta["resume_id"] = m.session_id
                        self._write_registry()
                chat.emit(
                    "result",
                    subtype=m.subtype,
                    is_error=m.is_error,
                    cost=m.total_cost_usd,
                    duration_ms=m.duration_ms,
                    num_turns=m.num_turns,
                    terminal_reason=m.terminal_reason,
                    errors=m.errors or [],
                    text=m.result if m.is_error else None,
                )
                interrupted = (m.terminal_reason or "").startswith("aborted") or m.subtype == "error_during_execution"
                chat.set_status("idle" if interrupted else "error" if m.is_error else "waiting")
            elif isinstance(m, SystemMessage):
                data = m.data or {}
                if m.subtype == "init":
                    chat.emit(
                        "system",
                        text=f"session {data.get('session_id', '')} · model {data.get('model', '')}"
                        f" · mode {data.get('permissionMode', '')}",
                    )
                elif m.subtype == "hook_response":
                    outcome = data.get("outcome")
                    if outcome not in (None, "success") or data.get("exit_code") not in (None, 0):
                        chat.emit(
                            "hook",
                            event=data.get("hook_event") or data.get("hook_event_name", ""),
                            name=data.get("hook_name", ""),
                            outcome=outcome,
                            exit_code=data.get("exit_code"),
                            output=tool_text(data.get("stderr") or data.get("output") or data.get("stdout")),
                        )


def title_of(prompt: str) -> str:
    line = " ".join(prompt.split())
    return line if len(line) <= 80 else line[:79] + "…"


def permission_mode(v: str | None) -> "PermissionMode":
    """The registry's mode as the SDK's type; only PERMISSION_MODES are ever stored."""
    if v == "default":
        return "default"
    if v == "plan":
        return "plan"
    return "auto"


def default_client(options: "ClaudeAgentOptions") -> "ClaudeSDKClient":
    from claude_agent_sdk import ClaudeSDKClient

    return ClaudeSDKClient(options)
