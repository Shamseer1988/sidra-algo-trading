"use client";

import { ClipboardCopy, FileSearch, Play } from "lucide-react";
import { useEffect, useState } from "react";

import { api, type DiagnosticReport, type DiagnosticRun } from "../../components/api";

/**
 * Reports > Diagnostics: the read-only report scripts, one button each.
 *
 * What an operator used to run over SSH -- the daily review, why no trade,
 * the settings check -- picked from a list, with a date where the report takes
 * one. The output is the script's own text, exactly as it prints it, so a
 * report read here and one read in a terminal are the same report. None of
 * these can change anything.
 */

function todayIst(): string {
  return new Date(Date.now() + 5.5 * 3600 * 1000).toISOString().slice(0, 10);
}

export function DiagnosticsPanel({ onMessage }: { onMessage: (message: string) => void }) {
  const [reports, setReports] = useState<DiagnosticReport[]>([]);
  const [chosen, setChosen] = useState("daily_review");
  const [day, setDay] = useState(todayIst());
  const [from, setFrom] = useState("");
  const [after, setAfter] = useState("");
  const [noBroker, setNoBroker] = useState(false);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<DiagnosticRun | null>(null);

  useEffect(() => {
    void api
      .diagnosticReports()
      .then(setReports)
      .catch((error: unknown) => onMessage(error instanceof Error ? error.message : "Could not load the reports"));
  }, [onMessage]);

  const report = reports.find((item) => item.name === chosen);
  const takes = (option: string) => Boolean(report?.options.includes(option));

  async function run() {
    setRunning(true);
    setResult(null);
    try {
      setResult(
        await api.runDiagnosticReport(chosen, {
          session_date: takes("date") ? day || null : null,
          from_date: takes("from") ? from || null : null,
          to_date: takes("to") ? day || null : null,
          after: takes("after") ? after || null : null,
          no_broker: takes("no_broker") ? noBroker : false,
        }),
      );
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "The report failed");
    } finally {
      setRunning(false);
    }
  }

  return (
    <section className="mt-6 space-y-5" data-testid="diagnostics">
      <article className="panel p-5 sm:p-7">
        <div className="flex items-center gap-3 border-b border-slate-800 pb-4">
          <FileSearch className="h-5 w-5 text-emerald-400" />
          <div>
            <p className="eyebrow">Read-only · safe any time</p>
            <h3 className="font-semibold text-white">Diagnostic reports</h3>
            <p className="mt-1 text-xs text-slate-400">The same reports as the server scripts. They read the record and change nothing.</p>
          </div>
        </div>

        <div className="mt-5 grid gap-2 sm:grid-cols-2 xl:grid-cols-4" role="radiogroup" aria-label="Report">
          {reports.map((item) => (
            <button
              key={item.name}
              role="radio"
              aria-checked={item.name === chosen}
              onClick={() => setChosen(item.name)}
              className={`rounded-md border p-3 text-left ${item.name === chosen ? "border-emerald-500/60 bg-emerald-950/20" : "border-slate-800 hover:border-slate-600"}`}
            >
              <span className="block text-sm font-semibold text-white">{item.title}</span>
              <span className="mt-1 block text-xs leading-5 text-slate-400">{item.help}</span>
            </button>
          ))}
        </div>

        <div className="mt-5 flex flex-wrap items-end gap-4">
          {takes("from") && (
            <label className="field-label">
              From
              <input type="date" className="field-input mt-1" value={from} onChange={(event) => setFrom(event.target.value)} />
            </label>
          )}
          {(takes("date") || takes("to")) && (
            <label className="field-label">
              {takes("to") ? "To" : "Session date"}
              <input type="date" aria-label="Session date" className="field-input mt-1" value={day} onChange={(event) => setDay(event.target.value)} />
            </label>
          )}
          {takes("after") && (
            <label className="field-label">
              Only after (IST)
              <input type="time" className="field-input mt-1" value={after} onChange={(event) => setAfter(event.target.value)} />
            </label>
          )}
          {takes("no_broker") && (
            <label className="flex items-center gap-2 text-sm text-slate-300">
              <input type="checkbox" checked={noBroker} onChange={(event) => setNoBroker(event.target.checked)} />
              Local records only (don&apos;t ask the broker)
            </label>
          )}
          <button className="primary-button" disabled={running || !report} onClick={() => void run()}>
            <Play className="h-4 w-4" />
            {running ? "Running…" : `Run ${report?.title ?? "report"}`}
          </button>
          {report && <span className="text-xs text-slate-500">Best run: {report.when}</span>}
        </div>
      </article>

      {result && (
        <article className="panel overflow-hidden" data-testid="diagnostics-output">
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-800 px-5 py-3 text-xs text-slate-400">
            <span className="font-mono">{result.command}</span>
            <span>
              {result.seconds}s{result.exit_code ? ` · exit ${result.exit_code}` : ""}
              {result.truncated ? " · output cut short" : ""}
            </span>
            <button
              className="secondary-button px-2 py-1 text-xs"
              onClick={() => void navigator.clipboard?.writeText(result.output).then(() => onMessage("Copied."))}
            >
              <ClipboardCopy className="h-3.5 w-3.5" />
              Copy
            </button>
          </div>
          <pre className="max-h-[70vh] overflow-auto whitespace-pre p-5 font-mono text-xs leading-5 text-slate-200">{result.output}</pre>
        </article>
      )}
    </section>
  );
}
