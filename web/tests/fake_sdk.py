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
  other    "echo: <prompt>", streamed in two deltas
"""

import asyncio
import json
import os
import uuid

from claude_agent_sdk import ClaudeSDKClient
from claude_agent_sdk._internal.transport import Transport


def make_client(options):
    return ClaudeSDKClient(options, transport=FakeTransport(options))


class FakeTransport(Transport):
    def __init__(self, options):
        self.options = options
        self.session_id = options.resume or options.session_id or str(uuid.uuid4())
        self.out = asyncio.Queue()
        self.ready = False
        self.waiting = {}        # request_id -> Future for our can_use_tool requests
        self.turn = None
        self.interrupted = asyncio.Event()

    async def connect(self):
        self.ready = True

    def is_ready(self):
        return self.ready

    async def end_input(self):
        pass

    async def close(self):
        self.ready = False
        if self.turn:
            self.turn.cancel()
        await self.out.put(None)

    async def read_messages(self):
        while True:  # bound: until close() queues None
            m = await self.out.get()
            if m is None:
                return
            yield m

    async def write(self, data):
        for line in data.splitlines():
            if line.strip():
                await self._handle(json.loads(line))

    async def _handle(self, m):
        t = m.get("type")
        if t == "control_request":
            req = m["request"]
            if req["subtype"] == "interrupt":
                self.interrupted.set()
            await self.out.put({"type": "control_response", "response": {
                "subtype": "success", "request_id": m["request_id"], "response": {}}})
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

    def _asst(self, *blocks):
        return {"type": "assistant", "session_id": self.session_id, "uuid": str(uuid.uuid4()),
                "message": {"id": "msg_" + os.urandom(4).hex(), "model": "fake-model", "role": "assistant",
                            "content": list(blocks)}}

    def _tool_result(self, tid, text, err=False):
        return {"type": "user", "session_id": self.session_id, "uuid": str(uuid.uuid4()),
                "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": tid, "content": text, "is_error": err}]}}

    def _delta(self, text):
        return {"type": "stream_event", "uuid": str(uuid.uuid4()), "session_id": self.session_id,
                "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}}

    def _result(self, subtype="success", is_error=False, reason="completed", result="done"):
        return {"type": "result", "subtype": subtype, "duration_ms": 5, "duration_api_ms": 1, "is_error": is_error,
                "num_turns": 1, "session_id": self.session_id, "total_cost_usd": 0.0, "result": result,
                "terminal_reason": reason}

    async def _ask(self, tid, name, inp):
        rid = "req_" + os.urandom(4).hex()
        fut = asyncio.get_running_loop().create_future()
        self.waiting[rid] = fut
        await self.out.put({"type": "control_request", "request_id": rid, "request": {
            "subtype": "can_use_tool", "tool_name": name, "input": inp, "tool_use_id": tid}})
        return await fut

    async def _turn(self, prompt):
        put = self.out.put
        await put({"type": "system", "subtype": "init", "session_id": self.session_id, "model": "fake-model",
                   "permissionMode": self.options.permission_mode, "cwd": str(self.options.cwd)})
        word = prompt.strip().split()[0] if prompt.strip() else ""
        if word in ("perm", "perm2"):
            calls = [("toolu_a", {"command": "ls -la"})] + ([("toolu_b", {"command": "git status"})] if word == "perm2" else [])
            await put(self._asst(*[{"type": "tool_use", "id": tid, "name": "Bash", "input": inp} for tid, inp in calls]))
            answers = await asyncio.gather(*[self._ask(tid, "Bash", inp) for tid, inp in calls])
            for (tid, inp), a in zip(calls, answers):
                ok = a.get("behavior") == "allow"
                await put(self._tool_result(tid, f"ran {inp['command']}" if ok else f"denied: {a.get('message', '')}", not ok))
            await put(self._asst({"type": "text", "text": "permission flow finished"}))
            await put(self._result())
        elif word == "slow":
            n = 0
            while not self.interrupted.is_set() and n < 600:  # bound: interrupt, or 60 s
                await put(self._delta(f"tick {n} "))
                n += 1
                try:
                    await asyncio.wait_for(self.interrupted.wait(), 0.1)
                except asyncio.TimeoutError:
                    pass
            await put(self._result("error_during_execution", True, "aborted_streaming", None))
        elif word == "hook":
            await put(self._asst({"type": "tool_use", "id": "toolu_h", "name": "Bash", "input": {"command": "sleep 1 &"}}))
            await put({"type": "system", "subtype": "hook_response", "session_id": self.session_id,
                       "uuid": str(uuid.uuid4()), "hook_event": "PreToolUse", "hook_name": "PreToolUse:Bash",
                       "outcome": "blocked", "exit_code": 2, "stderr": "background jobs (&) are blocked"})
            await put(self._tool_result("toolu_h", "PreToolUse:Bash hook error: background jobs (&) are blocked", True))
            await put(self._result())
        elif word == "md":
            await put(self._asst({"type": "text", "text":
                "# Heading\n\nSome **bold** and `code` and a [link](https://example.com).\n\n"
                "<img src=x onerror=\"window.__xss=1\"><script>window.__xss=2</script>\n\n"
                "- one\n- two\n\n```\nfenced <b>not bold</b>\n```\n\n[bad](javascript:alert(1))"}))
            await put(self._result())
        elif word == "long":
            for i in range(40):
                await put(self._asst({"type": "text", "text": f"line {i}\n\n" + "filler text " * 30}))
            await put(self._asst({"type": "text", "text": "LAST LINE"}))
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
