"use client";

/**
 * Rupee figures, formatted once.
 *
 * Money arrives from the API as a string because it is a Decimal there, and
 * parsing it to a number for display is fine — what is not fine is doing
 * arithmetic on it in the browser. Every total on this screen is computed by
 * the server; nothing here adds two figures together.
 */

export function toNumber(value: string | null | undefined): number | null {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function rupees(value: string | null | undefined, { signed = false } = {}): string {
  const number = toNumber(value);
  if (number === null) return "—";
  const text = Math.abs(number).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  if (number < 0) return `−₹${text}`;
  return signed ? `+₹${text}` : `₹${text}`;
}

export function percent(value: string | null | undefined): string {
  const number = toNumber(value);
  return number === null ? "—" : `${number.toFixed(2)}%`;
}

export function ratio(value: string | null | undefined): string {
  const number = toNumber(value);
  return number === null ? "—" : number.toFixed(2);
}

/** Green for profit, red for loss, neutral for a flat or missing figure. */
export function pnlTone(value: string | null | undefined): string {
  const number = toNumber(value);
  if (number === null || number === 0) return "text-slate-400";
  return number > 0 ? "text-emerald-300" : "text-rose-300";
}

export function Money({ value, signed = false }: { value: string | null | undefined; signed?: boolean }) {
  return <span className={`numeric ${pnlTone(value)}`}>{rupees(value, { signed })}</span>;
}
