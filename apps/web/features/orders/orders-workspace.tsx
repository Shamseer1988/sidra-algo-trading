"use client";

import { PaperExecutionPanel } from "../paper/paper-execution-panel";
import { BrokerBooksPanel } from "./broker-books-panel";
import { BrokerSelect } from "./source-controls";
import type { OrderSource } from "./use-order-source";

/**
 * One screen, two sources: what this system simulated, and what the broker has.
 *
 * They were never the same thing and the old screen only ever showed the first,
 * which is how a day can end with a position at the broker and a flat paper
 * book on the dashboard. The toggle on the tab row makes the question explicit
 * — whose records am I reading — instead of leaving it to be assumed.
 *
 * Switching source changes what is displayed and nothing else. It does not
 * change which broker a live order would reach: that is the live broker in
 * Settings → Trading controls, and it is said on the screen so the selector
 * here cannot be mistaken for it.
 */
export function OrdersWorkspace({ view, source: state }: { view: "orders" | "positions"; source: OrderSource }) {
  if (state.source !== "broker") return <PaperExecutionPanel view={view} />;

  const live = state.choices?.brokers.find((item) => item.key === state.choices?.selected);
  return (
    <section>
      <div className="mb-5 flex flex-wrap items-center justify-between gap-3">
        <p className="max-w-3xl text-xs text-slate-500 dark:text-slate-400">
          Viewing only. Which broker a live order actually reaches is the live broker in Settings → Trading controls
          {state.choices
            ? ` (currently ${state.choices.selected === "NONE" ? "none" : (live?.label ?? state.choices.selected)})`
            : ""}
          , and changing the selection here does not change it.
        </p>
        <BrokerSelect broker={state.broker} choices={state.choices} setBroker={state.setBroker} />
      </div>
      <BrokerBooksPanel view={view} broker={state.broker} brokerLabel={state.brokerLabel} />
    </section>
  );
}
