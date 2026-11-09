import { spawn, type ChildProcess } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { expect, test, type Page } from "@playwright/test";

// Chats against the real server.py and the real claude_agent_sdk client over a fake CLI
// transport (tests/fake_sdk.py): no model is called. The fake replies by the prompt's first word.

const TOKEN = "test-token";

interface Ctx {
  errors: string[];
  decisions: unknown[];
}

async function open(page: Page, base = ""): Promise<Ctx> {
  const ctx: Ctx = { errors: [], decisions: [] };
  page.on("console", (m) => {
    if (m.type() === "error") ctx.errors.push(m.text());
  });
  page.on("request", (r) => {
    if (r.method() === "POST" && new URL(r.url()).pathname.endsWith("/permission")) ctx.decisions.push(r.postDataJSON());
  });
  await page.goto(`${base}/?token=${TOKEN}`);
  await page.getByRole("tab", { name: "Chats" }).click();
  await expect(page.getByTestId("chats")).toBeVisible();
  return ctx;
}

/** Launch a chat with `prompt` and return its id (it is then the selected chat). */
async function launch(page: Page, prompt: string): Promise<string> {
  await page.getByTestId("chat-new").click();
  await expect(page.getByTestId("chat-role")).toContainText("Coordinator session in the main checkout");
  await page.getByTestId("chat-prompt").fill(prompt);
  await page.getByTestId("chat-launch-go").click();
  const id = page.getByTestId("chat-pane").getByTestId("chat-id");
  await expect(id).toHaveText(/^[0-9a-f-]{36}$/);
  await expect(page.getByTestId("chat-pane").getByTestId("msg-user").first()).toContainText(prompt);
  return (await id.textContent()) ?? "";
}

const pane = (page: Page) => page.getByTestId("chat-pane");
const status = (page: Page) => pane(page).getByTestId("chat-status");

test("launch streams the reply; a follow-up continues; no CSP errors", async ({ page }) => {
  const ctx = await open(page);
  await expect(page.getByTestId("chat-mode").locator("option")).toHaveCount(0); // form closed
  await page.getByTestId("chat-new").click();
  // No mode that bypasses permissions is offered; auto is the default.
  await expect(page.getByTestId("chat-mode").locator("option")).toHaveText([/^auto/, /^default/, /^plan/]);
  await expect(page.getByTestId("chat-mode")).toHaveValue("auto");
  await page.getByTestId("chat-new").click();

  await launch(page, "hello there");
  await expect(pane(page).getByTestId("msg-assistant").last()).toHaveText("echo: hello there");
  await expect(status(page)).toHaveText("waiting for you");
  await expect(pane(page).getByTestId("msg-result")).toHaveCount(1);

  await page.getByTestId("chat-input").fill("second question");
  await page.getByTestId("chat-send").click();
  await expect(pane(page).getByTestId("msg-assistant").last()).toHaveText("echo: second question");
  await expect(pane(page).getByTestId("msg-assistant")).toHaveCount(2);
  await expect(page.getByTestId("chat-input")).toHaveValue("");
  expect(ctx.errors).toEqual([]);
});

test("interrupt stops a streaming turn", async ({ page }) => {
  const ctx = await open(page);
  await launch(page, "slow");
  await expect(pane(page).getByTestId("msg-draft")).toContainText("tick 3");
  await expect(status(page)).toHaveText("running");
  await page.getByTestId("chat-interrupt").click();
  await expect(pane(page).getByTestId("msg-result")).toHaveText(/interrupted/);
  await expect(status(page)).toHaveText("idle");
  await expect(page.getByTestId("chat-interrupt")).toBeDisabled();
  expect(ctx.errors).toEqual([]);
});

test("permission: Deny has the focus and sends deny; Allow sends allow", async ({ page }) => {
  const ctx = await open(page);
  const id = await launch(page, "perm");
  await expect(page.getByTestId("permission-tool")).toHaveText("Bash");
  await expect(page.getByTestId("permission-input")).toContainText('"command": "ls -la"');
  await expect(page.getByTestId("permission-deny")).toBeFocused();
  await expect(status(page)).toHaveText("waiting for permission");
  await page.keyboard.press("Enter"); // the focused button: Deny
  await expect(page.getByTestId("permission-input")).toBeHidden();
  await expect.poll(() => ctx.decisions).toEqual([{ req_id: expect.stringMatching(/^[0-9a-f]{16}$/), allow: false }]);
  await expect(pane(page).getByTestId("msg-permission")).toHaveText("permission for Bash: denied");
  await pane(page).getByTestId("tool-result").locator("summary").click();
  await expect(pane(page).getByTestId("tool-result")).toContainText("denied: The maintainer denied this");
  await expect(status(page)).toHaveText("waiting for you");

  ctx.decisions.length = 0;
  await page.getByTestId("chat-input").fill("perm again");
  await page.getByTestId("chat-send").click();
  await expect(page.getByTestId("permission-tool")).toHaveText("Bash");
  await page.getByTestId("permission-allow").click();
  await expect.poll(() => ctx.decisions).toEqual([{ req_id: expect.stringMatching(/^[0-9a-f]{16}$/), allow: true }]);
  await expect(pane(page).getByTestId("tool-result").last()).toContainText("ran ls -la");
  await expect(pane(page).getByTestId("msg-assistant").last()).toHaveText("permission flow finished");
  expect(id).toMatch(/^[0-9a-f-]{36}$/);
  expect(ctx.errors).toEqual([]);
});

test("permission: two requests at once are queued and shown one at a time", async ({ page }) => {
  const ctx = await open(page);
  await launch(page, "perm2");
  await expect(page.getByTestId("permission-input")).toContainText("ls -la");
  await expect(page.getByTestId("permission-queue")).toHaveText("1 more waiting");
  await expect(page.getByRole("dialog")).toHaveCount(1);
  await page.getByTestId("permission-allow").click();
  await expect(page.getByTestId("permission-input")).toContainText("git status");
  await expect(page.getByTestId("permission-queue")).toHaveText("");
  await expect(page.getByTestId("permission-deny")).toBeFocused();
  await page.getByTestId("permission-deny").click();
  await expect(page.getByTestId("permission-input")).toBeHidden();
  expect(ctx.decisions.map((d) => (d as { allow: boolean }).allow)).toEqual([true, false]);
  await expect(pane(page).getByTestId("msg-permission")).toHaveText(["permission for Bash: allowed", "permission for Bash: denied"]);
  expect(ctx.errors).toEqual([]);
});

test("hook blocks and errors show in the chat; markdown renders without raw HTML", async ({ page }) => {
  const ctx = await open(page);
  await launch(page, "hook");
  await expect(pane(page).getByTestId("msg-hook")).toContainText("background jobs (&) are blocked");
  await expect(pane(page).getByTestId("msg-tool")).toContainText("error");

  await page.getByTestId("chat-input").fill("fail");
  await page.getByTestId("chat-send").click();
  await expect(pane(page).getByTestId("msg-result").last()).toContainText("turn failed: API Error: 529");
  await expect(status(page)).toHaveText("error");

  await page.getByTestId("chat-input").fill("md");
  await page.getByTestId("chat-send").click();
  const md = pane(page).getByTestId("msg-assistant").last();
  await expect(md.locator("h3")).toHaveText("Heading");
  await expect(md.locator("strong")).toHaveText("bold");
  await expect(md.locator("li")).toHaveText(["one", "two"]);
  await expect(md.locator('a[href="https://example.com"]')).toHaveAttribute("rel", "noopener noreferrer");
  await expect(md.locator("img, script, b")).toHaveCount(0);
  await expect(md).toContainText('<img src=x onerror="window.__xss=1">');
  await expect(md.locator("pre")).toContainText("fenced <b>not bold</b>");
  await expect(md.locator('a[href^="javascript"]')).toHaveCount(0);
  expect(await page.evaluate(() => (globalThis as unknown as { __xss?: number }).__xss)).toBeUndefined();
  expect(ctx.errors).toEqual([]);
});

test("switching chats keeps each one's state; the stream resumes after the last event", async ({ page }) => {
  const ctx = await open(page);
  const a = await launch(page, "alpha one");
  await expect(pane(page).getByTestId("msg-assistant")).toHaveText(["echo: alpha one"]);
  const b = await launch(page, "beta one");
  await expect(pane(page).getByTestId("msg-assistant")).toHaveText(["echo: beta one"]);

  await page.getByTestId(`chat-${a}`).click();
  await expect(pane(page).getByTestId("chat-id")).toHaveText(a);
  await expect(pane(page).getByTestId("msg-assistant")).toHaveText(["echo: alpha one"]);
  await page.getByTestId("chat-input").fill("alpha two");
  await page.getByTestId("chat-send").click();
  await expect(pane(page).getByTestId("msg-assistant")).toHaveText(["echo: alpha one", "echo: alpha two"]);

  await page.getByTestId(`chat-${b}`).click();
  await expect(pane(page).getByTestId("msg-assistant")).toHaveText(["echo: beta one"]);
  await page.getByTestId(`chat-${a}`).click();
  // Reopened with ?after=<last id>: nothing is repeated.
  await expect(pane(page).getByTestId("msg-assistant")).toHaveText(["echo: alpha one", "echo: alpha two"]);
  await expect(pane(page).getByTestId("msg-user")).toHaveText(["alpha one", "alpha two"]);

  // The server replays from Last-Event-ID, as EventSource sends it when it reconnects by itself.
  const replay = await page.evaluate(async (id) => {
    const token = sessionStorage.getItem("pr-console-token") ?? "";
    const r = await fetch(`/api/chats/${id}/events?token=${token}`, { headers: { "Last-Event-ID": "3" } });
    const reader = r.body!.getReader();
    let text = "";
    // bound: until the replay has reached the first assistant text, or 20 chunks
    for (let i = 0; i < 20 && !text.includes('"kind": "result"'); i++) {
      const { value, done } = await reader.read();
      if (done) break;
      text += new TextDecoder().decode(value);
    }
    await reader.cancel();
    return [...text.matchAll(/^id: (\d+)$/gm)].map((m) => Number(m[1]));
  }, a);
  expect(replay[0]).toBe(4);

  expect(ctx.errors).toEqual([]);
});

test("ending a chat, then resuming it by sending", async ({ page }) => {
  const ctx = await open(page);
  await launch(page, "first");
  await expect(status(page)).toHaveText("waiting for you");
  await page.getByTestId("chat-end").click();
  await expect(status(page)).toHaveText("ended");
  await expect(page.getByTestId("chat-resume")).toBeVisible();
  await page.getByTestId("chat-input").fill("after the end");
  await page.getByTestId("chat-send").click();
  await expect(pane(page).getByTestId("msg-assistant").last()).toHaveText("echo: after the end");
  await expect(status(page)).toHaveText("waiting for you");
  expect(ctx.errors).toEqual([]);
});

test("deleting a chat: only when ended, through a modal that names the request", async ({ page, request }) => {
  const h = { "X-Token": TOKEN, "Content-Type": "application/json" };
  // Earlier tests share this server and may hold all the session slots.
  for (const c of (await (await request.get("/api/chats", { headers: h })).json()) as { id: string; status: string }[])
    if (c.status !== "ended") await request.post(`/api/chats/${c.id}/end`, { headers: h, data: {} });
  const ctx = await open(page);
  const id = await launch(page, "to delete");
  await expect(status(page)).toHaveText("waiting for you");
  // A connected session cannot be deleted: the button is off and the server refuses.
  await expect(page.getByTestId("chat-delete")).toBeDisabled();
  expect((await request.post(`/api/chats/${id}/delete`, { headers: h, data: {} })).status()).toBe(400);
  await page.getByTestId("chat-end").click();
  await expect(status(page)).toHaveText("ended");
  await expect(page.getByTestId("chat-delete")).toBeEnabled(); // enabled once the session is fully disconnected

  const posts: string[] = [];
  page.on("request", (r) => {
    if (r.method() === "POST" && r.url().endsWith("/delete")) posts.push(r.url());
  });
  // Cancelling sends nothing.
  await page.getByTestId("chat-delete").click();
  await expect(page.getByTestId("dialog-command")).toHaveText(`POST /api/chats/${id}/delete`);
  await page.getByTestId("dialog-cancel").click();
  await expect(page.getByTestId("dialog")).toBeHidden();
  expect(posts).toEqual([]);
  await expect(page.getByTestId(`chat-${id}`)).toBeVisible();

  // Confirming removes the chat from the list and the pane.
  await page.getByTestId("chat-delete").click();
  await page.getByTestId("dialog-confirm").click();
  await expect(page.getByTestId(`chat-${id}`)).toHaveCount(0);
  await expect(page.getByTestId("chat-pane")).toHaveCount(0);
  expect(posts).toHaveLength(1);
  const rows = (await (await request.get("/api/chats", { headers: h })).json()) as { id: string }[];
  expect(rows.map((r) => r.id)).not.toContain(id);
  expect((await request.post(`/api/chats/${id}/delete`, { headers: h, data: {} })).status()).toBe(400);
  expect((await request.post("/api/chats/not-an-id/delete", { headers: h, data: {} })).status()).toBe(400);

  // Adopting the same session ID again starts a clean chat: the page forgot the old events.
  await page.getByTestId("chat-new").click();
  await page.getByTestId("chat-resume-id").fill(id);
  await page.getByTestId("chat-resume-go").click();
  await expect(page.getByTestId("chat-pane").getByTestId("chat-id")).toHaveText(id);
  await page.getByTestId("chat-input").fill("back again");
  await page.getByTestId("chat-send").click();
  await expect(pane(page).getByTestId("msg-assistant").last()).toHaveText("echo: back again");
  expect(ctx.errors).toEqual([]);
});

test("the server validates chat input", async ({ request }) => {
  const h = { "X-Token": TOKEN, "Content-Type": "application/json" };
  const dirs = (await (await request.get("/api/chats/dirs", { headers: h })).json()) as { path: string }[];
  const cwd = dirs[0]!.path;
  const post = (path: string, data: unknown) => request.post(path, { headers: h, data });
  expect((await post("/api/chats", { cwd, prompt: "" })).status()).toBe(400);
  expect((await post("/api/chats", { cwd, prompt: "x".repeat(16_001) })).status()).toBe(400);
  expect((await post("/api/chats", { cwd, prompt: "x", permission_mode: "bypassPermissions" })).status()).toBe(400);
  expect((await post("/api/chats", { cwd, prompt: "x", permission_mode: "acceptEdits" })).status()).toBe(400);
  expect((await post("/api/chats", { cwd, prompt: "x", model: "gpt" })).status()).toBe(400);
  expect((await post("/api/chats/resume", { id: "../../etc" })).status()).toBe(400);
  expect((await post("/api/chats/resume", { id: "00000000-0000-0000-0000-000000000000" })).status()).toBe(400);
  expect((await request.post("/api/chats", { headers: h, data: "x".repeat(70_000) })).status()).toBe(413);
  // The query token is for the event stream only.
  expect((await request.get(`/api/chats?token=${TOKEN}`)).status()).toBe(403);
  expect((await request.get(`/api/chats/00000000-0000-0000-0000-000000000000/events?token=wrong`)).status()).toBe(403);
});

test.describe("restart", () => {
  test.describe.configure({ timeout: 90_000 }); // starts its own server, slow under load
  const PORT = 8799;
  let proc: ChildProcess | null = null;
  const dir = mkdtempSync(join(tmpdir(), "pr-console-restart-"));

  async function start() {
    proc = spawn("../.venv/bin/python", ["-I", "tests/fake_server.py", "--port", String(PORT), "--chats-dir", dir], { stdio: "ignore" });
    await expect
      .poll(async () => {
        try {
          return (await fetch(`http://127.0.0.1:${PORT}/assets/unlock.js`)).status;
        } catch {
          return 0;
        }
      }, { timeout: 25_000, message: "the second fake server did not start (slow under load?)" })
      .toBe(200);
  }
  async function stop() {
    const p = proc;
    proc = null;
    if (!p) return;
    const gone = new Promise((ok) => p.once("exit", ok));
    p.kill("SIGTERM");
    await gone;
  }
  test.afterAll(stop);

  test("a restart lists past chats and reloads their transcripts", async ({ page }) => {
    await start();
    const base = `http://127.0.0.1:${PORT}`;
    await open(page, base);
    const id = await launch(page, "before restart");
    await expect(pane(page).getByTestId("msg-assistant")).toHaveText(["echo: before restart"]);
    await stop();
    await start();

    const ctx = await open(page, base);
    const card = page.getByTestId(`chat-${id}`);
    await expect(card).toContainText("before restart");
    await expect(card.getByTestId("chat-status")).toHaveText("ended");
    await card.click();
    await expect(pane(page).getByTestId("msg-user")).toHaveText(["before restart"]);
    await expect(pane(page).getByTestId("msg-assistant")).toHaveText(["echo: before restart"]);
    // Resuming after the restart continues the same chat.
    await page.getByTestId("chat-input").fill("after restart");
    await page.getByTestId("chat-send").click();
    await expect(pane(page).getByTestId("msg-assistant")).toHaveText(["echo: before restart", "echo: after restart"]);
    expect(ctx.errors).toEqual([]);
  });
});

test("a chat is always a coordinator in the main checkout", async ({ request }) => {
  const h = { "X-Token": TOKEN, "Content-Type": "application/json" };
  const dirs = (await (await request.get("/api/chats/dirs", { headers: h })).json()) as { path: string; is_main: boolean }[];
  const main = dirs.find((d) => d.is_main)!.path;
  // Earlier tests share this server and may hold all the session slots.
  for (const c of (await (await request.get("/api/chats", { headers: h })).json()) as { id: string; status: string }[])
    if (c.status !== "ended") await request.post(`/api/chats/${c.id}/end`, { headers: h, data: {} });
  const r = await request.post("/api/chats", { headers: h, data: { cwd: "/etc", prompt: "where am I" } });
  expect(r.status(), await r.text()).toBe(201);
  const chat = (await r.json()) as { id: string; cwd: string };
  expect(chat.cwd).toBe(main);
  expect((await request.post(`/api/chats/${chat.id}/end`, { headers: h, data: {} })).ok()).toBe(true);
});

test("a long transcript scrolls to its last line, above the log panel", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  const ctx = await open(page);
  await launch(page, "long");
  const last = pane(page).getByTestId("msg-assistant").filter({ hasText: "LAST LINE" });
  await expect(last).toBeVisible();
  const t = pane(page).getByTestId("transcript");
  await expect.poll(() => t.evaluate((el) => el.scrollHeight - el.scrollTop - el.clientHeight)).toBeLessThan(2);
  // The last line and the message box sit above the fixed log panel.
  const logTop = await page.locator(".log").evaluate((el) => el.getBoundingClientRect().top);
  expect((await last.boundingBox())!.y + (await last.boundingBox())!.height).toBeLessThanOrEqual(logTop);
  expect((await page.getByTestId("chat-send").boundingBox())!.y).toBeLessThan(logTop);

  // Scrolled up to read, new output does not pull the reader down.
  await t.evaluate((el) => { el.scrollTop = 0; el.dispatchEvent(new Event("scroll")); });
  await page.getByTestId("chat-input").fill("more");
  await page.getByTestId("chat-send").click();
  await expect(pane(page).getByTestId("msg-assistant").last()).toHaveText("echo: more");
  expect(await t.evaluate((el) => el.scrollTop)).toBe(0);
  expect(ctx.errors).toEqual([]);
});

test("messages show their time, and a running turn shows how long it has worked", async ({ page }) => {
  const ctx = await open(page);
  await launch(page, "slow");
  await expect(pane(page).getByTestId("chat-activity")).toContainText(/working for \d+s · last output \d+s ago/);
  await page.getByTestId("chat-interrupt").click();
  await expect(status(page)).toHaveText("idle");
  await expect(pane(page).getByTestId("chat-activity")).toHaveCount(0);
  const times = pane(page).getByTestId("msg-time");
  expect(await times.count()).toBeGreaterThan(1);
  await expect(times.first()).toHaveText(/^\d{1,2}:\d{2}:\d{2}/);
  expect(ctx.errors).toEqual([]);
});

test("switching tabs and back keeps the chat's place: the bottom when following, else where the reader was", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  const ctx = await open(page);
  await launch(page, "long");
  const t = () => pane(page).getByTestId("transcript");
  const gap = () => t().evaluate((el) => el.scrollHeight - el.scrollTop - el.clientHeight);
  await expect.poll(gap).toBeLessThan(2);

  await page.getByRole("tab", { name: "All PRs" }).click();
  await page.getByRole("tab", { name: "Chats" }).click();
  await expect.poll(gap).toBeLessThan(2); // following: back at the bottom

  await t().evaluate((el) => { el.scrollTop = 300; el.dispatchEvent(new Event("scroll")); });
  await page.getByRole("tab", { name: "All PRs" }).click();
  await page.getByRole("tab", { name: "Chats" }).click();
  await expect.poll(() => t().evaluate((el) => Math.round(el.scrollTop))).toBe(300); // reading: same place
  expect(ctx.errors).toEqual([]);
});
