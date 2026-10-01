"use client";

import { useEffect, useState } from "react";

import { api, type BrokerChoices } from "../../components/api";
import { PaperExecutionPanel } from "../paper/paper-execution-panel";
import { BrokerBooksPanel } from "./broker-books-panel";

/**
 * One screen, two sources: what this system simulated, and what the broker has.
 *
 * They were never the same thing and the old screen only ever showed the first,
 * which is how an operator ended a day with a position on the broker and a flat
 * paper book on the dashboard. The toggle makes the question explicit —
 * "whose records am I reading" — instead of leaving it to be assumed.
 *
 * Switching source changes what is *displayed* and nothing else. It does not
 * change which broker a live order would reach: that is the live broker in
 * Settings → Trading controls, and it is said on the screen so the selector
 * here cannot be mistaken for it.
 */

const SOURCE_KEY = "sidra.orders.source";
const BROKER_KEY = "sidra.orders.broker";

type Source = "paper" | "broker";

export function OrdersWorkspace({ view }: { view: "orders" | "positions" }) {
  const [source, setSource] = useState<Source>("paper");
  // null means "whichever broker is selected for trading", resolved server-side
  // so this screen never has to guess.
  const [broker, setBroker] = useState<string | null>(null);
  const [choices, setChoices] = useState<BrokerChoices | null>(null);

  // Read once on mount rather than in useState, so the server-rendered markup
  // and the first client render agree.
  useEffect(() => {
    try {
      const storedSource = window.localStorage.getItem(SOURCE_KEY);
      if (storedSource === "broker" || storedSource === "paper") setSource(storedSource);
      const storedBroker = window.localStorage.getItem(BROKER_KEY);
      if (storedBroker) setBroker(storedBroker);
    } catch {
      /* a browser that refuses storage still gets the default view */
    }
  }, []);

  useEffect(() => {
    if (source !== "broker" || choices) return;
    void api
      .brokerChoices()
      .then(setChoices)
      .catch(() => setChoices(null));
  }, [source, choices]);

  function chooseSource(next: Source) {
    setSource(next);
    try {
      window.localStorage.setItem(SOURCE_KEY, next);
    } catch {
      /* the choice simply will not persist */
    }
  }

  function chooseBroker(next: string | null) {
    setBroker(next);
    try {
      if (next) window.localStorage.setItem(BROKER_KEY, next);
      else window.localStorage.removeItem(BROKER_KEY);
    } catch {
      /* the choice simply will not persist */
    }
  }

  const selected = choices?.brokers.find((item) => item.key === broker);
  const live = choices?.brokers.find((item) => item.key === choices.selected);
  const brokerLabel = selected?.label ?? live?.label ?? (choices?.selected && choices.selected !== "NONE" ? choices.selected : "The broker");

  return (
    <section>
      <div className="mb-5 flex flex-wrap items-center gap-2">
        <div className="flex gap-2" role="group" aria-label="Record source">
          <button
            type="button"
            aria-pressed={source === "paper"}
            onClick={() => chooseSource("paper")}
            className={source === "paper" ? "primary-button" : "secondary-button"}
          >
            Paper
          </button>
          <button
            type="button"
            aria-pressed={source === "broker"}
            onClick={() => chooseSource("broker")}
            className={source === "broker" ? "primary-button" : "secondary-button"}
          >
            Broker
          </button>
        </div>

        {source === "broker" && (
          <label className="text-xs text-slate-500 dark:text-slate-400">
            <span className="sr-only">Broker to view</span>
            <select
              className="field-input py-2 text-sm"
              value={broker ?? ""}
              onChange={(event) => chooseBroker(event.target.value || null)}
            >
              <option value="">
                {choices && choices.selected !== "NONE"
                  ? `Live broker (${live?.label ?? choices.selected})`
                  : "Live broker"}
              </option>
              {(choices?.brokers ?? []).map((item) => (
                <option key={item.key} value={item.key} disabled={!item.connected}>
                  {item.label}
                  {item.connected ? "" : " — not connected"}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>

      {source === "broker" && (
        <p className="mb-4 text-xs text-slate-500 dark:text-slate-400">
          Viewing only. Which broker a live order actually reaches is the live broker in Settings → Trading controls
          {choices ? ` (currently ${choices.selected === "NONE" ? "none" : (live?.label ?? choices.selected)})` : ""}, and
          changing the selection here does not change it.
        </p>
      )}

      {source === "broker" ? (
        <BrokerBooksPanel view={view} broker={broker} brokerLabel={brokerLabel} />
      ) : (
        <PaperExecutionPanel view={view} />
      )}
    </section>
  );
}
