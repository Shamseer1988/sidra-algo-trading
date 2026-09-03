"use client";

import { ClipboardList, RefreshCw, WalletCards } from "lucide-react";
import { type ReactNode, useEffect, useState } from "react";

import { api, type PaperExecutionSummary, type PaperOrder, type PaperPosition } from "../../components/api";
import { formatIstTimestamp, formatPrice, titleCase } from "../../lib/formatting";
import { resolveScriptName } from "../../lib/instruments";

const emptySummary: PaperExecutionSummary = {
  orders: 0,
  pending_orders: 0,
  fills: 0,
  open_positions: 0,
  realized_pnl: 0,
  unrealized_pnl: 0,
  total_pnl: 0,
  fees_total: 0,
};

export function PaperExecutionPanel({ view }: { view: "orders" | "positions" }) {
  const [summary, setSummary] = useState<PaperExecutionSummary>(emptySummary);
  const [orders, setOrders] = useState<PaperOrder[]>([]);
  const [positions, setPositions] = useState<PaperPosition[]>([]);
  const [loading, setLoading] = useState(true);

  const load = () => {
    setLoading(true);
    void Promise.all([api.paperSummary(), api.paperOrders(), api.paperPositions()])
      .then(([nextSummary, nextOrders, nextPositions]) => {
        setSummary(nextSummary);
        setOrders(nextOrders);
        setPositions(nextPositions);
      })
      .finally(() => setLoading(false));
  };

  useEffect(load, []);

  const pnlClass =
    summary.total_pnl > 0
      ? "text-emerald-500 dark:text-emerald-400"
      : summary.total_pnl < 0
      ? "text-rose-500 dark:text-rose-400"
      : "text-slate-500 dark:text-slate-400";

  return (
    <section>
      <div className="page-toolbar">
        <div>
          <p className="eyebrow">Deterministic simulation ledger</p>
          <h2 className="page-title">{view === "orders" ? "Paper orderbook" : "Paper positions"}</h2>
          <p className="page-copy">
            Completed candles drive simulated fills, costs, bracket exits, and P&amp;L. These records cannot reach a broker.
          </p>
        </div>
        <button className="secondary-button" onClick={load} disabled={loading}>
          <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
          Refresh
        </button>
      </div>

      <div className="mt-5 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Metric label="Open positions" value={summary.open_positions} />
        <Metric label="Pending orders" value={summary.pending_orders} />
        <Metric label="Recorded fills" value={summary.fills} />
        <Metric label="Net paper P&amp;L" value={`₹${formatPrice(summary.total_pnl)}`} className={pnlClass} />
      </div>

      {view === "orders" ? <Orderbook rows={orders} loading={loading} /> : <Positions rows={positions} loading={loading} />}
    </section>
  );
}

function Metric({
  label,
  value,
  className = "text-slate-900 dark:text-white",
}: {
  label: string;
  value: string | number;
  className?: string;
}) {
  return (
    <article className="glass-inset rounded-md p-4">
      <p className="eyebrow">{label}</p>
      <p className={`mt-2 numeric text-2xl font-semibold ${className}`}>{value}</p>
    </article>
  );
}

function Orderbook({ rows, loading }: { rows: PaperOrder[]; loading: boolean }) {
  return (
    <article className="panel mt-6 overflow-hidden">
      <div className="flex items-center gap-3 border-b border-slate-200 dark:border-slate-800 px-5 py-4">
        <ClipboardList className="h-5 w-5 text-sky-500 dark:text-sky-300" />
        <div>
          <p className="eyebrow">Order lifecycle</p>
          <h3 className="font-semibold text-slate-900 dark:text-white">Simulated orderbook</h3>
        </div>
      </div>
      {loading ? (
        <div className="space-y-2 p-5">
          {Array.from({ length: 5 }, (_, index) => (
            <div key={index} className="skeleton h-12" />
          ))}
        </div>
      ) : rows.length ? (
        <div className="table-scroll">
          <table className="terminal-table">
            <thead>
              <tr>
                <th>Script</th>
                <th>Instrument</th>
                <th>Role</th>
                <th>Order</th>
                <th>Fill</th>
                <th>Fees</th>
                <th>Status</th>
                <th>Eligible</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const script = resolveScriptName(row.instrument_token);
                return (
                  <tr key={row.id}>
                    <td>
                      <span className="inline-block rounded bg-emerald-500/15 px-2.5 py-0.5 text-xs font-bold text-emerald-600 dark:text-emerald-300 border border-emerald-500/30">
                        {script}
                      </span>
                    </td>
                    <td>
                      <strong className="block font-mono text-slate-900 dark:text-slate-100">
                        {row.instrument_token}
                      </strong>
                      <span className="text-[11px] opacity-75">{row.side}</span>
                    </td>
                    <td>{titleCase(row.order_role)}</td>
                    <td>
                      {titleCase(row.order_type)} · {row.quantity}
                    </td>
                    <td className="numeric">
                      {row.filled_quantity}/{row.quantity}
                      {row.average_fill_price !== null && <span> · ₹{formatPrice(row.average_fill_price)}</span>}
                    </td>
                    <td className="numeric">₹{formatPrice(row.fee_total)}</td>
                    <td>
                      <span
                        className={`status-pill ${
                          row.status === "FILLED"
                            ? "status-good"
                            : row.status === "CANCELLED" || row.status === "REJECTED"
                            ? "status-bad"
                            : "status-warn"
                        }`}
                      >
                        {titleCase(row.status)}
                      </span>
                    </td>
                    <td className="muted-cell">{formatIstTimestamp(row.eligible_after)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <Empty
          icon={<ClipboardList className="h-6 w-6" />}
          text="The orderbook will populate after a paper signal reaches the next completed candle."
        />
      )}
    </article>
  );
}

function Positions({ rows, loading }: { rows: PaperPosition[]; loading: boolean }) {
  return (
    <article className="panel mt-6 overflow-hidden">
      <div className="flex items-center gap-3 border-b border-slate-200 dark:border-slate-800 px-5 py-4">
        <WalletCards className="h-5 w-5 text-violet-500 dark:text-violet-300" />
        <div>
          <p className="eyebrow">Mark-to-market</p>
          <h3 className="font-semibold text-slate-900 dark:text-white">Signal-linked paper positions</h3>
        </div>
      </div>
      {loading ? (
        <div className="space-y-2 p-5">
          {Array.from({ length: 5 }, (_, index) => (
            <div key={index} className="skeleton h-12" />
          ))}
        </div>
      ) : rows.length ? (
        <div className="table-scroll">
          <table className="terminal-table">
            <thead>
              <tr>
                <th>Script</th>
                <th>Instrument</th>
                <th>Side</th>
                <th>Quantity</th>
                <th>Average entry</th>
                <th>CMP</th>
                <th>Unrealized</th>
                <th>Net P&amp;L</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const script = resolveScriptName(row.instrument_token);
                return (
                  <tr key={row.id}>
                    <td>
                      <span className="inline-block rounded bg-emerald-500/15 px-2.5 py-0.5 text-xs font-bold text-emerald-600 dark:text-emerald-300 border border-emerald-500/30">
                        {script}
                      </span>
                    </td>
                    <td>
                      <strong className="block font-mono text-slate-900 dark:text-slate-100">
                        {row.instrument_token}
                      </strong>
                      <span className="text-[11px] opacity-75">{row.strategy_version}</span>
                    </td>
                    <td>
                      <span className={`status-pill ${row.side === "LONG" ? "status-good" : "status-bad"}`}>
                        {row.side}
                      </span>
                    </td>
                    <td className="numeric">
                      {row.open_quantity}/{row.initial_quantity}
                    </td>
                    <td className="numeric">
                      {row.average_entry_price === null ? "—" : `₹${formatPrice(row.average_entry_price)}`}
                    </td>
                    <td className="numeric">
                      {row.current_price === null ? "—" : `₹${formatPrice(row.current_price)}`}
                    </td>
                    <td
                      className={`numeric ${
                        row.unrealized_pnl >= 0
                          ? "text-emerald-500 dark:text-emerald-300"
                          : "text-rose-500 dark:text-rose-300"
                      }`}
                    >
                      ₹{formatPrice(row.unrealized_pnl)}
                    </td>
                    <td
                      className={`numeric ${
                        row.total_pnl >= 0
                          ? "text-emerald-500 dark:text-emerald-300"
                          : "text-rose-500 dark:text-rose-300"
                      }`}
                    >
                      ₹{formatPrice(row.total_pnl)}
                    </td>
                    <td>
                      <span className="status-pill status-warn">{titleCase(row.status)}</span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <Empty
          icon={<WalletCards className="h-6 w-6" />}
          text="Positions appear only after a paper entry order receives a simulated fill."
        />
      )}
    </article>
  );
}

function Empty({ icon, text }: { icon: ReactNode; text: string }) {
  return (
    <div className="empty-inset m-5 flex flex-col items-center gap-3 p-8 text-center text-sm text-slate-500 dark:text-slate-400">
      {icon}
      <p>{text}</p>
    </div>
  );
}
