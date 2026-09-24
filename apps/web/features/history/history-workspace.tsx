"use client";

import { CloudDownload, Download, FileSpreadsheet, Info, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import {
  api,
  type HistoryDay,
  type HistoryOverview,
  type HistoryTrade,
  type ReconciliationStatus,
} from "../../components/api";
import { formatIstTimestamp } from "../../lib/formatting";
import { Money, percent, ratio, rupees, toNumber } from "./money";
import { ReconciliationPill } from "./reconciliation";
import { TradeDetail } from "./trade-detail";

/**
 * The trading record, read the way a broker statement is read.
 *
 * Two things this screen refuses to do, both inherited from the service behind
 * it. It never shows a single "P&L" figure — gross, charges and net are three
 * columns, because a day that made ₹900 before costs and ₹340 after is not a
 * ₹900 day. And it never presents a broker's figure as if it were ours: when
 * the two disagree, both are shown side by side and the row says which is which.
 */

function isoDate(value: Date): string {
  return value.toISOString().slice(0, 10);
}

function defaultRange(): { from: string; to: string } {
  const today = new Date();
  const back = new Date(today);
  back.setDate(back.getDate() - 30);
  return { from: isoDate(back), to: isoDate(today) };
}

export function HistoryWorkspace({
  canOperate = true,
  onMessage,
}: {
  canOperate?: boolean;
  onMessage: (message: string) => void;
}) {
  const initial = useMemo(defaultRange, []);
  const [from, setFrom] = useState(initial.from);
  const [to, setTo] = useState(initial.to);
  const [overview, setOverview] = useState<HistoryOverview | null>(null);
  const [days, setDays] = useState<HistoryDay[]>([]);
  const [trades, setTrades] = useState<HistoryTrade[]>([]);
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState<"days" | "trades">("days");
  const [dayFilter, setDayFilter] = useState<string | null>(null);
  const [openTrade, setOpenTrade] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const range = { from_date: from, to_date: to };
      const [nextOverview, nextDays, nextTrades] = await Promise.all([
        api.historyOverview(range),
        api.historyDaily(range),
        api.historyTrades(range),
      ]);
      setOverview(nextOverview);
      setDays(nextDays);
      setTrades(nextTrades);
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Could not load the trading history");
    } finally {
      setLoading(false);
    }
  }, [from, to, onMessage]);

  useEffect(() => {
    void load();
  }, [load]);

  const shown = dayFilter ? trades.filter((trade) => trade.session_date === dayFilter) : trades;
  const [fetching, setFetching] = useState<string | null>(null);

  async function fetchBroker(sessionDate: string) {
    setFetching(sessionDate);
    try {
      const result = await api.fetchBrokerFigures(sessionDate);
      onMessage(
        `${result.broker} reported ${rupees(result.realized_pnl, { signed: true })} realised and ` +
          `${rupees(result.charges)} charges for ${sessionDate}. Recorded beside the local figures.`,
      );
      await load();
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Could not fetch the broker figures");
    } finally {
      setFetching(null);
    }
  }

  function openDay(sessionDate: string) {
    setDayFilter(sessionDate);
    setTab("trades");
  }

  return (
    <section>
      <div className="page-toolbar">
        <div>
          <p className="eyebrow">Trading record</p>
          <h2 className="page-title">History</h2>
          <p className="page-copy">
            Every trade this system took, with gross, charges and net kept apart. Charges are estimated locally from
            the published rate card unless a broker figure is recorded against the day — brokers report charges
            aggregated over a date range, never per trade.
          </p>
        </div>
        <div className="flex flex-wrap items-end gap-2">
          <label className="text-xs text-slate-400">
            From
            <input type="date" value={from} onChange={(event) => setFrom(event.target.value)} className="field-input mt-1 block font-mono text-sm" />
          </label>
          <label className="text-xs text-slate-400">
            To
            <input type="date" value={to} onChange={(event) => setTo(event.target.value)} className="field-input mt-1 block font-mono text-sm" />
          </label>
          <button className="secondary-button" onClick={() => void load()} disabled={loading}>
            <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
            Refresh
          </button>
          <a className="secondary-button" href={api.historyExportUrl("csv", { from_date: from, to_date: to })} download>
            <Download className="h-4 w-4" />
            CSV
          </a>
          <a className="secondary-button" href={api.historyExportUrl("xlsx", { from_date: from, to_date: to })} download>
            <FileSpreadsheet className="h-4 w-4" />
            Excel
          </a>
        </div>
      </div>

      {overview && <Summary overview={overview} />}

      <div className="mt-6 flex flex-wrap items-center gap-2">
        <button
          onClick={() => {
            setTab("days");
            setDayFilter(null);
          }}
          className={tab === "days" ? "primary-button" : "secondary-button"}
        >
          Days ({days.length})
        </button>
        <button onClick={() => setTab("trades")} className={tab === "trades" ? "primary-button" : "secondary-button"}>
          Trades ({trades.length})
        </button>
        {dayFilter && (
          <button onClick={() => setDayFilter(null)} className="secondary-button">
            Showing {dayFilter} — clear
          </button>
        )}
      </div>

      {tab === "days" ? (
        <DaysTable
          days={days}
          loading={loading}
          onOpenDay={openDay}
          canOperate={canOperate}
          fetching={fetching}
          onFetchBroker={fetchBroker}
        />
      ) : (
        <TradesTable trades={shown} loading={loading} onOpen={setOpenTrade} />
      )}

      {openTrade && <TradeDetail positionId={openTrade} onClose={() => setOpenTrade(null)} onMessage={onMessage} />}
    </section>
  );
}

function Summary({ overview }: { overview: HistoryOverview }) {
  const statuses = Object.entries(overview.reconciliation_counts) as [ReconciliationStatus, number][];
  return (
    <>
      <div className="mt-5 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Metric label="Net P&L" value={rupees(overview.net_pnl, { signed: true })} note="after charges" strong />
        <Metric label="Gross P&L" value={rupees(overview.gross_pnl, { signed: true })} note="before charges" />
        <Metric
          label="Charges"
          value={rupees(overview.charges)}
          note={
            overview.charges_as_percent_of_gross
              ? `${percent(overview.charges_as_percent_of_gross)} of gross profit`
              : "estimated locally"
          }
        />
        <Metric
          label="Trades"
          value={`${overview.trades}`}
          note={`${overview.trading_days} session${overview.trading_days === 1 ? "" : "s"}${
            overview.open_trades ? ` · ${overview.open_trades} still open` : ""
          }`}
        />
      </div>

      <div className="mt-3 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Metric
          label="Win rate"
          value={overview.win_rate_percent ? percent(overview.win_rate_percent) : "—"}
          note={`${overview.wins}W · ${overview.losses}L${overview.scratches ? ` · ${overview.scratches} scratch` : ""}`}
        />
        <Metric
          label="Profit factor"
          value={ratio(overview.profit_factor)}
          note={overview.profit_factor ? "gross won ÷ gross lost, net of charges" : "no losing trades to divide by"}
        />
        <Metric label="Expectancy" value={rupees(overview.expectancy, { signed: true })} note="net, a trade" />
        <Metric
          label="Best / worst day"
          value={`${rupees(overview.best_day, { signed: true })} / ${rupees(overview.worst_day, { signed: true })}`}
          note={overview.halted_days ? `${overview.halted_days} day(s) ended by a limit` : "no day hit a limit"}
        />
      </div>

      {statuses.length > 0 && (
        <article className="panel mt-4 p-4 sm:p-5">
          <div className="flex flex-wrap items-center gap-3">
            <p className="eyebrow">Reconciliation</p>
            {statuses.map(([status, count]) => (
              <span key={status} className="flex items-center gap-2 text-xs text-slate-400">
                <ReconciliationPill status={status} label={overview.reconciliation_labels[status] ?? status} />
                <span className="numeric">
                  {count} day{count === 1 ? "" : "s"}
                </span>
              </span>
            ))}
          </div>
          <p className="mt-3 flex gap-2 text-xs leading-5 text-slate-500">
            <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <span>
              A day with no live trades is always &ldquo;estimated charges&rdquo;: the costs are this system&rsquo;s
              model of the rate card and there is no broker side to match. A live day stays &ldquo;broker data
              pending&rdquo; until the broker&rsquo;s own figures are recorded against it.
            </span>
          </p>
        </article>
      )}
    </>
  );
}

function Metric({ label, value, note, strong }: { label: string; value: string; note: string; strong?: boolean }) {
  const negative = value.startsWith("−");
  return (
    <div className="glass-inset rounded-md p-4">
      <p className="eyebrow">{label}</p>
      <p
        className={`mt-2 numeric font-semibold ${strong ? "text-2xl" : "text-xl"} ${
          negative ? "text-rose-300" : strong ? "text-emerald-300" : "text-white"
        }`}
      >
        {value}
      </p>
      <p className="mt-1 text-xs text-slate-500">{note}</p>
    </div>
  );
}

function DaysTable({
  days,
  loading,
  onOpenDay,
  canOperate,
  fetching,
  onFetchBroker,
}: {
  days: HistoryDay[];
  loading: boolean;
  onOpenDay: (sessionDate: string) => void;
  canOperate: boolean;
  fetching: string | null;
  onFetchBroker: (sessionDate: string) => void;
}) {
  if (loading) return <p className="mt-6 text-sm text-slate-500">Loading…</p>;
  if (!days.length) return <p className="mt-6 text-sm text-slate-500">No trading in this range.</p>;
  return (
    <article className="panel mt-4 overflow-hidden">
      <div className="table-scroll">
        <table className="terminal-table">
          <thead>
            <tr>
              <th>Session</th>
              <th>Trades</th>
              <th>W / L</th>
              <th>Win rate</th>
              <th>Gross</th>
              <th>Charges</th>
              <th>Net</th>
              <th>Reconciliation</th>
              <th>Broker</th>
              <th>Day ended by</th>
            </tr>
          </thead>
          <tbody>
            {days.map((day) => (
              <tr key={day.session_date} className="cursor-pointer" onClick={() => onOpenDay(day.session_date)}>
                <td>
                  <strong className="block text-slate-100">{day.session_date}</strong>
                  {day.live_trades > 0 && <span className="text-[11px] text-amber-300">{day.live_trades} live</span>}
                </td>
                <td className="numeric">
                  {day.trades}
                  {day.open_trades > 0 && <span className="ml-1 text-[11px] text-slate-500">({day.open_trades} open)</span>}
                </td>
                <td className="numeric">
                  {day.wins} / {day.losses}
                  {day.scratches > 0 && <span className="ml-1 text-[11px] text-slate-500">+{day.scratches}s</span>}
                </td>
                <td className="numeric">{day.win_rate_percent ? percent(day.win_rate_percent) : "—"}</td>
                <td>
                  <Money value={day.gross_pnl} signed />
                </td>
                <td className="numeric text-slate-400">{rupees(day.charges)}</td>
                <td>
                  <strong>
                    <Money value={day.net_pnl} signed />
                  </strong>
                  {day.broker_realized_pnl !== null && (
                    // Both figures, never one replacing the other.
                    <span className="block text-[11px] text-slate-500">
                      {day.broker}: {rupees(day.broker_realized_pnl, { signed: true })} realised
                    </span>
                  )}
                </td>
                <td>
                  <ReconciliationPill
                    status={day.reconciliation}
                    label={day.reconciliation_label}
                    title={day.reconciliation_note}
                  />
                </td>
                <td onClick={(event) => event.stopPropagation()}>
                  {day.live_trades > 0 && canOperate ? (
                    <button
                      className="secondary-button whitespace-nowrap px-2 py-1 text-xs"
                      disabled={fetching === day.session_date}
                      onClick={() => onFetchBroker(day.session_date)}
                    >
                      <CloudDownload className={`h-3.5 w-3.5 ${fetching === day.session_date ? "animate-pulse" : ""}`} />
                      {day.broker_fetched_at ? "Re-fetch" : "Fetch"}
                    </button>
                  ) : (
                    // A paper day has nothing at a broker to ask about, so the
                    // button would only ever return an empty report.
                    <span className="muted-cell text-xs">—</span>
                  )}
                </td>
                <td className="muted-cell text-xs">{day.halt_reason ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </article>
  );
}

function TradesTable({
  trades,
  loading,
  onOpen,
}: {
  trades: HistoryTrade[];
  loading: boolean;
  onOpen: (positionId: string) => void;
}) {
  if (loading) return <p className="mt-6 text-sm text-slate-500">Loading…</p>;
  if (!trades.length) return <p className="mt-6 text-sm text-slate-500">No trades in this range.</p>;
  return (
    <article className="panel mt-4 overflow-hidden">
      <div className="table-scroll">
        <table className="terminal-table">
          <thead>
            <tr>
              <th>Session</th>
              <th>Instrument</th>
              <th>Strategy</th>
              <th>Qty</th>
              <th>Entry → exit</th>
              <th>Gross</th>
              <th>Charges</th>
              <th>Net</th>
              <th>R</th>
              <th>Reconciliation</th>
            </tr>
          </thead>
          <tbody>
            {trades.map((trade) => {
              const r = toNumber(trade.r_multiple);
              return (
                <tr key={trade.position_id} className="cursor-pointer" onClick={() => onOpen(trade.position_id)}>
                  <td>
                    <strong className="block text-slate-100">{trade.session_date}</strong>
                    <span className="text-[11px] text-slate-500">{formatIstTimestamp(trade.opened_at)}</span>
                  </td>
                  <td>
                    <strong className="block text-slate-100">{trade.script_name}</strong>
                    <span className="text-[11px] text-slate-500">
                      {trade.side} · {trade.execution_mode === "LIVE" ? "Live" : "Paper"}
                      {trade.is_open ? " · open" : ""}
                    </span>
                  </td>
                  <td className="muted-cell text-xs">{trade.strategy_version}</td>
                  <td className="numeric">{trade.quantity}</td>
                  <td className="numeric text-xs">
                    {trade.entry_price ?? "—"} → {trade.exit_price ?? "—"}
                  </td>
                  <td>
                    <Money value={trade.gross_pnl} signed />
                  </td>
                  <td className="numeric text-slate-400">{rupees(trade.charges)}</td>
                  <td>
                    <strong>
                      <Money value={trade.net_pnl} signed />
                    </strong>
                  </td>
                  <td className={`numeric ${r === null ? "text-slate-500" : r >= 0 ? "text-emerald-300" : "text-rose-300"}`}>
                    {r === null ? "open" : `${r.toFixed(2)}R`}
                  </td>
                  <td>
                    <ReconciliationPill
                      status={trade.reconciliation}
                      label={trade.reconciliation_label}
                      title={trade.reconciliation_note}
                    />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </article>
  );
}
