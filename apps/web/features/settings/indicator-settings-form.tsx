"use client";

import { Database, FileCog } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { api, type IndicatorCatalog, type IndicatorSettings } from "../../components/api";
import { ControlField, dirtyKeys } from "./control-field";
import { type Change, ChangesModal, FieldGrid, SaveBar, SettingsSection, shown } from "./settings-ui";

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

export function IndicatorSettingsForm({
  isAdmin,
  onMessage,
  embedded = false,
}: {
  isAdmin: boolean;
  onMessage: (message: string, kind?: "success" | "error" | "info") => void;
  embedded?: boolean;
}) {
  const [catalog, setCatalog] = useState<IndicatorCatalog | null>(null);
  const [draft, setDraft] = useState<Record<string, unknown>>({});
  const [reviewing, setReviewing] = useState(false);

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

  const changes: Change[] = (catalog?.settings ?? [])
    .filter((spec) => dirty.includes(spec.key))
    .map((spec) => ({ key: spec.key, label: spec.label, from: shown(spec.value, spec.unit), to: shown(draft[spec.key], spec.unit) }));

  async function save(): Promise<boolean> {
    try {
      await api.updateIndicators(draft as unknown as IndicatorSettings);
      onMessage(`Saved ${dirty.length} indicator change${dirty.length === 1 ? "" : "s"}. They apply from the next session.`, "success");
      await load();
      return true;
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Could not save indicator periods", "error");
      return false;
    }
  }

  if (!catalog) return <div className="panel p-5"><div className="skeleton h-32" /></div>;

  const fromDatabase = catalog.source === "DATABASE";

  return (
    <section className={`${embedded ? "" : "mt-6 max-w-6xl"} space-y-5`} data-testid="indicator-settings">
      <SettingsSection
        eyebrow="Intraday"
        title="Indicator periods"
        hint="The ruler every strategy, the universe builder and the backtester measure with. Changing one changes what a signal means, so they take effect from the next session."
        actions={
          <span
            className={`status-pill ${fromDatabase ? "status-good" : "status-watch"}`}
            title={
              fromDatabase
                ? "Stored in the database. The .env file is no longer consulted for them."
                : "Still from this deployment's .env file. Saving here stores them in the database, and .env stops being consulted for them."
            }
          >
            {fromDatabase ? <Database className="h-3 w-3" /> : <FileCog className="h-3 w-3" />}
            {fromDatabase ? "Saved here" : "From server .env"}
          </span>
        }
      >
        {fastSlowConflict && (
          <p className="mb-3 rounded-md border border-amber-500/30 bg-amber-950/20 p-3 text-xs leading-5 text-amber-100">
            The fast EMA must be shorter than the slow EMA. The server will refuse this pair.
          </p>
        )}
        <FieldGrid>
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
        </FieldGrid>
      </SettingsSection>

      {isAdmin && (
        <SaveBar
          count={dirty.length}
          blocked={fastSlowConflict ? "Fast EMA must be shorter than slow EMA" : null}
          onDiscard={() => void load()}
          onReview={() => setReviewing(true)}
        />
      )}
      {reviewing && (
        <ChangesModal
          title="Save indicator periods?"
          changes={changes}
          warning={
            fromDatabase
              ? "These apply from the next session."
              : "These apply from the next session. After this save, the .env file is no longer read for indicator periods."
          }
          onConfirm={save}
          onClose={() => setReviewing(false)}
        />
      )}
    </section>
  );
}
