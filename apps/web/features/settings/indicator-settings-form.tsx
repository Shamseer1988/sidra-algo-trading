"use client";

import { Database, FileCog, RotateCcw, Save } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { api, type IndicatorCatalog, type IndicatorSettings } from "../../components/api";
import { ControlField, dirtyKeys } from "./control-field";

/**
 * The indicator periods — the ruler the scanner measures with.
 *
 * These used to live only in environment variables, so changing the EMA
 * periods or the opening-range window meant editing `.env` and restarting the
 * container. They are described by the same catalogue as the trading controls
 * and rendered by the same field, so the two screens cannot drift apart.
 *
 * The one thing shown here that the trading form has no equivalent of is
 * `source`. Until somebody saves on this screen the deployment is still
 * reading `.env`, and an operator who does not know that will edit the file,
 * see it work, and reasonably conclude the UI is decorative. After the first
 * save the file stops being consulted — which is the more surprising half,
 * and the reason it is stated on screen rather than in a runbook.
 */

/** Seconds are how the server stores the timeframe; minutes are how people say it. */
function derivedValue(key: string, value: unknown): string | null {
  const number = Number(value);
  if (!Number.isFinite(number)) return null;
  if (key === "candle_timeframe_seconds" && number % 60 === 0) {
    const minutes = number / 60;
    return `${minutes} minute${minutes === 1 ? "" : "s"} a candle`;
  }
  return null;
}

export function IndicatorSettingsForm({ isAdmin, onMessage }: { isAdmin: boolean; onMessage: (message: string) => void }) {
  const [catalog, setCatalog] = useState<IndicatorCatalog | null>(null);
  const [draft, setDraft] = useState<Record<string, unknown>>({});
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      const next = await api.indicatorCatalog();
      setCatalog(next);
      setDraft(Object.fromEntries(next.settings.map((item) => [item.key, item.value])));
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Could not load indicator periods");
    }
  }, [onMessage]);

  useEffect(() => {
    void load();
  }, [load]);

  const dirty = useMemo(() => (catalog ? dirtyKeys(catalog.settings, draft) : []), [catalog, draft]);

  // The server enforces this too, and refuses the save. Saying it here means
  // the operator reads it beside the two boxes rather than in a toast after
  // the round trip.
  const fastSlowConflict = Number(draft.ema_fast_period) >= Number(draft.ema_slow_period);

  async function save() {
    if (!catalog || !isAdmin) return;
    setSaving(true);
    try {
      await api.updateIndicators(draft as unknown as IndicatorSettings);
      onMessage(`Saved ${dirty.length} indicator change${dirty.length === 1 ? "" : "s"}. They apply from the next session.`);
      await load();
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Could not save indicator periods");
    } finally {
      setSaving(false);
    }
  }

  if (!catalog) return <p className="mt-6 text-sm text-slate-500">Loading indicator periods…</p>;

  const fromDatabase = catalog.source === "DATABASE";

  return (
    <section className="mt-6 max-w-5xl space-y-6">
      <article className="panel p-5 sm:p-7">
        <p className="eyebrow">Configuration</p>
        <h3 className="mt-1 text-base font-semibold text-white">Indicator periods</h3>
        <p className="mt-1 text-xs leading-5 text-slate-400">
          Every strategy, the universe builder and the backtester measure with these same numbers. Changing one changes
          what a signal means, so they take effect from the next session rather than mid-run.
        </p>

        <p
          className={`mt-4 flex gap-2 rounded-md border p-3 text-xs leading-5 ${
            fromDatabase ? "border-slate-800 text-slate-400" : "border-amber-500/30 bg-amber-950/20 text-amber-100"
          }`}
        >
          {fromDatabase ? (
            <Database className="mt-0.5 h-4 w-4 shrink-0 text-slate-500" />
          ) : (
            <FileCog className="mt-0.5 h-4 w-4 shrink-0 text-amber-300" />
          )}
          <span>
            {fromDatabase
              ? "These values are stored in the database. The .env file is no longer consulted for them."
              : "These values still come from this deployment's .env file. Saving here stores them in the database, and .env stops being consulted for them from that point on."}
          </span>
        </p>

        {fastSlowConflict && (
          <p className="mt-3 rounded-md border border-amber-500/30 bg-amber-950/20 p-3 text-xs leading-5 text-amber-100">
            The fast EMA must be shorter than the slow EMA. The server will refuse this pair.
          </p>
        )}

        <div className="mt-5 grid gap-5 lg:grid-cols-2">
          {catalog.settings.map((spec) => (
            <ControlField
              key={spec.key}
              spec={spec}
              value={draft[spec.key]}
              derived={derivedValue(spec.key, draft[spec.key])}
              changed={dirty.includes(spec.key)}
              disabled={!isAdmin}
              onChange={(next) => setDraft((current) => ({ ...current, [spec.key]: next }))}
            />
          ))}
        </div>
      </article>

      {isAdmin && (
        <div className="flex flex-wrap items-center gap-3">
          {dirty.length > 0 ? (
            <>
              <button onClick={() => void save()} disabled={saving || fastSlowConflict} className="primary-button">
                <Save className="h-4 w-4" />
                Save {dirty.length} change{dirty.length === 1 ? "" : "s"}
              </button>
              <button onClick={() => void load()} className="secondary-button">
                <RotateCcw className="h-4 w-4" />
                Discard
              </button>
            </>
          ) : (
            <p className="text-sm text-slate-500">No changes to save.</p>
          )}
        </div>
      )}
    </section>
  );
}
