"use client";

import { RefreshCw } from "lucide-react";
import { type ReactNode, useCallback, useEffect, useState } from "react";

import { api, type ExecutionQuality } from "../../components/api";
import { BrokerSelect } from "../orders/source-controls";
import type { OrderSource } from "../orders/use-order-source";
import { Money, percent, toNumber } from "../history/money";

/**
 * How well the system traded, as opposed to what it traded.
 *
 * Three questions, and none of them was answerable before this screen existed
 * even though every figure behind them was already being recorded.
 *
 * **Did the orders fill?** Entries are capped limit orders, so some do not.
 * That is the design working — a fill at a materially worse price is a
 * different trade — but it is also a hidden strategy parameter. A strategy that
 * looks poor because it misses its best entries and a strategy that is poor are
 * indistinguishable until somebody counts.
 *
 * **What did the fills cost?** Entry slippage against the plan is what the
 * entry cap exists to bound; trade slippage against the simulator is what the
 * round trip cost against what the journal assumed.
 *
 * **Is a trade worth taking?** Expectancy net of what the broker actually
 * charged, against the win rate this profile needs simply to break even.
 *
 * The verdict sits above everything, in words, before a single number. A
 * per-trade expectancy from nine trades looks exactly like one from nine
 * hundred, and that sentence is the only thing between a reader and the
 * mistake.
 */

const RANGES = [
  { days: 7, label: "Last 7 days" },
  { days: 30, label: "Last 30 days" },
  { days: 90, label: "Last 90 days" },
  { days: 365, label: "Last year" },
];

function isoDate(value: Date): string {
  return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}-${String(value.getDate()).padStart(2, "0")}`;
}

export function ExecutionQualityPanel({
  onMessage,
  source,
}: {
  onMessage: (message: string) => void;
  source: OrderSource;
}) {
  const broker = source.source === "broker" ? (source.broker ?? undefined) : undefined;
  const [days, setDays] = useState(30);
  const [report, setReport] = useState<ExecutionQuality | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    const to = new Date();
    const from = new Date();
    from.setDate(from.getDate() - (days - 1));
    try {
      setReport(await api.historyQuality({ from_date: isoDate(from), to_date: isoDate(to), broker }));
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Could not load the execution report");
    } finally {
      setLoading(false);
    }
  }, [days, broker, onMessage]);

  useEffect(() => {
    void load();
  }, [load]);

  // The gap that decides everything: a win rate below what the profile needs to
  // break even is a strategy paying for the privilege of trading.
  const realRate = report ? toNumber(report.win_rate_percent) : null;
  const needed = report ? toNumber(report.break_even_win_rate_percent) : null;
  const covering = realRate !== null && needed !== null ? realRate >= needed : null;

  return (
    <section>
      <div className="page-toolbar">
        <div>
          <p className="eyebrow">Execution, not performance</p>
          <h2 className="page-title">Execution quality</h2>
          <p className="page-copy">
            Live trades only. A simulated trade cost nothing and filled perfectly, so including one would flatter
            every figure here.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {source.source === "broker" && (
            <BrokerSelect broker={source.broker} choices={source.choices} setBroker={source.setBroker} />
          )}
          <select
            className="field-input py-2 text-sm"
            value={days}
            onChange={(event) => setDays(Number(event.target.value))}
            aria-label="Period"
          >
            {RANGES.map((item) => (
              <option key={item.days} value={item.days}>
                {item.label}
              </option>
            ))}
          </select>
          <button className="secondary-button" onClick={() => void load()} disabled={loading}>
            <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
            Refresh
          </button>
        </div>
      </div>

      {report && (
        <>
          {/* Before any number, and deliberately hard to skip. */}
          <article className="panel mt-5 border-l-4 border-l-sky-500 p-5">
            <p className="eyebrow">What this sample supports</p>
            <p className="mt-2 text-sm text-slate-900 dark:text-slate-100">{report.verdict}</p>
          </article>

          <article className="panel mt-4 p-5">
            <h3 className="font-semibold text-slate-900 dark:text-white">From setup to position</h3>
            <dl className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
              <Figure label="Signals found" value={String(report.signals)} />
              <Figure label="Passed risk" value={String(report.accepted)} note={percent(report.acceptance_percent)} />
              <Figure label="Sent to broker" value={String(report.sent)} />
              <Figure label="Filled" value={String(report.filled)} />
              <Figure
                label="Fill rate"
                value={report.fill_rate_percent === null ? "—" : percent(report.fill_rate_percent)}
                note={report.unknown_fills ? `${report.unknown_fills} unknown, left out` : undefined}
              />
            </dl>
            {Object.keys(report.refusals).length > 0 && (
              <div className="mt-4">
                <p className="eyebrow">Why setups were refused</p>
                <ul className="mt-2 space-y-1 text-xs text-slate-500 dark:text-slate-400">
                  {Object.entries(report.refusals)
                    .sort((left, right) => right[1] - left[1])
                    .map(([reason, count]) => (
                      <li key={reason}>
                        <strong className="text-slate-700 dark:text-slate-200">{count}×</strong> {reason}
                      </li>
                    ))}
                </ul>
              </div>
            )}
          </article>

          <article className="panel mt-4 p-5">
            <h3 className="font-semibold text-slate-900 dark:text-white">What a trade is worth</h3>
            <dl className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
              <Figure label="Closed trades" value={String(report.trades)} note={`${report.wins}W / ${report.losses}L`} />
              <Figure label="Win rate" value={percent(report.win_rate_percent)} />
              <Figure
                label="Needs to break even"
                value={percent(report.break_even_win_rate_percent)}
                className={covering === null ? "" : covering ? "text-emerald-400" : "text-rose-400"}
              />
              <Figure label="Net per trade" value={<Money value={report.net_per_trade} signed />} />
              <Figure label="Average win" value={<Money value={report.average_win} signed />} />
              <Figure label="Average loss" value={<Money value={report.average_loss} signed />} />
              <Figure label="Gross per trade" value={<Money value={report.gross_per_trade} signed />} />
              <Figure label="Average R" value={report.average_r ?? "—"} />
            </dl>
            {covering === false && (
              <p className="mt-4 text-xs text-rose-600 dark:text-rose-300">
                The win rate is below what this profile needs to come out level. Either the winners are too small
                for the losers, or the costs are too large for both.
              </p>
            )}
          </article>

          <div className="mt-4 grid gap-4 lg:grid-cols-2">
            <article className="panel p-5">
              <h3 className="font-semibold text-slate-900 dark:text-white">What the fills cost</h3>
              <dl className="mt-4 grid gap-3 sm:grid-cols-2">
                <Figure
                  label="Entry slippage, average"
                  value={report.entry_slippage_average === null ? "—" : `₹${report.entry_slippage_average}/share`}
                  note={`${report.entry_slippage_trades} entries`}
                />
                <Figure
                  label="Entry slippage, worst"
                  value={report.entry_slippage_worst === null ? "—" : `₹${report.entry_slippage_worst}/share`}
                />
                <Figure
                  label="Round trip vs model"
                  value={<Money value={report.trade_slippage_total} signed />}
                  note={`${report.trade_slippage_trades} broker-priced trades`}
                />
                <Figure label="Per trade" value={<Money value={report.trade_slippage_average} signed />} />
              </dl>
              <p className="mt-4 text-xs text-slate-500 dark:text-slate-400">
                Entry slippage is positive when the fill was worse than the plan — a long that paid more, a short
                that received less. It is the number the entry cap is sized against.
              </p>
            </article>

            <article className="panel p-5">
              <h3 className="font-semibold text-slate-900 dark:text-white">What it cost to trade</h3>
              <dl className="mt-4 grid gap-3 sm:grid-cols-2">
                <Figure label="Gross" value={<Money value={report.gross} signed />} />
                <Figure
                  label="Charges"
                  value={<Money value={report.charges_broker ?? report.charges_estimated} />}
                  note={report.charges_broker !== null ? "broker's own" : "this system's estimate"}
                />
                <Figure label="Per trade" value={<Money value={report.charges_per_trade} />} />
                <Figure label="Share of gross" value={percent(report.charges_percent_of_gross)} />
              </dl>
              <p className="mt-4 text-xs text-slate-500 dark:text-slate-400">
                {report.charge_days_settled} day(s) settled by the broker
                {report.charge_days_pending > 0 ? `, ${report.charge_days_pending} still pending` : ""}. Per-trade
                charges are always this system&apos;s estimate — no broker publishes one.
              </p>
            </article>
          </div>

          {report.strategies.length > 0 && (
            <article className="panel mt-4 overflow-hidden">
              <div className="border-b border-slate-200 px-5 py-4 dark:border-slate-800">
                <h3 className="font-semibold text-slate-900 dark:text-white">By strategy</h3>
              </div>
              <div className="table-scroll">
                <table className="terminal-table">
                  <thead>
                    <tr>
                      <th>Strategy</th>
                      <th>Trades</th>
                      <th>Wins</th>
                      <th>Net</th>
                      <th>Per trade</th>
                      <th>Average R</th>
                    </tr>
                  </thead>
                  <tbody>
                    {report.strategies.map((row) => (
                      <tr key={row.strategy_version}>
                        <td>{row.strategy_version}</td>
                        <td className="numeric">{row.trades}</td>
                        <td className="numeric">{row.wins}</td>
                        <td className="numeric">
                          <Money value={row.net} signed />
                        </td>
                        <td className="numeric">
                          <Money value={row.net_per_trade} signed />
                        </td>
                        <td className="numeric">{row.average_r ?? "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </article>
          )}

          {report.notes.length > 0 && (
            <article className="panel mt-4 p-5">
              <p className="eyebrow">Worth reading</p>
              <ul className="mt-2 space-y-2 text-sm text-slate-600 dark:text-slate-300">
                {report.notes.map((note) => (
                  <li key={note}>• {note}</li>
                ))}
              </ul>
            </article>
          )}
        </>
      )}
    </section>
  );
}

function Figure({
  label,
  value,
  note,
  className = "",
}: {
  label: string;
  value: ReactNode;
  note?: string;
  className?: string;
}) {
  return (
    <div className="glass-inset rounded-md p-3">
      <dt className="eyebrow">{label}</dt>
      <dd className={`mt-1 numeric text-lg font-semibold ${className}`}>{value}</dd>
      {note && <p className="mt-1 text-[11px] text-slate-500 dark:text-slate-400">{note}</p>}
    </div>
  );
}
