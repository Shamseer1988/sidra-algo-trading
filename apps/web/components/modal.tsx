"use client";

import { X } from "lucide-react";
import { type ReactNode, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

/**
 * A popup over the page: every confirmation and every edit form uses it, so a
 * question is always in front of the reader instead of appearing somewhere
 * below the table they clicked in.
 *
 * Esc and the backdrop close it (unless `busy`), focus moves into it and stays
 * there, and the page behind does not scroll.
 *
 * Rendered into document.body through a portal: the glass panels use
 * backdrop-filter, which makes a `position: fixed` child fixed to the panel
 * rather than the window, so a popup opened from inside a table would be
 * clipped to that table's card.
 */
export function Modal({
  title,
  eyebrow,
  tone = "default",
  wide = false,
  busy = false,
  role = "dialog",
  testId,
  onClose,
  children,
  footer,
}: {
  title: ReactNode;
  eyebrow?: ReactNode;
  tone?: "default" | "warning" | "danger";
  wide?: boolean;
  busy?: boolean;
  role?: "dialog" | "alertdialog";
  testId?: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
}) {
  const box = useRef<HTMLDivElement>(null);
  const [mounted, setMounted] = useState(false);

  useEffect(() => setMounted(true), []);

  useEffect(() => {
    if (!mounted) return;
    const previous = document.activeElement as HTMLElement | null;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const first = box.current?.querySelector<HTMLElement>("input, select, textarea, button:not([data-close])");
    (first ?? box.current)?.focus();
    return () => {
      document.body.style.overflow = overflow;
      previous?.focus?.();
    };
  }, [mounted]);

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape" && !busy) onClose();
      if (event.key !== "Tab" || !box.current) return;
      const items = Array.from(
        box.current.querySelectorAll<HTMLElement>("a, button, input, select, textarea, [tabindex]:not([tabindex='-1'])"),
      ).filter((element) => !element.hasAttribute("disabled"));
      if (!items.length) return;
      const [first, last] = [items[0], items[items.length - 1]];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [busy, onClose]);

  const edge = tone === "danger" ? "border-rose-500/60" : tone === "warning" ? "border-amber-500/50" : "border-slate-700";

  if (!mounted) return null;

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/60 p-4 backdrop-blur-sm sm:items-center"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !busy) onClose();
      }}
    >
      <div
        ref={box}
        role={role}
        aria-modal="true"
        aria-label={typeof title === "string" ? title : undefined}
        tabIndex={-1}
        data-testid={testId}
        className={`my-8 w-full ${wide ? "max-w-5xl" : "max-w-lg"} rounded-xl border ${edge} bg-[var(--surface)] shadow-2xl shadow-black/50`}
      >
        <div className="flex items-start justify-between gap-4 border-b border-slate-800 px-5 py-4">
          <div className="min-w-0">
            {eyebrow && <p className="eyebrow">{eyebrow}</p>}
            <h3 className="text-base font-semibold text-slate-900 dark:text-white">{title}</h3>
          </div>
          <button data-close type="button" aria-label="Close" disabled={busy} onClick={onClose} className="text-slate-500 hover:text-slate-200">
            <X className="h-5 w-5" />
          </button>
        </div>
        <div className={`px-5 py-4 ${wide ? "max-h-[70vh] overflow-y-auto" : ""}`}>{children}</div>
        {footer && <div className="flex flex-wrap justify-end gap-2 border-t border-slate-800 px-5 py-3">{footer}</div>}
      </div>
    </div>,
    document.body,
  );
}
