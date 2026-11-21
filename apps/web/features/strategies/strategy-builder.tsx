"use client";

import { FlaskConical, Hammer, Save, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import {
  api,
  type BacktestRunDetail,
  type BuilderCatalog,
  type BuilderInstrument,
  type PaperStrategy,
} from "../../components/api";
import { formatPrice } from "../../lib/formatting";
import { ExitRulesFields } from "./exit-rules-fields";

/**
 * Strategies > Builder.
 *
 * 1. Start from one of the tested strategy types, or from a strategy you have.
 * 2. Set its rules.
 * 3. Backtest the draft on the shares the scanner watches -- nothing is saved.
 * 4. Save it. A new strategy is always saved Off and on paper; it starts
 *    paper trading only when you set it to Paper in the control center, and
 *    goes live only through the control center's own confirmation.
 *
 * The server holds every one of those rules; this screen explains them.
 */

type Source = { kind: "template"; type: string } | { kind: "copy" | "edit"; id: string };

const NUMBERS: { key: keyof PaperStrategy; label: string; help: string; step: string }[] = [
  { key: "minimum_score", label: "Minimum score (0-100)", help: "Higher takes fewer, cleaner setups.", step: "1" },
  { key: "minimum_rr", label: "Minimum reward : risk", help: "2 means the target is twice the stop distance.", step: "0.1" },
  { key: "volume_multiplier", label: "Volume multiplier", help: "Candle volume against the recent average.", step: "0.1" },
  { key: "retest_tolerance_percent", label: "Retest tolerance %", help: "How close price must come back to the level.", step: "0.01" },
  { key: "minimum_ema_spread_percent", label: "Minimum EMA spread %", help: "How far apart the fast and slow EMAs must be.", step: "0.01" },
  { key: "max_trades_per_day", label: "Trades a day (this strategy)", help: "The account's own daily limit still applies.", step: "1" },
  { key: "cooldown_minutes", label: "Cooldown minutes", help: "Wait after a trade before the next one.", step: "1" },
];

function isoDaysAgo(days: number): string {
  return new Date(Date.now() + 5.5 * 3600 * 1000 - days * 86400 * 1000).toISOString().slice(0, 10);
}

function errorText(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

export function StrategyBuilder({
  isAdmin,
  onMessage,
  onOpenControlCenter,
}: {
  isAdmin: boolean;
  onMessage: (message: string) => void;
  onOpenControlCenter: () => void;
}) {
  const [catalog, setCatalog] = useState<BuilderCatalog | null>(null);
  const [instruments, setInstruments] = useState<BuilderInstrument[]>([]);
  const [source, setSource] = useState<Source | null>(null);
  const [draft, setDraft] = useState<PaperStrategy | null>(null);
  const [picked, setPicked] = useState<string[]>([]);
  const [start, setStart] = useState(isoDaysAgo(28));
  const [end, setEnd] = useState(isoDaysAgo(1));
  const [run, setRun] = useState<BacktestRunDetail | null>(null);
  const [running, setRunning] = useState(false);
  const [reason, setReason] = useState("");

  const load = () =>
    void api
      .builderCatalog()
      .then(setCatalog)
      .catch((error: unknown) => onMessage(errorText(error, "Could not load the builder")));

  useEffect(load, []); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    void api
      .builderInstruments()
      .then(setInstruments)
      .catch(() => setInstruments([]));
  }, []);

  const editing = source?.kind === "edit" ? catalog?.strategies.find((item) => item.id === source.id) : undefined;
  const template = useMemo(
    () => catalog?.templates.find((item) => item.strategy_type === draft?.strategy_type),
    [catalog, draft?.strategy_type],
  );

  function begin(next: Source) {
    if (!catalog) return;
    setSource(next);
    setRun(null);
    if (next.kind === "template") {
      const found = catalog.templates.find((item) => item.strategy_type === next.type);
      if (found) setDraft({ ...found.defaults });
    } else {
      const found = catalog.strategies.find((item) => item.id === next.id);
      if (!found) return;
      setDraft(
        next.kind === "copy"
          ? { ...found.configuration, id: "new", name: `${found.name} (copy)`.slice(0, 80), enabled: false, live_orders: false, version: 1 }
          : { ...found.configuration },
      );
    }
  }

  const set = <K extends keyof PaperStrategy>(key: K, value: PaperStrategy[K]) =>
    setDraft((current) => (current ? { ...current, [key]: value } : current));

  function toggleSide(side: string) {
    if (!draft) return;
    const has = draft.allowed_sides.includes(side);
    set("allowed_sides", has ? draft.allowed_sides.filter((item) => item !== side) : [...draft.allowed_sides, side]);
  }

  function togglePicked(token: string) {
    setPicked((current) =>
      current.includes(token) ? current.filter((item) => item !== token) : current.length >= 10 ? current : [...current, token],
    );
  }

  async function backtest() {
    if (!draft) return;
    if (!picked.length) return onMessage("Pick at least one share to test on.");
    setRunning(true);
    setRun(null);
    try {
      setRun(await api.backtestDraft({ draft, start_date: start, end_date: end, instrument_tokens: picked }));
    } catch (error) {
      onMessage(errorText(error, "The backtest failed"));
    } finally {
      setRunning(false);
    }
  }

  async function save() {
    if (!draft) return;
    try {
      if (editing) {
        await api.saveStrategy(editing.id, draft, reason);
        onMessage(`Saved the rules of ${draft.name}. Its mode (${editing.mode.toLowerCase()}) is unchanged.`);
      } else {
        await api.saveNewStrategy(draft, reason);
        onMessage(`Saved ${draft.name}, switched off. Set it to Paper in the control center to start paper trading it.`);
      }
      setSource(null);
      setDraft(null);
      setRun(null);
      setReason("");
      load();
    } catch (error) {
      onMessage(errorText(error, "Could not save the strategy"));
    }
  }

  async function remove(id: string, name: string) {
    if (!window.confirm(`Delete ${name}? Its past signals, trades and backtests are kept.`)) return;
    try {
      await api.deleteStrategy(id);
      onMessage(`Deleted ${name}.`);
      load();
    } catch (error) {
      onMessage(errorText(error, "Could not delete the strategy"));
    }
  }

  if (!isAdmin) {
    return (
      <article className="panel mt-6 p-5 text-sm text-slate-400">
        Only an administrator can build or change strategies. The control center shows what each one is doing.
      </article>
    );
  }

  const summary = run?.summary;

  return (
    <section className="mt-6 space-y-5" data-testid="strategy-builder">
      <article className="panel p-5 sm:p-7">
        <div className="flex items-center gap-3 border-b border-slate-800 pb-4">
          <Hammer className="h-5 w-5 text-emerald-400" />
          <div>
            <p className="eyebrow">Step 1 · Start from</p>
            <h3 className="font-semibold text-white">A tested strategy type, or one you already have</h3>
            <p className="mt-1 text-xs text-slate-400">
              New strategies are always saved <strong>Off</strong> and paper-only. Nothing here can place a real order.
            </p>
          </div>
        </div>
        <div className="mt-4 grid gap-2 sm:grid-cols-2 xl:grid-cols-3" role="radiogroup" aria-label="Strategy type">
          {catalog?.templates.map((item) => {
            const chosen = source?.kind === "template" && source.type === item.strategy_type;
            return (
              <button
                key={item.strategy_type}
                role="radio"
                aria-checked={chosen}
                onClick={() => begin({ kind: "template", type: item.strategy_type })}
                className={`rounded-md border p-3 text-left ${chosen ? "border-emerald-500/60 bg-emerald-950/20" : "border-slate-800 hover:border-slate-600"}`}
              >
                <span className="block text-sm font-semibold text-white">{item.name}</span>
                <span className="mt-1 block text-xs leading-5 text-slate-400">{item.about}</span>
              </button>
            );
          })}
        </div>
        {catalog && catalog.strategies.length > 0 && (
          <div className="mt-5 overflow-x-auto border-t border-slate-800 pt-4">
            <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">Your strategies</p>
            <table className="w-full text-sm">
              <tbody>
                {catalog.strategies.map((item) => (
                  <tr key={item.id} className="border-t border-slate-800/60" data-testid={`existing-${item.id}`}>
                    <td className="py-2 pr-3 text-white">
                      {item.name} <span className="text-xs text-slate-500">v{item.version}</span>
                    </td>
                    <td className="py-2 pr-3">
                      <span className={`status-pill ${item.mode === "LIVE" ? "status-bad" : item.mode === "PAPER" ? "status-good" : "status-watch"}`}>
                        {item.mode === "LIVE" ? "Live" : item.mode === "PAPER" ? "Paper" : "Off"}
                      </span>
                    </td>
                    <td className="whitespace-nowrap py-2 text-right">
                      <button className="secondary-button px-2 py-1 text-xs" onClick={() => begin({ kind: "copy", id: item.id })}>
                        Copy
                      </button>{" "}
                      <button
                        className="secondary-button px-2 py-1 text-xs"
                        disabled={item.mode === "LIVE"}
                        title={item.mode === "LIVE" ? "A live strategy is not edited here. Set it to Paper first." : undefined}
                        onClick={() => begin({ kind: "edit", id: item.id })}
                      >
                        Edit
                      </button>{" "}
                      <button
                        className="secondary-button px-2 py-1 text-xs"
                        disabled={item.mode !== "OFF"}
                        title={item.mode !== "OFF" ? "Only a strategy that is Off can be deleted." : undefined}
                        aria-label={`Delete ${item.name}`}
                        onClick={() => void remove(item.id, item.name)}
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </article>

      {draft && (
        <article className="panel p-5 sm:p-7" data-testid="builder-rules">
          <p className="eyebrow">Step 2 · Rules{editing ? ` · editing ${editing.name}` : ""}</p>
          <h3 className="font-semibold text-white">{template?.name ?? draft.strategy_type}</h3>
          {template && <p className="mt-1 text-xs text-slate-400">{template.about}</p>}
          <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <label className="field-label sm:col-span-2">
              Name
              <input className="field-input mt-1" value={draft.name} maxLength={80} onChange={(event) => set("name", event.target.value)} />
            </label>
            <fieldset className="field-label">
              Sides
              <div className="mt-2 flex gap-4 text-sm text-slate-300">
                {["LONG", "SHORT"].map((side) => (
                  <label key={side} className="flex items-center gap-2">
                    <input type="checkbox" checked={draft.allowed_sides.includes(side)} onChange={() => toggleSide(side)} />
                    {side === "LONG" ? "Buy (long)" : "Sell (short)"}
                  </label>
                ))}
              </div>
            </fieldset>
            <label className="field-label">
              Risk per trade % (blank = account)
              <input
                className="field-input mt-1"
                type="number"
                min="0.01"
                max="5"
                step="0.01"
                value={draft.risk_per_trade_percent ?? ""}
                onChange={(event) => set("risk_per_trade_percent", event.target.value === "" ? null : Number(event.target.value))}
              />
            </label>
            {NUMBERS.map((field) => (
              <label key={field.key} className="field-label" title={field.help}>
                {field.label}
                <input
                  className="field-input mt-1"
                  type="number"
                  step={field.step}
                  value={draft[field.key] as number}
                  onChange={(event) => set(field.key, Number(event.target.value) as never)}
                />
                <span className="mt-1 block text-[11px] font-normal normal-case tracking-normal text-slate-500">{field.help}</span>
              </label>
            ))}
            {draft.strategy_type === "rs-pullback-v1" && (
              <label className="field-label">
                RS threshold % (blank = built-in)
                <input
                  className="field-input mt-1"
                  type="number"
                  min="0"
                  max="10"
                  step="0.05"
                  value={draft.rs_threshold_percent ?? ""}
                  onChange={(event) => set("rs_threshold_percent", event.target.value === "" ? null : Number(event.target.value))}
                />
              </label>
            )}
          </div>
          <ExitRulesFields rules={draft.exit_rules} disabled={false} onChange={(next) => set("exit_rules", next)} />
        </article>
      )}

      {draft && (
        <article className="panel p-5 sm:p-7" data-testid="builder-test">
          <div className="flex items-center gap-3">
            <FlaskConical className="h-5 w-5 text-sky-300" />
            <div>
              <p className="eyebrow">Step 3 · Test</p>
              <h3 className="font-semibold text-white">Backtest this draft, without saving it</h3>
              <p className="mt-1 text-xs text-slate-400">
                Replays the stored 1-minute candles with today&apos;s charges and account limits. Up to 10 shares and 32 days a run.
                A few weeks is an anecdote, not proof.
              </p>
            </div>
          </div>
          <div className="mt-4 flex flex-wrap items-end gap-3">
            <label className="field-label">
              From
              <input aria-label="Backtest from" className="field-input mt-1" type="date" value={start} onChange={(event) => setStart(event.target.value)} />
            </label>
            <label className="field-label">
              To
              <input aria-label="Backtest to" className="field-input mt-1" type="date" value={end} onChange={(event) => setEnd(event.target.value)} />
            </label>
            <span className="text-xs text-slate-500">{picked.length}/10 shares picked</span>
          </div>
          <div className="mt-3 flex max-h-48 flex-wrap gap-2 overflow-y-auto" aria-label="Shares to test on">
            {instruments.map((item) => (
              <label
                key={item.instrument_token}
                className={`cursor-pointer rounded border px-2 py-1 text-xs ${picked.includes(item.instrument_token) ? "border-emerald-500/60 text-emerald-300" : "border-slate-700 text-slate-400"}`}
              >
                <input
                  type="checkbox"
                  className="sr-only"
                  checked={picked.includes(item.instrument_token)}
                  onChange={() => togglePicked(item.instrument_token)}
                />
                {item.symbol}
              </label>
            ))}
            {!instruments.length && <span className="text-xs text-slate-500">No watchlist shares found.</span>}
          </div>
          <button className="primary-button mt-4" disabled={running || !picked.length} onClick={() => void backtest()}>
            <FlaskConical className="h-4 w-4" />
            {running ? "Replaying candles…" : "Run backtest"}
          </button>
          {summary && (
            <div className="mt-4 grid gap-3 sm:grid-cols-5" data-testid="builder-result">
              {[
                ["Trades", String(summary.trades)],
                ["Win rate", `${summary.win_rate}%`],
                ["Net P&L after charges", `₹${formatPrice(summary.net_pnl)}`],
                ["Profit factor", summary.profit_factor == null ? "—" : String(summary.profit_factor)],
                ["Worst drawdown", `₹${formatPrice(summary.max_drawdown)}`],
              ].map(([label, value]) => (
                <div key={label} className="glass-inset rounded-md p-3">
                  <p className="text-xs text-slate-500">{label}</p>
                  <p className="numeric mt-1 text-lg font-semibold text-white">{value}</p>
                </div>
              ))}
            </div>
          )}
        </article>
      )}

      {draft && (
        <article className="panel p-5 sm:p-7">
          <p className="eyebrow">Step 4 · Save</p>
          <h3 className="font-semibold text-white">{editing ? "Save these rules" : "Save as a new strategy (Off, paper only)"}</h3>
          <div className="mt-3 flex flex-wrap items-end gap-3">
            <label className="field-label min-w-[16rem] flex-1">
              Why (kept in the change history)
              <input className="field-input mt-1" value={reason} maxLength={300} onChange={(event) => setReason(event.target.value)} />
            </label>
            <button className="primary-button" onClick={() => void save()}>
              <Save className="h-4 w-4" />
              {editing ? "Save changes" : "Save strategy"}
            </button>
            <button className="secondary-button" onClick={onOpenControlCenter}>
              Open control center
            </button>
          </div>
        </article>
      )}
    </section>
  );
}
