"use client";

import { CalendarRange, RefreshCw } from "lucide-react";
import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";

import { api, type PaperDay, type PaperDayOrder, type PaperDayPosition } from "../../components/api";
import { formatIstTimestamp, formatPrice, titleCase } from "../../lib/formatting";
import { strategyLabel } from "../../lib/strategy-names";

/**
 * Today's paper trading, and only today's.
 *
 * This screen used to list the last 250 simulated positions of any date under
 * an all-time total, which read as one very good day. Earlier days live in
 * History and Reports. Every figure here is totalled by the server.
 */

function signed(value: number): string {
  const text = Math.abs(value).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return value < 0 ? `−₹${text}` : `${value > 0 ? "+" : ""}₹${text}`;
}

function tone(value: number): string {
  if (value === 0) return "text-slate-500 dark:text-slate-400";
  return value > 0 ? "text-emerald-600 dark:text-emerald-300" : "text-rose-600 dark:text-rose-300";
}

const price = (value: number | null) => (value === null ? "—" : `₹${formatPrice(value)}`);

export function PaperExecutionPanel({
  view,
  onOpenHistory,
}: {
  view: "orders" | "positions";
  onOpenHistory: () => void;
}) {
  const [day, setDay] = useState<PaperDay | null>(null);
  const [strategy, setStrategy] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const latest = useRef(0);

  const load = useCallback(async () => {
    // Only the newest request may draw: a slow answer for the previous filter
    // must not replace the rows of the one now selected.
    const request = ++latest.current;
    setLoading(true);
    try {
      const next = await api.paperDay({ strategy: strategy || undefined });
      if (request !== latest.current) return;
      setDay(next);
      setError(null);
    } catch (caught) {
      if (request === latest.current) {
        setDay(null);
        setError(caught instanceof Error ? caught.message : "Could not load today's paper trading");
      }
    } finally {
      if (request === latest.current) setLoading(false);
    }
  }, [strategy]);

  useEffect(() => {
    void load();
  }, [load]);

  const totals = day?.totals;
  return (
    <section>
      <div className="page-toolbar">
        <div>
          <p className="eyebrow">Today only{day ? ` · ${day.session_date}` : ""}</p>
          <h2 className="page-title">{view === "orders" ? "Paper orderbook" : "Paper positions"}</h2>
          <p className="page-copy">
            Simulated trades; none of these reached a broker. A trade that was also sent to the broker is marked
            Live copy and left out of the totals; the real one is under Live.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button className="secondary-button" onClick={onOpenHistory}>
            <CalendarRange className="h-4 w-4" />
            Earlier days
          </button>
          <button className="secondary-button" onClick={() => void load()} disabled={loading}>
            <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
            Refresh
          </button>
        </div>
      </div>

      {error && (
        <p role="alert" className="panel mt-5 p-5 text-sm text-rose-600 dark:text-rose-300">
          {error}
        </p>
      )}

      {day && totals && (
        <>
          <div role="group" aria-label="Strategy" className="mt-5 flex flex-wrap gap-2">
            <Chip active={!strategy} onClick={() => setStrategy("")}>
              All strategies
            </Chip>
            {day.strategies.map((item) => (
              <Chip key={item.strategy} active={strategy === item.strategy} onClick={() => setStrategy(item.strategy)}>
                {strategyLabel(item.strategy)}
                {item.trades > 0 ? (
                  <>
                    {" "}· {item.trades}{" "}
                    <span className={`numeric ${tone(item.net_pnl)}`}>{signed(item.net_pnl)}</span>
                  </>
                ) : (
                  <span className="opacity-70"> · none counted</span>
                )}
              </Chip>
            ))}
          </div>

          <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <Metric
              label="Net today"
              value={signed(totals.net_pnl)}
              className={tone(totals.net_pnl)}
              note={`${signed(totals.gross_pnl)} gross, ₹${formatPrice(totals.charges)} charges`}
            />
            <Metric
              label="Trades"
              value={totals.trades}
              note={`${totals.wins} won, ${totals.losses} lost${totals.scratches ? `, ${totals.scratches} flat` : ""}${day.live_copies ? `. Plus ${day.live_copies} live cop${day.live_copies === 1 ? "y" : "ies"}, not counted` : ""}`}
            />
            <Metric label="Open positions" value={totals.open_positions} note="still running" />
            <Metric label="Pending orders" value={day.pending_orders} note="waiting for a fill" />
          </div>

          {view === "orders" ? <Orderbook rows={day.orders} /> : <Positions rows={day.positions} />}
        </>
      )}
      {!day && loading && (
        <div className="mt-5 space-y-2">
          {Array.from({ length: 5 }, (_, index) => (
            <div key={index} className="skeleton h-12" />
          ))}
        </div>
      )}
    </section>
  );
}

function Chip({ active, onClick, children }: { active: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={`rounded-full border px-3 py-1.5 text-xs font-semibold transition ${
        active
          ? "border-emerald-500 bg-emerald-500/15 text-slate-900 dark:text-white"
          : "border-slate-300 text-slate-600 hover:border-slate-400 dark:border-slate-700 dark:text-slate-300"
      }`}
    >
      {children}
    </button>
  );
}

function Metric({
  label,
  value,
  note,
  className = "text-slate-900 dark:text-white",
}: {
  label: string;
  value: string | number;
  note: string;
  className?: string;
}) {
  return (
    <article className="glass-inset rounded-md p-4">
      <p className="eyebrow">{label}</p>
      <p className={`mt-2 numeric text-2xl font-semibold ${className}`}>{value}</p>
      <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">{note}</p>
    </article>
  );
}

function Frame({ title, empty, children }: { title: string; empty: string | null; children: ReactNode }) {
  return (
    <article className="panel mt-6 overflow-hidden">
      <h3 className="border-b border-slate-200 px-5 py-4 font-semibold text-slate-900 dark:border-slate-800 dark:text-white">
        {title}
      </h3>
      {empty ? (
        <p className="p-8 text-center text-sm text-slate-500 dark:text-slate-400">{empty}</p>
      ) : (
        <div className="table-scroll">{children}</div>
      )}
    </article>
  );
}

function Orderbook({ rows }: { rows: PaperDayOrder[] }) {
  return (
    <Frame title="Simulated orderbook" empty={rows.length ? null : "No paper orders today."}>
      <table className="terminal-table">
        <thead>
          <tr>
            <th>Time</th>
            <th>Share</th>
            <th>Strategy</th>
            <th>Order</th>
            <th>Filled</th>
            <th>Price</th>
            <th>Charges</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.id}>
              <td className="muted-cell">{formatIstTimestamp(row.created_at)}</td>
              <td>
                <strong title={row.instrument_token}>{row.script_name}</strong>
                {row.live_copy && <LiveCopy />}
              </td>
              <td>{strategyLabel(row.strategy)}</td>
              <td>
                {row.side} · {titleCase(row.order_role)}
                <span className="muted-cell block text-[11px]">{titleCase(row.order_type)}</span>
              </td>
              <td className="numeric">
                {row.filled_quantity}/{row.quantity}
              </td>
              <td className="numeric">{price(row.average_fill_price)}</td>
              <td className="numeric">₹{formatPrice(row.fee_total)}</td>
              <td>
                <span
                  title={row.rejection_reason ?? undefined}
                  className={`status-pill ${
                    row.status === "FILLED"
                      ? "status-good"
                      : row.status === "REJECTED"
                        ? "status-bad"
                        : "status-watch"
                  }`}
                >
                  {titleCase(row.status)}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </Frame>
  );
}

function Positions({ rows }: { rows: PaperDayPosition[] }) {
  return (
    <Frame title="Simulated positions" empty={rows.length ? null : "No paper positions today."}>
      <table className="terminal-table">
        <thead>
          <tr>
            <th>Share</th>
            <th>Strategy</th>
            <th>Side</th>
            <th>Qty</th>
            <th>Entry</th>
            <th>Exit / now</th>
            <th>Stop / target</th>
            <th>Gross</th>
            <th>Charges</th>
            <th>Net</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const closed = row.status === "CLOSED";
            // One figure is always zero: realised while open, unrealised once closed.
            return (
              <tr key={row.id}>
                <td>
                  <strong title={row.instrument_token}>{row.script_name}</strong>
                  {row.live_copy && <LiveCopy />}
                </td>
                <td>{strategyLabel(row.strategy)}</td>
                <td>
                  <span className={`status-pill ${row.side === "LONG" ? "status-good" : "status-bad"}`}>{row.side}</span>
                </td>
                <td className="numeric">
                  {closed ? row.initial_quantity : `${row.open_quantity}/${row.initial_quantity}`}
                </td>
                <td className="numeric">
                  {price(row.average_entry_price)}
                  <span className="muted-cell block text-[11px]">{formatIstTimestamp(row.opened_at)}</span>
                </td>
                <td className="numeric">
                  {price(closed ? row.average_exit_price : row.current_price)}
                  <span className="muted-cell block text-[11px]">
                    {closed ? formatIstTimestamp(row.closed_at) : "now"}
                  </span>
                </td>
                <td className="numeric text-xs">
                  {price(row.stop_price)} / {price(row.target_price)}
                </td>
                <td className={`numeric ${tone(closed ? row.realized_pnl : row.unrealized_pnl)}`}>
                  {signed(closed ? row.realized_pnl : row.unrealized_pnl)}
                </td>
                <td className="numeric">₹{formatPrice(row.fees_total)}</td>
                <td className={`numeric font-semibold ${tone(row.total_pnl)}`}>{signed(row.total_pnl)}</td>
                <td>
                  <span className={`status-pill ${closed ? "status-watch" : "status-good"}`}>{titleCase(row.status)}</span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </Frame>
  );
}

function LiveCopy() {
  return (
    <span
      className="status-pill status-watch ml-2 leading-4"
      title="Simulated copy of a trade that was sent to the broker. Not counted in the totals."
    >
      Live copy
    </span>
  );
}
