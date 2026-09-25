"use client";

import { BellRing, LockKeyhole, ShieldAlert, Webhook } from "lucide-react";

import type { SafetyStatus, TelegramStatus } from "../../components/api";

/**
 * The control plane, split into the two places it belongs.
 *
 * This file used to export one `ControlPanel` holding four cards — paper
 * tracking, the live lock, emergency stop, and the Telegram bot — and it was
 * rendered twice: once as its own "Telegram" menu entry and once embedded
 * inside Risk. Two menu entries, the same four cards, and no way to tell which
 * one you were looking at.
 *
 * Safety belongs with Risk, where an operator is already looking when something
 * is wrong. Alerts belong in Settings, with the rest of the configuration.
 */

export function SafetyControls({
  safety,
  canOperate,
  isAdmin,
  onEmergency,
  onClear,
  onPaper,
}: {
  safety: SafetyStatus;
  canOperate: boolean;
  isAdmin: boolean;
  onEmergency: () => void;
  onClear: () => void;
  onPaper: () => void;
}) {
  return (
    <div className="mt-6 grid gap-4 xl:grid-cols-3">
      <article className={`panel p-6 ${safety.emergency_stop_active ? "glass-danger" : ""}`}>
        <p className="eyebrow text-rose-400">Emergency stop</p>
        <h3 className="mt-1 text-lg font-semibold text-white">{safety.emergency_stop_active ? "Engaged" : "Ready"}</h3>
        <p className="mt-3 text-sm leading-6 text-slate-400">
          Stops the scanner and market-data activity. It does not reach outside this application, and it does not
          close anything already open at a broker.
        </p>
        {canOperate && (
          <div className="mt-6 flex gap-3">
            {safety.emergency_stop_active ? (
              <button onClick={onClear} disabled={!isAdmin} className="secondary-button">
                Clear stop
              </button>
            ) : (
              <button onClick={onEmergency} className="danger-button">
                <ShieldAlert className="h-4 w-4" />
                Emergency stop
              </button>
            )}
          </div>
        )}
        {safety.emergency_stop_active && safety.emergency_stop_reason && (
          <p className="mt-4 text-xs text-rose-300">{safety.emergency_stop_reason}</p>
        )}
      </article>

      <article className="panel p-6">
        <p className="eyebrow">Paper trade tracking</p>
        <h3 className="mt-1 text-lg font-semibold text-white">
          {safety.paper_tracking_enabled ? "Enabled" : "Disabled"}
        </h3>
        <p className="mt-3 text-sm leading-6 text-slate-400">Controls paper-signal journaling and notifications.</p>
        {isAdmin && (
          <button onClick={onPaper} className="secondary-button mt-6">
            {safety.paper_tracking_enabled ? "Disable paper tracking" : "Enable paper tracking"}
          </button>
        )}
      </article>

      <article className="panel p-6">
        <div className="flex items-start justify-between">
          <div>
            <p className="eyebrow">Live trading</p>
            <h3 className="mt-1 text-lg font-semibold text-white">Locked</h3>
          </div>
          <LockKeyhole className="h-5 w-5 text-slate-500" />
        </div>
        {/* The old copy here said live execution waits on gates that "do not
            exist". They exist now — readiness, activation, per-order approval,
            reconciliation — so saying otherwise was actively misleading. What
            has not changed is the lock: the application refuses to start with
            LIVE_TRADING_ENABLED=true, unconditionally, and no screen can
            change that. */}
        <p className="mt-3 text-sm leading-6 text-slate-400">
          The readiness, activation, approval and reconciliation gates are built and enforced. Live execution is held
          shut by <span className="font-mono text-slate-300">LIVE_TRADING_ENABLED</span>, which the application
          refuses to start with set — there is no switch for it here, by design.
        </p>
        <button disabled className="secondary-button mt-6">
          <LockKeyhole className="h-4 w-4" />
          Enable live trading
        </button>
      </article>
    </div>
  );
}

export function AlertsPanel({
  telegram,
  isAdmin,
  onTelegram,
  onRegisterWebhook,
}: {
  telegram: TelegramStatus;
  isAdmin: boolean;
  onTelegram: () => void;
  onRegisterWebhook: () => void;
}) {
  return (
    <section className="mt-6 max-w-5xl">
      <article className="panel p-5 sm:p-7">
        <p className="eyebrow">Notifications</p>
        <h3 className="mt-1 text-base font-semibold text-white">
          Telegram bot — {telegram.configured ? "configured" : "awaiting a dedicated bot"}
        </h3>
        <p className="mt-3 text-sm leading-6 text-slate-400">
          Outbound alerts: {telegram.configured ? "ready" : "not configured"}. Inbound controls:{" "}
          {telegram.inbound_enabled ? "ready" : "need an HTTPS webhook and allowed user IDs"}.
        </p>
        <p className="mt-2 text-xs leading-5 text-slate-500">{telegram.detail}</p>
        {isAdmin && (
          <div className="mt-6 flex flex-wrap gap-3">
            <button disabled={!telegram.configured} onClick={onTelegram} className="secondary-button">
              <BellRing className="h-4 w-4" />
              Send test alert
            </button>
            <button disabled={!telegram.inbound_enabled} onClick={onRegisterWebhook} className="secondary-button">
              <Webhook className="h-4 w-4" />
              Register webhook
            </button>
          </div>
        )}
        {isAdmin && (
          <p className="mt-3 text-xs leading-5 text-slate-500">
            A test alert proves outbound only. Registering tells Telegram where to deliver replies and which
            secret to send with them, so it is required after the webhook URL or secret changes — until then
            every inbound update is rejected and approvals stop without an error anywhere in this screen.
          </p>
        )}
      </article>
    </section>
  );
}
