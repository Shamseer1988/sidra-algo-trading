"use client";

import { CalendarDays, RefreshCw, ShieldCheck, TriangleAlert } from "lucide-react";

import type { DataQuality, MarketSession, Overview, ScannerStatus } from "../../components/api";
import { formatIstTimestamp, statusTone, titleCase } from "../../lib/formatting";

export function MarketPanel({
  session,
  quality,
  overview,
  scanner,
  onRefresh,
}: {
  session: MarketSession | null;
  quality: DataQuality[];
  overview: Overview;
  scanner: ScannerStatus;
  onRefresh: () => void;
}) {
  const qualityGood = quality.filter((item) => item.allows_signals).length;
  return (
    <section>
      <div className="page-toolbar">
        <div>
          <p className="eyebrow">Market intelligence</p>
          <h2 className="page-title">Market state</h2>
          <p className="page-copy">Session and feed quality are supplied by the backend. Prices are intentionally omitted until a verified market panel is released.</p>
        </div>
        <button className="secondary-button" onClick={onRefresh}><RefreshCw className="h-4 w-4" />Refresh market state</button>
      </div>

      <div className="mt-6 grid gap-4 lg:grid-cols-[.85fr_1.15fr]">
        <article className="panel p-5 sm:p-6">
          <div className="flex items-start justify-between gap-3"><div><p className="eyebrow">NSE session</p><h3 className="mt-1 text-lg font-semibold text-white">{session ? titleCase(session.phase) : "Awaiting backend state"}</h3></div><CalendarDays className="h-5 w-5 text-emerald-400" /></div>
          {session ? <dl className="data-list mt-5"><div><dt>Trading day</dt><dd>{session.trading_day ? "Yes" : "No"}</dd></div><div><dt>Session date</dt><dd>{session.session_date ?? "—"}</dd></div><div><dt>Regular session</dt><dd>{session.regular_open && session.regular_close ? `${session.regular_open}–${session.regular_close} IST` : "Not scheduled"}</dd></div><div><dt>Evaluated at</dt><dd className="numeric">{formatIstTimestamp(session.local_timestamp)}</dd></div></dl> : <p className="empty-inset mt-5">The market calendar endpoint is not yet available from this environment. Core scanner operations remain unaffected.</p>}
          <p className="mt-5 text-sm leading-6 text-slate-400">{session?.reason ?? "Session details will appear after the next successful backend refresh."}</p>
        </article>

        <article className="panel p-5 sm:p-6">
          <div className="flex items-start justify-between gap-3"><div><p className="eyebrow">Ingestion status</p><h3 className="mt-1 text-lg font-semibold text-white">{titleCase(overview.market_data.status)}</h3></div><span className={`status-pill ${statusTone(overview.market_data.status)}`}>{titleCase(overview.market_data.status)}</span></div>
          <p className="mt-4 text-sm leading-6 text-slate-400">{overview.market_data.detail}</p>
          <div className="mt-5 grid gap-3 sm:grid-cols-3"><Metric label="Scanner" value={titleCase(scanner.status)} /><Metric label="Tracked instruments" value={String(quality.length)} /><Metric label="Signal eligible" value={String(qualityGood)} /></div>
        </article>
      </div>

      <section className="panel mt-4 overflow-hidden">
        <div className="flex items-center justify-between gap-4 border-b border-slate-800 px-5 py-4"><div><p className="eyebrow">Data quality</p><h3 className="mt-1 text-base font-semibold text-white">Instrument readiness</h3></div>{quality.length ? <span className="text-xs text-slate-500">Backend observed timestamps only</span> : <TriangleAlert className="h-4 w-4 text-slate-500" />}</div>
        {quality.length ? <div className="table-scroll"><table className="terminal-table"><thead><tr><th>Instrument</th><th>State</th><th>Bars</th><th>Ticks</th><th>Latency</th><th>Signal gate</th></tr></thead><tbody>{quality.map((item) => <tr key={item.instrument_token}><td><strong>{item.instrument_token}</strong><span>{item.reason}</span></td><td><span className={`status-pill ${statusTone(item.state)}`}>{item.state}</span></td><td className="numeric">{item.received_bars}/{item.expected_bars}</td><td className="numeric">{item.received_ticks}</td><td className="numeric">{item.average_latency_ms.toFixed(0)} ms</td><td>{item.allows_signals ? <span className="inline-flex items-center gap-1 text-emerald-300"><ShieldCheck className="h-4 w-4" />Allowed</span> : <span className="text-amber-300">Blocked</span>}</td></tr>)}</tbody></table></div> : <div className="empty-inset m-5 text-sm">No instrument quality records have been reported. This can be normal before the scanner receives a completed market-data cycle.</div>}
      </section>
    </section>
  );
}

function Metric({ label, value }: { label: string; value: string }) { return <div className="glass-inset rounded-md p-3"><p className="text-[10px] font-bold uppercase tracking-[.14em] text-slate-500">{label}</p><p className="mt-2 text-base font-semibold text-white">{value}</p></div>; }
