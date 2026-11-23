"use client";

import { AlertTriangle, Info, OctagonAlert, X } from "lucide-react";

import { deskNotice, type DeskState } from "../../lib/desks";
import { istDate } from "../../lib/strategy-names";
import { useDismissed } from "../../lib/use-dismissed";

const TONES = {
  info: { icon: Info, className: "border-sky-500/30 bg-sky-950/20 text-sky-100", accent: "text-sky-300" },
  warn: { icon: AlertTriangle, className: "border-amber-500/30 bg-amber-950/20 text-amber-100", accent: "text-amber-300" },
  bad: { icon: OctagonAlert, className: "border-rose-500/40 bg-rose-950/25 text-rose-100", accent: "text-rose-300" },
} as const;

/**
 * One line above every page whenever the desks are not in their usual state.
 *
 * An information line stays closed until its message changes; a warning comes
 * back the next day; "both desks off" cannot be closed, because nothing trades.
 */
export function DeskBanner({ state, onOpenSettings }: { state: DeskState; onOpenSettings: () => void }) {
  const notice = deskNotice(state);
  const [closed, close] = useDismissed("desk", `${notice?.title}${notice?.tone === "warn" ? istDate() : ""}`);
  if (!notice || (closed && notice.tone !== "bad")) return null;
  const tone = TONES[notice.tone];
  const Icon = tone.icon;
  return (
    <div role="status" data-testid="desk-banner" className={`mb-4 flex flex-wrap items-start gap-3 rounded-md border p-3 text-sm ${tone.className}`}>
      <Icon className={`mt-0.5 h-4 w-4 shrink-0 ${tone.accent}`} />
      <div className="min-w-0 flex-1">
        <p className="font-semibold">{notice.title}</p>
        <p className="mt-0.5 text-xs leading-5 opacity-90">{notice.body}</p>
      </div>
      <button onClick={onOpenSettings} className="secondary-button shrink-0 px-3 py-1.5 text-xs">
        Trading desks
      </button>
      {notice.tone !== "bad" && (
        <button onClick={close} aria-label="Close this notice" className="secondary-button shrink-0 px-2 py-1.5">
          <X className="h-4 w-4" />
        </button>
      )}
    </div>
  );
}
