"use client";

import { ArrowRight, RotateCcw, Save } from "lucide-react";
import { type ReactNode, useState } from "react";

import { Hint } from "../../components/hint";
import { Modal } from "../../components/modal";

/**
 * The pieces every Settings tab is built from, so each tab looks and behaves
 * the same: a section card with a one-line heading, a field grid, a sticky bar
 * that counts unsaved changes, and a popup that shows every change, before and
 * after, before anything is saved.
 */

export function SettingsSection({
  title,
  eyebrow,
  hint,
  actions,
  testId,
  children,
}: {
  title: string;
  eyebrow?: string;
  hint?: ReactNode;
  actions?: ReactNode;
  testId?: string;
  children: ReactNode;
}) {
  return (
    <article className="panel p-4 sm:p-5" data-testid={testId}>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          {eyebrow && <p className="eyebrow">{eyebrow}</p>}
          <h3 className="flex items-center gap-1.5 text-base font-semibold text-white">
            {title}
            {hint && <Hint label={`About ${title}`}>{hint}</Hint>}
          </h3>
        </div>
        {actions && <div className="flex flex-wrap gap-2">{actions}</div>}
      </div>
      <div className="mt-4">{children}</div>
    </article>
  );
}

export function FieldGrid({ children }: { children: ReactNode }) {
  return <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">{children}</div>;
}

export type SectionTab = { id: string; label: string; edited?: number };

/** Sub-sections inside one Settings tab, so a tab is one screen, not a scroll. */
export function SectionNav({
  sections,
  active,
  onSelect,
}: {
  sections: SectionTab[];
  active: string;
  onSelect: (id: string) => void;
}) {
  return (
    <nav className="flex flex-wrap gap-2" aria-label="Sections" data-testid="settings-sections">
      {sections.map((section) => {
        const on = section.id === active;
        return (
          <button
            key={section.id}
            type="button"
            aria-pressed={on}
            onClick={() => onSelect(section.id)}
            className={`inline-flex items-center gap-2 rounded-full border px-3.5 py-1.5 text-xs font-semibold transition ${
              on
                ? "border-emerald-500/60 bg-emerald-500/15 text-emerald-300"
                : "border-slate-700 text-slate-400 hover:border-slate-500 hover:text-slate-200"
            }`}
          >
            {section.label}
            {Boolean(section.edited) && (
              <span className="rounded-full bg-sky-500/20 px-1.5 text-[10px] text-sky-300" title="Unsaved changes">
                {section.edited}
              </span>
            )}
          </button>
        );
      })}
    </nav>
  );
}

/** Sticky at the bottom of the tab while anything is unsaved. */
export function SaveBar({
  count,
  busy = false,
  blocked,
  onDiscard,
  onReview,
}: {
  count: number;
  busy?: boolean;
  blocked?: string | null;
  onDiscard: () => void;
  onReview: () => void;
}) {
  if (!count) return null;
  return (
    <div
      className="sticky bottom-4 z-20 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-sky-500/40 bg-[var(--surface)] px-4 py-3 shadow-xl shadow-black/30"
      data-testid="save-bar"
    >
      <p className="text-sm text-slate-200">
        <span className="font-semibold text-sky-300">{count}</span> unsaved change{count === 1 ? "" : "s"}
        {blocked && <span className="ml-2 text-xs text-amber-300">{blocked}</span>}
      </p>
      <div className="flex gap-2">
        <button type="button" className="secondary-button py-2" disabled={busy} onClick={onDiscard}>
          <RotateCcw className="h-4 w-4" />
          Discard
        </button>
        <button type="button" className="primary-button py-2" disabled={busy || Boolean(blocked)} onClick={onReview}>
          <Save className="h-4 w-4" />
          Review and save
        </button>
      </div>
    </div>
  );
}

export type Change = { key: string; label: string; from: string; to: string };

/**
 * Every change, before and after, in a popup. `requireReason` adds a reason
 * box the save cannot go without; `warning` is shown above the list.
 */
export function ChangesModal({
  title,
  changes,
  warning,
  tone = "warning",
  requireReason = false,
  confirmLabel = "Save changes",
  onConfirm,
  onClose,
}: {
  title: string;
  changes: Change[];
  warning?: ReactNode;
  tone?: "default" | "warning" | "danger";
  requireReason?: boolean;
  confirmLabel?: string;
  onConfirm: (reason: string) => Promise<boolean>;
  onClose: () => void;
}) {
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const ready = !requireReason || reason.trim().length > 0;
  return (
    <Modal
      title={title}
      eyebrow="Review before saving"
      tone={tone}
      busy={busy}
      role="alertdialog"
      testId="changes-modal"
      onClose={onClose}
      footer={
        <>
          <button type="button" className="secondary-button" disabled={busy} onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className={tone === "danger" ? "danger-button" : "primary-button"}
            disabled={busy || !ready}
            onClick={async () => {
              setBusy(true);
              const done = await onConfirm(reason.trim());
              setBusy(false);
              if (done) onClose();
            }}
          >
            <Save className="h-4 w-4" />
            {confirmLabel}
          </button>
        </>
      }
    >
      {warning && <div className="mb-4 rounded-md border border-amber-500/30 bg-amber-950/20 p-3 text-xs leading-5 text-amber-100">{warning}</div>}
      <ul className="space-y-2" data-testid="changes-list">
        {changes.map((change) => (
          <li key={change.key} className="glass-inset rounded-md px-3 py-2 text-sm">
            <p className="font-semibold text-white">{change.label}</p>
            <p className="numeric mt-0.5 flex flex-wrap items-center gap-2 text-xs">
              <span className="text-slate-400 line-through decoration-slate-600">{change.from}</span>
              <ArrowRight className="h-3 w-3 text-slate-500" />
              <span className="text-sky-300">{change.to}</span>
            </p>
          </li>
        ))}
      </ul>
      {requireReason && (
        <label className="field-label mt-4 block">
          Why (kept in the change history)
          <input
            className="field-input mt-1"
            maxLength={300}
            value={reason}
            placeholder="e.g. after reviewing last week's trades"
            onChange={(event) => setReason(event.target.value)}
          />
        </label>
      )}
    </Modal>
  );
}

export function Tile({ label, value, note, tone }: { label: string; value: string; note?: string; tone?: "warn" }) {
  return (
    <div className="glass-inset rounded-md p-3.5">
      <p className="eyebrow">{label}</p>
      <p className={`numeric mt-1.5 text-lg font-semibold ${tone === "warn" ? "text-amber-300" : "text-white"}`}>{value}</p>
      {note && <p className="mt-0.5 text-xs text-slate-500">{note}</p>}
    </div>
  );
}

/** A value as a person reads it, for the before/after list. */
export function shown(value: unknown, unit?: string): string {
  if (value === true) return "On";
  if (value === false) return "Off";
  if (value === "" || value === null || value === undefined) return "(empty)";
  if (unit === "INR") return `₹${Number(value).toLocaleString("en-IN")}`;
  if (unit === "PERCENT") return `${value}%`;
  return String(value);
}

/**
 * One provider as a card: a broker, a bot, a rate card. Pressing it opens the
 * full settings in a popup. `comingSoon` draws it greyed out with no action:
 * a place the next broker will go, not a working connection.
 */
export function ProviderCard({
  name,
  monogram,
  tone = "emerald",
  tagline,
  status,
  comingSoon = false,
  actionLabel = "Open settings",
  testId,
  onOpen,
}: {
  name: string;
  monogram: string;
  tone?: "emerald" | "sky" | "violet" | "amber";
  tagline: string;
  status?: ReactNode;
  comingSoon?: boolean;
  actionLabel?: string;
  testId?: string;
  onOpen?: () => void;
}) {
  const tones = {
    emerald: "bg-emerald-500/15 text-emerald-300 border-emerald-500/40",
    sky: "bg-sky-500/15 text-sky-300 border-sky-500/40",
    violet: "bg-violet-500/15 text-violet-300 border-violet-500/40",
    amber: "bg-amber-500/15 text-amber-300 border-amber-500/40",
  };
  return (
    <button
      type="button"
      data-testid={testId}
      disabled={comingSoon || !onOpen}
      onClick={onOpen}
      aria-label={comingSoon ? `${name} (coming soon)` : `${name}: ${actionLabel}`}
      className={`glass-inset group flex h-full flex-col rounded-lg p-4 text-left transition ${
        comingSoon ? "cursor-not-allowed opacity-55" : "hover:-translate-y-0.5 hover:border-emerald-500/50"
      }`}
    >
      <span className="flex w-full items-center gap-3">
        <span className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border text-sm font-bold ${tones[tone]}`}>
          {monogram}
        </span>
        <span className="min-w-0 flex-1">
          <span className="block text-base font-semibold text-white">{name}</span>
          <span className="block truncate text-xs text-slate-400">{tagline}</span>
        </span>
        {comingSoon && <span className="status-pill status-watch">Coming soon</span>}
      </span>
      {status && <span className="mt-3 flex flex-wrap gap-1.5">{status}</span>}
      {!comingSoon && onOpen && (
        <span className="mt-auto flex items-center gap-1 pt-3 text-xs font-semibold text-emerald-400 group-hover:underline">
          {actionLabel}
          <ArrowRight className="h-3.5 w-3.5" />
        </span>
      )}
    </button>
  );
}
