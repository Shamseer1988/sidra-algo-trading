"use client";

import { CircleCheck, CircleX, LockKeyhole, RefreshCw, ShieldAlert, ShieldOff } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { api, type LiveActivation, type LiveReconciliation, type SafetyStatus } from "../../components/api";
import { formatIstTimestamp } from "../../lib/formatting";

/**
 * Reconcile, arm and disarm, from the screen an operator is already on.
 *
 * These four operations previously existed only as an SSH script. That was a
 * deliberate choice while arming was a rare, considered act; it stopped being
 * the right one once the scheduler arms every morning unattended, because the
 * person who needs to *stop* something at 10:15 should not be finding a
 * terminal and a CA bundle to do it.
 *
 * What has not changed, and is not softened here:
 *
 *   * the API re-checks every readiness gate on arm, so this screen cannot
 *     grant anything the gates refuse -- the button is a caller, not an override
 *   * a reason is required and stored, because an armed trading system should
 *     be able to say who armed it and why
 *   * a reconciliation goes stale in 15 minutes, so arming is offered only
 *     after one that passed, and the staleness is shown rather than implied
 *
 * Disarm is deliberately the easiest thing on this card to reach: no reason,
 * no confirmation, and it succeeds even when nothing is armed.
 */

const RECONCILE_FRESHNESS_MS = 15 * 60 * 1000;

export function LiveExecutionControls({
  safety,
  isAdmin,
  onMessage,
}: {
  safety: SafetyStatus;
  isAdmin: boolean;
  onMessage: (message: string) => void;
}) {
  const [activation, setActivation] = useState<LiveActivation | null>(null);
  const [reconciliation, setReconciliation] = useState<LiveReconciliation | null>(null);
  const [approvalMode, setApprovalMode] = useState<string>("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());

  const load = useCallback(() => {
    void api.liveActivation().then(setActivation).catch(() => setActivation(null));
    void api.controls().then((c) => setApprovalMode(c.execution_approval_mode ?? "")).catch(() => setApprovalMode(""));
  }, []);

  useEffect(load, [load]);
  // The reconcile window is the thing most likely to catch somebody out, so the
  // card counts it down rather than leaving them to check a timestamp.
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 10_000);
    return () => clearInterval(timer);
  }, []);

  const armed = safety.live_execution_available || Boolean(activation?.armed);
  const liveConfigured = safety.application_mode === "LIVE" && safety.live_trading_enabled;

  const reconciledAt = reconciliation ? new Date(reconciliation.created_at).getTime() : null;
  const reconcileAge = reconciledAt === null ? null : now - reconciledAt;
  const reconcileFresh = reconcileAge !== null && reconcileAge < RECONCILE_FRESHNESS_MS;
  const canArm = isAdmin && liveConfigured && !armed && reconciliation?.safe_to_trade === true && reconcileFresh;

  async function run(label: string, action: () => Promise<void>) {
    setBusy(label);
    try {
      await action();
    } catch (error: unknown) {
      onMessage(error instanceof Error ? error.message : `${label} failed.`);
    } finally {
      setBusy(null);
    }
  }

  const reconcile = () =>
    run("Reconcile", async () => {
      const result = await api.reconcileLive();
      setReconciliation(result);
      setNow(Date.now());
      onMessage(
        result.safe_to_trade
          ? "Reconciliation passed. Arm within 15 minutes or it goes stale."
          : `Reconciliation blocked: ${result.detail}`,
      );
    });

  const arm = () =>
    run("Arm", async () => {
      const result = await api.armLive(reason.trim());
      setActivation(result);
      setReason("");
      onMessage(
        `Armed until ${result.expires_at ? formatIstTimestamp(result.expires_at) : "the configured expiry"}. Real orders can now be placed.`,
      );
    });

  const disarm = () =>
    run("Disarm", async () => {
      setActivation(await api.disarmLive());
      onMessage("Disarmed. Live submission is off until somebody arms it again.");
    });

  return (
    <article className={`panel p-6 ${armed ? "glass-danger" : ""}`}>
      <div className="flex items-start justify-between">
        <div>
          <p className="eyebrow">Live execution</p>
          <h3 className="mt-1 text-lg font-semibold text-white">
            {armed ? "Armed" : liveConfigured ? "Held by the gates" : "Locked"}
          </h3>
        </div>
        {armed ? <ShieldAlert className="h-5 w-5 text-rose-400" /> : <LockKeyhole className="h-5 w-5 text-slate-500" />}
      </div>

      {armed && (
        <p className="mt-3 text-sm leading-6 text-rose-200">
          Real orders can reach the broker.{" "}
          {activation?.expires_at ? (
            <>
              Expires <b>{formatIstTimestamp(activation.expires_at)}</b>.
            </>
          ) : (
            <>The activation window is not readable from here; disarm if that is unexpected.</>
          )}
          {activation?.reason && <span className="block text-xs text-slate-400">Reason: {activation.reason}</span>}
        </p>
      )}

      {!armed && (
        <p className="mt-3 text-sm leading-6 text-slate-400">
          {liveConfigured
            ? "Nothing is submitted until every readiness gate passes and an administrator arms a window that expires on its own."
            : `This deployment runs in ${safety.application_mode}, so no order can reach a broker from it.`}
        </p>
      )}

      {/* The difference between a system that trades while you sleep and one
          that does not. It lived only in Settings, three screens away from the
          place an operator decides whether to arm. */}
      {approvalMode && (
        <p className="mt-3 flex items-start gap-2 text-xs leading-5">
          {approvalMode === "AUTOMATIC" ? (
            <>
              <ShieldOff className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-300" />
              <span className="text-amber-200">
                Approval mode is <b>AUTOMATIC</b>: once armed, orders are sent without asking you.
              </span>
            </>
          ) : approvalMode === "TELEGRAM_APPROVAL" ? (
            <>
              <CircleCheck className="mt-0.5 h-3.5 w-3.5 shrink-0 text-emerald-300" />
              <span className="text-slate-400">
                Approval mode is <b className="text-slate-300">TELEGRAM_APPROVAL</b>: every order waits for your tap
                and is refused if unanswered.
              </span>
            </>
          ) : (
            <>
              <CircleX className="mt-0.5 h-3.5 w-3.5 shrink-0 text-slate-500" />
              <span className="text-slate-400">
                Approval mode is <b className="text-slate-300">DISABLED</b>: no order is sent even when armed.
              </span>
            </>
          )}
        </p>
      )}

      {reconciliation && (
        <div className="mt-4 rounded-md border border-slate-800 p-3 text-xs leading-5">
          <p className={reconciliation.safe_to_trade ? "text-emerald-300" : "text-rose-300"}>
            {reconciliation.safe_to_trade ? "Reconciliation passed" : "Reconciliation blocked"}
            {reconcileAge !== null && (
              <span className="text-slate-500">
                {" "}
                · {reconcileFresh ? `${Math.floor(reconcileAge / 60_000)} min old` : "stale, re-run before arming"}
              </span>
            )}
          </p>
          <p className="mt-1 text-slate-400">{reconciliation.detail}</p>
          {reconciliation.findings.slice(0, 4).map((finding, index) => (
            <p key={index} className="mt-1 text-slate-500">
              • {finding.detail}
            </p>
          ))}
        </div>
      )}

      {isAdmin && liveConfigured && !armed && (
        <input
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          placeholder="Why are you arming? (at least 8 characters)"
          className="mt-4 w-full rounded-md border border-slate-800 bg-slate-950/60 px-3 py-2 text-sm text-slate-200 placeholder:text-slate-600"
        />
      )}

      <div className="mt-4 flex flex-wrap gap-2">
        <button className="secondary-button" onClick={() => void reconcile()} disabled={busy !== null || !liveConfigured}>
          <RefreshCw className={`h-4 w-4 ${busy === "Reconcile" ? "animate-spin" : ""}`} />
          {busy === "Reconcile" ? "Reconciling…" : "Reconcile"}
        </button>
        {isAdmin && liveConfigured && !armed && (
          <button
            className="primary-button"
            onClick={() => void arm()}
            disabled={busy !== null || !canArm || reason.trim().length < 8}
            title={
              !liveConfigured
                ? "This deployment is not configured for live trading."
                : !reconciliation
                  ? "Reconcile first."
                  : !reconciliation.safe_to_trade
                    ? "The reconciliation blocked trading."
                    : !reconcileFresh
                      ? "The reconciliation is older than 15 minutes. Re-run it."
                      : reason.trim().length < 8
                        ? "A reason of at least 8 characters is required."
                        : "Arm live submission."
            }
          >
            <ShieldAlert className="h-4 w-4" />
            {busy === "Arm" ? "Arming…" : "Arm live trading"}
          </button>
        )}
        {isAdmin && (
          <button className="secondary-button" onClick={() => void disarm()} disabled={busy === "Disarm"}>
            <ShieldOff className="h-4 w-4" />
            {busy === "Disarm" ? "Disarming…" : "Disarm"}
          </button>
        )}
      </div>

      {!isAdmin && (
        <p className="mt-3 text-xs text-slate-500">Arming and disarming require an administrator.</p>
      )}
    </article>
  );
}
