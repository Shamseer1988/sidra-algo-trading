"use client";

import { AlertTriangle, Clock, Info, RotateCcw, Save, ShieldCheck } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { api, type EffectiveLimits, type RiskPreset, type SettingSpec, type SettingsCatalog } from "../../components/api";
import { formatIstTimestamp } from "../../lib/formatting";

/**
 * The trading controls, rendered from the server's description of them.
 *
 * The previous screen mapped over `Object.entries(controls)` and drew a text
 * box per key, labelled by the key itself. Nothing told an operator that
 * "maximum open exposure percent" is multiplied by leverage before it becomes
 * rupees, or that a daily loss stop applies to the session already running.
 *
 * Every label, unit, bound, help string and effect note below comes from
 * `/settings/trading/catalog`. Nothing about a control is written here, which
 * is deliberate: a control described in two places is a control that will
 * eventually be described differently in each.
 */

const UNIT_SUFFIX: Record<SettingSpec["unit"], string> = {
  INR: "₹",
  PERCENT: "%",
  COUNT: "",
  MULTIPLE: "×",
  POINTS: "/ 100",
  RATIO: ": 1",
  TIME_IST: "IST",
  CHOICE: "",
  BOOLEAN: "",
};

// Dark-theme tones as the base class, remapped for the light theme in
// globals.css alongside the other palette entries. Tailwind's `dark:` variant
// is not usable here: darkMode is unconfigured, so it follows the operating
// system's preference rather than this app's data-theme toggle, and a tone
// written that way reads correctly only by coincidence.
const EFFECT_TONE: Record<SettingSpec["effect"], string> = {
  IMMEDIATE: "text-amber-300",
  NEXT_SIGNAL: "text-sky-300",
  NEXT_SESSION: "text-slate-400",
};

function rangeHint(spec: SettingSpec): string {
  const low = spec.minimum ?? spec.exclusive_minimum;
  const high = spec.maximum ?? spec.exclusive_maximum;
  if (low === null && high === null) return "";
  const lowText = spec.exclusive_minimum !== null ? `over ${spec.exclusive_minimum}` : `${low}`;
  const highText = spec.exclusive_maximum !== null ? `under ${spec.exclusive_maximum}` : `${high}`;
  if (low !== null && high !== null) return `${lowText} to ${highText}`;
  return low !== null ? `at least ${lowText}` : `at most ${highText}`;
}

/** The rupee figure behind a percent, so the multiplication is not homework. */
function derivedValue(spec: SettingSpec, draft: Record<string, unknown>, effective: EffectiveLimits): string | null {
  const capital = Number(draft.account_capital ?? effective.capital);
  const number = Number(draft[spec.key]);
  if (!Number.isFinite(number) || !Number.isFinite(capital)) return null;
  if (spec.key === "risk_per_trade_percent") return `₹${((capital * number) / 100).toFixed(2)} a trade`;
  if (spec.key === "maximum_daily_risk_percent") return `₹${((capital * number) / 100).toFixed(2)} for the day`;
  if (spec.key === "maximum_open_exposure_percent") {
    const leverage = Number(draft.intraday_leverage_enabled ? draft.intraday_leverage_multiplier ?? 1 : 1);
    return `₹${((capital * number * (Number.isFinite(leverage) ? leverage : 1)) / 100).toFixed(2)} of exposure`;
  }
  if (spec.key === "daily_loss_limit" || spec.key === "daily_profit_target") {
    return capital > 0 ? `${((number * 100) / capital).toFixed(2)}% of capital` : null;
  }
  return null;
}

export function TradingControlsForm({ isAdmin, onMessage }: { isAdmin: boolean; onMessage: (message: string) => void }) {
  const [catalog, setCatalog] = useState<SettingsCatalog | null>(null);
  const [presets, setPresets] = useState<RiskPreset[]>([]);
  const [draft, setDraft] = useState<Record<string, unknown>>({});
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      const [next, available] = await Promise.all([api.settingsCatalog(), api.riskPresets()]);
      setCatalog(next);
      setPresets(available);
      setDraft(Object.fromEntries(next.settings.map((item) => [item.key, item.value])));
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Could not load trading controls");
    }
  }, [onMessage]);

  useEffect(() => {
    void load();
  }, [load]);

  const dirty = useMemo(() => {
    if (!catalog) return [] as string[];
    return catalog.settings.filter((item) => String(draft[item.key]) !== String(item.value)).map((item) => item.key);
  }, [catalog, draft]);

  async function save() {
    if (!catalog || !isAdmin) return;
    setSaving(true);
    try {
      await api.updateControls(draft as never);
      onMessage(`Saved ${dirty.length} change${dirty.length === 1 ? "" : "s"}.`);
      await load();
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Could not save trading controls");
    } finally {
      setSaving(false);
    }
  }

  async function applyPreset(preset: RiskPreset) {
    if (!isAdmin) return;
    try {
      await api.applyRiskPreset(preset.key, false);
      onMessage(`Applied ${preset.label}.`);
      await load();
      return;
    } catch (error) {
      // The server refuses a preset that loosens a limit until it is
      // acknowledged. That refusal is the confirmation step, so it is shown
      // rather than swallowed, and the operator confirms against the server's
      // own words rather than a message this file invented.
      const detail = error instanceof Error ? error.message : "Could not apply preset";
      if (!detail.includes("raises a risk limit")) {
        onMessage(detail);
        return;
      }
      if (!window.confirm(`${detail}\n\nApply it?`)) return;
      try {
        await api.applyRiskPreset(preset.key, true);
        onMessage(`Applied ${preset.label}.`);
        await load();
      } catch (retry) {
        onMessage(retry instanceof Error ? retry.message : "Could not apply preset");
      }
    }
  }

  if (!catalog) return <p className="mt-6 text-sm text-slate-500">Loading trading controls…</p>;

  return (
    <section className="mt-6 max-w-5xl space-y-6">
      <EffectiveSummary effective={catalog.effective} />

      {presets.length > 0 && (
        <article className="panel p-5 sm:p-7">
          <p className="eyebrow">Risk profiles</p>
          <h3 className="mt-1 text-base font-semibold text-white">Start from a profile</h3>
          <p className="mt-1 text-xs leading-5 text-slate-400">
            Applying one saves it the same way a hand edit does: validated, versioned and audited. Raising a limit asks
            first.
          </p>
          <div className="mt-4 grid gap-3 sm:grid-cols-2">
            {presets.map((preset) => (
              <div key={preset.key} className="glass-inset rounded-md p-4">
                <p className="text-sm font-semibold text-white">{preset.label}</p>
                <p className="mt-1 text-xs leading-5 text-slate-400">{preset.description}</p>
                <p className="mt-2 numeric text-xs text-slate-300">
                  ₹{preset.effective.planned_risk_per_trade} a trade · ₹{preset.effective.daily_loss_limit} daily stop
                  {preset.effective.daily_loss_percent ? ` (${preset.effective.daily_loss_percent}% of capital)` : ""} ·{" "}
                  {preset.effective.effective_trade_ceiling} trades
                </p>
                {isAdmin && (
                  <button onClick={() => void applyPreset(preset)} className="secondary-button mt-4">
                    <ShieldCheck className="h-4 w-4" />
                    Apply {preset.label}
                  </button>
                )}
              </div>
            ))}
          </div>
        </article>
      )}

      {catalog.group_order.map((group) => {
        const items = catalog.settings.filter((item) => item.group === group);
        if (!items.length) return null;
        return (
          <article key={group} className="panel p-5 sm:p-7">
            <p className="eyebrow">Configuration</p>
            <h3 className="mt-1 text-base font-semibold text-white">{catalog.group_labels[group]}</h3>
            <div className="mt-5 grid gap-5 lg:grid-cols-2">
              {items.map((spec) => (
                <ControlField
                  key={spec.key}
                  spec={spec}
                  value={draft[spec.key]}
                  derived={derivedValue(spec, draft, catalog.effective)}
                  changed={dirty.includes(spec.key)}
                  disabled={!isAdmin}
                  onChange={(next) => setDraft((current) => ({ ...current, [spec.key]: next }))}
                />
              ))}
            </div>
          </article>
        );
      })}

      {isAdmin && (
        <div className="flex flex-wrap items-center gap-3">
          {dirty.length > 0 ? (
            <>
              <button onClick={() => void save()} disabled={saving} className="primary-button">
                <Save className="h-4 w-4" />
                Save {dirty.length} change{dirty.length === 1 ? "" : "s"}
              </button>
              <button onClick={() => void load()} className="secondary-button">
                <RotateCcw className="h-4 w-4" />
                Discard
              </button>
            </>
          ) : (
            // A greyed-out button is a pale smudge on the light theme and reads
            // as broken rather than as "nothing to do".
            <p className="text-sm text-slate-500">No changes to save.</p>
          )}
        </div>
      )}
    </section>
  );
}

function EffectiveSummary({ effective }: { effective: EffectiveLimits }) {
  const constrained = effective.effective_trade_ceiling < effective.configured_trade_ceiling;
  return (
    <article className="panel p-5 sm:p-7">
      <p className="eyebrow">What these settings allow</p>
      <h3 className="mt-1 text-base font-semibold text-white">Effective limits</h3>
      <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Metric label="Planned risk a trade" value={`₹${effective.planned_risk_per_trade}`} note="If the stop is hit" />
        <Metric
          label="Trades today"
          value={`${effective.effective_trade_ceiling}`}
          note={constrained ? `of ${effective.configured_trade_ceiling} configured` : "account-wide, on first fill"}
          tone={constrained ? "warn" : undefined}
        />
        <Metric
          label="Daily loss stop"
          value={`₹${effective.daily_loss_limit}`}
          note={effective.daily_loss_percent ? `${effective.daily_loss_percent}% of capital` : "not set"}
        />
        <Metric
          label="Exposure ceiling"
          value={`₹${effective.exposure_ceiling}`}
          note={`${effective.leverage_multiplier}× leverage — exposure, not cash`}
        />
      </div>
      {effective.warnings.length > 0 && (
        <ul className="mt-4 space-y-2">
          {effective.warnings.map((warning) => (
            <li key={warning} className="flex gap-2 rounded-md border border-amber-500/30 bg-amber-950/20 p-3 text-xs leading-5 text-amber-100">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-300" />
              <span>{warning}</span>
            </li>
          ))}
        </ul>
      )}
      <p className="mt-4 text-xs leading-5 text-slate-500">
        A stop is an instruction to the market, not a guarantee. A gap, slippage, or a stop that cannot be placed will
        all exceed the planned figures above.
      </p>
    </article>
  );
}

function Metric({ label, value, note, tone }: { label: string; value: string; note: string; tone?: "warn" }) {
  return (
    <div className="glass-inset rounded-md p-4">
      <p className="eyebrow">{label}</p>
      <p className={`mt-2 numeric text-xl font-semibold ${tone === "warn" ? "text-amber-300" : "text-white"}`}>{value}</p>
      <p className="mt-1 text-xs text-slate-500">{note}</p>
    </div>
  );
}

function ControlField({
  spec,
  value,
  derived,
  changed,
  disabled,
  onChange,
}: {
  spec: SettingSpec;
  value: unknown;
  derived: string | null;
  changed: boolean;
  disabled: boolean;
  onChange: (value: unknown) => void;
}) {
  const range = rangeHint(spec);
  return (
    <div className={`rounded-md border p-4 ${changed ? "border-sky-500/40 bg-sky-950/10" : "border-slate-800"}`}>
      <div className="flex items-start justify-between gap-3">
        <label htmlFor={spec.key} className="text-sm font-semibold text-white">
          {spec.label}
          {spec.is_ceiling && <span className="ml-2 rounded bg-slate-800 px-1.5 py-0.5 text-[10px] font-medium text-slate-400">ceiling</span>}
        </label>
        {derived && <span className="numeric shrink-0 text-xs text-emerald-300">{derived}</span>}
      </div>

      <div className="mt-3 flex items-center gap-2">
        {spec.kind === "boolean" ? (
          <label className="flex cursor-pointer select-none items-center gap-3 text-sm text-slate-300">
            <input
              id={spec.key}
              type="checkbox"
              disabled={disabled}
              checked={Boolean(value)}
              onChange={(event) => onChange(event.target.checked)}
            />
            {value ? "On" : "Off"}
          </label>
        ) : spec.kind === "choice" ? (
          <select
            id={spec.key}
            disabled={disabled}
            className="field-input font-mono text-sm disabled:cursor-not-allowed disabled:opacity-50"
            value={String(value ?? "")}
            onChange={(event) => onChange(event.target.value)}
          >
            {spec.choices.map((choice) => (
              <option key={choice} value={choice}>
                {choice}
              </option>
            ))}
          </select>
        ) : (
          <>
            <input
              id={spec.key}
              type={spec.kind === "time" ? "time" : "number"}
              step={spec.kind === "integer" ? 1 : "any"}
              min={spec.minimum ?? undefined}
              max={spec.maximum ?? undefined}
              disabled={disabled}
              className="field-input font-mono text-sm disabled:cursor-not-allowed disabled:opacity-50"
              value={String(value ?? "")}
              onChange={(event) => onChange(spec.kind === "time" ? event.target.value : event.target.value === "" ? "" : Number(event.target.value))}
            />
            {UNIT_SUFFIX[spec.unit] && <span className="text-xs text-slate-500">{UNIT_SUFFIX[spec.unit]}</span>}
          </>
        )}
      </div>

      <p className="mt-3 flex gap-2 text-xs leading-5 text-slate-400">
        <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-slate-500" />
        <span>{spec.help}</span>
      </p>

      <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-slate-500">
        {range && <span>Allowed: {range}</span>}
        <span className={EFFECT_TONE[spec.effect]}>
          <Clock className="mr-1 inline h-3 w-3" />
          {spec.effect_label}
        </span>
        <span>
          {spec.last_changed_at ? `Last changed ${formatIstTimestamp(spec.last_changed_at)}` : "Not changed here yet"}
        </span>
      </div>
    </div>
  );
}
