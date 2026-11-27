const ACRONYMS = new Set(["rs", "orb", "vwap", "ema", "cpr", "sip"]);

/** `rs-pullback-v1@8` reads as "RS Pullback": the name a person uses, without the revision. */
export function strategyLabel(version: string): string {
  return version
    .split("@")[0]
    .replace(/-v\d+$/, "")
    .split("-")
    .map((word) => (ACRONYMS.has(word) ? word.toUpperCase() : word.charAt(0).toUpperCase() + word.slice(1)))
    .join(" ");
}

/** Today's date in India, whatever timezone the browser is in. `toISOString` is yesterday until 5:30 AM IST. */
export function istDate(daysAgo = 0): string {
  const today = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Kolkata" }).format(new Date());
  const value = new Date(`${today}T00:00:00Z`);
  value.setUTCDate(value.getUTCDate() - daysAgo);
  return value.toISOString().slice(0, 10);
}
