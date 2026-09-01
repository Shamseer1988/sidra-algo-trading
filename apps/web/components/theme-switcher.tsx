"use client";

import { Monitor, Moon, Sun } from "lucide-react";
import { useEffect, useState } from "react";

type ThemePreference = "light" | "dark" | "system";

function applyTheme(preference: ThemePreference) {
  const resolved = preference === "system"
    ? window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"
    : preference;
  document.documentElement.dataset.theme = resolved;
  document.documentElement.style.colorScheme = resolved;
}

export function ThemeSwitcher() {
  const [preference, setPreference] = useState<ThemePreference>("system");

  useEffect(() => {
    const stored = window.localStorage.getItem("sidra-theme") as ThemePreference | null;
    const next = stored && ["light", "dark", "system"].includes(stored) ? stored : "system";
    setPreference(next);
    applyTheme(next);
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const listener = () => { if (next === "system") applyTheme("system"); };
    media.addEventListener("change", listener);
    return () => media.removeEventListener("change", listener);
  }, []);

  const Icon = preference === "light" ? Sun : preference === "dark" ? Moon : Monitor;
  return (
    <label className="theme-control" title="Choose display theme">
      <Icon className="h-3.5 w-3.5" aria-hidden="true" />
      <span className="sr-only">Theme</span>
      <select
        aria-label="Theme"
        value={preference}
        onChange={(event) => {
          const next = event.target.value as ThemePreference;
          setPreference(next);
          window.localStorage.setItem("sidra-theme", next);
          applyTheme(next);
        }}
      >
        <option value="light">Light</option>
        <option value="dark">Dark</option>
        <option value="system">System</option>
      </select>
    </label>
  );
}

export function MarketClock() {
  const [value, setValue] = useState("");
  useEffect(() => {
    const update = () => setValue(new Intl.DateTimeFormat("en-IN", {
      timeZone: "Asia/Kolkata",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(new Date()));
    update();
    const timer = window.setInterval(update, 1000);
    return () => window.clearInterval(timer);
  }, []);
  return <span className="market-clock"><span>IST</span>{value || "--:--:--"}</span>;
}
