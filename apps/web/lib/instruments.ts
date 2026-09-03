/**
 * Script symbol and company name lookup for market instruments.
 */

export const KNOWN_SCRIPT_SYMBOLS: Record<string, string> = {
  // Indices
  "NSE_INDEX|Nifty 50": "NIFTY 50",
  "NSE_INDEX|NIFTY 50": "NIFTY 50",
  "NSE_INDEX|Nifty Bank": "BANKNIFTY",
  "NSE_INDEX|NIFTY BANK": "BANKNIFTY",
  "NSE_INDEX|Nifty IT": "NIFTY IT",
  "NSE:26000": "NIFTY 50",
  "NSE:26001": "BANKNIFTY",
  "NSE:26009": "NIFTY IT",
  // Equities
  "NSE_EQ|INE002A01018": "RELIANCE",
  "NSE:2885": "RELIANCE",
  "NSE_EQ|INE467B01029": "TCS",
  "NSE:11536": "TCS",
  "NSE_EQ|INE009A01021": "INFY",
  "NSE:1594": "INFY",
  "NSE_EQ|INE040A01034": "HDFCBANK",
  "NSE:1333": "HDFCBANK",
  "NSE_EQ|INE090A01021": "ICICIBANK",
  "NSE:4963": "ICICIBANK",
  "NSE_EQ|INE062A01020": "SBIN",
  "NSE:3045": "SBIN",
  "NSE_EQ|INE397D01024": "BHARTIARTL",
  "NSE:10604": "BHARTIARTL",
  "NSE_EQ|INE238A01034": "AXISBANK",
  "NSE:5900": "AXISBANK",
  "NSE_EQ|INE018A01030": "LT",
  "NSE:11483": "LT",
  "NSE_EQ|INE154A01025": "ITC",
  "NSE:1660": "ITC",
  "NSE_EQ|INE030A01027": "HINDUNILVR",
  "NSE:1394": "HINDUNILVR",
  "NSE_EQ|INE216A01030": "KOTAKBANK",
  "NSE:1922": "KOTAKBANK",
};

export function resolveScriptName(instrumentToken: string): string {
  if (!instrumentToken) return "UNKNOWN";
  if (KNOWN_SCRIPT_SYMBOLS[instrumentToken]) {
    return KNOWN_SCRIPT_SYMBOLS[instrumentToken];
  }
  if (instrumentToken.includes("|")) {
    const parts = instrumentToken.split("|");
    const suffix = parts[1]?.trim() ?? "";
    if (suffix && !suffix.startsWith("INE")) {
      return suffix.toUpperCase();
    }
  }
  if (instrumentToken.includes(":")) {
    const parts = instrumentToken.split(":");
    if (parts[1] && isNaN(Number(parts[1]))) {
      return parts[1].toUpperCase();
    }
  }
  return instrumentToken;
}
