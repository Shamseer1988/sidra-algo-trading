"use client";

import { Plus, Save, SlidersHorizontal } from "lucide-react";
import { useEffect, useState } from "react";

import { api, type PaperStrategy } from "../../components/api";

const editable = ["minimum_score", "minimum_rr", "volume_multiplier", "retest_tolerance_percent", "minimum_ema_spread_percent"] as const;

export function StrategiesPanel({ isAdmin, onMessage }: { isAdmin: boolean; onMessage: (message: string) => void }) {
  const [items, setItems] = useState<PaperStrategy[]>([]);
  const [loading, setLoading] = useState(true);
  useEffect(() => { void api.strategies().then(setItems).catch((error: unknown) => onMessage(error instanceof Error ? error.message : "Could not load strategies")).finally(() => setLoading(false)); }, [onMessage]);
  const change = (id: string, key: keyof PaperStrategy, value: string | boolean) => setItems((current) => current.map((item) => item.id === id ? { ...item, [key]: typeof item[key] === "number" ? Number(value) : value } : item));
  const add = () => setItems((current) => current.length ? [...current, { ...current[0], id: crypto.randomUUID(), name: `ORB Retest ${current.length + 1}`, enabled: false, version: 1 }] : current);
  const save = async () => { try { setItems(await api.updateStrategies(items)); onMessage("Strategies saved."); } catch (error) { onMessage(error instanceof Error ? error.message : "Could not save strategies"); } };
  return <section><div className="page-toolbar"><div><p className="eyebrow">Paper scanner configuration</p><h2 className="page-title">Strategies</h2><p className="page-copy">Persisted strategies dynamically govern paper-only scanner evaluation. They cannot submit an order.</p></div>{isAdmin && <div className="flex gap-2"><button className="secondary-button" onClick={add} disabled={!items.length}><Plus className="h-4 w-4" />Add</button><button className="primary-button" onClick={() => void save()} disabled={loading}><Save className="h-4 w-4" />Save strategies</button></div>}</div><div className="mt-6 space-y-4">{loading ? <StrategySkeleton /> : items.map((item) => <article key={item.id} className="panel p-5"><div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between"><div className="min-w-0"><input aria-label={`Strategy name ${item.name}`} disabled={!isAdmin} className="field-input max-w-md font-medium" value={item.name} onChange={(event) => change(item.id, "name", event.target.value)} /><p className="mt-2 text-xs text-slate-500">{item.strategy_type} · version {item.version}</p></div><label className="toggle-row"><input disabled={!isAdmin} type="checkbox" checked={item.enabled} onChange={(event) => change(item.id, "enabled", event.target.checked)} /><span>{item.enabled ? "Enabled" : "Paused"}</span></label></div><div className="mt-5 grid gap-3 sm:grid-cols-2 xl:grid-cols-5">{editable.map((key) => <label key={key} className="field-label">{key.replaceAll("_", " ")}<input disabled={!isAdmin} className="field-input mt-2" type="number" step="any" value={item[key]} onChange={(event) => change(item.id, key, event.target.value)} /></label>)}</div></article>)}{!loading && !items.length && <article className="empty-inset mt-6 text-center"><SlidersHorizontal className="mx-auto h-6 w-6 text-slate-500" /><p className="mt-3 text-sm text-slate-400">No persisted paper strategies are available.</p></article>}</div></section>;
}

function StrategySkeleton() { return <div className="panel p-5"><div className="skeleton h-10 max-w-md" /><div className="mt-5 grid gap-3 sm:grid-cols-2 xl:grid-cols-5">{Array.from({ length: 5 }, (_, index) => <div key={index} className="skeleton h-20" />)}</div></div>; }
