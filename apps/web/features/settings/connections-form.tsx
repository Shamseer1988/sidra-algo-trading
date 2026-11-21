"use client";

import { CheckCircle2, KeyRound, LogIn, Save, ShieldCheck, XCircle } from "lucide-react";
import { useEffect, useState } from "react";

import { api, type ConnectionCheck, type ConnectionsView, type CredentialField } from "../../components/api";
import { Hint } from "../../components/hint";
import { Modal } from "../../components/modal";
import { formatIstTimestamp } from "../../lib/formatting";
import { ProviderCard, SettingsSection } from "./settings-ui";

/**
 * Settings > Connections: the broker account and the Telegram bot, set up from the screen.
 *
 * Secrets go one way. A saved secret is shown as "Set", an identifier by its
 * last characters, and the boxes always start empty: typing replaces, leaving
 * a box empty keeps what is there. Each field says where its value comes from
 * -- this screen, or the server's file -- so nobody wonders which one is used.
 */

export function ConnectionsForm({ isAdmin, onMessage }: { isAdmin: boolean; onMessage: (message: string) => void }) {
  const [view, setView] = useState<ConnectionsView | null>(null);
  const [open, setOpen] = useState<"upstox" | "telegram" | null>(null);

  useEffect(() => {
    void api
      .connections()
      .then(setView)
      .catch((error: unknown) => onMessage(error instanceof Error ? error.message : "Could not load connections"));
  }, [onMessage]);

  if (!view) return <div className="panel mt-6 max-w-6xl p-5"><div className="skeleton h-40" /></div>;

  const upstox = view.upstox;
  return (
    <div className="mt-6 grid max-w-6xl gap-5">
      {!view.encryption_ready && (
        <p className="rounded-md border border-amber-500/30 bg-amber-950/20 p-4 text-sm text-amber-100" data-testid="no-server-key">
          The server has no encryption key yet, so credentials cannot be saved from this screen. This is a one-time server
          setup step (UPSTOX_TOKEN_ENCRYPTION_KEY); until then the values in the server&apos;s file are used.
        </p>
      )}
      <SettingsSection
        eyebrow="Brokers"
        title="Broker accounts"
        hint="The broker that places live orders and supplies market data. Secrets are stored encrypted and never shown again once saved; leaving a box empty keeps what is there."
      >
        <div className="grid gap-3 md:grid-cols-3">
          <ProviderCard
            testId="broker-upstox"
            name="Upstox"
            monogram="UP"
            tagline="Live orders and market data"
            onOpen={() => setOpen("upstox")}
            actionLabel="Configure Upstox"
            status={
              <>
                <StatusChip ok={upstox.login_ready} yes="App keys set" no="App keys missing" />
                <StatusChip ok={Boolean(upstox.token_valid_until)} yes="Logged in today" no="Not logged in" />
                <StatusChip ok={upstox.auto_login_ready} yes="Auto-login on" no="Auto-login off" />
              </>
            }
          />
          <ProviderCard testId="broker-fyers" name="Fyers" monogram="FY" tone="violet" tagline="Not connected yet" comingSoon />
          <ProviderCard testId="broker-firstock" name="Firstock" monogram="FS" tone="amber" tagline="Not connected yet" comingSoon />
        </div>
      </SettingsSection>

      <SettingsSection eyebrow="Alerts" title="Notifications" hint="Where this system tells you what it did.">
        <div className="grid gap-3 md:grid-cols-3">
          <ProviderCard
            testId="alerts-telegram"
            name="Telegram"
            monogram="TG"
            tone="sky"
            tagline="Trade alerts and approvals"
            onOpen={() => setOpen("telegram")}
            actionLabel="Configure Telegram"
            status={<StatusChip ok={view.telegram.ready} yes="Configured" no="Not configured" />}
          />
        </div>
      </SettingsSection>

      {open === "upstox" && (
        <Modal wide title="Upstox" eyebrow="Broker · live orders and market data" testId="upstox-modal" onClose={() => setOpen(null)}>
          <UpstoxCard view={view} isAdmin={isAdmin} onSaved={setView} onMessage={onMessage} />
        </Modal>
      )}
      {open === "telegram" && (
        <Modal wide title="Telegram" eyebrow="Alerts" testId="telegram-modal" onClose={() => setOpen(null)}>
          <TelegramCard view={view} isAdmin={isAdmin} onSaved={setView} onMessage={onMessage} />
        </Modal>
      )}
    </div>
  );
}

function SourceTag({ field }: { field: CredentialField }) {
  if (!field.configured) return <span className="status-pill status-watch">Not set</span>;
  return (
    <span className={`status-pill ${field.source === "screen" ? "status-good" : "status-watch"}`}>
      {field.source === "screen" ? "Saved here" : "From server file"}
    </span>
  );
}

function FieldRow({
  field,
  draft,
  disabled,
  onChange,
}: {
  field: CredentialField;
  draft: string | boolean | undefined;
  disabled: boolean;
  onChange: (value: string | boolean) => void;
}) {
  return (
    <div className={`rounded-md border p-3 text-sm font-semibold text-white ${draft !== undefined && draft !== "" ? "border-sky-500/40 bg-sky-950/20" : "border-slate-800"}`}>
      <span className="flex items-center gap-1.5">
        {field.label}
        <Hint>{field.help}</Hint>
        <span className="ml-auto">
          <SourceTag field={field} />
        </span>
      </span>
      {field.kind === "boolean" ? (
        <span className="mt-2 flex items-center gap-3 text-sm font-normal text-slate-300">
          <input
            type="checkbox"
            aria-label={field.label}
            disabled={disabled}
            checked={draft === undefined ? Boolean(field.value) : Boolean(draft)}
            onChange={(event) => onChange(event.target.checked)}
          />
          {(draft === undefined ? field.value : draft) ? "On" : "Off"}
        </span>
      ) : (
        <input
          aria-label={field.label}
          type={field.kind === "secret" ? "password" : "text"}
          autoComplete="off"
          disabled={disabled}
          className="field-input mt-2 font-mono text-sm"
          value={typeof draft === "string" ? draft : ""}
          placeholder={field.configured ? `${field.shown ?? "Set"} — type to replace` : "Not set"}
          onChange={(event) => onChange(event.target.value)}
        />
      )}
    </div>
  );
}

function UpstoxCard({
  view,
  isAdmin,
  onSaved,
  onMessage,
}: {
  view: ConnectionsView;
  isAdmin: boolean;
  onSaved: (view: ConnectionsView) => void;
  onMessage: (message: string) => void;
}) {
  const [draft, setDraft] = useState<Record<string, string | boolean>>({});
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [check, setCheck] = useState<ConnectionCheck | null>(null);
  const upstox = view.upstox;
  const changes = Object.fromEntries(
    Object.entries(draft).filter(([name, value]) => {
      const field = upstox.fields.find((item) => item.name === name);
      return field?.kind === "boolean" ? value !== field.value : value !== "";
    }),
  );
  const disabled = !isAdmin || !view.encryption_ready;

  async function save() {
    setBusy(true);
    try {
      onSaved(await api.saveUpstoxConnection(changes, [], reason.trim() || null));
      setDraft({});
      setReason("");
      onMessage("Upstox connection saved.");
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Could not save the Upstox connection");
    } finally {
      setBusy(false);
    }
  }

  async function runCheck() {
    setBusy(true);
    try {
      setCheck(await api.checkUpstoxConnection());
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Could not check the connection");
    } finally {
      setBusy(false);
    }
  }

  async function login() {
    try {
      const { authorization_url } = await api.startUpstoxOAuth();
      window.location.assign(authorization_url);
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Could not start the Upstox login");
    }
  }

  return (
    <div data-testid="connection-upstox">
      <div className="flex flex-col gap-3 border-b border-slate-800 pb-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex flex-wrap gap-2 text-xs">
          <StatusChip ok={upstox.login_ready} yes="App details complete" no="App details incomplete" />
          <StatusChip ok={upstox.auto_login_ready} yes="Morning auto-login ready" no="Morning auto-login not set" />
          <StatusChip
            ok={Boolean(upstox.token_valid_until)}
            yes={`Logged in until ${upstox.token_valid_until ? formatIstTimestamp(upstox.token_valid_until) : ""}`}
            no="Not logged in today"
          />
        </div>
      </div>

      <ol className="mt-4 list-decimal space-y-1 pl-5 text-xs leading-5 text-slate-400">
        <li>In Upstox Developer Apps, create an app. Copy its API key and secret, and set its redirect URL to the one below.</li>
        <li>For the 8:30 AM auto-login, add your mobile number, PIN and TOTP secret, and turn Morning auto-login on.</li>
        <li>Save, then press <strong>Log in to Upstox</strong> once, and <strong>Check connection</strong>.</li>
      </ol>

      <div className="mt-5 grid gap-3 sm:grid-cols-2">
        {upstox.fields.map((field) => (
          <FieldRow
            key={field.name}
            field={field}
            draft={draft[field.name]}
            disabled={disabled}
            onChange={(value) => setDraft({ ...draft, [field.name]: value })}
          />
        ))}
      </div>

      {check && (
        <p
          data-testid="upstox-check"
          className={`mt-4 flex items-center gap-2 rounded-md border p-3 text-sm ${check.ok ? "border-emerald-500/30 bg-emerald-950/20 text-emerald-200" : "border-rose-500/30 bg-rose-950/25 text-rose-100"}`}
        >
          {check.ok ? <CheckCircle2 className="h-4 w-4" /> : <XCircle className="h-4 w-4" />}
          {check.detail}
        </p>
      )}

      {isAdmin && (
        <div className="mt-5 flex flex-col gap-3 border-t border-slate-800 pt-4 lg:flex-row lg:items-end">
          <label className="field-label flex-1">
            Why (kept in the audit history; values are never recorded)
            <input className="field-input mt-1" maxLength={300} value={reason} onChange={(event) => setReason(event.target.value)} placeholder="e.g. new Upstox app" />
          </label>
          <div className="flex flex-wrap gap-2">
            <button className="secondary-button" disabled={busy} onClick={() => void runCheck()}>
              <ShieldCheck className="h-4 w-4" />
              Check connection
            </button>
            <button className="secondary-button" disabled={busy || !upstox.login_ready} onClick={() => void login()}>
              <LogIn className="h-4 w-4" />
              Log in to Upstox
            </button>
            <button className="primary-button" disabled={busy || disabled || !Object.keys(changes).length} onClick={() => void save()}>
              <Save className="h-4 w-4" />
              Save Upstox
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function TelegramCard({
  view,
  isAdmin,
  onSaved,
  onMessage,
}: {
  view: ConnectionsView;
  isAdmin: boolean;
  onSaved: (view: ConnectionsView) => void;
  onMessage: (message: string) => void;
}) {
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const telegram = view.telegram;
  const disabled = !isAdmin || !view.encryption_ready;
  const dirty = Object.values(draft).some((value) => value.trim() !== "");

  async function save() {
    setBusy(true);
    try {
      onSaved(
        await api.saveTelegramConnection({
          bot_token: draft.bot_token ?? "",
          chat_id: draft.chat_id ?? "",
          reason: reason.trim() || null,
        }),
      );
      setDraft({});
      setReason("");
      onMessage("Telegram saved. Send a test alert to check it.");
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Could not save Telegram");
    } finally {
      setBusy(false);
    }
  }

  async function test() {
    setBusy(true);
    try {
      await api.testTelegram();
      onMessage("Test alert sent. Check Telegram.");
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "The test alert failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div data-testid="connection-telegram">
      <div className="flex flex-col gap-3 border-b border-slate-800 pb-4 sm:flex-row sm:items-center sm:justify-between">
        <StatusChip ok={telegram.ready} yes="Alerts configured" no="Alerts not configured" />
      </div>
      <ol className="mt-4 list-decimal space-y-1 pl-5 text-xs leading-5 text-slate-400">
        <li>In Telegram, message @BotFather, send /newbot, and copy the token it gives you.</li>
        <li>Send your new bot any message, then paste your chat ID below (@userinfobot tells you yours).</li>
        <li>Save, then press <strong>Send test alert</strong>.</li>
      </ol>
      <div className="mt-5 grid gap-3 sm:grid-cols-2">
        {telegram.fields.map((field) => (
          <FieldRow
            key={field.name}
            field={field}
            draft={draft[field.name]}
            disabled={disabled}
            onChange={(value) => setDraft({ ...draft, [field.name]: String(value) })}
          />
        ))}
      </div>
      <p className="mt-3 flex items-center gap-2 text-xs text-slate-500">
        <KeyRound className="h-3.5 w-3.5" />
        Approvals and the stop command sent from Telegram still answer only the chat and users set in the server file.
      </p>
      {isAdmin && (
        <div className="mt-5 flex flex-col gap-3 border-t border-slate-800 pt-4 lg:flex-row lg:items-end">
          <label className="field-label flex-1">
            Why (kept in the audit history; values are never recorded)
            <input className="field-input mt-1" maxLength={300} value={reason} onChange={(event) => setReason(event.target.value)} />
          </label>
          <div className="flex flex-wrap gap-2">
            <button className="secondary-button" disabled={busy || !telegram.ready} onClick={() => void test()}>
              Send test alert
            </button>
            <button className="primary-button" disabled={busy || disabled || !dirty} onClick={() => void save()}>
              <Save className="h-4 w-4" />
              Save Telegram
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function StatusChip({ ok, yes, no }: { ok: boolean; yes: string; no: string }) {
  return (
    <span className={`status-pill ${ok ? "status-good" : "status-watch"}`}>
      {ok ? yes : no}
    </span>
  );
}
