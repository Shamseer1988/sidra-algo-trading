"use client";

import { CircleCheck, CircleX, LockKeyhole, RefreshCw, ShieldAlert } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { api, type LiveReadiness, type LiveReadinessHistory } from "../../components/api";
import { formatIstTimestamp, titleCase } from "../../lib/formatting";

type Props = { isAdmin: boolean; onMessage: (message: string) => void };

const empty: LiveReadiness = {
  status: "HARD_LOCKED",
  overall_ready: false,
  live_execution_available: false,
  checked_at: new Date(0).toISOString(),
  gates: [],
};

export function LiveReadinessWorkspace({ isAdmin, onMessage }: Props) {
  const [readiness, setReadiness] = useState<LiveReadiness>(empty);
  const [history, setHistory] = useState<LiveReadinessHistory[]>([]);
  const open = readiness.live_execution_available;
  const [loading, setLoading] = useState(true);
  const [verifying, setVerifying] = useState(false);
  const load = useCallback(() => {
    setLoading(true);
    void Promise.all([api.liveReadiness(), api.liveReadinessHistory()]).then(([report, checks]) => {
      setReadiness(report);
      setHistory(checks);
    }).catch((error: unknown) => {
      onMessage(error instanceof Error ? error.message : "Could not load live-readiness gates.");
    }).finally(() => setLoading(false));
  }, [onMessage]);

  useEffect(load, [load]);

  const verify = async () => {
    setVerifying(true);
    try {
      setReadiness(await api.verifyLiveReadiness());
      onMessage("Live-readiness review recorded. Recording a review arms nothing.");
      load();
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Live-readiness review failed.");
    } finally {
      setVerifying(false);
    }
  };

  return <section>
    <div className="page-toolbar">
      <div>
        <p className="eyebrow">Execution control plane</p>
        <h2 className="page-title">Live readiness gates</h2>
        <p className="page-copy">This workspace measures the prerequisites for live submission and records reviews of them. It cannot arm trading or transmit an order.</p>
      </div>
      <div className="flex gap-2"><button className="secondary-button" onClick={load} disabled={loading}><RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />Refresh</button>{isAdmin && <button className="primary-button" onClick={() => void verify()} disabled={verifying}><ShieldAlert className="h-4 w-4" />{verifying ? "Verifying…" : "Record review"}</button>}</div>
    </div>
    {/* This banner asserted a start-up refusal that was removed with the
        Release-1 locks. An operator checks this screen precisely to find out
        whether money is at risk, so it now reports the gates it just read
        rather than a constant. */}
    <article className={`mt-5 flex gap-3 rounded-md p-4 text-sm text-slate-200 ${open ? "glass-danger" : "glass-inset"}`}>
      {open
        ? <ShieldAlert className="mt-0.5 h-5 w-5 shrink-0 text-rose-300" />
        : <LockKeyhole className="mt-0.5 h-5 w-5 shrink-0 text-emerald-300" />}
      {open
        ? <p><strong className="text-white">Live submission is open.</strong> Every gate below has passed and an administrator has armed submission, so qualifying signals can reach the broker as real orders. The activation expires on its own; disarm to end it sooner. Recording a review here changes no trading control.</p>
        : <p><strong className="text-white">Live submission is held shut.</strong> The broker adapter, live risk engine, reconciliation and activation path all exist and are enforced. At least one gate below has not passed, so nothing can be submitted. Arming happens through the activation endpoint, never from this screen, and recording a review here changes no trading control.</p>}
    </article>
    <div className="mt-6 grid gap-3 md:grid-cols-3"><Metric label="Execution" value={open ? "Open" : "Held"} tone={open ? "text-rose-300" : "text-emerald-300"} /><Metric label="Gate status" value={titleCase(readiness.status)} tone="text-amber-300" /><Metric label="Last inspection" value={readiness.checked_at === empty.checked_at ? "—" : formatIstTimestamp(readiness.checked_at)} /></div>
    <article className="panel mt-6 overflow-hidden"><div className="border-b border-slate-800 px-5 py-4"><p className="eyebrow">Explicit controls</p><h3 className="font-semibold text-white">Activation prerequisites</h3></div><div className="divide-y divide-slate-800">{readiness.gates.map((gate) => <div className="flex gap-4 px-5 py-4" key={gate.key}>{gate.passed ? <CircleCheck className="mt-0.5 h-5 w-5 shrink-0 text-emerald-300" /> : <CircleX className="mt-0.5 h-5 w-5 shrink-0 text-rose-300" />}<div><p className="font-medium text-white">{gate.label}</p><p className="mt-1 text-sm leading-6 text-slate-400">{gate.detail}</p></div></div>)}</div></article>
    <article className="panel mt-6 overflow-hidden"><div className="border-b border-slate-800 px-5 py-4"><p className="eyebrow">Immutable audit trail</p><h3 className="font-semibold text-white">Recorded reviews</h3></div>{history.length ? <div className="table-scroll"><table className="terminal-table"><thead><tr><th>Status</th><th>Execution</th><th>Checked at</th></tr></thead><tbody>{history.map((check) => <tr key={check.id}><td><span className="status-pill status-danger">{titleCase(check.status)}</span></td><td>{check.overall_ready ? "Open" : "Held"}</td><td className="muted-cell">{formatIstTimestamp(check.checked_at)}</td></tr>)}</tbody></table></div> : <div className="empty-inset m-5 p-8 text-center text-sm text-slate-400">No readiness review has been recorded. Recording one does not change any trading control.</div>}</article>
  </section>;
}

function Metric({ label, value, tone = "text-white" }: { label: string; value: string; tone?: string }) {
  return <article className="glass-inset rounded-md p-4"><p className="eyebrow">{label}</p><p className={`mt-2 text-lg font-semibold ${tone}`}>{value}</p></article>;
}
