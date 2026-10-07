"use client";

import type { ExitRules } from "../../components/api";

/**
 * The exit rules, editable.
 *
 * Without this block the fields exist in the API and nowhere else, which is the
 * exact situation the settings work was meant to end: no trading or strategy
 * setting should require editing Python, JSON in the database, or .env.
 *
 * Every blank here means "inherit", not "zero", and the labels say so. A blank
 * ATR multiple follows the account control; typing the same number in freezes
 * it against later account changes, and those are different choices that look
 * identical on screen unless the form explains them.
 */

const TRAILING_LABELS: Record<ExitRules["trailing_rule"], string> = {
  NONE: "None — the stop stays where it was placed",
  BREAKEVEN_AT_R: "Move to entry once ahead",
  ATR_TRAIL: "Follow price at an ATR distance (paper only)",
};

const TARGET_LABELS: Record<ExitRules["target_rule"], string> = {
  RR_MULTIPLE: "A multiple of the risk taken",
  ATR_MULTIPLE: "A multiple of ATR from entry",
};

export function ExitRulesFields({
  rules,
  disabled,
  onChange,
}: {
  rules: ExitRules;
  disabled: boolean;
  onChange: (next: ExitRules) => void;
}) {
  const set = <K extends keyof ExitRules>(key: K, value: ExitRules[K]) => onChange({ ...rules, [key]: value });
  const number = (value: string): number | null => (value === "" ? null : Number(value));

  return (
    <div className="mt-4 rounded-md border border-slate-800 p-4">
      <p className="eyebrow">How the trade is left</p>

      <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <label className="field-label">
          Stop: ATR multiple (blank = account)
          <input
            disabled={disabled}
            className="field-input mt-2"
            type="number"
            step="0.1"
            min="0.1"
            max="10"
            value={rules.stop_atr_multiple ?? ""}
            onChange={(event) => set("stop_atr_multiple", number(event.target.value))}
          />
        </label>
        <label className="field-label">
          Stop: minimum % (blank = account)
          <input
            disabled={disabled}
            className="field-input mt-2"
            type="number"
            step="0.05"
            min="0"
            max="10"
            value={rules.min_stop_distance_percent ?? ""}
            onChange={(event) => set("min_stop_distance_percent", number(event.target.value))}
          />
        </label>
        <label className="field-label">
          Target rule
          <select
            disabled={disabled}
            className="field-input mt-2"
            value={rules.target_rule}
            onChange={(event) => set("target_rule", event.target.value as ExitRules["target_rule"])}
          >
            {Object.entries(TARGET_LABELS).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label className="field-label">
          {rules.target_rule === "ATR_MULTIPLE" ? "Target: ATR multiple" : "Target: reward:risk (blank = minimum)"}
          <input
            disabled={disabled}
            className="field-input mt-2"
            type="number"
            step="0.1"
            value={rules.target_rule === "ATR_MULTIPLE" ? rules.target_atr_multiple : (rules.target_rr ?? "")}
            onChange={(event) =>
              rules.target_rule === "ATR_MULTIPLE"
                ? set("target_atr_multiple", Number(event.target.value))
                : set("target_rr", number(event.target.value))
            }
          />
        </label>
      </div>

      <div className="mt-3 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <label className="field-label">
          Trailing
          <select
            disabled={disabled}
            className="field-input mt-2"
            value={rules.trailing_rule}
            onChange={(event) => set("trailing_rule", event.target.value as ExitRules["trailing_rule"])}
          >
            {Object.entries(TRAILING_LABELS).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        {rules.trailing_rule === "BREAKEVEN_AT_R" && (
          <label className="field-label">
            Move at (R ahead)
            <input
              disabled={disabled}
              className="field-input mt-2"
              type="number"
              step="0.1"
              min="0.1"
              max="10"
              value={rules.trailing_trigger_r}
              onChange={(event) => set("trailing_trigger_r", Number(event.target.value))}
            />
          </label>
        )}
        {rules.trailing_rule === "BREAKEVEN_AT_R" && (
          <p className="field-label text-[11px] font-normal opacity-75 sm:col-span-2">
            Applied live: once the trade is this far ahead, the stop resting at the broker is moved to the price
            actually paid. Break-even on price, not on money — both legs are charged either way.
          </p>
        )}
        {rules.trailing_rule === "ATR_TRAIL" && (
          <label className="field-label">
            Trail distance (× ATR)
            <input
              disabled={disabled}
              className="field-input mt-2"
              type="number"
              step="0.1"
              min="0.1"
              max="10"
              value={rules.trailing_atr_multiple}
              onChange={(event) => set("trailing_atr_multiple", Number(event.target.value))}
            />
          </label>
        )}
        {rules.trailing_rule === "ATR_TRAIL" && (
          /* Said here, where the rule is chosen, because this is where the trap
             is set: the journal would show trades exiting at the trailed level
             while the broker's stop had never moved. */
          <p className="field-label text-[11px] font-normal text-amber-600 dark:text-amber-300 sm:col-span-2">
            Not applied live — paper and backtesting only. Moving a stop at the broker means cancelling the resting
            one and placing another, and a rule that moves the level most minutes of the day would leave the position
            unprotected for the length of a round trip each time. Live trades on this strategy keep the stop they were
            given.
          </p>
        )}
        <label className="field-label">
          Close after (minutes, blank = never)
          <input
            disabled={disabled}
            className="field-input mt-2"
            type="number"
            min="1"
            max="390"
            value={rules.time_exit_minutes ?? ""}
            onChange={(event) => set("time_exit_minutes", number(event.target.value))}
          />
        </label>
        <label className="field-label">
          Square off at (IST, blank = never)
          <input
            disabled={disabled}
            className="field-input mt-2"
            type="time"
            value={rules.square_off_time ?? ""}
            onChange={(event) => set("square_off_time", event.target.value === "" ? null : event.target.value)}
          />
        </label>
      </div>

      {rules.time_exit_minutes === null && rules.square_off_time === null && (
        // Worth saying on the form rather than only on the detail page: this is
        // the default, and it is the one default that does not match how an
        // intraday account actually behaves at the close.
        <p className="mt-3 text-xs leading-5 text-amber-300">
          Nothing closes this strategy&rsquo;s positions when the session ends. Live, the broker would square off an
          intraday position at its own time and price, which the journal would then not match.
        </p>
      )}
    </div>
  );
}
