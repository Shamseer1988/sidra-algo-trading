"use client";

import { CalendarClock, History, Layers, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { api, type SwingPosition, type SwingSummary } from "../../components/api";
import { SwingBacktestPanel, SwingDataPanel } from "./swing-tools";

/**
 * The swing desk, on paper.
 *
 * Read-only: the swing worker owns the ledger, and nothing on this screen can
 * buy, sell or plan. Every figure is paper money, and the screen says so at the
 * top of every tab, because a portfolio table looks the same whether the
 * rupees in it are real or not.
 *
 * Prices for open positions are the last stored daily close, with its date --
 * the swing desk is not on the live tick feed.
 */

const rupees = (value: number | null | undefined, digits = 0) =>
  value === null || value === undefined
    ? "—"
    : `${value < 0 ? "−" : ""}₹${Math.abs(value).toLocaleString("en-IN", { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;

/** The plan's reasons in words, not field names. */
function reasonText(key: string, value: number | string): string {
  const number = typeof value === "number" ? value : Number(value);
  switch (key) {
    case "rs_vs_nifty_pct":
      return `${number >= 0 ? "Stronger" : "Weaker"} than NIFTY by ${Math.abs(number)}%`;
    case "atr14":
      return `Daily range about ₹${value}`;
    case "base_range_pct":
      return `20-day base ${value}% wide`;
    case "volume_vs_avg":
      return `Volume ${value}× normal`;
    case "ema20":
      return `20-day average ${value}`;
    case "close":
      return `Closed at ${value}`;
    default:
      return `${key.replaceAll("_", " ")}: ${value}`;
  }
}

const price = (value: number | null | undefined) =>
  value === null || value === undefined ? "—" : value.toLocaleString("en-IN", { maximumFractionDigits: 2 });

const tone = (value: number | null | undefined) =>
  value === null || value === undefined || value === 0 ? "" : value > 0 ? "text-emerald-500" : "text-rose-500";

const STRATEGY_LABELS: Record<string, string> = {
  "swing-trend-pullback-v1": "Trend Pullback",
  "swing-base-breakout-v1": "Base Breakout",
};

const REASON_LABELS: Record<string, string> = {
  "not triggered": "Price never reached the buy level",
  "gapped above trigger": "Opened too far above the buy level",
  "no candle": "No price data that day",
  stop: "Stop hit",
  "stop same day": "Bought and stopped the same day",
  "breakeven stop": "Stopped at entry after taking half",
  trail: "Trailing exit",
  time: "Holding limit reached",
  results: "Sold before results",
};

export function SwingWorkspace({ tab, isAdmin = false, onMessage }: { tab: string; isAdmin?: boolean; onMessage: (message: string) => void }) {
  const [summary, setSummary] = useState<SwingSummary | null>(null);
  const [history, setHistory] = useState<SwingPosition[] | null>(null);

  const load = useCallback(async () => {
    try {
      const [next, past] = await Promise.all([api.swingSummary(), api.swingHistory()]);
      setSummary(next);
      setHistory(past);
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Could not load the swing desk");
    }
  }, [onMessage]);

  useEffect(() => {
    void load();
  }, [load]);

  // The tools tabs stand on their own: they work before the desk has traded, or while it is off.
  if (tab === "backtest") return <section className="mt-6"><SwingBacktestPanel isAdmin={isAdmin} onMessage={onMessage} /></section>;
  if (tab === "data") return <section className="mt-6"><SwingDataPanel isAdmin={isAdmin} onMessage={onMessage} /></section>;
  if (!summary) return <p className="mt-6 text-sm text-slate-500">Loading the swing desk…</p>;

  return (
    <section className="mt-6 space-y-5" data-testid="swing-workspace">
      <div className="flex flex-wrap items-center gap-3 rounded-md border border-sky-500/30 bg-sky-950/20 p-3 text-sm text-sky-100">
        <Layers className="h-4 w-4 shrink-0 text-sky-300" />
        <p className="flex-1">
          <b>Paper only.</b> No order from this desk reaches the broker. Figures are simulated on ₹
          {summary.capital.toLocaleString("en-IN")} of paper capital, after charges.
          {summary.last_processed ? ` Last evening processed: ${summary.last_processed}.` : " No evening processed yet."}
          <span className="mt-1 block text-xs opacity-90" data-testid="swing-strategies">
            Running: {summary.strategies.map((name) => STRATEGY_LABELS[name] ?? name).join(", ")}. {summary.status_note}
          </span>
        </p>
        <button onClick={() => void load()} className="secondary-button px-3 py-1.5 text-xs">
          <RefreshCw className="h-3.5 w-3.5" />
          Refresh
        </button>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Metric label="Invested" value={rupees(summary.invested)} note={`of ${rupees(summary.capital)} paper capital`} />
        <Metric
          label="Open P&L"
          value={rupees(summary.unrealised)}
          note="at the last daily close"
          valueClass={tone(summary.unrealised)}
        />
        <Metric
          label="Open risk"
          value={rupees(summary.open_risk)}
          note={`limit ${rupees(summary.heat_cap)} · ${rupees(summary.risk_per_trade)} a trade`}
          meter={summary.heat_cap ? summary.open_risk / summary.heat_cap : 0}
        />
        <Metric
          label="Closed P&L"
          value={rupees(summary.realised)}
          note={`${summary.closed_trades} closed trade${summary.closed_trades === 1 ? "" : "s"}, after charges`}
          valueClass={tone(summary.realised)}
        />
      </div>

      {tab === "plan" ? (
        <PlanTable rows={summary.planned} />
      ) : tab === "history" ? (
        <HistoryTable rows={history ?? []} />
      ) : (
        <PortfolioTable rows={summary.open_positions} limit={summary.max_open_positions} />
      )}
    </section>
  );
}

function Metric({
  label,
  value,
  note,
  valueClass,
  meter,
}: {
  label: string;
  value: string;
  note: string;
  valueClass?: string;
  meter?: number;
}) {
  return (
    <div className="glass-inset rounded-md p-4">
      <p className="eyebrow">{label}</p>
      <p className={`mt-2 numeric text-xl font-semibold ${valueClass || "text-white"}`}>{value}</p>
      {meter !== undefined && (
        <div className="mt-2 h-1.5 overflow-hidden rounded bg-slate-700/40">
          <div className={`h-full ${meter > 0.9 ? "bg-amber-400" : "bg-sky-400"}`} style={{ width: `${Math.min(100, meter * 100)}%` }} />
        </div>
      )}
      <p className="mt-1 text-xs text-slate-500">{note}</p>
    </div>
  );
}

function Empty({ text }: { text: string }) {
  return <p className="px-5 py-8 text-center text-sm text-slate-500">{text}</p>;
}

function Panel({ icon: Icon, eyebrow, title, children }: { icon: typeof Layers; eyebrow: string; title: string; children: React.ReactNode }) {
  return (
    <article className="panel overflow-hidden">
      <div className="flex items-center gap-3 border-b border-slate-200 px-5 py-4 dark:border-slate-800">
        <Icon className="h-5 w-5 text-sky-500 dark:text-sky-300" />
        <div>
          <p className="eyebrow">{eyebrow}</p>
          <h3 className="font-semibold text-slate-900 dark:text-white">{title}</h3>
        </div>
      </div>
      {children}
    </article>
  );
}

function ShareCell({ row }: { row: SwingPosition }) {
  return (
    <td>
      <strong className="block font-mono text-slate-900 dark:text-slate-100">{row.symbol}</strong>
      <span className="text-[11px] opacity-75">{row.sector ?? ""}</span>
    </td>
  );
}

function PortfolioTable({ rows, limit }: { rows: SwingPosition[]; limit: number }) {
  return (
    <Panel icon={Layers} eyebrow={`${rows.length} of ${limit} positions`} title="Open paper positions">
      {rows.length ? (
        <div className="table-scroll">
          <table className="terminal-table" data-testid="swing-portfolio">
            <thead>
              <tr>
                <th>Share</th>
                <th>Strategy</th>
                <th>Qty</th>
                <th>Bought</th>
                <th>Last close</th>
                <th>P&L</th>
                <th>R now</th>
                <th>Stop</th>
                <th>Target</th>
                <th>Held</th>
                <th>Next</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <ShareCell row={row} />
                  <td>{STRATEGY_LABELS[row.strategy] ?? row.strategy}</td>
                  <td className="numeric">{row.quantity}</td>
                  <td className="numeric">
                    {price(row.entry_price)}
                    <span className="block text-[11px] opacity-75">{row.entry_date}</span>
                  </td>
                  <td className="numeric">
                    {price(row.last_close)}
                    <span className="block text-[11px] opacity-75">{row.last_close_date}</span>
                  </td>
                  <td className={`numeric ${tone(row.unrealised)}`}>{rupees(row.unrealised)}</td>
                  <td className={`numeric ${tone(row.r_now)}`}>{row.r_now === null ? "—" : `${row.r_now.toFixed(1)}R`}</td>
                  <td className="numeric">
                    {price(row.stop)}
                    {row.partial_done && <span className="block text-[11px] text-emerald-500">half sold, stop at entry</span>}
                  </td>
                  <td className="numeric">{row.partial_done ? "taken" : price(row.target)}</td>
                  <td className="numeric">
                    {row.holding_days} / {row.max_holding_days} d
                  </td>
                  <td className="text-xs">{row.next_action}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <Empty text="No paper position is open. Plans are made each evening after the close." />
      )}
    </Panel>
  );
}

function PlanTable({ rows }: { rows: SwingPosition[] }) {
  return (
    <Panel icon={CalendarClock} eyebrow="Made after the last close" title="Tomorrow's paper plan">
      {rows.length ? (
        <div className="table-scroll">
          <table className="terminal-table" data-testid="swing-plan">
            <thead>
              <tr>
                <th>Share</th>
                <th>Strategy</th>
                <th>Buy if price reaches</th>
                <th>Stop</th>
                <th>Take half at</th>
                <th>Qty</th>
                <th>Risk</th>
                <th>Why</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <ShareCell row={row} />
                  <td>{STRATEGY_LABELS[row.strategy] ?? row.strategy}</td>
                  <td className="numeric">{price(row.entry_trigger)}</td>
                  <td className="numeric">{price(row.initial_stop)}</td>
                  <td className="numeric">{price(row.target)}</td>
                  <td className="numeric">{row.quantity}</td>
                  <td className="numeric">{rupees(row.risk)}</td>
                  <td className="text-xs">
                    {Object.entries(row.reasons).map(([key, value]) => (
                      <span key={key} className="mr-1 inline-block rounded bg-slate-500/15 px-1.5 py-0.5">
                        {reasonText(key, value)}
                      </span>
                    ))}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="px-5 py-3 text-xs text-slate-500">
            Each plan is valid for the next session only. It is skipped if the share opens more than 2% above the buy
            level.
          </p>
        </div>
      ) : (
        <Empty text="No plan for the next session. Either no setup was found, the limits are full, or NIFTY is below its 50-day average." />
      )}
    </Panel>
  );
}

function HistoryTable({ rows }: { rows: SwingPosition[] }) {
  return (
    <Panel icon={History} eyebrow="Closed and skipped" title="Paper history">
      {rows.length ? (
        <div className="table-scroll">
          <table className="terminal-table" data-testid="swing-history">
            <thead>
              <tr>
                <th>Share</th>
                <th>Strategy</th>
                <th>Outcome</th>
                <th>Bought</th>
                <th>Closed</th>
                <th>Net P&L</th>
                <th>Charges</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <ShareCell row={row} />
                  <td>{STRATEGY_LABELS[row.strategy] ?? row.strategy}</td>
                  <td className="text-xs">
                    {row.status === "CANCELLED" ? "Not bought: " : ""}
                    {REASON_LABELS[row.exit_reason ?? ""] ?? row.exit_reason}
                  </td>
                  <td className="numeric">
                    {price(row.entry_price)}
                    <span className="block text-[11px] opacity-75">{row.entry_date ?? row.plan_date}</span>
                  </td>
                  <td className="numeric">{row.exit_date ?? "—"}</td>
                  <td className={`numeric ${tone(row.net_pnl)}`}>{rupees(row.net_pnl, 2)}</td>
                  <td className="numeric">{rupees(row.charges, 2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <Empty text="Nothing closed yet." />
      )}
    </Panel>
  );
}
