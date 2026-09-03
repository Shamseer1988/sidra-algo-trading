"use client";

import { Plus, Save, SlidersHorizontal } from "lucide-react";
import { useEffect, useState } from "react";

import { api, type PaperStrategy, type StrategyDefinition, type StrategyMetric } from "../../components/api";
import { randomId } from "../../lib/formatting";

const editable = [
  "minimum_score",
  "minimum_rr",
  "volume_multiplier",
  "retest_tolerance_percent",
  "minimum_ema_spread_percent",
] as const;

export function StrategiesPanel({ isAdmin, onMessage }: { isAdmin: boolean; onMessage: (message: string) => void }) {
  const [items, setItems] = useState<PaperStrategy[]>([]);
  const [metrics, setMetrics] = useState<StrategyMetric[]>([]);
  const [definitions, setDefinitions] = useState<StrategyDefinition[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    void api.strategies()
      .then(setItems)
      .catch((error: unknown) => onMessage(error instanceof Error ? error.message : "Could not load strategies"))
      .finally(() => setLoading(false));
  }, [onMessage]);

  useEffect(() => {
    void api.strategyMetrics().then(setMetrics).catch(() => setMetrics([]));
    void api.strategyDefinitions().then(setDefinitions).catch(() => setDefinitions([]));
  }, []);

  const change = (id: string, key: keyof PaperStrategy, value: string | boolean) => {
    setItems((current) => current.map((item) => item.id === id ? { ...item, [key]: typeof item[key] === "number" ? Number(value) : value } : item));
  };
  const add = () => {
    setItems((current) => {
      if (!current.length) return current;
      const type = definitions[0]?.identifier ?? current[0].strategy_type;
      const label = definitions[0]?.name ?? "Strategy";
      return [...current, { ...current[0], id: randomId(), name: `${label} ${current.length + 1}`, strategy_type: type, enabled: false, version: 1 }];
    });
  };
  const save = async () => {
    try {
      setItems(await api.updateStrategies(items));
      onMessage("Strategies saved.");
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Could not save strategies");
    }
  };

  return (
    <section>
      <div className="page-toolbar">
        <div>
          <p className="eyebrow">Paper scanner configuration</p>
          <h2 className="page-title">Strategies</h2>
          <p className="page-copy">Versioned definitions govern deterministic paper-only evaluation. Saving a material change increments that strategy’s configuration version.</p>
        </div>
        {isAdmin && <div className="flex gap-2"><button className="secondary-button" onClick={add} disabled={!items.length}><Plus className="h-4 w-4" />Add</button><button className="primary-button" onClick={() => void save()} disabled={loading}><Save className="h-4 w-4" />Save strategies</button></div>}
      </div>

      {metrics.length > 0 && <div className="mt-5 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">{metrics.map((metric) => <article key={`${metric.strategy_id}-${metric.strategy_version}`} className="glass-inset rounded-md p-4"><p className="truncate text-xs font-semibold text-white">{metric.strategy_name} · v{metric.strategy_version}</p><p className="mt-3 numeric text-2xl font-semibold text-emerald-300">{metric.acceptance_rate}%</p><p className="mt-1 text-xs text-slate-500">{metric.accepted} accepted · {metric.rejected} rejected · {metric.watching} watching</p></article>)}</div>}

      <div className="mt-6 space-y-4">
        {loading ? <StrategySkeleton /> : items.map((item) => <article key={item.id} className="panel p-5">
          <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
            <div className="min-w-0">
              <input aria-label={`Strategy name ${item.name}`} disabled={!isAdmin} className="field-input max-w-md font-medium" value={item.name} onChange={(event) => change(item.id, "name", event.target.value)} />
              <div className="mt-2 flex items-center gap-2 text-xs text-slate-500">
                <select aria-label={`Strategy type ${item.name}`} disabled={!isAdmin || !definitions.length} className="field-input h-8 py-0 text-xs" value={item.strategy_type} onChange={(event) => change(item.id, "strategy_type", event.target.value)}>
                  {(definitions.length ? definitions : [{ identifier: item.strategy_type, name: item.strategy_type, prerequisites: [] }]).map((def) => <option key={def.identifier} value={def.identifier}>{def.name}</option>)}
                </select>
                <span>version {item.version}</span>
              </div>
            </div>
            <label className="toggle-row"><input disabled={!isAdmin} type="checkbox" checked={item.enabled} onChange={(event) => change(item.id, "enabled", event.target.checked)} /><span>{item.enabled ? "Enabled" : "Paused"}</span></label>
          </div>
          <div className="mt-5 grid gap-3 sm:grid-cols-2 xl:grid-cols-5">{editable.map((key) => <label key={key} className="field-label">{key.replaceAll("_", " ")}<input disabled={!isAdmin} className="field-input mt-2" type="number" step="any" value={item[key]} onChange={(event) => change(item.id, key, event.target.value)} /></label>)}</div>
          <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-6">
            <label className="field-label">Universe (comma-separated; blank = scanner universe)<input disabled={!isAdmin} className="field-input mt-2" value={item.universe.join(", ")} onChange={(event) => setItems((current) => current.map((value) => value.id === item.id ? { ...value, universe: event.target.value.split(",").map((instrument) => instrument.trim()).filter(Boolean) } : value))} /></label>
            <label className="field-label">Allowed sides<input disabled={!isAdmin} className="field-input mt-2" value={item.allowed_sides.join(", ")} onChange={(event) => setItems((current) => current.map((value) => value.id === item.id ? { ...value, allowed_sides: event.target.value.split(",").map((side) => side.trim().toUpperCase()).filter(Boolean) } : value))} /></label>
            <label className="field-label">Session policy<select disabled={!isAdmin} className="field-input mt-2" value={item.allowed_sessions.includes("REGULAR") ? "REGULAR" : "DISABLED"} onChange={(event) => setItems((current) => current.map((value) => value.id === item.id ? { ...value, allowed_sessions: event.target.value === "REGULAR" ? ["REGULAR"] : [] } : value))}><option value="REGULAR">Regular NSE session</option><option value="DISABLED">No trading session</option></select></label>
            <label className="field-label">Risk per trade (%)<input disabled={!isAdmin} className="field-input mt-2" type="number" min="0.01" max="5" step="0.01" value={item.risk_per_trade_percent ?? ""} onChange={(event) => setItems((current) => current.map((value) => value.id === item.id ? { ...value, risk_per_trade_percent: event.target.value === "" ? null : Number(event.target.value) } : value))} /></label>
            <label className="field-label">Maximum trades/day<input disabled={!isAdmin} className="field-input mt-2" type="number" value={item.max_trades_per_day} onChange={(event) => change(item.id, "max_trades_per_day", event.target.value)} /></label>
            <label className="field-label">Max trades/side (blank = no cap)<input disabled={!isAdmin} className="field-input mt-2" type="number" min="1" max="20" value={item.max_trades_per_side ?? ""} onChange={(event) => setItems((current) => current.map((value) => value.id === item.id ? { ...value, max_trades_per_side: event.target.value === "" ? null : Number(event.target.value) } : value))} /></label>
            <label className="field-label">Cooldown minutes<input disabled={!isAdmin} className="field-input mt-2" type="number" value={item.cooldown_minutes} onChange={(event) => change(item.id, "cooldown_minutes", event.target.value)} /></label>
            {item.strategy_type === "rs-pullback-v1" && <label className="field-label">RS threshold % (blank = default)<input disabled={!isAdmin} className="field-input mt-2" type="number" min="0" max="10" step="0.05" value={item.rs_threshold_percent ?? ""} onChange={(event) => setItems((current) => current.map((value) => value.id === item.id ? { ...value, rs_threshold_percent: event.target.value === "" ? null : Number(event.target.value) } : value))} /></label>}
          </div>
        </article>)}
        {!loading && !items.length && <article className="empty-inset mt-6 text-center"><SlidersHorizontal className="mx-auto h-6 w-6 text-slate-500" /><p className="mt-3 text-sm text-slate-400">No persisted paper strategies are available.</p></article>}
      </div>
    </section>
  );
}

function StrategySkeleton() {
  return <div className="panel p-5"><div className="skeleton h-10 max-w-md" /><div className="mt-5 grid gap-3 sm:grid-cols-2 xl:grid-cols-5">{Array.from({ length: 5 }, (_, index) => <div key={index} className="skeleton h-20" />)}</div></div>;
}
