"use client";

import { BellRing, Menu, Radio, ShieldAlert, ShieldCheck, Wifi } from "lucide-react";

import type { Overview, SafetyStatus, ScannerStatus, User } from "../api";
import { statusTone, titleCase } from "../../lib/formatting";
import { workspaceMeta, type WorkspaceId } from "../../lib/navigation";
import { MarketClock, ThemeSwitcher } from "../theme-switcher";

export function TerminalHeader({
  active,
  overview,
  scanner,
  safety,
  user,
  onOpenNavigation,
  onOpenControls,
}: {
  active: WorkspaceId;
  overview: Overview;
  scanner: ScannerStatus;
  safety: SafetyStatus;
  user: User;
  onOpenNavigation: () => void;
  onOpenControls: () => void;
}) {
  const broker = overview.market_data.detail.toLowerCase().includes("upstox") ? "Upstox" : overview.firstock.status === "configured" ? "Firstock" : "No feed";
  const brokerTone = broker !== "No feed" && overview.market_data.status.toLowerCase() !== "disconnected" ? statusTone(overview.market_data.status) : "status-bad";
  const meta = workspaceMeta[active];
  return (
    <header className="sticky top-0 z-10 flex min-h-16 items-center justify-between gap-3 border-b border-slate-800 px-4 py-2 sm:px-6">
      <div className="flex min-w-0 items-center gap-3">
        <button aria-label="Open navigation" className="rounded-md p-2 text-slate-400 lg:hidden" onClick={onOpenNavigation}>
          <Menu className="h-5 w-5" />
        </button>
        <div className="min-w-0">
          <p className="truncate text-[10px] font-semibold uppercase tracking-[.15em] text-emerald-400">{meta.eyebrow}</p>
          <h1 className="truncate text-sm font-semibold text-white">Sidra Command Center</h1>
          <p className="truncate text-[11px] text-slate-500">{meta.title}</p>
        </div>
      </div>
      <div className="flex items-center justify-end gap-2">
        <MarketClock />
        <span className={`status-pill hidden xl:inline-flex ${brokerTone}`} title={`Selected market-data connector: ${broker} (${titleCase(overview.market_data.status)})`}><Wifi className="h-3 w-3" />{broker}</span>
        <span className={`status-pill hidden md:inline-flex ${statusTone(overview.market_data.status)}`} title={overview.market_data.detail}><Wifi className="h-3 w-3" />{titleCase(overview.market_data.status)}</span>
        <span className={`status-pill hidden sm:inline-flex ${statusTone(scanner.status)}`} title={scanner.detail}><Radio className="h-3 w-3" />{titleCase(scanner.status)}</span>
        <span className="status-pill status-good" title="Live execution is locked"><ShieldCheck className="h-3.5 w-3.5" />PAPER</span>
        <button className={`icon-button ${safety.emergency_stop_active ? "text-rose-300" : "text-slate-400"}`} title="Open emergency controls" onClick={onOpenControls}>
          {safety.emergency_stop_active ? <ShieldAlert className="h-4 w-4" /> : <BellRing className="h-4 w-4" />}
          <span className="sr-only">Open emergency controls</span>
        </button>
        <details className="hidden sm:block"><summary className="user-menu cursor-pointer list-none"><span>{user.email.slice(0, 1).toUpperCase()}</span></summary><div className="user-menu-popover"><p className="truncate text-xs font-medium text-white">{user.email}</p><p className="mt-1 text-[10px] uppercase tracking-wide text-slate-500">Account menu</p></div></details>
        <ThemeSwitcher />
      </div>
    </header>
  );
}
