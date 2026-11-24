"use client";

import { ChevronLeft, ChevronRight } from "lucide-react";
import { type ReactNode, useState } from "react";

export const PAGE_SIZES = [10, 25, 50, 100];

/** Page controls, for rows already here or pages the server sends. Nothing to turn means nothing drawn. */
export function Pager({
  total,
  page,
  size,
  onPage,
  onSize,
  label = "rows",
}: {
  total: number;
  page: number;
  size: number;
  onPage: (page: number) => void;
  onSize: (size: number) => void;
  label?: string;
}) {
  if (total <= PAGE_SIZES[0]) return null;
  const pages = Math.max(1, Math.ceil(total / size));
  const first = (page - 1) * size + 1;
  return (
    <nav
      aria-label={`Pages of ${label}`}
      className="flex flex-wrap items-center justify-between gap-3 px-5 py-3 text-xs"
      style={{ color: "var(--muted)", borderTop: "1px solid var(--glass-border)" }}
    >
      <span className="numeric">
        {first.toLocaleString("en-IN")} to {Math.min(first + size - 1, total).toLocaleString("en-IN")} of{" "}
        {total.toLocaleString("en-IN")} {label}
      </span>
      <span className="flex flex-wrap items-center gap-2">
        <select
          aria-label="Rows per page"
          className="field-input w-20 py-1 text-xs"
          value={size}
          onChange={(event) => onSize(Number(event.target.value))}
        >
          {PAGE_SIZES.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
        <button
          type="button"
          className="secondary-button px-2.5 py-1 text-xs"
          disabled={page <= 1}
          onClick={() => onPage(page - 1)}
        >
          <ChevronLeft className="h-3.5 w-3.5" />
          Previous
        </button>
        <span className="numeric">
          Page {page} of {pages.toLocaleString("en-IN")}
        </span>
        <button
          type="button"
          className="secondary-button px-2.5 py-1 text-xs"
          disabled={page >= pages}
          onClick={() => onPage(page + 1)}
        >
          Next
          <ChevronRight className="h-3.5 w-3.5" />
        </button>
      </span>
    </nav>
  );
}

/**
 * Rows already in the browser, a page at a time.
 *
 * Wrapped around the table rather than built into one, so every table keeps its
 * own columns. The page is clamped, not reset, when the rows change: a list that
 * refreshes itself must not throw a reader back to the first page.
 */
export function Paged<T>({
  rows,
  label,
  initial = 25,
  children,
}: {
  rows: readonly T[];
  label?: string;
  initial?: number;
  children: (visible: T[]) => ReactNode;
}) {
  const [size, setSize] = useState(initial);
  const [asked, setAsked] = useState(1);
  const page = Math.min(asked, Math.max(1, Math.ceil(rows.length / size)));
  return (
    <>
      {children(rows.slice((page - 1) * size, page * size))}
      <Pager
        total={rows.length}
        page={page}
        size={size}
        onPage={setAsked}
        onSize={(next) => {
          setSize(next);
          setAsked(1);
        }}
        label={label}
      />
    </>
  );
}
