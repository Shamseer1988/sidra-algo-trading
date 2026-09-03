"use client";

import {
  CheckCircle2,
  ExternalLink,
  KeyRound,
  Landmark,
  RefreshCw,
  ShieldCheck,
  TimerReset,
} from "lucide-react";
import { useEffect, useState } from "react";

import {
  api,
  type AutoAuthStatus,
  type BrokerControls,
} from "../../components/api";
import { formatIstTimestamp } from "../../lib/formatting";

export function UpstoxConsole({
  isAdmin,
  onMessage,
}: {
  isAdmin: boolean;
  onMessage: (message: string) => void;
}) {
  const [controls, setControls] = useState<BrokerControls | null>(null);
  const [autoAuth, setAutoAuth] = useState<AutoAuthStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [triggerLoading, setTriggerLoading] = useState(false);
  const [refreshLoading, setRefreshLoading] = useState(false);

  const load = () => {
    setLoading(true);
    void Promise.all([api.brokerControls(), api.autoAuthStatus()])
      .then(([nextControls, nextAutoAuth]) => {
        setControls(nextControls);
        setAutoAuth(nextAutoAuth);
      })
      .catch((error: unknown) =>
        onMessage(error instanceof Error ? error.message : "Failed to load Upstox settings"),
      )
      .finally(() => setLoading(false));
  };

  useEffect(load, []);

  const isActive = Boolean(controls?.upstox_paper_enabled);

  async function activateUpstox() {
    if (!isAdmin) return;
    try {
      const updated = await api.updateBrokerControls({
        upstox_paper_enabled: true,
        firstock_feed_enabled: false,
      });
      setControls(updated);
      onMessage("Upstox activated as primary market-data connector.");
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Failed to activate Upstox");
    }
  }

  async function renewUpstoxOAuth() {
    try {
      const { authorization_url } = await api.startUpstoxOAuth();
      window.location.assign(authorization_url);
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Could not start Upstox authorization");
    }
  }

  async function triggerAutoAuth() {
    setTriggerLoading(true);
    try {
      const result = await api.triggerAutoAuth();
      if (result.error) {
        onMessage(`Auto-login attempt failed: ${result.error}`);
      } else {
        onMessage(`Upstox auto-renewal succeeded! Token valid until ${result.expires_at ? formatIstTimestamp(result.expires_at) : "end of day"}.`);
      }
      setAutoAuth(await api.autoAuthStatus());
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Auto-login failed");
    } finally {
      setTriggerLoading(false);
    }
  }

  async function refreshInstruments() {
    setRefreshLoading(true);
    try {
      const res = await api.refreshUpstoxInstruments();
      onMessage(`Instruments refreshed: ${res.instrument_count} instruments loaded.`);
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Instrument refresh failed");
    } finally {
      setRefreshLoading(false);
    }
  }

  return (
    <section>
      <div className="page-toolbar">
        <div>
          <p className="eyebrow">Broker Integration</p>
          <h2 className="page-title">Upstox Console</h2>
          <p className="page-copy">
            Paper market-data connector, OAuth 2.0 access token management, and morning automated renewal scheduler.
          </p>
        </div>
        <button className="secondary-button" onClick={load} disabled={loading}>
          <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
          Refresh
        </button>
      </div>

      {/* Feed Status Banner */}
      <div className="panel mt-6 p-5 sm:p-6">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <div className="rounded-lg bg-emerald-500/10 p-2.5 border border-emerald-500/20 text-emerald-400">
              <Landmark className="h-6 w-6" />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h3 className="text-base font-semibold text-slate-900 dark:text-white">Upstox Market Data Feed</h3>
                <span
                  className={`status-pill ${
                    isActive ? "status-good" : "status-watch"
                  }`}
                >
                  {isActive ? "ACTIVE FEED" : "STANDBY"}
                </span>
              </div>
              <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                {isActive
                  ? "Currently streaming live completed candles and ticks to the scanner."
                  : "Upstox is in standby mode. Click below to set it as the primary market data feed."}
              </p>
            </div>
          </div>

          <div className="flex items-center gap-2">
            {!isActive && isAdmin && (
              <button onClick={() => void activateUpstox()} className="primary-button">
                <CheckCircle2 className="h-4 w-4" />
                Set as Primary Feed
              </button>
            )}
            {isActive && (
              <span className="rounded bg-emerald-500/15 px-3 py-1.5 text-xs font-semibold text-emerald-600 dark:text-emerald-300 border border-emerald-500/30">
                ✓ Primary Active Feed
              </span>
            )}
          </div>
        </div>
      </div>

      {/* Grid: OAuth & Scheduler */}
      <div className="mt-6 grid gap-6 lg:grid-cols-2">
        {/* Card 1: OAuth Access & Instruments */}
        <article className="panel p-5 sm:p-6 flex flex-col justify-between">
          <div>
            <div className="flex items-center gap-2.5 pb-4 border-b border-slate-200 dark:border-slate-800">
              <KeyRound className="h-5 w-5 text-sky-500 dark:text-sky-400" />
              <h3 className="font-semibold text-slate-900 dark:text-white">OAuth 2.0 Web Authentication</h3>
            </div>

            <p className="mt-4 text-xs leading-5 text-slate-600 dark:text-slate-400">
              Upstox requires a daily OAuth 2.0 authorization code exchange. You can either authenticate manually using the official Upstox login dialog or rely on the morning automated headless renewal.
            </p>

            <div className="mt-4 space-y-3">
              <div className="glass-inset rounded-md p-3 text-xs">
                <span className="text-slate-500 dark:text-slate-400">Token Status</span>
                <p className="mt-1 font-semibold text-slate-900 dark:text-slate-100">
                  {autoAuth?.expires_at ? "Token Active" : "No Active Token"}
                </p>
              </div>
              <div className="glass-inset rounded-md p-3 text-xs">
                <span className="text-slate-500 dark:text-slate-400">Token Expiration</span>
                <p className="mt-1 font-mono text-slate-900 dark:text-slate-200">
                  {autoAuth?.expires_at ? formatIstTimestamp(autoAuth.expires_at) : "Expired / Not Loaded"}
                </p>
              </div>
            </div>
          </div>

          <div className="mt-6 flex flex-wrap gap-2 pt-4 border-t border-slate-200 dark:border-slate-800">
            {isAdmin && (
              <>
                <button onClick={() => void renewUpstoxOAuth()} className="primary-button">
                  <ExternalLink className="h-4 w-4" />
                  Renew Access (Web Login)
                </button>
                <button
                  onClick={() => void refreshInstruments()}
                  disabled={refreshLoading}
                  className="secondary-button"
                >
                  <RefreshCw className={`h-4 w-4 ${refreshLoading ? "animate-spin" : ""}`} />
                  Refresh Scrip Master
                </button>
              </>
            )}
          </div>
        </article>

        {/* Card 2: Morning Auto-Renewal Scheduler */}
        <article className="panel p-5 sm:p-6 flex flex-col justify-between">
          <div>
            <div className="flex items-center gap-2.5 pb-4 border-b border-slate-200 dark:border-slate-800">
              <TimerReset className="h-5 w-5 text-emerald-500 dark:text-emerald-400" />
              <h3 className="font-semibold text-slate-900 dark:text-white">Morning Auto-Renewal (08:30 AM)</h3>
            </div>

            <p className="mt-4 text-xs leading-5 text-slate-600 dark:text-slate-400">
              APScheduler automatically logs in every business day at <strong>08:30 AM IST</strong> using headless TOTP + PIN verification, verifies trading calendar holidays, and sends a Telegram notification.
            </p>

            <div className="mt-4 grid gap-3 sm:grid-cols-2">
              <div className="glass-inset rounded-md p-3 text-xs">
                <span className="text-slate-500 dark:text-slate-400">Scheduler Config</span>
                <p className={`mt-1 font-semibold ${autoAuth?.configured ? "text-emerald-600 dark:text-emerald-400" : "text-amber-600 dark:text-amber-400"}`}>
                  {autoAuth?.configured ? "✅ Configured" : autoAuth?.enabled ? "⚠️ Missing Credentials" : "⏸ Disabled"}
                </p>
              </div>
              <div className="glass-inset rounded-md p-3 text-xs">
                <span className="text-slate-500 dark:text-slate-400">Next Scheduled Run</span>
                <p className="mt-1 font-mono text-slate-900 dark:text-slate-200">
                  {autoAuth?.next_run || "08:30 AM IST (Mon–Fri)"}
                </p>
              </div>
              <div className="glass-inset rounded-md p-3 text-xs sm:col-span-2">
                <span className="text-slate-500 dark:text-slate-400">Last Renewal Outcome</span>
                <p className="mt-1 font-medium text-slate-900 dark:text-slate-100">
                  {autoAuth?.last_run_at ? (
                    <>
                      <span className={autoAuth.last_success ? "text-emerald-600 dark:text-emerald-400 font-semibold" : "text-rose-600 dark:text-rose-400 font-semibold"}>
                        {autoAuth.last_success ? "✅ Success" : "❌ Failed"}
                      </span>
                      {" · "}
                      <span className="font-mono text-xs">{formatIstTimestamp(autoAuth.last_run_at)}</span>
                    </>
                  ) : (
                    "No automatic runs recorded yet today"
                  )}
                </p>
              </div>
            </div>

            {autoAuth?.error && (
              <div className="mt-3 rounded border border-rose-500/30 bg-rose-500/10 p-2.5 text-xs text-rose-600 dark:text-rose-400">
                <strong>Renewal Error:</strong> {autoAuth.error}
              </div>
            )}
          </div>

          <div className="mt-6 pt-4 border-t border-slate-200 dark:border-slate-800">
            {isAdmin && autoAuth?.configured && (
              <button
                disabled={triggerLoading}
                onClick={() => void triggerAutoAuth()}
                className="secondary-button"
              >
                {triggerLoading ? (
                  <>
                    <RefreshCw className="h-4 w-4 animate-spin" />
                    Renewing Token…
                  </>
                ) : (
                  <>
                    <TimerReset className="h-4 w-4" />
                    Trigger Auto-Login Now
                  </>
                )}
              </button>
            )}
          </div>
        </article>
      </div>

      {/* Security Note */}
      <div className="mt-6 rounded-lg border border-slate-200 dark:border-slate-800 bg-slate-50 dark:bg-slate-900/50 p-4 text-xs text-slate-600 dark:text-slate-400 flex items-start gap-3">
        <ShieldCheck className="h-5 w-5 text-emerald-500 shrink-0 mt-0.5" />
        <div>
          <strong className="text-slate-900 dark:text-slate-200">Server-Side Credentials Security:</strong> Upstox API keys, secret, TOTP key, mobile number, and PIN are securely stored in the server environment (.env) and never exposed to client browsers.
        </div>
      </div>
    </section>
  );
}