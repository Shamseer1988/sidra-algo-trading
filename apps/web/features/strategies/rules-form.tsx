"use client";

import type { BuilderInstrument, PaperStrategy } from "../../components/api";
import { ExitRulesFields } from "./exit-rules-fields";

/**
 * A strategy's rules, one form, used by the builder and by the edit popup in
 * the control center, so the two can never offer different fields.
 */

const NUMBERS: { key: keyof PaperStrategy; label: string; help: string; step: string }[] = [
  { key: "minimum_score", label: "Minimum score (0-100)", help: "Higher takes fewer, cleaner setups.", step: "1" },
  { key: "minimum_rr", label: "Minimum reward : risk", help: "2 means the target is twice the stop distance.", step: "0.1" },
  { key: "volume_multiplier", label: "Volume multiplier", help: "Candle volume against the recent average.", step: "0.1" },
  { key: "retest_tolerance_percent", label: "Retest tolerance %", help: "How close price must come back to the level.", step: "0.01" },
  { key: "minimum_ema_spread_percent", label: "Minimum EMA spread %", help: "How far apart the fast and slow EMAs must be.", step: "0.01" },
  { key: "max_trades_per_day", label: "Trades a day (this strategy)", help: "The account's own daily limit still applies.", step: "1" },
  { key: "cooldown_minutes", label: "Cooldown minutes", help: "Wait after a trade before the next one.", step: "1" },
];

export function StrategyRulesForm({
  draft,
  onChange,
  instruments,
  disabled = false,
}: {
  draft: PaperStrategy;
  onChange: (next: PaperStrategy) => void;
  instruments?: BuilderInstrument[];
  disabled?: boolean;
}) {
  const set = <K extends keyof PaperStrategy>(key: K, value: PaperStrategy[K]) => onChange({ ...draft, [key]: value });
  const optional = (value: string): number | null => (value === "" ? null : Number(value));

  function toggleSide(side: string) {
    const has = draft.allowed_sides.includes(side);
    set("allowed_sides", has ? draft.allowed_sides.filter((item) => item !== side) : [...draft.allowed_sides, side]);
  }

  function toggleShare(token: string) {
    const has = draft.universe.includes(token);
    set("universe", has ? draft.universe.filter((item) => item !== token) : [...draft.universe, token]);
  }

  const named = new Map((instruments ?? []).map((item) => [item.instrument_token, item.symbol]));
  // Shares in the strategy's own list that the watchlist no longer has stay visible, so they can be removed.
  const shares = [
    ...(instruments ?? []),
    ...draft.universe.filter((token) => !named.has(token)).map((token) => ({ instrument_token: token, symbol: token })),
  ];

  return (
    <div data-testid="rules-form">
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <label className="field-label sm:col-span-2">
          Name
          <input disabled={disabled} className="field-input mt-1" value={draft.name} maxLength={80} onChange={(event) => set("name", event.target.value)} />
        </label>
        <fieldset className="field-label">
          Sides
          <div className="mt-2 flex gap-4 text-sm text-slate-300">
            {["LONG", "SHORT"].map((side) => (
              <label key={side} className="flex items-center gap-2">
                <input disabled={disabled} type="checkbox" checked={draft.allowed_sides.includes(side)} onChange={() => toggleSide(side)} />
                {side === "LONG" ? "Buy (long)" : "Sell (short)"}
              </label>
            ))}
          </div>
        </fieldset>
        <label className="field-label">
          Risk per trade % (blank = account)
          <input
            disabled={disabled}
            className="field-input mt-1"
            type="number"
            min="0.01"
            max="5"
            step="0.01"
            value={draft.risk_per_trade_percent ?? ""}
            onChange={(event) => set("risk_per_trade_percent", optional(event.target.value))}
          />
        </label>
        {NUMBERS.map((field) => (
          <label key={field.key} className="field-label" title={field.help}>
            {field.label}
            <input
              disabled={disabled}
              className="field-input mt-1"
              type="number"
              step={field.step}
              value={draft[field.key] as number}
              onChange={(event) => set(field.key, Number(event.target.value) as never)}
            />
            <span className="mt-1 block text-[11px] font-normal normal-case tracking-normal text-slate-500">{field.help}</span>
          </label>
        ))}
        <label className="field-label">
          Trades a side (blank = no cap)
          <input
            disabled={disabled}
            className="field-input mt-1"
            type="number"
            min="1"
            max="20"
            value={draft.max_trades_per_side ?? ""}
            onChange={(event) => set("max_trades_per_side", optional(event.target.value))}
          />
        </label>
        {draft.strategy_type === "rs-pullback-v1" && (
          <label className="field-label">
            RS threshold % (blank = built-in)
            <input
              disabled={disabled}
              className="field-input mt-1"
              type="number"
              min="0"
              max="10"
              step="0.05"
              value={draft.rs_threshold_percent ?? ""}
              onChange={(event) => set("rs_threshold_percent", optional(event.target.value))}
            />
          </label>
        )}
      </div>

      {instruments && (
        <div className="mt-4">
          <p className="field-label">
            Shares this strategy trades{" "}
            <span className="font-normal normal-case tracking-normal text-slate-500">
              ({draft.universe.length ? `${draft.universe.length} picked` : "none picked = the whole watchlist"})
            </span>
          </p>
          <div className="mt-2 flex max-h-40 flex-wrap gap-2 overflow-y-auto" aria-label="Shares this strategy trades">
            {shares.map((item) => {
              const on = draft.universe.includes(item.instrument_token);
              return (
                <label
                  key={item.instrument_token}
                  className={`cursor-pointer rounded border px-2 py-1 text-xs ${on ? "border-emerald-500/60 text-emerald-300" : "border-slate-700 text-slate-400"}`}
                >
                  <input disabled={disabled} type="checkbox" className="sr-only" checked={on} onChange={() => toggleShare(item.instrument_token)} />
                  {item.symbol}
                </label>
              );
            })}
          </div>
        </div>
      )}

      <ExitRulesFields rules={draft.exit_rules} disabled={disabled} onChange={(next) => set("exit_rules", next)} />
    </div>
  );
}
