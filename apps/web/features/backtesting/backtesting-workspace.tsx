"use client";

import { FlaskConical, Play, RefreshCw, Sparkles } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { api, type BacktestRun, type BacktestSweep, type PaperStrategy } from "../../components/api";
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
    <ParameterSweep isAdmin={isAdmin} strategies={strategies} onMessage={onMessage} />
    <Runs rows={runs} loading={loading} />
  </section>;
}

const SWEEPABLE_PARAMS = [
  "minimum_score", "minimum_rr", "volume_multiplier", "retest_tolerance_percent", "minimum_ema_spread_percent",
  "rs_threshold_percent", "max_trades_per_day", "cooldown_minutes", "stop_atr_multiple", "min_stop_distance_percent",
  "risk_per_trade_percent", "opening_range_minutes", "ema_fast_period", "ema_slow_period", "atr_period",
];

function parseGrid(text: string): { grid: Record<string, number[]>; error: string | null } {
  const grid: Record<string, number[]> = {};
  for (const rawLine of text.split("\n")) {
    const line = rawLine.trim();
    if (!line) continue;
    const [key, rest] = line.split(":", 2);
    const name = key?.trim();
    if (!name || rest === undefined) return { grid, error: `Line "${line}" must look like "minimum_rr: 1, 1.5, 2"` };
    if (!SWEEPABLE_PARAMS.includes(name)) return { grid, error: `"${name}" is not a sweepable parameter` };
    const values = rest.split(",").map((v) => Number(v.trim())).filter((v) => !Number.isNaN(v));
    if (!values.length) return { grid, error: `"${name}" has no numeric values` };
    grid[name] = values;
  }
  if (!Object.keys(grid).length) return { grid, error: "Add at least one parameter line" };
  return { grid, error: null };
}

function ParameterSweep({ isAdmin, strategies, onMessage }: { isAdmin: boolean; strategies: PaperStrategy[]; onMessage: (message: string) => void }) {
  const [sweeps, setSweeps] = useState<BacktestSweep[]>([]);
  const [strategyId, setStrategyId] = useState("");
  const [startDate, setStartDate] = useState(isoDate(-21));
  const [endDate, setEndDate] = useState(isoDate(0));
  const [instruments, setInstruments] = useState("");
  const [validationFraction, setValidationFraction] = useState(0.35);
  const [gridText, setGridText] = useState("minimum_rr: 1.5, 2, 2.5\nvolume_multiplier: 1.2, 1.5, 2");
  const [running, setRunning] = useState(false);

  useEffect(() => {
    void api.sweeps().then(setSweeps).catch(() => setSweeps([]));
  }, []);
  useEffect(() => {
    if (!strategyId && strategies.length) setStrategyId(strategies[0].id);
  }, [strategies, strategyId]);

  const latest = sweeps[0];

  const run = async () => {
    const tokens = instruments.split(",").map((v) => v.trim()).filter(Boolean);
    if (!strategyId) { onMessage("Choose a base strategy for the sweep."); return; }
    if (!tokens.length) { onMessage("Enter at least one instrument token."); return; }
    const { grid, error } = parseGrid(gridText);
    if (error) { onMessage(error); return; }
    setRunning(true);
    try {
      const created = await api.createSweep({ strategy_id: strategyId, start_date: startDate, end_date: endDate, instrument_tokens: tokens, validation_fraction: validationFraction, parameter_grid: grid });
      setSweeps((current) => [created, ...current.filter((item) => item.id !== created.id)]);
      onMessage(`Sweep evaluated ${created.combination_count} parameter combinations.`);
    } catch (e) {
      onMessage(e instanceof Error ? e.message : "The parameter sweep could not be completed");
    } finally { setRunning(false); }
  };

  const promote = async (index: number) => {
    if (!latest) return;
    try {
      await api.promoteSweepCombination(latest.id, index);
      setSweeps((current) => current.map((item) => item.id === latest.id ? { ...item, promoted_index: index } : item));
      onMessage(`Promoted combination #${index} into the strategy configuration.`);
    } catch (e) {
      onMessage(e instanceof Error ? e.message : "Could not promote this combination");
    }
  };

  return (
    <article className="panel mt-6 p-5">
      <div className="flex items-start gap-3">
        <div className="grid h-10 w-10 place-items-center rounded-md bg-fuchsia-400/10 text-fuchsia-300"><Sparkles className="h-5 w-5" /></div>
        <div>
          <p className="font-semibold text-white">Parameter sweep</p>
          <p className="mt-1 text-sm text-slate-400">Each grid combination is backtested once; trades are split into an earlier in-sample block and a later validation block. Combinations are ranked by validation return so an in-sample-only fit ranks low.</p>
        </div>
      </div>
      <div className="mt-5 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <label className="field-label">Base strategy<select className="field-input mt-2" value={strategyId} onChange={(e) => setStrategyId(e.target.value)}>{strategies.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}</select></label>
        <label className="field-label">Start date<input className="field-input mt-2" type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)} /></label>
        <label className="field-label">End date<input className="field-input mt-2" type="date" value={endDate} onChange={(e) => setEndDate(e.target.value)} /></label>
        <label className="field-label">Validation fraction<input className="field-input mt-2" type="number" min="0.1" max="0.6" step="0.05" value={validationFraction} onChange={(e) => setValidationFraction(Number(e.target.value))} /></label>
        <label className="field-label xl:col-span-2">Instruments (comma-separated)<input className="field-input mt-2" placeholder="NSE:RELIANCE, NSE:INFY" value={instruments} onChange={(e) => setInstruments(e.target.value)} /></label>
        <label className="field-label xl:col-span-2">Parameter grid (one per line)<textarea className="field-input mt-2 font-mono text-xs" rows={3} value={gridText} onChange={(e) => setGridText(e.target.value)} /></label>
      </div>
      <div className="mt-4 flex justify-end">
        <button className="primary-button" disabled={!isAdmin || running} onClick={() => void run()}><Play className="h-4 w-4" />{running ? "Sweeping…" : "Run sweep"}</button>
      </div>
      {latest && <SweepResults sweep={latest} isAdmin={isAdmin} onPromote={promote} />}
    </article>
  );
}

function SweepResults({ sweep, isAdmin, onPromote }: { sweep: BacktestSweep; isAdmin: boolean; onPromote: (index: number) => void }) {
  const ordered = [...sweep.combinations].sort((a, b) => b.validation.return_percent - a.validation.return_percent);
  return (
    <div className="mt-6 table-scroll">
      <table className="terminal-table">
        <thead>
          <tr><th>#</th><th>Parameters</th><th>In-sample</th><th>Validation</th><th></th></tr>
        </thead>
        <tbody>
          {ordered.map((combo) => (
            <tr key={combo.index} className={combo.index === sweep.best_index ? "table-selected" : ""}>
              <td className="numeric">{combo.index}{combo.index === sweep.best_index ? " ★" : ""}{combo.index === sweep.promoted_index ? " ✓" : ""}</td>
              <td className="text-xs font-mono">{Object.entries(combo.parameters).map(([k, v]) => `${k}=${v}`).join(", ")}</td>
              <td className="numeric text-xs">{combo.in_sample.return_percent}% · {combo.in_sample.trades}t · {combo.in_sample.win_rate_percent}%</td>
              <td className={`numeric text-xs ${combo.validation.return_percent > 0 ? "text-emerald-300" : combo.validation.return_percent < 0 ? "text-rose-300" : ""}`}>
                {combo.validation.return_percent}% · {combo.validation.trades}t · {combo.validation.win_rate_percent}%{combo.proven ? "" : " (unproven)"}
              </td>
              <td>{isAdmin && <button className="secondary-button h-7 py-0 text-xs" onClick={() => onPromote(combo.index)}>Promote</button>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
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
