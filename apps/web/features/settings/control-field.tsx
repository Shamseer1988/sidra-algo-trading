"use client";

import { Clock, Info } from "lucide-react";

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
