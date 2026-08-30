import { Activity, Database, Radio, ShieldCheck } from "lucide-react";

const services = [
  { label: "Market feed", detail: "Not configured", icon: Radio, tone: "text-slate-400" },
  { label: "Database", detail: "Checking backend", icon: Database, tone: "text-slate-400" },
  { label: "Scanner", detail: "Foundation ready", icon: Activity, tone: "text-watch" },
  { label: "Safety mode", detail: "PAPER · live orders locked", icon: ShieldCheck, tone: "text-bullish" },
];

export default function Home() {
  return (
    <main className="min-h-screen bg-terminal-950 p-5 sm:p-8">
      <div className="mx-auto max-w-6xl">
        <header className="flex flex-col gap-4 border-b border-slate-800 pb-6 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <p className="text-xs font-semibold tracking-[0.24em] text-bullish">INTRADAY SENTINEL</p>
            <h1 className="mt-2 text-3xl font-semibold tracking-tight text-white">Market intelligence, paper first.</h1>
            <p className="mt-2 max-w-2xl text-sm text-slate-400">Phase 1 foundation is active. Data feeds and signal logic will appear here only after backend validation.</p>
          </div>
          <span className="inline-flex w-fit items-center gap-2 rounded-md border border-emerald-900/80 bg-emerald-950/40 px-3 py-2 text-xs font-medium text-bullish">
            <span className="h-2 w-2 rounded-full bg-bullish" /> PAPER MODE
          </span>
        </header>

        <section className="mt-8 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {services.map(({ label, detail, icon: Icon, tone }) => (
            <article key={label} className="rounded-lg border border-slate-800 bg-terminal-900 p-4 shadow-sm">
              <Icon className={`h-5 w-5 ${tone}`} aria-hidden="true" />
              <p className="mt-6 text-sm font-medium text-slate-200">{label}</p>
              <p className="mt-1 text-xs text-slate-500">{detail}</p>
            </article>
          ))}
        </section>

        <section className="mt-6 rounded-lg border border-slate-800 bg-terminal-900 p-6">
          <h2 className="text-base font-semibold text-white">Next delivery: secure application shell</h2>
          <p className="mt-2 max-w-3xl text-sm leading-6 text-slate-400">Authentication, role-aware settings, and detailed system health are next. No sample prices are rendered as live data, and there is no order-execution capability in this release.</p>
        </section>
      </div>
    </main>
  );
}
