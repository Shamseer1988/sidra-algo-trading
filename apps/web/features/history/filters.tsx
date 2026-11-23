"use client";

import { istDate, strategyLabel } from "../../lib/strategy-names";

export type Range = { from: string; to: string };

export const lastDays = (count: number): Range => ({ from: istDate(count - 1), to: istDate() });

const PRESETS: { label: string; range: () => Range }[] = [
  { label: "Today", range: () => lastDays(1) },
  { label: "7 days", range: () => lastDays(7) },
  { label: "30 days", range: () => lastDays(30) },
  { label: "This month", range: () => ({ from: `${istDate().slice(0, 8)}01`, to: istDate() }) },
];

/** The period, as one tap or as two dates. */
export function RangeFilter({ range, onChange }: { range: Range; onChange: (next: Range) => void }) {
  return (
    <div className="flex flex-wrap items-end gap-2">
      <div role="group" aria-label="Period" className="flex flex-wrap gap-1">
        {PRESETS.map((preset) => {
          const value = preset.range();
          const active = value.from === range.from && value.to === range.to;
          return (
            <button
              key={preset.label}
              type="button"
              aria-pressed={active}
              className={`${active ? "primary-button" : "secondary-button"} px-3 py-2 text-xs`}
              onClick={() => onChange(value)}
            >
              {preset.label}
            </button>
          );
        })}
      </div>
      <label className="text-xs text-slate-500 dark:text-slate-400">
        From
        <input
          type="date"
          value={range.from}
          max={range.to}
          onChange={(event) => event.target.value && onChange({ ...range, from: event.target.value })}
          className="field-input mt-1 block font-mono text-sm"
        />
      </label>
      <label className="text-xs text-slate-500 dark:text-slate-400">
        To
        <input
          type="date"
          value={range.to}
          min={range.from}
          onChange={(event) => event.target.value && onChange({ ...range, to: event.target.value })}
          className="field-input mt-1 block font-mono text-sm"
        />
      </label>
    </div>
  );
}

export function StrategySelect({
  value,
  options,
  onChange,
}: {
  value: string;
  options: string[];
  onChange: (next: string) => void;
}) {
  // A chosen strategy with nothing in the new period stays listed, so the
  // control never shows "All" while a filter is in force.
  const all = value && !options.includes(value) ? [...options, value] : options;
  return (
    <label className="text-xs text-slate-500 dark:text-slate-400">
      Strategy
      <select
        aria-label="Strategy"
        className="field-input mt-1 block py-2 text-sm"
        value={value}
        onChange={(event) => onChange(event.target.value)}
      >
        <option value="">All strategies</option>
        {all.map((name) => (
          <option key={name} value={name}>
            {strategyLabel(name)}
          </option>
        ))}
      </select>
    </label>
  );
}
