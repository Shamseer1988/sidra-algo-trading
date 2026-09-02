"use client";

import { CheckCircle2, RefreshCw, ShieldCheck, XCircle } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { api, type AssistedApproval } from "../../components/api";
import { formatIstTimestamp, titleCase } from "../../lib/formatting";

type Props = { isAdmin: boolean; onMessage: (message: string) => void };

export function AssistedTradingWorkspace({ isAdmin, onMessage }: Props) {
  const [approvals, setApprovals] = useState<AssistedApproval[]>([]);
  const [loading, setLoading] = useState(true);
  const [processing, setProcessing] = useState<string | null>(null);

  const load = useCallback(() => {
    setLoading(true);
    void api.assistedApprovals().then(setApprovals).catch((error: unknown) => {
      onMessage(error instanceof Error ? error.message : "Unable to load approval requests.");
    }).finally(() => setLoading(false));
  }, [onMessage]);

  useEffect(load, [load]);

  const decide = async (referenceId: string, decision: "APPROVE" | "REJECT") => {
    setProcessing(referenceId);
    try {
      const updated = await api.decideAssistedApproval(referenceId, decision);
      setApprovals((items) => items.map((item) => item.reference_id === referenceId ? updated : item));
      onMessage(`${decision === "APPROVE" ? "Approval recorded" : "Approval rejected"}. No broker order was submitted.`);
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Approval decision failed.");
    } finally {
      setProcessing(null);
    }
  };

  return <section>
    <div className="page-toolbar">
      <div>
        <p className="eyebrow">Human confirmation · paper-only</p>
        <h2 className="page-title">Assisted trading</h2>
        <p className="page-copy">Every approval is authenticated, deduplicated, expiry-checked, and revalidated against the paper risk ledger. Live broker submission is unavailable.</p>
      </div>
      <button className="secondary-button" onClick={load}><RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />Refresh</button>
    </div>
    <article className="glass-notice mt-5 flex gap-3 rounded-md p-4 text-sm text-slate-300">
      <ShieldCheck className="mt-0.5 h-5 w-5 shrink-0 text-sky-300" />
      <p><strong className="text-white">Submission boundary active.</strong> An approved request may reserve paper risk, but it cannot create or transmit a broker order.</p>
    </article>
    <article className="panel mt-6 overflow-hidden">
      <div className="flex items-center justify-between border-b border-slate-800 px-5 py-4">
        <div><p className="eyebrow">Decision ledger</p><h3 className="font-semibold text-white">Authenticated approval intents</h3></div>
        <span className="status-pill status-watch">{approvals.length} tracked</span>
      </div>
      {approvals.length ? <div className="table-scroll"><table className="terminal-table"><thead><tr><th>Reference</th><th>Source</th><th>Decision</th><th>Expiry</th><th>Risk check</th><th>Submission boundary</th><th>Action</th></tr></thead><tbody>{approvals.map((item) => {
        const canDecide = isAdmin && ["PENDING", "RECORDED"].includes(item.status);
        return <tr key={item.reference_id}>
          <td><strong>{item.reference_id}</strong><span>{formatIstTimestamp(item.created_at)}</span></td>
          <td>{titleCase(item.source)}</td>
          <td><span className={`status-pill ${item.status === "APPROVED_PAPER_ONLY" ? "status-active" : item.status === "REJECTED" || item.status === "EXPIRED" ? "status-danger" : "status-watch"}`}>{titleCase(item.status.replaceAll("_", " "))}</span></td>
          <td className="muted-cell">{item.expires_at ? formatIstTimestamp(item.expires_at) : "No expiry"}</td>
          <td className="muted-cell">{item.risk_revalidated_at ? formatIstTimestamp(item.risk_revalidated_at) : "Not run"}</td>
          <td className="max-w-56 text-slate-400">{item.submission_block_reason ?? "Always blocked"}</td>
          <td>{canDecide ? <div className="flex gap-2"><button aria-label={`Approve ${item.reference_id}`} disabled={processing === item.reference_id} onClick={() => void decide(item.reference_id, "APPROVE")} className="secondary-button !px-2 !py-1 text-emerald-200"><CheckCircle2 className="h-3.5 w-3.5" />Approve</button><button aria-label={`Reject ${item.reference_id}`} disabled={processing === item.reference_id} onClick={() => void decide(item.reference_id, "REJECT")} className="secondary-button !px-2 !py-1 text-rose-200"><XCircle className="h-3.5 w-3.5" />Reject</button></div> : <span className="muted-cell">Locked</span>}</td>
        </tr>;
      })}</tbody></table></div> : <div className="empty-inset m-5 p-8 text-center text-sm text-slate-400">No approval intents yet. Authenticated Telegram callbacks and protected web requests are recorded here.</div>}
    </article>
  </section>;
}
