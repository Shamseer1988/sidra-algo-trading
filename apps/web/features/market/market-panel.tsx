"use client";

import {
  CalendarDays,
  ChevronLeft,
  ChevronRight,
  RefreshCw,
  Search,
  ShieldAlert,
  ShieldCheck,
  TriangleAlert,
} from "lucide-react";
import { useMemo, useState } from "react";

import type { DataQuality, MarketSession, Overview, ScannerStatus } from "../../components/api";
import { formatIstTimestamp, statusTone, titleCase } from "../../lib/formatting";
import { resolveScriptName } from "../../lib/instruments";

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
  const [query, setQuery] = useState("");
  const [pageSize, setPageSize] = useState(10);
  const [page, setPage] = useState(1);

  const qualityGood = quality.filter((item) => item.allows_signals).length;

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return quality;
    return quality.filter((item) => {
      const script = resolveScriptName(item.instrument_token).toLowerCase();
      const token = item.instrument_token.toLowerCase();
      const state = item.state.toLowerCase();
      const reason = item.reason.toLowerCase();
      return (
        script.includes(q) ||
        token.includes(q) ||
        state.includes(q) ||
        reason.includes(q)
      );
    });
  }, [quality, query]);

  const totalPages = Math.max(1, Math.ceil(filtered.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  const paginated = useMemo(() => {
    const start = (currentPage - 1) * pageSize;
    return filtered.slice(start, start + pageSize);
  }, [filtered, currentPage, pageSize]);

  return (
    <section className="space-y-6">
      <div className="page-toolbar">
        <div>
          <p className="eyebrow">Market intelligence</p>
          <h2 className="page-title">Market state & instruments</h2>
          <p className="page-copy">
            Session status and real-time market data feed quality for tracked instruments.
          </p>
        </div>
        <button className="secondary-button" onClick={onRefresh}>
          <RefreshCw className="h-4 w-4" />
          Refresh market state
        </button>
      </div>

      <div className="grid gap-4 lg:grid-cols-[.85fr_1.15fr]">
        <article className="panel p-5 sm:p-6">
          <div className="flex items-start justify-between gap-3">
            <div>
              <p className="eyebrow">NSE session</p>
              <h3 className="mt-1 text-lg font-semibold">
                {session ? titleCase(session.phase) : "Awaiting backend state"}
              </h3>
            </div>
            <CalendarDays className="h-5 w-5 text-emerald-500 dark:text-emerald-400" />
          </div>
          {session ? (
            <dl className="data-list mt-5">
              <div>
                <dt>Trading day</dt>
                <dd>{session.trading_day ? "Yes" : "No"}</dd>
              </div>
              <div>
                <dt>Session date</dt>
                <dd>{session.session_date ?? "—"}</dd>
              </div>
              <div>
                <dt>Regular session</dt>
                <dd>
                  {session.regular_open && session.regular_close
                    ? `${session.regular_open}–${session.regular_close} IST`
                    : "Not scheduled"}
                </dd>
              </div>
              <div>
                <dt>Evaluated at</dt>
                <dd className="numeric">{formatIstTimestamp(session.local_timestamp)}</dd>
              </div>
            </dl>
          ) : (
            <p className="empty-inset mt-5">
              The market calendar endpoint is not yet available from this environment. Core scanner operations remain unaffected.
            </p>
          )}
          <p className="mt-5 text-sm leading-6 text-slate-500 dark:text-slate-400">
            {session?.reason ?? "Session details will appear after the next successful backend refresh."}
          </p>
        </article>

        <article className="panel p-5 sm:p-6">
          <div className="flex items-start justify-between gap-3">
            <div>
              <p className="eyebrow">Ingestion status</p>
              <h3 className="mt-1 text-lg font-semibold">
                {titleCase(overview.market_data.status)}
              </h3>
            </div>
            <span className={`status-pill ${statusTone(overview.market_data.status)}`}>
              {titleCase(overview.market_data.status)}
            </span>
          </div>
          <p className="mt-4 text-sm leading-6 text-slate-500 dark:text-slate-400">{overview.market_data.detail}</p>
          <div className="mt-5 grid gap-3 sm:grid-cols-3">
            <Metric label="Scanner" value={titleCase(scanner.status)} />
            <Metric label="Tracked instruments" value={String(quality.length)} />
            <Metric label="Signal eligible" value={String(qualityGood)} />
          </div>
        </article>
      </div>

      {/* Premium Theme-Adaptive Instrument Table */}
      <section className="panel overflow-hidden">
        {/* Table Header Controls */}
        <div className="flex flex-col gap-4 border-b border-slate-700/40 dark:border-slate-800/80 p-5 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <p className="eyebrow">Instrument Readiness & Quality</p>
            <h3 className="mt-1 text-base font-semibold">
              Tracked Securities ({filtered.length} instruments)
            </h3>
          </div>

          <div className="flex flex-wrap items-center gap-3">
            {/* Live Search */}
            <div className="relative min-w-[220px]">
              <Search className="absolute left-3 top-2.5 h-4 w-4 text-slate-400" />
              <input
                type="text"
                value={query}
                onChange={(e) => {
                  setQuery(e.target.value);
                  setPage(1);
                }}
                placeholder="Search symbol, code..."
                className="field-input w-full pl-9 pr-3 text-xs"
              />
            </div>

            {/* Rows Per Page Dropdown */}
            <div className="flex items-center gap-2 text-xs text-slate-500 dark:text-slate-400">
              <span>Show:</span>
              <select
                aria-label="Rows per page"
                value={pageSize}
                onChange={(e) => {
                  setPageSize(Number(e.target.value));
                  setPage(1);
                }}
                className="field-input w-20 py-1 text-xs"
              >
                <option value={10}>10</option>
                <option value={25}>25</option>
                <option value={50}>50</option>
                <option value={100}>100</option>
                <option value={150}>150</option>
              </select>
            </div>
          </div>
        </div>

        {/* Table with Vertical & Horizontal Scrolling and Sticky Header */}
        {filtered.length ? (
          <div className="table-scroll max-h-[560px] overflow-auto">
            <table className="terminal-table w-full text-left text-xs">
              <thead className="sticky top-0 z-10">
                <tr>
                  <th className="py-3.5 pl-5 pr-4">Script Name</th>
                  <th className="py-3.5 px-4">Instrument Code</th>
                  <th className="py-3.5 px-4">Quality State</th>
                  <th className="py-3.5 px-4 text-right">1-Min Bars</th>
                  <th className="py-3.5 px-4 text-right">Ticks</th>
                  <th className="py-3.5 px-4 text-right">Avg Latency</th>
                  <th className="py-3.5 pl-4 pr-5">Signal Gate</th>
                </tr>
              </thead>
              <tbody>
                {paginated.map((item) => {
                  const script = resolveScriptName(item.instrument_token);
                  return (
                    <tr key={item.instrument_token}>
                      {/* Script Name */}
                      <td className="py-3.5 pl-5 pr-4">
                        <span className="inline-block rounded bg-emerald-500/15 px-2.5 py-1 text-xs font-bold text-emerald-600 dark:text-emerald-300 border border-emerald-500/30">
                          {script}
                        </span>
                      </td>

                      {/* Instrument Code */}
                      <td className="py-3.5 px-4">
                        <strong className="block font-mono">{item.instrument_token}</strong>
                        <span className="block text-[11px] opacity-80">{item.reason}</span>
                      </td>

                      {/* Quality State */}
                      <td className="py-3.5 px-4">
                        <span className={`status-pill ${statusTone(item.state)}`}>
                          {item.state}
                        </span>
                      </td>

                      {/* Bars */}
                      <td className="py-3.5 px-4 text-right font-mono">
                        {item.received_bars} / {item.expected_bars}
                      </td>

                      {/* Ticks */}
                      <td className="py-3.5 px-4 text-right font-mono">
                        {item.received_ticks.toLocaleString("en-IN")}
                      </td>

                      {/* Latency */}
                      <td className="py-3.5 px-4 text-right font-mono">
                        <span
                          className={
                            item.average_latency_ms > 2000
                              ? "text-rose-600 dark:text-rose-400 font-semibold"
                              : item.average_latency_ms > 500
                              ? "text-amber-600 dark:text-amber-400 font-semibold"
                              : "text-emerald-600 dark:text-emerald-400 font-semibold"
                          }
                        >
                          {item.average_latency_ms.toFixed(0)} ms
                        </span>
                      </td>

                      {/* Signal Gate */}
                      <td className="py-3.5 pl-4 pr-5">
                        {item.allows_signals ? (
                          <span className="inline-flex items-center gap-1.5 font-medium text-emerald-600 dark:text-emerald-300">
                            <ShieldCheck className="h-4 w-4 text-emerald-500 dark:text-emerald-400" />
                            Eligible
                          </span>
                        ) : (
                          <span className="inline-flex items-center gap-1.5 font-medium text-rose-600 dark:text-rose-400">
                            <ShieldAlert className="h-4 w-4 text-rose-500 dark:text-rose-400" />
                            Blocked
                          </span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="empty-inset m-6 text-sm">
            {query
              ? `No instruments match the search filter "${query}".`
              : "No instrument quality records have been reported. This is normal before the scanner processes the first market ticks."}
          </div>
        )}

        {/* Table Footer with Pagination Controls */}
        {filtered.length > 0 && (
          <div className="flex flex-col items-center justify-between gap-3 border-t border-slate-700/30 dark:border-slate-800/80 p-4 text-xs sm:flex-row">
            <div>
              Showing{" "}
              <strong>
                {(currentPage - 1) * pageSize + 1}
              </strong>{" "}
              to{" "}
              <strong>
                {Math.min(currentPage * pageSize, filtered.length)}
              </strong>{" "}
              of <strong>{filtered.length}</strong> instruments
            </div>

            <div className="flex items-center gap-2">
              <button
                disabled={currentPage <= 1}
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                className="secondary-button py-1 px-2.5 text-xs disabled:opacity-40"
              >
                <ChevronLeft className="h-3.5 w-3.5" />
                Previous
              </button>

              <span className="px-2 font-medium">
                Page {currentPage} of {totalPages}
              </span>

              <button
                disabled={currentPage >= totalPages}
                onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                className="secondary-button py-1 px-2.5 text-xs disabled:opacity-40"
              >
                Next
                <ChevronRight className="h-3.5 w-3.5" />
              </button>
            </div>
          </div>
        )}
      </section>
    </section>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="glass-inset rounded-md p-3">
      <p className="text-[10px] font-bold uppercase tracking-[.14em] text-slate-500 dark:text-slate-400">{label}</p>
      <p className="mt-2 text-base font-semibold">{value}</p>
    </div>
  );
}
