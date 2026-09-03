"use client";

import {
  CheckCircle2,
  KeyRound,
  Play,
  RefreshCw,
  ShieldCheck,
  Terminal,
  WalletCards,
  Wifi,
  WifiOff,
} from "lucide-react";
import { useEffect, useState } from "react";

import {
  api,
  type BrokerControls,
  type FirstockBrokerStatus,
} from "../../components/api";
import { formatIstTimestamp, statusTone, titleCase } from "../../lib/formatting";

export function FirstockConsole({
  isAdmin,
  onMessage,
}: {
  isAdmin: boolean;
  onMessage: (message: string) => void;
}) {
  const [controls, setControls] = useState<BrokerControls | null>(null);
  const [status, setStatus] = useState<FirstockBrokerStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [testing, setTesting] = useState(false);

  const load = () => {
    setLoading(true);
    void Promise.all([api.brokerControls(), api.firstockStatus()])
      .then(([nextControls, nextStatus]) => {
        setControls(nextControls);
        setStatus(nextStatus);
      })
      .catch((error: unknown) =>
        onMessage(error instanceof Error ? error.message : "Failed to load Firstock state"),
      )
      .finally(() => setLoading(false));
  };

  useEffect(load, []);

  const isActive = Boolean(controls?.firstock_feed_enabled);

  async function activateFirstock() {
    if (!isAdmin) return;
    try {
      const updated = await api.updateBrokerControls({
        upstox_paper_enabled: false,
        firstock_feed_enabled: true,
      });
      setControls(updated);
      onMessage("Firstock feed activated as primary market-data connector.");
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Failed to activate Firstock");
    }
  }

  async function handleTestConnection() {
    setTesting(true);
    try {
      const result = await api.testFirstock();
      setStatus(result);
      if (result.websocket_status === "AUTHENTICATED") {
        onMessage("Firstock connection test succeeded! REST authentication verified.");
      } else {
        onMessage(`Firstock connection test: ${result.detail}`);
      }
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Firstock connection test failed");
    } finally {
      setTesting(false);
    }
  }

  return (
    <section>
      <div className="page-toolbar">
        <div>
          <p className="eyebrow">Broker Integration</p>
          <h2 className="page-title">Firstock Console</h2>
          <p className="page-copy">
            Alternative market-data feed connector, WebSocket connection telemetry, and REST API connection test.
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
            <div className="rounded-lg bg-sky-500/10 p-2.5 border border-sky-500/20 text-sky-400">
              <WalletCards className="h-6 w-6" />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h3 className="text-base font-semibold text-slate-900 dark:text-white">Firstock Market Data Feed</h3>
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
                  ? "Currently streaming market ticks to the intraday scanner."
                  : "Firstock is in standby mode. Click below to switch primary feed to Firstock."}
              </p>
            </div>
          </div>

          <div className="flex items-center gap-2">
            {!isActive && isAdmin && (
              <button onClick={() => void activateFirstock()} className="primary-button">
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

      {/* Grid: Telemetry & Connection Test */}
      <div className="mt-6 grid gap-6 lg:grid-cols-2">
        {/* Card 1: Connection State & Subscriptions */}
        <article className="panel p-5 sm:p-6 flex flex-col justify-between">
          <div>
            <div className="flex items-center gap-2.5 pb-4 border-b border-slate-200 dark:border-slate-800">
              <Wifi className="h-5 w-5 text-sky-500 dark:text-sky-400" />
              <h3 className="font-semibold text-slate-900 dark:text-white">Connection & WebSocket Health</h3>
            </div>

            <div className="mt-4 grid gap-3 sm:grid-cols-2">
              <div className="glass-inset rounded-md p-3 text-xs">
                <span className="text-slate-500 dark:text-slate-400">Configured Status</span>
                <p className={`mt-1 font-semibold ${status?.configured ? "text-emerald-600 dark:text-emerald-400" : "text-amber-600 dark:text-amber-400"}`}>
                  {status?.configured ? "✅ Server Configured" : "⚠️ Not Configured"}
                </p>
              </div>

              <div className="glass-inset rounded-md p-3 text-xs">
                <span className="text-slate-500 dark:text-slate-400">User ID</span>
                <p className="mt-1 font-mono font-semibold text-slate-900 dark:text-slate-200">
                  {status?.user_id_masked || "None"}
                </p>
              </div>

              <div className="glass-inset rounded-md p-3 text-xs">
                <span className="text-slate-500 dark:text-slate-400">WebSocket Session</span>
                <p className="mt-1">
                  <span className={`status-pill ${status ? statusTone(status.websocket_status) : "status-watch"}`}>
                    {titleCase(status?.websocket_status ?? "Unknown")}
                  </span>
                </p>
              </div>

              <div className="glass-inset rounded-md p-3 text-xs">
                <span className="text-slate-500 dark:text-slate-400">Subscriptions</span>
                <p className="mt-1 font-mono font-semibold text-slate-900 dark:text-slate-200">
                  {status?.subscription_count ?? 0} symbols
                </p>
              </div>
            </div>

            <div className="mt-4 glass-inset rounded-md p-3 text-xs">
              <span className="text-slate-500 dark:text-slate-400">Status Detail</span>
              <p className="mt-1 leading-5 text-slate-800 dark:text-slate-200">
                {status?.detail || "No status report available"}
              </p>
              {status?.updated_at && (
                <p className="mt-2 text-[11px] text-slate-500 dark:text-slate-400 font-mono">
                  Updated: {formatIstTimestamp(status.updated_at)}
                </p>
              )}
            </div>
          </div>

          <div className="mt-6 pt-4 border-t border-slate-200 dark:border-slate-800">
            <span className="text-xs text-slate-500 dark:text-slate-400">
              Live ticks stream via WebSocket when Firstock is the active feed.
            </span>
          </div>
        </article>

        {/* Card 2: Interactive Connection Test */}
        <article className="panel p-5 sm:p-6 flex flex-col justify-between">
          <div>
            <div className="flex items-center gap-2.5 pb-4 border-b border-slate-200 dark:border-slate-800">
              <Terminal className="h-5 w-5 text-violet-500 dark:text-violet-400" />
              <h3 className="font-semibold text-slate-900 dark:text-white">API Authentication Verification</h3>
            </div>

            <p className="mt-4 text-xs leading-5 text-slate-600 dark:text-slate-400">
              Test the Firstock REST client authentication using server-side credentials and TOTP generator. This verification validates credentials without sending live orders.
            </p>

            <div className="mt-5 space-y-3">
              <div className="rounded-lg border border-slate-200 dark:border-slate-800 bg-slate-50 dark:bg-slate-900/50 p-4 text-xs space-y-2">
                <div className="flex items-center justify-between">
                  <span className="text-slate-500 dark:text-slate-400">Auth Test Target:</span>
                  <span className="font-mono text-slate-900 dark:text-slate-200">connect.thefirstock.com</span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-slate-500 dark:text-slate-400">TOTP Auth Handshake:</span>
                  <span className="text-emerald-600 dark:text-emerald-400 font-semibold">Automatic pyotp</span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-slate-500 dark:text-slate-400">Execution Mode:</span>
                  <span className="text-slate-700 dark:text-slate-300">Read-Only Market Feed</span>
                </div>
              </div>
            </div>
          </div>

          <div className="mt-6 pt-4 border-t border-slate-200 dark:border-slate-800">
            {isAdmin && (
              <button
                disabled={testing || !status?.configured}
                onClick={() => void handleTestConnection()}
                className="secondary-button"
              >
                {testing ? (
                  <>
                    <RefreshCw className="h-4 w-4 animate-spin" />
                    Authenticating…
                  </>
                ) : (
                  <>
                    <Play className="h-4 w-4 text-emerald-500" />
                    Test Connection Now
                  </>
                )}
              </button>
            )}
            {!status?.configured && (
              <p className="mt-2 text-xs text-amber-500">
                Configure server environment credentials to enable testing.
              </p>
            )}
          </div>
        </article>
      </div>

      {/* Configuration Guide */}
      <div className="mt-6 rounded-lg border border-slate-200 dark:border-slate-800 bg-slate-50 dark:bg-slate-900/50 p-4 text-xs text-slate-600 dark:text-slate-400 flex items-start gap-3">
        <ShieldCheck className="h-5 w-5 text-sky-500 shrink-0 mt-0.5" />
        <div className="space-y-1">
          <strong className="text-slate-900 dark:text-slate-200">Firstock Server Environment Configuration:</strong>
          <p>
            Firstock requires <code className="font-mono text-slate-800 dark:text-slate-200">FIRSTOCK_USER_ID</code>, <code className="font-mono text-slate-800 dark:text-slate-200">FIRSTOCK_API_KEY</code>, <code className="font-mono text-slate-800 dark:text-slate-200">FIRSTOCK_VENDOR_CODE</code>, <code className="font-mono text-slate-800 dark:text-slate-200">FIRSTOCK_PASSWORD</code>, and <code className="font-mono text-slate-800 dark:text-slate-200">FIRSTOCK_TOTP_KEY</code> in <code className="font-mono text-slate-800 dark:text-slate-200">.env</code>.
          </p>
        </div>
      </div>
    </section>
  );
}