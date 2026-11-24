"use client";

import { useEffect, useMemo, useState } from "react";

import { api, type MarketCandle, type ScannerEvaluation } from "../../components/api";
import { formatPrice, statusTone, titleCase } from "../../lib/formatting";
import { resolveScriptName } from "../../lib/instruments";

const valueAt = (source: Record<string, unknown>, path: string): string | number | undefined =>
  path
    .split(".")
    .reduce<unknown>(
      (value, key) =>
        value && typeof value === "object"
          ? (value as Record<string, unknown>)[key]
          : undefined,
      source,
    ) as string | number | undefined;

const numberText = (value: string | number | undefined, suffix = "") =>
  typeof value === "number" ? `${value.toFixed(2)}${suffix}` : "—";

/** One check in full: the decision, its score, and the prices it would have used. */
export function SetupInspector({ evaluation }: { evaluation: ScannerEvaluation }) {
  const script = (evaluation.script_name || resolveScriptName(evaluation.instrument_token));
  const [candles, setCandles] = useState<MarketCandle[]>([]);

  useEffect(() => {
    void api
      .candles(evaluation.instrument_token, evaluation.session_date)
      .then(setCandles)
      .catch(() => setCandles([]));
  }, [evaluation.instrument_token, evaluation.session_date]);

  const risk =
    evaluation.entry_price !== null && evaluation.stop_price !== null
      ? Math.abs(evaluation.entry_price - evaluation.stop_price)
      : null;
  const reward =
    evaluation.entry_price !== null && evaluation.target_price !== null
      ? Math.abs(evaluation.target_price - evaluation.entry_price)
      : null;

  return (
    <aside>
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="eyebrow">Setup inspector</p>
          <div className="mt-1 flex items-center gap-2">
            <span className="rounded bg-emerald-500/15 px-2.5 py-1 text-sm font-bold text-emerald-600 dark:text-emerald-300 border border-emerald-500/25">
              {script}
            </span>
            <h3 className="text-sm font-mono opacity-80">{evaluation.instrument_token}</h3>
          </div>
          <p className="mt-1 text-xs opacity-70">
            {evaluation.strategy_name} · v{evaluation.strategy_version}
          </p>
        </div>
        <span className={`status-pill ${statusTone(evaluation.status)}`}>
          {titleCase(evaluation.status)}
        </span>
      </div>

      <p className="mt-5 text-sm leading-6 opacity-90">{evaluation.reason}</p>

      <CandleChart candles={candles} evaluation={evaluation} />

      <section className="mt-6">
        <p className="text-[11px] font-bold uppercase tracking-[.15em] text-slate-500 dark:text-slate-400">
          Score explanation
        </p>
        <div className="mt-3 space-y-3">
          {Object.entries(evaluation.score_breakdown).length ? (
            Object.entries(evaluation.score_breakdown).map(([label, score]) => (
              <div key={label}>
                <div className="mb-1 flex justify-between text-xs">
                  <span className="opacity-80">{label.replaceAll("_", " ")}</span>
                  <span className="numeric font-medium">{score}/20</span>
                </div>
                <div className="score-track">
                  <div
                    className="score-fill"
                    style={{ width: `${Math.min(score * 5, 100)}%` }}
                  />
                </div>
              </div>
            ))
          ) : (
            <p className="empty-inset text-xs">
              No score breakdown was produced because the evaluation was blocked before strategy scoring.
            </p>
          )}
        </div>
      </section>

      <InspectorConditions evaluation={evaluation} />

      <dl className="data-list mt-6">
        <div>
          <dt>Entry</dt>
          <dd className="numeric">
            {evaluation.entry_price === null ? "—" : `₹${formatPrice(evaluation.entry_price)}`}
          </dd>
        </div>
        <div>
          <dt>Stop</dt>
          <dd className="numeric">
            {evaluation.stop_price === null ? "—" : `₹${formatPrice(evaluation.stop_price)}`}
          </dd>
        </div>
        <div>
          <dt>Target</dt>
          <dd className="numeric">
            {evaluation.target_price === null ? "—" : `₹${formatPrice(evaluation.target_price)}`}
          </dd>
        </div>
        <div>
          <dt>Quantity</dt>
          <dd className="numeric">{evaluation.quantity ?? "—"}</dd>
        </div>
        <div>
          <dt>Paper risk</dt>
          <dd className="numeric">
            {evaluation.risk_amount === null ? "—" : `₹${formatPrice(evaluation.risk_amount)}`}
          </dd>
        </div>
        <div>
          <dt>Reward : risk</dt>
          <dd className="numeric">{risk && reward ? `${(reward / risk).toFixed(2)}R` : "—"}</dd>
        </div>
      </dl>

      <p className="mt-5 text-xs leading-5 text-slate-500 dark:text-slate-400">
        This is a strategy evaluation record, not a broker instruction or an open position.
      </p>
    </aside>
  );
}

function InspectorConditions({ evaluation }: { evaluation: ScannerEvaluation }) {
  const snapshot = evaluation.indicator_snapshot;
  const conditions = [
    ["Breakout / retest", evaluation.decision_state.replaceAll("_", " ")],
    ["Volume confirmation", `RVOL ${numberText(valueAt(snapshot, "volume.relative_volume"))}`],
    ["VWAP", numberText(valueAt(snapshot, "vwap"))],
    ["EMA", `${numberText(valueAt(snapshot, "ema_fast"))} / ${numberText(valueAt(snapshot, "ema_slow"))}`],
    ["Market confirmation", String(valueAt(snapshot, "nifty_regime.regime") ?? "—")],
    [
      "Relative strength",
      numberText(valueAt(snapshot, "relative_strength.relative_strength_percent"), "%"),
    ],
  ];

  return (
    <section className="mt-6">
      <p className="text-[11px] font-bold uppercase tracking-[.15em] text-slate-500 dark:text-slate-400">
        Conditions
      </p>
      <div className="data-list mt-3">
        {conditions.map(([label, value]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </div>
      {evaluation.failed_conditions.length > 0 && (
        <div className="mt-4 rounded-md border border-rose-900/40 bg-rose-950/20 p-4">
          <p className="text-[10px] font-bold uppercase tracking-[.15em] text-rose-600 dark:text-rose-300">
            Failed conditions
          </p>
          <ul className="mt-2 space-y-1 text-xs leading-5 text-rose-700 dark:text-rose-200">
            {evaluation.failed_conditions.map((condition) => (
              <li key={condition}>• {condition}</li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

function CandleChart({
  candles,
  evaluation,
}: {
  candles: MarketCandle[];
  evaluation: ScannerEvaluation;
}) {
  const range = useMemo(
    () => ({
      low: Math.min(
        ...candles.map((item) => item.low),
        evaluation.stop_price ?? evaluation.candle_close,
      ),
      high: Math.max(
        ...candles.map((item) => item.high),
        evaluation.target_price ?? evaluation.candle_close,
      ),
    }),
    [candles, evaluation.candle_close, evaluation.stop_price, evaluation.target_price],
  );

  const width = 320;
  const height = 120;
  const spread = Math.max(range.high - range.low, 0.01);
  const y = (price: number) => height - ((price - range.low) / spread) * height;
  const points = candles
    .map(
      (item, index) =>
        `${(index / Math.max(candles.length - 1, 1)) * width},${y(item.close)}`,
    )
    .join(" ");

  return (
    <section className="mt-6">
      <div className="mb-3 flex justify-between text-[11px] font-semibold uppercase tracking-[.14em] text-slate-500 dark:text-slate-400">
        <span>Completed-candle chart</span>
        <span>{candles.length} candles</span>
      </div>
      {candles.length ? (
        <svg viewBox={`0 0 ${width} ${height}`} className="signal-chart">
          <polyline points={points} fill="none" stroke="var(--foreground)" strokeWidth="2" />
          {evaluation.entry_price !== null && (
            <line
              x1="0"
              x2={width}
              y1={y(evaluation.entry_price)}
              y2={y(evaluation.entry_price)}
              stroke="var(--positive)"
              strokeDasharray="4 3"
            />
          )}
          {evaluation.stop_price !== null && (
            <line
              x1="0"
              x2={width}
              y1={y(evaluation.stop_price)}
              y2={y(evaluation.stop_price)}
              stroke="var(--negative)"
              strokeDasharray="4 3"
            />
          )}
        </svg>
      ) : (
        <div className="empty-inset text-xs">
          No completed candles are retained for this evaluation’s session.
        </div>
      )}
    </section>
  );
}
