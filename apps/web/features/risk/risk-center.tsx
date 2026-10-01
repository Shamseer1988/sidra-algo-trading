"use client";

import { ShieldCheck } from "lucide-react";
import { useEffect, useState } from "react";

import { api, type PaperRiskSummary, type SafetyStatus } from "../../components/api";
import { formatPrice } from "../../lib/formatting";
import { TradingCard } from "../controls/trading-card";
import { LiveExecutionControls } from "../controls/live-controls";

const emptyRisk: PaperRiskSummary = {
  session_date: "",
  daily_risk_limit: 0,
  daily_risk_allocated: 0,
  daily_risk_available: 0,
  maximum_open_positions: 0,
  active_reservations: 0,
  open_positions: 0,
  exposure_limit: 0,
  current_exposure: 0,
  exposure_available: 0,
  leverage_multiplier: 1,
  rejected_reservations: 0,
};

export function RiskCenter({
  safety,
  isAdmin,
  onMessage,
}: {
  safety: SafetyStatus;
  isAdmin: boolean;
  onMessage: (message: string) => void;
}) {
  const [risk, setRisk] = useState<PaperRiskSummary>(emptyRisk);
  useEffect(() => {
    void api
      .paperRiskSummary()
      .then(setRisk)
      .catch(() => setRisk(emptyRisk));
  }, []);

  return (
    <section>
      <div className="page-toolbar">
        <div>
          <p className="eyebrow">Safety controls</p>
          <h2 className="page-title">Risk</h2>
          <p className="page-copy">
            Reservation capacity gates every simulated entry. Daily allocations stay recorded after a paper position
            closes, so a day&rsquo;s budget cannot be spent twice.
          </p>
        </div>
        <ShieldCheck className="h-8 w-8 text-emerald-300" />
      </div>

      <div className="mt-5 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <RiskMetric
          label="Daily allocation"
          value={`₹${formatPrice(risk.daily_risk_allocated)} / ₹${formatPrice(risk.daily_risk_limit)}`}
        />
        <RiskMetric label="Available risk" value={`₹${formatPrice(risk.daily_risk_available)}`} />
        <RiskMetric label="Open capacity" value={`${risk.active_reservations}/${risk.maximum_open_positions}`} />
        <RiskMetric
          label="Exposure available"
          value={`₹${formatPrice(risk.exposure_available)}`}
          note={
            risk.leverage_multiplier > 1
              ? `of ₹${formatPrice(risk.exposure_limit)} at ${risk.leverage_multiplier}x`
              : `of ₹${formatPrice(risk.exposure_limit)}`
          }
        />
      </div>

      {/*
        One card, not three. The emergency stop, the paper-tracking switch and
        the live-execution panel each described themselves correctly and
        together said nothing an operator could act on: which of the seven
        gates is actually in the way right now. TradingCard answers that in one
        sentence, with the decision made on the server where it can be tested.

        The detailed panel stays below it, because "arm with a reason",
        "reconcile now" and the gate-by-gate readout are still the right tools
        once you know what you are looking at -- they were only ever wrong as
        the FIRST thing on the screen.
      */}
      <TradingCard isAdmin={isAdmin} onMessage={onMessage} />

      {/*
        Not collapsed. The first version of this put the panel inside a
        <details>, which hid arming and reconciliation behind a disclosure
        triangle nobody had been told about -- nine end-to-end tests went red
        for exactly the reason an operator would have been stuck: the controls
        were not visible. The goal was to put ONE clear answer above these
        tools, never to hide them.
      */}
      <div className="mt-6">
        <LiveExecutionControls safety={safety} isAdmin={isAdmin} onMessage={onMessage} />
      </div>
    </section>
  );
}

function RiskMetric({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <article className="glass-inset rounded-md p-4">
      <p className="eyebrow">{label}</p>
      <p className="mt-2 numeric text-xl font-semibold text-white">{value}</p>
      {note && <p className="mt-1 text-xs text-slate-500">{note}</p>}
    </article>
  );
}
