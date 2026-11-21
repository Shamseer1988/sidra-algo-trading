"use client";

import { AlertTriangle, ArrowRight, Power, ShieldCheck } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import {
  api,
  type EffectiveLimits,
  type RiskPreset,
  type SettingSpec,
  type SettingsCatalog,
  type SwingSettingsView,
} from "../../components/api";
import { Modal } from "../../components/modal";
import { switchConsequence, type Desk } from "../../lib/desks";
import { ControlField, dirtyKeys } from "./control-field";
import { IndicatorSettingsForm } from "./indicator-settings-form";
import {
  type Change,
  ChangesModal,
  FieldGrid,
  SaveBar,
  SectionNav,
  SettingsSection,
  Tile,
  shown,
} from "./settings-ui";

/** The group the desk switches live in; drawn as its own card, not as two checkboxes. */
const DESKS_GROUP = "TRADING_DESKS";
const DESK_KEYS: Record<string, Desk> = { intraday_desk_enabled: "intraday", swing_desk_enabled: "swing" };

/** Account-wide: which broker live orders go to, and whether a person approves each one. */
const COMMON_KEYS = ["live_broker", "execution_approval_mode"];
/** How an intraday live entry is sent. */
const ORDER_KEYS = ["live_entry_order_type", "entry_slippage_cap_percent", "entry_slippage_cap_r"];

/**
 * Where each intraday control is shown. Groups come from the server's
 * catalogue; the two key lists above pull the account-wide and order-entry
 * controls out of the "Account and broker" group they are stored in.
 */
type Section = { id: string; label: string; pick: (spec: SettingSpec) => boolean };
const INTRADAY_SECTIONS: Section[] = [
  {
    id: "risk",
    label: "Capital & risk",
    pick: (spec) => spec.group === "ACCOUNT_AND_BROKER" && !COMMON_KEYS.includes(spec.key) && !ORDER_KEYS.includes(spec.key),
  },
  { id: "limits", label: "Daily limits", pick: (spec) => spec.group === "DAILY_RISK" },
  { id: "session", label: "Trading window", pick: (spec) => spec.group === "TRADING_SESSION" },
  { id: "orders", label: "Order entry", pick: (spec) => ORDER_KEYS.includes(spec.key) },
  { id: "signals", label: "Signals & universe", pick: (spec) => spec.group === "SIGNAL_QUALITY" || spec.group === "EXECUTION" },
];
const INDICATORS = "indicators";

/** The rupee figure behind a percent, so the multiplication is not homework. */
function derivedValue(spec: SettingSpec, draft: Record<string, unknown>, effective: EffectiveLimits): string | null {
  const capital = Number(draft.account_capital ?? effective.capital);
  const number = Number(draft[spec.key]);
  if (!Number.isFinite(number) || !Number.isFinite(capital)) return null;
  if (spec.key === "risk_per_trade_percent") return `₹${((capital * number) / 100).toFixed(2)} a trade`;
  if (spec.key === "maximum_daily_risk_percent") return `₹${((capital * number) / 100).toFixed(2)} for the day`;
  if (spec.key === "maximum_open_exposure_percent") {
    const leverage = Number(draft.intraday_leverage_enabled ? draft.intraday_leverage_multiplier ?? 1 : 1);
    return `₹${((capital * number * (Number.isFinite(leverage) ? leverage : 1)) / 100).toFixed(2)} of exposure`;
  }
  if (spec.key === "daily_loss_limit" || spec.key === "daily_profit_target") {
    return capital > 0 ? `${((number * 100) / capital).toFixed(2)}% of capital` : null;
  }
  return null;
}

type Notify = (message: string, kind?: "success" | "error" | "info") => void;

/** Load the catalogue and keep a draft of it; shared by the General and Intraday tabs. */
function useTradingCatalog(onMessage: Notify) {
  const [catalog, setCatalog] = useState<SettingsCatalog | null>(null);
  const [draft, setDraft] = useState<Record<string, unknown>>({});

  const load = useCallback(async () => {
    try {
      const next = await api.settingsCatalog();
      setCatalog(next);
      setDraft(Object.fromEntries(next.settings.map((item) => [item.key, item.value])));
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Could not load trading controls");
    }
  }, [onMessage]);

  useEffect(() => {
    void load();
  }, [load]);

  const dirty = useMemo(() => (catalog ? dirtyKeys(catalog.settings, draft) : []), [catalog, draft]);

  const changes: Change[] = useMemo(
    () =>
      (catalog?.settings ?? [])
        .filter((spec) => dirty.includes(spec.key))
        .map((spec) => ({ key: spec.key, label: spec.label, from: shown(spec.value, spec.unit), to: shown(draft[spec.key], spec.unit) })),
    [catalog, dirty, draft],
  );

  async function save(): Promise<boolean> {
    try {
      await api.updateControls(draft as never);
      onMessage(`Saved ${dirty.length} change${dirty.length === 1 ? "" : "s"}.`, "success");
      window.dispatchEvent(new Event("sidra:controls-saved"));
      await load();
      return true;
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Could not save trading controls", "error");
      return false;
    }
  }

  return { catalog, draft, setDraft, dirty, changes, load, save };
}

function Field({
  spec,
  state,
  isAdmin,
}: {
  spec: SettingSpec;
  state: ReturnType<typeof useTradingCatalog>;
  isAdmin: boolean;
}) {
  const { catalog, draft, setDraft, dirty } = state;
  return (
    <ControlField
      spec={spec}
      value={draft[spec.key]}
      derived={catalog ? derivedValue(spec, draft, catalog.effective) : null}
      changed={dirty.includes(spec.key)}
      disabled={!isAdmin}
      onChange={(next) => setDraft((current) => ({ ...current, [spec.key]: next }))}
    />
  );
}

/**
 * Settings > General: what both desks share. The desk switches, where live
 * orders go and whether each one needs approval, and a short read of each
 * desk's own settings with a way into them.
 */
export function GeneralSettingsForm({
  isAdmin,
  onMessage,
  onOpenTab,
}: {
  isAdmin: boolean;
  onMessage: Notify;
  onOpenTab?: (tab: string) => void;
}) {
  const state = useTradingCatalog(onMessage);
  const { catalog, dirty, changes, load, save } = state;
  const [reviewing, setReviewing] = useState(false);
  const [swing, setSwing] = useState<SwingSettingsView | null>(null);

  useEffect(() => {
    void api.swingSettings().then(setSwing).catch(() => undefined);
  }, []);

  async function saveDesk(key: string, turningOn: boolean): Promise<boolean> {
    if (!catalog || !isAdmin) return false;
    if (dirty.length) {
      onMessage("Save or discard your other changes first, so this switch is saved on its own.");
      return false;
    }
    const saved = Object.fromEntries(catalog.settings.map((item) => [item.key, item.value]));
    try {
      await api.updateControls({ ...saved, [key]: turningOn } as never);
      const label = catalog.settings.find((item) => item.key === key)?.label ?? key;
      onMessage(`${label} is now ${turningOn ? "on" : "off"}.`, "success");
      window.dispatchEvent(new Event("sidra:controls-saved"));
      await load();
      return true;
    } catch (error) {
      onMessage(error instanceof Error ? error.message : "Could not save the switch");
      return false;
    }
  }

  if (!catalog) return <div className="panel mt-6 max-w-6xl p-5"><div className="skeleton h-40" /></div>;

  const deskSpecs = catalog.settings.filter((item) => item.group === DESKS_GROUP && item.key in DESK_KEYS);
  const common = catalog.settings.filter((item) => COMMON_KEYS.includes(item.key));
  const on = (desk: Desk) => deskSpecs.find((spec) => DESK_KEYS[spec.key] === desk)?.value === true;
  const effective = catalog.effective;

  return (
    <section className="mt-6 max-w-6xl space-y-5" data-testid="settings-general">
      {deskSpecs.length > 0 && <DesksCard specs={deskSpecs} isAdmin={isAdmin} onSave={saveDesk} />}

      <SettingsSection
        eyebrow="Both desks"
        title="Live orders"
        hint="Account-wide. Which broker a live order is sent to, and whether each one waits for a person to approve it. The swing desk is paper only and never sends an order."
      >
        <FieldGrid>
          {common.map((spec) => (
            <Field key={spec.key} spec={spec} state={state} isAdmin={isAdmin} />
          ))}
        </FieldGrid>
      </SettingsSection>

      <div className="grid gap-5 lg:grid-cols-2">
        <DeskSummary
          testId="summary-intraday"
          title="Intraday desk"
          on={on("intraday")}
          onOpen={onOpenTab ? () => onOpenTab("intraday") : undefined}
          tiles={[
            ["Capital", `₹${Number(effective.capital).toLocaleString("en-IN")}`],
            ["Risk a trade", `₹${effective.planned_risk_per_trade}`],
            ["Trades a day", `${effective.effective_trade_ceiling}`],
            ["Daily loss stop", `₹${effective.daily_loss_limit}`],
          ]}
        />
        <DeskSummary
          testId="summary-swing"
          title="Swing desk (paper)"
          on={on("swing")}
          onOpen={onOpenTab ? () => onOpenTab("swing") : undefined}
          tiles={
            swing
              ? [
                  ["Capital", `₹${Number(swing.settings.find((s) => s.key === "capital")?.value ?? 0).toLocaleString("en-IN")}`],
                  ["Risk a trade", `₹${swing.effective.risk_per_trade.toLocaleString("en-IN")}`],
                  ["Open positions", `${swing.settings.find((s) => s.key === "max_open_positions")?.value ?? "-"}`],
                  ["Open-risk cap", `₹${swing.effective.heat_cap.toLocaleString("en-IN")}`],
                ]
              : []
          }
        />
      </div>

      {isAdmin && <SaveBar count={dirty.length} onDiscard={() => void load()} onReview={() => setReviewing(true)} />}
      {reviewing && (
        <ChangesModal
          title="Save the account-wide settings?"
          changes={changes}
          warning="These apply to live trading on this account."
          onConfirm={save}
          onClose={() => setReviewing(false)}
        />
      )}
    </section>
  );
}

function DeskSummary({
  title,
  on,
  tiles,
  onOpen,
  testId,
}: {
  title: string;
  on: boolean;
  tiles: [string, string][];
  onOpen?: () => void;
  testId: string;
}) {
  return (
    <SettingsSection
      testId={testId}
      eyebrow="At a glance"
      title={title}
      actions={
        <>
          <span className={`status-pill ${on ? "status-good" : "status-watch"}`}>{on ? "ON" : "OFF"}</span>
          {onOpen && (
            <button type="button" className="secondary-button py-1.5 text-xs" onClick={onOpen}>
              Open settings
              <ArrowRight className="h-3.5 w-3.5" />
            </button>
          )}
        </>
      }
    >
      <div className="grid grid-cols-2 gap-3">
        {tiles.length ? tiles.map(([label, value]) => <Tile key={label} label={label} value={value} />) : <div className="skeleton col-span-2 h-20" />}
      </div>
    </SettingsSection>
  );
}

/**
 * Settings > Intraday trading: capital, risk, limits, window, order entry,
 * signals and indicator periods, one section at a time.
 */
export function TradingControlsForm({ isAdmin, onMessage }: { isAdmin: boolean; onMessage: Notify }) {
  const state = useTradingCatalog(onMessage);
  const { catalog, draft, setDraft, dirty, changes, load, save } = state;
  const [presets, setPresets] = useState<RiskPreset[]>([]);
  const [section, setSection] = useState(INTRADAY_SECTIONS[0].id);
  const [reviewing, setReviewing] = useState(false);

  useEffect(() => {
    void api.riskPresets().then(setPresets).catch(() => undefined);
  }, []);

  if (!catalog) return <div className="panel mt-6 max-w-6xl p-5"><div className="skeleton h-40" /></div>;

  const current = INTRADAY_SECTIONS.find((item) => item.id === section);
  const sectionTabs = [
    ...INTRADAY_SECTIONS.map((item) => ({
      id: item.id,
      label: item.label,
      edited: catalog.settings.filter((spec) => item.pick(spec) && dirty.includes(spec.key)).length,
    })),
    { id: INDICATORS, label: "Indicator periods" },
  ];
  const specs = current ? catalog.settings.filter(current.pick) : [];
  const groups = current ? catalog.group_order.filter((group) => specs.some((spec) => spec.group === group)) : [];

  return (
    <section className="mt-6 max-w-6xl space-y-5" data-testid="settings-intraday">
      <SectionNav sections={sectionTabs} active={section} onSelect={setSection} />

      {section === "risk" && (
        <>
          <EffectiveSummary effective={catalog.effective} />
          <CapitalRescaleNotice
            settings={catalog.settings}
            draft={draft}
            onApply={(values) => setDraft((existing) => ({ ...existing, ...values }))}
          />
        </>
      )}

      {section === INDICATORS ? (
        <IndicatorSettingsForm isAdmin={isAdmin} onMessage={onMessage} embedded />
      ) : (
        groups.map((group) => (
          <SettingsSection
            key={group}
            title={section === "orders" ? "Order entry" : catalog.group_labels[group]}
            eyebrow="Intraday"
          >
            <FieldGrid>
              {specs
                .filter((spec) => spec.group === group)
                .map((spec) => (
                  <Field key={spec.key} spec={spec} state={state} isAdmin={isAdmin} />
                ))}
            </FieldGrid>
          </SettingsSection>
        ))
      )}

      {section === "limits" && presets.length > 0 && (
        <RiskProfiles presets={presets} isAdmin={isAdmin} hasDraft={dirty.length > 0} onMessage={onMessage} onApplied={load} />
      )}

      {isAdmin && section !== INDICATORS && (
        <SaveBar count={dirty.length} onDiscard={() => void load()} onReview={() => setReviewing(true)} />
      )}
      {reviewing && (
        <ChangesModal
          title="Save intraday settings?"
          changes={changes}
          warning="The intraday desk trades real money. Check each line; the time each change applies is in its (i) hint."
          onConfirm={save}
          onClose={() => setReviewing(false)}
        />
      )}
    </section>
  );
}

/**
 * Ready-made risk profiles. Applying one is saved like a hand edit. The server
 * refuses a profile that raises a limit until that is acknowledged; the popup
 * then shows the server's own words and asks again.
 */
function RiskProfiles({
  presets,
  isAdmin,
  hasDraft,
  onMessage,
  onApplied,
}: {
  presets: RiskPreset[];
  isAdmin: boolean;
  hasDraft: boolean;
  onMessage: Notify;
  onApplied: () => Promise<void>;
}) {
  const [asking, setAsking] = useState<RiskPreset | null>(null);
  const [raises, setRaises] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  function close() {
    setAsking(null);
    setRaises(null);
  }

  async function apply(confirmIncrease: boolean) {
    if (!asking) return;
    setBusy(true);
    try {
      await api.applyRiskPreset(asking.key, confirmIncrease);
      onMessage(`Applied ${asking.label}.`, "success");
      await onApplied();
      close();
    } catch (error) {
      const detail = error instanceof Error ? error.message : "Could not apply preset";
      if (!confirmIncrease && detail.includes("raises a risk limit")) setRaises(detail);
      else onMessage(detail, "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <SettingsSection
      eyebrow="Intraday"
      title="Risk profiles"
      hint="Applying a profile saves it the same way a hand edit does: validated, versioned and audited. A profile that raises a limit asks a second time."
    >
      <div className="grid gap-3 md:grid-cols-2">
        {presets.map((preset) => (
          <div key={preset.key} className="glass-inset flex flex-col rounded-md p-3.5">
            <p className="flex items-center gap-1.5 text-sm font-semibold text-white">{preset.label}</p>
            <p className="mt-1 text-xs leading-5 text-slate-400">{preset.description}</p>
            <p className="numeric mt-2 text-xs text-slate-300">
              ₹{preset.effective.planned_risk_per_trade} a trade · ₹{preset.effective.daily_loss_limit} daily stop ·{" "}
              {preset.effective.effective_trade_ceiling} trades
            </p>
            {isAdmin && (
              <button
                type="button"
                className="secondary-button mt-3 self-start py-1.5 text-xs"
                disabled={hasDraft}
                title={hasDraft ? "Save or discard your edits first" : undefined}
                onClick={() => setAsking(preset)}
              >
                <ShieldCheck className="h-4 w-4" />
                Apply {preset.label}
              </button>
            )}
          </div>
        ))}
      </div>
      {asking && (
        <Modal
          title={raises ? `${asking.label} raises a limit` : `Apply ${asking.label}?`}
          eyebrow="Risk profile"
          tone={raises ? "danger" : "warning"}
          role="alertdialog"
          busy={busy}
          testId="preset-modal"
          onClose={close}
          footer={
            <>
              <button type="button" className="secondary-button" disabled={busy} onClick={close}>
                Cancel
              </button>
              <button
                type="button"
                className={raises ? "danger-button" : "primary-button"}
                disabled={busy}
                onClick={() => void apply(Boolean(raises))}
              >
                {raises ? "Yes, raise the limits" : `Apply ${asking.label}`}
              </button>
            </>
          }
        >
          <p className="text-sm leading-6 text-slate-300">{asking.description}</p>
          <p className="numeric mt-3 text-xs text-slate-300">
            ₹{asking.effective.planned_risk_per_trade} a trade · ₹{asking.effective.daily_loss_limit} daily stop ·{" "}
            {asking.effective.effective_trade_ceiling} trades
          </p>
          {raises && (
            <p className="mt-4 flex gap-2 rounded-md border border-rose-500/40 bg-rose-950/25 p-3 text-xs leading-5 text-rose-100">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
              {raises}
            </p>
          )}
        </Modal>
      )}
    </SettingsSection>
  );
}

/** The rupee limits that mean nothing without the capital they are a share of. */
const CAPITAL_LINKED = ["daily_loss_limit", "daily_profit_target"] as const;

/**
 * Capital was edited; the rupee limits did not move.
 *
 * These are stored as absolute rupees on purpose: a daily stop is an amount you
 * are willing to lose today, and one that quietly rescaled when capital was
 * edited would turn a mistyped 100000 into a ten-times-larger loss limit
 * nobody chose. But leaving them alone fails quietly in the other direction,
 * so: say what happened, show both percentages, and offer one button that only
 * fills the draft.
 */
function CapitalRescaleNotice({
  settings,
  draft,
  onApply,
}: {
  settings: SettingSpec[];
  draft: Record<string, unknown>;
  onApply: (values: Record<string, number>) => void;
}) {
  const saved = Number(settings.find((item) => item.key === "account_capital")?.value ?? 0);
  const next = Number(draft.account_capital ?? 0);
  if (!(saved > 0) || !(next > 0) || saved === next) return null;

  const rows = CAPITAL_LINKED.map((key) => {
    const amount = Number(draft[key] ?? 0);
    const spec = settings.find((item) => item.key === key);
    const oldPercent = (amount / saved) * 100;
    const suggested = Math.round((oldPercent / 100) * next);
    return { key, label: spec?.label ?? key, amount, oldPercent, newPercent: (amount / next) * 100, suggested };
  }).filter((row) => row.amount > 0 && row.suggested !== row.amount);

  if (!rows.length) return null;

  return (
    <article className="panel border border-amber-500/30 p-4 sm:p-5">
      <p className="eyebrow text-amber-300">Capital changed</p>
      <h3 className="mt-1 text-base font-semibold text-white">The rupee limits in Daily limits have not moved with it</h3>
      <div className="mt-3 space-y-1.5">
        {rows.map((row) => (
          <p key={row.key} className="numeric text-xs text-slate-300">
            <span className="text-slate-400">{row.label}:</span> ₹{row.amount.toLocaleString("en-IN")} was{" "}
            {row.oldPercent.toFixed(2)}% of capital, now {row.newPercent.toFixed(2)}% — keep {row.oldPercent.toFixed(2)}% with ₹
            {row.suggested.toLocaleString("en-IN")}
          </p>
        ))}
      </div>
      <button onClick={() => onApply(Object.fromEntries(rows.map((row) => [row.key, row.suggested])))} className="secondary-button mt-4">
        Keep the same percentages
      </button>
      <p className="mt-2 text-xs leading-5 text-slate-500">
        This only fills the boxes; nothing is saved until you review and save. Leaving the amounts as they are is a valid choice.
      </p>
    </article>
  );
}

function EffectiveSummary({ effective }: { effective: EffectiveLimits }) {
  const constrained = effective.effective_trade_ceiling < effective.configured_trade_ceiling;
  return (
    <SettingsSection
      eyebrow="What these settings allow"
      title="Effective limits"
      hint="Read together from every control below. A stop is an instruction to the market, not a guarantee: a gap, slippage, or a stop that cannot be placed will all exceed the planned figures."
    >
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Tile label="Planned risk a trade" value={`₹${effective.planned_risk_per_trade}`} note="If the stop is hit" />
        <Tile
          label="Trades today"
          value={`${effective.effective_trade_ceiling}`}
          note={constrained ? `of ${effective.configured_trade_ceiling} configured` : "account-wide, on first fill"}
          tone={constrained ? "warn" : undefined}
        />
        <Tile
          label="Daily loss stop"
          value={`₹${effective.daily_loss_limit}`}
          note={effective.daily_loss_percent ? `${effective.daily_loss_percent}% of capital` : "not set"}
        />
        <Tile label="Exposure ceiling" value={`₹${effective.exposure_ceiling}`} note={`${effective.leverage_multiplier}× leverage`} />
      </div>
      {effective.warnings.length > 0 && (
        <ul className="mt-3 space-y-2">
          {effective.warnings.map((warning) => (
            <li key={warning} className="flex gap-2 rounded-md border border-amber-500/30 bg-amber-950/20 p-3 text-xs leading-5 text-amber-100">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-300" />
              <span>{warning}</span>
            </li>
          ))}
        </ul>
      )}
    </SettingsSection>
  );
}

/**
 * One card per desk: what it does, whether it is on, and a switch that asks
 * first, in a popup that spells out the consequence.
 */
function DesksCard({
  specs,
  isAdmin,
  onSave,
}: {
  specs: SettingSpec[];
  isAdmin: boolean;
  onSave: (key: string, turningOn: boolean) => Promise<boolean>;
}) {
  const [confirming, setConfirming] = useState<SettingSpec | null>(null);
  const [busy, setBusy] = useState(false);
  const asking = confirming ? { desk: DESK_KEYS[confirming.key], on: confirming.value === true } : null;
  return (
    <SettingsSection
      testId="desks-card"
      eyebrow="What this account trades"
      title="Trading desks"
      hint="Switching a desk off stops new trades on it. It never abandons a position that is already open."
    >
      <div className="grid gap-3 lg:grid-cols-2">
        {specs.map((spec) => {
          const desk = DESK_KEYS[spec.key];
          const on = spec.value === true;
          return (
            <div key={spec.key} className="glass-inset flex items-center justify-between gap-3 rounded-md p-3.5" data-testid={`desk-${desk}`}>
              <div className="min-w-0">
                <p className="flex items-center gap-1.5 text-sm font-semibold text-white">
                  {spec.label}
                  <span className={`status-pill ${on ? "status-good" : "status-watch"}`}>{on ? "ON" : "OFF"}</span>
                </p>
                <p className="mt-1 text-xs text-slate-400">{desk === "swing" ? "Paper only — no swing order reaches the broker." : "Live orders, real money."}</p>
              </div>
              {isAdmin && (
                <button onClick={() => setConfirming(spec)} className="secondary-button shrink-0 py-1.5 text-xs">
                  <Power className="h-4 w-4" />
                  {on ? "Turn off" : "Turn on"}
                </button>
              )}
            </div>
          );
        })}
      </div>
      {confirming && asking && (
        <Modal
          title={`Turn ${confirming.label.toLowerCase()} ${asking.on ? "off" : "on"}?`}
          eyebrow="Trading desks"
          tone={asking.on ? "warning" : "default"}
          role="alertdialog"
          busy={busy}
          testId="desk-modal"
          onClose={() => setConfirming(null)}
          footer={
            <>
              <button disabled={busy} onClick={() => setConfirming(null)} className="secondary-button">
                Cancel
              </button>
              <button
                disabled={busy}
                className={asking.on ? "danger-button" : "primary-button"}
                onClick={async () => {
                  setBusy(true);
                  const done = await onSave(confirming.key, !asking.on);
                  setBusy(false);
                  if (done) setConfirming(null);
                }}
              >
                Yes, turn it {asking.on ? "off" : "on"}
              </button>
            </>
          }
        >
          <p className="text-sm leading-6 text-slate-300">{switchConsequence(asking.desk, !asking.on)}</p>
          <p className="mt-2 text-xs leading-5 text-slate-500">{confirming.help}</p>
        </Modal>
      )}
    </SettingsSection>
  );
}
