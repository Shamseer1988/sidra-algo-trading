"use client";

import { X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { api, type HistoryTradeDetail } from "../../components/api";
import { formatIstTimestamp } from "../../lib/formatting";
import { Money, rupees } from "./money";
import { ReconciliationPill } from "./reconciliation";

/**
 * One trade, opened up: the orders that made it and every fill's cost line.
 *
 * The cost breakdown is itemised — brokerage, STT, exchange, GST, SEBI, stamp
 * duty — rather than shown as one "charges" figure. When a local estimate
 * disagrees with a broker's bill, the only useful next question is which line
 * is wrong, and a single total cannot answer it.
 */

const CHARGE_LINES = [
  ["brokerage", "Brokerage"],
  ["stt", "STT"],
  ["exchange_charge", "Exchange"],
  ["gst", "GST"],
  ["sebi_charge", "SEBI"],
  ["stamp_duty", "Stamp duty"],
] as const;

export function TradeDetail({
  positionId,
  onClose,
  onMessage,
}: {
  positionId: string;
  onClose: () => void;
  onMessage: (message: string) => void;
}) {
  const [detail, setDetail] = useState<HistoryTradeDetail | null>(null);
  const panel = useRef<HTMLElement>(null);

  const load = useCallback(async () => {
    try {
      setDetail(await api.historyTrade(positionId));
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Could not load the trade");
      onClose();
    }
  }, [positionId, onClose, onMessage]);

  useEffect(() => {
    void load();
  }, [load]);

  // The panel opens below a long table, so without this the click appears to do
  // nothing on anything but a very tall window.
  useEffect(() => {
    if (detail) panel.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [detail]);

  // Escape closes it, because a panel that can only be dismissed by finding a
  // small button is a panel people leave open.
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);

  if (!detail) return null;
  const { trade, orders, fills } = detail;

  return (
    <article ref={panel} className="panel mt-6 scroll-mt-6 p-5 sm:p-7">
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-slate-800 pb-4">
        <div>
          <p className="eyebrow">{trade.session_date} · {trade.execution_mode === "LIVE" ? "Live" : "Paper"}</p>
          <h3 className="mt-1 text-lg font-semibold text-white">
            {trade.script_name} {trade.side} × {trade.quantity}
          </h3>
          <p className="mt-1 text-xs text-slate-500">{trade.strategy_version}</p>
        </div>
        <div className="flex items-center gap-3">
          <ReconciliationPill
            status={trade.reconciliation}
            label={trade.reconciliation_label}
            title={trade.reconciliation_note}
          />
          <button onClick={onClose} className="icon-button" aria-label="Close trade">
            <X className="h-4 w-4" />
          </button>
        </div>
      </div>

      <p className="mt-4 text-xs leading-5 text-slate-400">{trade.reconciliation_note}</p>

      <div className="mt-5 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Figure label="Gross P&L" node={<Money value={trade.gross_pnl} signed />} note="price movement only" />
        <Figure label="Charges" node={<span className="numeric text-slate-300">{rupees(trade.charges)}</span>} note="estimated" />
        <Figure label="Net P&L" node={<Money value={trade.net_pnl} signed />} note="what it was worth" />
        <Figure
          label="R multiple"
          node={<span className="numeric text-slate-100">{trade.r_multiple ? `${trade.r_multiple}R` : "open"}</span>}
          note={`on ₹${trade.risk_amount} planned risk, net`}
        />
      </div>

      <div className="mt-5 grid gap-3 sm:grid-cols-2 xl:grid-cols-4 text-xs text-slate-400">
        <Line label="Entry" value={`${trade.entry_price ?? "—"} at ${formatIstTimestamp(trade.opened_at)}`} />
        <Line label="Exit" value={`${trade.exit_price ?? "—"} at ${formatIstTimestamp(trade.closed_at)}`} />
        <Line label="Stop / target" value={`${trade.stop_price} / ${trade.target_price}`} />
        <Line label="Position status" value={trade.status} />
      </div>

      <h4 className="mt-7 text-sm font-semibold text-white">Orders</h4>
      <div className="table-scroll mt-3">
        <table className="terminal-table">
          <thead>
            <tr>
              <th>Role</th>
              <th>Type</th>
              <th>Side</th>
              <th>Status</th>
              <th>Qty</th>
              <th>Filled</th>
              <th>Avg price</th>
              <th>Limit / stop</th>
              <th>Fees</th>
              <th>Placed</th>
            </tr>
          </thead>
          <tbody>
            {orders.map((order) => (
              <tr key={order.order_id}>
                <td>{order.order_role}</td>
                <td>{order.order_type}</td>
                <td>{order.side}</td>
                <td>
                  {order.status}
                  {order.rejection_reason && (
                    <span className="block text-[11px] text-rose-300">{order.rejection_reason}</span>
                  )}
                </td>
                <td className="numeric">{order.quantity}</td>
                <td className="numeric">{order.filled_quantity}</td>
                <td className="numeric">{order.average_fill_price ?? "—"}</td>
                <td className="numeric text-xs">
                  {order.limit_price ?? "—"} / {order.stop_price ?? "—"}
                </td>
                <td className="numeric text-slate-400">{rupees(order.fee_total)}</td>
                <td className="muted-cell text-xs">{formatIstTimestamp(order.created_at)}</td>
              </tr>
            ))}
            {!orders.length && (
              <tr>
                <td colSpan={10} className="muted-cell">
                  No orders recorded against this position.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <h4 className="mt-7 text-sm font-semibold text-white">Fills and what each one cost</h4>
      <p className="mt-1 text-xs text-slate-500">
        Itemised from the published rate card. When a broker bill disagrees, the line that differs is the answer.
      </p>
      <div className="table-scroll mt-3">
        <table className="terminal-table">
          <thead>
            <tr>
              <th>Time</th>
              <th>Side</th>
              <th>Qty</th>
              <th>Price</th>
              <th>Value</th>
              <th>Slippage</th>
              {CHARGE_LINES.map(([key, label]) => (
                <th key={key}>{label}</th>
              ))}
              <th>Total</th>
            </tr>
          </thead>
          <tbody>
            {fills.map((fill) => (
              <tr key={fill.fill_id}>
                <td className="muted-cell text-xs">{formatIstTimestamp(fill.occurred_at)}</td>
                <td>{fill.side}</td>
                <td className="numeric">{fill.quantity}</td>
                <td className="numeric">{fill.price}</td>
                <td className="numeric text-slate-400">{rupees(fill.gross_value)}</td>
                <td className="numeric text-slate-400">{rupees(fill.slippage_amount)}</td>
                {CHARGE_LINES.map(([key]) => (
                  <td key={key} className="numeric text-slate-400">
                    {rupees(fill[key])}
                  </td>
                ))}
                <td className="numeric text-slate-200">{rupees(fill.total_fees)}</td>
              </tr>
            ))}
            {!fills.length && (
              <tr>
                <td colSpan={7 + CHARGE_LINES.length} className="muted-cell">
                  No fills recorded against this position.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </article>
  );
}

function Figure({ label, node, note }: { label: string; node: React.ReactNode; note: string }) {
  return (
    <div className="glass-inset rounded-md p-4">
      <p className="eyebrow">{label}</p>
      <p className="mt-2 text-xl font-semibold">{node}</p>
      <p className="mt-1 text-xs text-slate-500">{note}</p>
    </div>
  );
}

function Line({ label, value }: { label: string; value: string }) {
  return (
    <div className="glass-inset rounded-md p-3">
      <p className="eyebrow">{label}</p>
      <p className="mt-1 numeric text-slate-200">{value}</p>
    </div>
  );
}
