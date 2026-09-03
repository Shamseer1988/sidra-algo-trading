"use client";

import {
  Activity,
  Bookmark,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ChevronUp,
  Eye,
  Radio,
  RefreshCw,
  Search,
  Star,
  TriangleAlert,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import {
  api,
  type DataQuality,
  type MarketCandle,
  type SafetyStatus,
  type ScannerEvaluation,
  type ScannerStatus,
} from "../../components/api";
import { formatIstTimestamp, formatPrice, statusTone, titleCase } from "../../lib/formatting";
import { resolveScriptName } from "../../lib/instruments";

type SortKey = "instrument" | "score" | "updated" | "quality";
type SavedView = "ALL" | "ACCEPTED" | "ATTENTION" | "WATCHLIST";

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
  const [items, setItems] = useState<ScannerEvaluation[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [state, setState] = useState<"ALL" | ScannerEvaluation["status"]>("ALL");
  const [quality, setQuality] = useState("ALL");
  const [sort, setSort] = useState<{ key: SortKey; asc: boolean }>({
    key: "updated",
    asc: false,
  });
  const [view, setView] = useState<SavedView>("ALL");
  const [watchlist, setWatchlist] = useState<string[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  // Pagination state
  const [pageSize, setPageSize] = useState(10);
  const [page, setPage] = useState(1);

  const load = () => {
    setLoading(true);
    void api
      .evaluations(150)
      .then(setItems)
      .catch((loadError: unknown) =>
        setError(
          loadError instanceof Error
            ? loadError.message
            : "Could not load scanner evaluations",
        ),
      )
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    const stored = window.localStorage.getItem("sidra.scanner.watchlist");
    if (stored) {
      try {
        setWatchlist(JSON.parse(stored) as string[]);
      } catch {
        window.localStorage.removeItem("sidra.scanner.watchlist");
      }
    }
  }, []);

  useEffect(() => {
    load();
  }, [refreshKey]);

  const persistWatchlist = (next: string[]) => {
    setWatchlist(next);
    window.localStorage.setItem("sidra.scanner.watchlist", JSON.stringify(next));
  };

  const toggleWatchlist = (instrument: string) =>
    persistWatchlist(
      watchlist.includes(instrument)
        ? watchlist.filter((value) => value !== instrument)
        : [...watchlist, instrument],
    );

  const filtered = useMemo(
    () =>
      items
        .filter((item) => {
          const script = resolveScriptName(item.instrument_token).toLowerCase();
          const token = item.instrument_token.toLowerCase();
          const strat = item.strategy_name.toLowerCase();
          const q = query.toLowerCase();
          const matchesQuery =
            script.includes(q) || token.includes(q) || strat.includes(q);
          const matchesState = state === "ALL" || item.status === state;
          const matchesQuality = quality === "ALL" || item.data_quality_state === quality;
          const matchesView =
            view === "ALL" ||
            (view === "ACCEPTED" && item.status === "ACCEPTED") ||
            (view === "ATTENTION" &&
              (item.status === "REJECTED" ||
                ["INVALID", "STALE", "MISSING"].includes(item.data_quality_state))) ||
            (view === "WATCHLIST" && watchlist.includes(item.instrument_token));
          return matchesQuery && matchesState && matchesQuality && matchesView;
        })
        .sort((left, right) => {
          const leftValue =
            sort.key === "instrument"
              ? left.instrument_token
              : sort.key === "score"
              ? left.score
              : sort.key === "quality"
              ? left.data_quality_state
              : left.candle_opened_at;
          const rightValue =
            sort.key === "instrument"
              ? right.instrument_token
              : sort.key === "score"
              ? right.score
              : sort.key === "quality"
              ? right.data_quality_state
              : right.candle_opened_at;
          return (
            (leftValue > rightValue ? 1 : leftValue < rightValue ? -1 : 0) *
            (sort.asc ? 1 : -1)
          );
        }),
    [items, quality, query, sort, state, view, watchlist],
  );

  const totalPages = Math.max(1, Math.ceil(filtered.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  const paginated = useMemo(() => {
    const start = (currentPage - 1) * pageSize;
    return filtered.slice(start, start + pageSize);
  }, [filtered, currentPage, pageSize]);

  const selected = filtered.find((item) => item.id === selectedId) ?? paginated[0];

  const sortBy = (key: SortKey) =>
    setSort((current) => ({
      key,
      asc: current.key === key ? !current.asc : key === "instrument" || key === "quality",
    }));

  return (
    <section>
      <div className="page-toolbar">
        <div>
          <p className="eyebrow">Scanner operations</p>
          <h2 className="page-title">Scanner workspace</h2>
          <p className="page-copy">
            Completed-candle strategy evaluations, including accepted, watching, and rejected setups. Live execution remains unavailable.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            className="secondary-button"
            onClick={() => {
              onRefresh();
              load();
            }}
          >
            <RefreshCw className="h-4 w-4" />
            Refresh scanner
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
            <button
              className="secondary-button"
              disabled={scanner.status === "STOPPED"}
              onClick={onStop}
            >
              <TriangleAlert className="h-4 w-4" />
              Stop
            </button>
          )}
        </div>
      </div>

      <ScannerStatusStrip
        scanner={scanner}
        safety={safety}
        dataQuality={dataQuality}
      />

      <div className="scanner-filter-bar">
        <label className="relative min-w-[12rem] flex-1">
          <Search className="absolute left-3 top-3 h-4 w-4 text-slate-400" />
          <span className="sr-only">Search scanner evaluations</span>
          <input
            className="field-input pl-9"
            value={query}
            onChange={(event) => {
              setQuery(event.target.value);
              setPage(1);
            }}
            placeholder="Search symbol, script, or strategy"
          />
        </label>
        <select
          className="field-input w-auto"
          aria-label="Evaluation state"
          value={state}
          onChange={(event) => {
            setState(event.target.value as typeof state);
            setPage(1);
          }}
        >
          <option value="ALL">All states</option>
          <option value="ACCEPTED">Accepted</option>
          <option value="WATCHING">Watching</option>
          <option value="REJECTED">Rejected</option>
        </select>
        <select
          className="field-input w-auto"
          aria-label="Data quality"
          value={quality}
          onChange={(event) => {
            setQuality(event.target.value);
            setPage(1);
          }}
        >
          <option value="ALL">All quality</option>
          <option value="GOOD">Good</option>
          <option value="DEGRADED">Degraded</option>
          <option value="STALE">Stale</option>
          <option value="INVALID">Invalid</option>
          <option value="MISSING">Missing</option>
        </select>
        <label className="flex items-center gap-2 text-xs text-slate-500 dark:text-slate-400">
          <Bookmark className="h-4 w-4" />
          <select
            className="field-input w-auto"
            aria-label="Saved scanner view"
            value={view}
            onChange={(event) => {
              setView(event.target.value as SavedView);
              setPage(1);
            }}
          >
            <option value="ALL">All evaluations</option>
            <option value="ACCEPTED">Accepted setups</option>
            <option value="ATTENTION">Needs attention</option>
            <option value="WATCHLIST">My watchlist</option>
          </select>
        </label>
      </div>

      {error && (
        <div className="glass-notice mt-4 rounded-md p-4 text-sm text-rose-300">
          {error}
        </div>
      )}

      <div className="mt-4 grid gap-5 2xl:grid-cols-[minmax(0,1.8fr)_minmax(21rem,.8fr)]">
        <ScannerTable
          items={paginated}
          totalCount={filtered.length}
          loading={loading}
          selectedId={selected?.id}
          sort={sort}
          pageSize={pageSize}
          currentPage={currentPage}
          totalPages={totalPages}
          onPageChange={setPage}
          onPageSizeChange={(size) => {
            setPageSize(size);
            setPage(1);
          }}
          onSort={sortBy}
          onSelect={setSelectedId}
          watchlist={watchlist}
          onWatchlist={toggleWatchlist}
        />
        {selected ? (
          <SetupInspector evaluation={selected} />
        ) : (
          <EmptyInspector />
        )}
      </div>
    </section>
  );
}

function ScannerStatusStrip({
  scanner,
  safety,
  dataQuality,
}: {
  scanner: ScannerStatus;
  safety: SafetyStatus;
  dataQuality: DataQuality[];
}) {
  const good = dataQuality.filter((item) => item.allows_signals).length;
  return (
    <div className="mt-5 grid gap-3 sm:grid-cols-3">
      <article className="glass-inset rounded-md p-4">
        <p className="text-[10px] font-bold uppercase tracking-[.15em] text-slate-500 dark:text-slate-400">
          Worker
        </p>
        <p className="mt-2 flex items-center gap-2 text-sm font-semibold">
          <Radio className="h-4 w-4 text-emerald-500 dark:text-emerald-400" />
          {titleCase(scanner.status)}
        </p>
        <p className="mt-2 text-xs text-slate-500 dark:text-slate-400">
          {scanner.worker_restart_count
            ? `${scanner.worker_restart_count} supervised restart(s)`
            : "No restarts reported"}
        </p>
      </article>
      <article className="glass-inset rounded-md p-4">
        <p className="text-[10px] font-bold uppercase tracking-[.15em] text-slate-500 dark:text-slate-400">
          Data quality
        </p>
        <p className="mt-2 text-sm font-semibold">
          {good}/{dataQuality.length} signal eligible
        </p>
        <p className="mt-2 text-xs text-slate-500 dark:text-slate-400">Only valid, fresh snapshots can qualify.</p>
      </article>
      <article className="glass-inset rounded-md p-4">
        <p className="text-[10px] font-bold uppercase tracking-[.15em] text-slate-500 dark:text-slate-400">
          Safety gate
        </p>
        <p
          className={`mt-2 text-sm font-semibold ${
            safety.emergency_stop_active
              ? "text-rose-600 dark:text-rose-400"
              : "text-emerald-600 dark:text-emerald-400"
          }`}
        >
          {safety.emergency_stop_active ? "Emergency stop active" : "Paper scan permitted"}
        </p>
        <p className="mt-2 text-xs text-slate-500 dark:text-slate-400">No broker order path exists.</p>
      </article>
    </div>
  );
}

function ScannerTable({
  items,
  totalCount,
  loading,
  selectedId,
  sort,
  pageSize,
  currentPage,
  totalPages,
  onPageChange,
  onPageSizeChange,
  onSort,
  onSelect,
  watchlist,
  onWatchlist,
}: {
  items: ScannerEvaluation[];
  totalCount: number;
  loading: boolean;
  selectedId?: string;
  sort: { key: SortKey; asc: boolean };
  pageSize: number;
  currentPage: number;
  totalPages: number;
  onPageChange: (page: number) => void;
  onPageSizeChange: (size: number) => void;
  onSort: (key: SortKey) => void;
  onSelect: (id: string) => void;
  watchlist: string[];
  onWatchlist: (instrument: string) => void;
}) {
  const header = (label: string, key?: SortKey) => (
    <th key={label} className="py-3 px-3">
      <button className="scanner-sort text-xs font-semibold" onClick={() => key && onSort(key)}>
        {label}
        {key &&
          (sort.key === key ? (
            sort.asc ? (
              <ChevronUp className="h-3 w-3 text-emerald-500" />
            ) : (
              <ChevronDown className="h-3 w-3 text-emerald-500" />
            )
          ) : null)}
      </button>
    </th>
  );

  return (
    <article className="panel overflow-hidden flex flex-col">
      {/* Table Header Controls */}
      <div className="flex flex-col gap-3 border-b border-slate-700/30 dark:border-slate-800/80 p-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <p className="eyebrow">Evaluation tape</p>
          <h3 className="mt-0.5 text-base font-semibold">
            {loading ? "Loading scanner data..." : `${totalCount} visible evaluations`}
          </h3>
        </div>

        {/* Rows per page selector */}
        <div className="flex items-center gap-2 text-xs text-slate-500 dark:text-slate-400">
          <span>Show:</span>
          <select
            aria-label="Rows per page"
            value={pageSize}
            onChange={(e) => onPageSizeChange(Number(e.target.value))}
            className="field-input w-20 py-1 text-xs"
          >
            <option value={10}>10</option>
            <option value={25}>25</option>
            <option value={50}>50</option>
            <option value={100}>100</option>
            <option value={150}>150</option>
          </select>
        </div>
      </div>

      {loading ? (
        <div className="space-y-2 p-5">
          {Array.from({ length: 8 }, (_, index) => (
            <div key={index} className="skeleton h-12" />
          ))}
        </div>
      ) : items.length ? (
        <div className="table-scroll max-h-[580px] overflow-auto">
          <table className="terminal-table scanner-table w-full text-xs">
            <thead className="sticky top-0 z-10">
              <tr>
                {header("Symbol / Script", "instrument")}
                {header("Last close")}
                {header("Change")}
                {header("Volume")}
                {header("RVOL")}
                {header("VWAP")}
                {header("EMA")}
                {header("Rel. str.")}
                {header("Regime")}
                {header("Setup")}
                {header("Direction")}
                {header("Score", "score")}
                {header("Quality", "quality")}
                {header("Updated", "updated")}
                {header("Action")}
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <ScannerRow
                  key={item.id}
                  item={item}
                  selected={selectedId === item.id}
                  watched={watchlist.includes(item.instrument_token)}
                  onSelect={onSelect}
                  onWatchlist={onWatchlist}
                />
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="empty-inset m-5 text-sm">
          No evaluations match the current filters. The tape fills only after the scanner processes completed candles.
        </div>
      )}

      {/* Table Pagination Footer */}
      {totalCount > 0 && (
        <div className="flex flex-col items-center justify-between gap-3 border-t border-slate-700/30 dark:border-slate-800/80 p-3.5 text-xs sm:flex-row mt-auto">
          <div className="text-slate-500 dark:text-slate-400">
            Showing{" "}
            <strong>
              {(currentPage - 1) * pageSize + 1}
            </strong>{" "}
            to{" "}
            <strong>
              {Math.min(currentPage * pageSize, totalCount)}
            </strong>{" "}
            of <strong>{totalCount}</strong> evaluations
          </div>

          <div className="flex items-center gap-2">
            <button
              disabled={currentPage <= 1}
              onClick={() => onPageChange(Math.max(1, currentPage - 1))}
              className="secondary-button py-1 px-2.5 text-xs disabled:opacity-40"
            >
              <ChevronLeft className="h-3.5 w-3.5" />
              Previous
            </button>

            <span className="px-2 font-medium">
              Page {currentPage} of {totalPages}
            </span>

            <button
              disabled={currentPage >= totalPages}
              onClick={() => onPageChange(Math.min(totalPages, currentPage + 1))}
              className="secondary-button py-1 px-2.5 text-xs disabled:opacity-40"
            >
              Next
              <ChevronRight className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>
      )}
    </article>
  );
}

function ScannerRow({
  item,
  selected,
  watched,
  onSelect,
  onWatchlist,
}: {
  item: ScannerEvaluation;
  selected: boolean;
  watched: boolean;
  onSelect: (id: string) => void;
  onWatchlist: (instrument: string) => void;
}) {
  const script = resolveScriptName(item.instrument_token);
  const snapshot = item.indicator_snapshot;
  const volume = valueAt(snapshot, "volume.relative_volume");
  const relative = valueAt(snapshot, "relative_strength.relative_strength_percent");
  const regime = valueAt(snapshot, "nifty_regime.regime");

  return (
    <tr
      className={selected ? "table-selected" : ""}
      onClick={() => onSelect(item.id)}
    >
      <td className="py-3 px-3">
        <div className="flex items-center gap-1.5">
          <span className="rounded bg-emerald-500/15 px-2 py-0.5 text-xs font-bold text-emerald-600 dark:text-emerald-300 border border-emerald-500/25">
            {script}
          </span>
        </div>
        <strong className="block font-mono text-[11px] opacity-80">{item.instrument_token}</strong>
        <span className="text-[10px] opacity-70">
          {item.strategy_name} v{item.strategy_version}
        </span>
      </td>
      <td className="numeric font-mono py-3 px-3">₹{formatPrice(item.candle_close)}</td>
      <td className="muted-cell py-3 px-3">—</td>
      <td className="numeric font-mono py-3 px-3">{item.candle_volume.toLocaleString("en-IN")}</td>
      <td className="numeric font-mono py-3 px-3">{numberText(volume)}</td>
      <td className="numeric font-mono py-3 px-3">{numberText(valueAt(snapshot, "vwap"))}</td>
      <td className="numeric font-mono py-3 px-3">{numberText(valueAt(snapshot, "ema_fast"))}</td>
      <td className="numeric font-mono py-3 px-3">{numberText(relative, "%")}</td>
      <td className="py-3 px-3">{typeof regime === "string" ? titleCase(regime) : "—"}</td>
      <td className="py-3 px-3">
        <strong>{titleCase(item.decision_state)}</strong>
        <span className="text-[11px] opacity-70 block">{item.reason}</span>
      </td>
      <td className="py-3 px-3">
        {item.side ? (
          <span
            className={`status-pill ${
              item.side === "LONG" ? "status-good" : "status-bad"
            }`}
          >
            {item.side}
          </span>
        ) : (
          "—"
        )}
      </td>
      <td className="numeric font-mono py-3 px-3 font-semibold">{item.score}/100</td>
      <td className="py-3 px-3">
        <span className={`status-pill ${statusTone(item.data_quality_state)}`}>
          {item.data_quality_state}
        </span>
      </td>
      <td className="muted-cell py-3 px-3">{formatIstTimestamp(item.candle_opened_at)}</td>
      <td className="py-3 px-3">
        <div className="flex gap-1">
          <button
            className="icon-button"
            title="Inspect setup"
            aria-label={`Inspect ${item.instrument_token}`}
            onClick={(event) => {
              event.stopPropagation();
              onSelect(item.id);
            }}
          >
            <Eye className="h-3.5 w-3.5" />
          </button>
          <button
            className={`icon-button ${
              watched ? "text-amber-500 dark:text-amber-300" : "text-slate-400"
            }`}
            title="Toggle watchlist"
            aria-label={`Toggle ${item.instrument_token} watchlist`}
            onClick={(event) => {
              event.stopPropagation();
              onWatchlist(item.instrument_token);
            }}
          >
            <Star className="h-3.5 w-3.5" fill={watched ? "currentColor" : "none"} />
          </button>
        </div>
      </td>
    </tr>
  );
}

function SetupInspector({ evaluation }: { evaluation: ScannerEvaluation }) {
  const script = resolveScriptName(evaluation.instrument_token);
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
    <aside className="panel h-fit p-5">
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

function EmptyInspector() {
  return (
    <aside className="panel p-6">
      <p className="eyebrow">Setup inspector</p>
      <h3 className="mt-1 text-lg font-semibold">Select an evaluation</h3>
      <p className="mt-3 text-sm leading-6 opacity-70">
        When the scanner processes a completed candle, its strategy state, score, and rejection reasons can be inspected here.
      </p>
    </aside>
  );
}
