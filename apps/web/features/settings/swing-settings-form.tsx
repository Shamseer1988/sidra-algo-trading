"use client";

import { CalendarClock } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { api, type SettingSpec, type SwingSettingsView } from "../../components/api";
import { ControlField, dirtyKeys } from "./control-field";
import { type Change, ChangesModal, FieldGrid, SaveBar, SettingsSection, Tile, shown } from "./settings-ui";

type Notify = (message: string, kind?: "success" | "error" | "info") => void;

const rupees = (value: number) => `₹${Math.round(value).toLocaleString("en-IN")}`;

/** The rupee figure behind a percent, as the draft stands. */
function derived(spec: SettingSpec, draft: Record<string, unknown>): string | null {
  const capital = Number(draft.capital);
  const value = Number(draft[spec.key]);
  if (!Number.isFinite(capital) || !Number.isFinite(value)) return null;
  if (spec.key === "risk_per_trade_percent") return `${rupees((capital * value) / 100)} a trade`;
  if (spec.key === "heat_cap_percent") return `${rupees((capital * value) / 100)} of open risk`;
  if (spec.key === "max_position_percent") return `${rupees((capital * value) / 100)} in one share`;
  return null;
}

/**
 * Settings > Swing trading: the paper desk's capital and limits.
 *
 * These were fixed in code (₹1,00,000, 0.75% a trade, five positions). They
 * are read once per evening run, so a change applies from the next evening;
 * positions already open keep the size and stop they were opened with. The
 * desk is paper only: nothing here can send an order.
 */
export function SwingSettingsForm({ isAdmin, onMessage }: { isAdmin: boolean; onMessage: Notify }) {
  const [view, setView] = useState<SwingSettingsView | null>(null);
  const [draft, setDraft] = useState<Record<string, unknown>>({});
  const [reviewing, setReviewing] = useState(false);

  const apply = useCallback((next: SwingSettingsView) => {
    setView(next);
    setDraft(Object.fromEntries(next.settings.map((item) => [item.key, item.value])));
  }, []);

  const load = useCallback(async () => {
    try {
      apply(await api.swingSettings());
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Could not load swing settings");
    }
  }, [apply, onMessage]);

  useEffect(() => {
    void load();
  }, [load]);

  const dirty = useMemo(() => (view ? dirtyKeys(view.settings, draft) : []), [view, draft]);
  const changes: Change[] = (view?.settings ?? [])
    .filter((spec) => dirty.includes(spec.key))
    .map((spec) => ({ key: spec.key, label: spec.label, from: shown(spec.value, spec.unit), to: shown(draft[spec.key], spec.unit) }));
  const tooMuchRisk = Number(draft.risk_per_trade_percent) > Number(draft.heat_cap_percent);

  async function save(reason: string): Promise<boolean> {
    try {
      apply(await api.saveSwingSettings(draft, reason));
      onMessage("Swing settings saved. They apply from the next evening run.", "success");
      return true;
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Could not save swing settings", "error");
      return false;
    }
  }

  if (!view) return <div className="panel mt-6 max-w-6xl p-5"><div className="skeleton h-40" /></div>;

  const effective = view.effective;
  const schedule = view.schedule;

  return (
    <section className="mt-6 max-w-6xl space-y-5" data-testid="settings-swing">
      <SettingsSection
        eyebrow="Paper only — no real orders"
        title="What these settings allow"
        hint="Worked out from the saved values. Shares in a trade = risk a trade ÷ the distance from entry to stop, then cut to the largest-position limit."
        actions={<span className={`status-pill ${view.saved ? "status-good" : "status-watch"}`}>{view.saved ? "Saved here" : "Defaults"}</span>}
      >
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <Tile label="Risk a trade" value={rupees(effective.risk_per_trade)} note="If the stop is hit" />
          <Tile label="Open-risk cap" value={rupees(effective.heat_cap)} note="All open trades together" />
          <Tile label="Largest position" value={rupees(effective.largest_position)} note="In one share" />
          <Tile label="Trades at once" value={`${effective.trades_the_cap_allows}`} note="At full risk, within both caps" />
        </div>
      </SettingsSection>

      {view.group_order.map((group) => (
        <SettingsSection key={group} eyebrow="Swing" title={view.group_labels[group]}>
          {group === "SWING_CAPITAL" && tooMuchRisk && (
            <p className="mb-3 rounded-md border border-amber-500/30 bg-amber-950/20 p-3 text-xs leading-5 text-amber-100">
              Risk a trade is larger than the open-risk cap, so not even one trade could be taken. The server will refuse this.
            </p>
          )}
          <FieldGrid>
            {view.settings
              .filter((spec) => spec.group === group)
              .map((spec) => (
                <ControlField
                  key={spec.key}
                  spec={spec}
                  value={draft[spec.key]}
                  derived={derived(spec, draft)}
                  changed={dirty.includes(spec.key)}
                  disabled={!isAdmin}
                  onChange={(next) => setDraft((current) => ({ ...current, [spec.key]: next }))}
                />
              ))}
          </FieldGrid>
        </SettingsSection>
      ))}

      <SettingsSection
        eyebrow="Swing"
        title="Timing"
        hint="Fixed by how the swing desk works, not settings: daily candles are only final in the evening, and the desk keeps out of the intraday desk's way while the market is open."
      >
        <ul className="grid gap-3 text-sm sm:grid-cols-2">
          {[
            ["Plans made", schedule.plan_after],
            ["Entries", schedule.entry],
            ["Quiet while", schedule.quiet_hours],
            [
              "Results days",
              `No new plan within ${schedule.results_avoid_days} days of results; an open position is sold at the next open within ${schedule.results_exit_days}.`,
            ],
          ].map(([label, text]) => (
            <li key={label} className="glass-inset flex gap-3 rounded-md p-3.5">
              <CalendarClock className="mt-0.5 h-4 w-4 shrink-0 text-sky-400" />
              <span>
                <span className="block text-xs font-semibold uppercase tracking-wide text-slate-500">{label}</span>
                <span className="text-slate-200">{text}</span>
              </span>
            </li>
          ))}
        </ul>
      </SettingsSection>

      {isAdmin && (
        <SaveBar
          count={dirty.length}
          blocked={tooMuchRisk ? "Risk a trade is above the open-risk cap" : null}
          onDiscard={() => apply(view)}
          onReview={() => setReviewing(true)}
        />
      )}
      {reviewing && (
        <ChangesModal
          title="Save swing settings?"
          changes={changes}
          requireReason
          warning="Paper only. Applies from the next evening run; positions already open keep their size and stop. Changing these during a forward test means the weeks before and after are no longer one result."
          onConfirm={save}
          onClose={() => setReviewing(false)}
        />
      )}
    </section>
  );
}
