"use client";

import { Info, ReceiptIndianRupee, Save } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { api, type ChargesView, type IntradayCharges, type RoundTrip, type SwingCharges } from "../../components/api";
import type { DeskState } from "../../lib/desks";

/**
 * Settings > Charges: what a trade costs, one rate card per desk.
 *
 * Each card shows a worked example -- Rs 50,000 bought and sold -- that
 * follows the figures as they are typed, so a figure can be checked against a
 * contract note line by line before it is saved. A save is one recorded change,
 * with the reason. No order and no live limit reads these figures; they decide
 * what paper fills, backtests and reports say a trade cost.
 */

type Field<T> = { key: keyof T & string; label: string; unit: "₹" | "%" | "bps"; help: string };

const INTRADAY_FIELDS: Field<IntradayCharges>[] = [
  { key: "brokerage_percent", label: "Brokerage", unit: "%", help: "Per order, as a percentage of its value." },
  { key: "brokerage_cap", label: "Brokerage cap", unit: "₹", help: "The most brokerage one order is charged (e.g. ₹20)." },
  { key: "stt_sell_percent", label: "STT (sell side)", unit: "%", help: "Securities transaction tax, intraday: sell side only." },
  { key: "exchange_transaction_percent", label: "Exchange charge", unit: "%", help: "NSE transaction charge, both sides." },
  { key: "sebi_percent", label: "SEBI fee", unit: "%", help: "₹10 per crore is 0.0001%." },
  { key: "stamp_duty_buy_percent", label: "Stamp duty (buy side)", unit: "%", help: "Intraday: 0.003% on the buy." },
  { key: "gst_percent", label: "GST", unit: "%", help: "On brokerage and the exchange charge." },
  { key: "slippage_bps", label: "Paper slippage", unit: "bps", help: "Paper fills only: how far worse than the candle price a fill is assumed. 1 bps = 0.01%." },
  { key: "participation_percent", label: "Paper participation", unit: "%", help: "Paper fills only: the share of a candle's volume one order may take." },
];

const SWING_FIELDS: Field<SwingCharges>[] = [
  { key: "brokerage_per_order", label: "Brokerage per order", unit: "₹", help: "Delivery brokerage on each buy and each sell. ₹0 on a zero-brokerage delivery plan." },
  { key: "dp_charge_per_sale", label: "DP charge per sale", unit: "₹", help: "Depository charge, once per share sold per day. Read it off a contract note." },
  { key: "stt_percent", label: "STT (each side)", unit: "%", help: "Delivery: 0.1% on the buy and on the sell." },
  { key: "exchange_transaction_percent", label: "Exchange charge", unit: "%", help: "NSE transaction charge, both sides." },
  { key: "sebi_percent", label: "SEBI fee", unit: "%", help: "₹10 per crore is 0.0001%." },
  { key: "stamp_duty_buy_percent", label: "Stamp duty (buy side)", unit: "%", help: "Delivery: 0.015% on the buy." },
  { key: "gst_percent", label: "GST", unit: "%", help: "On brokerage, the exchange charge and the SEBI fee." },
];

const LINE_LABELS: Record<string, string> = {
  brokerage: "Brokerage",
  stt: "STT",
  exchange: "Exchange charge",
  gst: "GST",
  sebi: "SEBI fee",
  stamp_duty: "Stamp duty",
  dp: "DP charge",
};

const rupees = (value: number) => `₹${value.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

export function ChargesForm({
  isAdmin,
  desks,
  onMessage,
}: {
  isAdmin: boolean;
  desks: DeskState;
  onMessage: (message: string) => void;
}) {
  const [saved, setSaved] = useState<ChargesView | null>(null);

  useEffect(() => {
    void api
      .charges()
      .then(setSaved)
      .catch((error: unknown) => onMessage(error instanceof Error ? error.message : "Could not load charges"));
  }, [onMessage]);

  if (!saved) return <div className="panel mt-6 max-w-5xl p-5"><div className="skeleton h-40" /></div>;

  return (
    <div className="mt-6 grid max-w-5xl gap-6">
      {desks.intraday && (
        <RateCard
          testId="charges-intraday"
          title="Intraday"
          usedBy="Intraday paper fills, intraday backtests and sweeps, and the estimated charges on live trades in History."
          fields={INTRADAY_FIELDS}
          saved={saved.intraday}
          example={saved.intraday_example}
          isAdmin={isAdmin}
          preview={async (draft) => (await api.previewCharges({ intraday: draft })).intraday}
          save={async (draft, reason) => setSaved(await api.saveIntradayCharges(draft, reason))}
          onMessage={onMessage}
        />
      )}
      {desks.swing && (
        <RateCard
          testId="charges-swing"
          title="Swing (delivery)"
          usedBy="The swing paper desk and the swing backtest."
          fields={SWING_FIELDS}
          saved={saved.swing}
          example={saved.swing_example}
          isAdmin={isAdmin}
          preview={async (draft) => (await api.previewCharges({ swing: draft })).swing}
          save={async (draft, reason) => setSaved(await api.saveSwingCharges(draft, reason))}
          onMessage={onMessage}
        />
      )}
      {!desks.intraday && !desks.swing && (
        <p className="empty-inset text-sm text-slate-400">Both desks are off. Turn one on in Trading controls to see its charges.</p>
      )}
    </div>
  );
}

function RateCard<T extends object>({
  testId,
  title,
  usedBy,
  fields,
  saved,
  example,
  isAdmin,
  preview,
  save,
  onMessage,
}: {
  testId: string;
  title: string;
  usedBy: string;
  fields: Field<T>[];
  saved: T;
  example: RoundTrip;
  isAdmin: boolean;
  preview: (draft: T) => Promise<RoundTrip | null>;
  save: (draft: T, reason: string | null) => Promise<void>;
  onMessage: (message: string) => void;
}) {
  const [draft, setDraft] = useState<T>(saved);
  const [reason, setReason] = useState("");
  const [shown, setShown] = useState<RoundTrip>(example);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setDraft(saved);
    setShown(example);
  }, [saved, example]);

  const values = draft as Record<string, unknown>;
  const original = saved as Record<string, unknown>;
  const changed = useMemo(
    () => fields.filter((field) => String(values[field.key]) !== String(original[field.key])).map((field) => field.key),
    [fields, values, original],
  );
  const invalid = fields.some((field) => values[field.key] === "" || Number.isNaN(Number(values[field.key])));

  // The example follows the draft, a moment after typing stops. The preview
  // call is held in a ref: the parent passes a new function every render.
  const previewRef = useRef(preview);
  previewRef.current = preview;
  useEffect(() => {
    if (!changed.length || invalid) {
      if (!changed.length) setShown(example);
      return;
    }
    const timer = window.setTimeout(() => {
      void previewRef.current(draft)
        .then((trip) => trip && setShown(trip))
        .catch(() => undefined);
    }, 350);
    return () => window.clearTimeout(timer);
  }, [draft, changed.length, invalid, example]);

  async function submit() {
    setBusy(true);
    try {
      await save(draft, reason.trim() || null);
      setReason("");
      onMessage(`${title} charges saved.`);
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Could not save charges");
    } finally {
      setBusy(false);
    }
  }

  return (
    <article className="panel p-5 sm:p-7" data-testid={testId}>
      <div className="flex items-center gap-3 border-b border-slate-800 pb-4">
        <ReceiptIndianRupee className="h-5 w-5 text-emerald-400" />
        <div>
          <p className="eyebrow">Rate card</p>
          <h3 className="font-semibold text-white">{title}</h3>
          <p className="mt-1 text-xs text-slate-400">Used by: {usedBy}</p>
        </div>
      </div>

      <div className="mt-5 grid gap-6 lg:grid-cols-[1fr_18rem]">
        <div className="grid gap-3 sm:grid-cols-2">
          {fields.map((field) => (
            <label
              key={field.key}
              className={`rounded-md border p-3 text-sm font-semibold text-white ${changed.includes(field.key) ? "border-sky-500/40 bg-sky-950/20" : "border-slate-800"}`}
            >
              {field.label}
              <span className="mt-2 flex items-center gap-2">
                <input
                  aria-label={field.label}
                  type="number"
                  step="any"
                  min={0}
                  disabled={!isAdmin}
                  className="field-input font-mono text-sm"
                  value={String(values[field.key] ?? "")}
                  onChange={(event) =>
                    setDraft({ ...draft, [field.key]: event.target.value === "" ? "" : Number(event.target.value) })
                  }
                />
                <span className="w-8 text-xs text-slate-500">{field.unit}</span>
              </span>
              <span className="mt-2 flex gap-1.5 text-xs font-normal leading-5 text-slate-400">
                <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-slate-500" />
                {field.help}
              </span>
            </label>
          ))}
        </div>

        <aside className="glass-inset h-fit rounded-md p-4" data-testid={`${testId}-example`}>
          <p className="eyebrow">Worked example</p>
          <p className="mt-1 text-sm font-semibold text-white">{rupees(shown.trade_value)} bought, then sold</p>
          <table className="mt-3 w-full text-xs">
            <tbody>
              {Object.entries(shown.lines)
                .filter(([key, amount]) => amount !== 0 || key !== "dp")
                .map(([key, amount]) => (
                  <tr key={key} className="border-b border-slate-800/70">
                    <td className="py-1 text-slate-400">{LINE_LABELS[key] ?? key}</td>
                    <td className="numeric py-1 text-right text-slate-200">{rupees(amount)}</td>
                  </tr>
                ))}
              <tr>
                <td className="pt-2 font-semibold text-white">Round trip</td>
                <td className="numeric pt-2 text-right font-semibold text-amber-300" data-testid={`${testId}-total`}>
                  {rupees(shown.total)}
                </td>
              </tr>
            </tbody>
          </table>
          <p className="mt-3 text-[11px] leading-4 text-slate-500">
            Buy {rupees(shown.buy)} · sell {rupees(shown.sell)}. Compare with a real contract note; a figure off by a few rupees an
            order decides whether a small edge survives.
          </p>
        </aside>
      </div>

      {isAdmin && (
        <div className="mt-5 flex flex-col gap-3 border-t border-slate-800 pt-4 sm:flex-row sm:items-end">
          <label className="field-label flex-1">
            Why (kept in the change history)
            <input
              className="field-input mt-1"
              maxLength={300}
              value={reason}
              placeholder="e.g. from the contract note of 12 Nov"
              onChange={(event) => setReason(event.target.value)}
            />
          </label>
          <div className="flex gap-2">
            <button className="secondary-button" disabled={!changed.length || busy} onClick={() => setDraft(saved)}>
              Undo changes
            </button>
            <button className="primary-button" disabled={!changed.length || invalid || busy} onClick={() => void submit()}>
              <Save className="h-4 w-4" />
              Save {title.split(" ")[0].toLowerCase()} charges
            </button>
          </div>
        </div>
      )}
    </article>
  );
}
