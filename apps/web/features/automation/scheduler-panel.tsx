"use client";

import {
  CalendarClock,
  Clock,
  ExternalLink,
  Play,
  RefreshCw,
  ShieldCheck,
  TimerReset,
  Zap,
} from "lucide-react";
import { useEffect, useState } from "react";

import { api, type AutoAuthStatus } from "../../components/api";
import { formatIstTimestamp } from "../../lib/formatting";

const SCHEDULED_JOBS = [
  {
    id: "upstox-auto-auth",
    name: "Upstox Morning Auto-Renewal",
    cron: "30 8 * * 1-5",
    timeDescription: "08:30 AM IST (Every Business Day)",
    purpose: "Headless TOTP + PIN login, token generation & Telegram dispatch",
    holidayAware: true,
    status: "ACTIVE",
  },
  {
    id: "market-data-ingestion",
    name: "Market Feed Live Ingestion",
    cron: "15 9 * * 1-5",
    timeDescription: "09:15 AM – 15:30 PM IST",
    purpose: "WebSocket tick ingestion, 1-min completed candle aggregation",
    holidayAware: true,
    status: "ACTIVE",
  },
  {
    id: "orb-establishment",
    name: "15-Min Opening Range Builder",
    cron: "15-30 9 * * 1-5",
    timeDescription: "09:15 AM – 09:30 AM IST",
    purpose: "Establishes daily High and Low breakout reference levels",
    holidayAware: true,
    status: "ACTIVE",
  },
  {
    id: "trade-cutoff",
    name: "Intraday Trade Signal Cutoff",
    cron: "45 14 * * 1-5",
    timeDescription: "14:45 PM IST",
    purpose: "Ceases new signal evaluations to avoid late-session holding risk",
    holidayAware: true,
    status: "ACTIVE",
  },
  {
    id: "eod-cleanup",
    name: "EOD Session Settlement",
    cron: "30 15 * * 1-5",
    timeDescription: "15:30 PM IST",
    purpose: "Final mark-to-market position audit and daily ledger snapshot",
    holidayAware: true,
    status: "ACTIVE",
  },
];

export function SchedulerPanel({
  isAdmin,
  onMessage,
}: {
  isAdmin: boolean;
  onMessage: (message: string) => void;
}) {
  const [autoAuth, setAutoAuth] = useState<AutoAuthStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [triggerLoading, setTriggerLoading] = useState(false);

  const load = () => {
    setLoading(true);
    api.autoAuthStatus()
      .then(setAutoAuth)
      .catch(() => {})
      .finally(() => setLoading(false));
  };

  useEffect(load, []);

  async function triggerAutoAuth() {
    setTriggerLoading(true);
    try {
      const result = await api.triggerAutoAuth();
      if (result.error) {
        onMessage(`Auto-renewal failed: ${result.error}`);
      } else {
        onMessage(
          `Morning Auto-Renewal executed successfully! Token valid until ${
            result.expires_at ? formatIstTimestamp(result.expires_at) : "end of session"
          }.`
        );
      }
      setAutoAuth(await api.autoAuthStatus());
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Failed to execute auto-renewal job");
    } finally {
      setTriggerLoading(false);
    }
  }

  return (
    <section>
      <div className="page-toolbar">
        <div>
          <p className="eyebrow">Automation</p>
          <h2 className="page-title">Scheduler Console</h2>
          <p className="page-copy">
            Recurring background cron jobs, market timing automation, and token auto-renewal tasks.
          </p>
        </div>
        <button className="secondary-button" onClick={load} disabled={loading}>
          <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
          Refresh
        </button>
      </div>

      {/* Live Auto-Auth Card */}
      <article className="panel mt-6 p-5 sm:p-6">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 border-b border-slate-200 dark:border-slate-800 pb-5">
          <div className="flex items-center gap-3">
            <div className="rounded-lg bg-emerald-500/10 p-2.5 border border-emerald-500/20 text-emerald-400">
              <TimerReset className="h-6 w-6" />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h3 className="text-base font-semibold text-slate-900 dark:text-white">
                  Upstox 08:30 AM Auto-Renewal Cron
                </h3>
                <span
                  className={`status-pill ${
                    autoAuth?.configured ? "status-good" : "status-watch"
                  }`}
                >
                  {autoAuth?.configured ? "READY" : "INCOMPLETE"}
                </span>
              </div>
              <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                Managed by APScheduler in FastAPI lifespan. Runs headlessly before market open.
              </p>
            </div>
          </div>

          {isAdmin && autoAuth?.configured && (
            <button
              disabled={triggerLoading}
              onClick={() => void triggerAutoAuth()}
              className="primary-button shrink-0"
            >
              {triggerLoading ? (
                <>
                  <RefreshCw className="h-4 w-4 animate-spin" />
                  Running Job…
                </>
              ) : (
                <>
                  <Play className="h-4 w-4" />
                  Run Job Now
                </>
              )}
            </button>
          )}
        </div>

        <div className="mt-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <div className="glass-inset rounded-md p-3 text-xs">
            <span className="text-slate-500 dark:text-slate-400">Schedule</span>
            <p className="mt-1 font-mono font-semibold text-slate-900 dark:text-slate-200">
              08:30 AM IST (Mon–Fri)
            </p>
          </div>

          <div className="glass-inset rounded-md p-3 text-xs">
            <span className="text-slate-500 dark:text-slate-400">Next Scheduled Run</span>
            <p className="mt-1 font-mono text-slate-900 dark:text-slate-200">
              {autoAuth?.next_run || "08:30 AM IST"}
            </p>
          </div>

          <div className="glass-inset rounded-md p-3 text-xs">
            <span className="text-slate-500 dark:text-slate-400">Last Execution</span>
            <p className="mt-1 font-medium text-slate-900 dark:text-slate-200">
              {autoAuth?.last_run_at ? (
                <>
                  <span className={autoAuth.last_success ? "text-emerald-600 dark:text-emerald-400 font-semibold" : "text-rose-600 dark:text-rose-400 font-semibold"}>
                    {autoAuth.last_success ? "✅ Success" : "❌ Failed"}
                  </span>
                  {" · "}
                  <span className="font-mono text-xs">{formatIstTimestamp(autoAuth.last_run_at)}</span>
                </>
              ) : (
                "Pending first scheduled run"
              )}
            </p>
          </div>

          <div className="glass-inset rounded-md p-3 text-xs">
            <span className="text-slate-500 dark:text-slate-400">Current Token Expiry</span>
            <p className="mt-1 font-mono text-slate-900 dark:text-slate-200">
              {autoAuth?.expires_at ? formatIstTimestamp(autoAuth.expires_at) : "Not active"}
            </p>
          </div>
        </div>

        {autoAuth?.error && (
          <div className="mt-4 rounded border border-rose-500/30 bg-rose-500/10 p-3 text-xs text-rose-600 dark:text-rose-400">
            <strong>Last Run Error:</strong> {autoAuth.error}
          </div>
        )}
      </article>

      {/* Scheduled Automation Jobs Table */}
      <article className="panel mt-6 overflow-hidden">
        <div className="flex items-center gap-3 border-b border-slate-200 dark:border-slate-800 px-5 py-4">
          <CalendarClock className="h-5 w-5 text-sky-500 dark:text-sky-400" />
          <div>
            <p className="eyebrow">Recurring Tasks</p>
            <h3 className="font-semibold text-slate-900 dark:text-white">Scheduled Operations Roster</h3>
          </div>
        </div>

        <div className="table-scroll">
          <table className="terminal-table">
            <thead>
              <tr>
                <th>Job Name</th>
                <th>Time (IST)</th>
                <th>Cron Expression</th>
                <th>Purpose</th>
                <th>Holiday Aware</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {SCHEDULED_JOBS.map((job) => (
                <tr key={job.id}>
                  <td>
                    <strong className="block text-slate-900 dark:text-slate-100">{job.name}</strong>
                    <span className="font-mono text-[11px] text-slate-500 dark:text-slate-400">{job.id}</span>
                  </td>
                  <td>
                    <div className="flex items-center gap-1.5 font-medium text-slate-800 dark:text-slate-200">
                      <Clock className="h-3.5 w-3.5 text-slate-400" />
                      {job.timeDescription}
                    </div>
                  </td>
                  <td>
                    <code className="rounded bg-slate-100 dark:bg-slate-800 px-2 py-0.5 text-xs font-mono text-slate-800 dark:text-slate-300">
                      {job.cron}
                    </code>
                  </td>
                  <td className="text-xs text-slate-600 dark:text-slate-400">{job.purpose}</td>
                  <td>
                    {job.holidayAware ? (
                      <span className="rounded bg-emerald-500/10 px-2 py-0.5 text-[11px] font-semibold text-emerald-600 dark:text-emerald-400 border border-emerald-500/20">
                        NSE Calendar
                      </span>
                    ) : (
                      <span className="text-slate-400 text-xs">—</span>
                    )}
                  </td>
                  <td>
                    <span className="status-pill status-good">{job.status}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </article>

      {/* Holiday Engine Note */}
      <div className="mt-6 rounded-lg border border-slate-200 dark:border-slate-800 bg-slate-50 dark:bg-slate-900/50 p-4 text-xs text-slate-600 dark:text-slate-400 flex items-start gap-3">
        <ShieldCheck className="h-5 w-5 text-emerald-500 shrink-0 mt-0.5" />
        <div className="space-y-1">
          <strong className="text-slate-900 dark:text-slate-200">NSE Trading Calendar Protection:</strong>
          <p>
            The scheduler queries <code className="font-mono text-slate-800 dark:text-slate-200">TradingCalendar</code> before executing morning auth. On official exchange holidays (e.g. Republic Day, Independence Day, Diwali, Muharram), jobs are automatically skipped to prevent unnecessary authentication requests.
          </p>
        </div>
      </div>
    </section>
  );
}