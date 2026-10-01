"use client";

import type { ReactNode } from "react";

import type { WorkspaceTab } from "../../lib/navigation";

/**
 * The tabs inside a workspace, and anything that belongs on the same line.
 *
 * Deliberately buttons in a `tablist` rather than styled links: each tab is a
 * view of the workspace already loaded, not a page, and making them links would
 * promise a back-button behaviour the shell does not implement.
 *
 * `actions` is for a control that chooses *what* the tabs are showing rather
 * than which view of it — the Paper/Broker source on Orders & Positions is the
 * one so far. It sits outside the `tablist` element on purpose: a button that
 * is not a tab inside a tablist is read out as one by a screen reader, and
 * announcing "Broker, tab 3 of 3" for a control that does not change tab would
 * be a lie told only to the people who cannot see the layout.
 */
export function WorkspaceTabs({
  tabs,
  active,
  onSelect,
  actions,
}: {
  tabs: WorkspaceTab[];
  active: string;
  onSelect: (tab: string) => void;
  actions?: ReactNode;
}) {
  return (
    <div className="workspace-tabs">
      <div role="tablist" className="flex flex-wrap gap-1">
        {tabs.map((tab) => (
          <button
            key={tab.id}
            role="tab"
            aria-selected={active === tab.id}
            onClick={() => onSelect(tab.id)}
            className={`workspace-tab ${active === tab.id ? "workspace-tab-active" : ""}`}
          >
            {tab.label}
          </button>
        ))}
      </div>
      {actions && <div className="ml-auto flex items-center gap-2 pb-1.5">{actions}</div>}
    </div>
  );
}
