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

const MOCK_MARKET_SESSION = {
  phase: "regular",
  trading_day: true,
  reason: "Regular NSE market session",
  local_timestamp: new Date().toISOString(),
  session_date: "2026-08-31",
  regular_open: "09:15",
  regular_close: "15:30",
  is_special_session: false,
};

const MOCK_DATA_QUALITY = [{
  instrument_token: "NSE:RELIANCE",
  state: "GOOD",
  reason: "Completed-candle feed is current",
  session_date: "2026-08-31",
  expected_bars: 60,
  received_bars: 60,
  missing_buckets: [],
  received_ticks: 1300,
  duplicate_ticks: 0,
  out_of_order_ticks: 0,
  invalid_ticks: 0,
  average_latency_ms: 42,
  max_latency_ms: 96,
  last_exchange_timestamp: new Date().toISOString(),
  last_received_timestamp: new Date().toISOString(),
  observed_at: new Date().toISOString(),
  allows_signals: true,
}];

const MOCK_EVALUATIONS = [{
  id: "eval-001",
  instrument_token: "NSE:RELIANCE",
  session_date: "2026-08-31",
  candle_opened_at: new Date().toISOString(),
  strategy_id: "orb-default",
  strategy_name: "ORB Retest — Default",
  strategy_version: 1,
  status: "ACCEPTED",
  decision_state: "SIGNALLED",
  side: "LONG",
  reason: "Paper signal confirmed",
  failed_conditions: [],
  data_quality_state: "GOOD",
  candle_close: 2850.5,
  candle_volume: 1500,
  score: 95,
  score_breakdown: { breakout_retest: 20, vwap_alignment: 20, ema_alignment: 20, volume_confirmation: 20, market_confirmation: 15 },
  indicator_snapshot: { vwap: 2848.2, ema_fast: 2850.1, ema_slow: 2844.3, volume: { relative_volume: 1.6 }, relative_strength: { relative_strength_percent: 0.4 }, nifty_regime: { regime: "BULLISH" } },
  entry_price: 2850.5,
  stop_price: 2835,
  target_price: 2880,
  quantity: 32,
  risk_amount: 500,
  created_at: new Date().toISOString(),
}, {
  id: "eval-002",
  instrument_token: "NSE:INFY",
  session_date: "2026-08-31",
  candle_opened_at: new Date().toISOString(),
  strategy_id: "orb-default",
  strategy_name: "ORB Retest — Default",
  strategy_version: 1,
  status: "REJECTED",
  decision_state: "AWAITING_BREAKOUT",
  side: null,
  reason: "EMA spread indicates choppy market",
  failed_conditions: ["EMA spread indicates choppy market"],
  data_quality_state: "DEGRADED",
  candle_close: 1820,
  candle_volume: 900,
  score: 0,
  score_breakdown: {},
  indicator_snapshot: { vwap: 1822, ema_fast: 1820.01, ema_slow: 1820, volume: { relative_volume: 0.9 }, relative_strength: { relative_strength_percent: -0.1 }, nifty_regime: { regime: "NEUTRAL" } },
  entry_price: null,
  stop_price: null,
  target_price: null,
  quantity: null,
  risk_amount: null,
  created_at: new Date().toISOString(),
}];

const MOCK_CONTROLS = {
  account_capital: 100000,
  risk_per_trade_percent: 0.5,
  maximum_daily_risk_percent: 1.0,
  maximum_open_positions: 3,
  maximum_open_exposure_percent: 100,
  maximum_signals: 2,
  minimum_score: 90,
  minimum_rr: 1.5,
  volume_multiplier: 1.3,
  retest_tolerance_percent: 0.15,
  minimum_ema_spread_percent: 0.05,
  trade_start_time: "09:24",
  trade_cutoff_time: "14:45",
};

const MOCK_STRATEGIES = [{
  id: "orb-default",
  name: "ORB Retest — Default",
  enabled: true,
  strategy_type: "orb-retest-v1",
  version: 1,
  universe: ["NSE:RELIANCE", "NSE:INFY"],
  allowed_sides: ["LONG", "SHORT"],
  allowed_sessions: ["REGULAR"],
  max_trades_per_day: 2,
  cooldown_minutes: 5,
  risk_per_trade_percent: 0.5,
  minimum_score: 90,
  minimum_rr: 1.5,
  volume_multiplier: 1.3,
  retest_tolerance_percent: 0.15,
  minimum_ema_spread_percent: 0.05,
}];

const MOCK_STRATEGY_METRICS = [{
  strategy_id: "orb-default",
  strategy_name: "ORB Retest — Default",
  strategy_version: 1,
  evaluations: 12,
  accepted: 3,
  rejected: 7,
  watching: 2,
  acceptance_rate: 25,
}];

const MOCK_PAPER_SUMMARY = { orders: 3, pending_orders: 1, fills: 2, open_positions: 1, realized_pnl: 0, unrealized_pnl: 125.5, total_pnl: 110.25, fees_total: 15.25 };
const MOCK_PAPER_ORDERS = [{ id: "paper-order-1", paper_signal_id: "sig-001", client_order_id: "paper:sig-001:entry", instrument_token: "NSE:RELIANCE", session_date: "2026-08-31", side: "BUY", order_type: "MARKET", order_role: "ENTRY", status: "FILLED", quantity: 32, filled_quantity: 32, average_fill_price: 2851, limit_price: null, stop_price: null, fee_total: 8.2, eligible_after: new Date().toISOString(), rejection_reason: null, created_at: new Date().toISOString() }];
const MOCK_PAPER_POSITIONS = [{ id: "paper-position-1", paper_signal_id: "sig-001", instrument_token: "NSE:RELIANCE", session_date: "2026-08-31", strategy_version: "orb-retest-v1@1", side: "LONG", status: "OPEN", initial_quantity: 32, open_quantity: 32, average_entry_price: 2851, average_exit_price: null, current_price: 2855, stop_price: 2835, target_price: 2880, realized_pnl: 0, unrealized_pnl: 128, fees_total: 15.25, total_pnl: 112.75, opened_at: new Date().toISOString(), closed_at: null }];
const MOCK_RISK_SUMMARY = { session_date: "2026-08-31", daily_risk_limit: 1000, daily_risk_allocated: 500, daily_risk_available: 500, maximum_open_positions: 3, active_reservations: 1, open_positions: 1, exposure_limit: 100000, current_exposure: 91232, exposure_available: 8768, rejected_reservations: 0 };
const MOCK_BACKTESTS = [{ id: "backtest-1", status: "COMPLETED", start_date: "2026-08-24", end_date: "2026-08-31", timeframe_seconds: 60, instrument_tokens: ["NSE:RELIANCE"], source_candle_count: 1875, data_fingerprint: "a1b2c3d4e5f67890", initial_capital: 100000, final_equity: 101250, net_pnl: 1250, max_drawdown: 420, failure_detail: null, created_at: new Date().toISOString(), summary: { trades: 4, winners: 3, losers: 1, win_rate: 75, net_pnl: 1250, profit_factor: 2.5, initial_capital: 100000, final_equity: 101250, return_percent: 1.25, max_drawdown: 420, equity_curve: [{ at: null, equity: 100000, drawdown: 0 }, { at: new Date().toISOString(), equity: 101250, drawdown: 0 }], strategy_comparison: [{ strategy_id: "orb-default", strategy_name: "ORB Retest — Default", strategy_version: 1, trades: 4, winners: 3, losers: 1, win_rate: 75, net_pnl: 1250, profit_factor: 2.5 }] } }];

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
const MOCK_ASSISTED_APPROVALS = [{
  reference_id: "signal-assisted-001",
  decision: "PENDING",
  source: "WEB",
  status: "PENDING",
  expires_at: new Date(Date.now() + 300000).toISOString(),
  decided_at: null,
  risk_revalidated_at: null,
  submission_block_reason: null,
  created_at: new Date().toISOString(),
}];
const MOCK_LIVE_READINESS = {
  status: "HARD_LOCKED",
  overall_ready: false,
  live_execution_available: false,
  checked_at: new Date().toISOString(),
  gates: [
    { key: "runtime_lock", label: "Runtime hard lock", passed: true, detail: "PAPER configuration is asserted." },
    { key: "broker_adapter", label: "Broker execution adapter", passed: false, detail: "No broker submission adapter is implemented." },
  ],
};
const MOCK_OMS_RECONCILIATIONS = [{ id: "recon-001", mode: "PAPER", status: "CLEAN", internal_orders: 3, external_orders: 0, unknown_orders: 0, detail: "Paper OMS has no external broker side; internal links are consistent.", created_at: new Date().toISOString() }];

async function setupMockRoutes(page: Page, userRole: "ADMIN" | "VIEWER" = "ADMIN") {
  const user = userRole === "ADMIN" ? MOCK_ADMIN_USER : MOCK_VIEWER_USER;
  let currentScanner = { status: "STOPPED", last_heartbeat: new Date().toISOString(), detail: "Scanner is paused" };

  await page.route("**/api/v1/auth/me", async (route: Route) => {
    await route.fulfill({ json: user });
  });

  await page.route("**/api/v1/system/overview", async (route: Route) => {
    await route.fulfill({ json: MOCK_OVERVIEW });
  });

  await page.route("**/api/v1/system/market-session", async (route: Route) => {
    await route.fulfill({ json: MOCK_MARKET_SESSION });
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

  await page.route("**/api/v1/scanner/data-quality", async (route: Route) => {
    await route.fulfill({ json: MOCK_DATA_QUALITY });
  });

  await page.route("**/api/v1/scanner/evaluations*", async (route: Route) => {
    await route.fulfill({ json: MOCK_EVALUATIONS });
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

  await page.route("**/api/v1/settings/strategies/metrics", async (route: Route) => {
    await route.fulfill({ json: MOCK_STRATEGY_METRICS });
  });

  await page.route("**/api/v1/settings/strategies", async (route: Route) => {
    if (route.request().method() === "PUT") {
      await route.fulfill({ json: route.request().postDataJSON() });
    } else {
      await route.fulfill({ json: MOCK_STRATEGIES });
    }
  });

  await page.route("**/api/v1/paper/summary", async (route: Route) => { await route.fulfill({ json: MOCK_PAPER_SUMMARY }); });
  await page.route("**/api/v1/paper/orders", async (route: Route) => { await route.fulfill({ json: MOCK_PAPER_ORDERS }); });
  await page.route("**/api/v1/paper/positions", async (route: Route) => { await route.fulfill({ json: MOCK_PAPER_POSITIONS }); });
  await page.route("**/api/v1/risk/summary", async (route: Route) => { await route.fulfill({ json: MOCK_RISK_SUMMARY }); });
  await page.route("**/api/v1/backtests", async (route: Route) => { await route.fulfill({ json: MOCK_BACKTESTS }); });
  await page.route("**/api/v1/oms/reconciliations", async (route: Route) => { await route.fulfill({ json: MOCK_OMS_RECONCILIATIONS }); });
  await page.route(/\/api\/v1\/assisted\/approvals(?:\/.*)?$/, async (route: Route) => {
    if (route.request().method() === "POST") {
      await route.fulfill({ json: { ...MOCK_ASSISTED_APPROVALS[0], decision: "APPROVE", status: "APPROVED_PAPER_ONLY", risk_revalidated_at: new Date().toISOString(), submission_block_reason: "Live broker submission is unavailable in this release" } });
    } else {
      await route.fulfill({ json: MOCK_ASSISTED_APPROVALS });
    }
  });
  await page.route(/\/api\/v1\/live\/readiness(?:\/.*)?$/, async (route: Route) => {
    if (route.request().url().endsWith("/history")) {
      await route.fulfill({ json: [] });
    } else {
      await route.fulfill({ json: MOCK_LIVE_READINESS });
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

  test("4b. Scanner workspace: filters rejected evaluations and opens the setup inspector", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");

    await page.getByRole("button", { name: "Scanner", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Scanner workspace" })).toBeVisible();
    await expect(page.getByText("NSE:RELIANCE", { exact: true }).first()).toBeVisible();

    await page.getByLabel("Evaluation state").selectOption("REJECTED");
    await expect(page.getByText("NSE:INFY", { exact: true }).first()).toBeVisible();
    await expect(page.getByText("NSE:RELIANCE", { exact: true })).toHaveCount(0);
    await page.getByRole("button", { name: "Inspect NSE:INFY" }).click();
    await expect(page.getByText("Failed conditions")).toBeVisible();
    await expect(page.locator("aside ul li").filter({ hasText: "EMA spread indicates choppy market" })).toBeVisible();
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

  test("5b. Strategy workspace: versioned glass panel and strategy metrics", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");

    await page.getByRole("button", { name: "Strategies", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Strategies" })).toBeVisible();
    await expect(page.getByText("ORB Retest — Default · v1")).toBeVisible();
    await expect(page.getByText("25%")).toBeVisible();
    await expect(page.getByLabel("Strategy name ORB Retest — Default")).toBeVisible();
    await expect(page.getByLabel(/Universe/)).toHaveValue("NSE:RELIANCE, NSE:INFY");
  });

  test("5c. Paper orderbook: simulated lifecycle and positions are clearly labeled", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");

    await page.getByRole("button", { name: "Orders", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Paper orderbook" })).toBeVisible();
    await expect(page.getByText("Simulated orderbook")).toBeVisible();
    await expect(page.getByText("NSE:RELIANCE", { exact: true })).toBeVisible();
    await expect(page.getByText("₹110.25")).toBeVisible();

    await page.getByRole("button", { name: "Positions", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Paper positions", exact: true })).toBeVisible();
    await expect(page.getByText("Signal-linked paper positions")).toBeVisible();
    await expect(page.getByText("32/32")).toBeVisible();
  });

  test("5d. Risk center: reservation capacity and exposure are visible", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");

    await page.getByRole("button", { name: "Risk Center", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Risk center", exact: true })).toBeVisible();
    await expect(page.getByText("Verified reservation capacity gates every simulated entry.")).toBeVisible();
    await expect(page.getByText("₹500 / ₹1,000")).toBeVisible();
    await expect(page.getByText("1/3")).toBeVisible();
    await expect(page.getByText("₹8,768")).toBeVisible();
  });

  test("5e. Backtesting lab: persisted completed-candle research is visible", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");

    await page.getByRole("button", { name: "Backtesting", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Backtesting lab", exact: true })).toBeVisible();
    await expect(page.getByText("Historical replay uses only completed candles")).toBeVisible();
    await expect(page.getByText("₹1,250").first()).toBeVisible();
    await expect(page.getByText("ORB Retest — Default", { exact: true })).toBeVisible();
  });

  test("5f. Assisted trading: paper-only approval is visible and cannot imply broker submission", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");

    await page.getByRole("button", { name: "Assisted Trading", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Assisted trading", exact: true })).toBeVisible();
    await expect(page.getByText("Submission boundary active.")).toBeVisible();
    await page.getByRole("button", { name: "Approve signal-assisted-001" }).click();
    await expect(page.getByText("No broker order was submitted.")).toBeVisible();
    await expect(page.getByText("Approved Paper Only")).toBeVisible();
  });

  test("5g. Live gates: readiness inspection preserves the hard execution lock", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");

    await page.getByRole("button", { name: "Live Gates", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Live readiness gates", exact: true })).toBeVisible();
    await expect(page.getByText("Live execution hard lock active.")).toBeVisible();
    await expect(page.getByText("No broker submission adapter is implemented.")).toBeVisible();
    await page.getByRole("button", { name: "Record review" }).click();
    await expect(page.getByText("The live execution lock remains active.")).toBeVisible();
  });

  test("5h. System health: durable startup reconciliation is observable", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");

    await page.getByRole("button", { name: "System Health", exact: true }).click();
    await expect(page.getByRole("heading", { name: "System health", exact: true })).toBeVisible();
    await expect(page.getByText("Startup reconciliation")).toBeVisible();
    await expect(page.getByText("internal links are consistent.")).toBeVisible();
  });

  test("6. Security Panel: Active sessions list and session revocation", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");

    // Navigate to Settings -> Security Panel
    await page.click('button:has-text("Settings")');
    await expect(page.getByRole("heading", { name: "Active sessions", exact: true })).toBeVisible();
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
