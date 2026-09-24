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
  maximum_daily_trades: 2,
  minimum_score: 90,
  minimum_rr: 1.5,
  volume_multiplier: 1.3,
  retest_tolerance_percent: 0.15,
  minimum_ema_spread_percent: 0.05,
  trade_start_time: "09:24",
  trade_cutoff_time: "14:45",
};

// Shaped from a real /settings/trading/catalog response. The settings screen
// renders entirely from this, so a mock that drifted from the API would test a
// form nobody ships. Trimmed to one control of each kind.
const MOCK_CATALOG = {
  group_order: ["ACCOUNT_AND_BROKER", "DAILY_RISK", "TRADING_SESSION"],
  group_labels: {
    ACCOUNT_AND_BROKER: "Account and broker",
    DAILY_RISK: "Daily risk",
    TRADING_SESSION: "Trading session",
  },
  settings: [
    {
      key: "intraday_leverage_enabled",
      group: "ACCOUNT_AND_BROKER",
      group_label: "Account and broker",
      label: "Use intraday leverage",
      help: "When off, exposure is capped at the capital above. When on, the exposure ceiling is multiplied.",
      unit: "BOOLEAN",
      kind: "boolean",
      choices: [],
      is_ceiling: false,
      effect: "NEXT_SIGNAL",
      effect_label: "Applies to the next signal evaluated; signals already taken keep the old value.",
      value: true,
      minimum: null,
      maximum: null,
      exclusive_minimum: null,
      exclusive_maximum: null,
      last_changed_at: null,
    },
    {
      key: "live_broker",
      group: "ACCOUNT_AND_BROKER",
      group_label: "Account and broker",
      label: "Live broker",
      help: "Where a live order would be sent. NONE sends nothing and is a refusal, not a fallback.",
      unit: "CHOICE",
      kind: "choice",
      choices: ["NONE", "UPSTOX", "FIRSTOCK"],
      is_ceiling: false,
      effect: "IMMEDIATE",
      effect_label: "Applies at once, including to the session already running.",
      value: "NONE",
      minimum: null,
      maximum: null,
      exclusive_minimum: null,
      exclusive_maximum: null,
      last_changed_at: null,
    },
    {
      key: "maximum_daily_trades",
      group: "DAILY_RISK",
      group_label: "Daily risk",
      label: "Maximum trades per day",
      help: "Account-wide filled entries, across every strategy and both brokers, counted on first fill.",
      unit: "COUNT",
      kind: "integer",
      choices: [],
      is_ceiling: true,
      effect: "IMMEDIATE",
      effect_label: "Applies at once, including to the session already running.",
      value: 4,
      minimum: 1,
      maximum: 20,
      exclusive_minimum: null,
      exclusive_maximum: null,
      last_changed_at: "2026-09-24T04:00:00+00:00",
    },
    {
      key: "daily_loss_limit",
      group: "DAILY_RISK",
      group_label: "Daily risk",
      label: "Daily loss stop",
      help: "In rupees, not a percent. Reaching it closes the day and exits open positions.",
      unit: "INR",
      kind: "number",
      choices: [],
      is_ceiling: true,
      effect: "IMMEDIATE",
      effect_label: "Applies at once, including to the session already running.",
      value: 400,
      minimum: 0,
      maximum: 10000000,
      exclusive_minimum: null,
      exclusive_maximum: null,
      last_changed_at: null,
    },
    {
      key: "trade_start_time",
      group: "TRADING_SESSION",
      group_label: "Trading session",
      label: "Trade start time",
      help: "IST. No entry is taken before this, so set it after the opening range completes.",
      unit: "TIME_IST",
      kind: "time",
      choices: [],
      is_ceiling: false,
      effect: "NEXT_SESSION",
      effect_label: "Applies from the next trading session.",
      value: "09:24",
      minimum: null,
      maximum: null,
      exclusive_minimum: null,
      exclusive_maximum: null,
      last_changed_at: null,
    },
  ],
  effective: {
    capital: "10000",
    planned_risk_per_trade: "100.00",
    daily_risk_budget: "200.00",
    trades_the_budget_allows: 2,
    configured_trade_ceiling: 4,
    effective_trade_ceiling: 2,
    binding_control: "maximum_daily_risk_percent",
    maximum_open_positions: 1,
    exposure_ceiling: "50000.00",
    leverage_multiplier: "5.0",
    daily_loss_limit: "400.0",
    daily_loss_percent: "4.00",
    daily_profit_target: "2000.0",
    daily_profit_percent: "20.00",
    warnings: [
      "The daily risk budget of ₹200.00 funds 2 trade(s) at ₹100.00 of planned risk each, so only 2 of the 4 configured trades can be taken.",
    ],
  },
};

const MOCK_PRESETS = [
  {
    key: "CAUTIOUS_PAPER_START",
    label: "Cautious paper start",
    description: "₹100 planned risk a trade, ₹400 daily loss stop. Where to begin.",
    controls: {},
    effective: { ...MOCK_CATALOG.effective, planned_risk_per_trade: "100.00", daily_loss_limit: "400.0", effective_trade_ceiling: 4 },
  },
];

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
  rs_threshold_percent: null,
  max_trades_per_side: null,
  exit_rules: {
    stop_rule: "WIDEST_OF_STRUCTURE_ATR_PERCENT",
    stop_atr_multiple: null,
    min_stop_distance_percent: null,
    target_rule: "RR_MULTIPLE",
    target_rr: null,
    target_atr_multiple: 2.0,
    trailing_rule: "NONE",
    trailing_trigger_r: 1.0,
    trailing_atr_multiple: 2.0,
    time_exit_minutes: null,
    square_off_time: null,
  },
}];

const MOCK_STRATEGY_DETAIL = {
  configuration: MOCK_STRATEGIES[0],
  strategy_name: "Opening Range Breakout Retest",
  prerequisites: ["completed candle", "opening range", "VWAP"],
  purpose: "Trades the first genuine break of the opening range, but only after price comes back.",
  regime: "Wants a directional morning with real volume.",
  entry: "Price breaks the opening range, returns to the broken level, and closes back in the direction.",
  does_not: "It does not predict the direction of the day.",
  required_inputs: ["atr", "ema", "rvol"],
  exit_plan: [
    "Stop: the widest of the structural level, 1.5× ATR (the account's multiple), and 0.4% of the entry price (the account's floor).",
    "Target: 1.5× the risk taken (the strategy's minimum reward:risk).",
    "Trailing: none. The stop stays where it was placed.",
    "Time exit: none. The position is held until the stop or target is hit, or the day's limit flattens it. Nothing closes it because the session is ending.",
  ],
  limits: {
    max_trades_per_day: 2,
    max_trades_per_side: null,
    cooldown_minutes: 5,
    allowed_sides: ["LONG", "SHORT"],
    allowed_sessions: ["REGULAR"],
    universe_size: 2,
    minimum_score: 90,
    minimum_rr: 1.5,
  },
  signals_last_30_days: 4,
  last_signal_on: "2026-09-22",
  backtest: {
    source: "BACKTEST",
    trades: 12,
    wins: 5,
    losses: 7,
    win_rate_percent: "41.67",
    net_pnl: "-320.00",
    gross_pnl: "-100.00",
    charges: "220.00",
    average_r: "-0.21",
    from_date: "2026-08-01",
    to_date: "2026-08-31",
    out_of_sample: false,
    sufficient: false,
    shortfall: 18,
  },
  forward: {
    source: "PAPER_FORWARD",
    trades: 6,
    wins: 2,
    losses: 4,
    win_rate_percent: "33.33",
    net_pnl: "-180.00",
    gross_pnl: "-40.00",
    charges: "140.00",
    average_r: null,
    from_date: "2026-09-10",
    to_date: "2026-09-22",
    out_of_sample: true,
    sufficient: false,
    shortfall: 24,
  },
  verdict: "NEGATIVE",
  verdict_label: "Losing money",
  verdict_headline: "Losing money forward: ₹-180.00 net over 6 trades.",
  verdict_caveats: [
    "The backtest evidence is in-sample: the parameters were chosen knowing how this period turned out.",
    "Backtest needs 18 more resolved trades to reach 30.",
    "Paper-forward needs 24 more resolved trades to reach 30.",
  ],
  version_history: [
    { at: "2026-09-20T05:00:00Z", version: 2, changed_keys: ["minimum_score"], risk_increased: [] },
  ],
  recent_signals: [
    {
      id: "sig-detail-1",
      session_date: "2026-09-22",
      instrument_token: "NSE:RELIANCE",
      side: "LONG",
      status: "PAPER_RECORDED",
      score: 92,
      entry_price: "2850.0000",
      stop_price: "2835.0000",
      target_price: "2880.0000",
      created_at: "2026-09-22T04:30:00Z",
    },
  ],
};

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

export async function setupMockRoutes(page: Page, userRole: "ADMIN" | "VIEWER" = "ADMIN") {
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

  await page.route("**/api/v1/settings/trading/catalog", async (route: Route) => {
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(MOCK_CATALOG) });
  });

  await page.route("**/api/v1/settings/trading/presets", async (route: Route) => {
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(MOCK_PRESETS) });
  });

  await page.route("**/api/v1/settings/trading", async (route: Route) => {
    if (route.request().method() === "PUT") {
      const body = route.request().postDataJSON();
      await route.fulfill({ json: body });
    } else {
      await route.fulfill({ json: MOCK_CONTROLS });
    }
  });

  await page.route("**/api/v1/settings/strategies/*/detail", async (route: Route) => {
    await route.fulfill({ json: MOCK_STRATEGY_DETAIL });
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

  await page.route("**/api/v1/history/overview*", async (route: Route) => {
    await route.fulfill({ json: MOCK_HISTORY_OVERVIEW });
  });
  await page.route("**/api/v1/history/daily*", async (route: Route) => {
    await route.fulfill({ json: MOCK_HISTORY_DAYS });
  });
  await page.route("**/api/v1/history/trades/*", async (route: Route) => {
    await route.fulfill({ json: MOCK_HISTORY_TRADE_DETAIL });
  });
  await page.route("**/api/v1/history/trades*", async (route: Route) => {
    await route.fulfill({ json: MOCK_HISTORY_TRADES });
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


const MOCK_HISTORY_OVERVIEW = {
  from_date: "2026-08-25",
  to_date: "2026-09-24",
  trading_days: 2,
  trades: 3,
  open_trades: 0,
  wins: 2,
  losses: 1,
  scratches: 0,
  win_rate_percent: "66.67",
  gross_pnl: "800.00",
  charges: "115.00",
  net_pnl: "685.00",
  best_day: "460.00",
  worst_day: "225.00",
  largest_win: "460.00",
  largest_loss: "-235.00",
  average_win: "460.00",
  average_loss: "-235.00",
  profit_factor: "3.91",
  expectancy: "228.33",
  charges_as_percent_of_gross: "14.38",
  halted_days: 1,
  live_trades: 1,
  reconciliation_counts: { ESTIMATED_CHARGES: 1, MISMATCH: 1 },
  reconciliation_labels: {
    MATCHED: "Matched",
    ESTIMATED_CHARGES: "Estimated charges",
    BROKER_DATA_PENDING: "Broker data pending",
    MISMATCH: "Mismatch",
  },
};

const MOCK_HISTORY_DAYS = [
  {
    session_date: "2026-09-24",
    trades: 2,
    open_trades: 0,
    wins: 1,
    losses: 1,
    scratches: 0,
    win_rate_percent: "50.00",
    gross_pnl: "300.00",
    charges: "75.00",
    net_pnl: "225.00",
    unrealized_pnl: "0.00",
    best_trade: "460.00",
    worst_trade: "-235.00",
    live_trades: 1,
    halt_reason: "PAPER: DAILY_PROFIT_TARGET at ₹2040",
    reconciliation: "MISMATCH",
    reconciliation_label: "Mismatch",
    reconciliation_note: "UPSTOX reports ₹380 realised against our ₹300.",
    broker: "UPSTOX",
    broker_realized_pnl: "380.00",
    broker_charges: "75.00",
    broker_fetched_at: new Date().toISOString(),
  },
  {
    session_date: "2026-09-23",
    trades: 1,
    open_trades: 0,
    wins: 1,
    losses: 0,
    scratches: 0,
    win_rate_percent: "100.00",
    gross_pnl: "500.00",
    charges: "40.00",
    net_pnl: "460.00",
    unrealized_pnl: "0.00",
    best_trade: "460.00",
    worst_trade: "460.00",
    live_trades: 0,
    halt_reason: null,
    reconciliation: "ESTIMATED_CHARGES",
    reconciliation_label: "Estimated charges",
    reconciliation_note: "Paper session.",
    broker: null,
    broker_realized_pnl: null,
    broker_charges: null,
    broker_fetched_at: null,
  },
];

const MOCK_HISTORY_TRADES = [
  {
    position_id: "pos-001",
    signal_id: "sig-001",
    session_date: "2026-09-24",
    instrument_token: "NSE_EQ|INE002A01018",
    script_name: "RELIANCE",
    side: "LONG",
    strategy_version: "orb-retest-v1@3",
    status: "CLOSED",
    execution_mode: "LIVE",
    is_open: false,
    quantity: 10,
    open_quantity: 0,
    entry_price: "100.0000",
    exit_price: "150.0000",
    stop_price: "98.0000",
    target_price: "106.0000",
    opened_at: "2026-09-24T04:05:00Z",
    closed_at: "2026-09-24T06:00:00Z",
    gross_pnl: "500.00",
    charges: "40.00",
    net_pnl: "460.00",
    unrealized_pnl: "0.00",
    risk_amount: "100.00",
    r_multiple: "4.60",
    reconciliation: "MISMATCH",
    reconciliation_label: "Mismatch",
    reconciliation_note: "UPSTOX reports ₹380 realised against our ₹500.",
  },
  {
    position_id: "pos-002",
    signal_id: "sig-002",
    session_date: "2026-09-24",
    instrument_token: "NSE_EQ|INE467B01029",
    script_name: "TCS",
    side: "LONG",
    strategy_version: "vwap-pullback-v1@1",
    status: "CLOSED",
    execution_mode: "PAPER",
    is_open: false,
    quantity: 5,
    open_quantity: 0,
    entry_price: "200.0000",
    exit_price: "160.0000",
    stop_price: "196.0000",
    target_price: "212.0000",
    opened_at: "2026-09-24T05:05:00Z",
    closed_at: "2026-09-24T07:00:00Z",
    gross_pnl: "-200.00",
    charges: "35.00",
    net_pnl: "-235.00",
    unrealized_pnl: "0.00",
    risk_amount: "100.00",
    r_multiple: "-2.35",
    reconciliation: "ESTIMATED_CHARGES",
    reconciliation_label: "Estimated charges",
    reconciliation_note: "Simulated. Charges are this system's estimate from the published rate card.",
  },
];

const MOCK_HISTORY_TRADE_DETAIL = {
  trade: MOCK_HISTORY_TRADES[0],
  orders: [
    {
      order_id: "ord-001",
      client_order_id: "coid-001",
      order_role: "ENTRY",
      order_type: "LIMIT",
      side: "BUY",
      status: "FILLED",
      quantity: 10,
      filled_quantity: 10,
      average_fill_price: "100.0000",
      limit_price: "100.0000",
      stop_price: null,
      fee_total: "20.00",
      rejection_reason: null,
      created_at: "2026-09-24T04:05:00Z",
    },
  ],
  fills: [
    {
      fill_id: "fill-001",
      order_id: "ord-001",
      side: "BUY",
      quantity: 10,
      price: "100.0000",
      gross_value: "1000.00",
      slippage_amount: "0.50",
      brokerage: "15.00",
      stt: "1.00",
      exchange_charge: "0.50",
      gst: "3.00",
      sebi_charge: "0.10",
      stamp_duty: "0.40",
      total_fees: "20.00",
      occurred_at: "2026-09-24T04:06:00Z",
    },
  ],
};

/**
 * Navigate the restructured shell.
 *
 * Primary workspaces are in the sidebar; the views that used to be their own
 * menu entries are now tabs inside them, and everything diagnostic lives under
 * a collapsed "Admin & diagnostics" section. Tests say where they are going in
 * those terms rather than clicking a label and hoping it is still top-level.
 */
const ADMIN_WORKSPACES = new Set([
  "Backtesting",
  "OMS",
  "Shadow comparison",
  "Live readiness",
  "Upstox console",
  "Firstock console",
  "Scheduler",
  "System health",
  "Audit log",
]);

async function go(page: Page, workspace: string, tab?: string) {
  if (ADMIN_WORKSPACES.has(workspace)) {
    await page.getByRole("button", { name: "Admin & diagnostics" }).click();
  }
  await page.getByRole("button", { name: workspace, exact: true }).click();
  if (tab) await page.getByRole("tab", { name: tab, exact: true }).click();
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
    await go(page, "Settings");

    // A viewer sees the settings and can change none of them.
    await expect(page.locator('input[type="number"]').first()).toBeDisabled();
    await expect(page.getByLabel("Live broker")).toBeDisabled();
    await expect(page.getByLabel("Use intraday leverage")).toBeDisabled();
    // No save, no discard, and no way to apply a risk profile in one click.
    await expect(page.getByRole("button", { name: /^Save/ })).toHaveCount(0);
    await expect(page.getByRole("button", { name: /^Apply/ })).toHaveCount(0);
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
    await go(page, "Scanner & Universe", "Signals");
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

    await go(page, "Scanner & Universe", "Scanner");
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
    await go(page, "Settings");
    await expect(page.getByRole("heading", { name: "Trading controls" })).toBeVisible();

    // What the settings actually permit, shown above the inputs that set them.
    await expect(page.getByRole("heading", { name: "Effective limits" })).toBeVisible();
    // The contradiction is surfaced rather than left to be discovered from an
    // empty session: four configured, two reachable.
    await expect(page.getByText("of 4 configured")).toBeVisible();
    await expect(page.getByText(/only 2 of the 4 configured trades/)).toBeVisible();

    // Exposure is labelled as exposure, never as cash.
    await expect(page.getByText(/exposure, not cash/)).toBeVisible();

    // A stop is an instruction, not a promise.
    await expect(page.getByText(/not a guarantee/)).toBeVisible();

    // Every control carries its unit, its range, when it takes effect, and
    // when it last moved — the things the old raw-input grid never said.
    await expect(page.getByText("Allowed: 1 to 20")).toBeVisible();
    await expect(page.getByText(/Applies at once, including to the session already running/).first()).toBeVisible();
    await expect(page.getByText(/Last changed/)).toBeVisible();
    await expect(page.getByText("Not changed here yet").first()).toBeVisible();

    // No broker chosen is a refusal, not a fallback to whichever one exists.
    await expect(page.getByLabel("Live broker")).toHaveValue("NONE");

    // Nothing to save until something is edited.
    await expect(page.getByText("No changes to save.")).toBeVisible();

    // Editing one control offers to save exactly that one.
    await page.getByLabel("Maximum trades per day").fill("3");
    const saveBtn = page.getByRole("button", { name: "Save 1 change" });
    await expect(saveBtn).toBeEnabled();
    await saveBtn.click();
    await expect(page.getByText("Saved 1 change.")).toBeVisible();
  });

  test("5b. Strategy workspace: versioned glass panel and strategy metrics", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");

    await go(page, "Strategies");
    await expect(page.getByRole("heading", { name: "Strategies" })).toBeVisible();
    await expect(page.getByText("ORB Retest — Default · v1")).toBeVisible();
    await expect(page.getByText("25%")).toBeVisible();
    await expect(page.getByLabel("Strategy name ORB Retest — Default")).toBeVisible();
    await expect(page.getByLabel(/Universe/)).toHaveValue("NSE:RELIANCE, NSE:INFY");
  });

  test("5c. Paper orderbook: simulated lifecycle and positions are clearly labeled", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");

    await go(page, "Orders & Positions", "Orders");
    await expect(page.getByRole("heading", { name: "Paper orderbook" })).toBeVisible();
    await expect(page.getByText("Simulated orderbook")).toBeVisible();
    await expect(page.getByText("NSE:RELIANCE", { exact: true })).toBeVisible();
    await expect(page.getByText("₹110.25")).toBeVisible();

    await go(page, "Orders & Positions", "Positions");
    await expect(page.getByRole("heading", { name: "Paper positions", exact: true })).toBeVisible();
    await expect(page.getByText("Signal-linked paper positions")).toBeVisible();
    await expect(page.getByText("32/32")).toBeVisible();
  });

  test("5d. Risk center: reservation capacity and exposure are visible", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");

    await go(page, "Risk");
    await expect(page.getByRole("heading", { name: "Risk", exact: true })).toBeVisible();
    await expect(page.getByText(/Reservation capacity gates every simulated entry/)).toBeVisible();
    await expect(page.getByText("₹500 / ₹1,000")).toBeVisible();
    await expect(page.getByText("1/3")).toBeVisible();
    await expect(page.getByText("₹8,768")).toBeVisible();
  });

  test("5e. Backtesting lab: persisted completed-candle research is visible", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");

    await go(page, "Backtesting");
    await expect(page.getByRole("heading", { name: "Backtesting lab", exact: true })).toBeVisible();
    await expect(page.getByText("Historical replay uses only completed candles")).toBeVisible();
    await expect(page.getByText("₹1,250").first()).toBeVisible();
    // Scoped to the strategy-comparison row: the same name also appears as an
    // option in the sweep form, and the point of this assertion is the results table.
    await expect(page.getByRole("cell", { name: "ORB Retest — Default v1" })).toBeVisible();
  });

  test("5g. Live gates: readiness inspection preserves the hard execution lock", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");

    await go(page, "Live readiness");
    await expect(page.getByRole("heading", { name: "Live readiness gates", exact: true })).toBeVisible();
    await expect(page.getByText("Live execution hard lock active.")).toBeVisible();
    await expect(page.getByText("No broker submission adapter is implemented.")).toBeVisible();
    await page.getByRole("button", { name: "Record review" }).click();
    await expect(page.getByText("The live execution lock remains active.")).toBeVisible();
  });

  test("5h. System health: durable startup reconciliation is observable", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");

    await go(page, "System health");
    await expect(page.getByRole("heading", { name: "System health", exact: true })).toBeVisible();
    await expect(page.getByText("Startup reconciliation")).toBeVisible();
    await expect(page.getByText("internal links are consistent.")).toBeVisible();
  });

  test("6. Security Panel: Active sessions list and session revocation", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");

    await go(page, "Settings", "Sessions");
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
    await go(page, "Scanner & Universe", "Signals");

    // Verify CSV Export link is present with correct attributes
    const exportBtn = page.locator('[data-testid="export-csv-btn"]');
    await expect(exportBtn).toBeVisible();
    await expect(exportBtn).toHaveAttribute("href", "/api/v1/journal/export.csv");
    await expect(exportBtn).toHaveAttribute("download", "paper-journal.csv");
  });
  test("8. History: gross, charges and net stay three separate figures", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");
    await go(page, "History");

    await expect(page.getByRole("heading", { name: "History" })).toBeVisible();
    // The three must never collapse into one "P&L". A screen that showed only
    // ₹800 would be reporting money that cannot be withdrawn.
    // Matched on the tile's own label, not on the text anywhere inside it: the
    // Net tile carries the note "after charges" and would answer for Charges.
    const tile = (label: string) => page.locator(".glass-inset").filter({ has: page.getByText(label, { exact: true }) });
    await expect(tile("Net P&L")).toContainText("₹685.00");
    await expect(tile("Gross P&L")).toContainText("₹800.00");
    await expect(tile("Charges")).toContainText("₹115.00");
  });

  test("8b. History: a broker figure is shown beside ours, never instead of it", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");
    await go(page, "History");

    const day = page.locator("tr", { hasText: "2026-09-24" }).first();
    await expect(day).toContainText("₹225.00");
    await expect(day).toContainText("UPSTOX: +₹380.00 realised");
    await expect(day.getByText("Mismatch")).toBeVisible();
  });

  test("8c. History: a paper day reads as estimated charges, not as a fault", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");
    await go(page, "History");

    const day = page.locator("tr", { hasText: "2026-09-23" }).first();
    await expect(day.getByText("Estimated charges")).toBeVisible();
  });

  test("8d. History: opening a trade shows the itemised cost of every fill", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");
    await go(page, "History");
    await page.getByRole("button", { name: /^Trades \(/ }).click();

    await page.locator("tr", { hasText: "RELIANCE" }).first().click();
    await expect(page.getByRole("heading", { name: /RELIANCE LONG/ })).toBeVisible();
    // One "charges" total cannot answer "which line is wrong"; these can.
    await expect(page.getByRole("columnheader", { name: "Brokerage" })).toBeVisible();
    await expect(page.getByRole("columnheader", { name: "STT" })).toBeVisible();
    await expect(page.getByRole("columnheader", { name: "Stamp duty" })).toBeVisible();
  });

  test("8e. History: both exports carry the chosen range", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");
    await go(page, "History");

    const csv = page.getByRole("link", { name: "CSV" });
    const excel = page.getByRole("link", { name: "Excel" });
    await expect(csv).toHaveAttribute("href", /\/api\/v1\/history\/export\.csv\?from_date=\d{4}-\d{2}-\d{2}&to_date=\d{4}-\d{2}-\d{2}/);
    await expect(excel).toHaveAttribute("href", /\/api\/v1\/history\/export\.xlsx\?from_date=/);
  });
  test("9. Navigation: seven places to work, and nothing that renders \"unavailable\"", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");

    // Seven, exactly. The count is asserted rather than just the labels,
    // because the failure this guards against is an eighth entry creeping back.
    const primary = page.locator("aside .space-y-1").first().getByRole("button");
    await expect(primary).toHaveText([
      "Dashboard",
      "Strategies",
      "Scanner & Universe",
      "Orders & Positions",
      "History",
      "Risk",
      "Settings",
    ]);

    // The three placeholders that rendered a "planned workspace" page are gone,
    // as are the two entries that were second copies of a screen.
    for (const gone of ["Performance", "Automation Rules", "Users", "Assisted Trading", "Telegram", "Journal"]) {
      await expect(page.getByRole("button", { name: gone, exact: true })).toHaveCount(0);
    }
    await expect(page.getByText("Soon")).toHaveCount(0);
  });

  test("9b. Navigation: diagnostics are collapsed until asked for", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");

    await expect(page.getByRole("button", { name: "System health", exact: true })).toHaveCount(0);
    await page.getByRole("button", { name: "Admin & diagnostics" }).click();
    await expect(page.getByRole("button", { name: "System health", exact: true })).toBeVisible();
  });

  test("9c. Navigation: a tab changes the view without leaving the workspace", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");

    await go(page, "Orders & Positions");
    await expect(page.getByRole("tab", { name: "Orders" })).toHaveAttribute("aria-selected", "true");
    await page.getByRole("tab", { name: "Positions" }).click();
    await expect(page.getByRole("tab", { name: "Positions" })).toHaveAttribute("aria-selected", "true");
    // Still in the same workspace; the tabs did not navigate away.
    await expect(page.getByRole("tab", { name: "Orders" })).toBeVisible();
  });

  test("9d. Navigation: leaving a workspace and returning lands on its first tab", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");

    await go(page, "Orders & Positions", "Positions");
    await go(page, "Risk");
    await go(page, "Orders & Positions");
    await expect(page.getByRole("tab", { name: "Orders" })).toHaveAttribute("aria-selected", "true");
  });

  test("9e. Settings: alerts moved in from their own menu entry", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");

    await go(page, "Settings", "Alerts");
    await expect(page.getByRole("heading", { name: "Alerts", exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "Send test alert" })).toBeVisible();
  });

  test("9f. Risk: the live lock is stated accurately, and still cannot be switched off here", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");

    await go(page, "Risk");
    // The old copy claimed the live gates did not exist. They do; the lock is
    // what holds execution shut, and no screen may offer to lift it.
    await expect(page.getByText(/readiness, activation, approval and reconciliation gates are built/)).toBeVisible();
    await expect(page.getByText("LIVE_TRADING_ENABLED")).toBeVisible();
    await expect(page.getByRole("button", { name: "Enable live trading" })).toBeDisabled();
    await expect(page.getByRole("button", { name: "Emergency stop", exact: true })).toBeVisible();
  });
  test("10. Strategy detail: says what it is for and how the trade is left", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");
    await go(page, "Strategies");
    await page.getByRole("button", { name: "Details" }).first().click();

    await expect(page.getByRole("heading", { name: "ORB Retest — Default" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Exit plan" })).toBeVisible();
    await expect(page.getByText(/Nothing closes it because the session is ending/)).toBeVisible();
    await expect(page.getByRole("heading", { name: "Required inputs" })).toBeVisible();
  });

  test("10b. Strategy detail: never calls a strategy profitable, and names what is missing", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");
    await go(page, "Strategies");
    await page.getByRole("button", { name: "Details" }).first().click();

    // The verdict is the part of this screen that could do harm. It reports
    // what the evidence supports and what is absent, and the word "profitable"
    // appears nowhere.
    await expect(page.getByText("Losing money", { exact: true })).toBeVisible();
    await expect(page.getByText(/Backtest needs 18 more resolved trades/)).toBeVisible();
    await expect(page.getByText(/in-sample/).first()).toBeVisible();
    await expect(page.getByText(/profitable/i)).toHaveCount(0);
  });

  test("10c. Strategy detail: both bodies of evidence are shown, net of charges", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");
    await go(page, "Strategies");
    await page.getByRole("button", { name: "Details" }).first().click();

    const backtest = page.locator(".glass-inset").filter({ hasText: "Backtest" }).first();
    await expect(backtest).toContainText("−₹320.00");
    await expect(backtest).toContainText("less ₹220.00 charges");
    const forward = page.locator(".glass-inset").filter({ hasText: "Paper forward" }).first();
    await expect(forward).toContainText("−₹180.00");
  });

  test("10d. Strategies: the exit rules are editable, and the missing square-off is flagged", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");
    await go(page, "Strategies");

    // No trading or strategy setting should need a Python file or a .env edit.
    await expect(page.getByLabel("Trailing")).toBeVisible();
    await expect(page.getByLabel(/Square off at/)).toBeVisible();
    await expect(page.getByText(/Nothing closes this strategy.s positions when the session ends/)).toBeVisible();

    await page.getByLabel("Trailing").selectOption("BREAKEVEN_AT_R");
    await expect(page.getByLabel("Move at (R ahead)")).toBeVisible();
  });
});
