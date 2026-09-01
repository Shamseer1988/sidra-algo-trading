import {
  Activity,
  ArrowUpRight,
  LockKeyhole,
  Radio,
  RefreshCw,
  ShieldAlert,
  ShieldCheck,
  TriangleAlert,
} from "lucide-react";

import type { PaperSignal, SafetyStatus, ScannerStatus } from "../../components/api";

type Service = [string, { status: string; detail: string }, typeof Activity];

function statusClass(status: string) {
  const value = status.toLowerCase();
  if (["healthy", "running", "live", "configured"].includes(value)) return "status-good";
  if (["not_configured", "stopped", "disconnected", "degraded"].includes(value)) return "status-watch";
  return "status-bad";
}

function ServiceCard({ name, item, icon: Icon }: { name: string; item: Service[1]; icon: Service[2] }) {
  return (
    <article className="panel p-4">
      <div className="flex items-center justify-between gap-3">
        <span className="flex items-center gap-2 text-sm font-semibold text-slate-200">
          <Icon className="h-4 w-4 text-slate-500" />{name}
        </span>
        <span className={`status-pill ${statusClass(item.status)}`}>{item.status.replaceAll("_", " ")}</span>
      </div>
      <p className="mt-3 min-h-9 text-xs leading-5 text-slate-500">{item.detail}</p>
    </article>
  );
}

function Metric({ label, value, note }: { label: string; value: string; note: string }) {
  return (
    <article className="panel p-4 sm:p-5">
      <p className="text-[10px] font-bold uppercase tracking-[.16em] text-slate-500">{label}</p>
      <p className="mt-3 font-mono text-2xl font-semibold tabular-nums text-white">{value}</p>
      <p className="mt-1 text-xs text-slate-500">{note}</p>
    </article>
  );
}

export function Dashboard({
  services,
  scanner,
  safety,
  signals,
  canOperate,
  onStart,
  onStop,
  onRefresh,
}: {
  services: Service[];
  scanner: ScannerStatus;
  safety: SafetyStatus;
  signals: PaperSignal[];
  canOperate: boolean;
  onStart: () => void;
  onStop: () => void;
  onRefresh: () => void;
}) {
  const alerted = signals.filter((signal) => signal.status === "PAPER_ALERTED").length;
  const longSignals = signals.filter((signal) => signal.side === "LONG").length;
  const shortSignals = signals.length - longSignals;

  return (
    <section>
      <div className="flex flex-col gap-4 border-b border-slate-800 pb-5 lg:flex-row lg:items-end lg:justify-between">
        <div>
          <p className="eyebrow">Operations overview</p>
          <h2 className="page-title">Paper command center</h2>
          <p className="page-copy">Live operational state from Sidra services. No generated prices or simulated broker status.</p>
        </div>
        <button onClick={onRefresh} className="secondary-button"><RefreshCw className="h-4 w-4" />Refresh state</button>
      </div>

      {safety.emergency_stop_active && (
        <div className="mt-5 flex gap-3 rounded-lg border border-rose-900 bg-rose-950/40 p-4 text-sm text-rose-200">
          <ShieldAlert className="h-5 w-5 shrink-0" />
          <div><b>Emergency stop is active.</b><br />{safety.emergency_stop_reason}</div>
        </div>
      )}

      <section className="mt-5 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Metric label="Qualified signals" value={String(signals.length)} note={`${longSignals} long · ${shortSignals} short`} />
        <Metric label="Telegram delivery" value={String(alerted)} note="Paper alerts confirmed" />
        <Metric label="Paper tracking" value={safety.paper_tracking_enabled ? "ACTIVE" : "PAUSED"} note="Journal and notifications" />
        <Metric label="Live execution" value="LOCKED" note="No broker order path" />
      </section>

      <section className="mt-4 grid gap-4 xl:grid-cols-[1.25fr_.75fr]">
        <article className="panel overflow-hidden">
          <div className="flex items-center justify-between border-b border-slate-800 px-5 py-4">
            <div>
              <p className="eyebrow">Scanner engine</p>
              <h3 className="mt-1 text-base font-semibold text-white">{scanner.status}</h3>
            </div>
            <span className={`status-pill ${statusClass(scanner.status)}`}><Radio className="h-3 w-3" />{scanner.status}</span>
          </div>
          <div className="p-5">
            <p className="text-sm leading-6 text-slate-400">{scanner.detail}</p>
            {canOperate && (
              <div className="mt-5 flex flex-wrap gap-3">
                <button className="primary-button" disabled={scanner.status === "RUNNING" || safety.emergency_stop_active} onClick={onStart}>
                  <Activity className="h-4 w-4" />Start scanner
                </button>
                <button className="secondary-button" disabled={scanner.status === "STOPPED"} onClick={onStop}>
                  <TriangleAlert className="h-4 w-4" />Stop scanner
                </button>
              </div>
            )}
          </div>
        </article>

        <article className="panel p-5">
          <div className="flex items-start justify-between">
            <div><p className="eyebrow">Execution perimeter</p><h3 className="mt-1 text-base font-semibold text-white">Safety boundary intact</h3></div>
            <LockKeyhole className="h-5 w-5 text-emerald-400" />
          </div>
          <div className="mt-5 space-y-3 text-sm">
            <div className="flex items-center justify-between border-b border-slate-800 pb-3"><span className="text-slate-500">Execution mode</span><span className="status-pill status-good">PAPER</span></div>
            <div className="flex items-center justify-between border-b border-slate-800 pb-3"><span className="text-slate-500">Broker submission</span><span className="font-medium text-slate-300">Unavailable</span></div>
            <div className="flex items-center justify-between"><span className="text-slate-500">Emergency protection</span><span className="flex items-center gap-1.5 font-medium text-emerald-300"><ShieldCheck className="h-4 w-4" />Armed</span></div>
          </div>
        </article>
      </section>

      <section className="mt-4">
        <div className="mb-3 flex items-center justify-between">
          <div><p className="eyebrow">Infrastructure</p><h3 className="mt-1 text-base font-semibold text-white">System status</h3></div>
          <span className="flex items-center gap-1.5 text-xs text-slate-500">Verified by backend <ArrowUpRight className="h-3.5 w-3.5" /></span>
        </div>
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {services.map(([name, item, Icon]) => <ServiceCard key={name} name={name} item={item} icon={Icon} />)}
        </div>
      </section>
    </section>
  );
}
