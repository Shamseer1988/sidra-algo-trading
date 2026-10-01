"use client";

import type { OrderSource } from "./use-order-source";

/**
 * Paper or Broker, on the tab row where the other view choices are.
 *
 * A segmented control rather than two buttons the size of actions: it chooses
 * what is displayed, like the tabs beside it, and sizing it like Refresh or
 * Square off would put a view switch in the visual language this terminal uses
 * for things that do something.
 */
export function SourceToggle({ source, setSource }: Pick<OrderSource, "source" | "setSource">) {
  return (
    <div
      role="group"
      aria-label="Record source"
      className="inline-flex rounded-md border p-0.5"
      style={{ borderColor: "var(--glass-border-strong)", background: "var(--glass-muted)" }}
    >
      {([
        ["paper", "Paper"],
        ["broker", "Broker"],
      ] as const).map(([value, label]) => (
        <button
          key={value}
          type="button"
          aria-pressed={source === value}
          onClick={() => setSource(value)}
          className={`rounded px-3 py-1.5 text-xs font-semibold transition ${
            source === value
              ? "bg-emerald-500/85 text-[#03130e] shadow-[inset_0_1px_0_rgba(255,255,255,.35)]"
              : "text-slate-500 hover:text-slate-800 dark:text-slate-400 dark:hover:text-slate-200"
          }`}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

/**
 * Which broker's books to read. Not which broker trades — that is in Settings,
 * and the line beside this control says so, because a dropdown next to live
 * order rows that looked like it routed orders would be the most expensive
 * ambiguity on the screen.
 */
export function BrokerSelect({ broker, choices, setBroker }: Pick<OrderSource, "broker" | "choices" | "setBroker">) {
  const live = choices?.brokers.find((item) => item.key === choices.selected);
  return (
    <label className="text-xs text-slate-500 dark:text-slate-400">
      <span className="sr-only">Broker to view</span>
      <select
        className="field-input py-2 text-sm"
        value={broker ?? ""}
        onChange={(event) => setBroker(event.target.value || null)}
      >
        <option value="">
          {choices && choices.selected !== "NONE" ? `Live broker (${live?.label ?? choices.selected})` : "Live broker"}
        </option>
        {(choices?.brokers ?? []).map((item) => (
          <option key={item.key} value={item.key} disabled={!item.connected}>
            {item.label}
            {item.connected ? "" : " — not connected"}
          </option>
        ))}
      </select>
    </label>
  );
}
