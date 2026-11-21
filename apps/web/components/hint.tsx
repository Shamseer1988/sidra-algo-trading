"use client";

import { Info } from "lucide-react";
import { type ReactNode, useCallback, useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";

/**
 * An explanation behind a small "i": shown on hover, on keyboard focus, and on
 * tap (for a phone, where there is no hover). Esc or a tap elsewhere hides it.
 *
 * Settings used to print every explanation under every box, which made each
 * tab a long scroll. The words are unchanged; they wait behind the icon.
 *
 * Rendered into document.body: the glass panels use backdrop-filter, which
 * would pin a `position: fixed` child to the panel and clip it.
 */
export function Hint({ children, label = "What this means", testId }: { children: ReactNode; label?: string; testId?: string }) {
  const trigger = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  const [pinned, setPinned] = useState(false);
  const [place, setPlace] = useState<{ top: number; left: number; above: boolean } | null>(null);
  const id = useId();

  const measure = useCallback(() => {
    const box = trigger.current?.getBoundingClientRect();
    if (!box) return;
    const width = Math.min(320, window.innerWidth - 24);
    const left = Math.max(12, Math.min(box.left + box.width / 2 - width / 2, window.innerWidth - width - 12));
    const above = box.bottom + 180 > window.innerHeight && box.top > 200;
    setPlace({ top: above ? box.top - 8 : box.bottom + 8, left, above });
  }, []);

  useEffect(() => {
    if (!open) return;
    measure();
    const hide = (event: Event) => {
      if (event instanceof KeyboardEvent && event.key !== "Escape") return;
      if (event.type === "mousedown" && trigger.current?.contains(event.target as Node)) return;
      setOpen(false);
      setPinned(false);
    };
    window.addEventListener("scroll", measure, true);
    window.addEventListener("resize", measure);
    document.addEventListener("keydown", hide);
    document.addEventListener("mousedown", hide);
    return () => {
      window.removeEventListener("scroll", measure, true);
      window.removeEventListener("resize", measure);
      document.removeEventListener("keydown", hide);
      document.removeEventListener("mousedown", hide);
    };
  }, [open, measure]);

  return (
    <>
      <button
        ref={trigger}
        type="button"
        aria-label={label}
        aria-describedby={open ? id : undefined}
        data-testid={testId}
        className="inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-slate-500 transition hover:text-emerald-400 focus:text-emerald-400 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/40"
        onMouseEnter={() => setOpen(true)}
        onMouseLeave={() => !pinned && setOpen(false)}
        onFocus={() => setOpen(true)}
        onBlur={() => !pinned && setOpen(false)}
        onClick={(event) => {
          event.preventDefault();
          event.stopPropagation();
          setPinned(!pinned);
          setOpen(!pinned);
        }}
      >
        <Info className="h-3.5 w-3.5" />
      </button>
      {open &&
        place &&
        createPortal(
          <div
            id={id}
            role="tooltip"
            className="hint-bubble pointer-events-none fixed z-[60] w-[min(320px,calc(100vw-24px))] rounded-lg border border-slate-700 bg-[var(--surface)] px-3 py-2.5 text-xs font-normal normal-case leading-5 tracking-normal text-slate-300 shadow-xl shadow-black/40"
            style={{ top: place.top, left: place.left, transform: place.above ? "translateY(-100%)" : undefined }}
          >
            {children}
          </div>,
          document.body,
        )}
    </>
  );
}
