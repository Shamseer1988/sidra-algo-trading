import { Construction, LockKeyhole } from "lucide-react";

import { workspaceMeta, type WorkspaceId } from "../../lib/navigation";

const notes: Partial<Record<WorkspaceId, string>> = {
  orders: "Order lifecycle and broker submission are not implemented in this PAPER release.",
  positions: "Position tracking requires a verified order and execution model that is not present yet.",
  performance: "Performance reporting awaits the paper journal analytics release.",
  backtesting: "Backtesting will be added after the historical data and research workflow are verified.",
  automation: "Automation rules require explicit server-side rule evaluation and audit controls.",
  scheduler: "Scheduled automation is not available until its persistent scheduler and observability are released.",
  users: "User administration remains API-managed until the dedicated role-management workflow is released.",
};

export function UnavailableWorkspace({ workspace }: { workspace: WorkspaceId }) {
  const meta = workspaceMeta[workspace];
  return <section data-testid="unavailable-workspace"><div className="panel max-w-3xl p-7 sm:p-10"><Construction className="h-8 w-8 text-emerald-400" /><p className="eyebrow mt-6">Planned workspace</p><h2 className="page-title">{meta.title} is not available yet</h2><p className="page-copy">{notes[workspace] ?? "This workspace is intentionally withheld until its server-side capability is ready."}</p><div className="glass-inset mt-7 flex gap-3 rounded-md p-4 text-sm text-slate-400"><LockKeyhole className="h-5 w-5 shrink-0 text-slate-500" /><p>No sample orders, prices, performance, or user records are shown here. The PAPER execution boundary remains locked.</p></div></div></section>;
}
