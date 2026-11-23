export type BrokerBookOrder = { broker_order_id: string; client_order_id: string | null; status: string; symbol: string; side: string | null; order_type: string | null; quantity: number | null; filled_quantity: number | null; average_price: number | null; placed_at: string | null; trigger_price: number | null; status_message: string | null; ours: boolean };
export type BrokerBookPosition = { symbol: string; instrument_token: string | null; net_quantity: number | null; average_price: number | null; last_price: number | null; realised: number | null; unrealised: number | null; day_pnl: number | null };
export type BrokerChoice = { key: string; label: string; connected: boolean; detail: string };
export type BrokerChoices = { selected: string; brokers: BrokerChoice[] };
export type BrokerSnapshot = { broker: string; fetched_at: string; stale: boolean; readable: boolean; detail: string; orders: BrokerBookOrder[]; positions: BrokerBookPosition[]; realised: number | null; unrealised: number | null; open_positions: number; working_orders: number; untracked_working: number };
export type TradingStatus = { state: "TRADING" | "PAUSED" | "BLOCKED" | "STOPPED" | "PAPER"; headline: string; detail: string; remedy: string; blocker: string | null; trading: boolean; can_resume: boolean; can_pause: boolean; emergency_stop_active: boolean };
export type NotificationToggle = { key: string; label: string; help: string; value: boolean };
export type NotificationCatalog = { toggles: NotificationToggle[]; always_sent: string[] };
export type SetupStatus = { needs_first_admin: boolean };
export type ManagedUser = { id: string; email: string; role: "ADMIN" | "TRADER" | "VIEWER"; is_active: boolean; locked: boolean; created_at: string };
export type ChecklistItem = { id: string; title: string; done: boolean; detail: string; workspace: string; tab: string | null; required: boolean };
export type BuilderTemplate = { strategy_type: string; name: string; about: string; prerequisites: string[]; defaults: PaperStrategy };
export type BuilderExisting = { id: string; name: string; strategy_type: string; mode: "OFF" | "PAPER" | "LIVE"; version: number; configuration: PaperStrategy };
export type BuilderCatalog = { templates: BuilderTemplate[]; strategies: BuilderExisting[] };
export type BuilderInstrument = { instrument_token: string; symbol: string };
export type WatchShare = { instrument_key: string; symbol: string; name: string; in_nifty200: boolean; known: boolean; sector: string | null };
export type WatchlistView = { source: string; benchmark: string; benchmark_name: string; benchmarks: { key: string; name: string }[]; shares: WatchShare[]; applies: string; max_shares: number; updated_at: string | null };
export type WatchSearchResult = { instrument_key: string; symbol: string; name: string; in_nifty200: boolean; source: string; sector: string | null };
export type User = { email: string; role: "ADMIN" | "TRADER" | "VIEWER"; is_active: boolean };
export type ServiceStatus = { status: string; detail: string; checked_at: string };
export type Overview = { mode: string; live_trading_enabled: boolean; api: ServiceStatus; database: ServiceStatus; redis: ServiceStatus; scanner: ServiceStatus; market_data: ServiceStatus; firstock: ServiceStatus; telegram: ServiceStatus };
export type ScannerStatus = { status: string; last_heartbeat: string | null; detail: string; worker_restart_count: number };
export type MarketSession = { phase: string; trading_day: boolean; reason: string; local_timestamp: string; session_date: string | null; regular_open: string | null; regular_close: string | null; is_special_session: boolean };
export type DataQuality = { instrument_token: string; state: "GOOD" | "DEGRADED" | "STALE" | "INVALID"; reason: string; session_date: string; expected_bars: number; received_bars: number; missing_buckets: string[]; received_ticks: number; duplicate_ticks: number; out_of_order_ticks: number; invalid_ticks: number; average_latency_ms: number; max_latency_ms: number; last_exchange_timestamp: string | null; last_received_timestamp: string | null; observed_at: string; allows_signals: boolean };
export type ScannerEvaluation = { id: string; instrument_token: string; script_name?: string; session_date: string; candle_opened_at: string; strategy_id: string; strategy_name: string; strategy_version: number; status: "ACCEPTED" | "WATCHING" | "REJECTED"; decision_state: string; side: "LONG" | "SHORT" | null; reason: string; failed_conditions: string[]; data_quality_state: "GOOD" | "DEGRADED" | "STALE" | "INVALID" | "MISSING"; candle_close: number; candle_volume: number; score: number; score_breakdown: Record<string, number>; indicator_snapshot: Record<string, unknown>; entry_price: number | null; stop_price: number | null; target_price: number | null; quantity: number | null; risk_amount: number | null; created_at: string };
export type PaperSignal = { id: string; instrument_token: string; script_name?: string; session_date: string; candle_opened_at: string; side: "LONG" | "SHORT"; status: string; entry_price: number; stop_price: number; target_price: number; quantity: number; score: number; score_breakdown: Record<string, number>; created_at: string };
export type PaperOrder = { id: string; paper_signal_id: string; client_order_id: string; instrument_token: string; session_date: string; side: "BUY" | "SELL"; order_type: string; order_role: string; status: string; quantity: number; filled_quantity: number; average_fill_price: number | null; limit_price: number | null; stop_price: number | null; fee_total: number; eligible_after: string; rejection_reason: string | null; created_at: string };
export type PaperPosition = { id: string; paper_signal_id: string; instrument_token: string; session_date: string; strategy_version: string; side: "LONG" | "SHORT"; status: string; initial_quantity: number; open_quantity: number; average_entry_price: number | null; average_exit_price: number | null; current_price: number | null; stop_price: number; target_price: number; realized_pnl: number; unrealized_pnl: number; fees_total: number; total_pnl: number; opened_at: string | null; closed_at: string | null };
export type PaperDayOrder = PaperOrder & { script_name: string; strategy: string };
export type PaperDayPosition = PaperPosition & { script_name: string; strategy: string };
export type PaperDayTotals = { trades: number; open_positions: number; wins: number; losses: number; scratches: number; gross_pnl: number; charges: number; net_pnl: number };
export type PaperDay = { session_date: string; totals: PaperDayTotals; pending_orders: number; strategies: (PaperDayTotals & { strategy: string })[]; positions: PaperDayPosition[]; orders: PaperDayOrder[] };
export type PaperExecutionSummary = { orders: number; pending_orders: number; fills: number; open_positions: number; realized_pnl: number; unrealized_pnl: number; total_pnl: number; fees_total: number };
export type PaperRiskSummary = { session_date: string; daily_risk_limit: number; daily_risk_allocated: number; daily_risk_available: number; maximum_open_positions: number; active_reservations: number; open_positions: number; exposure_limit: number; current_exposure: number; exposure_available: number; leverage_multiplier: number; rejected_reservations: number };
export type LiveActivation = { id: string | null; armed: boolean; reason: string; expires_at: string | null; revoked_at: string | null; revoked_reason: string | null; blocking_gates: string[] };
export type LiveReconciliation = { id: string; status: string; safe_to_trade: boolean; internal_orders: number; external_orders: number; unknown_orders: number; detail: string; findings: { kind?: string; severity?: string; detail?: string }[]; created_at: string };
export type BacktestStrategyMetric = { strategy_id: string; strategy_name: string; strategy_version: number; trades: number; winners: number; losers: number; win_rate: number; net_pnl: number; profit_factor: number | null };
export type BacktestSummary = { trades: number; winners: number; losers: number; win_rate: number; net_pnl: number; profit_factor: number | null; initial_capital: number; final_equity: number; return_percent: number; max_drawdown: number; equity_curve: { at: string | null; equity: number; drawdown: number }[]; strategy_comparison: BacktestStrategyMetric[] };
export type BacktestRun = { id: string; status: string; start_date: string; end_date: string; timeframe_seconds: number; instrument_tokens: string[]; source_candle_count: number; data_fingerprint: string; initial_capital: number; final_equity: number | null; net_pnl: number | null; max_drawdown: number | null; summary: BacktestSummary; failure_detail: string | null; created_at: string };
export type BacktestTrade = { id: string; strategy_id: string; strategy_name: string; strategy_version: number; instrument_token: string; session_date: string; side: string; quantity: number; signal_at: string; entered_at: string; exited_at: string; entry_price: number; exit_price: number; gross_pnl: number; fees_total: number; net_pnl: number; realized_r: number; exit_reason: string };
export type BacktestRunDetail = BacktestRun & { trades: BacktestTrade[] };
export type OmsOrder = { id: string; intent_id: string; idempotency_key: string; instrument_token: string; side: string; quantity: number; mode: string; venue: string; status: string; filled_quantity: number; average_fill_price: number | null; unknown_since: string | null; created_at: string; updated_at: string };
export type OmsReconciliation = { id: string; mode: string; status: string; internal_orders: number; external_orders: number; unknown_orders: number; detail: string; created_at: string };
export type ShadowOrder = { id: string; instrument_token: string; side: string; intended_quantity: number; intended_price: number; comparison_status: string; paper_fill_price: number | null; price_delta: number | null; observed_at: string; compared_at: string | null };
export type ShadowSummary = { intended_orders: number; compared_orders: number; awaiting_paper_fill: number; average_price_delta: number; broker_submissions: number };
export type LiveGate = { key: string; label: string; passed: boolean; detail: string };
export type LiveReadiness = { status: string; overall_ready: boolean; live_execution_available: boolean; checked_at: string; gates: LiveGate[] };
export type LiveReadinessHistory = { id: string; status: string; overall_ready: boolean; checked_at: string; checked_by_user_id: string | null };
export type MarketCandle = { opened_at: string; closed_at: string; open: number; high: number; low: number; close: number; volume: number };
export type ExecutionApprovalMode = "DISABLED" | "TELEGRAM_APPROVAL" | "AUTOMATIC";
export type LiveBroker = "NONE" | "UPSTOX" | "FIRSTOCK";
export type SettingSpec = {
  key: string;
  group: string;
  group_label: string;
  label: string;
  help: string;
  unit: "INR" | "PERCENT" | "COUNT" | "MULTIPLE" | "POINTS" | "RATIO" | "TIME_IST" | "CHOICE" | "BOOLEAN";
  kind: "number" | "integer" | "boolean" | "time" | "choice";
  choices: string[];
  is_ceiling: boolean;
  effect: "IMMEDIATE" | "NEXT_SIGNAL" | "NEXT_SESSION";
  effect_label: string;
  value: unknown;
  minimum: number | null;
  maximum: number | null;
  exclusive_minimum: number | null;
  exclusive_maximum: number | null;
  last_changed_at: string | null;
};
export type EffectiveLimits = {
  capital: string;
  planned_risk_per_trade: string;
  daily_risk_budget: string;
  trades_the_budget_allows: number;
  configured_trade_ceiling: number;
  effective_trade_ceiling: number;
  binding_control: string;
  maximum_open_positions: number;
  exposure_ceiling: string;
  leverage_multiplier: string;
  daily_loss_limit: string;
  daily_loss_percent: string | null;
  daily_profit_target: string;
  daily_profit_percent: string | null;
  warnings: string[];
};
export type SettingsCatalog = {
  group_order: string[];
  group_labels: Record<string, string>;
  settings: SettingSpec[];
  effective: EffectiveLimits;
};
export type IndicatorSettings = {
  candle_timeframe_seconds: number;
  opening_range_minutes: number;
  ema_fast_period: number;
  ema_slow_period: number;
  atr_period: number;
  volume_lookback_candles: number;
  rvol_baseline_sessions: number;
  daily_history_sessions: number;
};
// "ENVIRONMENT" means nobody has saved these through the UI yet, so the
// deployment is still reading its .env. Worth showing rather than hiding.
export type IndicatorCatalog = { settings: SettingSpec[]; source: "DATABASE" | "ENVIRONMENT" };
export type ReconciliationStatus =
  | "MATCHED"
  | "ESTIMATED_CHARGES"
  | "BROKER_DATA_PENDING"
  | "MISMATCH"
  // A day we hold no local record of, rebuilt from the broker's own report.
  // The other four say what a comparison found; this one says there was
  // nothing to compare.
  | "RECONSTRUCTED";
// Every money field arrives as a string because the API sends Decimals: a
// rupee figure that round-trips through a JavaScript number is a rupee figure
// that can come back a paisa short.
export type HistoryTrade = {
  position_id: string;
  signal_id: string;
  session_date: string;
  instrument_token: string;
  script_name: string;
  side: string;
  strategy_version: string;
  status: string;
  execution_mode: "PAPER" | "LIVE";
  is_open: boolean;
  quantity: number;
  open_quantity: number;
  entry_price: string | null;
  exit_price: string | null;
  stop_price: string;
  target_price: string;
  opened_at: string | null;
  closed_at: string | null;
  gross_pnl: string;
  charges: string;
  net_pnl: string;
  unrealized_pnl: string;
  risk_amount: string;
  r_multiple: string | null;
  reconciliation: ReconciliationStatus;
  reconciliation_label: string;
  reconciliation_note: string;
  price_source: "BROKER" | "MODEL";
  slippage: string | null;
  broker: string | null;
};
export type HistoryDay = {
  session_date: string;
  trades: number;
  open_trades: number;
  wins: number;
  losses: number;
  scratches: number;
  win_rate_percent: string | null;
  gross_pnl: string;
  charges: string;
  net_pnl: string;
  unrealized_pnl: string;
  best_trade: string | null;
  worst_trade: string | null;
  live_trades: number;
  halt_reason: string | null;
  reconciliation: ReconciliationStatus;
  reconciliation_label: string;
  reconciliation_note: string;
  broker: string | null;
  broker_realized_pnl: string | null;
  broker_charges: string | null;
  /** Realised minus charges, as the broker's own statement nets the day. Null
   *  unless the broker reported both halves; never assembled in the browser. */
  broker_net_pnl: string | null;
  broker_fetched_at: string | null;
  /** Every figure on this day came from the broker because no local record of
   *  it survives. It carries no stop, target or strategy. */
  reconstructed: boolean;
};

/** One matched buy/sell pair, as the broker reported it. No stop, no target
 *  and no net: the first two are ours, and no broker publishes a per-trade
 *  cost for the third. */
export type BrokerTrade = {
  session_date: string;
  broker: string;
  script_name: string;
  isin: string | null;
  trade_type: string | null;
  quantity: number | null;
  buy_price: string | null;
  sell_price: string | null;
  gross_pnl: string | null;
  fetched_at: string | null;
};
export type HistoryOverview = {
  from_date: string;
  to_date: string;
  trading_days: number;
  trades: number;
  open_trades: number;
  wins: number;
  losses: number;
  scratches: number;
  win_rate_percent: string | null;
  gross_pnl: string;
  charges: string;
  net_pnl: string;
  best_day: string | null;
  worst_day: string | null;
  largest_win: string | null;
  largest_loss: string | null;
  average_win: string | null;
  average_loss: string | null;
  profit_factor: string | null;
  expectancy: string | null;
  charges_as_percent_of_gross: string | null;
  halted_days: number;
  live_trades: number;
  reconciliation_counts: Record<string, number>;
  reconciliation_labels: Record<string, string>;
  broker_realized_pnl: string | null;
  broker_charges: string | null;
  broker_net_pnl: string | null;
  /** How many of the period's days the broker has settled, and how many live
   *  days are still waiting on its figures. */
  broker_days: number;
  days_pending_broker: number;
};
export type ExecutionQuality = {
  from_date: string;
  to_date: string;
  verdict: string;
  notes: string[];
  signals: number;
  accepted: number;
  refused: number;
  refusals: Record<string, number>;
  sent: number;
  filled: number;
  unknown_fills: number;
  fill_rate_percent: string | null;
  acceptance_percent: string | null;
  entry_slippage_trades: number;
  entry_slippage_average: string | null;
  entry_slippage_worst: string | null;
  trade_slippage_trades: number;
  trade_slippage_total: string;
  trade_slippage_average: string | null;
  trades: number;
  wins: number;
  losses: number;
  scratches: number;
  win_rate_percent: string | null;
  break_even_win_rate_percent: string | null;
  average_win: string | null;
  average_loss: string | null;
  gross_per_trade: string | null;
  net_per_trade: string | null;
  average_r: string | null;
  gross: string;
  charges_estimated: string;
  charges_broker: string | null;
  charges_per_trade: string | null;
  charges_percent_of_gross: string | null;
  charge_days_settled: number;
  charge_days_pending: number;
  strategies: {
    strategy_version: string;
    trades: number;
    wins: number;
    net: string;
    net_per_trade: string | null;
    average_r: string | null;
  }[];
};

export type HistoryOrder = {
  order_id: string;
  client_order_id: string;
  order_role: string;
  order_type: string;
  side: string;
  status: string;
  quantity: number;
  filled_quantity: number;
  average_fill_price: string | null;
  limit_price: string | null;
  stop_price: string | null;
  fee_total: string;
  rejection_reason: string | null;
  created_at: string;
};
export type HistoryFill = {
  fill_id: string;
  order_id: string;
  side: string;
  quantity: number;
  price: string;
  gross_value: string;
  slippage_amount: string;
  brokerage: string;
  stt: string;
  exchange_charge: string;
  gst: string;
  sebi_charge: string;
  stamp_duty: string;
  total_fees: string;
  occurred_at: string;
};
export type HistoryTradeDetail = { trade: HistoryTrade; orders: HistoryOrder[]; fills: HistoryFill[] };
export type BrokerFetchResult = {
  session_date: string;
  broker: string;
  realized_pnl: string | null;
  charges: string | null;
  turnover: string | null;
  trade_count: number | null;
  fetched_at: string;
  note: string;
};
export type HistoryRange = { from_date?: string; to_date?: string; mode?: "PAPER" | "LIVE"; broker?: string; strategy?: string };
export type StrategyResult = { strategy: string; execution_mode: "LIVE" | "PAPER"; trades: number; open_trades: number; wins: number; losses: number; scratches: number; win_rate_percent: string | null; gross_pnl: string; charges: string; net_pnl: string; average_r: string | null; best_trade: string | null; worst_trade: string | null };
export type SwingSettingsView = {
  settings: SettingSpec[];
  group_order: string[];
  group_labels: Record<string, string>;
  effective: { risk_per_trade: number; heat_cap: number; largest_position: number; trades_the_cap_allows: number };
  schedule: { quiet_hours: string; plan_after: string; entry: string; results_avoid_days: number; results_exit_days: number };
  saved: boolean;
};
export type RiskPreset = { key: string; label: string; description: string; controls: Record<string, number>; effective: EffectiveLimits };
export type SettingRevision = { created_at: string; changed_keys: string[]; risk_increased: string[]; changed_by_user_id: string | null };
export type SwingPosition = {
  id: string;
  symbol: string;
  sector: string | null;
  strategy: string;
  status: "PLANNED" | "OPEN" | "CLOSED" | "CANCELLED";
  plan_date: string;
  entry_trigger: number;
  quantity: number;
  initial_stop: number;
  stop: number;
  target: number | null;
  risk: number;
  entry_price: number | null;
  entry_date: string | null;
  last_close: number | null;
  last_close_date: string | null;
  unrealised: number | null;
  r_now: number | null;
  holding_days: number;
  max_holding_days: number;
  partial_done: boolean;
  next_action: string | null;
  exit_date: string | null;
  exit_reason: string | null;
  net_pnl: number | null;
  charges: number | null;
  reasons: Record<string, number | string>;
};
export type SwingSummary = {
  enabled: boolean;
  paper_only: boolean;
  strategies: string[];
  status_note: string;
  last_processed: string | null;
  capital: number;
  risk_per_trade: number;
  heat_cap: number;
  max_open_positions: number;
  invested: number;
  open_risk: number;
  unrealised: number;
  realised: number;
  closed_trades: number;
  open_positions: SwingPosition[];
  planned: SwingPosition[];
};export type TradingControls = { intraday_desk_enabled?: boolean; swing_desk_enabled?: boolean; account_capital: number; risk_per_trade_percent: number; maximum_daily_risk_percent: number; maximum_open_positions: number; maximum_open_exposure_percent: number; maximum_daily_trades: number; minimum_score: number; minimum_rr: number; volume_multiplier: number; retest_tolerance_percent: number; minimum_ema_spread_percent: number; stop_atr_multiple: number; min_stop_distance_percent: number; trade_start_time: string; trade_cutoff_time: string; intraday_leverage_enabled?: boolean; intraday_leverage_multiplier?: number; execution_approval_mode?: ExecutionApprovalMode; live_broker?: LiveBroker; live_entry_order_type?: "MARKET" | "LIMIT"; entry_slippage_cap_percent?: number; entry_slippage_cap_r?: number; universe_max_share_price?: number; universe_min_share_price?: number; session_square_off_time?: string };
export type ExitRules = {
  stop_rule: "WIDEST_OF_STRUCTURE_ATR_PERCENT";
  stop_atr_multiple: number | null;
  min_stop_distance_percent: number | null;
  target_rule: "RR_MULTIPLE" | "ATR_MULTIPLE";
  target_rr: number | null;
  target_atr_multiple: number;
  trailing_rule: "NONE" | "BREAKEVEN_AT_R" | "ATR_TRAIL";
  trailing_trigger_r: number;
  trailing_atr_multiple: number;
  time_exit_minutes: number | null;
  square_off_time: string | null;
};
export type StrategyEvidence = {
  source: string;
  trades: number;
  wins: number;
  losses: number;
  win_rate_percent: string | null;
  net_pnl: string;
  gross_pnl: string;
  charges: string;
  average_r: string | null;
  from_date: string | null;
  to_date: string | null;
  out_of_sample: boolean;
  sufficient: boolean;
  shortfall: number;
};
export type StrategyVersionChange = { at: string; version: number; changed_keys: string[]; risk_increased: string[] };
export type StrategyRecentSignal = {
  id: string;
  session_date: string;
  instrument_token: string;
  side: string;
  status: string;
  score: number;
  entry_price: string;
  stop_price: string;
  target_price: string;
  created_at: string;
};
export type StrategyDetail = {
  configuration: PaperStrategy;
  strategy_name: string;
  prerequisites: string[];
  purpose: string;
  regime: string;
  entry: string;
  does_not: string;
  required_inputs: string[];
  exit_plan: string[];
  limits: Record<string, unknown>;
  signals_last_30_days: number;
  last_signal_on: string | null;
  backtest: StrategyEvidence;
  forward: StrategyEvidence;
  verdict: "NOT_ENOUGH_EVIDENCE" | "NEGATIVE" | "INCONCLUSIVE" | "PROMISING";
  verdict_label: string;
  verdict_headline: string;
  verdict_caveats: string[];
  version_history: StrategyVersionChange[];
  recent_signals: StrategyRecentSignal[];
};
export type StrategyMode = "OFF" | "PAPER" | "LIVE";
export type SwingStrategy = { id: string; name: string; mode: StrategyMode; live_available: boolean; live_blocked_reason: string; evidence: string };
export type IntradayCharges = { slippage_bps: number; participation_percent: number; brokerage_percent: number; brokerage_cap: number; stt_sell_percent: number; exchange_transaction_percent: number; gst_percent: number; sebi_percent: number; stamp_duty_buy_percent: number };
export type SwingCharges = { brokerage_per_order: number; stt_percent: number; exchange_transaction_percent: number; sebi_percent: number; stamp_duty_buy_percent: number; gst_percent: number; dp_charge_per_sale: number };
export type RoundTrip = { trade_value: number; buy: number; sell: number; total: number; lines: Record<string, number> };
export type CredentialField = { name: string; label: string; kind: "secret" | "identifier" | "text" | "boolean"; help: string; configured: boolean; source: "screen" | "server file" | null; shown: string | null; value: boolean | null };
export type ConnectionsView = {
  encryption_ready: boolean;
  upstox: { fields: CredentialField[]; login_ready: boolean; auto_login_ready: boolean; token_valid_until: string | null };
  telegram: { fields: CredentialField[]; ready: boolean };
};
export type DiagnosticReport = { name: string; title: string; help: string; options: string[]; when: string };
export type DiagnosticRun = { name: string; command: string; exit_code: number; output: string; started_at: string; seconds: number; truncated: boolean };
export type SwingJob = { id: string; status: "running" | "done" | "failed"; started_at: string; finished_at: string | null; detail: string };
export type SwingJobs = { market_hours: boolean; jobs: Partial<Record<"backfill" | "backtest", SwingJob>> };
export type SwingMetrics = { trades: number; wins: number; win_rate: number; net_pnl: number; charges: number; per_trade: number; average_r: number; profit_factor: number | null; max_drawdown: number; average_holding_days: number };
export type SwingBacktestRun = {
  id: string; finished_at: string; years: number; from_date: string | null; to_date: string | null; out_of_sample_from: string | null;
  strategies: string[]; shares_with_data: number; charges: SwingCharges;
  in_sample: SwingMetrics; out_of_sample: SwingMetrics; per_strategy: Record<string, { in_sample: SwingMetrics; out_of_sample: SwingMetrics }>;
  exits: Record<string, number>; skipped: Record<string, number>;
};
export type SwingCoverage = {
  from_date: string; to_date: string; index_days: number; last_index_day: string | null; minimum_sessions: number;
  counts: Record<"complete" | "gaps" | "short" | "empty", number>;
  shares: { symbol: string; instrument_token: string; sessions: number; first: string | null; last: string | null; missing: number; status: string }[];
  suspects: { symbol: string; instrument_token: string; ex_date: string; previous_close: number; open: number; jump: number; nearest_factor: number; confirmed: boolean }[];
};
export type CorporateEvent = { id: string; symbol: string | null; instrument_token: string; event_type: string; ex_date: string; price_factor: number | null; amount: number | null; confirmed: boolean; source: string; note: string | null };
export type ConnectionCheck ={ ok: boolean; detail: string; checked_at: string };
export type ChargesView = { intraday: IntradayCharges; swing: SwingCharges; intraday_example: RoundTrip; swing_example: RoundTrip };
export type PaperStrategy = { id: string; name: string; enabled: boolean; live_orders: boolean; strategy_type: string; version: number; universe: string[]; allowed_sides: string[]; allowed_sessions: string[]; max_trades_per_day: number; max_trades_per_side: number | null; cooldown_minutes: number; risk_per_trade_percent: number | null; minimum_score: number; minimum_rr: number; volume_multiplier: number; retest_tolerance_percent: number; minimum_ema_spread_percent: number; rs_threshold_percent: number | null; exit_rules: ExitRules };
export type UniverseEntry = { instrument_token: string; script_name: string; session_date: string; rank: number; score: number; selected: boolean; eligible: boolean; rejection_reason: string | null; liquidity_score: number; volatility_score: number; gap_score: number; trend_score: number; metrics: Record<string, number> };
export type UniverseSummary = { session_date: string; enabled: boolean; universe_size: number; total_candidates: number; eligible: number; selected: number; last_built_at: string | null };
export type ScoreBucket = { samples: number; win_rate_percent: number; average_realized_r: number };
export type ScoreComponentInsight = { component: string; threshold: number; lift_r: number; above: ScoreBucket; below: ScoreBucket };
export type ScoreAnalysis = { resolved_signals: number; insufficient_data: boolean; overall?: ScoreBucket; components: ScoreComponentInsight[] };
export type SweepMetrics = { trades: number; wins: number; losses: number; win_rate_percent: number; net_pnl: number; return_percent: number; profit_factor: number | null; average_realized_r: number };
export type SweepCombination = { index: number; parameters: Record<string, number>; in_sample: SweepMetrics; validation: SweepMetrics; proven: boolean };
export type BacktestSweep = { id: string; status: string; strategy_id: string; start_date: string; end_date: string; validation_fraction: number; instrument_tokens: string[]; parameter_grid: Record<string, number[]>; combination_count: number; best_index: number | null; promoted_index: number | null; combinations: SweepCombination[]; failure_detail: string | null; created_at: string };
export type MarketRegime = { enabled: boolean; available: boolean; regime: string | null; score: number | null; allow_long: boolean | null; allow_short: boolean | null; size_multiplier: number | null; reason: string | null; components: Record<string, number>; vix: Record<string, unknown> | null; breadth: Record<string, unknown> | null; session_date: string | null; computed_at: string | null };
export type StrategyDefinition = { identifier: string; name: string; prerequisites: string[] };
export type StrategyMetric = { strategy_id: string; strategy_name: string; strategy_version: number; evaluations: number; accepted: number; rejected: number; watching: number; acceptance_rate: number; sessions: number; first_session: string | null; last_session: string | null };
export type SafetyStatus = { paper_tracking_enabled: boolean; application_mode: string; live_trading_enabled: boolean; live_execution_available: boolean; emergency_stop_active: boolean; emergency_stop_reason: string | null; emergency_stop_source: string | null; emergency_stop_at: string | null };
export type TelegramStatus = { configured: boolean; webhook_configured: boolean; inbound_enabled: boolean; detail: string };
export type BrokerControls = { upstox_paper_enabled: boolean; firstock_feed_enabled: boolean };
export type UpstoxOAuthStart = { authorization_url: string };
export type AutoAuthStatus = { enabled: boolean; configured: boolean; last_run_at: string | null; last_success: boolean | null; expires_at: string | null; error: string | null; next_run: string | null };
export type AutoAuthTriggerResponse = { status: string; expires_at: string | null; error: string | null };
export type InstrumentRefreshResponse = { status: string; instrument_count: number; missing_keys: string[]; fetched_at: string };
export type FirstockBrokerStatus = { configured: boolean; user_id_masked: string | null; websocket_status: string; detail: string; updated_at: string | null; subscription_count: number };
export type UserSession = { id: string; created_at: string; expires_at: string; ip_address: string | null; user_agent: string | null };
export type AuditLog = { id: string; event_type: string; created_at: string; user_id: string | null; ip_address: string | null; message: string | null; metadata_json: Record<string, unknown> };

function csrfToken(): string | undefined {
  return document.cookie.split("; ").find((value) => value.startsWith("csrf_token="))?.split("=")[1];
}

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = "ApiError";
  }
}

let refreshInFlight: Promise<boolean> | null = null;

async function refreshAccessToken(): Promise<boolean> {
  if (!refreshInFlight) {
    refreshInFlight = (async () => {
      const token = csrfToken();
      const response = await fetch("/api/v1/auth/refresh", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json", ...(token ? { "X-CSRF-Token": decodeURIComponent(token) } : {}) },
      });
      return response.ok;
    })().finally(() => { refreshInFlight = null; });
  }
  return refreshInFlight;
}

/** One validation problem, as a sentence naming the field it is about.
 *
 * `loc` arrives as ["body", "instrument_tokens", 0]: the leading "body" or
 * "query" names the part of the request rather than anything the person
 * filled in, and a trailing index is noise on a message that already says
 * "at least 1 item". Pydantic also prefixes its own custom errors with
 * "Value error, ", which reads as a stutter in a toast.
 */
function problemLine(item: unknown): string {
  if (typeof item === "string") return item;
  if (!item || typeof item !== "object") return "";
  const { loc, msg } = item as { loc?: unknown; msg?: unknown };
  const text = (typeof msg === "string" ? msg : "").replace(/^Value error,\s*/, "");
  const field = Array.isArray(loc)
    ? loc.filter((part) => typeof part === "string" && part !== "body" && part !== "query").join(".")
    : "";
  if (field && text) return `${field}: ${text}`;
  return text || field;
}

/** What to show for a failed response.
 *
 * `detail` was typed as a string and handed straight to `ApiError`. FastAPI
 * answers a 422 with a **list of objects**, so the list was coerced by
 * `Error` and every validation failure in the app surfaced as
 * "[object Object]" — the Backtesting lab most visibly, because its form
 * posts a date range and an instrument list that are easy to get wrong. The
 * cast was the bug: it told the compiler a shape the server does not send.
 */
function errorMessage(detail: unknown, status: number): string {
  if (typeof detail === "string" && detail.trim()) return detail;
  if (Array.isArray(detail)) {
    const lines = detail.map(problemLine).filter(Boolean);
    if (lines.length) return lines.join("; ");
  }
  if (detail && typeof detail === "object") {
    const line = problemLine(detail);
    if (line) return line;
  }
  return `Request failed (${status})`;
}

async function request<T>(path: string, init?: RequestInit, allowRefresh = true): Promise<T> {
  const method = init?.method?.toUpperCase() ?? "GET";
  const csrf = ["POST", "PUT", "PATCH", "DELETE"].includes(method) && path !== "/auth/login" ? csrfToken() : undefined;
  const response = await fetch(`/api/v1${path}`, { ...init, credentials: "include", headers: { "Content-Type": "application/json", ...(csrf ? { "X-CSRF-Token": decodeURIComponent(csrf) } : {}), ...(init?.headers ?? {}) } });
  if (response.status === 401 && allowRefresh && !["/auth/login", "/auth/refresh"].includes(path)) {
    if (await refreshAccessToken()) return request<T>(path, init, false);
  }
  if (!response.ok) { const body = await response.json().catch(() => null) as { detail?: unknown } | null; throw new ApiError(errorMessage(body?.detail, response.status), response.status); }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

/** Only the parameters that were actually given; an empty `to_date=` is not the same as omitting it. */
function historyQuery(range: Record<string, string | undefined>): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(range)) if (value) params.set(key, value);
  const query = params.toString();
  return query ? `?${query}` : "";
}

function brokerBookQuery({ broker, force }: { broker?: string; force?: boolean }): string {
  const parts: string[] = [];
  if (broker) parts.push(`broker=${encodeURIComponent(broker)}`);
  if (force) parts.push("force=true");
  return parts.length ? `?${parts.join("&")}` : "";
}

export const api = {
  me: () => request<User>("/auth/me"), login: (email: string, password: string) => request<{ email: string; role: string }>("/auth/login", { method: "POST", body: JSON.stringify({ email, password }) }), refresh: () => request<{ email: string; role: string }>("/auth/refresh", { method: "POST" }), logout: () => request<void>("/auth/logout", { method: "POST" }), sessions: () => request<UserSession[]>("/auth/sessions"), revokeSession: (id: string) => request<void>(`/auth/sessions/${id}`, { method: "DELETE" }), auditLogs: () => request<AuditLog[]>("/auth/audit-logs"), overview: () => request<Overview>("/system/overview"), marketSession: () => request<MarketSession>("/system/market-session"), scanner: () => request<ScannerStatus>("/scanner/status"), dataQuality: () => request<DataQuality[]>("/scanner/data-quality"), evaluations: (limit = 100) => request<ScannerEvaluation[]>(`/scanner/evaluations?limit=${limit}`), signals: () => request<PaperSignal[]>("/scanner/signals"), paperSummary: () => request<PaperExecutionSummary>("/paper/summary"), paperOrders: () => request<PaperOrder[]>("/paper/orders"), paperPositions: () => request<PaperPosition[]>("/paper/positions"), paperRiskSummary: () => request<PaperRiskSummary>("/risk/summary"), shadowOrders: () => request<ShadowOrder[]>("/shadow/orders"), shadowSummary: () => request<ShadowSummary>("/shadow/summary"), omsOrders: () => request<OmsOrder[]>("/oms/orders"), omsReconciliations: () => request<OmsReconciliation[]>("/oms/reconciliations"), runOmsReconciliation: () => request<OmsReconciliation>("/oms/reconciliations/run", { method: "POST" }), backtests: () => request<BacktestRun[]>("/backtests"), runBacktest: (input: { start_date: string; end_date: string; instrument_tokens: string[]; strategy_ids: string[]; timeframe_seconds: number }) => request<BacktestRunDetail>("/backtests/run", { method: "POST", body: JSON.stringify(input) }), sweeps: () => request<BacktestSweep[]>("/backtests/sweeps"), createSweep: (input: { strategy_id: string; start_date: string; end_date: string; instrument_tokens: string[]; validation_fraction: number; parameter_grid: Record<string, number[]> }) => request<BacktestSweep>("/backtests/sweeps", { method: "POST", body: JSON.stringify(input) }), promoteSweepCombination: (id: string, index: number) => request<PaperStrategy[]>(`/backtests/sweeps/${id}/promote?combination_index=${index}`, { method: "POST" }), candles: (instrument: string, sessionDate: string) => request<MarketCandle[]>(`/market-data/candles/${encodeURIComponent(instrument)}?session_date=${encodeURIComponent(sessionDate)}`), strategies: () => request<PaperStrategy[]>("/settings/strategies"), strategyMetrics: () => request<StrategyMetric[]>("/settings/strategies/metrics"), strategyDefinitions: () => request<StrategyDefinition[]>("/settings/strategies/definitions"), strategyDetail: (id: string) => request<StrategyDetail>(`/settings/strategies/${encodeURIComponent(id)}/detail`), updateStrategies: (items: PaperStrategy[]) => request<PaperStrategy[]>("/settings/strategies", { method: "PUT", body: JSON.stringify(items) }), startScanner: () => request<ScannerStatus>("/scanner/start", { method: "POST" }), stopScanner: () => request<ScannerStatus>("/scanner/stop", { method: "POST" }), universe: () => request<UniverseEntry[]>("/universe"), universeSummary: () => request<UniverseSummary>("/universe/summary"), refreshUniverse: () => request<UniverseSummary>("/universe/refresh", { method: "POST" }), marketRegime: () => request<MarketRegime>("/market-data/regime"), scoreAnalysis: () => request<ScoreAnalysis>("/journal/score-analysis"), controls: () => request<TradingControls>("/settings/trading"), updateControls: (controls: TradingControls) => request<TradingControls>("/settings/trading", { method: "PUT", body: JSON.stringify(controls) }), settingsCatalog: () => request<SettingsCatalog>("/settings/trading/catalog"), indicatorCatalog: () => request<IndicatorCatalog>("/settings/indicators/catalog"), updateIndicators: (indicators: IndicatorSettings) => request<IndicatorSettings>("/settings/indicators", { method: "PUT", body: JSON.stringify(indicators) }), brokerSnapshot: (options: { broker?: string; force?: boolean } = {}) => request<BrokerSnapshot>(`/broker-books/snapshot${brokerBookQuery(options)}`), brokerChoices: () => request<BrokerChoices>("/broker-books/brokers"), tradingStatus: () => request<TradingStatus>("/safety/trading-status"), notificationCatalog: () => request<NotificationCatalog>("/settings/notifications"), updateNotifications: (values: Record<string, boolean>) => request<Record<string, boolean>>("/settings/notifications", { method: "PUT", body: JSON.stringify(values) }), settingsHistory: () => request<SettingRevision[]>("/settings/trading/history"), riskPresets: () => request<RiskPreset[]>("/settings/trading/presets"), applyRiskPreset: (preset: string, confirmRiskIncrease: boolean) => request<TradingControls>(`/settings/trading/presets/${encodeURIComponent(preset)}`, { method: "POST", body: JSON.stringify({ preset, confirm_risk_increase: confirmRiskIncrease }) }),

  swingSummary: () => request<SwingSummary>("/swing/summary"),
  swingStrategies: () => request<SwingStrategy[]>("/swing/strategies"),
  setSwingStrategyMode: (id: string, mode: StrategyMode, reason: string | null) =>
    request<SwingStrategy[]>(`/swing/strategies/${encodeURIComponent(id)}/mode`, { method: "PUT", body: JSON.stringify({ mode, reason }) }),
  builderCatalog: () => request<BuilderCatalog>("/strategy-builder/catalog"),
  builderInstruments: () => request<BuilderInstrument[]>("/strategy-builder/instruments"),
  backtestDraft: (body: { draft: PaperStrategy; start_date: string; end_date: string; instrument_tokens: string[] }) => request<BacktestRunDetail>("/strategy-builder/backtest", { method: "POST", body: JSON.stringify(body) }),
  saveNewStrategy: (draft: PaperStrategy, reason: string) => request<PaperStrategy[]>("/strategy-builder/strategies", { method: "POST", body: JSON.stringify({ draft, reason: reason || null }) }),
  saveStrategy: (id: string, draft: PaperStrategy, reason: string, confirmLive = false) => request<PaperStrategy[]>(`/strategy-builder/strategies/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify({ draft, reason: reason || null, confirm_live: confirmLive }) }),
  deleteStrategy: (id: string) => request<PaperStrategy[]>(`/strategy-builder/strategies/${encodeURIComponent(id)}`, { method: "DELETE" }),
  watchlist: () => request<WatchlistView>("/watchlist"),
  searchWatchlist: (q: string) => request<WatchSearchResult[]>(`/watchlist/search?q=${encodeURIComponent(q)}`),
  saveWatchlist: (body: { instruments: string[]; benchmark: string; reason: string; confirm: boolean }) => request<WatchlistView>("/watchlist", { method: "PUT", body: JSON.stringify(body) }),
  resetWatchlist: () => request<WatchlistView>("/watchlist", { method: "DELETE" }),
  setupStatus: () => request<SetupStatus>("/setup/status"),
  createFirstAdmin: (email: string, password: string) => request<ManagedUser>("/setup/first-admin", { method: "POST", body: JSON.stringify({ email, password }) }),
  setupChecklist: () => request<ChecklistItem[]>("/setup/checklist"),
  users: () => request<ManagedUser[]>("/users"),
  addUser: (body: { email: string; password: string; role: ManagedUser["role"] }) => request<ManagedUser>("/users", { method: "POST", body: JSON.stringify(body) }),
  changeUser: (id: string, body: { role?: ManagedUser["role"]; is_active?: boolean }) => request<ManagedUser>(`/users/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify(body) }),
  resetUserPassword: (id: string, password: string) => request<ManagedUser>(`/users/${encodeURIComponent(id)}/password`, { method: "POST", body: JSON.stringify({ password }) }),
  changeOwnPassword: (current_password: string, new_password: string) => request<void>("/account/password", { method: "POST", body: JSON.stringify({ current_password, new_password }) }),
  diagnosticReports: () => request<DiagnosticReport[]>("/diagnostics/reports"),
  runDiagnosticReport: (name: string, body: { session_date: string | null; from_date: string | null; to_date: string | null; after: string | null; no_broker: boolean }) =>
    request<DiagnosticRun>(`/diagnostics/reports/${encodeURIComponent(name)}`, { method: "POST", body: JSON.stringify(body) }),
  swingJobs: () => request<SwingJobs>("/swing/jobs"),
  swingCoverage: () => request<SwingCoverage>("/swing/data/coverage"),
  startSwingBackfill: (years: number) => request<SwingJobs>("/swing/data/backfill", { method: "POST", body: JSON.stringify({ years }) }),
  startSwingBacktest: (years: number, strategies: string[]) =>
    request<SwingJobs>("/swing/backtests", { method: "POST", body: JSON.stringify({ years, strategies }) }),
  swingBacktests: () => request<SwingBacktestRun[]>("/swing/backtests"),
  corporateEvents: () => request<CorporateEvent[]>("/swing/corporate-events"),
  recordCorporateEvent: (body: { symbol: string; event_type: string; ex_date: string; price_factor: number | null; amount: number | null; note: string | null }) =>
    request<CorporateEvent>("/swing/corporate-events", { method: "POST", body: JSON.stringify(body) }),
  deleteCorporateEvent: (id: string) => request<void>(`/swing/corporate-events/${encodeURIComponent(id)}`, { method: "DELETE" }),
  connections: () => request<ConnectionsView>("/connections"),
  saveUpstoxConnection: (values: Record<string, string | boolean>, clear: string[], reason: string | null) =>
    request<ConnectionsView>("/connections/upstox", { method: "PUT", body: JSON.stringify({ values, clear, reason }) }),
  saveTelegramConnection: (body: { bot_token: string; chat_id: string; reason: string | null }) =>
    request<ConnectionsView>("/connections/telegram", { method: "PUT", body: JSON.stringify(body) }),
  checkUpstoxConnection: () => request<ConnectionCheck>("/connections/upstox/check", { method: "POST" }),
  charges: () => request<ChargesView>("/charges"),
  swingSettings: () => request<SwingSettingsView>("/settings/swing"),
  saveSwingSettings: (values: Record<string, unknown>, reason: string) =>
    request<SwingSettingsView>("/settings/swing", { method: "PUT", body: JSON.stringify({ values, reason }) }),
  saveIntradayCharges: (charges: IntradayCharges, reason: string | null) =>
    request<ChargesView>("/charges/intraday", { method: "PUT", body: JSON.stringify({ charges, reason }) }),
  saveSwingCharges: (charges: SwingCharges, reason: string | null) =>
    request<ChargesView>("/charges/swing", { method: "PUT", body: JSON.stringify({ charges, reason }) }),
  previewCharges: (body: { intraday?: IntradayCharges; swing?: SwingCharges }) =>
    request<{ intraday: RoundTrip | null; swing: RoundTrip | null }>("/charges/preview", { method: "POST", body: JSON.stringify(body) }),
  setStrategyMode: (id: string, mode: StrategyMode, confirmLive: boolean, reason: string | null) =>
    request<PaperStrategy[]>(`/settings/strategies/${encodeURIComponent(id)}/mode`, { method: "PUT", body: JSON.stringify({ mode, confirm_live: confirmLive, reason }) }),
  swingHistory: () => request<SwingPosition[]>("/swing/history"),
  paperDay: (filter: { strategy?: string }) => request<PaperDay>(`/paper/day${historyQuery(filter)}`),
  historyStrategies: (range: HistoryRange) => request<StrategyResult[]>(`/history/strategies${historyQuery(range)}`),
  historyOverview: (range: HistoryRange) => request<HistoryOverview>(`/history/overview${historyQuery(range)}`),
  historyDaily: (range: HistoryRange) => request<HistoryDay[]>(`/history/daily${historyQuery(range)}`),
  historyQuality: (range: HistoryRange) => request<ExecutionQuality>(`/history/quality${historyQuery(range)}`),
  historyBrokerTrades: (range: HistoryRange) =>
    request<BrokerTrade[]>(`/history/broker-trades${historyQuery(range)}`),
  historyTrades: (range: HistoryRange & { session_date?: string; instrument_token?: string; strategy_version?: string }) =>
    request<HistoryTrade[]>(`/history/trades${historyQuery(range)}`),
  historyTrade: (positionId: string) => request<HistoryTradeDetail>(`/history/trades/${encodeURIComponent(positionId)}`),
  historyExportUrl: (kind: "csv" | "xlsx", range: HistoryRange) => `/api/v1/history/export.${kind}${historyQuery(range)}`,
  fetchBrokerFigures: (sessionDate: string) =>
    request<BrokerFetchResult>(`/history/broker-figures/${encodeURIComponent(sessionDate)}`, { method: "POST" }),
  safety: () => request<SafetyStatus>("/safety/status"), enablePaper: () => request<SafetyStatus>("/safety/paper/enable", { method: "POST" }), disablePaper: () => request<SafetyStatus>("/safety/paper/disable", { method: "POST" }), emergencyStop: (reason: string) => request<SafetyStatus>("/safety/emergency-stop", { method: "POST", body: JSON.stringify({ reason }) }), clearEmergencyStop: () => request<SafetyStatus>("/safety/emergency-stop/clear", { method: "POST" }), liveReadiness: () => request<LiveReadiness>("/live/readiness"), verifyLiveReadiness: () => request<LiveReadiness>("/live/readiness/verify", { method: "POST" }), liveReadinessHistory: () => request<LiveReadinessHistory[]>("/live/readiness/history"), liveActivation: () => request<LiveActivation>("/live-shadow/activation"), armLive: (reason: string) => request<LiveActivation>("/live-shadow/activation", { method: "POST", body: JSON.stringify({ reason }) }), disarmLive: () => request<LiveActivation>("/live-shadow/activation", { method: "DELETE" }), reconcileLive: () => request<LiveReconciliation>("/live-shadow/reconcile", { method: "POST" }), telegram: () => request<TelegramStatus>("/telegram/status"), testTelegram: () => request<TelegramStatus>("/telegram/test", { method: "POST" }), registerTelegramWebhook: () => request<TelegramStatus>("/telegram/webhook/register", { method: "POST" }), brokerControls: () => request<BrokerControls>("/market-data/brokers"), updateBrokerControls: (controls: BrokerControls) => request<BrokerControls>("/market-data/brokers", { method: "PUT", body: JSON.stringify(controls) }), startUpstoxOAuth: () => request<UpstoxOAuthStart>("/market-data/upstox/authorize", { method: "POST" }),
  completeUpstoxOAuth: (code: string, state: string) => request<{ status: string; expires_at: string }>(`/market-data/upstox/callback?code=${encodeURIComponent(code)}&state=${encodeURIComponent(state)}`),
  autoAuthStatus: () => request<AutoAuthStatus>("/market-data/upstox/auto-auth/status"),
  triggerAutoAuth: () => request<AutoAuthTriggerResponse>("/market-data/upstox/auto-auth/trigger", { method: "POST" }),
  refreshUpstoxInstruments: () => request<InstrumentRefreshResponse>("/market-data/upstox/instruments/refresh", { method: "POST" }),
  firstockStatus: () => request<FirstockBrokerStatus>("/broker/firstock/status"),
  testFirstock: () => request<FirstockBrokerStatus>("/broker/firstock/test", { method: "POST" }),
};
