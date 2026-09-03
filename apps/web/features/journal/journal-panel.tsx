"use client";

import { Download, FileCheck2, TrendingUp } from "lucide-react";
import { useEffect, useState } from "react";

import { api, type PaperSignal, type ScoreAnalysis } from "../../components/api";
import { formatIstTimestamp, titleCase } from "../../lib/formatting";

export function JournalPanel({ signals }: { signals: PaperSignal[] }) {
  const [analysis, setAnalysis] = useState<ScoreAnalysis | null>(null);

  useEffect(() => {
    void api.scoreAnalysis().then(setAnalysis).catch(() => setAnalysis(null));
  }, []);

  return (
    <section>
      <div className="page-toolbar">
        <div>
          <p className="eyebrow">Paper tracking</p>
          <h2 className="page-title">Journal</h2>
          <p className="page-copy">
            Export the server-side scanner journal. Simulated orders, fills, and P&amp;L are kept separately in the
            paper Orderbook and Positions workspaces.
          </p>
        </div>
        <a href="/api/v1/journal/export.csv" download="paper-journal.csv" className="primary-button">
          <Download className="h-4 w-4" />
          Export CSV
        </a>
      </div>

      <article className="panel mt-6 p-6">
        <div className="flex items-start gap-3">
          <FileCheck2 className="mt-1 h-5 w-5 text-emerald-400" />
          <div>
            <h3 className="font-semibold text-white">
              {signals.length} recorded paper signal{signals.length === 1 ? "" : "s"}
            </h3>
            <p className="mt-2 text-sm leading-6 text-slate-400">
              This export remains an auditable scanner-decision journal. The new execution ledger is explicitly
              simulated and cannot represent broker orders, balances, or positions.
            </p>
          </div>
        </div>
        {signals.length > 0 && (
          <div className="data-list mt-6">
            {signals.slice(0, 5).map((signal) => (
              <div key={signal.id}>
                <dt>
                  {(signal.script_name || signal.instrument_token)} · {signal.side}
                </dt>
                <dd>
                  {titleCase(signal.status)} · {formatIstTimestamp(signal.created_at)}
                </dd>
              </div>
            ))}
          </div>
        )}
      </article>

      <ScoreAnalysisCard analysis={analysis} />
    </section>
  );
}

function ScoreAnalysisCard({ analysis }: { analysis: ScoreAnalysis | null }) {
  return (
    <article className="panel mt-6 p-6">
      <div className="flex items-start gap-3">
        <TrendingUp className="mt-1 h-5 w-5 text-emerald-400" />
        <div>
          <h3 className="font-semibold text-white">Score-component outcome lift</h3>
          <p className="mt-2 text-sm leading-6 text-slate-400">
            For each score component, resolved signals are split at the component&apos;s median score. Lift is the
            difference in average realised R between the high-score and low-score halves — a positive lift means the
            component predicted better outcomes.
          </p>
        </div>
      </div>

      {!analysis || analysis.insufficient_data ? (
        <p className="empty-inset mt-5">
          {analysis
            ? `Need more resolved paper signals for a reliable analysis (${analysis.resolved_signals} so far).`
            : "The score analysis is not available yet."}
        </p>
      ) : (
        <>
          <p className="mt-5 text-xs text-slate-500">
            {analysis.resolved_signals} resolved signals · overall win rate {analysis.overall?.win_rate_percent}% ·
            average {analysis.overall?.average_realized_r}R
          </p>
          <div className="table-scroll mt-3">
            <table className="terminal-table">
              <thead>
                <tr>
                  <th>Component</th>
                  <th>Lift (R)</th>
                  <th>High half</th>
                  <th>Low half</th>
                </tr>
              </thead>
              <tbody>
                {analysis.components.map((row) => (
                  <tr key={row.component}>
                    <td className="capitalize">{row.component.replaceAll("_", " ")}</td>
                    <td className={`numeric font-mono ${row.lift_r > 0 ? "text-emerald-300" : row.lift_r < 0 ? "text-rose-300" : ""}`}>
                      {row.lift_r > 0 ? "+" : ""}
                      {row.lift_r.toFixed(2)}
                    </td>
                    <td className="numeric">
                      {row.above.win_rate_percent}% · {row.above.average_realized_r.toFixed(2)}R ({row.above.samples})
                    </td>
                    <td className="numeric">
                      {row.below.win_rate_percent}% · {row.below.average_realized_r.toFixed(2)}R ({row.below.samples})
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </article>
  );
}
