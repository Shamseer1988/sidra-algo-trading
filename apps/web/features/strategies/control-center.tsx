"use client";

import { FileText, Pencil, Radio, ShieldAlert, Trash2 } from "lucide-react";
import { type ReactNode, useCallback, useEffect, useState } from "react";

import { api, type PaperStrategy, type StrategyMode, type SwingStrategy } from "../../components/api";
import { Modal } from "../../components/modal";
import { StrategyEditor } from "./strategy-editor";

/**
 * Every strategy, intraday and swing, and what it is doing: Off, Paper or Live.
 *
 * The one place a strategy's mode changes. Each change is saved on its own,
 * after a confirmation that says what it will do -- and for Live, that it
 * sends real orders with real money. The server refuses Live without that
 * confirmation, and refuses swing Live altogether until the swing desk has
 * limits of its own.
 *
 * Each intraday row also carries its actions: Edit (the rules, in a popup; a
 * live strategy only after a real-money confirmation and a reason), Details,
 * and Delete for a strategy that is Off. Every confirmation is a popup in
 * front of the table, never a box that appears somewhere below it.
 */

export function modeOf(item: Pick<PaperStrategy, "enabled" | "live_orders">): StrategyMode {
  if (!item.enabled) return "OFF";
  return item.live_orders ? "LIVE" : "PAPER";
}

const MODE_TEXT: Record<StrategyMode, string> = {
  OFF: "Off — not evaluated, no signals.",
  PAPER: "Paper — signals and simulated fills only. No order reaches the broker.",
  LIVE: "Live — sends real orders with real money, through every live safety gate.",
};

const SWING_LABELS: Record<string, string> = {
  "swing-trend-pullback-v1": "Trend Pullback",
  "swing-base-breakout-v1": "Base Breakout",
};

type Pending = { desk: "intraday" | "swing"; id: string; name: string; from: StrategyMode; to: StrategyMode };

export function StrategyControlCenter({
  strategies,
  isAdmin,
  onIntradayChanged,
  onMessage,
  onDetails,
}: {
  strategies: PaperStrategy[];
  isAdmin: boolean;
  onIntradayChanged: (next: PaperStrategy[]) => void;
  onMessage: (message: string) => void;
  onDetails?: (id: string) => void;
}) {
  const [swing, setSwing] = useState<SwingStrategy[]>([]);
  const [pending, setPending] = useState<Pending | null>(null);
  const [editing, setEditing] = useState<PaperStrategy | null>(null);
  const [deleting, setDeleting] = useState<PaperStrategy | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);

  const loadSwing = useCallback(async () => {
    try {
      setSwing(await api.swingStrategies());
    } catch {
      setSwing([]);
    }
  }, []);

  useEffect(() => {
    void loadSwing();
  }, [loadSwing]);

  async function confirm() {
    if (!pending) return;
    setBusy(true);
    try {
      if (pending.desk === "intraday") {
        onIntradayChanged(await api.setStrategyMode(pending.id, pending.to, pending.to === "LIVE", reason || null));
      } else {
        setSwing(await api.setSwingStrategyMode(pending.id, pending.to, reason || null));
      }
      onMessage(`${pending.name} is now ${pending.to.toLowerCase()}.`);
      setPending(null);
      setReason("");
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Could not change the mode");
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (!deleting) return;
    setBusy(true);
    try {
      onIntradayChanged(await api.deleteStrategy(deleting.id));
      onMessage(`Deleted ${deleting.name}.`);
      setDeleting(null);
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Could not delete the strategy");
    } finally {
      setBusy(false);
    }
  }

  return (
    <article className="panel overflow-hidden" data-testid="strategy-control-center">
      <div className="flex items-center gap-3 border-b border-slate-800 px-5 py-4">
        <Radio className="h-5 w-5 text-emerald-400" />
        <div>
          <p className="eyebrow">What runs, and with what money</p>
          <h3 className="font-semibold text-white">Strategy control center</h3>
        </div>
      </div>
      <div className="table-scroll">
        <table className="terminal-table">
          <thead>
            <tr>
              <th>Strategy</th>
              <th>Desk</th>
              <th>Mode</th>
              <th>What it means</th>
              <th>Action</th>
            </tr>
          </thead>
          <tbody>
            {strategies.map((item) => (
              <ModeRow
                key={item.id}
                id={`intraday-${item.id}`}
                name={item.name}
                detail={`v${item.version}`}
                desk="Intraday"
                mode={modeOf(item)}
                isAdmin={isAdmin}
                liveBlocked={null}
                onPick={(to) => setPending({ desk: "intraday", id: item.id, name: item.name, from: modeOf(item), to })}
                actions={
                  <div className="flex gap-1.5">
                    {isAdmin && (
                      <button className="secondary-button px-2 py-1 text-xs" aria-label={`Edit ${item.name}`} onClick={() => setEditing(item)}>
                        <Pencil className="h-3.5 w-3.5" />
                        Edit
                      </button>
                    )}
                    {onDetails && (
                      <button className="secondary-button px-2 py-1 text-xs" aria-label={`Details of ${item.name}`} title="Details" onClick={() => onDetails(item.id)}>
                        <FileText className="h-3.5 w-3.5" />
                      </button>
                    )}
                    {isAdmin && modeOf(item) === "OFF" && (
                      <button className="secondary-button px-2 py-1 text-xs" aria-label={`Delete ${item.name}`} title="Delete" onClick={() => setDeleting(item)}>
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    )}
                  </div>
                }
              />
            ))}
            {swing.map((item) => (
              <ModeRow
                key={item.id}
                id={`swing-${item.id}`}
                name={SWING_LABELS[item.id] ?? item.name}
                detail={item.evidence}
                desk="Swing"
                mode={item.mode}
                isAdmin={isAdmin}
                liveBlocked={item.live_available ? null : item.live_blocked_reason}
                onPick={(to) =>
                  setPending({ desk: "swing", id: item.id, name: SWING_LABELS[item.id] ?? item.name, from: item.mode, to })
                }
                actions={<span className="text-xs text-slate-500">Mode only</span>}
              />
            ))}
          </tbody>
        </table>
      </div>

      {pending && (
        <Modal
          role="alertdialog"
          testId="mode-confirm"
          busy={busy}
          tone={pending.to === "LIVE" ? "danger" : "warning"}
          eyebrow={pending.desk === "intraday" ? "Intraday strategy" : "Swing strategy"}
          title={`${pending.name}: ${pending.from.toLowerCase()} → ${pending.to.toLowerCase()}?`}
          onClose={() => setPending(null)}
          footer={
            <>
              <button disabled={busy} onClick={() => setPending(null)} className="secondary-button">
                Cancel
              </button>
              <button disabled={busy} onClick={() => void confirm()} className={pending.to === "LIVE" ? "danger-button" : "primary-button"}>
                {pending.to === "LIVE" ? "Yes, trade this live" : `Yes, set to ${pending.to.toLowerCase()}`}
              </button>
            </>
          }
        >
          <p className={`flex items-center gap-2 text-sm ${pending.to === "LIVE" ? "font-semibold text-rose-300" : "text-slate-300"}`}>
            {pending.to === "LIVE" && <ShieldAlert className="h-4 w-4" />}
            {MODE_TEXT[pending.to]}
          </p>
          {pending.to === "LIVE" && (
            <p className="mt-2 text-xs font-semibold leading-5 text-rose-300">
              Real money. Orders go to the broker automatically under your current approval mode and limits.
            </p>
          )}
          <label className="field-label mt-4 block">
            Why (kept in the change history)
            <input
              className="field-input mt-1"
              value={reason}
              maxLength={300}
              onChange={(event) => setReason(event.target.value)}
              placeholder="e.g. forward test before going live"
            />
          </label>
        </Modal>
      )}

      {editing && (
        <StrategyEditor strategy={editing} onClose={() => setEditing(null)} onSaved={onIntradayChanged} onMessage={onMessage} />
      )}

      {deleting && (
        <Modal
          role="alertdialog"
          testId="delete-confirm"
          busy={busy}
          tone="warning"
          eyebrow="Intraday strategy"
          title={`Delete ${deleting.name}?`}
          onClose={() => setDeleting(null)}
          footer={
            <>
              <button disabled={busy} className="secondary-button" onClick={() => setDeleting(null)}>
                Cancel
              </button>
              <button disabled={busy} className="danger-button" onClick={() => void remove()}>
                Yes, delete it
              </button>
            </>
          }
        >
          <p className="text-sm text-slate-300">It is Off. Only the configuration goes; its past signals, trades and backtests are kept.</p>
        </Modal>
      )}
    </article>
  );
}

function ModeRow({
  id,
  name,
  detail,
  desk,
  mode,
  isAdmin,
  liveBlocked,
  onPick,
  actions,
}: {
  id: string;
  name: string;
  detail: string;
  desk: string;
  mode: StrategyMode;
  isAdmin: boolean;
  liveBlocked: string | null;
  onPick: (to: StrategyMode) => void;
  actions?: ReactNode;
}) {
  const tone: Record<StrategyMode, string> = {
    OFF: "border-slate-600 bg-slate-500/15 text-slate-200",
    PAPER: "border-sky-500/50 bg-sky-500/15 text-sky-300",
    LIVE: "border-rose-500/60 bg-rose-500/20 text-rose-300",
  };
  return (
    <tr data-testid={`mode-row-${id}`}>
      <td>
        {/* A fixed-width block: the table keeps every cell on one line, and a
            cell's own max-width does not hold in an auto-sized table. */}
        <div style={{ width: "13rem", whiteSpace: "normal" }}>
          <strong className="block text-white">{name}</strong>
          <span className="text-[11px] leading-4 opacity-75">{detail}</span>
        </div>
      </td>
      <td>{desk}</td>
      <td>
        <div className="inline-flex overflow-hidden rounded-md border border-slate-700" role="group" aria-label={`${name} mode`}>
          {(["OFF", "PAPER", "LIVE"] as StrategyMode[]).map((option) => {
            const blocked = option === "LIVE" && liveBlocked !== null;
            const active = option === mode;
            return (
              <button
                key={option}
                type="button"
                aria-pressed={active}
                title={blocked ? liveBlocked ?? "" : undefined}
                disabled={!isAdmin || active || blocked}
                onClick={() => onPick(option)}
                className={`px-3 py-1 text-xs font-semibold ${active ? tone[option] : "text-slate-400 hover:text-white"} ${blocked ? "cursor-not-allowed opacity-40" : ""}`}
              >
                {option === "OFF" ? "Off" : option === "PAPER" ? "Paper" : "Live"}
              </button>
            );
          })}
        </div>
      </td>
      <td className="text-xs leading-5 text-slate-400">
        <div style={{ width: "15rem", whiteSpace: "normal" }}>
          {liveBlocked && mode !== "LIVE" ? `${MODE_TEXT[mode]} ${liveBlocked}` : MODE_TEXT[mode]}
        </div>
      </td>
      <td>{actions}</td>
    </tr>
  );
}
