"use client";

import { Gauge, ListPlus, Plus, Save, Search, ShieldAlert, Undo2, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { Paged } from "../../components/paged";
import { api, type WatchSearchResult, type WatchShare, type WatchlistView } from "../../components/api";
import { Modal } from "../../components/modal";
import { formatIstTimestamp } from "../../lib/formatting";

/**
 * Strategies > Subscriptions: the shares the intraday scanner watches, and the
 * index they are measured against.
 *
 * Changes are staged on the screen -- each add confirmed against the full
 * company name, each removal marked -- and saved together after one
 * confirmation that lists everything that will change, says when it takes
 * effect, and that the live strategy may trade an added share with real money.
 */

type Staged = WatchShare & { added?: boolean };

export function SubscriptionsPanel({ isAdmin, onMessage }: { isAdmin: boolean; onMessage: (message: string) => void }) {
  const [view, setView] = useState<WatchlistView | null>(null);
  const [shares, setShares] = useState<Staged[]>([]);
  const [removed, setRemoved] = useState<Set<string>>(new Set());
  const [benchmark, setBenchmark] = useState("");
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<WatchSearchResult[]>([]);
  const [searching, setSearching] = useState(false);
  const [adding, setAdding] = useState<WatchSearchResult | null>(null);
  const [saving, setSaving] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [reason, setReason] = useState("");
  const [understood, setUnderstood] = useState(false);
  const [busy, setBusy] = useState(false);

  function take(next: WatchlistView) {
    setView(next);
    setShares(next.shares);
    setRemoved(new Set());
    setBenchmark(next.benchmark);
  }

  useEffect(() => {
    void api
      .watchlist()
      .then(take)
      .catch((error: unknown) => onMessage(error instanceof Error ? error.message : "Could not load the watchlist"));
  }, [onMessage]);

  useEffect(() => {
    const text = query.trim();
    if (text.length < 2) {
      setResults([]);
      return;
    }
    setSearching(true);
    const timer = window.setTimeout(() => {
      void api
        .searchWatchlist(text)
        .then(setResults)
        .catch(() => setResults([]))
        .finally(() => setSearching(false));
    }, 250);
    return () => window.clearTimeout(timer);
  }, [query]);

  const keys = useMemo(() => new Set(shares.map((share) => share.instrument_key)), [shares]);
  const added = shares.filter((share) => share.added);
  const removing = shares.filter((share) => removed.has(share.instrument_key));
  const kept = shares.filter((share) => !removed.has(share.instrument_key));
  const benchmarkChanged = view !== null && benchmark !== view.benchmark;
  const dirty = added.length > 0 || removing.length > 0 || benchmarkChanged;
  const nameOf = (key: string) => view?.benchmarks.find((item) => item.key === key)?.name ?? key;

  function confirmAdd() {
    if (!adding) return;
    setShares((current) => [
      ...current,
      { instrument_key: adding.instrument_key, symbol: adding.symbol, name: adding.name, in_nifty200: adding.in_nifty200, known: true, sector: adding.sector, added: true },
    ]);
    onMessage(`${adding.symbol} added to the list. Save to apply.`);
    setAdding(null);
    setQuery("");
    setResults([]);
  }

  function toggleRemove(share: Staged) {
    if (share.added) {
      setShares((current) => current.filter((item) => item.instrument_key !== share.instrument_key));
      return;
    }
    setRemoved((current) => {
      const next = new Set(current);
      if (next.has(share.instrument_key)) next.delete(share.instrument_key);
      else next.add(share.instrument_key);
      return next;
    });
  }

  function discard() {
    if (view) take(view);
  }

  async function save() {
    setBusy(true);
    try {
      take(await api.saveWatchlist({ instruments: kept.map((share) => share.instrument_key), benchmark, reason, confirm: true }));
      onMessage("Watchlist saved.");
      setSaving(false);
      setReason("");
      setUnderstood(false);
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Could not save the watchlist");
    } finally {
      setBusy(false);
    }
  }

  async function reset() {
    setBusy(true);
    try {
      take(await api.resetWatchlist());
      onMessage("Using the server's .env list again.");
      setResetting(false);
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Could not reset the watchlist");
    } finally {
      setBusy(false);
    }
  }

  if (!view) return <div className="panel mt-6 p-5"><div className="skeleton h-40" /></div>;

  return (
    <section className="mt-6 space-y-5" data-testid="subscriptions">
      <article className="panel p-5 sm:p-7">
        <div className="flex flex-wrap items-start justify-between gap-3 border-b border-slate-800 pb-4">
          <div className="flex items-center gap-3">
            <Gauge className="h-5 w-5 text-emerald-400" />
            <div>
              <p className="eyebrow">Benchmark</p>
              <h3 className="font-semibold text-white">What relative strength and the market regime are measured against</h3>
            </div>
          </div>
          <span className="text-xs text-slate-500">
            {view.source === "screen" ? `Saved on this screen${view.updated_at ? ` · ${formatIstTimestamp(view.updated_at)}` : ""}` : "From the server's .env file"}
          </span>
        </div>
        <label className="field-label mt-4 block max-w-sm">
          Benchmark index
          <select aria-label="Benchmark index" disabled={!isAdmin} className="field-input mt-1" value={benchmark} onChange={(event) => setBenchmark(event.target.value)}>
            {!view.benchmarks.some((item) => item.key === view.benchmark) && <option value={view.benchmark}>{view.benchmark_name}</option>}
            {view.benchmarks.map((item) => (
              <option key={item.key} value={item.key}>
                {item.name}
              </option>
            ))}
          </select>
        </label>
        {benchmarkChanged && (
          <p className="mt-2 text-xs text-amber-400">
            Changing the benchmark changes what the live RS Pullback calls &quot;strong&quot;. Its tested results were measured against NIFTY 50.
          </p>
        )}
      </article>

      <article className="panel p-5 sm:p-7">
        <div className="flex flex-wrap items-start justify-between gap-3 border-b border-slate-800 pb-4">
          <div className="flex items-center gap-3">
            <ListPlus className="h-5 w-5 text-emerald-400" />
            <div>
              <p className="eyebrow">Watchlist · {kept.length} of at most {view.max_shares}</p>
              <h3 className="font-semibold text-white">The shares the intraday scanner streams and can trade</h3>
              <p className="mt-1 text-xs text-slate-400">{view.applies}</p>
            </div>
          </div>
          {isAdmin && (
            <div className="flex flex-wrap gap-2">
              {dirty && (
                <button className="secondary-button" onClick={discard}>
                  <Undo2 className="h-4 w-4" />
                  Discard changes
                </button>
              )}
              <button className="primary-button" disabled={!dirty} onClick={() => setSaving(true)}>
                <Save className="h-4 w-4" />
                Save changes
              </button>
            </div>
          )}
        </div>

        {isAdmin && (
          <div className="relative mt-4 max-w-xl">
            <label className="field-label" htmlFor="share-search">
              Add a share
            </label>
            <div className="relative mt-1">
              <Search className="pointer-events-none absolute left-3 top-3 h-4 w-4 text-slate-500" />
              <input
                id="share-search"
                className="field-input pl-9"
                placeholder="Type a symbol or company name, e.g. ADANIPORTS"
                value={query}
                autoComplete="off"
                onChange={(event) => setQuery(event.target.value)}
              />
            </div>
            {query.trim().length >= 2 && (
              <ul className="absolute z-20 mt-1 max-h-80 w-full overflow-y-auto rounded-md border border-slate-700 bg-[var(--surface)] shadow-xl" data-testid="search-results">
                {searching && !results.length && <li className="px-3 py-2 text-xs text-slate-500">Searching…</li>}
                {!searching && !results.length && <li className="px-3 py-2 text-xs text-slate-500">No NSE share matches “{query.trim()}”.</li>}
                {results.map((item) => {
                  const already = keys.has(item.instrument_key);
                  return (
                    <li key={item.instrument_key}>
                      <button
                        type="button"
                        disabled={already}
                        onClick={() => setAdding(item)}
                        className="flex w-full items-start justify-between gap-3 px-3 py-2 text-left hover:bg-slate-800/40 disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        <span className="min-w-0">
                          <span className="block text-sm font-semibold text-white">{item.symbol}</span>
                          <span className="block truncate text-xs text-slate-400">{item.name}</span>
                        </span>
                        <span className="shrink-0 text-[11px] text-slate-500">
                          {already ? "Already listed" : item.in_nifty200 ? "NIFTY 200" : "NSE"}
                        </span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        )}

        <Paged rows={shares}>
          {(visible) => (
            <div className="mt-5 table-scroll">
              <table className="terminal-table">
                <thead>
                  <tr>
                    <th>Symbol</th>
                    <th>Company</th>
                    <th>Sector group</th>
                    <th>Index</th>
                    {isAdmin && <th>Action</th>}
                  </tr>
                </thead>
                <tbody>
                  {visible.map((share) => {
                    const out = removed.has(share.instrument_key);
                    return (
                      <tr key={share.instrument_key} data-testid={`share-${share.symbol}`} className={out ? "opacity-50" : ""}>
                        <td>
                          <strong className={`text-white ${out ? "line-through" : ""}`}>{share.symbol}</strong>
                          {share.added && <span className="ml-2 status-pill status-good">New</span>}
                          {out && <span className="ml-2 status-pill status-bad">Removing</span>}
                        </td>
                        <td className={`text-xs ${share.known ? "text-slate-300" : "text-amber-400"}`}>
                          <div style={{ width: "20rem", whiteSpace: "normal" }}>{share.name}</div>
                        </td>
                        <td className="text-xs">{share.sector ?? <span className="text-amber-400" title="No sector group: the per-sector limit treats it as its own group.">Own group</span>}</td>
                        <td className="text-xs text-slate-400">{share.in_nifty200 ? "NIFTY 200" : "—"}</td>
                        {isAdmin && (
                          <td>
                            <button className="secondary-button px-2 py-1 text-xs" aria-label={`${out ? "Keep" : "Remove"} ${share.symbol}`} onClick={() => toggleRemove(share)}>
                              {out ? <Undo2 className="h-3.5 w-3.5" /> : <X className="h-3.5 w-3.5" />}
                              {out ? "Keep" : "Remove"}
                            </button>
                          </td>
                        )}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </Paged>

        {isAdmin && view.source === "screen" && (
          <button className="mt-4 text-xs text-slate-500 underline hover:text-slate-300" onClick={() => setResetting(true)}>
            Use the server&apos;s .env list instead
          </button>
        )}
      </article>

      {adding && (
        <Modal
          title={`Add ${adding.symbol}?`}
          eyebrow="Check the company name"
          testId="add-confirm"
          onClose={() => setAdding(null)}
          footer={
            <>
              <button className="secondary-button" onClick={() => setAdding(null)}>
                Cancel
              </button>
              <button className="primary-button" onClick={confirmAdd}>
                <Plus className="h-4 w-4" />
                Yes, add {adding.symbol}
              </button>
            </>
          }
        >
          <p className="text-lg font-semibold text-slate-900 dark:text-white" data-testid="add-confirm-name">
            {adding.name}
          </p>
          <dl className="mt-3 grid grid-cols-[8rem_1fr] gap-y-1 text-sm">
            <dt className="text-slate-500">Symbol</dt>
            <dd className="text-slate-200">{adding.symbol}</dd>
            <dt className="text-slate-500">Upstox key</dt>
            <dd className="font-mono text-xs text-slate-300">{adding.instrument_key}</dd>
            <dt className="text-slate-500">Index</dt>
            <dd className="text-slate-300">{adding.in_nifty200 ? "NIFTY 200 member" : "NSE equity, outside the NIFTY 200"}</dd>
            <dt className="text-slate-500">Sector group</dt>
            <dd className={adding.sector ? "text-slate-300" : "text-amber-400"}>{adding.sector ?? "None: the per-sector limit will treat it as its own group"}</dd>
            <dt className="text-slate-500">Name from</dt>
            <dd className="text-slate-400">{adding.source}</dd>
          </dl>
          <p className="mt-3 text-xs text-slate-400">It is added to the list on this screen; nothing changes until you save.</p>
        </Modal>
      )}

      {saving && (
        <Modal
          title="Save the watchlist?"
          eyebrow="Intraday scanner"
          tone="danger"
          role="alertdialog"
          busy={busy}
          testId="save-confirm"
          onClose={() => setSaving(false)}
          footer={
            <>
              <button className="secondary-button" disabled={busy} onClick={() => setSaving(false)}>
                Cancel
              </button>
              <button className="danger-button" disabled={busy || !understood || !reason.trim()} onClick={() => void save()}>
                {busy ? "Saving…" : "Yes, save the watchlist"}
              </button>
            </>
          }
        >
          <ul className="space-y-1 text-sm">
            {added.map((share) => (
              <li key={share.instrument_key} className="text-emerald-400">
                + {share.symbol} — {share.name}
              </li>
            ))}
            {removing.map((share) => (
              <li key={share.instrument_key} className="text-rose-400">
                − {share.symbol} — {share.name}
              </li>
            ))}
            {benchmarkChanged && (
              <li className="text-amber-400">
                Benchmark: {view.benchmark_name} → {nameOf(benchmark)}
              </li>
            )}
          </ul>
          <p className="mt-3 text-xs text-slate-400">
            {kept.length} share{kept.length === 1 ? "" : "s"} after this change. {view.applies}
          </p>
          <p className="mt-3 flex items-start gap-2 rounded-md border border-rose-500/40 bg-rose-950/20 p-3 text-xs leading-5 text-rose-200">
            <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0" />
            A live strategy can trade any share on this list with real money, under the usual limits. An added share has no paper or backtest record here yet.
          </p>
          <label className="field-label mt-4 block">
            Why (kept in the audit log)
            <input className="field-input mt-1" value={reason} maxLength={300} onChange={(event) => setReason(event.target.value)} />
          </label>
          <label className="mt-3 flex items-center gap-2 text-sm text-slate-200">
            <input type="checkbox" checked={understood} onChange={(event) => setUnderstood(event.target.checked)} />
            I have checked the company names and understand the live strategy can trade these shares.
          </label>
        </Modal>
      )}

      {resetting && (
        <Modal
          title="Use the server's .env list?"
          eyebrow="Intraday scanner"
          tone="warning"
          role="alertdialog"
          busy={busy}
          onClose={() => setResetting(false)}
          footer={
            <>
              <button className="secondary-button" disabled={busy} onClick={() => setResetting(false)}>
                Cancel
              </button>
              <button className="danger-button" disabled={busy} onClick={() => void reset()}>
                Yes, use .env
              </button>
            </>
          }
        >
          <p className="text-sm text-slate-300">
            The list and benchmark saved here are forgotten, and UPSTOX_SUBSCRIPTIONS and the benchmark in the server&apos;s .env apply again. {view.applies}
          </p>
        </Modal>
      )}
    </section>
  );
}
