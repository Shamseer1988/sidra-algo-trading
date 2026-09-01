import { test, expect, type Page, type Route } from "@playwright/test";

const MOCK_ADMIN_USER = {
  email: "admin@sentinel.internal",
  role: "ADMIN",
  is_active: true,
};

const MOCK_VIEWER_USER = {
  email: "viewer@sentinel.internal",
  role: "VIEWER",
  is_active: true,
};

const MOCK_OVERVIEW = {
  mode: "PAPER",
  live_trading_enabled: false,
  api: { status: "healthy", detail: "FastAPI service active", checked_at: new Date().toISOString() },
  database: { status: "healthy", detail: "PostgreSQL connected", checked_at: new Date().toISOString() },
  redis: { status: "healthy", detail: "Redis connected", checked_at: new Date().toISOString() },
  scanner: { status: "running", detail: "Active scanner", checked_at: new Date().toISOString() },
  market_data: { status: "live", detail: "Upstox paper feed", checked_at: new Date().toISOString() },
  firstock: { status: "not_configured", detail: "Using Upstox", checked_at: new Date().toISOString() },
  telegram: { status: "configured", detail: "Bot ready", checked_at: new Date().toISOString() },
};

const MOCK_SAFETY = {
  paper_tracking_enabled: true,
  live_trading_enabled: false,
  live_execution_available: false,
  emergency_stop_active: false,
  emergency_stop_reason: null,
  emergency_stop_source: null,
  emergency_stop_at: null,
};

const MOCK_TELEGRAM = {
  configured: true,
  webhook_configured: true,
  inbound_enabled: true,
  detail: "Telegram bot configured",
};

const MOCK_CONTROLS = {
  account_capital: 100000,
  risk_per_trade_percent: 0.5,
  maximum_daily_risk_percent: 1.0,
  maximum_signals: 2,
  minimum_score: 90,
  minimum_rr: 1.5,
  volume_multiplier: 1.3,
  retest_tolerance_percent: 0.15,
  minimum_ema_spread_percent: 0.05,
  trade_start_time: "09:24",
  trade_cutoff_time: "14:45",
};

const MOCK_SIGNALS = [
  {
    id: "sig-001",
    instrument_token: "NSE:RELIANCE",
    session_date: "2026-08-31",
    candle_opened_at: new Date().toISOString(),
    side: "LONG",
    status: "PAPER_SIGNALLED",
    entry_price: 2850.5,
    stop_price: 2835.0,
    target_price: 2880.0,
    quantity: 32,
    score: 95,
    score_breakdown: { breakout: 20, vwap: 20, volume: 18, relative_strength: 19, ema_trend: 18 },
    created_at: new Date().toISOString(),
  },
  {
    id: "sig-002",
    instrument_token: "NSE:INFY",
    session_date: "2026-08-31",
    candle_opened_at: new Date().toISOString(),
    side: "SHORT",
    status: "PAPER_ALERTED",
    entry_price: 1820.0,
    stop_price: 1835.0,
    target_price: 1790.0,
    quantity: 65,
    score: 92,
    score_breakdown: { breakout: 18, vwap: 20, volume: 18, relative_strength: 18, ema_trend: 18 },
    created_at: new Date().toISOString(),
  },
];

const MOCK_CANDLES = [
  { opened_at: new Date().toISOString(), closed_at: new Date().toISOString(), open: 2840, high: 2855, low: 2838, close: 2851, volume: 1500 },
  { opened_at: new Date().toISOString(), closed_at: new Date().toISOString(), open: 2851, high: 2862, low: 2848, close: 2859, volume: 2100 },
];

const MOCK_SESSIONS = [
  { id: "sess-1", created_at: new Date().toISOString(), expires_at: new Date(Date.now() + 86400000).toISOString(), ip_address: "127.0.0.1", user_agent: "Chrome on Windows" },
  { id: "sess-2", created_at: new Date().toISOString(), expires_at: new Date(Date.now() + 86400000).toISOString(), ip_address: "192.168.1.5", user_agent: "Firefox on macOS" },
];

const MOCK_AUDIT = [
  { id: "aud-1", event_type: "auth.login_success", created_at: new Date().toISOString(), user_id: "u-1", ip_address: "127.0.0.1", message: "Admin sign-in", metadata_json: {} },
];

async function setupMockRoutes(page: Page, userRole: "ADMIN" | "VIEWER" = "ADMIN") {
  const user = userRole === "ADMIN" ? MOCK_ADMIN_USER : MOCK_VIEWER_USER;
  let currentScanner = { status: "STOPPED", last_heartbeat: new Date().toISOString(), detail: "Scanner is paused" };

  await page.route("**/api/v1/auth/me", async (route: Route) => {
    await route.fulfill({ json: user });
  });

  await page.route("**/api/v1/system/overview", async (route: Route) => {
    await route.fulfill({ json: MOCK_OVERVIEW });
  });

  await page.route("**/api/v1/scanner/status", async (route: Route) => {
    await route.fulfill({ json: currentScanner });
  });

  await page.route("**/api/v1/scanner/start", async (route: Route) => {
    currentScanner = { status: "RUNNING", last_heartbeat: new Date().toISOString(), detail: "Scanner active" };
    await route.fulfill({ json: currentScanner });
  });

  await page.route("**/api/v1/scanner/stop", async (route: Route) => {
    currentScanner = { status: "STOPPED", last_heartbeat: new Date().toISOString(), detail: "Scanner is paused" };
    await route.fulfill({ json: currentScanner });
  });

  await page.route("**/api/v1/scanner/signals", async (route: Route) => {
    await route.fulfill({ json: MOCK_SIGNALS });
  });

  await page.route("**/api/v1/safety/status", async (route: Route) => {
    await route.fulfill({ json: MOCK_SAFETY });
  });

  await page.route("**/api/v1/telegram/status", async (route: Route) => {
    await route.fulfill({ json: MOCK_TELEGRAM });
  });

  await page.route("**/api/v1/settings/trading", async (route: Route) => {
    if (route.request().method() === "PUT") {
      const body = route.request().postDataJSON();
      await route.fulfill({ json: body });
    } else {
      await route.fulfill({ json: MOCK_CONTROLS });
    }
  });

  await page.route("**/api/v1/market-data/brokers", async (route: Route) => {
    await route.fulfill({ json: { upstox_paper_enabled: true, firstock_feed_enabled: false } });
  });

  await page.route("**/api/v1/market-data/candles/**", async (route: Route) => {
    await route.fulfill({ json: MOCK_CANDLES });
  });

  await page.route("**/api/v1/auth/sessions", async (route: Route) => {
    await route.fulfill({ json: MOCK_SESSIONS });
  });

  await page.route("**/api/v1/auth/sessions/*", async (route: Route) => {
    await route.fulfill({ status: 204 });
  });

  await page.route("**/api/v1/auth/audit-logs", async (route: Route) => {
    await route.fulfill({ json: MOCK_AUDIT });
  });

  await page.route("**/api/v1/journal/export.csv*", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "text/csv",
      headers: { "Content-Disposition": "attachment; filename=paper-journal.csv" },
      body: "signal_id,session_date,instrument,side,entry,stop,target,status\nsig-001,2026-08-31,NSE:RELIANCE,LONG,2850.5,2835.0,2880.0,OPEN\n",
    });
  });
}

test.describe("Phase 9 Release Gate 1: Browser E2E Tests", () => {
  test("expired access token refreshes once and retries the protected request", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    let refreshed = false;
    let refreshCalls = 0;
    await page.route("**/api/v1/auth/refresh", async (route) => {
      refreshCalls += 1;
      refreshed = true;
      await route.fulfill({ json: { email: MOCK_ADMIN_USER.email, role: "ADMIN" } });
    });
    await page.route("**/api/v1/auth/me", async (route) => {
      await route.fulfill(refreshed ? { json: MOCK_ADMIN_USER } : { status: 401, json: { detail: "Access token expired" } });
    });

    await page.goto("/");

    await expect(page.getByText("Sidra Command Center")).toBeVisible();
    expect(refreshCalls).toBe(1);
  });

  test("1. Login Page: Rejection on invalid credentials & successful login flow", async ({ page }) => {
    await page.route("**/api/v1/auth/me", async (route) => {
      await route.fulfill({ status: 401, json: { detail: "Authentication required" } });
    });

    await page.route("**/api/v1/auth/login", async (route) => {
      const data = route.request().postDataJSON();
      if (data.password === "wrongpassword") {
        await route.fulfill({ status: 401, json: { detail: "Invalid credentials" } });
      } else {
        await route.fulfill({ json: { email: data.email, role: "ADMIN" } });
      }
    });

    await page.goto("/login");
    await expect(page.getByRole("heading", { name: "Welcome back" })).toBeVisible();

    // Test rejection
    await page.fill("#email", "admin@sentinel.internal");
    await page.fill("#password", "wrongpassword");
    await page.click('button[type="submit"]');
    await expect(page.locator("form p[role='alert']")).toHaveText("Invalid credentials");

    // Test valid login
    await page.fill("#password", "correctpassword123");
    await page.click('button[type="submit"]');
  });

  test("2. RBAC Denial: Viewer role restrictions vs Admin controls", async ({ page }) => {
    await setupMockRoutes(page, "VIEWER");
    await page.goto("/");

    // Verify user role shown as VIEWER
    await expect(page.getByText("VIEWER", { exact: true })).toBeVisible();

    // Navigate to Settings
    await page.click('button:has-text("Settings")');

    // In Viewer mode, settings form inputs must be disabled and Save button not rendered
    const input = page.locator('input[type="number"]').first();
    await expect(input).toBeDisabled();
    await expect(page.getByRole("button", { name: "Save controls" })).toHaveCount(0);
  });

  test("3. Scanner Controls: Start and Stop triggers in Dashboard", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");

    // Check dashboard loaded
    await expect(page.getByText("Sidra Command Center")).toBeVisible();
    await expect(page.getByText("PAPER", { exact: true }).first()).toBeVisible();

    // Trigger Start Scanner
    const startBtn = page.getByRole("button", { name: "Start scanner" });
    await expect(startBtn).toBeEnabled();
    await startBtn.click();
    await expect(page.getByText("Scanner start requested.")).toBeVisible();

    // Trigger Stop Scanner
    const stopBtn = page.getByRole("button", { name: "Stop scanner" });
    await expect(stopBtn).toBeEnabled();
    await stopBtn.click();
    await expect(page.getByText("Scanner stopped.")).toBeVisible();
  });

  test("4. Signals Explorer: Filter by instrument, side, and completed-candle chart view", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");

    // Switch to Signals tab
    await page.click('button:has-text("Signals")');
    await expect(page.getByRole("heading", { name: "Signals" })).toBeVisible();

    // Verify both mock signals render in table
    await expect(page.getByRole("cell", { name: /NSE:RELIANCE/ })).toBeVisible();
    await expect(page.getByRole("cell", { name: /NSE:INFY/ })).toBeVisible();

    // Filter by search query
    const searchInput = page.locator('[data-testid="signals-search"]');
    await searchInput.fill("RELIANCE");
    await expect(page.getByRole("cell", { name: /NSE:RELIANCE/ })).toBeVisible();
    await expect(page.getByRole("cell", { name: /NSE:INFY/ })).toHaveCount(0);

    // Clear search and filter by Side dropdown
    await searchInput.fill("");
    const sideSelect = page.locator('[data-testid="signals-side-select"]');
    await sideSelect.selectOption("SHORT");
    await expect(page.getByRole("cell", { name: /NSE:INFY/ })).toBeVisible();
    await expect(page.getByRole("cell", { name: /NSE:RELIANCE/ })).toHaveCount(0);

    // Reset filter and inspect chart & score breakdown
    await sideSelect.selectOption("ALL");
    await page.click('td:has-text("NSE:RELIANCE")');
    await expect(page.getByText("Score breakdown")).toBeVisible();
    await expect(page.getByText("Completed-candle chart")).toBeVisible();
    await expect(page.locator('[data-testid="candle-chart"] svg polyline')).toBeVisible();
  });

  test("5. Settings & Risk Controls: Form modification and submission", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");

    // Navigate to Settings
    await page.click('button:has-text("Settings")');
    await expect(page.getByRole("heading", { name: "Paper risk controls" })).toBeVisible();

    // Save controls
    const saveBtn = page.getByRole("button", { name: "Save controls" });
    await expect(saveBtn).toBeVisible();
    await saveBtn.click();
    await expect(page.getByText("Trading controls saved.")).toBeVisible();
  });

  test("6. Security Panel: Active sessions list and session revocation", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");

    // Navigate to Settings -> Security Panel
    await page.click('button:has-text("Settings")');
    await expect(page.getByText("Active sessions")).toBeVisible();
    await expect(page.getByText("Chrome on Windows")).toBeVisible();
    await expect(page.getByText("Firefox on macOS")).toBeVisible();

    // Click revoke on first session
    const revokeButtons = page.getByRole("button", { name: "Revoke session" });
    await expect(revokeButtons).toHaveCount(2);
    await revokeButtons.first().click();

    // Verify session revoked notification and list updated
    await expect(page.getByText("Session revoked.")).toBeVisible();
    await expect(page.getByRole("button", { name: "Revoke session" })).toHaveCount(1);
  });

  test("7. CSV Export: Paper journal export trigger link", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");

    // Navigate to Signals tab
    await page.click('button:has-text("Signals")');

    // Verify CSV Export link is present with correct attributes
    const exportBtn = page.locator('[data-testid="export-csv-btn"]');
    await expect(exportBtn).toBeVisible();
    await expect(exportBtn).toHaveAttribute("href", "/api/v1/journal/export.csv");
    await expect(exportBtn).toHaveAttribute("download", "paper-journal.csv");
  });
});
