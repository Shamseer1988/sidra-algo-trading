"use client";

import { Activity, RefreshCw, Search, Star, TriangleAlert } from "lucide-react";
import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";

import {
  api,
  type DataQuality,
  type EvaluationPage,
  type SafetyStatus,
  type ScannerDay,
  type ScannerEvaluation,
  type ScannerMoment,
  type ScannerShareDay,
  type ScannerStatus,
} from "../../components/api";
import { Modal } from "../../components/modal";
import { Paged, Pager } from "../../components/paged";
import { formatIstTimestamp, formatPrice, statusTone, titleCase } from "../../lib/formatting";
import { istDate } from "../../lib/strategy-names";
import { SetupInspector } from "./setup-inspector";

/**
 * The scanner's day, as answers rather than a tape.
 *
 * It checks every share against every strategy each minute: about fifty
 * thousand checks a day. Summary says what came of them, By share says where
 * each share stood, and All checks is the full record, paged by the server.
 */

type View = "summary" | "shares" | "checks";

/** Events arrive many times a minute while scanning; the summary follows at this pace. */
const FOLLOW_MS = 30_000;
const STARS = "sidra.scanner.watchlist";

const count = (value: number) => value.toLocaleString("en-IN");

function timeOf(value: string): string {
  return new Date(value).toLocaleTimeString("en-IN", {
    timeZone: "Asia/Kolkata",
    hour: "numeric",
    minute: "2-digit",
    hour12: true,
  });
}

function dayLabel(date: string): string {
  const text = new Date(`${date}T00:00:00Z`).toLocaleDateString("en-IN", {
    weekday: "short",
    day: "numeric",
    month: "short",
    timeZone: "UTC",
  });
  return date === istDate() ? `Today, ${text}` : text;
}

export function ScannerPanel({
  scanner,
  safety,
  dataQuality,
  refreshKey,
  canOperate,
  onStart,
  onStop,
  onRefresh,
}: {
  scanner: ScannerStatus;
  safety: SafetyStatus;
  dataQuality: DataQuality[];
  refreshKey: number;
  canOperate: boolean;
  onStart: () => void;
  onStop: () => void;
  onRefresh: () => void;
}) {
  const [view, setView] = useState<View>("summary");
  const [day, setDay] = useState<ScannerDay | null>(null);
  const [chosen, setChosen] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [open, setOpen] = useState<ScannerEvaluation | null>(null);
  const [inspectError, setInspectError] = useState("");
  const latest = useRef(0);
  const loadedAt = useRef(0);

  const load = useCallback(async () => {
    const request = ++latest.current;
    loadedAt.current = Date.now();
    setLoading(true);
    try {
      const next = await api.scannerDay(chosen || undefined);
      if (request !== latest.current) return;
      setDay(next);
      setError("");
    } catch (caught) {
      if (request === latest.current) {
        setError(caught instanceof Error ? caught.message : "Could not load the scanner's day");
      }
    } finally {
      if (request === latest.current) setLoading(false);
    }
  }, [chosen]);

  useEffect(() => {
    void load();
  }, [load]);

  // A chosen past day does not change, so only the newest one follows events.
  useEffect(() => {
    if (refreshKey && !chosen && Date.now() - loadedAt.current >= FOLLOW_MS) void load();
  }, [refreshKey, chosen, load]);

  const inspect = useCallback(async (id: string) => {
    setInspectError("");
    try {
      setOpen(await api.scannerEvaluation(id));
    } catch (caught) {
      setInspectError(caught instanceof Error ? caught.message : "Could not open this check");
    }
  }, []);

  return (
    <section>
      <div className="page-toolbar">
        <div>
          <p className="eyebrow">What the scanner saw</p>
          <h2 className="page-title">Scanner</h2>
          <p className="page-copy">
            Every minute each share is checked against each strategy. This is what those checks came to.
          </p>
        </div>
        <div className="flex flex-wrap items-end gap-2">
          <label className="text-xs" style={{ color: "var(--muted)" }}>
            Day
            <select
              aria-label="Scanner day"
              className="field-input mt-1 block py-2 text-sm"
              value={chosen}
              onChange={(event) => setChosen(event.target.value)}
            >
              <option value="">Latest{day?.sessions[0] ? ` (${dayLabel(day.sessions[0])})` : ""}</option>
              {(day?.sessions ?? []).map((date) => (
                <option key={date} value={date}>
                  {dayLabel(date)}
                </option>
              ))}
            </select>
          </label>
          <button
            className="secondary-button"
            onClick={() => {
              onRefresh();
              void load();
            }}
            disabled={loading}
          >
            <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
            Refresh
          </button>
          {canOperate && (
            <button
              className="primary-button"
              disabled={scanner.status === "RUNNING" || safety.emergency_stop_active}
              onClick={onStart}
            >
              <Activity className="h-4 w-4" />
              Start
            </button>
          )}
          {canOperate && (
            <button className="secondary-button" disabled={scanner.status === "STOPPED"} onClick={onStop}>
              <TriangleAlert className="h-4 w-4" />
              Stop
            </button>
          )}
        </div>
      </div>

      <StatusStrip scanner={scanner} safety={safety} dataQuality={dataQuality} />

      {(error || inspectError) && (
        <p role="alert" className="glass-notice mt-4 rounded-md p-4 text-sm" style={{ color: "var(--negative)" }}>
          {error || inspectError}
        </p>
      )}

      <div role="group" aria-label="Scanner view" className="mt-5 flex flex-wrap gap-2">
        {([
          ["summary", "Summary"],
          ["shares", "By share"],
          ["checks", "All checks"],
        ] as const).map(([value, label]) => (
          <button
            key={value}
            type="button"
            aria-pressed={view === value}
            className={`${view === value ? "primary-button" : "secondary-button"} px-4 py-2 text-xs`}
            onClick={() => setView(value)}
          >
            {label}
          </button>
        ))}
      </div>

      {!day ? (
        loading && (
          <div className="mt-5 space-y-2">
            {[0, 1, 2, 3].map((item) => (
              <div key={item} className="skeleton h-20" />
            ))}
          </div>
        )
      ) : view === "summary" ? (
        <Summary day={day} onInspect={inspect} />
      ) : view === "shares" ? (
        <Shares day={day} onInspect={inspect} />
      ) : (
        <Checks key={day.session_date} day={day} onOpen={setOpen} />
      )}

      {open && (
        <Modal title="Why this check was decided this way" onClose={() => setOpen(null)} wide>
          <SetupInspector evaluation={open} />
        </Modal>
      )}
    </section>
  );
}

function StatusStrip({
  scanner,
  safety,
  dataQuality,
}: {
  scanner: ScannerStatus;
  safety: SafetyStatus;
  dataQuality: DataQuality[];
}) {
  const good = dataQuality.filter((item) => item.allows_signals).length;
  const cells: [string, string, string][] = [
    ["Scanner now", titleCase(scanner.status.toLowerCase()), scanner.detail],
    [
      "Data feed now",
      dataQuality.length ? `${good} of ${dataQuality.length} shares ready` : "No live data",
      dataQuality.length
        ? "A share can signal only while its candles are complete and fresh."
        : "Normal outside market hours and on holidays.",
    ],
    [
      "Orders now",
      safety.emergency_stop_active
        ? "Emergency stop is on"
        : safety.live_execution_available
          ? "Live orders can be sent"
          : "No live order can be sent",
      safety.emergency_stop_active
        ? (safety.emergency_stop_reason ?? "Nothing new is opened.")
        : safety.live_execution_available
          ? "Only for strategies set to Live; the rest stay on paper."
          : "A live gate is holding. Paper scanning is not affected.",
    ],
  ];
  return (
    <dl className="panel mt-5 grid gap-px sm:grid-cols-3">
      {cells.map(([label, value, note]) => (
        <div key={label} className="px-5 py-3.5">
          <dt className="text-xs" style={{ color: "var(--muted)" }}>
            {label}
          </dt>
          <dd className="mt-1 font-semibold" style={{ color: "var(--foreground)" }}>
            {value}
          </dd>
          <dd className="mt-0.5 text-xs" style={{ color: "var(--muted)" }}>
            {note}
          </dd>
        </div>
      ))}
    </dl>
  );
}

function Panel({ title, note, children }: { title: string; note: string; children: ReactNode }) {
  return (
    <article className="panel overflow-hidden">
      <div className="px-5 py-4" style={{ borderBottom: "1px solid var(--glass-border)" }}>
        <h3 className="font-semibold" style={{ color: "var(--foreground)" }}>
          {title}
        </h3>
        <p className="mt-1 text-xs" style={{ color: "var(--muted)" }}>
          {note}
        </p>
      </div>
      {children}
    </article>
  );
}

function Empty({ children }: { children: ReactNode }) {
  return (
    <p className="p-6 text-sm" style={{ color: "var(--muted)" }}>
      {children}
    </p>
  );
}

function Side({ side }: { side: "LONG" | "SHORT" | null }) {
  return side ? <span className={`status-pill ${side === "LONG" ? "status-good" : "status-bad"}`}>{side}</span> : <>—</>;
}

function Summary({ day, onInspect }: { day: ScannerDay; onInspect: (id: string) => void }) {
  const { totals } = day;
  if (!totals.checks) {
    return (
      <article className="panel mt-5">
        <Empty>Nothing was scanned on {dayLabel(day.session_date)}.</Empty>
      </article>
    );
  }
  const most = Math.max(1, ...day.reasons.map((item) => item.count));
  return (
    <div className="mt-5 space-y-5">
      <dl className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Figure label="Shares watched" value={count(totals.shares)} note={dayLabel(day.session_date)} />
        <Figure
          label="Checks made"
          value={count(totals.checks)}
          note={`${count(totals.outside_hours)} were outside a strategy's trading hours`}
        />
        <Figure
          label="Setups scored"
          value={count(totals.scored)}
          note="Came close enough for the strategy to give a score"
        />
        <Figure
          label="Signals"
          value={count(totals.accepted)}
          note={
            totals.blocked_by_data
              ? `${count(totals.blocked_by_data)} checks skipped for missing candles`
              : "No check was skipped for missing candles"
          }
        />
      </dl>

      <Panel title="Signals taken" note="Passed every rule of the strategy. Whether an order followed is in History.">
        {day.signals.length ? (
          <Moments rows={day.signals} label="signals" onInspect={onInspect} />
        ) : (
          <Empty>No signal this day.</Empty>
        )}
      </Panel>

      <div className="grid gap-5 xl:grid-cols-2">
        <Panel title="Which strategy found setups" note="No scores means it found no setup, not that the score limit refused one.">
          <div className="table-scroll">
            <table className="terminal-table" aria-label="Strategies this day">
              <thead>
                <tr>
                  <th>Strategy</th>
                  <th>In hours</th>
                  <th>Scored</th>
                  <th>Signals</th>
                  <th>Best score</th>
                </tr>
              </thead>
              <tbody>
                {day.strategies.map((row) => (
                  <tr key={row.strategy_id}>
                    <td>
                      <strong>{row.strategy_name}</strong>
                    </td>
                    <td className="numeric">{count(row.checks - row.outside_hours)}</td>
                    <td className="numeric">{count(row.scored)}</td>
                    <td className="numeric">{row.accepted}</td>
                    <td className="numeric">{row.best_score || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>

        <Panel title="What stopped the rest" note="Most common first, counted only inside trading hours.">
          {day.reasons.length ? (
            <ol className="max-h-[420px] space-y-3 overflow-auto p-5">
              {day.reasons.map((item) => (
                <li key={item.reason}>
                  <div className="flex items-baseline justify-between gap-4 text-sm" style={{ color: "var(--text-body)" }}>
                    <span>{item.reason}</span>
                    <span className="numeric font-semibold">{count(item.count)}</span>
                  </div>
                  <div className="mt-1 h-1.5 rounded-full" style={{ background: "var(--glass-muted)" }} aria-hidden>
                    <div
                      className="h-1.5 rounded-full"
                      style={{ width: `${(item.count / most) * 100}%`, background: "var(--info)" }}
                    />
                  </div>
                </li>
              ))}
            </ol>
          ) : (
            <Empty>Nothing was turned away inside trading hours.</Empty>
          )}
        </Panel>
      </div>

      <Panel title="Closest misses" note="Scored but not taken, best score first.">
        {day.near.length ? (
          <Moments rows={day.near} label="misses" onInspect={onInspect} />
        ) : (
          <Empty>No setup came close enough to be scored.</Empty>
        )}
      </Panel>
    </div>
  );
}

function Figure({ label, value, note }: { label: string; value: string; note: string }) {
  return (
    <div className="panel p-5">
      <dt className="text-xs" style={{ color: "var(--muted)" }}>
        {label}
      </dt>
      <dd className="mt-1 text-2xl font-semibold" style={{ color: "var(--foreground)" }}>
        {value}
      </dd>
      <dd className="mt-1 text-xs" style={{ color: "var(--muted)" }}>
        {note}
      </dd>
    </div>
  );
}

function Moments({ rows, label, onInspect }: { rows: ScannerMoment[]; label: string; onInspect: (id: string) => void }) {
  return (
    <Paged rows={rows} label={label} initial={10}>
      {(visible) => (
        <div className="table-scroll">
          <table className="terminal-table" aria-label={label}>
            <thead>
              <tr>
                <th>Time</th>
                <th>Share</th>
                <th>Strategy</th>
                <th>Side</th>
                <th>Score</th>
                <th>Why</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {visible.map((row) => (
                <tr key={row.id} onClick={() => onInspect(row.id)}>
                  <td className="numeric">{timeOf(row.at)}</td>
                  <td>
                    <strong title={row.instrument_token}>{row.script_name}</strong>
                  </td>
                  <td>{row.strategy_name}</td>
                  <td>
                    <Side side={row.side} />
                  </td>
                  <td className="numeric">{row.score}</td>
                  <td>{row.reason}</td>
                  <td>
                    <Details name={row.script_name} onClick={() => onInspect(row.id)} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Paged>
  );
}

function Details({ name, onClick }: { name: string; onClick: () => void }) {
  return (
    <button
      type="button"
      className="secondary-button px-2.5 py-1 text-xs"
      aria-label={`Inspect ${name}`}
      onClick={(event) => {
        event.stopPropagation();
        onClick();
      }}
    >
      Details
    </button>
  );
}

function Shares({ day, onInspect }: { day: ScannerDay; onInspect: (id: string) => void }) {
  const [search, setSearch] = useState("");
  const [onlyStarred, setOnlyStarred] = useState(false);
  const [stars, setStars] = useState<string[]>([]);

  useEffect(() => {
    try {
      const stored: unknown = JSON.parse(window.localStorage.getItem(STARS) ?? "[]");
      if (Array.isArray(stored)) setStars(stored.map(String));
    } catch {
      /* stars simply start empty */
    }
  }, []);

  const toggle = (token: string) => {
    const next = stars.includes(token) ? stars.filter((item) => item !== token) : [...stars, token];
    setStars(next);
    try {
      window.localStorage.setItem(STARS, JSON.stringify(next));
    } catch {
      /* starred until the page reloads */
    }
  };

  const wanted = search.trim().toLowerCase();
  const rows = day.shares.filter(
    (row: ScannerShareDay) =>
      (!wanted || row.script_name.toLowerCase().includes(wanted) || row.instrument_token.toLowerCase().includes(wanted)) &&
      (!onlyStarred || stars.includes(row.instrument_token)),
  );

  return (
    <div className="mt-5">
      <Panel title="Where each share stood" note="The best moment any strategy saw in it that day. Stars are kept in this browser only.">
        <div className="flex flex-wrap items-center gap-3 px-5 py-3">
          <SearchBox value={search} onChange={setSearch} label="Find a share" />
          <label className="flex items-center gap-2 text-xs" style={{ color: "var(--muted)" }}>
            <input type="checkbox" checked={onlyStarred} onChange={(event) => setOnlyStarred(event.target.checked)} />
            Starred only
          </label>
        </div>
        {rows.length ? (
          <Paged rows={rows} label="shares">
            {(visible) => (
              <div className="table-scroll">
                <table className="terminal-table" aria-label="Shares this day">
                  <thead>
                    <tr>
                      <th>Share</th>
                      <th>Last price</th>
                      <th>Signals</th>
                      <th>Scored</th>
                      <th>Best score</th>
                      <th>Strategy and time</th>
                      <th>Outcome of that moment</th>
                      <th>Skipped (data)</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {visible.map((row) => {
                      const starred = stars.includes(row.instrument_token);
                      return (
                        <tr key={row.instrument_token} onClick={() => row.best && onInspect(row.best.id)}>
                          <td>
                            <strong title={row.instrument_token}>{row.script_name}</strong>
                          </td>
                          <td className="numeric">{row.last_close === null ? "—" : `₹${formatPrice(row.last_close)}`}</td>
                          <td className="numeric">{row.accepted || "—"}</td>
                          <td className="numeric">{row.scored || "—"}</td>
                          <td className="numeric">{row.best?.score ?? "—"}</td>
                          <td>{row.best ? `${row.best.strategy_name}, ${timeOf(row.best.at)}` : "No setup"}</td>
                          <td>{row.best?.reason ?? "—"}</td>
                          <td className="numeric">{row.blocked || "—"}</td>
                          <td>
                            <div className="flex gap-2">
                              <button
                                type="button"
                                className="icon-button"
                                aria-pressed={starred}
                                aria-label={`Star ${row.script_name}`}
                                style={{ color: starred ? "var(--warning)" : "var(--muted)" }}
                                onClick={(event) => {
                                  event.stopPropagation();
                                  toggle(row.instrument_token);
                                }}
                              >
                                <Star className="h-3.5 w-3.5" fill={starred ? "currentColor" : "none"} />
                              </button>
                              {row.best && <Details name={row.script_name} onClick={() => onInspect(row.best!.id)} />}
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </Paged>
        ) : (
          <Empty>No share matches.</Empty>
        )}
      </Panel>
    </div>
  );
}

function SearchBox({ value, onChange, label }: { value: string; onChange: (next: string) => void; label: string }) {
  return (
    <label className="relative min-w-[12rem] flex-1 sm:max-w-xs">
      <Search className="absolute left-3 top-3 h-4 w-4" style={{ color: "var(--muted)" }} />
      <span className="sr-only">{label}</span>
      <input
        className="field-input pl-9"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={label}
        maxLength={60}
      />
    </label>
  );
}

function Checks({ day, onOpen }: { day: ScannerDay; onOpen: (row: ScannerEvaluation) => void }) {
  const [status, setStatus] = useState("");
  const [strategy, setStrategy] = useState("");
  const [search, setSearch] = useState("");
  const [typed, setTyped] = useState("");
  const [hideOutside, setHideOutside] = useState(true);
  const [sort, setSort] = useState<"time" | "score">("time");
  const [page, setPage] = useState(1);
  const [size, setSize] = useState(25);
  const [result, setResult] = useState<EvaluationPage | null>(null);
  const [error, setError] = useState("");
  const latest = useRef(0);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setSearch(typed.trim());
      setPage(1);
    }, 300);
    return () => window.clearTimeout(timer);
  }, [typed]);

  useEffect(() => {
    const request = ++latest.current;
    void api
      .evaluationPage({
        session_date: day.session_date,
        status: status || undefined,
        strategy_id: strategy || undefined,
        search: search || undefined,
        hide_outside: hideOutside ? "true" : undefined,
        sort,
        limit: String(size),
        offset: String((page - 1) * size),
      })
      .then((next) => {
        if (request !== latest.current) return;
        setResult(next);
        setError("");
      })
      .catch((caught: unknown) => {
        if (request === latest.current) setError(caught instanceof Error ? caught.message : "Could not load the checks");
      });
  }, [day.session_date, status, strategy, search, hideOutside, sort, page, size]);

  const first = <T,>(set: (value: T) => void) => (value: T) => {
    set(value);
    setPage(1);
  };

  return (
    <div className="mt-5">
      <Panel title="Every check" note="One row is one share looked at by one strategy on one candle.">
        <div className="flex flex-wrap items-center gap-3 px-5 py-3">
          <SearchBox value={typed} onChange={setTyped} label="Find a share" />
          <select
            aria-label="Evaluation state"
            className="field-input w-auto py-2"
            value={status}
            onChange={(event) => first(setStatus)(event.target.value)}
          >
            <option value="">Any result</option>
            <option value="ACCEPTED">Signal</option>
            <option value="WATCHING">Watching</option>
            <option value="REJECTED">Rejected</option>
          </select>
          <select
            aria-label="Strategy"
            className="field-input w-auto py-2"
            value={strategy}
            onChange={(event) => first(setStrategy)(event.target.value)}
          >
            <option value="">All strategies</option>
            {day.strategies.map((item) => (
              <option key={item.strategy_id} value={item.strategy_id}>
                {item.strategy_name}
              </option>
            ))}
            <option value="data-quality">Skipped for missing candles</option>
          </select>
          <select
            aria-label="Order"
            className="field-input w-auto py-2"
            value={sort}
            onChange={(event) => first(setSort)(event.target.value as "time" | "score")}
          >
            <option value="time">Newest first</option>
            <option value="score">Best score first</option>
          </select>
          <label className="flex items-center gap-2 text-xs" style={{ color: "var(--muted)" }}>
            <input
              type="checkbox"
              checked={hideOutside}
              onChange={(event) => first(setHideOutside)(event.target.checked)}
            />
            Hide checks outside trading hours
          </label>
        </div>
        {error ? (
          <Empty>{error}</Empty>
        ) : !result ? (
          <Empty>Loading…</Empty>
        ) : result.items.length ? (
          <div className="table-scroll">
            <table className="terminal-table" aria-label="Every check">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Share</th>
                  <th>Strategy</th>
                  <th>Result</th>
                  <th>Why</th>
                  <th>Side</th>
                  <th>Score</th>
                  <th>Price</th>
                  <th>Volume</th>
                  <th>Data</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {result.items.map((row) => (
                  <tr key={row.id} onClick={() => onOpen(row)}>
                    <td className="muted-cell">{formatIstTimestamp(row.candle_opened_at)}</td>
                    <td>
                      <strong title={row.instrument_token}>{row.script_name ?? row.instrument_token}</strong>
                    </td>
                    <td>{row.strategy_name}</td>
                    <td>
                      <span
                        className={`status-pill ${
                          row.status === "ACCEPTED" ? "status-good" : row.status === "REJECTED" ? "status-bad" : "status-watch"
                        }`}
                      >
                        {row.status === "ACCEPTED" ? "Signal" : titleCase(row.status.toLowerCase())}
                      </span>
                    </td>
                    <td>{row.reason}</td>
                    <td>
                      <Side side={row.side} />
                    </td>
                    <td className="numeric">{row.score || "—"}</td>
                    <td className="numeric">₹{formatPrice(row.candle_close)}</td>
                    <td className="numeric">{count(row.candle_volume)}</td>
                    <td>
                      <span className={`status-pill ${statusTone(row.data_quality_state)}`}>{row.data_quality_state}</span>
                    </td>
                    <td>
                      <Details name={row.script_name ?? row.instrument_token} onClick={() => onOpen(row)} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty>No check matches.</Empty>
        )}
        {result && !error && (
          <Pager
            total={result.total}
            page={page}
            size={size}
            onPage={setPage}
            onSize={(next) => first(setSize)(next)}
            label="checks"
          />
        )}
      </Panel>
    </div>
  );
}
