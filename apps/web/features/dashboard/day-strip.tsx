"use client";

import { RefreshCw } from "lucide-react";
import { useEffect, useState } from "react";

import {
  api,
  type HistoryOverview,
  type MarketSession,
  type TradingControls,
  type TradingStatus,
} from "../../components/api";
import { titleCase } from "../../lib/formatting";
import { istDate } from "../../lib/strategy-names";
import { rupees } from "../history/money";

/** What decides the day, where a page title used to be: market, trading state, trades used, loss limit. */

type Coming = { tomorrow: MarketSession; next: MarketSession | null };

/** Noon IST on that date asks the server's own calendar about the day. */
const sessionOn = (date: string) => api.marketSession(`${date}T06:30:00Z`);

async function coming(): Promise<Coming> {
  const tomorrow = await sessionOn(istDate(-1));
  if (tomorrow.trading_day) return { tomorrow, next: tomorrow };
  for (let ahead = 2; ahead <= 7; ahead += 1) {
    const day = await sessionOn(istDate(-ahead));
    if (day.trading_day) return { tomorrow, next: day };
  }
  return { tomorrow, next: null };
}

/** "09:45" -> "9:45 AM" */
function clock(value: string | null | undefined): string {
  if (!value) return "—";
  const [hour, minute] = value.split(":").map(Number);
  return `${hour % 12 || 12}:${String(minute).padStart(2, "0")} ${hour < 12 ? "AM" : "PM"}`;
}

const dayName = (date: string) =>
  new Date(`${date}T00:00:00Z`).toLocaleDateString("en-IN", {
    weekday: "short",
    day: "numeric",
    month: "short",
    timeZone: "UTC",
  });

export function DayStrip({
  live,
  controls,
  tick,
  onRefresh,
}: {
  live: HistoryOverview | null;
  controls: TradingControls;
  /** Changes each time the glance below reloads, so the strip never lags it. */
  tick: unknown;
  onRefresh: () => void;
}) {
  const [market, setMarket] = useState<MarketSession | null>(null);
  const [status, setStatus] = useState<TradingStatus | null>(null);
  const [ahead, setAhead] = useState<Coming | null>(null);

  useEffect(() => {
    let current = true;
    const keep = <T,>(set: (value: T | null) => void) => (value: T | null) => {
      if (current) set(value);
    };
    void api.marketSession().then(keep(setMarket), () => keep(setMarket)(null));
    void api.tradingStatus().then(keep(setStatus), () => keep(setStatus)(null));
    return () => {
      current = false;
    };
  }, [tick]);

  // The calendar ahead changes once a day.
  const today = istDate();
  useEffect(() => {
    let current = true;
    void coming().then(
      (value) => current && setAhead(value),
      () => current && setAhead(null),
    );
    return () => {
      current = false;
    };
  }, [today]);

  const next = ahead?.next?.session_date ? dayName(ahead.next.session_date) : null;
  const marketNote = !ahead
    ? (market?.reason ?? "")
    : ahead.tomorrow.trading_day
      ? `Tomorrow: open, ${clock(ahead.tomorrow.regular_open)}`
      : `Tomorrow closed: ${ahead.tomorrow.reason}${next ? `. Next: ${next}` : ""}`;

  return (
    <div className="flex items-stretch gap-3" data-testid="day-strip">
      <h2 className="sr-only">Dashboard</h2>
      <dl className="panel grid min-w-0 flex-1 gap-px overflow-hidden sm:grid-cols-2 xl:grid-cols-4">
        <Cell
          label={`Market · ${dayName(today)}`}
          value={market ? (market.trading_day ? titleCase(market.phase.toLowerCase()) : "Closed today") : "—"}
          note={market && !market.trading_day ? `${market.reason}. ${marketNote}` : marketNote}
        />
        <Cell
          label="Trading"
          value={status?.headline ?? "—"}
          note={status?.detail ?? "Could not be read"}
          tone={status ? (status.trading ? "var(--positive)" : "var(--foreground)") : undefined}
        />
        <Cell
          label="Live trades today"
          value={live ? `${live.trades} of ${controls.maximum_daily_trades}` : "—"}
          note={`Entries ${clock(controls.trade_start_time)} to ${clock(controls.trade_cutoff_time)}`}
        />
        <Cell
          label="Daily loss limit"
          value={controls.daily_loss_limit ? rupees(String(controls.daily_loss_limit)) : "Not set"}
          note={live ? `Today so far ${rupees(live.net_pnl, { signed: true })} net` : "Today's result could not be read"}
        />
      </dl>
      <button onClick={onRefresh} className="secondary-button shrink-0 px-3" aria-label="Refresh state" title="Refresh">
        <RefreshCw className="h-4 w-4" />
      </button>
    </div>
  );
}

function Cell({ label, value, note, tone }: { label: string; value: string; note: string; tone?: string }) {
  return (
    <div className="min-w-0 px-5 py-3.5">
      <dt className="text-xs" style={{ color: "var(--muted)" }}>
        {label}
      </dt>
      <dd className="mt-1 truncate text-lg font-semibold" title={value} style={{ color: tone ?? "var(--foreground)" }}>
        {value}
      </dd>
      <dd className="mt-0.5 line-clamp-2 text-xs" title={note} style={{ color: "var(--muted)" }}>
        {note}
      </dd>
    </div>
  );
}
