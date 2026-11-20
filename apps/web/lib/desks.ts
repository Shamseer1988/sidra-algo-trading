/**
 * The two trading desks, as the screen sees them.
 *
 * The server owns the switches (Settings → Trading desks) and enforces them:
 * a desk that is off opens nothing new, and its open positions keep their
 * stops and exits until they are closed. This file only decides what to show,
 * which is why hiding a page here never switches anything off.
 *
 * Nothing saved means intraday on and swing off -- what every deployment did
 * before the desks existed -- so a server that predates the switches renders
 * exactly as it always has.
 */

import type { TradingControls } from "../components/api";

export type Desk = "intraday" | "swing";
export type DeskState = { intraday: boolean; swing: boolean };

export function deskState(
  controls: Pick<TradingControls, "intraday_desk_enabled" | "swing_desk_enabled"> | null | undefined,
): DeskState {
  return {
    intraday: controls?.intraday_desk_enabled ?? true,
    swing: controls?.swing_desk_enabled ?? false,
  };
}

/** A screen tagged with a desk shows only while that desk is on; an untagged one always shows. */
export function deskVisible(desk: Desk | undefined, state: DeskState): boolean {
  return desk === undefined ? true : state[desk];
}

export function visibleSections<Item extends { desk?: Desk }, Section extends { items: Item[] }>(
  sections: Section[],
  state: DeskState,
): Section[] {
  return sections
    .map((section) => ({ ...section, items: section.items.filter((item) => deskVisible(item.desk, state)) }))
    .filter((section) => section.items.length > 0);
}

export type DeskNotice = { tone: "info" | "warn" | "bad"; title: string; body: string };

/**
 * What to say above every page, or nothing.
 *
 * Nothing in the normal state (intraday on, swing off). Every other state is
 * one somebody chose and should not forget, so it is stated on every screen in
 * plain words: what stops, and -- the part people worry about -- what does not.
 */
export function deskNotice(state: DeskState): DeskNotice | null {
  if (!state.intraday && !state.swing) {
    return {
      tone: "bad",
      title: "Both trading desks are off",
      body: "No new trades will be opened. Any position still open keeps its stop and is closed on its own rules.",
    };
  }
  if (!state.intraday) {
    return {
      tone: "warn",
      title: "Intraday trading is off",
      body:
        "No new intraday trades. Intraday positions already open keep their stops and are still closed by the " +
        "square-off time. Swing trading is on, but no swing order is sent to the broker yet.",
    };
  }
  if (state.swing) {
    return {
      tone: "info",
      title: "Swing trading is on, but not live yet",
      body:
        "The swing desk is still being built. Until it has its own risk limits, no swing order is sent to the " +
        "broker. Intraday trading is working as normal.",
    };
  }
  return null;
}

/** Plain words for the confirmation step, so a switch never changes on one tap. */
export function switchConsequence(desk: Desk, turningOn: boolean): string {
  if (desk === "intraday") {
    return turningOn
      ? "From the next candle the scanner looks for intraday trades again, within your trading window and limits."
      : "From the next candle no new intraday trade is opened. Positions already open are not abandoned: their stops, targets and the square-off keep working until they close.";
  }
  return turningOn
    ? "The swing desk is switched on, but it is not live yet: no swing order is sent to the broker until it has its own risk limits."
    : "No new swing trades. Holdings already open stay protected by their stops until they are sold.";
}
