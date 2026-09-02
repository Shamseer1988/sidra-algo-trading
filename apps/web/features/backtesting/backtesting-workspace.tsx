"use client";

import { FlaskConical, Play, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { api, type BacktestRun, type PaperStrategy } from "../../components/api";
import { formatIstTimestamp, formatPrice } from "../../lib/formatting";

function isoDate(offsetDays: number) {
  const value = new Date();
  value.setDate(value.getDate() + offsetDays);
  return value.toISOString().slice(0, 10);
}

export function BacktestingWorkspace({ isAdmin, onMessage }: { isAdmin: boolean; onMessage: (message: string) => void }) {
  const [runs, setRuns] = useState<BacktestRun[]>([]);
  const [strategies, setStrategies] = useState<PaperStrategy[]>([]);
  const [startDate, setStartDate] = useState(isoDate(-7));
  const [endDate, setEndDate] = useState(isoDate(0));
  const [instruments, setInstruments] = useState("");
  const [running, setRunning] = useState(false);
  const [loading, setLoading] = useState(true);
  const activeStrategies = useMemo(() => strategies.filter((item) => item.enabled), [strategies]);
  const latest = runs[0];

  const load = useCallback(() => {
    setLoading(true);
    void Promise.all([api.backtests(), api.strategies()])
      .then(([nextRuns, nextStrategies]) => { setRuns(nextRuns); setStrategies(nextStrategies); })
      .catch((error: unknown) => onMessage(error instanceof Error ? error.message : "Could not load backtesting research"))
      .finally(() => setLoading(false));
  }, [onMessage]);
  useEffect(() => { load(); }, [load]);

  const run = async () => {
    const instrumentTokens = instruments.split(",").map((item) => item.trim()).filter(Boolean);
    if (!instrumentTokens.length) { onMessage("Enter at least one instrument token to run a backtest."); return; }
    setRunning(true);
    try {
      const created = await api.runBacktest({ start_date: startDate, end_date: endDate, instrument_tokens: instrumentTokens, strategy_ids: activeStrategies.map((item) => item.id), timeframe_seconds: 60 });
      setRuns((current) => [created, ...current.filter((item) => item.id !== created.id)]);
      onMessage(`Backtest completed with ${created.summary.trades} simulated trades.`);
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Backtest could not be completed");
    } finally { setRunning(false); }
  };

  return <section>
    <div className="page-toolbar"><div><p className="eyebrow">Completed-candle research</p><h2 className="page-title">Backtesting lab</h2><p className="page-copy">Historical replay uses only completed candles, fills on the next candle, and applies the same paper cost model. It never contacts a broker.</p></div><button className="secondary-button" onClick={load} disabled={loading || running}><RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />Refresh</button></div>
    <article className="panel mt-5 p-5"><div className="flex items-start gap-3"><div className="grid h-10 w-10 place-items-center rounded-md bg-sky-400/10 text-sky-300"><FlaskConical className="h-5 w-5" /></div><div><p className="font-semibold text-white">Reproducible research run</p><p className="mt-1 text-sm text-slate-400">Each run stores its candle fingerprint, strategy version, controls, and execution assumptions.</p></div></div><div className="mt-5 grid gap-3 sm:grid-cols-2 xl:grid-cols-4"><label className="field-label">Start date<input className="field-input mt-2" type="date" value={startDate} onChange={(event) => setStartDate(event.target.value)} /></label><label className="field-label">End date<input className="field-input mt-2" type="date" value={endDate} onChange={(event) => setEndDate(event.target.value)} /></label><label className="field-label xl:col-span-2">Instruments (comma-separated)<input aria-label="Backtest instruments" className="field-input mt-2" placeholder="NSE:RELIANCE, NSE:INFY" value={instruments} onChange={(event) => setInstruments(event.target.value)} /></label></div><div className="mt-4 flex flex-wrap items-center justify-between gap-3"><p className="text-xs text-slate-500">{activeStrategies.length ? `${activeStrategies.length} enabled strategy configuration${activeStrategies.length > 1 ? "s" : ""} selected` : "No enabled strategy configuration is available"}</p><button className="primary-button" data-testid="run-backtest-btn" onClick={() => void run()} disabled={!isAdmin || running || !activeStrategies.length}><Play className="h-4 w-4" />{running ? "Replaying candles…" : "Run backtest"}</button></div>{!isAdmin && <p className="mt-3 text-xs text-amber-300">Only administrators can create a persisted research run.</p>}</article>
    {latest ? <LatestRun run={latest} /> : <article className="empty-inset mt-6 flex flex-col items-center gap-3 p-10 text-center"><FlaskConical className="h-7 w-7 text-slate-500" /><p className="text-sm text-slate-400">No historical runs yet. Select stored instruments and a date range to create a completed-candle-only research record.</p></article>}
    <Runs rows={runs} loading={loading} />
  </section>;
}

function LatestRun({ run }: { run: BacktestRun }) {
  const summary = run.summary;
  const pnlTone = (run.net_pnl ?? 0) >= 0 ? "text-emerald-300" : "text-rose-300";
  return <><div className="mt-6 grid gap-3 sm:grid-cols-2 xl:grid-cols-4"><Metric label="Net P&amp;L" value={`₹${formatPrice(run.net_pnl ?? 0)}`} tone={pnlTone} /><Metric label="Return" value={`${summary.return_percent}%`} tone={pnlTone} /><Metric label="Max drawdown" value={`₹${formatPrice(run.max_drawdown ?? 0)}`} /><Metric label="Win rate" value={`${summary.win_rate}%`} /></div><article className="panel mt-6 p-5"><div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between"><div><p className="eyebrow">Latest verified replay</p><h3 className="mt-1 font-semibold text-white">{run.start_date} → {run.end_date}</h3><p className="mt-2 text-sm text-slate-400">{summary.trades} trades · {run.source_candle_count.toLocaleString("en-IN")} source candles · fingerprint {run.data_fingerprint.slice(0, 12)}</p></div><EquityCurve points={summary.equity_curve} /></div><div className="mt-5 table-scroll"><table className="terminal-table"><thead><tr><th>Strategy</th><th>Trades</th><th>Win rate</th><th>Net P&amp;L</th><th>Profit factor</th></tr></thead><tbody>{summary.strategy_comparison.map((item) => <tr key={`${item.strategy_id}-${item.strategy_version}`}><td><strong>{item.strategy_name}</strong><span>v{item.strategy_version}</span></td><td className="numeric">{item.trades}</td><td className="numeric">{item.win_rate}%</td><td className={`numeric ${item.net_pnl >= 0 ? "text-emerald-300" : "text-rose-300"}`}>₹{formatPrice(item.net_pnl)}</td><td className="numeric">{item.profit_factor ?? "—"}</td></tr>)}</tbody></table></div></article></>;
}

function EquityCurve({ points }: { points: { equity: number }[] }) {
  const values = points.map((item) => item.equity);
  if (values.length < 2) return <div className="glass-inset rounded-md px-5 py-4 text-sm text-slate-500">Equity curve appears after the first closed trade.</div>;
  const minimum = Math.min(...values); const maximum = Math.max(...values); const span = maximum - minimum || 1;
  const polyline = values.map((value, index) => `${(index / (values.length - 1)) * 200},${72 - ((value - minimum) / span) * 62}`).join(" ");
  return <div className="glass-inset rounded-md p-3"><p className="mb-2 text-xs text-slate-500">Closed-trade equity</p><svg aria-label="Backtest equity curve" viewBox="0 0 200 80" className="h-20 w-52"><polyline fill="none" stroke="currentColor" strokeWidth="2.5" className="text-sky-300" points={polyline} /></svg></div>;
}

function Metric({ label, value, tone = "text-white" }: { label: string; value: string; tone?: string }) { return <article className="glass-inset rounded-md p-4"><p className="eyebrow">{label}</p><p className={`mt-2 numeric text-2xl font-semibold ${tone}`}>{value}</p></article>; }

function Runs({ rows, loading }: { rows: BacktestRun[]; loading: boolean }) { return <article className="panel mt-6 overflow-hidden"><div className="border-b border-slate-800 px-5 py-4"><p className="eyebrow">Research ledger</p><h3 className="font-semibold text-white">Persisted backtest runs</h3></div>{loading ? <div className="space-y-2 p-5">{Array.from({ length: 3 }, (_, index) => <div key={index} className="skeleton h-12" />)}</div> : rows.length ? <div className="table-scroll"><table className="terminal-table"><thead><tr><th>Period</th><th>Instruments</th><th>Trades</th><th>Net P&amp;L</th><th>Drawdown</th><th>Created</th></tr></thead><tbody>{rows.map((run) => <tr key={run.id}><td><strong>{run.start_date} → {run.end_date}</strong><span>{run.status}</span></td><td>{run.instrument_tokens.join(", ")}</td><td className="numeric">{run.summary.trades}</td><td className={`numeric ${(run.net_pnl ?? 0) >= 0 ? "text-emerald-300" : "text-rose-300"}`}>₹{formatPrice(run.net_pnl ?? 0)}</td><td className="numeric">₹{formatPrice(run.max_drawdown ?? 0)}</td><td className="muted-cell">{formatIstTimestamp(run.created_at)}</td></tr>)}</tbody></table></div> : <div className="p-5 text-sm text-slate-500">No stored historical research runs.</div>}</article>; }
