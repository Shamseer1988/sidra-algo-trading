export type User = { email: string; role: "ADMIN" | "TRADER" | "VIEWER"; is_active: boolean };
export type ServiceStatus = { status: string; detail: string; checked_at: string };
export type Overview = { mode: string; live_trading_enabled: boolean; api: ServiceStatus; database: ServiceStatus; redis: ServiceStatus; scanner: ServiceStatus; market_data: ServiceStatus; firstock: ServiceStatus; telegram: ServiceStatus };
export type ScannerStatus = { status: string; last_heartbeat: string | null; detail: string };
export type PaperSignal = { id: string; instrument_token: string; session_date: string; candle_opened_at: string; side: "LONG" | "SHORT"; status: string; entry_price: number; stop_price: number; target_price: number; quantity: number; score: number; score_breakdown: Record<string, number>; created_at: string };
export type MarketCandle = { opened_at: string; closed_at: string; open: number; high: number; low: number; close: number; volume: number };
export type TradingControls = { account_capital: number; risk_per_trade_percent: number; maximum_daily_risk_percent: number; maximum_signals: number; minimum_score: number; minimum_rr: number; volume_multiplier: number; retest_tolerance_percent: number; minimum_ema_spread_percent: number; trade_start_time: string; trade_cutoff_time: string };
export type PaperStrategy = { id: string; name: string; enabled: boolean; strategy_type: string; minimum_score: number; minimum_rr: number; volume_multiplier: number; retest_tolerance_percent: number; minimum_ema_spread_percent: number };
export type SafetyStatus = { paper_tracking_enabled: boolean; live_trading_enabled: boolean; live_execution_available: boolean; emergency_stop_active: boolean; emergency_stop_reason: string | null; emergency_stop_source: string | null; emergency_stop_at: string | null };
export type TelegramStatus = { configured: boolean; webhook_configured: boolean; inbound_enabled: boolean; detail: string };
export type BrokerControls = { upstox_paper_enabled: boolean; firstock_feed_enabled: boolean };
export type UpstoxOAuthStart = { authorization_url: string };
export type UserSession = { id: string; created_at: string; expires_at: string; ip_address: string | null; user_agent: string | null };
export type AuditLog = { id: string; event_type: string; created_at: string; user_id: string | null; ip_address: string | null; message: string | null; metadata_json: Record<string, unknown> };

function csrfToken(): string | undefined {
  return document.cookie.split("; ").find((value) => value.startsWith("csrf_token="))?.split("=")[1];
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const method = init?.method?.toUpperCase() ?? "GET";
  const csrf = ["POST", "PUT", "PATCH", "DELETE"].includes(method) && path !== "/auth/login" ? csrfToken() : undefined;
  const response = await fetch(`/api/v1${path}`, { ...init, credentials: "include", headers: { "Content-Type": "application/json", ...(csrf ? { "X-CSRF-Token": decodeURIComponent(csrf) } : {}), ...(init?.headers ?? {}) } });
  if (!response.ok) { const body = await response.json().catch(() => null) as { detail?: string } | null; throw new Error(body?.detail ?? "Request failed"); }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export const api = {
  me: () => request<User>("/auth/me"), login: (email: string, password: string) => request<{ email: string; role: string }>("/auth/login", { method: "POST", body: JSON.stringify({ email, password }) }), refresh: () => request<{ email: string; role: string }>("/auth/refresh", { method: "POST" }), logout: () => request<void>("/auth/logout", { method: "POST" }), sessions: () => request<UserSession[]>("/auth/sessions"), revokeSession: (id: string) => request<void>(`/auth/sessions/${id}`, { method: "DELETE" }), auditLogs: () => request<AuditLog[]>("/auth/audit-logs"), overview: () => request<Overview>("/system/overview"), scanner: () => request<ScannerStatus>("/scanner/status"), signals: () => request<PaperSignal[]>("/scanner/signals"), candles: (instrument: string, sessionDate: string) => request<MarketCandle[]>(`/market-data/candles/${encodeURIComponent(instrument)}?session_date=${encodeURIComponent(sessionDate)}`), strategies: () => request<PaperStrategy[]>("/settings/strategies"), updateStrategies: (items: PaperStrategy[]) => request<PaperStrategy[]>("/settings/strategies", { method: "PUT", body: JSON.stringify(items) }), startScanner: () => request<ScannerStatus>("/scanner/start", { method: "POST" }), stopScanner: () => request<ScannerStatus>("/scanner/stop", { method: "POST" }), controls: () => request<TradingControls>("/settings/trading"), updateControls: (controls: TradingControls) => request<TradingControls>("/settings/trading", { method: "PUT", body: JSON.stringify(controls) }),
  safety: () => request<SafetyStatus>("/safety/status"), enablePaper: () => request<SafetyStatus>("/safety/paper/enable", { method: "POST" }), disablePaper: () => request<SafetyStatus>("/safety/paper/disable", { method: "POST" }), emergencyStop: (reason: string) => request<SafetyStatus>("/safety/emergency-stop", { method: "POST", body: JSON.stringify({ reason }) }), clearEmergencyStop: () => request<SafetyStatus>("/safety/emergency-stop/clear", { method: "POST" }), telegram: () => request<TelegramStatus>("/telegram/status"), testTelegram: () => request<TelegramStatus>("/telegram/test", { method: "POST" }), brokerControls: () => request<BrokerControls>("/market-data/brokers"), updateBrokerControls: (controls: BrokerControls) => request<BrokerControls>("/market-data/brokers", { method: "PUT", body: JSON.stringify(controls) }), startUpstoxOAuth: () => request<UpstoxOAuthStart>("/market-data/upstox/authorize", { method: "POST" }),
  completeUpstoxOAuth: (code: string, state: string) => request<{ status: string; expires_at: string }>(`/market-data/upstox/callback?code=${encodeURIComponent(code)}&state=${encodeURIComponent(state)}`),
};
