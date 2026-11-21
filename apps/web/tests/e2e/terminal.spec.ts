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
  application_mode: "PAPER",
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
      key: "entry_slippage_cap_percent",
      group: "ACCOUNT_AND_BROKER",
      group_label: "Account and broker",
      label: "Entry slippage cap",
      help: "How far past the signal's entry price a LIMIT entry may fill, as a percent. The order is priced at that worst price and sized from it, so a fill anywhere inside the cap risks no more than the trade was budgeted.",
      unit: "PERCENT",
      kind: "decimal",
      choices: [],
      is_ceiling: true,
      effect: "NEXT_SIGNAL",
      effect_label: "Applies to the next signal evaluated; signals already taken keep the old value.",
      value: 0.25,
      minimum: 0,
      maximum: 5,
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
  live_orders: false,
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

const swingSpec = (key: string, group: string, label: string, unit: string, kind: string, value: number, minimum: number, maximum: number) => ({
  key,
  group,
  group_label: group,
  label,
  help: `${label} help`,
  unit,
  kind,
  choices: [],
  is_ceiling: false,
  effect: "NEXT_SESSION",
  effect_label: "Applies from the next evening run; positions already open keep their size and stop.",
  value,
  minimum,
  maximum,
  exclusive_minimum: null,
  exclusive_maximum: null,
  last_changed_at: null,
});

export const MOCK_SWING_SETTINGS = {
  settings: [
    swingSpec("capital", "SWING_CAPITAL", "Swing capital", "INR", "number", 100000, 10000, 10000000),
    swingSpec("risk_per_trade_percent", "SWING_CAPITAL", "Risk a trade", "PERCENT", "number", 0.75, 0, 3),
    swingSpec("heat_cap_percent", "SWING_CAPITAL", "Open-risk cap", "PERCENT", "number", 4, 0, 25),
    swingSpec("max_open_positions", "SWING_POSITIONS", "Open positions", "COUNT", "integer", 5, 1, 20),
  ],
  group_order: ["SWING_CAPITAL", "SWING_POSITIONS"],
  group_labels: { SWING_CAPITAL: "Capital and risk", SWING_POSITIONS: "Positions" },
  effective: { risk_per_trade: 750, heat_cap: 4000, largest_position: 35000, trades_the_cap_allows: 5 },
  schedule: {
    quiet_hours: "9:00 AM - 3:40 PM IST on weekdays",
    plan_after: "6:00 PM IST, once the candles are final",
    entry: "At the next session open",
    results_avoid_days: 5,
    results_exit_days: 3,
  },
  saved: false,
};

export async function setupMockRoutes(page: Page, userRole: "ADMIN" | "VIEWER" = "ADMIN") {
  const user = userRole === "ADMIN" ? MOCK_ADMIN_USER : MOCK_VIEWER_USER;
  let currentScanner = { status: "STOPPED", last_heartbeat: new Date().toISOString(), detail: "Scanner is paused" };

  await page.route("**/api/v1/auth/me", async (route: Route) => {
    await route.fulfill({ json: user });
  });

  // A finished setup by default, so the setup banner stays out of every other test.
  await page.route("**/api/v1/setup/checklist", async (route: Route) => {
    await route.fulfill({ json: [] });
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

  await page.route("**/api/v1/settings/swing", async (route: Route) => {
    await route.fulfill({ json: MOCK_SWING_SETTINGS });
  });

  await page.route("**/api/v1/live-shadow/activation", async (route: Route) => {
    const method = route.request().method();
    if (method === "POST") {
      const body = route.request().postDataJSON();
      await route.fulfill({
        json: {
          id: "act-1",
          armed: true,
          reason: body.reason,
          expires_at: new Date(Date.now() + 8 * 3600_000).toISOString(),
          revoked_at: null,
          revoked_reason: null,
          blocking_gates: [],
        },
      });
    } else if (method === "DELETE") {
      await route.fulfill({
        json: {
          id: "act-1",
          armed: false,
          reason: "",
          expires_at: null,
          revoked_at: new Date().toISOString(),
          revoked_reason: "Revoked",
          blocking_gates: [],
        },
      });
    } else {
      await route.fulfill({
        json: {
          id: null,
          armed: false,
          reason: "",
          expires_at: null,
          revoked_at: null,
          revoked_reason: null,
          blocking_gates: ["external_reconciliation"],
        },
      });
    }
  });

  await page.route("**/api/v1/live-shadow/reconcile", async (route: Route) => {
    await route.fulfill({
      json: {
        id: "rec-1",
        status: "CLEAN",
        safe_to_trade: true,
        internal_orders: 0,
        external_orders: 0,
        unknown_orders: 0,
        detail: "Broker and local state agree.",
        findings: [],
        created_at: new Date().toISOString(),
      },
    });
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

  await page.route("**/api/v1/history/broker-figures/*", async (route: Route) => {
    await route.fulfill({
      json: {
        session_date: "2026-09-24",
        broker: "UPSTOX",
        realized_pnl: "380.00",
        charges: "75.00",
        turnover: "12000.00",
        trade_count: 2,
        fetched_at: new Date().toISOString(),
        note: "Recorded beside the local figures, not over them.",
      },
    });
  });
  await page.route("**/api/v1/broker-books/brokers", async (route: Route) => {
    await route.fulfill({ json: MOCK_BROKER_CHOICES });
  });
  await page.route("**/api/v1/broker-books/snapshot*", async (route: Route) => {
    await route.fulfill({ json: MOCK_BROKER_SNAPSHOT });
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

/**
 * The broker's own books. Two rows that matter: one order carrying our client
 * order id, and one working order without it -- the untracked order that blocks
 * reconciliation, which is the single thing an operator most needs this screen
 * to answer.
 */
const MOCK_BROKER_SNAPSHOT = {
  broker: "UPSTOX",
  fetched_at: new Date().toISOString(),
  stale: false,
  readable: true,
  detail: "2 order(s), 1 position row(s) at UPSTOX.",
  orders: [
    {
      broker_order_id: "2610010001",
      client_order_id: "sidra-7f2c",
      status: "COMPLETE",
      symbol: "BHARTIARTL",
      side: "SELL",
      order_type: "MARKET",
      quantity: 67,
      filled_quantity: 67,
      average_price: 183.56,
      placed_at: "2026-10-01T04:07:01Z",
      trigger_price: null,
      status_message: null,
      ours: true,
    },
    {
      broker_order_id: "2610010002",
      client_order_id: null,
      status: "OPEN",
      symbol: "TATASTEEL",
      side: "BUY",
      order_type: "LIMIT",
      quantity: 40,
      filled_quantity: 0,
      average_price: null,
      placed_at: "2026-10-01T04:31:00Z",
      trigger_price: null,
      status_message: null,
      ours: false,
    },
  ],
  positions: [
    {
      symbol: "BHARTIARTL",
      instrument_token: "NSE_EQ|INE397D01024",
      net_quantity: -67,
      average_price: 183.56,
      last_price: 181.02,
      realised: 0,
      unrealised: 170.18,
      day_pnl: 170.18,
    },
  ],
  realised: 0,
  unrealised: 170.18,
  open_positions: 1,
  working_orders: 1,
  untracked_working: 1,
};

const MOCK_BROKER_CHOICES = {
  selected: "UPSTOX",
  brokers: [
    { key: "UPSTOX", label: "Upstox", connected: true, detail: "Authorised." },
    { key: "FIRSTOCK", label: "Firstock", connected: false, detail: "Firstock credentials are not configured." },
  ],
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
    broker_net_pnl: "305.00",
    broker_fetched_at: new Date().toISOString(),
    reconstructed: false,
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
    broker_net_pnl: null,
    broker_fetched_at: null,
    reconstructed: false,
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
    price_source: "BROKER",
    slippage: "-18.40",
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
    price_source: "MODEL",
    slippage: null,
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
    await expect(page.getByLabel("Live broker")).toBeDisabled();
    await expect(page.getByRole("button", { name: "Turn off" })).toHaveCount(0);
    await page.getByRole("tab", { name: "Intraday trading", exact: true }).click();
    await expect(page.getByLabel("Use intraday leverage")).toBeDisabled();
    await page.getByRole("button", { name: "Daily limits" }).click();
    await expect(page.locator('input[type="number"]').first()).toBeDisabled();
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

    // Settings opens on General; the intraday desk's own settings have their own tab.
    await go(page, "Settings");
    await expect(page.getByRole("heading", { name: "General", exact: true })).toBeVisible();
    await expect(page.getByTestId("summary-intraday")).toBeVisible();
    await expect(page.getByTestId("summary-swing")).toContainText("₹750");
    await page.getByTestId("summary-intraday").getByRole("button", { name: "Open settings" }).click();
    await expect(page.getByRole("heading", { name: "Intraday trading", exact: true })).toBeVisible();

    // What the settings actually permit, shown above the inputs that set them.
    await expect(page.getByRole("heading", { name: "Effective limits" })).toBeVisible();
    // The contradiction is surfaced rather than left to be discovered from an
    // empty session: four configured, two reachable.
    await expect(page.getByText("of 4 configured")).toBeVisible();
    await expect(page.getByText(/only 2 of the 4 configured trades/)).toBeVisible();

    // The explanations wait behind the (i): not printed, shown on hover.
    await page.getByRole("button", { name: "Daily limits" }).click();
    await expect(page.getByText("Allowed: 1 to 20")).toHaveCount(0);
    await page.getByTestId("hint-maximum_daily_trades").hover();
    const tip = page.getByRole("tooltip");
    await expect(tip).toContainText("Allowed: 1 to 20");
    await expect(tip).toContainText(/Applies at once, including to the session already running/);
    await expect(tip).toContainText("maximum_daily_trades");
    await shot(page, "settings-hint");

    // Nothing to save until something is edited.
    await expect(page.getByTestId("save-bar")).toHaveCount(0);

    // Editing one control offers to save exactly that one, after a popup that shows it.
    await page.getByLabel("Maximum trades per day").fill("3");
    await expect(page.getByTestId("save-bar")).toContainText("1 unsaved change");
    await page.getByRole("button", { name: "Review and save" }).click();
    const review = page.getByTestId("changes-modal");
    await expect(review).toContainText("Maximum trades per day");
    await shot(page, "settings-review");
    await review.getByRole("button", { name: "Save changes" }).click();
    await expect(page.getByText("Saved 1 change.")).toBeVisible();
  });

  test("5b. Strategy workspace: versioned glass panel and strategy metrics", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");

    await go(page, "Strategies");
    await expect(page.getByRole("heading", { name: "Strategies" })).toBeVisible();
    await expect(page.getByText("ORB Retest — Default · v1")).toBeVisible();
    await expect(page.getByText("25%")).toBeVisible();
    // The long editor is gone: rules open in a popup from the row's Edit button.
    await expect(page.getByLabel("Strategy name ORB Retest — Default")).toHaveCount(0);
    await page.getByRole("button", { name: "Edit ORB Retest — Default" }).click();
    const editor = page.getByTestId("strategy-editor");
    await expect(editor.getByLabel("Name")).toHaveValue("ORB Retest — Default");
    await expect(editor.getByLabel("Minimum score (0-100)")).toHaveValue("90");
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

  test("5c-0. Orders: the source toggle rides the tab row, and is not a tab", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");

    await go(page, "Orders & Positions", "Orders");
    // It chooses what the tabs are showing, so it belongs on their line -- but
    // a button inside the tablist element would be announced as "tab 3 of 3"
    // to a screen reader, which is a lie told only to people who cannot see it.
    await expect(page.locator(".workspace-tabs").getByRole("button", { name: "Broker", exact: true })).toBeVisible();
    await expect(page.getByRole("tab")).toHaveCount(2);

    // And nowhere else: it is a control for this screen, not for the shell.
    await go(page, "History");
    await expect(page.getByRole("button", { name: "Broker", exact: true })).toHaveCount(0);
  });

  test("5c-1. Orders: the source toggle says whose records are on the screen", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");

    // Paper is the default, because reaching the broker costs part of the
    // rate-limit budget that placing an order draws on.
    await go(page, "Orders & Positions", "Orders");
    await expect(page.getByRole("heading", { name: "Paper orderbook" })).toBeVisible();

    await page.getByRole("button", { name: "Broker", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Upstox orderbook" })).toBeVisible();
    await expect(page.getByText("BHARTIARTL", { exact: true })).toBeVisible();
    // The paper book is gone, not merged into it. Two different claims about
    // the same day must never be added together on one screen.
    await expect(page.getByText("Simulated orderbook")).toHaveCount(0);
  });

  test("5c-2. Orders: an order we did not place is marked, and the block explained", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");

    await go(page, "Orders & Positions", "Orders");
    await page.getByRole("button", { name: "Broker", exact: true }).click();

    // The question asked about every untracked order that stopped trading,
    // answered in the row rather than by comparing two screens.
    const book = page.getByRole("table");
    await expect(book.getByText("Manual", { exact: true })).toBeVisible();
    await expect(book.getByText("Sidra", { exact: true })).toBeVisible();
    await expect(page.getByText(/1 working order is not ours/)).toBeVisible();
    await expect(page.getByText(/Reconciliation refuses new live orders/)).toBeVisible();
  });

  test("5c-2a. Orders: a refused stop says what it asked for and why it was refused", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.route("**/api/v1/broker-books/snapshot*", async (route: Route) => {
      await route.fulfill({
        json: {
          ...MOCK_BROKER_SNAPSHOT,
          orders: [
            {
              broker_order_id: "261005000053817",
              client_order_id: "sidra-stop",
              status: "REJECTED",
              symbol: "BAJFINANCE-EQ",
              side: "SELL",
              order_type: "SL",
              quantity: 12,
              filled_quantity: 0,
              average_price: 0,
              placed_at: "2026-10-05T06:37:00Z",
              trigger_price: 977.9632,
              status_message: "You've entered an invalid trigger price.",
              ours: true,
            },
          ],
          working_orders: 0,
          untracked_working: 0,
        },
      });
    });
    await page.goto("/");

    await go(page, "Orders & Positions", "Orders");
    await page.getByRole("button", { name: "Broker", exact: true }).click();

    // Both were sitting unparsed in the adapter on the day a stop was rejected
    // for a trigger that was not a multiple of the tick size. The screen showed
    // ₹0.00 -- the average price of an order that never filled -- and the
    // operator had to open the broker's own app to find out why.
    const stop = page.getByRole("row").filter({ hasText: "BAJFINANCE-EQ" });
    await expect(stop).toContainText("trigger ₹977.96");
    await expect(stop).toContainText("invalid trigger price");
  });

  test("5c-3. Orders: the broker view can look, and cannot touch", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");

    await go(page, "Orders & Positions", "Orders");
    await page.getByRole("button", { name: "Broker", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Upstox orderbook" })).toBeVisible();

    // A screen one misplaced tap can empty an account with is not what was
    // asked for. Flattening is done from Risk, deliberately.
    for (const forbidden of [/cancel/i, /square off/i, /exit position/i, /modify/i]) {
      await expect(page.getByRole("button", { name: forbidden })).toHaveCount(0);
    }
  });

  test("5c-4. Orders: a broker that cannot be read says so, and claims nothing else", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.route("**/api/v1/broker-books/snapshot*", async (route: Route) => {
      await route.fulfill({
        json: {
          ...MOCK_BROKER_SNAPSHOT,
          readable: false,
          detail: "UPSTOX could not be read: connection reset",
          orders: [],
          positions: [],
          realised: null,
          unrealised: null,
          open_positions: 0,
          working_orders: 0,
          untracked_working: 0,
        },
      });
    });
    await page.goto("/");

    await go(page, "Orders & Positions", "Orders");
    await page.getByRole("button", { name: "Broker", exact: true }).click();

    await expect(page.getByText("connection reset")).toBeVisible();
    // The distinction that matters: this screen could not ask, which is not
    // the same as there being no position.
    await expect(page.getByText(/This says nothing about whether a position is open/)).toBeVisible();
    await expect(page.getByText("₹0.00")).toHaveCount(0);
  });

  test("5c-5. Orders: a broker with no credentials cannot be chosen", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");

    await go(page, "Orders & Positions", "Orders");
    await page.getByRole("button", { name: "Broker", exact: true }).click();

    const selector = page.getByLabel("Broker to view");
    await expect(selector).toBeVisible();
    // toBeDisabled does not apply to <option>; the property is what the browser
    // actually enforces.
    await expect(selector.getByRole("option", { name: /Firstock — not connected/ })).toHaveJSProperty("disabled", true);
    // Looking at a broker is not pointing orders at one, and the screen says so.
    await expect(page.getByText(/changing the selection here does not change it/)).toBeVisible();
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

  test("5g. Live gates: readiness inspection arms nothing", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");

    await go(page, "Live readiness");
    await expect(page.getByRole("heading", { name: "Live readiness gates", exact: true })).toBeVisible();
    // A failing gate means held, and the banner must say so from the gates it
    // read rather than from a constant that outlived the Release-1 lock.
    await expect(page.getByText("Live submission is held shut.")).toBeVisible();
    await expect(page.getByText("No broker submission adapter is implemented.")).toBeVisible();
    await page.getByRole("button", { name: "Record review" }).click();
    await expect(page.getByText("Recording a review arms nothing.")).toBeVisible();
  });

  test("5g2. Live gates: an armed account is not described as held shut", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.route("**/api/v1/live/readiness", async (route: Route) => {
      await route.fulfill({
        json: {
          ...MOCK_LIVE_READINESS,
          status: "READY",
          overall_ready: true,
          live_execution_available: true,
          gates: MOCK_LIVE_READINESS.gates.map((gate) => ({ ...gate, passed: true })),
        },
      });
    });
    await page.goto("/");

    await go(page, "Live readiness");
    await expect(page.getByText("Live submission is open.")).toBeVisible();
    await expect(page.getByText("Live submission is held shut.")).not.toBeVisible();
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

  /**
   * The calendar reads the month currently on screen, so a fixture with fixed
   * dates would land in a month the test never looks at. These build the dates
   * from the same local date parts the component uses -- parsing "2026-10-01"
   * with the Date constructor gives UTC midnight, which is the previous day
   * west of Greenwich, and that is exactly the drift being guarded against.
   */
  function dayInThisMonth(day: number): string {
    const now = new Date();
    return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
  }

  function calendarDay(day: number, net: string, overrides: Record<string, unknown> = {}) {
    return {
      ...MOCK_HISTORY_DAYS[0],
      session_date: dayInThisMonth(day),
      net_pnl: net,
      gross_pnl: net,
      charges: "40.00",
      trades: 2,
      ...overrides,
    };
  }

  test("8f. Reports: the calendar colours a month by net, not by gross", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.route("**/api/v1/history/daily*", async (route: Route) => {
      await route.fulfill({ json: [calendarDay(2, "460.00"), calendarDay(3, "-300.00")] });
    });
    await page.goto("/");

    await go(page, "Reports");
    await expect(page.getByRole("heading", { name: "P&L calendar" })).toBeVisible();

    const profit = page.getByRole("button", { name: new RegExp(`${dayInThisMonth(2)}: \\+₹460`) });
    const loss = page.getByRole("button", { name: new RegExp(`${dayInThisMonth(3)}: −₹300`) });
    await expect(profit).toHaveClass(/bg-emerald-500/);
    await expect(loss).toHaveClass(/bg-rose-500/);
  });

  test("8f-1. Reports: the month total counts only the month on screen", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    // A row from outside the range -- a widened window, a stale response -- must
    // not land in a total printed above a grid that cannot show it.
    await page.route("**/api/v1/history/daily*", async (route: Route) => {
      await route.fulfill({ json: [calendarDay(2, "460.00"), { ...MOCK_HISTORY_DAYS[0], session_date: "2020-01-15", net_pnl: "9999.00" }] });
    });
    await page.goto("/");

    await go(page, "Reports");
    await expect(page.getByText("+₹460.00").first()).toBeVisible();
    await expect(page.getByText("₹9,999.00")).toHaveCount(0);
  });

  test("8g. Reports: a day with no record is not drawn as a flat day", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.route("**/api/v1/history/daily*", async (route: Route) => {
      await route.fulfill({ json: [calendarDay(2, "460.00")] });
    });
    await page.goto("/");

    await go(page, "Reports");
    // A day this system has nothing to say about must not read as a ₹0 result,
    // and must not be clickable into an empty detail panel.
    const quiet = page.getByRole("button", { name: new RegExp(`${dayInThisMonth(4)}: no trades`) });
    await expect(quiet).toBeDisabled();
    await expect(quiet).not.toHaveClass(/bg-emerald-500|bg-rose-500/);
  });

  test("8h. Reports: opening a day lists the trades behind the figure", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.route("**/api/v1/history/daily*", async (route: Route) => {
      await route.fulfill({
        json: [calendarDay(2, "146.34", { gross_pnl: "172.27", charges: "25.93", broker: "UPSTOX", broker_realized_pnl: "146.34", broker_charges: "0.00" })],
      });
    });
    await page.goto("/");

    await go(page, "Reports");
    await page.getByRole("button", { name: new RegExp(`${dayInThisMonth(2)}:`) }).click();

    // The trade, not a summary of it: what was bought, how much of it, and
    // where it was meant to go.
    const row = page.getByRole("row").filter({ hasText: "RELIANCE" });
    await expect(row).toContainText("10");
    await expect(row).toContainText("₹100.00");
    await expect(row).toContainText("₹150.00");
    await expect(row).toContainText("₹106.00");
    await expect(row).toContainText("+₹460.00");

    // The broker's figure sits beside ours and is allowed to disagree. Nothing
    // on this screen replaces a local record with one.
    await expect(page.getByText(/UPSTOX reported \+₹146.34 realised/)).toBeVisible();
    await expect(page.getByText(/rather than replacing them/)).toBeVisible();
  });

  test("8h-1. Reports: the day total is the server's, not a sum of the rows", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    // The fixture's one trade nets ₹460 while the day nets ₹146.34. They differ
    // on purpose: adding Decimals in a browser is how a total comes to disagree
    // with the same day on the History screen, and the one that disagrees would
    // always be this one.
    await page.route("**/api/v1/history/daily*", async (route: Route) => {
      await route.fulfill({ json: [calendarDay(2, "146.34", { gross_pnl: "172.27", charges: "25.93" })] });
    });
    await page.goto("/");

    await go(page, "Reports");
    await page.getByRole("button", { name: new RegExp(`${dayInThisMonth(2)}:`) }).click();

    const total = page.getByRole("row").filter({ hasText: "Day total" });
    await expect(total).toContainText("+₹172.27");
    await expect(total).toContainText("₹25.93");
    await expect(total).toContainText("+₹146.34");
  });

  test("8h-2. Reports: a live row says whose fills priced it", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.route("**/api/v1/history/daily*", async (route: Route) => {
      await route.fulfill({ json: [calendarDay(2, "146.34")] });
    });
    await page.goto("/");

    await go(page, "Reports");
    await page.getByRole("button", { name: new RegExp(`${dayInThisMonth(2)}:`) }).click();

    // The broker-priced row carries its difference from the model. That gap is
    // slippage, and it was invisible for as long as only one of the two numbers
    // reached a screen.
    const broker = page.getByRole("row").filter({ hasText: "RELIANCE" });
    await expect(broker).toContainText("−₹18.40 vs model");
    await expect(broker).not.toContainText("modelled fills");

    // The paper row is not claiming to be anything else.
    const paper = page.getByRole("row").filter({ hasText: "TCS" });
    await expect(paper).not.toContainText("vs model");
  });

  test("8h-3. Reports: a live row with no recorded fill admits it", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.route("**/api/v1/history/daily*", async (route: Route) => {
      await route.fulfill({ json: [calendarDay(2, "146.34")] });
    });
    await page.route("**/api/v1/history/trades*", async (route: Route) => {
      await route.fulfill({
        json: [{ ...MOCK_HISTORY_TRADES[0], price_source: "MODEL", slippage: null }],
      });
    });
    await page.goto("/");

    await go(page, "Reports");
    await page.getByRole("button", { name: new RegExp(`${dayInThisMonth(2)}:`) }).click();

    // Saying so is the whole fix: a live row quietly showing the simulator's
    // prices is what sent an operator to compare two screens that disagreed.
    await expect(page.getByRole("row").filter({ hasText: "RELIANCE" })).toContainText("modelled fills");
    await expect(page.getByText(/priced from completed candles, not from what the broker filled/)).toBeVisible();
  });

  test("8k. Reports: the calendar says whose record it is showing", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    const asked: string[] = [];
    await page.route("**/api/v1/history/daily*", async (route: Route) => {
      asked.push(new URL(route.request().url()).search);
      await route.fulfill({ json: [calendarDay(2, "146.34")] });
    });
    await page.goto("/");

    await go(page, "Reports");

    // Paper by default, and it says so rather than letting a simulated figure
    // be read as money.
    await expect(page.getByText(/Simulated trades only/)).toBeVisible();
    expect(asked.at(-1)).toContain("mode=PAPER");

    await page.getByRole("button", { name: "Broker", exact: true }).click();
    await expect(page.getByText(/Only the trades that reached a broker/)).toBeVisible();
    expect(asked.at(-1)).toContain("mode=LIVE");
  });

  test("8l. Reports: the broker selector narrows the calendar to one account", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    const asked: string[] = [];
    await page.route("**/api/v1/history/daily*", async (route: Route) => {
      asked.push(new URL(route.request().url()).search);
      await route.fulfill({ json: [calendarDay(2, "146.34")] });
    });
    await page.goto("/");

    await go(page, "Reports");
    await page.getByRole("button", { name: "Broker", exact: true }).click();
    // Two connected brokers are two accounts; a figure summing them is true of
    // neither, so the selector has to reach the query.
    await page.getByLabel("Broker to view").selectOption("UPSTOX");

    await expect.poll(() => asked.at(-1)).toContain("broker=UPSTOX");
  });

  test("8m. Reports: the Broker view leads with the broker's own figures for the day", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    // 1 October 2026, as both screens showed it. Ours: gross +₹172.27, charges
    // ₹25.94, net +₹146.34. Upstox: realised +₹166.93, charges ₹37.65, net
    // +₹129.28. Under a heading that said "Broker", the operator was reading
    // ours.
    await page.route("**/api/v1/history/daily*", async (route: Route) => {
      await route.fulfill({
        json: [
          calendarDay(2, "146.34", {
            gross_pnl: "172.27",
            charges: "25.94",
            live_trades: 2,
            broker: "UPSTOX",
            broker_realized_pnl: "166.93",
            broker_charges: "37.65",
            broker_net_pnl: "129.28",
          }),
        ],
      });
    });
    await page.goto("/");

    await go(page, "Reports");
    await page.getByRole("button", { name: "Broker", exact: true }).click();

    // The cell, the month tile and the day total all read the statement.
    const cell = page.getByRole("button", { name: new RegExp(`${dayInThisMonth(2)}: \\+₹129.28`) });
    await expect(cell).toBeVisible();
    await cell.click();

    const total = page.getByRole("row").filter({ hasText: "as UPSTOX reports it" });
    await expect(total).toContainText("+₹166.93");
    await expect(total).toContainText("₹37.65");
    await expect(total).toContainText("+₹129.28");

    // And ours is still on the screen underneath, not overwritten.
    const ours = page.getByRole("row").filter({ hasText: "This system's own figures" });
    await expect(ours).toContainText("+₹172.27");
    await expect(ours).toContainText("₹25.94");
    await expect(ours).toContainText("+₹146.34");
  });

  test("8m-1. Reports: a day the broker has not settled says so rather than passing as one", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.route("**/api/v1/history/daily*", async (route: Route) => {
      await route.fulfill({
        json: [
          calendarDay(2, "146.34", {
            gross_pnl: "172.27",
            charges: "25.94",
            live_trades: 2,
            broker: null,
            broker_realized_pnl: null,
            broker_charges: null,
            broker_net_pnl: null,
          }),
        ],
      });
    });
    await page.goto("/");

    await go(page, "Reports");
    await page.getByRole("button", { name: "Broker", exact: true }).click();

    // Still ours, and marked as ours. A broker's charges arrive after
    // settlement, so the newest days always read this way for a while.
    const cell = page.getByRole("button", {
      name: new RegExp(`${dayInThisMonth(2)}: \\+₹146.34 over 2 trade\\(s\\), estimated`),
    });
    await expect(cell).toBeVisible();
    await expect(cell).toContainText("est.");
    await expect(page.getByText(/still on this system's estimate/)).toBeVisible();
  });

  test("8n. Reports: a day rebuilt from the broker is shown, not reported as untraded", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    // After a history purge this is the only thing left of a session. The
    // calendar used to seed its days from local positions alone, so a day that
    // really happened read as a day on which nothing was traded.
    await page.route("**/api/v1/history/daily*", async (route: Route) => {
      await route.fulfill({
        json: [
          calendarDay(2, "129.28", {
            gross_pnl: "166.93",
            charges: "37.65",
            trades: 2,
            live_trades: 2,
            reconstructed: true,
            reconciliation: "RECONSTRUCTED",
            reconciliation_label: "Reconstructed from broker",
            reconciliation_note: "Rebuilt from UPSTOX's own report.",
            broker: "UPSTOX",
            broker_realized_pnl: "166.93",
            broker_charges: "37.65",
            broker_net_pnl: "129.28",
          }),
        ],
      });
    });
    await page.route("**/api/v1/history/broker-trades*", async (route: Route) => {
      await route.fulfill({
        json: [
          {
            session_date: dayInThisMonth(2),
            broker: "UPSTOX",
            script_name: "TATASTEEL",
            isin: "INE081A01020",
            trade_type: "INTRADAY",
            quantity: 67,
            buy_price: "183.63",
            sell_price: "180.97",
            gross_pnl: "-178.22",
            fetched_at: new Date().toISOString(),
          },
        ],
      });
    });
    await page.goto("/");

    await go(page, "Reports");
    await page.getByRole("button", { name: "Broker", exact: true }).click();

    const cell = page.getByRole("button", { name: new RegExp(`${dayInThisMonth(2)}: \\+₹129.28`) });
    await expect(cell).toBeVisible();
    await cell.click();

    // The broker's own row, and the four columns it cannot fill drawn as
    // dashes rather than left to look like figures.
    const row = page.getByRole("row").filter({ hasText: "TATASTEEL" });
    await expect(row).toContainText("67");
    await expect(row).toContainText("₹183.63");
    await expect(row).toContainText("₹180.97");
    await expect(row).toContainText("−₹178.22");
    await expect(page.getByText(/they are unknown, not zero/)).toBeVisible();
  });

  test("8i. Reports: realised and unrealised are two figures, never one", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");

    await go(page, "Reports", "P&L summary");
    await expect(page.getByRole("heading", { name: "P&L", exact: true })).toBeVisible();
    await expect(page.getByText("Realised today")).toBeVisible();
    await expect(page.getByText("Unrealised", { exact: true })).toBeVisible();
    await expect(page.getByText("₹170.18")).toBeVisible();
  });

  test("8j. Reports: a figure the broker did not report is not shown as zero", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.route("**/api/v1/broker-books/snapshot*", async (route: Route) => {
      await route.fulfill({ json: { ...MOCK_BROKER_SNAPSHOT, realised: null, unrealised: null } });
    });
    await page.goto("/");

    await go(page, "Reports", "P&L summary");
    // A zero nobody claimed is worse than a blank: it reads as a settled fact.
    await expect(page.getByText("not reported").first()).toBeVisible();
  });

  test("8o. Reports: execution quality says what the sample supports before any number", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.route("**/api/v1/history/quality*", async (route: Route) => {
      await route.fulfill({
        json: {
          from_date: "2026-09-08",
          to_date: "2026-10-07",
          verdict:
            "5 closed live trades. This is not a sample. Nothing below is evidence of an edge — it is evidence about execution.",
          notes: ["Charges came to 80.0% of gross profit, from the broker's own figures."],
          signals: 41,
          accepted: 9,
          refused: 32,
          refusals: { "Share price 1818.40 is above the 1500 cap": 12, "Daily trade ceiling reached": 20 },
          sent: 9,
          filled: 6,
          unknown_fills: 1,
          fill_rate_percent: "75.0",
          acceptance_percent: "22.0",
          entry_slippage_trades: 6,
          entry_slippage_average: "0.09",
          entry_slippage_worst: "0.14",
          trade_slippage_trades: 6,
          trade_slippage_total: "-12.40",
          trade_slippage_average: "-2.07",
          trades: 5,
          wins: 2,
          losses: 3,
          scratches: 0,
          win_rate_percent: "40.0",
          break_even_win_rate_percent: "49.6",
          average_win: "126.00",
          average_loss: "-124.00",
          gross_per_trade: "-9.12",
          net_per_trade: "-36.71",
          average_r: "-0.37",
          gross: "-45.61",
          charges_estimated: "64.85",
          charges_broker: "137.96",
          charges_per_trade: "27.59",
          charges_percent_of_gross: null,
          charge_days_settled: 4,
          charge_days_pending: 1,
          strategies: [
            { strategy_version: "orb-retest-v1@9", trades: 3, wins: 2, net: "48.00", net_per_trade: "16.00", average_r: "0.16" },
            { strategy_version: "rs-pullback-v1@4", trades: 2, wins: 0, net: "-231.55", net_per_trade: "-115.77", average_r: "-1.16" },
          ],
        },
      });
    });
    await page.goto("/");

    await go(page, "Reports", "Execution quality");

    // The sentence that stops five trades being read as a finding, above
    // everything else on the screen.
    await expect(page.getByText(/This is not a sample/)).toBeVisible();

    // The funnel: setups found, what survived risk, what filled.
    await expect(page.getByText("41")).toBeVisible();
    await expect(page.getByText(/1 unknown, left out/)).toBeVisible();
    // Why setups were refused — including the share-price band.
    await expect(page.getByText(/is above the 1500 cap/)).toBeVisible();

    // The comparison that decides everything: real win rate against the rate
    // this profile needs to come out level.
    await expect(page.getByText(/below what this profile needs/)).toBeVisible();

    // The broker's own charges, named as the broker's.
    await expect(page.getByText("broker's own").first()).toBeVisible();
    await expect(page.getByRole("row").filter({ hasText: "rs-pullback-v1@4" })).toContainText("−₹231.55");
  });

  test("9. Navigation: eight places to work, and nothing that renders \"unavailable\"", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");

    // The exact list, not just a count, because the failure this guards
    // against is a placeholder or a duplicate screen creeping back in. Reports
    // joined History rather than being folded into it: the record of what
    // happened and the summary of what it adds up to are different questions.
    const primary = page.locator("aside .space-y-1").first().getByRole("button");
    await expect(primary).toHaveText([
      "Dashboard",
      "Strategies",
      "Scanner & Universe",
      "Orders & Positions",
      "History",
      "Reports",
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

  test("9d-1. Settings: the entry slippage cap is adjustable without touching a file", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");

    await go(page, "Settings", "Intraday trading");
    await page.getByRole("button", { name: "Order entry" }).click();

    // The control that decides how far past its signal a live entry may fill,
    // and therefore whether the planned risk is a ceiling or an intention. It
    // has to be reachable here: a number that can only be changed by editing a
    // Python file is one nobody adjusts.
    await expect(page.getByText("Entry slippage cap")).toBeVisible();
    await page.getByTestId("hint-entry_slippage_cap_percent").hover();
    await expect(page.getByRole("tooltip")).toContainText(/sized from it, so a fill anywhere inside the cap/);
  });

  test("9e. Settings: alerts moved in from their own menu entry", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");

    await go(page, "Settings", "Alerts");
    await expect(page.getByRole("heading", { name: "Alerts", exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "Send test alert" })).toBeVisible();
    // A test alert only proves the outbound direction. Registering is what tells
    // Telegram where replies go and which secret to send, and without a control
    // for it here an operator has no way to recover from a rotated secret.
    await expect(page.getByRole("button", { name: "Register webhook" })).toBeVisible();
  });

  test("9f. Risk: a paper deployment says paper, and offers no way to arm", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");

    await go(page, "Risk");
    // The copy here has been wrong twice: gates that "do not exist", then a
    // start-up refusal that no longer exists either.
    await expect(page.getByText(/no order can reach a broker from it/)).toBeVisible();
    // Arm is absent entirely on a paper runtime, not merely disabled.
    await expect(page.getByRole("button", { name: "Arm live trading" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Reconcile" })).toBeDisabled();
    await expect(page.getByRole("button", { name: "Emergency stop", exact: true })).toBeVisible();
  });

  const LIVE_SAFETY = {
    application_mode: "LIVE",
    live_trading_enabled: true,
    live_execution_available: false,
  };

  async function liveRuntime(page: Page, overrides: Record<string, unknown> = {}) {
    await page.route("**/api/v1/safety/status", async (route: Route) => {
      await route.fulfill({ json: { ...MOCK_SAFETY, ...LIVE_SAFETY, ...overrides } });
    });
  }

  test("9g. Risk: an armed deployment is never described as locked", async ({ page }) => {
    // The regression that matters: a screen telling an operator their money is
    // safe while the system can place real orders.
    await setupMockRoutes(page, "ADMIN");
    await liveRuntime(page, { live_execution_available: true });
    await page.goto("/");

    await go(page, "Risk");
    await expect(page.getByText("Armed", { exact: true })).toBeVisible();
    await expect(page.getByText(/Real orders can reach the broker/)).toBeVisible();
    await expect(page.getByText(/no order can reach a broker from it/)).not.toBeVisible();
    // Disarm is always offered; arm is not offered while already armed.
    await expect(page.getByRole("button", { name: "Disarm" })).toBeEnabled();
    await expect(page.getByRole("button", { name: "Arm live trading" })).toHaveCount(0);
  });

  test("9i. Risk: arming is refused until a reconciliation has passed", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await liveRuntime(page);
    await page.goto("/");

    await go(page, "Risk");
    const armButton = page.getByRole("button", { name: "Arm live trading" });
    // No reconciliation yet, and no reason typed: two independent reasons to refuse.
    await expect(armButton).toBeDisabled();

    await page.getByRole("button", { name: "Reconcile" }).click();
    await expect(page.getByText(/Reconciliation passed ·/)).toBeVisible();
    // Still refused: the reason is required and is checked separately.
    await expect(armButton).toBeDisabled();

    await page.getByPlaceholder(/at least 8 characters/).fill("first live session");
    await expect(armButton).toBeEnabled();
    await armButton.click();
    await expect(page.getByText(/Real orders can now be placed/)).toBeVisible();
  });

  test("9n. Risk: a refusing Arm button is visibly refusing, not just inert", async ({ page }) => {
    // A safety control that looks clickable and does nothing teaches the
    // operator that the screen is broken rather than that they are blocked.
    await setupMockRoutes(page, "ADMIN");
    await liveRuntime(page);
    await page.goto("/");

    await go(page, "Risk");
    const armButton = page.getByRole("button", { name: "Arm live trading" });
    await expect(armButton).toBeDisabled();
    const opacity = await armButton.evaluate((node) => getComputedStyle(node).opacity);
    expect(Number(opacity)).toBeLessThan(0.75);
    const cursor = await armButton.evaluate((node) => getComputedStyle(node).cursor);
    expect(cursor).toBe("not-allowed");
  });

  test("9j. Risk: a short reason cannot arm", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await liveRuntime(page);
    await page.goto("/");

    await go(page, "Risk");
    await page.getByRole("button", { name: "Reconcile" }).click();
    await expect(page.getByText(/Reconciliation passed ·/)).toBeVisible();
    await page.getByPlaceholder(/at least 8 characters/).fill("oops");
    await expect(page.getByRole("button", { name: "Arm live trading" })).toBeDisabled();
  });

  test("9k. Risk: a blocked reconciliation names the finding and refuses to arm", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await liveRuntime(page);
    await page.route("**/api/v1/live-shadow/reconcile", async (route: Route) => {
      await route.fulfill({
        json: {
          id: "rec-2",
          status: "BLOCKED",
          safe_to_trade: false,
          internal_orders: 1,
          external_orders: 1,
          unknown_orders: 0,
          detail: "Trading blocked: 1 blocking (UNTRACKED_BROKER_ORDER), 0 for review.",
          findings: [{ kind: "UNTRACKED_BROKER_ORDER", severity: "BLOCKING", detail: "Broker order 123 has no local record." }],
          created_at: new Date().toISOString(),
        },
      });
    });
    await page.goto("/");

    await go(page, "Risk");
    await page.getByRole("button", { name: "Reconcile" }).click();
    await expect(page.getByText(/Reconciliation blocked ·/)).toBeVisible();
    await expect(page.getByText(/Broker order 123 has no local record/)).toBeVisible();
    await page.getByPlaceholder(/at least 8 characters/).fill("trying anyway");
    // A typed reason must not be able to talk past a blocked reconciliation.
    await expect(page.getByRole("button", { name: "Arm live trading" })).toBeDisabled();
  });

  test("9l. Risk: a viewer is told arming is not theirs, and sees no arm button", async ({ page }) => {
    await setupMockRoutes(page, "VIEWER");
    await liveRuntime(page);
    await page.goto("/");

    await go(page, "Risk");
    await expect(page.getByText(/Arming and disarming require an administrator/)).toBeVisible();
    await expect(page.getByRole("button", { name: "Arm live trading" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Disarm" })).toHaveCount(0);
  });

  test("9m. Risk: the approval mode is stated where arming is decided", async ({ page }) => {
    // Whether orders need your thumb is the difference between a system that
    // trades while you sleep and one that does not. It lived three screens away.
    await setupMockRoutes(page, "ADMIN");
    await liveRuntime(page);
    await page.route("**/api/v1/settings/trading", async (route: Route) => {
      await route.fulfill({ json: { ...MOCK_CONTROLS, execution_approval_mode: "AUTOMATIC" } });
    });
    await page.goto("/");

    await go(page, "Risk");
    await expect(page.getByText(/orders are sent without asking you/)).toBeVisible();
  });

  test("9h. Dashboard: the perimeter panel follows the runtime, not a constant", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.route("**/api/v1/safety/status", async (route: Route) => {
      await route.fulfill({
        json: { ...MOCK_SAFETY, application_mode: "LIVE", live_trading_enabled: true, live_execution_available: true },
      });
    });
    await page.goto("/");

    await expect(page.getByText("Live submission is open")).toBeVisible();
    await expect(page.getByText("Safety boundary intact")).not.toBeVisible();
    await expect(page.getByText("Live command center")).toBeVisible();
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
    await page.getByRole("button", { name: "Edit ORB Retest — Default" }).click();

    // No trading or strategy setting should need a Python file or a .env edit.
    await expect(page.getByLabel("Trailing")).toBeVisible();
    await expect(page.getByLabel(/Square off at/)).toBeVisible();
    await expect(page.getByText(/Nothing closes this strategy.s positions when the session ends/)).toBeVisible();

    await page.getByLabel("Trailing").selectOption("BREAKEVEN_AT_R");
    await expect(page.getByLabel("Move at (R ahead)")).toBeVisible();
    // The form says which side of the system a rule reaches. Break-even moves
    // the stop resting at the broker; the ATR trail does not, and the journal
    // showing trades exiting at a level the account never had is exactly the
    // trap this line exists to close.
    await expect(page.getByText(/the stop resting at the broker is moved/)).toBeVisible();

    await page.getByLabel("Trailing").selectOption("ATR_TRAIL");
    await expect(page.getByText(/Not applied live — paper and backtesting only/)).toBeVisible();
  });
  test("11. History: the broker figures can be fetched, and only where there is a broker", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");
    await go(page, "History");

    // The live day offers the fetch; the paper day has nothing at a broker to
    // ask about, so the button would only ever return an empty report.
    const live = page.locator("tr", { hasText: "2026-09-24" }).first();
    const paper = page.locator("tr", { hasText: "2026-09-23" }).first();
    await expect(live.getByRole("button", { name: "Re-fetch" })).toBeVisible();
    await expect(paper.getByRole("button", { name: /fetch/i })).toHaveCount(0);

    await live.getByRole("button", { name: "Re-fetch" }).click();
    await expect(page.getByText(/UPSTOX reported \+₹380.00 realised/)).toBeVisible();
    await expect(page.getByText(/Recorded beside the local figures/)).toBeVisible();
  });
});

// --- Trading desks (swing desk step 0.4b) --------------------------------------
//
// The switches live on the server and are enforced there; these check what the
// screen does with them: the menu, the banner above every page, and a switch
// that asks first and then saves exactly one change.

function deskSpec(key: "intraday_desk_enabled" | "swing_desk_enabled", value: boolean) {
  const intraday = key === "intraday_desk_enabled";
  return {
    key,
    group: "TRADING_DESKS",
    group_label: "Trading desks",
    label: intraday ? "Intraday trading" : "Swing trading",
    help: intraday
      ? "Buy and sell within the day; every position is closed by the square-off time. Turning this off stops new intraday trades from the next candle. Positions already open are not abandoned: their stops, targets and square-off keep working until they are closed."
      : "Hold delivery positions for several days. Paper only for now: switched on, the swing desk plans trades every evening after the close and simulates them with Rs 1,00,000 of paper money, and no swing order reaches the broker.",
    unit: "BOOLEAN",
    kind: "boolean",
    choices: [],
    is_ceiling: false,
    effect: "NEXT_SIGNAL",
    effect_label: "Applies to the next signal evaluated; signals already taken keep the old value.",
    value,
    minimum: null,
    maximum: null,
    exclusive_minimum: null,
    exclusive_maximum: null,
    last_changed_at: null,
  };
}

async function mockDesks(page: Page, desks: { intraday: boolean; swing: boolean }) {
  const state = { ...desks };
  const puts: Record<string, unknown>[] = [];
  const catalog = () => ({
    ...MOCK_CATALOG,
    group_order: ["TRADING_DESKS", ...MOCK_CATALOG.group_order],
    group_labels: { TRADING_DESKS: "Trading desks", ...MOCK_CATALOG.group_labels },
    settings: [
      deskSpec("intraday_desk_enabled", state.intraday),
      deskSpec("swing_desk_enabled", state.swing),
      ...MOCK_CATALOG.settings,
    ],
  });
  const controls = () => ({ ...MOCK_CONTROLS, intraday_desk_enabled: state.intraday, swing_desk_enabled: state.swing });
  await page.route("**/api/v1/settings/trading/catalog", async (route: Route) => {
    await route.fulfill({ json: catalog() });
  });
  await page.route("**/api/v1/settings/trading", async (route: Route) => {
    if (route.request().method() === "PUT") {
      const body = route.request().postDataJSON() as Record<string, unknown>;
      puts.push(body);
      state.intraday = body.intraday_desk_enabled as boolean;
      state.swing = body.swing_desk_enabled as boolean;
      await route.fulfill({ json: body });
    } else {
      await route.fulfill({ json: controls() });
    }
  });
  return puts;
}

async function shot(page: Page, name: string) {
  const dir = process.env.SCREENSHOT_DIR;
  if (!dir) return;
  await page.waitForTimeout(400); // let tab and pill transitions finish
  await page.screenshot({ path: `${dir}/${name}.png`, fullPage: false });
}

test.describe("Trading desks", () => {
  test("the usual state shows no banner and the whole menu", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await mockDesks(page, { intraday: true, swing: false });
    await page.goto("/");
    await expect(page.getByRole("button", { name: "Scanner & Universe", exact: true })).toBeVisible();
    await expect(page.getByTestId("scanner-card")).toBeVisible();
    await expect(page.getByTestId("desk-banner")).toHaveCount(0);
  });

  test("intraday off hides the intraday screens and says what still runs", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await mockDesks(page, { intraday: false, swing: true });
    await page.goto("/");
    const banner = page.getByTestId("desk-banner");
    await expect(banner).toContainText("Intraday trading is off");
    await expect(banner).toContainText("keep their stops");
    await expect(page.getByRole("button", { name: "Scanner & Universe", exact: true })).toHaveCount(0);
    await expect(page.getByTestId("scanner-card")).toHaveCount(0);
    await shot(page, "desk-intraday-off");
  });

  test("both desks off is said in red on every page", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await mockDesks(page, { intraday: false, swing: false });
    await page.goto("/");
    await expect(page.getByTestId("desk-banner")).toContainText("Both trading desks are off");
    await go(page, "Settings");
    await expect(page.getByTestId("desk-banner")).toContainText("Both trading desks are off");
  });

  test("a switch asks first, then saves that one change and nothing else", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    const puts = await mockDesks(page, { intraday: true, swing: false });
    await page.goto("/");
    await go(page, "Settings");
    const card = page.getByTestId("desk-intraday");
    await expect(card).toContainText("ON");
    await shot(page, "desk-settings");

    await card.getByRole("button", { name: "Turn off" }).click();
    const ask = page.getByTestId("desk-modal");
    await expect(ask).toContainText("Positions already open are not abandoned");
    await shot(page, "desk-confirm");
    expect(puts).toHaveLength(0);

    await ask.getByRole("button", { name: "Yes, turn it off" }).click();
    await expect(card).toContainText("OFF");
    expect(puts).toHaveLength(1);
    const saved = Object.fromEntries(MOCK_CATALOG.settings.map((item) => [item.key, item.value]));
    expect(puts[0]).toEqual({ ...saved, intraday_desk_enabled: false, swing_desk_enabled: false });
    await expect(page.getByTestId("desk-banner")).toContainText("Both trading desks are off");
  });

  test("a switch is not saved alongside another unsaved edit", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    const puts = await mockDesks(page, { intraday: true, swing: false });
    await page.goto("/");
    await go(page, "Settings");
    await page.getByLabel("Live broker").selectOption("UPSTOX");
    const card = page.getByTestId("desk-intraday");
    await card.getByRole("button", { name: "Turn off" }).click();
    await page.getByTestId("desk-modal").getByRole("button", { name: "Yes, turn it off" }).click();
    await expect(page.getByText("Save or discard your other changes first")).toBeVisible();
    expect(puts).toHaveLength(0);
  });
});

// --- Swing desk (paper) screens ----------------------------------------------------

const SWING_POSITION = {
  id: "p1",
  symbol: "CGPOWER",
  sector: "Capital Goods",
  strategy: "swing-trend-pullback-v1",
  status: "OPEN",
  plan_date: "2026-11-18",
  entry_trigger: 681.2,
  quantity: 52,
  initial_stop: 668.0,
  stop: 668.0,
  target: 707.6,
  risk: 686.4,
  entry_price: 681.2,
  entry_date: "2026-11-19",
  last_close: 702.35,
  last_close_date: "2026-11-20",
  unrealised: 1099.8,
  r_now: 1.6,
  holding_days: 1,
  max_holding_days: 12,
  partial_done: false,
  next_action: "Hold",
  exit_date: null,
  exit_reason: null,
  net_pnl: null,
  charges: null,
  reasons: { rs_vs_nifty_pct: 6.4 },
};

const SWING_SUMMARY = {
  enabled: true,
  paper_only: true,
  strategies: ["swing-trend-pullback-v1"],
  status_note: "Forward test on paper. Did not pass the backtest gate after charges.",
  last_processed: "2026-11-20",
  capital: 100000,
  risk_per_trade: 750,
  heat_cap: 4000,
  max_open_positions: 5,
  invested: 35422.4,
  open_risk: 686.4,
  unrealised: 1099.8,
  realised: -312.5,
  closed_trades: 2,
  open_positions: [SWING_POSITION],
  planned: [
    {
      ...SWING_POSITION,
      id: "p2",
      symbol: "BEL",
      sector: "Capital Goods",
      status: "PLANNED",
      entry_trigger: 412.4,
      initial_stop: 401.1,
      target: 435.0,
      quantity: 66,
      risk: 745.8,
      entry_price: null,
      entry_date: null,
      next_action: "Buy-stop at 412.4 next session",
      reasons: { rs_vs_nifty_pct: 3.1, atr14: 7.5 },
    },
  ],
};

const SWING_HISTORY = [
  {
    ...SWING_POSITION,
    id: "p3",
    symbol: "DIXON",
    status: "CLOSED",
    exit_date: "2026-11-17",
    exit_reason: "trail",
    net_pnl: 1250.25,
    charges: 96.4,
  },
  {
    ...SWING_POSITION,
    id: "p4",
    symbol: "TRENT",
    status: "CANCELLED",
    entry_price: null,
    exit_reason: "gapped above trigger",
    net_pnl: null,
    charges: null,
  },
];

async function mockSwing(page: Page) {
  await page.route("**/api/v1/swing/summary", async (route: Route) => {
    await route.fulfill({ json: SWING_SUMMARY });
  });
  await page.route("**/api/v1/swing/history", async (route: Route) => {
    await route.fulfill({ json: SWING_HISTORY });
  });
}

test.describe("Swing desk (paper)", () => {
  test("is not in the menu while the swing desk is off", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await mockDesks(page, { intraday: true, swing: false });
    await page.goto("/");
    await expect(page.getByRole("button", { name: "Swing (paper)", exact: true })).toHaveCount(0);
  });

  test("shows the paper portfolio, plan and history, marked paper only", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await mockDesks(page, { intraday: true, swing: true });
    await mockSwing(page);
    await page.goto("/");
    await page.getByRole("button", { name: "Swing (paper)", exact: true }).click();

    const screen = page.getByTestId("swing-workspace");
    await expect(screen).toContainText("Paper only.");
    await expect(screen).toContainText("No order from this desk reaches the broker");
    await expect(page.getByTestId("swing-strategies")).toContainText("Running: Trend Pullback. Forward test on paper.");
    const portfolio = page.getByTestId("swing-portfolio");
    await expect(portfolio).toContainText("CGPOWER");
    await expect(portfolio).toContainText("1.6R");
    await shot(page, "swing-portfolio");

    await page.getByRole("tab", { name: "Tomorrow's plan", exact: true }).click();
    const plan = page.getByTestId("swing-plan");
    await expect(plan).toContainText("BEL");
    await expect(plan).toContainText("412.4");
    await shot(page, "swing-plan");

    await page.getByRole("tab", { name: "History", exact: true }).click();
    const history = page.getByTestId("swing-history");
    await expect(history).toContainText("Trailing exit");
    await expect(history).toContainText("Not bought: Opened too far above the buy level");
    await shot(page, "swing-history");
  });
});

// --- Strategy control center (U1) ---------------------------------------------------

const SWING_STRATEGIES = [
  {
    id: "swing-trend-pullback-v1",
    name: "Trend Pullback",
    mode: "PAPER",
    live_available: false,
    live_blocked_reason: "Swing live trading is not available yet: the swing desk has no live risk limits.",
    evidence: "Backtest 2024-26, out-of-sample: 40 trades, -Rs 33 a trade after charges (in-sample +Rs 67).",
  },
  {
    id: "swing-base-breakout-v1",
    name: "Base Breakout",
    mode: "OFF",
    live_available: false,
    live_blocked_reason: "Swing live trading is not available yet: the swing desk has no live risk limits.",
    evidence: "Backtest 2024-26, out-of-sample: 3 trades, -Rs 354 a trade; negative in-sample too.",
  },
];

async function mockModes(page: Page) {
  const calls: { url: string; body: Record<string, unknown> }[] = [];
  await page.route("**/api/v1/swing/strategies", async (route: Route) => {
    await route.fulfill({ json: SWING_STRATEGIES });
  });
  await page.route("**/api/v1/swing/strategies/*/mode", async (route: Route) => {
    const body = route.request().postDataJSON() as Record<string, unknown>;
    calls.push({ url: route.request().url(), body });
    await route.fulfill({ json: SWING_STRATEGIES.map((s) => (route.request().url().includes(s.id) ? { ...s, mode: body.mode } : s)) });
  });
  await page.route("**/api/v1/settings/strategies/*/mode", async (route: Route) => {
    const body = route.request().postDataJSON() as Record<string, unknown>;
    calls.push({ url: route.request().url(), body });
    const live = body.mode === "LIVE";
    await route.fulfill({ json: MOCK_STRATEGIES.map((s) => ({ ...s, enabled: body.mode !== "OFF", live_orders: live })) });
  });
  return calls;
}

test.describe("Strategy control center", () => {
  test("lists intraday and swing strategies with what each mode means", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await mockModes(page);
    await page.goto("/");
    await go(page, "Strategies");
    const center = page.getByTestId("strategy-control-center");
    await expect(center).toContainText("ORB Retest — Default");
    await expect(center).toContainText("Trend Pullback");
    await expect(page.getByTestId("mode-row-intraday-orb-default")).toContainText("No order reaches the broker");
    await shot(page, "u1-control-center");
  });

  test("going live asks first, says real money, and sends the confirmation", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    const calls = await mockModes(page);
    await page.goto("/");
    await go(page, "Strategies");
    const row = page.getByTestId("mode-row-intraday-orb-default");
    await row.getByRole("button", { name: "Live", exact: true }).click();
    const confirm = page.getByTestId("mode-confirm");
    await expect(confirm).toContainText("Real money");
    expect(calls).toHaveLength(0);
    await shot(page, "u1-live-confirm");
    await confirm.getByLabel("Why (kept in the change history)").fill("RS Pullback is the live one");
    await confirm.getByRole("button", { name: "Yes, trade this live" }).click();
    await expect(confirm).toHaveCount(0);
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toContain("/settings/strategies/orb-default/mode");
    expect(calls[0].body).toEqual({ mode: "LIVE", confirm_live: true, reason: "RS Pullback is the live one" });
    await expect(row.getByRole("button", { name: "Live", exact: true })).toHaveAttribute("aria-pressed", "true");
  });

  test("swing live is not offered, and paper or off is one confirmed change", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    const calls = await mockModes(page);
    await page.goto("/");
    await go(page, "Strategies");
    const row = page.getByTestId("mode-row-swing-swing-base-breakout-v1");
    await expect(row.getByRole("button", { name: "Live", exact: true })).toBeDisabled();
    await row.getByRole("button", { name: "Paper", exact: true }).click();
    await page.getByTestId("mode-confirm").getByRole("button", { name: "Yes, set to paper" }).click();
    expect(calls[0].url).toContain("/swing/strategies/swing-base-breakout-v1/mode");
    expect(calls[0].body).toEqual({ mode: "PAPER", reason: null });
  });

  test("a viewer sees the modes but cannot change them", async ({ page }) => {
    await setupMockRoutes(page, "VIEWER");
    await mockModes(page);
    await page.goto("/");
    await go(page, "Strategies");
    await expect(page.getByTestId("mode-row-intraday-orb-default").getByRole("button", { name: "Live", exact: true })).toBeDisabled();
  });
});

// --- Charges (U2) -------------------------------------------------------------------

const INTRADAY_CARD = { slippage_bps: 2, participation_percent: 10, brokerage_percent: 0.03, brokerage_cap: 20, stt_sell_percent: 0.025, exchange_transaction_percent: 0.00297, gst_percent: 18, sebi_percent: 0.0001, stamp_duty_buy_percent: 0.003 };
const SWING_CARD = { brokerage_per_order: 20, stt_percent: 0.1, exchange_transaction_percent: 0.00297, sebi_percent: 0.0001, stamp_duty_buy_percent: 0.015, gst_percent: 18, dp_charge_per_sale: 20 };
const trip = (total: number, brokerage: number, dp = 0) => ({ trade_value: 50000, buy: total / 2, sell: total / 2, total, lines: { brokerage, stt: 12.5, exchange: 2.97, gst: 3.33, sebi: 0.1, stamp_duty: 1.5, dp } });

async function mockCharges(page: Page) {
  const calls: { url: string; method: string; body: Record<string, unknown> | null }[] = [];
  const view = { intraday: INTRADAY_CARD, swing: SWING_CARD, intraday_example: trip(50.4, 30), swing_example: trip(184.2, 40, 20) };
  await page.route("**/api/v1/charges", async (route: Route) => {
    await route.fulfill({ json: view });
  });
  await page.route("**/api/v1/charges/preview", async (route: Route) => {
    const body = route.request().postDataJSON() as Record<string, Record<string, number>>;
    calls.push({ url: route.request().url(), method: "POST", body });
    await route.fulfill({ json: { intraday: null, swing: body.swing ? trip(144.2, body.swing.brokerage_per_order * 2, 20) : null } });
  });
  await page.route("**/api/v1/charges/swing", async (route: Route) => {
    const body = route.request().postDataJSON() as { charges: typeof SWING_CARD };
    calls.push({ url: route.request().url(), method: "PUT", body });
    await route.fulfill({ json: { ...view, swing: body.charges, swing_example: trip(144.2, 0, 20) } });
  });
  return calls;
}

test.describe("Charges", () => {
  test("shows each desk's rate card with a worked example; swing only when its desk is on", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await mockDesks(page, { intraday: true, swing: false });
    await mockCharges(page);
    await page.goto("/");
    await go(page, "Settings", "Charges");
    await expect(page.getByTestId("charges-broker-fyers")).toBeDisabled();
    await expect(page.getByTestId("charges-broker-fyers")).toContainText("Coming soon");
    await shot(page, "charges-cards");
    await page.getByTestId("charges-broker-upstox").click();
    await expect(page.getByTestId("charges-intraday")).toContainText("Brokerage cap");
    await expect(page.getByTestId("charges-intraday-total")).toHaveText("₹50.40");
    await expect(page.getByTestId("charges-swing")).toHaveCount(0);
  });

  test("a changed figure updates the example, then saves once with the reason", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await mockDesks(page, { intraday: true, swing: true });
    const calls = await mockCharges(page);
    await page.goto("/");
    await go(page, "Settings", "Charges");
    await page.getByTestId("charges-broker-upstox").click();
    await page.getByTestId("charges-modal").getByRole("button", { name: "Swing (delivery)" }).click();
    const card = page.getByTestId("charges-swing");
    await expect(card.getByRole("button", { name: "Save swing charges" })).toBeDisabled();
    await card.getByLabel("Brokerage per order").fill("0");
    await expect(page.getByTestId("charges-swing-total")).toHaveText("₹144.20");
    expect(calls.filter((c) => c.method === "PUT")).toHaveLength(0);
    await page.getByTestId("charges-swing-example").scrollIntoViewIfNeeded();
    await shot(page, "u2-charges");
    await card.getByLabel("Why (kept in the change history)").fill("zero-brokerage delivery plan");
    await card.getByRole("button", { name: "Save swing charges" }).click();
    await expect(card.getByRole("button", { name: "Save swing charges" })).toBeDisabled();
    const puts = calls.filter((c) => c.method === "PUT");
    expect(puts).toHaveLength(1);
    expect(puts[0].body).toEqual({ charges: { ...SWING_CARD, brokerage_per_order: 0 }, reason: "zero-brokerage delivery plan" });
  });

  test("a viewer sees the charges but cannot change them", async ({ page }) => {
    await setupMockRoutes(page, "VIEWER");
    await mockCharges(page);
    await page.goto("/");
    await go(page, "Settings", "Charges");
    await page.getByTestId("charges-broker-upstox").click();
    await expect(page.getByTestId("charges-intraday").getByLabel("Brokerage cap")).toBeDisabled();
    await expect(page.getByRole("button", { name: /Save .* charges/ })).toHaveCount(0);
  });
});
// --- Connections (U3) ---------------------------------------------------------------

const cred = (name: string, label: string, kind: string, configured: boolean, source: string | null, shown: string | null, value: boolean | null = null) =>
  ({ name, label, kind, help: `${label} help`, configured, source, shown, value });

const CONNECTIONS = {
  encryption_ready: true,
  upstox: {
    fields: [
      cred("api_key", "API key", "identifier", true, "server file", "••••123"),
      cred("api_secret", "API secret", "secret", true, "server file", "Set"),
      cred("redirect_uri", "Redirect URL", "text", true, "server file", "https://host/cb"),
      cred("mobile_number", "Mobile number", "identifier", false, null, null),
      cred("pin", "Login PIN", "secret", false, null, null),
      cred("totp_secret", "TOTP secret", "secret", false, null, null),
      cred("auto_auth_enabled", "Morning auto-login", "boolean", false, null, null, false),
    ],
    login_ready: true,
    auto_login_ready: false,
    token_valid_until: null,
  },
  telegram: {
    fields: [cred("bot_token", "Bot token", "secret", true, "server file", "Set"), cred("chat_id", "Chat ID", "identifier", true, "server file", "••••789")],
    ready: true,
  },
};

async function mockConnections(page: Page, view = CONNECTIONS) {
  const calls: { url: string; body: Record<string, unknown> | null }[] = [];
  await page.route("**/api/v1/connections", async (route: Route) => {
    await route.fulfill({ json: view });
  });
  await page.route("**/api/v1/connections/upstox", async (route: Route) => {
    calls.push({ url: route.request().url(), body: route.request().postDataJSON() as Record<string, unknown> });
    await route.fulfill({ json: { ...view, upstox: { ...view.upstox, auto_login_ready: true } } });
  });
  await page.route("**/api/v1/connections/upstox/check", async (route: Route) => {
    calls.push({ url: route.request().url(), body: null });
    await route.fulfill({ json: { ok: true, detail: "Connected to Upstox account ••••4K2 (exchanges: NSE).", checked_at: "2026-11-21T03:00:00Z" } });
  });
  return calls;
}

test.describe("Connections", () => {
  test("shows where each credential comes from, never the secret itself", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await mockConnections(page);
    await page.goto("/");
    await go(page, "Settings", "Connections");
    await page.getByTestId("broker-upstox").click();
    const upstox = page.getByTestId("connection-upstox");
    await expect(upstox).toContainText("From server file");
    await expect(upstox).toContainText("Morning auto-login not set");
    await expect(upstox.getByLabel("API secret")).toHaveValue("");
    await expect(upstox.getByLabel("API secret")).toHaveAttribute("type", "password");
    await shot(page, "u3-connections");
  });

  test("saving sends only the boxes that were filled, then the connection can be checked", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    const calls = await mockConnections(page);
    await page.goto("/");
    await go(page, "Settings", "Connections");
    await page.getByTestId("broker-upstox").click();
    const upstox = page.getByTestId("connection-upstox");
    await expect(upstox.getByRole("button", { name: "Save Upstox" })).toBeDisabled();
    await upstox.getByLabel("Mobile number").fill("9876543210");
    await upstox.getByLabel("Login PIN").fill("246810");
    await upstox.getByLabel("Morning auto-login").check();
    await upstox.getByRole("button", { name: "Save Upstox" }).click();
    await expect(upstox).toContainText("Morning auto-login ready");
    expect(calls[0].body).toEqual({ values: { mobile_number: "9876543210", pin: "246810", auto_auth_enabled: true }, clear: [], reason: null });
    await expect(upstox.getByLabel("Login PIN")).toHaveValue("");
    await upstox.getByRole("button", { name: "Check connection" }).click();
    await expect(page.getByTestId("upstox-check")).toContainText("Connected to Upstox account");
  });

  test("without a server key the boxes are locked and the reason is given", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await mockConnections(page, { ...CONNECTIONS, encryption_ready: false });
    await page.goto("/");
    await go(page, "Settings", "Connections");
    await expect(page.getByTestId("no-server-key")).toBeVisible();
    await page.getByTestId("broker-upstox").click();
    await expect(page.getByTestId("connection-upstox").getByLabel("Login PIN")).toBeDisabled();
  });
});

test.describe("Swing trading settings", () => {
  test("capital and limits are set from the screen, and a save needs a reason", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    const puts: Record<string, unknown>[] = [];
    await page.route("**/api/v1/settings/swing", async (route: Route) => {
      if (route.request().method() === "PUT") {
        const body = route.request().postDataJSON() as { values: Record<string, unknown> };
        puts.push(body);
        await route.fulfill({
          json: {
            ...MOCK_SWING_SETTINGS,
            saved: true,
            settings: MOCK_SWING_SETTINGS.settings.map((spec) => ({ ...spec, value: body.values[spec.key] })),
          },
        });
        return;
      }
      await route.fulfill({ json: MOCK_SWING_SETTINGS });
    });
    await page.goto("/");
    await go(page, "Settings", "Swing trading");
    const tab = page.getByTestId("settings-swing");
    await expect(tab).toContainText("₹750");
    await expect(tab).toContainText("6:00 PM IST");
    await page.getByLabel("Swing capital").fill("200000");
    await expect(tab).toContainText("₹1,500 a trade");
    await shot(page, "settings-swing");
    await page.getByRole("button", { name: "Review and save" }).click();
    const review = page.getByTestId("changes-modal");
    await expect(review).toContainText("₹1,00,000");
    await expect(review).toContainText("₹2,00,000");
    await expect(review.getByRole("button", { name: "Save changes" })).toBeDisabled();
    await review.getByLabel("Why (kept in the change history)").fill("more paper capital");
    await shot(page, "settings-swing-review");
    await review.getByRole("button", { name: "Save changes" }).click();
    await expect(page.getByText("Swing settings saved.")).toBeVisible();
    expect(puts).toHaveLength(1);
    expect(puts[0]).toEqual({
      values: { capital: 200000, risk_per_trade_percent: 0.75, heat_cap_percent: 4, max_open_positions: 5 },
      reason: "more paper capital",
    });
  });

  test("a connection is a card; brokers not built yet say so and do nothing", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await mockConnections(page);
    await page.goto("/");
    await go(page, "Settings", "Connections");
    await expect(page.getByTestId("broker-fyers")).toBeDisabled();
    await expect(page.getByTestId("broker-firstock")).toContainText("Coming soon");
    await expect(page.getByTestId("connection-upstox")).toHaveCount(0);
    await shot(page, "connections-cards");
    await page.getByTestId("broker-upstox").click();
    await expect(page.getByTestId("upstox-modal")).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.getByTestId("upstox-modal")).toHaveCount(0);
  });
});

// --- Swing backtest and data (U4) ----------------------------------------------------

const M = (trades: number, perTrade: number) => ({ trades, wins: Math.round(trades * 0.4), win_rate: 0.4, net_pnl: trades * perTrade, charges: trades * 120, per_trade: perTrade, average_r: 0.1, profit_factor: 1.1, max_drawdown: 3000, average_holding_days: 6 });
const RUN = {
  id: "r1", finished_at: "2026-11-21T12:00:00Z", years: 2, from_date: "2024-11-21", to_date: "2026-11-20", out_of_sample_from: "2026-04-10",
  strategies: ["swing-trend-pullback-v1"], shares_with_data: 200, charges: { brokerage_per_order: 20, stt_percent: 0.1, exchange_transaction_percent: 0.00297, sebi_percent: 0.0001, stamp_duty_buy_percent: 0.015, gst_percent: 18, dp_charge_per_sale: 20 },
  in_sample: M(90, 67), out_of_sample: M(40, -33),
  per_strategy: { "swing-trend-pullback-v1": { in_sample: M(90, 67), out_of_sample: M(40, -33) } },
  exits: { trail: 20 }, skipped: { regime: 5 },
};

async function mockSwingTools(page: Page, opts: { marketHours?: boolean } = {}) {
  const calls: { url: string; method: string; body: unknown }[] = [];
  let jobs: Record<string, unknown> = {};
  await page.route("**/api/v1/swing/jobs", async (route: Route) => {
    await route.fulfill({ json: { market_hours: Boolean(opts.marketHours), jobs } });
  });
  await page.route("**/api/v1/swing/backtests", async (route: Route) => {
    if (route.request().method() === "POST") {
      calls.push({ url: route.request().url(), method: "POST", body: route.request().postDataJSON() });
      jobs = { backtest: { id: "j", status: "running", started_at: "", finished_at: null, detail: "Started." } };
      await route.fulfill({ status: 202, json: { market_hours: false, jobs } });
    } else {
      await route.fulfill({ json: [RUN] });
    }
  });
  await page.route("**/api/v1/swing/data/coverage", async (route: Route) => {
    await route.fulfill({
      json: {
        from_date: "2024-11-21", to_date: "2026-11-21", index_days: 496, last_index_day: "2026-11-20", minimum_sessions: 250,
        counts: { complete: 197, gaps: 0, short: 3, empty: 0 },
        shares: [{ symbol: "VAML", instrument_token: "NSE_EQ|X", sessions: 110, first: "2026-06-01", last: "2026-11-20", missing: 0, status: "short" }],
        suspects: [{ symbol: "SIEMENS", instrument_token: "NSE_EQ|S", ex_date: "2025-04-07", previous_close: 5400, open: 2686, jump: 2.0104, nearest_factor: 2, confirmed: false }],
      },
    });
  });
  await page.route("**/api/v1/swing/corporate-events", async (route: Route) => {
    if (route.request().method() === "POST") {
      calls.push({ url: route.request().url(), method: "POST", body: route.request().postDataJSON() });
      await route.fulfill({ status: 201, json: {} });
    } else {
      await route.fulfill({ json: [] });
    }
  });
  return calls;
}

test.describe("Swing backtest and data", () => {
  test("shows the last backtest with out-of-sample first-class, and starts a new one", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await mockDesks(page, { intraday: true, swing: true });
    await mockSwing(page);
    const calls = await mockSwingTools(page);
    await page.goto("/");
    await go(page, "Swing (paper)", "Backtest");
    await expect(page.getByTestId("metrics-OUT-OF-SAMPLE")).toContainText("−₹33");
    await shot(page, "u4-backtest");
    await page.getByLabel("Base Breakout").uncheck();
    await page.getByRole("button", { name: "Run backtest" }).click();
    await expect(page.getByTestId("job-backtest")).toContainText("Running");
    expect(calls[0].body).toEqual({ years: 2, strategies: ["swing-trend-pullback-v1"] });
  });

  test("heavy jobs are locked during market hours", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await mockDesks(page, { intraday: true, swing: true });
    await mockSwing(page);
    await mockSwingTools(page, { marketHours: true });
    await page.goto("/");
    await go(page, "Swing (paper)", "Data & events");
    await expect(page.getByRole("button", { name: "Fetch daily candles (2 years)" })).toBeDisabled();
    await expect(page.getByTestId("market-hours-note")).toBeVisible();
  });

  test("a suspected split is reviewed and recorded from the screen", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await mockDesks(page, { intraday: true, swing: true });
    await mockSwing(page);
    const calls = await mockSwingTools(page);
    await page.goto("/");
    await go(page, "Swing (paper)", "Data & events");
    await page.getByRole("button", { name: "Check data" }).click();
    await expect(page.getByTestId("swing-coverage")).toContainText("VAML");
    await page.getByTestId("suspect-SIEMENS").getByRole("button", { name: "Review" }).click();
    const form = page.getByTestId("event-form");
    await form.getByLabel("Note").fill("Siemens Energy demerger");
    await shot(page, "u4-data");
    await form.getByRole("button", { name: "Record" }).click();
    const post = calls.find((call) => call.url.includes("corporate-events"));
    expect(post?.body).toEqual({ symbol: "SIEMENS", event_type: "SPLIT", ex_date: "2025-04-07", price_factor: 2.0104, amount: null, note: "Siemens Energy demerger" });
  });
});


// --- Diagnostics (U5) ----------------------------------------------------------------

test.describe("Diagnostics", () => {
  test("runs a read-only report with its date and shows the script's own output", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    const bodies: unknown[] = [];
    await page.route("**/api/v1/diagnostics/reports", async (route: Route) => {
      await route.fulfill({
        json: [
          { name: "daily_review", title: "Daily review", help: "The day in one place.", options: ["date"], when: "After 3:30 PM for the full day" },
          { name: "check_settings", title: "Settings check", help: "Every setting in force.", options: [], when: "Any time" },
        ],
      });
    });
    await page.route("**/api/v1/diagnostics/reports/*", async (route: Route) => {
      bodies.push(route.request().postDataJSON());
      await route.fulfill({
        json: { name: "daily_review", command: "python scripts/daily_review.py --date 2026-11-20", exit_code: 0, output: "DAILY REVIEW 2026-11-20\n  trades: 2", started_at: "2026-11-21T10:00:00", seconds: 3.2, truncated: false },
      });
    });
    await page.goto("/");
    await go(page, "Reports", "Diagnostics");
    await page.getByLabel("Session date").fill("2026-11-20");
    await page.getByRole("button", { name: "Run Daily review" }).click();
    await expect(page.getByTestId("diagnostics-output")).toContainText("DAILY REVIEW 2026-11-20");
    expect(bodies[0]).toEqual({ session_date: "2026-11-20", from_date: null, to_date: null, after: null, no_broker: false });
    await shot(page, "u5-diagnostics");
    await page.getByRole("radio", { name: /Settings check/ }).click();
    await expect(page.getByLabel("Session date")).toHaveCount(0);
  });
});


// --- First run, users and the setup checklist (U6) ------------------------------------

const CHECKLIST = [
  { id: "admin", title: "Administrator account", done: true, detail: "1 account(s).", workspace: "settings", tab: "users", required: true },
  { id: "upstox_app", title: "Upstox app keys", done: false, detail: "API key, secret and redirect URL.", workspace: "settings", tab: "connections", required: true },
  { id: "telegram", title: "Telegram alerts", done: false, detail: "A bot token and chat ID.", workspace: "settings", tab: "connections", required: false },
];

test.describe("First run and users", () => {
  test("a fresh install offers the first administrator, then signs in", async ({ page }) => {
    await page.route("**/api/v1/auth/me", async (route) => route.fulfill({ status: 401, json: { detail: "Authentication required" } }));
    await page.route("**/api/v1/setup/status", async (route) => route.fulfill({ json: { needs_first_admin: true } }));
    const made: unknown[] = [];
    await page.route("**/api/v1/setup/first-admin", async (route) => {
      made.push(route.request().postDataJSON());
      await route.fulfill({ status: 201, json: { id: "u1", email: "owner@example.com", role: "ADMIN", is_active: true, locked: false, created_at: "2026-11-21T10:00:00Z" } });
    });
    let signedIn = false;
    await page.route("**/api/v1/auth/login", async (route) => {
      signedIn = true;
      await route.fulfill({ json: { email: "owner@example.com", role: "ADMIN" } });
    });
    await page.goto("/login");
    await expect(page.getByRole("heading", { name: "Create the first administrator" })).toBeVisible();
    await page.getByLabel("Email").fill("owner@example.com");
    await page.getByLabel("Password", { exact: true }).fill("a long enough passphrase");
    await page.getByLabel("Confirm password").fill("not the same passphrase");
    await page.getByRole("button", { name: "Create administrator and sign in" }).click();
    await expect(page.getByText("The two passwords are not the same.")).toBeVisible();
    expect(made).toHaveLength(0);
    await page.getByLabel("Confirm password").fill("a long enough passphrase");
    await shot(page, "u6-first-admin");
    await page.getByRole("button", { name: "Create administrator and sign in" }).click();
    await expect.poll(() => signedIn).toBe(true);
    expect(made[0]).toEqual({ email: "owner@example.com", password: "a long enough passphrase" });
  });

  test("an existing install shows the normal sign-in", async ({ page }) => {
    await page.route("**/api/v1/auth/me", async (route) => route.fulfill({ status: 401, json: { detail: "Authentication required" } }));
    await page.route("**/api/v1/setup/status", async (route) => route.fulfill({ json: { needs_first_admin: false } }));
    await page.goto("/login");
    await expect(page.getByRole("heading", { name: "Welcome back" })).toBeVisible();
    await expect(page.getByLabel("Confirm password")).toHaveCount(0);
  });

  test("an administrator adds a viewer and changes a role", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    const calls: { method: string; url: string; body: unknown }[] = [];
    const rows = [
      { id: "a1", email: "admin@sidra.test", role: "ADMIN", is_active: true, locked: false, created_at: "2026-11-01T04:00:00Z" },
      { id: "t1", email: "partner@sidra.test", role: "TRADER", is_active: true, locked: false, created_at: "2026-11-10T04:00:00Z" },
    ];
    await page.route("**/api/v1/users", async (route) => {
      if (route.request().method() === "POST") {
        const body = route.request().postDataJSON();
        calls.push({ method: "POST", url: route.request().url(), body });
        await route.fulfill({ status: 201, json: { id: "v1", email: body.email, role: body.role, is_active: true, locked: false, created_at: "2026-11-21T04:00:00Z" } });
      } else {
        await route.fulfill({ json: rows });
      }
    });
    await page.route("**/api/v1/users/*", async (route) => {
      calls.push({ method: route.request().method(), url: route.request().url(), body: route.request().postDataJSON() });
      await route.fulfill({ json: { ...rows[1], role: "VIEWER" } });
    });
    await page.goto("/");
    await go(page, "Settings", "Users");
    await expect(page.getByTestId("user-row")).toHaveCount(2);
    await page.getByLabel("Role for partner@sidra.test").selectOption("VIEWER");
    await expect.poll(() => calls.length).toBe(1);
    expect(calls[0]).toMatchObject({ method: "PUT", body: { role: "VIEWER" } });
    expect(calls[0].url).toContain("/users/t1");
    const form = page.locator("form").filter({ hasText: "Add account" });
    await form.getByLabel("Email").fill("viewer@sidra.test");
    await form.getByLabel(/First password/).fill("twelve chars ok");
    await shot(page, "u6-users");
    await form.getByRole("button", { name: "Add account" }).click();
    await expect.poll(() => calls.length).toBe(2);
    expect(calls[1].body).toEqual({ email: "viewer@sidra.test", password: "twelve chars ok", role: "VIEWER" });
  });

  test("a viewer sees only their own password card", async ({ page }) => {
    await setupMockRoutes(page, "VIEWER");
    await page.goto("/");
    await go(page, "Settings", "Users");
    await expect(page.getByRole("heading", { name: "My password" })).toBeVisible();
    await expect(page.getByTestId("user-row")).toHaveCount(0);
  });

  test("an unfinished setup shows a banner that opens the checklist, and a step opens its screen", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.route("**/api/v1/setup/checklist", async (route) => route.fulfill({ json: CHECKLIST }));
    await page.goto("/");
    await expect(page.getByTestId("setup-banner")).toContainText("1 required step left");
    await page.getByRole("button", { name: "Open the setup checklist" }).click();
    await expect(page.getByTestId("setup-checklist")).toContainText("Upstox app keys");
    await shot(page, "u6-checklist");
    await page.getByTestId("step-upstox_app").getByRole("button", { name: "Open" }).click();
    await expect(page.getByRole("heading", { name: "Connections", exact: true })).toBeVisible();
  });
});


// --- Strategy builder (U7) ----------------------------------------------------------------

test.describe("Strategy builder", () => {
  test("builds a draft from a template, backtests it unsaved, and saves it off", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    const base = MOCK_STRATEGIES[0];
    const live = { ...base, id: "rs-live", name: "RS Pullback", strategy_type: "rs-pullback-v1", enabled: true, live_orders: true };
    const off = { ...base, id: "old-draft", name: "Old draft", enabled: false, live_orders: false };
    await page.route("**/api/v1/strategy-builder/catalog", async (route) => {
      await route.fulfill({
        json: {
          templates: [
            { strategy_type: "rs-pullback-v1", name: "Relative-Strength Pullback", about: "Shares stronger than NIFTY, on a pullback.", prerequisites: [], defaults: { ...base, id: "new", name: "My Relative-Strength Pullback", strategy_type: "rs-pullback-v1", enabled: false, live_orders: false } },
            { strategy_type: "orb-retest-v1", name: "Opening Range Breakout Retest", about: "First 15 minutes' range, broken and retested.", prerequisites: [], defaults: { ...base, id: "new", name: "My ORB", enabled: false, live_orders: false } },
          ],
          strategies: [
            { id: live.id, name: live.name, strategy_type: live.strategy_type, mode: "LIVE", version: 4, configuration: live },
            { id: off.id, name: off.name, strategy_type: off.strategy_type, mode: "OFF", version: 1, configuration: off },
          ],
        },
      });
    });
    await page.route("**/api/v1/strategy-builder/instruments", async (route) => {
      await route.fulfill({ json: [{ instrument_token: "NSE_EQ|INE002A01018", symbol: "RELIANCE" }, { instrument_token: "NSE_EQ|INE467B01029", symbol: "TCS" }] });
    });
    const posted: { url: string; body: Record<string, unknown> }[] = [];
    await page.route("**/api/v1/strategy-builder/backtest", async (route) => {
      posted.push({ url: route.request().url(), body: route.request().postDataJSON() });
      await route.fulfill({ status: 201, json: { ...MOCK_BACKTEST_RESULT_LIKE, trades: [] } });
    });
    await page.route("**/api/v1/strategy-builder/strategies", async (route) => {
      posted.push({ url: route.request().url(), body: route.request().postDataJSON() });
      await route.fulfill({ status: 201, json: [live] });
    });

    await page.goto("/");
    await go(page, "Strategies", "Builder");
    const builder = page.getByTestId("strategy-builder");
    await expect(builder.getByTestId("existing-rs-live").getByRole("button", { name: "Edit" })).toBeDisabled();
    await expect(builder.getByRole("button", { name: "Delete RS Pullback" })).toBeDisabled();
    await expect(builder.getByRole("button", { name: "Delete Old draft" })).toBeEnabled();

    await builder.getByRole("radio", { name: /Relative-Strength Pullback/ }).click();
    await builder.getByLabel("Minimum score (0-100)").fill("88");
    await page.getByTestId("builder-test").getByText("RELIANCE", { exact: true }).click();
    await builder.getByRole("button", { name: "Run backtest" }).click();
    await expect(page.getByTestId("builder-result")).toContainText("Trades");
    expect(posted[0].body.instrument_tokens).toEqual(["NSE_EQ|INE002A01018"]);
    expect((posted[0].body.draft as { minimum_score: number }).minimum_score).toBe(88);
    await shot(page, "u7-builder");

    await builder.getByLabel("Why (kept in the change history)").fill("a stricter RS variant to paper trade");
    await builder.getByRole("button", { name: "Save strategy" }).click();
    await expect.poll(() => posted.length).toBe(2);
    expect(posted[1].body).toMatchObject({ reason: "a stricter RS variant to paper trade", draft: { name: "My Relative-Strength Pullback", minimum_score: 88 } });
  });

  test("a viewer is told the builder is for administrators", async ({ page }) => {
    await setupMockRoutes(page, "VIEWER");
    await page.goto("/");
    await go(page, "Strategies", "Builder");
    await expect(page.getByText("Only an administrator can build or change strategies.")).toBeVisible();
  });
});

const MOCK_BACKTEST_RESULT_LIKE = {
  id: "bt-u7", status: "COMPLETED", start_date: "2026-10-26", end_date: "2026-11-20", timeframe_seconds: 60,
  instrument_tokens: ["NSE_EQ|INE002A01018"], source_candle_count: 7125, data_fingerprint: "abc", initial_capital: 50000,
  final_equity: 50412, net_pnl: 412, max_drawdown: 380, failure_detail: null, created_at: "2026-11-21T10:00:00Z",
  summary: { trades: 6, winners: 3, losers: 3, win_rate: 50, net_pnl: 412, profit_factor: 1.4, initial_capital: 50000, final_equity: 50412, return_percent: 0.82, max_drawdown: 380, equity_curve: [], strategy_comparison: [] },
};


// --- Strategies: actions and popups --------------------------------------------------------

test.describe("Strategy actions and popups", () => {
  async function mockSave(page: Page) {
    const calls: { method: string; url: string; body: Record<string, unknown> | null }[] = [];
    await page.route("**/api/v1/strategy-builder/instruments", async (route) => {
      await route.fulfill({ json: [{ instrument_token: "NSE:RELIANCE", symbol: "RELIANCE" }, { instrument_token: "NSE:INFY", symbol: "INFY" }] });
    });
    await page.route("**/api/v1/strategy-builder/strategies/*", async (route) => {
      const body = route.request().method() === "DELETE" ? null : (route.request().postDataJSON() as Record<string, unknown>);
      calls.push({ method: route.request().method(), url: route.request().url(), body });
      await route.fulfill({ json: MOCK_STRATEGIES });
    });
    return calls;
  }

  test("Edit opens the rules in a popup and saves them without touching the mode", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await mockModes(page);
    const calls = await mockSave(page);
    await page.goto("/");
    await go(page, "Strategies");
    await page.getByRole("button", { name: "Edit ORB Retest — Default" }).click();
    const editor = page.getByTestId("strategy-editor");
    await expect(editor).toBeVisible();
    await expect(editor.getByRole("button", { name: "Save changes" })).toBeDisabled();
    await editor.getByLabel("Minimum score (0-100)").fill("85");
    await editor.getByLabel("Why (kept in the change history)").fill("fewer, cleaner setups");
    await shot(page, "strategy-edit-popup");
    await editor.getByRole("button", { name: "Save changes" }).click();
    await expect(editor).toHaveCount(0);
    expect(calls[0].method).toBe("PUT");
    expect(calls[0].url).toContain("/strategy-builder/strategies/orb-default");
    expect(calls[0].body).toMatchObject({ reason: "fewer, cleaner setups", confirm_live: false, draft: { minimum_score: 85, enabled: true, live_orders: false } });
  });

  test("editing a live strategy needs a reason and a real-money tick", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await mockModes(page);
    const calls = await mockSave(page);
    await page.route("**/api/v1/settings/strategies", async (route) => {
      await route.fulfill({ json: [{ ...MOCK_STRATEGIES[0], live_orders: true }] });
    });
    await page.goto("/");
    await go(page, "Strategies");
    await page.getByRole("button", { name: "Edit ORB Retest — Default" }).click();
    const editor = page.getByTestId("strategy-editor");
    await expect(editor.getByTestId("live-edit-warning")).toContainText("real money");
    await editor.getByLabel("Minimum score (0-100)").fill("92");
    const save = editor.getByRole("button", { name: "Save live strategy" });
    await expect(save).toBeDisabled();
    await editor.getByLabel(/Why/).fill("one change: stricter score");
    await expect(save).toBeDisabled();
    await editor.getByLabel("I understand this changes a strategy that trades real money.").check();
    await shot(page, "strategy-edit-live");
    await save.click();
    await expect.poll(() => calls.length).toBe(1);
    expect(calls[0].body).toMatchObject({ confirm_live: true, reason: "one change: stricter score", draft: { minimum_score: 92 } });
  });

  test("a mode change is confirmed in a popup in front of the table", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await mockModes(page);
    await page.goto("/");
    await go(page, "Strategies");
    await page.getByTestId("mode-row-intraday-orb-default").getByRole("button", { name: "Off", exact: true }).click();
    const dialog = page.getByRole("alertdialog");
    await expect(dialog).toBeVisible();
    await expect(dialog).toHaveAttribute("aria-modal", "true");
    await shot(page, "mode-popup");
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
  });

  test("only an Off strategy offers Delete, and it asks in a popup", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await mockModes(page);
    const calls = await mockSave(page);
    await page.route("**/api/v1/settings/strategies", async (route) => {
      await route.fulfill({ json: [MOCK_STRATEGIES[0], { ...MOCK_STRATEGIES[0], id: "old", name: "Old draft", enabled: false, live_orders: false }] });
    });
    await page.goto("/");
    await go(page, "Strategies");
    await expect(page.getByRole("button", { name: "Delete ORB Retest — Default" })).toHaveCount(0);
    await page.getByRole("button", { name: "Delete Old draft" }).click();
    const confirm = page.getByTestId("delete-confirm");
    await expect(confirm).toContainText("kept");
    await confirm.getByRole("button", { name: "Yes, delete it" }).click();
    await expect.poll(() => calls.length).toBe(1);
    expect(calls[0]).toMatchObject({ method: "DELETE" });
    expect(calls[0].url).toContain("/strategy-builder/strategies/old");
  });

  test("New strategy opens the builder", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await page.goto("/");
    await go(page, "Strategies");
    await page.getByRole("button", { name: "New strategy" }).click();
    await expect(page.getByRole("tab", { name: "Builder" })).toHaveAttribute("aria-selected", "true");
  });
});


// --- Strategies > Subscriptions ---------------------------------------------------------

test.describe("Subscriptions", () => {
  const VIEW = {
    source: "server file",
    benchmark: "NSE_INDEX|Nifty 50",
    benchmark_name: "NIFTY 50",
    benchmarks: [{ key: "NSE_INDEX|Nifty 50", name: "NIFTY 50" }, { key: "NSE_INDEX|Nifty Bank", name: "NIFTY BANK" }],
    shares: [
      { instrument_key: "NSE_EQ|INE062A01020", symbol: "SBIN", name: "State Bank of India", in_nifty200: true, known: true, sector: "BANK" },
      { instrument_key: "NSE_EQ|INE467B01029", symbol: "TCS", name: "Tata Consultancy Services Ltd.", in_nifty200: true, known: true, sector: "IT" },
    ],
    applies: "A change saved now applies from the next trading day; today's scan keeps the list it started with.",
    max_shares: 100,
    updated_at: null,
  };

  async function mockWatchlist(page: Page) {
    const saved: Record<string, unknown>[] = [];
    await page.route("**/api/v1/watchlist", async (route) => {
      if (route.request().method() === "PUT") {
        const body = route.request().postDataJSON() as Record<string, unknown>;
        saved.push(body);
        await route.fulfill({ json: { ...VIEW, source: "screen" } });
      } else {
        await route.fulfill({ json: VIEW });
      }
    });
    await page.route("**/api/v1/watchlist/search*", async (route) => {
      await route.fulfill({
        json: [
          { instrument_key: "NSE_EQ|INE742F01042", symbol: "ADANIPORTS", name: "Adani Ports and Special Economic Zone Ltd.", in_nifty200: true, source: "NIFTY 200 file", sector: "ADANI" },
          { instrument_key: "NSE_EQ|INE423A01024", symbol: "ADANIENT", name: "Adani Enterprises Ltd.", in_nifty200: true, source: "NIFTY 200 file", sector: "ADANI" },
        ],
      });
    });
    return saved;
  }

  test("a share is found by symbol, confirmed by its full company name, and saved with the removal", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    const saved = await mockWatchlist(page);
    await page.goto("/");
    await go(page, "Strategies", "Subscriptions");
    await expect(page.getByTestId("share-SBIN")).toContainText("State Bank of India");
    await expect(page.getByRole("button", { name: "Save changes" })).toBeDisabled();

    await page.getByLabel("Add a share").fill("ADANIPORTS");
    await page.getByTestId("search-results").getByRole("button", { name: /ADANIPORTS/ }).click();
    const add = page.getByTestId("add-confirm");
    await expect(add.getByTestId("add-confirm-name")).toHaveText("Adani Ports and Special Economic Zone Ltd.");
    await shot(page, "subscriptions-add-confirm");
    await add.getByRole("button", { name: "Yes, add ADANIPORTS" }).click();
    await expect(page.getByTestId("share-ADANIPORTS")).toContainText("New");

    await page.getByRole("button", { name: "Remove TCS" }).click();
    await expect(page.getByTestId("share-TCS")).toContainText("Removing");
    await page.getByRole("button", { name: "Save changes" }).click();

    const confirm = page.getByTestId("save-confirm");
    await expect(confirm).toContainText("+ ADANIPORTS — Adani Ports and Special Economic Zone Ltd.");
    await expect(confirm).toContainText("− TCS — Tata Consultancy Services Ltd.");
    await expect(confirm).toContainText("real money");
    const yes = confirm.getByRole("button", { name: "Yes, save the watchlist" });
    await expect(yes).toBeDisabled();
    await confirm.getByLabel("Why (kept in the audit log)").fill("add ports, drop TCS");
    await confirm.getByLabel(/I have checked the company names/).check();
    await shot(page, "subscriptions-save-confirm");
    await yes.click();
    await expect.poll(() => saved.length).toBe(1);
    expect(saved[0]).toEqual({
      instruments: ["NSE_EQ|INE062A01020", "NSE_EQ|INE742F01042"],
      benchmark: "NSE_INDEX|Nifty 50",
      reason: "add ports, drop TCS",
      confirm: true,
    });
  });

  test("a share already listed cannot be added twice, and a viewer cannot change anything", async ({ page }) => {
    await setupMockRoutes(page, "VIEWER");
    await mockWatchlist(page);
    await page.goto("/");
    await go(page, "Strategies", "Subscriptions");
    await expect(page.getByTestId("share-SBIN")).toBeVisible();
    await expect(page.getByLabel("Add a share")).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Remove SBIN" })).toHaveCount(0);
    await expect(page.getByLabel("Benchmark index")).toBeDisabled();
  });

  test("changing the benchmark warns about the live strategy and is listed in the confirmation", async ({ page }) => {
    await setupMockRoutes(page, "ADMIN");
    await mockWatchlist(page);
    await page.goto("/");
    await go(page, "Strategies", "Subscriptions");
    await page.getByLabel("Benchmark index").selectOption("NSE_INDEX|Nifty Bank");
    await expect(page.getByText(/changes what the live RS Pullback calls/)).toBeVisible();
    await page.getByRole("button", { name: "Save changes" }).click();
    await expect(page.getByTestId("save-confirm")).toContainText("Benchmark: NIFTY 50 → NIFTY BANK");
  });
});

// --- Themes -------------------------------------------------------------------

/** WCAG relative luminance of a computed `rgb(...)` colour. */
function luminance(css: string): number {
  const [r, g, b] = (css.match(/[\d.]+/g) ?? ["0", "0", "0"]).slice(0, 3).map((part) => {
    const c = Number(part) / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

const THEME_SCREENS: [string, string | undefined][] = [
  ["Dashboard", undefined],
  ["Orders & Positions", undefined],
  ["Reports", undefined],
  ["Settings", "General"],
  ["Settings", "Intraday trading"],
  ["Settings", "Swing trading"],
  ["Settings", "Connections"],
];

for (const theme of ["light", "dark"] as const) {
  test.describe(`Theme: ${theme}`, () => {
    test("secondary text and headings read against their panel", async ({ page }) => {
      await page.addInitScript((value) => window.localStorage.setItem("sidra-theme", value), theme);
      await setupMockRoutes(page, "ADMIN");
      await page.goto("/");
      await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
      for (const [workspace, tab] of THEME_SCREENS) {
        await go(page, workspace, tab);
        await shot(page, `theme-${theme}-${workspace}-${tab ?? "main"}`.toLowerCase().replace(/[^a-z0-9-]+/g, "-"));
      }
      // Every grey a screen uses for secondary text, measured against the page:
      // dark ink on the Day theme, light ink on the Night theme.
      const tones = await page.evaluate(() => {
        const out: Record<string, string> = {};
        for (const cls of ["text-white", "text-slate-200", "text-slate-400", "text-slate-500", "text-emerald-300", "text-amber-300"]) {
          const el = document.createElement("span");
          el.className = cls;
          document.body.appendChild(el);
          out[cls] = getComputedStyle(el).color;
          el.remove();
        }
        out.panel = getComputedStyle(document.querySelector(".panel") as Element).backgroundColor;
        return out;
      });
      for (const [cls, colour] of Object.entries(tones)) {
        if (cls === "panel") continue;
        const l = luminance(colour);
        if (theme === "light") expect(l, `${cls} is dark enough on white`).toBeLessThan(0.2);
        else expect(l, `${cls} is light enough on the night desk`).toBeGreaterThan(0.15);
      }
      // A `dark:` tone follows the app's switch, not the operating system's.
      await page.emulateMedia({ colorScheme: theme === "light" ? "dark" : "light" });
      const probe = await page.evaluate(() => {
        const el = document.createElement("span");
        el.className = "text-slate-900 dark:text-white";
        document.body.appendChild(el);
        const colour = getComputedStyle(el).color;
        el.remove();
        return colour;
      });
      if (theme === "light") expect(luminance(probe)).toBeLessThan(0.05);
      else expect(luminance(probe)).toBeGreaterThan(0.9);
    });
  });
}
