"use client";

import { Database, FlaskConical, Play, Plus, Trash2 } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { Paged } from "../../components/paged";
import {
  api,
  type CorporateEvent,
  type SwingBacktestRun,
  type SwingCoverage,
  type SwingJobs,
  type SwingMetrics,
} from "../../components/api";

/**
 * Swing > Backtest and Swing > Data: the jobs that used to be scripts.
 *
 * Heavy jobs (fetching daily candles, a backtest) wait for the close: the
 * server refuses them on a weekday between 9:00 AM and 3:35 PM, and the button
 * says so before anyone presses it. One job runs at a time; the screen checks
 * on it every few seconds until it is done.
 */

const STRATEGIES: { id: string; name: string }[] = [
  { id: "swing-trend-pullback-v1", name: "Trend Pullback" },
  { id: "swing-base-breakout-v1", name: "Base Breakout" },
];
const NAMES: Record<string, string> = Object.fromEntries(STRATEGIES.map((s) => [s.id, s.name]));

const rupees = (value: number) => `${value < 0 ? "−" : ""}₹${Math.abs(value).toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
const tone = (value: number) => (value > 0 ? "text-emerald-300" : value < 0 ? "text-rose-300" : "text-slate-300");

function useJobs(onMessage: (message: string) => void) {
  const [jobs, setJobs] = useState<SwingJobs | null>(null);
  const refresh = useCallback(async () => {
    try {
      setJobs(await api.swingJobs());
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Could not read the swing jobs");
    }
  }, [onMessage]);
  useEffect(() => {
    void refresh();
  }, [refresh]);
  const running = Object.values(jobs?.jobs ?? {}).some((job) => job.status === "running");
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => void refresh(), 5000);
    return () => window.clearInterval(timer);
  }, [running, refresh]);
  return { jobs, setJobs, refresh, running };
}

function JobLine({ jobs, kind }: { jobs: SwingJobs | null; kind: "backfill" | "backtest" }) {
  const job = jobs?.jobs[kind];
  if (!job) return null;
  const style =
    job.status === "running"
      ? "border-sky-500/30 bg-sky-950/20 text-sky-100"
      : job.status === "done"
        ? "border-emerald-500/30 bg-emerald-950/20 text-emerald-200"
        : "border-rose-500/30 bg-rose-950/25 text-rose-100";
  return (
    <p className={`mt-4 rounded-md border p-3 text-sm ${style}`} data-testid={`job-${kind}`}>
      <b>{job.status === "running" ? "Running…" : job.status === "done" ? "Done." : "Failed."}</b> {job.detail}
    </p>
  );
}

function MarketHoursNote({ jobs }: { jobs: SwingJobs | null }) {
  if (!jobs?.market_hours) return null;
  return (
    <p className="mt-3 text-xs text-amber-300" data-testid="market-hours-note">
      Market hours: this waits until after 3:35 PM IST, so it never slows the intraday scanner.
    </p>
  );
}

// --- Backtest --------------------------------------------------------------------------------

export function SwingBacktestPanel({ isAdmin, onMessage }: { isAdmin: boolean; onMessage: (message: string) => void }) {
  const { jobs, setJobs, running } = useJobs(onMessage);
  const [runs, setRuns] = useState<SwingBacktestRun[]>([]);
  const [years, setYears] = useState(2);
  const [chosen, setChosen] = useState<string[]>(STRATEGIES.map((s) => s.id));
  const [opened, setOpened] = useState(0);

  const loadRuns = useCallback(async () => {
    try {
      setRuns(await api.swingBacktests());
    } catch {
      setRuns([]);
    }
  }, []);
  useEffect(() => {
    void loadRuns();
  }, [loadRuns]);
  // A run that just finished: show it.
  const status = jobs?.jobs.backtest?.status;
  useEffect(() => {
    if (status === "done") void loadRuns();
  }, [status, loadRuns]);

  async function start() {
    try {
      setJobs(await api.startSwingBacktest(years, chosen));
      onMessage("Backtest started. It takes a minute or two.");
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Could not start the backtest");
    }
  }

  const run = runs[opened];
  return (
    <div className="space-y-5">
      <article className="panel p-5 sm:p-7" data-testid="swing-backtest">
        <div className="flex items-center gap-3 border-b border-slate-800 pb-4">
          <FlaskConical className="h-5 w-5 text-sky-300" />
          <div>
            <p className="eyebrow">Test before trusting</p>
            <h3 className="font-semibold text-white">Run a swing backtest</h3>
            <p className="mt-1 text-xs text-slate-400">
              NIFTY 200 daily history, ₹1,00,000 paper capital, the charges in Settings › Charges. The first 70% of the history is
              in-sample; judge a strategy by the last 30%, which it never saw.
            </p>
          </div>
        </div>
        <div className="mt-5 flex flex-wrap items-end gap-4">
          <label className="field-label">
            History
            <select className="field-input mt-1" value={years} disabled={!isAdmin} onChange={(event) => setYears(Number(event.target.value))}>
              {[1, 2, 3].map((value) => (
                <option key={value} value={value}>
                  {value} year{value > 1 ? "s" : ""}
                </option>
              ))}
            </select>
          </label>
          <fieldset className="flex flex-wrap gap-4 text-sm text-slate-300">
            {STRATEGIES.map((strategy) => (
              <label key={strategy.id} className="flex items-center gap-2">
                <input
                  type="checkbox"
                  disabled={!isAdmin}
                  checked={chosen.includes(strategy.id)}
                  onChange={(event) =>
                    setChosen(event.target.checked ? [...chosen, strategy.id] : chosen.filter((id) => id !== strategy.id))
                  }
                />
                {strategy.name}
              </label>
            ))}
          </fieldset>
          {isAdmin && (
            <button className="primary-button" disabled={running || jobs?.market_hours || !chosen.length} onClick={() => void start()}>
              <Play className="h-4 w-4" />
              Run backtest
            </button>
          )}
        </div>
        <MarketHoursNote jobs={jobs} />
        <JobLine jobs={jobs} kind="backtest" />
      </article>

      {runs.length > 0 && run && (
        <article className="panel p-5 sm:p-7" data-testid="swing-backtest-result">
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-800 pb-4">
            <div>
              <p className="eyebrow">Result</p>
              <h3 className="font-semibold text-white">
                {run.from_date} → {run.to_date} · {run.strategies.map((id) => NAMES[id] ?? id).join(", ")}
              </h3>
              <p className="mt-1 text-xs text-slate-400">
                Out-of-sample from {run.out_of_sample_from}. Charges: brokerage ₹{run.charges.brokerage_per_order}/order, DP ₹
                {run.charges.dp_charge_per_sale}/sale. {run.shares_with_data} shares with data.
              </p>
            </div>
            {runs.length > 1 && (
              <select className="field-input w-auto" aria-label="Earlier runs" value={opened} onChange={(event) => setOpened(Number(event.target.value))}>
                {runs.map((item, index) => (
                  <option key={item.id} value={index}>
                    {new Date(item.finished_at).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" })}
                  </option>
                ))}
              </select>
            )}
          </div>
          <div className="table-scroll mt-4">
            <table className="terminal-table">
              <thead>
                <tr>
                  <th />
                  <th>Trades</th>
                  <th>Win</th>
                  <th>Avg R</th>
                  <th>₹ / trade</th>
                  <th>Net</th>
                  <th>Charges</th>
                  <th>Max DD</th>
                </tr>
              </thead>
              <tbody>
                <MetricsRow label="In-sample" m={run.in_sample} />
                <MetricsRow label="OUT-OF-SAMPLE" m={run.out_of_sample} strong />
                {Object.entries(run.per_strategy).flatMap(([id, part]) => [
                  <MetricsRow key={`${id}-in`} label={`${NAMES[id] ?? id} · in`} m={part.in_sample} />,
                  <MetricsRow key={`${id}-out`} label={`${NAMES[id] ?? id} · out`} m={part.out_of_sample} strong />,
                ])}
              </tbody>
            </table>
          </div>
          <p className="mt-3 text-xs leading-5 text-slate-500">
            Read the out-of-sample rows. Figures are after charges and lean optimistic (today&apos;s index members only). Fewer than
            about 100 trades is an anecdote, not evidence.
          </p>
        </article>
      )}
    </div>
  );
}

function MetricsRow({ label, m, strong }: { label: string; m: SwingMetrics; strong?: boolean }) {
  return (
    <tr data-testid={`metrics-${label}`}>
      <td className={strong ? "font-semibold text-white" : "text-slate-400"}>{label}</td>
      <td className="numeric">{m.trades}</td>
      <td className="numeric">{m.trades ? `${Math.round(m.win_rate * 100)}%` : "—"}</td>
      <td className="numeric">{m.trades ? m.average_r.toFixed(2) : "—"}</td>
      <td className={`numeric ${tone(m.per_trade)}`}>{m.trades ? rupees(m.per_trade) : "—"}</td>
      <td className={`numeric ${tone(m.net_pnl)}`}>{rupees(m.net_pnl)}</td>
      <td className="numeric text-slate-400">{rupees(m.charges)}</td>
      <td className="numeric text-slate-400">{rupees(m.max_drawdown)}</td>
    </tr>
  );
}

// --- Data ----------------------------------------------------------------------------------

export function SwingDataPanel({ isAdmin, onMessage }: { isAdmin: boolean; onMessage: (message: string) => void }) {
  const { jobs, setJobs, running } = useJobs(onMessage);
  const [coverage, setCoverage] = useState<SwingCoverage | null>(null);
  const [events, setEvents] = useState<CorporateEvent[]>([]);
  const [loading, setLoading] = useState(false);
  const [form, setForm] = useState({ symbol: "", event_type: "SPLIT", ex_date: "", price_factor: "", amount: "", note: "" });

  const loadEvents = useCallback(async () => {
    try {
      setEvents(await api.corporateEvents());
    } catch {
      setEvents([]);
    }
  }, []);
  useEffect(() => {
    void loadEvents();
  }, [loadEvents]);

  async function check() {
    setLoading(true);
    try {
      setCoverage(await api.swingCoverage());
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Could not check the data");
    } finally {
      setLoading(false);
    }
  }

  async function fetchCandles() {
    try {
      setJobs(await api.startSwingBackfill(2));
      onMessage("Fetching daily candles. This takes several minutes.");
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Could not start the fetch");
    }
  }

  async function saveEvent() {
    try {
      await api.recordCorporateEvent({
        symbol: form.symbol,
        event_type: form.event_type,
        ex_date: form.ex_date,
        price_factor: form.price_factor ? Number(form.price_factor) : null,
        amount: form.amount ? Number(form.amount) : null,
        note: form.note || null,
      });
      onMessage(`${form.symbol.toUpperCase()} ${form.event_type.toLowerCase()} recorded.`);
      setForm({ symbol: "", event_type: "SPLIT", ex_date: "", price_factor: "", amount: "", note: "" });
      await loadEvents();
      if (coverage) await check();
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Could not record the event");
    }
  }

  async function removeEvent(item: CorporateEvent) {
    try {
      await api.deleteCorporateEvent(item.id);
      onMessage(`${item.symbol} ${item.event_type.toLowerCase()} removed.`);
      await loadEvents();
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Could not remove the event");
    }
  }

  const adjusting = form.event_type === "SPLIT" || form.event_type === "BONUS";
  return (
    <div className="space-y-5">
      <article className="panel p-5 sm:p-7" data-testid="swing-data">
        <div className="flex items-center gap-3 border-b border-slate-800 pb-4">
          <Database className="h-5 w-5 text-sky-300" />
          <div>
            <p className="eyebrow">Daily history</p>
            <h3 className="font-semibold text-white">Is the data complete?</h3>
            <p className="mt-1 text-xs text-slate-400">
              The swing desk reads daily candles for the NIFTY 200. The worker fetches new days every evening; fetch here to fill
              gaps or after a long break. Fetching never rewrites a day already stored.
            </p>
          </div>
        </div>
        <div className="mt-5 flex flex-wrap gap-2">
          <button className="secondary-button" disabled={loading} onClick={() => void check()}>
            {loading ? "Checking…" : "Check data"}
          </button>
          {isAdmin && (
            <button className="primary-button" disabled={running || jobs?.market_hours} onClick={() => void fetchCandles()}>
              Fetch daily candles (2 years)
            </button>
          )}
        </div>
        <MarketHoursNote jobs={jobs} />
        <JobLine jobs={jobs} kind="backfill" />

        {coverage && (
          <div className="mt-5" data-testid="swing-coverage">
            <div className="grid gap-3 sm:grid-cols-4">
              {(["complete", "gaps", "short", "empty"] as const).map((key) => (
                <div key={key} className="glass-inset rounded-md p-3">
                  <p className="eyebrow">{{ complete: "Complete", gaps: "With gaps", short: "Too short", empty: "No data" }[key]}</p>
                  <p className={`numeric mt-1 text-xl font-semibold ${key === "complete" ? "text-emerald-300" : coverage.counts[key] ? "text-amber-300" : "text-slate-300"}`}>
                    {coverage.counts[key]}
                  </p>
                </div>
              ))}
            </div>
            <p className="mt-2 text-xs text-slate-500">
              {coverage.index_days} NIFTY trading days stored, last {coverage.last_index_day ?? "—"}. &quot;Too short&quot; means under{" "}
              {coverage.minimum_sessions} sessions: a recently listed share the strategies cannot use yet.
            </p>
            {coverage.shares.length > 0 && (
              <Paged rows={coverage.shares}>
                {(visible) => (
                  <div className="table-scroll mt-3">
                    <table className="terminal-table">
                      <thead>
                        <tr>
                          <th>Share</th>
                          <th>Status</th>
                          <th>Sessions</th>
                          <th>First</th>
                          <th>Missing days</th>
                        </tr>
                      </thead>
                      <tbody>
                        {visible.map((share) => (
                          <tr key={share.instrument_token}>
                            <td>{share.symbol}</td>
                            <td>{share.status}</td>
                            <td className="numeric">{share.sessions}</td>
                            <td>{share.first ?? "—"}</td>
                            <td className="numeric">{share.missing}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </Paged>
            )}
            <h4 className="mt-5 text-sm font-semibold text-white">Drops that look like a split, bonus or demerger</h4>
            {coverage.suspects.length === 0 ? (
              <p className="mt-1 text-sm text-slate-500">None found.</p>
            ) : (
              <Paged rows={coverage.suspects}>
                {(visible) => (
                  <div className="table-scroll mt-2">
                    <table className="terminal-table">
                      <thead>
                        <tr>
                          <th>Share</th>
                          <th>Date</th>
                          <th>Close → next open</th>
                          <th>Drop</th>
                          <th>Status</th>
                          <th />
                        </tr>
                      </thead>
                      <tbody>
                        {visible.map((item) => (
                          <tr key={`${item.instrument_token}-${item.ex_date}`} data-testid={`suspect-${item.symbol}`}>
                            <td>{item.symbol}</td>
                            <td>{item.ex_date}</td>
                            <td className="numeric">
                              {item.previous_close} → {item.open}
                            </td>
                            <td className="numeric">×{item.jump}</td>
                            <td>{item.confirmed ? <span className="status-pill status-good">Recorded</span> : <span className="status-pill status-watch">Not checked</span>}</td>
                            <td>
                              {isAdmin && !item.confirmed && (
                                <button
                                  className="secondary-button px-2 py-1 text-xs"
                                  onClick={() =>
                                    setForm({ symbol: item.symbol, event_type: "SPLIT", ex_date: item.ex_date, price_factor: String(item.jump), amount: "", note: "" })
                                  }
                                >
                                  Review
                                </button>
                              )}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </Paged>
            )}
            <p className="mt-2 text-xs leading-5 text-slate-500">
              A suspected drop is a question, not an answer: it changes nothing until you check the company&apos;s announcement
              (NSE › corporate actions) and record it below. A big fall on a crash day is not a split.
            </p>
          </div>
        )}
      </article>

      <article className="panel p-5 sm:p-7" data-testid="corporate-events">
        <p className="eyebrow">Corporate events</p>
        <h3 className="font-semibold text-white">Splits, bonuses, dividends and results dates</h3>
        <p className="mt-1 text-xs leading-5 text-slate-400">
          A recorded split or bonus divides every price before its date by the factor, so the history has no false crash. For a
          demerger, record a split with the observed drop as the factor and say so in the note. A results date keeps the swing desk
          out of that share around the announcement.
        </p>
        {isAdmin && (
          <div className="mt-4 grid gap-3 sm:grid-cols-3 lg:grid-cols-6" data-testid="event-form">
            <label className="field-label">
              Share
              <input className="field-input mt-1 uppercase" value={form.symbol} onChange={(event) => setForm({ ...form, symbol: event.target.value })} placeholder="e.g. SIEMENS" />
            </label>
            <label className="field-label">
              Type
              <select className="field-input mt-1" value={form.event_type} onChange={(event) => setForm({ ...form, event_type: event.target.value })}>
                <option value="SPLIT">Split (or demerger)</option>
                <option value="BONUS">Bonus</option>
                <option value="DIVIDEND">Dividend</option>
                <option value="RESULTS">Results date</option>
              </select>
            </label>
            <label className="field-label">
              Ex-date
              <input type="date" className="field-input mt-1" value={form.ex_date} onChange={(event) => setForm({ ...form, ex_date: event.target.value })} />
            </label>
            {adjusting ? (
              <label className="field-label">
                Price factor
                <input type="number" step="any" min="1" className="field-input mt-1" value={form.price_factor} onChange={(event) => setForm({ ...form, price_factor: event.target.value })} placeholder="2 for 1:2" />
              </label>
            ) : form.event_type === "DIVIDEND" ? (
              <label className="field-label">
                ₹ per share
                <input type="number" step="any" min="0" className="field-input mt-1" value={form.amount} onChange={(event) => setForm({ ...form, amount: event.target.value })} />
              </label>
            ) : (
              <span />
            )}
            <label className="field-label">
              Note
              <input className="field-input mt-1" maxLength={255} value={form.note} onChange={(event) => setForm({ ...form, note: event.target.value })} placeholder="source / reason" />
            </label>
            <div className="flex items-end">
              <button className="primary-button" disabled={!form.symbol || !form.ex_date} onClick={() => void saveEvent()}>
                <Plus className="h-4 w-4" />
                Record
              </button>
            </div>
          </div>
        )}
        {events.length === 0 ? (
          <p className="mt-4 text-sm text-slate-500">None recorded.</p>
        ) : (
          <Paged rows={events}>
            {(visible) => (
              <div className="table-scroll mt-4">
                <table className="terminal-table">
                  <thead>
                    <tr>
                      <th>Date</th>
                      <th>Share</th>
                      <th>Type</th>
                      <th>Factor / amount</th>
                      <th>Note</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {visible.map((item) => (
                      <tr key={item.id}>
                        <td>{item.ex_date}</td>
                        <td>{item.symbol}</td>
                        <td>{item.event_type}</td>
                        <td className="numeric">{item.price_factor ? `÷${item.price_factor}` : item.amount ? `₹${item.amount}` : "—"}</td>
                        <td>{item.note ?? ""}</td>
                        <td>
                          {isAdmin && (
                            <button className="secondary-button px-2 py-1 text-xs" aria-label={`Remove ${item.symbol} ${item.event_type}`} onClick={() => void removeEvent(item)}>
                              <Trash2 className="h-3.5 w-3.5" />
                            </button>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Paged>
        )}
      </article>
    </div>
  );
}
