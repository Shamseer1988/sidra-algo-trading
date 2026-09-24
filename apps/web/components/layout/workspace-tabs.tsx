"use client";

import type { WorkspaceTab } from "../../lib/navigation";

/**
 * The tabs inside a workspace.
 *
 * Deliberately buttons in a `tablist` rather than styled links: each tab is a
 * view of the workspace already loaded, not a page, and making them links would
 * promise a back-button behaviour the shell does not implement.
 */
export function WorkspaceTabs({
  tabs,
  active,
  onSelect,
}: {
  tabs: WorkspaceTab[];
  active: string;
  onSelect: (tab: string) => void;
}) {
  return (
    <div role="tablist" className="workspace-tabs">
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
  );
}
