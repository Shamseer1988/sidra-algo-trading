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
