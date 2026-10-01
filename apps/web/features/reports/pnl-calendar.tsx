"use client";

import { ChevronLeft, ChevronRight } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { api, type HistoryDay } from "../../components/api";
import { Money, rupees, toNumber } from "../history/money";

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

export function PnlCalendar({ onMessage }: { onMessage: (message: string) => void }) {
  const today = useMemo(() => new Date(), []);
  const [year, setYear] = useState(today.getFullYear());
  const [month, setMonth] = useState(today.getMonth());
  const [days, setDays] = useState<HistoryDay[]>([]);
  const [loading, setLoading] = useState(true);
  const [open, setOpen] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const last = new Date(year, month + 1, 0).getDate();
      setDays(await api.historyDaily({ from_date: key(year, month, 1), to_date: key(year, month, last) }));
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Could not load the month");
    } finally {
      setLoading(false);
    }
  }, [year, month, onMessage]);

  useEffect(() => {
    void load();
  }, [load]);

  const byDate = useMemo(() => new Map(days.map((day) => [day.session_date, day])), [days]);

  function step(delta: number) {
    const next = new Date(year, month + delta, 1);
    setYear(next.getFullYear());
    setMonth(next.getMonth());
    setOpen(null);
  }

  const total = days.reduce((sum, day) => sum + (toNumber(day.net_pnl) ?? 0), 0);
  const charges = days.reduce((sum, day) => sum + (toNumber(day.charges) ?? 0), 0);
  const green = days.filter((day) => (toNumber(day.net_pnl) ?? 0) > 0).length;
  const red = days.filter((day) => (toNumber(day.net_pnl) ?? 0) < 0).length;
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
            Net of charges, from this system&apos;s own records. Charges are estimated from the published rate card
            unless a broker figure has been fetched for that day — where one has, the day shows both.
          </p>
        </div>
        <div className="flex items-center gap-2">
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

      {detail && <DayDetail day={detail} />}
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

function DayDetail({ day }: { day: HistoryDay }) {
  return (
    <article className="panel mt-4 p-5">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="font-semibold text-slate-900 dark:text-white">{day.session_date}</h3>
        <span className="text-xs text-slate-500 dark:text-slate-400">
          {day.wins}W / {day.losses}L / {day.scratches} scratch · {day.live_trades} live
        </span>
      </div>
      <dl className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Figure label="Gross" value={<Money value={day.gross_pnl} signed />} />
        <Figure label="Charges" value={<Money value={day.charges} />} />
        <Figure label="Net" value={<Money value={day.net_pnl} signed />} />
        <Figure label="Unrealised at close" value={<Money value={day.unrealized_pnl} signed />} />
      </dl>
      {day.broker_realized_pnl !== null && (
        <p className="mt-4 text-xs text-slate-500 dark:text-slate-400">
          {day.broker ?? "The broker"} reported {rupees(day.broker_realized_pnl, { signed: true })} realised and{" "}
          {rupees(day.broker_charges)} in charges for this day, recorded beside our figures rather than replacing them.
        </p>
      )}
      {day.halt_reason && (
        <p className="mt-2 text-xs text-amber-600 dark:text-amber-300">Trading halted: {day.halt_reason}</p>
      )}
      <p className="mt-2 text-xs text-slate-500 dark:text-slate-400">{day.reconciliation_note}</p>
    </article>
  );
}

function Figure({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="glass-inset rounded-md p-3">
      <dt className="eyebrow">{label}</dt>
      <dd className="mt-1 text-lg font-semibold">{value}</dd>
    </div>
  );
}
