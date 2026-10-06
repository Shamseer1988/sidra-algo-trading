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

// Per screen, because "show me the broker" on Orders and on Reports are two
// different questions an operator answers differently: one is about what is
// open right now, the other about what a month came to.
const SOURCE_KEY = (scope: string) => `sidra.${scope}.source`;
const BROKER_KEY = (scope: string) => `sidra.${scope}.broker`;

export type Source = "paper" | "broker";

export type OrderSource = {
  source: Source;
  broker: string | null;
  choices: BrokerChoices | null;
  brokerLabel: string;
  setSource: (next: Source) => void;
  setBroker: (next: string | null) => void;
};

export function useOrderSource(active: boolean, scope = "orders"): OrderSource {
  const [source, setSourceState] = useState<Source>("paper");
  const [broker, setBrokerState] = useState<string | null>(null);
  const [choices, setChoices] = useState<BrokerChoices | null>(null);

  // Read on mount rather than in useState, so the server-rendered markup and
  // the first client render agree.
  useEffect(() => {
    try {
      const storedSource = window.localStorage.getItem(SOURCE_KEY(scope));
      if (storedSource === "broker" || storedSource === "paper") setSourceState(storedSource);
      const storedBroker = window.localStorage.getItem(BROKER_KEY(scope));
      if (storedBroker) setBrokerState(storedBroker);
    } catch {
      /* a browser that refuses storage still gets the default view */
    }
  }, [scope]);

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
      window.localStorage.setItem(SOURCE_KEY(scope), next);
    } catch {
      /* the choice simply will not persist */
    }
  }, [scope]);

  const setBroker = useCallback((next: string | null) => {
    setBrokerState(next);
    try {
      if (next) window.localStorage.setItem(BROKER_KEY(scope), next);
      else window.localStorage.removeItem(BROKER_KEY(scope));
    } catch {
      /* the choice simply will not persist */
    }
  }, [scope]);

  const named = choices?.brokers.find((item) => item.key === broker);
  const live = choices?.brokers.find((item) => item.key === choices.selected);
  const brokerLabel =
    named?.label ?? live?.label ?? (choices && choices.selected !== "NONE" ? choices.selected : "The broker");

  return { source, broker, choices, brokerLabel, setSource, setBroker };
}
