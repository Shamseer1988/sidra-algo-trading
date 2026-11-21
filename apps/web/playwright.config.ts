import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/e2e",
  // Pages are compiled before the first test (see tests/warm-up.ts), and local
  // runs use at most four workers: the dev server compiles on request, and more
  // parallel first loads than that made the first assertion of a random test
  // or two time out on a busy machine.
  globalSetup: "./tests/warm-up.ts",
  fullyParallel: true,
  expect: { timeout: 10_000 },
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : 4,
  reporter: "list",
  use: {
    baseURL: "http://localhost:3100",
    trace: "on-first-retry",
  },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        // The container ships a Chromium that this Playwright build does not
        // expect by version number, and there is no network to fetch the one it
        // wants. Pointing at the installed binary keeps the suite runnable here;
        // PLAYWRIGHT_CHROMIUM_PATH lets any other environment override it, and
        // an unset variable falls through to Playwright's own resolution.
        launchOptions: process.env.PLAYWRIGHT_CHROMIUM_PATH
          ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_PATH }
          : {},
      },
    },
  ],
  webServer: {
    command: "npx next dev -p 3100",
    url: "http://localhost:3100",
    reuseExistingServer: !process.env.CI,
    timeout: 120 * 1000,
  },
});
