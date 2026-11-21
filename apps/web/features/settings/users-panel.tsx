"use client";

import { CheckCircle2, Circle, KeyRound, ListChecks, UserPlus, Users } from "lucide-react";
import { FormEvent, useCallback, useEffect, useState } from "react";

import { api, type ChecklistItem, type ManagedUser } from "../../components/api";
import { Modal } from "../../components/modal";
import { formatIstTimestamp } from "../../lib/formatting";

/**
 * Settings > Users, and Settings > Setup checklist.
 *
 * Users: who can open this app and what they may do. An Admin changes
 * settings and people; a Trader operates (start the scanner, square off) but
 * cannot change settings; a Viewer only looks. The server refuses anything
 * that would lock the app out -- switching yourself off, or removing the last
 * administrator -- so the screen only has to explain, not police.
 *
 * Setup checklist: what a new deployment still needs, each step a link to the
 * screen that does it. It reads the same records those screens save, so it
 * cannot drift from them.
 */

const ROLES: { value: ManagedUser["role"]; label: string; help: string }[] = [
  { value: "ADMIN", label: "Admin", help: "Everything, including settings, connections and people." },
  { value: "TRADER", label: "Trader", help: "Operates the desk (scanner, square-off), cannot change settings." },
  { value: "VIEWER", label: "Viewer", help: "Sees everything, changes nothing." },
];

function errorText(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

export function UsersPanel({ isAdmin, onMessage }: { isAdmin: boolean; onMessage: (message: string) => void }) {
  const [rows, setRows] = useState<ManagedUser[]>([]);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<ManagedUser["role"]>("VIEWER");
  const [busy, setBusy] = useState(false);
  const [resetting, setResetting] = useState<ManagedUser | null>(null);
  const [newPassword, setNewPassword] = useState("");
  const [switchingOff, setSwitchingOff] = useState<ManagedUser | null>(null);

  const load = useCallback(() => {
    if (!isAdmin) return;
    void api
      .users()
      .then(setRows)
      .catch((error: unknown) => onMessage(errorText(error, "Could not load the accounts")));
  }, [isAdmin, onMessage]);

  useEffect(load, [load]);

  async function add(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    try {
      const made = await api.addUser({ email, password, role });
      onMessage(`Added ${made.email} as ${made.role.toLowerCase()}. Give them the password yourself; it is not sent anywhere.`);
      setEmail("");
      setPassword("");
      load();
    } catch (error) {
      onMessage(errorText(error, "Could not add the account"));
    } finally {
      setBusy(false);
    }
  }

  async function change(user: ManagedUser, body: { role?: ManagedUser["role"]; is_active?: boolean }) {
    try {
      await api.changeUser(user.id, body);
      onMessage(
        body.is_active === false
          ? `${user.email} is switched off and signed out everywhere.`
          : body.is_active
          ? `${user.email} can sign in again.`
          : `${user.email} is now ${body.role?.toLowerCase()}.`,
      );
    } catch (error) {
      onMessage(errorText(error, "Could not change the account"));
    }
    load();
  }

  async function reset() {
    if (!resetting) return;
    setBusy(true);
    try {
      await api.resetUserPassword(resetting.id, newPassword);
      onMessage(`Password reset for ${resetting.email}; their sessions have ended.`);
      setResetting(null);
      setNewPassword("");
    } catch (error) {
      onMessage(errorText(error, "Could not reset the password"));
    } finally {
      setBusy(false);
    }
    load();
  }

  return (
    <section className="mt-6 space-y-5" data-testid="users-panel">
      {resetting && (
        <Modal
          title={`New password for ${resetting.email}`}
          eyebrow="Users"
          busy={busy}
          testId="reset-password"
          onClose={() => setResetting(null)}
          footer={
            <>
              <button className="secondary-button" disabled={busy} onClick={() => setResetting(null)}>
                Cancel
              </button>
              <button className="primary-button" disabled={busy || newPassword.length < 12} onClick={() => void reset()}>
                Reset password
              </button>
            </>
          }
        >
          <label className="field-label">
            New password (12+ characters)
            <input className="field-input mt-1" type="text" autoComplete="off" value={newPassword} onChange={(event) => setNewPassword(event.target.value)} />
          </label>
          <p className="mt-2 text-xs text-slate-400">Their open sessions end at once. Give them the new password yourself; it is not sent anywhere.</p>
        </Modal>
      )}
      {switchingOff && (
        <Modal
          title={`Switch off ${switchingOff.email}?`}
          eyebrow="Users"
          tone="warning"
          role="alertdialog"
          testId="switch-off-confirm"
          onClose={() => setSwitchingOff(null)}
          footer={
            <>
              <button className="secondary-button" onClick={() => setSwitchingOff(null)}>
                Cancel
              </button>
              <button
                className="danger-button"
                onClick={() => {
                  const user = switchingOff;
                  setSwitchingOff(null);
                  void change(user, { is_active: false });
                }}
              >
                Yes, switch off
              </button>
            </>
          }
        >
          <p className="text-sm text-slate-300">They are signed out everywhere and cannot sign in until switched on again. Nothing they did is removed.</p>
        </Modal>
      )}
      {isAdmin && (
        <article className="panel p-5 sm:p-7">
          <div className="flex items-center gap-3 border-b border-slate-800 pb-4">
            <Users className="h-5 w-5 text-emerald-400" />
            <div>
              <p className="eyebrow">Admin only</p>
              <h3 className="font-semibold text-white">Accounts</h3>
            </div>
          </div>
          <div className="mt-4 overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-left text-xs uppercase tracking-wide text-slate-500">
                <tr>
                  <th className="py-2 pr-4">Email</th>
                  <th className="py-2 pr-4">Role</th>
                  <th className="py-2 pr-4">Status</th>
                  <th className="py-2 pr-4">Added</th>
                  <th className="py-2" />
                </tr>
              </thead>
              <tbody>
                {rows.map((user) => (
                  <tr key={user.id} className="border-t border-slate-800" data-testid="user-row">
                    <td className="py-2 pr-4 text-white">{user.email}</td>
                    <td className="py-2 pr-4">
                      <select
                        aria-label={`Role for ${user.email}`}
                        className="field-input py-1"
                        value={user.role}
                        onChange={(event) => void change(user, { role: event.target.value as ManagedUser["role"] })}
                      >
                        {ROLES.map((item) => (
                          <option key={item.value} value={item.value}>
                            {item.label}
                          </option>
                        ))}
                      </select>
                    </td>
                    <td className="py-2 pr-4">
                      <span className={user.is_active ? "text-emerald-400" : "text-slate-500"}>
                        {user.is_active ? (user.locked ? "Locked (too many tries)" : "Active") : "Switched off"}
                      </span>
                    </td>
                    <td className="py-2 pr-4 text-xs text-slate-400">{formatIstTimestamp(user.created_at)}</td>
                    <td className="py-2 text-right whitespace-nowrap">
                      <button className="secondary-button px-2 py-1 text-xs" onClick={() => { setNewPassword(""); setResetting(user); }}>
                        Reset password
                      </button>{" "}
                      <button
                        className="secondary-button px-2 py-1 text-xs"
                        onClick={() => (user.is_active ? setSwitchingOff(user) : void change(user, { is_active: true }))}
                      >
                        {user.is_active ? "Switch off" : "Switch on"}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <form onSubmit={add} className="mt-6 grid gap-3 border-t border-slate-800 pt-5 sm:grid-cols-[1fr_1fr_auto_auto] sm:items-end">
            <label className="field-label">
              Email
              <input className="field-input mt-1" type="email" required value={email} onChange={(event) => setEmail(event.target.value)} />
            </label>
            <label className="field-label">
              First password (12+ characters)
              <input
                className="field-input mt-1"
                type="text"
                required
                minLength={12}
                autoComplete="off"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
              />
            </label>
            <label className="field-label">
              Role
              <select className="field-input mt-1" value={role} onChange={(event) => setRole(event.target.value as ManagedUser["role"])}>
                {ROLES.map((item) => (
                  <option key={item.value} value={item.value}>
                    {item.label}
                  </option>
                ))}
              </select>
            </label>
            <button className="primary-button" disabled={busy}>
              <UserPlus className="h-4 w-4" />
              Add account
            </button>
          </form>
          <ul className="mt-4 space-y-1 text-xs text-slate-400">
            {ROLES.map((item) => (
              <li key={item.value}>
                <span className="font-semibold text-slate-300">{item.label}:</span> {item.help}
              </li>
            ))}
          </ul>
        </article>
      )}
      <OwnPasswordCard onMessage={onMessage} />
    </section>
  );
}

function OwnPasswordCard({ onMessage }: { onMessage: (message: string) => void }) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [again, setAgain] = useState("");

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (next !== again) return onMessage("The two new passwords are not the same.");
    try {
      await api.changeOwnPassword(current, next);
      onMessage("Password changed. Every session has ended; sign in again with the new one.");
      window.location.assign("/login");
    } catch (error) {
      onMessage(errorText(error, "Could not change the password"));
    }
  }

  return (
    <article className="panel p-5 sm:p-7">
      <div className="flex items-center gap-3 border-b border-slate-800 pb-4">
        <KeyRound className="h-5 w-5 text-emerald-400" />
        <h3 className="font-semibold text-white">My password</h3>
      </div>
      <form onSubmit={save} className="mt-4 grid gap-3 sm:grid-cols-[1fr_1fr_1fr_auto] sm:items-end">
        <label className="field-label">
          Current password
          <input className="field-input mt-1" type="password" required autoComplete="current-password" value={current} onChange={(event) => setCurrent(event.target.value)} />
        </label>
        <label className="field-label">
          New password
          <input className="field-input mt-1" type="password" required minLength={12} autoComplete="new-password" value={next} onChange={(event) => setNext(event.target.value)} />
        </label>
        <label className="field-label">
          New password again
          <input className="field-input mt-1" type="password" required minLength={12} autoComplete="new-password" value={again} onChange={(event) => setAgain(event.target.value)} />
        </label>
        <button className="primary-button">Change password</button>
      </form>
    </article>
  );
}

export function SetupChecklist({
  onMessage,
  onOpen,
}: {
  onMessage: (message: string) => void;
  onOpen?: (workspace: string, tab: string | null) => void;
}) {
  const [items, setItems] = useState<ChecklistItem[] | null>(null);

  useEffect(() => {
    void api
      .setupChecklist()
      .then(setItems)
      .catch((error: unknown) => onMessage(errorText(error, "Could not load the checklist")));
  }, [onMessage]);

  const required = items?.filter((item) => item.required) ?? [];
  const left = required.filter((item) => !item.done).length;

  return (
    <article className="panel mt-6 max-w-4xl p-5 sm:p-7" data-testid="setup-checklist">
      <div className="flex items-center gap-3 border-b border-slate-800 pb-4">
        <ListChecks className="h-5 w-5 text-emerald-400" />
        <div>
          <p className="eyebrow">{items ? (left ? `${left} required step${left === 1 ? "" : "s"} left` : "Ready") : "Checking…"}</p>
          <h3 className="font-semibold text-white">Setting up this app</h3>
        </div>
      </div>
      <ol className="mt-4 space-y-2">
        {items?.map((item, index) => (
          <li key={item.id} className="flex items-start gap-3 rounded-md border border-slate-800 p-3" data-testid={`step-${item.id}`}>
            {item.done ? <CheckCircle2 className="mt-0.5 h-5 w-5 shrink-0 text-emerald-400" /> : <Circle className="mt-0.5 h-5 w-5 shrink-0 text-slate-500" />}
            <div className="min-w-0 flex-1">
              <p className="text-sm font-semibold text-white">
                {index + 1}. {item.title}
                {!item.required && <span className="ml-2 text-xs font-normal text-slate-500">optional</span>}
              </p>
              <p className="mt-0.5 text-xs text-slate-400">{item.detail}</p>
            </div>
            {onOpen && (
              <button className="secondary-button shrink-0 px-2 py-1 text-xs" onClick={() => onOpen(item.workspace, item.tab)}>
                {item.done ? "Review" : "Open"}
              </button>
            )}
          </li>
        ))}
      </ol>
    </article>
  );
}

/** Shown above every page to an administrator until the required setup steps are done. */
export function SetupBanner({ isAdmin, onOpen }: { isAdmin: boolean; onOpen: () => void }) {
  const [left, setLeft] = useState(0);
  useEffect(() => {
    if (!isAdmin) return;
    void api
      .setupChecklist()
      .then((items) => setLeft(items.filter((item) => item.required && !item.done).length))
      .catch(() => setLeft(0));
  }, [isAdmin]);
  if (!isAdmin || !left) return null;
  return (
    <div className="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-md border border-amber-400 bg-amber-50 px-4 py-3 text-sm font-medium text-amber-900 dark:border-amber-700/60 dark:bg-amber-950/30 dark:text-amber-200" data-testid="setup-banner">
      <span>
        Setup is not finished: {left} required step{left === 1 ? "" : "s"} left.
      </span>
      <button className="secondary-button px-2 py-1 text-xs" onClick={onOpen}>
        Open the setup checklist
      </button>
    </div>
  );
}
