"""A fake Claude Code CLI behind the real claude_agent_sdk.ClaudeSDKClient.

FakeTransport speaks the CLI's stream-json control protocol (initialize, interrupt, can_use_tool),
so the console's real SDK calls run unchanged and no model is called. The reply is chosen by
the prompt:

  "perm"   one Bash call that asks for permission; the result says whether it ran
  "perm2"  two calls that ask at once (to test the queue)
  "slow"   streams text until interrupted
  "hook"   a PreToolUse hook that blocks a tool
  "md"     markdown with raw HTML in it
  "fail"   an error result
  "bg"     starts a background task and ends the turn; when the task finishes, a second turn starts
           on its own
  "ask"    reports needs_input through the console's set_chat_status tool
  "finish" reports done through the same tool
  other    "echo: <prompt>", streamed in two deltas
"""

import asyncio
import contextlib
import json
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from claude_agent_sdk._internal.transport import Transport

# A stream-json message of the CLI protocol; Transport types them as dict[str, Any].
Msg = dict[str, Any]


def make_client(options: ClaudeAgentOptions) -> ClaudeSDKClient:
    return ClaudeSDKClient(options, transport=FakeTransport(options))


class FakeTransport(Transport):
    def __init__(self, options: ClaudeAgentOptions) -> None:
        self.options = options
        self.session_id = options.resume or options.session_id or str(uuid.uuid4())
        self.out: asyncio.Queue[Msg | None] = asyncio.Queue()
        self.ready = False
        self.waiting: dict[str, asyncio.Future[Msg]] = {}  # request_id -> our can_use_tool requests
        self.turn: asyncio.Task[None] | None = None
        self.interrupted = asyncio.Event()
        self.bg: asyncio.Task[None] | None = None
        self.mcp_ready = False

    async def connect(self) -> None:
        self.ready = True

    def is_ready(self) -> bool:
        return self.ready

    async def end_input(self) -> None:
        pass

    async def close(self) -> None:
        self.ready = False
        for t in (self.turn, self.bg):
            if t:
                t.cancel()
        await self.out.put(None)

    async def read_messages(self) -> AsyncIterator[Msg]:
        while True:  # bound: until close() queues None
            m = await self.out.get()
            if m is None:
                return
            yield m

    async def write(self, data: str) -> None:
        for line in data.splitlines():
            if line.strip():
                await self._handle(json.loads(line))

    async def _handle(self, m: Msg) -> None:
        t = m.get("type")
        if t == "control_request":
            req = m["request"]
            if req["subtype"] == "interrupt":
                self.interrupted.set()
            await self.out.put(
                {
                    "type": "control_response",
                    "response": {"subtype": "success", "request_id": m["request_id"], "response": {}},
                }
            )
        elif t == "control_response":
            r = m["response"]
            fut = self.waiting.pop(r["request_id"], None)
            if fut and not fut.done():
                fut.set_result(r.get("response") or {"behavior": "deny", "message": r.get("error", "")})
        elif t == "user":
            content = m["message"]["content"]
            self.interrupted.clear()
            self.turn = asyncio.create_task(self._turn(content if isinstance(content, str) else ""))

    # -- replies

    def _asst(self, *blocks: Msg) -> Msg:
        return {
            "type": "assistant",
            "session_id": self.session_id,
            "uuid": str(uuid.uuid4()),
            "message": {
                "id": "msg_" + os.urandom(4).hex(),
                "model": "fake-model",
                "role": "assistant",
                "content": list(blocks),
            },
        }

    def _tool_result(self, tid: str, text: str, err: bool = False) -> Msg:
        return {
            "type": "user",
            "session_id": self.session_id,
            "uuid": str(uuid.uuid4()),
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": tid, "content": text, "is_error": err}],
            },
        }

    def _delta(self, text: str) -> Msg:
        return {
            "type": "stream_event",
            "uuid": str(uuid.uuid4()),
            "session_id": self.session_id,
            "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        }

    def _result(
        self, subtype: str = "success", is_error: bool = False, reason: str = "completed", result: str | None = "done"
    ) -> Msg:
        return {
            "type": "result",
            "subtype": subtype,
            "duration_ms": 5,
            "duration_api_ms": 1,
            "is_error": is_error,
            "num_turns": 1,
            "session_id": self.session_id,
            "total_cost_usd": 0.0,
            "result": result,
            "terminal_reason": reason,
        }

    async def _control(self, request: Msg) -> Msg:
        rid = "req_" + os.urandom(4).hex()
        fut: asyncio.Future[Msg] = asyncio.get_running_loop().create_future()
        self.waiting[rid] = fut
        await self.out.put({"type": "control_request", "request_id": rid, "request": request})
        return await fut

    async def _ask(self, tid: str, name: str, inp: Msg) -> Msg:
        return await self._control({"subtype": "can_use_tool", "tool_name": name, "input": inp, "tool_use_id": tid})

    async def _set_status(self, state: str, summary: str) -> None:
        """Calls the console's in-process set_chat_status tool the way the CLI calls an SDK MCP server."""
        args = {"state": state, "summary": summary}
        await self.out.put(
            self._asst({"type": "tool_use", "id": "toolu_s", "name": "mcp__console__set_chat_status", "input": args})
        )
        if not self.mcp_ready:  # the MCP handshake the CLI makes before its first call
            info = {"name": "fake", "version": "0"}
            init = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": info}
            await self._mcp({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": init})
            await self._mcp({"jsonrpc": "2.0", "method": "notifications/initialized"})
            self.mcp_ready = True
        params = {"name": "set_chat_status", "arguments": args}
        r = await self._mcp({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params})
        text = r.get("mcp_response", {}).get("result", {}).get("content", [{}])[0].get("text", "")
        await self.out.put(self._tool_result("toolu_s", text))

    async def _mcp(self, message: Msg) -> Msg:
        return await self._control({"subtype": "mcp_message", "server_name": "console", "message": message})

    def _task(self, subtype: str, **fields: object) -> Msg:
        return {
            "type": "system",
            "subtype": subtype,
            "task_id": "task_1",
            "uuid": str(uuid.uuid4()),
            "session_id": self.session_id,
            **fields,
        }

    async def _background(self) -> None:
        await asyncio.sleep(1.5)
        await self.out.put(
            self._task("task_notification", status="completed", output_file="/dev/null", summary="build finished")
        )
        await self.out.put(self._asst({"type": "text", "text": "background task finished"}))
        await self.out.put(self._result())

    async def _turn(self, prompt: str) -> None:
        put = self.out.put
        await put(
            {
                "type": "system",
                "subtype": "init",
                "session_id": self.session_id,
                "model": "fake-model",
                "permissionMode": self.options.permission_mode,
                "cwd": str(self.options.cwd),
            }
        )
        word = prompt.strip().split()[0] if prompt.strip() else ""
        if word in ("perm", "perm2"):
            calls = [("toolu_a", {"command": "ls -la"})] + (
                [("toolu_b", {"command": "git status"})] if word == "perm2" else []
            )
            await put(
                self._asst(*[{"type": "tool_use", "id": tid, "name": "Bash", "input": inp} for tid, inp in calls])
            )
            answers = await asyncio.gather(*[self._ask(tid, "Bash", inp) for tid, inp in calls])
            for (tid, inp), a in zip(calls, answers, strict=True):
                ok = a.get("behavior") == "allow"
                await put(
                    self._tool_result(tid, f"ran {inp['command']}" if ok else f"denied: {a.get('message', '')}", not ok)
                )
            await put(self._asst({"type": "text", "text": "permission flow finished"}))
            await put(self._result())
        elif word == "slow":
            n = 0
            while not self.interrupted.is_set() and n < 600:  # bound: interrupt, or 60 s
                await put(self._delta(f"tick {n} "))
                n += 1
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.interrupted.wait(), 0.1)
            await put(self._result("error_during_execution", True, "aborted_streaming", None))
        elif word == "hook":
            await put(
                self._asst({"type": "tool_use", "id": "toolu_h", "name": "Bash", "input": {"command": "sleep 1 &"}})
            )
            await put(
                {
                    "type": "system",
                    "subtype": "hook_response",
                    "session_id": self.session_id,
                    "uuid": str(uuid.uuid4()),
                    "hook_event": "PreToolUse",
                    "hook_name": "PreToolUse:Bash",
                    "outcome": "blocked",
                    "exit_code": 2,
                    "stderr": "background jobs (&) are blocked",
                }
            )
            await put(self._tool_result("toolu_h", "PreToolUse:Bash hook error: background jobs (&) are blocked", True))
            await put(self._result())
        elif word == "md":
            await put(
                self._asst(
                    {
                        "type": "text",
                        "text": "# Heading\n\nSome **bold** and `code` and a [link](https://example.com).\n\n"
                        '<img src=x onerror="window.__xss=1"><script>window.__xss=2</script>\n\n'
                        "- one\n- two\n\n```\nfenced <b>not bold</b>\n```\n\n[bad](javascript:alert(1))",
                    }
                )
            )
            await put(self._result())
        elif word == "long":
            for i in range(40):
                await put(self._asst({"type": "text", "text": f"line {i}\n\n" + "filler text " * 30}))
            await put(self._asst({"type": "text", "text": "LAST LINE"}))
            await put(self._result())
        elif word == "bg":
            await put(self._task("task_started", description="build", task_type="local_bash"))
            await put(self._asst({"type": "text", "text": "started a build in the background"}))
            await put(self._result())
            self.bg = asyncio.create_task(self._background())
        elif word in ("ask", "finish"):
            await put(self._asst({"type": "text", "text": "turn over"}))
            await self._set_status("needs_input" if word == "ask" else "done", f"summary of {word}")
            await put(self._result())
        elif word == "fail":
            await put(self._result("success", True, "completed", "API Error: 529 overloaded"))
        else:
            text = f"echo: {prompt}"
            half = len(text) // 2
            await put(self._delta(text[:half]))
            await asyncio.sleep(0.05)
            await put(self._delta(text[half:]))
            await put(self._asst({"type": "text", "text": text}))
            await put(self._result())
