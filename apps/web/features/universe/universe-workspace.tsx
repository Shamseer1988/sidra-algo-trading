"use client";

import { RefreshCw, Search } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { api, type UniverseEntry, type UniverseSummary } from "../../components/api";
import { Paged } from "../../components/paged";
import { formatIstTimestamp } from "../../lib/formatting";

/** Which shares the scanner may trade, and why each of the others may not. */
export function UniverseWorkspace({
  canOperate,
  onMessage,
}: {
  canOperate: boolean;
  onMessage: (message: string) => void;
}) {
  const [entries, setEntries] = useState<UniverseEntry[]>([]);
  const [summary, setSummary] = useState<UniverseSummary | null>(null);
  const [earlier, setEarlier] = useState(false);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [show, setShow] = useState<"selected" | "spare" | "out">("selected");
  const [search, setSearch] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [rows, meta] = await Promise.all([api.universe(), api.universeSummary()]);
      if (rows.length) {
        setEntries(rows);
        setSummary(meta);
        setEarlier(false);
        return;
      }
      // Nothing built today (a holiday, or before 9:16 AM): show the last scanned day's list instead of a blank.
      const date = (await api.scannerDay().catch(() => null))?.sessions[0];
      const [oldRows, oldMeta] = date
        ? await Promise.all([api.universe(date), api.universeSummary(date)])
        : [rows, meta];
      setEntries(oldRows);
      setSummary(oldMeta);
      setEarlier(oldRows.length > 0);
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Could not load the scan universe");
    } finally {
      setLoading(false);
    }
  }, [onMessage]);

  useEffect(() => {
    void load();
  }, [load]);

  const rebuild = async () => {
    setRefreshing(true);
    try {
      await api.refreshUniverse();
      await load();
      onMessage("Scan universe rebuilt.");
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Could not rebuild the scan universe");
    } finally {
      setRefreshing(false);
    }
  };

  const groups = {
    selected: entries.filter((entry) => entry.selected),
    spare: entries.filter((entry) => entry.eligible && !entry.selected),
    out: entries.filter((entry) => !entry.eligible),
  };
  const wanted = search.trim().toLowerCase();
  const rows = groups[show].filter(
    (entry) =>
      !wanted || entry.script_name.toLowerCase().includes(wanted) || entry.instrument_token.toLowerCase().includes(wanted),
  );

  return (
    <section>
      <div className="page-toolbar">
        <div>
          <p className="eyebrow">Which shares may trade</p>
          <h2 className="page-title">Scan universe</h2>
          <p className="page-copy">
            Before the first entry each morning, the watchlist is ranked and the shares that fit your limits are chosen.
            {summary && !summary.enabled
              ? " The ranking is off, so this is for information and the whole watchlist is scanned."
              : " Only the chosen shares can give a signal."}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button className="secondary-button" onClick={() => void load()} disabled={loading}>
            <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
            Refresh
          </button>
          {canOperate && (
            <button className="secondary-button" onClick={() => void rebuild()} disabled={refreshing}>
              Rebuild now
            </button>
          )}
        </div>
      </div>

      {earlier && summary && (
        <p role="status" className="glass-notice mt-5 rounded-md p-4 text-sm" style={{ color: "var(--text-body)" }}>
          No list has been built today. This is the one from {summary.session_date}.
        </p>
      )}

      {summary && (
        <dl className="mt-5 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <Stat label="Chosen" value={`${summary.selected} of ${summary.universe_size}`} note="shares allowed to signal" />
          <Stat label="Fit your limits" value={String(summary.eligible)} note={`of ${summary.total_candidates} on the watchlist`} />
          <Stat label="Left out" value={String(groups.out.length)} note="the reason is listed for each" />
          <Stat
            label="Built"
            value={summary.last_built_at ? formatIstTimestamp(summary.last_built_at) : "Not built yet"}
            note={summary.enabled ? "ranking is on" : "ranking is off"}
          />
        </dl>
      )}

      {loading && !entries.length ? (
        <div className="skeleton mt-6 h-40" />
      ) : !entries.length ? (
        <p className="panel mt-6 p-8 text-center text-sm" style={{ color: "var(--muted)" }}>
          No list has been built yet. It is built before the first entry of a trading day.
        </p>
      ) : (
        <article className="panel mt-6 overflow-hidden">
          <div className="flex flex-wrap items-center gap-3 px-5 py-4">
            <div role="group" aria-label="Which shares" className="flex flex-wrap gap-2">
              {([
                ["selected", "Chosen"],
                ["spare", "Fit, not chosen"],
                ["out", "Left out"],
              ] as const).map(([value, label]) => (
                <button
                  key={value}
                  type="button"
                  aria-pressed={show === value}
                  className={`${show === value ? "primary-button" : "secondary-button"} px-3 py-2 text-xs`}
                  onClick={() => setShow(value)}
                >
                  {label} ({groups[value].length})
                </button>
              ))}
            </div>
            <label className="relative min-w-[12rem] flex-1 sm:max-w-xs">
              <Search className="absolute left-3 top-3 h-4 w-4" style={{ color: "var(--muted)" }} />
              <span className="sr-only">Find a share</span>
              <input
                className="field-input pl-9"
                placeholder="Find a share"
                value={search}
                onChange={(event) => setSearch(event.target.value)}
              />
            </label>
          </div>
          {rows.length ? (
            <Paged key={show} rows={rows} label="shares">
              {(visible) => (
                <div className="table-scroll">{show === "out" ? <LeftOut rows={visible} /> : <Ranked rows={visible} />}</div>
              )}
            </Paged>
          ) : (
            <p className="p-6 text-sm" style={{ color: "var(--muted)" }}>
              No share here.
            </p>
          )}
          {show !== "out" && (
            <p className="px-5 py-3 text-xs" style={{ color: "var(--muted)" }}>
              Each score is out of 100. Liquidity: daily turnover compared with the other shares. Movement: how close its
              usual daily move is to the preferred range. Gap: the opening gap, small to medium scoring best. Trend: how
              far it moved over the last five sessions, up or down.
            </p>
          )}
        </article>
      )}
    </section>
  );
}

function Stat({ label, value, note }: { label: string; value: string; note: string }) {
  return (
    <div className="panel p-5">
      <dt className="text-xs" style={{ color: "var(--muted)" }}>
        {label}
      </dt>
      <dd className="mt-1 text-xl font-semibold" style={{ color: "var(--foreground)" }}>
        {value}
      </dd>
      <dd className="mt-1 text-xs" style={{ color: "var(--muted)" }}>
        {note}
      </dd>
    </div>
  );
}

function Ranked({ rows }: { rows: UniverseEntry[] }) {
  const percent = (value: number | undefined) => (value === undefined ? "—" : `${value.toFixed(2)}%`);
  return (
    <table className="terminal-table" aria-label="Ranked shares">
      <thead>
        <tr>
          <th>Rank</th>
          <th>Share</th>
          <th>Score</th>
          <th>Liquidity</th>
          <th>Movement</th>
          <th>Gap</th>
          <th>Trend</th>
          <th>Usual daily move</th>
          <th>Opening gap</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((entry) => (
          <tr key={entry.instrument_token}>
            <td className="numeric">{entry.rank}</td>
            <td>
              <strong title={entry.instrument_token}>{entry.script_name}</strong>
            </td>
            <td className="numeric">{entry.score.toFixed(1)}</td>
            <td className="numeric">{entry.liquidity_score.toFixed(0)}</td>
            <td className="numeric">{entry.volatility_score.toFixed(0)}</td>
            <td className="numeric">{entry.gap_score.toFixed(0)}</td>
            <td className="numeric">{entry.trend_score.toFixed(0)}</td>
            <td className="numeric">{percent(entry.metrics.atr_percent)}</td>
            <td className="numeric">{percent(entry.metrics.gap_percent)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function LeftOut({ rows }: { rows: UniverseEntry[] }) {
  return (
    <table className="terminal-table" aria-label="Shares left out">
      <thead>
        <tr>
          <th>Share</th>
          <th>Why it was left out</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((entry) => (
          <tr key={entry.instrument_token}>
            <td>
              <strong title={entry.instrument_token}>{entry.script_name}</strong>
            </td>
            <td>{entry.rejection_reason ?? "No reason recorded"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
