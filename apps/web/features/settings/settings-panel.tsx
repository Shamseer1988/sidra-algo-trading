"use client";

import { useCallback, useEffect, useState } from "react";

import { api, type AuditLog, type BrokerControls, type UserSession } from "../../components/api";
import type { WorkspaceId } from "../../lib/navigation";
import { formatIstTimestamp } from "../../lib/formatting";
import { IndicatorSettingsForm } from "./indicator-settings-form";
import { TradingControlsForm } from "./trading-controls-form";

export function SettingsPanel({
  isAdmin,
  onMessage,
  onNavigate,
}: {
  isAdmin: boolean;
  onMessage: (message: string) => void;
  onNavigate?: (id: WorkspaceId) => void;
}) {
  return (
    <section>
      <div className="page-toolbar">
        <div>
          <p className="eyebrow">Configuration</p>
          <h2 className="page-title">Trading controls</h2>
          <p className="page-copy">
            Every control below is described, bounded and validated by the server. The effective limits at the top are
            what these settings actually permit once they are read together.
          </p>
        </div>
      </div>

      <TradingControlsForm isAdmin={isAdmin} onMessage={onMessage} />
      <IndicatorSettingsForm isAdmin={isAdmin} onMessage={onMessage} />

      <MarketDataFeedSelector isAdmin={isAdmin} onMessage={onMessage} onNavigate={onNavigate} />
      <SecurityPanel isAdmin={isAdmin} onMessage={onMessage} />
    </section>
  );
}

export function MarketDataFeedSelector({
  isAdmin,
  onMessage,
  onNavigate,
}: {
  isAdmin: boolean;
  onMessage: (message: string) => void;
  onNavigate?: (id: WorkspaceId) => void;
}) {
  const [controls, setControls] = useState<BrokerControls | null>(null);

  useEffect(() => {
    void api
      .brokerControls()
      .then(setControls)
      .catch((error: unknown) =>
        onMessage(error instanceof Error ? error.message : "Could not load market-data settings"),
      );
  }, [onMessage]);

  async function select(provider: "UPSTOX" | "FIRSTOCK" | "NONE") {
    if (!controls || !isAdmin) return;
    const next = {
      upstox_paper_enabled: provider === "UPSTOX",
      firstock_feed_enabled: provider === "FIRSTOCK",
    };
    try {
      setControls(await api.updateBrokerControls(next));
      onMessage(
        `Selected ${provider === "NONE" ? "no market-data connector" : `${provider} market data`}.`,
      );
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Could not save market-data settings");
    }
  }

  const active = !controls
    ? "Loading…"
    : controls.upstox_paper_enabled
    ? "Upstox PAPER"
    : controls.firstock_feed_enabled
    ? "Firstock feed"
    : "Disabled";

  return (
    <article className="panel mt-6 max-w-5xl p-5 sm:p-7">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 border-b border-slate-200 dark:border-slate-800 pb-4">
        <div>
          <p className="eyebrow">Market Data Ingestion</p>
          <h3 className="text-base font-semibold text-slate-900 dark:text-white">
            Primary Feed Connector:{" "}
            <span className="text-emerald-500 dark:text-emerald-400">{active}</span>
          </h3>
          <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
            Select the active market-data source for completed candles and scanner evaluations.
          </p>
        </div>
      </div>

      <div className="mt-5 grid gap-3 sm:grid-cols-3">
        <button
          disabled={!isAdmin || !controls}
          onClick={() => void select("UPSTOX")}
          className={`secondary-button justify-center ${
            controls?.upstox_paper_enabled
              ? "border-emerald-500 bg-emerald-500/10 text-emerald-600 dark:text-emerald-300 font-semibold"
              : ""
          }`}
        >
          {controls?.upstox_paper_enabled ? "✓ Upstox PAPER (Active)" : "Use Upstox PAPER"}
        </button>

        <button
          disabled={!isAdmin || !controls}
          onClick={() => void select("FIRSTOCK")}
          className={`secondary-button justify-center ${
            controls?.firstock_feed_enabled
              ? "border-emerald-500 bg-emerald-500/10 text-emerald-600 dark:text-emerald-300 font-semibold"
              : ""
          }`}
        >
          {controls?.firstock_feed_enabled ? "✓ Firstock Feed (Active)" : "Use Firstock feed"}
        </button>

        <button
          disabled={!isAdmin || !controls}
          onClick={() => void select("NONE")}
          className="secondary-button justify-center"
        >
          Disable both
        </button>
      </div>

      <div className="mt-5 flex flex-wrap items-center gap-3 pt-4 border-t border-slate-200 dark:border-slate-800 text-xs text-slate-500 dark:text-slate-400">
        <span>Manage credentials, OAuth tokens, and connection health:</span>
        {onNavigate && (
          <div className="flex gap-3">
            <button
              onClick={() => onNavigate("upstox")}
              className="text-emerald-600 dark:text-emerald-400 font-medium hover:underline inline-flex items-center gap-1"
            >
              Configure Upstox Console →
            </button>
            <span>·</span>
            <button
              onClick={() => onNavigate("firstock")}
              className="text-sky-600 dark:text-sky-400 font-medium hover:underline inline-flex items-center gap-1"
            >
              Configure Firstock Console →
            </button>
          </div>
        )}
      </div>
    </article>
  );
}

// Backward-compatibility alias
export const BrokerSettingsCard = MarketDataFeedSelector;

export function SecurityPanel({ isAdmin, onMessage, auditOnly = false }: { isAdmin: boolean; onMessage: (message: string) => void; auditOnly?: boolean }) {
  const [sessions, setSessions] = useState<UserSession[]>([]); const [audit, setAudit] = useState<AuditLog[]>([]);
  const load = useCallback(() => { if (!auditOnly) void api.sessions().then(setSessions).catch((error: unknown) => onMessage(error instanceof Error ? error.message : "Could not load sessions")); if (isAdmin) void api.auditLogs().then(setAudit).catch((error: unknown) => onMessage(error instanceof Error ? error.message : "Could not load audit history")); }, [auditOnly, isAdmin, onMessage]);
  useEffect(() => { load(); }, [load]);
  async function revoke(id: string) { try { await api.revokeSession(id); setSessions((current) => current.filter((item) => item.id !== id)); onMessage("Session revoked."); } catch (error) { onMessage(error instanceof Error ? error.message : "Could not revoke session"); } }
  return <section className={`mt-6 grid max-w-5xl gap-6 ${auditOnly ? "" : "xl:grid-cols-2"}`}>{!auditOnly && <article className="panel p-5 sm:p-7"><p className="eyebrow">Session management</p><h3 className="mt-1 text-lg font-semibold text-white">Active sessions</h3><div className="mt-5 space-y-3">{sessions.length ? sessions.map((item) => <div key={item.id} className="glass-inset rounded-md p-3 text-xs text-slate-400"><p className="truncate text-slate-200">{item.user_agent || "Unknown device"}</p><p className="mt-1">{item.ip_address || "Unknown IP"} · expires {formatIstTimestamp(item.expires_at)}</p><button onClick={() => void revoke(item.id)} className="mt-3 text-rose-300 hover:text-rose-200">Revoke session</button></div>) : <p className="text-sm text-slate-500">No active sessions found.</p>}</div></article>}{isAdmin && <article className="panel p-5 sm:p-7"><p className="eyebrow">Audit history</p><h3 className="mt-1 text-lg font-semibold text-white">Latest events</h3><div className="mt-5 space-y-3">{audit.length ? audit.slice(0, auditOnly ? 16 : 8).map((item) => <div key={item.id} className="border-b border-slate-800 pb-3 text-xs"><p className="text-slate-200">{item.event_type}</p><p className="mt-1 text-slate-500">{formatIstTimestamp(item.created_at)}{item.ip_address ? ` · ${item.ip_address}` : ""}</p></div>) : <p className="text-sm text-slate-500">No audit events found.</p>}</div></article>}</section>;
}
