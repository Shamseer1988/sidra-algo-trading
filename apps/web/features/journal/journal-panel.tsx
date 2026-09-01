import { Download, FileCheck2 } from "lucide-react";

import type { PaperSignal } from "../../components/api";
import { formatIstTimestamp, titleCase } from "../../lib/formatting";

export function JournalPanel({ signals }: { signals: PaperSignal[] }) {
  return <section><div className="page-toolbar"><div><p className="eyebrow">Paper tracking</p><h2 className="page-title">Journal</h2><p className="page-copy">Export the server-side paper journal. It contains scanner decisions only and never represents broker orders.</p></div><a href="/api/v1/journal/export.csv" download="paper-journal.csv" className="primary-button"><Download className="h-4 w-4" />Export CSV</a></div><article className="panel mt-6 p-6"><div className="flex items-start gap-3"><FileCheck2 className="mt-1 h-5 w-5 text-emerald-400" /><div><h3 className="font-semibold text-white">{signals.length} recorded paper signal{signals.length === 1 ? "" : "s"}</h3><p className="mt-2 text-sm leading-6 text-slate-400">The detailed journal analytics workspace is planned. This release keeps the existing export path available without inventing P&amp;L or execution data.</p></div></div>{signals.length > 0 && <div className="data-list mt-6">{signals.slice(0, 5).map((signal) => <div key={signal.id}><dt>{signal.instrument_token} · {signal.side}</dt><dd>{titleCase(signal.status)} · {formatIstTimestamp(signal.created_at)}</dd></div>)}</div>}</article></section>;
}
