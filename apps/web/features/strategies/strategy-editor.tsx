"use client";

import { ShieldAlert } from "lucide-react";
import { useEffect, useState } from "react";

import { api, type BuilderInstrument, type PaperStrategy } from "../../components/api";
import { Modal } from "../../components/modal";
import { modeOf } from "./control-center";
import { StrategyRulesForm } from "./rules-form";

/**
 * The control center's Edit popup: one strategy's rules, never its mode.
 *
 * A live strategy can be edited, but only after saying so: the popup turns
 * red, asks for a reason and a tick that this changes a strategy trading real
 * money, and the server refuses the save without both.
 */
export function StrategyEditor({
  strategy,
  onClose,
  onSaved,
  onMessage,
}: {
  strategy: PaperStrategy;
  onClose: () => void;
  onSaved: (next: PaperStrategy[]) => void;
  onMessage: (message: string) => void;
}) {
  const [draft, setDraft] = useState<PaperStrategy>(strategy);
  const [instruments, setInstruments] = useState<BuilderInstrument[]>([]);
  const [reason, setReason] = useState("");
  const [understood, setUnderstood] = useState(false);
  const [busy, setBusy] = useState(false);
  const live = modeOf(strategy) === "LIVE";
  const changed = JSON.stringify(draft) !== JSON.stringify(strategy);
  const ready = changed && draft.name.trim().length >= 3 && (!live || (understood && reason.trim().length > 0));

  useEffect(() => {
    void api.builderInstruments().then(setInstruments).catch(() => setInstruments([]));
  }, []);

  async function save() {
    setBusy(true);
    try {
      onSaved(await api.saveStrategy(strategy.id, draft, reason, live));
      onMessage(`Saved the rules of ${draft.name}. Its mode is unchanged.`);
      onClose();
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Could not save the strategy");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      wide
      busy={busy}
      tone={live ? "danger" : "default"}
      testId="strategy-editor"
      eyebrow={`Edit rules · v${strategy.version} · ${modeOf(strategy).toLowerCase()}`}
      title={strategy.name}
      onClose={onClose}
      footer={
        <>
          <span className="mr-auto self-center text-xs text-slate-500">
            {changed ? "Saving a change makes a new version and is recorded in the history." : "Nothing changed yet."}
          </span>
          <button className="secondary-button" disabled={busy} onClick={onClose}>
            Cancel
          </button>
          <button className={live ? "danger-button" : "primary-button"} disabled={busy || !ready} onClick={() => void save()}>
            {busy ? "Saving…" : live ? "Save live strategy" : "Save changes"}
          </button>
        </>
      }
    >
      {live && (
        <div className="mb-4 rounded-md border border-rose-500/50 bg-rose-950/30 p-3 text-sm text-rose-100" data-testid="live-edit-warning">
          <p className="flex items-center gap-2 font-semibold">
            <ShieldAlert className="h-4 w-4" />
            This strategy is live and trades real money.
          </p>
          <p className="mt-1 text-xs leading-5 text-rose-200/90">
            A change takes effect from its next signal. Change one thing at a time, so its effect can be told apart.
          </p>
        </div>
      )}
      <StrategyRulesForm draft={draft} onChange={setDraft} instruments={instruments} />
      <div className="mt-5 grid gap-3 border-t border-slate-800 pt-4">
        <label className="field-label">
          Why {live ? "(required for a live strategy)" : "(kept in the change history)"}
          <input className="field-input mt-1" value={reason} maxLength={300} onChange={(event) => setReason(event.target.value)} />
        </label>
        {live && (
          <label className="flex items-center gap-2 text-sm text-rose-100">
            <input type="checkbox" checked={understood} onChange={(event) => setUnderstood(event.target.checked)} />
            I understand this changes a strategy that trades real money.
          </label>
        )}
      </div>
    </Modal>
  );
}
