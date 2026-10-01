"use client";

import { Play, ShieldAlert, ShieldOff } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { api, type TradingStatus } from "../../components/api";

/**
 * Is it trading, and if not, what is in the way.
 *
 * Seven separate controls can stop this system: the runtime mode, the
 * emergency stop, the scanner's control state, the paper-tracking flag, the
 * administrator activation, the approval mode and the selected broker. Three
 * stop the scanner and four stop live orders. Each had its own card and each
 * described itself correctly, and together they produced a screen the operator
 * could not act on — asked to quieten some Telegram messages, he came one
 * click from pressing "Disable paper tracking", which halts candle evaluation,
 * signals and live orders alike.
 *
 * So: one card, three lines, two buttons.
 *
 *   headline   what is happening
 *   detail     why
 *   remedy     what to do about it
 *
 * The decision of WHICH blocker to name when several are failing is made on
 * the server, because it is a safety judgement and a judgement made in a React
 * component is one nobody can test or audit. This file renders an answer; it
 * does not compute one.
 *
 * Pause is Disarm. Disarming already stops new entries while leaving exits,
 * the journal and the scanner running, which is exactly what pause should
 * mean — so this calls it that rather than adding a second switch with the
 * same effect under a different name.
 */

const TONE: Record<TradingStatus["state"], { ring: string; dot: string; label: string }> = {
  TRADING: { ring: "border-emerald-500/30", dot: "bg-emerald-400", label: "text-emerald-300" },
  PAUSED: { ring: "border-amber-500/30", dot: "bg-amber-400", label: "text-amber-300" },
  BLOCKED: { ring: "border-rose-500/30", dot: "bg-rose-400", label: "text-rose-300" },
  STOPPED: { ring: "border-rose-500/40", dot: "bg-rose-500", label: "text-rose-300" },
  PAPER: { ring: "border-sky-500/30", dot: "bg-sky-400", label: "text-sky-300" },
};

export function TradingCard({
  isAdmin,
  onMessage,
  refreshKey,
}: {
  isAdmin: boolean;
  onMessage: (message: string) => void;
  refreshKey?: number;
}) {
  const [status, setStatus] = useState<TradingStatus | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setStatus(await api.tradingStatus());
    } catch {
      // Deliberately not surfaced as a message and deliberately not left as
      // null: this card owns the emergency stop, and a card that renders
      // nothing when a *reporting* endpoint fails takes the emergency stop off
      // the screen with it. The status is unknown; the button is not.
      setStatus(null);
    }
  }, []);

  useEffect(() => {
    void load();
    // Polled because the things that change it — a lapsed activation, a
    // reconciliation going stale, the scanner dying — all happen without
    // anybody pressing anything on this screen.
    const timer = setInterval(() => void load(), 20000);
    return () => clearInterval(timer);
  }, [load, refreshKey]);

  // Rendered even with no status. An operator who cannot be told what is
  // happening is exactly the operator most likely to want to stop everything.
  const tone = status ? (TONE[status.state] ?? TONE.BLOCKED) : TONE.BLOCKED;

  async function act(run: () => Promise<unknown>, done: string) {
    setBusy(true);
    try {
      await run();
      onMessage(done);
      await load();
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "That did not work");
    } finally {
      setBusy(false);
    }
  }

  return (
    <article className={`panel mt-6 border p-6 sm:p-7 ${tone.ring}`}>
      <div className="flex items-center gap-2">
        <span className={`h-2 w-2 rounded-full ${tone.dot}`} />
        <p className={`eyebrow ${tone.label}`}>{status ? status.state : "UNKNOWN"}</p>
      </div>

      <h3 className="mt-2 text-xl font-semibold text-white">
        {status ? status.headline : "Trading status is unavailable"}
      </h3>
      <p className="mt-3 max-w-3xl text-sm leading-6 text-slate-300">
        {status
          ? status.detail
          : "This screen cannot reach the status endpoint, so it cannot say whether anything is trading. The gates themselves are unaffected and still enforce on every order."}
      </p>
      <p className="mt-2 max-w-3xl text-sm leading-6 text-slate-500">
        {status ? status.remedy : "Emergency stop still works. Check the API container if this persists."}
      </p>

      {isAdmin && (
        <div className="mt-6 flex flex-wrap gap-3">
          {status?.can_pause && (
            <button
              disabled={busy}
              onClick={() => void act(() => api.disarmLive(), "Paused. Open positions are still managed.")}
              className="secondary-button"
            >
              <ShieldOff className="h-4 w-4" />
              Pause live orders
            </button>
          )}
          {status?.can_resume && (
            <button
              disabled={busy}
              onClick={() => void act(() => api.armLive("Resumed from the trading card"), "Resumed.")}
              className="primary-button"
            >
              <Play className="h-4 w-4" />
              Resume live orders
            </button>
          )}
          {status?.emergency_stop_active ? (
            <button disabled={busy} onClick={() => void act(() => api.clearEmergencyStop(), "Emergency stop cleared.")} className="secondary-button">
              Clear emergency stop
            </button>
          ) : (
            <button
              disabled={busy}
              onClick={() => {
                const reason = window.prompt("Why are you stopping? This is recorded.");
                if (reason && reason.trim().length >= 3) {
                  void act(() => api.emergencyStop(reason.trim()), "Emergency stop engaged.");
                }
              }}
              className="danger-button"
            >
              <ShieldAlert className="h-4 w-4" />
              Emergency stop
            </button>
          )}
        </div>
      )}

      <p className="mt-4 text-xs leading-5 text-slate-600">
        Nothing on this card reaches outside this application. It does not close a position already open at your
        broker — use the broker app for that.
      </p>
    </article>
  );
}
