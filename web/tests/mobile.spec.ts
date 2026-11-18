import { expect, test, type Page } from "@playwright/test";

// A phone (Pixel 7 width): no sideways page scroll, every tab reachable, PR cards show verdict and actions.

const TOKEN = "test-token";
test.use({ viewport: { width: 412, height: 915 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 });

const SHOTS = process.env.PHONE_SHOTS;

async function noOverflow(page: Page) {
  const w = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, inner: window.innerWidth }));
  expect(w.scroll).toBeLessThanOrEqual(w.inner);
}

async function shot(page: Page, name: string) {
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/after-${name}.png`, fullPage: false });
}

test("phone: every tab fits and is clickable; PR cards show verdict and actions", async ({ page }) => {
  await page.goto(`/?token=${TOKEN}`);
  await expect(page.getByTestId("pr-101")).toBeVisible();
  await expect(page.getByTestId("pr-table")).toHaveCount(0);

  const tabs: [string, string][] = [
    ["All PRs", "pr-cards"],
    ["Sessions", "sessions"],
    ["Chats", "chats"],
    ["History", "history"],
  ];
  for (const [name, testid] of tabs) {
    const tab = page.getByRole("tab", { name });
    await expect(tab).toBeInViewport({ ratio: 1 });
    await tab.click();
    await expect(page.getByTestId(testid)).toBeVisible();
    await expect(page.getByTestId("repo-name")).toBeInViewport();
    await noOverflow(page);
    await shot(page, name.toLowerCase().replace(" ", "-"));
  }

  await page.getByRole("tab", { name: "All PRs" }).click();
  const card = page.getByTestId("pr-101");
  await card.scrollIntoViewIfNeeded();
  await card.evaluate((el) => el.scrollIntoView({ block: "center" }));
  await expect(card.getByTestId("verdict-101")).toBeInViewport({ ratio: 1 });
  const close = card.getByTestId("close-101");
  await expect(close).toBeInViewport({ ratio: 1 });
  // A modal fits the screen.
  await close.click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  const box = await dialog.boundingBox();
  expect(box).not.toBeNull();
  expect(box?.x).toBeGreaterThanOrEqual(0);
  expect((box?.x ?? 0) + (box?.width ?? 0)).toBeLessThanOrEqual(412);
  expect((box?.y ?? 0) + (box?.height ?? 0)).toBeLessThanOrEqual(915);
  await noOverflow(page);
  await shot(page, "modal");
  await page.getByTestId("dialog-cancel").click();
  await expect(dialog).toBeHidden();
});

test("phone: the log starts collapsed and opens when an action runs", async ({ page }) => {
  await page.goto(`/?token=${TOKEN}`);
  await expect(page.getByRole("button", { name: "Expand log" })).toBeVisible();
  const card = page.getByTestId("pr-101");
  await card.evaluate((el) => el.scrollIntoView({ block: "center" }));
  await card.getByTestId("close-101").click();
  await page.getByTestId("dialog-confirm").click();
  await expect(page.getByRole("button", { name: "Collapse log" })).toBeVisible();
  await expect(page.getByTestId("log-argv")).toBeVisible();
});

test("phone: chats are single pane with a way back; composer and permission controls usable", async ({ page }) => {
  await page.goto(`/?token=${TOKEN}`);
  await page.getByRole("tab", { name: "Chats" }).click();
  await page.getByTestId("chat-new").click();
  await page.getByTestId("chat-prompt").fill("perm");
  await noOverflow(page);
  await shot(page, "chat-launch");
  await page.getByTestId("chat-launch-go").click();
  const pane = page.getByTestId("chat-pane");
  await expect(pane).toBeVisible();
  await expect(page.getByTestId("chat-new")).toHaveCount(0); // list hidden: single pane
  await expect(page.getByTestId("permission-deny")).toBeVisible();
  await expect(page.getByTestId("permission-deny")).toBeInViewport({ ratio: 1 });
  await expect(page.getByTestId("permission-allow")).toBeInViewport({ ratio: 1 });
  await expect(page.getByTestId("chat-input")).toBeInViewport();
  await noOverflow(page);
  await shot(page, "chat-pane");
  await page.getByTestId("permission-deny").click();
  await expect(page.getByTestId("permission-deny")).toBeHidden();
  await expect(page.getByTestId("chat-send")).toBeInViewport({ ratio: 1 });

  await page.getByTestId("chat-back").click();
  await expect(pane).toHaveCount(0);
  await expect(page.getByTestId("chat-new")).toBeVisible();
  await noOverflow(page);
  await shot(page, "chat-list");
});
