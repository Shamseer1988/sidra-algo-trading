"use client";

import { Plus, SlidersHorizontal } from "lucide-react";
import { useEffect, useState } from "react";

import { api, type PaperStrategy, type StrategyMetric } from "../../components/api";
import { StrategyControlCenter } from "./control-center";
import { StrategyDetailView } from "./strategy-detail";


/** One strategy's acceptance over the sessions behind it.
 *
 * The count the percentage is taken over is shown beside it, because an
 * accepted signal is rare on purpose -- a handful a day against thousands of
 * evaluations -- so the rate is a fraction of a percent even when everything
 * is working. Read alone, "0%" looks like a fault; read as "0 of 11,482
 * accepted across 5 sessions" it is the scanner refusing setups, which is
 * its job.
 */
function StrategyMetricCard({ metric }: { metric: StrategyMetric }) {
  const covered = metric.sessions === 1
    ? metric.last_session ?? "one session"
    : `${metric.sessions} sessions${metric.first_session ? ` from ${metric.first_session}` : ""}`;
  return (
    <article className="glass-inset rounded-md p-4">
      <p className="truncate text-xs font-semibold text-white">{metric.strategy_name} · v{metric.strategy_version}</p>
      <p className="mt-3 numeric text-2xl font-semibold text-emerald-300">
        {metric.accepted}
        <span className="ml-1 text-sm font-normal text-slate-400">of {metric.evaluations.toLocaleString()} accepted</span>
      </p>
      <p className="mt-1 text-xs text-slate-500">
        {metric.acceptance_rate}% · {metric.watching.toLocaleString()} watching · {metric.rejected.toLocaleString()} rejected
      </p>
      <p className="mt-1 text-xs text-slate-600">{covered}</p>
    </article>
  );
}

export function StrategiesPanel({
  isAdmin,
  onMessage,
  onOpenBuilder,
}: {
  isAdmin: boolean;
  onMessage: (message: string) => void;
  onOpenBuilder?: () => void;
}) {
  const [items, setItems] = useState<PaperStrategy[]>([]);
  const [metrics, setMetrics] = useState<StrategyMetric[]>([]);
  const [loading, setLoading] = useState(true);
  const [opened, setOpened] = useState<string | null>(null);

  useEffect(() => {
    void api.strategies()
      .then(setItems)
      .catch((error: unknown) => onMessage(error instanceof Error ? error.message : "Could not load strategies"))
      .finally(() => setLoading(false));
  }, [onMessage]);

  useEffect(() => {
    void api.strategyMetrics().then(setMetrics).catch(() => setMetrics([]));
  }, []);

  if (opened) return <StrategyDetailView strategyId={opened} onBack={() => setOpened(null)} onMessage={onMessage} />;

  return (
    <section>
      <div className="page-toolbar">
        <div>
          <p className="eyebrow">Intraday and swing</p>
          <h2 className="page-title">Strategies</h2>
          <p className="page-copy">Every strategy in one table. Set what each one does (off, on paper, or live with real money), and use Edit to change its rules in a popup. Each change is confirmed first and recorded in the history; saving rules never changes a strategy&apos;s mode.</p>
        </div>
        {isAdmin && onOpenBuilder && <div className="flex gap-2"><button className="primary-button" onClick={onOpenBuilder}><Plus className="h-4 w-4" />New strategy</button></div>}
      </div>

      {loading ? <StrategySkeleton /> : <div className="mt-5"><StrategyControlCenter strategies={items} isAdmin={isAdmin} onIntradayChanged={setItems} onMessage={onMessage} onDetails={setOpened} /></div>}

      {metrics.length > 0 && <div className="mt-5 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">{metrics.map((metric) => <StrategyMetricCard key={`${metric.strategy_id}-${metric.strategy_version}`} metric={metric} />)}</div>}

      {!loading && !items.length && <article className="empty-inset mt-6 text-center"><SlidersHorizontal className="mx-auto h-6 w-6 text-slate-500" /><p className="mt-3 text-sm text-slate-400">No intraday strategies yet. Make one with New strategy.</p></article>}
    </section>
  );
}

function StrategySkeleton() {
  return <div className="panel p-5"><div className="skeleton h-10 max-w-md" /><div className="mt-5 grid gap-3 sm:grid-cols-2 xl:grid-cols-5">{Array.from({ length: 5 }, (_, index) => <div key={index} className="skeleton h-20" />)}</div></div>;
}
