"use client";

import { useCallback, useEffect, useState } from "react";

import { api, type BrokerChoices } from "../../components/api";

/**
 * Which records the Orders & Positions screen is showing, held above it.
 *
 * The state lives here rather than inside the panel because the control that
 * sets it sits on the workspace tab row, which the shell renders, while the
 * panel it controls is rendered below. One hook, read by both, beats passing a
 * callback up through the shell or keeping two copies that can disagree.
 *
 * `broker` is null for "whichever broker is selected for trading", resolved
 * server-side so this screen never has to guess — and so that looking at a
 * broker can never be confused with routing orders to one.
 */

const SOURCE_KEY = "sidra.orders.source";
const BROKER_KEY = "sidra.orders.broker";

export type Source = "paper" | "broker";

export type OrderSource = {
  source: Source;
  broker: string | null;
  choices: BrokerChoices | null;
  brokerLabel: string;
  setSource: (next: Source) => void;
  setBroker: (next: string | null) => void;
};

export function useOrderSource(active: boolean): OrderSource {
  const [source, setSourceState] = useState<Source>("paper");
  const [broker, setBrokerState] = useState<string | null>(null);
  const [choices, setChoices] = useState<BrokerChoices | null>(null);

  // Read on mount rather than in useState, so the server-rendered markup and
  // the first client render agree.
  useEffect(() => {
    try {
      const storedSource = window.localStorage.getItem(SOURCE_KEY);
      if (storedSource === "broker" || storedSource === "paper") setSourceState(storedSource);
      const storedBroker = window.localStorage.getItem(BROKER_KEY);
      if (storedBroker) setBrokerState(storedBroker);
    } catch {
      /* a browser that refuses storage still gets the default view */
    }
  }, []);

  // Only when the screen is open and the broker source is chosen. The call
  // contacts no broker, but a request made from every other workspace would
  // still be a request nobody asked for.
  useEffect(() => {
    if (!active || source !== "broker" || choices) return;
    void api
      .brokerChoices()
      .then(setChoices)
      .catch(() => setChoices(null));
  }, [active, source, choices]);

  const setSource = useCallback((next: Source) => {
    setSourceState(next);
    try {
      window.localStorage.setItem(SOURCE_KEY, next);
    } catch {
      /* the choice simply will not persist */
    }
  }, []);

  const setBroker = useCallback((next: string | null) => {
    setBrokerState(next);
    try {
      if (next) window.localStorage.setItem(BROKER_KEY, next);
      else window.localStorage.removeItem(BROKER_KEY);
    } catch {
      /* the choice simply will not persist */
    }
  }, []);

  const named = choices?.brokers.find((item) => item.key === broker);
  const live = choices?.brokers.find((item) => item.key === choices.selected);
  const brokerLabel =
    named?.label ?? live?.label ?? (choices && choices.selected !== "NONE" ? choices.selected : "The broker");

  return { source, broker, choices, brokerLabel, setSource, setBroker };
}
