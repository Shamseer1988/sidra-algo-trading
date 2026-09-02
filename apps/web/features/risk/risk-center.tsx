"use client";

import { ShieldCheck } from "lucide-react";
import { useEffect, useState } from "react";

import { api, type PaperRiskSummary, type SafetyStatus, type TelegramStatus } from "../../components/api";
import { formatPrice } from "../../lib/formatting";
import { ControlPanel } from "../controls/control-panel";

const emptyRisk: PaperRiskSummary = { session_date: "", daily_risk_limit: 0, daily_risk_allocated: 0, daily_risk_available: 0, maximum_open_positions: 0, active_reservations: 0, open_positions: 0, exposure_limit: 0, current_exposure: 0, exposure_available: 0, rejected_reservations: 0 };

export function RiskCenter({ safety, telegram, canOperate, isAdmin, onEmergency, onClear, onPaper, onTelegram }: { safety: SafetyStatus; telegram: TelegramStatus; canOperate: boolean; isAdmin: boolean; onEmergency: () => void; onClear: () => void; onPaper: () => void; onTelegram: () => void }) {
  const [risk, setRisk] = useState<PaperRiskSummary>(emptyRisk);
  useEffect(() => { void api.paperRiskSummary().then(setRisk).catch(() => setRisk(emptyRisk)); }, []);
  return <section><div className="page-toolbar"><div><p className="eyebrow">Transactional paper controls</p><h2 className="page-title">Risk center</h2><p className="page-copy">Verified reservation capacity gates every simulated entry. Daily allocations remain recorded after a paper position closes.</p></div><ShieldCheck className="h-8 w-8 text-emerald-300" /></div><div className="mt-5 grid gap-3 sm:grid-cols-2 xl:grid-cols-4"><RiskMetric label="Daily allocation" value={`₹${formatPrice(risk.daily_risk_allocated)} / ₹${formatPrice(risk.daily_risk_limit)}`} /><RiskMetric label="Available risk" value={`₹${formatPrice(risk.daily_risk_available)}`} /><RiskMetric label="Open capacity" value={`${risk.active_reservations}/${risk.maximum_open_positions}`} /><RiskMetric label="Exposure available" value={`₹${formatPrice(risk.exposure_available)}`} /></div><div className="mt-6"><ControlPanel safety={safety} telegram={telegram} canOperate={canOperate} isAdmin={isAdmin} onEmergency={onEmergency} onClear={onClear} onPaper={onPaper} onTelegram={onTelegram} /></div></section>;
}

function RiskMetric({ label, value }: { label: string; value: string }) { return <article className="glass-inset rounded-md p-4"><p className="eyebrow">{label}</p><p className="mt-2 numeric text-xl font-semibold text-white">{value}</p></article>; }
