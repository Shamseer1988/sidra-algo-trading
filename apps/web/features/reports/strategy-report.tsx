"use client";

import { RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { api, type StrategyResult } from "../../components/api";
import { strategyLabel } from "../../lib/strategy-names";
import { lastDays, RangeFilter } from "../history/filters";
import { Money, percent, rupees, toNumber } from "../history/money";

/**
 * Every strategy over one period, live and paper in separate tables.
 *
 * Never one table and never one total: a simulated rupee and a real one are
 * not the same thing, and adding them makes a figure no account ever held.
 */
export function StrategyReport({ onMessage }: { onMessage: (message: string) => void }) {
  const [range, setRange] = useState(() => lastDays(30));
  const [rows, setRows] = useState<StrategyResult[]>([]);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setRows(await api.historyStrategies({ from_date: range.from, to_date: range.to }));
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Could not load the strategy results");
    } finally {
      setLoading(false);
    }
  }, [range, onMessage]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <section>
      <div className="page-toolbar">
        <div>
          <p className="eyebrow">Live and paper, side by side</p>
          <h2 className="page-title">By strategy</h2>
          <p className="page-copy">
            A signal sent to the broker counts under Live and is left out of Paper, so no trade is counted twice. Net is
            after charges; live charges here are estimated per trade, because the broker bills per day.
          </p>
        </div>
        <div className="flex flex-wrap items-end gap-2">
          <RangeFilter range={range} onChange={setRange} />
          <button className="secondary-button" onClick={() => void load()} disabled={loading}>
            <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
            Refresh
          </button>
        </div>
      </div>
      <Table title="Live: real orders" rows={rows.filter((row) => row.execution_mode === "LIVE")} loading={loading} />
      <Table title="Paper: simulated" rows={rows.filter((row) => row.execution_mode === "PAPER")} loading={loading} />
      <p className="mt-4 text-xs text-slate-500 dark:text-slate-400">
        Average R is the net result divided by the risk planned. Fewer than about 100 closed trades is a record, not
        proof that a strategy works.
      </p>
    </section>
  );
}

function Table({ title, rows, loading }: { title: string; rows: StrategyResult[]; loading: boolean }) {
  return (
    <article className="panel mt-5 overflow-hidden">
      <h3 className="border-b border-slate-200 px-5 py-4 font-semibold text-slate-900 dark:border-slate-800 dark:text-white">
        {title}
      </h3>
      {loading ? (
        <p className="p-5 text-sm text-slate-500">Loading…</p>
      ) : !rows.length ? (
        <p className="p-5 text-sm text-slate-500">No trades in this period.</p>
      ) : (
        <div className="table-scroll">
          <table className="terminal-table" aria-label={title}>
            <thead>
              <tr>
                <th>Strategy</th>
                <th>Trades</th>
                <th>W / L</th>
                <th>Win rate</th>
                <th>Gross</th>
                <th>Charges</th>
                <th>Net</th>
                <th>Average R</th>
                <th>Best / worst</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const r = toNumber(row.average_r);
                return (
                  <tr key={row.strategy}>
                    <td>
                      <strong>{strategyLabel(row.strategy)}</strong>
                    </td>
                    <td className="numeric">
                      {row.trades}
                      {row.open_trades > 0 && <span className="muted-cell text-[11px]"> ({row.open_trades} open)</span>}
                    </td>
                    <td className="numeric">
                      {row.wins} / {row.losses}
                      {row.scratches > 0 && <span className="muted-cell text-[11px]"> +{row.scratches} flat</span>}
                    </td>
                    <td className="numeric">{percent(row.win_rate_percent)}</td>
                    <td>
                      <Money value={row.gross_pnl} signed />
                    </td>
                    <td className="numeric muted-cell">{rupees(row.charges)}</td>
                    <td>
                      <strong>
                        <Money value={row.net_pnl} signed />
                      </strong>
                    </td>
                    <td className="numeric">{r === null ? "—" : `${r.toFixed(2)}R`}</td>
                    <td className="text-xs">
                      <Money value={row.best_trade} signed /> / <Money value={row.worst_trade} signed />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </article>
  );
}
