"use client";

import { Ban, CheckCircle2, Database, FlaskConical, Lock, Play, RefreshCw, ShieldAlert, XCircle } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import {
  api,
  type NewExperiment,
  type PaperStrategy,
  type ResearchDataset,
  type ResearchExperiment,
  type ResearchHypothesis,
  type ResearchJob,
  type ResearchStat,
  type ResearchWorkerStatus,
} from "../../components/api";
import { formatIstTimestamp, statusTone, titleCase } from "../../lib/formatting";

/**
 * The Research Lab: datasets, pre-registered experiments, and the jobs that run them.
 *
 * Read-mostly by design. Nothing on this screen can change a strategy or a
 * trading setting: a result here is a recommendation, and acting on it is a
 * separate, deliberate change in Strategies > Builder. The holdout can be
 * scored once, and the screen asks for that in place rather than in a dialog.
 */
type Props = { tab: string; isAdmin: boolean; onMessage: (message: string) => void };

function useLoad<T>(load: () => Promise<T>, onMessage: (message: string) => void) {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const refresh = useCallback(() => {
    setLoading(true);
    void load()
      .then(setData)
      .catch((error: unknown) => onMessage(error instanceof Error ? error.message : "Could not load research data"))
      .finally(() => setLoading(false));
  }, [load, onMessage]);
  useEffect(refresh, [refresh]);
  return { data, loading, refresh };
}

type Costs = { x1: number | null; x1_5: number | null; x2: number | null };
type Deflated = { sharpe: number | null; threshold: number | null; dsr: number | null; trials: number };
type Fold = { train: [string, string]; test: [string, string]; chosen_index: number | null; out_of_sample: { trades: number; average_realized_r: number } };
type SelectionSummary = {
  selection_sessions?: number;
  variants_tested?: number;
  dataset?: { name: string };
  walk_forward?: { stitched?: ResearchStat; deflated_sharpe?: Deflated; cost_sensitivity?: Costs; regimes?: Record<string, ResearchStat>; folds?: Fold[] };
};
type HoldoutSummary = {
  verdict?: string;
  verdict_rule?: string;
  holdout?: [string, string];
  holdout_sessions?: number;
  summary?: ResearchStat;
  regimes?: Record<string, ResearchStat>;
  cost_sensitivity?: Costs;
};

const r = (value: number | null | undefined, digits = 3) => (value === null || value === undefined ? "—" : value.toFixed(digits));
const pct = (part: number, whole: number) => (whole ? `${((part / whole) * 100).toFixed(1)}%` : "—");

export function ResearchLab({ tab, isAdmin, onMessage }: Props) {
  if (tab === "datasets") return <Datasets onMessage={onMessage} />;
  if (tab === "jobs") return <Jobs isAdmin={isAdmin} onMessage={onMessage} />;
  if (tab === "experiments") return <Experiments isAdmin={isAdmin} onMessage={onMessage} />;
  return <Overview onMessage={onMessage} />;
}

function Header({ eyebrow, title, copy, onRefresh, loading }: { eyebrow: string; title: string; copy: string; onRefresh: () => void; loading: boolean }) {
  return (
    <div className="page-toolbar">
      <div>
        <p className="eyebrow">{eyebrow}</p>
        <h2 className="page-title">{title}</h2>
        <p className="page-copy">{copy}</p>
      </div>
      <button className="secondary-button" onClick={onRefresh} disabled={loading}>
        <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
        Refresh
      </button>
    </div>
  );
}

// --- overview --------------------------------------------------------------------------

function Overview({ onMessage }: { onMessage: (message: string) => void }) {
  const load = useCallback(
    () => Promise.all([api.researchWorker(), api.researchDatasets(), api.researchExperiments()]),
    [],
  );
  const { data, loading, refresh } = useLoad(load, onMessage);
  const [worker, datasets, experiments] = data ?? [null, [], []];
  const scored = (experiments as ResearchExperiment[]).filter((item) => item.verdict);
  return (
    <section>
      <Header eyebrow="Evidence before money" title="Research Lab" copy="Historical datasets, pre-registered experiments and the jobs that run them. Research runs in its own worker, outside market hours, and cannot trade." onRefresh={refresh} loading={loading} />
      <div className="mt-6 grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <WorkerCard worker={worker as ResearchWorkerStatus | null} />
        <Stat label="Datasets" value={String((datasets as ResearchDataset[]).length)} detail={`${(datasets as ResearchDataset[]).filter((item) => item.status === "COMPLETE").length} finalised`} />
        <Stat label="Experiments" value={String((experiments as ResearchExperiment[]).length)} detail={`${scored.length} with a holdout verdict`} />
        <Stat label="Passed holdout" value={String(scored.filter((item) => item.verdict === "PASSED").length)} detail="A pass is evidence for paper trading, not permission to trade" />
      </div>
      <article className="panel mt-6 p-5 text-sm leading-6 text-slate-300">
        <p className="font-semibold text-white">How an experiment decides</p>
        <ol className="mt-2 list-decimal space-y-1 pl-5">
          <li>A hypothesis is written down, then an experiment: dataset, strategy, parameter grid, a selection period, a sealed holdout after it, and the mean R that would count as success.</li>
          <li>Registering freezes all of that. The selection job searches the grid on the selection period only, with walk-forward folds, and never reads the holdout.</li>
          <li>The chosen parameters are scored on the holdout once. PASSED needs the registered mean R and a 95% interval above zero.</li>
          <li>Nothing here changes a strategy. Acting on a result is a separate decision in Strategies › Builder.</li>
        </ol>
      </article>
    </section>
  );
}

function WorkerCard({ worker }: { worker: ResearchWorkerStatus | null }) {
  return (
    <article className="panel p-5">
      <div className="flex items-center justify-between gap-3">
        <span className="text-sm font-semibold text-white">Research worker</span>
        <span className={`status-pill ${statusTone(worker?.status ?? "unknown")}`}>{titleCase(worker?.status ?? "unknown")}</span>
      </div>
      <p className="mt-3 text-sm text-slate-400">{worker ? `${worker.window_detail} Queued ${worker.queued}, running ${worker.running}.` : "Status unavailable."}</p>
    </article>
  );
}

function Stat({ label, value, detail }: { label: string; value: string; detail: string }) {
  return (
    <article className="panel p-5">
      <p className="text-sm text-slate-400">{label}</p>
      <p className="mt-1 text-2xl font-semibold text-white tabular-nums">{value}</p>
      <p className="mt-2 text-xs text-slate-500">{detail}</p>
    </article>
  );
}

// --- datasets --------------------------------------------------------------------------

function Datasets({ onMessage }: { onMessage: (message: string) => void }) {
  const load = useCallback(() => api.researchDatasets(), []);
  const { data, loading, refresh } = useLoad(load, onMessage);
  const rows = data ?? [];
  return (
    <section>
      <Header eyebrow="Historical data" title="Datasets" copy="Each dataset is a fixed set of bars with a content hash. Coverage is measured against the benchmark's own sessions; missing bars are reported, never filled." onRefresh={refresh} loading={loading} />
      <article className="panel mt-6 overflow-hidden">
        {rows.length ? (
          <div className="table-scroll">
            <table className="terminal-table">
              <thead><tr><th>Dataset</th><th>Period</th><th>Instruments</th><th>Bars</th><th>Sessions</th><th>Complete</th><th>Prices</th><th>Content hash</th></tr></thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.id}>
                    <td><strong>{row.name}</strong><span className={`status-pill ${row.status === "COMPLETE" ? "status-good" : "status-watch"}`}>{titleCase(row.status)}</span></td>
                    <td>{row.start_date} → {row.end_date}<span>{row.timeframe} · {row.source}</span></td>
                    <td className="numeric">{row.instrument_count}</td>
                    <td className="numeric">{row.row_count.toLocaleString("en-IN")}</td>
                    <td className="numeric">{row.sessions}</td>
                    <td className="numeric">{pct(row.complete_instrument_sessions, row.instrument_sessions)}</td>
                    <td>{titleCase(row.price_adjustment.toLowerCase())}</td>
                    <td className="muted-cell font-mono text-xs">{row.content_sha256 ? row.content_sha256.slice(0, 16) : "not finalised"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="empty-inset m-5 flex flex-col items-center gap-3 p-10 text-center">
            <Database className="h-7 w-7 text-slate-500" />
            <p className="text-sm text-slate-400">No datasets yet. Queue a backfill with scripts/research_backfill.py; it runs after 3:40 PM.</p>
          </div>
        )}
      </article>
    </section>
  );
}

// --- jobs --------------------------------------------------------------------------------

function Jobs({ isAdmin, onMessage }: { isAdmin: boolean; onMessage: (message: string) => void }) {
  const load = useCallback(() => api.researchJobs(), []);
  const { data, loading, refresh } = useLoad(load, onMessage);
  const cancel = async (job: ResearchJob) => {
    try {
      await api.cancelResearchJob(job.id);
      onMessage(job.status === "QUEUED" ? "Job cancelled." : "Cancellation requested; the worker stops the job at its next check.");
      refresh();
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Could not cancel the job");
    }
  };
  return (
    <section>
      <Header eyebrow="Research worker" title="Jobs" copy="Backtests, sweeps, backfills and experiment stages, run by the research worker and never by the API." onRefresh={refresh} loading={loading} />
      <article className="panel mt-6 overflow-hidden">
        <div className="table-scroll">
          <table className="terminal-table">
            <thead><tr><th>Job</th><th>Status</th><th>Attempts</th><th>Asked</th><th>Finished</th><th>Detail</th><th /></tr></thead>
            <tbody>
              {(data ?? []).map((job) => (
                <tr key={job.id}>
                  <td><strong>{titleCase(job.job_type)}</strong><span className="font-mono text-xs">{job.id.slice(0, 8)}</span></td>
                  <td><span className={`status-pill ${statusTone(job.status === "SUCCEEDED" ? "healthy" : job.status === "FAILED" ? "failed" : "degraded")}`}>{titleCase(job.status.toLowerCase())}</span></td>
                  <td className="numeric">{job.attempts}/{job.max_attempts}</td>
                  <td className="muted-cell">{formatIstTimestamp(job.created_at)}</td>
                  <td className="muted-cell">{job.finished_at ? formatIstTimestamp(job.finished_at) : "—"}</td>
                  <td className="max-w-md text-xs text-slate-400">{job.failure_detail ?? (job.result ? JSON.stringify(job.result).slice(0, 140) : "")}</td>
                  <td>{isAdmin && (job.status === "QUEUED" || job.status === "RUNNING") && !job.cancel_requested && <button className="secondary-button" onClick={() => void cancel(job)}><Ban className="h-4 w-4" />Cancel</button>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!loading && !(data ?? []).length && <p className="p-5 text-sm text-slate-500">No research jobs yet.</p>}
      </article>
    </section>
  );
}

// --- experiments -----------------------------------------------------------------------------

function Experiments({ isAdmin, onMessage }: { isAdmin: boolean; onMessage: (message: string) => void }) {
  const load = useCallback(
    () => Promise.all([api.researchExperiments(), api.researchHypotheses(), api.researchDatasets(), api.strategies()]),
    [],
  );
  const { data, loading, refresh } = useLoad(load, onMessage);
  const [experiments, hypotheses, datasets, strategies] = data ?? [[], [], [], []];
  const [selected, setSelected] = useState<string | null>(null);
  const current = selected ?? (experiments as ResearchExperiment[])[0]?.id ?? null;
  return (
    <section>
      <Header eyebrow="Pre-registered" title="Experiments" copy="Written down before they run, frozen when registered, and scored on the holdout once." onRefresh={refresh} loading={loading} />
      {isAdmin && <NewExperimentForm hypotheses={hypotheses as ResearchHypothesis[]} datasets={datasets as ResearchDataset[]} strategies={strategies as PaperStrategy[]} onMessage={onMessage} onCreated={(id) => { setSelected(id); refresh(); }} onRefresh={refresh} />}
      <div className="mt-6 grid gap-4 xl:grid-cols-[minmax(0,320px)_minmax(0,1fr)]">
        <article className="panel p-3">
          {(experiments as ResearchExperiment[]).length ? (experiments as ResearchExperiment[]).map((item) => (
            <button key={item.id} onClick={() => setSelected(item.id)} className={`block w-full rounded-md px-3 py-2 text-left ${item.id === current ? "bg-slate-800" : "hover:bg-slate-800/50"}`}>
              <span className="block text-sm font-semibold text-white">{item.name}</span>
              <span className="mt-1 flex items-center gap-2 text-xs text-slate-400"><StatusPill experiment={item} />{item.variants} variants</span>
            </button>
          )) : <p className="p-3 text-sm text-slate-500">No experiments yet.</p>}
        </article>
        {current ? <ExperimentDetail id={current} isAdmin={isAdmin} onMessage={onMessage} onChanged={refresh} /> : <article className="empty-inset p-10 text-center text-sm text-slate-400">Select or create an experiment.</article>}
      </div>
    </section>
  );
}

function StatusPill({ experiment }: { experiment: ResearchExperiment }) {
  if (experiment.verdict === "PASSED") return <span className="status-pill status-good">Passed</span>;
  if (experiment.verdict === "FAILED") return <span className="status-pill status-bad">Failed</span>;
  return <span className={`status-pill ${experiment.status === "FAILED_TO_RUN" ? "status-bad" : "status-watch"}`}>{titleCase(experiment.status.toLowerCase())}</span>;
}

function ExperimentDetail({ id, isAdmin, onMessage, onChanged }: { id: string; isAdmin: boolean; onMessage: (m: string) => void; onChanged: () => void }) {
  const load = useCallback(() => api.researchExperiment(id), [id]);
  const { data, loading, refresh } = useLoad(load, onMessage);
  const [confirming, setConfirming] = useState(false);
  useEffect(() => setConfirming(false), [id]);
  if (!data) return <article className="panel p-5">{loading ? <div className="skeleton h-40" /> : null}</article>;
  const selection = data.results.find((item) => item.kind === "SELECTION");
  const holdout = data.results.find((item) => item.kind === "HOLDOUT");
  const act = async (action: () => Promise<unknown>, done: string) => {
    try {
      await action();
      onMessage(done);
      refresh();
      onChanged();
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "The request was refused");
    }
  };
  return (
    <article className="panel p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="text-lg font-semibold text-white">{data.name}</h3>
          <p className="mt-1 flex items-center gap-2 text-sm text-slate-400"><StatusPill experiment={data} />{data.registered_at ? <><Lock className="h-3.5 w-3.5" />Registered {formatIstTimestamp(data.registered_at)}</> : "Draft — not yet frozen"}</p>
        </div>
        {isAdmin && data.status === "DRAFT" && <button className="primary-button" onClick={() => void act(() => api.registerExperiment(id), "Registered and queued for selection.")}><Play className="h-4 w-4" />Register and run selection</button>}
        {isAdmin && data.status === "SELECTED" && !data.holdout_used && !confirming && <button className="primary-button" onClick={() => setConfirming(true)}><ShieldAlert className="h-4 w-4" />Score the holdout</button>}
      </div>
      {confirming && (
        <div className="mt-4 rounded-md border border-amber-500/40 bg-amber-500/10 p-4 text-sm text-amber-100">
          <p>The holdout ({data.holdout_start} → {data.holdout_end}) can be scored <strong>once</strong>, for the chosen parameters only. A second look is refused, because a second look is not a test.</p>
          <div className="mt-3 flex gap-2">
            <button className="primary-button" onClick={() => { setConfirming(false); void act(() => api.scoreHoldout(id), "Holdout scoring queued."); }}>Score it once</button>
            <button className="secondary-button" onClick={() => setConfirming(false)}>Not now</button>
          </div>
        </div>
      )}
      <dl className="mt-5 grid gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
        <Row label="Strategy" value={`${String(data.strategy.name ?? data.strategy.id)} (${String(data.strategy.strategy_type)})`} />
        <Row label="Instruments" value={`${data.instruments.length}`} />
        <Row label="Selection" value={`${data.selection_start} → ${data.selection_end}`} />
        <Row label="Holdout (sealed)" value={`${data.holdout_start} → ${data.holdout_end}${data.holdout_used ? " · used" : ""}`} />
        <Row label="Walk-forward" value={`train ${data.train_sessions}, test ${data.test_sessions} sessions`} />
        <Row label="Success threshold" value={`mean R ≥ ${data.success_mean_r} and 95% CI above zero`} />
        <Row label="Grid" value={`${Object.entries(data.parameter_grid).map(([key, values]) => `${key}: ${values.join(", ")}`).join(" · ")} (${data.variants} variants)`} />
        {data.chosen_parameters && <Row label="Chosen in sample" value={Object.entries(data.chosen_parameters).map(([key, value]) => `${key}=${String(value)}`).join(", ")} />}
      </dl>
      {selection && <SelectionResult summary={selection.summary as SelectionSummary} fingerprint={selection.code_fingerprint} />}
      {holdout && <HoldoutResult summary={holdout.summary as HoldoutSummary} />}
    </article>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return <div className="flex min-w-0 flex-col"><dt className="text-xs uppercase tracking-wide text-slate-500">{label}</dt><dd className="break-words text-slate-200">{value}</dd></div>;
}

function StatLine({ title, stat }: { title: string; stat?: ResearchStat }) {
  if (!stat) return null;
  const ci = stat.ci95_mean_r;
  return (
    <tr>
      <td>{title}</td>
      <td className="numeric">{stat.trades}</td>
      <td className="numeric">{r(stat.mean_r)}</td>
      <td className="numeric">{ci ? `${r(ci[0])} … ${r(ci[1])}` : "n/a (<10 trades)"}</td>
      <td className="numeric">{r(stat.t_stat ?? null, 2)}</td>
      <td className="numeric">{stat.win_rate === null || stat.win_rate === undefined ? "—" : `${(stat.win_rate * 100).toFixed(1)}%`}</td>
    </tr>
  );
}

function StatTable({ rows }: { rows: [string, ResearchStat | undefined][] }) {
  return (
    <div className="table-scroll mt-3">
      <table className="terminal-table">
        <thead><tr><th /><th>Trades</th><th>Mean R</th><th>95% CI of mean R</th><th>t</th><th>Win rate</th></tr></thead>
        <tbody>{rows.map(([title, stat]) => <StatLine key={title} title={title} stat={stat} />)}</tbody>
      </table>
    </div>
  );
}

function SelectionResult({ summary, fingerprint }: { summary: SelectionSummary; fingerprint: string }) {
  const walk = summary.walk_forward ?? {};
  const dsr: Partial<Deflated> = walk.deflated_sharpe ?? {};
  const costs: Partial<Costs> = walk.cost_sensitivity ?? {};
  const regimes = walk.regimes ?? {};
  return (
    <section className="mt-6 border-t border-slate-800 pt-5">
      <h4 className="font-semibold text-white">Selection: walk-forward, out of sample</h4>
      <p className="mt-1 text-xs text-slate-500">{summary.selection_sessions} sessions · {summary.variants_tested} variants tried · code {fingerprint.slice(0, 12)} · dataset {summary.dataset?.name}</p>
      <StatTable rows={[["Stitched out-of-sample", walk.stitched], ...Object.entries(regimes).map(([name, stat]) => [`Benchmark ${name.toLowerCase()} days`, stat] as [string, ResearchStat])]} />
      <p className="mt-3 text-sm text-slate-300">
        Deflated Sharpe: observed {r(dsr.sharpe)} per trade against {r(dsr.threshold)} expected from the best of {dsr.trials} by luck.
        Probability it is real: <strong className={(dsr.dsr ?? 0) >= 0.95 ? "text-emerald-300" : "text-rose-300"}>{dsr.dsr === null || dsr.dsr === undefined ? "n/a" : `${(dsr.dsr * 100).toFixed(1)}%`}</strong> (0.95 or more counts).
      </p>
      <p className="mt-1 text-sm text-slate-300">Mean R at 1× / 1.5× / 2× charges: {r(costs.x1)} / {r(costs.x1_5)} / {r(costs.x2)}</p>
      <Folds folds={walk.folds ?? []} />
    </section>
  );
}

function Folds({ folds }: { folds: Fold[] }) {
  if (!folds.length) return <p className="mt-3 text-sm text-slate-500">Too few sessions for a walk-forward fold.</p>;
  return (
    <div className="table-scroll mt-3">
      <table className="terminal-table">
        <thead><tr><th>Train</th><th>Test</th><th>Chose</th><th>Out trades</th><th>Out avg R</th></tr></thead>
        <tbody>{folds.map((fold, index) => (
          <tr key={index}><td>{fold.train[0]} → {fold.train[1]}</td><td>{fold.test[0]} → {fold.test[1]}</td><td className="numeric">#{fold.chosen_index}</td><td className="numeric">{fold.out_of_sample.trades}</td><td className="numeric">{r(fold.out_of_sample.average_realized_r)}</td></tr>
        ))}</tbody>
      </table>
    </div>
  );
}

function HoldoutResult({ summary }: { summary: HoldoutSummary }) {
  const passed = summary.verdict === "PASSED";
  return (
    <section className="mt-6 border-t border-slate-800 pt-5">
      <h4 className="flex items-center gap-2 font-semibold text-white">
        {passed ? <CheckCircle2 className="h-5 w-5 text-emerald-300" /> : <XCircle className="h-5 w-5 text-rose-300" />}
        Holdout: {passed ? "passed" : "failed"}
      </h4>
      <p className="mt-1 text-xs text-slate-500">{summary.holdout?.[0]} → {summary.holdout?.[1]} · {summary.holdout_sessions} sessions · rule: {summary.verdict_rule}</p>
      <StatTable rows={[["Holdout", summary.summary], ...Object.entries(summary.regimes ?? {}).map(([name, stat]) => [`Benchmark ${name.toLowerCase()} days`, stat] as [string, ResearchStat])]} />
      <p className="mt-3 text-sm text-slate-300">Mean R at 1× / 1.5× / 2× charges: {r(summary.cost_sensitivity?.x1)} / {r(summary.cost_sensitivity?.x1_5)} / {r(summary.cost_sensitivity?.x2)}</p>
      <p className="mt-3 text-xs text-slate-500">A pass is evidence for forward paper trading. It changes nothing by itself; any strategy change is made in Strategies › Builder.</p>
    </section>
  );
}

// --- creating ---------------------------------------------------------------------------------

function NewExperimentForm({ hypotheses, datasets, strategies, onMessage, onCreated, onRefresh }: { hypotheses: ResearchHypothesis[]; datasets: ResearchDataset[]; strategies: PaperStrategy[]; onMessage: (m: string) => void; onCreated: (id: string) => void; onRefresh: () => void }) {
  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState("");
  const [statement, setStatement] = useState("");
  const complete = useMemo(() => datasets.filter((item) => item.status === "COMPLETE"), [datasets]);
  const [draft, setDraft] = useState({ hypothesis_id: "", name: "", dataset_id: "", strategy_id: "", instruments: "", grid: '{"minimum_score": [65, 71, 77]}', selection_start: "", selection_end: "", holdout_start: "", holdout_end: "", train_sessions: "60", test_sessions: "20", success_mean_r: "0.1" });
  const set = (key: keyof typeof draft) => (event: { target: { value: string } }) => setDraft((current) => ({ ...current, [key]: event.target.value }));

  const addHypothesis = async () => {
    try {
      const created = await api.createHypothesis({ title, statement });
      setDraft((current) => ({ ...current, hypothesis_id: created.id }));
      onMessage("Hypothesis recorded.");
      setTitle("");
      setStatement("");
      onRefresh();
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Could not record the hypothesis");
    }
  };
  const create = async () => {
    let grid: Record<string, unknown[]>;
    try {
      grid = JSON.parse(draft.grid);
    } catch {
      onMessage("The parameter grid is not valid JSON, for example {\"minimum_score\": [65, 71]}.");
      return;
    }
    const body: NewExperiment = {
      hypothesis_id: draft.hypothesis_id,
      name: draft.name,
      dataset_id: draft.dataset_id,
      strategy_id: draft.strategy_id,
      instruments: draft.instruments.split(/[\s,]+/).map((item) => item.trim()).filter(Boolean),
      parameter_grid: grid,
      selection_start: draft.selection_start,
      selection_end: draft.selection_end,
      holdout_start: draft.holdout_start,
      holdout_end: draft.holdout_end,
      train_sessions: Number(draft.train_sessions),
      test_sessions: Number(draft.test_sessions),
      success_mean_r: Number(draft.success_mean_r),
    };
    try {
      const created = await api.createExperiment(body);
      onMessage("Experiment saved as a draft. Register it to freeze it and run selection.");
      setOpen(false);
      onCreated(created.id);
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "The experiment was refused");
    }
  };

  if (!open) return <div className="mt-5"><button className="secondary-button" onClick={() => setOpen(true)}><FlaskConical className="h-4 w-4" />New experiment</button></div>;
  return (
    <article className="panel mt-5 p-5">
      <p className="font-semibold text-white">1. Hypothesis</p>
      <div className="mt-3 grid gap-3 sm:grid-cols-2">
        <label className="field-label">Existing<select id="exp-hypothesis" className="field-input mt-2" value={draft.hypothesis_id} onChange={set("hypothesis_id")}><option value="">Choose…</option>{hypotheses.map((item) => <option key={item.id} value={item.id}>{item.title}</option>)}</select></label>
        <div className="grid gap-2">
          <input id="hyp-title" className="field-input" placeholder="Or a new one: title" value={title} onChange={(event) => setTitle(event.target.value)} />
          <textarea id="hyp-statement" className="field-input" rows={2} placeholder="The claim, in one or two sentences" value={statement} onChange={(event) => setStatement(event.target.value)} />
          <button className="secondary-button" onClick={() => void addHypothesis()} disabled={title.length < 3 || statement.length < 10}>Record hypothesis</button>
        </div>
      </div>
      <p className="mt-6 font-semibold text-white">2. Experiment (frozen when registered)</p>
      <div className="mt-3 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <label className="field-label">Name<input id="exp-name" className="field-input mt-2" value={draft.name} onChange={set("name")} /></label>
        <label className="field-label">Dataset<select id="exp-dataset" className="field-input mt-2" value={draft.dataset_id} onChange={set("dataset_id")}><option value="">Choose a finalised dataset…</option>{complete.map((item) => <option key={item.id} value={item.id}>{item.name} ({item.start_date} → {item.end_date})</option>)}</select></label>
        <label className="field-label">Strategy<select id="exp-strategy" className="field-input mt-2" value={draft.strategy_id} onChange={set("strategy_id")}><option value="">Choose…</option>{strategies.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
        <label className="field-label">Success: mean R ≥<input id="exp-threshold" className="field-input mt-2" type="number" step="0.01" value={draft.success_mean_r} onChange={set("success_mean_r")} /></label>
        <label className="field-label xl:col-span-4">Instruments (keys, comma or space separated)<textarea id="exp-instruments" className="field-input mt-2" rows={2} value={draft.instruments} onChange={set("instruments")} /></label>
        <label className="field-label xl:col-span-2">Parameter grid (JSON)<textarea id="exp-grid" className="field-input mt-2 font-mono text-xs" rows={3} value={draft.grid} onChange={set("grid")} /></label>
        <label className="field-label">Train sessions<input id="exp-train" className="field-input mt-2" type="number" value={draft.train_sessions} onChange={set("train_sessions")} /></label>
        <label className="field-label">Test sessions<input id="exp-test" className="field-input mt-2" type="number" value={draft.test_sessions} onChange={set("test_sessions")} /></label>
        <label className="field-label">Selection from<input id="exp-sel-start" className="field-input mt-2" type="date" value={draft.selection_start} onChange={set("selection_start")} /></label>
        <label className="field-label">Selection to<input id="exp-sel-end" className="field-input mt-2" type="date" value={draft.selection_end} onChange={set("selection_end")} /></label>
        <label className="field-label">Holdout from<input id="exp-hold-start" className="field-input mt-2" type="date" value={draft.holdout_start} onChange={set("holdout_start")} /></label>
        <label className="field-label">Holdout to<input id="exp-hold-end" className="field-input mt-2" type="date" value={draft.holdout_end} onChange={set("holdout_end")} /></label>
      </div>
      <div className="mt-4 flex gap-2">
        <button className="primary-button" onClick={() => void create()} disabled={!draft.hypothesis_id || !draft.dataset_id || !draft.strategy_id || draft.name.length < 3}>Save draft</button>
        <button className="secondary-button" onClick={() => setOpen(false)}>Close</button>
      </div>
    </article>
  );
}
