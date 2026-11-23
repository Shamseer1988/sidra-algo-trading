"use client";

import { ArrowUpRight, Landmark, NotebookPen, Trophy } from "lucide-react";
import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";

import { api, type HistoryDay, type HistoryOverview, type PaperDay, type StrategyResult, type TradingControls } from "../../components/api";
import { istDate, strategyLabel } from "../../lib/strategy-names";
import { DayStrip } from "./day-strip";
import { percent, rupees, toNumber } from "../history/money";

/**
 * The day at a glance: live money, paper, and which strategy is doing the work.
 *
 * Live and paper sit beside each other and are never added. Every total is the
 * server's. Nothing here asks the broker for anything: live figures come from
 * our record of its fills, so a dashboard left open all day costs none of the
 * request budget that placing an order draws on.
 */

const REFRESH_MS = 60_000;
/** Fewer closed trades than this and a ranking is mostly luck; the row says so. */
const FEW_TRADES = 30;

type Glance = {
  live: HistoryOverview | null;
  paper: PaperDay | null;
  today: StrategyResult[] | null;
  month: StrategyResult[] | null;
  days: HistoryDay[] | null;
};

const settled = <T,>(result: PromiseSettledResult<T>): T | null =>
  result.status === "fulfilled" ? result.value : null;

function ink(value: number | null): string {
  if (value === null || value === 0) return "var(--foreground)";
  return value > 0 ? "var(--positive)" : "var(--negative)";
}

export function DayGlance({
  revision,
  controls,
  onRefresh,
  onOpenOrders,
  onOpenHistory,
  onOpenReports,
}: {
  revision: number;
  controls: TradingControls;
  onRefresh: () => void;
  onOpenOrders: () => void;
  onOpenHistory: () => void;
  onOpenReports: () => void;
}) {
  const [glance, setGlance] = useState<Glance | null>(null);
  const [period, setPeriod] = useState<"today" | "month">("today");
  const busy = useRef(false);

  const load = useCallback(async () => {
    if (busy.current) return;
    busy.current = true;
    const today = istDate();
    const day = { from_date: today, to_date: today };
    const month = { from_date: istDate(29), to_date: today };
    // Settled one by one: a slow report must not blank the live figure.
    const [live, paper, todays, months, days] = await Promise.allSettled([
      api.historyOverview({ ...day, mode: "LIVE" }),
      api.paperDay({}),
      api.historyStrategies(day),
      api.historyStrategies(month),
      api.historyDaily({ ...month, mode: "LIVE" }),
    ]);
    setGlance({
      live: settled(live),
      paper: settled(paper),
      today: settled(todays),
      month: settled(months),
      days: settled(days),
    });
    busy.current = false;
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => {
      if (!document.hidden) void load();
    }, REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [load, revision]);

  if (!glance) {
    return (
      <div className="space-y-4">
        <div className="skeleton h-24" />
        <div className="grid gap-4 lg:grid-cols-3">
          {[0, 1, 2].map((item) => (
            <div key={item} className="skeleton h-44" />
          ))}
        </div>
      </div>
    );
  }

  const { live, paper } = glance;
  const rows = (period === "today" ? glance.today : glance.month) ?? null;
  const ranked = rows ? [...rows].sort((a, b) => Number(b.net_pnl) - Number(a.net_pnl)) : null;
  const leader = (glance.today ?? []).reduce<StrategyResult | null>(
    (best, row) => (best === null || Number(row.net_pnl) > Number(best.net_pnl) ? row : best),
    null,
  );

  return (
    <div className="space-y-4" data-testid="day-glance">
      <DayStrip live={live} controls={controls} tick={glance} onRefresh={onRefresh} />
      <div className="grid gap-4 lg:grid-cols-3">
        <Card icon={<Landmark className="h-4 w-4" />} title="Live today" tag="Real money" onOpen={onOpenHistory}>
          {live ? (
            <>
              <Hero value={toNumber(live.net_pnl)} text={rupees(live.net_pnl, { signed: true })} label="net" />
              <Facts
                items={[
                  ["Trades", String(live.trades)],
                  ["Won / lost", `${live.wins} / ${live.losses}`],
                  ["Realised gross", rupees(live.gross_pnl, { signed: true })],
                  ["Charges (est.)", rupees(live.charges)],
                ]}
              />
              {live.open_trades > 0 && <Note>{live.open_trades} still open, not finished.</Note>}
            </>
          ) : (
            <Missing />
          )}
        </Card>

        <Card icon={<NotebookPen className="h-4 w-4" />} title="Paper today" tag="Simulated" onOpen={onOpenOrders}>
          {paper ? (
            <>
              <Hero
                value={paper.totals.net_pnl}
                text={rupees(String(paper.totals.net_pnl), { signed: true })}
                label="net"
              />
              <Facts
                items={[
                  ["Trades", String(paper.totals.trades)],
                  ["Won / lost", `${paper.totals.wins} / ${paper.totals.losses}`],
                  ["Gross", rupees(String(paper.totals.gross_pnl), { signed: true })],
                  ["Charges", rupees(String(paper.totals.charges))],
                ]}
              />
              {paper.totals.open_positions > 0 && <Note>{paper.totals.open_positions} still open, not finished.</Note>}
            </>
          ) : (
            <Missing />
          )}
        </Card>

        <Card icon={<Trophy className="h-4 w-4" />} title="Leading today" tag="By net" onOpen={onOpenReports}>
          {glance.today === null ? (
            <Missing />
          ) : leader ? (
            <>
              <p className="mt-4 flex items-center gap-2 text-2xl font-semibold" style={{ color: "var(--foreground)" }}>
                {strategyLabel(leader.strategy)}
                <Ledger mode={leader.execution_mode} />
              </p>
              <Facts
                items={[
                  ["Net", rupees(leader.net_pnl, { signed: true })],
                  ["Trades", String(leader.trades)],
                  ["Won / lost", `${leader.wins} / ${leader.losses}`],
                  ["Average R", leader.average_r === null ? "—" : `${Number(leader.average_r).toFixed(2)}R`],
                ]}
              />
              <Note>One day says little; see 30 days below.</Note>
            </>
          ) : (
            <Note>No trades yet today.</Note>
          )}
        </Card>
      </div>

      <div className="grid gap-4 xl:grid-cols-[1.15fr_.85fr]">
        <article className="panel p-5">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <p className="eyebrow">Which strategy is working</p>
              <h3 className="mt-1 text-base font-semibold" style={{ color: "var(--foreground)" }}>
                Strategies by net result
              </h3>
            </div>
            <div role="group" aria-label="Strategy period" className="flex gap-1">
              {([
                ["today", "Today"],
                ["month", "30 days"],
              ] as const).map(([value, label]) => (
                <button
                  key={value}
                  type="button"
                  aria-pressed={period === value}
                  className={`${period === value ? "primary-button" : "secondary-button"} px-3 py-1.5 text-xs`}
                  onClick={() => setPeriod(value)}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>
          {ranked === null ? (
            <Missing />
          ) : ranked.length === 0 ? (
            <Note>No trades in this period.</Note>
          ) : (
            <div className="table-scroll mt-4">
              <table className="terminal-table" aria-label="Strategies by net result">
                <thead>
                  <tr>
                    <th>Strategy</th>
                    <th>Trades</th>
                    <th>Win rate</th>
                    <th>Average R</th>
                    <th>Net</th>
                  </tr>
                </thead>
                <tbody>
                  {ranked.map((row) => (
                    <tr key={`${row.strategy}-${row.execution_mode}`}>
                      <td>
                        <span className="flex flex-wrap items-center gap-2">
                          <strong>{strategyLabel(row.strategy)}</strong>
                          <Ledger mode={row.execution_mode} />
                        </span>
                      </td>
                      <td className="numeric">
                        {row.trades}
                        {period === "month" && row.trades < FEW_TRADES && (
                          <span className="muted-cell text-[11px]"> · too few to judge</span>
                        )}
                      </td>
                      <td className="numeric">{percent(row.win_rate_percent)}</td>
                      <td className="numeric">
                        {row.average_r === null ? "—" : `${Number(row.average_r).toFixed(2)}R`}
                      </td>
                      <td className="numeric font-semibold" style={{ color: ink(toNumber(row.net_pnl)) }}>
                        {rupees(row.net_pnl, { signed: true })}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <Note>
            A trade sent to the broker counts as Live here and is left out of Paper. The Paper today card also includes
            its simulated copy.
          </Note>
        </article>

        <DailyBars days={glance.days} />
      </div>
    </div>
  );
}

function Card({
  icon,
  title,
  tag,
  onOpen,
  children,
}: {
  icon: ReactNode;
  title: string;
  tag: string;
  onOpen: () => void;
  children: ReactNode;
}) {
  return (
    <article className="panel relative overflow-hidden p-5" aria-label={title}>
      <span className="absolute inset-x-0 top-0 h-0.5" style={{ background: "var(--primary)" }} aria-hidden />
      <div className="flex items-center justify-between gap-3">
        <span className="flex items-center gap-2 text-sm font-semibold" style={{ color: "var(--foreground)" }}>
          <span
            className="grid h-8 w-8 place-items-center rounded-lg"
            style={{ background: "var(--primary-soft)", color: "var(--primary)" }}
          >
            {icon}
          </span>
          {title}
        </span>
        <button
          type="button"
          onClick={onOpen}
          aria-label={`Open ${title}`}
          className="flex items-center gap-1 text-xs"
          style={{ color: "var(--muted)" }}
        >
          {tag} <ArrowUpRight className="h-3.5 w-3.5" />
        </button>
      </div>
      {children}
    </article>
  );
}

function Hero({ value, text, label }: { value: number | null; text: string; label: string }) {
  return (
    <p className="mt-4 text-3xl font-semibold tracking-tight" style={{ color: ink(value) }}>
      {text}
      <span className="ml-2 text-sm font-normal" style={{ color: "var(--muted)" }}>
        {label}
      </span>
    </p>
  );
}

function Facts({ items }: { items: [string, string][] }) {
  return (
    <dl className="mt-4 grid grid-cols-2 gap-x-4 gap-y-3">
      {items.map(([label, value]) => (
        <div key={label}>
          <dt className="text-xs" style={{ color: "var(--muted)" }}>
            {label}
          </dt>
          <dd className="numeric mt-0.5 text-sm font-semibold" style={{ color: "var(--foreground)" }}>
            {value}
          </dd>
        </div>
      ))}
    </dl>
  );
}

function Ledger({ mode }: { mode: "LIVE" | "PAPER" }) {
  return <span className={`status-pill leading-4 ${mode === "LIVE" ? "status-good" : "status-watch"}`}>{mode === "LIVE" ? "Live" : "Paper"}</span>;
}

function Note({ children }: { children: ReactNode }) {
  return (
    <p className="mt-3 text-xs" style={{ color: "var(--muted)" }}>
      {children}
    </p>
  );
}

function Missing() {
  return <Note>Could not be read just now. It will be tried again in a minute.</Note>;
}

/** Live net per session. Above or below the line carries the sign, so colour is never the only cue. */
function DailyBars({ days }: { days: HistoryDay[] | null }) {
  const [at, setAt] = useState<number | null>(null);
  const bars = (days ?? [])
    .map((day) => ({
      date: day.session_date,
      net: toNumber(day.broker_net_pnl ?? day.net_pnl) ?? 0,
      settled: (day.broker_net_pnl ?? null) !== null,
      trades: day.trades,
    }))
    .sort((a, b) => a.date.localeCompare(b.date));
  const high = Math.max(0, ...bars.map((bar) => bar.net));
  const low = Math.min(0, ...bars.map((bar) => bar.net));
  const best = Math.max(...bars.map((bar) => bar.net));
  const worst = Math.min(...bars.map((bar) => bar.net));
  const span = high - low || 1;
  const width = 400;
  const height = 150;
  const slot = width / Math.max(bars.length, 1);
  const y = (value: number) => ((high - value) / span) * (height - 8) + 4;
  const focus = at === null ? null : bars[at];
  const money = (value: number) => rupees(String(value), { signed: true });
  const short = (date: string) =>
    new Date(`${date}T00:00:00Z`).toLocaleDateString("en-IN", { day: "numeric", month: "short", timeZone: "UTC" });

  return (
    <article className="panel p-5">
      <p className="eyebrow">Real money</p>
      <h3 className="mt-1 text-base font-semibold" style={{ color: "var(--foreground)" }}>
        Live net by day, last 30 days
      </h3>
      {days === null ? (
        <Missing />
      ) : !bars.length ? (
        <Note>No live trades in the last 30 days.</Note>
      ) : (
        <>
          <p className="numeric mt-2 min-h-5 text-xs" aria-live="polite" style={{ color: "var(--muted-strong)" }}>
            {focus
              ? `${short(focus.date)}: ${money(focus.net)}${focus.settled ? "" : " (estimate)"}`
              : "Point at a bar to read the day"}
          </p>
          <svg
            viewBox={`0 0 ${width} ${height}`}
            className="mt-2 w-full"
            role="img"
            aria-label={`Live net for ${bars.length} days, best ${money(best)}, worst ${money(worst)}. History has the same days as a table.`}
            onMouseLeave={() => setAt(null)}
          >
            {bars.map((bar, index) => {
              const top = y(Math.max(bar.net, 0));
              const size = Math.max(Math.abs(y(bar.net) - y(0)), 2);
              const thick = Math.min(slot - 2, 18);
              return (
                <g
                  key={bar.date}
                  tabIndex={0}
                  role="button"
                  aria-label={`${short(bar.date)} ${money(bar.net)}`}
                  onMouseEnter={() => setAt(index)}
                  onFocus={() => setAt(index)}
                  onBlur={() => setAt(null)}
                >
                  <rect x={index * slot} y={0} width={slot} height={height} fill="transparent" />
                  <rect
                    x={index * slot + (slot - thick) / 2}
                    y={bar.net < 0 ? y(0) : top}
                    width={thick}
                    height={size}
                    rx={Math.min(3, size / 2)}
                    fill={bar.net < 0 ? "var(--negative)" : "var(--positive)"}
                    opacity={at === null || at === index ? 1 : 0.45}
                  />
                </g>
              );
            })}
            <line x1={0} x2={width} y1={y(0)} y2={y(0)} stroke="var(--border)" strokeWidth={1} />
          </svg>
          <div className="mt-1 flex justify-between text-[11px]" style={{ color: "var(--muted)" }}>
            <span>{short(bars[0].date)}</span>
            <span>
              best {money(best)} · worst {money(worst)}
            </span>
            <span>{short(bars[bars.length - 1].date)}</span>
          </div>
          <Note>The broker&apos;s own net where it has billed the day; otherwise our estimate.</Note>
        </>
      )}
    </article>
  );
}
