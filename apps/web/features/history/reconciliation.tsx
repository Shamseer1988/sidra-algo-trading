"use client";

import { AlertTriangle, CheckCircle2, Clock, Info } from "lucide-react";

import type { ReconciliationStatus } from "../../components/api";

/**
 * How a reconciliation status is drawn, in one place.
 *
 * The four statuses are four different instructions, and the colours have to
 * carry that. In particular "Estimated charges" is deliberately neutral rather
 * than a warning: it is the normal steady state of a reconciled day, because
 * brokers report charges aggregated over a date range and never per trade. A
 * screen that painted it amber would train an operator to ignore amber.
 */

const TONE: Record<ReconciliationStatus, { pill: string; icon: typeof Info }> = {
  MATCHED: { pill: "border-emerald-500/30 bg-emerald-500/10 text-emerald-300", icon: CheckCircle2 },
  ESTIMATED_CHARGES: { pill: "border-slate-700 bg-slate-800/60 text-slate-300", icon: Info },
  BROKER_DATA_PENDING: { pill: "border-sky-500/30 bg-sky-950/10 text-sky-300", icon: Clock },
  MISMATCH: { pill: "border-rose-500/40 bg-rose-950/20 text-rose-300", icon: AlertTriangle },
};

export function ReconciliationPill({
  status,
  label,
  title,
}: {
  status: ReconciliationStatus;
  label: string;
  title?: string;
}) {
  const tone = TONE[status] ?? TONE.ESTIMATED_CHARGES;
  const Icon = tone.icon;
  return (
    <span
      title={title}
      className={`badge-pill inline-flex items-center gap-1.5 whitespace-nowrap rounded border px-2 py-0.5 text-[11px] font-medium ${tone.pill}`}
    >
      <Icon className="h-3 w-3 shrink-0" />
      {label}
    </span>
  );
}

export function reconciliationTone(status: ReconciliationStatus) {
  return (TONE[status] ?? TONE.ESTIMATED_CHARGES).pill;
}
