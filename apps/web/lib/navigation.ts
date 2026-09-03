import {
  Activity,
  AreaChart,
  BarChart3,
  BellRing,
  BookOpenCheck,
  Bot,
  BriefcaseBusiness,
  CandlestickChart,
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
  UsersRound,
  WalletCards,
  type LucideIcon,
} from "lucide-react";

export type WorkspaceId =
  | "overview"
  | "market"
  | "scanner"
  | "signals"
  | "strategies"
  | "orders"
  | "positions"
  | "oms"
  | "shadow"
  | "assisted"
  | "risk"
  | "performance"
  | "backtesting"
  | "journal"
  | "upstox"
  | "firstock"
  | "automation"
  | "scheduler"
  | "telegram"
  | "system"
  | "audit"
  | "liveGates"
  | "users"
  | "settings";

export type NavigationItem = { id: WorkspaceId; label: string; icon: LucideIcon; available: boolean };
export type NavigationSection = { label: string; items: NavigationItem[] };

export const navigationSections: NavigationSection[] = [
  {
    label: "Trading",
    items: [
      { id: "overview", label: "Overview", icon: Activity, available: true },
      { id: "market", label: "Market", icon: CandlestickChart, available: true },
      { id: "scanner", label: "Scanner", icon: RadioTower, available: true },
      { id: "signals", label: "Signals", icon: BarChart3, available: true },
      { id: "strategies", label: "Strategies", icon: SlidersHorizontal, available: true },
      { id: "orders", label: "Orders", icon: ClipboardList, available: true },
      { id: "positions", label: "Positions", icon: BriefcaseBusiness, available: true },
      { id: "oms", label: "OMS", icon: ClipboardList, available: true },
      { id: "shadow", label: "Shadow Mode", icon: Ghost, available: true },
      { id: "assisted", label: "Assisted Trading", icon: Bot, available: true },
    ],
  },
  {
    label: "Risk & analytics",
    items: [
      { id: "risk", label: "Risk Center", icon: ShieldAlert, available: true },
      { id: "performance", label: "Performance", icon: AreaChart, available: false },
      { id: "backtesting", label: "Backtesting", icon: LineChart, available: true },
      { id: "journal", label: "Journal", icon: BookOpenCheck, available: true },
    ],
  },
  {
    label: "Brokers",
    items: [
      { id: "upstox", label: "Upstox", icon: Landmark, available: true },
      { id: "firstock", label: "Firstock", icon: WalletCards, available: true },
    ],
  },
  {
    label: "Automation",
    items: [
      { id: "automation", label: "Automation Rules", icon: Bot, available: false },
      { id: "scheduler", label: "Scheduler", icon: TimerReset, available: true },
      { id: "telegram", label: "Telegram", icon: BellRing, available: true },
    ],
  },
  {
    label: "System",
    items: [
      { id: "system", label: "System Health", icon: DatabaseZap, available: true },
      { id: "audit", label: "Audit Log", icon: ScrollText, available: true },
      { id: "liveGates", label: "Live Gates", icon: ShieldAlert, available: true },
      { id: "users", label: "Users", icon: UsersRound, available: false },
      { id: "settings", label: "Settings", icon: Settings2, available: true },
    ],
  },
];

export const workspaceMeta: Record<WorkspaceId, { eyebrow: string; title: string }> = {
  overview: { eyebrow: "Operations overview", title: "Paper command center" },
  market: { eyebrow: "Market intelligence", title: "Market state" },
  scanner: { eyebrow: "Scanner operations", title: "Scanner workspace" },
  signals: { eyebrow: "Paper scanner output", title: "Signals" },
  strategies: { eyebrow: "Paper scanner configuration", title: "Strategies" },
  orders: { eyebrow: "Execution workspace", title: "Orders" },
  positions: { eyebrow: "Execution workspace", title: "Positions" },
  oms: { eyebrow: "Execution lifecycle", title: "OMS operations" },
  shadow: { eyebrow: "Zero-submission comparison", title: "Shadow mode" },
  assisted: { eyebrow: "Controlled paper decisions", title: "Assisted trading" },
  risk: { eyebrow: "Safety controls", title: "Risk center" },
  performance: { eyebrow: "Analytics workspace", title: "Performance" },
  backtesting: { eyebrow: "Research workspace", title: "Backtesting" },
  journal: { eyebrow: "Paper tracking", title: "Journal" },
  upstox: { eyebrow: "Paper market data", title: "Upstox" },
  firstock: { eyebrow: "Market data", title: "Firstock" },
  automation: { eyebrow: "Automation workspace", title: "Automation rules" },
  scheduler: { eyebrow: "Automation workspace", title: "Scheduler" },
  telegram: { eyebrow: "Notifications", title: "Telegram" },
  system: { eyebrow: "Infrastructure", title: "System health" },
  audit: { eyebrow: "Security & operations", title: "Audit log" },
  liveGates: { eyebrow: "Future execution controls", title: "Live readiness gates" },
  users: { eyebrow: "Administration", title: "Users" },
  settings: { eyebrow: "Configuration", title: "Settings" },
};
