"use client";

import { Download, Radio, Search } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { api, type MarketCandle, type PaperSignal } from "../../components/api";
import { formatIstTimestamp, formatPrice, titleCase } from "../../lib/formatting";

export function SignalsPanel({ signals }: { signals: PaperSignal[] }) {
  const [query, setQuery] = useState("");
  const [side, setSide] = useState("ALL");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const filtered = useMemo(
    () => signals.filter((signal) => (side === "ALL" || signal.side === side) && signal.instrument_token.toLowerCase().includes(query.toLowerCase())),
    [query, side, signals],
  );
  const selected = signals.find((signal) => signal.id === selectedId) ?? filtered[0];

  return (
    <section>
      <div className="page-toolbar">
        <div><p className="eyebrow">Paper scanner output</p><h2 className="page-title">Signals</h2><p className="page-copy">Completed-candle decisions only. Approval records intent; it never sends a broker order.</p></div>
        <div className="flex flex-wrap items-center gap-2">
          <label className="relative"><Search className="absolute left-3 top-3 h-4 w-4 text-slate-500" /><span className="sr-only">Find instrument</span><input data-testid="signals-search" className="field-input w-52 pl-9" placeholder="Find instrument" value={query} onChange={(event) => setQuery(event.target.value)} /></label>
          <select data-testid="signals-side-select" aria-label="Signal direction" className="field-input w-24" value={side} onChange={(event) => setSide(event.target.value)}><option value="ALL">All</option><option value="LONG">Long</option><option value="SHORT">Short</option></select>
          <a data-testid="export-csv-btn" href="/api/v1/journal/export.csv" download="paper-journal.csv" className="secondary-button"><Download className="h-4 w-4" />Export CSV</a>
        </div>
      </div>
      {!filtered.length ? <EmptySignals /> : <div className="mt-6 grid gap-5 xl:grid-cols-[1.45fr_.75fr]"><SignalsTable signals={filtered} selectedId={selected?.id} onSelect={setSelectedId} />{selected && <SignalDetail signal={selected} />}</div>}
    </section>
  );
}

function EmptySignals() {
  return <article className="panel mt-6 p-9 text-center"><Radio className="mx-auto h-7 w-7 text-slate-600" /><h3 className="mt-4 font-medium text-white">No paper signals yet</h3><p className="mx-auto mt-2 max-w-md text-sm leading-6 text-slate-500">This is expected until a selected market-data connector is configured, the scanner is running, and a completed candle qualifies.</p></article>;
}

function SignalsTable({ signals, selectedId, onSelect }: { signals: PaperSignal[]; selectedId?: string; onSelect: (id: string) => void }) {
  return <article className="panel overflow-hidden"><div className="table-scroll"><table className="terminal-table"><thead><tr><th>Instrument</th><th>Side</th><th>Entry</th><th>Target</th><th>Score</th><th>Time</th></tr></thead><tbody>{signals.map((signal) => <tr key={signal.id} onClick={() => onSelect(signal.id)} className={selectedId === signal.id ? "table-selected" : ""}><td><strong>{signal.instrument_token}</strong><span>{titleCase(signal.status)}</span></td><td><span className={`status-pill ${signal.side === "LONG" ? "status-good" : "status-bad"}`}>{signal.side}</span></td><td className="numeric">₹{formatPrice(signal.entry_price)}</td><td className="numeric">₹{formatPrice(signal.target_price)}</td><td className="numeric">{signal.score}/100</td><td className="muted-cell">{formatIstTimestamp(signal.created_at)}</td></tr>)}</tbody></table></div></article>;
}

function SignalDetail({ signal }: { signal: PaperSignal }) {
  const risk = Math.abs(signal.entry_price - signal.stop_price);
  const reward = Math.abs(signal.target_price - signal.entry_price);
  return <aside className="panel p-5"><div className="flex items-start justify-between gap-3"><div><p className="eyebrow">Selected paper signal</p><h3 className="mt-1 text-xl font-semibold text-white">{signal.instrument_token}</h3></div><span className={`status-pill ${signal.side === "LONG" ? "status-good" : "status-bad"}`}>{signal.side}</span></div><div className="signal-price-grid"><Value label="Entry" value={`₹${formatPrice(signal.entry_price)}`} /><Value label="Stop" value={`₹${formatPrice(signal.stop_price)}`} /><Value label="Target" value={`₹${formatPrice(signal.target_price)}`} /></div><PriceChart signal={signal} /><dl className="data-list"><div><dt>Quantity</dt><dd className="numeric">{signal.quantity}</dd></div><div><dt>Reward : risk</dt><dd>{risk ? `${(reward / risk).toFixed(2)}R` : "—"}</dd></div><div><dt>Status</dt><dd>{titleCase(signal.status)}</dd></div></dl><p className="mt-5 text-[11px] font-semibold uppercase tracking-[.16em] text-slate-500">Score breakdown</p><div className="mt-3 space-y-3">{Object.entries(signal.score_breakdown).map(([label, value]) => <div key={label}><div className="mb-1 flex justify-between text-xs"><span className="capitalize text-slate-400">{label.replaceAll("_", " ")}</span><span className="numeric text-slate-300">{value}/20</span></div><div className="score-track"><div className="score-fill" style={{ width: `${Math.min(value * 5, 100)}%` }} /></div></div>)}</div><p className="mt-6 text-xs leading-5 text-slate-500">Paper tracking only. This record cannot submit or approve a broker order.</p></aside>;
}

function Value({ label, value }: { label: string; value: string }) { return <div><p>{label}</p><strong className="numeric">{value}</strong></div>; }

function PriceChart({ signal }: { signal: PaperSignal }) {
  const [candles, setCandles] = useState<MarketCandle[]>([]);
  useEffect(() => { void api.candles(signal.instrument_token, signal.session_date).then(setCandles).catch(() => setCandles([])); }, [signal.instrument_token, signal.session_date]);
  const range = useMemo(() => ({ low: Math.min(...candles.map((item) => item.low), signal.stop_price), high: Math.max(...candles.map((item) => item.high), signal.target_price) }), [candles, signal.stop_price, signal.target_price]);
  const width = 300; const height = 112; const spread = Math.max(range.high - range.low, 0.01);
  const points = candles.map((item, index) => `${(index / Math.max(candles.length - 1, 1)) * width},${height - ((item.close - range.low) / spread) * height}`).join(" ");
  const marker = (value: number) => height - ((value - range.low) / spread) * height;
  return <section data-testid="candle-chart" className="mt-6"><div className="mb-3 flex items-center justify-between"><p className="text-[11px] font-semibold uppercase tracking-[.16em] text-slate-500">Completed-candle chart</p><span className="numeric text-xs text-slate-500">{candles.length} candles</span></div>{candles.length ? <svg viewBox={`0 0 ${width} ${height}`} className="signal-chart"><line x1="0" x2={width} y1={marker(signal.entry_price)} y2={marker(signal.entry_price)} stroke="var(--positive)" strokeDasharray="4 3" /><line x1="0" x2={width} y1={marker(signal.stop_price)} y2={marker(signal.stop_price)} stroke="var(--negative)" strokeDasharray="4 3" /><polyline points={points} fill="none" stroke="var(--foreground)" strokeWidth="2" /></svg> : <div className="empty-inset">No completed candles are available for this signal’s session.</div>}</section>;
}
