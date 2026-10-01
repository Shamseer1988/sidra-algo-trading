"use client";

import { PnlCalendar } from "./pnl-calendar";
import { PnlSummary } from "./pnl-summary";

/**
 * Reports: the two ways of looking back.
 *
 * Separate from History, which is the record itself — every trade, with its
 * entry, exit, stop and reconciliation status. Reports is what the record adds
 * up to: a month of days as a calendar, and the realised/unrealised split as
 * it stands now. Someone asking "how did October go" and someone asking "what
 * happened on that BHARTIARTL trade" want different screens, and giving them
 * one screen made both harder to read.
 */

export function ReportsWorkspace({ tab, onMessage }: { tab: string; onMessage: (message: string) => void }) {
  return tab === "pnl" ? <PnlSummary onMessage={onMessage} /> : <PnlCalendar onMessage={onMessage} />;
}
