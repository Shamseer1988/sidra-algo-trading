"use client";

import { Clock } from "lucide-react";

import { Hint } from "../../components/hint";

import type { SettingSpec } from "../../components/api";
import { formatIstTimestamp } from "../../lib/formatting";

/**
 * One described control, rendered.
 *
 * This lives apart from the forms because there are now two of them — trading
 * controls and indicator periods — and both are driven by the same
 * `SettingSpec` shape the server sends. A second copy of this markup would be
 * a second place for the units, the range hint and the effect note to be
 * written differently, which is the whole failure the catalogue exists to
 * prevent.
 */

export const UNIT_SUFFIX: Record<SettingSpec["unit"], string> = {
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
export const EFFECT_TONE: Record<SettingSpec["effect"], string> = {
  IMMEDIATE: "text-amber-300",
  NEXT_SIGNAL: "text-sky-300",
  NEXT_SESSION: "text-slate-400",
};

export function rangeHint(spec: SettingSpec): string {
  const low = spec.minimum ?? spec.exclusive_minimum;
  const high = spec.maximum ?? spec.exclusive_maximum;
  if (low === null && high === null) return "";
  const lowText = spec.exclusive_minimum !== null ? `over ${spec.exclusive_minimum}` : `${low}`;
  const highText = spec.exclusive_maximum !== null ? `under ${spec.exclusive_maximum}` : `${high}`;
  if (low !== null && high !== null) return `${lowText} to ${highText}`;
  return low !== null ? `at least ${lowText}` : `at most ${highText}`;
}

/** Which keys in a draft differ from the values the catalogue last reported. */
export function dirtyKeys(specs: SettingSpec[], draft: Record<string, unknown>): string[] {
  return specs.filter((spec) => String(draft[spec.key]) !== String(spec.value)).map((spec) => spec.key);
}

export function ControlField({
  spec,
  value,
  derived,
  changed,
  disabled,
  onChange,
}: {
  spec: SettingSpec;
  value: unknown;
  derived?: string | null;
  changed: boolean;
  disabled: boolean;
  onChange: (value: unknown) => void;
}) {
  const range = rangeHint(spec);
  return (
    <div
      data-testid={`field-${spec.key}`}
      className={`rounded-md border px-3.5 py-3 ${changed ? "border-sky-500/40 bg-sky-950/10" : "border-slate-800"}`}
    >
      <div className="flex items-center gap-1.5">
        <label htmlFor={spec.key} className="min-w-0 truncate text-sm font-semibold text-white" title={spec.label}>
          {spec.label}
        </label>
        <Hint testId={`hint-${spec.key}`}>
          <SettingHint spec={spec} range={range} />
        </Hint>
        {spec.is_ceiling && <span className="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] font-medium text-slate-400">ceiling</span>}
        {changed && <span className="ml-auto text-[10px] font-semibold uppercase tracking-wide text-sky-300">Edited</span>}
      </div>

      <div className="mt-2 flex items-center gap-2">
        {spec.kind === "boolean" ? (
          <label className="flex cursor-pointer select-none items-center gap-3 py-1.5 text-sm text-slate-300">
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
            className="field-input py-2 font-mono text-sm disabled:cursor-not-allowed disabled:opacity-50"
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
              className="field-input py-2 font-mono text-sm disabled:cursor-not-allowed disabled:opacity-50"
              value={String(value ?? "")}
              onChange={(event) => onChange(spec.kind === "time" ? event.target.value : event.target.value === "" ? "" : Number(event.target.value))}
            />
            {UNIT_SUFFIX[spec.unit] && <span className="shrink-0 text-xs text-slate-500">{UNIT_SUFFIX[spec.unit]}</span>}
          </>
        )}
      </div>
      {derived && <p className="numeric mt-1.5 text-xs text-emerald-300">{derived}</p>}
    </div>
  );
}

/**
 * Everything that used to be printed under the box: what it does, its range,
 * when a change applies, its stored name and when it last changed.
 *
 * The stored key stays because every diagnostic names controls that way. An
 * operator told to change `maximum_daily_risk_percent` was searching a page
 * whose only word for it is "Daily planned-risk budget".
 */
export function SettingHint({ spec, range }: { spec: SettingSpec; range: string }) {
  return (
    <span className="block space-y-1.5">
      <span className="block text-slate-200">{spec.help}</span>
      {range && <span className="block text-slate-400">Allowed: {range}</span>}
      <span className={`block ${EFFECT_TONE[spec.effect]}`}>
        <Clock className="mr-1 inline h-3 w-3" />
        {spec.effect_label}
      </span>
      <span className="block text-slate-500">
        <span className="font-mono">{spec.key}</span>
        {" · "}
        {spec.last_changed_at ? `last changed ${formatIstTimestamp(spec.last_changed_at)}` : "not changed here yet"}
      </span>
    </span>
  );
}
