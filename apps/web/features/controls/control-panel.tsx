"use client";

import { BellRing, RotateCcw, Save, Webhook } from "lucide-react";

import { useCallback, useEffect, useState } from "react";

import { api, type NotificationCatalog, type TelegramStatus } from "../../components/api";

/**
 * Alerts: the Telegram bot, and which messages it sends.
 *
 * This file once held the whole control plane — paper tracking, the live lock,
 * emergency stop and the bot — as four cards rendered on two different menu
 * entries. The safety cards have since moved to Risk and then collapsed into a
 * single status card there, because three cards each describing one gate
 * correctly still left an operator unable to tell which of seven gates was
 * actually in the way.
 *
 * What remains here is configuration: where messages go, and which ones.
 */

export function AlertsPanel({
  telegram,
  isAdmin,
  onTelegram,
  onRegisterWebhook,
}: {
  telegram: TelegramStatus;
  isAdmin: boolean;
  onTelegram: () => void;
  onRegisterWebhook: () => void;
}) {
  return (
    <section className="mt-6 max-w-5xl">
      <article className="panel p-5 sm:p-7">
        <p className="eyebrow">Notifications</p>
        <h3 className="mt-1 text-base font-semibold text-white">
          Telegram bot — {telegram.configured ? "configured" : "awaiting a dedicated bot"}
        </h3>
        <p className="mt-3 text-sm leading-6 text-slate-400">
          Outbound alerts: {telegram.configured ? "ready" : "not configured"}. Inbound controls:{" "}
          {telegram.inbound_enabled ? "ready" : "need an HTTPS webhook and allowed user IDs"}.
        </p>
        <p className="mt-2 text-xs leading-5 text-slate-500">{telegram.detail}</p>
        {isAdmin && (
          <div className="mt-6 flex flex-wrap gap-3">
            <button disabled={!telegram.configured} onClick={onTelegram} className="secondary-button">
              <BellRing className="h-4 w-4" />
              Send test alert
            </button>
            <button disabled={!telegram.inbound_enabled} onClick={onRegisterWebhook} className="secondary-button">
              <Webhook className="h-4 w-4" />
              Register webhook
            </button>
          </div>
        )}
        {isAdmin && (
          <p className="mt-3 text-xs leading-5 text-slate-500">
            A test alert proves outbound only. Registering tells Telegram where to deliver replies and which
            secret to send with them, so it is required after the webhook URL or secret changes — until then
            every inbound update is rejected and approvals stop without an error anywhere in this screen.
          </p>
        )}
      </article>

      <NotificationPreferences isAdmin={isAdmin} />
    </section>
  );
}

/**
 * Which messages Telegram sends.
 *
 * Under TELEGRAM_APPROVAL one signal produced two notifications — the paper
 * journal's "TRADING SIGNAL MATCHED" and the live path's approval request —
 * describing the same trade. Two messages for one decision is not twice the
 * information; it teaches skimming, and the one with the buttons on it is the
 * one that has to be read.
 *
 * Only messages that report something going *right* are offered. A failure
 * notice is not noise to be managed: muting it would not reduce the number of
 * things that go wrong, only the number you hear about. The server enforces
 * this too — these are the only fields it accepts — and the always-sent list
 * below comes from the server so the screen cannot promise a different set
 * from the one the senders actually honour.
 */
function NotificationPreferences({ isAdmin }: { isAdmin: boolean }) {
  const [catalog, setCatalog] = useState<NotificationCatalog | null>(null);
  const [draft, setDraft] = useState<Record<string, boolean>>({});
  const [saving, setSaving] = useState(false);
  const [note, setNote] = useState("");

  const load = useCallback(async () => {
    try {
      const next = await api.notificationCatalog();
      setCatalog(next);
      setDraft(Object.fromEntries(next.toggles.map((item) => [item.key, item.value])));
    } catch (error) {
      setNote(error instanceof Error ? error.message : "Could not load notification preferences");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (!catalog) return null;

  const dirty = catalog.toggles.filter((item) => draft[item.key] !== item.value).length;

  async function save() {
    setSaving(true);
    try {
      await api.updateNotifications(draft);
      setNote("Saved. New messages follow these settings immediately.");
      await load();
    } catch (error) {
      setNote(error instanceof Error ? error.message : "Could not save notification preferences");
    } finally {
      setSaving(false);
    }
  }

  return (
    <article className="panel mt-6 p-5 sm:p-7">
      <p className="eyebrow">Notifications</p>
      <h3 className="mt-1 text-base font-semibold text-white">Which messages to send</h3>
      <p className="mt-3 text-sm leading-6 text-slate-400">
        Switch off the messages you do not need. Anything that reports a failure is not listed, because it always
        sends.
      </p>

      <div className="mt-5 space-y-4">
        {catalog.toggles.map((item) => (
          <div key={item.key} className="rounded-md border border-slate-800 p-4">
            <label className="flex cursor-pointer items-start gap-3">
              <input
                type="checkbox"
                className="mt-1"
                disabled={!isAdmin}
                checked={Boolean(draft[item.key])}
                onChange={(event) => setDraft((current) => ({ ...current, [item.key]: event.target.checked }))}
              />
              <span>
                <span className="text-sm font-semibold text-white">{item.label}</span>
                <span className="mt-1 block text-xs leading-5 text-slate-400">{item.help}</span>
                <span className="mt-2 block font-mono text-[11px] text-slate-600">{item.key}</span>
              </span>
            </label>
          </div>
        ))}
      </div>

      <div className="glass-inset mt-5 rounded-md p-4">
        <p className="text-xs font-semibold text-slate-300">Always sent, and not switchable</p>
        <ul className="mt-2 space-y-1 text-xs leading-5 text-slate-400">
          {catalog.always_sent.map((item) => (
            <li key={item}>• {item}</li>
          ))}
        </ul>
      </div>

      {isAdmin && (
        <div className="mt-5 flex flex-wrap items-center gap-3">
          {dirty > 0 ? (
            <>
              <button onClick={() => void save()} disabled={saving} className="primary-button">
                <Save className="h-4 w-4" />
                Save {dirty} change{dirty === 1 ? "" : "s"}
              </button>
              <button onClick={() => void load()} className="secondary-button">
                <RotateCcw className="h-4 w-4" />
                Discard
              </button>
            </>
          ) : (
            <p className="text-sm text-slate-500">No changes to save.</p>
          )}
        </div>
      )}
      {note && <p className="mt-3 text-xs text-slate-400">{note}</p>}
    </article>
  );
}
