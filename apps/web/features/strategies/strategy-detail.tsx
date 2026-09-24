"use client";

import { AlertTriangle, ArrowLeft, Clock, FlaskConical, History, Target } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { api, type StrategyDetail, type StrategyEvidence } from "../../components/api";
import { formatIstTimestamp } from "../../lib/formatting";
import { Money, rupees } from "../history/money";

/**
 * One strategy, in the order somebody asks about it.
 *
 * What is it for, when does it work, what does it need, where does it get out,
 * how often may it trade, what has changed, what has it signalled — and last,
 * because it is the question that gets answered too confidently, is it any
 * good.
 *
 * That last panel is the reason this screen is worth building carefully. Its
 * strongest available verdict is "promising, not proven", and it reports what
 * is missing rather than a number that looks like an answer. A win rate over
 * eleven trades is not a win rate; it is a rumour with a decimal point.
 */

const VERDICT_TONE: Record<StrategyDetail["verdict"], string> = {
  NOT_ENOUGH_EVIDENCE: "border-slate-700 bg-slate-800/60 text-slate-300",
  NEGATIVE: "border-rose-500/40 bg-rose-950/20 text-rose-300",
  INCONCLUSIVE: "border-amber-500/30 bg-amber-950/20 text-amber-100",
  PROMISING: "border-emerald-500/30 bg-emerald-500/10 text-emerald-300",
};

export function StrategyDetailView({
  strategyId,
  onBack,
  onMessage,
}: {
  strategyId: string;
  onBack: () => void;
  onMessage: (message: string) => void;
}) {
  const [detail, setDetail] = useState<StrategyDetail | null>(null);

  const load = useCallback(async () => {
    try {
      setDetail(await api.strategyDetail(strategyId));
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Could not load the strategy");
      onBack();
    }
  }, [strategyId, onBack, onMessage]);

  useEffect(() => {
    void load();
  }, [load]);

  if (!detail) return <p className="mt-6 text-sm text-slate-500">Loading strategy…</p>;
  const { configuration: config } = detail;

  return (
    <section>
      <div className="page-toolbar">
        <div>
          <button onClick={onBack} className="mb-3 inline-flex items-center gap-1.5 text-xs text-slate-400 hover:text-slate-200">
            <ArrowLeft className="h-3.5 w-3.5" />
            All strategies
          </button>
          <p className="eyebrow">
            {detail.strategy_name} · version {config.version} · {config.enabled ? "enabled" : "paused"}
          </p>
          <h2 className="page-title">{config.name}</h2>
          <p className="page-copy">{detail.purpose}</p>
        </div>
      </div>

      <Verdict detail={detail} />

      <div className="mt-6 grid gap-4 xl:grid-cols-2">
        <article className="panel p-5 sm:p-7">
          <p className="eyebrow">When it works</p>
          <h3 className="mt-1 text-base font-semibold text-white">Regime</h3>
          <p className="mt-3 text-sm leading-6 text-slate-400">{detail.regime}</p>
          <p className="mt-5 eyebrow">Entry</p>
          <p className="mt-2 text-sm leading-6 text-slate-400">{detail.entry}</p>
          <p className="mt-5 eyebrow">What it does not do</p>
          {/* Stated explicitly because most disappointment with a strategy is a
              disagreement about what it was ever supposed to do. */}
          <p className="mt-2 text-sm leading-6 text-slate-400">{detail.does_not}</p>
        </article>

        <article className="panel p-5 sm:p-7">
          <div className="flex items-center gap-2">
            <Target className="h-4 w-4 text-emerald-300" />
            <p className="eyebrow">How the trade is left</p>
          </div>
          <h3 className="mt-1 text-base font-semibold text-white">Exit plan</h3>
          <ul className="mt-4 space-y-3">
            {detail.exit_plan.map((line) => (
              <li key={line} className="flex gap-2 text-sm leading-6 text-slate-400">
                <span className="mt-2 h-1 w-1 shrink-0 rounded-full bg-slate-600" />
                <span>{line}</span>
              </li>
            ))}
          </ul>
        </article>

        <article className="panel p-5 sm:p-7">
          <p className="eyebrow">What it needs</p>
          <h3 className="mt-1 text-base font-semibold text-white">Required inputs</h3>
          <p className="mt-2 text-xs leading-5 text-slate-500">
            A required input that is missing blocks the signal outright. It is not scored lower — a setup without
            its confirmation is a different setup, not a weaker one.
          </p>
          <div className="mt-4 flex flex-wrap gap-2">
            {detail.required_inputs.map((input) => (
              <span key={input} className="rounded border border-emerald-500/30 bg-emerald-500/10 px-2 py-0.5 text-[11px] text-emerald-300">
                {input.replaceAll("_", " ")}
              </span>
            ))}
            {!detail.required_inputs.length && <span className="text-sm text-slate-500">None declared.</span>}
          </div>
          {detail.prerequisites.length > 0 && (
            <>
              <p className="mt-5 eyebrow">Also reads</p>
              <p className="mt-2 text-sm leading-6 text-slate-400">{detail.prerequisites.join(" · ")}</p>
            </>
          )}
        </article>

        <article className="panel p-5 sm:p-7">
          <div className="flex items-center gap-2">
            <Clock className="h-4 w-4 text-sky-300" />
            <p className="eyebrow">How often it may trade</p>
          </div>
          <h3 className="mt-1 text-base font-semibold text-white">Limits</h3>
          <dl className="data-list mt-4">
            <div>
              <dt>Trades a day</dt>
              <dd>{String(detail.limits.max_trades_per_day)}</dd>
            </div>
            <div>
              <dt>Trades a side</dt>
              <dd>{detail.limits.max_trades_per_side === null ? "no cap" : String(detail.limits.max_trades_per_side)}</dd>
            </div>
            <div>
              <dt>Cooldown</dt>
              <dd>{String(detail.limits.cooldown_minutes)} min</dd>
            </div>
            <div>
              <dt>Sides</dt>
              <dd>{(detail.limits.allowed_sides as string[]).join(", ") || "none"}</dd>
            </div>
            <div>
              <dt>Universe</dt>
              <dd>{Number(detail.limits.universe_size) || "scanner universe"}</dd>
            </div>
            <div>
              <dt>Minimum score</dt>
              <dd>{String(detail.limits.minimum_score)} / 100</dd>
            </div>
          </dl>
          <p className="mt-4 text-xs text-slate-500">
            {detail.signals_last_30_days} signal{detail.signals_last_30_days === 1 ? "" : "s"} in the last 30 days
            {detail.last_signal_on ? `, most recently on ${detail.last_signal_on}` : " — none at all"}.
          </p>
        </article>
      </div>

      <VersionHistory detail={detail} />
      <RecentSignals detail={detail} />
    </section>
  );
}

function Verdict({ detail }: { detail: StrategyDetail }) {
  return (
    <article className={`mt-5 rounded-md border p-5 ${VERDICT_TONE[detail.verdict]}`}>
      <div className="flex flex-wrap items-center gap-3">
        <FlaskConical className="h-4 w-4 shrink-0" />
        <p className="text-sm font-semibold">{detail.verdict_label}</p>
      </div>
      <p className="mt-2 text-sm leading-6">{detail.verdict_headline}</p>
      {detail.verdict_caveats.length > 0 && (
        <ul className="mt-4 space-y-2">
          {detail.verdict_caveats.map((caveat) => (
            <li key={caveat} className="flex gap-2 text-xs leading-5 opacity-90">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span>{caveat}</span>
            </li>
          ))}
        </ul>
      )}

      <div className="mt-5 grid gap-3 sm:grid-cols-2">
        <EvidenceCard title="Backtest" evidence={detail.backtest} />
        <EvidenceCard title="Paper forward" evidence={detail.forward} />
      </div>
    </article>
  );
}

function EvidenceCard({ title, evidence }: { title: string; evidence: StrategyEvidence }) {
  return (
    <div className="glass-inset rounded-md p-4">
      <div className="flex items-baseline justify-between gap-2">
        <p className="eyebrow">{title}</p>
        {!evidence.out_of_sample && evidence.trades > 0 && (
          <span className="text-[10px] font-semibold uppercase tracking-wide text-amber-300">in-sample</span>
        )}
      </div>
      {evidence.trades === 0 ? (
        <p className="mt-2 text-sm text-slate-500">No resolved trades.</p>
      ) : (
        <>
          <p className="mt-2 text-xl font-semibold">
            <Money value={evidence.net_pnl} signed />
          </p>
          <p className="mt-1 text-xs text-slate-500">
            {evidence.trades} trade{evidence.trades === 1 ? "" : "s"} · {evidence.wins}W/{evidence.losses}L
            {evidence.win_rate_percent ? ` · ${evidence.win_rate_percent}%` : ""}
            {evidence.average_r ? ` · ${evidence.average_r}R average` : ""}
          </p>
          <p className="mt-1 text-xs text-slate-500">
            gross {rupees(evidence.gross_pnl)} less {rupees(evidence.charges)} charges
            {evidence.from_date ? ` · ${evidence.from_date} to ${evidence.to_date}` : ""}
          </p>
        </>
      )}
      {evidence.shortfall > 0 && (
        <p className="mt-2 text-xs text-slate-400">{evidence.shortfall} more resolved trades needed.</p>
      )}
    </div>
  );
}

function VersionHistory({ detail }: { detail: StrategyDetail }) {
  return (
    <article className="panel mt-6 p-5 sm:p-7">
      <div className="flex items-center gap-2">
        <History className="h-4 w-4 text-slate-400" />
        <p className="eyebrow">What changed, and when</p>
      </div>
      <h3 className="mt-1 text-base font-semibold text-white">Version history</h3>
      {detail.version_history.length ? (
        <div className="table-scroll mt-4">
          <table className="terminal-table">
            <thead>
              <tr>
                <th>Saved</th>
                <th>Version</th>
                <th>Changed</th>
                <th>Loosened</th>
              </tr>
            </thead>
            <tbody>
              {detail.version_history.map((change) => (
                <tr key={`${change.at}-${change.version}`}>
                  <td className="muted-cell text-xs">{formatIstTimestamp(change.at)}</td>
                  <td className="numeric">v{change.version}</td>
                  <td className="text-xs">{change.changed_keys.join(", ")}</td>
                  <td className="text-xs text-amber-300">{change.risk_increased.join(", ") || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="mt-4 text-sm text-slate-500">
          No saved changes recorded yet. History starts from the first save made after versioning was added, so a
          strategy that has not been edited since then shows nothing here.
        </p>
      )}
    </article>
  );
}

function RecentSignals({ detail }: { detail: StrategyDetail }) {
  return (
    <article className="panel mt-6 p-5 sm:p-7">
      <p className="eyebrow">What it has been finding</p>
      <h3 className="mt-1 text-base font-semibold text-white">Recent signals</h3>
      {detail.recent_signals.length ? (
        <div className="table-scroll mt-4">
          <table className="terminal-table">
            <thead>
              <tr>
                <th>Session</th>
                <th>Instrument</th>
                <th>Side</th>
                <th>Score</th>
                <th>Entry → stop / target</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {detail.recent_signals.map((signal) => (
                <tr key={signal.id}>
                  <td>
                    <strong className="block text-slate-100">{signal.session_date}</strong>
                    <span className="text-[11px] text-slate-500">{formatIstTimestamp(signal.created_at)}</span>
                  </td>
                  <td className="muted-cell text-xs">{signal.instrument_token}</td>
                  <td>{signal.side}</td>
                  <td className="numeric">{signal.score}</td>
                  <td className="numeric text-xs">
                    {signal.entry_price} → {signal.stop_price} / {signal.target_price}
                  </td>
                  <td className="text-xs">{signal.status.replaceAll("_", " ").toLowerCase()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="mt-4 text-sm text-slate-500">
          This version has produced no signals. A strategy that is enabled and silent is a different situation from
          one that is switched off — check the required inputs above and the scanner&rsquo;s data quality.
        </p>
      )}
    </article>
  );
}
