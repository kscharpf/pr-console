import asyncio
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

import chats as C
from tests import TEST_CONFIG


def no_client(_options: ClaudeAgentOptions) -> ClaudeSDKClient:
    raise AssertionError("these tests connect no session")


class Validation(unittest.TestCase):
    def test_session_id(self) -> None:
        ok = "1648822d-86eb-4655-bf84-6baf32bde3b6"
        self.assertEqual(C.validate_session_id(ok), ok)
        for bad in [
            "",
            "../../etc/passwd",
            ok.upper(),
            ok + "x",
            ok[:-1],
            " " + ok,
            None,
            7,
            "1648822d_86eb_4655_bf84_6baf32bde3b6",
            ok.replace("-", "/"),
        ]:
            with self.assertRaises(C.ChatError, msg=repr(bad)):
                C.validate_session_id(bad)

    def test_prompt(self) -> None:
        self.assertEqual(C.validate_prompt("hi"), "hi")
        self.assertEqual(len(C.validate_prompt("x" * C.MAX_PROMPT)), C.MAX_PROMPT)
        for bad in ["", "   \n", "x" * (C.MAX_PROMPT + 1), None, ["hi"]]:
            with self.assertRaises(C.ChatError):
                C.validate_prompt(bad)

    def test_modes_never_bypass(self) -> None:
        for m in C.PERMISSION_MODES:
            self.assertEqual(C.validate_choice(m, C.PERMISSION_MODES, "mode"), m)
        for bad in ["bypassPermissions", "acceptEdits", "dontAsk", "DEFAULT"]:
            with self.assertRaises(C.ChatError):
                C.validate_choice(bad, C.PERMISSION_MODES, "mode")
        self.assertEqual(C.MODELS[0], "opus")
        self.assertEqual(C.validate_choice(None, C.MODELS, "model"), "")

    def test_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            root = os.path.realpath(t)
            main, wt = Path(root, "main"), Path(root, "main", ".claude", "worktrees", "a")
            wt.mkdir(parents=True)
            other = Path(root, "other")
            other.mkdir()
            Path(root, "link").symlink_to(wt)
            dirs: list[C.ChatDir] = [
                {"path": str(main), "branch": "main", "is_main": True},
                {"path": str(wt), "branch": "x", "is_main": False},
            ]
            self.assertEqual(C.validate_cwd(str(main), dirs), str(main))
            self.assertEqual(C.validate_cwd(str(wt) + "/", dirs), str(wt))
            self.assertEqual(C.validate_cwd(str(Path(root, "link")), dirs), str(wt))  # same directory
            for bad in [
                str(other),
                str(wt / ".."),
                str(main / ".claude"),
                "",
                None,
                "/",
                str(main) + "\0",
                "relative/main",
            ]:
                with self.assertRaises(C.ChatError, msg=repr(bad)):
                    C.validate_cwd(bad, dirs)

    def test_worktree_dirs_reads_git(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            repo = Path(t, "r")
            repo.mkdir()

            def run(*a: str) -> None:
                subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)

            run("init", "-q", "-b", "main")
            run("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", "x")
            run("worktree", "add", "-q", "-b", "feat", str(Path(t, "wt")))
            dirs = C.worktree_dirs(str(repo))
            self.assertEqual(
                [(Path(d["path"]).name, d["branch"], d["is_main"]) for d in dirs],
                [("r", "main", True), ("wt", "feat", False)],
            )
            with self.assertRaises(C.ChatError):
                C.worktree_dirs(t)  # not a repository


class Buffer(unittest.TestCase):
    def test_ids_replay_and_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            log = Path(t, "c.jsonl")
            b = C.EventBuffer(log)
            b.append("user", text="hi")
            b.append("delta", text="he")
            b.append("text", text="hello")
            self.assertEqual([e["id"] for e in b.after(0)], [1, 2, 3])
            self.assertEqual([e["kind"] for e in b.after(1)], ["delta", "text"])
            self.assertEqual(b.after(3), [])
            # deltas are not persisted; ids continue after a reload
            with log.open("a") as fh:
                fh.write('{"torn')
            r = C.EventBuffer.load(log)
            self.assertEqual([(e["id"], e["kind"]) for e in r.events], [(1, "user"), (3, "text")])
            self.assertEqual(r.append("user", text="again")["id"], 4)

    def test_after_waits_for_an_event(self) -> None:
        b = C.EventBuffer()
        threading.Timer(0.1, lambda: b.append("info", text="x")).start()
        t0 = time.monotonic()
        evs = b.after(0, timeout=5)
        self.assertEqual([e["kind"] for e in evs], ["info"])
        self.assertLess(time.monotonic() - t0, 2)
        t0 = time.monotonic()
        self.assertEqual(b.after(1, timeout=0.1), [])  # times out without one
        self.assertGreaterEqual(time.monotonic() - t0, 0.09)

    def test_compaction_drops_only_superseded_deltas(self) -> None:
        b = C.EventBuffer()
        for _ in range(C.BUFFER_COMPACT):
            b.append("delta", text="x")
        b.append("text", text="full")
        b.append("delta", text="new")
        kinds = [e["kind"] for e in b.events]
        self.assertEqual(kinds, ["text", "delta"])
        self.assertEqual(b.after(0)[-1]["id"], C.BUFFER_COMPACT + 2)

    def test_close_wakes_a_waiting_stream_and_later_waits_return_at_once(self) -> None:
        b = C.EventBuffer()
        got: list[float] = []
        t0 = time.monotonic()

        def wait() -> None:
            b.after(0, timeout=10)
            got.append(time.monotonic() - t0)

        th = threading.Thread(target=wait)
        th.start()
        time.sleep(0.1)
        b.close()
        th.join(5)
        self.assertEqual(len(got), 1)
        self.assertLess(got[0], 2)
        t1 = time.monotonic()
        self.assertEqual(b.after(0, timeout=10), [])  # closed before the wait: no 10 s timeout
        self.assertLess(time.monotonic() - t1, 1)

    def test_tool_text_truncates(self) -> None:
        self.assertEqual(C.tool_text([{"type": "text", "text": "a"}, {"type": "image"}]), 'a\n{"type": "image"}')
        long = C.tool_text("x" * (C.MAX_TOOL_TEXT + 5))
        self.assertTrue(long.endswith("(5 more characters)"))
        self.assertEqual(C.tool_text(None), "")


class Registry(unittest.TestCase):
    def test_registry_reload_marks_ended(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            cid = "1648822d-86eb-4655-bf84-6baf32bde3b6"
            Path(t, "registry.json").write_text(
                json.dumps(
                    [
                        {
                            "id": cid,
                            "cwd": t,
                            "created": 1.0,
                            "title": "x",
                            "model": "",
                            "permission_mode": "default",
                            "resume_id": cid,
                        },
                        {"id": "../evil", "cwd": t, "created": 2.0, "title": "y"},
                    ]
                )
            )
            Path(t, f"{cid}.jsonl").write_text(json.dumps({"id": 5, "t": 9.0, "kind": "user", "text": "hi"}) + "\n")
            m = C.ChatManager(t, client_factory=no_client)
            rows = m.list()
            self.assertEqual([(r["id"], r["status"], r["last_event_id"]) for r in rows], [(cid, "ended", 5)])
            with self.assertRaises(C.ChatError):
                m.get("../evil")

    def test_delete_forgets_an_ended_chat_and_refuses_a_connected_one(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            cid, other = "1648822d-86eb-4655-bf84-6baf32bde3b6", "2648822d-86eb-4655-bf84-6baf32bde3b6"
            meta = {"cwd": t, "created": 1.0, "title": "x", "model": "", "permission_mode": "default"}
            Path(t, "registry.json").write_text(
                json.dumps([{"id": cid, "resume_id": cid, **meta}, {"id": other, "resume_id": other, **meta}])
            )
            for i in (cid, other):
                Path(t, f"{i}.jsonl").write_text(json.dumps({"id": 1, "t": 9.0, "kind": "user", "text": "hi"}) + "\n")
            m = C.ChatManager(t, client_factory=no_client)

            async def never() -> None:
                await asyncio.sleep(60)

            async def spawn() -> "asyncio.Task[None]":
                return asyncio.ensure_future(never())

            fut = asyncio.run_coroutine_threadsafe(spawn(), m.loop).result()  # stands in for a connected session
            m.chats[other].task = fut
            with self.assertRaises(C.ChatError):
                m.delete(other)
            # a connected session whose status reads "error" is still connected
            m.chats[other].status = "error"
            self.assertTrue(next(r for r in m.list() if r["id"] == other)["connected"])
            with self.assertRaises(C.ChatError):
                m.delete(other)
            self.assertIn(other, [r["id"] for r in m.list()])
            self.assertTrue(Path(t, f"{other}.jsonl").exists())

            m.delete(cid)
            self.assertEqual([r["id"] for r in m.list()], [other])
            self.assertFalse(Path(t, f"{cid}.jsonl").exists())
            saved = json.loads(Path(t, "registry.json").read_text())
            self.assertEqual([e["id"] for e in saved], [other])
            with self.assertRaises(C.ChatError):
                m.delete(cid)  # already gone
            with self.assertRaises(C.ChatError):
                m.delete("../evil")
            fut.cancel()
            # a restart does not bring it back
            again = C.ChatManager(t, client_factory=no_client)
            self.assertEqual([r["id"] for r in again.list()], [other])

    def test_delete_cannot_slip_between_resume_and_the_task_it_starts(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            cid = "1648822d-86eb-4655-bf84-6baf32bde3b6"
            Path(t, "registry.json").write_text(
                json.dumps(
                    [
                        {
                            "id": cid,
                            "cwd": t,
                            "created": 1.0,
                            "title": "x",
                            "model": "",
                            "permission_mode": "default",
                            "resume_id": cid,
                        }
                    ]
                )
            )
            m = C.ChatManager(t, client_factory=no_client)
            in_start, release = threading.Event(), threading.Event()

            async def slow_start(*_args: object) -> None:
                in_start.set()
                while not release.is_set():  # the gap: resume has passed its checks, no task exists yet
                    await asyncio.sleep(0.01)

            m._start = slow_start  # type: ignore[method-assign,assignment]
            t1 = threading.Thread(target=lambda: m.resume(cid, prompt="hi"))
            t1.start()
            self.assertTrue(in_start.wait(5))
            with self.assertRaises(C.ChatError):
                m.delete(cid)
            self.assertTrue(m.list()[0]["connected"])
            release.set()
            t1.join(5)
            self.assertFalse(m.chats[cid].starting)
            self.assertEqual([r["id"] for r in m.list()], [cid])

    def test_delete_flags_the_chat_so_streams_stop(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            cid = "1648822d-86eb-4655-bf84-6baf32bde3b6"
            Path(t, "registry.json").write_text(
                json.dumps(
                    [
                        {
                            "id": cid,
                            "cwd": t,
                            "created": 1.0,
                            "title": "x",
                            "model": "",
                            "permission_mode": "default",
                            "resume_id": cid,
                        }
                    ]
                )
            )
            m = C.ChatManager(t, client_factory=no_client)
            chat = m.get(cid)
            m.delete(cid)
            self.assertTrue(chat.deleted)
            self.assertTrue(chat.buffer.closed)


if __name__ == "__main__":
    unittest.main()


class CoordinatorRole(unittest.TestCase):
    def test_config_prompt_names_the_role_and_the_repo_rules(self) -> None:
        for s in (
            "coordinator",
            "AGENTS.md",
            "docs/process/coordinator.md",
            "wp-implementer",
            "reviewer",
            "merge trains",
        ):
            self.assertIn(s, TEST_CONFIG["coordinator"]["prompt_append"])

    def test_manager_keeps_the_configured_append(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            m = C.ChatManager(t, client_factory=no_client, prompt_append="ROLE")
            self.assertEqual(m.prompt_append, "ROLE")


class TurnEndStatus(unittest.TestCase):
    def chat(self) -> C.Chat:
        meta: C.ChatMeta = {
            "id": "x",
            "cwd": "/",
            "created": 0.0,
            "title": "t",
            "model": "",
            "permission_mode": "auto",
            "resume_id": None,
        }
        return C.Chat(meta, C.EventBuffer(None))

    def test_order_of_precedence(self) -> None:
        c = self.chat()
        self.assertEqual(C.turn_end_status(c, interrupted=False, is_error=False), "waiting")
        c.declared = ("done", "ready")
        self.assertEqual(C.turn_end_status(c, interrupted=False, is_error=False), "done")
        self.assertEqual(c.status_note, "ready")
        c.bg_tasks.add("t1")  # a background task outranks what the agent declared
        self.assertEqual(C.turn_end_status(c, interrupted=False, is_error=False), "background")
        self.assertEqual(C.turn_end_status(c, interrupted=False, is_error=True), "error")
        self.assertEqual(C.turn_end_status(c, interrupted=True, is_error=False), "idle")
