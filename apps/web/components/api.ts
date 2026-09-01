export type User = { email: string; role: "ADMIN" | "TRADER" | "VIEWER"; is_active: boolean };
export type ServiceStatus = { status: string; detail: string; checked_at: string };
export type Overview = { mode: string; live_trading_enabled: boolean; api: ServiceStatus; database: ServiceStatus; redis: ServiceStatus; scanner: ServiceStatus; market_data: ServiceStatus; firstock: ServiceStatus; telegram: ServiceStatus };
export type ScannerStatus = { status: string; last_heartbeat: string | null; detail: string; worker_restart_count: number };
export type MarketSession = { phase: string; trading_day: boolean; reason: string; local_timestamp: string; session_date: string | null; regular_open: string | null; regular_close: string | null; is_special_session: boolean };
export type DataQuality = { instrument_token: string; state: "GOOD" | "DEGRADED" | "STALE" | "INVALID"; reason: string; session_date: string; expected_bars: number; received_bars: number; missing_buckets: string[]; received_ticks: number; duplicate_ticks: number; out_of_order_ticks: number; invalid_ticks: number; average_latency_ms: number; max_latency_ms: number; last_exchange_timestamp: string | null; last_received_timestamp: string | null; observed_at: string; allows_signals: boolean };
export type ScannerEvaluation = { id: string; instrument_token: string; session_date: string; candle_opened_at: string; strategy_id: string; strategy_name: string; strategy_version: number; status: "ACCEPTED" | "WATCHING" | "REJECTED"; decision_state: string; side: "LONG" | "SHORT" | null; reason: string; failed_conditions: string[]; data_quality_state: "GOOD" | "DEGRADED" | "STALE" | "INVALID" | "MISSING"; candle_close: number; candle_volume: number; score: number; score_breakdown: Record<string, number>; indicator_snapshot: Record<string, unknown>; entry_price: number | null; stop_price: number | null; target_price: number | null; quantity: number | null; risk_amount: number | null; created_at: string };
export type PaperSignal = { id: string; instrument_token: string; session_date: string; candle_opened_at: string; side: "LONG" | "SHORT"; status: string; entry_price: number; stop_price: number; target_price: number; quantity: number; score: number; score_breakdown: Record<string, number>; created_at: string };
export type MarketCandle = { opened_at: string; closed_at: string; open: number; high: number; low: number; close: number; volume: number };
export type TradingControls = { account_capital: number; risk_per_trade_percent: number; maximum_daily_risk_percent: number; maximum_signals: number; minimum_score: number; minimum_rr: number; volume_multiplier: number; retest_tolerance_percent: number; minimum_ema_spread_percent: number; trade_start_time: string; trade_cutoff_time: string };
export type PaperStrategy = { id: string; name: string; enabled: boolean; strategy_type: string; version: number; minimum_score: number; minimum_rr: number; volume_multiplier: number; retest_tolerance_percent: number; minimum_ema_spread_percent: number };
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
  me: () => request<User>("/auth/me"), login: (email: string, password: string) => request<{ email: string; role: string }>("/auth/login", { method: "POST", body: JSON.stringify({ email, password }) }), refresh: () => request<{ email: string; role: string }>("/auth/refresh", { method: "POST" }), logout: () => request<void>("/auth/logout", { method: "POST" }), sessions: () => request<UserSession[]>("/auth/sessions"), revokeSession: (id: string) => request<void>(`/auth/sessions/${id}`, { method: "DELETE" }), auditLogs: () => request<AuditLog[]>("/auth/audit-logs"), overview: () => request<Overview>("/system/overview"), marketSession: () => request<MarketSession>("/system/market-session"), scanner: () => request<ScannerStatus>("/scanner/status"), dataQuality: () => request<DataQuality[]>("/scanner/data-quality"), evaluations: (limit = 100) => request<ScannerEvaluation[]>(`/scanner/evaluations?limit=${limit}`), signals: () => request<PaperSignal[]>("/scanner/signals"), candles: (instrument: string, sessionDate: string) => request<MarketCandle[]>(`/market-data/candles/${encodeURIComponent(instrument)}?session_date=${encodeURIComponent(sessionDate)}`), strategies: () => request<PaperStrategy[]>("/settings/strategies"), updateStrategies: (items: PaperStrategy[]) => request<PaperStrategy[]>("/settings/strategies", { method: "PUT", body: JSON.stringify(items) }), startScanner: () => request<ScannerStatus>("/scanner/start", { method: "POST" }), stopScanner: () => request<ScannerStatus>("/scanner/stop", { method: "POST" }), controls: () => request<TradingControls>("/settings/trading"), updateControls: (controls: TradingControls) => request<TradingControls>("/settings/trading", { method: "PUT", body: JSON.stringify(controls) }),
  safety: () => request<SafetyStatus>("/safety/status"), enablePaper: () => request<SafetyStatus>("/safety/paper/enable", { method: "POST" }), disablePaper: () => request<SafetyStatus>("/safety/paper/disable", { method: "POST" }), emergencyStop: (reason: string) => request<SafetyStatus>("/safety/emergency-stop", { method: "POST", body: JSON.stringify({ reason }) }), clearEmergencyStop: () => request<SafetyStatus>("/safety/emergency-stop/clear", { method: "POST" }), telegram: () => request<TelegramStatus>("/telegram/status"), testTelegram: () => request<TelegramStatus>("/telegram/test", { method: "POST" }), brokerControls: () => request<BrokerControls>("/market-data/brokers"), updateBrokerControls: (controls: BrokerControls) => request<BrokerControls>("/market-data/brokers", { method: "PUT", body: JSON.stringify(controls) }), startUpstoxOAuth: () => request<UpstoxOAuthStart>("/market-data/upstox/authorize", { method: "POST" }),
  completeUpstoxOAuth: (code: string, state: string) => request<{ status: string; expires_at: string }>(`/market-data/upstox/callback?code=${encodeURIComponent(code)}&state=${encodeURIComponent(state)}`),
};
