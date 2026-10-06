"use client";

import { RefreshCw } from "lucide-react";
import { type ReactNode, useCallback, useEffect, useState } from "react";

import { api, type BrokerSnapshot, type HistoryOverview } from "../../components/api";
import { BrokerSelect } from "../orders/source-controls";
import type { OrderSource } from "../orders/use-order-source";
import { formatIstTimestamp } from "../../lib/formatting";
import { Money, percent, ratio } from "../history/money";

/**
 * Two P&L questions, kept apart because they have different answers.
 *
 * **Right now** is the broker's: realised today and what is still moving, as
 * the broker marks it. It is the figure that decides whether to stop for the
 * day, and it is the broker's claim, not ours.
 *
 * **This period** is ours: every trade this system recorded, gross, charges and
 * net kept in three columns. It is the figure that says whether a strategy
 * works, and no part of it is a projection.
 *
 * They will not always agree, and nothing here reconciles them. Where they
 * disagree that is information — it is what the reconciliation status on the
 * History screen exists to report — and averaging them away would hide it.
 */

function isoDate(value: Date): string {
  return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}-${String(value.getDate()).padStart(2, "0")}`;
}

function money(value: number | null): string {
  if (value === null) return "not reported";
  const text = Math.abs(value).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return value < 0 ? `−₹${text}` : `₹${text}`;
}

function tone(value: number | null): string {
  if (value === null || value === 0) return "text-slate-900 dark:text-white";
  return value > 0 ? "text-emerald-400" : "text-rose-400";
}

export function PnlSummary({
  onMessage,
  source,
}: {
  onMessage: (message: string) => void;
  source: OrderSource;
}) {
  const mode = source.source === "broker" ? ("LIVE" as const) : ("PAPER" as const);
  const broker = source.source === "broker" ? (source.broker ?? undefined) : undefined;
  const [snapshot, setSnapshot] = useState<BrokerSnapshot | null>(null);
  const [overview, setOverview] = useState<HistoryOverview | null>(null);
  const [days, setDays] = useState(30);
  const [loading, setLoading] = useState(true);

  const load = useCallback(
    async (force: boolean) => {
      setLoading(true);
      const to = new Date();
      const from = new Date();
      from.setDate(from.getDate() - (days - 1));
      try {
        const [nextOverview, nextSnapshot] = await Promise.all([
          api.historyOverview({ from_date: isoDate(from), to_date: isoDate(to), mode, broker }),
          api.brokerSnapshot({ force }).catch(() => null),
        ]);
        setOverview(nextOverview);
        setSnapshot(nextSnapshot);
      } catch (error) {
        onMessage(error instanceof Error ? error.message : "Could not load the P&L summary");
      } finally {
        setLoading(false);
      }
    },
    [days, onMessage, mode, broker],
  );

  useEffect(() => {
    void load(false);
  }, [load]);

  return (
    <section>
      <div className="page-toolbar">
        <div>
          <p className="eyebrow">Realised and unrealised</p>
          <h2 className="page-title">P&amp;L</h2>
          <p className="page-copy">
            What the broker says about today, and what the period came to.{" "}
            {source.source === "broker"
              ? "The period below counts only trades that reached a broker."
              : "The period below counts only simulated trades, which never reached one."}{" "}
            Gross, charges and net are three figures, not one — a day that made ₹900 before costs and ₹340 after is a
            ₹340 day.
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
            <option value={7}>Last 7 days</option>
            <option value={30}>Last 30 days</option>
            <option value={90}>Last 90 days</option>
            <option value={365}>Last year</option>
          </select>
          <button className="secondary-button" onClick={() => void load(true)} disabled={loading}>
            <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
            Refresh
          </button>
        </div>
      </div>

      <article className="panel mt-5 p-5">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h3 className="font-semibold text-slate-900 dark:text-white">Right now, at the broker</h3>
          {snapshot?.readable ? (
            <span className="text-xs text-slate-500 dark:text-slate-400">
              {snapshot.broker} · read {formatIstTimestamp(snapshot.fetched_at)}
              {snapshot.stale ? " · cached" : ""}
            </span>
          ) : null}
        </div>
        {snapshot?.readable ? (
          <dl className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <Figure label="Realised today" value={money(snapshot.realised)} className={tone(snapshot.realised)} />
            <Figure label="Unrealised" value={money(snapshot.unrealised)} className={tone(snapshot.unrealised)} />
            <Figure label="Open positions" value={String(snapshot.open_positions)} />
            <Figure label="Working orders" value={String(snapshot.working_orders)} />
          </dl>
        ) : (
          <p className="mt-3 text-sm text-slate-500 dark:text-slate-400">
            {snapshot?.detail ?? "The broker has not been read yet."} Today&apos;s figures below are ours alone until it
            can be.
          </p>
        )}
      </article>

      {overview && (
        <article className="panel mt-4 p-5">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <h3 className="font-semibold text-slate-900 dark:text-white">Our records, {overview.from_date} to {overview.to_date}</h3>
            <span className="text-xs text-slate-500 dark:text-slate-400">
              {overview.trading_days} trading day{overview.trading_days === 1 ? "" : "s"} · {overview.trades} trade
              {overview.trades === 1 ? "" : "s"} · {overview.live_trades} live
            </span>
          </div>
          <dl className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <Figure label="Gross" value={<Money value={overview.gross_pnl} signed />} />
            <Figure label="Charges" value={<Money value={overview.charges} />} />
            <Figure label="Net" value={<Money value={overview.net_pnl} signed />} />
            <Figure label="Win rate" value={percent(overview.win_rate_percent)} />
            <Figure label="Profit factor" value={ratio(overview.profit_factor)} />
            <Figure label="Expectancy per trade" value={<Money value={overview.expectancy} signed />} />
            <Figure label="Largest win" value={<Money value={overview.largest_win} signed />} />
            <Figure label="Largest loss" value={<Money value={overview.largest_loss} signed />} />
          </dl>
          <p className="mt-4 text-xs text-slate-500 dark:text-slate-400">
            Charges came to {percent(overview.charges_as_percent_of_gross)} of gross. Best day{" "}
            {overview.best_day ?? "—"}, worst day {overview.worst_day ?? "—"}. Open trades at the end of the period:{" "}
            {overview.open_trades}. Halted days: {overview.halted_days}.
          </p>
          {overview.trades === 0 && (
            <p className="mt-2 text-xs text-slate-500 dark:text-slate-400">
              No trades were recorded in this period, so there is nothing here to read as performance. A figure built
              from zero trades is not a small result; it is no result.
            </p>
          )}
        </article>
      )}

      {/* The same period as the broker's own statements total it, over the days
          it has settled -- kept as its own panel rather than replacing the one
          above, because the two cover different sets of days and stacking them
          in one table would invite reading a figure over four settled sessions
          as a figure over nineteen traded ones. */}
      {overview && source.source === "broker" && overview.broker_days > 0 && (
        <article className="panel mt-4 p-5">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <h3 className="font-semibold text-slate-900 dark:text-white">
              As {source.broker ?? "the broker"} reports it
            </h3>
            <span className="text-xs text-slate-500 dark:text-slate-400">
              {overview.broker_days} settled day{overview.broker_days === 1 ? "" : "s"}
              {overview.days_pending_broker > 0 ? ` · ${overview.days_pending_broker} still pending` : ""}
            </span>
          </div>
          <dl className="mt-4 grid gap-3 sm:grid-cols-3">
            <Figure label="Realised" value={<Money value={overview.broker_realized_pnl} signed />} />
            <Figure label="Charges" value={<Money value={overview.broker_charges} />} />
            <Figure label="Net" value={<Money value={overview.broker_net_pnl} signed />} />
          </dl>
          <p className="mt-4 text-xs text-slate-500 dark:text-slate-400">
            These are the broker&apos;s own figures for the {overview.broker_days} day
            {overview.broker_days === 1 ? "" : "s"} it has published, and they cover only those days — not the whole
            period above.
            {overview.days_pending_broker > 0
              ? ` ${overview.days_pending_broker} live day${overview.days_pending_broker === 1 ? "" : "s"} in this period ${overview.days_pending_broker === 1 ? "is" : "are"} still waiting on a statement.`
              : ""}{" "}
            Nothing here replaces our records; where the two disagree, the P&amp;L calendar says so day by day.
          </p>
        </article>
      )}
    </section>
  );
}

function Figure({ label, value, className = "" }: { label: string; value: ReactNode; className?: string }) {
  return (
    <div className="glass-inset rounded-md p-3">
      <dt className="eyebrow">{label}</dt>
      <dd className={`mt-1 numeric text-lg font-semibold ${className}`}>{value}</dd>
    </div>
  );
}
