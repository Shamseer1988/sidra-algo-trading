import {
  Activity,
  BriefcaseBusiness,
  CalendarRange,
  ClipboardList,
  DatabaseZap,
  Ghost,
  Landmark,
  LineChart,
  RadioTower,
  ScrollText,
  Settings2,
  ShieldAlert,
  SlidersHorizontal,
  TimerReset,
  WalletCards,
  type LucideIcon,
} from "lucide-react";

/**
 * Seven places to work, and a drawer for the instruments.
 *
 * The old menu had twenty-four entries across five sections, three of which
 * rendered "unavailable", two of which were the same component behind different
 * labels, and one of which (Telegram) was a second copy of a card already shown
 * inside Risk. An operator looking for "where do I see what I traded" had five
 * plausible answers and no correct one.
 *
 * The rule now: a primary entry is somewhere you go during a trading day. A
 * related view that answers the same question is a tab inside it, not a
 * sibling in the menu. Everything that exists to diagnose the machine rather
 * than to trade moves to Admin & diagnostics, which is collapsed by default.
 *
 * Moving a screen into Admin changes navigation only. Every service, gate and
 * check behind those screens keeps running exactly as before — hiding a page
 * has never been a way to switch anything off in this system, and the readiness
 * gates in particular are enforced server-side whether or not anybody is
 * looking at them.
 */

export type WorkspaceId =
  // Primary
  | "dashboard"
  | "strategies"
  | "scanner"
  | "orders"
  | "history"
  | "risk"
  | "settings"
  // Admin & diagnostics
  | "backtesting"
  | "oms"
  | "shadow"
  | "liveGates"
  | "upstox"
  | "firstock"
  | "scheduler"
  | "system"
  | "audit";

export type NavigationItem = { id: WorkspaceId; label: string; icon: LucideIcon };
export type NavigationSection = { label: string; items: NavigationItem[]; collapsible?: boolean };

export const navigationSections: NavigationSection[] = [
  {
    label: "Trading",
    items: [
      { id: "dashboard", label: "Dashboard", icon: Activity },
      { id: "strategies", label: "Strategies", icon: SlidersHorizontal },
      { id: "scanner", label: "Scanner & Universe", icon: RadioTower },
      { id: "orders", label: "Orders & Positions", icon: BriefcaseBusiness },
      { id: "history", label: "History", icon: CalendarRange },
      { id: "risk", label: "Risk", icon: ShieldAlert },
      { id: "settings", label: "Settings", icon: Settings2 },
    ],
  },
  {
    label: "Admin & diagnostics",
    collapsible: true,
    items: [
      { id: "backtesting", label: "Backtesting", icon: LineChart },
      { id: "oms", label: "OMS", icon: ClipboardList },
      { id: "shadow", label: "Shadow comparison", icon: Ghost },
      { id: "liveGates", label: "Live readiness", icon: ShieldAlert },
      { id: "upstox", label: "Upstox console", icon: Landmark },
      { id: "firstock", label: "Firstock console", icon: WalletCards },
      { id: "scheduler", label: "Scheduler", icon: TimerReset },
      { id: "system", label: "System health", icon: DatabaseZap },
      { id: "audit", label: "Audit log", icon: ScrollText },
    ],
  },
];

export const workspaceMeta: Record<WorkspaceId, { eyebrow: string; title: string }> = {
  dashboard: { eyebrow: "Operations overview", title: "Dashboard" },
  strategies: { eyebrow: "Scanner configuration", title: "Strategies" },
  scanner: { eyebrow: "What we are watching today", title: "Scanner & Universe" },
  orders: { eyebrow: "Execution workspace", title: "Orders & Positions" },
  history: { eyebrow: "Trading record", title: "History" },
  risk: { eyebrow: "Safety controls", title: "Risk" },
  settings: { eyebrow: "Configuration", title: "Settings" },
  backtesting: { eyebrow: "Research", title: "Backtesting" },
  oms: { eyebrow: "Execution lifecycle", title: "OMS operations" },
  shadow: { eyebrow: "Zero-submission comparison", title: "Shadow comparison" },
  liveGates: { eyebrow: "Pre-live checklist", title: "Live readiness" },
  upstox: { eyebrow: "Broker connectivity", title: "Upstox console" },
  firstock: { eyebrow: "Broker connectivity", title: "Firstock console" },
  scheduler: { eyebrow: "Automation", title: "Scheduler" },
  system: { eyebrow: "Infrastructure", title: "System health" },
  audit: { eyebrow: "Security & operations", title: "Audit log" },
};

/**
 * The tabs inside a workspace.
 *
 * Each one used to be its own menu entry. They are grouped here because they
 * answer the same question as the workspace that holds them — "what are we
 * looking at today", "what is open", "what happened" — and a menu that lists
 * four answers to one question makes the reader choose before they have the
 * information to choose with.
 *
 * The first tab is the default, and the ids are stable because they appear in
 * the URL hash and in tests.
 */
export type WorkspaceTab = { id: string; label: string };

export const workspaceTabs: Partial<Record<WorkspaceId, WorkspaceTab[]>> = {
  scanner: [
    { id: "scanner", label: "Scanner" },
    { id: "universe", label: "Universe" },
    { id: "signals", label: "Signals" },
    { id: "market", label: "Market" },
  ],
  orders: [
    { id: "orders", label: "Orders" },
    { id: "positions", label: "Positions" },
  ],
  history: [
    { id: "trades", label: "Trades" },
    { id: "journal", label: "Signal journal" },
  ],
  settings: [
    { id: "trading", label: "Trading controls" },
    { id: "indicators", label: "Indicator periods" },
    { id: "alerts", label: "Alerts" },
    { id: "data", label: "Market data" },
    { id: "security", label: "Sessions" },
  ],
};

export function defaultTab(workspace: WorkspaceId): string | null {
  return workspaceTabs[workspace]?.[0]?.id ?? null;
}
