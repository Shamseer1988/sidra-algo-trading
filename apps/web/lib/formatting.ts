export function formatPrice(value: number) {
  return value.toLocaleString("en-IN", { maximumFractionDigits: 2 });
}

export function formatIstTimestamp(value: string | null | undefined) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("en-IN", {
    timeZone: "Asia/Kolkata",
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

export function statusTone(status: string) {
  const value = status.toLowerCase();
  if (["healthy", "running", "live", "configured", "good", "open", "operational"].includes(value)) return "status-good";
  if (["connecting", "not_configured", "stopped", "disconnected", "degraded", "pre_open", "post_market"].includes(value)) return "status-watch";
  return "status-bad";
}

export function titleCase(value: string) {
  return value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

/**
 * A UUID that works in insecure contexts too (plain-HTTP LAN addresses), where
 * `crypto.randomUUID` is unavailable. `crypto.getRandomValues` is always present.
 */
export function randomId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
