"use client";

import { ChevronLeft, ChevronRight } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { api, type HistoryDay, type HistoryTrade } from "../../components/api";
import { BrokerSelect } from "../orders/source-controls";
import type { OrderSource } from "../orders/use-order-source";
import { pnlTone, rupees, toNumber } from "../history/money";

/**
 * A month of trading days, red for a loss and green for a profit.
 *
 * The shape every broker's app uses, for a reason: a month of P&L read as a
 * table is a column of numbers, and read as a calendar it is a pattern —
 * Mondays, the days after a big win, the week a strategy stopped working.
 *
 * Two rules it keeps from the History screen, because they are the same
 * figures. **Net, not gross.** A day that made ₹900 before costs and ₹340
 * after is a ₹340 day, and the cell shows ₹340. **The broker's figure is never
 * silently substituted.** Where a broker figure has been fetched for a day it
 * is shown in the detail strip beside ours, labelled, and the two are allowed
 * to disagree; nothing on this screen overwrites a local record with one.
 *
 * Dates are handled as plain YYYY-MM-DD strings and local date parts. Parsing
 * "2026-10-01" with the Date constructor gives UTC midnight, which is the
 * previous day in any timezone west of Greenwich — a calendar that drew every
 * trade one cell early.
 */

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function pad(value: number): string {
  return String(value).padStart(2, "0");
}

function key(year: number, month: number, day: number): string {
  return `${year}-${pad(month + 1)}-${pad(day)}`;
}

function monthLabel(year: number, month: number): string {
  return new Date(year, month, 1).toLocaleDateString("en-IN", { month: "long", year: "numeric" });
}

/** Monday-first offset for the 1st of the month. */
function leadingBlanks(year: number, month: number): number {
  return (new Date(year, month, 1).getDay() + 6) % 7;
}

export function PnlCalendar({
  onMessage,
  source,
}: {
  onMessage: (message: string) => void;
  source: OrderSource;
}) {
  // "Broker" means the trades that reached a broker, not a second ledger. A
  // live trade and its paper journal row are the same trade, so summing both
  // would double a day -- and showing them together, which is what this screen
  // did, gives an operator a figure their broker account never saw.
  const mode = source.source === "broker" ? ("LIVE" as const) : ("PAPER" as const);
  const broker = source.source === "broker" ? (source.broker ?? undefined) : undefined;
  const today = useMemo(() => new Date(), []);
  const [year, setYear] = useState(today.getFullYear());
  const [month, setMonth] = useState(today.getMonth());
  const [days, setDays] = useState<HistoryDay[]>([]);
  const [loading, setLoading] = useState(true);
  const [open, setOpen] = useState<string | null>(null);
  const [trades, setTrades] = useState<HistoryTrade[]>([]);
  const [tradesLoading, setTradesLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const last = new Date(year, month + 1, 0).getDate();
      setDays(
        await api.historyDaily({
          from_date: key(year, month, 1),
          to_date: key(year, month, last),
          mode,
          broker,
        }),
      );
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Could not load the month");
    } finally {
      setLoading(false);
    }
  }, [year, month, onMessage, mode, broker]);

  useEffect(() => {
    void load();
  }, [load]);

  // The day's own trades, fetched only when a day is opened. A month of trades
  // up front would be a far larger request for a panel that is usually closed.
  useEffect(() => {
    if (!open) {
      setTrades([]);
      return;
    }
    let current = true;
    setTradesLoading(true);
    void api
      .historyTrades({ from_date: open, to_date: open, mode, broker })
      .then((rows) => {
        if (current) setTrades(rows);
      })
      .catch(() => {
        if (current) setTrades([]);
      })
      .finally(() => {
        if (current) setTradesLoading(false);
      });
    return () => {
      current = false;
    };
  }, [open, mode, broker]);

  const byDate = useMemo(() => new Map(days.map((day) => [day.session_date, day])), [days]);


  function step(delta: number) {
    const next = new Date(year, month + delta, 1);
    setYear(next.getFullYear());
    setMonth(next.getMonth());
    setOpen(null);
  }

  // Only the month on screen. The totals are read as a caption to the grid
  // below them, so a row from outside the range -- a server that widened the
  // window, a cached response from the previous month -- would put a figure
  // above a calendar that cannot account for it.
  const shown = days.filter((day) => day.session_date.startsWith(`${year}-${pad(month + 1)}-`));
  const total = shown.reduce((sum, day) => sum + (toNumber(day.net_pnl) ?? 0), 0);
  const charges = shown.reduce((sum, day) => sum + (toNumber(day.charges) ?? 0), 0);
  const green = shown.filter((day) => (toNumber(day.net_pnl) ?? 0) > 0).length;
  const red = shown.filter((day) => (toNumber(day.net_pnl) ?? 0) < 0).length;
  const cells = new Date(year, month + 1, 0).getDate();
  const blanks = leadingBlanks(year, month);
  const detail = open ? byDate.get(open) : undefined;

  return (
    <section>
      <div className="page-toolbar">
        <div>
          <p className="eyebrow">Month at a glance</p>
          <h2 className="page-title">P&amp;L calendar</h2>
          <p className="page-copy">
            {source.source === "broker"
              ? "Only the trades that reached a broker. Charges are this system's estimate from the published rate card until the broker's own figure is fetched after settlement — where one has been, the day shows both."
              : "Simulated trades only. These never reached a broker and no money moved; the figures are the paper ledger's."}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {source.source === "broker" && (
            <BrokerSelect broker={source.broker} choices={source.choices} setBroker={source.setBroker} />
          )}
          <button className="secondary-button" onClick={() => step(-1)} aria-label="Previous month">
            <ChevronLeft className="h-4 w-4" />
          </button>
          <span className="min-w-[10rem] text-center text-sm font-semibold text-slate-900 dark:text-white">
            {monthLabel(year, month)}
          </span>
          <button className="secondary-button" onClick={() => step(1)} aria-label="Next month">
            <ChevronRight className="h-4 w-4" />
          </button>
        </div>
      </div>

      <div className="mt-5 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Tile label="Month net" value={rupees(String(total), { signed: true })} tone={total} />
        <Tile label="Charges" value={rupees(String(charges))} tone={0} />
        <Tile label="Green days" value={String(green)} tone={green ? 1 : 0} />
        <Tile label="Red days" value={String(red)} tone={red ? -1 : 0} />
      </div>

      <article className="panel mt-6 p-4 sm:p-5">
        <div className="grid grid-cols-7 gap-1 sm:gap-2">
          {WEEKDAYS.map((label) => (
            <div key={label} className="pb-1 text-center text-[11px] font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
              {label}
            </div>
          ))}
          {Array.from({ length: blanks }, (_, index) => (
            <div key={`blank-${index}`} />
          ))}
          {Array.from({ length: cells }, (_, index) => {
            const date = key(year, month, index + 1);
            return (
              <DayCell
                key={date}
                day={index + 1}
                date={date}
                row={byDate.get(date)}
                loading={loading}
                selected={open === date}
                onSelect={() => setOpen(open === date ? null : date)}
              />
            );
          })}
        </div>
      </article>

      {detail && <DayDetail day={detail} trades={trades} loading={tradesLoading} />}
      {open && !detail && (
        <p className="mt-4 text-sm text-slate-500 dark:text-slate-400">Nothing was traded on {open}.</p>
      )}
    </section>
  );
}

function Tile({ label, value, tone }: { label: string; value: string; tone: number }) {
  const colour = tone > 0 ? "text-emerald-400" : tone < 0 ? "text-rose-400" : "text-slate-900 dark:text-white";
  return (
    <article className="glass-inset rounded-md p-4">
      <p className="eyebrow">{label}</p>
      <p className={`mt-2 numeric text-2xl font-semibold ${colour}`}>{value}</p>
    </article>
  );
}

function DayCell({
  day,
  date,
  row,
  loading,
  selected,
  onSelect,
}: {
  day: number;
  date: string;
  row: HistoryDay | undefined;
  loading: boolean;
  selected: boolean;
  onSelect: () => void;
}) {
  const net = row ? toNumber(row.net_pnl) : null;
  // A day with no record is not a flat day. It is a day this system has nothing
  // to say about, and it must not be drawn as a ₹0 result.
  const base = "rounded-md border p-1.5 text-left transition min-h-[62px] sm:min-h-[74px]";
  const tone =
    net === null
      ? "border-slate-200/60 dark:border-slate-800 bg-transparent text-slate-400 dark:text-slate-600"
      : net > 0
      ? "border-emerald-500/40 bg-emerald-500/15 text-emerald-700 dark:text-emerald-300"
      : net < 0
      ? "border-rose-500/40 bg-rose-500/15 text-rose-700 dark:text-rose-300"
      : "border-slate-400/40 bg-slate-500/10 text-slate-600 dark:text-slate-300";
  const ring = selected ? "ring-2 ring-sky-400" : "";

  return (
    <button
      type="button"
      onClick={onSelect}
      disabled={!row}
      aria-label={row ? `${date}: ${rupees(row.net_pnl, { signed: true })} over ${row.trades} trade(s)` : `${date}: no trades`}
      className={`${base} ${tone} ${ring} ${row ? "hover:brightness-110" : "cursor-default"}`}
    >
      <span className="block text-[11px] font-semibold opacity-80">{day}</span>
      {loading && !row ? (
        <span className="mt-2 block h-3 w-full rounded bg-slate-500/10" />
      ) : row ? (
        <>
          <span className="numeric mt-1 block text-[11px] font-semibold leading-tight sm:text-xs">
            {rupees(row.net_pnl, { signed: true })}
          </span>
          <span className="block text-[10px] opacity-75">
            {row.trades} trade{row.trades === 1 ? "" : "s"}
          </span>
        </>
      ) : null}
    </button>
  );
}

function DayDetail({ day, trades, loading }: { day: HistoryDay; trades: HistoryTrade[]; loading: boolean }) {
  return (
    <article className="panel mt-4 overflow-hidden">
      <div className="flex flex-wrap items-baseline justify-between gap-2 border-b border-slate-200 dark:border-slate-800 px-5 py-4">
        <div>
          <p className="eyebrow">Every trade on this day</p>
          <h3 className="font-semibold text-slate-900 dark:text-white">{day.session_date}</h3>
        </div>
        <span className="text-xs text-slate-500 dark:text-slate-400">
          {day.wins}W / {day.losses}L / {day.scratches} scratch · {day.live_trades} live
        </span>
      </div>

      {loading ? (
        <div className="space-y-2 p-5">
          {Array.from({ length: 3 }, (_, index) => (
            <div key={index} className="skeleton h-10" />
          ))}
        </div>
      ) : trades.length ? (
        <div className="table-scroll">
          <table className="terminal-table">
            <thead>
              <tr>
                <th>Stock</th>
                <th>Qty</th>
                <th>Entry</th>
                <th>Exit</th>
                <th>Target</th>
                <th>Gross</th>
                <th>Charges</th>
                <th>Net</th>
              </tr>
            </thead>
            <tbody>
              {trades.map((trade) => (
                <tr key={trade.position_id}>
                  <td>
                    <strong className="block text-slate-900 dark:text-slate-100">{trade.script_name}</strong>
                    <span className="text-[11px] opacity-75">
                      {trade.side} · {trade.execution_mode === "LIVE" ? "Live" : "Paper"}
                      {trade.execution_mode === "LIVE" && trade.price_source === "MODEL" ? " · modelled fills" : ""}
                      {trade.is_open ? " · still open" : ""}
                    </span>
                  </td>
                  <td className="numeric">{trade.quantity}</td>
                  <td className="numeric">{rupees(trade.entry_price)}</td>
                  <td className="numeric">{rupees(trade.exit_price)}</td>
                  <td className="numeric">{rupees(trade.target_price)}</td>
                  <td className={`numeric ${pnlTone(trade.gross_pnl)}`}>
                    {rupees(trade.gross_pnl, { signed: true })}
                    {trade.slippage !== null && toNumber(trade.slippage) !== 0 && (
                      <span className="text-[11px] opacity-75">
                        {rupees(trade.slippage, { signed: true })} vs model
                      </span>
                    )}
                  </td>
                  <td className="numeric">{rupees(trade.charges)}</td>
                  <td className={`numeric ${pnlTone(trade.net_pnl)}`}>{rupees(trade.net_pnl, { signed: true })}</td>
                </tr>
              ))}
            </tbody>
            <tfoot>
              {/* The day's totals as the server computed them, not a sum of the
                  rows above. Adding eight Decimals in a browser is how a total
                  comes to disagree with the figure the same day shows on the
                  History screen, and the one that disagrees is always this one. */}
              <tr className="border-t border-slate-300 dark:border-slate-700 font-semibold">
                <td colSpan={5}>Day total</td>
                <td className={`numeric ${pnlTone(day.gross_pnl)}`}>{rupees(day.gross_pnl, { signed: true })}</td>
                <td className="numeric">{rupees(day.charges)}</td>
                <td className={`numeric ${pnlTone(day.net_pnl)}`}>{rupees(day.net_pnl, { signed: true })}</td>
              </tr>
            </tfoot>
          </table>
        </div>
      ) : (
        <p className="p-5 text-sm text-slate-500 dark:text-slate-400">
          The day has a record but no individual trades to list. A day whose only entry is still open, or one recorded
          before per-trade history was kept, reads like this.
        </p>
      )}

      <div className="space-y-2 px-5 py-4 text-xs text-slate-500 dark:text-slate-400">
        {trades.some((trade) => trade.execution_mode === "LIVE" && trade.price_source === "MODEL") && (
          <p>
            A live row marked <strong>modelled fills</strong> is priced from completed candles, not from what the
            broker filled, because no fill has been recorded against it yet. Those prices will not match the
            broker&apos;s app. Reconciliation records the real fills on its next pass.
          </p>
        )}
        {trades.some((trade) => trade.is_open) && (
          <p>
            A trade still open carries no exit and no realised figure. Its unrealised movement is in the day&apos;s
            {" "}
            {rupees(day.unrealized_pnl, { signed: true })}, and it is not part of the net above.
          </p>
        )}
        {day.broker_realized_pnl !== null && (
          <p>
            {day.broker ?? "The broker"} reported {rupees(day.broker_realized_pnl, { signed: true })} realised and{" "}
            {rupees(day.broker_charges)} in charges for this day, recorded beside our figures rather than replacing them.
          </p>
        )}
        {day.halt_reason && <p className="text-amber-600 dark:text-amber-300">Trading halted: {day.halt_reason}</p>}
        <p>{day.reconciliation_note}</p>
      </div>
    </article>
  );
}
