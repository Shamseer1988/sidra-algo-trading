export type User = { email: string; role: "ADMIN" | "TRADER" | "VIEWER"; is_active: boolean };
export type ServiceStatus = { status: string; detail: string; checked_at: string };
export type Overview = { mode: string; live_trading_enabled: boolean; api: ServiceStatus; database: ServiceStatus; redis: ServiceStatus; scanner: ServiceStatus; market_data: ServiceStatus; firstock: ServiceStatus; telegram: ServiceStatus };
export type ScannerStatus = { status: string; last_heartbeat: string | null; detail: string; worker_restart_count: number };
export type MarketSession = { phase: string; trading_day: boolean; reason: string; local_timestamp: string; session_date: string | null; regular_open: string | null; regular_close: string | null; is_special_session: boolean };
export type DataQuality = { instrument_token: string; state: "GOOD" | "DEGRADED" | "STALE" | "INVALID"; reason: string; session_date: string; expected_bars: number; received_bars: number; missing_buckets: string[]; received_ticks: number; duplicate_ticks: number; out_of_order_ticks: number; invalid_ticks: number; average_latency_ms: number; max_latency_ms: number; last_exchange_timestamp: string | null; last_received_timestamp: string | null; observed_at: string; allows_signals: boolean };
export type ScannerEvaluation = { id: string; instrument_token: string; session_date: string; candle_opened_at: string; strategy_id: string; strategy_name: string; strategy_version: number; status: "ACCEPTED" | "WATCHING" | "REJECTED"; decision_state: string; side: "LONG" | "SHORT" | null; reason: string; failed_conditions: string[]; data_quality_state: "GOOD" | "DEGRADED" | "STALE" | "INVALID" | "MISSING"; candle_close: number; candle_volume: number; score: number; score_breakdown: Record<string, number>; indicator_snapshot: Record<string, unknown>; entry_price: number | null; stop_price: number | null; target_price: number | null; quantity: number | null; risk_amount: number | null; created_at: string };
export type PaperSignal = { id: string; instrument_token: string; session_date: string; candle_opened_at: string; side: "LONG" | "SHORT"; status: string; entry_price: number; stop_price: number; target_price: number; quantity: number; score: number; score_breakdown: Record<string, number>; created_at: string };
export type PaperOrder = { id: string; paper_signal_id: string; client_order_id: string; instrument_token: string; session_date: string; side: "BUY" | "SELL"; order_type: string; order_role: string; status: string; quantity: number; filled_quantity: number; average_fill_price: number | null; limit_price: number | null; stop_price: number | null; fee_total: number; eligible_after: string; rejection_reason: string | null; created_at: string };
export type PaperPosition = { id: string; paper_signal_id: string; instrument_token: string; session_date: string; strategy_version: string; side: "LONG" | "SHORT"; status: string; initial_quantity: number; open_quantity: number; average_entry_price: number | null; average_exit_price: number | null; current_price: number | null; stop_price: number; target_price: number; realized_pnl: number; unrealized_pnl: number; fees_total: number; total_pnl: number; opened_at: string | null; closed_at: string | null };
export type PaperExecutionSummary = { orders: number; pending_orders: number; fills: number; open_positions: number; realized_pnl: number; unrealized_pnl: number; total_pnl: number; fees_total: number };
export type PaperRiskSummary = { session_date: string; daily_risk_limit: number; daily_risk_allocated: number; daily_risk_available: number; maximum_open_positions: number; active_reservations: number; open_positions: number; exposure_limit: number; current_exposure: number; exposure_available: number; rejected_reservations: number };
export type BacktestStrategyMetric = { strategy_id: string; strategy_name: string; strategy_version: number; trades: number; winners: number; losers: number; win_rate: number; net_pnl: number; profit_factor: number | null };
export type BacktestSummary = { trades: number; winners: number; losers: number; win_rate: number; net_pnl: number; profit_factor: number | null; initial_capital: number; final_equity: number; return_percent: number; max_drawdown: number; equity_curve: { at: string | null; equity: number; drawdown: number }[]; strategy_comparison: BacktestStrategyMetric[] };
export type BacktestRun = { id: string; status: string; start_date: string; end_date: string; timeframe_seconds: number; instrument_tokens: string[]; source_candle_count: number; data_fingerprint: string; initial_capital: number; final_equity: number | null; net_pnl: number | null; max_drawdown: number | null; summary: BacktestSummary; failure_detail: string | null; created_at: string };
export type BacktestTrade = { id: string; strategy_id: string; strategy_name: string; strategy_version: number; instrument_token: string; session_date: string; side: string; quantity: number; signal_at: string; entered_at: string; exited_at: string; entry_price: number; exit_price: number; gross_pnl: number; fees_total: number; net_pnl: number; realized_r: number; exit_reason: string };
export type BacktestRunDetail = BacktestRun & { trades: BacktestTrade[] };
export type OmsOrder = { id: string; intent_id: string; idempotency_key: string; instrument_token: string; side: string; quantity: number; mode: string; venue: string; status: string; filled_quantity: number; average_fill_price: number | null; unknown_since: string | null; created_at: string; updated_at: string };
export type OmsReconciliation = { id: string; mode: string; status: string; internal_orders: number; external_orders: number; unknown_orders: number; detail: string; created_at: string };
export type ShadowOrder = { id: string; instrument_token: string; side: string; intended_quantity: number; intended_price: number; comparison_status: string; paper_fill_price: number | null; price_delta: number | null; observed_at: string; compared_at: string | null };
export type ShadowSummary = { intended_orders: number; compared_orders: number; awaiting_paper_fill: number; average_price_delta: number; broker_submissions: number };
export type AssistedApproval = { reference_id: string; decision: string; source: string; status: string; expires_at: string | null; decided_at: string | null; risk_revalidated_at: string | null; submission_block_reason: string | null; created_at: string };
export type MarketCandle = { opened_at: string; closed_at: string; open: number; high: number; low: number; close: number; volume: number };
export type TradingControls = { account_capital: number; risk_per_trade_percent: number; maximum_daily_risk_percent: number; maximum_open_positions: number; maximum_open_exposure_percent: number; maximum_signals: number; minimum_score: number; minimum_rr: number; volume_multiplier: number; retest_tolerance_percent: number; minimum_ema_spread_percent: number; trade_start_time: string; trade_cutoff_time: string };
export type PaperStrategy = { id: string; name: string; enabled: boolean; strategy_type: string; version: number; universe: string[]; allowed_sides: string[]; allowed_sessions: string[]; max_trades_per_day: number; cooldown_minutes: number; risk_per_trade_percent: number | null; minimum_score: number; minimum_rr: number; volume_multiplier: number; retest_tolerance_percent: number; minimum_ema_spread_percent: number };
export type StrategyMetric = { strategy_id: string; strategy_name: string; strategy_version: number; evaluations: number; accepted: number; rejected: number; watching: number; acceptance_rate: number };
export type SafetyStatus = { paper_tracking_enabled: boolean; live_trading_enabled: boolean; live_execution_available: boolean; emergency_stop_active: boolean; emergency_stop_reason: string | null; emergency_stop_source: string | null; emergency_stop_at: string | null };
export type TelegramStatus = { configured: boolean; webhook_configured: boolean; inbound_enabled: boolean; detail: string };
export type BrokerControls = { upstox_paper_enabled: boolean; firstock_feed_enabled: boolean };
export type UpstoxOAuthStart = { authorization_url: string };
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

async function request<T>(path: string, init?: RequestInit, allowRefresh = true): Promise<T> {
  const method = init?.method?.toUpperCase() ?? "GET";
  const csrf = ["POST", "PUT", "PATCH", "DELETE"].includes(method) && path !== "/auth/login" ? csrfToken() : undefined;
  const response = await fetch(`/api/v1${path}`, { ...init, credentials: "include", headers: { "Content-Type": "application/json", ...(csrf ? { "X-CSRF-Token": decodeURIComponent(csrf) } : {}), ...(init?.headers ?? {}) } });
  if (response.status === 401 && allowRefresh && !["/auth/login", "/auth/refresh"].includes(path)) {
    if (await refreshAccessToken()) return request<T>(path, init, false);
  }
  if (!response.ok) { const body = await response.json().catch(() => null) as { detail?: string } | null; throw new ApiError(body?.detail ?? "Request failed", response.status); }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export const api = {
  me: () => request<User>("/auth/me"), login: (email: string, password: string) => request<{ email: string; role: string }>("/auth/login", { method: "POST", body: JSON.stringify({ email, password }) }), refresh: () => request<{ email: string; role: string }>("/auth/refresh", { method: "POST" }), logout: () => request<void>("/auth/logout", { method: "POST" }), sessions: () => request<UserSession[]>("/auth/sessions"), revokeSession: (id: string) => request<void>(`/auth/sessions/${id}`, { method: "DELETE" }), auditLogs: () => request<AuditLog[]>("/auth/audit-logs"), overview: () => request<Overview>("/system/overview"), marketSession: () => request<MarketSession>("/system/market-session"), scanner: () => request<ScannerStatus>("/scanner/status"), dataQuality: () => request<DataQuality[]>("/scanner/data-quality"), evaluations: (limit = 100) => request<ScannerEvaluation[]>(`/scanner/evaluations?limit=${limit}`), signals: () => request<PaperSignal[]>("/scanner/signals"), paperSummary: () => request<PaperExecutionSummary>("/paper/summary"), paperOrders: () => request<PaperOrder[]>("/paper/orders"), paperPositions: () => request<PaperPosition[]>("/paper/positions"), paperRiskSummary: () => request<PaperRiskSummary>("/risk/summary"), shadowOrders: () => request<ShadowOrder[]>("/shadow/orders"), shadowSummary: () => request<ShadowSummary>("/shadow/summary"), omsOrders: () => request<OmsOrder[]>("/oms/orders"), omsReconciliations: () => request<OmsReconciliation[]>("/oms/reconciliations"), runOmsReconciliation: () => request<OmsReconciliation>("/oms/reconciliations/run", { method: "POST" }), backtests: () => request<BacktestRun[]>("/backtests"), runBacktest: (input: { start_date: string; end_date: string; instrument_tokens: string[]; strategy_ids: string[]; timeframe_seconds: number }) => request<BacktestRunDetail>("/backtests/run", { method: "POST", body: JSON.stringify(input) }), candles: (instrument: string, sessionDate: string) => request<MarketCandle[]>(`/market-data/candles/${encodeURIComponent(instrument)}?session_date=${encodeURIComponent(sessionDate)}`), strategies: () => request<PaperStrategy[]>("/settings/strategies"), strategyMetrics: () => request<StrategyMetric[]>("/settings/strategies/metrics"), updateStrategies: (items: PaperStrategy[]) => request<PaperStrategy[]>("/settings/strategies", { method: "PUT", body: JSON.stringify(items) }), startScanner: () => request<ScannerStatus>("/scanner/start", { method: "POST" }), stopScanner: () => request<ScannerStatus>("/scanner/stop", { method: "POST" }), controls: () => request<TradingControls>("/settings/trading"), updateControls: (controls: TradingControls) => request<TradingControls>("/settings/trading", { method: "PUT", body: JSON.stringify(controls) }),
  safety: () => request<SafetyStatus>("/safety/status"), enablePaper: () => request<SafetyStatus>("/safety/paper/enable", { method: "POST" }), disablePaper: () => request<SafetyStatus>("/safety/paper/disable", { method: "POST" }), emergencyStop: (reason: string) => request<SafetyStatus>("/safety/emergency-stop", { method: "POST", body: JSON.stringify({ reason }) }), clearEmergencyStop: () => request<SafetyStatus>("/safety/emergency-stop/clear", { method: "POST" }), assistedApprovals: () => request<AssistedApproval[]>("/assisted/approvals"), decideAssistedApproval: (referenceId: string, decision: "APPROVE" | "REJECT") => request<AssistedApproval>(`/assisted/approvals/${encodeURIComponent(referenceId)}/decision`, { method: "POST", body: JSON.stringify({ decision }) }), telegram: () => request<TelegramStatus>("/telegram/status"), testTelegram: () => request<TelegramStatus>("/telegram/test", { method: "POST" }), brokerControls: () => request<BrokerControls>("/market-data/brokers"), updateBrokerControls: (controls: BrokerControls) => request<BrokerControls>("/market-data/brokers", { method: "PUT", body: JSON.stringify(controls) }), startUpstoxOAuth: () => request<UpstoxOAuthStart>("/market-data/upstox/authorize", { method: "POST" }),
  completeUpstoxOAuth: (code: string, state: string) => request<{ status: string; expires_at: string }>(`/market-data/upstox/callback?code=${encodeURIComponent(code)}&state=${encodeURIComponent(state)}`),
};
