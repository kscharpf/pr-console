import { spawn, type ChildProcess } from "node:child_process";
import { expect, test, type Page, type Request } from "@playwright/test";

const TOKEN = "test-token";

interface Ctx {
  posts: unknown[];
  cspErrors: string[];
}

async function open(page: Page): Promise<Ctx> {
  const ctx: Ctx = { posts: [], cspErrors: [] };
  page.on("request", (r: Request) => {
    if (r.method() === "POST" && new URL(r.url()).pathname === "/api/action") ctx.posts.push(r.postDataJSON());
  });
  page.on("console", (m) => {
    if (m.type() === "error") ctx.cspErrors.push(m.text());
  });
  await page.goto(`/?token=${TOKEN}`);
  await expect(page.getByTestId("pr-101")).toBeVisible();
  return ctx;
}

const dialog = (page: Page) => page.getByRole("dialog");

/** Click a button: the modal must name the command; Cancel sends nothing; Confirm sends `body`. */
async function confirmFlow(page: Page, ctx: Ctx, click: () => Promise<void>, command: string, body: unknown, fill?: string) {
  await click();
  await expect(dialog(page)).toBeVisible();
  if (fill !== undefined) await page.getByTestId("dialog-input").fill(fill);
  await expect(page.getByTestId("dialog-command")).toHaveText(command);
  await page.getByTestId("dialog-cancel").click();
  await expect(dialog(page)).toBeHidden();
  await page.waitForTimeout(300);
  expect(ctx.posts).toEqual([]);

  await click();
  await expect(dialog(page)).toBeVisible();
  if (fill !== undefined) await page.getByTestId("dialog-input").fill(fill);
  await expect(page.getByTestId("dialog-command")).toHaveText(command);
  await page.getByTestId("dialog-confirm").click();
  await expect(dialog(page)).toBeHidden();
  await expect.poll(() => ctx.posts).toEqual([body]);
  await expect(page.getByTestId("log-output")).toContainText("fake run of");
  expect(ctx.cspErrors).toEqual([]);
  ctx.posts.length = 0;
}


test("assets load under the CSP, the token leaves the address bar", async ({ page }) => {
  const ctx = await open(page);
  expect(new URL(page.url()).search).toBe("");
  await expect(page.getByTestId("verdict-101")).toHaveText("READY FOR TRAIN");
  await expect(page.getByTestId("train-banner")).toContainText("#200");
  expect(ctx.cspErrors).toEqual([]);
});

const rowCases: [string, string, string, unknown, string?][] = [
  ["reverify-200", "reverify", "scripts/reverify --wait 200", { kind: "reverify", pr: 200 }],
  ["reverify_cancel-200", "cancel+reverify", "scripts/reverify --cancel 200", { kind: "reverify_cancel", pr: 200 }],
  ["merge-200", "merge", "gh pr merge 200 --merge", { kind: "merge", pr: 200 }],
  ["label-101", "add label", "gh pr edit 101 --add-label needs-maintainer", { kind: "label", pr: 101, add: true }],
  ["label-102", "remove label", "gh pr edit 102 --remove-label needs-maintainer", { kind: "label", pr: 102, add: false }],
  ["close-103", "close", "gh pr close 103", { kind: "close", pr: 103, comment: "" }],
  [
    "close-103",
    "close with comment",
    "gh pr close 103 --comment 'superseded by #101'",
    { kind: "close", pr: 103, comment: "superseded by #101" },
    "superseded by #101",
  ],
];

for (const [id, what, command, body, fill] of rowCases) {
  test(`row action: ${what}`, async ({ page }) => {
    const ctx = await open(page);
    await confirmFlow(page, ctx, () => page.getByTestId(id).click(), command, body, fill);
  });
}

test("merge and reverify are offered on train PRs only", async ({ page }) => {
  await open(page);
  await expect(page.getByTestId("merge-200")).toBeVisible();
  await expect(page.getByTestId("merge-101")).toHaveCount(0);
  await expect(page.getByTestId("reverify-101")).toHaveCount(0);
  await expect(page.getByTestId("pick-103")).toHaveCount(0); // NOT READY cannot be picked
});

test("train builder: pick, reorder by arrow and drag, dry run, build with the open-train warning", async ({ page }) => {
  const ctx = await open(page);
  await page.getByTestId("pick-101").check();
  await page.getByTestId("pick-102").check();
  const items = page.getByTestId("train-list").locator("li");
  await expect(items).toHaveText([/#101/, /#102/]);

  await page.getByRole("button", { name: "Move #102 up" }).click();
  await expect(items).toHaveText([/#102/, /#101/]);
  await page.getByTestId("train-item-102").dragTo(page.getByTestId("train-item-101"));
  await expect(items).toHaveText([/#101/, /#102/]);
  await page.getByRole("button", { name: "Move #101 down" }).click();
  await expect(items).toHaveText([/#102/, /#101/]);

  // A dry run changes nothing and needs no confirmation.
  await page.getByTestId("train-dry").click();
  await expect.poll(() => ctx.posts).toEqual([{ kind: "train", prs: [102, 101], dry_run: true }]);
  ctx.posts.length = 0;

  await page.getByTestId("train-build").click();
  await expect(page.getByTestId("train-open-warning")).toContainText("#200");
  await page.getByTestId("dialog-cancel").click();
  await page.getByTestId("train-build").click(); // reopen for the shared flow
  await page.getByTestId("dialog-cancel").click();
  await confirmFlow(page, ctx, () => page.getByTestId("train-build").click(), "scripts/train 102 101", {
    kind: "train",
    prs: [102, 101],
    dry_run: false,
  });
});

test("rebuild --without: dry run without a modal, rebuild with one", async ({ page }) => {
  const ctx = await open(page);
  await page.getByTestId("rb-train").fill("200");
  await page.getByTestId("rb-without").fill("#102");
  await page.getByTestId("rb-dry").click();
  await expect.poll(() => ctx.posts).toEqual([{ kind: "rebuild", train: 200, without: [102], dry_run: true }]);
  await expect(dialog(page)).toBeHidden();
  ctx.posts.length = 0;
  await confirmFlow(page, ctx, () => page.getByTestId("rb-run").click(), "scripts/train --rebuild 200 --without 102", {
    kind: "rebuild",
    train: 200,
    without: [102],
    dry_run: false,
  });
});

test("sessions cleanup: dry run, then two confirmations; cancelling either sends no --apply", async ({ page }) => {
  const ctx = await open(page);
  await page.getByRole("tab", { name: "Sessions" }).click();
  const btn = page.getByTestId("cleanup-m3/wp-done");
  await expect(btn).toBeVisible();
  await expect(page.getByText("ready to clean up")).toBeVisible();
  const dry = { kind: "cleanup", branch: "m3/wp-done", apply: false };
  const apply = { kind: "cleanup", branch: "m3/wp-done", apply: true };

  // Cancel at the first confirmation.
  await btn.click();
  await expect(page.getByTestId("dialog-output")).toContainText("would remove worktree");
  await expect(page.getByTestId("dialog-command")).toHaveText("scripts/cleanup-merged --apply m3/wp-done");
  expect(ctx.posts).toEqual([dry]);
  await page.getByTestId("dialog-cancel").click();
  await page.waitForTimeout(300);
  expect(ctx.posts).toEqual([dry]);
  ctx.posts.length = 0;

  // Cancel at the second.
  await btn.click();
  await page.getByTestId("dialog-confirm").click();
  await expect(dialog(page)).toContainText("Really delete m3/wp-done?");
  await page.getByTestId("dialog-cancel").click();
  await page.waitForTimeout(300);
  expect(ctx.posts).toEqual([dry]);
  ctx.posts.length = 0;

  // Confirm both.
  await btn.click();
  await page.getByTestId("dialog-confirm").click();
  await expect(dialog(page)).toContainText("Really delete m3/wp-done?");
  await page.getByTestId("dialog-confirm").click();
  await expect.poll(() => ctx.posts).toEqual([dry, apply]);
});

test("history lists the actions", async ({ page }) => {
  const ctx = await open(page);
  await page.getByTestId("train-dry").isDisabled();
  await page.getByTestId("pick-101").check();
  await page.getByTestId("train-dry").click();
  await expect.poll(() => ctx.posts.length).toBe(1);
  await page.getByRole("tab", { name: "History" }).click();
  await expect(page.getByTestId("history")).toContainText("--dry-run 101");
});

test("a reload keeps working; a new tab without the token is told so", async ({ page, context }) => {
  const ctx = await open(page);
  await page.reload();
  await expect(page.getByTestId("pr-101")).toBeVisible();
  expect(new URL(page.url()).search).toBe("");
  await page.reload();
  await expect(page.getByTestId("pr-101")).toBeVisible();
  await expect(page.getByTestId("load-error")).toHaveCount(0);
  // The data calls still carry the token after the reload.
  await page.getByTestId("label-101").click();
  await page.getByTestId("dialog-confirm").click();
  await expect.poll(() => ctx.posts).toEqual([{ kind: "label", pr: 101, add: true }]);

  const other = await context.newPage();
  const r = await other.goto("/");
  expect(r?.status()).toBe(403);
  await expect(other.locator("#msg")).toHaveText(/Missing token/);
});

test("the server: assets need no token, the page and /api do, traversal is refused", async ({ request }) => {
  expect((await request.get("/assets/unlock.js")).status()).toBe(200);
  expect((await request.get("/api/prs")).status()).toBe(403);
  expect((await request.get("/api/prs", { headers: { "X-Token": TOKEN } })).status()).toBe(200);
  // Traversal is covered by tests/test_server.py with raw paths (the client normalises dot segments).
  expect((await request.get("/assets/..%2f..%2fserver.py")).status()).toBe(404);
  expect((await request.get("/assets/unlock.js", { headers: { Host: "evil.example:8798" } })).status()).toBe(403);
  const page = await request.get("/");
  expect(page.status()).toBe(403);
  expect(await page.text()).not.toContain("/assets/index-");
  const post = await request.post("/api/action", { headers: { "X-Token": TOKEN, "Content-Type": "text/plain" }, data: "{}" });
  expect(post.status()).toBe(403);
});

test("auto-refresh polls /api/prs without swallowing clicks", async ({ page }) => {
  test.setTimeout(45_000);
  const ctx = await open(page);
  let polls = 0;
  page.on("request", (r) => {
    if (new URL(r.url()).pathname === "/api/prs") polls += 1;
  });
  await page.waitForTimeout(16_000);
  expect(polls).toBeGreaterThanOrEqual(1);
  await page.getByTestId("pick-101").check();
  await expect(page.getByTestId("pick-101")).toBeChecked();
  expect(ctx.cspErrors).toEqual([]);
});

test("dark mode follows the system and toggles", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "dark" });
  const ctx = await open(page);
  await expect(page.locator("html")).toHaveAttribute("data-mantine-color-scheme", "dark");
  await page.getByRole("button", { name: "Toggle colour scheme" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-mantine-color-scheme", "light");
  expect(ctx.cspErrors).toEqual([]);
});

test("a PR offers its diff in a new tab and its summary on hover", async ({ page }) => {
  await open(page);
  const diff = page.getByTestId("diff-101");
  await expect(diff).toHaveAttribute("href", "https://github.com/example/none/pull/101/files");
  await expect(diff).toHaveAttribute("target", "_blank");
  await page.getByTestId("title-101").hover();
  await expect(page.getByRole("tooltip")).toContainText("Summary of #101.");
});

test("row action buttons share one style", async ({ page }) => {
  await open(page);
  const variants = await page.locator('[data-testid^="reverify-"], [data-testid^="merge-"], [data-testid^="close-"], [data-testid^="label-"]')
    .evaluateAll((els) => els.map((e) => [e.getAttribute("data-variant"), e.getAttribute("data-size")].join("/")));
  expect(new Set(variants)).toEqual(new Set(["light/compact-xs"]));
});

test("the page takes the repo and its conventions from /api/config", async ({ page, request }) => {
  expect((await request.get("/api/config")).status()).toBe(403);
  const r = await request.get("/api/config", { headers: { "X-Token": TOKEN } });
  expect(r.status()).toBe(200);
  const cfg = await r.json();
  expect(cfg.gh_repo).toBe("example/none");
  expect(cfg.coordinator.prompt_append).toBeUndefined();
  await open(page);
  await expect(page.getByTestId("repo-name")).toHaveText("example/none");
  await page.getByTestId("merge-200").click();
  await expect(dialog(page)).toContainText("Runs in the Example checkout:");
  await page.getByTestId("dialog-cancel").click();
});

test.describe("a config without some commands", () => {
  const PORT = 8797;
  const base = `http://127.0.0.1:${PORT}`;
  let proc: ChildProcess | null = null;

  test.beforeAll(async () => {
    proc = spawn("../.venv/bin/python", ["-I", "tests/fake_server.py", "--port", String(PORT), "--config", "../tests/fixtures/minimal.toml"], {
      stdio: "ignore",
    });
    await expect
      .poll(async () => {
        try {
          return (await fetch(`${base}/assets/unlock.js`)).status;
        } catch {
          return 0;
        }
      })
      .toBe(200);
  });
  test.afterAll(async () => {
    const p = proc;
    proc = null;
    if (!p) return;
    const gone = new Promise((ok) => p.once("exit", ok));
    p.kill("SIGTERM");
    await gone;
  });

  test("hides the buttons of the actions it leaves out, and the server refuses them", async ({ page, request }) => {
    await page.goto(`${base}/?token=${TOKEN}`);
    await expect(page.getByTestId("pr-101")).toBeVisible();
    await expect(page.getByTestId("merge-200")).toBeVisible();
    await expect(page.getByTestId("close-101")).toBeVisible();
    await expect(page.getByTestId("reverify-200")).toHaveCount(0);
    await expect(page.getByTestId("reverify_cancel-200")).toHaveCount(0);
    await expect(page.getByTestId("label-101")).toHaveCount(0);
    await expect(page.getByTestId("train-dry")).toHaveCount(0);
    await expect(page.getByTestId("rb-dry")).toHaveCount(0);
    await expect(page.getByTestId("train-build")).toBeVisible();

    const post = (data: unknown) =>
      request.post(`${base}/api/action`, { headers: { "X-Token": TOKEN, "Content-Type": "application/json" }, data });
    for (const body of [{ kind: "reverify", pr: 200 }, { kind: "reverify_cancel", pr: 200 }, { kind: "label", pr: 101, add: true }]) {
      const r = await post(body);
      expect(r.status()).toBe(400);
      expect((await r.json()).error).toContain("not configured");
    }
    expect((await post({ kind: "train", prs: [101], dry_run: true })).status()).toBe(400);
    expect((await post({ kind: "close", pr: 103, comment: "" })).status()).toBe(202);
  });
});
