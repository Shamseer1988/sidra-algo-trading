"use client";

import { AlertTriangle, ClipboardList, PlugZap, RefreshCw, WalletCards } from "lucide-react";
import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";

import { api, type BrokerBookOrder, type BrokerBookPosition, type BrokerSnapshot } from "../../components/api";
import { formatIstTimestamp } from "../../lib/formatting";

/**
 * The broker's own order book and position book, on a screen.
 *
 * Three things this panel is careful about.
 *
 * **It is the broker's figures, not ours.** Nothing here is computed in the
 * browser and nothing is reconciled against our records. A row says what the
 * broker said. Where the broker reported no figure the cell says so rather than
 * showing ₹0.00, because a zero nobody claimed is worse than a blank.
 *
 * **Reads cost part of the budget trading draws on.** Upstox allows 2000 reads
 * in any thirty minutes per user, shared with order placement and
 * reconciliation, so this panel polls slowly, stops polling when the tab is in
 * the background, and leans on the six-second server-side cache that every
 * viewer shares. Refresh is the one way past it.
 *
 * **Nothing here can change an order.** The endpoint behind it is read-only --
 * no cancel, no modify, no square-off. Flattening a position is done from Risk,
 * where it is a deliberate act rather than a tap on a table row.
 */

const POLL_MS = 15_000;

export function BrokerBooksPanel({
  view,
  broker,
  brokerLabel,
}: {
  view: "orders" | "positions";
  broker: string | null;
  brokerLabel: string;
}) {
  const [snapshot, setSnapshot] = useState<BrokerSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // A forced read is the operator asking; a poll must never become one.
  const inFlight = useRef(false);

  const load = useCallback(
    async (force: boolean) => {
      if (inFlight.current) return;
      inFlight.current = true;
      if (force) setLoading(true);
      try {
        setSnapshot(await api.brokerSnapshot({ broker: broker ?? undefined, force }));
        setError(null);
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : "Could not read the broker books");
      } finally {
        inFlight.current = false;
        setLoading(false);
      }
    },
    [broker],
  );

  useEffect(() => {
    setSnapshot(null);
    setLoading(true);
    void load(false);
  }, [load]);

  useEffect(() => {
    const timer = setInterval(() => {
      // A tab left open in the background would otherwise spend the same
      // rate-limit budget an order needs, for nobody.
      if (typeof document !== "undefined" && document.hidden) return;
      void load(false);
    }, POLL_MS);
    return () => clearInterval(timer);
  }, [load]);

  const readable = snapshot?.readable ?? false;

  return (
    <section>
      <div className="page-toolbar">
        <div>
          <p className="eyebrow">Live at the broker</p>
          <h2 className="page-title">{view === "orders" ? `${brokerLabel} orderbook` : `${brokerLabel} positions`}</h2>
          <p className="page-copy">
            Exactly what {brokerLabel} reports for this account, including anything placed by hand. Read-only: orders
            cannot be changed or withdrawn from this screen.
          </p>
        </div>
        <div className="flex flex-col items-end gap-1">
          <button className="secondary-button" onClick={() => void load(true)} disabled={loading}>
            <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
            Refresh
          </button>
          {snapshot && (
            <p className="text-[11px] text-slate-500 dark:text-slate-400">
              Read {formatIstTimestamp(snapshot.fetched_at)}
              {snapshot.stale ? " · from the shared cache" : ""}
            </p>
          )}
        </div>
      </div>

      {error && (
        <article className="glass-notice mt-5 rounded-md p-4 text-sm text-rose-600 dark:text-rose-300">{error}</article>
      )}

      {snapshot && !readable && (
        <article className="glass-notice mt-5 flex items-start gap-3 rounded-md p-5">
          <PlugZap className="mt-0.5 h-5 w-5 shrink-0 text-amber-500 dark:text-amber-300" />
          <div>
            <p className="font-semibold text-slate-900 dark:text-white">{brokerLabel} could not be read</p>
            <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">{snapshot.detail}</p>
            <p className="mt-2 text-xs text-slate-500 dark:text-slate-400">
              This says nothing about whether a position is open. It says this screen could not ask. Check the broker
              console for the login, and the broker&apos;s own app for the position.
            </p>
          </div>
        </article>
      )}

      {readable && snapshot && (
        <>
          <div className="mt-5 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <Metric label="Open positions" value={String(snapshot.open_positions)} />
            <Metric label="Working orders" value={String(snapshot.working_orders)} />
            <Metric label="Realised today" value={money(snapshot.realised)} className={tone(snapshot.realised)} />
            <Metric label="Unrealised" value={money(snapshot.unrealised)} className={tone(snapshot.unrealised)} />
          </div>

          {snapshot.untracked_working > 0 && (
            <article className="glass-notice mt-4 flex items-start gap-3 rounded-md p-4">
              <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-amber-500 dark:text-amber-300" />
              <div className="text-sm">
                <p className="font-semibold text-slate-900 dark:text-white">
                  {snapshot.untracked_working} working order {snapshot.untracked_working === 1 ? "is" : "are"} not ours
                </p>
                <p className="mt-1 text-slate-600 dark:text-slate-300">
                  Reconciliation refuses new live orders while an order it cannot account for is working, and that is
                  the correct behaviour — this system cannot size a position it does not know the other half of. The
                  rows marked <strong>Manual</strong> below are the ones in question. Close or withdraw them at the
                  broker, and trading resumes on the next reconciliation.
                </p>
              </div>
            </article>
          )}

          {view === "orders" ? (
            <Orderbook rows={snapshot.orders} brokerLabel={brokerLabel} />
          ) : (
            <Positions rows={snapshot.positions} brokerLabel={brokerLabel} />
          )}
        </>
      )}

      {!snapshot && loading && (
        <div className="mt-5 space-y-2">
          {Array.from({ length: 4 }, (_, index) => (
            <div key={index} className="skeleton h-16" />
          ))}
        </div>
      )}
    </section>
  );
}

/**
 * Rupees, or the fact that the broker did not say.
 *
 * None and 0.00 are different claims and the service behind this keeps them
 * apart deliberately; collapsing them here would undo that.
 */
function money(value: number | null): string {
  if (value === null) return "not reported";
  const text = Math.abs(value).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return value < 0 ? `−₹${text}` : `₹${text}`;
}

function cell(value: number | null): string {
  return value === null ? "—" : money(value);
}

function tone(value: number | null): string {
  if (value === null || value === 0) return "text-slate-500 dark:text-slate-400";
  return value > 0 ? "text-emerald-500 dark:text-emerald-400" : "text-rose-500 dark:text-rose-400";
}

function Metric({ label, value, className = "text-slate-900 dark:text-white" }: { label: string; value: string; className?: string }) {
  return (
    <article className="glass-inset rounded-md p-4">
      <p className="eyebrow">{label}</p>
      <p className={`mt-2 numeric text-2xl font-semibold ${className}`}>{value}</p>
    </article>
  );
}

function PlacedBy({ ours }: { ours: boolean }) {
  return (
    <span className={`status-pill ${ours ? "status-good" : "status-watch"}`}>{ours ? "Sidra" : "Manual"}</span>
  );
}

function statusPill(status: string): string {
  const value = status.toUpperCase();
  if (value === "COMPLETE" || value === "FILLED") return "status-good";
  if (value === "REJECTED" || value === "CANCELLED") return "status-bad";
  return "status-watch";
}

function Orderbook({ rows, brokerLabel }: { rows: BrokerBookOrder[]; brokerLabel: string }) {
  return (
    <article className="panel mt-6 overflow-hidden">
      <div className="flex items-center gap-3 border-b border-slate-200 dark:border-slate-800 px-5 py-4">
        <ClipboardList className="h-5 w-5 text-sky-500 dark:text-sky-300" />
        <div>
          <p className="eyebrow">Ours and placed by hand alike</p>
          <h3 className="font-semibold text-slate-900 dark:text-white">Every order on the account</h3>
        </div>
      </div>
      {rows.length ? (
        <div className="table-scroll">
          <table className="terminal-table">
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Side</th>
                <th>Order</th>
                <th>Filled</th>
                <th>Average price</th>
                <th>Status</th>
                <th>Placed by</th>
                <th>Placed</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.broker_order_id}>
                  <td>
                    <strong className="block font-mono text-slate-900 dark:text-slate-100">{row.symbol}</strong>
                    <span className="text-[11px] opacity-75">{row.broker_order_id}</span>
                  </td>
                  <td>
                    {row.side ? (
                      <span className={`status-pill ${row.side.startsWith("B") ? "status-good" : "status-bad"}`}>
                        {row.side}
                      </span>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td>
                    {row.order_type ?? "—"}
                    {row.quantity !== null && <span className="text-[11px] opacity-75"> · {row.quantity}</span>}
                  </td>
                  <td className="numeric">
                    {row.filled_quantity === null ? "—" : row.filled_quantity}
                    {row.quantity !== null && `/${row.quantity}`}
                  </td>
                  <td className="numeric">{cell(row.average_price)}</td>
                  <td>
                    <span className={`status-pill ${statusPill(row.status)}`}>{row.status}</span>
                  </td>
                  <td>
                    <PlacedBy ours={row.ours} />
                  </td>
                  <td className="muted-cell">{formatIstTimestamp(row.placed_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <Empty
          icon={<ClipboardList className="h-6 w-6" />}
          text={`${brokerLabel} reports no orders on this account today. An order placed by hand would appear here too.`}
        />
      )}
    </article>
  );
}

function Positions({ rows, brokerLabel }: { rows: BrokerBookPosition[]; brokerLabel: string }) {
  return (
    <article className="panel mt-6 overflow-hidden">
      <div className="flex items-center gap-3 border-b border-slate-200 dark:border-slate-800 px-5 py-4">
        <WalletCards className="h-5 w-5 text-violet-500 dark:text-violet-300" />
        <div>
          <p className="eyebrow">As the broker marks it</p>
          <h3 className="font-semibold text-slate-900 dark:text-white">Net position by symbol</h3>
        </div>
      </div>
      {rows.length ? (
        <div className="table-scroll">
          <table className="terminal-table">
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Net quantity</th>
                <th>Average price</th>
                <th>Last price</th>
                <th>Realised</th>
                <th>Unrealised</th>
                <th>Day P&amp;L</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={`${row.symbol}-${row.instrument_token ?? ""}`}>
                  <td>
                    <strong className="block font-mono text-slate-900 dark:text-slate-100">{row.symbol}</strong>
                    {row.instrument_token && row.instrument_token !== row.symbol && (
                      <span className="text-[11px] opacity-75">{row.instrument_token}</span>
                    )}
                  </td>
                  <td className="numeric">
                    {row.net_quantity === null ? (
                      "—"
                    ) : row.net_quantity === 0 ? (
                      <span className="status-pill status-watch">Squared off</span>
                    ) : (
                      row.net_quantity
                    )}
                  </td>
                  <td className="numeric">{cell(row.average_price)}</td>
                  <td className="numeric">{cell(row.last_price)}</td>
                  <td className={`numeric ${tone(row.realised)}`}>{cell(row.realised)}</td>
                  <td className={`numeric ${tone(row.unrealised)}`}>{cell(row.unrealised)}</td>
                  <td className={`numeric ${tone(row.day_pnl)}`}>{cell(row.day_pnl)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <Empty
          icon={<WalletCards className="h-6 w-6" />}
          text={`${brokerLabel} reports no position rows today. A squared-off position would still be listed, so an empty book means nothing was traded on this account.`}
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
