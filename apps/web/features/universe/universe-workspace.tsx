"use client";

import { DatabaseZap, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { api, type UniverseEntry, type UniverseSummary } from "../../components/api";
import { formatIstTimestamp } from "../../lib/formatting";

export function UniverseWorkspace({
  canOperate,
  onMessage,
}: {
  canOperate: boolean;
  onMessage: (message: string) => void;
}) {
  const [entries, setEntries] = useState<UniverseEntry[]>([]);
  const [summary, setSummary] = useState<UniverseSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(() => {
    setLoading(true);
    Promise.all([api.universe(), api.universeSummary()])
      .then(([rows, meta]) => {
        setEntries(rows);
        setSummary(meta);
      })
      .catch((error: unknown) => onMessage(error instanceof Error ? error.message : "Could not load the scan universe"))
      .finally(() => setLoading(false));
  }, [onMessage]);

  useEffect(() => {
    load();
  }, [load]);

  const rebuild = async () => {
    setRefreshing(true);
    try {
      setSummary(await api.refreshUniverse());
      setEntries(await api.universe());
      onMessage("Scan universe rebuilt.");
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Could not rebuild the scan universe");
    } finally {
      setRefreshing(false);
    }
  };

  const selected = entries.filter((entry) => entry.selected);
  const rejected = entries.filter((entry) => !entry.eligible);

  return (
    <section>
      <div className="page-toolbar">
        <div>
          <p className="eyebrow">Daily stock selection</p>
          <h2 className="page-title">Scan universe</h2>
          <p className="page-copy">
            A pre-open ranking of the streamed instruments by liquidity, volatility, gap and momentum. When the universe
            is enabled the scanner only generates signals for the selected names.
          </p>
        </div>
        {canOperate && (
          <button className="secondary-button" onClick={() => void rebuild()} disabled={refreshing}>
            <RefreshCw className={`h-4 w-4 ${refreshing ? "animate-spin" : ""}`} />
            Rebuild now
          </button>
        )}
      </div>

      {summary && (
        <div className="mt-5 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <Stat label="Status" value={summary.enabled ? "Enabled" : "Advisory only"} />
          <Stat label="Selected / size" value={`${summary.selected} / ${summary.universe_size}`} />
          <Stat label="Eligible candidates" value={`${summary.eligible} / ${summary.total_candidates}`} />
          <Stat
            label="Last built"
            value={summary.last_built_at ? formatIstTimestamp(summary.last_built_at) : "Not built today"}
          />
        </div>
      )}

      {loading ? (
        <div className="panel mt-6 p-9 text-center text-sm text-slate-500">Loading the scan universe…</div>
      ) : !entries.length ? (
        <article className="panel mt-6 p-9 text-center">
          <DatabaseZap className="mx-auto h-7 w-7 text-slate-600" />
          <h3 className="mt-4 font-medium text-white">No universe for this session yet</h3>
          <p className="mx-auto mt-2 max-w-md text-sm leading-6 text-slate-500">
            The universe is built after {summary ? "the configured pre-open time" : "market open"} once daily candle
            history is available, or immediately with “Rebuild now”.
          </p>
        </article>
      ) : (
        <>
          <UniverseTable title={`Selected (${selected.length})`} rows={selected.length ? selected : entries.slice(0, 30)} />
          {rejected.length > 0 && <UniverseTable title={`Screened out (${rejected.length})`} rows={rejected} muted />}
        </>
      )}
    </section>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <article className="glass-inset rounded-md p-4">
      <p className="text-[11px] font-semibold uppercase tracking-[.16em] text-slate-500">{label}</p>
      <p className="mt-2 truncate text-lg font-semibold text-white">{value}</p>
    </article>
  );
}

function UniverseTable({ title, rows, muted = false }: { title: string; rows: UniverseEntry[]; muted?: boolean }) {
  return (
    <article className="panel mt-6 overflow-hidden border border-slate-800">
      <p className="border-b border-slate-800 px-4 py-3 text-xs font-semibold uppercase tracking-[.16em] text-slate-500">
        {title}
      </p>
      <div className="table-scroll">
        <table className="terminal-table">
          <thead>
            <tr>
              <th>#</th>
              <th>Script</th>
              <th>Instrument</th>
              <th>Score</th>
              <th>Liquidity</th>
              <th>Volatility</th>
              <th>Gap</th>
              <th>Trend</th>
              <th>{muted ? "Reason" : "ATR %"}</th>
              <th>{muted ? "" : "Gap %"}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((entry) => (
              <tr key={entry.instrument_token} className={muted ? "opacity-70" : ""}>
                <td className="numeric">{entry.rank}</td>
                <td>
                  <span className="inline-block rounded bg-emerald-500/10 px-2 py-0.5 text-xs font-bold text-emerald-300 border border-emerald-500/20">
                    {entry.script_name}
                  </span>
                </td>
                <td className="font-mono text-slate-300">{entry.instrument_token}</td>
                <td className="numeric font-mono">{muted ? "—" : entry.score.toFixed(1)}</td>
                <td className="numeric">{muted ? "—" : entry.liquidity_score.toFixed(0)}</td>
                <td className="numeric">{muted ? "—" : entry.volatility_score.toFixed(0)}</td>
                <td className="numeric">{muted ? "—" : entry.gap_score.toFixed(0)}</td>
                <td className="numeric">{muted ? "—" : entry.trend_score.toFixed(0)}</td>
                <td className={muted ? "muted-cell" : "numeric"}>
                  {muted ? entry.rejection_reason : (entry.metrics.atr_percent ?? 0).toFixed(2)}
                </td>
                <td className="numeric">{muted ? "" : (entry.metrics.gap_percent ?? 0).toFixed(2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </article>
  );
}
