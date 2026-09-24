"use client";

import { Activity, ChevronDown, ChevronRight, CircleUserRound, LogOut, PanelLeftClose, PanelLeftOpen } from "lucide-react";
import { useState } from "react";

import type { User } from "../api";
import { navigationSections, type WorkspaceId } from "../../lib/navigation";

export function TerminalSidebar({
  active,
  collapsed,
  menuOpen,
  user,
  onSelect,
  onToggle,
  onSignOut,
}: {
  active: WorkspaceId;
  collapsed: boolean;
  menuOpen: boolean;
  user: User;
  onSelect: (workspace: WorkspaceId) => void;
  onToggle: () => void;
  onSignOut: () => void;
}) {
  const [adminOpen, setAdminOpen] = useState(false);
  return (
    <aside className={`sidebar ${menuOpen ? "sidebar-open" : ""} ${collapsed ? "sidebar-collapsed" : ""}`}>
      <div className={`flex items-center gap-3 px-5 py-5 ${collapsed ? "lg:justify-center lg:px-3" : ""}`}>
        <span className="brand-mark"><Activity className="h-5 w-5" /></span>
        <span className={`min-w-0 ${collapsed ? "lg:hidden" : ""}`}>
          <span className="block text-sm font-bold tracking-[.14em] text-white">SIDRA ALGO</span>
          <span className="mt-0.5 block text-[9px] font-semibold tracking-[.18em] text-slate-500">TRADING TERMINAL</span>
        </span>
      </div>

      <nav className="terminal-nav flex-1 overflow-y-auto px-3 pb-4">
        {navigationSections.map((section) => {
          // A collapsible section opens itself when the active screen is inside
          // it, so arriving there from the header or a cross-link never leaves
          // the menu contradicting the page.
          const holdsActive = section.items.some((item) => item.id === active);
          const open = !section.collapsible || adminOpen || holdsActive;
          return (
            <div key={section.label} className="mb-5">
              {section.collapsible ? (
                <button
                  onClick={() => setAdminOpen((value) => !value)}
                  className={`flex w-full items-center gap-1 px-2 pb-2 text-[9px] font-bold uppercase tracking-[.2em] text-slate-600 hover:text-slate-400 ${collapsed ? "lg:hidden" : ""}`}
                  aria-expanded={open}
                >
                  {open ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
                  {section.label}
                </button>
              ) : (
                <p className={`px-2 pb-2 text-[9px] font-bold uppercase tracking-[.2em] text-slate-600 ${collapsed ? "lg:hidden" : ""}`}>
                  {section.label}
                </p>
              )}
              {open && (
                <div className="space-y-1">
                  {section.items.map(({ id, label, icon: Icon }) => (
                    <button
                      key={id}
                      title={collapsed ? label : undefined}
                      onClick={() => onSelect(id)}
                      className={`nav-item w-full ${collapsed ? "lg:justify-center lg:px-2" : ""} ${active === id ? "nav-active" : ""}`}
                    >
                      <Icon className="h-4 w-4 shrink-0" />
                      <span className={`min-w-0 flex-1 truncate text-left ${collapsed ? "lg:hidden" : ""}`}>{label}</span>
                    </button>
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </nav>

      <button title={collapsed ? "Expand sidebar" : "Collapse sidebar"} className="nav-item mx-3 mb-2 hidden lg:flex" onClick={onToggle}>
        {collapsed ? <PanelLeftOpen className="h-4 w-4" /> : <PanelLeftClose className="h-4 w-4" />}
        <span className={collapsed ? "lg:hidden" : ""}>Collapse</span>
      </button>
      <div className="border-t border-slate-800 p-4">
        <div className={`flex items-center gap-3 ${collapsed ? "lg:justify-center" : ""}`}>
          <CircleUserRound className="h-8 w-8 shrink-0 text-slate-500" />
          <div className={`min-w-0 ${collapsed ? "lg:hidden" : ""}`}>
            <p className="truncate text-sm font-medium text-slate-200">{user.email}</p>
            <p className="text-xs text-slate-500">{user.role}</p>
          </div>
        </div>
        <button title="Sign out" data-testid="sign-out-btn" className={`nav-item mt-4 w-full ${collapsed ? "lg:justify-center lg:px-2" : ""}`} onClick={onSignOut}>
          <LogOut className="h-4 w-4" />
          <span className={collapsed ? "lg:hidden" : ""}>Sign out</span>
        </button>
      </div>
    </aside>
  );
}
