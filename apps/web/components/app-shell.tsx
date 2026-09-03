"use client";

import { type FormEvent, type ReactNode, useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { Activity, BellRing, Database, Radio, RefreshCw, Wifi } from "lucide-react";

import { api, ApiError, type DataQuality, type MarketRegime, type MarketSession, type Overview, type PaperSignal, type SafetyStatus, type ScannerStatus, type TelegramStatus, type TradingControls, type User } from "./api";
import { TerminalHeader } from "./layout/terminal-header";
import { TerminalSidebar } from "./layout/terminal-sidebar";
import { UnavailableWorkspace } from "../features/common/unavailable-workspace";
import { ControlPanel } from "../features/controls/control-panel";
import { Dashboard } from "../features/dashboard/dashboard";
import { JournalPanel } from "../features/journal/journal-panel";
import { MarketPanel } from "../features/market/market-panel";
import { PaperExecutionPanel } from "../features/paper/paper-execution-panel";
import { OmsWorkspace } from "../features/oms/oms-workspace";
import { ShadowWorkspace } from "../features/shadow/shadow-workspace";
import { AssistedTradingWorkspace } from "../features/assisted/assisted-trading-workspace";
import { LiveReadinessWorkspace } from "../features/live/live-readiness-workspace";
import { RiskCenter } from "../features/risk/risk-center";
import { BacktestingWorkspace } from "../features/backtesting/backtesting-workspace";
import { ScannerPanel } from "../features/scanner/scanner-panel";
import { UniverseWorkspace } from "../features/universe/universe-workspace";
import { SecurityPanel, SettingsPanel } from "../features/settings/settings-panel";
import { UpstoxConsole } from "../features/brokers/upstox-console";
import { FirstockConsole } from "../features/brokers/firstock-console";
import { SchedulerPanel } from "../features/automation/scheduler-panel";
import { SignalsPanel } from "../features/signals/signals-panel";
import { StrategiesPanel } from "../features/strategies/strategies-panel";
import { SystemHealthPanel } from "../features/system/system-health-panel";
import type { WorkspaceId } from "../lib/navigation";
import { CheckCircle2, XCircle, Info, X } from "lucide-react";

type ToastKind = "success" | "error" | "info";
type Toast = { id: number; text: string; kind: ToastKind };

const ERROR_PATTERN =
  /\b(fail(?:ed|ure)?|error|could ?n[o']t|cannot|can[o']t|unable|unavailable|invalid|denied|rejected|not found|temporarily|no .{1,30} available)\b/i;

function classifyToast(text: string): ToastKind {
  return ERROR_PATTERN.test(text) ? "error" : "success";
}

export function AppShell() {
  const router = useRouter();
  const [active, setActive] = useState<WorkspaceId>("overview");
  const [menuOpen, setMenuOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const [loading, setLoading] = useState(true);
  const [toasts, setToasts] = useState<Toast[]>([]);

  const dismissToast = useCallback((id: number) => setToasts((current) => current.filter((item) => item.id !== id)), []);
  const setMessage = useCallback(
    (text: string, kind?: ToastKind) => {
      if (!text) return;
      const id = Date.now() + Math.random();
      setToasts((current) => [...current.slice(-3), { id, text, kind: kind ?? classifyToast(text) }]);
      window.setTimeout(() => dismissToast(id), 5000);
    },
    [dismissToast],
  );
  const [user, setUser] = useState<User | null>(null);
  const [overview, setOverview] = useState<Overview | null>(null);
  const [scanner, setScanner] = useState<ScannerStatus | null>(null);
  const [safety, setSafety] = useState<SafetyStatus | null>(null);
  const [telegram, setTelegram] = useState<TelegramStatus | null>(null);
  const [controls, setControls] = useState<TradingControls | null>(null);
  const [signals, setSignals] = useState<PaperSignal[]>([]);
  const [marketSession, setMarketSession] = useState<MarketSession | null>(null);
  const [dataQuality, setDataQuality] = useState<DataQuality[]>([]);
  const [regime, setRegime] = useState<MarketRegime | null>(null);
  const [scannerRevision, setScannerRevision] = useState(0);

  const load = useCallback(async () => {
    try {
      const [nextUser, nextOverview, nextScanner, nextSafety, nextTelegram, nextControls, nextSignals] = await Promise.all([api.me(), api.overview(), api.scanner(), api.safety(), api.telegram(), api.controls(), api.signals()]);
      setUser(nextUser); setOverview(nextOverview); setScanner(nextScanner); setSafety(nextSafety); setTelegram(nextTelegram); setControls(nextControls); setSignals(nextSignals);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) router.replace("/login");
      else setMessage(error instanceof Error ? error.message : "Terminal data is temporarily unavailable");
    } finally { setLoading(false); }
  }, [router, setMessage]);

  const loadMarketState = useCallback(async () => {
    const [sessionResult, qualityResult, regimeResult] = await Promise.allSettled([api.marketSession(), api.dataQuality(), api.marketRegime()]);
    if (sessionResult.status === "fulfilled") setMarketSession(sessionResult.value);
    if (qualityResult.status === "fulfilled") setDataQuality(qualityResult.value);
    if (regimeResult.status === "fulfilled") setRegime(regimeResult.value);
  }, []);

  useEffect(() => { void load(); const interval = window.setInterval(() => void load(), 15000); return () => window.clearInterval(interval); }, [load]);
  useEffect(() => { if (user) void loadMarketState(); }, [loadMarketState, user]);
  useEffect(() => {
    if (!user) return;
    const protocol = window.location.protocol === "https:" ? "wss" : "ws";
    const socket = new WebSocket(`${protocol}://${window.location.host}/api/v1/events/scanner`);
    socket.onmessage = (event) => { try { const payload = JSON.parse(event.data) as { type?: string }; if (payload.type === "paper_signal" || payload.type === "scanner_evaluation") { setScannerRevision((value) => value + 1); void load(); } } catch { /* polling remains the safe fallback */ } };
    return () => socket.close();
  }, [load, user]);

  const canOperate = user?.role === "ADMIN" || user?.role === "TRADER";
  const isAdmin = user?.role === "ADMIN";
  const services = useMemo(() => overview && scanner ? [
    ["Application API", overview.api, Wifi], ["PostgreSQL", overview.database, Database], ["Redis", overview.redis, Activity], ["Scanner", { status: scanner.status, detail: scanner.detail }, Radio], ["Market data", overview.market_data, Radio], ["Firstock", overview.firstock, Radio], ["Telegram", overview.telegram, BellRing],
  ] as const : [], [overview, scanner]);

  const selectWorkspace = (workspace: WorkspaceId) => { setActive(workspace); setMenuOpen(false); if (workspace === "market") void loadMarketState(); };
  async function scannerAction(action: "start" | "stop") { try { setScanner(action === "start" ? await api.startScanner() : await api.stopScanner()); setMessage(`Scanner ${action === "start" ? "start requested" : "stopped"}.`); void load(); } catch (error) { setMessage(error instanceof Error ? error.message : "Scanner control failed"); } }
  async function emergencyAction(clear = false) { try { setSafety(clear ? await api.clearEmergencyStop() : await api.emergencyStop("Emergency stop engaged from trading terminal")); setMessage(clear ? "Emergency stop cleared." : "Emergency stop engaged; scanner stopped.", clear ? "success" : "info"); void load(); } catch (error) { setMessage(error instanceof Error ? error.message : "Safety action failed"); } }
  async function paperAction() { try { if (safety) setSafety(safety.paper_tracking_enabled ? await api.disablePaper() : await api.enablePaper()); setMessage("Paper-tracking setting updated."); } catch (error) { setMessage(error instanceof Error ? error.message : "Paper setting failed"); } }
  async function telegramAction() { try { setTelegram(await api.testTelegram()); setMessage("Telegram test alert sent."); } catch (error) { setMessage(error instanceof Error ? error.message : "Telegram test failed"); } }
  async function saveControls(event: FormEvent<HTMLFormElement>) { event.preventDefault(); if (!controls) return; try { setControls(await api.updateControls(controls)); setMessage("Trading controls saved."); } catch (error) { setMessage(error instanceof Error ? error.message : "Settings save failed"); } }
  function updateControl(key: keyof TradingControls, value: string) {
    if (!controls) return;
    if (key === "intraday_leverage_enabled") {
      setControls({ ...controls, intraday_leverage_enabled: value === "true" });
      return;
    }
    const numeric = ["account_capital", "risk_per_trade_percent", "maximum_daily_risk_percent", "maximum_open_positions", "maximum_open_exposure_percent", "maximum_signals", "minimum_score", "minimum_rr", "volume_multiplier", "retest_tolerance_percent", "minimum_ema_spread_percent", "stop_atr_multiple", "min_stop_distance_percent", "intraday_leverage_multiplier"].includes(key);
    setControls({ ...controls, [key]: numeric ? Number(value) : value });
  }
  async function signOut() { await api.logout(); router.replace("/login"); router.refresh(); }

  if (loading) return <main className="grid min-h-screen place-items-center bg-terminal-950 text-sm text-slate-400"><RefreshCw className="mr-2 h-4 w-4 animate-spin" />Loading protected terminal…</main>;
  if (!user || !overview || !scanner || !safety || !telegram || !controls) return null;

  const refreshAll = () => { void load(); void loadMarketState(); };
  const controlsPanel = <ControlPanel safety={safety} telegram={telegram} canOperate={Boolean(canOperate)} isAdmin={Boolean(isAdmin)} onEmergency={() => void emergencyAction()} onClear={() => void emergencyAction(true)} onPaper={() => void paperAction()} onTelegram={() => void telegramAction()} />;
  let content: ReactNode;
  switch (active) {
    case "overview": content = <Dashboard services={services} scanner={scanner} safety={safety} signals={signals} canOperate={Boolean(canOperate)} onStart={() => void scannerAction("start")} onStop={() => void scannerAction("stop")} onRefresh={refreshAll} />; break;
    case "market": content = <MarketPanel session={marketSession} quality={dataQuality} overview={overview} scanner={scanner} regime={regime} onRefresh={refreshAll} />; break;
    case "scanner": content = <ScannerPanel scanner={scanner} safety={safety} dataQuality={dataQuality} refreshKey={scannerRevision} canOperate={Boolean(canOperate)} onStart={() => void scannerAction("start")} onStop={() => void scannerAction("stop")} onRefresh={refreshAll} />; break;
    case "universe": content = <UniverseWorkspace canOperate={Boolean(canOperate)} onMessage={setMessage} />; break;
    case "signals": content = <SignalsPanel signals={signals} />; break;
    case "strategies": content = <StrategiesPanel isAdmin={Boolean(isAdmin)} onMessage={setMessage} />; break;
    case "orders": content = <PaperExecutionPanel view="orders" />; break;
    case "positions": content = <PaperExecutionPanel view="positions" />; break;
    case "oms": content = <OmsWorkspace isAdmin={Boolean(isAdmin)} onMessage={setMessage} />; break;
    case "shadow": content = <ShadowWorkspace />; break;
    case "assisted": content = <AssistedTradingWorkspace isAdmin={Boolean(isAdmin)} onMessage={setMessage} />; break;
    case "risk": content = <RiskCenter safety={safety} telegram={telegram} canOperate={Boolean(canOperate)} isAdmin={Boolean(isAdmin)} onEmergency={() => void emergencyAction()} onClear={() => void emergencyAction(true)} onPaper={() => void paperAction()} onTelegram={() => void telegramAction()} />; break;
    case "backtesting": content = <BacktestingWorkspace isAdmin={Boolean(isAdmin)} onMessage={setMessage} />; break;
    case "telegram": content = controlsPanel; break;
    case "journal": content = <JournalPanel signals={signals} />; break;
    case "upstox": content = <UpstoxConsole isAdmin={Boolean(isAdmin)} onMessage={setMessage} />; break;
    case "firstock": content = <FirstockConsole isAdmin={Boolean(isAdmin)} onMessage={setMessage} />; break;
    case "scheduler": content = <SchedulerPanel isAdmin={Boolean(isAdmin)} onMessage={setMessage} />; break;
    case "system": content = <SystemHealthPanel overview={overview} scanner={scanner} />; break;
    case "audit": content = <SecurityPanel isAdmin={Boolean(isAdmin)} onMessage={setMessage} auditOnly />; break;
    case "liveGates": content = <LiveReadinessWorkspace isAdmin={Boolean(isAdmin)} onMessage={setMessage} />; break;
    case "settings": content = <SettingsPanel controls={controls} isAdmin={Boolean(isAdmin)} onSave={saveControls} onChange={updateControl} onMessage={setMessage} onNavigate={selectWorkspace} />; break;
    default: content = <UnavailableWorkspace workspace={active} />;
  }

  return <main className="min-h-screen bg-terminal-950 text-slate-200"><TerminalSidebar active={active} collapsed={collapsed} menuOpen={menuOpen} user={user} onSelect={selectWorkspace} onToggle={() => setCollapsed((value) => !value)} onSignOut={() => void signOut()} /><div className={`min-h-screen transition-[padding] duration-200 ${collapsed ? "lg:pl-[76px]" : "lg:pl-64"}`}><TerminalHeader active={active} overview={overview} scanner={scanner} safety={safety} user={user} onOpenNavigation={() => setMenuOpen((value) => !value)} onOpenControls={() => selectWorkspace("risk")} /><div className="mx-auto max-w-[1600px] p-4 sm:p-6">{content}</div></div><Toaster toasts={toasts} onDismiss={dismissToast} /></main>;
}

function Toaster({ toasts, onDismiss }: { toasts: Toast[]; onDismiss: (id: number) => void }) {
  const icons = { success: CheckCircle2, error: XCircle, info: Info } as const;
  return (
    <div className="toast-stack" role="status" aria-live="polite">
      {toasts.map((toast) => {
        const Icon = icons[toast.kind];
        return (
          <div key={toast.id} className={`toast toast--${toast.kind}`}>
            <Icon className="h-4 w-4 shrink-0" />
            <span className="flex-1">{toast.text}</span>
            <button aria-label="Dismiss" onClick={() => onDismiss(toast.id)} className="shrink-0 opacity-70 hover:opacity-100">
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
        );
      })}
    </div>
  );
}
