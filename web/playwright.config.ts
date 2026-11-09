import { defineConfig, devices } from "@playwright/test";

const PORT = 8798;

export default defineConfig({
  testDir: "tests",
  fullyParallel: false,
  workers: 1,
  reporter: "list",
  use: { baseURL: `http://127.0.0.1:${PORT}`, trace: "retain-on-failure" },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], viewport: { width: 1500, height: 1000 } } }],
  webServer: {
    // The real server.py Handler serving the real build (web/dist), with fixtures in place of GitHub.
    command: `../.venv/bin/python -I tests/fake_server.py --port ${PORT}`,
    url: `http://127.0.0.1:${PORT}/assets/unlock.js`,
    reuseExistingServer: false,
    timeout: 20_000,
  },
});
