"use client";

import { Activity, BellRing, Database, Radio, RefreshCw, Wifi } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { api, type OmsReconciliation, type Overview, type ScannerStatus } from "../../components/api";
import { statusTone, titleCase } from "../../lib/formatting";

const serviceIcons = { api: Wifi, database: Database, redis: Activity, scanner: Radio, market: Radio, firstock: Radio, telegram: BellRing };

export function SystemHealthPanel({ overview, scanner }: { overview: Overview; scanner: ScannerStatus }) {
  const [reconciliation, setReconciliation] = useState<OmsReconciliation | null>(null);
  const [reconciliationLoading, setReconciliationLoading] = useState(true);
  const loadReconciliation = useCallback(() => {
    setReconciliationLoading(true);
    void api.omsReconciliations().then((checks) => setReconciliation(checks[0] ?? null)).finally(() => setReconciliationLoading(false));
  }, []);
  useEffect(loadReconciliation, [loadReconciliation]);
  const services = [
    ["Application API", overview.api, "api"], ["PostgreSQL", overview.database, "database"], ["Redis", overview.redis, "redis"], ["Scanner worker", { status: scanner.status, detail: scanner.detail }, "scanner"], ["Market data", overview.market_data, "market"], ["Firstock", overview.firstock, "firstock"], ["Telegram", overview.telegram, "telegram"], ["Startup reconciliation", { status: reconciliation?.status.toLowerCase() ?? "not_run", detail: reconciliation?.detail ?? "No reconciliation checkpoint has been recorded yet." }, "api"],
  ] as const;
  return <section><div className="page-toolbar"><div><p className="eyebrow">Infrastructure</p><h2 className="page-title">System health</h2><p className="page-copy">Backend-checked service state and the latest durable OMS reconciliation for the active PAPER environment.</p></div><button className="secondary-button" onClick={loadReconciliation} disabled={reconciliationLoading}><RefreshCw className={`h-4 w-4 ${reconciliationLoading ? "animate-spin" : ""}`} />Refresh checks</button></div><div className="mt-6 grid gap-4 sm:grid-cols-2 xl:grid-cols-3">{services.map(([name, item, icon]) => { const Icon = serviceIcons[icon]; return <article key={name} className="panel p-5"><div className="flex items-center justify-between gap-3"><span className="flex items-center gap-2 text-sm font-semibold text-white"><Icon className="h-4 w-4 text-slate-500" />{name}</span><span className={`status-pill ${statusTone(item.status)}`}>{titleCase(item.status)}</span></div><p className="mt-4 min-h-10 text-sm leading-5 text-slate-400">{item.detail}</p></article>; })}</div></section>;
}
